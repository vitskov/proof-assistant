"""Public concurrency configuration, resource, and status contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .admission import (
    AdmissionController,
    AdmissionLease,
    AdmissionRequest,
    AdmissionSnapshot,
    ResourceKind,
    SQLiteAdmissionStore,
)
from .ai import AIAdmissionController, AIControllerStatus, AITaskClass
from .build import BuildAdmissionController, BuildControllerStatus
from .calibration import (
    CalibrationError,
    CalibrationKey,
    CalibrationProfile,
    CalibrationStore,
    LeanCalibrationError,
    LeanImportProfile,
    ReplMemoryCalibration,
    measure_lean_repl_memory,
    project_calibration_key,
    project_import_profile,
    project_lean_version,
    summarize_repl_memory,
)
from .config import (
    AIConcurrencyConfig,
    AIConcurrencyPatch,
    AutoInt,
    AutoValue,
    BudgetPolicy,
    BuildConcurrencyConfig,
    BuildConcurrencyPatch,
    CodexPlan,
    ConcurrencyConfig,
    ConcurrencyConfigError,
    ConcurrencyConfigPatch,
    ConcurrencyMode,
    ConfigScope,
    LeanConcurrencyConfig,
    LeanConcurrencyPatch,
    LegacyVerificationConfig,
    LegacyVerificationPatch,
    MachineConcurrencySettings,
    MachineConfigLocationError,
    MachineConfigRevisionError,
    MachineConfigStore,
    PressureState,
    ResolvedConcurrencyConfig,
    ResourceProfile,
    SchedulerConcurrencyConfig,
    SchedulerConcurrencyPatch,
    default_machine_config_path,
    environment_patch,
    patch_from_mapping,
    patch_to_mapping,
    resolve_concurrency_config,
)
from .distributed_leases import (
    CoordinatorOwnedAILeases,
    DistributedAILeaseCoordinator,
    DistributedAIRequest,
    NodeResourceControllers,
)
from .hardware import (
    AutoTunedLimits,
    HardwareResources,
    derive_auto_limits,
    detect_hardware,
)
from .lean import LeanAdmissionController, LeanControllerStatus
from .macos_memory import query_macos_memory_pressure_level
from .memory_pressure import (
    MemoryPressureClassifier,
    MemoryPressureDecision,
    MemoryPressurePolicy,
    MemoryPressureSource,
)
from .runtime import ConcurrencyRuntime, ConcurrencyRuntimeSpec
from .scheduler import (
    ConcurrencyScheduler,
    DependencyScheduler,
    DuplicateEscalationPolicy,
    ScheduledTask,
    TaskPriority,
)
from .telemetry import (
    PressureStallMetrics,
    QueueDepths,
    TelemetryCollector,
    TelemetrySnapshot,
    parse_psi,
)


@dataclass(frozen=True)
class CapacityStatus:
    configured: AutoInt
    effective: int
    maximum: int
    source: ConfigScope
    maximum_source: ConfigScope

    @property
    def automatic(self) -> bool:
        return self.configured == AutoValue.AUTO


@dataclass(frozen=True)
class SchedulerStatus:
    agents_per_target_initial: int
    agents_per_target_max: int
    dependency_priority: bool
    duplicate_agent_escalation: bool


@dataclass(frozen=True)
class ConcurrencyStatus:
    """Backend-composed configured/effective status suitable for any UI."""

    configured: ConcurrencyConfig
    machine_revision: int
    resource_profile: ResourceProfile
    ai: CapacityStatus
    lean: CapacityStatus
    build: CapacityStatus
    scheduler: SchedulerStatus
    hardware: HardwareResources
    telemetry: TelemetrySnapshot | None
    decisions: tuple[str, ...]
    sources: Mapping[str, ConfigScope]


def derive_concurrency_status(
    resolved: ResolvedConcurrencyConfig,
    hardware: HardwareResources,
    *,
    telemetry: TelemetrySnapshot | None = None,
    calibrated_repl_p95_gib: float | None = None,
) -> ConcurrencyStatus:
    """Create a complete initial status without embedding tuning in a UI."""

    configured = resolved.config
    effective = derive_auto_limits(
        hardware,
        configured,
        calibrated_repl_p95_gib=calibrated_repl_p95_gib,
    )
    lean_max = (
        effective.lean_pool
        if configured.lean.max_pool == AutoValue.AUTO
        else int(configured.lean.max_pool)
    )
    return ConcurrencyStatus(
        configured=configured,
        machine_revision=resolved.machine_revision,
        resource_profile=effective.resource_profile,
        ai=CapacityStatus(
            configured=configured.ai.initial,
            effective=effective.ai_initial,
            maximum=effective.ai_ceiling,
            source=resolved.source_for("ai.initial"),
            maximum_source=resolved.source_for("ai.hard_max"),
        ),
        lean=CapacityStatus(
            configured=configured.lean.pool_size,
            effective=effective.lean_pool,
            maximum=lean_max,
            source=resolved.source_for("lean.pool_size"),
            maximum_source=resolved.source_for("lean.max_pool"),
        ),
        build=CapacityStatus(
            configured=configured.build.max_concurrent,
            effective=effective.build_concurrency,
            maximum=configured.build.hard_max,
            source=resolved.source_for("build.max_concurrent"),
            maximum_source=resolved.source_for("build.hard_max"),
        ),
        scheduler=SchedulerStatus(
            agents_per_target_initial=configured.scheduler.agents_per_target_initial,
            agents_per_target_max=configured.scheduler.agents_per_target_max,
            dependency_priority=configured.scheduler.dependency_priority,
            duplicate_agent_escalation=configured.scheduler.duplicate_agent_escalation,
        ),
        hardware=hardware,
        telemetry=telemetry,
        decisions=effective.reasons,
        sources=resolved.sources,
    )


__all__ = [
    "AIAdmissionController",
    "AIConcurrencyConfig",
    "AIConcurrencyPatch",
    "AIControllerStatus",
    "AITaskClass",
    "AdmissionController",
    "AdmissionLease",
    "AdmissionRequest",
    "AdmissionSnapshot",
    "AutoInt",
    "AutoTunedLimits",
    "AutoValue",
    "BudgetPolicy",
    "BuildConcurrencyConfig",
    "BuildConcurrencyPatch",
    "BuildAdmissionController",
    "BuildControllerStatus",
    "CalibrationError",
    "CalibrationKey",
    "CalibrationProfile",
    "CalibrationStore",
    "CapacityStatus",
    "CodexPlan",
    "ConcurrencyConfig",
    "ConcurrencyConfigError",
    "ConcurrencyConfigPatch",
    "ConcurrencyMode",
    "ConcurrencyRuntime",
    "ConcurrencyRuntimeSpec",
    "ConcurrencyScheduler",
    "CoordinatorOwnedAILeases",
    "ConcurrencyStatus",
    "ConfigScope",
    "HardwareResources",
    "DependencyScheduler",
    "DistributedAILeaseCoordinator",
    "DistributedAIRequest",
    "DuplicateEscalationPolicy",
    "LeanAdmissionController",
    "LeanCalibrationError",
    "LeanConcurrencyConfig",
    "LeanConcurrencyPatch",
    "LeanControllerStatus",
    "LeanImportProfile",
    "LegacyVerificationConfig",
    "LegacyVerificationPatch",
    "MachineConcurrencySettings",
    "MachineConfigLocationError",
    "MachineConfigRevisionError",
    "MachineConfigStore",
    "MemoryPressureClassifier",
    "MemoryPressureDecision",
    "MemoryPressurePolicy",
    "MemoryPressureSource",
    "NodeResourceControllers",
    "PressureStallMetrics",
    "PressureState",
    "QueueDepths",
    "ReplMemoryCalibration",
    "ResolvedConcurrencyConfig",
    "ResourceProfile",
    "ResourceKind",
    "SQLiteAdmissionStore",
    "ScheduledTask",
    "SchedulerConcurrencyConfig",
    "SchedulerConcurrencyPatch",
    "SchedulerStatus",
    "TelemetryCollector",
    "TelemetrySnapshot",
    "TaskPriority",
    "default_machine_config_path",
    "derive_auto_limits",
    "derive_concurrency_status",
    "detect_hardware",
    "environment_patch",
    "measure_lean_repl_memory",
    "parse_psi",
    "query_macos_memory_pressure_level",
    "patch_from_mapping",
    "patch_to_mapping",
    "project_calibration_key",
    "project_import_profile",
    "project_lean_version",
    "resolve_concurrency_config",
    "summarize_repl_memory",
]
