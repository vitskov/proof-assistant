from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

import proof_assistant.ai.execution as execution
from proof_assistant.ai.config import MachineProviderConfigStore
from proof_assistant.ai.contracts import CredentialSource, Difficulty, DriverId
from proof_assistant.ai.execution import (
    AdmittedToolHost,
    AIBackend,
    AIBackendConfig,
    ProviderAuthenticationRequired,
    ProviderExecutionError,
    _LoopbackToolServer,
)
from proof_assistant.ai.runtime import (
    CommandResult,
    EnvironmentCredentialStore,
    HttpResponse,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lean_check",
            "description": "Check Lean code",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    }
]


class FakeHttp:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, Mapping[str, str], object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float = 30.0,
    ) -> HttpResponse:
        del timeout_seconds
        payload = json.loads(body) if body else None
        self.requests.append((method, url, headers, payload))
        return self.responses.pop(0)


class FakeCommand:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], str | None, Mapping[str, str]]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_text: str | None = None,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del timeout_seconds
        self.calls.append((argv, input_text, dict(env or {})))
        return self.result


def response(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode())


def credentials(
    driver: DriverId, key: str = "provider-secret"
) -> EnvironmentCredentialStore:
    variable = {
        DriverId.OPENAI_API: "OPENAI_API_KEY",
        DriverId.ANTHROPIC_API: "ANTHROPIC_API_KEY",
        DriverId.GEMINI_API: "GEMINI_API_KEY",
    }[driver]
    return EnvironmentCredentialStore({variable: key})


def test_codex_driver_receives_minimal_environment(monkeypatch, tmp_path: Path) -> None:
    observed = {}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-cli-boundary")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-cross-cli-boundary")

    class FakeCodexBackend:
        def __init__(self, config, *, cwd):
            del cwd
            observed["config"] = config

        def run(self, **kwargs):
            del kwargs
            return type(
                "Result",
                (),
                {
                    "final_text": "done",
                    "thread_id": "thread",
                    "turn_id": "turn",
                    "model": "gpt-test",
                    "effort": "high",
                    "events": [],
                    "tool_calls": [],
                },
            )()

        def close(self):
            return None

    monkeypatch.setattr(execution, "CodexBackend", FakeCodexBackend)
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.CODEX_CLI,
            model="gpt-test",
            difficulty=Difficulty.HIGH,
        )
    )
    result = backend.run(
        system_prompt="system",
        user_prompt="prove",
        tools=[],
        tool_handler=lambda name, arguments: "unused",
    )
    config = observed["config"]
    assert result.final_text == "done"
    assert not config.inherit_environment
    assert config.environment["HOME"] == str(tmp_path)
    assert config.environment["PATH"] == "/usr/bin"
    assert "OPENAI_API_KEY" not in config.environment
    assert "ANTHROPIC_API_KEY" not in config.environment


def test_openai_api_function_loop_returns_through_host(tmp_path: Path) -> None:
    http = FakeHttp(
        [
            response(
                {
                    "id": "resp-1",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "lean_check",
                            "call_id": "call-1",
                            "arguments": '{"code":"example : True := by trivial"}',
                        }
                    ],
                }
            ),
            response({"id": "resp-2", "output_text": "complete"}),
        ]
    )
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.OPENAI_API,
            model="gpt-test",
            difficulty=Difficulty.HIGH,
            provider_config_path=tmp_path / "providers.json",
        ),
        http_runner=http,
        credential_store=credentials(DriverId.OPENAI_API),
    )

    result = backend.run(
        system_prompt="system",
        user_prompt="prove",
        tools=TOOLS,
        tool_handler=lambda name, args: f"{name}:{args['code']}:ok",
    )

    assert result.final_text == "complete"
    assert result.driver == "openai_api"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].success
    second_input = http.requests[1][3]["input"]  # type: ignore[index]
    assert "previous_response_id" not in http.requests[1][3]  # type: ignore[operator]
    assert http.requests[0][3]["store"] is False  # type: ignore[index]
    assert http.requests[0][3]["include"] == [  # type: ignore[index]
        "reasoning.encrypted_content"
    ]
    assert second_input[1]["type"] == "function_call"  # type: ignore[index]
    assert second_input[2]["type"] == "function_call_output"  # type: ignore[index]
    assert "provider-secret" not in json.dumps(
        [request[3] for request in http.requests]
    )


