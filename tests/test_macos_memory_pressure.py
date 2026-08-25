from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from proof_assistant.concurrency import (
    MemoryPressureClassifier,
    MemoryPressurePolicy,
    MemoryPressureSource,
    PressureState,
    TelemetryCollector,
    query_macos_memory_pressure_level,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float = 1.0) -> None:
        self.value += seconds


class FakePsutil:
    def __init__(
        self,
        *,
        total: int = 100,
        available: int = 40,
        swap_used: int = 80,
        swap_out: int | None = 1_000,
    ) -> None:
        self.total = total
        self.available = available
        self.swap_used = swap_used
        self.swap_out = swap_out

    def virtual_memory(self):
        return SimpleNamespace(total=self.total, available=self.available)

    def swap_memory(self):
        values = {"used": self.swap_used}
        if self.swap_out is not None:
            values["sout"] = self.swap_out
        return SimpleNamespace(**values)

    def Process(self):
        return SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=12),
            memory_full_info=lambda: SimpleNamespace(pss=10),
        )

    def cpu_times_percent(self, interval=None):
        return SimpleNamespace(iowait=0.0)

    def cpu_percent(self, interval=None):
        return 10.0


def _policy() -> MemoryPressurePolicy:
    return MemoryPressurePolicy(
        swap_out_memory_fraction_per_second=0.001,
        swap_out_minimum_bytes_per_second=10.0,
        swap_out_maximum_bytes_per_second=10.0,
    )


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    (
        (0, "0\n", 0),
        (0, "1\n", 1),
        (0, "2\n", 2),
        (0, "3\n", 3),
        (1, "2\n", None),
        (0, "not-a-level\n", None),
        (0, "4\n", None),
        (0, "99\n", None),
    ),
)
def test_native_query_validates_exit_status_and_output(returncode, stdout, expected):
    observed = []

    def runner(argv, **kwargs):
        observed.append((argv, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    assert (
        query_macos_memory_pressure_level(os_name="Darwin", runner=runner) == expected
    )
    assert observed[0][0] == [
        "/usr/sbin/sysctl",
        "-n",
        "kern.memorystatus_vm_pressure_level",
    ]
    assert observed[0][1]["timeout"] == 0.5


@pytest.mark.parametrize(
    "error",
    (FileNotFoundError("sysctl missing"), subprocess.TimeoutExpired("sysctl", 0.5)),
)
def test_native_query_tolerates_absent_or_timed_out_sysctl(error):
    def runner(*args, **kwargs):
        raise error

    assert query_macos_memory_pressure_level(os_name="Darwin", runner=runner) is None


def test_native_query_is_not_attempted_off_darwin():
    def runner(*args, **kwargs):
        raise AssertionError("runner must not be called")

    assert query_macos_memory_pressure_level(os_name="Linux", runner=runner) is None


def test_darwin_21_old_swap_and_swap_occupancy_growth_remain_green(monkeypatch):
    monkeypatch.setattr("platform.release", lambda: "21.6.0")
    fake = FakePsutil(swap_used=80, swap_out=1_000)
    clock = FakeClock()
    collector = TelemetryCollector(
        psutil_module=fake,
        os_name="Darwin",
        clock=clock,
        load_average=lambda: (1.0, 1.0, 1.0),
        native_memory_pressure=lambda: 0,
        pressure_policy=_policy(),
    )
    first = collector.sample()
    assert first.pressure == PressureState.GREEN
    assert first.swap_used_bytes == 80

    fake.swap_used = 95
    clock.advance()
    second = collector.sample()
    assert second.pressure == PressureState.GREEN
    assert second.swap_out_delta_bytes == 0
    assert second.swap_out_rate_bytes_per_second == 0.0
    assert second.memory_pressure_source == MemoryPressureSource.MACOS_NATIVE


def test_macos_transient_swap_out_is_ignored_but_sustained_rate_is_yellow():
    fake = FakePsutil()
    clock = FakeClock()
    collector = TelemetryCollector(
        psutil_module=fake,
        os_name="Darwin",
        clock=clock,
        load_average=lambda: (1.0, 1.0, 1.0),
        native_memory_pressure=lambda: 0,
        pressure_policy=_policy(),
    )
    assert collector.sample().pressure == PressureState.GREEN
    fake.swap_out = 1_020
    clock.advance()
    transient = collector.sample()
    assert transient.active_swap_out is True
    assert transient.pressure_candidate == PressureState.YELLOW
    assert transient.pressure == PressureState.GREEN

    fake.swap_out = 1_040
    clock.advance()
    sustained = collector.sample()
    assert sustained.pressure == PressureState.YELLOW


@pytest.mark.parametrize(
    ("ratio", "native", "expected"),
    (
        (0.40, 0, PressureState.GREEN),
        (0.20, 0, PressureState.YELLOW),
        (0.12, 0, PressureState.RED),
        (0.06, 0, PressureState.EMERGENCY),
        (0.40, 1, PressureState.YELLOW),
        (0.40, 2, PressureState.YELLOW),
        (0.40, 3, PressureState.RED),
    ),
)
def test_macos_available_thresholds_and_native_floors(ratio, native, expected):
    classifier = MemoryPressureClassifier(os_name="Darwin", policy=_policy())
    decision = classifier.classify(
        available_memory_ratio=ratio,
        total_memory_bytes=100,
        swap_out_rate_bytes_per_second=0.0,
        native_memory_pressure_level=native,
        memory_psi_available=False,
    )
    assert decision.pressure == expected


def test_macos_native_critical_escalates_immediately_and_recovery_is_slow():
    classifier = MemoryPressureClassifier(os_name="Darwin", policy=_policy())

    def classify(ratio=0.40, native=0):
        return classifier.classify(
            available_memory_ratio=ratio,
            total_memory_bytes=100,
            swap_out_rate_bytes_per_second=0.0,
            native_memory_pressure_level=native,
            memory_psi_available=False,
        ).pressure

    assert classify() == PressureState.GREEN
    assert classify(native=3) == PressureState.RED
    for _ in range(3):
        assert classify() == PressureState.RED
    assert classify() == PressureState.GREEN


def test_macos_ordinary_worsening_requires_two_samples():
    classifier = MemoryPressureClassifier(os_name="Darwin", policy=_policy())

    def classify(ratio):
        return classifier.classify(
            available_memory_ratio=ratio,
            total_memory_bytes=100,
            swap_out_rate_bytes_per_second=0.0,
            native_memory_pressure_level=0,
            memory_psi_available=False,
        ).pressure

    assert classify(0.40) == PressureState.GREEN
    assert classify(0.18) == PressureState.GREEN
    assert classify(0.18) == PressureState.YELLOW


def test_macos_fallback_tolerates_missing_sout_and_uses_available_memory_only():
    fake = FakePsutil(available=10, swap_out=None)
    collector = TelemetryCollector(
        psutil_module=fake,
        os_name="Darwin",
        clock=FakeClock(),
        load_average=lambda: (1.0, 1.0, 1.0),
        native_memory_pressure=lambda: None,
        pressure_policy=_policy(),
    )
    sample = collector.sample()
    assert sample.pressure == PressureState.RED
    assert sample.swap_out_delta_bytes is None
    assert sample.swap_out_rate_bytes_per_second is None
    assert sample.memory_pressure_source == MemoryPressureSource.MACOS_FALLBACK


def test_swap_out_counter_reset_starts_a_new_baseline():
    fake = FakePsutil(swap_out=1_000)
    clock = FakeClock()
    collector = TelemetryCollector(
        psutil_module=fake,
        os_name="Darwin",
        clock=clock,
        load_average=lambda: (1.0, 1.0, 1.0),
        native_memory_pressure=lambda: 0,
        pressure_policy=_policy(),
    )
    collector.sample()
    fake.swap_out = 10
    clock.advance()
    reset = collector.sample()
    assert reset.swap_out_delta_bytes == 0
    assert reset.swap_out_rate_bytes_per_second == 0.0
    assert reset.pressure == PressureState.GREEN


def test_linux_allocation_ratio_and_sustained_swap_out_preserve_strong_response(
    tmp_path,
):
    fake = FakePsutil(total=100, available=90, swap_out=1_000)
    clock = FakeClock()
    psi = tmp_path / "pressure"
    psi.mkdir()
    for name in ("cpu", "memory", "io"):
        (psi / name).write_text("some avg10=0.00 avg60=0.00 total=0\n")
    collector = TelemetryCollector(
        psutil_module=fake,
        os_name="Linux",
        clock=clock,
        load_average=lambda: (1.0, 1.0, 1.0),
        psi_root=psi,
        pressure_policy=_policy(),
    )
    allocation = SimpleNamespace(total_memory_bytes=20, available_memory_bytes=18)
    assert (
        collector.sample(memory_allocation=allocation).pressure == PressureState.GREEN
    )

    fake.swap_out = 1_020
    clock.advance()
    assert (
        collector.sample(memory_allocation=allocation).pressure == PressureState.GREEN
    )
    fake.swap_out = 1_040
    clock.advance()
    sustained = collector.sample(memory_allocation=allocation)
    assert sustained.pressure == PressureState.RED
    assert sustained.memory_pressure_source == MemoryPressureSource.LINUX_PSI

    constrained = SimpleNamespace(total_memory_bytes=20, available_memory_bytes=1)
    clock.advance()
    assert (
        collector.sample(memory_allocation=constrained).pressure
        == PressureState.EMERGENCY
    )
