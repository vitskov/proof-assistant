"""Stable contracts between the verifier, application workflow, and UIs.

This module intentionally imports neither Textual nor the incremental verifier.
Every UI consumes these immutable value objects and invokes the service protocol;
it never reaches into SQLite, Git snapshots, or Lean orchestration directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from ..ai import (
    SUPPORTED_DRIVERS as SUPPORTED_DRIVERS,
)
from ..ai import (
    CredentialSource as CredentialSource,
)
from ..ai import (
    Difficulty as Difficulty,
)
from ..ai import (
    DriverId as DriverId,
)
from ..ai import (
    DriverStatus as DriverStatus,
)
from ..ai import (
    DriverTransport as DriverTransport,
)
from ..ai import (
    InstallPlan as InstallPlan,
)
from ..ai import (
    InstallResult as InstallResult,
)
from ..ai import (
    ProviderConfig as ProviderConfig,
)
from ..ai import (
    ProviderSetupSnapshot as ProviderSetupSnapshot,
)
from ..ai import (
    SecretSubmission as SecretSubmission,
)
from ..ai import (
    TaskKind as TaskKind,
)
from ..ai import (
    TaskModelPolicy as TaskModelPolicy,
)
from ..ai import (
    TaskPreference as TaskPreference,
)
from ..ai.contracts import validate_model_identifier as validate_model_identifier

CONTRACT_SCHEMA_VERSION = 11

DISPATCHED_AI_TASKS = (
    TaskKind.CLARIFICATION,
    TaskKind.DIAGNOSTIC,
    TaskKind.PROOF,
)


class WorkflowState(StrEnum):
    PROJECT_READY = "PROJECT_READY"
    OBSERVING_SOURCE = "OBSERVING_SOURCE"
    CHANGE_REVIEW = "CHANGE_REVIEW"
    VERIFYING = "VERIFYING"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    BUSY_EXTERNAL = "BUSY_EXTERNAL"


class ProgressPhase(StrEnum):
    VALIDATING = "VALIDATING"
    OBSERVING_SOURCE = "OBSERVING_SOURCE"
    IMPORTING_SOURCE = "IMPORTING_SOURCE"
    INDEXING = "INDEXING"
    IMPACT_ANALYSIS = "IMPACT_ANALYSIS"
    CACHE_SETUP = "CACHE_SETUP"
    LEAN_BUILD = "LEAN_BUILD"
    LEAN_EXTRACTION = "LEAN_EXTRACTION"
    PROOF_BATCH = "PROOF_BATCH"
    CERTIFICATION = "CERTIFICATION"
    REPORTING = "REPORTING"
    COMPLETE = "COMPLETE"


class FileChangeKind(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    RENAMED = "RENAMED"


class ClaimChangeKind(StrEnum):
    ADDED = "ADDED"
    STATEMENT = "STATEMENT"
    ASSISTANT_CONTEXT = "ASSISTANT_CONTEXT"
    PROOF_ONLY = "PROOF_ONLY"
    DEPENDENCY = "DEPENDENCY"
    DELETED = "DELETED"
    TASK_SCOPE = "TASK_SCOPE"
    TASK_MODE = "TASK_MODE"
    POLICY = "POLICY"


class ProjectAvailability(StrEnum):
    """Backend classification shared by catalog display and creation preflight."""

    AVAILABLE = "AVAILABLE"
    RESUMABLE = "RESUMABLE"
    MIGRATION_READY = "MIGRATION_READY"
    NEEDS_MAIN_FILE = "NEEDS_MAIN_FILE"
    INCOMPLETE = "INCOMPLETE"
    OCCUPIED = "OCCUPIED"


class ProjectDeletionAvailability(StrEnum):
    READY = "READY"
    BUSY = "BUSY"
    REFUSED = "REFUSED"


class ManuscriptFolderOrigin(StrEnum):
    """Why the backend chose the current folder-picker location."""

    REQUESTED = "REQUESTED"
    PREFERENCE = "PREFERENCE"
    HOME_FALLBACK = "HOME_FALLBACK"


class VerificationJobState(StrEnum):
    """Durable lifecycle of one detached verification worker."""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"

    @property
    def terminal(self) -> bool:
        return self in {
            VerificationJobState.SUCCEEDED,
            VerificationJobState.FAILED,
            VerificationJobState.INTERRUPTED,
        }


class FailureScope(StrEnum):
    """The authority boundary at which a verification failure occurred."""

    RUN = "RUN"
    BATCH = "BATCH"
    CLAIM = "CLAIM"
    COMPONENT = "COMPONENT"


class FailureKind(StrEnum):
    """Stable, UI-neutral failure classes independent of diagnostic wording."""

    CLAIM_TECHNICAL = "CLAIM_TECHNICAL"
    BATCH_TECHNICAL = "BATCH_TECHNICAL"
    PROVIDER = "PROVIDER"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    SOURCE_INTEGRITY = "SOURCE_INTEGRITY"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    UNKNOWN = "UNKNOWN"


class ClarificationOrigin(StrEnum):
    """Subsystem that authorized an immutable clarification question."""

    HOST_POLICY = "HOST_POLICY"
    PROOF_WORKER = "PROOF_WORKER"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"


class ClarificationAnalysisStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class ClarificationConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SettingsScopeKind(StrEnum):
    """Persistence scope for concurrency/resource settings."""

    MACHINE = "MACHINE"
    PROJECT = "PROJECT"


class BenchmarkKind(StrEnum):
    CODEX = "codex-concurrency"
    LEAN = "lean-concurrency"
    BUILD = "build-concurrency"


@dataclass(frozen=True)
class VerificationRoleSettings:
    """Frozen provider/model/effort assignment for one RepoProver role."""

    task: TaskKind
    ai_driver: str
    model: str
    effort: str

    def __post_init__(self) -> None:
        if not isinstance(self.task, TaskKind):
            raise TypeError("verification role task must be a TaskKind")
        DriverId(self.ai_driver)
        validate_model_identifier(self.model, field_name=f"{self.task.value} model")
        Difficulty(self.effort)


@dataclass(frozen=True)
class VerificationSettings:
    ai_driver: str = "codex_cli"
    model: str = "gpt-5.6-sol"
    effort: str = "high"
    jobs: int = 2
    batch_size: int = 8
    lean_pool_size: int = 1
    turn_timeout: float = 86400.0
    setup_timeout: float = 1800.0
    request_timeout: float = 120.0
    gc_timeout: float = 900.0
    role_settings: tuple[VerificationRoleSettings, ...] = ()

    def __post_init__(self) -> None:
        DriverId(self.ai_driver)
        validate_model_identifier(self.model, field_name="verification model")
        Difficulty(self.effort)
        if any(
            not isinstance(item, VerificationRoleSettings)
            for item in self.role_settings
        ):
            raise TypeError("role_settings must contain VerificationRoleSettings")
        tasks = tuple(item.task for item in self.role_settings)
        if len(tasks) != len(set(tasks)):
            raise ValueError("verification role settings must be unique")
        if self.role_settings and set(tasks) != set(TaskKind):
            missing = sorted(task.value for task in set(TaskKind) - set(tasks))
            raise ValueError(
                "frozen verification settings require every role; missing: "
                + ", ".join(missing)
            )
        if self.role_settings:
            role_drivers = {item.ai_driver for item in self.role_settings}
            if role_drivers != {self.ai_driver}:
                raise ValueError(
                    "frozen verification settings require one provider for every role"
                )
            proof = next(
                item for item in self.role_settings if item.task is TaskKind.PROOF
            )
            if (self.ai_driver, self.model, self.effort) != (
                proof.ai_driver,
                proof.model,
                proof.effort,
            ):
                raise ValueError(
                    "verification scalar compatibility fields must match the proof role"
                )

    def for_task(self, task: TaskKind) -> VerificationRoleSettings:
        """Return the frozen role assignment, with legacy proof fallback."""

        for setting in self.role_settings:
            if setting.task is task:
                return setting
        return VerificationRoleSettings(
            task=task,
            ai_driver=self.ai_driver,
            model=self.model,
            effort=self.effort,
        )


@dataclass(frozen=True)
class ProjectAIRoleOverride:
    """Secret-free model and effort choice for one project role."""

    task: TaskKind
    model: str
    difficulty: Difficulty

    def __post_init__(self) -> None:
        if not isinstance(self.task, TaskKind):
            raise TypeError("project AI role task must be a TaskKind")
        validate_model_identifier(
            self.model, field_name=f"project {self.task.value} model"
        )


@dataclass(frozen=True)
class ProjectAIOverride:
    """One provider plus explicit role-aware choices for a managed project."""

    ai_driver: DriverId
    roles: tuple[ProjectAIRoleOverride, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.ai_driver, DriverId):
            raise TypeError("project AI driver must be a DriverId")
        if any(not isinstance(item, ProjectAIRoleOverride) for item in self.roles):
            raise TypeError("project AI roles must contain ProjectAIRoleOverride")
        tasks = tuple(item.task for item in self.roles)
        if len(tasks) != len(set(tasks)):
            raise ValueError("project AI role overrides must be unique")

    def role_for(self, task: TaskKind) -> ProjectAIRoleOverride | None:
        return next((item for item in self.roles if item.task is task), None)

    @property
    def complete(self) -> bool:
        return {item.task for item in self.roles} == set(TaskKind)


@dataclass(frozen=True)
class ProjectVerificationSettingsSnapshot:
    """Resolved project AI preference plus current machine-derived run settings."""

    project_path: Path
    revision: int
    override: ProjectAIOverride | None
    effective: VerificationSettings
    validation_error: str | None = None

    @property
    def inherited(self) -> bool:
        return self.override is None

    @property
    def valid(self) -> bool:
        return self.validation_error is None


@dataclass(frozen=True)
class ConcurrencySettingsView:
    """Editable machine policy; ``None`` means automatic for numeric fields."""

    mode: str = "adaptive"
    resource_profile: str = "auto"
    codex_plan: str = "unknown"
    budget_policy: str = "balanced"
    ai_initial: int | None = None
    ai_hard_max: int | None = None
    ai_minimum: int = 1
    ai_increase_after_successes: int | None = None
    lean_pool: int | None = None
    lean_max: int | None = None
    lean_minimum: int = 1
    lean_memory_calibration: bool = True
    fallback_memory_per_repl_gib: float = 3.0
    max_builds: int | None = None
    build_hard_max: int = 8
    agents_per_target_initial: int = 1
    agents_per_target_max: int = 4
    duplicate_agent_escalation: bool = True
    dependency_priority: bool = True
    adaptive_controller: bool = True
    hardware_telemetry: bool = True


@dataclass(frozen=True)
class EffectiveConcurrencyView:
    ai_limit: int
    ai_ceiling: int
    lean_pool: int
    lean_max: int
    build_limit: int
    build_ceiling: int
    agents_per_target_current: int
    agents_per_target_max: int


@dataclass(frozen=True)
class ResourceTelemetryView:
    os_name: str
    architecture: str
    resource_profile: str
    physical_cpus: int
    logical_cpus: int
    cpu_percent: float
    total_memory_gib: float
    available_memory_gib: float
    memory_percent_available: float
    swap_used_gib: float
    swap_out_mib_per_second: float | None
    memory_pressure: str
    memory_pressure_source: str
    native_memory_pressure_level: int | None
    load_average: tuple[float, float, float] | None
    io_wait_percent: float | None
    ai_active: int
    ai_queued: int
    ai_throttles: int
    ai_backoff_until: str | None
    lean_active: int
    lean_queued: int
    lean_p95_rss_gib: float | None
    build_active: int
    build_queued: int
    sampled_at: str


@dataclass(frozen=True)
class LegacySettingsView:
    """Old coupled knobs retained visibly as machine compatibility settings."""

    proof_jobs: int = 2
    batch_size: int = 8
    per_worker_lean_pool: int = 1
    proof_jobs_status: str = "compatibility minimum logical-worker fan-out; machine AI admission is authoritative"
    batch_size_status: str = "active scheduling granularity; next run"
    per_worker_lean_pool_status: str = "superseded by global Lean admission"
    process_local_ai_status: str = "removed; superseded by machine AI admission"
    raw_build_status: str = "removed; superseded by machine build admission"


@dataclass(frozen=True)
class SettingResolution:
    field: str
    configured: str
    effective: str
    source: str


@dataclass(frozen=True)
class MachineSettingsSnapshot:
    scope: SettingsScopeKind
    machine_id: str
    config_path: Path
    cache_path: Path
    revision: int
    configured: ConcurrencySettingsView
    effective: EffectiveConcurrencyView
    telemetry: ResourceTelemetryView
    legacy: LegacySettingsView
    resolution: tuple[SettingResolution, ...]
    reasons: tuple[str, ...]
    updated_at: str


@dataclass(frozen=True)
class MachineSettingsUpdateRequest:
    expected_revision: int
    configured: ConcurrencySettingsView
    legacy: LegacySettingsView
    scope: SettingsScopeKind = SettingsScopeKind.MACHINE


@dataclass(frozen=True)
class SettingsWarning:
    warning_id: str
    message: str
    recommended_value: str


@dataclass(frozen=True)
class SettingsChangePreview:
    preview_token: str
    requested: MachineSettingsUpdateRequest
    effective_if_applied: EffectiveConcurrencyView
    warnings: tuple[SettingsWarning, ...]
    live_fields: tuple[str, ...]
    next_run_fields: tuple[str, ...]


@dataclass(frozen=True)
class BenchmarkResult:
    kind: BenchmarkKind
    recommendation: int
    tested_values: tuple[int, ...]
    detail: str
    used_codex_traffic: bool
    calibration_path: Path


@dataclass(frozen=True)
class CalibrationResetResult:
    """Result of deleting one exact project/environment calibration profile."""

    project_path: Path
    profile_id: str
    calibration_path: Path
    removed: bool


@dataclass(frozen=True)
class AdaptiveHistoryResetResult:
    """Effective controller state after clearing machine adaptive history.

    Existing leases are deliberately preserved.  In adaptive mode the three
    admission limits return to their current policy-derived starting values;
    fixed/manual limits remain unchanged.
    """

    reset_at: str
    ai_limit: int
    lean_pool: int
    build_limit: int
    in_flight_work_preserved: bool = True


@dataclass(frozen=True)
class VerificationJob:
    """Immutable backend-owned identity and lifecycle facts for one job."""

    job_id: str
    project_path: Path
    state: VerificationJobState
    request_fingerprint: str
    plan_id: str | None
    settings: VerificationSettings | None
    created_at: str
    started_at: str | None
    updated_at: str
    completed_at: str | None
    heartbeat_at: str | None
    pid: int | None
    error: str | None
    cancellable: bool
    attached_legacy: bool
    worker_log_path: Path | None = None
    launch_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class LatexSourceCandidate:
    """A LaTeX file that may be selected as a manuscript root."""

    relative_path: str
    has_documentclass: bool


@dataclass(frozen=True)
class SourceInspection:
    """Read-only source discovery returned before project creation."""

    source_path: Path
    candidates: tuple[LatexSourceCandidate, ...]
    suggested_main_file: str
    source_in_dropbox: bool

    @property
    def selection_required(self) -> bool:
        return len(self.candidates) > 1


@dataclass(frozen=True)
class ManuscriptFolderEntry:
    """One backend-enumerated child directory in the terminal picker."""

    name: str
    path: Path
    symlink: bool = False


@dataclass(frozen=True)
class ManuscriptFolderListing:
    """Complete UI-neutral state for one directory-picker location."""

    directory: Path
    parent: Path | None
    home: Path
    folders: tuple[ManuscriptFolderEntry, ...]
    origin: ManuscriptFolderOrigin


@dataclass(frozen=True)
class NewProjectRequest:
    name: str
    source_path: Path
    main_file: str
    project_path: Path | None = None
    task_text: str | None = None
    settings: VerificationSettings = field(default_factory=VerificationSettings)


@dataclass(frozen=True)
class ProjectSummary:
    project_id: str
    name: str
    project_path: Path
    source_path: Path
    main_file: str
    input_files: tuple[str, ...]
    last_opened_at: str
    workflow_state: WorkflowState
    latest_outcome: str | None = None
    open_questions: int = 0
    source_in_dropbox: bool = False


@dataclass(frozen=True)
class ProjectCatalogEntry:
    """A reconciled catalog row, including safe recovery/occupancy states."""

    name: str
    project_path: Path
    availability: ProjectAvailability
    project: ProjectSummary | None = None
    issue: str | None = None
    source_path: Path | None = None
    main_file_candidates: tuple[LatexSourceCandidate, ...] = ()
    suggested_main_file: str | None = None

    def __post_init__(self) -> None:
        has_project = self.project is not None
        if (self.availability == ProjectAvailability.RESUMABLE) != has_project:
            raise ValueError(
                "A RESUMABLE catalog entry must carry exactly one ProjectSummary"
            )
        if self.availability == ProjectAvailability.NEEDS_MAIN_FILE:
            choices = {
                candidate.relative_path for candidate in self.main_file_candidates
            }
            if self.source_path is None or not choices:
                raise ValueError(
                    "NEEDS_MAIN_FILE requires a source path and candidate files"
                )
            if self.suggested_main_file not in choices:
                raise ValueError(
                    "NEEDS_MAIN_FILE suggestion must identify a candidate file"
                )

    @property
    def resumable(self) -> bool:
        return self.availability == ProjectAvailability.RESUMABLE


@dataclass(frozen=True)
class ProjectDestinationInspection:
    """Non-mutating classification of a proposed managed-project path."""

    project_path: Path
    availability: ProjectAvailability
    issue: str | None = None

    @property
    def can_create(self) -> bool:
        return self.availability == ProjectAvailability.AVAILABLE


@dataclass(frozen=True)
class ProjectDeletionInspection:
    """Backend-owned preflight for recoverably deleting one managed project."""

    project_path: Path
    source_path: Path | None
    availability: ProjectDeletionAvailability
    issue: str | None = None
    source_in_dropbox: bool = False

    @property
    def can_delete(self) -> bool:
        return self.availability == ProjectDeletionAvailability.READY


@dataclass(frozen=True)
class ProjectDeletionResult:
    """Exact paths resulting from a successful recoverable project move."""

    project_path: Path
    source_path: Path
    trash_path: Path
    deleted_at: str
    recoverable: bool = True


@dataclass(frozen=True)
class FileChange:
    path: str
    kind: FileChangeKind
    old_path: str | None = None
    old_sha256: str | None = None
    new_sha256: str | None = None


@dataclass(frozen=True)
class ClaimImpact:
    claim_id: str
    kind: ClaimChangeKind
    source_file: str | None = None


@dataclass(frozen=True)
class ChangeImpactPlan:
    plan_id: str
    project_path: Path
    source_path: Path
    main_file: str
    input_files: tuple[str, ...]
    base_snapshot: str | None
    candidate_inventory_sha256: str
    file_changes: tuple[FileChange, ...]
    direct_claim_changes: tuple[ClaimImpact, ...]
    affected_claims: tuple[str, ...]
    unaffected_certificates: tuple[str, ...]
    superseded_questions: tuple[str, ...]
    task_changed: bool
    source_in_dropbox: bool
    created_at: str
    main_file_changed: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.file_changes or self.task_changed or self.main_file_changed)


@dataclass(frozen=True)
class SourceLocation:
    relative_path: str
    absolute_path: Path
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    context_start_line: int
    context_end_line: int
    excerpt: str
    highlighted_lines: tuple[int, ...]
    snapshot_commit: str


@dataclass(frozen=True)
class ClarificationEvidenceItem:
    evidence_id: str
    kind: str
    content: str
    sha256: str


@dataclass(frozen=True)
class ClarificationEvidencePacket:
    question_id: str
    snapshot_commit: str
    origin: ClarificationOrigin
    items: tuple[ClarificationEvidenceItem, ...]
    evidence_sha256: str


@dataclass(frozen=True)
class ClarificationReasoning:
    statement: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ClarificationAnalysis:
    status: ClarificationAnalysisStatus
    evidence_sha256: str
    origin: ClarificationOrigin
    hypothesis: str | None = None
    confidence: ClarificationConfidence | None = None
    reasoning: tuple[ClarificationReasoning, ...] = ()
    alternatives: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    recommended_author_check: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    failure_detail: str | None = None


@dataclass(frozen=True)
class ClarificationPresentation:
    question_id: str
    claim_id: str
    category: str
    headline: str
    explanation: str
    requested_actions: tuple[str, ...]
    possible_resolutions: tuple[str, ...]
    location: SourceLocation
    blocked_claims: tuple[str, ...]
    generated_by: str
    provenance_sha256: str
    analysis: ClarificationAnalysis | None = None
    observed_problem: str = ""


@dataclass(frozen=True)
class FindingSummary:
    outcome: str
    detail: str
    verified: tuple[str, ...] = ()
    reused: tuple[str, ...] = ()
    reconciled: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    suspect_false: tuple[str, ...] = ()
    counterexamples: tuple[str, ...] = ()
    skipped_unproved: tuple[str, ...] = ()
    dependency_discrepancies: tuple[Mapping[str, Any], ...] = ()
    report_path: Path | None = None
    project_path: Path | None = None
    failure_report: FailureDependencyReport | None = None


@dataclass(frozen=True)
class FailureArtifact:
    """A durable artifact supporting one exact failure reason."""

    path: Path
    label: str
    sha256: str | None = None
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class FailureIncident:
    """One immutable failure observation made during a verification run."""

    incident_id: int
    run_id: int
    scope: FailureScope
    kind: FailureKind
    phase: str
    category: str
    message: str
    detail: str | None
    provenance: str
    claim_ids: tuple[str, ...]
    batch_index: int | None
    retryable: bool
    artifacts: tuple[FailureArtifact, ...] = ()


@dataclass(frozen=True)
class FailureGraphNode:
    """A claim in the immutable dependency graph recorded for one run."""

    claim_id: str
    kind: str
    source_file: str
    statement_start: int
    statement_end: int
    state: str
    incident_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class FailureGraphEdge:
    """A grouped dependent-to-dependency edge in one run's graph."""

    dependent: str
    dependency: str
    kinds: tuple[str, ...]
    provenances: tuple[str, ...]


