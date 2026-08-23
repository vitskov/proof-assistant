from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
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
                    "name": "repoprover-codex-skill-probe",
                    "title": "RepoProver Codex skill isolation probe",
                    "version": "0.4.1",
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

    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise CodexProtocolError("skills/list returned an invalid isolation response")
    paths: set[str] = set()
    for entry in response["data"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("skills"), list):
            raise CodexProtocolError(
                "skills/list contained an invalid workspace record"
            )
        errors = entry.get("errors") or []
        if not isinstance(errors, list) or errors:
            raise CodexProtocolError(
                "Codex reported skill discovery errors; refusing an unverified child"
            )
        for skill in entry["skills"]:
            if not isinstance(skill, dict) or not isinstance(skill.get("path"), str):
                raise CodexProtocolError(
                    "skills/list contained an invalid skill record"
                )
            paths.add(skill["path"])

    selectors = ",".join(
        f"{{path={json.dumps(path)},enabled=false}}" for path in sorted(paths)
    )
    return [*base_args, "-c", f"skills.config=[{selectors}]"]


@dataclass
class _Pending:
    event: threading.Event
    result: Any = None
    error: Any = None


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
        self._notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
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
            name="repoprover-codex-app-server-reader",
            daemon=True,
        )
        self._reader.start()

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
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
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=5)

    def register_request_handler(
        self, method: str, handler: Callable[[dict[str, Any]], Any]
    ) -> None:
        self._handlers[method] = handler

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise CodexServerExited("codex app-server is not running")
        line = json.dumps(payload, separators=(",", ":"))
        with self._write_lock:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 120.0,
    ) -> Any:
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

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.start()
        self._send({"method": method, "params": params or {}})

    def next_notification(self, timeout: float | None = None) -> dict[str, Any]:
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
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    # Codex should emit JSONL on stdout, but ignoring non-JSON here
                    # is safer than killing an otherwise recoverable session.
                    continue

                if (
                    "id" in msg
                    and "method" not in msg
                    and ("result" in msg or "error" in msg)
                ):
                    with self._state_lock:
                        pending = self._pending.pop(msg["id"], None)
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
                    "method": "_repoprover_codex/server_exited",
                    "params": {"returncode": proc.poll()},
                }
            )

    def _handle_server_request(self, msg: dict[str, Any]) -> None:
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
            result = handler(msg.get("params") or {})
            self._send({"id": request_id, "result": result})
        except Exception as exc:
            self._send(
                {
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            )
