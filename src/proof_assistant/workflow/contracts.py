"""Stable contracts between the verifier, application workflow, and UIs.

This module intentionally imports neither Textual nor the incremental verifier.
Every UI consumes these immutable value objects and invokes the service protocol;
it never reaches into SQLite, Git snapshots, or Lean orchestration directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

CONTRACT_SCHEMA_VERSION = 1


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
    PROOF_ONLY = "PROOF_ONLY"
    DEPENDENCY = "DEPENDENCY"
    DELETED = "DELETED"
    TASK_SCOPE = "TASK_SCOPE"
    TASK_MODE = "TASK_MODE"
    POLICY = "POLICY"


@dataclass(frozen=True)
class VerificationSettings:
    model: str = "gpt-5.6-sol"
    effort: str = "high"
    jobs: int = 1
    batch_size: int = 8
    lean_pool_size: int = 1
    turn_timeout: float = 86400.0
    setup_timeout: float = 1800.0
    request_timeout: float = 120.0
    gc_timeout: float = 900.0


@dataclass(frozen=True)
class NewProjectRequest:
    name: str
    source_path: Path
    project_path: Path | None = None
    task_text: str | None = None
    settings: VerificationSettings = field(default_factory=VerificationSettings)


@dataclass(frozen=True)
class ProjectSummary:
    project_id: str
    name: str
    project_path: Path
    source_path: Path
    last_opened_at: str
    workflow_state: WorkflowState
    latest_outcome: str | None = None
    open_questions: int = 0
    source_in_dropbox: bool = False


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

    @property
    def has_changes(self) -> bool:
        return bool(self.file_changes or self.task_changed)


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
    dependency_discrepancies: tuple[Mapping[str, Any], ...] = ()
    report_path: Path | None = None
    project_path: Path | None = None


@dataclass(frozen=True)
class ProgressEvent:
    sequence: int
    phase: ProgressPhase
    message: str
    completed: int | None = None
    total: int | None = None
    claim_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowSnapshot:
    state: WorkflowState
    project: ProjectSummary
    pending_plan: ChangeImpactPlan | None = None
    clarifications: tuple[ClarificationPresentation, ...] = ()
    findings: FindingSummary | None = None
    error: str | None = None


ProgressSink = Callable[[ProgressEvent], None]


class CancellationToken(Protocol):
    @property
    def cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...


class WorkflowServiceContract(Protocol):
    def default_task_text(self) -> str: ...

    def list_projects(self) -> Sequence[ProjectSummary]: ...

    def create_project(self, request: NewProjectRequest) -> WorkflowSnapshot: ...

    def resume_project(self, project: Path) -> WorkflowSnapshot: ...

    def plan_changes(self, project: Path) -> ChangeImpactPlan | None: ...

    def confirm_and_verify(
        self,
        project: Path,
        plan_id: str | None,
        settings: VerificationSettings,
        *,
        progress: ProgressSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> WorkflowSnapshot: ...


def contract_dict(value: object) -> dict[str, Any]:
    """Return a JSON-friendly representation for persisted UI state/events."""
    payload = asdict(value)

    def normalize(item: Any) -> Any:
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, StrEnum):
            return str(item)
        if isinstance(item, dict):
            return {str(key): normalize(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(entry) for entry in item]
        return item

    return normalize(payload)
