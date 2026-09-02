from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..ai.execution import AIBackend, AIBackendConfig
from ..backend import CodexBackend, CodexConfig
from ..incremental.io import atomic_write_json, canonical_hash
from ..incremental.store import StateStore
from ..workflow.contracts import (
    ClarificationPresentation,
    SourceLocation,
    contract_dict,
)


class ClarificationNarrator(Protocol):
    """Narrow replaceable boundary for prose generation only."""

    @property
    def name(self) -> str: ...

    def narrate(self, facts: Mapping[str, object]) -> Mapping[str, object]: ...


class IsolatedCodexClarificationNarrator:
    """Optional Codex prose generator with MCP/apps/plugins/skills isolated."""

    def __init__(self, config: CodexConfig, *, cwd: Path) -> None:
        if not config.isolate_external_tools:
            raise ValueError(
                "Clarification Codex must isolate external tools and skills"
            )
        self.config = config
        self.cwd = cwd.resolve()

    @property
    def name(self) -> str:
        return f"codex:{self.config.model}"

    def narrate(self, facts: Mapping[str, object]) -> Mapping[str, object]:
        backend = CodexBackend(self.config, cwd=self.cwd)
        try:
            result = backend.run(
                system_prompt=(
                    "You rewrite an already-authorized mathematical clarification "
                    "request for a manuscript author. Return one JSON object with only "
                    "headline, explanation, and requested_actions. Do not add facts, "
                    "change the quoted passage, source location, claim, diagnosis, "
                    "blocked claims, or possible resolutions. requested_actions must be "
                    "an array of short strings. Do not use Markdown fences."
                ),
                user_prompt=json.dumps(dict(facts), ensure_ascii=False, sort_keys=True),
                tools=[],
                tool_handler=lambda _name, _arguments: "Tools are disabled",
            )
        finally:
            backend.close()
        payload = json.loads(result.final_text)
        if not isinstance(payload, dict):
            raise ValueError("Codex clarification response must be a JSON object")
        return payload


class IsolatedAIClarificationNarrator:
    """Provider-neutral narration-only driver using no dynamic tools."""

    def __init__(self, config: AIBackendConfig, *, cwd: Path) -> None:
        self.config = config
        self.cwd = cwd.resolve()

    @property
    def name(self) -> str:
        return f"{self.config.driver_id.value}:{self.config.model}"

    def narrate(self, facts: Mapping[str, object]) -> Mapping[str, object]:
        backend = AIBackend(self.config, cwd=self.cwd)
        try:
            result = backend.run(
                system_prompt=(
                    "You rewrite an already-authorized mathematical clarification "
                    "request for a manuscript author. Return one JSON object with only "
                    "headline, explanation, and requested_actions. Do not add facts, "
                    "change the quoted passage, source location, claim, diagnosis, "
                    "blocked claims, or possible resolutions. requested_actions must be "
                    "an array of short strings. Do not use Markdown fences."
                ),
                user_prompt=json.dumps(dict(facts), ensure_ascii=False, sort_keys=True),
                tools=[],
                tool_handler=lambda _name, _arguments: "Tools are disabled",
            )
        finally:
            backend.close()
        payload = json.loads(result.final_text)
        if not isinstance(payload, dict):
            raise ValueError("AI clarification response must be a JSON object")
        return payload


# Public product-facing name; the implementation remains a narration-only boundary.
CodexClarificationPresenter = IsolatedCodexClarificationNarrator


def _line_column(text: str, offset: int) -> tuple[int, int]:
    bounded = min(max(offset, 0), len(text))
    line = text.count("\n", 0, bounded) + 1
    prior_newline = text.rfind("\n", 0, bounded)
    return line, bounded - prior_newline


def _source_location(
    *, project: Path, source_root: Path, store: StateStore, question: Mapping[str, Any]
) -> SourceLocation:
    snapshot = str(question["snapshot_commit"])
    claim_id = str(question["claim_id"])
    version = store.claim_version(snapshot, claim_id)
    if version is None:
        raise ValueError(
            f"Question {question['question_id']} has no source claim version"
        )
    relative = str(version["source_file"])
    immutable_path = project / "manuscript" / relative
    text = immutable_path.read_text(encoding="utf-8")
    statement_start = int(version["statement_start"])
    statement_end = int(version["statement_end"])
    passage = str(question["passage"])
    found = text.find(passage, statement_start, statement_end)
    start = found if found >= 0 else statement_start
    end = start + len(passage) if found >= 0 else statement_end
    start_line, start_column = _line_column(text, start)
    end_line, end_column = _line_column(text, end)
    lines = text.splitlines()
    context_start = max(1, start_line - 4)
    context_end = min(len(lines), max(end_line, start_line) + 4)
    excerpt = "\n".join(lines[context_start - 1 : context_end])
    return SourceLocation(
        relative_path=relative,
        absolute_path=(source_root / relative).resolve(strict=False),
        start_line=start_line,
        end_line=end_line,
        start_column=start_column,
        end_column=end_column,
        context_start_line=context_start,
        context_end_line=context_end,
        excerpt=excerpt,
        highlighted_lines=tuple(range(start_line, max(start_line, end_line) + 1)),
        snapshot_commit=snapshot,
    )


