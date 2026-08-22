import pytest

from repoprover_codex.tools import dynamic_tool_result, openai_tools_to_codex


def test_translate_openai_function_tool():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lean_check",
                "description": "Check Lean",
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        }
    ]
    result = openai_tools_to_codex(tools)
    assert result == [
        {
            "type": "function",
            "name": "lean_check",
            "description": "Check Lean",
            "inputSchema": tools[0]["function"]["parameters"],
        }
    ]


def test_invalid_dynamic_tool_name_fails_closed():
    tools = [
        {
            "type": "function",
            "function": {"name": "bad tool name", "parameters": {}},
        }
    ]
    with pytest.raises(ValueError):
        openai_tools_to_codex(tools)


def test_dynamic_result_shape():
    assert dynamic_tool_result("ok") == {
        "contentItems": [{"type": "inputText", "text": "ok"}],
        "success": True,
    }
