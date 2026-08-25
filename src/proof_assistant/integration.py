from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .ai import DriverId, TaskKind
from .ai.execution import AIBackend, AIBackendConfig, AITurnResult
from .backend import CodexConfig
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
    """Compatibility name for a provider-neutral RepoProver agent turn."""

    codex: AITurnResult
    agent_type: str

    @property
    def ai(self) -> AITurnResult:
        return self.codex


def run_repoprover_agent(
    agent: RepoProverAgent,
    *,
    run_kwargs: dict[str, object],
    codex: CodexConfig | None = None,
    ai: AIBackendConfig | None = None,
) -> RepoProverCodexRun:
    """Run one already-constructed RepoProver agent through an AI driver.

    This is a deliberately narrow integration seam for internal testing. It uses
    the public-ish methods already present on BaseAgent subclasses:
    get_system_prompt(), build_user_prompt(), get_tools(), handle_tool_call().

    It does NOT replace BaseAgent.run globally and does not alter upstream files.
    """
    system_prompt = agent.get_system_prompt()
    user_prompt = agent.build_user_prompt(**run_kwargs)
    tools = agent.get_tools()
    cwd = getattr(agent, "repo_root", None)

    if (codex is None) == (ai is None):
        raise ValueError("Provide exactly one of codex= or ai=")
    if ai is None:
        assert codex is not None
        ai = AIBackendConfig(
            driver=DriverId.CODEX_CLI,
            model=codex.model,
            difficulty=codex.effort,
            executable=codex.executable,
            request_timeout=codex.request_timeout,
            turn_timeout=codex.turn_timeout,
            concurrency=codex.concurrency,
            task_kind=TaskKind(codex.ai_task_class.value),
        )
    backend = AIBackend(ai, cwd=cwd)
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
