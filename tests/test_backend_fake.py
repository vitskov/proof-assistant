from repoprover_codex.backend import CodexBackend, CodexConfig


class FakeClient:
    def __init__(self):
        self.handlers = {}
        self.requests = []
        self.notifications = []
        self.event_stream = [
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": "bridge succeeded",
                    }
                },
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            },
        ]

    def register_request_handler(self, method, handler):
        self.handlers[method] = handler

    def start(self):
        pass

    def close(self):
        pass

    def notify(self, method, params=None):
        self.notifications.append((method, params or {}))

    def request(self, method, params=None, timeout=0):
        self.requests.append((method, params or {}))
        if method == "initialize":
            return {"serverInfo": {"name": "fake"}}
        if method == "model/list":
            return {
                "data": [
                    {
                        "model": "gpt-test",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "high", "description": "test"}
                        ],
                    }
                ]
            }
        if method == "mcpServerStatus/list":
            return {"data": [], "nextCursor": None}
        if method == "skills/list":
            return {"data": [{"cwd": "/tmp", "skills": [], "errors": []}]}
        if method == "thread/start":
            assert params["dynamicTools"][0]["name"] == "lean_check"
            assert params["selectedCapabilityRoots"] == []
            assert params["environments"] == []
            return {"thread": {"id": "thr-1"}}
        if method == "turn/start":
            assert params["model"] == "gpt-test"
            assert params["effort"] == "high"
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(method)

    def next_notification(self, timeout=None):
        return self.event_stream.pop(0)


def test_backend_full_fake_roundtrip_and_dynamic_tool():
    client = FakeClient()
    backend = CodexBackend(
        CodexConfig(model="gpt-test", effort="high"),
        client=client,
    )

    seen = []

    def handler(name, args):
        seen.append((name, args))
        return "Lean says OK"

    tool = {
        "type": "function",
        "function": {
            "name": "lean_check",
            "description": "Check Lean",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    # Exercise the server-originated tool-call handler directly; AppServerClient
    # integration is separately covered by the protocol simulator test.
    dynamic_result = client.handlers["item/tool/call"](
        {"tool": "lean_check", "arguments": {"code": "example"}}
    )
    # Handler is installed during run(), so this pre-run call should fail closed.
    assert dynamic_result["success"] is False

    result = backend.run(
        system_prompt="system",
        user_prompt="user",
        tools=[tool],
        tool_handler=handler,
    )
    assert result.final_text == "bridge succeeded"
    assert result.thread_id == "thr-1"
    assert result.turn_id == "turn-1"

    dynamic_result = client.handlers["item/tool/call"](
        {"tool": "lean_check", "arguments": {"code": "example"}}
    )
    assert dynamic_result["success"] is True
    assert dynamic_result["contentItems"][0]["text"] == "Lean says OK"
    assert seen == [("lean_check", {"code": "example"})]
