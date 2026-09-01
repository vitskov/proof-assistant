"""Shell-free terminal-editor discovery and invocation."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

EditorResolver = Callable[[], Path | None]
EditorRunner = Callable[[tuple[str, ...]], int]
TERMINAL_EDITOR_PREFERENCE = ("nano", "pico", "micro")


def resolve_terminal_editor() -> Path | None:
    """Resolve a supported terminal editor in contractual priority order."""

    for name in TERMINAL_EDITOR_PREFERENCE:
        executable = shutil.which(name)
        if executable is not None:
            return Path(executable)
    return None


def terminal_editor_command(
    editor: Path,
    *,
    path: Path,
    line: int,
    column: int,
) -> tuple[str, ...]:
    """Build one shell-free editor invocation at an exact source location."""

    safe_line = max(1, line)
    safe_column = max(1, column)
    name = editor.name.casefold()
    position = (
        f"+{safe_line}:{safe_column}"
        if name == "micro"
        else f"+{safe_line}"
        if name == "pico"
        else f"+{safe_line},{safe_column}"
    )
    return (str(editor), position, str(path.resolve()))


def run_terminal_editor(command: tuple[str, ...]) -> int:
    """Run an already validated editor command without a shell."""

    return subprocess.run(command, check=False).returncode
