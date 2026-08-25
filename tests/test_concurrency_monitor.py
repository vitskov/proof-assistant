from __future__ import annotations

from dataclasses import replace

import pytest

from proof_assistant.concurrency import (
    AIConcurrencyPatch,
    BuildConcurrencyPatch,
    ConcurrencyConfigPatch,
    ConcurrencyMode,
    ConcurrencyRuntime,
    ConcurrencyRuntimeSpec,
    HardwareResources,
    LeanConcurrencyPatch,
    MachineConfigStore,
    MemoryPressureSource,
    PressureState,
    QueueDepths,
    TelemetrySnapshot,
)
from proof_assistant.incremental.orchestration import _ConcurrencyMonitor

GIB = 1024**3


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


class GreenTelemetry:
    def __init__(self) -> None:
        self.snapshot = TelemetrySnapshot(
            monotonic_time=1.0,
            cpu_percent=10.0,
            load_average=None,
            total_memory_bytes=32 * GIB,
            available_memory_bytes=24 * GIB,
            available_memory_ratio=0.75,
            swap_used_bytes=0,
            swap_out_delta_bytes=0,
            swap_out_rate_bytes_per_second=0.0,
            process_rss_bytes=128 * 1024**2,
            process_pss_bytes=None,
            disk_iowait_percent=0.0,
            pressure=PressureState.GREEN,
            pressure_candidate=PressureState.GREEN,
            native_memory_pressure_level=0,
            memory_pressure_source=MemoryPressureSource.MACOS_NATIVE,
            active_swap_out=False,
            swap_out_threshold_bytes_per_second=16 * 1024**2,
            pressure_reasons=("available memory 75.0%",),
            queues=QueueDepths(),
        )

    def sample(
        self, *, queues: QueueDepths, memory_allocation=None
    ) -> TelemetrySnapshot:
        return replace(self.snapshot, queues=queues)


def _resources() -> HardwareResources:
    return HardwareResources(
        os_name="Darwin",
        architecture="arm64",
        host_logical_cpus=8,
        host_physical_cpus=8,
        usable_logical_cpus=8,
        usable_physical_cpus=8,
        host_total_memory_bytes=32 * GIB,
        total_memory_bytes=32 * GIB,
        available_memory_bytes=24 * GIB,
        interactive_detected=True,
    )


def _patch(limit: int, *, telemetry_enabled: bool) -> ConcurrencyConfigPatch:
    return ConcurrencyConfigPatch(
        mode=ConcurrencyMode.ADAPTIVE,
        telemetry_enabled=telemetry_enabled,
        ai=AIConcurrencyPatch(initial=limit, hard_max=limit),
        lean=LeanConcurrencyPatch(pool_size=limit, max_pool=limit),
        build=BuildConcurrencyPatch(max_concurrent=limit, hard_max=limit),
    )


def _runtime_and_monitor(tmp_path, monkeypatch, *, limit: int = 3):
    machine_path = tmp_path / "machine-settings.yaml"
    settings_client = MachineConfigStore(machine_path)
    settings_client.save(_patch(limit, telemetry_enabled=True), expected_revision=0)
    spec = ConcurrencyRuntimeSpec(machine_config_path=str(machine_path))
    resources = _resources()
    clock = FakeClock()
    runtime = ConcurrencyRuntime.from_resolved(
        spec.resolve(environ={}),
        tmp_path / "cache",
        resources=resources,
        clock=clock,
        jitter=lambda seconds: seconds,
    )
    monitor = _ConcurrencyMonitor(
        runtime,
        spec,
        resolver=lambda: spec.resolve(environ={}),
    )
    monitor.collector = GreenTelemetry()
    monkeypatch.setattr(
        "proof_assistant.incremental.orchestration.detect_hardware",
        lambda: resources,
    )
    return runtime, monitor, settings_client


