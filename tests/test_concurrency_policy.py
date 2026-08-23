from __future__ import annotations

import pytest

from proof_assistant.concurrency import (
    AIAdmissionController,
    BudgetPolicy,
    SQLiteAdmissionStore,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


@pytest.mark.parametrize(
    ("policy", "successes"),
    (
        (BudgetPolicy.ECONOMY, 12),
        (BudgetPolicy.BALANCED, 8),
        (BudgetPolicy.THROUGHPUT, 4),
    ),
)
def test_budget_policy_materially_controls_aimd_recovery(tmp_path, policy, successes):
    clock = FakeClock()
    controller = AIAdmissionController(
        SQLiteAdmissionStore(tmp_path / f"{policy}.sqlite3", clock=clock),
        initial=1,
        minimum=1,
        ceiling=3,
        budget_policy=policy,
        increase_after_successes=None,
        increase_cooldown_seconds=0,
    )
    for _index in range(successes - 1):
        assert controller.record_success(1.0, queued=True) == 1
    assert controller.record_success(1.0, queued=True) == 2


def test_explicit_success_threshold_overrides_budget_policy(tmp_path):
    controller = AIAdmissionController(
        SQLiteAdmissionStore(tmp_path / "override.sqlite3", clock=FakeClock()),
        initial=1,
        minimum=1,
        ceiling=3,
        budget_policy=BudgetPolicy.ECONOMY,
        increase_after_successes=2,
        increase_cooldown_seconds=0,
    )
    assert controller.record_success(1.0, queued=True) == 1
    assert controller.record_success(1.0, queued=True) == 2
