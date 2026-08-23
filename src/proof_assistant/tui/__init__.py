"""Textual user interface for Proof Assistant.

The TUI is deliberately a replaceable adapter.  It knows only the immutable
values and the service protocol in :mod:`proof_assistant.workflow.contracts`.
"""

from proof_assistant.tui.app import ProofAssistantApp, run_tui

__all__ = ["ProofAssistantApp", "run_tui"]
