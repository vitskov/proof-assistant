from __future__ import annotations

import ast
import asyncio
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any

from rich.syntax import Syntax
from textual.pilot import Pilot
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    MarkdownViewer,
    OptionList,
    RadioButton,
    Select,
    Static,
    TabbedContent,
    TextArea,
)

import proof_assistant.tui.screens as tui_screens
import proof_assistant.tui.settings.screens as tui_settings_screens
from proof_assistant.tui import ProofAssistantApp
from proof_assistant.tui.app import CommandMenuScreen, ResizeNeededScreen
from proof_assistant.tui.commands import (
    AppHeader,
    AppHeaderIcon,
    AppHeaderTitle,
    CommandFooter,
)
from proof_assistant.tui.screens import (
    ChangeReviewScreen,
    ClarificationScreen,
    CopyableText,
    DashboardScreen,
    DesktopInput,
    DesktopTextArea,
    ExistingProjectMainFileSelectionScreen,
    FailureDependencyScreen,
    FailureTree,
    FindingsScreen,
    MainFileSelectionScreen,
    ManuscriptFolderPickerScreen,
    NewProjectScreen,
    ProgressScreen,
    ProjectDeletionConfirmationScreen,
    ProjectDeletionOutcomeScreen,
    ProjectDestinationConflictScreen,
    ProjectReviewScreen,
    RecoveryScreen,
    ReportViewerScreen,
    ShortcutHelpScreen,
    WelcomeScreen,
)
from proof_assistant.tui.settings import (
    ConcurrencyResourcesScreen,
    LegacySettingsScreen,
    SettingsHomeScreen,
    SettingsWarningConfirmationScreen,
)
from proof_assistant.tui.theme import PROOF_DARK_THEME, PROOF_LIGHT_THEME
from proof_assistant.workflow.contracts import (
    AdaptiveHistoryResetResult,
    BenchmarkKind,
    BenchmarkResult,
    CalibrationResetResult,
    CancellationReport,
    ChangeImpactPlan,
    ClaimChangeKind,
    ClaimImpact,
    ClarificationAnalysis,
    ClarificationAnalysisStatus,
    ClarificationConfidence,
    ClarificationOrigin,
    ClarificationPresentation,
    ClarificationReasoning,
    ConcurrencySettingsView,
    EffectiveConcurrencyView,
    FailureArtifact,
    FailureComponent,
    FailureComponentEdge,
    FailureDependencyReport,
    FailureGraphEdge,
    FailureGraphNode,
    FailureIncident,
    FailureKind,
    FailureOutlineNode,
    FailurePath,
    FailureScope,
    FileChange,
    FileChangeKind,
    FindingSummary,
    LatexSourceCandidate,
    LegacySettingsView,
    MachineSettingsSnapshot,
    MachineSettingsUpdateRequest,
    ManuscriptFolderEntry,
    ManuscriptFolderListing,
    ManuscriptFolderOrigin,
    NewProjectRequest,
    ProgressEvent,
    ProgressPhase,
    ProjectAvailability,
    ProjectCatalogEntry,
    ProjectDeletionAvailability,
    ProjectDeletionInspection,
    ProjectDeletionResult,
    ProjectDestinationInspection,
    ProjectSummary,
    ReportDocument,
    ResourceTelemetryView,
    SettingResolution,
    SettingsChangePreview,
    SettingsScopeKind,
    SettingsWarning,
    SourceInspection,
    SourceLocation,
    TaskKind,
    VerificationJob,
    VerificationJobObservation,
    VerificationJobState,
    VerificationRoleSettings,
    VerificationSettings,
    WorkflowSnapshot,
    WorkflowState,
)


def async_test[AsyncResult](
    function: Callable[..., Awaitable[AsyncResult]],
) -> Callable[..., AsyncResult]:
    """Run a Pilot coroutine without adding a pytest plugin dependency."""

    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> AsyncResult:
        return asyncio.run(function(*args, **kwargs))

    return run


def project(*, state: WorkflowState = WorkflowState.PROJECT_READY) -> ProjectSummary:
    return ProjectSummary(
        project_id="paper-1",
        name="Paper One",
        project_path=Path("/tmp/proof-assistant/paper-one"),
        source_path=Path("/Users/writer/Dropbox/paper"),
        main_file="main.tex",
        input_files=("sections/introduction.tex", "sections/results.tex"),
        last_opened_at="2026-08-23T12:00:00Z",
        workflow_state=state,
        source_in_dropbox=True,
    )


def machine_settings(*, revision: int = 3) -> MachineSettingsSnapshot:
    configured = ConcurrencySettingsView()
    return MachineSettingsSnapshot(
        scope=SettingsScopeKind.MACHINE,
        machine_id="machine-test-7",
        config_path=Path("/Users/writer/.config/proof-assistant/machines/test.json"),
        cache_path=Path("/Users/writer/.cache/proof-assistant/concurrency"),
        revision=revision,
        configured=configured,
        effective=EffectiveConcurrencyView(
            ai_limit=4,
            ai_ceiling=8,
            lean_pool=3,
            lean_max=6,
            build_limit=1,
            build_ceiling=8,
            agents_per_target_current=1,
            agents_per_target_max=4,
        ),
        telemetry=ResourceTelemetryView(
            os_name="macOS",
            architecture="arm64",
            resource_profile="interactive",
            physical_cpus=10,
            logical_cpus=10,
            cpu_percent=41.5,
            total_memory_gib=32.0,
            available_memory_gib=19.25,
            memory_percent_available=60.2,
            swap_used_gib=0.0,
            swap_out_mib_per_second=0.0,
            memory_pressure="GREEN",
            memory_pressure_source="macos_native",
            native_memory_pressure_level=0,
            load_average=(2.0, 1.8, 1.5),
            io_wait_percent=None,
            ai_active=2,
            ai_queued=5,
            ai_throttles=0,
            ai_backoff_until=None,
            lean_active=3,
            lean_queued=1,
            lean_p95_rss_gib=2.2,
            build_active=1,
            build_queued=0,
            sampled_at="2026-08-23T20:00:00Z",
        ),
        legacy=LegacySettingsView(),
        resolution=(
            SettingResolution("ai.initial", "Auto", "4", "machine auto policy"),
            SettingResolution("lean.pool", "Auto", "3", "CPU and RAM"),
            SettingResolution("build.max", "Auto", "1", "hardware policy"),
        ),
        reasons=(
            "Interactive profile reserves foreground CPU and memory.",
            "Lean pool is RAM-bound at the conservative fallback footprint.",
        ),
        updated_at="2026-08-23T20:00:00Z",
    )


def catalog_entry(
    p: ProjectSummary,
    *,
    availability: ProjectAvailability = ProjectAvailability.RESUMABLE,
    issue: str | None = None,
) -> ProjectCatalogEntry:
    return ProjectCatalogEntry(
        name=p.name,
        project_path=p.project_path,
        availability=availability,
        project=p if availability == ProjectAvailability.RESUMABLE else None,
        issue=issue,
        source_path=p.source_path,
    )


def clarification(p: ProjectSummary) -> ClarificationPresentation:
    location = SourceLocation(
        relative_path="sections/main.tex",
        absolute_path=p.source_path / "sections/main.tex",
        start_line=42,
        end_line=44,
        start_column=1,
        end_column=20,
        context_start_line=40,
        context_end_line=46,
        excerpt=(
            "Some context.\n"
            "\\begin{lemma}\\label{lem:key}\n"
            "The restriction is positive definite.\n"
            "\\end{lemma}\n"
            "More context.\n"
        ),
        highlighted_lines=(42, 43, 44),
        snapshot_commit="abc123",
    )
    return ClarificationPresentation(
        question_id="q-1",
        claim_id="lem:key",
        category="missing_assumption",
        headline="Clarify positive definiteness",
        explanation="The current assumptions appear to imply only semidefiniteness.",
        observed_problem="The verifier could not justify positive definiteness from the stated assumptions.",
        requested_actions=("State the missing hypothesis explicitly.",),
        possible_resolutions=("Strengthen the assumptions.", "Weaken the conclusion."),
        location=location,
        blocked_claims=("thm:child-a", "thm:child-b"),
        generated_by="deterministic-fallback",
        provenance_sha256="deadbeef",
        analysis=ClarificationAnalysis(
            status=ClarificationAnalysisStatus.AVAILABLE,
            evidence_sha256="evidence-deadbeef",
            origin=ClarificationOrigin.PROOF_WORKER,
            hypothesis=(
                "The assumptions likely establish semidefiniteness while the claim "
                "requires positive definiteness."
            ),
            confidence=ClarificationConfidence.HIGH,
            reasoning=(
                ClarificationReasoning(
                    "The stated assumptions do not exclude a nontrivial kernel.",
                    ("E-deadbeef0000001",),
                ),
            ),
            alternatives=("A missing earlier dependency may supply definiteness.",),
            uncertainties=("The intended kernel hypothesis is not stated.",),
            recommended_author_check="Confirm the missing definiteness hypothesis.",
            provider="claude_cli",
            model="fable",
            effort="xhigh",
        ),
    )


def change_plan(p: ProjectSummary) -> ChangeImpactPlan:
    return ChangeImpactPlan(
        plan_id="plan-1",
        project_path=p.project_path,
        source_path=p.source_path,
        main_file="candidate-main.tex",
        input_files=("sections/new-input.tex",),
        base_snapshot="old-snapshot",
        candidate_inventory_sha256="inventory-2",
        file_changes=(
            FileChange("sections/main.tex", FileChangeKind.MODIFIED),
            FileChange("sections/new.tex", FileChangeKind.ADDED),
        ),
        direct_claim_changes=(
            ClaimImpact("lem:key", ClaimChangeKind.STATEMENT, "sections/main.tex"),
        ),
        affected_claims=("lem:key", "thm:child-a", "thm:child-b"),
        unaffected_certificates=("lem:independent",),
        superseded_questions=("q-1",),
        task_changed=False,
        source_in_dropbox=True,
        created_at="2026-08-23T12:01:00Z",
        main_file_changed=True,
    )


def findings(p: ProjectSummary) -> FindingSummary:
    return FindingSummary(
        outcome="verified",
        detail="All selected claims have current Lean certificates.",
        verified=("lem:key", "thm:child-a"),
        reused=("lem:independent",),
        report_path=p.project_path / "VERIFICATION_REPORT.md",
        project_path=p.project_path,
    )


def failure_dependency_report(
    p: ProjectSummary,
    *,
    cycles: bool = False,
    synthetic_root: bool = False,
    retryable: bool = False,
) -> FailureDependencyReport:
    artifact = FailureArtifact(
        path=p.project_path / ".repoprover" / "runs" / "41" / "lean-build.log",
        label="Lean build log",
        sha256="a1b2c3",
        command=("lake", "env", "lean", "Proofs/LemBroken.lean"),
        exit_code=1,
    )
    incident = FailureIncident(
        incident_id=41,
        run_id=73,
        scope=FailureScope.RUN if synthetic_root else FailureScope.CLAIM,
        kind=(
            FailureKind.INFRASTRUCTURE
            if synthetic_root
            else FailureKind.CLAIM_TECHNICAL
        ),
        phase="LEAN_BUILD",
        category="lean_compile_error",
        message="Lean rejected the generated proof.",
        detail="unknown constant Spectral.bound",
        provenance="lean-build",
        claim_ids=() if synthetic_root else ("lem:broken",),
        batch_index=2,
        retryable=retryable,
        artifacts=(artifact,),
    )
    extra_nodes = tuple(
        FailureGraphNode(
            claim_id=f"lem:ok-{index:02d}",
            kind="lemma",
            source_file=f"sections/part-{index:02d}.tex",
            statement_start=10 + index,
            statement_end=12 + index,
            state="CERTIFIED",
        )
        for index in range(24)
    )
    graph_nodes = (
        FailureGraphNode(
            claim_id="thm:goal",
            kind="theorem",
            source_file="main.tex",
            statement_start=80,
            statement_end=92,
            state="BLOCKED_DEPENDENCY",
        ),
        FailureGraphNode(
            claim_id="lem:broken",
            kind="lemma",
            source_file="sections/failure.tex",
            statement_start=42,
            statement_end=49,
            state="FAILED_TECHNICAL",
            incident_ids=(41,),
        ),
        *extra_nodes,
    )
    ok_outline = tuple(
        FailureOutlineNode(
            claim_id=node.claim_id,
            state=node.state,
            blocker=False,
            incident_ids=(),
            shared_reference=index == 0,
        )
        for index, node in enumerate(extra_nodes)
    )
    goal_outline = FailureOutlineNode(
        claim_id="thm:goal",
        state="BLOCKED_DEPENDENCY",
        blocker=False,
        incident_ids=(),
        shared_reference=False,
        children=(
            FailureOutlineNode(
                claim_id="lem:broken",
                state="FAILED_TECHNICAL",
                blocker=True,
                incident_ids=(41,),
                shared_reference=False,
            ),
            *ok_outline,
        ),
    )
    outline = (
        (
            FailureOutlineNode(
                claim_id="incident:41",
                state="RUN:INFRASTRUCTURE",
                blocker=True,
                incident_ids=(41,),
                shared_reference=False,
                children=(goal_outline,),
            ),
        )
        if synthetic_root
        else (goal_outline,)
    )
    components = (
        FailureComponent(
            component_id="component:cycle",
            members=("lem:broken", "thm:goal"),
            cyclic=True,
            blocker=True,
            incident_ids=(41,),
        ),
        FailureComponent(
            component_id="component:stable",
            members=(extra_nodes[0].claim_id,),
            cyclic=False,
            blocker=False,
        ),
    )
    return FailureDependencyReport(
        run_id=73,
        snapshot="snapshot-73",
        outcome="failed",
        detail="A proof dependency could not be certified.",
        targets=("thm:goal",),
        selected=("thm:goal", "lem:broken"),
        nodes=graph_nodes,
        edges=(
            FailureGraphEdge(
                dependent="thm:goal",
                dependency="lem:broken",
                kinds=("explicit_ref",),
                provenances=("manuscript",),
            ),
        ),
        incidents=(incident,),
        global_incident_ids=(41,) if synthetic_root else (),
        primary_incident_id=41,
        first_blocker=FailurePath(
            target="thm:goal",
            blocker="lem:broken",
            claims=("thm:goal", "lem:broken"),
        ),
        paths=(
            FailurePath(
                target="thm:goal",
                blocker="lem:broken",
                claims=("thm:goal", "lem:broken"),
            ),
        ),
        has_cycles=cycles,
        outline=() if cycles else outline,
        components=components if cycles else (),
        component_edges=(
            (
                FailureComponentEdge(
                    dependent_component="component:stable",
                    dependency_component="component:cycle",
                ),
            )
            if cycles
            else ()
        ),
    )


def failed_snapshot(
    p: ProjectSummary, report: FailureDependencyReport | None
) -> WorkflowSnapshot:
    failed_project = replace(p, workflow_state=WorkflowState.FAILED)
    failed_findings = replace(
        findings(failed_project),
        outcome="failed",
        detail="Verification stopped with structured failure evidence.",
        unresolved=("thm:goal",),
        failure_report=report,
    )
    return WorkflowSnapshot(
        WorkflowState.FAILED,
        failed_project,
        findings=failed_findings,
        error="Lean compilation failed.",
    )


