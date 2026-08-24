from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from proof_assistant.concurrency import (
    AIConcurrencyConfig,
    AutoValue,
    CodexPlan,
    ConcurrencyConfig,
    HardwareResources,
    MemoryPressurePolicy,
    PressureState,
    QueueDepths,
    ResourceProfile,
    TelemetryCollector,
    derive_auto_limits,
    derive_concurrency_status,
    detect_hardware,
    parse_psi,
    resolve_concurrency_config,
)

GIB = 1024**3


class FakePsutil:
    Error = RuntimeError

    def __init__(self, *, logical=16, physical=8, total=64 * GIB, available=48 * GIB):
        self.logical = logical
        self.physical = physical
        self.memory = SimpleNamespace(total=total, available=available)

    def cpu_count(self, logical):
        return self.logical if logical else self.physical

    def virtual_memory(self):
        return self.memory


def mapping_reader(values: dict[str, str]):
    def read(path: Path) -> str:
        try:
            return values[path.as_posix()]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    return read


def test_macos_detection_uses_host_topology_and_detects_ssh_as_server():
    resources = detect_hardware(
        psutil_module=FakePsutil(logical=10, physical=8, total=32 * GIB),
        environ={"SSH_CONNECTION": "client server"},
        os_name="Darwin",
        architecture="arm64",
        affinity=set(range(10)),
    )
    assert resources.usable_logical_cpus == 10
    assert resources.usable_physical_cpus == 8
    assert resources.total_memory_bytes == 32 * GIB
    assert resources.interactive_detected is False


def test_linux_detection_honors_affinity_cgroup_cpuset_quota_and_slurm(tmp_path):
    cgroup = tmp_path / "cgroup"
    reader = mapping_reader(
        {
            (cgroup / "cpu.max").as_posix(): "600000 100000",
            (cgroup / "cpuset.cpus.effective").as_posix(): "0-7",
            (cgroup / "memory.max").as_posix(): str(20 * GIB),
            (cgroup / "memory.current").as_posix(): str(5 * GIB),
        }
    )
    resources = detect_hardware(
        psutil_module=FakePsutil(
            logical=64, physical=32, total=128 * GIB, available=90 * GIB
        ),
        environ={"SLURM_CPUS_PER_TASK": "4", "SLURM_MEM_PER_NODE": "12288"},
        os_name="Linux",
        affinity=set(range(16)),
        cgroup_root=cgroup,
        read_text=reader,
    )
    assert resources.affinity_cpus == 16
    assert resources.cgroup_cpus == 6
    assert resources.slurm_cpus == 4
    assert resources.usable_logical_cpus == 4
    assert resources.usable_physical_cpus == 2
    assert resources.cgroup_memory_bytes == 20 * GIB
    assert resources.slurm_memory_bytes == 12 * GIB
    assert resources.total_memory_bytes == 12 * GIB
    assert resources.available_memory_bytes == 12 * GIB


def test_missing_optional_linux_metrics_degrade_cleanly(tmp_path):
    resources = detect_hardware(
        psutil_module=FakePsutil(logical=8, physical=4, total=16 * GIB),
        environ={},
        os_name="Linux",
        affinity=set(range(8)),
        cgroup_root=tmp_path,
        read_text=mapping_reader({}),
    )
    assert resources.usable_logical_cpus == 8
    assert resources.cgroup_cpus is None
    assert resources.cgroup_memory_bytes is None


@pytest.mark.parametrize(
    ("plan", "initial", "ceiling"),
    [
        (CodexPlan.PLUS, 2, 6),
        (CodexPlan.PRO_5X, 4, 12),
        (CodexPlan.PRO_20X, 8, 24),
        (CodexPlan.UNKNOWN, 4, 8),
        (CodexPlan.API, 4, 8),
    ],
)
def test_ai_plan_profiles_match_guide(plan, initial, ceiling):
    hardware = HardwareResources(
        "Linux", "x86_64", 32, 16, 32, 16, 64 * GIB, 64 * GIB, 50 * GIB, False
    )
    limits = derive_auto_limits(
        hardware,
        ConcurrencyConfig(ai=AIConcurrencyConfig(plan=plan)),
    )
    assert (limits.ai_initial, limits.ai_ceiling) == (initial, ceiling)


def test_small_interactive_machine_is_ram_limited_and_build_conservative():
    hardware = HardwareResources(
        "Darwin", "arm64", 8, 4, 8, 4, 8 * GIB, 8 * GIB, 6 * GIB, True
    )
    limits = derive_auto_limits(hardware, ConcurrencyConfig())
    assert limits.resource_profile == ResourceProfile.INTERACTIVE
    assert limits.memory_reserve_gib == 6.0
    assert limits.lean_cpu_cap == 2
    assert limits.lean_memory_cap == 1
    assert limits.lean_pool == 1
    assert limits.build_concurrency == 1


