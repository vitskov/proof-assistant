from __future__ import annotations

import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .concurrency import AITaskClass, ConcurrencyRuntimeSpec
from .models import validate_model_effort
from .protocol import (
    AppServerClient,
    CodexProtocolError,
    CodexServerExited,
    isolated_skill_config_args,
    isolated_tool_config_args,
)
from .tools import dynamic_tool_result, openai_tools_to_codex


@dataclass(frozen=True)
class CodexConfig:
    executable: str = "codex"
    model: str = ""
    effort: str = "high"
    request_timeout: float = 120.0
    turn_timeout: float = 1800.0
    approval_policy: str = "never"
    sandbox: str = "read-only"
    validate_model: bool = True
    isolate_external_tools: bool = True
    extra_app_server_args: tuple[str, ...] = field(default_factory=tuple)
    concurrency: ConcurrencyRuntimeSpec | None = None
    ai_task_class: AITaskClass = AITaskClass.PROOF


@dataclass(frozen=True)
class CodexToolCall:
    name: str
    arguments: dict[str, Any] | Any
    result: str
    success: bool


@dataclass
class CodexResult:
    final_text: str
    thread_id: str
    turn_id: str | None
    model: str
    effort: str
    events: list[dict[str, Any]]
    tool_calls: list[CodexToolCall] = field(default_factory=list)


# A conservative package-level guard for callers that run several agents in a
# single process. Separate processes remain subject to Codex account limits.
_ACTIVE_TURNS = threading.BoundedSemaphore(value=2)


