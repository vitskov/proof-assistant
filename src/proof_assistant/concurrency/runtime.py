"""Composition root for machine-scoped concurrency controllers."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..cache import CacheLayout
from .admission import ResourceKind, SQLiteAdmissionStore
from .ai import AIAdmissionController
from .build import BuildAdmissionController
from .calibration import (
    CalibrationProfile,
    CalibrationStore,
    project_calibration_key,
)
from .config import (
    AutoValue,
    ConcurrencyConfigPatch,
    MachineConfigStore,
    ResolvedConcurrencyConfig,
    resolve_concurrency_config,
)
from .hardware import (
    AutoTunedLimits,
    HardwareResources,
    derive_auto_limits,
    detect_hardware,
)
from .lean import LeanAdmissionController
from .scheduler import DependencyScheduler, DuplicateEscalationPolicy
from .telemetry import TelemetrySnapshot


@dataclass(frozen=True)
class ConcurrencyRuntimeSpec:
    """Frozen, picklable instructions passed to local or distributed workers."""

    cache_home: str | None = None
    machine_config_path: str | None = None
    cli_patch: ConcurrencyConfigPatch | None = None
    project_path: str | None = None

    def resolve(
        self, *, environ: Mapping[str, str] | None = None
    ) -> ResolvedConcurrencyConfig:
        machine = MachineConfigStore(
            Path(self.machine_config_path) if self.machine_config_path else None
        ).load()
        return resolve_concurrency_config(
            machine=machine,
            environ=environ,
            cli=self.cli_patch,
        )

    def create(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        resources: HardwareResources | None = None,
        clock: Any = time.time,
        calibrated_repl_p95_gib: float | None = None,
        jitter: Callable[[float], float] | None = None,
    ) -> ConcurrencyRuntime:
        layout = CacheLayout.discover(self.cache_home)
        resolved = self.resolve(environ=environ)
        resources = resources or detect_hardware()
        calibration_profile: CalibrationProfile | None = None
        calibration_store = CalibrationStore(layout.root)
        if (
            calibrated_repl_p95_gib is None
            and self.project_path is not None
            and resolved.config.lean.memory_calibration
        ):
            key = project_calibration_key(
                Path(self.project_path),
                resources=resources,
                codex_plan=resolved.config.ai.plan.value,
                codex_model="",
            )
            calibration_profile = calibration_store.load_fresh(key)
            if calibration_profile is not None and calibration_profile.repl is not None:
                calibrated_repl_p95_gib = calibration_profile.repl.p95_working_rss_gib
        if resolved.config.lean.memory_calibration:
            conservative_p95 = calibration_store.conservative_repl_p95_gib(
                os_name=resources.os_name,
                architecture=resources.architecture,
                usable_logical_cpus=resources.usable_logical_cpus,
                total_memory_bytes=resources.total_memory_bytes,
                fallback_budget_gib=(resolved.config.lean.fallback_memory_per_repl_gib),
                safety_multiplier=resolved.config.lean.p95_safety_multiplier,
            )
            if conservative_p95 is not None:
                calibrated_repl_p95_gib = max(
                    calibrated_repl_p95_gib or 0.0,
                    conservative_p95,
                )
        return ConcurrencyRuntime.from_resolved(
            resolved,
            layout.root,
            resources=resources,
            clock=clock,
            calibrated_repl_p95_gib=calibrated_repl_p95_gib,
            jitter=jitter,
            calibration_profile=calibration_profile,
        )


def _resolved_marker(
    resolved: ResolvedConcurrencyConfig,
    tuned: AutoTunedLimits,
    resources: HardwareResources,
    resource: ResourceKind,
) -> dict[str, Any]:
    common: dict[str, Any] = {
        "schema": 1,
        "mode": resolved.config.mode,
        "resource": resource,
    }
    if resource == ResourceKind.AI:
        return {
            **common,
            "config": asdict(resolved.config.ai),
            "effective_initial": tuned.ai_initial,
            "effective_ceiling": tuned.ai_ceiling,
        }
    allocation = {
        "os": resources.os_name,
        "architecture": resources.architecture,
        "logical_cpus": resources.usable_logical_cpus,
        "physical_cpus": resources.usable_physical_cpus,
        "memory_bytes": resources.total_memory_bytes,
    }
    if resource == ResourceKind.LEAN:
        return {
            **common,
            "resource_profile": resolved.config.resource_profile,
            "config": asdict(resolved.config.lean),
            "effective_initial": tuned.lean_pool,
            "effective_repl_memory_budget_gib": tuned.repl_memory_budget_gib,
            "allocation": allocation,
        }
    return {
        **common,
        "resource_profile": resolved.config.resource_profile,
        "config": asdict(resolved.config.build),
        "effective_initial": tuned.build_concurrency,
        "allocation": allocation,
    }


def _sync_resolved_limits(
    store: SQLiteAdmissionStore,
    resolved: ResolvedConcurrencyConfig,
    tuned: AutoTunedLimits,
    resources: HardwareResources,
) -> None:
    """Publish each resource independently so one policy cannot reset another."""

    limits = {
        ResourceKind.AI: tuned.ai_initial,
        ResourceKind.LEAN: tuned.lean_pool,
        ResourceKind.BUILD: tuned.build_concurrency,
    }
    for resource, limit in limits.items():
        store.sync_limits_if_state_changed(
            state_resource=resource,
            state_key="resolved_runtime",
            payload=_resolved_marker(resolved, tuned, resources, resource),
            limits={resource: limit},
        )


@dataclass
class ConcurrencyRuntime:
    """One machine's controllers, all backed by one durable admission database."""

    resolved: ResolvedConcurrencyConfig
    resources: HardwareResources
    auto_limits: AutoTunedLimits
    store: SQLiteAdmissionStore
    ai: AIAdmissionController
    lean: LeanAdmissionController
    build: BuildAdmissionController
    scheduler: DependencyScheduler
    duplicates: DuplicateEscalationPolicy
    calibration_profile: CalibrationProfile | None = None
    cache_root: Path | None = None

    @classmethod
    def from_resolved(
        cls,
        resolved: ResolvedConcurrencyConfig,
        cache_root: str | Path,
        *,
        resources: HardwareResources | None = None,
        clock: Any = time.time,
        calibrated_repl_p95_gib: float | None = None,
        jitter: Callable[[float], float] | None = None,
        calibration_profile: CalibrationProfile | None = None,
    ) -> ConcurrencyRuntime:
        resources = resources or detect_hardware()
        tuned = derive_auto_limits(
            resources,
            resolved.config,
            calibrated_repl_p95_gib=calibrated_repl_p95_gib,
        )
        root = Path(cache_root).expanduser().resolve() / "concurrency"
        store = SQLiteAdmissionStore(root / "admission.sqlite3", clock=clock)
        config = resolved.config
        ai = AIAdmissionController(
            store,
            initial=tuned.ai_initial,
            minimum=config.ai.minimum,
            ceiling=tuned.ai_ceiling,
            mode=config.mode,
            budget_policy=config.ai.budget_policy,
            jitter=jitter,
            increase_after_successes=(
                None
                if config.ai.increase_after_successes == AutoValue.AUTO
                else int(config.ai.increase_after_successes)
            ),
            increase_cooldown_seconds=config.ai.increase_cooldown_seconds,
            throttle_multiplier=config.ai.throttle_multiplier,
        )
        lean_maximum = (
            int(config.lean.max_pool)
            if config.lean.max_pool != AutoValue.AUTO
            else max(tuned.lean_pool, config.lean.initial_auto_cap)
        )
        lean = LeanAdmissionController(
            store,
            initial=tuned.lean_pool,
            minimum=config.lean.min_pool,
            maximum=lean_maximum,
            mode=config.mode,
        )
        build = BuildAdmissionController(
            store,
            initial=tuned.build_concurrency,
            minimum=config.build.min_concurrent,
            maximum=config.build.hard_max,
            hard_maximum=config.build.hard_max,
            mode=config.mode,
        )
        _sync_resolved_limits(store, resolved, tuned, resources)
        return cls(
            resolved=resolved,
            resources=resources,
            auto_limits=tuned,
            store=store,
            ai=ai,
            lean=lean,
            build=build,
            scheduler=DependencyScheduler(),
            duplicates=DuplicateEscalationPolicy(
                budget_policy=config.ai.budget_policy,
                maximum_agents=config.scheduler.agents_per_target_max,
                enabled=config.scheduler.duplicate_agent_escalation,
            ),
            calibration_profile=calibration_profile,
            cache_root=Path(cache_root).expanduser().resolve(),
        )

    def apply_resolved(
        self,
        resolved: ResolvedConcurrencyConfig,
        *,
        calibrated_repl_p95_gib: float | None = None,
    ) -> None:
        """Apply validated live settings; active leases are never cancelled."""

        if (
            calibrated_repl_p95_gib is None
            and self.calibration_profile is not None
            and self.calibration_profile.repl is not None
            and resolved.config.lean.memory_calibration
        ):
            calibrated_repl_p95_gib = self.calibration_profile.repl.p95_working_rss_gib
        if resolved.config.lean.memory_calibration and self.cache_root is not None:
            conservative_p95 = CalibrationStore(
                self.cache_root
            ).conservative_repl_p95_gib(
                os_name=self.resources.os_name,
                architecture=self.resources.architecture,
                usable_logical_cpus=self.resources.usable_logical_cpus,
                total_memory_bytes=self.resources.total_memory_bytes,
                fallback_budget_gib=(resolved.config.lean.fallback_memory_per_repl_gib),
                safety_multiplier=resolved.config.lean.p95_safety_multiplier,
            )
            if conservative_p95 is not None:
                calibrated_repl_p95_gib = max(
                    calibrated_repl_p95_gib or 0.0,
                    conservative_p95,
                )

        tuned = derive_auto_limits(
            self.resources,
            resolved.config,
            calibrated_repl_p95_gib=calibrated_repl_p95_gib,
        )
        config = resolved.config
        self.ai.mode = config.mode
        self.ai.budget_policy = config.ai.budget_policy
        self.ai.increase_after_successes = (
            None
            if config.ai.increase_after_successes == AutoValue.AUTO
            else int(config.ai.increase_after_successes)
        )
        self.ai.increase_cooldown_seconds = config.ai.increase_cooldown_seconds
        self.ai.throttle_multiplier = config.ai.throttle_multiplier
        self.ai.minimum = config.ai.minimum
        self.ai.ceiling = tuned.ai_ceiling
        self.lean.mode = config.mode
        self.lean.minimum = config.lean.min_pool
        self.lean.maximum = (
            int(config.lean.max_pool)
            if config.lean.max_pool != AutoValue.AUTO
            else max(tuned.lean_pool, config.lean.initial_auto_cap)
        )
        self.build.mode = config.mode
        self.build.minimum = config.build.min_concurrent
        self.build.maximum = config.build.hard_max
        self.build.hard_maximum = config.build.hard_max
        _sync_resolved_limits(self.store, resolved, tuned, self.resources)

        self.duplicates = DuplicateEscalationPolicy(
            budget_policy=config.ai.budget_policy,
            maximum_agents=config.scheduler.agents_per_target_max,
            enabled=config.scheduler.duplicate_agent_escalation,
        )
        self.resolved = resolved
        self.auto_limits = tuned

    def observe_telemetry(
        self,
        snapshot: TelemetrySnapshot,
        *,
        lean_throughput_improved: bool = True,
        build_throughput_improved: bool = True,
    ) -> None:
        io_pressure = bool(
            snapshot.disk_iowait_percent is not None
            and snapshot.disk_iowait_percent >= 20.0
        )
        if snapshot.io_psi is not None:
            io_pressure = io_pressure or bool(
                snapshot.io_psi.some_avg10 is not None
                and snapshot.io_psi.some_avg10 >= 10.0
            )
        self.lean.observe(
            pressure=snapshot.pressure,
            queue_depth=snapshot.queues.lean,
            cpu_percent=snapshot.cpu_percent,
            throughput_improved=lean_throughput_improved,
        )
        self.build.observe(
            pressure=snapshot.pressure,
            queue_depth=snapshot.queues.build,
            io_pressure=io_pressure,
            throughput_improved=build_throughput_improved,
        )

    def reset_adaptive_history(self) -> None:
        """Reset machine controller evidence while preserving every lease.

        Adaptive controllers return to the current resolved policy's initial
        limits. Fixed/manual mode retains its explicit effective limits.
        """

        self.ai.reset_adaptive_history()
        self.lean.reset_adaptive_history()
        self.build.reset_adaptive_history()
        if not self.ai.is_fixed:
            self.ai.set_limit(self.auto_limits.ai_initial)
            self.lean.set_limit(self.auto_limits.lean_pool)
            self.build.set_limit(self.auto_limits.build_concurrency)

    def provenance(self) -> dict[str, Any]:
        """Configured and effective values suitable for run metadata."""

        ai = self.ai.status()
        lean = self.lean.status()
        build = self.build.status()
        return {
            "machine_revision": self.resolved.machine_revision,
            "mode": self.resolved.config.mode.value,
            "configured": {
                "mode": self.resolved.config.mode.value,
                "resource_profile": self.resolved.config.resource_profile.value,
                "telemetry_enabled": self.resolved.config.telemetry_enabled,
                "codex_plan": self.resolved.config.ai.plan.value,
                "budget_policy": self.resolved.config.ai.budget_policy.value,
                "ai_initial": str(self.resolved.config.ai.initial),
                "ai_hard_max": str(self.resolved.config.ai.hard_max),
                "ai_minimum": self.resolved.config.ai.minimum,
                "ai_increase_after_successes": str(
                    self.resolved.config.ai.increase_after_successes
                ),
                "lean_pool": str(self.resolved.config.lean.pool_size),
                "lean_max": str(self.resolved.config.lean.max_pool),
                "lean_minimum": self.resolved.config.lean.min_pool,
                "build_max": str(self.resolved.config.build.max_concurrent),
                "build_hard_max": self.resolved.config.build.hard_max,
                "agents_per_target_initial": (
                    self.resolved.config.scheduler.agents_per_target_initial
                ),
                "agents_per_target_max": (
                    self.resolved.config.scheduler.agents_per_target_max
                ),
                "dependency_priority": (
                    self.resolved.config.scheduler.dependency_priority
                ),
                "duplicate_agent_escalation": (
                    self.resolved.config.scheduler.duplicate_agent_escalation
                ),
            },
            "effective": {
                "ai_limit": ai.current_limit,
                "ai_ceiling": ai.ceiling,
                "lean_pool": lean.current_limit,
                "lean_max": lean.maximum,
                "build_limit": build.current_limit,
                "build_ceiling": build.hard_maximum,
                "agents_per_target_current": (
                    self.resolved.config.scheduler.agents_per_target_initial
                ),
                "agents_per_target_max": (
                    self.resolved.config.scheduler.agents_per_target_max
                ),
            },
            "active": {
                "ai": ai.active,
                "lean": lean.active,
                "build": build.active,
            },
            "ai_adaptation": {
                "rolling_latency_seconds": ai.rolling_latency_seconds,
                "rolling_success_rate": ai.rolling_success_rate,
                "throttles": ai.throttles,
                "transient_failures": ai.transient_failures,
                "successes_required_for_growth": self.ai.success_threshold,
                "backoff_until": ai.backoff_until,
                "backoff_stage": ai.backoff_stage,
            },
            "queued": {
                "ai": ai.queued,
                "lean": lean.queued,
                "build": build.queued,
            },
            "pressure": {
                "lean": lean.pressure,
                "build": build.pressure,
            },
            "lean_memory_calibration": (
                {
                    "profile_id": self.calibration_profile.key.identifier,
                    "measured_at": self.calibration_profile.measured_at,
                    "p95_working_rss_gib": (
                        self.calibration_profile.repl.p95_working_rss_gib
                    ),
                    "memory_budget_gib": self.calibration_profile.repl.budget_gib,
                }
                if self.calibration_profile is not None
                and self.calibration_profile.repl is not None
                and self.resolved.config.lean.memory_calibration
                else None
            ),
            "reasons": list(self.auto_limits.reasons),
        }
