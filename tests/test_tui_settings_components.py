from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import fields
from functools import wraps
from pathlib import Path
from typing import Any

import pytest
import textual
from textual.app import App, ComposeResult

import proof_assistant.tui.settings.components as components
from proof_assistant.ai import SUPPORTED_DRIVERS
from proof_assistant.ai.contracts import (
    Difficulty,
    DriverId,
    DriverTransport,
    TaskKind,
)
from proof_assistant.tui.settings.components import (
    ProviderConnectionRoster,
    ProviderConnectionRow,
    ProviderDefaultsPreviewState,
    RoleAssignmentRow,
    RoleDraft,
    RoleRoster,
    SettingsScopeSelector,
)
from proof_assistant.workflow.contracts import SettingsScopeKind


def async_test[Result](
    function: Callable[..., Awaitable[Result]],
) -> Callable[..., Result]:
    """Run a Pilot coroutine without requiring pytest-asyncio."""

    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> Result:
        return asyncio.run(function(*args, **kwargs))

    return run


def _role_rows(*, state: str = "Ready") -> tuple[RoleAssignmentRow, ...]:
    return tuple(
        RoleAssignmentRow(task, f"model-{index}", Difficulty.HIGH, state)
        for index, task in enumerate(reversed(tuple(TaskKind)))
    )


def _role_draft(
    *,
    driver: DriverId = DriverId.CODEX_CLI,
    effort: Difficulty = Difficulty.MEDIUM,
) -> tuple[RoleDraft, ...]:
    return tuple(
        RoleDraft(task, driver, f"model-{index}", effort)
        for index, task in enumerate(reversed(tuple(TaskKind)))
    )


def _provider_rows() -> tuple[ProviderConnectionRow, ...]:
    return tuple(
        ProviderConnectionRow(
            driver,
            DriverTransport.CLI,
            "Connected",
            "Ready",
            primary=driver is DriverId.CLAUDE_CLI,
        )
        for driver in reversed(SUPPORTED_DRIVERS)
    )


class _RoleRosterApp(App[None]):
    def __init__(self, rows: Iterable[RoleAssignmentRow]) -> None:
        super().__init__()
        self.rows = tuple(rows)

    def compose(self) -> ComposeResult:
        yield RoleRoster(self.rows, id="roles")


class _ProviderRosterApp(App[None]):
    def compose(self) -> ComposeResult:
        yield ProviderConnectionRoster(_provider_rows(), id="providers")


class _ScopeSelectorApp(App[None]):
    def __init__(self, scope: SettingsScopeKind) -> None:
        super().__init__()
        self.scope = scope

    def compose(self) -> ComposeResult:
        yield SettingsScopeSelector(scope=self.scope, id="scope")


def test_component_suite_runs_on_textual_8_2_8() -> None:
    assert textual.__version__ == "8.2.8"


@async_test
async def test_role_roster_renders_exactly_eight_canonical_task_rows() -> None:
    app = _RoleRosterApp(_role_rows())
    async with app.run_test() as pilot:
        await pilot.pause()
        roster = app.query_one("#roles", RoleRoster)

        assert roster.row_count == 8
        assert [roster.get_row_at(index)[0] for index in range(roster.row_count)] == [
            "Author clarification",
            "Scan / triage diagnostics",
            "Primary prove agent",
            "Sketch agent",
            "Maintain / fix agent",
            "Math and engineering reviewers",
            "Independent prove agent",
            "Progress / reporting agent",
        ]


@async_test
async def test_duplicate_proof_role_uses_independent_prove_agent_label() -> None:
    app = _RoleRosterApp(_role_rows())
    async with app.run_test() as pilot:
        await pilot.pause()
        roster = app.query_one("#roles", RoleRoster)

        assert roster.get_row(TaskKind.DUPLICATE_PROOF.value)[0] == (
            "Independent prove agent"
        )