def test_detached_monitor_applies_machine_limit_without_revoking_leases(
    tmp_path, monkeypatch
):
    runtime, monitor, second_settings_client = _runtime_and_monitor(
        tmp_path, monkeypatch
    )
    leases = {
        "ai": [
            runtime.ai.try_acquire(runtime.ai.request(f"ai-{index}", "proof"))
            for index in range(3)
        ],
        "lean": [
            runtime.lean.try_acquire(runtime.lean.request(f"lean-{index}"))
            for index in range(3)
        ],
        "build": [
            runtime.build.try_acquire(runtime.build.request(f"build-{index}"))
            for index in range(3)
        ],
    }
    assert all(lease is not None for group in leases.values() for lease in group)

    # A separate settings client changes the machine authority while this run
    # retains its original in-memory runtime object.  The settings client has
    # its own runtime, just as two independent TUI/backend processes do.
    settings_runtime = ConcurrencyRuntime.from_resolved(
        monitor.spec.resolve(environ={}),
        tmp_path / "cache",
        resources=runtime.resources,
        clock=runtime.store.clock,
        jitter=lambda seconds: seconds,
    )
    second_settings_client.save(_patch(1, telemetry_enabled=True), expected_revision=1)
    settings_runtime.apply_resolved(monitor.spec.resolve(environ={}))
    assert runtime.resolved.machine_revision == 1
    assert runtime.ai.limit == runtime.lean.limit == runtime.build.limit == 1

    # Queued work gives the old adaptive runtime every opportunity to grow.
    assert runtime.lean.try_acquire(runtime.lean.request("lean-waiting")) is None
    assert runtime.build.try_acquire(runtime.build.request("build-waiting")) is None

    try:
        monitor._sample()
        monitor._sample()
        monitor._sample()

        assert runtime.resolved.machine_revision == 2
        assert monitor.config_refreshes == 1
        for controller in (runtime.ai, runtime.lean, runtime.build):
            snapshot = controller.snapshot()
            assert snapshot.limit == 1
            # Lowering admission capacity drains safely: active work survives.
            assert snapshot.active == 3

        # Repeated adaptive samples cannot restore the old limit of three.
        assert runtime.lean.maximum == 1
        assert runtime.build.maximum == 1
        assert runtime.ai.ceiling == 1
    finally:
        for controller, group in (
            (runtime.ai, leases["ai"]),
            (runtime.lean, leases["lean"]),
            (runtime.build, leases["build"]),
        ):
            for lease in group:
                if lease is not None:
                    controller.release(lease)


def test_monitor_reloads_telemetry_switch_before_adaptation(tmp_path, monkeypatch):
    runtime, monitor, second_settings_client = _runtime_and_monitor(
        tmp_path, monkeypatch, limit=1
    )
    observed: list[TelemetrySnapshot] = []
    monkeypatch.setattr(runtime, "observe_telemetry", observed.append)

    monitor._sample()
    assert len(observed) == 1

    second_settings_client.save(_patch(1, telemetry_enabled=False), expected_revision=1)
    monitor._sample()
    assert len(observed) == 1
    assert monitor.enabled is False

    second_settings_client.save(_patch(1, telemetry_enabled=True), expected_revision=2)
    monitor._sample()
    assert len(observed) == 2
    assert monitor.enabled is True
    provenance = monitor.provenance()
    assert provenance["machine_revision"] == 3
    assert provenance["latest"]["memory_pressure_source"] == "macos_native"
    assert provenance["latest"]["native_memory_pressure_level"] == 0
    assert provenance["latest"]["swap_out_rate_bytes_per_second"] == 0.0


def test_monitor_skips_adaptation_when_machine_settings_cannot_be_refreshed(
    tmp_path, monkeypatch
):
    runtime, monitor, _settings_client = _runtime_and_monitor(tmp_path, monkeypatch)
    observed: list[TelemetrySnapshot] = []
    monkeypatch.setattr(runtime, "observe_telemetry", observed.append)

    def fail_refresh():
        raise OSError("machine settings temporarily unavailable")

    monitor._resolver = fail_refresh
    with pytest.raises(OSError, match="temporarily unavailable"):
        monitor._sample()

    assert observed == []
    assert monitor.config_refresh_failures == 1
    assert monitor.samples == 0
