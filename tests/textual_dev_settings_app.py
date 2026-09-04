"""Deterministic production settings screen for live textual-dev inspection.

Run from the repository root with::

    textual run --dev tests/textual_dev_settings_app.py
"""

from __future__ import annotations

from importlib import import_module

from proof_assistant.ai import (
    Difficulty,
    DriverId,
    ProviderConfig,
    TaskKind,
    TaskPreference,
)
from proof_assistant.tui import ProofAssistantApp

_provider_test_support = import_module("tests.test_tui_providers")
ProviderWorkflowFake = _provider_test_support.ProviderWorkflowFake
_snapshot = _provider_test_support._snapshot

_CLAUDE_DEFAULTS = {
    TaskKind.CLARIFICATION: ("fable", Difficulty.XHIGH),
    TaskKind.DIAGNOSTIC: ("opus", Difficulty.HIGH),
    TaskKind.PROOF: ("best", Difficulty.HIGH),
    TaskKind.SKETCH: ("sonnet", Difficulty.MEDIUM),
    TaskKind.MAINTENANCE: ("sonnet", Difficulty.MEDIUM),
    TaskKind.REVIEW: ("opus", Difficulty.HIGH),
    TaskKind.DUPLICATE_PROOF: ("fable", Difficulty.XHIGH),
    TaskKind.REPORTING: ("haiku", Difficulty.LOW),
}


class ProviderSettingsDevApp(ProofAssistantApp):
    """Open the real provider-settings implementation over a deterministic fake."""

    def __init__(self) -> None:
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
        self.dev_snapshot = _snapshot(
            primary=DriverId.CLAUDE_CLI,
            config=config,
        )
        super().__init__(ProviderWorkflowFake(self.dev_snapshot))

    def on_mount(self) -> None:
        super().on_mount()
        self.set_timer(0.15, self._open_provider_settings)

    def _open_provider_settings(self) -> None:
        self.show_ai_provider_settings(self.dev_snapshot)


if __name__ == "__main__":
    ProviderSettingsDevApp().run()
