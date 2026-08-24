"""Proof Assistant's Textual application.

No code in this package imports the verifier, its database, or its Git support.
All long-running work crosses ``WorkflowServiceContract`` on a worker thread.
"""

from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

from textual.app import App
from textual.worker import Worker, get_current_worker

from proof_assistant.tui.commands import GLOBAL_BINDINGS
from proof_assistant.tui.screens import (
    ChangeReviewScreen,
    ClarificationScreen,
    DashboardScreen,
    ExistingProjectMainFileSelectionScreen,
    FailureDependencyScreen,
    FindingsScreen,
    MainFileSelectionScreen,
    NewProjectDraft,
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
)
from proof_assistant.tui.theme import (
    DEFAULT_PROOF_THEME,
    PROOF_DARK_THEME,
    PROOF_LIGHT_THEME,
    PROOF_THEMES,
    THEME_VARIABLE_DEFAULTS,
)
from proof_assistant.workflow.contracts import (
    ChangeImpactPlan,
    FailureDependencyReport,
    MachineSettingsSnapshot,
    NewProjectRequest,
    ProjectCatalogEntry,
    ProjectDeletionInspection,
    ProjectDeletionResult,
    ProjectDestinationInspection,
    ProjectSummary,
    ReportDocument,
    SourceInspection,
    VerificationJobObservation,
    VerificationSettings,
    WorkflowServiceContract,
    WorkflowSnapshot,
    WorkflowState,
)

LocationOpener = Callable[[Path], None]


def _default_location_opener(path: Path) -> None:
    """Open a source location with the operating system's registered handler."""

    webbrowser.open(path.resolve().as_uri())


