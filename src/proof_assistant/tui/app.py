"""Proof Assistant's Textual application.

No code in this package imports the verifier, its database, or its Git support.
All long-running work crosses ``WorkflowServiceContract`` on a worker thread.
"""

from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

from textual.app import App

from proof_assistant.tui.screens import (
    ChangeReviewScreen,
    ClarificationScreen,
    DashboardScreen,
    ExistingProjectMainFileSelectionScreen,
    FindingsScreen,
    MainFileSelectionScreen,
    NewProjectDraft,
    NewProjectScreen,
    ProgressScreen,
    ProjectDestinationConflictScreen,
    ProjectReviewScreen,
    RecoveryScreen,
    WelcomeScreen,
)
from proof_assistant.workflow.contracts import (
    ChangeImpactPlan,
    NewProjectRequest,
    ProgressEvent,
    ProjectCatalogEntry,
    ProjectDestinationInspection,
    ProjectSummary,
    SourceInspection,
    VerificationSettings,
    WorkflowServiceContract,
    WorkflowSnapshot,
    WorkflowState,
)

LocationOpener = Callable[[Path], None]


class ThreadCancellationToken:
    """Small thread-safe implementation of the public cancellation contract."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise InterruptedError("verification cancelled by user")


def _default_location_opener(path: Path) -> None:
    """Open a source location with the operating system's registered handler."""

    webbrowser.open(path.resolve().as_uri())


class ProofAssistantApp(App[None]):
    """A thin, dependency-injected UI over ``WorkflowServiceContract``."""

    TITLE = "Proof Assistant"
    SUB_TITLE = "Persistent manuscript verification"
    CSS = """
    Screen { background: $surface; }
    #page { width: 100%; height: 100%; padding: 1 3; }
    .title { text-style: bold; color: $accent; margin-bottom: 1; }
    .section { text-style: bold; margin-top: 1; }
    .muted { color: $text-muted; }
    .warning { color: $warning; border: tall $warning; padding: 0 1; margin: 1 0; }
    .error { color: $error; border: tall $error; padding: 0 1; margin: 1 0; }
    .success { color: $success; }
    .toolbar { height: auto; margin-top: 1; }
    .toolbar Button { margin-right: 1; }
    Input { margin-bottom: 1; }
    TextArea { height: 12; border: round $accent; margin-bottom: 1; }
    #project-list { height: 1fr; border: round $panel; padding: 1; }
    .project-row { height: auto; margin-bottom: 1; }
    .project-row Static { width: 1fr; }
    .project-row Button { width: auto; }
    #main-file-options { height: auto; border: round $panel; padding: 1; }
    #project-review { height: auto; border: round $accent; padding: 1; }
    #progress-sources { height: 7; border: round $accent; }
    #progress-stages { height: 16; border: round $panel; }
    #progress-log { height: 1fr; min-height: 6; border: round $panel; }
    #cancellation-report { height: 1fr; min-height: 14; border: round $warning; }
    .progress-warning { height: 5; }
    ProgressScreen #status-line { height: 3; border: none; }
    #source-excerpt {
        height: 1fr; min-height: 10; border: round $accent; overflow: auto;
    }
    #impact-detail { height: 1fr; border: round $panel; padding: 1; overflow: auto; }
    #findings-detail { height: 1fr; border: round $panel; padding: 1; overflow: auto; }
    #status-line { margin: 1 0; }
    """

    def __init__(
        self,
        service: WorkflowServiceContract,
        *,
        location_opener: LocationOpener | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.location_opener = location_opener or _default_location_opener
        self.current_snapshot: WorkflowSnapshot | None = None
        self.settings = VerificationSettings()
        self._cancellation: ThreadCancellationToken | None = None

    def on_mount(self) -> None:
        self.push_screen(WelcomeScreen())

    def show_welcome(self) -> None:
        self.switch_screen(WelcomeScreen())

    def show_new_project(self, draft: NewProjectDraft | None = None) -> None:
        self.switch_screen(NewProjectScreen(draft))

    def show_main_file_selection(
        self,
        draft: NewProjectDraft,
        inspection: SourceInspection,
        destination: ProjectDestinationInspection,
        *,
        selected_main: str | None = None,
    ) -> None:
        self.switch_screen(
            MainFileSelectionScreen(
                draft,
                inspection,
                destination,
                selected_main=selected_main,
            )
        )

    def review_new_project(
        self,
        draft: NewProjectDraft,
        inspection: SourceInspection,
        destination: ProjectDestinationInspection,
        main_file: str,
        *,
        auto_selected: bool,
    ) -> None:
        self.switch_screen(
            ProjectReviewScreen(
                draft,
                inspection,
                destination,
                main_file,
                auto_selected=auto_selected,
            )
        )

    def show_existing_project_main_selection(self, entry: ProjectCatalogEntry) -> None:
        self.switch_screen(ExistingProjectMainFileSelectionScreen(entry))

    def open_location(self, path: Path) -> None:
        try:
            self.location_opener(path)
        except Exception as exc:  # pragma: no cover - depends on desktop setup
            screen = self.screen
            if hasattr(screen, "show_notice"):
                screen.show_notice(f"Could not open {path}: {exc}", error=True)

    def inspect_source_for_project(self, draft: NewProjectDraft) -> None:
        """Preflight destination and source through backend-owned contracts."""

        progress = ProgressScreen(
            "Inspecting manuscript source",
            project=draft.project_path,
        )
        self.switch_screen(progress)

        def inspect() -> None:
            try:
                destination = self.service.inspect_project_destination(
                    draft.name, draft.project_path
                )
                if not destination.can_create:
                    self.call_from_thread(
                        self._show_destination_conflict, draft, destination
                    )
                    return
                result = self.service.inspect_source(draft.source_path)
            except Exception as exc:
                self.call_from_thread(
                    self.show_error,
                    "Source inspection failed",
                    str(exc),
                    draft.project_path,
                )
                return
            self.call_from_thread(
                self._after_source_inspection,
                draft,
                result,
                destination,
            )

        self.run_worker(inspect, thread=True, exclusive=True, group="workflow")

    def _after_source_inspection(
        self,
        draft: NewProjectDraft,
        inspection: SourceInspection,
        destination: ProjectDestinationInspection,
    ) -> None:
        if not inspection.candidates:
            self.show_error(
                "Source inspection failed",
                "No LaTeX source files were found.",
                draft.project_path,
            )
            return
        if len(inspection.candidates) == 1:
            self.review_new_project(
                draft,
                inspection,
                destination,
                inspection.candidates[0].relative_path,
                auto_selected=True,
            )
            return
        self.show_main_file_selection(draft, inspection, destination)

    def _show_destination_conflict(
        self,
        draft: NewProjectDraft,
        inspection: ProjectDestinationInspection,
    ) -> None:
        self.switch_screen(ProjectDestinationConflictScreen(draft, inspection))

    def select_existing_project_main_file(
        self, entry: ProjectCatalogEntry, main_file: str
    ) -> None:
        progress = ProgressScreen(
            "Saving existing project's main file",
            project=entry.project_path,
            main_file=main_file,
        )
        self.switch_screen(progress)

        def select() -> None:
            try:
                snapshot = self.service.select_project_main_file(
                    entry.project_path, main_file
                )
            except Exception as exc:
                self.call_from_thread(
                    self.show_error,
                    "Could not save main-file selection",
                    str(exc),
                    entry.project_path,
                )
                return
            self.call_from_thread(self.show_snapshot, snapshot)

        self.run_worker(select, thread=True, exclusive=True, group="workflow")

    def create_project(self, request: NewProjectRequest) -> None:
        """Create and then immediately begin the first verification pass."""

        self.settings = request.settings
        progress = ProgressScreen(
            "Creating project",
            project=request.project_path,
            main_file=request.main_file,
        )
        self.switch_screen(progress)

        def create() -> None:
            try:
                snapshot = self.service.create_project(request)
            except Exception as exc:
                self.call_from_thread(
                    self.show_error, "Project creation failed", str(exc), None
                )
                return
            self.call_from_thread(self._after_create, snapshot)

        self.run_worker(create, thread=True, exclusive=True, group="workflow")

    def _after_create(self, snapshot: WorkflowSnapshot) -> None:
        self.current_snapshot = snapshot
        if snapshot.pending_plan is not None:
            self.show_snapshot(snapshot)
            return
        if snapshot.state in {
            WorkflowState.PROJECT_READY,
            WorkflowState.OBSERVING_SOURCE,
        }:
            self.start_verification(snapshot.project, None, self.settings)
            return
        self.show_snapshot(snapshot)

    def resume_project(self, project: Path) -> None:
        progress = ProgressScreen("Resuming project", project=project)
        self.switch_screen(progress)

        def resume() -> None:
            try:
                snapshot = self.service.resume_project(project)
            except Exception as exc:
                self.call_from_thread(
                    self.show_error, "Could not resume project", str(exc), project
                )
                return
            self.call_from_thread(self.show_snapshot, snapshot)

        self.run_worker(resume, thread=True, exclusive=True, group="workflow")

    def check_for_changes(self, project: ProjectSummary) -> None:
        progress = ProgressScreen(
            "Checking selected manuscript source",
            project=project.project_path,
            main_file=project.main_file,
            input_files=project.input_files,
        )
        self.switch_screen(progress)

        def plan() -> None:
            try:
                result = self.service.plan_changes(project.project_path)
            except Exception as exc:
                self.call_from_thread(
                    self.show_error,
                    "Source change detection failed",
                    str(exc),
                    project.project_path,
                )
                return
            self.call_from_thread(self._after_plan, project, result)

        self.run_worker(plan, thread=True, exclusive=True, group="workflow")

    def _after_plan(
        self, project: ProjectSummary, plan: ChangeImpactPlan | None
    ) -> None:
        if plan is None or not plan.has_changes:
            if (
                self.current_snapshot is not None
                and self.current_snapshot.clarifications
            ):
                screen = ClarificationScreen(self.current_snapshot)
                self.switch_screen(screen)
                self.call_after_refresh(
                    screen.show_notice, "No stable manuscript changes detected yet."
                )
            else:
                screen = DashboardScreen(self._snapshot_for_project(project))
                self.switch_screen(screen)
                self.call_after_refresh(
                    screen.show_notice, "No stable manuscript changes detected."
                )
            return
        snapshot = WorkflowSnapshot(
            state=WorkflowState.CHANGE_REVIEW,
            project=project,
            pending_plan=plan,
        )
        self.current_snapshot = snapshot
        self.switch_screen(ChangeReviewScreen(snapshot))

    def start_verification(
        self,
        project: ProjectSummary,
        plan_id: str | None,
        settings: VerificationSettings | None = None,
        *,
        main_file: str | None = None,
        input_files: tuple[str, ...] | None = None,
    ) -> None:
        """Run verification on a thread and stream typed progress events."""

        chosen_settings = settings or self.settings
        self.settings = chosen_settings
        token = ThreadCancellationToken()
        self._cancellation = token
        progress_screen = ProgressScreen(
            "Verifying manuscript",
            project=project.project_path,
            cancellable=True,
            source_in_dropbox=project.source_in_dropbox,
            main_file=main_file or project.main_file,
            input_files=(project.input_files if input_files is None else input_files),
        )
        self.switch_screen(progress_screen)

        def progress(event: ProgressEvent) -> None:
            self.call_from_thread(progress_screen.record_progress, event)

        def verify() -> None:
            try:
                snapshot = self.service.confirm_and_verify(
                    project.project_path,
                    plan_id,
                    chosen_settings,
                    progress=progress,
                    cancellation=token,
                )
            except InterruptedError as exc:
                self.call_from_thread(
                    self.show_error,
                    "Verification interrupted",
                    str(exc),
                    project.project_path,
                )
                return
            except Exception as exc:
                self.call_from_thread(
                    self.show_error,
                    "Verification failed",
                    str(exc),
                    project.project_path,
                )
                return
            self.call_from_thread(self.show_snapshot, snapshot)

        self.run_worker(verify, thread=True, exclusive=True, group="workflow")

    def cancel_verification(self) -> None:
        if self._cancellation is not None:
            self._cancellation.cancel()

    def show_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        self.current_snapshot = snapshot
        state = snapshot.state
        if state == WorkflowState.CHANGE_REVIEW and snapshot.pending_plan is not None:
            self.switch_screen(ChangeReviewScreen(snapshot))
        elif state == WorkflowState.AWAITING_CLARIFICATION and snapshot.clarifications:
            self.switch_screen(ClarificationScreen(snapshot))
        elif state == WorkflowState.COMPLETED and snapshot.findings is not None:
            self.switch_screen(FindingsScreen(snapshot))
        elif state in {
            WorkflowState.FAILED,
            WorkflowState.INTERRUPTED,
            WorkflowState.BUSY_EXTERNAL,
        }:
            self.switch_screen(RecoveryScreen(snapshot))
        else:
            self.switch_screen(DashboardScreen(snapshot))

    def show_error(self, title: str, detail: str, project: Path | None) -> None:
        self.switch_screen(RecoveryScreen.from_error(title, detail, project))

    def _snapshot_for_project(self, project: ProjectSummary) -> WorkflowSnapshot:
        if (
            self.current_snapshot is not None
            and self.current_snapshot.project == project
        ):
            return self.current_snapshot
        return WorkflowSnapshot(state=project.workflow_state, project=project)


def run_tui(
    service: WorkflowServiceContract,
    *,
    location_opener: LocationOpener | None = None,
) -> int:
    """Run Proof Assistant's interactive terminal interface."""

    result = ProofAssistantApp(service, location_opener=location_opener).run()
    return int(result or 0)
