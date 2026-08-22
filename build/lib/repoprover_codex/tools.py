from __future__ import annotations

import re
from typing import Any

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def openai_tools_to_codex(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Translate RepoProver/OpenAI function tools to Codex dynamic tools."""
    result: list[dict[str, Any]] = []
    for item in tools or []:
        if item.get("type") != "function":
            continue
        fn = item.get("function") or {}
        name = str(fn.get("name") or "")
        if not _NAME_RE.match(name):
            raise ValueError(
                f"RepoProver tool name {name!r} is not valid for Codex dynamicTools"
            )
        parameters = fn.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        result.append(
            {
                "type": "function",
                "name": name,
                "description": str(fn.get("description") or ""),
                "inputSchema": parameters,
            }
        )
    return result


def dynamic_tool_result(value: Any, *, success: bool = True) -> dict[str, Any]:
    text = value if isinstance(value, str) else str(value)
    return {
        "contentItems": [{"type": "inputText", "text": text}],
        "success": bool(success),
    }
