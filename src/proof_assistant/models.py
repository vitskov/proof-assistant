from __future__ import annotations

from .json_types import JSONObject


def model_id(entry: JSONObject) -> str:
    return str(
        entry.get("model")
        or entry.get("id")
        or entry.get("slug")
        or entry.get("name")
        or ""
    )


def supported_efforts(entry: JSONObject) -> list[str]:
    raw = (
        entry.get("supportedReasoningEfforts")
        or entry.get("supported_reasoning_efforts")
        or []
    )
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, dict):
            value = (
                item.get("reasoningEffort")
                or item.get("reasoning_effort")
                or item.get("effort")
                or item.get("value")
                or item.get("name")
            )
            if value:
                values.append(str(value))
    return values


def validate_model_effort(
    catalog: list[JSONObject],
    *,
    model: str,
    effort: str,
) -> None:
    if not model:
        raise ValueError("An explicit Codex model is required")
    match = next((entry for entry in catalog if model_id(entry) == model), None)
    if match is None:
        available = ", ".join(filter(None, (model_id(x) for x in catalog)))
        raise ValueError(
            f"Codex model {model!r} is not advertised by model/list. "
            f"Available: {available or '<none>'}"
        )
    efforts = supported_efforts(match)
    if efforts and effort not in efforts:
        raise ValueError(
            f"Reasoning effort {effort!r} is not advertised for {model!r}. "
            f"Supported: {', '.join(efforts)}"
        )