@dataclass(frozen=True)
class FailureOutlineNode:
    """Tree-first rendering node for an acyclic dependency graph.

    A repeated DAG node is emitted as a leaf with ``shared_reference=True``.
    This preserves sharing without recursively duplicating its descendants.
    """

    # A claim ID, or ``incident:<id>`` for a run/batch-scoped synthetic root.
    claim_id: str
    state: str
    blocker: bool
    incident_ids: tuple[int, ...]
    shared_reference: bool
    children: tuple[FailureOutlineNode, ...] = ()


@dataclass(frozen=True)
class FailureComponent:
    """A strongly connected component used only for cyclic graph fallback."""

    component_id: str
    members: tuple[str, ...]
    cyclic: bool
    blocker: bool
    incident_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class FailureComponentEdge:
    dependent_component: str
    dependency_component: str


@dataclass(frozen=True)
class FailurePath:
    """A canonical target-to-blocker path using dependent-first order."""

    target: str
    blocker: str
    claims: tuple[str, ...]


@dataclass(frozen=True)
class FailureDependencyReport:
    """Immutable failure explanation and dependency visualization for one run."""

    run_id: int
    snapshot: str | None
    outcome: str
    detail: str
    targets: tuple[str, ...]
    selected: tuple[str, ...]
    nodes: tuple[FailureGraphNode, ...]
    edges: tuple[FailureGraphEdge, ...]
    incidents: tuple[FailureIncident, ...]
    global_incident_ids: tuple[int, ...]
    primary_incident_id: int | None
    first_blocker: FailurePath | None
    paths: tuple[FailurePath, ...]
    has_cycles: bool
    outline: tuple[FailureOutlineNode, ...]
    components: tuple[FailureComponent, ...]
    component_edges: tuple[FailureComponentEdge, ...]


