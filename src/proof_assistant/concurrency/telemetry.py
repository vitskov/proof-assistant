"""Low-cost, injectable resource telemetry for adaptive controllers and UIs."""

from __future__ import annotations

import os
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from .config import PressureState
from .macos_memory import query_macos_memory_pressure_level
from .memory_pressure import (
    MemoryPressureClassifier,
    MemoryPressurePolicy,
    MemoryPressureSource,
)


@dataclass(frozen=True)
class PressureStallMetrics:
    some_avg10: float | None = None
    some_avg60: float | None = None
    full_avg10: float | None = None
    full_avg60: float | None = None


@dataclass(frozen=True)
class QueueDepths:
    ai: int = 0
    lean: int = 0
    build: int = 0


@dataclass(frozen=True)
class TelemetrySnapshot:
    monotonic_time: float
    cpu_percent: float
    load_average: tuple[float, float, float] | None
    total_memory_bytes: int
    available_memory_bytes: int
    available_memory_ratio: float
    swap_used_bytes: int
    swap_out_delta_bytes: int | None
    swap_out_rate_bytes_per_second: float | None
    process_rss_bytes: int
    process_pss_bytes: int | None
    disk_iowait_percent: float | None
    pressure: PressureState
    pressure_candidate: PressureState
    native_memory_pressure_level: int | None
    memory_pressure_source: MemoryPressureSource
    active_swap_out: bool
    swap_out_threshold_bytes_per_second: float
    pressure_reasons: tuple[str, ...]
    queues: QueueDepths
    cpu_psi: PressureStallMetrics | None = None
    memory_psi: PressureStallMetrics | None = None
    io_psi: PressureStallMetrics | None = None


def parse_psi(text: str) -> PressureStallMetrics | None:
    rows: dict[str, dict[str, float]] = {}
    try:
        for line in text.splitlines():
            parts = line.split()
            if not parts or parts[0] not in {"some", "full"}:
                continue
            rows[parts[0]] = {
                key: float(value)
                for item in parts[1:]
                if "=" in item
                for key, value in (item.split("=", 1),)
                if key.startswith("avg")
            }
    except ValueError:
        return None
    if not rows:
        return None
    return PressureStallMetrics(
        some_avg10=rows.get("some", {}).get("avg10"),
        some_avg60=rows.get("some", {}).get("avg60"),
        full_avg10=rows.get("full", {}).get("avg10"),
        full_avg60=rows.get("full", {}).get("avg60"),
    )


class TelemetryCollector:
    """Stateful sampler with platform-aware memory-pressure classification."""

    def __init__(
        self,
        *,
        psutil_module: Any = psutil,
        os_name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        load_average: Callable[[], tuple[float, float, float]] = os.getloadavg,
        psi_root: Path = Path("/proc/pressure"),
        read_text: Callable[[Path], str] | None = None,
        native_memory_pressure: Callable[[], int | None] | None = None,
        pressure_policy: MemoryPressurePolicy | None = None,
    ) -> None:
        self.psutil = psutil_module
        self.os_name = os_name or platform.system()
        self.clock = clock
        self.load_average = load_average
        self.psi_root = psi_root
        self.read_text = read_text or (lambda path: path.read_text(encoding="utf-8"))
        self.native_memory_pressure = native_memory_pressure or (
            lambda: query_macos_memory_pressure_level(os_name=self.os_name)
        )
        self.pressure_classifier = MemoryPressureClassifier(
            os_name=self.os_name,
            policy=pressure_policy,
        )
        self._last_time: float | None = None
        self._last_swap_out: int | None = None

    def _psi(self, resource: str) -> PressureStallMetrics | None:
        if self.os_name != "Linux":
            return None
        try:
            return parse_psi(self.read_text(self.psi_root / resource))
        except (OSError, UnicodeError):
            return None

    def sample(
        self,
        *,
        queues: QueueDepths | None = None,
        memory_allocation: Any | None = None,
    ) -> TelemetrySnapshot:
        now = self.clock()
        memory = self.psutil.virtual_memory()
        swap = self.psutil.swap_memory()
        total = max(1, int(memory.total))
        available = max(0, min(total, int(memory.available)))
        if memory_allocation is not None:
            total = max(
                1,
                min(total, int(memory_allocation.total_memory_bytes)),
            )
            available = max(
                0,
                min(
                    available,
                    int(memory_allocation.available_memory_bytes),
                    total,
                ),
            )
        swap_used = max(0, int(swap.used))
        elapsed = (
            max(1e-9, now - self._last_time) if self._last_time is not None else 0.0
        )
        raw_swap_out = getattr(swap, "sout", None)
        try:
            swap_out = max(0, int(raw_swap_out)) if raw_swap_out is not None else None
        except (TypeError, ValueError, OverflowError):
            swap_out = None
        if swap_out is None:
            swap_out_delta: int | None = None
            swap_out_rate: float | None = None
            self._last_swap_out = None
        else:
            if self._last_swap_out is None or swap_out < self._last_swap_out:
                swap_out_delta = 0
            else:
                swap_out_delta = swap_out - self._last_swap_out
            swap_out_rate = swap_out_delta / elapsed if elapsed else 0.0
            self._last_swap_out = swap_out
        ratio = available / total
        cpu_psi = self._psi("cpu")
        memory_psi = self._psi("memory")
        io_psi = self._psi("io")
        try:
            native_pressure = self.native_memory_pressure()
        except Exception:
            native_pressure = None
        if native_pressure not in {0, 1, 2, 3}:
            native_pressure = None
        decision = self.pressure_classifier.classify(
            available_memory_ratio=ratio,
            total_memory_bytes=total,
            swap_out_rate_bytes_per_second=swap_out_rate,
            native_memory_pressure_level=native_pressure,
            memory_psi_available=memory_psi is not None,
        )

        process = self.psutil.Process()
        rss = int(process.memory_info().rss)
        pss: int | None = None
        try:
            raw_pss = getattr(process.memory_full_info(), "pss", None)
            pss = int(raw_pss) if raw_pss is not None else None
        except Exception:
            pass
        iowait: float | None = None
        try:
            raw_iowait = getattr(self.psutil.cpu_times_percent(interval=None), "iowait")
            iowait = float(raw_iowait)
        except Exception:
            pass
        try:
            loads = tuple(float(value) for value in self.load_average())
        except OSError:
            loads = None
        snapshot = TelemetrySnapshot(
            monotonic_time=now,
            cpu_percent=float(self.psutil.cpu_percent(interval=None)),
            load_average=loads,
            total_memory_bytes=total,
            available_memory_bytes=available,
            available_memory_ratio=ratio,
            swap_used_bytes=swap_used,
            swap_out_delta_bytes=swap_out_delta,
            swap_out_rate_bytes_per_second=swap_out_rate,
            process_rss_bytes=rss,
            process_pss_bytes=pss,
            disk_iowait_percent=iowait,
            pressure=decision.pressure,
            pressure_candidate=decision.candidate,
            native_memory_pressure_level=native_pressure,
            memory_pressure_source=decision.source,
            active_swap_out=decision.active_swap_out,
            swap_out_threshold_bytes_per_second=(
                decision.swap_out_threshold_bytes_per_second
            ),
            pressure_reasons=decision.reasons,
            queues=queues or QueueDepths(),
            cpu_psi=cpu_psi,
            memory_psi=memory_psi,
            io_psi=io_psi,
        )
        self._last_time = now
        return snapshot
