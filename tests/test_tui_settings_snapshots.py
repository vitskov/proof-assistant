from __future__ import annotations

from collections.abc import Callable

import pytest
from textual.containers import VerticalScroll
from textual.pilot import Pilot

from proof_assistant.ai import (
    Difficulty,
    DriverId,
    ProviderConfig,
    TaskKind,
    TaskPreference,
)
from proof_assistant.tui import ProofAssistantApp
from proof_assistant.tui.layout import ActionBar
from proof_assistant.tui.settings import AIProviderSettingsScreen
from proof_assistant.tui.settings.components import RoleRoster
from proof_assistant.tui.theme import PROOF_DARK_THEME, PROOF_LIGHT_THEME
from tests.test_tui_providers import ProviderWorkflowFake, _snapshot, wait_for

_CLAUDE_DEFAULTS = {
    TaskKind.CLARIFICATION: ("opus", Difficulty.HIGH),
    TaskKind.DIAGNOSTIC: ("opus", Difficulty.HIGH),
    TaskKind.PROOF: ("best", Difficulty.HIGH),
    TaskKind.SKETCH: ("sonnet", Difficulty.MEDIUM),
    TaskKind.MAINTENANCE: ("sonnet", Difficulty.MEDIUM),
    TaskKind.REVIEW: ("opus", Difficulty.HIGH),
    TaskKind.DUPLICATE_PROOF: ("fable", Difficulty.XHIGH),
    TaskKind.REPORTING: ("haiku", Difficulty.LOW),
}


def _claude_settings_app() -> ProofAssistantApp:
    config = ProviderConfig(
        primary_driver=DriverId.CLAUDE_CLI,
        tasks=tuple(
            TaskPreference(
                task,
                driver=DriverId.CLAUDE_CLI,
                model=model,
                difficulty=difficulty,
            )
            for task, (model, difficulty) in _CLAUDE_DEFAULTS.items()
        ),
    )
    snapshot = _snapshot(primary=DriverId.CLAUDE_CLI, config=config)
    return ProofAssistantApp(ProviderWorkflowFake(snapshot))  # type: ignore[arg-type]


async def _open_role_assignments(pilot: Pilot[None], theme: str) -> None:
    app = pilot.app
    assert isinstance(app, ProofAssistantApp)
    app.theme = theme
    await wait_for(pilot, lambda: app._ai_setup_snapshot is not None)
    app.show_ai_provider_settings(app._ai_setup_snapshot)
    await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
    await wait_for(
        pilot,
        lambda: app.screen.query_one("#ai-role-roster", RoleRoster).row_count == 8,
    )
    await pilot.pause()
    roster = app.screen.query_one("#ai-role-roster", RoleRoster)
    actions = app.screen.query_one("#ai-settings-actions", ActionBar)
    role_page = app.screen.query_one("#roles-page", VerticalScroll)
    assert roster.row_count == len(TaskKind) == 8
    assert roster.max_scroll_y == 0
    if app.size.width >= 120:
        assert roster.region.bottom <= actions.region.y
    else:
        assert role_page.max_scroll_y > 0
    assert not app.screen.query("#ai-api-key").nodes


@pytest.mark.parametrize("terminal_size", ((80, 24), (120, 40), (140, 48)))
@pytest.mark.parametrize("theme", (PROOF_DARK_THEME.name, PROOF_LIGHT_THEME.name))
def test_role_aware_settings_matches_reviewed_snapshot(
    snap_compare: Callable[..., bool],
    terminal_size: tuple[int, int],
    theme: str,
) -> None:
    app = _claude_settings_app()

    async def run_before(pilot: Pilot[None]) -> None:
        await _open_role_assignments(pilot, theme)

    assert snap_compare(
        app,
        terminal_size=terminal_size,
        run_before=run_before,
    )
