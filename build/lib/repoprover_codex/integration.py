from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .backend import CodexBackend, CodexConfig, CodexResult


@dataclass
class RepoProverCodexRun:
    codex: CodexResult
    agent_type: str


def run_repoprover_agent(
    agent: Any,
    *,
    run_kwargs: dict[str, Any],
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
