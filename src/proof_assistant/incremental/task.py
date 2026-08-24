from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from ..json_types import JSONObject, JSONTypeError, json_object
from ..manuscript import ManuscriptInputError, read_task_file
from .models import TaskPolicy, TaskSpec

DEFAULT_TASK_INSTRUCTIONS = (
    "Verify every claimed lemma, proposition, theorem, corollary, and other "
    "theorem-like statement under its stated assumptions. Preserve distinctions "
    "between verified, ambiguous, unresolved, and false statements. Do not use "
    "sorry, admit, or new axioms."
)


def task_document(instructions: str | None = None) -> str:
    """Return the canonical project-owned verification task document."""
    payload: JSONObject = {
        "schema": 1,
        "mode": "theorem",
        "targets": [],
        "policy": {
            "pause_on_ambiguity": True,
            "preserve_certified": True,
            "counterexample_search": True,
            "require_statement_correspondence_review": False,
        },
        "instructions": (instructions or DEFAULT_TASK_INSTRUCTIONS).strip(),
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def parse_task_text(
    text: str, *, source_name: str = "VERIFY.yaml"
) -> tuple[str, TaskSpec]:
    """Validate task YAML text without requiring an external task file."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        decoded: object = yaml.safe_load(text)
        payload = json_object(decoded, path=source_name)
    except yaml.YAMLError as exc:
        raise ManuscriptInputError(f"Invalid structured task YAML: {exc}") from exc
    except JSONTypeError as exc:
        raise ManuscriptInputError(f"Invalid structured task YAML: {exc}") from exc
    if payload.get("schema") != 1:
        raise ManuscriptInputError(f"{source_name} must be an object with `schema: 1`")
    return digest, _task_spec_from_payload(payload)


def _policy_value(payload: JSONObject, key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ManuscriptInputError(f"Task policy `{key}` must be true or false")
    return value


def _task_spec_from_payload(payload: JSONObject) -> TaskSpec:
    """Construct a validated task spec from a schema-1 mapping."""
    mode = payload.get("mode", "theorem")
    if not isinstance(mode, str) or mode not in {"theorem", "argument-audit"}:
        raise ManuscriptInputError("Task mode must be `theorem` or `argument-audit`")
    targets = payload.get("targets", [])
    if not isinstance(targets, list) or not all(
        isinstance(item, str) and item.strip() for item in targets
    ):
        raise ManuscriptInputError(
            "Task `targets` must be a list of non-empty claim IDs"
        )
    raw_policy = payload.get("policy", {})
    if not isinstance(raw_policy, dict):
        raise ManuscriptInputError("Task `policy` must be an object")
    allowed = {
        "pause_on_ambiguity",
        "preserve_certified",
        "counterexample_search",
        "require_statement_correspondence_review",
    }
    unknown = sorted(set(raw_policy) - allowed)
    if unknown:
        raise ManuscriptInputError("Unknown task policy keys: " + ", ".join(unknown))
    policy = TaskPolicy(
        pause_on_ambiguity=_policy_value(raw_policy, "pause_on_ambiguity", True),
        preserve_certified=_policy_value(raw_policy, "preserve_certified", True),
        counterexample_search=_policy_value(raw_policy, "counterexample_search", True),
        require_statement_correspondence_review=_policy_value(
            raw_policy, "require_statement_correspondence_review", False
        ),
    )
    notes = payload.get("instructions", "")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        raise ManuscriptInputError("Task `instructions` must be a string")
    return TaskSpec(
        mode=mode,
        targets=tuple(target.strip() for target in targets),
        policy=policy,
        free_form=notes,
        source_format="yaml",
    )


def parse_task_file(path: str | Path) -> tuple[Path, str, str, TaskSpec]:
    task_path, text, digest = read_task_file(path)
    if task_path.suffix.casefold() not in {".yaml", ".yml"}:
        return task_path, text, digest, TaskSpec(free_form=text)
    parsed_digest, task = parse_task_text(text, source_name=str(task_path))
    if parsed_digest != digest:
        raise ManuscriptInputError("Task changed while it was being read")
    return task_path, text, digest, task