@pytest.mark.parametrize(
    ("difficulty", "expected_reasoning"),
    [
        (Difficulty.AUTO, None),
        (Difficulty.NONE, {"effort": "none"}),
    ],
)
def test_openai_api_distinguishes_auto_from_explicit_none(
    tmp_path: Path,
    difficulty: Difficulty,
    expected_reasoning: object,
) -> None:
    http = FakeHttp([response({"id": "resp", "output_text": "complete"})])
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.OPENAI_API,
            model="gpt-5.6-sol",
            difficulty=difficulty,
            provider_config_path=tmp_path / "providers.json",
        ),
        http_runner=http,
        credential_store=credentials(DriverId.OPENAI_API),
    )

    result = backend.run(
        system_prompt="system",
        user_prompt="prove",
        tools=(),
        tool_handler=lambda _name, _args: "unused",
    )

    payload = http.requests[0][3]
    assert isinstance(payload, dict)
    assert payload.get("reasoning") == expected_reasoning
    assert result.effort == difficulty.value


def test_api_credential_source_none_fails_closed_without_environment_fallback(
    tmp_path: Path,
) -> None:
    config_store = MachineProviderConfigStore(tmp_path / "providers.json")
    settings = config_store.load()
    drivers = tuple(
        replace(preference, credential_source=CredentialSource.NONE)
        if preference.driver is DriverId.OPENAI_API
        else preference
        for preference in settings.config.drivers
    )
    config_store.save(
        replace(settings.config, drivers=drivers),
        expected_revision=settings.revision,
    )
    http = FakeHttp([])
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.OPENAI_API,
            model="gpt-5.6-sol",
            provider_config_path=config_store.path,
        ),
        http_runner=http,
        credential_store=credentials(
            DriverId.OPENAI_API, "must-not-be-used-when-source-is-none"
        ),
    )

    with pytest.raises(ProviderAuthenticationRequired, match="no configured"):
        backend.run(
            system_prompt="system",
            user_prompt="prove",
            tools=(),
            tool_handler=lambda _name, _args: "unused",
        )

    assert http.requests == []


def test_anthropic_api_uses_native_tool_result_and_effort(tmp_path: Path) -> None:
    http = FakeHttp(
        [
            response(
                {
                    "id": "msg-1",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "lean_check",
                            "input": {"code": "#check Nat"},
                        }
                    ],
                }
            ),
            response({"id": "msg-2", "content": [{"type": "text", "text": "done"}]}),
        ]
    )
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.ANTHROPIC_API,
            model="claude-test",
            difficulty=Difficulty.XHIGH,
            provider_config_path=tmp_path / "providers.json",
        ),
        http_runner=http,
        credential_store=credentials(DriverId.ANTHROPIC_API),
    )
    result = backend.run(
        system_prompt="system",
        user_prompt="prove",
        tools=TOOLS,
        tool_handler=lambda _name, _args: "ok",
    )
    assert result.final_text == "done"
    first_payload = http.requests[0][3]
    second_payload = http.requests[1][3]
    assert first_payload["output_config"] == {"effort": "xhigh"}  # type: ignore[index]
    assert second_payload["messages"][-1]["content"][0]["type"] == "tool_result"  # type: ignore[index]