@dataclass(frozen=True)
class ReportDocument:
    """Backend-loaded verification report for terminal-native presentation."""

    path: Path
    markdown: str


@dataclass(frozen=True)
class ProgressEvent:
    sequence: int
    phase: ProgressPhase
    message: str
    completed: int | None = None
    total: int | None = None
    claim_id: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationJobObservation:
    """Cursor-based replay and current state for a detached verification job."""

    job: VerificationJob
    events: tuple[ProgressEvent, ...]
    after_sequence: int
    next_sequence: int
    started: bool = False
    attached: bool = True
    poll_after_seconds: float = 1.0


@dataclass(frozen=True)
class CancellationReport:
    """Durable backend facts established at a cooperative stop boundary.

    A UI may describe cancellation as safe only after receiving this report.
    Certificates listed here were committed before interruption. Claims listed
    as retryable were in flight and have been moved out of ``PROVING`` so a
    resumed verification can schedule them again.
    """

    run_id: int | None
    detail: str
    preserved_certificates: tuple[str, ...]
    retryable_claims: tuple[str, ...]
    temporary_worktrees_cleaned: bool


@dataclass(frozen=True)
class WorkflowSnapshot:
    state: WorkflowState
    project: ProjectSummary
    pending_plan: ChangeImpactPlan | None = None
    clarifications: tuple[ClarificationPresentation, ...] = ()
    findings: FindingSummary | None = None
    error: str | None = None
    cancellation: CancellationReport | None = None


