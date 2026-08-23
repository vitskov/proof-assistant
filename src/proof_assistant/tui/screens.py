"""Screens for the Proof Assistant terminal interface."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.syntax import Syntax
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RadioButton,
    RadioSet,
    Static,
    TextArea,
)

from proof_assistant.workflow.contracts import (
    ChangeImpactPlan,
    ClarificationPresentation,
    FileChange,
    NewProjectRequest,
    ProgressEvent,
    ProgressPhase,
    ProjectAvailability,
    ProjectCatalogEntry,
    ProjectDestinationInspection,
    ProjectSummary,
    SourceInspection,
    VerificationSettings,
    WorkflowSnapshot,
    WorkflowState,
)

if TYPE_CHECKING:
    from proof_assistant.tui.app import ProofAssistantApp


def _dropbox_warning(project: ProjectSummary | ChangeImpactPlan) -> str:
    if not project.source_in_dropbox:
        return ""
    return (
        "Dropbox source detected. This is supported: Proof Assistant works from a "
        "stable managed copy. Finish all related edits before confirming a new "
        "iteration."
    )


def _path_text(path: Path | None) -> str:
    return str(path) if path is not None else "not available"


@dataclass(frozen=True)
class NewProjectDraft:
    """TUI-owned form state awaiting backend source inspection."""

    name: str
    source_path: Path
    project_path: Path | None
    task_text: str | None
    settings: VerificationSettings

    def request(
        self, main_file: str, *, resolved_project_path: Path | None = None
    ) -> NewProjectRequest:
        """Create the strict backend request after a main file is selected."""

        return NewProjectRequest(
            name=self.name,
            source_path=self.source_path,
            main_file=main_file,
            project_path=(
                resolved_project_path
                if resolved_project_path is not None
                else self.project_path
            ),
            task_text=self.task_text,
            settings=self.settings,
        )


_PHASE_DESCRIPTIONS: dict[ProgressPhase, str] = {
    ProgressPhase.VALIDATING: "Validate project state and selected main source",
    ProgressPhase.OBSERVING_SOURCE: "Wait for a stable authoritative source tree",
    ProgressPhase.IMPORTING_SOURCE: "Copy the stable source into managed storage",
    ProgressPhase.INDEXING: "Parse claims and the recursive LaTeX input graph",
    ProgressPhase.IMPACT_ANALYSIS: "Determine changed claims and proof descendants",
    ProgressPhase.CACHE_SETUP: "Prepare the shared Lean dependency cache",
    ProgressPhase.LEAN_BUILD: "Build the generated Lean verification project",
    ProgressPhase.LEAN_EXTRACTION: "Extract candidate formal statements",
    ProgressPhase.PROOF_BATCH: "Generate and check proof batches",
    ProgressPhase.CERTIFICATION: "Kernel-check and persist certificates",
    ProgressPhase.REPORTING: "Write durable findings, reports, and logs",
    ProgressPhase.COMPLETE: "Finish the verification iteration",
}

_PHASE_ORDER = tuple(ProgressPhase)


class NoticeScreen(Screen[None]):
    """Base screen with a standard status/notice line."""

    @property
    def proof_app(self) -> ProofAssistantApp:
        return self.app  # type: ignore[return-value]

    def show_notice(self, message: str, *, error: bool = False) -> None:
        try:
            widget = self.query_one("#status-line", Static)
        except Exception:
            return
        widget.update(message)
        widget.set_classes("error" if error else "muted")


class WelcomeScreen(NoticeScreen):
    """Choose a new project or resume a catalogued project."""

    BINDINGS = [("n", "new_project", "New project"), ("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="page"):
            yield Static("Proof Assistant", classes="title")
            yield Static(
                "Create a managed verification project or resume exactly where one "
                "left off."
            )
            with Horizontal(classes="toolbar"):
                yield Button("New project", id="new-project", variant="primary")
                yield Button("Refresh projects", id="refresh-projects")
            yield Static("Existing projects", classes="section")
            yield Vertical(id="project-list")
            yield Static("Loading project catalog…", id="status-line", classes="muted")
        yield Footer()

    def on_mount(self) -> None:
        self.load_projects()

    def action_new_project(self) -> None:
        self.proof_app.show_new_project()

    def action_quit(self) -> None:
        self.proof_app.exit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "new-project":
            self.action_new_project()
        elif button_id == "refresh-projects":
            self.load_projects()
        elif button_id.startswith("resume-"):
            project = event.button.data
            if isinstance(project, Path):
                self.proof_app.resume_project(project)
        elif button_id.startswith("select-existing-main-"):
            entry = event.button.data
            if isinstance(entry, ProjectCatalogEntry):
                self.proof_app.show_existing_project_main_selection(entry)
        elif button_id.startswith("open-catalog-"):
            project = event.button.data
            if isinstance(project, Path):
                self.proof_app.open_location(project)

    def load_projects(self) -> None:
        self.show_notice("Loading project catalog…")

        def load() -> None:
            try:
                projects = tuple(self.proof_app.service.list_projects())
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self.show_notice, f"Could not load projects: {exc}", error=True
                )
                return
            self.proof_app.call_from_thread(self._render_projects, projects)

        self.run_worker(load, thread=True, exclusive=True, group="catalog")

    async def _render_projects(self, projects: tuple[ProjectCatalogEntry, ...]) -> None:
        container = self.query_one("#project-list", Vertical)
        await container.remove_children()
        if not projects:
            await container.mount(
                Static("No projects yet. Choose New project to begin.")
            )
        for index, entry in enumerate(projects):
            project = entry.project
            lines = [entry.name, str(entry.project_path), entry.availability.value]
            if project is not None:
                warning = " · Dropbox source" if project.source_in_dropbox else ""
                lines.extend(
                    [
                        f"Main: {project.main_file}",
                        f"State: {project.workflow_state.value}{warning}",
                    ]
                )
            if entry.issue:
                lines.append(f"Issue: {entry.issue}")
            detail = Static("\n".join(lines), classes="project-summary")
            controls: list[Button] = []
            if entry.resumable:
                button = Button("Resume", id=f"resume-{index}")
                button.data = entry.project_path
                controls.append(button)
            elif entry.availability == ProjectAvailability.NEEDS_MAIN_FILE:
                button = Button("Select main file", id=f"select-existing-main-{index}")
                button.data = entry
                controls.append(button)
            elif entry.availability in {
                ProjectAvailability.INCOMPLETE,
                ProjectAvailability.OCCUPIED,
            }:
                button = Button("Open folder", id=f"open-catalog-{index}")
                button.data = entry.project_path
                controls.append(button)
            await container.mount(Horizontal(detail, *controls, classes="project-row"))
        self.show_notice(f"{len(projects)} project(s) available.")


class NewProjectScreen(NoticeScreen):
    """First wizard step: collect source, destination, and project task."""

    BINDINGS = [("escape", "back", "Back")]

    def __init__(self, draft: NewProjectDraft | None = None) -> None:
        super().__init__()
        self.draft = draft
        self._custom_task = draft is not None and draft.task_text is not None

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="page"):
            yield Static("New verification project", classes="title")
            yield Label("Project name")
            yield Input(
                value=self.draft.name if self.draft is not None else "",
                placeholder="my-paper",
                id="project-name",
            )
            yield Label("Existing manuscript source folder")
            yield Input(
                value=(str(self.draft.source_path) if self.draft is not None else ""),
                placeholder="/absolute/path/to/manuscript",
                id="source-path",
            )
            yield Static(
                "The source may be in Dropbox. Files are copied into a managed, "
                "Git-versioned "
                "project before verification.",
                classes="muted",
            )
            yield Label("Managed project folder (optional)")
            yield Input(
                value=(
                    str(self.draft.project_path)
                    if self.draft is not None and self.draft.project_path is not None
                    else ""
                ),
                placeholder="$HOME/proof-assistant/<project-name>",
                id="project-path",
            )
            yield Static(
                "Managed projects, Python environments, and Lean caches must not "
                "be in Dropbox.",
                classes="warning",
            )
            yield Static("Verification task", classes="section")
            with Horizontal(classes="toolbar"):
                yield Button("Use default task", id="default-task", variant="primary")
                yield Button("Customize task", id="custom-task")
            yield TextArea(
                (
                    self.draft.task_text
                    if self.draft is not None and self.draft.task_text is not None
                    else self.proof_app.service.default_task_text()
                ),
                id="task-editor",
                language="markdown",
                show_line_numbers=True,
                disabled=not self._custom_task,
            )
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Continue: inspect source", id="continue", variant="success"
                )
                yield Button("Cancel", id="cancel")
            yield Static(
                "No project will be created until you review and confirm all settings.",
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def action_back(self) -> None:
        self.proof_app.show_welcome()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "cancel":
            self.action_back()
        elif button_id == "default-task":
            self._custom_task = False
            editor = self.query_one("#task-editor", TextArea)
            editor.text = self.proof_app.service.default_task_text()
            editor.disabled = True
            self.show_notice("The maintained default verification task will be used.")
        elif button_id == "custom-task":
            self._custom_task = True
            editor = self.query_one("#task-editor", TextArea)
            editor.disabled = False
            editor.focus()
            self.show_notice("Edit the project-owned task below.")
        elif button_id == "continue":
            self._continue()

    def _continue(self) -> None:
        name = self.query_one("#project-name", Input).value.strip()
        source_text = self.query_one("#source-path", Input).value.strip()
        project_text = self.query_one("#project-path", Input).value.strip()
        if not name:
            self.show_notice("Enter a project name.", error=True)
            return
        if not source_text:
            self.show_notice("Enter the manuscript source folder.", error=True)
            return
        task_text: str | None = None
        if self._custom_task:
            task_text = self.query_one("#task-editor", TextArea).text.strip()
            if not task_text:
                self.show_notice(
                    "The custom verification task cannot be empty.", error=True
                )
                return
        project_path = Path(project_text).expanduser() if project_text else None
        draft = NewProjectDraft(
            name=name,
            source_path=Path(source_text).expanduser(),
            project_path=project_path,
            task_text=task_text,
            settings=VerificationSettings(),
        )
        self.proof_app.inspect_source_for_project(draft)


class MainFileSelectionScreen(NoticeScreen):
    """Require a deliberate main-file choice for an ambiguous source folder."""

    BINDINGS = [("escape", "back", "Back")]

    def __init__(
        self,
        draft: NewProjectDraft,
        inspection: SourceInspection,
        destination: ProjectDestinationInspection,
        *,
        selected_main: str | None = None,
    ) -> None:
        super().__init__()
        self.draft = draft
        self.inspection = inspection
        self.destination = destination
        self.selected_main = selected_main

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="page"):
            yield Static("Select the manuscript's main LaTeX file", classes="title")
            yield Static(
                f"Source folder: {self.inspection.source_path}\n"
                f"Found {len(self.inspection.candidates)} LaTeX files. Select the "
                "single root document Proof Assistant should verify. Its recursive "
                "\\input and \\include files will be resolved by the backend.",
                markup=False,
            )
            buttons: list[RadioButton] = []
            for index, candidate in enumerate(self.inspection.candidates):
                hints: list[str] = []
                if candidate.has_documentclass:
                    hints.append("contains \\documentclass")
                if candidate.relative_path == self.inspection.suggested_main_file:
                    hints.append("suggested")
                suffix = f"  ({', '.join(hints)})" if hints else ""
                # Intentionally do not preselect the suggestion. Multiple-source
                # projects require a deliberate user decision.
                buttons.append(
                    RadioButton(
                        f"{candidate.relative_path}{suffix}",
                        value=candidate.relative_path == self.selected_main,
                        id=f"main-option-{index}",
                    )
                )
            yield RadioSet(*buttons, id="main-file-options")
            with Horizontal(classes="toolbar"):
                yield Button("Continue to review", id="select-main", variant="success")
                yield Button("Back", id="back")
            yield Static(
                (
                    f"Selected main file: {self.selected_main}"
                    if self.selected_main is not None
                    else "No file is selected yet. The suggestion is only a hint."
                ),
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def action_back(self) -> None:
        self.proof_app.show_new_project(self.draft)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back()
            return
        if event.button.id != "select-main":
            return
        index = self.query_one("#main-file-options", RadioSet).pressed_index
        if index < 0:
            self.show_notice(
                "Select one main LaTeX file before continuing.", error=True
            )
            return
        main_file = self.inspection.candidates[index].relative_path
        self.proof_app.review_new_project(
            self.draft,
            self.inspection,
            self.destination,
            main_file,
            auto_selected=False,
        )


class ProjectReviewScreen(NoticeScreen):
    """Final wizard step before the first persistent project mutation."""

    BINDINGS = [("escape", "back", "Back")]

    def __init__(
        self,
        draft: NewProjectDraft,
        inspection: SourceInspection,
        destination: ProjectDestinationInspection,
        main_file: str,
        *,
        auto_selected: bool,
    ) -> None:
        super().__init__()
        self.draft = draft
        self.inspection = inspection
        self.destination = destination
        self.main_file = main_file
        self.auto_selected = auto_selected

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="page"):
            yield Static("Review new verification project", classes="title")
            if self.inspection.source_in_dropbox:
                yield Static(
                    "Dropbox source detected. This is supported: files will be copied "
                    "into managed project storage before verification.",
                    classes="warning",
                    id="dropbox-warning",
                )
            yield Static(self._detail(), id="project-review", markup=False)
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Confirm, create, and verify",
                    id="confirm-create",
                    variant="success",
                )
                yield Button("Back", id="review-back")
                yield Button("Cancel", id="cancel")
            yield Static(
                "This confirmation creates the managed project and starts its first "
                "verification iteration.",
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def _detail(self) -> str:
        selection = (
            "automatically selected (only LaTeX source found)"
            if self.auto_selected
            else "selected by user"
        )
        task_mode = (
            "custom project task"
            if self.draft.task_text is not None
            else ("maintained default task")
        )
        return (
            f"Project name: {self.draft.name}\n"
            f"Authoritative source: {self.draft.source_path}\n"
            f"Managed project: {self.destination.project_path}\n"
            f"Main LaTeX file: {self.main_file}\n"
            f"Main-file selection: {selection}\n"
            f"LaTeX files discovered: {len(self.inspection.candidates)}\n"
            f"Verification task: {task_mode}"
        )

    def action_back(self) -> None:
        if self.inspection.selection_required:
            self.proof_app.show_main_file_selection(
                self.draft,
                self.inspection,
                self.destination,
                selected_main=self.main_file,
            )
        else:
            self.proof_app.show_new_project(self.draft)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-create":
            self.proof_app.create_project(
                self.draft.request(
                    self.main_file,
                    resolved_project_path=self.destination.project_path,
                )
            )
        elif event.button.id == "review-back":
            self.action_back()
        elif event.button.id == "cancel":
            self.proof_app.show_welcome()


class ExistingProjectMainFileSelectionScreen(NoticeScreen):
    """Recover a catalogued legacy project through backend-provided candidates."""

    BINDINGS = [("escape", "back", "Projects")]

    def __init__(self, entry: ProjectCatalogEntry) -> None:
        super().__init__()
        if entry.availability != ProjectAvailability.NEEDS_MAIN_FILE:
            raise ValueError("existing-project selection requires NEEDS_MAIN_FILE")
        self.entry = entry

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="page"):
            yield Static("Select a main file for the existing project", classes="title")
            yield Static(
                f"Project: {self.entry.project_path}\n"
                f"Source: {_path_text(self.entry.source_path)}\n"
                f"Issue: {self.entry.issue or 'Main-file selection is required.'}",
                markup=False,
            )
            buttons: list[RadioButton] = []
            for index, candidate in enumerate(self.entry.main_file_candidates):
                hints: list[str] = []
                if candidate.has_documentclass:
                    hints.append("contains \\documentclass")
                if candidate.relative_path == self.entry.suggested_main_file:
                    hints.append("suggested")
                suffix = f"  ({', '.join(hints)})" if hints else ""
                buttons.append(
                    RadioButton(
                        f"{candidate.relative_path}{suffix}",
                        value=False,
                        id=f"existing-main-option-{index}",
                    )
                )
            yield RadioSet(*buttons, id="existing-main-file-options")
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Save selected main file",
                    id="confirm-existing-main",
                    variant="success",
                )
                yield Button("Projects", id="back")
            yield Static(
                "Selection is persisted by the backend before the project resumes.",
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def action_back(self) -> None:
        self.proof_app.show_welcome()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back()
            return
        if event.button.id != "confirm-existing-main":
            return
        index = self.query_one("#existing-main-file-options", RadioSet).pressed_index
        if index < 0:
            self.show_notice("Select one main LaTeX file first.", error=True)
            return
        main_file = self.entry.main_file_candidates[index].relative_path
        self.proof_app.select_existing_project_main_file(self.entry, main_file)


class ProjectDestinationConflictScreen(NoticeScreen):
    """Show a backend-classified destination conflict without mutating it."""

    def __init__(
        self,
        draft: NewProjectDraft,
        inspection: ProjectDestinationInspection,
    ) -> None:
        super().__init__()
        self.draft = draft
        self.inspection = inspection

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="page"):
            yield Static("Managed project destination is unavailable", classes="title")
            yield Static(
                f"Resolved project path: {self.inspection.project_path}\n"
                f"Classification: {self.inspection.availability.value}\n"
                f"Issue: {self.inspection.issue or 'The destination is unavailable.'}",
                classes="error",
                id="destination-conflict",
                markup=False,
            )
            with Horizontal(classes="toolbar"):
                yield Button("Back to setup", id="back", variant="primary")
                yield Button("Return to projects", id="projects")
                yield Button("Open folder", id="open-folder")
            yield Static(
                "No source was imported and no project was created.",
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.proof_app.show_new_project(self.draft)
        elif event.button.id == "projects":
            self.proof_app.show_welcome()
        elif event.button.id == "open-folder":
            self.proof_app.open_location(self.inspection.project_path)


class DashboardScreen(NoticeScreen):
    """Project landing page between verification iterations."""

    def __init__(self, snapshot: WorkflowSnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot

    def compose(self) -> ComposeResult:
        project = self.snapshot.project
        yield Header()
        with Vertical(id="page"):
            yield Static(project.name, classes="title")
            yield Static(f"Project: {project.project_path}")
            yield Static(f"Authoritative source: {project.source_path}")
            yield Static(f"Main LaTeX file: {project.main_file}")
            inputs = ", ".join(project.input_files) or "none"
            yield Static(f"Resolved inputs: {inputs}")
            yield Static(f"State: {self.snapshot.state.value}")
            warning = _dropbox_warning(project)
            if warning:
                yield Static(warning, classes="warning", id="dropbox-warning")
            if self.snapshot.error:
                yield Static(self.snapshot.error, classes="error")
            with Horizontal(classes="toolbar"):
                yield Button("Start verification", id="verify", variant="success")
                yield Button(
                    "Check for source changes", id="check-changes", variant="primary"
                )
                yield Button("Open project folder", id="open-project")
                yield Button("Projects", id="projects")
            yield Static("", id="status-line", classes="muted")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        project = self.snapshot.project
        if event.button.id == "verify":
            self.proof_app.start_verification(project, None)
        elif event.button.id == "check-changes":
            self.proof_app.check_for_changes(project)
        elif event.button.id == "open-project":
            self.proof_app.open_location(project.project_path)
        elif event.button.id == "projects":
            self.proof_app.show_welcome()


class ProgressScreen(NoticeScreen):
    """Live view of typed progress events emitted by the workflow service."""

    def __init__(
        self,
        title: str,
        *,
        project: Path | None,
        cancellable: bool = False,
        source_in_dropbox: bool = False,
        main_file: str | None = None,
        input_files: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.heading = title
        self.project = project
        self.cancellable = cancellable
        self.source_in_dropbox = source_in_dropbox
        self.main_file = main_file
        self.input_files = input_files
        self._lines: list[str] = []
        self._current_phase: ProgressPhase | None = None
        self._seen_phases: set[ProgressPhase] = set()
        self._progress_percent = 0.0

    def compose(self) -> ComposeResult:
        yield Header()
        # The outer scroll is intentional: every copyable pane remains usable on
        # a short terminal instead of forcing the full stage list into 24 rows.
        with VerticalScroll(id="page"):
            yield Static(self.heading, classes="title")
            yield TextArea(
                self._source_detail(),
                read_only=True,
                soft_wrap=False,
                id="progress-sources",
            )
            if self.source_in_dropbox:
                yield TextArea(
                    "Dropbox source detected. Proof Assistant is verifying a stable "
                    "managed snapshot; finish all related source edits before the next "
                    "change review.",
                    read_only=True,
                    soft_wrap=True,
                    classes="warning progress-warning",
                    id="dropbox-warning",
                )
            yield ProgressBar(total=100, show_eta=False, id="progress-bar")
            yield TextArea(
                self._stage_detail(),
                read_only=True,
                soft_wrap=True,
                id="progress-stages",
            )
            yield TextArea(
                "Waiting for progress…",
                read_only=True,
                soft_wrap=True,
                id="progress-log",
            )
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Request cooperative cancellation",
                    id="cancel",
                    variant="warning",
                    disabled=not self.cancellable,
                )
            yield TextArea(
                (
                    "Work continues in a background thread. Cancellation is "
                    "cooperative and is confirmed only when the backend returns a "
                    "cancellation report."
                    if self.cancellable
                    else "Work continues in a background thread."
                ),
                read_only=True,
                soft_wrap=True,
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def _source_detail(self) -> str:
        inputs = "\n".join(f"  {path}" for path in self.input_files) or "  none"
        return (
            f"{self.heading}\n"
            f"Project: {_path_text(self.project)}\n"
            f"Main file: {self.main_file or 'not available'}\n"
            f"Resolved input files ({len(self.input_files)}):\n{inputs}"
        )

    def _stage_detail(self) -> str:
        if self._current_phase is None:
            current = "Current stage: waiting for the verifier"
        else:
            current = (
                f"Current stage: {self._current_phase.value} — "
                f"{_PHASE_DESCRIPTIONS[self._current_phase]}"
            )
        rows = []
        for index, phase in enumerate(_PHASE_ORDER, start=1):
            if phase == self._current_phase:
                marker = "active"
            elif phase in self._seen_phases:
                marker = "done"
            else:
                marker = "pending"
            rows.append(
                f"{index:02d}. [{marker:<7}] {phase.value}: "
                f"{_PHASE_DESCRIPTIONS[phase]}"
            )
        return current + "\n\nVerification stages\n" + "\n".join(rows)

    def record_progress(self, event: ProgressEvent) -> None:
        self._current_phase = event.phase
        self._seen_phases.add(event.phase)
        claim = f" [{event.claim_id}]" if event.claim_id else ""
        self._lines.append(
            f"{event.sequence:04d} {event.phase.value}{claim}: {event.message}"
        )
        self._lines = self._lines[-200:]
        self.query_one("#progress-stages", TextArea).text = self._stage_detail()
        self.query_one("#progress-log", TextArea).text = "\n".join(self._lines)
        candidate_percent = self._event_progress_percent(event)
        self._progress_percent = max(self._progress_percent, candidate_percent)
        self.query_one("#progress-bar", ProgressBar).update(
            progress=self._progress_percent
        )

    def _event_progress_percent(self, event: ProgressEvent) -> float:
        """Map typed phases and optional phase-local counts to overall progress."""

        phase_index = _PHASE_ORDER.index(event.phase)
        last_index = len(_PHASE_ORDER) - 1
        if phase_index >= last_index:
            return 100.0
        phase_start = 100.0 * phase_index / last_index
        phase_end = 100.0 * (phase_index + 1) / last_index
        if event.completed is None or event.total is None or event.total <= 0:
            return phase_start
        unit_ratio = max(0.0, min(1.0, event.completed / event.total))
        return phase_start + unit_ratio * (phase_end - phase_start)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" and self.cancellable:
            self.proof_app.cancel_verification()
            event.button.disabled = True
            self.query_one("#status-line", TextArea).text = (
                "Cooperative cancellation requested. Verification is still running "
                "until the backend confirms a stop boundary."
            )


class ClarificationScreen(NoticeScreen):
    """Show exact source context for a persisted clarification request."""

    def __init__(self, snapshot: WorkflowSnapshot, index: int = 0) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.index = max(0, min(index, len(snapshot.clarifications) - 1))

    @property
    def question(self) -> ClarificationPresentation:
        return self.snapshot.clarifications[self.index]

    def compose(self) -> ComposeResult:
        question = self.question
        location = question.location
        yield Header()
        with Vertical(id="page"):
            yield Static(
                "Clarification required "
                f"({self.index + 1}/{len(self.snapshot.clarifications)})",
                classes="title",
            )
            warning = _dropbox_warning(self.snapshot.project)
            if warning:
                yield Static(warning, classes="warning", id="dropbox-warning")
            yield Static(f"{question.headline}\n{question.explanation}")
            yield Static(
                f"Claim: {question.claim_id} · Category: {question.category}\n"
                f"Source: {location.relative_path}:{location.start_line}:"
                f"{location.start_column}\n"
                f"Absolute path: {location.absolute_path}",
                classes="section",
                id="source-location",
            )
            yield Static(self._syntax(question), id="source-excerpt")
            yield Static(self._request_detail(question), id="clarification-detail")
            with Horizontal(classes="toolbar"):
                yield Button("Open exact file", id="open-file", variant="primary")
                yield Button("Open source folder", id="open-folder")
                yield Button(
                    "Check all files for changes", id="check-changes", variant="success"
                )
                yield Button("Previous", id="previous", disabled=self.index == 0)
                yield Button(
                    "Next",
                    id="next",
                    disabled=self.index + 1 >= len(self.snapshot.clarifications),
                )
            yield Static(
                "Edit the authoritative source, finish all related edits, then check "
                "for changes.",
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def _syntax(self, question: ClarificationPresentation) -> Syntax:
        location = question.location
        return Syntax(
            location.excerpt,
            "latex",
            line_numbers=True,
            start_line=location.context_start_line,
            highlight_lines=set(location.highlighted_lines),
            word_wrap=True,
        )

    def _request_detail(self, question: ClarificationPresentation) -> str:
        actions = (
            "\n".join(f"  • {item}" for item in question.requested_actions)
            or "  • Clarify the passage."
        )
        resolutions = (
            "\n".join(f"  • {item}" for item in question.possible_resolutions)
            or "  • No proposed resolution."
        )
        blocked = ", ".join(question.blocked_claims) or "none"
        return (
            f"Requested action\n{actions}\n\nPossible resolutions\n{resolutions}\n\n"
            f"Blocked claims: {blocked}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        question = self.question
        if event.button.id == "open-file":
            self.proof_app.open_location(question.location.absolute_path)
        elif event.button.id == "open-folder":
            self.proof_app.open_location(question.location.absolute_path.parent)
        elif event.button.id == "check-changes":
            self.proof_app.check_for_changes(self.snapshot.project)
        elif event.button.id == "previous" and self.index > 0:
            self.proof_app.switch_screen(
                ClarificationScreen(self.snapshot, self.index - 1)
            )
        elif event.button.id == "next" and self.index + 1 < len(
            self.snapshot.clarifications
        ):
            self.proof_app.switch_screen(
                ClarificationScreen(self.snapshot, self.index + 1)
            )


class ChangeReviewScreen(NoticeScreen):
    """Require explicit confirmation of an immutable source-impact plan."""

    def __init__(self, snapshot: WorkflowSnapshot) -> None:
        super().__init__()
        if snapshot.pending_plan is None:
            raise ValueError("change review requires a pending plan")
        self.snapshot = snapshot
        self.plan = snapshot.pending_plan

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="page"):
            yield Static("Review manuscript changes", classes="title")
            warning = _dropbox_warning(self.plan)
            if warning:
                yield Static(warning, classes="warning", id="dropbox-warning")
            yield Static(
                "Review the complete stable change set. Confirmation revalidates "
                "the source "
                "inventory before the next iteration starts."
            )
            yield Static(self._detail(), id="impact-detail")
            with Horizontal(classes="toolbar"):
                yield Button("Start next iteration", id="confirm", variant="success")
                yield Button(
                    "Keep waiting for more edits", id="wait", variant="primary"
                )
                yield Button("Open source folder", id="open-source")
                yield Button("Projects", id="projects")
            yield Static(
                "Explicit confirmation is required.", id="status-line", classes="muted"
            )
        yield Footer()

    def _detail(self) -> str:
        plan = self.plan
        files = (
            "\n".join(_file_change_line(change) for change in plan.file_changes)
            or "  none"
        )
        direct = (
            "\n".join(
                f"  {impact.kind.value:<11} {impact.claim_id}"
                + (f" ({impact.source_file})" if impact.source_file else "")
                for impact in plan.direct_claim_changes
            )
            or "  none"
        )
        affected = "\n".join(f"  {claim}" for claim in plan.affected_claims) or "  none"
        unaffected = (
            "\n".join(f"  {claim}" for claim in plan.unaffected_certificates)
            or "  none"
        )
        superseded = (
            "\n".join(f"  {question}" for question in plan.superseded_questions)
            or "  none"
        )
        inputs = "\n".join(f"  {path}" for path in plan.input_files) or "  none"
        return (
            f"Selected main file\n  {plan.main_file}\n\n"
            f"Resolved input closure\n{inputs}\n\n"
            f"Files\n{files}\n\nDirect claim changes\n{direct}\n\n"
            f"Full affected proof-tree closure\n{affected}\n\n"
            f"Certificates expected to remain unaffected\n{unaffected}\n\n"
            f"Clarifications superseded by this change\n{superseded}\n\n"
            f"Task changed: {'yes' if plan.task_changed else 'no'}\n"
            f"Main file changed: {'yes' if plan.main_file_changed else 'no'}\n"
            f"Plan: {plan.plan_id}\nInventory: {plan.candidate_inventory_sha256}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.proof_app.start_verification(
                self.snapshot.project,
                self.plan.plan_id,
                self.proof_app.settings,
                main_file=self.plan.main_file,
                input_files=self.plan.input_files,
            )
        elif event.button.id == "wait":
            self.proof_app.switch_screen(DashboardScreen(self.snapshot))
        elif event.button.id == "open-source":
            self.proof_app.open_location(self.plan.source_path)
        elif event.button.id == "projects":
            self.proof_app.show_welcome()


def _file_change_line(change: FileChange) -> str:
    rename = f" <- {change.old_path}" if change.old_path else ""
    return f"  {change.kind.value:<9} {change.path}{rename}"


class FindingsScreen(NoticeScreen):
    """Human-readable outcome and durable output locations."""

    def __init__(self, snapshot: WorkflowSnapshot) -> None:
        super().__init__()
        if snapshot.findings is None:
            raise ValueError("findings screen requires findings")
        self.snapshot = snapshot
        self.findings = snapshot.findings

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="page"):
            yield Static(
                f"Verification finished: {self.findings.outcome}", classes="title"
            )
            warning = _dropbox_warning(self.snapshot.project)
            if warning:
                yield Static(warning, classes="warning", id="dropbox-warning")
            yield Static(self._detail(), id="findings-detail")
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Check for manuscript changes",
                    id="check-changes",
                    variant="primary",
                )
                yield Button(
                    "Open report",
                    id="open-report",
                    disabled=self.findings.report_path is None,
                )
                yield Button("Open project folder", id="open-project")
                yield Button("Projects", id="projects")
            yield Static(
                "All reports, certificates, source snapshots, and logs remain in "
                "the project folder.",
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def _detail(self) -> str:
        finding = self.findings
        groups = [
            ("Verified", finding.verified),
            ("Certificates reused", finding.reused),
            ("Statements reconciled", finding.reconciled),
            ("Unresolved", finding.unresolved),
            ("Suspected false", finding.suspect_false),
            ("Kernel-checked counterexamples", finding.counterexamples),
        ]
        chunks = [finding.detail]
        for heading, claims in groups:
            if claims:
                chunks.append(
                    f"{heading} ({len(claims)})\n" + "\n".join(f"  {c}" for c in claims)
                )
        if finding.dependency_discrepancies:
            chunks.append(
                "Dependency discrepancies\n"
                + "\n".join(
                    f"  {dict(item)}" for item in finding.dependency_discrepancies
                )
            )
        chunks.append(f"Report: {_path_text(finding.report_path)}")
        chunks.append(
            "Project: "
            f"{_path_text(finding.project_path or self.snapshot.project.project_path)}"
        )
        return "\n\n".join(chunks)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "check-changes":
            self.proof_app.check_for_changes(self.snapshot.project)
        elif event.button.id == "open-report" and self.findings.report_path is not None:
            self.proof_app.open_location(self.findings.report_path)
        elif event.button.id == "open-project":
            self.proof_app.open_location(
                self.findings.project_path or self.snapshot.project.project_path
            )
        elif event.button.id == "projects":
            self.proof_app.show_welcome()


class RecoveryScreen(NoticeScreen):
    """Interrupted, failed, or externally busy project recovery."""

    def __init__(
        self,
        snapshot: WorkflowSnapshot | None,
        *,
        title: str | None = None,
        detail: str | None = None,
        project: Path | None = None,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.heading = title or (snapshot.state.value if snapshot else "Error")
        self.detail = (
            detail
            or (snapshot.error if snapshot else None)
            or "No further detail available."
        )
        self.project = project or (snapshot.project.project_path if snapshot else None)

    @classmethod
    def from_error(
        cls, title: str, detail: str, project: Path | None
    ) -> RecoveryScreen:
        return cls(None, title=title, detail=detail, project=project)

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="page"):
            yield Static(self.heading, classes="title")
            if self._has_cancellation_report:
                yield TextArea(
                    self._cancellation_detail(),
                    read_only=True,
                    soft_wrap=True,
                    id="cancellation-report",
                )
            else:
                yield Static(self.detail, classes="error")
            if (
                self.snapshot is not None
                and self.snapshot.state == WorkflowState.BUSY_EXTERNAL
            ):
                yield Static(
                    "Another process owns the project. This screen is read-only; "
                    "retry after it finishes.",
                    classes="warning",
                )
            yield Static(f"Project: {_path_text(self.project)}")
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Retry / recover", id="retry", disabled=self.project is None
                )
                yield Button(
                    "Open project folder",
                    id="open-project",
                    disabled=self.project is None,
                )
                yield Button("Projects", id="projects", variant="primary")
            if self._has_cancellation_report:
                status = (
                    "The backend confirmed the cooperative stop boundary. Resume to "
                    "retry the listed claims."
                )
            elif self._interrupted_without_report:
                status = (
                    "No backend cancellation report was received; certificate "
                    "preservation, retry state, and temporary-worktree cleanup are "
                    "not confirmed."
                )
            else:
                status = "Existing project state is preserved."
            yield Static(status, id="status-line", classes="muted")
        yield Footer()

    @property
    def _has_cancellation_report(self) -> bool:
        return (
            self.snapshot is not None
            and self.snapshot.state == WorkflowState.INTERRUPTED
            and self.snapshot.cancellation is not None
        )

    @property
    def _interrupted_without_report(self) -> bool:
        snapshot_interrupted = (
            self.snapshot is not None
            and self.snapshot.state == WorkflowState.INTERRUPTED
        )
        return (snapshot_interrupted or "interrupt" in self.heading.lower()) and not (
            self._has_cancellation_report
        )

    def _cancellation_detail(self) -> str:
        if self.snapshot is None or self.snapshot.cancellation is None:
            return "No backend cancellation report was received."
        report = self.snapshot.cancellation
        preserved = (
            "\n".join(f"  {claim_id}" for claim_id in report.preserved_certificates)
            or "  none"
        )
        retryable = (
            "\n".join(f"  {claim_id}" for claim_id in report.retryable_claims)
            or "  none"
        )
        cleanup = "yes" if report.temporary_worktrees_cleaned else "no"
        return (
            "Cooperative cancellation confirmed by the backend\n"
            f"Project: {_path_text(self.project)}\n"
            f"Run ID: {report.run_id if report.run_id is not None else 'not available'}\n"
            f"Detail: {report.detail}\n\n"
            f"Preserved certificates ({len(report.preserved_certificates)}):\n"
            f"{preserved}\n\n"
            f"Retryable claims ({len(report.retryable_claims)}):\n{retryable}\n\n"
            f"Temporary worktrees cleaned: {cleanup}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "retry" and self.project is not None:
            self.proof_app.resume_project(self.project)
        elif event.button.id == "open-project" and self.project is not None:
            self.proof_app.open_location(self.project)
        elif event.button.id == "projects":
            self.proof_app.show_welcome()