class FakeWorkflowService:
    """Contract-only fake: no filesystem, Git, or SQLite behavior."""

    def __init__(self) -> None:
        self.project = project()
        self.projects: tuple[ProjectCatalogEntry, ...] = (catalog_entry(self.project),)
        self.inspected: list[Path] = []
        self.folder_browse_requests: list[Path | None] = []
        self.remembered_manuscript_folders: list[Path] = []
        self.folder_browse_error: Exception | None = None
        self.folder_preference_error: Exception | None = None
        folder_home = Path("/Users/writer")
        folder_documents = folder_home / "Documents"
        self.folder_listings = {
            None: ManuscriptFolderListing(
                directory=folder_documents,
                parent=folder_home,
                home=folder_home,
                folders=(
                    ManuscriptFolderEntry("notes", folder_documents / "notes"),
                    ManuscriptFolderEntry("paper", folder_documents / "paper"),
                ),
                origin=ManuscriptFolderOrigin.PREFERENCE,
            ),
            folder_documents: ManuscriptFolderListing(
                directory=folder_documents,
                parent=folder_home,
                home=folder_home,
                folders=(
                    ManuscriptFolderEntry("notes", folder_documents / "notes"),
                    ManuscriptFolderEntry("paper", folder_documents / "paper"),
                ),
                origin=ManuscriptFolderOrigin.REQUESTED,
            ),
            folder_documents / "notes": ManuscriptFolderListing(
                directory=folder_documents / "notes",
                parent=folder_documents,
                home=folder_home,
                folders=(),
                origin=ManuscriptFolderOrigin.REQUESTED,
            ),
            folder_documents / "paper": ManuscriptFolderListing(
                directory=folder_documents / "paper",
                parent=folder_documents,
                home=folder_home,
                folders=(),
                origin=ManuscriptFolderOrigin.REQUESTED,
            ),
            folder_home: ManuscriptFolderListing(
                directory=folder_home,
                parent=Path("/Users"),
                home=folder_home,
                folders=(ManuscriptFolderEntry("Documents", folder_documents),),
                origin=ManuscriptFolderOrigin.REQUESTED,
            ),
        }
        self.inspected_destinations: list[tuple[str, Path | None]] = []
        self.inspected_deletions: list[Path] = []
        self.deleted_projects: list[Path] = []
        self.selected_main_files: list[tuple[Path, str]] = []
        self.loaded_reports: list[Path] = []
        self.loaded_failure_reports: list[tuple[Path, int | None]] = []
        self.created: list[NewProjectRequest] = []
        self.resumed: list[Path] = []
        self.planned: list[Path] = []
        self.started_jobs: list[tuple[Path, str | None, VerificationSettings]] = []
        self.observed_jobs: list[tuple[Path, int]] = []
        self.cancel_requests: list[tuple[Path, str]] = []
        self.synchronous_verifier_calls = 0
        self.job_state: VerificationJobState | None = None
        self.job_settings: VerificationSettings | None = None
        self.job_attached_legacy = False
        self.job_events: tuple[ProgressEvent, ...] = ()
        self.observation_error: Exception | None = None
        self.plan_result: ChangeImpactPlan | None = None
        self.default_settings = VerificationSettings()
        self.machine_settings = machine_settings()
        self.machine_settings_reads = 0
        self.machine_settings_projects: list[Path | None] = []
        self.settings_previews: list[MachineSettingsUpdateRequest] = []
        self.settings_applications: list[tuple[str, tuple[str, ...]]] = []
        self.settings_resets: list[int] = []
        self.settings_preview_started = threading.Event()
        self.settings_preview_release: threading.Event | None = None
        self.settings_apply_started = threading.Event()
        self.settings_apply_release: threading.Event | None = None
        self.settings_warnings: tuple[SettingsWarning, ...] = ()
        self._preview: SettingsChangePreview | None = None
        self.benchmarks: list[tuple[BenchmarkKind, Path | None, bool]] = []
        self.calibration_resets: list[Path] = []
        self.adaptive_history_resets = 0
        self.inspection = SourceInspection(
            source_path=self.project.source_path,
            candidates=(LatexSourceCandidate("main.tex", True),),
            suggested_main_file="main.tex",
            source_in_dropbox=True,
        )
        self.destination_result = ProjectDestinationInspection(
            project_path=self.project.project_path,
            availability=ProjectAvailability.AVAILABLE,
        )
        self.deletion_inspection_result = ProjectDeletionInspection(
            project_path=self.project.project_path,
            source_path=self.project.source_path,
            availability=ProjectDeletionAvailability.READY,
            source_in_dropbox=True,
        )
        self.deletion_result = ProjectDeletionResult(
            project_path=self.project.project_path,
            source_path=self.project.source_path,
            trash_path=Path(
                "/Users/writer/.local/share/proof-assistant/recoverable-trash/"
                "Paper-One-proof-assistant"
            ),
            deleted_at="2026-08-23T19:30:00Z",
        )
        self.deletion_inspection_error: Exception | None = None
        self.deletion_error: Exception | None = None
        self.creation_release: threading.Event | None = None
        self.verification_release: threading.Event | None = None
        self.create_result = WorkflowSnapshot(WorkflowState.PROJECT_READY, self.project)
        self.resume_result = WorkflowSnapshot(WorkflowState.PROJECT_READY, self.project)
        self.select_main_result = WorkflowSnapshot(
            WorkflowState.PROJECT_READY, self.project
        )
        self.report_result = ReportDocument(
            self.project.project_path / "VERIFICATION_REPORT.md",
            "# Verification report\n\n**Verified:** yes\n\n```lean\nexample : True := by trivial\n```\n",
        )
        self.report_error: Exception | None = None
        self.failure_report_result: FailureDependencyReport | None = None
        self.failure_report_error: Exception | None = None
        self.verify_result = WorkflowSnapshot(
            WorkflowState.COMPLETED,
            ProjectSummary(
                **{**self.project.__dict__, "workflow_state": WorkflowState.COMPLETED}
            ),
            findings=findings(self.project),
        )

    def default_task_text(self) -> str:
        return "Verify every claimed theorem without sorry or new axioms."

    def default_verification_settings(
        self, project: Path | None = None
    ) -> VerificationSettings:
        del project
        return self.default_settings

    def get_machine_settings(
        self, *, project: Path | None = None
    ) -> MachineSettingsSnapshot:
        self.machine_settings_projects.append(project)
        self.machine_settings_reads += 1
        return self.machine_settings

    def preview_machine_settings(
        self, request: MachineSettingsUpdateRequest
    ) -> SettingsChangePreview:
        if self.settings_preview_release is not None:
            self.settings_preview_started.set()
            if not self.settings_preview_release.wait(timeout=5):
                raise AssertionError("timed out waiting to release settings preview")
        if request.scope != SettingsScopeKind.MACHINE:
            raise ValueError("only MACHINE settings are supported")
        if request.expected_revision != self.machine_settings.revision:
            raise ValueError("machine settings revision changed; refresh and retry")
        self.settings_previews.append(request)
        configured = request.configured
        current = self.machine_settings.effective
        effective = replace(
            current,
            ai_limit=configured.ai_initial or current.ai_limit,
            ai_ceiling=configured.ai_hard_max or current.ai_ceiling,
            lean_pool=configured.lean_pool or current.lean_pool,
            lean_max=configured.lean_max or current.lean_max,
            build_limit=configured.max_builds or current.build_limit,
            agents_per_target_max=configured.agents_per_target_max,
        )
        self._preview = SettingsChangePreview(
            preview_token=f"preview-{len(self.settings_previews)}",
            requested=request,
            effective_if_applied=effective,
            warnings=self.settings_warnings,
            live_fields=("ai.limit", "lean.pool", "build.limit"),
            next_run_fields=("legacy.jobs", "legacy.batch_size"),
        )
        return self._preview

    def apply_machine_settings(
        self,
        preview_token: str,
        accepted_warning_ids: tuple[str, ...] = (),
    ) -> MachineSettingsSnapshot:
        if self.settings_apply_release is not None:
            self.settings_apply_started.set()
            if not self.settings_apply_release.wait(timeout=5):
                raise AssertionError("timed out waiting to release settings apply")
        if self._preview is None or self._preview.preview_token != preview_token:
            raise ValueError("unknown or expired preview")
        expected_warnings = {warning.warning_id for warning in self._preview.warnings}
        if not expected_warnings.issubset(accepted_warning_ids):
            raise ValueError("unsafe settings were not accepted")
        self.settings_applications.append((preview_token, accepted_warning_ids))
        self.machine_settings = replace(
            self.machine_settings,
            revision=self.machine_settings.revision + 1,
            configured=self._preview.requested.configured,
            effective=self._preview.effective_if_applied,
            legacy=self._preview.requested.legacy,
            updated_at="2026-08-23T20:01:00Z",
        )
        return self.machine_settings

    def reset_machine_settings(self, expected_revision: int) -> MachineSettingsSnapshot:
        if expected_revision != self.machine_settings.revision:
            raise ValueError("machine settings revision changed; refresh and retry")
        self.settings_resets.append(expected_revision)
        defaults = machine_settings(revision=expected_revision + 1)
        self.machine_settings = defaults
        return defaults

    def run_concurrency_benchmark(
        self,
        kind: BenchmarkKind,
        *,
        project: Path | None = None,
        allow_codex_traffic: bool = False,
    ) -> BenchmarkResult:
        self.benchmarks.append((kind, project, allow_codex_traffic))
        recommendation = {
            BenchmarkKind.CODEX: 4,
            BenchmarkKind.LEAN: 3,
            BenchmarkKind.BUILD: 1,
        }[kind]
        return BenchmarkResult(
            kind=kind,
            recommendation=recommendation,
            tested_values=(1, 2, recommendation),
            detail="Synthetic backend calibration completed.",
            used_codex_traffic=False,
            calibration_path=Path(
                f"/Users/writer/.cache/proof-assistant/concurrency/{kind.value}.json"
            ),
        )

    def reset_project_lean_calibration(self, project: Path) -> CalibrationResetResult:
        self.calibration_resets.append(project)
        return CalibrationResetResult(
            project_path=project,
            profile_id="profile-test",
            calibration_path=Path(
                "/Users/writer/.cache/repoprover-codex/concurrency/"
                "calibration/profile-test.json"
            ),
            removed=True,
        )

    def reset_adaptive_history(self) -> AdaptiveHistoryResetResult:
        self.adaptive_history_resets += 1
        return AdaptiveHistoryResetResult(
            reset_at="2026-08-23T21:00:00Z",
            ai_limit=4,
            lean_pool=3,
            build_limit=1,
        )

    def list_projects(self) -> Sequence[ProjectCatalogEntry]:
        return self.projects

    def inspect_project_destination(
        self, name: str, project_path: Path | None = None
    ) -> ProjectDestinationInspection:
        self.inspected_destinations.append((name, project_path))
        if self.destination_result.can_create and project_path is not None:
            return ProjectDestinationInspection(
                project_path=project_path,
                availability=ProjectAvailability.AVAILABLE,
            )
        return self.destination_result

    def inspect_source(self, source: Path) -> SourceInspection:
        self.inspected.append(source)
        return SourceInspection(
            source_path=source,
            candidates=self.inspection.candidates,
            suggested_main_file=self.inspection.suggested_main_file,
            source_in_dropbox=self.inspection.source_in_dropbox,
        )

    def browse_manuscript_folders(
        self, directory: Path | None = None
    ) -> ManuscriptFolderListing:
        self.folder_browse_requests.append(directory)
        if self.folder_browse_error is not None:
            raise self.folder_browse_error
        return self.folder_listings[directory]

    def remember_manuscript_folder(self, directory: Path) -> Path:
        if self.folder_preference_error is not None:
            raise self.folder_preference_error
        self.remembered_manuscript_folders.append(directory)
        return directory

    def inspect_project_deletion(self, project_path: Path) -> ProjectDeletionInspection:
        self.inspected_deletions.append(project_path)
        if self.deletion_inspection_error is not None:
            raise self.deletion_inspection_error
        return self.deletion_inspection_result

    def create_project(self, request: NewProjectRequest) -> WorkflowSnapshot:
        self.created.append(request)
        if self.creation_release is not None:
            self.creation_release.wait(timeout=3)
        return self.create_result

    def select_project_main_file(
        self, project_path: Path, main_file: str
    ) -> WorkflowSnapshot:
        self.selected_main_files.append((project_path, main_file))
        return self.select_main_result

    def load_report(self, project_path: Path) -> ReportDocument:
        self.loaded_reports.append(project_path)
        if self.report_error is not None:
            raise self.report_error
        return self.report_result

    def load_failure_report(
        self, project_path: Path, run_id: int | None = None
    ) -> FailureDependencyReport | None:
        self.loaded_failure_reports.append((project_path, run_id))
        if self.failure_report_error is not None:
            raise self.failure_report_error
        return self.failure_report_result

    def delete_project(self, project_path: Path) -> ProjectDeletionResult:
        self.deleted_projects.append(project_path)
        if self.deletion_error is not None:
            raise self.deletion_error
        self.projects = tuple(
            entry for entry in self.projects if entry.project_path != project_path
        )
        return self.deletion_result

    def resume_project(self, project_path: Path) -> WorkflowSnapshot:
        self.resumed.append(project_path)
        if self.job_state == VerificationJobState.SUCCEEDED:
            return self.verify_result
        if self.job_state == VerificationJobState.INTERRUPTED:
            interrupted = replace(
                self.project, workflow_state=WorkflowState.INTERRUPTED
            )
            return WorkflowSnapshot(
                WorkflowState.INTERRUPTED,
                interrupted,
                cancellation=CancellationReport(
                    run_id=73,
                    detail="Stopped after the current claim reached a boundary.",
                    preserved_certificates=("lem:stable", "thm:finished"),
                    retryable_claims=("thm:in-flight", "cor:dependent"),
                    temporary_worktrees_cleaned=True,
                ),
            )
        return self.resume_result

    def plan_changes(self, project_path: Path) -> ChangeImpactPlan | None:
        self.planned.append(project_path)
        return self.plan_result

    def _job(self) -> VerificationJob:
        state = self.job_state or VerificationJobState.RUNNING
        return VerificationJob(
            job_id="job-73",
            project_path=self.project.project_path,
            state=state,
            request_fingerprint="fingerprint-73",
            plan_id=(self.started_jobs[-1][1] if self.started_jobs else None),
            settings=None if self.job_attached_legacy else self.job_settings,
            created_at="2026-08-23T19:00:00Z",
            started_at="2026-08-23T19:00:01Z",
            updated_at="2026-08-23T19:00:02Z",
            completed_at=("2026-08-23T19:01:00Z" if state.terminal else None),
            heartbeat_at="2026-08-23T19:00:02Z",
            pid=7321,
            error=None,
            cancellable=not self.job_attached_legacy and not state.terminal,
            attached_legacy=self.job_attached_legacy,
        )

    def _observation(
        self,
        after_sequence: int,
        *,
        started: bool = False,
    ) -> VerificationJobObservation:
        events = (
            ()
            if self.job_attached_legacy
            else tuple(
                event for event in self.job_events if event.sequence > after_sequence
            )
        )
        next_sequence = max(
            (event.sequence for event in self.job_events), default=after_sequence
        )
        return VerificationJobObservation(
            job=self._job(),
            events=events,
            after_sequence=after_sequence,
            next_sequence=next_sequence,
            started=started,
            attached=not started,
            poll_after_seconds=0.01,
        )

    def start_verification(
        self,
        project_path: Path,
        plan_id: str | None,
        settings: VerificationSettings,
    ) -> VerificationJobObservation:
        self.started_jobs.append((project_path, plan_id, settings))
        self.job_state = VerificationJobState.RUNNING
        self.job_settings = settings
        self.job_events = (
            ProgressEvent(1, ProgressPhase.INDEXING, "Indexed sources", 1, 2),
        )
        return self._observation(0, started=True)

    def observe_verification(
        self, project_path: Path, after_sequence: int = 0
    ) -> VerificationJobObservation | None:
        self.observed_jobs.append((project_path, after_sequence))
        if self.observation_error is not None:
            raise self.observation_error
        if self.job_state is None:
            return None
        should_finish = self.verification_release is None or (
            self.verification_release.is_set()
        )
        if should_finish and not self.job_state.terminal:
            self.job_state = (
                VerificationJobState.INTERRUPTED
                if self.job_state == VerificationJobState.CANCEL_REQUESTED
                else VerificationJobState.SUCCEEDED
            )
            self.job_events = (
                *self.job_events,
                ProgressEvent(2, ProgressPhase.COMPLETE, "Finished", 2, 2),
            )
        return self._observation(after_sequence)

    def request_verification_cancel(
        self, project_path: Path, job_id: str
    ) -> VerificationJobObservation:
        self.cancel_requests.append((project_path, job_id))
        self.job_state = VerificationJobState.CANCEL_REQUESTED
        return self._observation(0)

    def confirm_and_verify(self, *args: Any, **kwargs: Any) -> WorkflowSnapshot:
        self.synchronous_verifier_calls += 1
        raise AssertionError("TUI must never call the synchronous verifier")


