"""Deterministic responsive-layout app for snapshots and textual-dev inspection.

Manual quality-control command::

    textual run --dev tests/textual_dev_app.py
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.events import Resize
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from proof_assistant.tui.layout import (
    COMPOSITION_CLASSES,
    HORIZONTAL_BREAKPOINTS,
    VERTICAL_BREAKPOINTS,
    ActionBar,
    PageHeader,
    PageWorkspace,
    ResponsivePage,
    ResponsiveToolbar,
    classify_viewport,
)
from proof_assistant.tui.theme import (
    DEFAULT_PROOF_THEME,
    PROOF_THEMES,
    THEME_VARIABLE_DEFAULTS,
)


class ResponsiveFoundationApp(App[None]):
    """A service-free visual contract for the shared responsive primitives."""

    HORIZONTAL_BREAKPOINTS = HORIZONTAL_BREAKPOINTS
    VERTICAL_BREAKPOINTS = VERTICAL_BREAKPOINTS
    CSS = """
    Screen {
        background: $proof-page-background;
        color: $foreground;
    }
    ResponsivePage {
        width: 100%;
        height: 100%;
        layout: vertical;
        overflow: hidden;
        background: $proof-page-background;
    }
    PageHeader {
        height: 3;
        padding: 0 1;
        background: $proof-chrome-background;
    }
    #title { text-style: bold; }
    #context { color: $proof-muted; }
    PageWorkspace {
        height: 1fr;
        overflow-x: hidden;
        overflow-y: auto;
        padding: 1;
        background: $proof-panel-background;
    }
    ResponsiveToolbar {
        height: auto;
        margin-bottom: 1;
    }
    ResponsiveToolbar Button {
        width: auto;
        margin-right: 1;
    }
    #role-content {
        height: 1fr;
    }
    #role-list {
        width: 34;
        height: 1fr;
        margin-right: 1;
    }
    #role-detail {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        border: round $proof-panel-border;
        background: $proof-input-background;
    }
    ActionBar {
        height: 3;
        padding: 0 1;
        align-vertical: middle;
        background: $proof-chrome-background;
    }
    ActionBar Label { width: 1fr; }
    ActionBar Button { width: auto; }
    .compact ResponsiveToolbar, .compact-short ResponsiveToolbar {
        layout: vertical;
    }
    .compact #role-list, .compact-short #role-list {
        width: 100%;
        margin-right: 0;
    }
    .compact #role-detail, .compact-short #role-detail {
        display: none;
    }
    .standard #role-list { width: 100%; }
    .standard #role-detail { display: none; }
    """

    def __init__(self, *, selected_theme: str = DEFAULT_PROOF_THEME) -> None:
        super().__init__()
        self.selected_theme = selected_theme

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return THEME_VARIABLE_DEFAULTS

    def compose(self) -> ComposeResult:
        with ResponsivePage():
            with PageHeader():
                yield Static("Settings / Verification AI", id="title")
                yield Static("Scope: Machine defaults", id="context")
            with PageWorkspace():
                with ResponsiveToolbar():
                    yield Button("Use provider defaults", variant="primary")
                    yield Button("Restore role")
                with Horizontal(id="role-content"):
                    yield OptionList(
                        Option("Author clarification       Fable   Extra high"),
                        Option("Scan / triage diagnostics  Opus    High"),
                        Option("Primary prove agent        Best    High"),
                        Option("Sketch agent               Sonnet  Medium"),
                        Option("Maintain / fix agent       Sonnet  Medium"),
                        Option("Math / engineering review  Opus    High"),
                        Option("Independent prove agent    Fable   Extra high"),
                        Option("Progress / reporting       Haiku   Low"),
                        id="role-list",
                    )
                    yield Static(
                        "[b]Independent prove agent[/b]\n\n"
                        "Independently rechecks the primary proof.\n\n"
                        "Model: Fable\nEffort: Extra high\nState: Recommended",
                        id="role-detail",
                    )
            with ActionBar():
                yield Label("Saved · Claude CLI · 8 roles configured")
                yield Button("Done", variant="primary")

    def on_mount(self) -> None:
        for theme in PROOF_THEMES:
            self.register_theme(theme)
        self.theme = self.selected_theme
        self._apply_composition(self.size.width, self.size.height)

    def on_resize(self, event: Resize) -> None:
        self._apply_composition(event.size.width, event.size.height)

    def _apply_composition(self, width: int, height: int) -> None:
        self.remove_class(*COMPOSITION_CLASSES)
        self.add_class(classify_viewport(width, height).value)


if __name__ == "__main__":
    ResponsiveFoundationApp().run()
