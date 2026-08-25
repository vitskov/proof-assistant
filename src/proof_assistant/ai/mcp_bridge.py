"""Ephemeral stdio MCP bridge used by isolated coding-agent CLI turns.

The bridge has no provider credentials and no direct workspace authority.  It
forwards only tool calls named in a mode-0600 schema file to a loopback socket
owned by the verification worker.  The worker remains the sole place where
RepoProver handlers and Lean/build admission execute.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise RuntimeError(f"Missing required bridge environment: {name}")
    return value


def _load_tools(path: Path) -> tuple[dict[str, object], ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("Bridge tool schema must be a JSON list")
    tools: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping) or item.get("type") != "function":
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError("Bridge tool schema contains an invalid function name")
        parameters = function.get("parameters")
        tools.append(
            {
                "name": name,
                "description": str(function.get("description") or ""),
                "inputSchema": (
                    dict(parameters)
                    if isinstance(parameters, Mapping)
                    else {"type": "object", "properties": {}}
                ),
            }
        )
    return tuple(tools)


class _ToolProxy:
    def __init__(self) -> None:
        host = _required_environment("PROOF_ASSISTANT_MCP_HOST")
        port = int(_required_environment("PROOF_ASSISTANT_MCP_PORT"))
        self._token = _required_environment("PROOF_ASSISTANT_MCP_TOKEN")
        self._socket = socket.create_connection((host, port), timeout=30.0)
        self._stream = self._socket.makefile("rwb", buffering=0)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._socket.close()

    def call(self, name: str, arguments: Mapping[str, object]) -> tuple[str, bool]:
        request_id = uuid.uuid4().hex
        payload = {
            "token": self._token,
            "request_id": request_id,
            "name": name,
            "arguments": dict(arguments),
        }
        self._stream.write(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
            + b"\n"
        )
        line = self._stream.readline()
        if not line:
            raise RuntimeError("Proof Assistant tool host closed the bridge socket")
        response = json.loads(line)
        if (
            not isinstance(response, Mapping)
            or response.get("request_id") != request_id
        ):
            raise RuntimeError("Proof Assistant tool host returned an invalid response")
        return str(response.get("result") or ""), bool(response.get("success"))


def _response(identifier: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _error(identifier: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "error": {"code": code, "message": message},
    }


def _write(message: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(dict(message), separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run() -> int:
    tools = _load_tools(Path(_required_environment("PROOF_ASSISTANT_MCP_TOOLS")))
    names = {str(item["name"]) for item in tools}
    proxy: _ToolProxy | None = None
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(request, Mapping):
                continue
            identifier = request.get("id")
            method = str(request.get("method") or "")
            raw_params = request.get("params")
            params = raw_params if isinstance(raw_params, Mapping) else {}
            # JSON-RPC notifications have no id and receive no response.
            if method == "notifications/initialized":
                continue
            if method == "initialize":
                requested_version = params.get("protocolVersion")
                _write(
                    _response(
                        identifier,
                        {
                            "protocolVersion": (
                                requested_version
                                if isinstance(requested_version, str)
                                else "2025-06-18"
                            ),
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {
                                "name": "proof-assistant-tools",
                                "version": "0.1.0",
                            },
                        },
                    )
                )
            elif method == "ping":
                _write(_response(identifier, {}))
            elif method == "tools/list":
                _write(_response(identifier, {"tools": list(tools)}))
            elif method == "tools/call":
                name = str(params.get("name") or "")
                raw_arguments = params.get("arguments")
                arguments = raw_arguments if isinstance(raw_arguments, Mapping) else {}
                if name not in names:
                    _write(_error(identifier, -32602, f"Unknown tool {name!r}"))
                    continue
                try:
                    if proxy is None:
                        proxy = _ToolProxy()
                    result, success = proxy.call(name, arguments)
                except Exception as exc:
                    # Never include provider environment or command output here.
                    result = f"Proof Assistant tool bridge error: {type(exc).__name__}"
                    success = False
                _write(
                    _response(
                        identifier,
                        {
                            "content": [{"type": "text", "text": result}],
                            "isError": not success,
                        },
                    )
                )
            elif identifier is not None:
                _write(_error(identifier, -32601, f"Unsupported method {method!r}"))
    finally:
        if proxy is not None:
            proxy.close()
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except Exception as exc:
        print(
            f"proof-assistant MCP bridge failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
