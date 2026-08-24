"""Platform-aware memory-pressure classification and hysteresis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .config import PressureState

MIB = 1024**2


class MemoryPressureSource(StrEnum):
    MACOS_NATIVE = "macos_native"
    MACOS_FALLBACK = "macos_fallback"
    LINUX_PSI = "linux_psi"
    LINUX_FALLBACK = "linux_fallback"
    PORTABLE_FALLBACK = "portable_fallback"


@dataclass(frozen=True)
class MemoryPressurePolicy:
    """Central, injectable defaults for supported-platform classification."""

    macos_yellow_ratio: float = 0.20
    macos_red_ratio: float = 0.12
    macos_emergency_ratio: float = 0.06
    portable_yellow_ratio: float = 0.30
    portable_red_ratio: float = 0.15
    portable_emergency_ratio: float = 0.08
    swap_out_memory_fraction_per_second: float = 0.001
    swap_out_minimum_bytes_per_second: float = 16 * MIB
    swap_out_maximum_bytes_per_second: float = 128 * MIB
    worsening_samples: int = 2
    recovery_samples: int = 4

    def __post_init__(self) -> None:
        ratios = (
            self.macos_emergency_ratio,
            self.macos_red_ratio,
            self.macos_yellow_ratio,
            self.portable_emergency_ratio,
            self.portable_red_ratio,
            self.portable_yellow_ratio,
        )
        if not all(0.0 < value < 1.0 for value in ratios):
            raise ValueError("memory-pressure ratios must be between zero and one")
        if not (
            self.macos_emergency_ratio
            < self.macos_red_ratio
            < self.macos_yellow_ratio
        ):
            raise ValueError("invalid macOS memory-pressure thresholds")
        if not (
            self.portable_emergency_ratio
            < self.portable_red_ratio
            < self.portable_yellow_ratio
        ):
            raise ValueError("invalid portable memory-pressure thresholds")
        if (
            self.swap_out_memory_fraction_per_second <= 0
            or self.swap_out_minimum_bytes_per_second <= 0
            or self.swap_out_maximum_bytes_per_second
            < self.swap_out_minimum_bytes_per_second
            or self.worsening_samples < 1
            or self.recovery_samples < 1
        ):
            raise ValueError("invalid memory-pressure hysteresis policy")

    def swap_out_threshold(self, total_memory_bytes: int) -> float:
        scaled = total_memory_bytes * self.swap_out_memory_fraction_per_second
        return max(
            self.swap_out_minimum_bytes_per_second,
            min(self.swap_out_maximum_bytes_per_second, scaled),
        )


@dataclass(frozen=True)
class MemoryPressureDecision:
    pressure: PressureState
    candidate: PressureState
    source: MemoryPressureSource
    active_swap_out: bool
    swap_out_threshold_bytes_per_second: float
    reasons: tuple[str, ...]


_SEVERITY = {
    PressureState.GREEN: 0,
    PressureState.YELLOW: 1,
    PressureState.RED: 2,
    PressureState.EMERGENCY: 3,
}


def _more_severe(left: PressureState, right: PressureState) -> PressureState:
    return left if _SEVERITY[left] >= _SEVERITY[right] else right


class MemoryPressureClassifier:
    """Stateful classifier with faster escalation than recovery."""

    def __init__(
        self,
        *,
        os_name: str,
        policy: MemoryPressurePolicy | None = None,
    ) -> None:
        self.os_name = os_name
        self.policy = policy or MemoryPressurePolicy()
        self._pressure: PressureState | None = None
        self._worsening_samples = 0
        self._recovery_samples = 0
        self._high_swap_out_samples = 0

    def _available_candidate(self, ratio: float) -> PressureState:
        if self.os_name == "Darwin":
            emergency = self.policy.macos_emergency_ratio
            red = self.policy.macos_red_ratio
            yellow = self.policy.macos_yellow_ratio
        else:
            emergency = self.policy.portable_emergency_ratio
            red = self.policy.portable_red_ratio
            yellow = self.policy.portable_yellow_ratio
        if ratio <= emergency:
            return PressureState.EMERGENCY
        if ratio <= red:
            return PressureState.RED
        if ratio <= yellow:
            return PressureState.YELLOW
        return PressureState.GREEN

    def _source(
        self,
        *,
        native_memory_pressure_level: int | None,
        memory_psi_available: bool,
    ) -> MemoryPressureSource:
        if self.os_name == "Darwin":
            return (
                MemoryPressureSource.MACOS_NATIVE
                if native_memory_pressure_level is not None
                else MemoryPressureSource.MACOS_FALLBACK
            )
        if self.os_name == "Linux":
            return (
                MemoryPressureSource.LINUX_PSI
                if memory_psi_available
                else MemoryPressureSource.LINUX_FALLBACK
            )
        return MemoryPressureSource.PORTABLE_FALLBACK

    def _stabilize(
        self, candidate: PressureState, *, immediate: bool
    ) -> PressureState:
        if self._pressure is None:
            self._pressure = candidate
            return candidate
        current_severity = _SEVERITY[self._pressure]
        candidate_severity = _SEVERITY[candidate]
        if candidate_severity > current_severity:
            self._recovery_samples = 0
            self._worsening_samples += 1
            if immediate or self._worsening_samples >= self.policy.worsening_samples:
                self._pressure = candidate
                self._worsening_samples = 0
        elif candidate_severity < current_severity:
            self._worsening_samples = 0
            self._recovery_samples += 1
            if self._recovery_samples >= self.policy.recovery_samples:
                self._pressure = candidate
                self._recovery_samples = 0
        else:
            self._worsening_samples = 0
            self._recovery_samples = 0
        return self._pressure

    def classify(
        self,
        *,
        available_memory_ratio: float,
        total_memory_bytes: int,
        swap_out_rate_bytes_per_second: float | None,
        native_memory_pressure_level: int | None,
        memory_psi_available: bool,
    ) -> MemoryPressureDecision:
        ratio = max(0.0, min(1.0, available_memory_ratio))
        candidate = self._available_candidate(ratio)
        reasons = [f"available memory {ratio:.1%}"]
        threshold = self.policy.swap_out_threshold(total_memory_bytes)
        active_swap_out = bool(
            swap_out_rate_bytes_per_second is not None
            and swap_out_rate_bytes_per_second >= threshold
        )
        if active_swap_out:
            self._high_swap_out_samples += 1
            paging_floor = PressureState.YELLOW
            if self.os_name == "Linux" and self._high_swap_out_samples >= 2:
                paging_floor = PressureState.RED
            candidate = _more_severe(candidate, paging_floor)
            reasons.append(
                "active swap-out "
                f"{swap_out_rate_bytes_per_second / MIB:.1f} MiB/s"
            )
        else:
            self._high_swap_out_samples = 0

        if self.os_name == "Darwin" and native_memory_pressure_level is not None:
            if native_memory_pressure_level in {1, 2}:
                candidate = _more_severe(candidate, PressureState.YELLOW)
                reasons.append(
                    f"macOS native warning/urgent ({native_memory_pressure_level})"
                )
            elif native_memory_pressure_level == 3:
                candidate = _more_severe(candidate, PressureState.RED)
                reasons.append("macOS native critical (3)")
            else:
                reasons.append("macOS native normal (0)")

        immediate = candidate == PressureState.EMERGENCY or (
            self.os_name == "Darwin" and native_memory_pressure_level == 3
        )
        pressure = self._stabilize(candidate, immediate=immediate)
        if pressure != candidate:
            reasons.append(f"hysteresis retained {pressure.value}")
        return MemoryPressureDecision(
            pressure=pressure,
            candidate=candidate,
            source=self._source(
                native_memory_pressure_level=native_memory_pressure_level,
                memory_psi_available=memory_psi_available,
            ),
            active_swap_out=active_swap_out,
            swap_out_threshold_bytes_per_second=threshold,
            reasons=tuple(reasons),
        )