async def wait_for(
    pilot: Pilot[None], predicate: Callable[[], bool], *, attempts: int = 100
) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError(
        f"condition did not become true; current screen={pilot.app.screen!r}"
    )


def progress_log_contains(app: ProofAssistantApp, text: str) -> bool:
    if not isinstance(app.screen, ProgressScreen):
        return False
    nodes = app.screen.query("#progress-log").nodes
    return bool(nodes) and isinstance(nodes[0], TextArea) and text in nodes[0].text


def progress_sources_contain(app: ProofAssistantApp, text: str) -> bool:
    if not isinstance(app.screen, ProgressScreen):
        return False
    nodes = app.screen.query("#progress-sources").nodes
    return bool(nodes) and isinstance(nodes[0], TextArea) and text in nodes[0].text


def cancellation_report_is_ready(app: ProofAssistantApp) -> bool:
    """Wait for both the recovery route and its asynchronously mounted content."""

    if not isinstance(app.screen, RecoveryScreen):
        return False
    nodes = app.screen.query("#cancellation-report").nodes
    return bool(nodes) and isinstance(nodes[0], TextArea)


def button_is_ready(app: ProofAssistantApp, selector: str) -> bool:
    button = app.screen.query(selector).first()
    return button is not None and button.region.width > 0 and button.region.height > 0


def settings_home_is_ready(app: ProofAssistantApp) -> bool:
    if not isinstance(app.screen, SettingsHomeScreen):
        return False
    buttons = app.screen.query("#open-concurrency-settings").nodes
    return bool(buttons) and isinstance(buttons[0], Button) and not buttons[0].disabled


def settings_warning_is_ready(app: ProofAssistantApp) -> bool:
    return isinstance(app.screen, SettingsWarningConfirmationScreen) and bool(
        app.screen.query("#settings-warning-cancel").nodes
    )


async def activate_scrolled_button(
    pilot: Pilot[None], app: ProofAssistantApp, selector: str
) -> None:
    """Use keyboard focus so a short terminal scrolls the action into view."""

    button = app.screen.query_one(selector, Button)
    button.focus()
    await pilot.pause()
    await wait_for(pilot, lambda: button_is_ready(app, selector))
    await pilot.press("enter")


async def select_runtime_destination(
    pilot: Pilot[None], app: ProofAssistantApp, index: int
) -> None:
    """Open one peer destination in Runtime & resources."""

    navigation = app.screen.query_one("#runtime-settings-nav", OptionList)
    navigation.highlighted = index
    navigation.focus()
    await pilot.press("enter")
    await pilot.pause()


async def settle_screen(pilot: Pilot[None]) -> None:
    """Wait until the current screen's application header is mounted and laid out."""

    def header_is_ready() -> bool:
        headers = pilot.app.screen.query(AppHeader).nodes
        if not headers:
            return False
        header = headers[0]
        return bool(
            header.is_mounted
            and header.screen is pilot.app.screen
            and header.region.width > 0
        )

    await wait_for(pilot, header_is_ready)
    await pilot.pause()


def test_syntax_static_is_selectable_without_a_duplicate_source_pane() -> None:
    """Keep the highlighted excerpt selectable without rendering it twice."""

    source_path = Path(tui_screens.__file__)
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    static_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Static"
    ]
    source_excerpt_calls = [
        node
        for node in static_calls
        if any(
            keyword.arg == "id"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "source-excerpt"
            for keyword in node.keywords
        )
    ]
    assert len(source_excerpt_calls) == 1
    assert Static.ALLOW_SELECT
    assert "source-excerpt-copy" not in source_text


@async_test
async def test_application_header_renders_title_and_subtitle() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        title = app.screen.query_one(AppHeaderTitle)
        assert app.title in title.content.plain
        assert app.sub_title in title.content.plain


@async_test
async def test_application_header_survives_rapid_screen_replacement() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        retired_header = app.screen.query_one(AppHeader)

        for _ in range(4):
            app.switch_screen(WelcomeScreen(ai_setup_supported=True))
        app.title = "Replacement-safe title"

        await wait_for(pilot, lambda: not retired_header.is_attached)
        await settle_screen(pilot)
        title = app.screen.query_one(AppHeaderTitle)
        await wait_for(pilot, lambda: "Replacement-safe title" in title.content.plain)


@async_test
async def test_application_header_title_click_toggles_tall_layout() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        header = app.screen.query_one(AppHeader)
        assert not header.has_class("-tall")
        await pilot.click("AppHeaderTitle")
        await wait_for(pilot, lambda: header.has_class("-tall"))
        await pilot.click("AppHeaderTitle")
        await wait_for(pilot, lambda: not header.has_class("-tall"))


@async_test
async def test_application_header_menu_opens_without_toggling_tall() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        header = app.screen.query_one(AppHeader)
        icon = app.screen.query_one(AppHeaderIcon)
        assert icon.tooltip == "Open commands menu"
        assert icon.can_focus
        icon.focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, CommandMenuScreen))
        assert app.focused is app.screen.query_one("#command-search", Input)
        assert not header.has_class("-tall")


@async_test
async def test_permanent_footer_and_screen_aware_command_menu() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: bool(app.screen.query(CommandFooter).nodes))
        assert app.theme == PROOF_DARK_THEME.name
        assert {"ctrl+p", "ctrl+q", "ctrl+n", "ctrl+o", "ctrl+r"}.issubset(
            app.screen.active_bindings
        )
        assert not {"f1", "f2", "f3", "ctrl+t", "n", "o", "r", "s"} & set(
            app.screen.active_bindings
        )

        await wait_for(pilot, lambda: button_is_ready(app, "#resume-0"))
        await pilot.press("ctrl+p")
        await wait_for(pilot, lambda: isinstance(app.screen, CommandMenuScreen))
        assert (
            app.screen.query_one("#command-current-0", Button).label.plain
            == "Resume / open"
        )
        assert app.screen.query_one("#command-danger-heading", Static)
        assert app.screen.query_one("#command-current-1", Button).has_class(
            "command-danger"
        )
        app.screen.query_one("#command-help", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, ShortcutHelpScreen))
        reference = app.screen.query_one("#shortcut-reference", TextArea).text
        assert "Ctrl+P" in reference
        assert "Ctrl+Q" in reference
        assert "Ctrl+N" in reference
        assert "Ctrl+O" in reference
        assert "Ctrl+R" in reference
        assert "Ctrl+S" in reference
        assert "F1" not in reference
        assert "Ctrl+Enter" not in reference
        assert app.screen.query_one(CommandFooter)


@async_test
async def test_global_quit_shortcut_works_from_focused_inputs_and_modals() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)
    quit_requested = asyncio.Event()
    app.exit = lambda *args, **kwargs: quit_requested.set()  # type: ignore[method-assign]

    async with app.run_test(size=(100, 36)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_new_project()
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        await wait_for(pilot, lambda: bool(app.screen.query("#project-name").nodes))
        form_input = app.screen.query_one("#project-name", DesktopInput)
        form_input.focus()
        await pilot.press("ctrl+q")
        await wait_for(pilot, quit_requested.is_set)

        quit_requested.clear()
        await pilot.press("ctrl+p")
        await wait_for(pilot, lambda: isinstance(app.screen, CommandMenuScreen))
        await pilot.press("ctrl+q")
        await wait_for(pilot, quit_requested.is_set)
        assert isinstance(app.screen, NewProjectScreen)


@async_test
async def test_global_quit_from_resize_gate_preserves_dirty_settings_guard() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)
    quit_requested = asyncio.Event()
    app.exit = lambda *args, **kwargs: quit_requested.set()  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_concurrency_settings(service.machine_settings)
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        settings = app.screen
        await wait_for(pilot, lambda: bool(settings.query("#ai-concurrency").nodes))
        settings.query_one("#ai-concurrency", Input).value = "6"

        await pilot.resize_terminal(79, 24)
        await wait_for(pilot, lambda: isinstance(app.screen, ResizeNeededScreen))
        await pilot.press("ctrl+q")
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-unsaved-discard").nodes),
        )
        assert not quit_requested.is_set()
        app.screen.query_one("#settings-unsaved-discard", Button).press()
        await wait_for(pilot, quit_requested.is_set)


@async_test
async def test_command_menu_search_activates_match_and_escape_restores_focus() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 36)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: button_is_ready(app, "#resume-0"))
        origin = app.screen
        resume = origin.query_one("#resume-0", Button)
        await wait_for(pilot, lambda: app.focused is resume)

        await pilot.press("ctrl+p")
        await wait_for(pilot, lambda: isinstance(app.screen, CommandMenuScreen))
        search = app.screen.query_one("#command-search", Input)
        await pilot.press("x", "y", "z")
        await pilot.press("escape")
        await wait_for(pilot, lambda: app.screen is origin)
        assert app.focused is resume

        await pilot.press("ctrl+p")
        await wait_for(pilot, lambda: isinstance(app.screen, CommandMenuScreen))
        search = app.screen.query_one("#command-search", Input)
        assert app.focused is search
        search.value = "new project"
        await pilot.pause()
        assert app.screen.query_one("#command-help", Button).display is False
        assert app.screen.query_one("#command-current-2", Button).display is True
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))

        app.action_toggle_proof_theme()
        assert app.theme == PROOF_LIGHT_THEME.name
        assert app.get_css_variables()["proof-page-background"] == "#FFFCF0"

        await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        assert app.theme == PROOF_LIGHT_THEME.name
        assert app.screen.query_one(CommandFooter)


@async_test
async def test_desktop_text_widgets_remove_legacy_keys_and_keep_select_all() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 36)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await pilot.press("ctrl+n")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        name = app.screen.query_one("#project-name", Input)
        name.focus()
        await pilot.press("question_mark")
        assert name.value == "?"
        assert isinstance(app.screen, NewProjectScreen)

        await pilot.press("ctrl+a")
        assert name.selection.start == 0
        assert name.selection.end == len(name.value)

        forbidden = {"f6", "f7", "ctrl+e", "ctrl+d", "ctrl+w", "ctrl+u", "ctrl+k"}
        for widget_type in (DesktopInput, DesktopTextArea):
            keys = {
                key
                for binding in widget_type.BINDINGS
                for key in binding.key.split(",")
            }
            assert not forbidden & keys
            assert not any(key.startswith("alt+") for key in keys)

        assert CopyableText("Page title", classes="title").can_focus is False
        assert CopyableText("Useful value").can_focus is True


def test_application_bindings_have_no_collisions_or_legacy_accelerators() -> None:
    forbidden_actions = {"cancel_job", "confirm", "failures", "report", "verify"}
    for module in (tui_screens, tui_settings_screens):
        for value in vars(module).values():
            if (
                not isinstance(value, type)
                or value.__module__ != module.__name__
                or "BINDINGS" not in value.__dict__
            ):
                continue
            seen: set[str] = set()
            for binding in value.BINDINGS:
                key_spec = binding[0] if isinstance(binding, tuple) else binding.key
                action = binding[1] if isinstance(binding, tuple) else binding.action
                keys = tuple(key.strip() for key in key_spec.split(","))
                for key in keys:
                    assert key not in seen, f"{value.__name__} binds {key} twice"
                    seen.add(key)
                    assert not (len(key) == 1 and key.isalpha())
                    assert key not in {"[", "]", "ctrl+enter"}
                    assert not key.startswith("alt+")
                    assert not (key.startswith("f") and key[1:].isdigit())
                assert action not in forbidden_actions


@async_test
async def test_menu_navigation_preserves_draft_and_uses_no_function_keys() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 36)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        assert not any(
            key.startswith("f") and key[1:].isdigit()
            for key in app.screen.active_bindings
        )
        await pilot.press("ctrl+n")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        draft_screen = app.screen
        name = draft_screen.query_one("#project-name", Input)
        name.value = "A preserved draft"
        name.focus()

        await pilot.press("ctrl+p")
        await wait_for(pilot, lambda: isinstance(app.screen, CommandMenuScreen))
        app.screen.query_one("#command-settings", Button).press()
        await wait_for(pilot, lambda: settings_home_is_ready(app))
        app.screen.query_one("#settings-back", Button).press()
        await wait_for(pilot, lambda: app.screen is draft_screen)
        assert name.value == "A preserved draft"

        await pilot.press("ctrl+p")
        await wait_for(pilot, lambda: isinstance(app.screen, CommandMenuScreen))
        app.screen.query_one("#command-projects", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))


@async_test
async def test_settings_preserves_observer_and_main_menu_detaches_only_client() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(110, 35)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.start_verification(service.project, None)
        await wait_for(pilot, lambda: progress_log_contains(app, "INDEXING"))
        progress = app.screen
        assert isinstance(progress, ProgressScreen)
        observer = app._observer_worker
        observation = app._active_observation
        assert observer is not None
        assert observation is not None

        app.action_global_settings()
        await wait_for(pilot, lambda: settings_home_is_ready(app))
        await wait_for(pilot, lambda: app._active_observation is not observation)
        assert service.cancel_requests == []
        assert app._observer_worker is observer
        current_observation = app._active_observation
        assert current_observation is not None
        assert current_observation.job.job_id == observation.job.job_id
        assert (
            current_observation.job.request_fingerprint
            == observation.job.request_fingerprint
        )
        assert current_observation.next_sequence >= observation.next_sequence
        assert app._progress_screen is progress

        app.screen.query_one("#settings-back", Button).press()
        await wait_for(pilot, lambda: app.screen is progress)
        app.action_main_menu()
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        assert app._observer_worker is None
        assert app._active_observation is None
        assert app._progress_screen is None

        # A late terminal result from the detached observer is identity-guarded
        # and cannot steal the landing screen.
        service.verification_release.set()
        await pilot.pause(0.05)
        assert isinstance(app.screen, WelcomeScreen)
        assert service.cancel_requests == []


