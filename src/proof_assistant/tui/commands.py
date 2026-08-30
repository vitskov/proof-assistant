"""Central command vocabulary for the terminal interface.

Bindings, footer labels, and the keyboard-reference screen all use these
definitions.  Keeping the user-facing command names here prevents individual
screens from inventing subtly different labels for the same interaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from textual.app import ComposeResult, RenderResult
from textual.binding import Binding
from textual.content import Content
from textual.events import Click
from textual.reactive import Reactive
from textual.widget import Widget
from textual.widgets import Footer, Static


class AppHeaderIcon(Widget):
    """Command-palette affordance at the left edge of the app header."""

    DEFAULT_CSS = """
    AppHeaderIcon {
        dock: left;
        padding: 0 1;
        width: 8;
        content-align: left middle;
    }
    AppHeaderIcon:hover { background: $foreground 10%; }
    """

    icon: Reactive[str] = Reactive("⭘")

    def on_mount(self) -> None:
        if self.app.ENABLE_COMMAND_PALETTE:
            self.tooltip = "Open the command palette"
        else:
            self.disabled = True

    async def on_click(self, event: Click) -> None:
        event.stop()
        await self.run_action("app.command_palette")

    def render(self) -> RenderResult:
        return self.icon


class AppHeaderClockSpace(Widget):
    """Right-side space that keeps the header title centered."""

    DEFAULT_CSS = """
    AppHeaderClockSpace {
        dock: right;
        width: 10;
        padding: 0 1;
    }
    """

    def render(self) -> RenderResult:
        return ""


class AppHeaderClock(AppHeaderClockSpace):
    """Optional live clock rendered at the right edge of the header."""

    DEFAULT_CSS = """
    AppHeaderClock {
        background: $foreground-darken-1 5%;
        color: $foreground;
        text-opacity: 85%;
        content-align: center middle;
    }
    """

    time_format: Reactive[str] = Reactive("%X")

    def on_mount(self) -> None:
        self.set_interval(1, callback=self.refresh, name="update header clock")

    def render(self) -> RenderResult:
        return datetime.now().time().strftime(self.time_format)


class AppHeaderTitle(Static):
    """Centered application title and subtitle."""

    DEFAULT_CSS = """
    AppHeaderTitle {
        text-wrap: nowrap;
        text-overflow: ellipsis;
        content-align: center middle;
        width: 100%;
    }
    """


class AppHeader(Widget):
    """Textual-compatible header safe under rapid screen replacement."""

    DEFAULT_CSS = """
    AppHeader {
        dock: top;
        width: 100%;
        background: $panel;
        color: $foreground;
        height: 1;
    }
    AppHeader.-tall { height: 3; }
    """

    tall: Reactive[bool] = Reactive(False)
    icon: Reactive[str] = Reactive("⭘")
    time_format: Reactive[str] = Reactive("%X")

    def __init__(
        self,
        show_clock: bool = False,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        icon: str | None = None,
        time_format: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._show_clock = show_clock
        if icon is not None:
            self.icon = icon
        if time_format is not None:
            self.time_format = time_format

    def compose(self) -> ComposeResult:
        yield AppHeaderIcon().data_bind(AppHeader.icon)
        yield AppHeaderTitle()
        yield (
            AppHeaderClock().data_bind(AppHeader.time_format)
            if self._show_clock
            else AppHeaderClockSpace()
        )

    def watch_tall(self, tall: bool) -> None:
        self.set_class(tall, "-tall")

    def on_click(self) -> None:
        self.toggle_class("-tall")

    def format_title(self) -> Content:
        return self.app.format_title(
            self.screen_title,
            self.screen_sub_title,
        )

    @property
    def screen_title(self) -> str:
        title = self.screen.title
        return title if title is not None else self.app.title

    @property
    def screen_sub_title(self) -> str:
        sub_title = self.screen.sub_title
        return sub_title if sub_title is not None else self.app.sub_title

    def on_mount(self) -> None:
        self.call_after_refresh(self._install_title_watchers)

    def _install_title_watchers(self) -> None:
        """Bind title updates after this header's composed subtree is ready."""

        if not self.is_mounted or not self.is_attached:
            return
        containing_screen = self.screen
        if not containing_screen.is_current:
            return
        title = self.query_one(AppHeaderTitle)
        if title.parent is not self or not title.is_mounted or not title.is_attached:
            return

        async def set_title() -> None:
            # A reactive callback may already be queued when its screen is
            # replaced. Ignore only a detached header/title subtree; all update
            # and formatting failures on the live subtree remain visible.
            if not self.is_mounted or not self.is_attached:
                return
            if (
                title.parent is not self
                or not title.is_mounted
                or not title.is_attached
            ):
                return
            title.update(self.format_title())

        self.watch(self.app, "title", set_title)
        self.watch(self.app, "sub_title", set_title)
        self.watch(containing_screen, "title", set_title)
        self.watch(containing_screen, "sub_title", set_title)


