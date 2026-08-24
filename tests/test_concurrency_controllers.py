from __future__ import annotations

import multiprocessing
import pickle
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from proof_assistant.concurrency.admission import (
    AdmissionController,
    AdmissionRequest,
    ResourceKind,
    SQLiteAdmissionStore,
)
from proof_assistant.concurrency.ai import AIAdmissionController, AITaskClass
from proof_assistant.concurrency.build import BuildAdmissionController
from proof_assistant.concurrency.config import (
    AIConcurrencyPatch,
    ConcurrencyConfigPatch,
    resolve_concurrency_config,
)
from proof_assistant.concurrency.hardware import HardwareResources
from proof_assistant.concurrency.lean import LeanAdmissionController
from proof_assistant.concurrency.runtime import (
    ConcurrencyRuntime,
    ConcurrencyRuntimeSpec,
)
from proof_assistant.concurrency.scheduler import (
    DependencyScheduler,
    DuplicateEscalationPolicy,
    ScheduledTask,
)


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def request(
    resource: ResourceKind, owner: str, *, priority: int = 10, ttl: float = 30
) -> AdmissionRequest:
    return AdmissionRequest(resource, priority, owner, ttl)


def _process_admission_attempt(
    path: str,
    owner: str,
    ready,
    start,
    results,
    finish,
) -> None:
    store = SQLiteAdmissionStore(path)
    controller = AdmissionController(store, ResourceKind.AI, 1)
    ready.put(owner)
    start.wait(10)
    lease = controller.try_acquire(request(ResourceKind.AI, owner, ttl=30))
    results.put(lease is not None)
    finish.wait(10)
    if lease is not None:
        controller.release(lease)


def _process_yellow_build_attempt(
    path: str,
    owner: str,
    ready,
    start,
    results,
    finish,
) -> None:
    controller = BuildAdmissionController(
        SQLiteAdmissionStore(path), initial=4, maximum=4
    )
    ready.put(owner)
    start.wait(10)
    lease = controller.try_acquire(controller.request(owner))
    results.put(lease is not None)
    finish.wait(10)
    if lease is not None:
        controller.release(lease)


def test_namespaces_are_independent_and_limits_are_cross_instance(tmp_path):
    clock = FakeClock()
    path = tmp_path / "admission.sqlite3"
    first_store = SQLiteAdmissionStore(path, clock=clock)
    second_store = SQLiteAdmissionStore(path, clock=clock)
    ai = AdmissionController(first_store, ResourceKind.AI, 1)
    lean = AdmissionController(second_store, ResourceKind.LEAN, 1)

    ai_lease = ai.try_acquire(request(ResourceKind.AI, "proof"))
    lean_lease = lean.try_acquire(request(ResourceKind.LEAN, "lean"))
    assert ai_lease is not None
    assert lean_lease is not None
    assert ai.try_acquire(request(ResourceKind.AI, "review")) is None

    # A second controller sees the canonical machine limit and active lease.
    ai_again = AdmissionController(second_store, ResourceKind.AI, 99)
    assert ai_again.limit == 1
    assert ai_again.snapshot().active == 1


def test_priority_queue_is_fairish_and_abandoned_waiters_expire(tmp_path):
    clock = FakeClock()
    store = SQLiteAdmissionStore(tmp_path / "admission.sqlite3", clock=clock)
    controller = AdmissionController(store, ResourceKind.AI, 1)
    running = controller.try_acquire(request(ResourceKind.AI, "running"))
    assert running is not None

    low = request(ResourceKind.AI, "low", priority=40)
    high = request(ResourceKind.AI, "high", priority=0)
    assert controller.try_acquire(low) is None
    assert controller.try_acquire(high) is None
    controller.release(running)
    assert controller.try_acquire(low) is None
    high_lease = controller.try_acquire(high)
    assert high_lease is not None
    controller.release(high_lease)
    store.cancel(low)

    # A high-priority waiter that never polls cannot block the machine forever.
    stale = request(ResourceKind.AI, "stale", priority=0, ttl=5)
    blocker = controller.try_acquire(request(ResourceKind.AI, "blocker"))
    assert blocker is not None
    assert controller.try_acquire(stale) is None
    controller.release(blocker)
    clock.advance(61)
    assert controller.try_acquire(low) is not None


