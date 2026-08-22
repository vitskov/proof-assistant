
import os
import stat
import subprocess
import textwrap

import pytest

from repoprover_codex import protocol
from repoprover_codex.protocol import (
    AppServerClient,
    CodexProtocolError,
    isolated_skill_config_args,
    isolated_tool_config_args,
)


def test_real_stdio_bidirectional_request_response(tmp_path):
    fake = tmp_path / "fake-codex"
    fake.write_text(
        textwrap.dedent(
            r"""#!/usr/bin/env python3
import json, sys

def send(x):
    print(json.dumps(x), flush=True)

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"id": msg["id"], "result": {"ok": True}})
    elif method == "initialized":
        pass
    elif method == "ping":
        # Exercise server -> host request while a host request is pending.
        send({
            "id": 900,
            "method": "item/tool/call",
            "params": {
                "threadId": "thr",
                "turnId": "turn",
                "callId": "call",
                "tool": "echo",
                "arguments": {"text": "hello"}
            }
        })
        reply = json.loads(sys.stdin.readline())
        assert reply["id"] == 900
        assert reply["result"]["success"] is True
        send({"id": msg["id"], "result": {"toolResult": reply["result"]}})
"""
        )
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    client = AppServerClient(str(fake))
    client.register_request_handler(
        "item/tool/call",
        lambda params: {
            "contentItems": [
                {"type": "inputText", "text": params["arguments"]["text"]}
            ],
            "success": True,
        },
    )
    try:
        assert client.request("initialize", {}) == {"ok": True}
        client.notify("initialized", {})
        result = client.request("ping", {})
    finally:
        client.close()

    assert result["toolResult"]["success"] is True
    assert result["toolResult"]["contentItems"][0]["text"] == "hello"


def test_missing_codex_executable_has_structured_error(tmp_path):
    client = AppServerClient(str(tmp_path / "does-not-exist"))
    with pytest.raises(CodexProtocolError, match="executable not found"):
        client.start()


def test_request_timeout_is_reported_and_client_can_close(tmp_path):
    fake = tmp_path / "silent-codex"
    fake.write_text(
        textwrap.dedent(
            """#!/usr/bin/env python3
import sys
for _line in sys.stdin:
    pass
"""
        )
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    client = AppServerClient(str(fake))
    try:
        with pytest.raises(TimeoutError, match="initialize"):
            client.request("initialize", {}, timeout=0.02)
    finally:
        client.close()


def test_app_server_crash_unblocks_pending_request(tmp_path):
    fake = tmp_path / "crashing-codex"
    fake.write_text(
        textwrap.dedent(
            """#!/usr/bin/env python3
import sys
sys.stdin.readline()
raise SystemExit(7)
"""
        )
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    client = AppServerClient(str(fake))
    try:
        with pytest.raises(CodexProtocolError, match="app-server exited"):
            client.request("initialize", {}, timeout=2)
    finally:
        client.close()


def test_external_tools_are_disabled_in_child_config(monkeypatch):
    listing = '[{"name":"context7","enabled":true},{"name":"google-docs","enabled":true},{"name":"off","enabled":false}]'

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=listing, stderr="")

    monkeypatch.setattr(protocol.subprocess, "run", fake_run)
    assert isolated_tool_config_args("codex") == [
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "-c",
        'mcp_servers.context7={command="true",enabled=false}',
        "-c",
        'mcp_servers.google-docs={command="true",enabled=false}',
    ]


def test_unsafe_mcp_server_name_fails_closed(monkeypatch):
    listing = '[{"name":"unsafe.name","enabled":true}]'

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=listing, stderr="")

    monkeypatch.setattr(protocol.subprocess, "run", fake_run)
    with pytest.raises(CodexProtocolError, match="unsupported name"):
        isolated_tool_config_args("codex")


def test_local_skills_are_disabled_by_path_in_child_config(monkeypatch, tmp_path):
    class FakeProbe:
        instance = None

        def __init__(self, executable, *, cwd, env, extra_args):
            self.executable = executable
            self.cwd = cwd
            self.extra_args = extra_args
            self.closed = False
            FakeProbe.instance = self

        def request(self, method, params, *, timeout):
            if method == "initialize":
                return {"serverInfo": {"name": "fake"}}
            if method == "skills/list":
                return {
                    "data": [
                        {
                            "cwd": str(tmp_path),
                            "skills": [
                                {
                                    "path": "/opt/codex/a/SKILL.md",
                                    "enabled": True,
                                },
                                {
                                    "path": "/opt/codex/b/SKILL.md",
                                    "enabled": False,
                                },
                            ],
                            "errors": [],
                        }
                    ]
                }
            raise AssertionError(method)

        def notify(self, method, params):
            assert method == "initialized"

        def close(self):
            self.closed = True

    monkeypatch.setattr(protocol, "AppServerClient", FakeProbe)
    external_args = ["--disable", "apps", "--disable", "plugins"]
    args = isolated_skill_config_args(
        "codex",
        cwd=tmp_path,
        external_tool_args=external_args,
    )
    assert args == [
        "-c",
        "skills.include_instructions=false",
        "-c",
        "skills.bundled.enabled=false",
        "-c",
        'skills.config=[{path="/opt/codex/a/SKILL.md",enabled=false},'
        '{path="/opt/codex/b/SKILL.md",enabled=false}]',
    ]
    assert FakeProbe.instance.extra_args[:4] == external_args
    assert FakeProbe.instance.closed is True