@dataclass(frozen=True)
class CommandSpec:
    """One documented TUI command with a canonical key and label."""

    key: str
    key_display: str
    action: str
    label: str
    detail: str
    aliases: tuple[str, ...] = ()

    @property
    def reference_keys(self) -> str:
        return " / ".join((self.key_display, *self.aliases))

    def binding(
        self,
        *,
        action: str | None = None,
        show: bool = True,
        priority: bool = False,
    ) -> Binding:
        return Binding(
            self.key,
            action or self.action,
            self.label,
            show=show,
            key_display=self.key_display,
            priority=priority,
        )


HELP = CommandSpec(
    "f1",
    "F1",
    "show_shortcuts",
    "Keys",
    "Open the complete keyboard-command reference.",
    aliases=("?",),
)
COMMAND_PALETTE = CommandSpec(
    "ctrl+p",
    "Ctrl+P",
    "command_palette",
    "Commands",
    "Search Textual commands and available themes.",
)
SELECT_ALL = CommandSpec(
    "ctrl+a",
    "Ctrl+A",
    "select_all",
    "Select all",
    "Select all content in a text field.",
)
TOGGLE_THEME = CommandSpec(
    "ctrl+t",
    "Ctrl+T",
    "toggle_proof_theme",
    "Theme",
    "Switch between the Proof Ink and Proof Paper themes.",
)
QUIT = CommandSpec(
    "ctrl+q",
    "Ctrl+Q",
    "quit",
    "Quit",
    "Exit Proof Assistant and return to the shell.",
)
MAIN_MENU = CommandSpec(
    "f2",
    "F2",
    "main_menu",
    "Main menu",
    "Return to the Proof Assistant landing screen.",
)
GLOBAL_SETTINGS = CommandSpec(
    "f3",
    "F3",
    "global_settings",
    "Settings",
    "Open machine settings from any screen.",
)

BACK = CommandSpec("escape", "Esc", "back", "Back", "Return to the prior screen.")
CANCEL = CommandSpec(
    "escape", "Esc", "cancel", "Cancel", "Dismiss a dialog without applying it."
)
CLOSE = CommandSpec("q", "Q", "close", "Close", "Close the current viewer to Projects.")
CONFIRM = CommandSpec(
    "ctrl+enter",
    "Ctrl+Enter",
    "confirm",
    "Continue",
    "Continue or confirm the current reviewed form.",
)
SAVE = CommandSpec(
    "ctrl+s",
    "Ctrl+S",
    "save",
    "Save",
    "Validate, preview, and save the current settings.",
)

NEW_PROJECT = CommandSpec(
    "n", "N", "new_project", "New", "Create a new verification project."
)
REFRESH = CommandSpec("r", "R", "refresh", "Refresh", "Reload current data.")
SETTINGS = CommandSpec("s", "S", "settings", "Settings", "Open machine settings.")
VERIFY = CommandSpec("v", "V", "verify", "Verify", "Start a verification iteration.")
CHECK_CHANGES = CommandSpec(
    "c", "C", "check_changes", "Changes", "Check the manuscript for changes."
)
OPEN = CommandSpec("o", "O", "open", "Open", "Open the relevant file or folder.")
REPORT = CommandSpec(
    "r", "R", "report", "Report", "Open the terminal verification report."
)
FAILURES = CommandSpec(
    "f", "F", "failures", "Failures", "Open failure dependency analysis."
)
RETRY = CommandSpec("r", "R", "retry", "Retry", "Retry or recover the project.")
PREVIOUS = CommandSpec(
    "[", "[", "previous", "Previous", "Show the previous clarification."
)
NEXT = CommandSpec("]", "]", "next", "Next", "Show the next clarification.")
PARENT_FOLDER = CommandSpec(
    "backspace", "Backspace", "parent", "Up", "Open the parent folder."
)
HOME_FOLDER = CommandSpec(
    "ctrl+home", "Ctrl+Home", "home_folder", "Home", "Open the home folder."
)
CANCEL_JOB = CommandSpec(
    "c", "C", "cancel_job", "Cancel", "Request cooperative job cancellation."
)
DETACH_JOB = CommandSpec(
    "d", "D", "detach_job", "Detach", "Stop observing while the job continues."
)