def test_gemini_api_maps_high_and_rejects_non_native_xhigh(tmp_path: Path) -> None:
    http = FakeHttp(
        [response({"candidates": [{"content": {"parts": [{"text": "verified"}]}}]})]
    )
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.GEMINI_API,
            model="gemini-3-test",
            difficulty=Difficulty.HIGH,
            provider_config_path=tmp_path / "providers.json",
        ),
        http_runner=http,
        credential_store=credentials(DriverId.GEMINI_API),
    )
    result = backend.run(
        system_prompt="system",
        user_prompt="prove",
        tools=TOOLS,
        tool_handler=lambda _name, _args: "ok",
    )
    assert result.final_text == "verified"
    assert http.requests[0][3]["generationConfig"] == {  # type: ignore[index]
        "thinkingConfig": {"thinkingLevel": "high"}
    }

    rejected = AIBackend(
        AIBackendConfig(
            driver=DriverId.GEMINI_API,
            model="gemini-3-test",
            difficulty=Difficulty.XHIGH,
            provider_config_path=tmp_path / "providers.json",
        ),
        http_runner=FakeHttp([]),
        credential_store=credentials(DriverId.GEMINI_API),
    )
    with pytest.raises(ProviderExecutionError, match="does not support"):
        rejected.run(
            system_prompt="system",
            user_prompt="prove",
            tools=TOOLS,
            tool_handler=lambda _name, _args: "ok",
        )


def test_gemini_25_difficulty_maps_to_numeric_thinking_budget(
    tmp_path: Path,
) -> None:
    http = FakeHttp(
        [response({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})]
    )
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.GEMINI_API,
            model="gemini-2.5-pro",
            difficulty=Difficulty.HIGH,
            provider_config_path=tmp_path / "providers.json",
        ),
        http_runner=http,
        credential_store=credentials(DriverId.GEMINI_API),
    )
    backend.run(
        system_prompt="system",
        user_prompt="prove",
        tools=TOOLS,
        tool_handler=lambda _name, _args: "ok",
    )
    assert http.requests[0][3]["generationConfig"] == {  # type: ignore[index]
        "thinkingConfig": {"thinkingBudget": 32_768}
    }


def test_provider_runtime_rejects_invalid_driver_difficulty_before_traffic(
    tmp_path: Path,
) -> None:
    http = FakeHttp([])
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.ANTHROPIC_API,
            model="claude-opus-4-6",
            difficulty=Difficulty.NONE,
            provider_config_path=tmp_path / "providers.json",
        ),
        http_runner=http,
        credential_store=credentials(DriverId.ANTHROPIC_API),
    )
    with pytest.raises(ProviderExecutionError, match="does not support difficulty"):
        backend.run(
            system_prompt="system",
            user_prompt="prove",
            tools=TOOLS,
            tool_handler=lambda _name, _args: "ok",
        )
    assert http.requests == []


@pytest.mark.parametrize(
    ("driver", "stdout"),
    [
        (
            DriverId.CLAUDE_CLI,
            json.dumps({"result": "done", "session_id": "claude-session"}),
        ),
        (
            DriverId.COPILOT_CLI,
            json.dumps({"sessionId": "copilot-session", "content": "done"}) + "\n",
        ),
    ],
)
def test_cli_drivers_are_isolated_and_do_not_put_prompts_in_argv(
    tmp_path: Path, driver: DriverId, stdout: str
) -> None:
    command = FakeCommand(CommandResult(0, stdout, ""))
    backend = AIBackend(
        AIBackendConfig(
            driver=driver, model="model", provider_config_path=tmp_path / "p.json"
        ),
        command_runner=command,
    )
    result = backend.run(
        system_prompt="private-system-prompt",
        user_prompt="private-user-prompt",
        tools=TOOLS,
        tool_handler=lambda _name, _args: "ok",
    )
    argv, input_text, environment = command.calls[0]
    assert result.final_text == "done"
    assert "private-system-prompt" not in argv
    assert "private-user-prompt" not in argv
    assert environment["PROOF_ASSISTANT_MCP_TOKEN"] not in argv
    if driver is DriverId.CLAUDE_CLI:
        assert "--strict-mcp-config" in argv
        assert "mcp__proof_assistant__*" in argv
        assert input_text == "private-user-prompt"
    else:
        assert "--disable-builtin-mcps" in argv
        assert "--no-custom-instructions" in argv
        assert "--available-tools=proof_assistant" in argv
        assert "--allow-tool=proof_assistant" in argv
        assert input_text is None