ProgressSink = Callable[[ProgressEvent], None]


class CancellationToken(Protocol):
    """Cooperative signal checked only at backend-owned consistency boundaries."""

    @property
    def cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...


class WorkflowServiceContract(Protocol):
    def default_task_text(self) -> str: ...

    def default_verification_settings(
        self, project: Path | None = None
    ) -> VerificationSettings: ...

    def get_project_verification_settings(
        self, project: Path
    ) -> ProjectVerificationSettingsSnapshot: ...

    def update_project_verification_settings(
        self,
        project: Path,
        override: ProjectAIOverride,
        *,
        expected_revision: int,
    ) -> ProjectVerificationSettingsSnapshot: ...

    def reset_project_verification_settings(
        self, project: Path, *, expected_revision: int
    ) -> ProjectVerificationSettingsSnapshot: ...

    def get_ai_setup(self) -> ProviderSetupSnapshot: ...

    def update_ai_settings(
        self, config: ProviderConfig, *, expected_revision: int
    ) -> ProviderSetupSnapshot: ...

    def ai_task_policies(
        self, driver: DriverId | None = None
    ) -> tuple[TaskModelPolicy, ...]: ...

    def preview_ai_driver_install(self, driver: DriverId) -> InstallPlan: ...

    def install_ai_driver(
        self, plan: InstallPlan, *, consent_token: str
    ) -> InstallResult: ...

    def verify_ai_driver_account(
        self, driver: DriverId, *, consent: bool
    ) -> ProviderSetupSnapshot: ...

    def store_ai_credential(
        self,
        driver: DriverId,
        source: CredentialSource,
        credential: SecretSubmission,
    ) -> ProviderSetupSnapshot: ...

    def delete_ai_credential(
        self, driver: DriverId, source: CredentialSource
    ) -> ProviderSetupSnapshot: ...

    def get_machine_settings(
        self, *, project: Path | None = None
    ) -> MachineSettingsSnapshot: ...

    def preview_machine_settings(
        self, request: MachineSettingsUpdateRequest
    ) -> SettingsChangePreview: ...

    def apply_machine_settings(
        self,
        preview_token: str,
        accepted_warning_ids: tuple[str, ...] = (),
    ) -> MachineSettingsSnapshot: ...

    def reset_machine_settings(
        self, expected_revision: int
    ) -> MachineSettingsSnapshot: ...

    def run_concurrency_benchmark(
        self,
        kind: BenchmarkKind,
        *,
        project: Path | None = None,
        allow_codex_traffic: bool = False,
    ) -> BenchmarkResult: ...

    def reset_project_lean_calibration(
        self, project: Path
    ) -> CalibrationResetResult: ...

    def reset_adaptive_history(self) -> AdaptiveHistoryResetResult: ...

    def browse_manuscript_folders(
        self, directory: Path | None = None
    ) -> ManuscriptFolderListing: ...

    def remember_manuscript_folder(self, directory: Path) -> Path: ...

    def inspect_source(self, source: Path) -> SourceInspection: ...

    def inspect_project_destination(
        self, name: str, project_path: Path | None = None
    ) -> ProjectDestinationInspection: ...

    def inspect_project_deletion(self, project: Path) -> ProjectDeletionInspection: ...

    def list_projects(self) -> Sequence[ProjectCatalogEntry]: ...

    def create_project(self, request: NewProjectRequest) -> WorkflowSnapshot: ...

    def delete_project(self, project: Path) -> ProjectDeletionResult: ...

    def select_project_main_file(
        self, project: Path, main_file: str
    ) -> WorkflowSnapshot: ...

    def load_report(self, project: Path) -> ReportDocument: ...

    def load_failure_report(
        self, project: Path, run_id: int | None = None
    ) -> FailureDependencyReport | None: ...

    def resume_project(self, project: Path) -> WorkflowSnapshot: ...

    def plan_changes(self, project: Path) -> ChangeImpactPlan | None: ...

    def start_verification(
        self,
        project: Path,
        plan_id: str | None,
        settings: VerificationSettings,
    ) -> VerificationJobObservation: ...

    def observe_verification(
        self, project: Path, after_sequence: int = 0
    ) -> VerificationJobObservation | None: ...

    def request_verification_cancel(
        self, project: Path, job_id: str
    ) -> VerificationJobObservation: ...