@async_test
async def test_terminal_folder_picker_traverses_and_persists_only_on_select() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)
    documents = Path("/Users/writer/Documents")
    notes = documents / "notes"

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await pilot.click("#new-project")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        await pilot.click("#browse-source")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ManuscriptFolderPickerScreen)
                and app.screen.listing is not None
            ),
        )
        assert app.screen.listing is not None
        assert app.screen.listing.directory == documents
        assert service.folder_browse_requests == [None]
        assert service.remembered_manuscript_folders == []

        current = app.screen.query_one("#folder-picker-current", TextArea)
        assert current.read_only
        current.select_all()
        assert str(documents) in current.selected_text
        assert "saved manuscript-folder preference" in current.selected_text

        table = app.screen.query_one("#folder-picker-table", DataTable)
        table.move_cursor(row=0, column=0, animate=False)
        await pilot.pause()
        selection = app.screen.query_one("#folder-picker-selection", TextArea)
        await pilot.press("down")
        await pilot.pause()
        assert str(documents / "paper") in selection.text
        await pilot.press("up")
        await pilot.pause()
        selection.select_all()
        assert str(notes) in selection.selected_text
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ManuscriptFolderPickerScreen)
                and app.screen.listing is not None
                and app.screen.listing.directory == notes
            ),
        )
        assert service.remembered_manuscript_folders == []

        await pilot.click("#folder-picker-parent")
        await wait_for(
            pilot,
            lambda: (
                app.screen.listing is not None
                and app.screen.listing.directory == documents
            ),
        )
        await pilot.click("#folder-picker-home")
        await wait_for(
            pilot,
            lambda: (
                app.screen.listing is not None
                and app.screen.listing.directory == Path("/Users/writer")
            ),
        )
        app.screen.query_one("#folder-picker-table", DataTable).move_cursor(
            row=0, column=0, animate=False
        )
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                app.screen.listing is not None
                and app.screen.listing.directory == documents
            ),
        )
        await pilot.click("#folder-picker-use")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        assert app.screen.query_one("#source-path", Input).value == str(documents)
        assert service.remembered_manuscript_folders == [documents]


@async_test
async def test_terminal_folder_picker_cancel_and_browse_error_are_safe() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await pilot.click("#new-project")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        source = app.screen.query_one("#source-path", Input)
        source.value = "/manual/path"
        await pilot.click("#browse-source")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ManuscriptFolderPickerScreen)
                and app.screen.listing is not None
            ),
        )
        await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        assert app.screen.query_one("#source-path", Input).value == "/manual/path"
        assert service.remembered_manuscript_folders == []

        service.folder_browse_error = PermissionError("folder is not readable")
        await pilot.click("#browse-source")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ManuscriptFolderPickerScreen)
                and "not readable"
                in app.screen.query_one("#status-line", TextArea).text
            ),
        )
        error = app.screen.query_one("#status-line", TextArea)
        error.select_all()
        assert "not readable" in error.selected_text
        assert service.remembered_manuscript_folders == []
        await pilot.click("#folder-picker-cancel")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        assert app.screen.query_one("#source-path", Input).value == "/manual/path"


@async_test
async def test_new_project_custom_task_starts_first_verification() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 50)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await pilot.click("#new-project")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))

        app.screen.query_one("#project-name", Input).value = "spectral-paper"
        app.screen.query_one(
            "#source-path", Input
        ).value = "/Users/writer/Dropbox/paper"
        app.screen.query_one(
            "#project-path", Input
        ).value = "/Users/writer/proof-assistant/spectral"
        await pilot.click("#custom-task")
        app.screen.query_one(
            "#task-editor", TextArea
        ).text = "Verify the main spectral theorem."
        await pilot.click("#continue")
        await wait_for(pilot, lambda: isinstance(app.screen, ProjectReviewScreen))
        assert service.created == []
        review = app.screen.query_one("#project-review", TextArea).text
        assert "Main LaTeX file: main.tex" in review
        assert "automatically selected" in review
        await pilot.click("#confirm-create")

        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        await settle_screen(pilot)
        assert len(service.created) == 1
        request = service.created[0]
        assert request.name == "spectral-paper"
        assert request.source_path == Path("/Users/writer/Dropbox/paper")
        assert request.main_file == "main.tex"
        assert request.project_path == Path("/Users/writer/proof-assistant/spectral")
        assert request.task_text == "Verify the main spectral theorem."
        assert service.inspected == [Path("/Users/writer/Dropbox/paper")]
        assert service.started_jobs[0][1] is None
        assert service.synchronous_verifier_calls == 0
        assert app.screen.query_one("#dropbox-warning", TextArea)
        assert "All selected claims" in str(
            app.screen.query_one("#findings-detail", TextArea).text
        )


@async_test
async def test_report_opens_in_terminal_with_rendered_and_copyable_content() -> None:
    service = FakeWorkflowService()
    opened: list[Path] = []
    app = ProofAssistantApp(service, location_opener=opened.append)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_snapshot(service.verify_result)
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        await settle_screen(pilot)
        app.screen.query_one("#open-report", Button).press()
        await pilot.pause()
        await wait_for(pilot, lambda: isinstance(app.screen, ReportViewerScreen))
        await wait_for(pilot, lambda: bool(app.screen.query("#report-source").nodes))

        assert service.loaded_reports == [service.project.project_path]
        assert opened == []
        path = app.screen.query_one("#report-path", TextArea)
        assert path.read_only
        assert str(service.report_result.path) in path.text
        viewer = app.screen.query_one("#report-markdown", MarkdownViewer)
        await wait_for(
            pilot,
            lambda: (
                bool(viewer.document.query("MarkdownH1").nodes)
                and bool(viewer.document.query("MarkdownFence").nodes)
            ),
        )
        tabs = app.screen.query_one("#report-tabs", TabbedContent)
        tabs.active = "report-source-pane"
        await pilot.pause()
        source = app.screen.query_one("#report-source", TextArea)
        assert source.read_only
        assert source.text == service.report_result.markdown
        source.select_all()
        assert "example : True" in source.selected_text
        tabs.active = "report-rendered-pane"
        await pilot.pause()
        viewer.focus()
        await pilot.press("pagedown")

        app.screen.query_one("#back", Button).press()
        await pilot.pause()
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        await settle_screen(pilot)


@async_test
async def test_report_read_error_is_copyable_and_stays_in_terminal() -> None:
    service = FakeWorkflowService()
    report_path = service.project.project_path / "VERIFICATION_REPORT.md"
    service.report_error = RuntimeError(
        f"Could not read verification report: {report_path}: Permission denied"
    )
    opened: list[Path] = []
    app = ProofAssistantApp(service, location_opener=opened.append)

    async with app.run_test(size=(110, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_snapshot(service.verify_result)
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        await settle_screen(pilot)
        app.screen.query_one("#open-report", Button).press()
        await pilot.pause()
        await wait_for(pilot, lambda: isinstance(app.screen, ReportViewerScreen))
        error = app.screen.query_one("#report-error", TextArea)
        assert error.read_only
        assert str(report_path) in error.text
        assert "Permission denied" in error.text
        error.select_all()
        assert "Could not read verification report" in error.selected_text
        assert opened == []


@async_test
async def test_acyclic_failure_report_uses_tree_with_copyable_exact_evidence() -> None:
    service = FakeWorkflowService()
    report = failure_dependency_report(service.project)
    snapshot = failed_snapshot(service.project, report)
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_snapshot(snapshot)
        await wait_for(pilot, lambda: isinstance(app.screen, FailureDependencyScreen))
        await settle_screen(pilot)

        assert app.screen.query_one("#failure-retry", Button).disabled
        assert not app.screen.check_action("retry", ())
        tree = app.screen.query_one("#failure-tree", FailureTree)
        assert not app.screen.query("#failure-components").nodes
        assert tree.size.height >= 6
        assert tree.virtual_size.height > tree.size.height
        labels = [tree.root.label.plain]

        def collect_labels(node) -> None:
            for child in node.children:
                labels.append(child.label.plain)
                collect_labels(child)

        collect_labels(tree.root)
        assert any("[FAIL] lem:broken" in label for label in labels)
        assert any("[BLOCKED] thm:goal" in label for label in labels)
        assert any("[OK] lem:ok-00 (shared reference)" in label for label in labels)

        tree.focus()
        await pilot.press("pagedown")
        assert tree.cursor_line > 0
        assert tree.scroll_offset.y > 0

        failed_node = tree.root.children[0].children[0]
        tree.move_cursor(failed_node, animate=False)
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                app.screen.query_one("#failure-tabs", TabbedContent).active
                == "failure-detail-pane"
            ),
        )
        detail = app.screen.query_one("#failure-detail", TextArea)
        assert detail.read_only
        assert "unknown constant Spectral.bound" in detail.text
        assert "sections/failure.tex" in detail.text
        assert "Statement lines: 42-49" in detail.text
        assert "Lean build log" in detail.text
        assert str(report.incidents[0].artifacts[0].path) in detail.text
        assert "lake env lean Proofs/LemBroken.lean" in detail.text
        detail.focus()
        await pilot.press("ctrl+a")
        assert "unknown constant Spectral.bound" in detail.selected_text

        tabs = app.screen.query_one("#failure-tabs", TabbedContent)
        tabs.active = "failure-outline-pane"
        await pilot.pause()
        outline = app.screen.query_one("#failure-outline", TextArea)
        outline.focus()
        await pilot.press("ctrl+a")
        assert "[FAIL] lem:broken" in outline.selected_text
        assert "[BLOCKED] thm:goal" in outline.selected_text
        assert "[OK] lem:ok-00" in outline.selected_text
        assert "shared reference" in outline.selected_text
        assert str(report.incidents[0].artifacts[0].path) in outline.selected_text

        app.screen.query_one("#failure-back", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        await settle_screen(pilot)


@async_test
async def test_synthetic_global_failure_root_opens_exact_incident() -> None:
    service = FakeWorkflowService()
    report = failure_dependency_report(service.project, synthetic_root=True)
    snapshot = failed_snapshot(service.project, report)
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_snapshot(snapshot)
        await wait_for(pilot, lambda: isinstance(app.screen, FailureDependencyScreen))
        await settle_screen(pilot)

        tree = app.screen.query_one("#failure-tree", FailureTree)
        synthetic = tree.root.children[0]
        assert "[FAIL] incident:41" in synthetic.label.plain
        tree.move_cursor(synthetic, animate=False)
        await pilot.press("enter")
        detail = app.screen.query_one("#failure-detail", TextArea)
        assert "Run/batch failure node: incident:41" in detail.text
        assert "not owned by one manuscript claim" in detail.text
        assert "Lean rejected the generated proof" in detail.text
        assert "Verifier state: not available" not in detail.text
        meta = app.screen.query_one("#failure-report-meta", TextArea)
        assert "Incidents: 41" in meta.text


@async_test
async def test_retryable_failure_can_start_a_new_verification_run() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    report = failure_dependency_report(
        service.project,
        synthetic_root=True,
        retryable=True,
    )
    snapshot = failed_snapshot(service.project, report)
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 32)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_snapshot(snapshot)
        await wait_for(pilot, lambda: isinstance(app.screen, FailureDependencyScreen))
        await settle_screen(pilot)

        retry = app.screen.query_one("#failure-retry", Button)
        assert not retry.disabled
        assert app.screen.check_action("retry", ())
        retry.press()

        await wait_for(pilot, lambda: bool(service.started_jobs))
        assert service.started_jobs[0][0] == service.project.project_path
        assert service.started_jobs[0][1] is None
        assert isinstance(app.screen, ProgressScreen)

        service.verification_release.set()
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))


@async_test
async def test_cyclic_failure_report_uses_component_fallback_not_tree() -> None:
    service = FakeWorkflowService()
    report = failure_dependency_report(service.project, cycles=True)
    snapshot = failed_snapshot(service.project, report)
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_snapshot(snapshot)
        await wait_for(pilot, lambda: isinstance(app.screen, FailureDependencyScreen))
        await settle_screen(pilot)

        assert not app.screen.query(FailureTree).nodes
        table = app.screen.query_one("#failure-components", DataTable)
        assert table.size.height >= 5
        meta = app.screen.query_one("#failure-report-meta", TextArea)
        assert meta.read_only
        assert "[CYCLE] Flat components/edges; no inferred tree" in meta.text

        table.focus()
        table.move_cursor(row=1, column=0, animate=False)
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                app.screen.query_one("#failure-tabs", TabbedContent).active
                == "failure-detail-pane"
            ),
        )
        detail = app.screen.query_one("#failure-detail", TextArea)
        assert "Claim: lem:broken" in detail.text
        assert "unknown constant Spectral.bound" in detail.text

        tabs = app.screen.query_one("#failure-tabs", TabbedContent)
        tabs.active = "failure-outline-pane"
        await pilot.pause()
        outline = app.screen.query_one("#failure-outline", TextArea)
        outline.focus()
        await pilot.press("ctrl+a")
        assert "Cycle components (backend-computed)" in outline.selected_text
        assert "component:stable -> component:cycle" in outline.selected_text
        assert "[FAIL] component:cycle" in outline.selected_text


@async_test
async def test_failed_findings_without_embedded_report_do_not_route_to_recovery() -> (
    None
):
    service = FakeWorkflowService()
    snapshot = failed_snapshot(service.project, None)
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_snapshot(snapshot)
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        await settle_screen(pilot)
        assert not isinstance(app.screen, RecoveryScreen)
        button = app.screen.query_one("#open-failures", Button)
        assert button.label.plain == "Load failure analysis"
        button.press()
        await wait_for(pilot, lambda: isinstance(app.screen, FailureDependencyScreen))
        await wait_for(
            pilot, lambda: bool(app.screen.query("#failure-report-error").nodes)
        )
        await settle_screen(pilot)
        error = app.screen.query_one("#failure-report-error", TextArea)
        assert error.read_only
        assert str(service.project.project_path) in error.text
        assert "No failure report is available" in error.text
        error.focus()
        await pilot.press("ctrl+a")
        assert str(service.project.project_path) in error.selected_text
        assert service.loaded_failure_reports == [(service.project.project_path, None)]


@async_test
async def test_default_task_is_backend_owned() -> None:
    service = FakeWorkflowService()
    service.creation_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 50)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await pilot.click("#new-project")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        app.screen.query_one("#project-name", Input).value = "paper"
        app.screen.query_one("#source-path", Input).value = "/source"
        assert app.screen.query_one("#task-editor", TextArea).text == (
            service.default_task_text()
        )
        await pilot.click("#continue")
        await wait_for(pilot, lambda: isinstance(app.screen, ProjectReviewScreen))
        assert service.created == []
        assert "automatically selected" in str(
            app.screen.query_one("#project-review", TextArea).text
        )
        await pilot.click("#confirm-create")
        await wait_for(
            pilot,
            lambda: progress_sources_contain(app, "Main file: main.tex"),
        )
        assert not isinstance(app.screen, MainFileSelectionScreen)
        source_detail = app.screen.query_one("#progress-sources", TextArea)
        assert source_detail.read_only
        assert "Main file: main.tex" in source_detail.text
        service.creation_release.set()
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        await settle_screen(pilot)

        assert service.created[0].task_text is None
        assert (
            service.created[0].project_path == service.destination_result.project_path
        )
        assert service.created[0].main_file == "main.tex"


