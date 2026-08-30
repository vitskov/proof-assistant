from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

import pytest

from proof_assistant.tui.theme import PROOF_DARK_THEME, PROOF_LIGHT_THEME
from tests.textual_dev_app import ResponsiveFoundationApp


@pytest.mark.parametrize("terminal_size", ((80, 24), (120, 40), (140, 48)))
@pytest.mark.parametrize("theme", (PROOF_DARK_THEME.name, PROOF_LIGHT_THEME.name))
def test_responsive_foundation_matches_reviewed_snapshot(
    snap_compare: Callable[..., bool],
    deterministic_color_snapshots: None,
    terminal_size: tuple[int, int],
    theme: str,
) -> None:
    app = ResponsiveFoundationApp(selected_theme=theme)

    assert snap_compare(app, terminal_size=terminal_size)


def test_textual_dev_diagnostics_complete_noninteractively() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "textual_dev", "diagnose"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "# Textual Diagnostics" in result.stdout
