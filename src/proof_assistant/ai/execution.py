"""Provider-neutral proof-turn execution with one shared tool authority.

Coding-agent CLIs run with their built-in mutation/execution surface disabled
and receive only an ephemeral Proof Assistant MCP server.  Direct API drivers
execute provider-native function-calling loops in this process.  In both cases
every RepoProver tool invocation returns through :class:`AdmittedToolHost`.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..backend import CodexBackend, CodexConfig
from ..concurrency import AITaskClass, ConcurrencyRuntime, ConcurrencyRuntimeSpec
from ..json_types import JSONObject, JSONValue, json_object
from .catalog import driver_definition
from .config import MachineProviderConfigStore
from .contracts import (
    CredentialSource,
    Difficulty,
    DriverId,
    ModelDescriptor,
    TaskKind,
)
from .runtime import (
    CommandRunner,
    CompositeCredentialStore,
    CredentialStore,
    HttpResponse,
    HttpRunner,
    SubprocessCommandRunner,
    UrllibHttpRunner,
)


class ProviderExecutionError(RuntimeError):
    """A redacted provider invocation failure."""


class ProviderAuthenticationRequired(ProviderExecutionError):
    pass


class ProviderRateLimited(ProviderExecutionError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AIBackendConfig:
    driver: DriverId | str = DriverId.CODEX_CLI
    model: str = ""
    difficulty: Difficulty | str = Difficulty.HIGH
    executable: str | None = None
    request_timeout: float = 120.0
    turn_timeout: float = 1800.0
    max_tool_rounds: int = 100
    concurrency: ConcurrencyRuntimeSpec | None = None
    task_kind: TaskKind = TaskKind.PROOF
    provider_config_path: Path | None = None

    @property
    def driver_id(self) -> DriverId:
        return DriverId(self.driver)

    @property
    def difficulty_id(self) -> Difficulty:
        return Difficulty(self.difficulty)


@dataclass(frozen=True, slots=True)
class AIToolCall:
    name: str
    arguments: JSONValue
    result: str
    success: bool


@dataclass(slots=True)
class AITurnResult:
    final_text: str
    thread_id: str
    turn_id: str | None
    driver: str
    model: str
    effort: str
    events: list[JSONObject]
    tool_calls: list[AIToolCall] = field(default_factory=list)


_TASK_CLASS = {
    TaskKind.CLARIFICATION: AITaskClass.CLARIFICATION,
    TaskKind.DIAGNOSTIC: AITaskClass.DIAGNOSTIC,
    TaskKind.PROOF: AITaskClass.PROOF,
    TaskKind.SKETCH: AITaskClass.SKETCH,
    TaskKind.MAINTENANCE: AITaskClass.MAINTENANCE,
    TaskKind.REVIEW: AITaskClass.REVIEW,
    TaskKind.DUPLICATE_PROOF: AITaskClass.DUPLICATE_PROOF,
    TaskKind.REPORTING: AITaskClass.REPORTING,
}


def _redacted(text: str, secrets_to_remove: Sequence[str] = ()) -> str:
    result = text
    for secret in secrets_to_remove:
        if secret:
            result = result.replace(secret, "<redacted>")
    result = re.sub(
        r"(?i)(api[_-]?key|authorization|bearer|token|password)\s*[:=]\s*[^\s,;}]+",
        r"\1=<redacted>",
        result,
    )
    result = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])", "", result)
    result = "".join(
        character if not unicodedata.category(character).startswith("C") else " "
        for character in result
    )
    return result[:4000]


def _tool_definitions(
    tools: Sequence[Mapping[str, object]] | None,
) -> tuple[JSONObject, ...]:
    definitions: list[JSONObject] = []
    for item in tools or ():
        if item.get("type") != "function":
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name):
            raise ValueError(f"Invalid RepoProver tool name: {name!r}")
        parameters = function.get("parameters")
        definitions.append(
            json_object(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": str(function.get("description") or ""),
                        "parameters": (
                            dict(parameters)
                            if isinstance(parameters, Mapping)
                            else {"type": "object", "properties": {}}
                        ),
                    },
                }
            )
        )
    return tuple(definitions)


def _function_schema(tool: JSONObject) -> JSONObject:
    function = tool.get("function")
    if not isinstance(function, Mapping):
        raise ProviderExecutionError("Normalized tool is missing its function schema")
    return json_object(function, path="tool.function")


class AdmittedToolHost:
    """Single provider-independent authority for agent tool calls."""

    def __init__(
        self,
        handler: Callable[[str, JSONObject], str],
        allowed_names: Sequence[str],
        *,
        concurrency: ConcurrencyRuntimeSpec | None,
        timeout: float,
    ) -> None:
        self._handler = handler
        self._allowed = frozenset(allowed_names)
        self._concurrency_spec = concurrency
        self._runtime: ConcurrencyRuntime | None = None
        self._timeout = timeout
        self._calls: list[AIToolCall] = []
        self._lock = threading.Lock()

    @property
    def calls(self) -> list[AIToolCall]:
        with self._lock:
            return list(self._calls)

    def call(self, name: str, arguments: Mapping[str, object]) -> tuple[str, bool]:
        normalized_arguments = json_object(arguments, path=f"tool.{name}.arguments")
        if name not in self._allowed:
            result = f"Error: unknown or unauthorized dynamic tool {name!r}"
            self._record(name, normalized_arguments, result, False)
            return result, False
        try:
            result = self._call_admitted(name, normalized_arguments)
            text = result if isinstance(result, str) else str(result)
            success = not text.lstrip().startswith("Error:")
        except Exception as exc:
            text = f"Error: {type(exc).__name__}: {exc}"
            success = False
        self._record(name, normalized_arguments, text, success)
        return text, success

    def _record(
        self, name: str, arguments: JSONValue, result: str, success: bool
    ) -> None:
        with self._lock:
            self._calls.append(AIToolCall(name, arguments, result, success))

    def _call_admitted(self, name: str, arguments: JSONObject) -> str:
        if self._concurrency_spec is None:
            return self._handler(name, arguments)
        runtime = self._runtime
        if runtime is None:
            runtime = self._concurrency_spec.create()
            self._runtime = runtime
        if name == "lean_check":
            request = runtime.lean.request(
                f"lean-check:{uuid.uuid4().hex}",
                ttl_seconds=max(120.0, min(self._timeout, 600.0)),
            )
            with runtime.lean.lease(request, timeout=self._timeout):
                return self._handler(name, arguments)
        if name == "bash":
            command = str(arguments.get("command") or "")
            if re.search(r"(?m)(?:^|[;&|]\s*)lake\s+build(?:\s|$)", command):
                target = re.search(r"\blake\s+build\s+([^\s;&|]+)", command)
                full_build = target is None or target.group(1).startswith("-")
                request = runtime.build.request(
                    f"agent-build:{uuid.uuid4().hex}",
                    full_build=full_build,
                    ttl_seconds=max(120.0, min(self._timeout, 900.0)),
                )
                with runtime.build.lease(request, timeout=self._timeout):
                    return self._handler(name, arguments)
        return self._handler(name, arguments)


class _LoopbackToolServer:
    def __init__(self, host: AdmittedToolHost) -> None:
        self.host = host
        self.token = secrets.token_urlsafe(32)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.socket.settimeout(0.2)
        address = self.socket.getsockname()
        self.address = (str(address[0]), int(address[1]))
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        self.socket.close()
        self._thread.join(timeout=2.0)

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _address = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._serve_connection,
                args=(connection,),
                daemon=True,
            ).start()

    def _serve_connection(self, connection: socket.socket) -> None:
        with connection, connection.makefile("rwb", buffering=0) as stream:
            while not self._closed.is_set():
                line = stream.readline()
                if not line:
                    return
                try:
                    request = json.loads(line)
                    if not isinstance(request, Mapping):
                        raise ValueError("request is not an object")
                    supplied = str(request.get("token") or "")
                    if not hmac.compare_digest(supplied, self.token):
                        raise PermissionError("invalid capability token")
                    request_id = str(request.get("request_id") or "")
                    name = str(request.get("name") or "")
                    raw_arguments = request.get("arguments")
                    arguments = (
                        raw_arguments if isinstance(raw_arguments, Mapping) else {}
                    )
                    result, success = self.host.call(name, arguments)
                    response = {
                        "request_id": request_id,
                        "result": result,
                        "success": success,
                    }
                except Exception as exc:
                    response = {
                        "request_id": "",
                        "result": f"Error: tool host rejected request ({type(exc).__name__})",
                        "success": False,
                    }
                try:
                    stream.write(
                        json.dumps(response, separators=(",", ":")).encode("utf-8")
                        + b"\n"
                    )
                except OSError:
                    return


class AIBackend:
    def __init__(
        self,
        config: AIBackendConfig,
        *,
        cwd: str | Path | None = None,
        command_runner: CommandRunner | None = None,
        http_runner: HttpRunner | None = None,
        credential_store: CredentialStore | None = None,
    ) -> None:
        self.config = config
        self.cwd = Path(cwd).resolve() if cwd is not None else None
        self.command_runner = command_runner or SubprocessCommandRunner()
        self.http_runner = http_runner or UrllibHttpRunner()
        self.credential_store = credential_store or CompositeCredentialStore()
        self._runtime: ConcurrencyRuntime | None = None

    def close(self) -> None:
        """Backends are turn-scoped; present for a uniform lifecycle."""

    def run(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: Sequence[Mapping[str, object]] | None,
        tool_handler: Callable[[str, JSONObject], str],
    ) -> AITurnResult:
        driver = self.config.driver_id
        self._validate_runtime_contract(driver)
        definitions = _tool_definitions(tools)
        if driver is DriverId.CODEX_CLI:
            return self._run_codex(
                system_prompt, user_prompt, definitions, tool_handler
            )
        runtime = self._runtime
        request = None
        started = time.monotonic()
        queued = False
        if self.config.concurrency is not None:
            runtime = self.config.concurrency.create()
            self._runtime = runtime
            status = runtime.ai.status()
            queued = status.active >= status.current_limit
            request = runtime.ai.request(
                f"{driver.value}:{self.config.task_kind.value}:{uuid.uuid4().hex}",
                _TASK_CLASS[self.config.task_kind],
                ttl_seconds=max(120.0, min(self.config.turn_timeout, 900.0)),
            )
        try:
            if runtime is not None and request is not None:
                with runtime.ai.lease(request, timeout=self.config.turn_timeout):
                    result = self._run_non_codex(
                        driver, system_prompt, user_prompt, definitions, tool_handler
                    )
            else:
                result = self._run_non_codex(
                    driver, system_prompt, user_prompt, definitions, tool_handler
                )
        except ProviderRateLimited as exc:
            if runtime is not None:
                runtime.ai.record_throttle(retry_after=exc.retry_after)
            raise
        except (TimeoutError, ConnectionError, OSError):
            if runtime is not None:
                runtime.ai.record_transient_failure()
            raise
        if runtime is not None:
            runtime.ai.record_success(time.monotonic() - started, queued=queued)
        return result

    def _validate_runtime_contract(self, driver: DriverId) -> None:
        model = self.config.model
        try:
            ModelDescriptor(model, model)
        except ValueError as exc:
            raise ProviderExecutionError(
                "AI model must be a non-empty, control-free provider identifier"
            ) from exc
        difficulty = self.config.difficulty_id
        definition = driver_definition(driver)
        descriptor = next(
            (item for item in definition.curated_models if item.model_id == model),
            None,
        )
        allowed = descriptor.difficulties if descriptor else definition.difficulties
        if difficulty not in allowed:
            choices = ", ".join(item.value for item in allowed)
            raise ProviderExecutionError(
                f"{driver.value} model {model!r} does not support difficulty "
                f"{difficulty.value!r}; choose one of: {choices}"
            )

    def _run_codex(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: Sequence[JSONObject],
        tool_handler: Callable[[str, JSONObject], str],
    ) -> AITurnResult:
        difficulty = self.config.difficulty_id
        effort = (
            Difficulty.HIGH.value if difficulty is Difficulty.AUTO else difficulty.value
        )
        backend = CodexBackend(
            CodexConfig(
                executable=self.config.executable or "codex",
                model=self.config.model,
                effort=effort,
                request_timeout=self.config.request_timeout,
                turn_timeout=self.config.turn_timeout,
                sandbox="read-only",
                isolate_external_tools=True,
                environment=self._cli_environment(DriverId.CODEX_CLI),
                inherit_environment=False,
                concurrency=self.config.concurrency,
                ai_task_class=_TASK_CLASS[self.config.task_kind],
            ),
            cwd=self.cwd,
        )
        try:
            result = backend.run(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=list(tools),
                tool_handler=tool_handler,
            )
        finally:
            backend.close()
        return AITurnResult(
            final_text=result.final_text,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            driver=DriverId.CODEX_CLI.value,
            model=result.model,
            effort=result.effort,
            events=result.events,
            tool_calls=[
                AIToolCall(call.name, call.arguments, call.result, call.success)
                for call in result.tool_calls
            ],
        )

    def _run_non_codex(
        self,
        driver: DriverId,
        system_prompt: str,
        user_prompt: str,
        tools: Sequence[JSONObject],
        tool_handler: Callable[[str, JSONObject], str],
    ) -> AITurnResult:
        names = [str(_function_schema(item).get("name") or "") for item in tools]
        host = AdmittedToolHost(
            tool_handler,
            names,
            concurrency=self.config.concurrency,
            timeout=self.config.turn_timeout,
        )
        if driver in {DriverId.CLAUDE_CLI, DriverId.COPILOT_CLI}:
            return self._run_cli(driver, system_prompt, user_prompt, tools, host)
        credential = self._credential(driver)
        if driver is DriverId.OPENAI_API:
            return self._run_openai(system_prompt, user_prompt, tools, host, credential)
        if driver is DriverId.ANTHROPIC_API:
            return self._run_anthropic(
                system_prompt, user_prompt, tools, host, credential
            )
        if driver is DriverId.GEMINI_API:
            return self._run_gemini(system_prompt, user_prompt, tools, host, credential)
        raise ProviderExecutionError(f"Unsupported AI driver: {driver.value}")

    def _credential(self, driver: DriverId) -> str:
        settings = MachineProviderConfigStore(self.config.provider_config_path).load()
        preference = settings.config.preference_for(driver)
        source = preference.credential_source
        if source is CredentialSource.NONE:
            raise ProviderAuthenticationRequired(
                f"{driver.value} has no configured API credential source"
            )
        credential = self.credential_store.get(driver, source)
        if credential is None:
            raise ProviderAuthenticationRequired(
                f"{driver.value} requires a configured API credential"
            )
        return credential

    def _cli_environment(self, driver: DriverId) -> dict[str, str]:
        names = {
            "HOME",
            "PATH",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "SSH_AUTH_SOCK",
        }
        if driver is DriverId.COPILOT_CLI:
            names.update(
                {
                    "COPILOT_GITHUB_TOKEN",
                    "GH_TOKEN",
                    "GITHUB_TOKEN",
                    "GH_HOST",
                    "COPILOT_GH_HOST",
                }
            )
        return {name: os.environ[name] for name in names if name in os.environ}

    def _run_cli(
        self,
        driver: DriverId,
        system_prompt: str,
        user_prompt: str,
        tools: Sequence[JSONObject],
        host: AdmittedToolHost,
    ) -> AITurnResult:
        executable = self.config.executable or (
            "claude" if driver is DriverId.CLAUDE_CLI else "copilot"
        )
        with tempfile.TemporaryDirectory(prefix="proof-assistant-ai-") as temporary:
            root = Path(temporary)
            tools_path = root / "tools.json"
            system_path = root / "system.txt"
            user_path = root / "task.txt"
            mcp_path = root / "mcp.json"
            tools_path.write_text(json.dumps(list(tools)), encoding="utf-8")
            system_path.write_text(system_prompt, encoding="utf-8")
            user_path.write_text(user_prompt, encoding="utf-8")
            for path in (tools_path, system_path, user_path):
                path.chmod(0o600)
            server = _LoopbackToolServer(host)
            server.start()
            try:
                environment = self._cli_environment(driver)
                environment.update(
                    {
                        "PROOF_ASSISTANT_MCP_HOST": server.address[0],
                        "PROOF_ASSISTANT_MCP_PORT": str(server.address[1]),
                        "PROOF_ASSISTANT_MCP_TOKEN": server.token,
                        "PROOF_ASSISTANT_MCP_TOOLS": str(tools_path),
                    }
                )
                mcp = {
                    "mcpServers": {
                        "proof_assistant": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "proof_assistant.ai.mcp_bridge"],
                            "env": {
                                key: environment[key]
                                for key in (
                                    "PROOF_ASSISTANT_MCP_HOST",
                                    "PROOF_ASSISTANT_MCP_PORT",
                                    "PROOF_ASSISTANT_MCP_TOKEN",
                                    "PROOF_ASSISTANT_MCP_TOOLS",
                                )
                            },
                        }
                    }
                }
                mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
                mcp_path.chmod(0o600)
                difficulty = self.config.difficulty_id
                if driver is DriverId.CLAUDE_CLI:
                    argv = [
                        executable,
                        "-p",
                        "--output-format",
                        "json",
                        "--no-session-persistence",
                        "--strict-mcp-config",
                        "--mcp-config",
                        str(mcp_path),
                        "--setting-sources",
                        "",
                        "--tools",
                        "mcp__proof_assistant__*",
                        "--allowedTools",
                        "mcp__proof_assistant__*",
                        "--system-prompt-file",
                        str(system_path),
                        "--model",
                        self.config.model,
                    ]
                    if difficulty is not Difficulty.AUTO:
                        argv.extend(("--effort", difficulty.value))
                    input_text = user_prompt
                else:
                    argv = [
                        executable,
                        "-p",
                        (
                            "Follow the two attached instruction files exactly. "
                            "Use only tools from the proof_assistant MCP server."
                        ),
                        "-s",
                        "--output-format=json",
                        "--no-ask-user",
                        "--no-auto-update",
                        "--no-custom-instructions",
                        "--no-remote",
                        "--no-remote-export",
                        "--disable-builtin-mcps",
                        f"--additional-mcp-config=@{mcp_path}",
                        "--allow-tool=proof_assistant",
                        "--available-tools=proof_assistant",
                        "--deny-tool=shell,write,read,url,memory,skill,task",
                        f"--attachment={system_path}",
                        f"--attachment={user_path}",
                        f"--model={self.config.model}",
                    ]
                    if difficulty is not Difficulty.AUTO:
                        argv.append(f"--effort={difficulty.value}")
                    input_text = None
                result = self.command_runner.run(
                    tuple(argv),
                    input_text=input_text,
                    timeout_seconds=self.config.turn_timeout,
                    env=environment,
                )
            finally:
                server.close()
        if result.returncode != 0:
            detail = self._cli_failure_detail(driver, result.stdout, result.stderr)
            error_type = (
                ProviderAuthenticationRequired
                if driver is DriverId.CLAUDE_CLI
                and any(
                    marker in detail.casefold()
                    for marker in (
                        "authenticate",
                        "authentication",
                        "oauth",
                        "log in",
                        "login",
                        "logged in",
                        "auth token",
                    )
                )
                else ProviderExecutionError
            )
            raise error_type(
                f"{driver.value} exited with status {result.returncode}: {detail}"
            )
        events, final_text, thread_id = self._parse_cli_output(driver, result.stdout)
        return AITurnResult(
            final_text=final_text,
            thread_id=thread_id,
            turn_id=None,
            driver=driver.value,
            model=self.config.model,
            effort=self.config.difficulty_id.value,
            events=events,
            tool_calls=host.calls,
        )

    @staticmethod
    def _cli_failure_detail(driver: DriverId, stdout: str, stderr: str) -> str:
        """Extract a concise provider error without echoing a full event transcript."""

        if driver is DriverId.CLAUDE_CLI:
            for output in (stdout, stderr):
                try:
                    payload = json.loads(output)
                except json.JSONDecodeError:
                    continue
                items = payload if isinstance(payload, list) else [payload]
                for item in reversed(items):
                    if not isinstance(item, dict):
                        continue
                    for key in ("result", "error", "message"):
                        value = item.get(key)
                        if isinstance(value, str) and value.strip():
                            return _redacted(value.strip())
        fallback = "\n".join(part for part in (stderr, stdout) if part.strip())
        return _redacted(fallback)

    @staticmethod
    def _parse_cli_output(
        driver: DriverId, stdout: str
    ) -> tuple[list[JSONObject], str, str]:
        events: list[JSONObject] = []
        if driver is DriverId.CLAUDE_CLI:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise ProviderExecutionError(
                    "Claude CLI returned invalid JSON"
                ) from exc
            if isinstance(payload, list):
                events = [item for item in payload if isinstance(item, dict)]
                terminal = next(
                    (
                        item
                        for item in reversed(events)
                        if item.get("type") == "result"
                        or item.get("result") is not None
                        or item.get("output") is not None
                    ),
                    None,
                )
                if terminal is None:
                    raise ProviderExecutionError(
                        "Claude CLI returned no terminal result"
                    )
                final = terminal.get("result") or terminal.get("output") or ""
                thread = (
                    terminal.get("session_id")
                    or terminal.get("sessionId")
                    or next(
                        (
                            item.get("session_id") or item.get("sessionId")
                            for item in reversed(events)
                            if item.get("session_id") or item.get("sessionId")
                        ),
                        None,
                    )
                    or uuid.uuid4().hex
                )
                return events, str(final).strip(), str(thread)
            if not isinstance(payload, dict):
                raise ProviderExecutionError("Claude CLI returned an invalid result")
            events.append(payload)
            final = payload.get("result") or payload.get("output") or ""
            thread = (
                payload.get("session_id")
                or payload.get("sessionId")
                or uuid.uuid4().hex
            )
            return events, str(final).strip(), str(thread)
        final_chunks: list[str] = []
        thread_id = uuid.uuid4().hex
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                final_chunks.append(line)
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            for key in ("session_id", "sessionId", "thread_id"):
                if event.get(key):
                    thread_id = str(event[key])
            candidate = event.get("content") or event.get("text")
            if isinstance(candidate, str):
                final_chunks.append(candidate)
            message = event.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    final_chunks.append(content)
        final = "\n".join(final_chunks).strip() or stdout.strip()
        return events, final, thread_id

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        credential: str,
    ) -> JSONObject:
        response = self.http_runner.request(
            method,
            url,
            headers=headers,
            body=json.dumps(payload).encode("utf-8"),
            timeout_seconds=self.config.request_timeout,
        )
        if response.status == 429:
            retry = _retry_after(response)
            raise ProviderRateLimited("Provider rate limit reached", retry_after=retry)
        if not 200 <= response.status < 300:
            detail = _redacted(
                response.body.decode("utf-8", errors="replace"), (credential,)
            )
            if response.status in {401, 403}:
                raise ProviderAuthenticationRequired(
                    f"Provider rejected the configured credential (HTTP {response.status})"
                )
            raise ProviderExecutionError(
                f"Provider request failed with HTTP {response.status}: {detail}"
            )
        try:
            decoded = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderExecutionError("Provider returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ProviderExecutionError("Provider returned a non-object JSON response")
        return json_object(decoded, path="provider.response")

    def _run_openai(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: Sequence[JSONObject],
        host: AdmittedToolHost,
        credential: str,
    ) -> AITurnResult:
        openai_tools: list[JSONObject] = []
        for item in tools:
            function = _function_schema(item)
            openai_tools.append(
                {
                    "type": "function",
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "parameters": function.get("parameters") or {},
                }
            )
        input_items: list[object] = [{"role": "user", "content": user_prompt}]
        response_id: str | None = None
        events: list[JSONObject] = []
        for _round in range(self.config.max_tool_rounds):
            payload: dict[str, object] = {
                "model": self.config.model,
                "instructions": system_prompt,
                "input": input_items,
                "tools": openai_tools,
                "store": False,
                "include": ["reasoning.encrypted_content"],
            }
            difficulty = self.config.difficulty_id
            if difficulty is not Difficulty.AUTO:
                payload["reasoning"] = {"effort": difficulty.value}
            response = self._request_json(
                "POST",
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": "application/json",
                },
                payload=payload,
                credential=credential,
            )
            events.append(response)
            response_id = str(response.get("id") or response_id or uuid.uuid4().hex)
            output = response.get("output")
            calls: list[Mapping[str, object]] = []
            if isinstance(output, list):
                calls = [
                    item
                    for item in output
                    if isinstance(item, Mapping) and item.get("type") == "function_call"
                ]
            if not calls:
                return AITurnResult(
                    final_text=_openai_text(response),
                    thread_id=response_id,
                    turn_id=response_id,
                    driver=DriverId.OPENAI_API.value,
                    model=self.config.model,
                    effort=difficulty.value,
                    events=events,
                    tool_calls=host.calls,
                )
            input_items.extend(output if isinstance(output, list) else [])
            outputs: list[dict[str, object]] = []
            for call in calls:
                name = str(call.get("name") or "")
                raw_arguments = call.get("arguments")
                try:
                    arguments = json.loads(str(raw_arguments or "{}"))
                except json.JSONDecodeError:
                    arguments = {}
                if not isinstance(arguments, Mapping):
                    arguments = {}
                result, _success = host.call(name, arguments)
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(call.get("call_id") or call.get("id") or ""),
                        "output": result,
                    }
                )
            input_items.extend(outputs)
        raise ProviderExecutionError(
            "OpenAI tool loop exceeded its configured round limit"
        )

    def _run_anthropic(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: Sequence[JSONObject],
        host: AdmittedToolHost,
        credential: str,
    ) -> AITurnResult:
        anthropic_tools: list[JSONObject] = []
        for item in tools:
            function = _function_schema(item)
            anthropic_tools.append(
                {
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "input_schema": function.get("parameters") or {},
                }
            )
        messages: list[dict[str, object]] = [{"role": "user", "content": user_prompt}]
        events: list[JSONObject] = []
        thread_id = uuid.uuid4().hex
        for _round in range(self.config.max_tool_rounds):
            payload: dict[str, object] = {
                "model": self.config.model,
                "max_tokens": 8192,
                "system": system_prompt,
                "messages": messages,
            }
            if anthropic_tools:
                payload["tools"] = anthropic_tools
            difficulty = self.config.difficulty_id
            if difficulty is not Difficulty.AUTO:
                payload["output_config"] = {"effort": difficulty.value}
            response = self._request_json(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": credential,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                payload=payload,
                credential=credential,
            )
            events.append(response)
            thread_id = str(response.get("id") or thread_id)
            content = response.get("content")
            blocks = content if isinstance(content, list) else []
            calls = [
                block
                for block in blocks
                if isinstance(block, Mapping) and block.get("type") == "tool_use"
            ]
            if not calls:
                text = "\n".join(
                    str(block.get("text"))
                    for block in blocks
                    if isinstance(block, Mapping)
                    and block.get("type") == "text"
                    and block.get("text")
                ).strip()
                return AITurnResult(
                    text,
                    thread_id,
                    thread_id,
                    DriverId.ANTHROPIC_API.value,
                    self.config.model,
                    difficulty.value,
                    events,
                    host.calls,
                )
            messages.append({"role": "assistant", "content": blocks})
            results: list[dict[str, object]] = []
            for call in calls:
                name = str(call.get("name") or "")
                arguments = call.get("input")
                if not isinstance(arguments, Mapping):
                    arguments = {}
                result, success = host.call(name, arguments)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": str(call.get("id") or ""),
                        "content": result,
                        "is_error": not success,
                    }
                )
            messages.append({"role": "user", "content": results})
        raise ProviderExecutionError(
            "Anthropic tool loop exceeded its configured round limit"
        )

    def _run_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: Sequence[JSONObject],
        host: AdmittedToolHost,
        credential: str,
    ) -> AITurnResult:
        declarations: list[JSONObject] = []
        for item in tools:
            function = _function_schema(item)
            declarations.append(
                {
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "parameters": function.get("parameters") or {},
                }
            )
        contents: list[dict[str, object]] = [
            {"role": "user", "parts": [{"text": user_prompt}]}
        ]
        events: list[JSONObject] = []
        thread_id = uuid.uuid4().hex
        model = self.config.model.removeprefix("models/")
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + urllib.parse.quote(model, safe="")
            + ":generateContent"
        )
        difficulty = self.config.difficulty_id
        if difficulty in {Difficulty.XHIGH, Difficulty.MAX, Difficulty.NONE}:
            raise ProviderExecutionError(
                f"Gemini does not support difficulty {difficulty.value!r}; use auto, low, medium, or high"
            )
        for _round in range(self.config.max_tool_rounds):
            payload: dict[str, object] = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": contents,
            }
            if declarations:
                payload["tools"] = [{"functionDeclarations": declarations}]
            thinking_config = _gemini_thinking_config(model, difficulty)
            if thinking_config is not None:
                payload["generationConfig"] = {"thinkingConfig": thinking_config}
            response = self._request_json(
                "POST",
                url,
                headers={
                    "x-goog-api-key": credential,
                    "content-type": "application/json",
                },
                payload=payload,
                credential=credential,
            )
            events.append(response)
            candidates = response.get("candidates")
            first = candidates[0] if isinstance(candidates, list) and candidates else {}
            content = first.get("content") if isinstance(first, Mapping) else {}
            parts = content.get("parts") if isinstance(content, Mapping) else []
            parts = parts if isinstance(parts, list) else []
            calls = [
                part["functionCall"]
                for part in parts
                if isinstance(part, Mapping)
                and isinstance(part.get("functionCall"), Mapping)
            ]
            if not calls:
                text = "\n".join(
                    str(part.get("text"))
                    for part in parts
                    if isinstance(part, Mapping) and part.get("text")
                ).strip()
                return AITurnResult(
                    text,
                    thread_id,
                    None,
                    DriverId.GEMINI_API.value,
                    self.config.model,
                    difficulty.value,
                    events,
                    host.calls,
                )
            contents.append({"role": "model", "parts": parts})
            result_parts: list[dict[str, object]] = []
            for call in calls:
                name = str(call.get("name") or "")
                arguments = call.get("args")
                if not isinstance(arguments, Mapping):
                    arguments = {}
                result, success = host.call(name, arguments)
                result_parts.append(
                    {
                        "functionResponse": {
                            "name": name,
                            "response": {"result": result, "success": success},
                        }
                    }
                )
            contents.append({"role": "user", "parts": result_parts})
        raise ProviderExecutionError(
            "Gemini tool loop exceeded its configured round limit"
        )


def _retry_after(response: HttpResponse) -> float | None:
    for key, value in response.headers:
        if key.casefold() == "retry-after":
            try:
                return max(0.0, float(value))
            except ValueError:
                return None
    return None


def _gemini_thinking_config(
    model: str, difficulty: Difficulty
) -> dict[str, object] | None:
    """Translate Proof Assistant difficulty to GenerateContent controls.

    Gemini 3 accepts named thinking levels. Gemini 2.5 accepts only a numeric
    thinking budget, so the numbers below are explicit Proof Assistant policy
    points within the documented provider ranges, not provider-advertised
    difficulty aliases.
    """

    if difficulty is Difficulty.AUTO:
        return None
    if model.startswith("gemini-3"):
        return {"thinkingLevel": difficulty.value}
    if model.startswith("gemini-2.5-"):
        maximum = 32_768 if model.startswith("gemini-2.5-pro") else 24_576
        budget = {
            Difficulty.LOW: 1_024,
            Difficulty.MEDIUM: 8_192,
            Difficulty.HIGH: maximum,
        }.get(difficulty)
        if budget is not None:
            return {"thinkingBudget": budget}
    raise ProviderExecutionError(
        f"Gemini model {model!r} has no validated mapping for difficulty "
        f"{difficulty.value!r}; use auto"
    )


def _openai_text(response: Mapping[str, object]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str):
        return direct.strip()
    output = response.get("output")
    chunks: list[str] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks).strip()