@async_test
async def test_multiple_latex_files_require_deliberate_main_selection() -> None:
    service = FakeWorkflowService()
    service.inspection = SourceInspection(
        source_path=Path("/source"),
        candidates=(
            LatexSourceCandidate("appendix.tex", False),
            LatexSourceCandidate("paper.tex", True),
            LatexSourceCandidate("slides.tex", True),
        ),
        suggested_main_file="paper.tex",
        source_in_dropbox=False,
    )
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 50)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await pilot.click("#new-project")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        app.screen.query_one("#project-name", Input).value = "multi-root"
        app.screen.query_one("#source-path", Input).value = "/source"
        await pilot.click("#continue")
        await wait_for(pilot, lambda: isinstance(app.screen, MainFileSelectionScreen))

        # A suggestion is visible but is intentionally not an implicit choice.
        assert "suggested" in str(app.screen.query_one("#main-option-1").label)
        candidates = app.screen.query_one("#main-file-candidates-copy", TextArea)
        candidates.select_all()
        assert "appendix.tex" in candidates.selected_text
        assert "paper.tex" in candidates.selected_text
        assert "slides.tex" in candidates.selected_text
        assert "suggested" in candidates.selected_text
        await pilot.click("#select-main")
        assert service.created == []
        assert "Select one main" in str(
            app.screen.query_one("#status-line", TextArea).text
        )

        await pilot.click("#main-option-2")
        await wait_for(
            pilot,
            lambda: app.screen.query_one("#main-option-2", RadioButton).value,
        )
        await pilot.click("#select-main")
        await wait_for(pilot, lambda: isinstance(app.screen, ProjectReviewScreen))
        assert service.created == []
        review = app.screen.query_one("#project-review", TextArea).text
        assert "Main LaTeX file: slides.tex" in review
        assert "selected by user" in review
        await pilot.click("#confirm-create")
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        await settle_screen(pilot)
        assert service.created[0].main_file == "slides.tex"


@async_test
async def test_new_project_wizard_back_preserves_draft_and_selection() -> None:
    service = FakeWorkflowService()
    service.inspection = SourceInspection(
        source_path=Path("/source"),
        candidates=(
            LatexSourceCandidate("paper.tex", True),
            LatexSourceCandidate("supplement.tex", True),
        ),
        suggested_main_file="paper.tex",
        source_in_dropbox=False,
    )
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 50)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await pilot.click("#new-project")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        app.screen.query_one("#project-name", Input).value = "preserved-paper"
        app.screen.query_one("#source-path", Input).value = "/source"
        app.screen.query_one("#project-path", Input).value = "/projects/preserved"
        await pilot.click("#custom-task")
        app.screen.query_one("#task-editor", TextArea).text = "Verify the key result."

        await pilot.click("#continue")
        await wait_for(pilot, lambda: isinstance(app.screen, MainFileSelectionScreen))
        await pilot.click("#back")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        assert app.screen.query_one("#project-name", Input).value == "preserved-paper"
        assert app.screen.query_one("#source-path", Input).value == "/source"
        assert app.screen.query_one("#project-path", Input).value == (
            "/projects/preserved"
        )
        task_editor = app.screen.query_one("#task-editor", TextArea)
        assert not task_editor.disabled
        assert task_editor.text == "Verify the key result."

        await pilot.click("#continue")
        await wait_for(pilot, lambda: isinstance(app.screen, MainFileSelectionScreen))
        await pilot.click("#main-option-1")
        await pilot.click("#select-main")
        await wait_for(pilot, lambda: isinstance(app.screen, ProjectReviewScreen))
        assert service.created == []
        await pilot.click("#review-back")
        await wait_for(pilot, lambda: isinstance(app.screen, MainFileSelectionScreen))
        assert app.screen.query_one("#main-option-1").value
        await pilot.click("#back")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        assert app.screen.query_one("#project-name", Input).value == "preserved-paper"
        assert app.screen.query_one("#task-editor", TextArea).text == (
            "Verify the key result."
        )
        assert service.created == []
        await settle_screen(pilot)


@async_test
async def test_legacy_catalog_project_selects_main_and_recovers() -> None:
    service = FakeWorkflowService()
    legacy_path = Path("/projects/legacy-paper")
    service.projects = (
        ProjectCatalogEntry(
            name="Legacy Paper",
            project_path=legacy_path,
            availability=ProjectAvailability.NEEDS_MAIN_FILE,
            issue="Legacy project has no persisted main file.",
            source_path=Path("/source/legacy"),
            main_file_candidates=(
                LatexSourceCandidate("article.tex", True),
                LatexSourceCandidate("alternate.tex", True),
            ),
            suggested_main_file="article.tex",
        ),
    )
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 45)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#select-existing-main-0").nodes),
        )
        catalog_text = "\n".join(
            widget.text
            for widget in app.screen.query(TextArea).filter(".project-summary")
        )
        assert "Legacy Paper" in catalog_text
        assert "NEEDS_MAIN_FILE" in catalog_text
        assert "no persisted main file" in catalog_text

        app.screen.query_one("#select-existing-main-0", Button).press()
        await pilot.pause()
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, ExistingProjectMainFileSelectionScreen),
        )
        assert service.selected_main_files == []
        assert "suggested" in str(app.screen.query_one("#existing-main-option-0").label)
        candidates = app.screen.query_one(
            "#existing-main-file-candidates-copy", TextArea
        )
        candidates.select_all()
        assert "article.tex" in candidates.selected_text
        assert "alternate.tex" in candidates.selected_text
        assert "suggested" in candidates.selected_text
        await pilot.click("#existing-main-option-1")
        await pilot.click("#confirm-existing-main")
        await wait_for(
            pilot, lambda: app.screen.__class__.__name__ == "DashboardScreen"
        )
        assert service.selected_main_files == [(legacy_path, "alternate.tex")]
        await settle_screen(pilot)


@async_test
async def test_occupied_and_incomplete_catalog_entries_remain_visible() -> None:
    service = FakeWorkflowService()
    occupied = ProjectCatalogEntry(
        name="Occupied Folder",
        project_path=Path("/projects/occupied"),
        availability=ProjectAvailability.OCCUPIED,
        issue="Folder contains unrelated files.",
    )
    incomplete = ProjectCatalogEntry(
        name="Incomplete Project",
        project_path=Path("/projects/incomplete"),
        availability=ProjectAvailability.INCOMPLETE,
        issue="Managed metadata is incomplete.",
    )
    service.projects = (occupied, incomplete)
    opened: list[Path] = []
    app = ProofAssistantApp(service, location_opener=opened.append)

    async with app.run_test(size=(120, 45)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(
            pilot, lambda: len(app.screen.query(".project-summary").nodes) == 2
        )
        catalog_text = "\n".join(
            widget.text
            for widget in app.screen.query(TextArea).filter(".project-summary")
        )
        assert "OCCUPIED" in catalog_text
        assert "Folder contains unrelated files" in catalog_text
        assert "INCOMPLETE" in catalog_text
        assert "Managed metadata is incomplete" in catalog_text
        assert all(
            not (button.id or "").startswith("resume-")
            for button in app.screen.query(Button)
        )
        app.screen.query_one("#open-catalog-0", Button).press()
        await pilot.pause()
        assert opened == [Path("/projects/occupied")]


@async_test
async def test_project_deletion_is_cancel_first_button_confirmed_and_recoverable() -> (
    None
):
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: bool(app.screen.query("#delete-project-0").nodes))
        delete_button = app.screen.query_one("#delete-project-0", Button)
        assert delete_button.label.plain == "Delete project"
        delete_button.press()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ProjectDeletionConfirmationScreen)
                and bool(app.screen.query("#delete-project-paths").nodes)
                and bool(app.screen.query("#delete-project-cancel").nodes)
                and bool(app.screen.query("#delete-project-confirm").nodes)
            ),
        )

        paths = app.screen.query_one("#delete-project-paths", TextArea)
        assert paths.read_only
        assert str(service.project.project_path) in paths.text
        assert str(service.project.source_path) in paths.text
        assert "untouched" in paths.text
        safety = app.screen.query_one("#delete-project-safety", TextArea)
        safety.select_all()
        assert "recoverable deletion storage" in safety.selected_text
        assert "until you manually remove" in safety.selected_text
        assert "will not be changed, moved, or deleted" in safety.selected_text
        cancel = app.screen.query_one("#delete-project-cancel", Button)
        await wait_for(pilot, lambda: app.focused is cancel)
        assert app.focused is cancel

        # Enter activates the deliberately focused safe action.
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        assert service.deleted_projects == []

        app.screen.query_one("#delete-project-0", Button).press()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ProjectDeletionConfirmationScreen)
                and bool(app.screen.query("#delete-project-confirm").nodes)
            ),
        )
        destructive = app.screen.query_one("#delete-project-confirm", Button)
        assert not destructive.disabled
        assert destructive.label.plain == "Delete managed project (recoverable)"
        assert not app.screen.query("#delete-project-confirmation").nodes
        destructive.press()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ProjectDeletionOutcomeScreen)
        )
        await wait_for(
            pilot,
            lambda: (
                bool(app.screen.query("#delete-project-result").nodes)
                and bool(app.screen.query("#deletion-projects").nodes)
            ),
        )
        result = app.screen.query_one("#delete-project-result", TextArea)
        assert result.read_only
        result.select_all()
        assert str(service.deletion_result.trash_path) in result.selected_text
        assert str(service.project.source_path) in result.selected_text
        assert "Recoverable deletion destination" in result.selected_text
        assert "Recoverable: yes" in result.selected_text
        assert service.deleted_projects == [service.project.project_path]

        app.screen.query_one("#deletion-projects", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(
            pilot,
            lambda: (
                bool(app.screen.query("#project-list TextArea").nodes)
                and "No projects yet"
                in "\n".join(
                    widget.text for widget in app.screen.query("#project-list TextArea")
                )
            ),
        )
        assert all(
            not (button.id or "").startswith("delete-project-")
            for button in app.screen.query(Button)
        )


@async_test
async def test_project_deletion_refusal_and_failure_are_copyable() -> None:
    service = FakeWorkflowService()
    service.deletion_inspection_result = ProjectDeletionInspection(
        project_path=service.project.project_path,
        source_path=service.project.source_path,
        availability=ProjectDeletionAvailability.BUSY,
        issue="A backend verification is currently active for this project.",
        source_in_dropbox=True,
    )
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: bool(app.screen.query("#delete-project-0").nodes))
        app.screen.query_one("#delete-project-0", Button).press()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ProjectDeletionConfirmationScreen)
                and bool(app.screen.query("#delete-project-issue").nodes)
                and bool(app.screen.query("#delete-project-confirm").nodes)
            ),
        )
        issue = app.screen.query_one("#delete-project-issue", TextArea)
        issue.select_all()
        assert "backend verification is currently active" in issue.selected_text
        assert not app.screen.query("#delete-project-confirmation").nodes
        assert app.screen.query_one("#delete-project-confirm", Button).disabled
        await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        assert service.deleted_projects == []

        service.deletion_inspection_result = ProjectDeletionInspection(
            project_path=service.project.project_path,
            source_path=service.project.source_path,
            availability=ProjectDeletionAvailability.READY,
            source_in_dropbox=True,
        )
        service.deletion_error = RuntimeError(
            "Delete-time preflight refused because the project became busy."
        )
        app.screen.query_one("#delete-project-0", Button).press()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ProjectDeletionConfirmationScreen)
                and bool(app.screen.query("#delete-project-confirm").nodes)
            ),
        )
        app.screen.query_one("#delete-project-confirm", Button).press()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ProjectDeletionOutcomeScreen)
        )
        await wait_for(
            pilot,
            lambda: (
                bool(app.screen.query("#delete-project-error").nodes)
                and bool(app.screen.query("#deletion-retry").nodes)
            ),
        )
        error = app.screen.query_one("#delete-project-error", TextArea)
        error.select_all()
        assert "became busy" in error.selected_text
        assert str(service.project.project_path) in error.selected_text
        assert str(service.project.source_path) in error.selected_text
        assert "untouched" in error.selected_text
        assert app.screen.query_one("#deletion-retry", Button)


@async_test
async def test_destination_conflict_stops_before_source_inspection_or_creation() -> (
    None
):
    service = FakeWorkflowService()
    conflict_path = Path("/projects/already-used")
    service.destination_result = ProjectDestinationInspection(
        project_path=conflict_path,
        availability=ProjectAvailability.OCCUPIED,
        issue="Destination contains unrelated user files.",
    )
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 45)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await pilot.click("#new-project")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        app.screen.query_one("#project-name", Input).value = "Conflict Paper"
        app.screen.query_one("#source-path", Input).value = "/source/paper"
        app.screen.query_one("#project-path", Input).value = str(conflict_path)
        await pilot.click("#continue")
        await wait_for(
            pilot, lambda: isinstance(app.screen, ProjectDestinationConflictScreen)
        )

        detail = app.screen.query_one("#destination-conflict", TextArea).text
        assert str(conflict_path) in detail
        assert "OCCUPIED" in detail
        assert "unrelated user files" in detail
        assert service.inspected_destinations == [("Conflict Paper", conflict_path)]
        assert service.inspected == []
        assert service.created == []
        await pilot.click("#back")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen))
        assert app.screen.query_one("#project-name", Input).value == "Conflict Paper"
        assert app.screen.query_one("#source-path", Input).value == "/source/paper"
        assert app.screen.query_one("#project-path", Input).value == str(conflict_path)
        assert service.created == []
        await pilot.click("#cancel")
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)


@async_test
async def test_verification_progress_lists_copyable_sources_and_typed_stages() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.start_verification(service.project, None)
        await wait_for(
            pilot,
            lambda: progress_log_contains(app, "INDEXING"),
        )

        sources = app.screen.query_one("#progress-sources", TextArea)
        stages = app.screen.query_one("#progress-stages", TextArea)
        event_log = app.screen.query_one("#progress-log", TextArea)
        assert sources.read_only and stages.read_only and event_log.read_only
        assert "Main file: main.tex" in sources.text
        assert "sections/introduction.tex" in sources.text
        assert "sections/results.tex" in sources.text
        assert "Current stage: INDEXING" in stages.text
        assert "PROOF_BATCH" in stages.text
        assert "CERTIFICATION" in stages.text
        assert "0001 INDEXING: Indexed sources" in event_log.text

        # TextArea is the explicit in-app copy mechanism: selection remains
        # available even though edits are disabled.
        sources.select_all()
        assert "Main file: main.tex" in sources.selected_text

        service.verification_release.set()
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        await settle_screen(pilot)


