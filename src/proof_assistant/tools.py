from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .json_types import JSONObject, as_json_value

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def openai_tools_to_codex(
    tools: Sequence[Mapping[str, object]] | None,
) -> list[JSONObject]:
    """Translate RepoProver/OpenAI function tools to Codex dynamic tools."""
    result: list[JSONObject] = []
    for item in tools or []:
        if item.get("type") != "function":
            continue
        fn = item.get("function")
        if not isinstance(fn, Mapping):
            continue
        name = str(fn.get("name") or "")
        if not _NAME_RE.match(name):
            raise ValueError(
                f"RepoProver tool name {name!r} is not valid for Codex dynamicTools"
            )
        parameters = fn.get("parameters")
        if not isinstance(parameters, Mapping):
            parameters = {"type": "object", "properties": {}}
        result.append(
            {
                "type": "function",
                "name": name,
                "description": str(fn.get("description") or ""),
                "inputSchema": as_json_value(parameters),
            }
        )
    return result


def dynamic_tool_result(value: object, *, success: bool = True) -> JSONObject:
    text = value if isinstance(value, str) else str(value)
    return {
        "contentItems": [{"type": "inputText", "text": text}],
        "success": bool(success),
    }
