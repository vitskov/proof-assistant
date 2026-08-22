"""Tiny RepoProver-like object for adapter experiments without importing RepoProver."""

class ToyAgent:
    agent_type = "toy"
    repo_root = None

    def get_system_prompt(self):
        return "Use the supplied tool and then finish."

    def build_user_prompt(self, **kwargs):
        return kwargs["prompt"]

    def get_tools(self):
        return [{
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }]

    def handle_tool_call(self, name, arguments):
        if name != "echo":
            return f"Error: unknown tool {name}"
        return arguments["text"]