@async_test
async def test_progress_bar_uses_typed_phases_and_never_regresses() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await pilot.pause(0.1)
        screen = ProgressScreen(
            "Verifying manuscript",
            project=service.project.project_path,
            main_file=service.project.main_file,
            input_files=service.project.input_files,
        )
        app.switch_screen(screen)
        await wait_for(pilot, lambda: bool(screen.query("#progress-bar").nodes))
        bar = screen.query_one("#progress-bar")

        # Real backend phase events often omit per-unit counts.
        screen.record_progress(
            ProgressEvent(1, ProgressPhase.INDEXING, "Indexing selected closure")
        )
        assert 27.0 < bar.progress < 28.0

        # A late event from an earlier phase cannot move the bar backward.
        screen.record_progress(
            ProgressEvent(
                2,
                ProgressPhase.OBSERVING_SOURCE,
                "Late stability event",
                completed=1,
                total=1,
            )
        )
        assert 27.0 < bar.progress < 28.0

        # Counts are interpreted within their typed phase, not as global work.
        screen.record_progress(
            ProgressEvent(
                3,
                ProgressPhase.PROOF_BATCH,
                "Half of this proof batch",
                completed=1,
                total=2,
            )
        )
        assert 77.0 < bar.progress < 78.0
        screen.record_progress(
            ProgressEvent(4, ProgressPhase.LEAN_BUILD, "Build generated project")
        )
        screen.record_progress(
            ProgressEvent(5, ProgressPhase.CERTIFICATION, "Certify finished proofs")
        )
        screen.record_progress(
            ProgressEvent(6, ProgressPhase.PROOF_BATCH, "Start another proof batch")
        )
        stages = screen.query_one("#progress-stages", TextArea).text
        assert "[active ] PROOF_BATCH" in stages
        assert "[done   ] LEAN_BUILD" in stages
        assert "[done   ] CERTIFICATION" in stages
        assert "Current stage: PROOF_BATCH" in stages
        assert "Progress:" in stages
        assert 81.0 < bar.progress < 82.0

        screen.record_progress(ProgressEvent(7, ProgressPhase.COMPLETE, "Complete"))
        assert bar.progress == 100.0


@async_test
async def test_cooperative_cancellation_waits_for_backend_report() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(110, 35)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await pilot.pause(0.1)
        app.start_verification(service.project, None)
        await wait_for(pilot, lambda: progress_log_contains(app, "INDEXING"))

        cancel_button = app.screen.query_one("#cancel", Button)
        assert "Request cooperative cancellation" in str(cancel_button.label)
        cancel_button.press()
        await pilot.pause()
        assert isinstance(app.screen, ProgressScreen)
        waiting = app.screen.query_one("#status-line", TextArea)
        assert waiting.read_only
        await wait_for(
            pilot,
            lambda: (
                bool(service.cancel_requests)
                and "Persistent cancellation request recorded" in waiting.text
            ),
        )
        assert "survives all clients" in waiting.text
        assert "safely cancelled" not in waiting.text.lower()

        service.verification_release.set()
        await wait_for(pilot, lambda: cancellation_report_is_ready(app))
        report = app.screen.query_one("#cancellation-report", TextArea)
        assert report.read_only
        assert "Run ID: 73" in report.text
        assert "Preserved certificates (2)" in report.text
        assert "lem:stable" in report.text
        assert "thm:finished" in report.text
        assert "Retryable claims (2)" in report.text
        assert "thm:in-flight" in report.text
        assert "cor:dependent" in report.text
        assert "Temporary worktrees cleaned: yes" in report.text
        assert "Stopped after the current claim" in report.text
        report.select_all()
        assert "Run ID: 73" in report.selected_text


@async_test
async def test_welcome_focuses_first_resumable_project_after_catalog_load() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: button_is_ready(app, "#resume-0"))
        button = app.screen.query_one("#resume-0", Button)
        await wait_for(pilot, lambda: app.focused is button)

        assert app.focused is button


@async_test
async def test_welcome_catalog_refresh_preserves_deliberate_focus() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: button_is_ready(app, "#resume-0"))
        resume = app.screen.query_one("#resume-0", Button)
        await wait_for(pilot, lambda: app.focused is resume)
        settings = app.screen.query_one("#settings", Button)
        settings.focus()
        await wait_for(pilot, lambda: app.focused is settings)

        app.screen.action_refresh()
        await wait_for(
            pilot,
            lambda: (
                app.screen.query_one("#status-line", TextArea).text
                == "1 project(s) available."
            ),
        )
        await pilot.pause()

        assert app.focused is settings


@async_test
async def test_welcome_enter_opens_initially_focused_project() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: button_is_ready(app, "#resume-0"))
        button = app.screen.query_one("#resume-0", Button)
        await wait_for(pilot, lambda: app.focused is button)
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, DashboardScreen))

        assert service.resumed == [service.project.project_path]


@async_test
async def test_welcome_open_shortcut_uses_the_focused_project_row() -> None:
    service = FakeWorkflowService()
    second = replace(
        service.project,
        project_id="project-two",
        name="paper-two",
        project_path=Path("/tmp/proof-assistant/paper-two"),
    )
    service.projects = (catalog_entry(service.project), catalog_entry(second))
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: button_is_ready(app, "#resume-1"))
        first_button = app.screen.query_one("#resume-0", Button)
        await wait_for(pilot, lambda: app.focused is first_button)
        second_button = app.screen.query_one("#resume-1", Button)
        second_button.focus()
        await wait_for(pilot, lambda: app.focused is second_button)
        await pilot.press("ctrl+o")
        await wait_for(pilot, lambda: isinstance(app.screen, DashboardScreen))

        assert service.resumed == [second.project_path]


@async_test
async def test_welcome_mouse_click_opens_project() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: button_is_ready(app, "#resume-0"))
        await pilot.click("#resume-0")
        await wait_for(pilot, lambda: isinstance(app.screen, DashboardScreen))

        assert service.resumed == [service.project.project_path]


@async_test
async def test_welcome_reports_invalid_project_action_payload() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: button_is_ready(app, "#resume-0"))
        button = app.screen.query_one("#resume-0", tui_screens._ProjectActionButton)
        button.payload = object()
        button.press()
        await wait_for(
            pilot,
            lambda: (
                "stale or invalid"
                in app.screen.query_one("#status-line", TextArea).text
            ),
        )

        status = app.screen.query_one("#status-line", TextArea)
        assert isinstance(app.screen, WelcomeScreen)
        assert status.has_class("error")
        assert service.resumed == []


@async_test
async def test_resume_clarification_exact_source_and_no_change(tmp_path: Path) -> None:
    service = FakeWorkflowService()
    source_path = tmp_path / "paper with spaces"
    exact_file = source_path / "sections/main.tex"
    exact_file.parent.mkdir(parents=True)
    exact_file.write_text("manuscript source\n", encoding="utf-8")
    waiting_project = ProjectSummary(
        **{
            **service.project.__dict__,
            "source_path": source_path,
            "workflow_state": WorkflowState.AWAITING_CLARIFICATION,
        }
    )
    service.project = waiting_project
    service.projects = (catalog_entry(waiting_project),)
    service.resume_result = WorkflowSnapshot(
        WorkflowState.AWAITING_CLARIFICATION,
        waiting_project,
        clarifications=(clarification(waiting_project),),
    )
    opened: list[Path] = []
    app = ProofAssistantApp(
        service,
        location_opener=opened.append,
    )

    async with app.run_test(size=(140, 55)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(
            pilot,
            lambda: (
                app.screen.query("#resume-0").first() is not None
                and app.screen.query("#resume-0").first().region.width > 0
            ),
        )
        await pilot.click("#resume-0")
        await wait_for(pilot, lambda: isinstance(app.screen, ClarificationScreen))

        detail = app.screen.query_one("#clarification-detail", TextArea).text
        assert "thm:child-a" in detail
        assert "Strengthen the assumptions" in detail
        assert "Why verification stopped" in detail
        assert "could not justify positive definiteness" in detail
        assert "Best current guess" in detail
        assert "not confirmed fact or a Lean result" in detail
        assert "E-deadbeef0000001" in detail
        assert "Provider: claude_cli" in detail
        banner = str(app.screen.query_one("#clarification-best-guess", Static).render())
        assert "AI-assisted hypothesis" in banner
        assert "Confidence: High" in banner
        assert "not a Lean result" in banner
        assert "sections/main.tex:42:1" in str(
            app.screen.query_one("#source-location", TextArea).text
        )
        assert app.screen.query_one("#dropbox-warning", TextArea)
        source = app.screen.query_one("#source-excerpt", Static)
        syntax = app.screen._syntax(app.screen.question)
        assert isinstance(syntax, Syntax)
        assert "The restriction is positive definite" in syntax.code
        assert syntax.line_numbers
        assert syntax.start_line == 40
        assert syntax.highlight_lines == set(
            app.screen.question.location.highlighted_lines
        )
        assert 42 in syntax.highlight_lines
        assert not syntax.word_wrap
        assert source.allow_select
        assert source.region.height > 0
        assert not app.screen.query("#source-excerpt-copy")
        assert not app.screen.query("#open-file")
        assert not hasattr(app, "edit_source")
        clarification_screen = app.screen
        await pilot.press("ctrl+o")
        assert app.screen is clarification_screen
        assert opened == []
        await pilot.click("#open-folder")
        assert opened == [waiting_project.source_path / "sections"]

        service.plan_result = None
        await pilot.click("#check-changes")
        await wait_for(
            pilot,
            lambda: (
                bool(service.planned)
                and isinstance(app.screen, ClarificationScreen)
                and "No stable manuscript changes"
                in app.screen.query_one("#status-line", TextArea).text
            ),
        )
        assert "No stable manuscript changes" in str(
            app.screen.query_one("#status-line", TextArea).text
        )
        assert service.started_jobs == []

        await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))


@async_test
async def test_clarification_navigation_preserves_screen_and_handles_unavailable_guess() -> (
    None
):
    service = FakeWorkflowService()
    first = clarification(service.project)
    second = replace(
        first,
        question_id="q-2",
        claim_id="lem:second",
        analysis=ClarificationAnalysis(
            status=ClarificationAnalysisStatus.UNAVAILABLE,
            evidence_sha256="evidence-second",
            origin=ClarificationOrigin.HOST_POLICY,
            failure_detail="No eligible strongest-model analysis was recorded.",
        ),
    )
    snapshot = WorkflowSnapshot(
        WorkflowState.AWAITING_CLARIFICATION,
        service.project,
        clarifications=(first, second),
    )
    screen = ClarificationScreen(snapshot)
    app = ProofAssistantApp(service)

    async with app.run_test(size=(100, 32)) as pilot:
        app.switch_screen(screen)
        await wait_for(
            pilot,
            lambda: (
                app.screen is screen
                and bool(screen.query("#show-clarification-resolution").nodes)
            ),
        )
        await pilot.click("#show-clarification-resolution")
        assert screen.has_class("show-resolution")

        await pilot.click("#next")
        await wait_for(pilot, lambda: screen.question.question_id == "q-2")
        assert app.screen is screen
        assert screen.has_class("show-resolution")
        banner = str(screen.query_one("#clarification-best-guess", Static).render())
        detail = screen.query_one("#clarification-detail", TextArea).text
        assert "Best current guess unavailable" in banner
        assert "No eligible strongest-model analysis" in banner
        assert "Origin: Host policy" in detail
        assert "No substitute model or provider was used" in detail


@async_test
async def test_change_impact_requires_explicit_confirmation() -> None:
    service = FakeWorkflowService()
    p = service.project
    service.plan_result = change_plan(p)
    service.verification_release = threading.Event()
    service.resume_result = WorkflowSnapshot(WorkflowState.PROJECT_READY, p)
    app = ProofAssistantApp(service)

    async with app.run_test(size=(140, 55)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(
            pilot,
            lambda: (
                app.screen.query("#resume-0").first() is not None
                and app.screen.query("#resume-0").first().region.width > 0
            ),
        )
        await pilot.click("#resume-0")
        await wait_for(
            pilot, lambda: app.screen.__class__.__name__ == "DashboardScreen"
        )
        await pilot.click("#check-changes")
        await wait_for(pilot, lambda: isinstance(app.screen, ChangeReviewScreen))

        text = app.screen.query_one("#impact-detail", TextArea).text
        assert "sections/main.tex" in text
        assert "thm:child-b" in text
        assert "lem:independent" in text
        assert "q-1" in text
        assert "candidate-main.tex" in text
        assert "sections/new-input.tex" in text
        assert "Main file changed: yes" in text
        assert service.started_jobs == []

        await pilot.click("#confirm")
        await wait_for(
            pilot,
            lambda: progress_log_contains(app, "INDEXING"),
        )
        source_text = app.screen.query_one("#progress-sources", TextArea).text
        assert "Main file: candidate-main.tex" in source_text
        assert "sections/new-input.tex" in source_text
        assert "sections/introduction.tex" not in source_text
        service.verification_release.set()
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        await settle_screen(pilot)
        assert service.started_jobs[0][1] == "plan-1"


@async_test
async def test_legacy_busy_project_attaches_coarse_read_only_progress() -> None:
    service = FakeWorkflowService()
    busy = ProjectSummary(
        **{**service.project.__dict__, "workflow_state": WorkflowState.BUSY_EXTERNAL}
    )
    service.projects = (catalog_entry(busy),)
    service.resume_result = WorkflowSnapshot(
        WorkflowState.BUSY_EXTERNAL,
        busy,
        error="A legacy backend verification is active for this project.",
    )
    service.job_state = VerificationJobState.RUNNING
    service.job_attached_legacy = True
    service.verification_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(110, 35)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(
            pilot,
            lambda: (
                app.screen.query("#resume-0").first() is not None
                and app.screen.query("#resume-0").first().region.width > 0
            ),
        )
        await pilot.click("#resume-0")
        await wait_for(pilot, lambda: isinstance(app.screen, ProgressScreen))
        await wait_for(
            pilot, lambda: progress_sources_contain(app, "legacy coarse read-only")
        )
        status = app.screen.query_one("#status-line", TextArea)
        assert status.read_only
        assert "owns neither the verification nor its lock" in status.text
        assert "another process" not in status.text.lower()
        assert app.screen.query_one("#cancel", Button).disabled
        assert (
            "durable per-stage events are not available"
            in app.screen.query_one("#progress-log", TextArea).text
        )
        await activate_scrolled_button(pilot, app, "#detach-observer")
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))


def test_production_tui_has_no_synchronous_verifier_or_local_token() -> None:
    app_source = (
        Path(tui_screens.__file__).with_name("app.py").read_text(encoding="utf-8")
    )
    assert "confirm_and_verify" not in app_source
    assert "ThreadCancellationToken" not in app_source


