from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Sequence
from functools import wraps
from pathlib import Path
from typing import Any

from textual.pilot import Pilot
from textual.widgets import Button, Input, Static, TextArea

from proof_assistant.tui import ProofAssistantApp
from proof_assistant.tui.screens import (
    ChangeReviewScreen,
    ClarificationScreen,
    ExistingProjectMainFileSelectionScreen,
    FindingsScreen,
    MainFileSelectionScreen,
    NewProjectScreen,
    ProgressScreen,
    ProjectDestinationConflictScreen,
    ProjectReviewScreen,
    RecoveryScreen,
    WelcomeScreen,
)
from proof_assistant.workflow.contracts import (
    CancellationReport,
    CancellationToken,
    ChangeImpactPlan,
    ClaimChangeKind,
    ClaimImpact,
    ClarificationPresentation,
    FileChange,
    FileChangeKind,
    FindingSummary,
    LatexSourceCandidate,
    NewProjectRequest,
    ProgressEvent,
    ProgressPhase,
    ProgressSink,
    ProjectAvailability,
    ProjectCatalogEntry,
    ProjectDestinationInspection,
    ProjectSummary,
    SourceInspection,
    SourceLocation,
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
        requested_actions=("State the missing hypothesis explicitly.",),
        possible_resolutions=("Strengthen the assumptions.", "Weaken the conclusion."),
        location=location,
        blocked_claims=("thm:child-a", "thm:child-b"),
        generated_by="deterministic-fallback",
        provenance_sha256="deadbeef",
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


class FakeWorkflowService:
    """Contract-only fake: no filesystem, Git, or SQLite behavior."""

    def __init__(self) -> None:
        self.project = project()
        self.projects: tuple[ProjectCatalogEntry, ...] = (catalog_entry(self.project),)
        self.inspected: list[Path] = []
        self.inspected_destinations: list[tuple[str, Path | None]] = []
        self.selected_main_files: list[tuple[Path, str]] = []
        self.created: list[NewProjectRequest] = []
        self.resumed: list[Path] = []
        self.planned: list[Path] = []
        self.verified: list[tuple[Path, str | None, VerificationSettings]] = []
        self.plan_result: ChangeImpactPlan | None = None
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
        self.creation_release: threading.Event | None = None
        self.verification_release: threading.Event | None = None
        self.create_result = WorkflowSnapshot(WorkflowState.PROJECT_READY, self.project)
        self.resume_result = WorkflowSnapshot(WorkflowState.PROJECT_READY, self.project)
        self.select_main_result = WorkflowSnapshot(
            WorkflowState.PROJECT_READY, self.project
        )
        self.verify_result = WorkflowSnapshot(
            WorkflowState.COMPLETED,
            ProjectSummary(
                **{**self.project.__dict__, "workflow_state": WorkflowState.COMPLETED}
            ),
            findings=findings(self.project),
        )

    def default_task_text(self) -> str:
        return "Verify every claimed theorem without sorry or new axioms."

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

    def resume_project(self, project_path: Path) -> WorkflowSnapshot:
        self.resumed.append(project_path)
        return self.resume_result

    def plan_changes(self, project_path: Path) -> ChangeImpactPlan | None:
        self.planned.append(project_path)
        return self.plan_result

    def confirm_and_verify(
        self,
        project_path: Path,
        plan_id: str | None,
        settings: VerificationSettings,
        *,
        progress: ProgressSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> WorkflowSnapshot:
        self.verified.append((project_path, plan_id, settings))
        if progress is not None:
            progress(ProgressEvent(1, ProgressPhase.INDEXING, "Indexed sources", 1, 2))
        if self.verification_release is not None:
            self.verification_release.wait(timeout=3)
        if cancellation is not None and cancellation.cancelled:
            interrupted = ProjectSummary(
                **{
                    **self.project.__dict__,
                    "workflow_state": WorkflowState.INTERRUPTED,
                }
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
        if progress is not None:
            progress(ProgressEvent(2, ProgressPhase.COMPLETE, "Finished", 2, 2))
        return self.verify_result


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


async def settle_screen(pilot: Pilot[None]) -> None:
    """Flush Textual 1.0 Header callbacks before another switch or teardown."""

    await wait_for(pilot, lambda: bool(pilot.app.screen.query("HeaderTitle").nodes))
    await pilot.pause(0.05)


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
        review = str(app.screen.query_one("#project-review", Static).renderable)
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
        assert service.verified[0][1] is None
        assert app.screen.query_one("#dropbox-warning", Static)
        assert "All selected claims" in str(
            app.screen.query_one("#findings-detail", Static).renderable
        )


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
            app.screen.query_one("#project-review", Static).renderable
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
        await pilot.click("#select-main")
        assert service.created == []
        assert "Select one main" in str(
            app.screen.query_one("#status-line", Static).renderable
        )

        await pilot.click("#main-option-2")
        await pilot.click("#select-main")
        await wait_for(pilot, lambda: isinstance(app.screen, ProjectReviewScreen))
        assert service.created == []
        review = str(app.screen.query_one("#project-review", Static).renderable)
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
            str(widget.renderable) for widget in app.screen.query(".project-summary")
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
            str(widget.renderable) for widget in app.screen.query(".project-summary")
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

        detail = str(app.screen.query_one("#destination-conflict", Static).renderable)
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
        # Let Textual 1.0 finish mounting Header internals before switching.
        await pilot.pause(0.1)
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
        assert "still running" in waiting.text
        assert "safely cancelled" not in waiting.text.lower()

        service.verification_release.set()
        await wait_for(pilot, lambda: isinstance(app.screen, RecoveryScreen))
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
async def test_resume_clarification_exact_source_and_no_change() -> None:
    service = FakeWorkflowService()
    waiting_project = ProjectSummary(
        **{
            **service.project.__dict__,
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
    app = ProofAssistantApp(service, location_opener=opened.append)

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

        detail = str(app.screen.query_one("#clarification-detail", Static).renderable)
        assert "thm:child-a" in detail
        assert "Strengthen the assumptions" in detail
        assert "sections/main.tex:42:1" in str(
            app.screen.query_one("#source-location", Static).renderable
        )
        assert app.screen.query_one("#dropbox-warning", Static)

        await pilot.click("#open-file")
        await pilot.click("#open-folder")
        assert opened == [
            waiting_project.source_path / "sections/main.tex",
            waiting_project.source_path / "sections",
        ]

        service.plan_result = None
        await pilot.click("#check-changes")
        await wait_for(
            pilot,
            lambda: (
                bool(service.planned)
                and isinstance(app.screen, ClarificationScreen)
                and "No stable manuscript changes"
                in str(app.screen.query_one("#status-line", Static).renderable)
            ),
        )
        assert "No stable manuscript changes" in str(
            app.screen.query_one("#status-line", Static).renderable
        )
        assert service.verified == []


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

        text = str(app.screen.query_one("#impact-detail", Static).renderable)
        assert "sections/main.tex" in text
        assert "thm:child-b" in text
        assert "lem:independent" in text
        assert "q-1" in text
        assert "candidate-main.tex" in text
        assert "sections/new-input.tex" in text
        assert "Main file changed: yes" in text
        assert service.verified == []

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
        assert service.verified[0][1] == "plan-1"


@async_test
async def test_busy_project_opens_recovery_screen() -> None:
    service = FakeWorkflowService()
    busy = ProjectSummary(
        **{**service.project.__dict__, "workflow_state": WorkflowState.BUSY_EXTERNAL}
    )
    service.projects = (catalog_entry(busy),)
    service.resume_result = WorkflowSnapshot(
        WorkflowState.BUSY_EXTERNAL,
        busy,
        error="A separate verifier owns the project lock.",
    )
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
        await wait_for(pilot, lambda: isinstance(app.screen, RecoveryScreen))
        assert "separate verifier" in str(
            app.screen.query_one(".error", Static).renderable
        )
