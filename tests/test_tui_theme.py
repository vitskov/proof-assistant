from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from proof_assistant.tui import screens as tui_screens
from proof_assistant.tui.settings import screens as settings_screens
from proof_assistant.tui.theme import PROOF_DARK_PALETTE, PROOF_LIGHT_PALETTE


def _luminance(color: str) -> float:
    values = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in values
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("palette", (PROOF_DARK_PALETTE, PROOF_LIGHT_PALETTE))
def test_theme_text_and_control_contrast(palette) -> None:
    for background in (palette.background, palette.surface, palette.panel):
        assert _contrast(palette.foreground, background) >= 4.5
        assert _contrast(palette.subtle_border, background) >= 3.0
        assert _contrast(palette.focus, background) >= 3.0
    assert _contrast(palette.muted, palette.background) >= 4.5

    callouts = (
        (palette.callout_warning_text, palette.callout_warning_background),
        (palette.callout_error_text, palette.callout_error_background),
        (palette.callout_success_text, palette.callout_success_background),
        (palette.callout_info_text, palette.callout_info_background),
    )
    for foreground, background in callouts:
        assert _contrast(foreground, background) >= 4.5

    for button_background in (
        palette.primary,
        palette.secondary,
        palette.warning,
        palette.error,
        palette.success,
        palette.accent,
    ):
        assert _contrast("#FFFCF0", button_background) >= 4.5


def test_theme_module_is_the_only_tui_color_source() -> None:
    tui_root = Path(tui_screens.__file__).parent
    color_literal = re.compile(r"#[0-9A-Fa-f]{6}\b")
    for source_path in tui_root.rglob("*.py"):
        if source_path.name == "theme.py":
            continue
        assert color_literal.search(source_path.read_text(encoding="utf-8")) is None


@pytest.mark.parametrize(
    "source_path",
    (Path(tui_screens.__file__), Path(settings_screens.__file__)),
)
def test_every_concrete_tui_screen_composes_the_command_footer(source_path) -> None:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        base_names = {
            base.id for base in class_node.bases if isinstance(base, ast.Name)
        }
        if not base_names & {"NoticeScreen", "ModalScreen", "_SettingsEditorScreen"}:
            continue
        compose = next(
            (
                node
                for node in class_node.body
                if isinstance(node, ast.FunctionDef) and node.name == "compose"
            ),
            None,
        )
        if compose is None:
            continue
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CommandFooter"
            for node in ast.walk(compose)
        ), f"{class_node.name} does not compose the permanent command footer"