class ProofAssistantApp(App[None]):
    """A thin, dependency-injected UI over ``WorkflowServiceContract``."""

    TITLE = "Proof Assistant"
    SUB_TITLE = "Persistent manuscript verification"
    BINDINGS = GLOBAL_BINDINGS
    CSS = """
    Screen {
        background: $proof-page-background;
        color: $foreground;
    }
    Header {
        background: $proof-chrome-background;
        color: $proof-chrome-foreground;
    }
    Footer {
        background: $footer-background;
        color: $footer-foreground;
    }
    FooterKey:hover { background: $proof-info-background; }
    #page {
        width: 100%; height: 100%; padding: 1 3;
        background: $proof-page-background;
    }
    .title {
        text-style: bold; color: $text-primary; margin-bottom: 1;
    }
    .section { text-style: bold; color: $text-secondary; margin-top: 1; }
    .muted { color: $proof-muted; }
    .toolbar { height: auto; margin-top: 1; }
    .toolbar Button { margin-right: 1; }
    Input, Select {
        margin-bottom: 1;
        background: $proof-input-background;
        border: tall $proof-panel-border;
    }
    Input:focus, Select:focus { border: tall $proof-focus; }
    #source-folder-controls { height: auto; }
    #source-folder-controls Input { width: 1fr; }
    #source-folder-controls Button { width: auto; margin-left: 1; }
    TextArea {
        height: 12;
        color: $foreground;
        background: $proof-input-background;
        border: round $proof-panel-border;
        margin-bottom: 1;
    }
    TextArea:focus { border: round $proof-focus; }
    .copyable-info, .copyable-info:focus {
        border: none; padding: 0; margin: 0; background: transparent;
    }
    .warning, .warning:focus {
        color: $proof-warning-text;
        background: $proof-warning-background;
        border: tall $warning;
        padding: 0 1; margin: 1 0;
    }
    .error, .error:focus {
        color: $proof-error-text;
        background: $proof-error-background;
        border: tall $error;
        padding: 0 1; margin: 1 0;
    }
    .success { color: $proof-success-text; }
    DataTable, Tree, RadioSet {
        background: $proof-panel-background;
        border: round $proof-panel-border;
    }
    DataTable:focus, Tree:focus, RadioSet:focus { border: round $proof-focus; }
    #project-list {
        height: 1fr; border: round $proof-panel-border;
        background: $proof-panel-background; padding: 1;
    }
    #folder-picker-table {
        height: 1fr; min-height: 6; border: round $proof-panel-border;
        background: $proof-panel-background;
    }
    #folder-picker-controls Button { min-width: 0; width: auto; }
    .project-row { height: auto; margin-bottom: 1; }
    .project-row .project-summary { width: 1fr; }
    .project-row Button { width: auto; }
    #main-file-options {
        height: auto; border: round $proof-panel-border;
        background: $proof-panel-background; padding: 1;
    }
    #project-review {
        height: auto; border: round $proof-focus;
        background: $proof-info-background; padding: 1;
    }
    #progress-sources {
        height: 7; border: round $proof-focus;
        background: $proof-info-background;
    }
    #progress-stages {
        height: 16; border: round $proof-panel-border;
        background: $proof-panel-background;
    }
    #progress-log {
        height: 1fr; min-height: 6; border: round $proof-panel-border;
    }
    #cancellation-report {
        height: 1fr; min-height: 14; border: round $warning;
        background: $proof-warning-background; color: $proof-warning-text;
    }
    #report-tabs { height: 1fr; min-height: 8; }
    #report-markdown {
        height: 1fr; border: round $proof-panel-border;
        background: $proof-panel-background;
    }
    #report-source { height: 1fr; border: round $proof-focus; }
    #failure-tabs { height: 1fr; min-height: 8; }
    #failure-tree, #failure-components, #failure-detail, #failure-outline {
        height: 1fr; min-height: 6; border: round $proof-panel-border;
        background: $proof-panel-background;
    }
    ProjectDeletionConfirmationScreen {
        align: center middle; background: $proof-overlay;
    }
    #delete-project-dialog {
        width: 92%; max-width: 76; height: 92%; max-height: 22;
        border: round $error; background: $proof-dialog-background; padding: 1 2;
    }
    .progress-warning { height: 5; }
    ProgressScreen #status-line { height: 3; border: none; }
    #source-excerpt {
        height: 1fr; min-height: 10; border: round $proof-focus;
        background: $proof-code-background; overflow: auto;
    }
    #impact-detail, #findings-detail {
        height: 1fr; border: round $proof-panel-border;
        background: $proof-panel-background; padding: 1; overflow: auto;
    }
    #status-line { margin: 1 0; }
    #settings-machine-summary { height: 7; border: round $proof-panel-border; }
    #concurrency-summary {
        height: 18; border: round $proof-focus;
        background: $proof-info-background;
    }
    #resource-telemetry { height: 10; border: round $proof-panel-border; }
    #settings-resolution { height: 12; border: round $proof-panel-border; }
    #benchmark-result { height: 8; border: round $proof-panel-border; }
    #legacy-settings-summary { height: 16; border: round $proof-panel-border; }
    .settings-benchmark-toolbar { height: auto; }
    .settings-benchmark-toolbar Button { width: auto; margin-bottom: 1; }
    SettingsWarningConfirmationScreen {
        align: center middle; background: $proof-overlay;
    }
    #settings-warning-dialog {
        width: 92%; max-width: 76; height: 92%; max-height: 22;
        border: round $warning; background: $proof-dialog-background; padding: 1 2;
    }
    #settings-warning-detail {
        height: 1fr; min-height: 7;
        background: $proof-warning-background; color: $proof-warning-text;
    }
    ShortcutHelpScreen {
        align: center middle; background: $proof-overlay;
    }
    #shortcut-help-dialog {
        width: 96%; max-width: 104; height: 92%;
        border: round $proof-focus;
        background: $proof-dialog-background; padding: 1 2;
    }
    #shortcut-reference {
        height: 1fr; min-height: 12;
        border: round $proof-panel-border;
        background: $proof-panel-background;
    }
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
        self._settings_return_snapshot: WorkflowSnapshot | None = None
        self._active_observation: VerificationJobObservation | None = None
        self._observer_worker: Worker[None] | None = None
        self._progress_screen: ProgressScreen | None = None

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return THEME_VARIABLE_DEFAULTS

    def on_mount(self) -> None:
        for theme in PROOF_THEMES:
            self.register_theme(theme)
        self.theme = DEFAULT_PROOF_THEME
        self.push_screen(WelcomeScreen())

    def action_show_shortcuts(self) -> None:
        if isinstance(self.screen, ShortcutHelpScreen):
            self.pop_screen()
        else:
            self.push_screen(ShortcutHelpScreen())

    def action_toggle_proof_theme(self) -> None:
        self.theme = (
            PROOF_LIGHT_THEME.name
            if self.theme == PROOF_DARK_THEME.name
            else PROOF_DARK_THEME.name
        )

    def show_welcome(self) -> None:
        self.switch_screen(WelcomeScreen())

    def show_settings(
        self,
        snapshot: MachineSettingsSnapshot | None = None,
        *,
        project: Path | None = None,
        return_to_project: bool | None = None,
    ) -> None:
        """Open machine settings with optional project calibration context."""

        if return_to_project is not None:
            self._settings_return_snapshot = (
                self.current_snapshot if return_to_project else None
            )
        self.switch_screen(SettingsHomeScreen(snapshot, project=project))

    def show_concurrency_settings(
        self,
        snapshot: MachineSettingsSnapshot,
        *,
        project: Path | None = None,
    ) -> None:
        self.switch_screen(ConcurrencyResourcesScreen(snapshot, project=project))

    def show_legacy_settings(
        self,
        snapshot: MachineSettingsSnapshot,
        *,
        project: Path | None = None,
    ) -> None:
        self.switch_screen(LegacySettingsScreen(snapshot, project=project))

    def close_settings(self) -> None:
        if self._settings_return_snapshot is not None:
            snapshot = self._settings_return_snapshot
            self._settings_return_snapshot = None
            self.show_snapshot(snapshot)
        else:
            self.show_welcome()

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

    def request_project_deletion(self, project: ProjectSummary) -> None:
        """Ask the backend to preflight deletion before showing confirmation."""

        screen = self.screen
        if hasattr(screen, "show_notice"):
            screen.show_notice(
                "Checking whether managed project can move to recoverable deletion "
                "storage: "
                f"{project.project_path}"
            )

        def inspect() -> None:
            try:
                inspection = self.service.inspect_project_deletion(project.project_path)
            except Exception as exc:
                self.call_from_thread(
                    self._show_project_deletion_outcome,
                    project,
                    None,
                    None,
                    str(exc),
                )
                return
            self.call_from_thread(
                self._confirm_project_deletion,
                project,
                inspection,
            )

        self.run_worker(inspect, thread=True, exclusive=True, group="catalog")

    def _confirm_project_deletion(
        self,
        project: ProjectSummary,
        inspection: ProjectDeletionInspection,
    ) -> None:
        dialog = ProjectDeletionConfirmationScreen(project, inspection)

        def after_confirmation(confirmed: bool | None) -> None:
            if confirmed:
                self._delete_project(project, inspection)
            else:
                screen = self.screen
                if hasattr(screen, "show_notice"):
                    screen.show_notice(
                        f"Deletion canceled; managed project remains at "
                        f"{inspection.project_path}."
                    )

        self.push_screen(dialog, callback=after_confirmation)

    def _delete_project(
        self,
        project: ProjectSummary,
        inspection: ProjectDeletionInspection,
    ) -> None:
        self.switch_screen(
            ProgressScreen(
                "Moving managed project to recoverable deletion storage",
                project=inspection.project_path,
            )
        )

        def delete() -> None:
            try:
                result = self.service.delete_project(project.project_path)
            except Exception as exc:
                self.call_from_thread(
                    self._show_project_deletion_outcome,
                    project,
                    inspection,
                    None,
                    str(exc),
                )
                return
            self.call_from_thread(
                self._show_project_deletion_outcome,
                project,
                inspection,
                result,
                None,
            )

        self.run_worker(delete, thread=True, exclusive=True, group="catalog")

    def _show_project_deletion_outcome(
        self,
        project: ProjectSummary,
        inspection: ProjectDeletionInspection | None,
        result: ProjectDeletionResult | None,
        error: str | None,
    ) -> None:
        self.switch_screen(
            ProjectDeletionOutcomeScreen(
                project,
                inspection=inspection,
                result=result,
                error=error,
            )
        )

    def open_location(self, path: Path) -> None:
        try:
            self.location_opener(path)
        except Exception as exc:  # pragma: no cover - depends on desktop setup
            screen = self.screen
            if hasattr(screen, "show_notice"):
                screen.show_notice(f"Could not open {path}: {exc}", error=True)

    def view_report(self, snapshot: WorkflowSnapshot) -> None:
        """Load a report through the backend and render it inside the terminal."""

        project = snapshot.project.project_path
        progress = ProgressScreen("Loading verification report", project=project)
        self.switch_screen(progress)

        def load() -> None:
            try:
                document = self.service.load_report(project)
            except Exception as exc:
                self.call_from_thread(
                    self._show_report_viewer,
                    snapshot,
                    None,
                    str(exc),
                )
                return
            self.call_from_thread(
                self._show_report_viewer,
                snapshot,
                document,
                None,
            )

        self.run_worker(load, thread=True, exclusive=True, group="workflow")

    def _show_report_viewer(
        self,
        snapshot: WorkflowSnapshot,
        document: ReportDocument | None,
        error: str | None,
    ) -> None:
        self.switch_screen(ReportViewerScreen(snapshot, document=document, error=error))

    def view_failure_report(self, snapshot: WorkflowSnapshot) -> None:
        """Load backend-owned failure evidence for an SSH-safe terminal view."""

        embedded = snapshot.findings.failure_report if snapshot.findings else None
        if embedded is not None:
            self.switch_screen(FailureDependencyScreen(snapshot, report=embedded))
            return

        project = snapshot.project.project_path
        progress = ProgressScreen("Loading failure dependency report", project=project)
        self.switch_screen(progress)

        def load() -> None:
            try:
                report = self.service.load_failure_report(project, run_id=None)
            except Exception as exc:
                self.call_from_thread(
                    self._show_failure_report,
                    snapshot,
                    None,
                    str(exc),
                )
                return
            error = None if report is not None else "No failure report is available."
            self.call_from_thread(
                self._show_failure_report,
                snapshot,
                report,
                error,
            )

        self.run_worker(load, thread=True, exclusive=True, group="workflow")

    def _show_failure_report(
        self,
        snapshot: WorkflowSnapshot,
        report: FailureDependencyReport | None,
        error: str | None,
    ) -> None:
        self.switch_screen(
            FailureDependencyScreen(snapshot, report=report, error=error)
        )

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
            self.start_verification(snapshot.project, None)
            return
        self.show_snapshot(snapshot)

    def resume_project(self, project: ProjectSummary | Path) -> None:
        """Attach to active backend work before loading a canonical snapshot."""

        project_path = (
            project.project_path if isinstance(project, ProjectSummary) else project
        )
        progress = ProgressScreen("Inspecting project activity", project=project_path)
        self.switch_screen(progress)

        def resume() -> None:
            try:
                observation = self.service.observe_verification(
                    project_path, after_sequence=0
                )
            except Exception as exc:
                self.call_from_thread(
                    self.show_error,
                    "Could not inspect active verification",
                    str(exc),
                    project_path,
                )
                return
            if observation is not None:
                if observation.job.state.terminal:
                    try:
                        snapshot = self.service.resume_project(project_path)
                    except Exception as exc:
                        self.call_from_thread(
                            self.show_error,
                            "Could not load completed verification result",
                            str(exc),
                            project_path,
                        )
                        return
                    self.call_from_thread(self.show_snapshot, snapshot)
                    return
                if isinstance(project, ProjectSummary):
                    summary = project
                else:
                    try:
                        summary = self.service.resume_project(project_path).project
                    except Exception as exc:
                        self.call_from_thread(
                            self.show_error,
                            "Could not load project metadata",
                            str(exc),
                            project_path,
                        )
                        return
                self.call_from_thread(
                    self._attach_to_verification,
                    summary,
                    observation,
                )
                return
            try:
                snapshot = self.service.resume_project(project_path)
            except Exception as exc:
                self.call_from_thread(
                    self.show_error,
                    "Could not resume project",
                    str(exc),
                    project_path,
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
        """Submit a detached backend job, then observe its durable event stream."""

        chosen_settings = settings or self.service.default_verification_settings()
        progress_screen = ProgressScreen(
            "Submitting detached verification",
            project=project.project_path,
            cancellable=True,
            source_in_dropbox=project.source_in_dropbox,
            main_file=main_file or project.main_file,
            input_files=(project.input_files if input_files is None else input_files),
            detached_job=True,
        )
        self.switch_screen(progress_screen)
        self._progress_screen = progress_screen

        def submit() -> None:
            try:
                observation = self.service.start_verification(
                    project.project_path,
                    plan_id,
                    chosen_settings,
                )
            except Exception as exc:
                observation = getattr(exc, "observation", None)
                if isinstance(observation, VerificationJobObservation):
                    self.call_from_thread(
                        self._activate_observation,
                        progress_screen,
                        observation,
                        "An active job has different requested settings; attached "
                        "to the backend-owned job without replacing it.",
                    )
                    self._poll_verification(project, progress_screen, observation)
                    return
                self.call_from_thread(
                    self._record_polling_error,
                    progress_screen,
                    f"Detached verification could not be submitted: {exc}",
                    True,
                )
                return
            self.call_from_thread(
                self._activate_observation,
                progress_screen,
                observation,
                None,
            )
            self._poll_verification(project, progress_screen, observation)

        self._observer_worker = self.run_worker(
            submit,
            thread=True,
            exclusive=True,
            group="verification-observer",
        )

    def cancel_verification(self) -> None:
        """Persist a cancellation request; the observer remains replaceable."""

        observation = self._active_observation
        screen = self._progress_screen
        if observation is None or screen is None:
            return
        screen.record_cancellation_pending()

        def request() -> None:
            try:
                updated = self.service.request_verification_cancel(
                    observation.job.project_path,
                    observation.job.job_id,
                )
            except Exception as exc:
                self.call_from_thread(
                    self._record_polling_error,
                    screen,
                    f"Cancellation request failed: {exc}",
                    True,
                )
                return
            self.call_from_thread(
                self._activate_observation,
                screen,
                updated,
                "Persistent cancellation request recorded by the backend.",
            )

        self.run_worker(
            request,
            thread=True,
            exclusive=True,
            group="verification-cancel",
        )

    def detach_verification_observer(self) -> None:
        """Stop this client's polling without changing the detached job."""

        if self._observer_worker is not None:
            self._observer_worker.cancel()
        self._observer_worker = None
        self._active_observation = None
        self._progress_screen = None
        self.show_welcome()

    def _attach_to_verification(
        self,
        project: ProjectSummary,
        observation: VerificationJobObservation,
    ) -> None:
        progress_screen = ProgressScreen(
            "Observing detached verification",
            project=project.project_path,
            cancellable=observation.job.cancellable,
            source_in_dropbox=project.source_in_dropbox,
            main_file=project.main_file,
            input_files=project.input_files,
            detached_job=True,
        )
        self.switch_screen(progress_screen)
        self._progress_screen = progress_screen
        self._activate_observation(
            progress_screen,
            observation,
            (
                "Attached to coarse legacy activity; durable per-stage events are "
                "not available."
                if observation.job.attached_legacy
                else "Attached to the backend-owned job and replayed durable progress."
            ),
        )

        def observe() -> None:
            self._poll_verification(project, progress_screen, observation)

        self._observer_worker = self.run_worker(
            observe,
            thread=True,
            exclusive=True,
            group="verification-observer",
        )

    def _activate_observation(
        self,
        screen: ProgressScreen,
        observation: VerificationJobObservation,
        note: str | None,
    ) -> None:
        if self._progress_screen is not screen:
            return
        self._active_observation = observation
        if not self._progress_content_ready(screen):
            self.set_timer(
                0.01,
                lambda: self._activate_observation(screen, observation, note),
            )
            return
        screen.record_observation(observation)
        if note:
            screen.record_observer_note(note)

    def _progress_content_ready(self, screen: ProgressScreen) -> bool:
        return screen.is_mounted and bool(screen.query("#status-line").nodes)

    def _record_polling_error(
        self,
        screen: ProgressScreen,
        message: str,
        job_may_continue: bool,
    ) -> None:
        if self._progress_screen is not screen:
            return
        if not self._progress_content_ready(screen):
            self.set_timer(
                0.01,
                lambda: self._record_polling_error(screen, message, job_may_continue),
            )
            return
        screen.record_polling_error(message, job_may_continue)

    def _poll_verification(
        self,
        project: ProjectSummary,
        screen: ProgressScreen,
        initial: VerificationJobObservation,
    ) -> None:
        worker = get_current_worker()
        observation = initial
        cursor = observation.next_sequence
        while not worker.is_cancelled:
            if observation.job.state.terminal:
                try:
                    snapshot = self.service.resume_project(project.project_path)
                except Exception as exc:
                    self.call_from_thread(
                        self._record_polling_error,
                        screen,
                        f"Job reached {observation.job.state.value}, but the "
                        f"canonical project result could not be loaded: {exc}",
                        False,
                    )
                    return
                if not worker.is_cancelled:
                    self.call_from_thread(self._finish_observed_job, snapshot)
                return

            delay = max(0.01, min(5.0, observation.poll_after_seconds))
            time.sleep(delay)
            if worker.is_cancelled:
                return
            try:
                updated = self.service.observe_verification(
                    project.project_path,
                    after_sequence=cursor,
                )
            except Exception as exc:
                self.call_from_thread(
                    self._record_polling_error,
                    screen,
                    f"Progress polling failed: {exc}",
                    True,
                )
                return
            if updated is None:
                self.call_from_thread(
                    self._record_polling_error,
                    screen,
                    "Progress polling returned no observation for detached job "
                    f"{observation.job.job_id} at {project.project_path}.",
                    True,
                )
                return
            observation = updated
            cursor = observation.next_sequence
            self.call_from_thread(
                self._activate_observation,
                screen,
                observation,
                None,
            )

    def _finish_observed_job(self, snapshot: WorkflowSnapshot) -> None:
        self._active_observation = None
        self._observer_worker = None
        self._progress_screen = None
        self.show_snapshot(snapshot)

    def show_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        self.current_snapshot = snapshot
        state = snapshot.state
        if state == WorkflowState.CHANGE_REVIEW and snapshot.pending_plan is not None:
            self.switch_screen(ChangeReviewScreen(snapshot))
        elif state == WorkflowState.AWAITING_CLARIFICATION and snapshot.clarifications:
            self.switch_screen(ClarificationScreen(snapshot))
        elif (
            state == WorkflowState.FAILED
            and snapshot.findings is not None
            and snapshot.findings.failure_report is not None
        ):
            self.switch_screen(
                FailureDependencyScreen(
                    snapshot, report=snapshot.findings.failure_report
                )
            )
        elif state in {WorkflowState.COMPLETED, WorkflowState.FAILED} and (
            snapshot.findings is not None
        ):
            self.switch_screen(FindingsScreen(snapshot))
        elif state in {WorkflowState.VERIFYING, WorkflowState.BUSY_EXTERNAL}:
            self._discover_snapshot_activity(snapshot)
        elif state in {
            WorkflowState.FAILED,
            WorkflowState.INTERRUPTED,
        }:
            self.switch_screen(RecoveryScreen(snapshot))
        else:
            self.switch_screen(DashboardScreen(snapshot))

    def _discover_snapshot_activity(self, snapshot: WorkflowSnapshot) -> None:
        project = snapshot.project
        progress = ProgressScreen(
            "Attaching to backend verification activity",
            project=project.project_path,
            main_file=project.main_file,
            input_files=project.input_files,
        )
        self.switch_screen(progress)

        def discover() -> None:
            try:
                observation = self.service.observe_verification(
                    project.project_path,
                    after_sequence=0,
                )
            except Exception as exc:
                self.call_from_thread(
                    self.show_error,
                    "Could not observe backend verification activity",
                    str(exc),
                    project.project_path,
                )
                return
            if observation is not None:
                if observation.job.state.terminal:
                    try:
                        terminal_snapshot = self.service.resume_project(
                            project.project_path
                        )
                    except Exception as exc:
                        self.call_from_thread(
                            self.show_error,
                            "Could not load completed verification result",
                            str(exc),
                            project.project_path,
                        )
                        return
                    self.call_from_thread(self.show_snapshot, terminal_snapshot)
                    return
                self.call_from_thread(
                    self._attach_to_verification,
                    project,
                    observation,
                )
                return
            self.call_from_thread(
                self.switch_screen,
                RecoveryScreen(
                    snapshot,
                    detail=(
                        "The backend reports project activity, but no attachable "
                        "verification observation is currently available. This TUI "
                        "owns neither the verification nor its lock."
                    ),
                ),
            )

        self.run_worker(
            discover,
            thread=True,
            exclusive=True,
            group="verification-discovery",
        )

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