@async_test
async def test_detached_submission_freezes_all_role_settings() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    role_settings = tuple(
        VerificationRoleSettings(
            task=task,
            ai_driver="claude_cli",
            model=(
                "best"
                if task is TaskKind.PROOF
                else "fable"
                if task is TaskKind.DUPLICATE_PROOF
                else "opus"
                if task
                in {TaskKind.CLARIFICATION, TaskKind.DIAGNOSTIC, TaskKind.REVIEW}
                else "sonnet"
                if task in {TaskKind.SKETCH, TaskKind.MAINTENANCE}
                else "haiku"
            ),
            effort=(
                "xhigh"
                if task is TaskKind.DUPLICATE_PROOF
                else "low"
                if task is TaskKind.REPORTING
                else "medium"
                if task in {TaskKind.SKETCH, TaskKind.MAINTENANCE}
                else "high"
            ),
        )
        for task in TaskKind
    )
    chosen = VerificationSettings(
        ai_driver="claude_cli",
        model="best",
        effort="high",
        role_settings=role_settings,
    )
    service.default_settings = chosen
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.start_verification(service.project, None)
        service.default_settings = VerificationSettings()
        await wait_for(pilot, lambda: bool(service.started_jobs))

        submitted = service.started_jobs[0][2]
        assert submitted is chosen
        assert {item.task for item in submitted.role_settings} == set(TaskKind)
        assert submitted.for_task(TaskKind.PROOF).model == "best"
        assert submitted.for_task(TaskKind.DUPLICATE_PROOF).model == "fable"
        assert submitted.for_task(TaskKind.DUPLICATE_PROOF).effort == "xhigh"
        assert submitted.for_task(TaskKind.CLARIFICATION).model == "opus"
        assert submitted.for_task(TaskKind.SKETCH).model == "sonnet"
        assert submitted.for_task(TaskKind.REPORTING).model == "haiku"
        assert submitted.for_task(TaskKind.REPORTING).effort == "low"
        await activate_scrolled_button(pilot, app, "#detach-observer")
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))


@async_test
async def test_detached_job_defaults_to_two_workers_and_honors_one() -> None:
    default_service = FakeWorkflowService()
    default_service.verification_release = threading.Event()
    default_app = ProofAssistantApp(default_service)

    async with default_app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(default_app.screen, WelcomeScreen))
        default_app.start_verification(default_service.project, None)
        await wait_for(pilot, lambda: bool(default_service.started_jobs))
        assert default_service.started_jobs[0][2].jobs == 2
        await wait_for(
            pilot,
            lambda: progress_sources_contain(default_app, "Parallel proof jobs: 2"),
        )
        await activate_scrolled_button(pilot, default_app, "#detach-observer")
        await wait_for(pilot, lambda: isinstance(default_app.screen, WelcomeScreen))

    single_service = FakeWorkflowService()
    single_service.verification_release = threading.Event()
    single_app = ProofAssistantApp(single_service)
    async with single_app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(single_app.screen, WelcomeScreen))
        single_app.start_verification(
            single_service.project,
            None,
            VerificationSettings(jobs=1),
        )
        await wait_for(pilot, lambda: bool(single_service.started_jobs))
        assert single_service.started_jobs[0][2].jobs == 1
        await wait_for(
            pilot,
            lambda: progress_sources_contain(single_app, "Parallel proof jobs: 1"),
        )
        await activate_scrolled_button(pilot, single_app, "#detach-observer")


@async_test
async def test_closing_tui_stops_observation_without_cancelling_job() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.start_verification(service.project, None)
        await wait_for(pilot, lambda: progress_log_contains(app, "INDEXING"))
        status = app.screen.query_one("#status-line", TextArea)
        assert "Closing or detaching stops polling only" in status.text
        app.exit()

    assert service.cancel_requests == []
    assert service.job_state == VerificationJobState.RUNNING


@async_test
async def test_second_tui_attaches_and_replays_detached_progress() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    first_app = ProofAssistantApp(service)

    async with first_app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(first_app.screen, WelcomeScreen))
        first_app.start_verification(service.project, None)
        await wait_for(pilot, lambda: progress_log_contains(first_app, "INDEXING"))
        await activate_scrolled_button(pilot, first_app, "#detach-observer")
        await wait_for(pilot, lambda: isinstance(first_app.screen, WelcomeScreen))

    second_app = ProofAssistantApp(service)
    async with second_app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(second_app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: button_is_ready(second_app, "#resume-0"))
        await pilot.click("#resume-0")
        await wait_for(pilot, lambda: progress_log_contains(second_app, "INDEXING"))
        assert len(service.started_jobs) == 1
        assert (service.project.project_path, 0) in service.observed_jobs
        assert "Attached to the backend-owned job and replayed durable progress" in (
            second_app.screen.query_one("#progress-log", TextArea).text
        )
        sources = second_app.screen.query_one("#progress-sources", TextArea)
        sources.select_all()
        assert "Job ID: job-73" in sources.selected_text
        assert "Durable event cursor: 1" in sources.selected_text
        await activate_scrolled_button(pilot, second_app, "#detach-observer")


@async_test
async def test_persistent_cancel_survives_client_and_terminal_routes_recovery() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    first_app = ProofAssistantApp(service)

    async with first_app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(first_app.screen, WelcomeScreen))
        first_app.start_verification(service.project, None)
        await wait_for(pilot, lambda: progress_log_contains(first_app, "INDEXING"))
        await activate_scrolled_button(pilot, first_app, "#cancel")
        await wait_for(pilot, lambda: bool(service.cancel_requests))
        assert service.job_state == VerificationJobState.CANCEL_REQUESTED
        await activate_scrolled_button(pilot, first_app, "#detach-observer")

    second_app = ProofAssistantApp(service)
    async with second_app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(second_app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: button_is_ready(second_app, "#resume-0"))
        await pilot.click("#resume-0")
        await wait_for(
            pilot,
            lambda: progress_sources_contain(second_app, "CANCEL_REQUESTED"),
        )
        assert second_app.screen.query_one("#cancel", Button).disabled
        service.verification_release.set()
        await wait_for(pilot, lambda: cancellation_report_is_ready(second_app))
        assert (
            "Run ID: 73"
            in second_app.screen.query_one("#cancellation-report", TextArea).text
        )


@async_test
async def test_settings_overlay_restores_exact_live_verification_observer() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.start_verification(service.project, None)
        await wait_for(pilot, lambda: progress_log_contains(app, "INDEXING"))
        progress = app.screen
        assert isinstance(progress, ProgressScreen)
        observation = app._active_observation
        observer_worker = app._observer_worker
        assert observation is not None
        assert observer_worker is not None

        app.action_global_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        assert app._active_observation is not None
        assert app._active_observation.job.job_id == observation.job.job_id
        assert app._observer_worker is observer_worker
        assert not observer_worker.is_cancelled

        app.screen.action_back()
        await wait_for(pilot, lambda: app.screen is progress)
        assert app._active_observation is not None
        assert app._active_observation.job.job_id == observation.job.job_id
        assert app._observer_worker is observer_worker
        assert not observer_worker.is_cancelled

        service.verification_release.set()
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))


@async_test
async def test_verification_completion_waits_for_settings_overlay_to_close() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.start_verification(service.project, None)
        await wait_for(pilot, lambda: progress_log_contains(app, "INDEXING"))
        app.action_global_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))

        service.verification_release.set()
        await wait_for(pilot, lambda: app._pending_settings_snapshot is not None)
        assert isinstance(app.screen, SettingsHomeScreen)
        assert app._settings_overlay_active
        assert app._observer_worker is None
        assert app._progress_screen is None

        app.screen.action_back()
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        assert not app._settings_overlay_active
        assert app._pending_settings_snapshot is None


@async_test
async def test_non_observer_snapshot_completion_waits_for_settings_overlay() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.current_snapshot = service.resume_result
        progress = ProgressScreen(
            "Saving existing project's main file",
            project=service.project.project_path,
        )
        app.switch_screen(progress)
        await wait_for(pilot, lambda: app.screen is progress)

        app.action_global_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        app.show_snapshot(service.verify_result)
        await pilot.pause()

        assert isinstance(app.screen, SettingsHomeScreen)
        assert app._settings_overlay_active
        assert app._pending_settings_snapshot is service.verify_result

        app.screen.action_back()
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        assert not app._settings_overlay_active
        assert app._pending_settings_snapshot is None


@async_test
async def test_unattachable_activity_recovery_waits_for_settings_overlay() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)
    snapshot = WorkflowSnapshot(WorkflowState.VERIFYING, service.project)

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        progress = ProgressScreen(
            "Discovering verification",
            project=service.project.project_path,
        )
        app.switch_screen(progress)
        await wait_for(pilot, lambda: app.screen is progress)
        app.action_global_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))

        app._show_unattachable_activity(snapshot)
        await pilot.pause()
        assert isinstance(app.screen, SettingsHomeScreen)
        assert app._pending_settings_navigation is not None

        app.screen.action_back()
        await wait_for(pilot, lambda: isinstance(app.screen, RecoveryScreen))
        assert not app._settings_overlay_active


@async_test
async def test_deletion_confirmation_waits_for_settings_overlay() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_settings(service.machine_settings)
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))

        app._confirm_project_deletion(
            service.project,
            service.deletion_inspection_result,
        )
        await pilot.pause()
        assert isinstance(app.screen, SettingsHomeScreen)
        assert app._pending_settings_navigation is not None

        app.screen.action_back()
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, ProjectDeletionConfirmationScreen),
        )
        assert not app._settings_overlay_active
        await pilot.press("escape")


@async_test
async def test_main_menu_discards_queued_settings_completion_result() -> None:
    catalog_release = threading.Event()

    class DelayedCatalogService(FakeWorkflowService):
        def __init__(self) -> None:
            super().__init__()
            self.catalog_calls = 0

        def list_projects(self) -> Sequence[ProjectCatalogEntry]:
            self.catalog_calls += 1
            catalog_release.wait(timeout=5)
            return super().list_projects()

    service = DelayedCatalogService()
    service.verification_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        stale_welcome = app.screen
        await wait_for(pilot, lambda: service.catalog_calls == 1)
        app.start_verification(service.project, None)
        await wait_for(pilot, lambda: progress_log_contains(app, "INDEXING"))
        app.action_global_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        service.verification_release.set()
        await wait_for(pilot, lambda: app._pending_settings_snapshot is not None)

        app.action_main_menu()
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        assert app.screen is not stale_welcome
        assert not stale_welcome.is_attached
        await wait_for(pilot, lambda: service.catalog_calls == 2)
        assert app._pending_settings_snapshot is None
        app.show_settings(service.machine_settings)
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        app.screen.action_back()
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        catalog_release.set()
        await wait_for(
            pilot,
            lambda: (
                "1 project(s) available."
                in app.screen.query_one("#status-line", TextArea).text
            ),
        )
        assert not isinstance(app.screen, FindingsScreen)


@async_test
async def test_polling_error_is_copyable_and_detached_job_may_continue() -> None:
    service = FakeWorkflowService()
    service.verification_release = threading.Event()
    service.observation_error = RuntimeError(
        "cannot read /tmp/proof-assistant/paper-one/job-events.jsonl"
    )
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.start_verification(service.project, None)
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ProgressScreen)
                and bool(app.screen.query("#status-line").nodes)
                and "Progress polling failed"
                in app.screen.query_one("#status-line", TextArea).text
            ),
        )
        status = app.screen.query_one("#status-line", TextArea)
        status.select_all()
        assert "job-events.jsonl" in status.selected_text
        assert "may still be running" in status.selected_text
        assert service.cancel_requests == []


@async_test
async def test_terminal_observation_resumes_canonical_result_without_attach_loop() -> (
    None
):
    service = FakeWorkflowService()
    service.job_state = VerificationJobState.SUCCEEDED
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await wait_for(pilot, lambda: button_is_ready(app, "#resume-0"))
        await pilot.click("#resume-0")
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        assert service.started_jobs == []
        assert service.observed_jobs == [(service.project.project_path, 0)]
        assert service.resumed == [service.project.project_path]


@async_test
async def test_machine_settings_navigation_and_copyable_live_status_at_80x24() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.screen.query_one("#settings", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        await wait_for(pilot, lambda: settings_home_is_ready(app))

        machine = app.screen.query_one("#settings-machine-summary", TextArea)
        assert machine.read_only
        assert "Settings scope: MACHINE" in machine.text
        assert "machine-test-7" in machine.text
        assert str(service.machine_settings.config_path) in machine.text
        machine.select_all()
        assert str(service.machine_settings.cache_path) in machine.selected_text

        app.screen.query_one("#open-concurrency-settings", Button).press()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        await settle_screen(pilot)
        assert app.screen.query_one("#benchmark-lean", Button).disabled
        assert app.screen.query_one("#reset-lean-calibration", Button).disabled
        summary = app.screen.query_one("#concurrency-summary", TextArea)
        assert summary.read_only
        assert "AI concurrency: Auto" in summary.text
        assert "Effective now: 4" in summary.text
        assert "Effective ceiling: 8" in summary.text
        assert "Lean REPL pool: Auto" in summary.text
        assert "Concurrent builds: Auto" in summary.text
        summary.select_all()
        assert "Agents per target" in summary.selected_text

        telemetry = app.screen.query_one("#resource-telemetry", TextArea)
        assert "10 physical / 10 usable logical" in telemetry.text
        assert "pressure GREEN" in telemetry.text
        assert "active swap-out 0.00 MiB/s" in telemetry.text
        assert "Pressure source: macos_native; native level 0" in telemetry.text
        assert "Codex: 2 active; 5 queued" in telemetry.text
        assert "Lean: 3 active; 1 queued" in telemetry.text
        resolution = app.screen.query_one("#settings-resolution", TextArea)
        assert "source=machine auto policy" in resolution.text
        assert "Why Auto chose these values" in resolution.text
        app.screen.refresh_status()
        await wait_for(pilot, lambda: service.machine_settings_reads >= 2)
        assert service.machine_settings_reads >= 2


@async_test
async def test_dashboard_exposes_machine_settings_navigation() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_snapshot(service.resume_result)
        await wait_for(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await settle_screen(pilot)
        app.screen.query_one("#settings", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        await settle_screen(pilot)
        assert app.screen.project == service.project.project_path
        app.screen.query_one("#open-concurrency-settings", Button).press()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        await settle_screen(pilot)
        assert app.screen.project == service.project.project_path
        assert not app.screen.query_one("#benchmark-lean", Button).disabled
        app.screen.action_back()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        await settle_screen(pilot)
        app.screen.query_one("#settings-back", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, DashboardScreen))


@async_test
async def test_concurrency_settings_preview_apply_and_replacement_tui_persistence() -> (
    None
):
    service = FakeWorkflowService()
    first_app = ProofAssistantApp(service)

    async with first_app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(first_app.screen, WelcomeScreen))
        first_app.show_concurrency_settings(service.machine_settings)
        await wait_for(
            pilot,
            lambda: isinstance(first_app.screen, ConcurrencyResourcesScreen),
        )
        await settle_screen(pilot)
        first_app.screen.query_one("#concurrency-mode", Select).value = "fixed"
        first_app.screen.query_one("#resource-profile", Select).value = "server"
        first_app.screen.query_one("#codex-plan", Select).value = "pro_20x"
        first_app.screen.query_one("#budget-policy", Select).value = "economy"
        first_app.screen.query_one("#ai-concurrency", Input).value = "6"
        first_app.screen.query_one("#ai-hard-max", Input).value = "12"
        first_app.screen.query_one("#lean-pool", Input).value = "4"
        first_app.screen.query_one("#lean-max", Input).value = "6"
        first_app.screen.query_one("#max-builds", Input).value = "2"
        first_app.screen.query_one("#agents-per-target", Input).value = "3"
        first_app.screen.query_one("#duplicate-escalation", Checkbox).value = False
        await pilot.pause()
        first_app.screen.query_one("#save-concurrency", Button).press()
        await wait_for(pilot, lambda: bool(service.settings_applications))
        await wait_for(
            pilot,
            lambda: (
                "Applied live"
                in first_app.screen.query_one("#status-line", TextArea).text
            ),
        )

        request = service.settings_previews[-1]
        assert request.scope == SettingsScopeKind.MACHINE
        assert request.expected_revision == 3
        assert request.configured.mode == "fixed"
        assert not request.configured.adaptive_controller
        assert request.configured.resource_profile == "server"
        assert request.configured.codex_plan == "pro_20x"
        assert request.configured.budget_policy == "economy"
        assert request.configured.ai_initial == 6
        assert request.configured.ai_hard_max == 12
        assert request.configured.lean_pool == 4
        assert request.configured.max_builds == 2
        assert request.configured.agents_per_target_max == 3
        assert not request.configured.duplicate_agent_escalation
        status = first_app.screen.query_one("#status-line", TextArea)
        assert "Applied live" in status.text
        assert "Takes effect next run" in status.text

    second_app = ProofAssistantApp(service)
    async with second_app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(second_app.screen, WelcomeScreen))
        second_app.show_settings()
        await wait_for(
            pilot,
            lambda: settings_home_is_ready(second_app),
        )
        second_app.screen.query_one("#open-concurrency-settings", Button).press()
        await wait_for(
            pilot,
            lambda: isinstance(second_app.screen, ConcurrencyResourcesScreen),
        )
        await settle_screen(pilot)
        assert second_app.screen.query_one("#concurrency-mode", Select).value == "fixed"
        assert second_app.screen.query_one("#ai-concurrency", Input).value == "6"
        assert (
            "Effective now: 6"
            in second_app.screen.query_one("#concurrency-summary", TextArea).text
        )
        assert (
            "Effective ceiling: 12"
            in second_app.screen.query_one("#concurrency-summary", TextArea).text
        )
        assert (
            "AI concurrency: 6 [Manual override]"
            in second_app.screen.query_one("#concurrency-summary", TextArea).text
        )