def test_large_server_lean_formula_uses_cpu_ram_and_initial_cap():
    hardware = HardwareResources(
        "Linux",
        "x86_64",
        128,
        64,
        128,
        64,
        256 * GIB,
        256 * GIB,
        200 * GIB,
        False,
    )
    limits = derive_auto_limits(hardware, ConcurrencyConfig())
    assert limits.resource_profile == ResourceProfile.SERVER
    assert limits.lean_cpu_cap == 57
    assert limits.lean_memory_cap == 43
    assert limits.lean_pool == 32
    assert limits.build_concurrency == 3
    calibrated = derive_auto_limits(
        hardware, ConcurrencyConfig(), calibrated_repl_p95_gib=2.0
    )
    assert calibrated.repl_memory_budget_gib == 3.0


def test_public_status_distinguishes_configured_effective_and_sources():
    hardware = HardwareResources(
        "Darwin", "arm64", 10, 8, 10, 8, 32 * GIB, 32 * GIB, 20 * GIB, True
    )
    resolved = resolve_concurrency_config(environ={})
    status = derive_concurrency_status(resolved, hardware)
    assert status.ai.configured == AutoValue.AUTO
    assert status.ai.effective == 4
    assert status.ai.maximum == 8
    assert status.lean.effective >= 1
    assert status.build.effective == 1
    assert status.scheduler.agents_per_target_initial == 1
    assert status.scheduler.agents_per_target_max == 4
    assert status.decisions
    assert status.hardware is hardware


class FakeTelemetryPsutil:
    Error = RuntimeError

    def __init__(self):
        self.available = 40
        self.swap_used = 100
        self.swap_out = 1_000

    def virtual_memory(self):
        return SimpleNamespace(total=100, available=self.available)

    def swap_memory(self):
        return SimpleNamespace(used=self.swap_used, sout=self.swap_out)

    def Process(self):
        return SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=12),
            memory_full_info=lambda: SimpleNamespace(pss=10),
        )

    def cpu_times_percent(self, interval=None):
        return SimpleNamespace(iowait=2.5)

    def cpu_percent(self, interval=None):
        return 25.0


def test_telemetry_pressure_active_swap_out_and_linux_psi(tmp_path):
    fake = FakeTelemetryPsutil()
    times = iter((10.0, 15.0, 20.0, 25.0, 30.0))
    psi = tmp_path / "pressure"
    psi_text = "some avg10=1.50 avg60=0.50 avg300=0.10 total=1\nfull avg10=0.20 avg60=0.10 avg300=0.00 total=1\n"
    collector = TelemetryCollector(
        psutil_module=fake,
        os_name="Linux",
        clock=lambda: next(times),
        load_average=lambda: (1.0, 2.0, 3.0),
        psi_root=psi,
        read_text=mapping_reader(
            {(psi / name).as_posix(): psi_text for name in ("cpu", "memory", "io")}
        ),
        pressure_policy=MemoryPressurePolicy(
            swap_out_memory_fraction_per_second=0.001,
            swap_out_minimum_bytes_per_second=10.0,
            swap_out_maximum_bytes_per_second=10.0,
        ),
    )
    first = collector.sample(queues=QueueDepths(ai=2, lean=1, build=0))
    assert first.pressure == PressureState.GREEN
    assert first.queues.ai == 2
    assert first.cpu_psi is not None and first.cpu_psi.some_avg10 == 1.5
    fake.swap_used = 150
    second = collector.sample()
    assert second.swap_out_delta_bytes == 0
    assert second.swap_out_rate_bytes_per_second == 0.0
    assert second.pressure == PressureState.GREEN
    fake.swap_out = 1_100
    third = collector.sample()
    assert third.swap_out_delta_bytes == 100
    assert third.swap_out_rate_bytes_per_second == 20.0
    assert third.pressure == PressureState.GREEN
    fake.swap_out = 1_200
    fourth = collector.sample()
    assert fourth.pressure == PressureState.RED
    fake.swap_used = 200
    fake.available = 7
    fifth = collector.sample()
    assert fifth.pressure == PressureState.EMERGENCY


def test_psi_parser_tolerates_missing_or_malformed_metrics():
    assert parse_psi("") is None
    assert parse_psi("unexpected") is None
    parsed = parse_psi("some avg10=3.2 avg60=1.0 total=42")
    assert parsed is not None and parsed.some_avg60 == 1.0