def contract_dict(value: object) -> dict[str, Any]:
    """Return a JSON-friendly representation for persisted UI state/events."""

    def normalize(item: Any) -> Any:
        if isinstance(item, FailureDependencyReport):
            payload = {
                field_info.name: normalize(getattr(item, field_info.name))
                for field_info in fields(item)
                if field_info.name != "outline"
            }
            # A recursive dataclass tree is ergonomic for a live TUI, but an
            # arbitrarily deep manuscript must not overflow Python or a JSON
            # encoder. Persist outline occurrences as a flat parent-indexed list.
            occurrences: list[dict[str, Any]] = []
            stack: list[tuple[FailureOutlineNode, int | None, int]] = [
                (root, None, 0) for root in reversed(item.outline)
            ]
            while stack:
                node, parent, depth = stack.pop()
                index = len(occurrences)
                occurrences.append(
                    {
                        "index": index,
                        "parent": parent,
                        "depth": depth,
                        "claim_id": node.claim_id,
                        "state": node.state,
                        "blocker": node.blocker,
                        "incident_ids": list(node.incident_ids),
                        "shared_reference": node.shared_reference,
                    }
                )
                stack.extend(
                    (child, index, depth + 1) for child in reversed(node.children)
                )
            payload["outline"] = occurrences
            payload["outline_format"] = "flat_parent_indexed"
            return payload
        if isinstance(item, FailureOutlineNode):
            outline = contract_dict(
                FailureDependencyReport(
                    run_id=0,
                    snapshot=None,
                    outcome="",
                    detail="",
                    targets=(),
                    selected=(),
                    nodes=(),
                    edges=(),
                    incidents=(),
                    global_incident_ids=(),
                    primary_incident_id=None,
                    first_blocker=None,
                    paths=(),
                    has_cycles=False,
                    outline=(item,),
                    components=(),
                    component_edges=(),
                )
            )
            return {
                "outline": outline["outline"],
                "outline_format": outline["outline_format"],
            }
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, StrEnum):
            return str(item)
        if is_dataclass(item) and not isinstance(item, type):
            return {
                field_info.name: normalize(getattr(item, field_info.name))
                for field_info in fields(item)
            }
        if isinstance(item, dict):
            return {str(key): normalize(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(entry) for entry in item]
        return item

    payload = normalize(value)
    if not isinstance(payload, dict):
        raise TypeError("contract_dict requires a dataclass or mapping contract")
    return payload