class ClarificationPresenter:
    def __init__(self, narrator: ClarificationNarrator | None = None) -> None:
        self.narrator = narrator

    @staticmethod
    def _presentation_path(project: Path) -> Path:
        return project / ".repoprover" / "presentations" / "clarifications.json"

    @staticmethod
    def _write_presentations(
        project: Path, presentations: Sequence[ClarificationPresentation]
    ) -> None:
        atomic_write_json(
            ClarificationPresenter._presentation_path(project),
            {
                "schema_version": 1,
                "clarifications": [
                    contract_dict(presentation) for presentation in presentations
                ],
            },
        )

    def present_all(
        self, project: Path, source_root: Path
    ) -> tuple[ClarificationPresentation, ...]:
        database = project / ".repoprover" / "state.sqlite3"
        with StateStore(database) as store:
            presentations = tuple(
                self._present(project, source_root, store, dict(question))
                for question in store.open_questions()
            )
        self._write_presentations(project, presentations)
        return presentations

    def load_or_present_all(
        self, project: Path, source_root: Path
    ) -> tuple[ClarificationPresentation, ...]:
        """Load durable presentations, rebuilding locally when they are stale.

        Reopening a project must never make an external AI request. Narration is
        generated at the verification-result boundary and persisted there; a
        missing or stale cache is reconstructed with the deterministic presenter.
        """

        database = project / ".repoprover" / "state.sqlite3"
        with StateStore(database) as store:
            questions = tuple(dict(question) for question in store.open_questions())
            persisted = self._load_presentations(
                project, source_root, store, questions
            )
        if persisted is not None:
            return persisted
        return ClarificationPresenter().present_all(project, source_root)

    def _load_presentations(
        self,
        project: Path,
        source_root: Path,
        store: StateStore,
        questions: Sequence[Mapping[str, Any]],
    ) -> tuple[ClarificationPresentation, ...] | None:
        try:
            payload = json.loads(
                self._presentation_path(project).read_text(encoding="utf-8")
            )
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or set(payload) != {"schema_version", "clarifications"}
            ):
                return None
            raw_presentations = payload.get("clarifications")
            if (
                not isinstance(raw_presentations, list)
                or len(raw_presentations) != len(questions)
            ):
                return None
            presentations = tuple(
                self._decode_presentation(item) for item in raw_presentations
            )
            for question, presentation in zip(questions, presentations, strict=True):
                resolutions, blocked, location, facts = self._presentation_inputs(
                    project, source_root, store, question
                )
                if (
                    presentation.question_id != str(question["question_id"])
                    or presentation.claim_id != str(question["claim_id"])
                    or presentation.category != str(question["category"])
                    or presentation.possible_resolutions != resolutions
                    or presentation.blocked_claims != blocked
                    or presentation.location != location
                    or presentation.provenance_sha256
                    != canonical_hash(
                        {
                            "facts": facts,
                            "generated_by": presentation.generated_by,
                            "location": location.excerpt,
                        }
                    )
                ):
                    return None
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return presentations

    @staticmethod
    def _decode_presentation(payload: object) -> ClarificationPresentation:
        if not isinstance(payload, dict) or set(payload) != {
            "question_id",
            "claim_id",
            "category",
            "headline",
            "explanation",
            "requested_actions",
            "possible_resolutions",
            "location",
            "blocked_claims",
            "generated_by",
            "provenance_sha256",
        }:
            raise ValueError("Invalid persisted clarification presentation")

        def text(key: str) -> str:
            value = payload[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Invalid persisted clarification field: {key}")
            return value

        def strings(key: str) -> tuple[str, ...]:
            value = payload[key]
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(f"Invalid persisted clarification field: {key}")
            return tuple(value)

        location_payload = payload["location"]
        if not isinstance(location_payload, dict) or set(location_payload) != {
            "relative_path",
            "absolute_path",
            "start_line",
            "end_line",
            "start_column",
            "end_column",
            "context_start_line",
            "context_end_line",
            "excerpt",
            "highlighted_lines",
            "snapshot_commit",
        }:
            raise ValueError("Invalid persisted clarification source location")

        def location_text(key: str) -> str:
            value = location_payload[key]
            if not isinstance(value, str):
                raise ValueError(f"Invalid persisted source-location field: {key}")
            return value

        def location_integer(key: str) -> int:
            value = location_payload[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Invalid persisted source-location field: {key}")
            return int(value)

        highlighted = location_payload["highlighted_lines"]
        if not isinstance(highlighted, list) or not all(
            isinstance(line, int) and not isinstance(line, bool) for line in highlighted
        ):
            raise ValueError("Invalid persisted highlighted source lines")
        location = SourceLocation(
            relative_path=location_text("relative_path"),
            absolute_path=Path(location_text("absolute_path")),
            start_line=location_integer("start_line"),
            end_line=location_integer("end_line"),
            start_column=location_integer("start_column"),
            end_column=location_integer("end_column"),
            context_start_line=location_integer("context_start_line"),
            context_end_line=location_integer("context_end_line"),
            excerpt=location_text("excerpt"),
            highlighted_lines=tuple(highlighted),
            snapshot_commit=location_text("snapshot_commit"),
        )
        headline, explanation, requested_actions = (
            ClarificationPresenter._validate_narration(
                {
                    "headline": text("headline"),
                    "explanation": text("explanation"),
                    "requested_actions": strings("requested_actions"),
                }
            )
        )
        return ClarificationPresentation(
            question_id=text("question_id"),
            claim_id=text("claim_id"),
            category=text("category"),
            headline=headline,
            explanation=explanation,
            requested_actions=requested_actions,
            possible_resolutions=strings("possible_resolutions"),
            location=location,
            blocked_claims=strings("blocked_claims"),
            generated_by=text("generated_by"),
            provenance_sha256=text("provenance_sha256"),
        )

    @staticmethod
    def _presentation_inputs(
        project: Path,
        source_root: Path,
        store: StateStore,
        question: Mapping[str, Any],
    ) -> tuple[tuple[str, ...], tuple[str, ...], SourceLocation, dict[str, object]]:
        resolutions = tuple(json.loads(str(question["resolutions_json"])))
        blocked = tuple(json.loads(str(question["blocking_claims_json"])))
        if not all(isinstance(item, str) for item in (*resolutions, *blocked)):
            raise ValueError("Clarification resolutions and blockers must be strings")
        location = _source_location(
            project=project,
            source_root=source_root,
            store=store,
            question=question,
        )
        facts: dict[str, object] = {
            "question_id": str(question["question_id"]),
            "claim_id": str(question["claim_id"]),
            "category": str(question["category"]),
            "passage": str(question["passage"]),
            "problem": str(question["problem"]),
            "possible_resolutions": resolutions,
            "blocked_claims": blocked,
            "source_file": location.relative_path,
            "start_line": location.start_line,
            "end_line": location.end_line,
        }
        version = store.claim_version(
            str(question["snapshot_commit"]), str(question["claim_id"])
        )
        if version is not None and str(version["assistant_context"]).strip():
            facts["assistant_context"] = str(version["assistant_context"])
        return resolutions, blocked, location, facts

    def _present(
        self,
        project: Path,
        source_root: Path,
        store: StateStore,
        question: Mapping[str, Any],
    ) -> ClarificationPresentation:
        resolutions, blocked, location, facts = self._presentation_inputs(
            project, source_root, store, question
        )
        headline = f"Clarification needed for {facts['claim_id']}"
        explanation = str(question["problem"])
        requested_actions: tuple[str, ...] = (
            f"Edit {location.relative_path} at lines "
            f"{location.start_line}–{location.end_line}.",
            "Make the intended mathematical meaning explicit, then save all source files.",
        )
        generated_by = "deterministic"
        if self.narrator is not None:
            try:
                proposed = self.narrator.narrate(facts)
                headline, explanation, requested_actions = self._validate_narration(
                    proposed
                )
                generated_by = self.narrator.name
            except Exception:
                # Presentation must never become unavailable because prose generation did.
                generated_by = "deterministic-fallback"
        provenance = canonical_hash(
            {"facts": facts, "generated_by": generated_by, "location": location.excerpt}
        )
        return ClarificationPresentation(
            question_id=str(question["question_id"]),
            claim_id=str(question["claim_id"]),
            category=str(question["category"]),
            headline=headline,
            explanation=explanation,
            requested_actions=requested_actions,
            possible_resolutions=resolutions,
            location=location,
            blocked_claims=blocked,
            generated_by=generated_by,
            provenance_sha256=provenance,
        )

    @staticmethod
    def _validate_narration(
        payload: Mapping[str, Any],
    ) -> tuple[str, str, tuple[str, ...]]:
        if set(payload) != {"headline", "explanation", "requested_actions"}:
            raise ValueError("Narration changed the strict response schema")
        headline = payload["headline"]
        explanation = payload["explanation"]
        actions = payload["requested_actions"]
        if not isinstance(headline, str) or not headline.strip() or len(headline) > 160:
            raise ValueError("Invalid clarification headline")
        if (
            not isinstance(explanation, str)
            or not explanation.strip()
            or len(explanation) > 4000
        ):
            raise ValueError("Invalid clarification explanation")
        if (
            not isinstance(actions, Sequence)
            or isinstance(actions, (str, bytes))
            or not 1 <= len(actions) <= 6
            or not all(isinstance(action, str) and action.strip() for action in actions)
        ):
            raise ValueError("Invalid requested actions")
        return (
            headline.strip(),
            explanation.strip(),
            tuple(action.strip() for action in actions),
        )
