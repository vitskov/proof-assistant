from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..concurrency import ConcurrencyRuntimeSpec
from .diagnostics import (
    CLARIFICATION_CATEGORIES,
    CLARIFICATION_DIAGNOSTICS,
    REQUIRED_CLARIFICATION_DIAGNOSTICS,
    classify_failure,
)
from .graph import affected_claims, build_graph, canonical_cycles
from .io import atomic_write_json
from .models import ClaimState, ManuscriptEdge
from .session import claim_module_path
from .store import StateStore

LEAN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_'.]*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$")
SEMANTIC_EDGE_KINDS = frozenset(
    {
        "assumption_use",
        "definition_use",
        "formalization_discovered",
        "notation_use",
        "proof_step",
        "semantic",
        "user_confirmed",
    }
)


AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "claim_get",
            "description": "Read authoritative structured metadata and source for one manuscript claim.",
            "parameters": {
                "type": "object",
                "properties": {"claim_id": {"type": "string"}},
                "required": ["claim_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claim_list_dependencies",
            "description": "List deterministic manuscript dependencies of one claim.",
            "parameters": {
                "type": "object",
                "properties": {"claim_id": {"type": "string"}},
                "required": ["claim_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "certificate_query",
            "description": "Read the prior Lean certificate for a manuscript claim, if one exists.",
            "parameters": {
                "type": "object",
                "properties": {"claim_id": {"type": "string"}},
                "required": ["claim_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff_claim",
            "description": "Read the prior/current source-object hashes and current text for a changed claim.",
            "parameters": {
                "type": "object",
                "properties": {"claim_id": {"type": "string"}},
                "required": ["claim_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claim_propose_dependency",
            "description": "Persist a validated semantic manuscript dependency discovered during formalization.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "depends_on": {"type": "string"},
                    "kind": {"type": "string", "enum": sorted(SEMANTIC_EDGE_KINDS)},
                    "reason": {"type": "string"},
                },
                "required": ["claim_id", "depends_on", "kind", "reason"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claim_mark_formalized",
            "description": "Propose a manuscript-to-Lean correspondence for host validation after the independent build.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "lean_declaration": {"type": "string"},
                    "result": {
                        "type": "string",
                        "enum": ["formalized", "counterexample"],
                    },
                },
                "required": ["claim_id", "lean_declaration", "result"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clarification_request",
            "description": "Pause a claim for a genuine manuscript-level ambiguity after technical diagnosis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": sorted(CLARIFICATION_CATEGORIES),
                    },
                    "passage": {"type": "string"},
                    "problem": {"type": "string"},
                    "possible_resolutions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 6,
                    },
                    "diagnostics_performed": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": sorted(CLARIFICATION_DIAGNOSTICS),
                        },
                        "minItems": 2,
                        "uniqueItems": True,
                    },
                },
                "required": [
                    "claim_id",
                    "category",
                    "passage",
                    "problem",
                    "possible_resolutions",
                    "diagnostics_performed",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claim_report_unresolved",
            "description": "Record an inconclusive or technical result without claiming that the theorem is false.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["claim_id", "message"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass(frozen=True)
class IncrementalAgentContext:
    project: Path
    workspace: Path
    run_id: int
    snapshot: str
    previous_snapshot: str | None
    allowed_claims: frozenset[str]
    require_correspondence_review: bool = False
    pause_on_ambiguity: bool = True
    counterexample_search: bool = True
    concurrency: ConcurrencyRuntimeSpec = ConcurrencyRuntimeSpec()
    admission_timeout: float = 3600.0

    @property
    def database(self) -> Path:
        return self.project / ".repoprover" / "state.sqlite3"

    def _claim(
        self, store: StateStore, claim_id: str, *, require_allowed: bool = False
    ):
        if require_allowed and claim_id not in self.allowed_claims:
            raise ValueError(f"Claim is outside this proof batch: {claim_id}")
        row = store.claim_row(claim_id)
        if row is None or bool(row["retired"]):
            raise ValueError(f"Unknown current manuscript claim: {claim_id}")
        return row

    def claim_get(self, arguments: dict[str, Any]) -> str:
        claim_id = str(arguments.get("claim_id") or "")
        with StateStore(self.database) as store:
            row = self._claim(store, claim_id)
            version = store.claim_version(self.snapshot, claim_id)
            assert version is not None
            return json.dumps(
                {
                    "claim_id": claim_id,
                    "kind": row["kind"],
                    "state": row["status"],
                    "source_file": version["source_file"],
                    "label": version["label"],
                    "statement": version["statement_text"],
                    "proof": version["proof_text"],
                    "lean_file": claim_module_path(claim_id).as_posix(),
                    "prior_lean_declaration": row["lean_declaration"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )

    def claim_list_dependencies(self, arguments: dict[str, Any]) -> str:
        claim_id = str(arguments.get("claim_id") or "")
        with StateStore(self.database) as store:
            self._claim(store, claim_id)
            dependencies = [
                {
                    "claim_id": row["dst"],
                    "kind": row["edge_kind"],
                    "provenance": row["provenance"],
                    "state": (
                        store.claim_row(str(row["dst"])) or {"status": "missing"}
                    )["status"],
                }
                for row in store.manuscript_edges()
                if row["src"] == claim_id
            ]
            return json.dumps(dependencies, sort_keys=True)

    def certificate_query(self, arguments: dict[str, Any]) -> str:
        claim_id = str(arguments.get("claim_id") or "")
        with StateStore(self.database) as store:
            self._claim(store, claim_id)
            row = store.certificate(claim_id)
            return json.dumps(dict(row) if row else None, sort_keys=True)

    def git_diff_claim(self, arguments: dict[str, Any]) -> str:
        claim_id = str(arguments.get("claim_id") or "")
        with StateStore(self.database) as store:
            self._claim(store, claim_id)
            current = store.claim_version(self.snapshot, claim_id)
            previous = (
                store.claim_version(self.previous_snapshot, claim_id)
                if self.previous_snapshot
                else None
            )
            fields = ("statement_hash", "proof_hash", "statement_text", "proof_text")
            return json.dumps(
                {
                    "previous": {field: previous[field] for field in fields}
                    if previous
                    else None,
                    "current": {field: current[field] for field in fields}
                    if current
                    else None,
                },
                ensure_ascii=False,
                sort_keys=True,
            )

    def claim_propose_dependency(self, arguments: dict[str, Any]) -> str:
        claim_id = str(arguments.get("claim_id") or "")
        dependency = str(arguments.get("depends_on") or "")
        kind = str(arguments.get("kind") or "")
        reason = str(arguments.get("reason") or "").strip()
        if kind not in SEMANTIC_EDGE_KINDS:
            raise ValueError(f"Unsupported semantic edge kind: {kind}")
        if not reason:
            raise ValueError("Dependency proposal requires a reason")
        if claim_id == dependency:
            raise ValueError("A claim cannot depend on itself")
        with StateStore(self.database) as store:
            self._claim(store, claim_id, require_allowed=True)
            self._claim(store, dependency)
            existing_edges = [
                ManuscriptEdge(
                    str(row["src"]),
                    str(row["dst"]),
                    str(row["edge_kind"]),
                    str(row["provenance"]),
                    bool(row["approved"]),
                )
                for row in store.manuscript_edges()
            ]
            all_ids = {str(row["claim_id"]) for row in store.current_claim_rows()}
            proposed = ManuscriptEdge(
                claim_id, dependency, kind, "formalization_discovered"
            )
            cycles = canonical_cycles(build_graph(all_ids, [*existing_edges, proposed]))
            if cycles:
                raise ValueError(
                    "Dependency proposal would create a cycle: "
                    + "; ".join(" -> ".join(cycle) for cycle in cycles)
                )
            store.add_manuscript_edge(
                proposed,
                snapshot=self.snapshot,
            )
            store.add_diagnostic(
                run_id=self.run_id,
                claim_id=claim_id,
                category="semantic_dependency",
                message=reason,
                details={"depends_on": dependency, "kind": kind},
            )
        return f"Recorded {claim_id} -> {dependency} ({kind})"

    def claim_mark_formalized(self, arguments: dict[str, Any]) -> str:
        claim_id = str(arguments.get("claim_id") or "")
        declaration = str(arguments.get("lean_declaration") or "")
        result = str(arguments.get("result") or "")
        if result not in {"formalized", "counterexample"}:
            raise ValueError("Result must be formalized or counterexample")
        if result == "counterexample" and not self.counterexample_search:
            raise ValueError("Counterexample search is disabled by the task policy")
        if not LEAN_NAME_RE.fullmatch(declaration) or not declaration.startswith(
            "ManuscriptVerification."
        ):
            raise ValueError(
                "Lean declaration must be a valid ManuscriptVerification name"
            )
        with StateStore(self.database) as store:
            self._claim(store, claim_id, require_allowed=True)
            store.set_correspondence(
                claim_id,
                declaration,
                run_id=self.run_id,
                status=(
                    "counterexample"
                    if result == "counterexample"
                    else (
                        "proposed_review"
                        if self.require_correspondence_review
                        else "proposed"
                    )
                ),
                approved=(
                    result == "counterexample" or not self.require_correspondence_review
                ),
            )
            if self.require_correspondence_review and result != "counterexample":
                store.set_claim_state(
                    claim_id,
                    ClaimState.STATEMENT_DRAFTED,
                    run_id=self.run_id,
                    action="await_correspondence_review",
                    reason="Task policy requires human approval of the formal statement correspondence",
                )
        return "Correspondence recorded for independent host validation"

    def clarification_request(self, arguments: dict[str, Any]) -> str:
        claim_id = str(arguments.get("claim_id") or "")
        category = str(arguments.get("category") or "")
        passage = str(arguments.get("passage") or "").strip()
        problem = str(arguments.get("problem") or "").strip()
        resolutions = arguments.get("possible_resolutions")
        diagnostics = arguments.get("diagnostics_performed")
        if not self.pause_on_ambiguity:
            raise ValueError(
                "Clarification pauses are disabled by the task policy; "
                "record an unresolved result instead"
            )
        if category not in CLARIFICATION_CATEGORIES:
            raise ValueError(
                f"Not a manuscript-level clarification category: {category}"
            )
        if not problem or not passage:
            raise ValueError("Clarification passage and problem must be non-empty")
        if (
            not isinstance(resolutions, list)
            or not 1 <= len(resolutions) <= 6
            or not all(isinstance(item, str) and item.strip() for item in resolutions)
        ):
            raise ValueError(
                "Clarification resolutions must contain 1-6 non-empty strings"
            )
        if not isinstance(diagnostics, list) or not all(
            isinstance(item, str) and item in CLARIFICATION_DIAGNOSTICS
            for item in diagnostics
        ):
            raise ValueError("Clarification diagnostics are invalid")
        missing_diagnostics = REQUIRED_CLARIFICATION_DIAGNOSTICS - set(diagnostics)
        if missing_diagnostics:
            raise ValueError(
                "Clarification requires prior diagnostics: "
                + ", ".join(sorted(missing_diagnostics))
            )
        with StateStore(self.database) as store:
            self._claim(store, claim_id, require_allowed=True)
            version = store.claim_version(self.snapshot, claim_id)
            assert version is not None
            source = str(version["statement_text"]) + "\n" + str(version["proof_text"])
            if passage not in source:
                raise ValueError(
                    "Quoted clarification passage is not in the indexed claim source"
                )
            edge_rows = store.manuscript_edges()
            all_ids = {str(row["claim_id"]) for row in store.current_claim_rows()}
            edges = [
                ManuscriptEdge(
                    str(row["src"]),
                    str(row["dst"]),
                    str(row["edge_kind"]),
                    str(row["provenance"]),
                    bool(row["approved"]),
                )
                for row in edge_rows
            ]
            blocking = sorted(
                affected_claims({claim_id}, claim_ids=all_ids, edges=edges)
            )
            question_id = store.create_question(
                claim_id=claim_id,
                snapshot=self.snapshot,
                category=category,
                passage=passage,
                problem=problem,
                possible_resolutions=resolutions,
                blocking_claims=blocking,
                run_id=self.run_id,
            )
            store.add_diagnostic(
                run_id=self.run_id,
                claim_id=claim_id,
                category="clarification_diagnostic_pipeline",
                message="Manuscript clarification requested after required diagnostics",
                details={"performed": sorted(set(diagnostics))},
            )
            store.set_claim_state(
                claim_id,
                ClaimState.NEEDS_CLARIFICATION,
                run_id=self.run_id,
                action="clarification",
                reason=f"{question_id}: {problem}",
            )
        return f"Created clarification {question_id}; the host will preserve completed work"

    def claim_report_unresolved(self, arguments: dict[str, Any]) -> str:
        claim_id = str(arguments.get("claim_id") or "")
        message = str(arguments.get("message") or "").strip()
        if not message:
            raise ValueError("Unresolved diagnostic must be non-empty")
        category = classify_failure(message)
        if category in {
            "lean_syntax",
            "mathlib_lookup",
            "missing_import",
            "typeclass_failure",
        }:
            state = ClaimState.FAILED_TECHNICAL
        elif category == "possible_counterexample":
            state = ClaimState.SUSPECT_FALSE
        else:
            state = ClaimState.UNRESOLVED
        with StateStore(self.database) as store:
            self._claim(store, claim_id, require_allowed=True)
            store.set_claim_state(
                claim_id,
                state,
                run_id=self.run_id,
                action="unresolved",
                reason=message,
            )
            store.add_diagnostic(
                run_id=self.run_id,
                claim_id=claim_id,
                category=category,
                message=message,
            )
            if state == ClaimState.FAILED_TECHNICAL:
                store.add_failure_incident(
                    run_id=self.run_id,
                    scope="CLAIM",
                    failure_kind="CLAIM_TECHNICAL",
                    phase="PROOF_BATCH",
                    category=category,
                    message=message,
                    provenance="agent.claim_report_unresolved",
                    claim_ids=(claim_id,),
                    retryable=True,
                )
        return f"Recorded {category}; no falsity conclusion was made"


class IncrementalToolsMixin:
    _incremental_context: IncrementalAgentContext

    def register_tools(self, defs: dict[str, dict], handlers: dict[str, Any]) -> None:
        super().register_tools(defs, handlers)  # type: ignore[misc]
        self._register_tools_from_list(AGENT_TOOLS, defs, handlers)

    def _handle_claim_get(self, arguments: dict[str, Any]) -> str:
        return self._incremental_context.claim_get(arguments)

    def _handle_claim_list_dependencies(self, arguments: dict[str, Any]) -> str:
        return self._incremental_context.claim_list_dependencies(arguments)

    def _handle_certificate_query(self, arguments: dict[str, Any]) -> str:
        return self._incremental_context.certificate_query(arguments)

    def _handle_git_diff_claim(self, arguments: dict[str, Any]) -> str:
        return self._incremental_context.git_diff_claim(arguments)

    def _handle_claim_propose_dependency(self, arguments: dict[str, Any]) -> str:
        return self._incremental_context.claim_propose_dependency(arguments)

    def _handle_claim_mark_formalized(self, arguments: dict[str, Any]) -> str:
        return self._incremental_context.claim_mark_formalized(arguments)

    def _handle_clarification_request(self, arguments: dict[str, Any]) -> str:
        return self._incremental_context.clarification_request(arguments)

    def _handle_claim_report_unresolved(self, arguments: dict[str, Any]) -> str:
        return self._incremental_context.claim_report_unresolved(arguments)


INCREMENTAL_SYSTEM_PROMPT = """\
You are the proof-search worker inside Proof Assistant's deterministic
incremental manuscript verifier. Lean is the proof authority. The host, not
you, owns claim identity, source snapshots, graph propagation, certificates,
and invalidation.

Read `.repoprover-agent/CURRENT_BATCH.json` and `RepoProverInput/TASK.md` fully.
Use `claim_get` for each assigned claim and `claim_list_dependencies` before
formalizing it. Work only on assigned claim modules under
`Formalization/Claims/`; do not edit `manuscript/`, `.repoprover/`,
`Formalization/All.lean`, certificate exports, or status/report files.

For every successful claim, create a faithful declaration under the
`ManuscriptVerification` namespace, run `lean_check`, run the relevant Lake
build, then call `claim_mark_formalized`. This call is only a proposal: the
host independently rebuilds the project, extracts the elaborated declaration
from Lean's environment, checks proof dependencies and axioms, and decides
whether a certificate is valid. Never use `sorry`, `admit`, a new axiom, or an
inconsistent hypothesis to manufacture a result.

If source wording changed, compare it with the prior certificate and update
the formal statement faithfully. The host will reuse the old proof only when
the newly reviewed, elaborated formal type is structurally identical.

Before asking the author, diagnose syntax/import/API/typeclass issues, search
Mathlib, try an independent formalization, decompose the proof, and check
assumption sufficiency. Use `clarification_request` only for a genuine
manuscript-level ambiguity or missing fact. Use `claim_report_unresolved` for
inconclusive proof search; inability to prove is not evidence of falsity. A
counterexample result must itself be represented by a kernel-checked Lean
declaration and reported with `claim_mark_formalized(result="counterexample")`.

Commit completed Lean changes. End with exactly one marker on its own line:
`-- VERIFIED`, `-- NEEDS CLARIFICATION`, `-- COUNTEREXAMPLE`, or `-- UNRESOLVED`.
"""


def create_incremental_agent(
    workspace: Path,
    *,
    context: IncrementalAgentContext,
    claims: Sequence[str],
):
    try:
        from repoprover.agents.contributor import ContributorAgent, ContributorTask
    except ImportError as exc:
        raise RuntimeError(
            "RepoProver is not importable in the active Python environment"
        ) from exc

    class IncrementalManuscriptAgent(IncrementalToolsMixin, ContributorAgent):
        agent_type = "incremental_manuscript_verifier"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._incremental_context = context
            super().__init__(*args, **kwargs)

        def get_system_prompt(self) -> str:
            policy = (
                "Clarification pauses are enabled."
                if context.pause_on_ambiguity
                else (
                    "Clarification pauses are disabled; record ambiguity as unresolved."
                )
            )
            counterexamples = (
                "Kernel-checked counterexample search is enabled."
                if context.counterexample_search
                else "Counterexample search and submissions are disabled."
            )
            return f"{INCREMENTAL_SYSTEM_PROMPT}\n{policy}\n{counterexamples}\n"

        def build_user_prompt(self, **_kwargs: Any) -> str:
            rendered = "\n".join(f"- `{claim}`" for claim in claims)
            return f"""\
Verify or reconcile exactly this ready dependency-frontier batch:

{rendered}

Read every assigned claim through the structured host tools. Preserve already
certified dependencies and do not work on claims outside this batch. Put each
claim's formal counterpart in the exact module path returned by `claim_get`.
"""

    return IncrementalManuscriptAgent(
        task=ContributorTask.fix(),
        repo_root=workspace,
    )


def write_batch_context(
    workspace: Path,
    *,
    run_id: int,
    snapshot: str,
    claims: Sequence[str],
    pause_on_ambiguity: bool,
    counterexample_search: bool,
    concurrency: Mapping[str, Any],
    admission_timeout: float,
) -> Path:
    path = workspace / ".repoprover-agent" / "CURRENT_BATCH.json"
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "run_id": run_id,
            "snapshot": snapshot,
            "claims": list(claims),
            "policy": {
                "pause_on_ambiguity": pause_on_ambiguity,
                "counterexample_search": counterexample_search,
            },
            "resource_admission": {
                "timeout_seconds": admission_timeout,
                "configured": concurrency["configured"],
                "effective": concurrency["effective"],
            },
        },
    )
    return path
