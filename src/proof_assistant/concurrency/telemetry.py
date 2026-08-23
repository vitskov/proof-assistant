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
    swap_delta_bytes: int
    swap_rate_bytes_per_second: float
    process_rss_bytes: int
    process_pss_bytes: int | None
    disk_iowait_percent: float | None
    pressure: PressureState
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
    """Stateful sampler that derives swap growth and pressure hysteresis."""

    def __init__(
        self,
        *,
        psutil_module: Any = psutil,
        os_name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        load_average: Callable[[], tuple[float, float, float]] = os.getloadavg,
        psi_root: Path = Path("/proc/pressure"),
        read_text: Callable[[Path], str] | None = None,
    ) -> None:
        self.psutil = psutil_module
        self.os_name = os_name or platform.system()
        self.clock = clock
        self.load_average = load_average
        self.psi_root = psi_root
        self.read_text = read_text or (lambda path: path.read_text(encoding="utf-8"))
        self._last_time: float | None = None
        self._last_swap_used: int | None = None
        self._swap_growth_samples = 0

    def _psi(self, resource: str) -> PressureStallMetrics | None:
        if self.os_name != "Linux":
            return None
        try:
            return parse_psi(self.read_text(self.psi_root / resource))
        except (OSError, UnicodeError):
            return None

    def sample(self, *, queues: QueueDepths | None = None) -> TelemetrySnapshot:
        now = self.clock()
        memory = self.psutil.virtual_memory()
        swap = self.psutil.swap_memory()
        total = max(1, int(memory.total))
        available = max(0, min(total, int(memory.available)))
        swap_used = max(0, int(swap.used))
        if self._last_swap_used is None:
            swap_delta = 0
        else:
            swap_delta = swap_used - self._last_swap_used
        elapsed = (
            max(1e-9, now - self._last_time) if self._last_time is not None else 0.0
        )
        swap_rate = swap_delta / elapsed if elapsed else 0.0
        if swap_delta > 0:
            self._swap_growth_samples += 1
        else:
            self._swap_growth_samples = 0
        ratio = available / total
        if ratio < 0.08:
            pressure = PressureState.EMERGENCY
        elif ratio < 0.15 or self._swap_growth_samples >= 2:
            pressure = PressureState.RED
        elif ratio <= 0.30 or self._swap_growth_samples == 1:
            pressure = PressureState.YELLOW
        else:
            pressure = PressureState.GREEN

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
            swap_delta_bytes=swap_delta,
            swap_rate_bytes_per_second=swap_rate,
            process_rss_bytes=rss,
            process_pss_bytes=pss,
            disk_iowait_percent=iowait,
            pressure=pressure,
            queues=queues or QueueDepths(),
            cpu_psi=self._psi("cpu"),
            memory_psi=self._psi("memory"),
            io_psi=self._psi("io"),
        )
        self._last_time = now
        self._last_swap_used = swap_used
        return snapshot
