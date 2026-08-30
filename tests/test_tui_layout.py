from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.pilot import Pilot
from textual.widgets import Button, ContentSwitcher, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from proof_assistant.tui.app import ProofAssistantApp, ResizeNeededScreen
from proof_assistant.tui.layout import (
    COMPOSITION_CLASSES,
    HORIZONTAL_BREAKPOINTS,
    VERTICAL_BREAKPOINTS,
    ActionBar,
    PageWorkspace,
    ResponsivePage,
    ViewportComposition,
    classify_viewport,
)
from proof_assistant.tui.screens import WelcomeScreen
from tests.tui_geometry import (
    assert_focus_is_visible,
    assert_inside_viewport,
    assert_regions_do_not_overlap,
)


def async_test[Result](
    function: Callable[..., Awaitable[Result]],
) -> Callable[..., Result]:
    """Run a Pilot coroutine without requiring pytest-asyncio."""

    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> Result:
        return asyncio.run(function(*args, **kwargs))

    return run


@pytest.mark.parametrize(
    ("size", "expected"),
    (
        ((79, 24), ViewportComposition.RESIZE_NEEDED),
        ((80, 23), ViewportComposition.RESIZE_NEEDED),
        ((80, 24), ViewportComposition.COMPACT),
        ((100, 32), ViewportComposition.COMPACT),
        ((119, 40), ViewportComposition.COMPACT),
        ((120, 31), ViewportComposition.COMPACT_SHORT),
        ((120, 32), ViewportComposition.STANDARD),
        ((139, 40), ViewportComposition.STANDARD),
        ((140, 39), ViewportComposition.COMPACT_SHORT),
        ((140, 40), ViewportComposition.WIDE),
        ((120, 40), ViewportComposition.STANDARD),
        ((140, 48), ViewportComposition.WIDE),
    ),
)
def test_classify_viewport_matches_design_boundaries(
    size: tuple[int, int], expected: ViewportComposition
) -> None:
    assert classify_viewport(*size) is expected


class _ResponsiveSettingsHarness(App[None]):
    """Small deterministic harness for responsive navigation invariants."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #responsive-page {
        height: 100%;
    }

    #workspace {
        height: 1fr;
    }

    #action-bar {
        height: 3;
    }

    #category-nav {
        width: 28;
    }

    #pages {
        height: 1fr;
    }

    .settings-page {
        height: 1fr;
    }
    """
    HORIZONTAL_BREAKPOINTS = HORIZONTAL_BREAKPOINTS
    VERTICAL_BREAKPOINTS = VERTICAL_BREAKPOINTS

    def compose(self) -> ComposeResult:
        with ResponsivePage(id="responsive-page"):
            with PageWorkspace(id="workspace"):
                yield OptionList(
                    Option("Role assignments", id="roles"),
                    Option("Provider connections", id="providers"),
                    id="category-nav",
                )
                with ContentSwitcher(initial="roles-page", id="pages"):
                    with _InertSettingsPane(id="roles-page", classes="settings-page"):
                        yield Button("Edit primary proof", id="edit-role")
                        for index in range(18):
                            yield Label(f"Role detail line {index}")
                    with _InertSettingsPane(
                        id="providers-page", classes="settings-page"
                    ):
                        yield Static("No credential values are retained here")
                        yield Input(password=True, id="provider-secret")
                        yield Button("Run provider check", id="provider-check")
                        for index in range(18):
                            yield Label(f"Provider detail line {index}")
            with ActionBar(id="action-bar"):
                yield Static("Saved · Machine defaults")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        switcher = self.query_one("#pages", ContentSwitcher)
        current = switcher.current
        if current is not None:
            for secret_input in switcher.query(f"#{current} Input"):
                secret_input.value = ""
        switcher.current = f"{event.option.id}-page"


class _InertSettingsPane(Vertical):
    """A lightweight pane with no pane-owned workers, timers, or secret state."""


async def _select_option(pilot: Pilot[None], option_index: int) -> None:
    navigation = pilot.app.query_one("#category-nav", OptionList)
    navigation.focus()
    navigation.highlighted = option_index
    await pilot.press("enter")
    await pilot.pause()


