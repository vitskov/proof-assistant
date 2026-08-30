from __future__ import annotations

import pytest


@pytest.fixture
def deterministic_color_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep SVG baselines independent of an operator's Rich color environment."""
    monkeypatch.delenv("NO_COLOR", raising=False)
