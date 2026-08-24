from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .backend import CodexBackend, CodexConfig, CodexResult
from .json_types import JSONObject


class RepoProverAgent(Protocol):
    """The narrow structural interface used from optional RepoProver agents."""

    repo_root: str | Path
    agent_type: object

    def get_system_prompt(self) -> str: ...

    def build_user_prompt(self, **run_kwargs: object) -> str: ...

    def get_tools(self) -> list[JSONObject]: ...

    def handle_tool_call(self, name: str, arguments: JSONObject) -> str: ...


@dataclass
class RepoProverCodexRun:
    codex: CodexResult
    agent_type: str


def run_repoprover_agent(
    agent: RepoProverAgent,
    *,
    run_kwargs: dict[str, object],
    codex: CodexConfig,
) -> RepoProverCodexRun:
    """Run one already-constructed RepoProver agent through Codex.

    This is a deliberately narrow integration seam for internal testing. It uses
    the public-ish methods already present on BaseAgent subclasses:
    get_system_prompt(), build_user_prompt(), get_tools(), handle_tool_call().

    It does NOT replace BaseAgent.run globally and does not alter upstream files.
    """
    system_prompt = agent.get_system_prompt()
    user_prompt = agent.build_user_prompt(**run_kwargs)
    tools = agent.get_tools()
    cwd = getattr(agent, "repo_root", None)

    backend = CodexBackend(codex, cwd=cwd)
    try:
        result = backend.run(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            tool_handler=agent.handle_tool_call,
        )
    finally:
        backend.close()

    return RepoProverCodexRun(
        codex=result,
        agent_type=str(getattr(agent, "agent_type", type(agent).__name__)),
    )
