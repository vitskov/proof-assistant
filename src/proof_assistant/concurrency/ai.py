"""Global AI admission and adaptive Codex concurrency policy."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any

from .admission import (
    AdmissionController,
    AdmissionLease,
    AdmissionRequest,
    ResourceKind,
    SQLiteAdmissionStore,
)


class AITaskClass(StrEnum):
    CLARIFICATION = "clarification"
    DIAGNOSTIC = "diagnostic"
    PROOF = "proof"
    SKETCH = "sketch"
    MAINTENANCE = "maintenance"
    REVIEW = "review"
    DUPLICATE_PROOF = "duplicate_proof"
    REPORTING = "reporting"


TASK_PRIORITIES: dict[AITaskClass, int] = {
    AITaskClass.CLARIFICATION: 0,
    AITaskClass.DIAGNOSTIC: 0,
    AITaskClass.PROOF: 10,
    AITaskClass.SKETCH: 10,
    AITaskClass.MAINTENANCE: 15,
    AITaskClass.REVIEW: 20,
    AITaskClass.DUPLICATE_PROOF: 30,
    AITaskClass.REPORTING: 40,
}


def _enum_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw).casefold().replace("-", "_")


def _fixed_mode(mode: Any) -> bool:
    return _enum_text(mode) in {"fixed", "manual"}


def _retry_after_seconds(value: float | int | str | None, now: float) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, parsed.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class AIControllerStatus:
    current_limit: int
    minimum: int
    ceiling: int
    active: int
    queued: int
    rolling_latency_seconds: float
    rolling_success_rate: float
    throttles: int
    transient_failures: int
    backoff_until: float
    backoff_stage: int


class AIAdmissionController(AdmissionController):
    """A machine-global AI budget with AIMD and shared task-class admission.

    Reviewer requests deliberately use the same ``ResourceKind.AI`` namespace
    as proof, clarification, and maintenance requests.
    """

    BACKOFF_SECONDS = (30.0, 60.0, 120.0, 240.0, 300.0)

    def __init__(
        self,
        store: SQLiteAdmissionStore,
        *,
        initial: int,
        minimum: int = 1,
        ceiling: int,
        mode: Any = "adaptive",
        budget_policy: Any = "balanced",
        jitter: Callable[[float], float] | None = None,
        success_window: int = 32,
        increase_after_successes: int | None = None,
        increase_cooldown_seconds: float = 0.0,
        throttle_multiplier: float = 0.5,
    ) -> None:
        if not 1 <= minimum <= initial <= ceiling:
            raise ValueError(
                "AI limits must satisfy 1 <= minimum <= initial <= ceiling"
            )
        self.minimum = minimum
        self.ceiling = ceiling
        self.mode = mode
        self.budget_policy = budget_policy
        if increase_after_successes is not None and increase_after_successes < 1:
            raise ValueError("AI increase threshold must be positive")
        if increase_cooldown_seconds < 0:
            raise ValueError("AI increase cooldown must not be negative")
        if not 0 < throttle_multiplier < 1:
            raise ValueError("AI throttle multiplier must be between zero and one")
        self.increase_after_successes = increase_after_successes
        self.increase_cooldown_seconds = increase_cooldown_seconds
        self.throttle_multiplier = throttle_multiplier
        self.success_window = success_window
        self._jitter = jitter or (
            lambda seconds: seconds * random.uniform(0.8, 1.2)  # noqa: S311
        )
        super().__init__(store, ResourceKind.AI, initial)
        persisted = self.store.get_state(ResourceKind.AI, "adaptive")
        if persisted is None:
            self._write_state(
                backoff_until=0.0,
                backoff_stage=0,
                throttles=0,
                transient_failures=0,
                stable_successes=0,
                outcomes=[],
                latencies=[],
                last_increase_at=None,
            )

    @property
    def is_fixed(self) -> bool:
        return _fixed_mode(self.mode)

    def _state(self) -> dict[str, Any]:
        return self.store.get_state(ResourceKind.AI, "adaptive") or {}

    def _write_state(self, **changes: Any) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            state.update(changes)
            return state

        return self.store.update_state(ResourceKind.AI, "adaptive", update)

    def request(
        self,
        owner: str,
        task_class: AITaskClass | str,
        *,
        ttl_seconds: float = 120.0,
        request_id: str | None = None,
    ) -> AdmissionRequest:
        kind = (
            task_class
            if isinstance(task_class, AITaskClass)
            else AITaskClass(_enum_text(task_class))
        )
        return AdmissionRequest(
            resource=ResourceKind.AI,
            priority=TASK_PRIORITIES[kind],
            owner=owner,
            ttl_seconds=ttl_seconds,
            request_id=request_id,
        )

    @property
    def backoff_until(self) -> float:
        return float(self._state().get("backoff_until", 0.0))

    @property
    def in_backoff(self) -> bool:
        return self.store.now() < self.backoff_until

    def try_acquire(self, request: AdmissionRequest) -> AdmissionLease | None:
        if self.in_backoff:
            return None
        return super().try_acquire(request)

    def _increase_threshold(self) -> int:
        if self.increase_after_successes is not None:
            return self.increase_after_successes
        policy = _enum_text(self.budget_policy)
        if policy == "economy":
            return 12
        if policy == "throughput":
            return 4
        return 8

    @property
    def success_threshold(self) -> int:
        """Successful queued turns required before additive growth."""

        return self._increase_threshold()

    def record_success(self, latency_seconds: float, *, queued: bool) -> int:
        if latency_seconds < 0:
            raise ValueError("latency must not be negative")
        now = self.store.now()
        increase_due = False

        def update(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal increase_due
            outcomes = [bool(item) for item in state.get("outcomes", [])]
            latencies = [float(item) for item in state.get("latencies", [])]
            state["outcomes"] = [*outcomes, True][-self.success_window :]
            state["latencies"] = [*latencies, latency_seconds][-self.success_window :]
            stable = int(state.get("stable_successes", 0)) + 1
            backoff_until = float(state.get("backoff_until", 0.0))
            if now >= backoff_until:
                state["backoff_until"] = 0.0
                state["transient_failures"] = 0
            last = state.get("last_increase_at")
            cooldown_ready = last is None or (
                now - float(last) >= self.increase_cooldown_seconds
            )
            increase_due = bool(
                not self.is_fixed
                and now >= backoff_until
                and queued
                and stable >= self._increase_threshold()
                and self.limit < self.ceiling
                and cooldown_ready
            )
            if increase_due:
                stable = 0
                state["last_increase_at"] = now
                state["backoff_stage"] = 0
            state["stable_successes"] = stable
            return state

        self.store.update_state(ResourceKind.AI, "adaptive", update)
        if increase_due and self.limit < self.ceiling:
            self.set_limit(self.limit + 1)
        return self.limit

    def record_throttle(self, *, retry_after: float | int | str | None = None) -> float:
        now = self.store.now()
        delay = 0.0

        def update(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal delay
            stage = min(
                int(state.get("backoff_stage", 0)),
                len(self.BACKOFF_SECONDS) - 1,
            )
            explicit = _retry_after_seconds(retry_after, now)
            delay = (
                explicit
                if explicit is not None
                else self._jitter(self.BACKOFF_SECONDS[stage])
            )
            outcomes = [bool(item) for item in state.get("outcomes", [])]
            state.update(
                outcomes=[*outcomes, False][-self.success_window :],
                stable_successes=0,
                backoff_until=max(float(state.get("backoff_until", 0.0)), now + delay),
                backoff_stage=min(stage + 1, len(self.BACKOFF_SECONDS) - 1),
                throttles=int(state.get("throttles", 0)) + 1,
            )
            return state

        self.store.update_state(ResourceKind.AI, "adaptive", update)
        if not self.is_fixed:
            self.set_limit(
                max(self.minimum, int(self.limit * self.throttle_multiplier))
            )
        return delay

    def record_transient_failure(self) -> float:
        now = self.store.now()
        delay = 0.0
        failures = 0

        def update(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal delay, failures
            failures = int(state.get("transient_failures", 0)) + 1
            stage = min(
                int(state.get("backoff_stage", 0)),
                len(self.BACKOFF_SECONDS) - 1,
            )
            delay = self._jitter(self.BACKOFF_SECONDS[stage])
            outcomes = [bool(item) for item in state.get("outcomes", [])]
            state.update(
                outcomes=[*outcomes, False][-self.success_window :],
                stable_successes=0,
                transient_failures=failures,
                backoff_stage=min(stage + 1, len(self.BACKOFF_SECONDS) - 1),
                backoff_until=max(float(state.get("backoff_until", 0.0)), now + delay),
            )
            return state

        self.store.update_state(ResourceKind.AI, "adaptive", update)
        if not self.is_fixed and failures >= 2:
            self.set_limit(max(self.minimum, self.limit - 1))
        return delay

    def set_bounds(self, *, minimum: int, ceiling: int) -> int:
        if minimum < 1 or ceiling < minimum:
            raise ValueError("invalid AI concurrency bounds")
        self.minimum = minimum
        self.ceiling = ceiling
        bounded = min(ceiling, max(minimum, self.limit))
        self.set_limit(bounded)
        return bounded

    def reset_adaptive_history(self) -> None:
        """Clear AIMD evidence without revoking any in-flight AI lease."""

        self.store.set_state(
            ResourceKind.AI,
            "adaptive",
            {
                "backoff_until": 0.0,
                "backoff_stage": 0,
                "throttles": 0,
                "transient_failures": 0,
                "stable_successes": 0,
                "outcomes": [],
                "latencies": [],
                "last_increase_at": None,
            },
        )

    def status(self) -> AIControllerStatus:
        snapshot = self.snapshot()
        state = self._state()
        latencies = [float(item) for item in state.get("latencies", [])]
        outcomes = [bool(item) for item in state.get("outcomes", [])]
        latency = sum(latencies) / len(latencies) if latencies else 0.0
        success_rate = sum(outcomes) / len(outcomes) if outcomes else 1.0
        return AIControllerStatus(
            current_limit=snapshot.limit,
            minimum=self.minimum,
            ceiling=self.ceiling,
            active=snapshot.active,
            queued=snapshot.queued,
            rolling_latency_seconds=latency,
            rolling_success_rate=success_rate,
            throttles=int(state.get("throttles", 0)),
            transient_failures=int(state.get("transient_failures", 0)),
            backoff_until=float(state.get("backoff_until", 0.0)),
            backoff_stage=int(state.get("backoff_stage", 0)),
        )


def retry_after_http_date(seconds_from_now: float, *, now: float) -> str:
    """Small deterministic helper useful to adapters and tests."""

    return datetime.fromtimestamp(now + seconds_from_now, tz=UTC).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )
