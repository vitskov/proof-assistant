from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from functools import wraps
from pathlib import Path
from typing import Any

from textual.pilot import Pilot
from textual.widgets import Input, Static, TextArea

from proof_assistant.tui import ProofAssistantApp
from proof_assistant.tui.screens import (
    ChangeReviewScreen,
    ClarificationScreen,
    FindingsScreen,
    NewProjectScreen,
    RecoveryScreen,
    WelcomeScreen,
)
from proof_assistant.workflow.contracts import (
    CancellationToken,
    ChangeImpactPlan,
    ClaimChangeKind,
    ClaimImpact,
    ClarificationPresentation,
    FileChange,
    FileChangeKind,
    FindingSummary,
    NewProjectRequest,
    ProgressEvent,
    ProgressPhase,
    ProgressSink,
    ProjectSummary,
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
        last_opened_at="2026-08-23T12:00:00Z",
        workflow_state=state,
        source_in_dropbox=True,
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
        self.projects: tuple[ProjectSummary, ...] = (self.project,)
        self.created: list[NewProjectRequest] = []
        self.resumed: list[Path] = []
        self.planned: list[Path] = []
        self.verified: list[tuple[Path, str | None, VerificationSettings]] = []
        self.plan_result: ChangeImpactPlan | None = None
        self.create_result = WorkflowSnapshot(WorkflowState.PROJECT_READY, self.project)
        self.resume_result = WorkflowSnapshot(WorkflowState.PROJECT_READY, self.project)
        self.verify_result = WorkflowSnapshot(
            WorkflowState.COMPLETED,
            ProjectSummary(
                **{**self.project.__dict__, "workflow_state": WorkflowState.COMPLETED}
            ),
            findings=findings(self.project),
        )

    def default_task_text(self) -> str:
        return "Verify every claimed theorem without sorry or new axioms."

    def list_projects(self) -> Sequence[ProjectSummary]:
        return self.projects

    def create_project(self, request: NewProjectRequest) -> WorkflowSnapshot:
        self.created.append(request)
        return self.create_result

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
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if progress is not None:
            progress(ProgressEvent(1, ProgressPhase.INDEXING, "Indexed sources", 1, 2))
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
        await pilot.click("#create")

        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        assert len(service.created) == 1
        request = service.created[0]
        assert request.name == "spectral-paper"
        assert request.source_path == Path("/Users/writer/Dropbox/paper")
        assert request.project_path == Path("/Users/writer/proof-assistant/spectral")
        assert request.task_text == "Verify the main spectral theorem."
        assert service.verified[0][1] is None
        assert app.screen.query_one("#dropbox-warning", Static)
        assert "All selected claims" in str(
            app.screen.query_one("#findings-detail", Static).renderable
        )


@async_test
async def test_default_task_is_backend_owned() -> None:
    service = FakeWorkflowService()
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
        await pilot.click("#create")
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))

        assert service.created[0].task_text is None
        assert service.created[0].project_path is None


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
    service.projects = (waiting_project,)
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
        assert service.verified == []

        await pilot.click("#confirm")
        await wait_for(pilot, lambda: isinstance(app.screen, FindingsScreen))
        assert service.verified[0][1] == "plan-1"


@async_test
async def test_busy_project_opens_recovery_screen() -> None:
    service = FakeWorkflowService()
    busy = ProjectSummary(
        **{**service.project.__dict__, "workflow_state": WorkflowState.BUSY_EXTERNAL}
    )
    service.projects = (busy,)
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