def test_claude_cli_accepts_current_json_event_array(tmp_path: Path) -> None:
    stdout = json.dumps(
        [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "claude-session",
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": "claude-session",
                "result": "done",
            },
        ]
    )
    command = FakeCommand(CommandResult(0, stdout, ""))
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.CLAUDE_CLI,
            model="fable",
            difficulty=Difficulty.XHIGH,
            provider_config_path=tmp_path / "providers.json",
        ),
        command_runner=command,
    )

    result = backend.run(
        system_prompt="system",
        user_prompt="prove",
        tools=TOOLS,
        tool_handler=lambda _name, _args: "ok",
    )

    assert result.final_text == "done"
    assert result.thread_id == "claude-session"
    assert result.model == "fable"
    assert result.effort == "xhigh"
    assert [event["type"] for event in result.events] == ["system", "result"]
    argv = command.calls[0][0]
    assert argv[argv.index("--model") + 1] == "fable"
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_claude_cli_surfaces_expired_auth_without_dumping_event_transcript(
    tmp_path: Path,
) -> None:
    stdout = json.dumps(
        [
            {"type": "system", "subtype": "init", "session_id": "session"},
            {
                "type": "result",
                "error": "authentication_failed",
                "result": "Failed to authenticate: OAuth session expired",
            },
        ]
    )
    command = FakeCommand(CommandResult(1, stdout, "non-fatal update warning"))
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.CLAUDE_CLI,
            model="haiku",
            difficulty=Difficulty.LOW,
            provider_config_path=tmp_path / "providers.json",
        ),
        command_runner=command,
    )

    with pytest.raises(ProviderAuthenticationRequired) as caught:
        backend.run(
            system_prompt="system",
            user_prompt="prove",
            tools=TOOLS,
            tool_handler=lambda _name, _args: "ok",
        )

    assert "OAuth session expired" in str(caught.value)
    assert '"type": "system"' not in str(caught.value)
    assert "update warning" not in str(caught.value)


@pytest.mark.parametrize(
    "message",
    (
        "Not logged in — Please run /login",
        "Login expired — Please run /login",
        "Missing auth token",
    ),
)
def test_claude_cli_current_login_errors_require_authentication(
    tmp_path: Path, message: str
) -> None:
    command = FakeCommand(CommandResult(1, "", message))
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.CLAUDE_CLI,
            model="haiku",
            difficulty=Difficulty.LOW,
            provider_config_path=tmp_path / "providers.json",
        ),
        command_runner=command,
    )

    with pytest.raises(ProviderAuthenticationRequired, match="login|logged in|token"):
        backend.run(
            system_prompt="system",
            user_prompt="prove",
            tools=TOOLS,
            tool_handler=lambda _name, _args: "ok",
        )


def test_stdio_mcp_bridge_forwards_only_declared_tools(tmp_path: Path) -> None:
    host = AdmittedToolHost(
        lambda name, args: f"{name}:{args['code']}",
        ["lean_check"],
        concurrency=None,
        timeout=30,
    )
    server = _LoopbackToolServer(host)
    server.start()
    tools_path = tmp_path / "tools.json"
    tools_path.write_text(json.dumps(TOOLS), encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "PROOF_ASSISTANT_MCP_HOST": server.address[0],
            "PROOF_ASSISTANT_MCP_PORT": str(server.address[1]),
            "PROOF_ASSISTANT_MCP_TOKEN": server.token,
            "PROOF_ASSISTANT_MCP_TOOLS": str(tools_path),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "proof_assistant.ai.mcp_bridge"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        initialized = json.loads(process.stdout.readline())
        assert initialized["result"]["serverInfo"]["name"] == "proof-assistant-tools"
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "lean_check", "arguments": {"code": "ok"}},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        called = json.loads(process.stdout.readline())
        assert called["result"]["content"][0]["text"] == "lean_check:ok"
        assert len(host.calls) == 1
    finally:
        process.stdin.close()
        process.wait(timeout=10)
        server.close()
