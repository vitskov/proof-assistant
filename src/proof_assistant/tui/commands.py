"""Central command vocabulary for the terminal interface.

Bindings, footer labels, and the keyboard-reference screen all use these
definitions.  Keeping the user-facing command names here prevents individual
screens from inventing subtly different labels for the same interaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.binding import Binding
from textual.widgets import Footer


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