def test_heartbeat_extends_ttl_and_crashed_lease_is_reclaimed(tmp_path):
    clock = FakeClock()
    store = SQLiteAdmissionStore(tmp_path / "admission.sqlite3", clock=clock)
    controller = AdmissionController(store, ResourceKind.AI, 1)
    lease = controller.try_acquire(request(ResourceKind.AI, "worker", ttl=10))
    assert lease is not None

    clock.advance(8)
    renewed = controller.heartbeat(lease)
    assert renewed is not None
    assert renewed.expires_at == pytest.approx(clock() + 10)
    clock.advance(9)
    assert controller.try_acquire(request(ResourceKind.AI, "queued")) is None
    clock.advance(2)
    reclaimed = controller.try_acquire(request(ResourceKind.AI, "queued"))
    assert reclaimed is not None
    assert controller.heartbeat(lease) is None


def test_lease_context_heartbeats_long_work_and_releases_on_exit(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    store = SQLiteAdmissionStore(tmp_path / "admission.sqlite3", clock=clock)
    controller = AdmissionController(store, ResourceKind.BUILD, 1)
    heartbeat_started = threading.Event()
    heartbeat_allowed = threading.Event()
    heartbeat_finished = threading.Event()
    original_heartbeat = store.heartbeat

    def tracked_heartbeat(lease, *, ttl_seconds=None):
        heartbeat_started.set()
        if not heartbeat_allowed.wait(5):
            raise AssertionError("timed out waiting to release the test heartbeat")
        renewed = original_heartbeat(lease, ttl_seconds=ttl_seconds)
        heartbeat_finished.set()
        return renewed

    monkeypatch.setattr(store, "heartbeat", tracked_heartbeat)
    build_request = request(ResourceKind.BUILD, "long-build", ttl=0.2)
    with controller.lease(build_request) as lease:
        assert heartbeat_started.wait(5)
        clock.advance(0.15)
        heartbeat_allowed.set()
        assert heartbeat_finished.wait(5)
        clock.advance(0.1)
        assert clock() > lease.expires_at
        assert controller.snapshot().active == 1
    assert controller.snapshot().active == 0


def test_limit_reduction_drains_without_revoking_inflight_work(tmp_path):
    store = SQLiteAdmissionStore(tmp_path / "admission.sqlite3")
    controller = AdmissionController(store, ResourceKind.BUILD, 2)
    first = controller.try_acquire(request(ResourceKind.BUILD, "one"))
    second = controller.try_acquire(request(ResourceKind.BUILD, "two"))
    assert first is not None and second is not None

    controller.set_limit(1)
    assert controller.snapshot().active == 2
    assert controller.try_acquire(request(ResourceKind.BUILD, "three")) is None
    controller.release(first)
    assert controller.try_acquire(request(ResourceKind.BUILD, "three")) is None
    controller.release(second)
    assert controller.try_acquire(request(ResourceKind.BUILD, "three")) is not None


def test_sqlite_admission_is_atomic_across_real_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    finish = context.Event()
    path = str(tmp_path / "admission.sqlite3")
    processes = [
        context.Process(
            target=_process_admission_attempt,
            args=(path, owner, ready, start, results, finish),
        )
        for owner in ("worker-a", "worker-b")
    ]
    for process in processes:
        process.start()
    try:
        assert {ready.get(timeout=10), ready.get(timeout=10)} == {
            "worker-a",
            "worker-b",
        }
        start.set()
        assert sum((results.get(timeout=10), results.get(timeout=10))) == 1
    finally:
        finish.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
            assert process.exitcode == 0


def test_repeated_admission_polls_always_close_sqlite_connections(
    tmp_path, monkeypatch
):
    store = SQLiteAdmissionStore(tmp_path / "admission.sqlite3")
    opened: list[sqlite3.Connection] = []
    original_connect = store._connect

    def tracked_connect() -> sqlite3.Connection:
        connection = original_connect()
        opened.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", tracked_connect)
    controller = AdmissionController(store, ResourceKind.AI, 2)
    for _ in range(100):
        assert controller.snapshot().limit == 2
        assert controller.limit == 2

    assert len(opened) == 201
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def make_ai(
    path: Path,
    clock: FakeClock,
    *,
    initial: int = 4,
    mode: str = "adaptive",
) -> AIAdmissionController:
    return AIAdmissionController(
        SQLiteAdmissionStore(path, clock=clock),
        initial=initial,
        minimum=1,
        ceiling=8,
        mode=mode,
        jitter=lambda seconds: seconds,
        increase_after_successes=2,
    )


def test_ai_budget_is_shared_by_proofs_reviewers_and_diagnostics(tmp_path):
    clock = FakeClock()
    ai = make_ai(tmp_path / "admission.sqlite3", clock, initial=2)
    proof = ai.try_acquire(ai.request("proof", AITaskClass.PROOF))
    review = ai.try_acquire(ai.request("review", AITaskClass.REVIEW))
    assert proof is not None and review is not None
    assert ai.try_acquire(ai.request("diagnosis", AITaskClass.DIAGNOSTIC)) is None

    ai.release(proof)
    diagnosis = ai.try_acquire(ai.request("diagnosis", AITaskClass.DIAGNOSTIC))
    assert diagnosis is not None
    assert diagnosis.priority < review.priority


def test_ai_additive_increase_multiplicative_decrease_and_fixed_mode(tmp_path):
    clock = FakeClock()
    ai = make_ai(tmp_path / "adaptive.sqlite3", clock)
    assert ai.record_success(2.0, queued=True) == 4
    assert ai.record_success(1.0, queued=True) == 5
    delay = ai.record_throttle(retry_after=75)
    assert delay == 75
    assert ai.limit == 2
    assert ai.in_backoff
    assert ai.try_acquire(ai.request("proof", AITaskClass.PROOF)) is None
    ai.record_success(1.0, queued=True)
    ai.record_success(1.0, queued=True)
    assert ai.limit == 2
    clock.advance(76)
    assert ai.try_acquire(ai.request("proof", AITaskClass.PROOF)) is not None

    fixed = make_ai(tmp_path / "fixed.sqlite3", clock, mode="fixed")
    fixed.record_success(1.0, queued=True)
    fixed.record_success(1.0, queued=True)
    fixed.record_throttle(retry_after=1)
    assert fixed.limit == 4


def test_ai_adaptive_success_history_survives_worker_process_handles(tmp_path):
    clock = FakeClock()
    path = tmp_path / "shared.sqlite3"
    first = make_ai(path, clock)
    assert first.record_success(2.0, queued=True) == 4

    # A new controller is what a short-lived batch/Codex process constructs.
    second = make_ai(path, clock)
    assert second.record_success(1.0, queued=True) == 5
    status = first.status()
    assert status.current_limit == 5
    assert status.rolling_latency_seconds == pytest.approx(1.5)
    assert status.rolling_success_rate == 1.0


def test_ai_backoff_sequence_and_repeated_transient_failure(tmp_path):
    clock = FakeClock()
    ai = make_ai(tmp_path / "admission.sqlite3", clock)
    delays = []
    for _ in range(6):
        delays.append(ai.record_throttle())
        clock.advance(301)
    assert delays == [30, 60, 120, 240, 300, 300]

    other = make_ai(tmp_path / "transient.sqlite3", clock)
    other.record_transient_failure()
    assert other.limit == 4
    clock.advance(31)
    other.record_transient_failure()
    assert other.limit == 3


def test_lean_adaptation_uses_hysteresis_and_pressure_pauses_admission(tmp_path):
    clock = FakeClock()
    lean = LeanAdmissionController(
        SQLiteAdmissionStore(tmp_path / "admission.sqlite3", clock=clock),
        initial=2,
        maximum=4,
        resize_interval_seconds=30,
    )
    lean.observe(pressure="green", queue_depth=2, cpu_percent=50)
    assert lean.limit == 2
    lean.observe(pressure="green", queue_depth=2, cpu_percent=50)
    assert lean.limit == 3

    clock.advance(31)
    lean.observe(pressure="red", queue_depth=1, cpu_percent=80)
    assert lean.try_acquire(lean.request("red-work")) is None
    lean.observe(pressure="red", queue_depth=1, cpu_percent=80)
    assert lean.limit == 2
    assert lean.status().admission_paused

    lean.observe(pressure="green", queue_depth=0, cpu_percent=50)
    assert lean.try_acquire(lean.request("green-work")) is not None


def test_build_controller_is_independent_conservative_and_fixed_mode_is_stable(
    tmp_path,
):
    clock = FakeClock()
    store = SQLiteAdmissionStore(tmp_path / "admission.sqlite3", clock=clock)
    build = BuildAdmissionController(
        store,
        initial=2,
        maximum=4,
        resize_interval_seconds=30,
    )
    build.observe(pressure="yellow", queue_depth=2)
    full = build.try_acquire(build.request("full"))
    assert full is not None
    assert build.try_acquire(build.request("second-full")) is None
    targeted = build.try_acquire(build.request("targeted", full_build=False))
    assert targeted is not None
    build.release(targeted)
    build.release(full)
    build.observe(pressure="green", queue_depth=2)
    build.observe(pressure="green", queue_depth=2)
    assert build.limit == 3
    clock.advance(31)
    build.observe(pressure="red", queue_depth=2)
    build.observe(pressure="red", queue_depth=2)
    assert build.limit == 2

    fixed = BuildAdmissionController(
        SQLiteAdmissionStore(tmp_path / "fixed.sqlite3", clock=clock),
        initial=1,
        maximum=2,
        mode="fixed",
    )
    fixed.observe(pressure="green", queue_depth=99)
    fixed.observe(pressure="green", queue_depth=99)
    assert fixed.limit == 1


def test_memory_pressure_is_shared_across_process_controller_handles(tmp_path):
    clock = FakeClock()
    path = tmp_path / "admission.sqlite3"
    first_store = SQLiteAdmissionStore(path, clock=clock)
    lean_monitor = LeanAdmissionController(first_store, initial=2, maximum=2)
    build_monitor = BuildAdmissionController(first_store, initial=1, maximum=1)
    lean_monitor.observe(pressure="red", queue_depth=1, cpu_percent=80)
    build_monitor.observe(pressure="yellow", queue_depth=1)

    second_store = SQLiteAdmissionStore(path, clock=clock)
    worker_lean = LeanAdmissionController(second_store, initial=2, maximum=2)
    worker_build = BuildAdmissionController(second_store, initial=1, maximum=1)
    assert worker_lean.try_acquire(worker_lean.request("cross-process-lean")) is None
    full = worker_build.try_acquire(worker_build.request("full"))
    assert full is not None
    assert build_monitor.try_acquire(build_monitor.request("second-full")) is None
    worker_build.release(full)
    targeted = worker_build.try_acquire(
        worker_build.request("targeted", full_build=False)
    )
    assert targeted is not None

    # A crashed monitor cannot leave admission paused forever.
    clock.advance(21)
    assert worker_lean.try_acquire(worker_lean.request("recovered-lean")) is not None


def test_yellow_pressure_guarantees_one_build_but_never_admits_two(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    finish = context.Event()
    path = str(tmp_path / "admission.sqlite3")
    monitor = BuildAdmissionController(SQLiteAdmissionStore(path), initial=4, maximum=4)
    monitor.observe(pressure="yellow", queue_depth=4)
    processes = [
        context.Process(
            target=_process_yellow_build_attempt,
            args=(path, f"build-{index}", ready, start, results, finish),
        )
        for index in range(4)
    ]
    for process in processes:
        process.start()
    for _process in processes:
        ready.get(timeout=10)
    start.set()
    admitted = [results.get(timeout=10) for _process in processes]
    assert admitted.count(True) == 1
    finish.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0


def test_duplicate_escalation_obeys_budget_and_repair_first():
    balanced = DuplicateEscalationPolicy(budget_policy="balanced", maximum_agents=4)
    assert (
        balanced.agents_for_target(
            failed_attempts=1,
            repair_attempted=False,
            independent_attempt_useful=True,
        )
        == 1
    )
    assert (
        balanced.agents_for_target(
            failed_attempts=2,
            repair_attempted=True,
            independent_attempt_useful=True,
        )
        == 2
    )
    economy = DuplicateEscalationPolicy(budget_policy="economy", maximum_agents=4)
    throughput = DuplicateEscalationPolicy(budget_policy="throughput", maximum_agents=4)
    assert (
        economy.agents_for_target(
            failed_attempts=1,
            repair_attempted=True,
            independent_attempt_useful=True,
        )
        == 1
    )
    assert (
        throughput.agents_for_target(
            failed_attempts=1,
            repair_attempted=True,
            independent_attempt_useful=True,
        )
        == 2
    )


def test_dependency_scheduler_prefers_largest_unlock_and_survives_cycles():
    scheduler = DependencyScheduler()
    dependencies = {
        "b": ("a",),
        "c": ("a",),
        "d": ("b",),
        "cycle-1": ("cycle-2",),
        "cycle-2": ("cycle-1",),
    }
    tasks = (
        ScheduledTask("b", ready_order=0),
        ScheduledTask("a", ready_order=1),
        ScheduledTask("cycle-1", ready_order=2),
    )
    ordered = scheduler.prioritize(
        tasks,
        requested=dependencies,
        blocked=dependencies,
        dependencies=dependencies,
    )
    assert ordered[0].claim_id == "a"
    scores = scheduler.unlock_scores(
        ("cycle-1",),
        requested=("cycle-1", "cycle-2"),
        dependencies=dependencies,
    )
    assert scores == {"cycle-1": 1}


def test_runtime_composes_three_machine_scoped_controllers(tmp_path):
    gib = 1024**3
    resources = HardwareResources(
        os_name="Darwin",
        architecture="arm64",
        host_logical_cpus=8,
        host_physical_cpus=6,
        usable_logical_cpus=8,
        usable_physical_cpus=6,
        host_total_memory_bytes=32 * gib,
        total_memory_bytes=32 * gib,
        available_memory_bytes=24 * gib,
        interactive_detected=True,
    )
    runtime = ConcurrencyRuntime.from_resolved(
        resolve_concurrency_config(environ={}),
        tmp_path,
        resources=resources,
        clock=FakeClock(),
        jitter=lambda seconds: seconds,
    )
    assert runtime.ai.store is runtime.lean.store is runtime.build.store
    assert runtime.ai.resource == ResourceKind.AI
    assert runtime.lean.resource == ResourceKind.LEAN
    assert runtime.build.resource == ResourceKind.BUILD
    provenance = runtime.provenance()
    assert provenance["configured"]["ai_initial"] == "auto"
    assert provenance["effective"]["ai_limit"] == 4

    # A sibling batch worker with the same spec must preserve adaptive history.
    runtime.ai.set_limit(2)
    sibling = ConcurrencyRuntime.from_resolved(
        runtime.resolved,
        tmp_path,
        resources=resources,
        clock=runtime.store.clock,
        jitter=lambda seconds: seconds,
    )
    assert sibling.ai.limit == 2

    changed = resolve_concurrency_config(
        environ={},
        cli=ConcurrencyConfigPatch(ai=AIConcurrencyPatch(initial=3, hard_max=3)),
    )
    revised = ConcurrencyRuntime.from_resolved(
        changed,
        tmp_path,
        resources=resources,
        clock=runtime.store.clock,
        jitter=lambda seconds: seconds,
    )
    assert revised.ai.limit == 3


def test_runtime_spec_is_picklable_and_resolves_machine_scoped_runtime(
    tmp_path, monkeypatch
):
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(
        "proof_assistant.concurrency.runtime.CacheLayout.discover",
        lambda _requested: SimpleNamespace(root=cache_root),
    )
    spec = ConcurrencyRuntimeSpec(
        cache_home=str(cache_root),
        machine_config_path=str(tmp_path / "machine.yaml"),
        cli_patch=ConcurrencyConfigPatch(ai=AIConcurrencyPatch(initial=1, hard_max=1)),
    )
    restored = pickle.loads(pickle.dumps(spec))
    runtime = restored.create(environ={}, jitter=lambda seconds: seconds)
    assert runtime.ai.limit == 1
    assert runtime.store.path.parent == cache_root / "concurrency"