@async_test
async def test_role_roster_preserves_selected_task_after_row_refresh() -> None:
    app = _RoleRosterApp(_role_rows())
    async with app.run_test() as pilot:
        await pilot.pause()
        roster = app.query_one("#roles", RoleRoster)
        roster.select_task(TaskKind.REVIEW)

        roster.set_roles(_role_rows(state="Changed"))
        await pilot.pause()

        assert roster.selected_task is TaskKind.REVIEW


@async_test
async def test_provider_roster_renders_every_provider_in_canonical_order() -> None:
    app = _ProviderRosterApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        roster = app.query_one("#providers", ProviderConnectionRoster)

        assert [roster.get_row_at(index)[0] for index in range(roster.row_count)] == [
            "OpenAI Codex CLI",
            "Anthropic Claude Code CLI (primary)",
        ]


@pytest.mark.parametrize(
    "scope", (SettingsScopeKind.MACHINE, SettingsScopeKind.PROJECT)
)
@async_test
async def test_scope_selector_exposes_requested_machine_or_project_scope(
    scope: SettingsScopeKind,
) -> None:
    app = _ScopeSelectorApp(scope)
    async with app.run_test() as pilot:
        await pilot.pause()
        selector = app.query_one("#scope", SettingsScopeSelector)

        assert selector.scope is scope


def test_complete_role_draft_is_canonicalized_by_task_order() -> None:
    state = ProviderDefaultsPreviewState.from_draft(_role_draft())

    assert tuple(item.task for item in state.draft) == tuple(TaskKind)


def test_role_draft_rejects_a_missing_task() -> None:
    with pytest.raises(ValueError, match="missing: reporting"):
        ProviderDefaultsPreviewState.from_draft(_role_draft()[1:])


def test_role_draft_rejects_a_duplicate_task() -> None:
    draft = _role_draft()

    with pytest.raises(ValueError, match="each task exactly once"):
        ProviderDefaultsPreviewState.from_draft((*draft[:-1], draft[0]))


def test_defaults_undo_restores_draft_before_latest_preview() -> None:
    original = ProviderDefaultsPreviewState.from_draft(_role_draft())
    claude = _role_draft(driver=DriverId.CLAUDE_CLI, effort=Difficulty.HIGH)
    codex_xhigh = _role_draft(driver=DriverId.CODEX_CLI, effort=Difficulty.XHIGH)

    restored = original.preview(claude).preview(codex_xhigh).undo()

    assert restored.draft == ProviderDefaultsPreviewState.from_draft(claude).draft


def test_accepting_defaults_clears_the_one_level_undo() -> None:
    state = ProviderDefaultsPreviewState.from_draft(_role_draft()).preview(
        _role_draft(driver=DriverId.CLAUDE_CLI)
    )

    accepted = state.accept()

    assert not accepted.can_undo


@pytest.mark.parametrize(
    "state_type",
    (
        RoleAssignmentRow,
        ProviderConnectionRow,
        RoleDraft,
        ProviderDefaultsPreviewState,
    ),
)
def test_component_state_contracts_declare_no_credential_fields(
    state_type: type[object],
) -> None:
    credential_terms = {"api_key", "credential", "password", "secret", "token"}

    assert credential_terms.isdisjoint(
        field.name.casefold() for field in fields(state_type)
    )


def test_defaults_state_repr_contains_no_credential_like_values() -> None:
    states = (
        *_role_rows(),
        *_provider_rows(),
        *_role_draft(),
        ProviderDefaultsPreviewState.from_draft(_role_draft()),
    )
    representation = repr(states).casefold()

    assert all(
        term not in representation
        for term in ("api_key", "password", "secret", "token", "credential")
    )


def test_components_import_textual_exclusively_through_public_modules() -> None:
    source_path = Path(components.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    private_imports: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "textual"
        ):
            module_parts = (node.module or "").split(".")
            if any(part.startswith("_") for part in module_parts):
                private_imports.append(node.module or "")
            private_imports.extend(
                alias.name for alias in node.names if alias.name.startswith("_")
            )
        elif isinstance(node, ast.Import):
            private_imports.extend(
                alias.name
                for alias in node.names
                if alias.name.startswith("textual.")
                and any(part.startswith("_") for part in alias.name.split("."))
            )

    assert private_imports == []