# Alternative help is intentionally non-priority: typing '?' in an editable field
# remains ordinary text input, while F1 always opens the reference screen.
GLOBAL_BINDINGS: list[Binding | tuple[str, str] | tuple[str, str, str]] = [
    HELP.binding(priority=True),
    Binding("question_mark", "show_shortcuts", show=False),
    MAIN_MENU.binding(priority=True),
    GLOBAL_SETTINGS.binding(priority=True),
    COMMAND_PALETTE.binding(priority=True),
    TOGGLE_THEME.binding(priority=True),
    QUIT.binding(priority=True),
]


@dataclass(frozen=True)
class ReferenceCommand:
    keys: str
    label: str
    detail: str


def _reference(command: CommandSpec) -> ReferenceCommand:
    return ReferenceCommand(command.reference_keys, command.label, command.detail)


SHORTCUT_GROUPS: tuple[tuple[str, tuple[ReferenceCommand, ...]], ...] = (
    (
        "Everywhere",
        tuple(
            map(
                _reference,
                (
                    HELP,
                    MAIN_MENU,
                    GLOBAL_SETTINGS,
                    COMMAND_PALETTE,
                    TOGGLE_THEME,
                    QUIT,
                ),
            )
        ),
    ),
    (
        "Focus, lists, and text",
        (
            ReferenceCommand("Tab / Shift+Tab", "Focus", "Move between controls."),
            ReferenceCommand("Enter", "Activate", "Activate the focused control."),
            ReferenceCommand(
                "Arrow keys", "Navigate", "Move within text, tables, trees, and lists."
            ),
            ReferenceCommand(
                "PageUp / PageDown", "Page", "Scroll the focused pane by one page."
            ),
            ReferenceCommand(
                "Home / End", "First / Last", "Move to the beginning or end."
            ),
            _reference(SELECT_ALL),
            ReferenceCommand(
                "Space", "Toggle", "Toggle the focused checkbox, radio, or tree item."
            ),
        ),
    ),
    (
        "Projects and dashboard",
        tuple(
            map(
                _reference,
                (NEW_PROJECT, REFRESH, SETTINGS, VERIFY, CHECK_CHANGES, OPEN, BACK),
            )
        ),
    ),
    (
        "Setup and review",
        tuple(map(_reference, (BACK, CANCEL, CONFIRM, PARENT_FOLDER, HOME_FOLDER))),
    ),
    (
        "Progress",
        tuple(map(_reference, (CANCEL_JOB, DETACH_JOB))),
    ),
    (
        "Clarifications",
        tuple(map(_reference, (PREVIOUS, NEXT, CHECK_CHANGES, OPEN))),
    ),
    (
        "Results and recovery",
        tuple(
            map(
                _reference,
                (CHECK_CHANGES, REPORT, FAILURES, RETRY, OPEN, BACK, CLOSE),
            )
        ),
    ),
    (
        "Settings",
        tuple(map(_reference, (BACK, REFRESH, SAVE, CONFIRM, CANCEL))),
    ),
)


def shortcut_reference_text() -> str:
    """Render the canonical command registry as an SSH-safe text reference."""

    lines = [
        "Commands shown in the footer are available on the current screen.",
        "F1-F3 are global and safe while typing; ? opens help outside text fields.",
        "On a dialog, F2 or F3 dismisses the dialog first; press again to navigate.",
        "",
    ]
    for group_name, commands in SHORTCUT_GROUPS:
        lines.append(group_name)
        lines.append("─" * len(group_name))
        for command in commands:
            lines.append(f"{command.keys:<19} {command.label:<13} {command.detail}")
        lines.append("")
    return "\n".join(lines).rstrip()


class CommandFooter(Footer):
    """Compact, theme-aware command strip used by every TUI screen."""

    def __init__(self) -> None:
        # The palette is an ordinary canonical binding above, so suppress the
        # Footer's separate right-aligned ``^p palette`` presentation.
        super().__init__(show_command_palette=False)

    def on_mount(self) -> None:
        self.compact = True
