from __future__ import annotations

import pytest

from proof_assistant.concurrency.admission import SQLiteAdmissionStore
from proof_assistant.concurrency.ai import AIAdmissionController, AITaskClass
from proof_assistant.concurrency.distributed_leases import (
    CoordinatorOwnedAILeases,
    DistributedAIRequest,
    NodeResourceControllers,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_coordinator(path, clock, *, limit=2):
    controller = AIAdmissionController(
        SQLiteAdmissionStore(path, clock=clock),
        initial=limit,
        minimum=1,
        ceiling=max(limit, 4),
        jitter=lambda seconds: seconds,
    )
    return CoordinatorOwnedAILeases(controller)


def test_global_ai_count_cannot_be_exceeded_across_workers(tmp_path):
    clock = FakeClock()
    path = tmp_path / "global.sqlite3"
    first_process = make_coordinator(path, clock)
    second_process = make_coordinator(path, clock)

    proof = first_process.request(
        DistributedAIRequest("node-a", "proof", AITaskClass.PROOF)
    )
    reviewer = second_process.request(
        DistributedAIRequest("node-b", "review", AITaskClass.REVIEW)
    )
    blocked = first_process.request(
        DistributedAIRequest("node-c", "diagnosis", AITaskClass.DIAGNOSTIC)
    )
    assert proof is not None and reviewer is not None
    assert blocked is None
    assert first_process.active == second_process.active == 2


def test_expired_worker_lease_is_reclaimed_and_heartbeat_prevents_early_reclaim(
    tmp_path,
):
    clock = FakeClock()
    coordinator = make_coordinator(tmp_path / "global.sqlite3", clock, limit=1)
    lease = coordinator.request(DistributedAIRequest("node-a", "proof", ttl_seconds=10))
    assert lease is not None
    clock.advance(8)
    renewed = coordinator.heartbeat("node-a", lease)
    assert renewed is not None
    clock.advance(9)
    assert (
        coordinator.request(DistributedAIRequest("node-b", "proof", ttl_seconds=10))
        is None
    )
    clock.advance(2)
    replacement = coordinator.request(
        DistributedAIRequest("node-b", "proof", ttl_seconds=10)
    )
    assert replacement is not None
    assert replacement.owner == "node-b:proof"


def test_worker_cannot_heartbeat_or_release_another_workers_lease(tmp_path):
    clock = FakeClock()
    coordinator = make_coordinator(tmp_path / "global.sqlite3", clock, limit=1)
    lease = coordinator.request(DistributedAIRequest("node-a", "proof"))
    assert lease is not None
    with pytest.raises(PermissionError):
        coordinator.heartbeat("node-b", lease)
    with pytest.raises(PermissionError):
        coordinator.release("node-b", lease)
    assert coordinator.release("node-a", lease)


def test_each_node_has_independent_lean_and_build_capacity(tmp_path):
    clock = FakeClock()
    node_a = NodeResourceControllers.create(
        tmp_path,
        node_id="node-a",
        lean_limit=1,
        build_limit=1,
        clock=clock,
    )
    node_b = NodeResourceControllers.create(
        tmp_path,
        node_id="node-b",
        lean_limit=2,
        build_limit=2,
        clock=clock,
    )
    assert node_a.lean.limit == 1
    assert node_b.lean.limit == 2
    assert node_a.build.limit == 1
    assert node_b.build.limit == 2

    a_lean = node_a.lean.try_acquire(node_a.lean.request("a-lean"))
    b_lean_1 = node_b.lean.try_acquire(node_b.lean.request("b-lean-1"))
    b_lean_2 = node_b.lean.try_acquire(node_b.lean.request("b-lean-2"))
    assert a_lean is not None and b_lean_1 is not None and b_lean_2 is not None
    assert node_a.lean.try_acquire(node_a.lean.request("a-lean-2")) is None

    a_build = node_a.build.try_acquire(node_a.build.request("a-build"))
    b_build_1 = node_b.build.try_acquire(node_b.build.request("b-build-1"))
    b_build_2 = node_b.build.try_acquire(node_b.build.request("b-build-2"))
    assert a_build is not None and b_build_1 is not None and b_build_2 is not None
    assert node_a.build.try_acquire(node_a.build.request("a-build-2")) is None
