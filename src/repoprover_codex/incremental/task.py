from __future__ import annotations

from pathlib import Path

import yaml

from ..manuscript import ManuscriptInputError, read_task_file
from .models import TaskPolicy, TaskSpec


def parse_task_file(path: str | Path) -> tuple[Path, str, str, TaskSpec]:
    task_path, text, digest = read_task_file(path)
    if task_path.suffix.casefold() not in {".yaml", ".yml"}:
        return task_path, text, digest, TaskSpec(free_form=text)
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManuscriptInputError(f"Invalid structured task YAML: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ManuscriptInputError(
            "Structured task YAML must be an object with `schema: 1`"
        )
    mode = payload.get("mode", "theorem")
    if mode not in {"theorem", "argument-audit"}:
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
    for key, value in raw_policy.items():
        if not isinstance(value, bool):
            raise ManuscriptInputError(f"Task policy `{key}` must be true or false")
    policy = TaskPolicy(**raw_policy)
    notes = payload.get("instructions", "")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        raise ManuscriptInputError("Task `instructions` must be a string")
    return (
        task_path,
        text,
        digest,
        TaskSpec(
            mode=mode,
            targets=tuple(target.strip() for target in targets),
            policy=policy,
            free_form=notes,
            source_format="yaml",
        ),
    )
