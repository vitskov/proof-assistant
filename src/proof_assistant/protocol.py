from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .json_types import JSONObject, JSONValue, json_object, load_json


class CodexProtocolError(RuntimeError):
    pass


class CodexServerExited(CodexProtocolError):
    pass


_SAFE_CONFIG_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def isolated_tool_config_args(
    executable: str,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> list[str]:
    """Build child-only overrides that disable inherited external tools.

    Codex merges an empty ``mcp_servers`` table with the user's base config, and
    overriding only ``enabled`` replaces the transport table. Replacing each
    configured entry with a complete disabled dummy transport is therefore the
    fail-closed form supported by the current CLI. Apps and plugin-bundled MCP
    servers have separate feature switches and are disabled as well.
    """
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    try:
        result = subprocess.run(
            [executable, "mcp", "list", "--json"],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=child_env,
        )
    except FileNotFoundError as exc:
        raise CodexProtocolError(f"Codex executable not found: {executable!r}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexProtocolError(
            f"Could not inspect configured Codex MCP servers: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CodexProtocolError(
            "Could not inspect configured Codex MCP servers before starting "
            f"an isolated backend: {detail or f'exit {result.returncode}'}"
        )
    try:
        entries = load_json(result.stdout)
    except ValueError as exc:
        raise CodexProtocolError(
            "codex mcp list --json returned malformed JSON"
        ) from exc
    if not isinstance(entries, list):
        raise CodexProtocolError("codex mcp list --json returned a non-list result")

    args = ["--disable", "apps", "--disable", "plugins"]
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise CodexProtocolError("Codex MCP listing contained an invalid name")
        if _SAFE_CONFIG_KEY.fullmatch(name) is None:
            raise CodexProtocolError(
                f"Cannot safely isolate Codex MCP server with unsupported name {name!r}"
            )
        args.extend(
            [
                "-c",
                f'mcp_servers.{name}={{command="true",enabled=false}}',
            ]
        )
    return args


def isolated_skill_config_args(
    executable: str,
    *,
    cwd: str | Path | None,
    external_tool_args: list[str],
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> list[str]:
    """Discover and disable every skill visible to the child workspace.

    Skill configuration is deny-list based in the current Codex CLI. A probe
    app-server therefore scans the same workspace with external tools and
    bundled skills disabled. The real child receives path-specific, ephemeral
    overrides for every remaining user/repository/admin skill.
    """
    base_args = [
        "-c",
        "skills.include_instructions=false",
        "-c",
        "skills.bundled.enabled=false",
    ]
    probe = AppServerClient(
        executable,
        cwd=cwd,
        env=env,
        extra_args=[*external_tool_args, *base_args],
    )
    try:
        probe.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "proof-assistant-skill-probe",
                    "title": "Proof Assistant skill isolation probe",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=timeout,
        )
        probe.notify("initialized", {})
        response = probe.request(
            "skills/list",
            {
                "cwds": [str(Path(cwd).resolve())] if cwd is not None else [],
                "forceReload": True,
            },
            timeout=timeout,
        )
    finally:
        probe.close()

    if not isinstance(response, dict):
        raise CodexProtocolError("skills/list returned an invalid isolation response")
    data = response.get("data")
    if not isinstance(data, list):
        raise CodexProtocolError("skills/list returned an invalid isolation response")
    paths: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            raise CodexProtocolError(
                "skills/list contained an invalid workspace record"
            )
        skills = entry.get("skills")
        if not isinstance(skills, list):
            raise CodexProtocolError(
                "skills/list contained an invalid workspace record"
            )
        errors = entry.get("errors") or []
        if not isinstance(errors, list) or errors:
            raise CodexProtocolError(
                "Codex reported skill discovery errors; refusing an unverified child"
            )
        for skill in skills:
            if not isinstance(skill, dict):
                raise CodexProtocolError(
                    "skills/list contained an invalid skill record"
                )
            path = skill.get("path")
            if not isinstance(path, str):
                raise CodexProtocolError(
                    "skills/list contained an invalid skill record"
                )
            paths.add(path)

    selectors = ",".join(
        f"{{path={json.dumps(path)},enabled=false}}" for path in sorted(paths)
    )
    return [*base_args, "-c", f"skills.config=[{selectors}]"]


@dataclass
class _Pending:
    event: threading.Event
    result: JSONValue = None
    error: JSONValue = None


class AppServerClient:
    """Synchronous JSONL client for ``codex app-server`` over stdio.

    The server is bidirectional. Besides responses and notifications, it sends
    requests back to this process for dynamic tool calls and approval handling.
    """

    def __init__(
        self,
        executable: str = "codex",
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.executable = executable
        self.cwd = Path(cwd).resolve() if cwd else None
        self.env = env
        self.extra_args = list(extra_args or [])
        self.proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._notifications: queue.Queue[JSONObject] = queue.Queue()
        self._handlers: dict[str, Callable[[JSONObject], JSONValue]] = {}
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def start(self) -> None:
        if self.proc is not None:
            return
        cmd = [self.executable, "app-server", "--listen", "stdio://", *self.extra_args]
        child_env = None
        if self.env is not None:
            child_env = os.environ.copy()
            child_env.update(self.env)
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                bufsize=1,
                cwd=str(self.cwd) if self.cwd else None,
                env=child_env,
            )
        except FileNotFoundError as exc:
            raise CodexProtocolError(
                f"Codex executable not found: {self.executable!r}"
            ) from exc
        except OSError as exc:
            raise CodexProtocolError(
                f"Could not start Codex executable {self.executable!r}: {exc}"
            ) from exc
        self._reader = threading.Thread(
            target=self._reader_loop,
            name="proof-assistant-app-server-reader",
            daemon=True,
        )
        self._reader.start()

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        reader = self._reader
        self._reader = None
        try:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        finally:
            # ``Popen.wait`` reaps the child but does not close file objects
            # supplied as PIPE.  Join the reader after process exit, then close
            # every pipe even when termination or waiting raised.  Long-lived
            # coordinators may create many clients, so relying on cyclic GC can
            # exhaust macOS's relatively small per-process descriptor limit.
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except Exception:
                    pass

    def register_request_handler(
        self, method: str, handler: Callable[[JSONObject], JSONValue]
    ) -> None:
        self._handlers[method] = handler

    def _send(self, payload: Mapping[str, object]) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise CodexServerExited("codex app-server is not running")
        line = json.dumps(json_object(payload), separators=(",", ":"))
        with self._write_lock:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()

    def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float = 120.0,
    ) -> JSONValue:
        self.start()
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            pending = _Pending(threading.Event())
            self._pending[request_id] = pending
        self._send({"id": request_id, "method": method, "params": params or {}})
        if not pending.event.wait(timeout):
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"Timed out waiting for app-server method {method!r}")
        if pending.error is not None:
            raise CodexProtocolError(f"{method}: {pending.error}")
        return pending.result

    def notify(self, method: str, params: Mapping[str, object] | None = None) -> None:
        self.start()
        self._send({"method": method, "params": params or {}})

    def next_notification(self, timeout: float | None = None) -> JSONObject:
        return self._notifications.get(timeout=timeout)

    def _reader_loop(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    decoded = load_json(raw)
                except ValueError:
                    # Codex should emit JSONL on stdout, but ignoring non-JSON here
                    # is safer than killing an otherwise recoverable session.
                    continue
                if not isinstance(decoded, dict):
                    continue
                msg = decoded

                if (
                    "id" in msg
                    and "method" not in msg
                    and ("result" in msg or "error" in msg)
                ):
                    response_id = msg["id"]
                    pending = None
                    if isinstance(response_id, int) and not isinstance(
                        response_id, bool
                    ):
                        with self._state_lock:
                            pending = self._pending.pop(response_id, None)
                    if pending is not None:
                        pending.result = msg.get("result")
                        pending.error = msg.get("error")
                        pending.event.set()
                    continue

                if "id" in msg and "method" in msg:
                    self._handle_server_request(msg)
                    continue

                if "method" in msg:
                    self._notifications.put(msg)
        finally:
            with self._state_lock:
                remaining = list(self._pending.values())
                self._pending.clear()
            for pending in remaining:
                pending.error = "codex app-server exited"
                pending.event.set()
            self._notifications.put(
                {
                    "method": "_proof_assistant/server_exited",
                    "params": {"returncode": proc.poll()},
                }
            )

    def _handle_server_request(self, msg: JSONObject) -> None:
        method = str(msg["method"])
        request_id = msg["id"]
        handler = self._handlers.get(method)
        if handler is None:
            self._send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Host does not implement {method}",
                    },
                }
            )
            return
        try:
            result = handler(json_object(msg.get("params") or {}, path="$.params"))
            self._send({"id": request_id, "result": result})
        except Exception as exc:
            self._send(
                {
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            )