class CodexBackend:
    """Native Codex app-server backend.

    Authentication intentionally remains inside the Codex executable.
    """

    def __init__(
        self,
        config: CodexConfig,
        *,
        cwd: str | Path | None = None,
        client: AppServerClient | None = None,
    ) -> None:
        self.config = config
        self.cwd = Path(cwd).resolve() if cwd else None
        if client is None:
            extra_args: list[str] = []
            if config.isolate_external_tools:
                external_tool_args = isolated_tool_config_args(config.executable)
                extra_args.extend(external_tool_args)
                extra_args.extend(
                    isolated_skill_config_args(
                        config.executable,
                        cwd=self.cwd,
                        external_tool_args=external_tool_args,
                    )
                )
            extra_args.extend(config.extra_app_server_args)
            client = AppServerClient(
                config.executable,
                cwd=self.cwd,
                extra_args=extra_args,
            )
        self.client = client
        self._tool_handler: Callable[[str, dict[str, Any]], str] | None = None
        self._tool_names: set[str] = set()
        self._tool_calls: list[CodexToolCall] = []
        self._tool_lock = threading.Lock()
        self._concurrency_runtime: Any | None = None
        self.client.register_request_handler("item/tool/call", self._on_tool_call)

        # Dynamic tools are authoritative. We fail closed on any unexpected
        # request asking the host to approve Codex-native mutations/execution.
        for method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        ):
            self.client.register_request_handler(method, self._deny_approval)

    def close(self) -> None:
        self.client.close()

    def _deny_approval(self, _params: dict[str, Any]) -> dict[str, Any]:
        # Protocol versions have used slightly different response fields. The
        # common decision vocabulary is "decline"/"denied"; returning both is
        # intentionally conservative for an integration smoke-test backend.
        return {"decision": "decline", "approved": False}

    def initialize(self) -> dict[str, Any]:
        response = self.client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "proof-assistant",
                    "title": "Proof Assistant backend",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=self.config.request_timeout,
        )
        try:
            self.client.notify("initialized", {})
        except Exception:
            pass
        if self.config.isolate_external_tools:
            self._validate_external_tool_isolation()
            self._validate_skill_isolation()
        return response or {}

    def _validate_external_tool_isolation(self) -> None:
        """Fail if the child still exposes an MCP/app/plugin capability."""
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "limit": 100,
                "detail": "toolsAndAuthOnly",
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = self.client.request(
                "mcpServerStatus/list",
                params,
                timeout=self.config.request_timeout,
            )
            if not isinstance(response, dict) or not isinstance(
                response.get("data"), list
            ):
                raise CodexProtocolError(
                    "mcpServerStatus/list returned an invalid isolation response"
                )
            exposed: list[str] = []
            for entry in response["data"]:
                if not isinstance(entry, dict):
                    raise CodexProtocolError(
                        "mcpServerStatus/list contained an invalid server record"
                    )
                if any(
                    entry.get(field)
                    for field in ("tools", "resources", "resourceTemplates")
                ):
                    exposed.append(str(entry.get("name") or "<unnamed>"))
            if exposed:
                raise CodexProtocolError(
                    "External Codex tools remained exposed after isolation: "
                    + ", ".join(sorted(exposed))
                )
            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                return
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor == cursor
            ):
                raise CodexProtocolError(
                    "mcpServerStatus/list returned an invalid pagination cursor"
                )
            cursor = next_cursor

    def _validate_skill_isolation(self) -> None:
        response = self.client.request(
            "skills/list",
            {
                "cwds": [str(self.cwd)] if self.cwd is not None else [],
                "forceReload": True,
            },
            timeout=self.config.request_timeout,
        )
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise CodexProtocolError(
                "skills/list returned an invalid isolation response"
            )
        enabled: list[str] = []
        for entry in response["data"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("skills"), list):
                raise CodexProtocolError(
                    "skills/list contained an invalid workspace record"
                )
            errors = entry.get("errors") or []
            if not isinstance(errors, list) or errors:
                raise CodexProtocolError(
                    "Codex reported skill discovery errors after isolation"
                )
            for skill in entry["skills"]:
                if not isinstance(skill, dict):
                    raise CodexProtocolError(
                        "skills/list contained an invalid skill record"
                    )
                if skill.get("enabled"):
                    enabled.append(str(skill.get("name") or "<unnamed>"))
        if enabled:
            raise CodexProtocolError(
                "Local Codex skills remained enabled after isolation: "
                + ", ".join(sorted(enabled))
            )

    def model_catalog(self) -> list[dict[str, Any]]:
        response = self.client.request(
            "model/list",
            {"limit": 100},
            timeout=self.config.request_timeout,
        )
        if isinstance(response, dict):
            return list(response.get("data") or response.get("models") or [])
        return list(response or [])

    def _on_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        tool = str(params.get("tool") or "")
        raw_arguments = params.get("arguments", {})
        if self._tool_handler is None:
            return self._tool_failure(
                tool,
                raw_arguments,
                "No RepoProver tool handler is installed",
            )
        if tool not in self._tool_names:
            return self._tool_failure(
                tool,
                raw_arguments,
                f"Unknown dynamic tool {tool!r}",
            )
        arguments = raw_arguments
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return self._tool_failure(
                tool,
                raw_arguments,
                f"Invalid arguments for {tool!r}: expected JSON object",
            )
        try:
            result = self._run_tool_handler_with_admission(tool, arguments)
        except Exception as exc:
            return self._tool_failure(tool, arguments, f"Error: {exc}")
        text = result if isinstance(result, str) else str(result)
        success = not text.lstrip().startswith("Error:")
        self._record_tool_call(tool, arguments, text, success)
        return dynamic_tool_result(text, success=success)

    def _run_tool_handler_with_admission(
        self, tool: str, arguments: dict[str, Any]
    ) -> str:
        """Apply machine resource admission at the common dynamic-tool seam.

        RepoProver-derived agents do not all share one Python mixin.  Gating
        here ensures every managed ``lean_check`` and agent-requested Lake
        build uses the same machine controllers, including compatibility CLI
        paths.  The AI lease remains independent and active while its tool call
        waits for local capacity.
        """

        assert self._tool_handler is not None
        spec = self.config.concurrency
        if spec is None:
            return self._tool_handler(tool, arguments)
        runtime = self._concurrency_runtime
        if runtime is None:
            runtime = spec.create()
            self._concurrency_runtime = runtime
        if tool == "lean_check":
            request = runtime.lean.request(
                f"lean-check:{uuid.uuid4().hex}",
                ttl_seconds=max(120.0, min(self.config.turn_timeout, 600.0)),
            )
            with runtime.lean.lease(request, timeout=self.config.turn_timeout):
                return self._tool_handler(tool, arguments)
        if tool == "bash":
            command = str(arguments.get("command") or "")
            is_lake_build = bool(
                re.search(r"(?m)(?:^|[;&|]\s*)lake\s+build(?:\s|$)", command)
            )
            if is_lake_build:
                target_match = re.search(r"\blake\s+build\s+([^\s;&|]+)", command)
                full_build = target_match is None or target_match.group(1).startswith(
                    "-"
                )
                request = runtime.build.request(
                    f"agent-build:{uuid.uuid4().hex}",
                    full_build=full_build,
                    ttl_seconds=max(120.0, min(self.config.turn_timeout, 900.0)),
                )
                with runtime.build.lease(request, timeout=self.config.turn_timeout):
                    return self._tool_handler(tool, arguments)
        return self._tool_handler(tool, arguments)

    def _record_tool_call(
        self,
        tool: str,
        arguments: dict[str, Any] | Any,
        result: str,
        success: bool,
    ) -> None:
        with self._tool_lock:
            self._tool_calls.append(
                CodexToolCall(
                    name=tool,
                    arguments=arguments,
                    result=result,
                    success=success,
                )
            )

    def _tool_failure(
        self,
        tool: str,
        arguments: dict[str, Any] | Any,
        message: str,
    ) -> dict[str, Any]:
        self._record_tool_call(tool, arguments, message, False)
        return dynamic_tool_result(message, success=False)

    @staticmethod
    def _extract_thread_id(response: Any) -> str:
        if not isinstance(response, dict):
            raise CodexProtocolError(f"Unexpected thread/start response: {response!r}")
        thread = response.get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            return str(thread["id"])
        if response.get("threadId"):
            return str(response["threadId"])
        if response.get("id"):
            return str(response["id"])
        raise CodexProtocolError(f"thread/start returned no thread id: {response!r}")

    @staticmethod
    def _extract_turn_id(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        turn = response.get("turn")
        if isinstance(turn, dict) and turn.get("id"):
            return str(turn["id"])
        if response.get("turnId"):
            return str(response["turnId"])
        if response.get("id"):
            return str(response["id"])
        return None

    @staticmethod
    def _text_from_item(item: dict[str, Any]) -> str:
        # v2 agentMessage items normally contain text directly; tolerate common
        # variants so the adapter is not unnecessarily brittle.
        if isinstance(item.get("text"), str):
            return item["text"]
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                value = part.get("text") or part.get("content")
                if isinstance(value, str):
                    chunks.append(value)
            return "".join(chunks)
        return ""

    def run(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]] | None,
        tool_handler: Callable[[str, dict[str, Any]], str],
    ) -> CodexResult:
        if self.config.concurrency is None:
            with _ACTIVE_TURNS:
                return self._run_admitted(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                    tool_handler=tool_handler,
                )

        runtime = self._concurrency_runtime
        if runtime is None:
            runtime = self.config.concurrency.create()
            self._concurrency_runtime = runtime
        owner = f"codex:{self.config.ai_task_class.value}:{uuid.uuid4().hex}"
        request = runtime.ai.request(
            owner,
            self.config.ai_task_class,
            ttl_seconds=max(120.0, min(self.config.turn_timeout, 900.0)),
        )
        started = time.monotonic()
        queued_before = runtime.ai.status().active >= runtime.ai.status().current_limit
        admitted_at = started
        try:
            with runtime.ai.lease(request, timeout=self.config.turn_timeout):
                admitted_at = time.monotonic()
                result = self._run_admitted(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                    tool_handler=tool_handler,
                )
        except Exception as exc:
            message = str(exc).casefold()
            if any(
                marker in message
                for marker in ("rate limit", "rate-limit", "throttl", "429")
            ):
                retry_after = getattr(exc, "retry_after", None)
                runtime.ai.record_throttle(retry_after=retry_after)
            elif isinstance(exc, (TimeoutError, CodexServerExited)) or any(
                marker in message
                for marker in (
                    "service unavailable",
                    "temporarily unavailable",
                    "connection reset",
                    "connection refused",
                    " 502",
                    " 503",
                    " 504",
                )
            ):
                runtime.ai.record_transient_failure()
            raise
        runtime.ai.record_success(
            time.monotonic() - started,
            queued=queued_before or admitted_at - started > 0.2,
        )
        return result

    def _run_admitted(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]] | None,
        tool_handler: Callable[[str, dict[str, Any]], str],
    ) -> CodexResult:
        self.client.start()
        self.initialize()

        if self.config.validate_model:
            validate_model_effort(
                self.model_catalog(),
                model=self.config.model,
                effort=self.config.effort,
            )

        self._tool_handler = tool_handler
        dynamic_tools = openai_tools_to_codex(tools)
        self._tool_names = {str(tool["name"]) for tool in dynamic_tools}
        with self._tool_lock:
            self._tool_calls = []

        thread_params: dict[str, Any] = {
            "cwd": str(self.cwd) if self.cwd else None,
            "approvalPolicy": self.config.approval_policy,
            "sandbox": self.config.sandbox,
            "baseInstructions": system_prompt,
            "dynamicTools": dynamic_tools,
            "model": self.config.model or None,
            # Hosting-platform capabilities and remote environments are not part
            # of RepoProver's explicit tool registry.
            "selectedCapabilityRoots": [],
            "environments": [],
        }
        thread_params = {k: v for k, v in thread_params.items() if v is not None}

        thread_response = self.client.request(
            "thread/start", thread_params, timeout=self.config.request_timeout
        )
        thread_id = self._extract_thread_id(thread_response)

        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": user_prompt}],
            "model": self.config.model,
            "effort": self.config.effort,
        }
        turn_response = self.client.request(
            "turn/start", turn_params, timeout=self.config.request_timeout
        )
        turn_id = self._extract_turn_id(turn_response)

        events: list[dict[str, Any]] = []
        final_chunks: list[str] = []

        while True:
            try:
                notification = self.client.next_notification(
                    timeout=self.config.turn_timeout
                )
            except queue.Empty as exc:
                raise TimeoutError(
                    f"Codex turn did not complete within {self.config.turn_timeout}s"
                ) from exc

            events.append(notification)
            method = str(notification.get("method") or "")
            params = notification.get("params") or {}

            if method == "_proof_assistant/server_exited":
                raise CodexServerExited(
                    f"codex app-server exited during turn: {params!r}"
                )

            if method == "item/completed":
                item = params.get("item") or {}
                if isinstance(item, dict) and item.get("type") in (
                    "agentMessage",
                    "assistantMessage",
                    "message",
                ):
                    text = self._text_from_item(item)
                    if text:
                        final_chunks.append(text)

            if method == "turn/completed":
                completed_turn = params.get("turn") or {}
                completed_id = (
                    completed_turn.get("id")
                    if isinstance(completed_turn, dict)
                    else None
                )
                if (
                    turn_id is None
                    or completed_id is None
                    or str(completed_id) == turn_id
                ):
                    status = (
                        completed_turn.get("status")
                        if isinstance(completed_turn, dict)
                        else None
                    )
                    if status in ("failed", "cancelled", "interrupted"):
                        raise CodexProtocolError(
                            f"Codex turn ended with status {status!r}: {params!r}"
                        )
                    break

        return CodexResult(
            final_text="\n".join(x for x in final_chunks if x).strip(),
            thread_id=thread_id,
            turn_id=turn_id,
            model=self.config.model,
            effort=self.config.effort,
            events=events,
            tool_calls=list(self._tool_calls),
        )
