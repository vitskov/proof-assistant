"""Portable hardware allocation detection and conservative auto-tuning."""

from __future__ import annotations

import math
import os
import platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from .config import (
    AutoValue,
    CodexPlan,
    ConcurrencyConfig,
    ResourceProfile,
)

GIB = 1024**3


@dataclass(frozen=True)
class HardwareResources:
    os_name: str
    architecture: str
    host_logical_cpus: int
    host_physical_cpus: int
    usable_logical_cpus: int
    usable_physical_cpus: int
    host_total_memory_bytes: int
    total_memory_bytes: int
    available_memory_bytes: int
    interactive_detected: bool
    affinity_cpus: int | None = None
    cgroup_cpus: int | None = None
    slurm_cpus: int | None = None
    cgroup_memory_bytes: int | None = None
    slurm_memory_bytes: int | None = None

    @property
    def total_memory_gib(self) -> float:
        return self.total_memory_bytes / GIB

    @property
    def available_memory_gib(self) -> float:
        return self.available_memory_bytes / GIB


@dataclass(frozen=True)
class AutoTunedLimits:
    resource_profile: ResourceProfile
    ai_initial: int
    ai_ceiling: int
    lean_pool: int
    build_concurrency: int
    memory_reserve_gib: float
    repl_memory_budget_gib: float
    lean_cpu_cap: int
    lean_memory_cap: int
    reasons: tuple[str, ...]


def _read_optional(path: Path, read_text: Callable[[Path], str]) -> str | None:
    try:
        return read_text(path).strip()
    except (OSError, UnicodeError):
        return None


def _parse_cpu_list(raw: str | None) -> int | None:
    if not raw:
        return None
    values: set[int] = set()
    try:
        for part in raw.split(","):
            bounds = part.strip().split("-", 1)
            start = int(bounds[0])
            end = int(bounds[-1])
            if start < 0 or end < start:
                return None
            values.update(range(start, end + 1))
    except ValueError:
        return None
    return len(values) or None


def _positive_int(raw: str | None) -> int | None:
    try:
        value = int(raw) if raw is not None else 0
    except ValueError:
        return None
    return value if value > 0 else None


def _cgroup_cpu_limit(
    root: Path, read_text: Callable[[Path], str]
) -> tuple[int | None, int | None]:
    quota_count: int | None = None
    cpu_max = _read_optional(root / "cpu.max", read_text)
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) >= 2 and parts[0] != "max":
            quota = _positive_int(parts[0])
            period = _positive_int(parts[1])
            if quota is not None and period is not None:
                quota_count = max(1, math.floor(quota / period))
    if quota_count is None:
        quota = _positive_int(
            _read_optional(root / "cpu" / "cpu.cfs_quota_us", read_text)
        )
        period = _positive_int(
            _read_optional(root / "cpu" / "cpu.cfs_period_us", read_text)
        )
        if quota is not None and period is not None:
            quota_count = max(1, math.floor(quota / period))
    cpuset = _parse_cpu_list(
        _read_optional(root / "cpuset.cpus.effective", read_text)
        or _read_optional(root / "cpuset.cpus", read_text)
        or _read_optional(root / "cpuset" / "cpuset.cpus", read_text)
    )
    return quota_count, cpuset


def _cgroup_memory(
    root: Path, read_text: Callable[[Path], str]
) -> tuple[int | None, int | None]:
    maximum = _read_optional(root / "memory.max", read_text)
    current = _positive_int(_read_optional(root / "memory.current", read_text))
    limit = None if maximum == "max" else _positive_int(maximum)
    if limit is None:
        limit = _positive_int(
            _read_optional(root / "memory" / "memory.limit_in_bytes", read_text)
        )
        current = _positive_int(
            _read_optional(root / "memory" / "memory.usage_in_bytes", read_text)
        )
    return limit, current


def _slurm_cpu_limit(environ: Mapping[str, str]) -> int | None:
    per_task = _positive_int(environ.get("SLURM_CPUS_PER_TASK"))
    if per_task is not None:
        return per_task
    on_node = _positive_int(environ.get("SLURM_CPUS_ON_NODE"))
    if on_node is not None:
        return on_node
    raw = environ.get("SLURM_JOB_CPUS_PER_NODE", "").split("(", 1)[0]
    return _positive_int(raw)


