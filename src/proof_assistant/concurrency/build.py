"""Independent admission and conservative adaptation for Lake builds."""

from __future__ import annotations

import fcntl
from dataclasses import dataclass
from typing import Any

from .admission import (
    AdmissionController,
    AdmissionLease,
    AdmissionRequest,
    ResourceKind,
    SQLiteAdmissionStore,
)
from .lean import fixed_mode, pressure_name


@dataclass(frozen=True)
class BuildControllerStatus:
    current_limit: int
    minimum: int
    maximum: int
    hard_maximum: int
    active: int
    queued: int
    pressure: str
    admission_paused: bool
    last_resize_at: float | None


class BuildAdmissionController(AdmissionController):
    """A build-only controller; it never borrows AI or Lean capacity."""

    def __init__(
        self,
        store: SQLiteAdmissionStore,
        *,
        initial: int,
        minimum: int = 1,
        maximum: int | None = None,
        hard_maximum: int = 8,
        mode: Any = "adaptive",
        resize_interval_seconds: float = 60.0,
        hysteresis_samples: int = 2,
    ) -> None:
        maximum = hard_maximum if maximum is None else maximum
        if not 0 <= minimum <= initial <= maximum <= hard_maximum:
            raise ValueError("invalid build concurrency bounds")
        if resize_interval_seconds < 0 or hysteresis_samples < 1:
            raise ValueError("invalid build adaptation timing")
        self.minimum = minimum
        self.maximum = maximum
        self.hard_maximum = hard_maximum
        self.mode = mode
        self.resize_interval_seconds = resize_interval_seconds
        self.hysteresis_samples = hysteresis_samples
        self._last_resize_at: float | None = None
        self._grow_samples = 0
        self._shrink_samples = 0
        self._pressure = "green"
        self._paused = False
        super().__init__(store, ResourceKind.BUILD, initial)

    @property
    def is_fixed(self) -> bool:
        return fixed_mode(self.mode)

    def request(
        self,
        owner: str,
        *,
        full_build: bool = True,
        ttl_seconds: float = 900.0,
        request_id: str | None = None,
    ) -> AdmissionRequest:
        # Targeted checks get preference over expensive full certification builds.
        priority = 20 if full_build else 10
        return AdmissionRequest(
            ResourceKind.BUILD,
            priority,
            owner,
            ttl_seconds,
            request_id,
        )

    def try_acquire(self, request: AdmissionRequest) -> AdmissionLease | None:
        shared = self.store.get_state(ResourceKind.BUILD, "pressure") or {}
        shared_fresh = self.store.now() - float(shared.get("observed_at", 0.0)) <= 20.0
        state = str(shared.get("state")) if shared_fresh else self._pressure
        blocks_all = state in {"red", "emergency"}
        if blocks_all:
            return None
        if state == "yellow" and request.priority >= 20:
            # Yellow pressure must reduce full-build concurrency without making
            # forward progress depend on memory returning to green.  Serialize
            # this cross-process check so exactly one build may enter even when
            # the configured limit is larger than one.
            lock_path = self.store.path.with_name("build-yellow-admission.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    if self.snapshot().active >= 1:
                        return None
                    return super().try_acquire(request)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return super().try_acquire(request)

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
        io_pressure: bool = False,
        throughput_improved: bool = True,
    ) -> int:
        if queue_depth < 0:
            raise ValueError("build queue depth must not be negative")
        state = pressure_name(pressure)
        self._pressure = state
        # Yellow intentionally stops admission of additional full-build pressure.
        self._paused = state in {"yellow", "red", "emergency"}
        self.store.set_state(
            ResourceKind.BUILD,
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

        shrink = state == "red" or io_pressure
        grow = (
            state == "green"
            and not io_pressure
            and queue_depth > 0
            and throughput_improved
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

    def apply_manual_limit(
        self, limit: int, *, allow_above_hard_maximum: bool = False
    ) -> int:
        if limit < self.minimum:
            raise ValueError("manual build limit is below the configured minimum")
        if limit > self.hard_maximum and not allow_above_hard_maximum:
            raise ValueError("manual build limit exceeds the hard safety ceiling")
        if allow_above_hard_maximum and limit > self.hard_maximum:
            self.hard_maximum = limit
            self.maximum = limit
        elif limit > self.maximum:
            raise ValueError("manual build limit is outside configured bounds")
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
            ResourceKind.BUILD,
            "pressure",
            {
                "state": "green",
                "paused": False,
                "observed_at": self.store.now(),
            },
        )

    def status(self) -> BuildControllerStatus:
        snapshot = self.snapshot()
        shared = self.store.get_state(ResourceKind.BUILD, "pressure") or {}
        shared_fresh = self.store.now() - float(shared.get("observed_at", 0.0)) <= 20.0
        pressure = str(shared.get("state")) if shared_fresh else self._pressure
        paused = pressure in {"red", "emergency"} or (
            pressure == "yellow" and snapshot.active >= 1
        )
        return BuildControllerStatus(
            current_limit=snapshot.limit,
            minimum=self.minimum,
            maximum=self.maximum,
            hard_maximum=self.hard_maximum,
            active=snapshot.active,
            queued=snapshot.queued,
            pressure=pressure,
            admission_paused=paused,
            last_resize_at=self._last_resize_at,
        )
