
import os
import stat
import textwrap

from repoprover_codex.protocol import AppServerClient


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