@async_test
async def test_settings_revision_change_is_not_silently_overwritten() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_concurrency_settings(
            service.machine_settings,
            project=service.project.project_path,
        )
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        await settle_screen(pilot)
        editor = app.screen
        assert editor.snapshot.revision == 3

        service.machine_settings = replace(service.machine_settings, revision=4)
        editor.refresh_status()
        await wait_for(
            pilot,
            lambda: (
                "changed in another client"
                in editor.query_one("#status-line", TextArea).text
            ),
        )
        assert editor.snapshot.revision == 3
        editor.query_one("#ai-concurrency", Input).value = "5"
        editor.query_one("#save-concurrency", Button).press()
        await wait_for(
            pilot,
            lambda: (
                "revision changed" in editor.query_one("#status-line", TextArea).text
            ),
        )
        assert service.settings_previews == []
        assert service.settings_applications == []


@async_test
async def test_unsafe_setting_requires_copyable_cancel_first_confirmation() -> None:
    service = FakeWorkflowService()
    service.settings_warnings = (
        SettingsWarning(
            "lean-memory-risk",
            "Lean pool 24 may cause severe swapping on this machine.",
            "Use recommended value: 3",
        ),
    )
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_concurrency_settings(service.machine_settings)
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        await settle_screen(pilot)
        app.screen.query_one("#lean-pool", Input).value = "24"
        app.screen.query_one("#save-concurrency", Button).press()
        await wait_for(
            pilot,
            lambda: settings_warning_is_ready(app),
        )
        detail = app.screen.query_one("#settings-warning-detail", TextArea)
        assert detail.read_only
        assert "severe swapping" in detail.text
        assert "Use recommended value: 3" in detail.text
        await wait_for(
            pilot,
            lambda: (
                app.screen.focused
                is app.screen.query("#settings-warning-cancel").first()
            ),
        )
        assert app.screen.focused is app.screen.query_one(
            "#settings-warning-cancel", Button
        )
        assert service.settings_applications == []
        app.screen.query_one("#settings-warning-confirm", Button).press()
        await wait_for(pilot, lambda: bool(service.settings_applications))
        assert service.settings_applications == [("preview-1", ("lean-memory-risk",))]


@async_test
async def test_reset_to_auto_recalculates_and_updates_editors() -> None:
    service = FakeWorkflowService()
    service.machine_settings = replace(
        service.machine_settings,
        configured=replace(
            service.machine_settings.configured,
            mode="fixed",
            ai_initial=7,
            lean_pool=5,
            max_builds=2,
        ),
    )
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_concurrency_settings(service.machine_settings)
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        await settle_screen(pilot)
        app.screen.query_one("#reset-concurrency", Button).press()
        await wait_for(
            pilot,
            lambda: (
                bool(app.screen.query("#settings-destructive-confirm").nodes)
                and app.screen.focused
                is app.screen.query_one("#settings-destructive-cancel", Button)
            ),
        )
        assert service.settings_resets == []
        assert app.screen.focused is app.screen.query_one(
            "#settings-destructive-cancel", Button
        )
        app.action_global_settings()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        assert service.settings_resets == []

        app.screen.query_one("#reset-concurrency", Button).press()
        await wait_for(
            pilot,
            lambda: (
                bool(app.screen.query("#settings-destructive-confirm").nodes)
                and app.screen.focused
                is app.screen.query_one("#settings-destructive-cancel", Button)
            ),
        )
        await pilot.press("enter")
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        assert service.settings_resets == []

        app.screen.query_one("#reset-concurrency", Button).press()
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-destructive-confirm").nodes),
        )
        app.screen.query_one("#settings-destructive-confirm", Button).press()
        await wait_for(pilot, lambda: bool(service.settings_resets))
        await wait_for(
            pilot,
            lambda: app.screen.query_one("#ai-concurrency", Input).value == "Auto",
        )
        await wait_for(
            pilot,
            lambda: (
                "reset to Auto" in app.screen.query_one("#status-line", TextArea).text
            ),
        )
        assert app.screen.query_one("#concurrency-mode", Select).value == "adaptive"
        assert app.screen.query_one("#ai-concurrency", Input).value == "Auto"
        assert app.screen.query_one("#lean-pool", Input).value == "Auto"
        assert app.screen.query_one("#max-builds", Input).value == "Auto"
        assert "reset to Auto" in app.screen.query_one("#status-line", TextArea).text


@async_test
async def test_machine_settings_editors_guard_dirty_navigation() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_concurrency_settings(service.machine_settings)
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        runtime = app.screen
        assert isinstance(runtime, ConcurrencyResourcesScreen)
        await wait_for(pilot, lambda: bool(runtime.query("#ai-concurrency").nodes))
        runtime.query_one("#ai-concurrency", Input).value = "6"
        runtime.action_back()
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-unsaved-continue").nodes),
        )
        assert app.screen.focused is app.screen.query_one(
            "#settings-unsaved-continue", Button
        )
        await pilot.press("enter")
        await wait_for(pilot, lambda: app.screen is runtime)
        assert runtime.query_one("#ai-concurrency", Input).value == "6"
        assert service.settings_previews == []

        app.action_main_menu()
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-unsaved-save").nodes),
        )
        app.screen.query_one("#settings-unsaved-save", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        assert service.machine_settings.configured.ai_initial == 6

        app.show_legacy_settings(service.machine_settings)
        await wait_for(pilot, lambda: isinstance(app.screen, LegacySettingsScreen))
        legacy = app.screen
        assert isinstance(legacy, LegacySettingsScreen)
        await wait_for(pilot, lambda: bool(legacy.query("#legacy-proof-jobs").nodes))
        legacy.query_one("#legacy-proof-jobs", Input).value = "5"
        app.action_global_settings()
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-unsaved-discard").nodes),
        )
        app.screen.query_one("#settings-unsaved-discard", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        assert service.machine_settings.legacy.proof_jobs != 5


@async_test
async def test_runtime_newer_edit_survives_in_flight_apply_and_cancels_leave() -> None:
    service = FakeWorkflowService()
    service.settings_apply_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_concurrency_settings(service.machine_settings)
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        screen = app.screen
        assert isinstance(screen, ConcurrencyResourcesScreen)
        await wait_for(pilot, lambda: bool(screen.query("#ai-concurrency").nodes))
        screen.query_one("#ai-concurrency", Input).value = "6"
        screen.action_back()
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-unsaved-save").nodes),
        )
        app.screen.query_one("#settings-unsaved-save", Button).press()
        await wait_for(pilot, service.settings_apply_started.is_set)
        screen.query_one("#ai-concurrency", Input).value = "7"

        service.settings_apply_release.set()
        await wait_for(
            pilot,
            lambda: (
                app.screen is screen
                and "newer edits remain unsaved"
                in screen.query_one("#status-line", TextArea).text
            ),
        )
        assert service.machine_settings.configured.ai_initial == 6
        assert screen.query_one("#ai-concurrency", Input).value == "7"
        assert screen._draft_is_dirty()


@async_test
async def test_legacy_newer_edit_survives_in_flight_preview_and_cancels_leave() -> None:
    service = FakeWorkflowService()
    service.settings_preview_release = threading.Event()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_legacy_settings(service.machine_settings)
        await wait_for(pilot, lambda: isinstance(app.screen, LegacySettingsScreen))
        screen = app.screen
        assert isinstance(screen, LegacySettingsScreen)
        await wait_for(pilot, lambda: bool(screen.query("#legacy-proof-jobs").nodes))
        screen.query_one("#legacy-proof-jobs", Input).value = "3"
        screen.action_back()
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-unsaved-save").nodes),
        )
        app.screen.query_one("#settings-unsaved-save", Button).press()
        await wait_for(pilot, service.settings_preview_started.is_set)
        screen.query_one("#legacy-proof-jobs", Input).value = "4"

        service.settings_preview_release.set()
        await wait_for(
            pilot,
            lambda: (
                app.screen is screen
                and "newer edits remain unsaved"
                in screen.query_one("#status-line", TextArea).text
            ),
        )
        assert service.machine_settings.legacy.proof_jobs == 3
        assert screen.query_one("#legacy-proof-jobs", Input).value == "4"
        assert screen._draft_is_dirty()


@async_test
async def test_legacy_settings_are_distinct_editable_and_machine_persisted() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_legacy_settings(service.machine_settings)
        await wait_for(pilot, lambda: isinstance(app.screen, LegacySettingsScreen))
        await settle_screen(pilot)
        summary = app.screen.query_one("#legacy-settings-summary", TextArea)
        assert summary.read_only
        assert "Proof batch workers (jobs): 2" in summary.text
        assert "Claims per batch: 8" in summary.text
        assert "Lean REPLs per batch worker: 1" in summary.text
        assert "superseded by machine AI admission" in summary.text
        assert "superseded by machine build admission" in summary.text
        summary.select_all()
        assert "Legacy controls never form a separate" in summary.selected_text

        app.screen.query_one("#legacy-proof-jobs", Input).value = "1"
        app.screen.query_one("#legacy-batch-size", Input).value = "4"
        app.screen.query_one("#legacy-lean-pool", Input).value = "2"
        app.screen.query_one("#save-legacy", Button).press()
        await wait_for(pilot, lambda: bool(service.settings_applications))
        await wait_for(
            pilot,
            lambda: (
                "Takes effect next run"
                in app.screen.query_one("#status-line", TextArea).text
            ),
        )
        persisted = service.machine_settings.legacy
        assert persisted.proof_jobs == 1
        assert persisted.batch_size == 4
        assert persisted.per_worker_lean_pool == 2
        assert (
            "Takes effect next run"
            in app.screen.query_one("#status-line", TextArea).text
        )


@async_test
async def test_benchmark_actions_are_backend_owned_copyable_and_codex_safe() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_concurrency_settings(
            service.machine_settings,
            project=service.project.project_path,
        )
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        await settle_screen(pilot)
        await select_runtime_destination(pilot, app, 2)
        app.screen.query_one("#benchmark-codex", Button).press()
        await wait_for(pilot, lambda: len(service.benchmarks) == 1)
        await wait_for(
            pilot,
            lambda: (
                "Benchmark: codex-concurrency"
                in app.screen.query_one("#benchmark-result", TextArea).text
            ),
        )
        assert service.benchmarks[0] == (BenchmarkKind.CODEX, None, False)
        result = app.screen.query_one("#benchmark-result", TextArea)
        assert result.read_only
        assert "Codex traffic used: no" in result.text
        assert "Recommended value: 4" in result.text
        result.select_all()
        assert "Calibration record:" in result.selected_text

        app.screen.query_one("#benchmark-lean", Button).press()
        await wait_for(pilot, lambda: len(service.benchmarks) == 2)
        await activate_scrolled_button(pilot, app, "#benchmark-build")
        await wait_for(pilot, lambda: len(service.benchmarks) == 3)
        assert [kind for kind, _project, _traffic in service.benchmarks] == [
            BenchmarkKind.CODEX,
            BenchmarkKind.LEAN,
            BenchmarkKind.BUILD,
        ]
        assert all(not traffic for _kind, _project, traffic in service.benchmarks)
        assert service.benchmarks[1] == (
            BenchmarkKind.LEAN,
            service.project.project_path,
            False,
        )


@async_test
async def test_concurrency_reset_actions_are_backend_owned_and_copyable() -> None:
    service = FakeWorkflowService()
    app = ProofAssistantApp(service)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_concurrency_settings(
            service.machine_settings,
            project=service.project.project_path,
        )
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
        )
        await settle_screen(pilot)
        await select_runtime_destination(pilot, app, 2)
        await activate_scrolled_button(pilot, app, "#reset-lean-calibration")
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-destructive-confirm").nodes),
        )
        assert service.calibration_resets == []
        app.screen.query_one("#settings-destructive-confirm", Button).press()
        await wait_for(pilot, lambda: bool(service.calibration_resets))
        result = app.screen.query_one("#benchmark-result", TextArea)
        await wait_for(pilot, lambda: "Lean calibration reset" in result.text)
        assert service.calibration_resets == [service.project.project_path]
        result.select_all()
        assert "profile-test" in result.selected_text

        await activate_scrolled_button(pilot, app, "#reset-adaptive-history")
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-destructive-confirm").nodes),
        )
        assert service.adaptive_history_resets == 0
        app.screen.query_one("#settings-destructive-confirm", Button).press()
        await wait_for(pilot, lambda: service.adaptive_history_resets == 1)
        await wait_for(pilot, lambda: "Adaptive history reset" in result.text)
        result.select_all()
        assert "In-flight work preserved: yes" in result.selected_text


def test_settings_tui_has_no_hardware_or_configuration_implementation_imports() -> None:
    settings_root = Path(tui_screens.__file__).parent / "settings"
    forbidden = {
        "psutil",
        "proof_assistant.concurrency.hardware",
        "proof_assistant.concurrency.config",
        "proof_assistant.concurrency.manager",
    }
    violations: list[str] = []
    for source_path in settings_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module_names: list[str] = []
            if isinstance(node, ast.Import):
                module_names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_names = [node.module]
            if any(
                name == prefix or name.startswith(prefix + ".")
                for name in module_names
                for prefix in forbidden
            ):
                violations.append(f"{source_path.name}: {module_names}")
    assert violations == []
