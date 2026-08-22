import queue

import pytest

from repoprover_codex.backend import CodexBackend, CodexConfig
from repoprover_codex.protocol import CodexProtocolError, CodexServerExited


TOOL = {
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


class ScenarioClient:
    def __init__(
        self,
        events=None,
        tool_params=None,
        initialize_error=None,
        external_servers=None,
        local_skills=None,
    ):
        self.handlers = {}
        self.events = list(
            events
            if events is not None
            else [
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed"}},
                }
            ]
        )
        self.tool_params = tool_params
        self.initialize_error = initialize_error
        self.external_servers = list(external_servers or [])
        self.local_skills = list(local_skills or [])
        self.tool_result = None

    def register_request_handler(self, method, handler):
        self.handlers[method] = handler

    def start(self):
        pass

    def close(self):
        pass

    def notify(self, method, params=None):
        pass

    def request(self, method, params=None, timeout=0):
        if method == "initialize":
            if self.initialize_error:
                raise self.initialize_error
            return {"serverInfo": {"name": "fake"}}
        if method == "model/list":
            return {
                "data": [
                    {
                        "model": "gpt-test",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                        ],
                    }
                ]
            }
        if method == "mcpServerStatus/list":
            return {"data": self.external_servers, "nextCursor": None}
        if method == "skills/list":
            return {
                "data": [
                    {"cwd": "/tmp", "skills": self.local_skills, "errors": []}
                ]
            }
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            if self.tool_params is not None:
                self.tool_result = self.handlers["item/tool/call"](self.tool_params)
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(method)

    def next_notification(self, timeout=None):
        if not self.events:
            raise queue.Empty
        event = self.events.pop(0)
        if event == "timeout":
            raise queue.Empty
        return event


def run_scenario(client, handler=lambda name, args: "Compiles successfully"):
    backend = CodexBackend(
        CodexConfig(
            model="gpt-test",
            effort="low",
            request_timeout=0.01,
            turn_timeout=0.01,
        ),
        client=client,
    )
    return backend.run(
        system_prompt="system",
        user_prompt="user",
        tools=[TOOL],
        tool_handler=handler,
    )


def test_unauthenticated_codex_remains_provider_failure():
    client = ScenarioClient(
        initialize_error=CodexProtocolError("authentication required; run codex login")
    )
    with pytest.raises(CodexProtocolError, match="authentication required"):
        run_scenario(client)


def test_external_tool_isolation_fails_closed():
    client = ScenarioClient(
        external_servers=[
            {
                "name": "unexpected-app",
                "tools": {"write": {"name": "write", "inputSchema": {}}},
                "resources": [],
                "resourceTemplates": [],
            }
        ]
    )
    with pytest.raises(CodexProtocolError, match="unexpected-app"):
        run_scenario(client)


def test_local_skill_isolation_fails_closed():
    client = ScenarioClient(
        local_skills=[
            {
                "name": "unexpected-skill",
                "path": "/tmp/unexpected/SKILL.md",
                "enabled": True,
            }
        ]
    )
    with pytest.raises(CodexProtocolError, match="unexpected-skill"):
        run_scenario(client)


def test_turn_timeout_is_not_treated_as_unproved_theorem():
    with pytest.raises(TimeoutError, match="did not complete"):
        run_scenario(ScenarioClient(events=["timeout"]))


@pytest.mark.parametrize("status", ["cancelled", "interrupted", "failed"])
def test_abnormal_turn_status_is_provider_failure(status):
    event = {
        "method": "turn/completed",
        "params": {"turn": {"id": "turn-1", "status": status}},
    }
    with pytest.raises(CodexProtocolError, match=status):
        run_scenario(ScenarioClient(events=[event]))


def test_app_server_crash_during_turn_is_immediate():
    event = {
        "method": "_repoprover_codex/server_exited",
        "params": {"returncode": 9},
    }
    with pytest.raises(CodexServerExited, match="exited during turn"):
        run_scenario(ScenarioClient(events=[event]))


def test_dynamic_tool_exception_is_returned_and_recorded():
    client = ScenarioClient(
        tool_params={"tool": "lean_check", "arguments": {"code": "bad"}}
    )

    def fail(_name, _arguments):
        raise RuntimeError("Lean worker died")

    result = run_scenario(client, fail)
    assert client.tool_result["success"] is False
    assert result.tool_calls[0].success is False
    assert "Lean worker died" in result.tool_calls[0].result


@pytest.mark.parametrize("arguments", ["not-json", [], 42])
def test_malformed_dynamic_tool_arguments_fail_closed(arguments):
    client = ScenarioClient(
        tool_params={"tool": "lean_check", "arguments": arguments}
    )
    result = run_scenario(client)
    assert client.tool_result["success"] is False
    assert result.tool_calls[0].success is False
    assert "expected JSON object" in result.tool_calls[0].result


def test_lean_tool_error_is_distinct_from_provider_completion():
    client = ScenarioClient(
        tool_params={"tool": "lean_check", "arguments": {"code": "bad"}}
    )
    result = run_scenario(client, lambda _name, _args: "Error: Lean rejected code")
    assert client.tool_result["success"] is False
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].success is False
    assert result.tool_calls[0].result == "Error: Lean rejected code"
