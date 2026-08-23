"""Lean admission and pressure-aware pool adaptation."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .admission import (
    AdmissionController,
    AdmissionLease,
    AdmissionRequest,
    ResourceKind,
    SQLiteAdmissionStore,
)


def pressure_name(pressure: Any) -> str:
    return str(getattr(pressure, "value", pressure)).casefold()


def fixed_mode(mode: Any) -> bool:
    return str(getattr(mode, "value", mode)).casefold() in {"fixed", "manual"}


@dataclass(frozen=True)
class LeanControllerStatus:
    current_limit: int
    minimum: int
    maximum: int
    active: int
    queued: int
    pressure: str
    admission_paused: bool
    last_resize_at: float | None


class LeanAdmissionController(AdmissionController):
    """CPU/RAM-sensitive Lean admission with conservative hysteresis."""

    def __init__(
        self,
        store: SQLiteAdmissionStore,
        *,
        initial: int,
        minimum: int = 1,
        maximum: int,
        mode: Any = "adaptive",
        resize_interval_seconds: float = 45.0,
        hysteresis_samples: int = 2,
    ) -> None:
        if not 0 <= minimum <= initial <= maximum:
            raise ValueError("invalid Lean concurrency bounds")
        if resize_interval_seconds < 0 or hysteresis_samples < 1:
            raise ValueError("invalid Lean adaptation timing")
        self.minimum = minimum
        self.maximum = maximum
        self.mode = mode
        self.resize_interval_seconds = resize_interval_seconds
        self.hysteresis_samples = hysteresis_samples
        self._last_resize_at: float | None = None
        self._grow_samples = 0
        self._shrink_samples = 0
        self._pressure = "green"
        self._paused = False
        super().__init__(store, ResourceKind.LEAN, initial)

    @property
    def is_fixed(self) -> bool:
        return fixed_mode(self.mode)

    def request(
        self,
        owner: str,
        *,
        priority: int = 10,
        ttl_seconds: float = 120.0,
        request_id: str | None = None,
    ) -> AdmissionRequest:
        return AdmissionRequest(
            ResourceKind.LEAN,
            priority,
            owner,
            ttl_seconds,
            request_id,
        )

    def try_acquire(self, request: AdmissionRequest) -> AdmissionLease | None:
        calibration = self.store.get_state(ResourceKind.LEAN, "calibration") or {}
        calibration_active = bool(calibration.get("paused")) and (
            float(calibration.get("expires_at", 0.0)) > self.store.now()
        )
        if calibration_active and calibration.get("owner") != request.owner:
            return None
        shared = self.store.get_state(ResourceKind.LEAN, "pressure") or {}
        shared_pause = bool(shared.get("paused")) and (
            self.store.now() - float(shared.get("observed_at", 0.0)) <= 20.0
        )
        if self._paused or shared_pause:
            return None
        return super().try_acquire(request)

    @contextmanager
    def exclusive_calibration_lease(
        self,
        owner: str,
        *,
        timeout: float = 60.0,
        ttl_seconds: float = 3600.0,
    ):
        """Pause new Lean work and admit one crash-recoverable calibration.

        Calibration is refused when work is already active or queued. A
        process-scoped file lock serializes competing calibrators, while the
        expiring SQLite state stops ordinary controllers in other processes
        from entering after the idle check.
        """

        if not owner.strip() or timeout <= 0 or ttl_seconds <= 0:
            raise ValueError("Lean calibration lease bounds must be positive")
        lock_path = self.store.path.with_name("lean-calibration.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "Another Lean calibration is already active"
                ) from exc
            try:
                before = self.snapshot()
                if before.active or before.queued:
                    raise RuntimeError(
                        "Lean calibration requires an idle Lean admission queue"
                    )
                self.store.set_state(
                    ResourceKind.LEAN,
                    "calibration",
                    {
                        "paused": True,
                        "owner": owner,
                        "expires_at": self.store.now() + ttl_seconds,
                    },
                )
                after = self.snapshot()
                if after.active or after.queued:
                    raise RuntimeError(
                        "Lean work arrived while calibration was entering; retry when idle"
                    )
                request = self.request(
                    owner,
                    priority=-100,
                    ttl_seconds=ttl_seconds,
                    request_id=owner,
                )
                with self.lease(request, timeout=timeout) as lease:
                    yield lease
            finally:
                self.store.set_state(
                    ResourceKind.LEAN,
                    "calibration",
                    {
                        "paused": False,
                        "owner": owner,
                        "expires_at": 0.0,
                    },
                )
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _resize_allowed(self, now: float) -> bool:
        return self._last_resize_at is None or (
            now - self._last_resize_at >= self.resize_interval_seconds
        )

    def _resize(self, limit: int, now: float) -> int:
        bounded = max(self.minimum, min(self.maximum, limit))
        if bounded != self.limit:
            self.set_limit(bounded)
            self._last_resize_at = now
        self._grow_samples = 0
        self._shrink_samples = 0
        return self.limit

    def observe(
        self,
        *,
        pressure: Any,
        queue_depth: int,
        cpu_percent: float,
        swap_growing: bool = False,
        throughput_improved: bool = True,
    ) -> int:
        """Observe one telemetry sample and possibly resize the effective pool."""

        if queue_depth < 0 or not 0 <= cpu_percent <= 100:
            raise ValueError("invalid Lean telemetry")
        state = pressure_name(pressure)
        self._pressure = state
        self._paused = state in {"red", "emergency"}
        self.store.set_state(
            ResourceKind.LEAN,
            "pressure",
            {
                "state": state,
                "paused": self._paused,
                "observed_at": self.store.now(),
            },
        )
        now = self.store.now()

        if self.is_fixed:
            return self.limit
        if state == "emergency":
            return self._resize(self.minimum, now)

        shrink = (
            state == "red"
            or swap_growing
            or (cpu_percent > 92.0 and not throughput_improved)
        )
        grow = (
            state == "green"
            and not swap_growing
            and queue_depth > 0
            and cpu_percent < 75.0
        )
        if shrink:
            self._shrink_samples += 1
            self._grow_samples = 0
            if self._shrink_samples >= self.hysteresis_samples and self._resize_allowed(
                now
            ):
                return self._resize(self.limit - 1, now)
        elif grow:
            self._grow_samples += 1
            self._shrink_samples = 0
            if self._grow_samples >= self.hysteresis_samples and self._resize_allowed(
                now
            ):
                return self._resize(self.limit + 1, now)
        else:
            self._grow_samples = 0
            self._shrink_samples = 0
        return self.limit

    def apply_manual_limit(self, limit: int) -> int:
        if not self.minimum <= limit <= self.maximum:
            raise ValueError("manual Lean limit is outside configured bounds")
        self.mode = "fixed"
        return self.set_limit(limit)

    def restore_adaptive(self) -> None:
        self.mode = "adaptive"

    def reset_adaptive_history(self) -> None:
        """Clear pressure/hysteresis evidence without disturbing active work."""

        self._last_resize_at = None
        self._grow_samples = 0
        self._shrink_samples = 0
        self._pressure = "green"
        self._paused = False
        self.store.set_state(
            ResourceKind.LEAN,
            "pressure",
            {
                "state": "green",
                "paused": False,
                "observed_at": self.store.now(),
            },
        )

    def status(self) -> LeanControllerStatus:
        snapshot = self.snapshot()
        shared = self.store.get_state(ResourceKind.LEAN, "pressure") or {}
        shared_fresh = self.store.now() - float(shared.get("observed_at", 0.0)) <= 20.0
        pressure = str(shared.get("state")) if shared_fresh else self._pressure
        paused = pressure in {"red", "emergency"}
        return LeanControllerStatus(
            current_limit=snapshot.limit,
            minimum=self.minimum,
            maximum=self.maximum,
            active=snapshot.active,
            queued=snapshot.queued,
            pressure=pressure,
            admission_paused=paused,
            last_resize_at=self._last_resize_at,
        )