def _slurm_memory_limit(environ: Mapping[str, str], usable_cpus: int) -> int | None:
    per_node = _positive_int(environ.get("SLURM_MEM_PER_NODE"))
    if per_node is not None:
        return per_node * 1024**2
    per_cpu = _positive_int(environ.get("SLURM_MEM_PER_CPU"))
    return per_cpu * usable_cpus * 1024**2 if per_cpu is not None else None


def _interactive_default(os_name: str, environ: Mapping[str, str]) -> bool:
    if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
        return False
    if os_name == "Darwin":
        return True
    return any(
        environ.get(name)
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP")
    )


def detect_hardware(
    *,
    psutil_module: Any = psutil,
    environ: Mapping[str, str] | None = None,
    os_name: str | None = None,
    architecture: str | None = None,
    affinity: set[int] | None = None,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    read_text: Callable[[Path], str] | None = None,
) -> HardwareResources:
    """Detect the allocation visible to this process, not the entire host."""

    source = os.environ if environ is None else environ
    system_name = os_name or platform.system()
    machine = architecture or platform.machine()
    reader = read_text or (lambda path: path.read_text(encoding="utf-8"))
    host_logical = int(psutil_module.cpu_count(logical=True) or os.cpu_count() or 1)
    host_physical = int(psutil_module.cpu_count(logical=False) or host_logical)

    if affinity is None and hasattr(os, "sched_getaffinity"):
        try:
            affinity = set(os.sched_getaffinity(0))
        except OSError:
            affinity = None
    affinity_count = len(affinity) if affinity else None
    cgroup_quota: int | None = None
    cgroup_cpuset: int | None = None
    cgroup_memory: int | None = None
    cgroup_current: int | None = None
    if system_name == "Linux":
        cgroup_quota, cgroup_cpuset = _cgroup_cpu_limit(cgroup_root, reader)
        cgroup_memory, cgroup_current = _cgroup_memory(cgroup_root, reader)
    cgroup_cpu_values = [
        item for item in (cgroup_quota, cgroup_cpuset) if item is not None
    ]
    cgroup_cpus = min(cgroup_cpu_values) if cgroup_cpu_values else None
    slurm_cpus = _slurm_cpu_limit(source)
    cpu_limits = [host_logical]
    cpu_limits.extend(
        value
        for value in (affinity_count, cgroup_cpus, slurm_cpus)
        if value is not None
    )
    usable_logical = max(1, min(cpu_limits))
    # CPU sets rarely expose physical topology. Scaling the host physical count
    # by the allocated logical fraction is conservative under SMT constraints.
    usable_physical = max(
        1,
        min(
            usable_logical,
            math.floor(host_physical * usable_logical / max(1, host_logical)),
        ),
    )

    memory = psutil_module.virtual_memory()
    host_total = int(memory.total)
    host_available = int(memory.available)
    slurm_memory = _slurm_memory_limit(source, usable_logical)
    memory_limits = [host_total]
    memory_limits.extend(
        value
        for value in (cgroup_memory, slurm_memory)
        if value is not None and value < (1 << 60)
    )
    total_memory = max(1, min(memory_limits))
    allocation_available = total_memory
    if cgroup_memory is not None and cgroup_current is not None:
        allocation_available = min(
            allocation_available, max(0, cgroup_memory - cgroup_current)
        )
    available_memory = max(0, min(host_available, allocation_available, total_memory))
    return HardwareResources(
        os_name=system_name,
        architecture=machine,
        host_logical_cpus=host_logical,
        host_physical_cpus=host_physical,
        usable_logical_cpus=usable_logical,
        usable_physical_cpus=usable_physical,
        host_total_memory_bytes=host_total,
        total_memory_bytes=total_memory,
        available_memory_bytes=available_memory,
        interactive_detected=_interactive_default(system_name, source),
        affinity_cpus=affinity_count,
        cgroup_cpus=cgroup_cpus,
        slurm_cpus=slurm_cpus,
        cgroup_memory_bytes=cgroup_memory,
        slurm_memory_bytes=slurm_memory,
    )


_AI_PROFILES = {
    CodexPlan.PLUS: (2, 6),
    CodexPlan.PRO_5X: (4, 12),
    CodexPlan.PRO_20X: (8, 24),
    CodexPlan.UNKNOWN: (4, 8),
    # API limits ultimately come from RPM/TPM. Use the unknown conservative
    # policy until those stable limits are supplied explicitly.
    CodexPlan.API: (4, 8),
}