@async_test
async def test_resize_applies_exactly_one_composition_class() -> None:
    app = ProofAssistantApp(object())  # type: ignore[arg-type]
    async with app.run_test(size=(80, 24)) as pilot:
        for width, height in ((120, 31), (120, 32), (140, 39), (140, 40)):
            await pilot.resize_terminal(width, height)
            expected = classify_viewport(width, height).value
            applied = {name for name in COMPOSITION_CLASSES if app.has_class(name)}
            assert applied == {expected}


@async_test
async def test_below_minimum_viewport_blocks_controls_until_resize() -> None:
    app = ProofAssistantApp(object())  # type: ignore[arg-type]
    async with app.run_test(size=(79, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ResizeNeededScreen)
        assert "79x24" in str(
            app.screen.query_one("#resize-needed-message", Static).render()
        )

        for key in ("f2", "f3", "escape"):
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, ResizeNeededScreen)

        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert isinstance(app.screen, WelcomeScreen)


@async_test
async def test_textual_applies_expected_axis_breakpoint_classes() -> None:
    app = ProofAssistantApp(object())  # type: ignore[arg-type]
    async with app.run_test(size=(80, 24)) as pilot:
        cases = (
            ((119, 31), ("-h-compact", "-v-compact")),
            ((120, 32), ("-h-standard", "-v-standard")),
            ((140, 40), ("-h-wide", "-v-wide")),
        )
        for (width, height), expected in cases:
            await pilot.resize_terminal(width, height)
            axis_classes = {
                name
                for _threshold, name in (*HORIZONTAL_BREAKPOINTS, *VERTICAL_BREAKPOINTS)
                if app.screen.has_class(name)
            }
            assert axis_classes == set(expected)


@async_test
async def test_resize_preserves_content_switcher_selection() -> None:
    app = _ResponsiveSettingsHarness()
    async with app.run_test(size=(120, 40)) as pilot:
        await _select_option(pilot, 1)
        switcher = app.query_one("#pages", ContentSwitcher)
        assert switcher.current == "providers-page"

        await pilot.resize_terminal(80, 24)

        assert switcher.current == "providers-page"


@async_test
async def test_resize_keeps_fixed_action_bar_inside_viewport() -> None:
    app = _ResponsiveSettingsHarness()
    async with app.run_test(size=(140, 48)) as pilot:
        action_bar = app.query_one("#action-bar", ActionBar)
        for width, height in ((120, 40), (80, 24), (140, 40), (140, 48)):
            await pilot.resize_terminal(width, height)
            assert_inside_viewport(app, action_bar)
            assert action_bar.region.bottom == height


@async_test
async def test_fixed_action_bar_does_not_overlap_primary_workspace() -> None:
    app = _ResponsiveSettingsHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        workspace = app.query_one("#workspace", PageWorkspace)
        action_bar = app.query_one("#action-bar", ActionBar)

        assert_regions_do_not_overlap(workspace, action_bar)
        assert workspace.region.bottom <= action_bar.region.y


@async_test
async def test_resize_keeps_keyboard_focus_visible() -> None:
    app = _ResponsiveSettingsHarness()
    async with app.run_test(size=(140, 48)) as pilot:
        await _select_option(pilot, 1)
        button = app.query_one("#provider-check", Button)
        button.focus()
        await pilot.pause()

        await pilot.resize_terminal(80, 24)
        await pilot.pause()

        assert_focus_is_visible(app)


@async_test
async def test_hidden_content_switcher_pane_is_not_focusable() -> None:
    app = _ResponsiveSettingsHarness()
    async with app.run_test(size=(120, 40)) as pilot:
        await _select_option(pilot, 1)
        hidden_button = app.query_one("#edit-role", Button)

        assert not hidden_button.is_on_screen
        assert hidden_button not in list(app.screen.focus_chain)


@async_test
async def test_hidden_content_switcher_pane_does_not_retain_secret_draft() -> None:
    app = _ResponsiveSettingsHarness()
    async with app.run_test(size=(120, 40)) as pilot:
        await _select_option(pilot, 1)
        secret = app.query_one("#provider-secret", Input)
        secret.value = "sensitive-token-value"

        await _select_option(pilot, 0)

        assert secret.value == ""


@async_test
async def test_visible_settings_page_has_one_primary_vertical_scroll_owner() -> None:
    app = _ResponsiveSettingsHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        visible_scrolls = [
            widget
            for widget in app.query(VerticalScroll)
            if widget.is_on_screen and widget.styles.overflow_y in ("auto", "scroll")
        ]

        assert [widget.id for widget in visible_scrolls] == ["workspace"]