def derive_auto_limits(
    resources: HardwareResources,
    config: ConcurrencyConfig,
    *,
    calibrated_repl_p95_gib: float | None = None,
) -> AutoTunedLimits:
    """Apply the guide's exact initial AI, Lean, and build formulas."""

    if config.resource_profile == ResourceProfile.AUTO:
        profile = (
            ResourceProfile.INTERACTIVE
            if resources.interactive_detected
            else ResourceProfile.SERVER
        )
    else:
        profile = config.resource_profile
    interactive = profile == ResourceProfile.INTERACTIVE
    reasons: list[str] = [
        f"resource profile: {profile.value}",
        f"usable CPUs: {resources.usable_physical_cpus} physical / "
        f"{resources.usable_logical_cpus} logical",
    ]

    default_ai, default_ceiling = _AI_PROFILES[config.ai.plan]
    ai_initial = (
        default_ai if config.ai.initial == AutoValue.AUTO else int(config.ai.initial)
    )
    ai_ceiling = (
        default_ceiling
        if config.ai.hard_max == AutoValue.AUTO
        else int(config.ai.hard_max)
    )
    ai_ceiling = max(config.ai.minimum, ai_ceiling)
    ai_initial = max(config.ai.minimum, min(ai_initial, ai_ceiling))
    reasons.append(
        f"AI plan policy {config.ai.plan.value}: initial {ai_initial}, "
        f"ceiling {ai_ceiling}"
    )

    total_gib = resources.total_memory_gib
    available_gib = resources.available_memory_gib
    reserve_gib = (
        max(6.0, 0.30 * total_gib) if interactive else max(4.0, 0.15 * total_gib)
    )
    cpu_fraction = 0.60 if interactive else 0.90
    lean_cpu_cap = max(1, math.floor(resources.usable_physical_cpus * cpu_fraction))
    pa_memory = max(1.0, min(available_gib, total_gib - reserve_gib))
    repl_memory = 0.65 * pa_memory
    if calibrated_repl_p95_gib is None:
        repl_budget = config.lean.fallback_memory_per_repl_gib
        reasons.append(
            f"Lean memory uses {repl_budget:.2f} GiB uncalibrated fallback per REPL"
        )
    else:
        repl_budget = max(
            2.0,
            config.lean.fallback_memory_per_repl_gib,
            config.lean.p95_safety_multiplier * calibrated_repl_p95_gib,
        )
        reasons.append(
            f"Lean memory uses calibrated p95 budget {repl_budget:.2f} GiB per REPL"
        )
    lean_memory_cap = max(1, math.floor(repl_memory / repl_budget))
    automatic_lean = max(
        config.lean.min_pool,
        min(lean_cpu_cap, lean_memory_cap, config.lean.initial_auto_cap),
    )
    if isinstance(config.lean.max_pool, int):
        automatic_lean = min(automatic_lean, config.lean.max_pool)
    lean_pool = (
        automatic_lean
        if config.lean.pool_size == AutoValue.AUTO
        else int(config.lean.pool_size)
    )
    reasons.append(
        f"Lean cap is min(CPU {lean_cpu_cap}, RAM {lean_memory_cap}, "
        f"initial {config.lean.initial_auto_cap})"
    )

    physical = resources.usable_physical_cpus
    if physical <= 16 or total_gib <= 32:
        automatic_builds = 1
    elif physical <= 32 or total_gib <= 64:
        automatic_builds = 2
    elif physical <= 64 or total_gib <= 128:
        automatic_builds = 3
    else:
        automatic_builds = 4
    automatic_builds = max(
        config.build.min_concurrent,
        min(automatic_builds, config.build.hard_max),
    )
    build_concurrency = (
        automatic_builds
        if config.build.max_concurrent == AutoValue.AUTO
        else int(config.build.max_concurrent)
    )
    build_concurrency = min(build_concurrency, config.build.hard_max)
    reasons.append(
        f"full-build policy selected {build_concurrency} with hard ceiling "
        f"{config.build.hard_max}"
    )
    return AutoTunedLimits(
        resource_profile=profile,
        ai_initial=ai_initial,
        ai_ceiling=ai_ceiling,
        lean_pool=lean_pool,
        build_concurrency=build_concurrency,
        memory_reserve_gib=reserve_gib,
        repl_memory_budget_gib=repl_budget,
        lean_cpu_cap=lean_cpu_cap,
        lean_memory_cap=lean_memory_cap,
        reasons=tuple(reasons),
    )
