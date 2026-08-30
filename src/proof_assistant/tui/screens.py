"""Screens for the Proof Assistant terminal interface."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    MarkdownViewer,
    ProgressBar,
    RadioButton,
    RadioSet,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)
from textual.widgets.tree import TreeNode

from proof_assistant.tui.commands import (
    BACK,
    CANCEL,
    CANCEL_JOB,
    CHECK_CHANGES,
    CLOSE,
    CONFIRM,
    DETACH_JOB,
    FAILURES,
    HOME_FOLDER,
    NEW_PROJECT,
    NEXT,
    OPEN,
    PARENT_FOLDER,
    PREVIOUS,
    REFRESH,
    REPORT,
    RETRY,
    SELECT_ALL,
    SETTINGS,
    VERIFY,
    CommandFooter,
    shortcut_reference_text,
)
from proof_assistant.tui.commands import AppHeader as Header
from proof_assistant.tui.layout import (
    COMPOSITION_CLASSES,
    ActionBar,
    PageHeader,
    PageWorkspace,
    ResponsivePage,
    ResponsiveToolbar,
    ScrollableDialogBody,
    classify_viewport,
)
from proof_assistant.workflow.contracts import (
    ChangeImpactPlan,
    ClarificationPresentation,
    FailureComponent,
    FailureDependencyReport,
    FailureIncident,
    FileChange,
    ManuscriptFolderEntry,
    ManuscriptFolderListing,
    NewProjectRequest,
    ProgressEvent,
    ProgressPhase,
    ProjectAvailability,
    ProjectCatalogEntry,
    ProjectDeletionInspection,
    ProjectDeletionResult,
    ProjectDestinationInspection,
    ProjectSummary,
    ProviderSetupSnapshot,
    ReportDocument,
    SourceInspection,
    VerificationJobObservation,
    VerificationSettings,
    WorkflowSnapshot,
    WorkflowState,
)

if TYPE_CHECKING:
    from proof_assistant.tui.app import ProofAssistantApp
    from proof_assistant.workflow.contracts import LatexSourceCandidate


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


def _candidate_text(
    candidate: LatexSourceCandidate, suggested_main_file: str | None
) -> str:
    hints: list[str] = []
    if candidate.has_documentclass:
        hints.append("contains \\documentclass")
    if candidate.relative_path == suggested_main_file:
        hints.append("suggested")
    suffix = f"  ({', '.join(hints)})" if hints else ""
    return f"{candidate.relative_path}{suffix}"


class CopyableText(TextArea):
    """Read-only selectable text for every externally useful displayed value."""

    BINDINGS = [SELECT_ALL.binding()]

    def __init__(
        self,
        text: str,
        *,
        id: str | None = None,
        classes: str | None = None,
        soft_wrap: bool = True,
        max_lines: int = 12,
        expand: bool = False,
    ) -> None:
        class_names = "copyable-info"
        if classes:
            class_names = f"{class_names} {classes}"
        super().__init__(
            text,
            read_only=True,
            soft_wrap=soft_wrap,
            id=id,
            classes=class_names,
        )
        if not expand:
            line_count = max(1, text.count("\n") + 1)
            border_rows = (
                2 if classes and {"warning", "error"} & set(classes.split()) else 0
            )
            minimum = 3 if border_rows else 1
            self.styles.height = max(minimum, min(max_lines, line_count + border_rows))


class _ProjectActionButton(Button):
    """Button carrying a typed project-catalog action payload."""

    def __init__(self, *args: object, payload: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.payload = payload


@dataclass(frozen=True)
class _FailureSelection:
    """Widget payload referring only to an immutable backend report entry."""

    claim_id: str | None = None
    component_id: str | None = None
    incident_ids: tuple[int, ...] = ()
    shared_reference: bool = False


class FailureTree(Tree[_FailureSelection]):
    """Textual 1.0 tree with complete small-terminal navigation bindings."""

    BINDINGS = [
        *Tree.BINDINGS,
        ("pageup", "page_up", "Previous page"),
        ("pagedown", "page_down", "Next page"),
        ("home", "scroll_home", "First node"),
        ("end", "scroll_end", "Last node"),
    ]


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
            widget = self.query_one("#status-line")
        except Exception:
            return
        if isinstance(widget, TextArea):
            widget.text = message
        elif isinstance(widget, Static):
            widget.update(message)
        widget.remove_class("error", "muted")
        widget.add_class("error" if error else "muted")


class ShortcutHelpScreen(ModalScreen[None]):
    """Complete command reference generated from the binding registry."""

    BINDINGS = [BACK.binding(action="close"), CLOSE.binding()]

    def compose(self) -> ComposeResult:
        with Vertical(id="shortcut-help-dialog"):
            yield CopyableText("Keyboard commands", classes="title")
            with ScrollableDialogBody():
                yield CopyableText(
                    shortcut_reference_text(),
                    id="shortcut-reference",
                    soft_wrap=False,
                    expand=True,
                )
                yield CopyableText(
                    "The footer always shows commands for the active screen and "
                    "focused control.",
                    classes="muted",
                )
        yield CommandFooter()

    def action_close(self) -> None:
        self.dismiss()


class WelcomeScreen(NoticeScreen):
    """Choose a new project or resume a catalogued project."""

    BINDINGS = [
        NEW_PROJECT.binding(),
        REFRESH.binding(),
        SETTINGS.binding(),
    ]

    def __init__(
        self,
        ai_setup: ProviderSetupSnapshot | None = None,
        *,
        ai_setup_supported: bool = False,
    ) -> None:
        super().__init__()
        self.ai_setup = ai_setup
        self.ai_setup_supported = ai_setup_supported

    def _ai_status_text(self) -> str:
        if not self.ai_setup_supported:
            return (
                "AI provider setup: unavailable in this legacy workflow service. "
                "Existing project controls remain available."
            )
        if self.ai_setup is None:
            return "AI provider setup: checking through the backend…"
        return (
            f"AI: {self.ai_setup.primary_driver.value} — "
            f"{'ready' if self.ai_setup.primary_ready else 'NOT READY'}. "
            f"{self.ai_setup.detail}"
        )

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText("Proof Assistant", classes="title")
                yield CopyableText(
                    self._ai_status_text(),
                    id="landing-ai-provider-status",
                    classes=(
                        "muted"
                        if self.ai_setup is None or self.ai_setup.primary_ready
                        else "warning"
                    ),
                )
            with PageWorkspace():
                yield CopyableText(
                    "Verify a LaTeX manuscript in a backend-managed project. Source "
                    "paths, project paths, findings, and status text remain selectable "
                    "for terminal copy and paste."
                )
                yield CopyableText(
                    "Open or resume an existing project", classes="section"
                )
                yield CopyableText(
                    "Resume continues from durable backend state. Opening this TUI "
                    "does not take ownership of a project or its verification job.",
                    classes="muted",
                )
                yield Vertical(id="project-list")
            with ActionBar():
                yield Button(
                    "New project…",
                    id="new-project",
                    variant="primary",
                    disabled=(
                        self.ai_setup_supported
                        and (self.ai_setup is None or not self.ai_setup.primary_ready)
                    ),
                )
                yield Button(
                    "AI providers…",
                    id="landing-ai-providers",
                    disabled=not self.ai_setup_supported,
                )
                yield Button("Settings", id="settings")
                yield Button("Refresh list", id="refresh-projects")
                yield CopyableText(
                    "Loading project catalog…", id="status-line", classes="muted"
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.load_projects()

    def action_new_project(self) -> None:
        if self.ai_setup_supported and (
            self.ai_setup is None or not self.ai_setup.primary_ready
        ):
            self.show_notice(
                "Set up a ready primary AI driver before starting verification.",
                error=True,
            )
            self.proof_app.show_ai_provider_settings(self.ai_setup)
            return
        self.proof_app.show_new_project()

    def action_refresh(self) -> None:
        self.load_projects()

    def action_settings(self) -> None:
        self.proof_app.show_settings(return_to_project=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "new-project":
            self.action_new_project()
        elif button_id == "refresh-projects":
            self.action_refresh()
        elif button_id == "settings":
            self.action_settings()
        elif button_id == "landing-ai-providers":
            self.proof_app.show_ai_provider_settings(self.ai_setup)
        elif button_id.startswith("resume-"):
            project = (
                event.button.payload
                if isinstance(event.button, _ProjectActionButton)
                else None
            )
            if isinstance(project, ProjectSummary):
                self.proof_app.resume_project(project)
        elif button_id.startswith("select-existing-main-"):
            entry = (
                event.button.payload
                if isinstance(event.button, _ProjectActionButton)
                else None
            )
            if isinstance(entry, ProjectCatalogEntry):
                self.proof_app.show_existing_project_main_selection(entry)
        elif button_id.startswith("delete-project-"):
            project = (
                event.button.payload
                if isinstance(event.button, _ProjectActionButton)
                else None
            )
            if isinstance(project, ProjectSummary):
                self.proof_app.request_project_deletion(project)
        elif button_id.startswith("open-catalog-"):
            project = (
                event.button.payload
                if isinstance(event.button, _ProjectActionButton)
                else None
            )
            if isinstance(project, Path):
                self.proof_app.open_location(project)

    def record_ai_setup(self, snapshot: ProviderSetupSnapshot) -> None:
        """Refresh the landing card from a sanitized backend DTO."""

        self.ai_setup = snapshot
        self.ai_setup_supported = True
        status_nodes = self.query("#landing-ai-provider-status").nodes
        if status_nodes and isinstance(status_nodes[0], TextArea):
            status_nodes[0].text = self._ai_status_text()
            status_nodes[0].remove_class("warning", "muted")
            status_nodes[0].add_class("muted" if snapshot.primary_ready else "warning")
        new_nodes = self.query("#new-project").nodes
        if new_nodes and isinstance(new_nodes[0], Button):
            new_nodes[0].disabled = not snapshot.primary_ready
        provider_nodes = self.query("#landing-ai-providers").nodes
        if provider_nodes and isinstance(provider_nodes[0], Button):
            provider_nodes[0].disabled = False

    def record_ai_setup_error(self, detail: str) -> None:
        status_nodes = self.query("#landing-ai-provider-status").nodes
        if status_nodes and isinstance(status_nodes[0], TextArea):
            status_nodes[0].text = (
                "AI provider setup could not be checked. Existing projects and "
                f"reports remain accessible. Detail: {detail}"
            )
            status_nodes[0].remove_class("muted")
            status_nodes[0].add_class("warning")

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
            self.proof_app.call_from_thread(self._start_render_projects, projects)

        self.run_worker(load, thread=True, exclusive=True, group="catalog")

    def _start_render_projects(self, projects: tuple[ProjectCatalogEntry, ...]) -> None:
        self.run_worker(self._render_projects(projects), group="catalog-render")

    async def _render_projects(self, projects: tuple[ProjectCatalogEntry, ...]) -> None:
        container = self.query_one("#project-list", Vertical)
        await container.remove_children()
        if not projects:
            await container.mount(
                CopyableText("No projects yet. Choose New project to begin.")
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
            detail = CopyableText("\n".join(lines), classes="project-summary")
            controls: list[Button] = []
            if entry.resumable:
                button = _ProjectActionButton(
                    "Resume / open",
                    id=f"resume-{index}",
                    variant="success",
                    payload=project,
                )
                controls.append(button)
                delete_button = _ProjectActionButton(
                    "Delete project",
                    id=f"delete-project-{index}",
                    variant="error",
                    payload=project,
                )
                controls.append(delete_button)
            elif entry.availability == ProjectAvailability.NEEDS_MAIN_FILE:
                button = _ProjectActionButton(
                    "Select main file",
                    id=f"select-existing-main-{index}",
                    payload=entry,
                )
                controls.append(button)
            elif entry.availability in {
                ProjectAvailability.INCOMPLETE,
                ProjectAvailability.OCCUPIED,
            }:
                button = _ProjectActionButton(
                    "Open folder",
                    id=f"open-catalog-{index}",
                    payload=entry.project_path,
                )
                controls.append(button)
            await container.mount(Horizontal(detail, *controls, classes="project-row"))
        self.show_notice(f"{len(projects)} project(s) available.")


class ProjectDeletionConfirmationScreen(ModalScreen[bool]):
    """Cancel-first button confirmation for backend-owned recoverable deletion."""

    BINDINGS = [CANCEL.binding()]

    def __init__(
        self,
        project: ProjectSummary,
        inspection: ProjectDeletionInspection,
    ) -> None:
        super().__init__()
        self.project = project
        self.inspection = inspection

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-project-dialog"):
            yield CopyableText("Delete managed project?", classes="title")
            with ScrollableDialogBody():
                yield CopyableText(
                    f"Project name: {self.project.name}\n"
                    "Managed project selected for recoverable deletion: "
                    f"{self.inspection.project_path}\n"
                    "External manuscript source (untouched): "
                    f"{_path_text(self.inspection.source_path)}\n"
                    f"Backend preflight: {self.inspection.availability.value}",
                    id="delete-project-paths",
                    max_lines=6,
                )
                yield CopyableText(
                    "Only the managed project folder will be moved to Proof "
                    "Assistant's recoverable deletion storage. The external "
                    "manuscript source will not be changed, moved, or deleted. The "
                    "managed project remains recoverable until you manually remove "
                    "the returned destination.",
                    classes="warning",
                    id="delete-project-safety",
                    max_lines=6,
                )
                if self.inspection.source_in_dropbox:
                    yield CopyableText(
                        "The external manuscript source is in Dropbox; it remains "
                        "completely untouched.",
                        classes="warning",
                        id="delete-project-dropbox",
                    )
                if self.inspection.issue:
                    yield CopyableText(
                        f"Backend preflight issue: {self.inspection.issue}",
                        classes=(
                            "error" if not self.inspection.can_delete else "warning"
                        ),
                        id="delete-project-issue",
                        max_lines=5,
                    )
            with ActionBar():
                yield Button("Cancel", id="delete-project-cancel", variant="primary")
                yield Button(
                    "Delete managed project (recoverable)",
                    id="delete-project-confirm",
                    variant="error",
                    disabled=not self.inspection.can_delete,
                )
            yield CopyableText(
                (
                    "Deletion is refused by the backend preflight. Cancel and resolve "
                    "the issue above."
                    if not self.inspection.can_delete
                    else "Cancel is focused by default. Review the paths above, then "
                    "activate Delete managed project (recoverable) to continue."
                ),
                id="delete-project-status",
                classes="muted" if self.inspection.can_delete else "error",
                max_lines=4,
            )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_cancel)

    def _focus_cancel(self) -> None:
        buttons = self.query("#delete-project-cancel")
        if buttons.nodes:
            self.query_one("#delete-project-cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delete-project-cancel":
            self.action_cancel()
        elif event.button.id == "delete-project-confirm":
            if self.inspection.can_delete:
                self.dismiss(True)


class ProjectDeletionOutcomeScreen(NoticeScreen):
    """Copyable success or failure result returned by the deletion contract."""

    BINDINGS = [BACK.binding(action="projects")]

    def __init__(
        self,
        project: ProjectSummary,
        *,
        inspection: ProjectDeletionInspection | None = None,
        result: ProjectDeletionResult | None = None,
        error: str | None = None,
    ) -> None:
        super().__init__()
        self.project = project
        self.inspection = inspection
        self.result = result
        self.error = error

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText(
                    (
                        "Managed project moved to recoverable deletion storage"
                        if self.result is not None
                        else "Project deletion failed"
                    ),
                    classes=(
                        "title success" if self.result is not None else "title error"
                    ),
                )
            with PageWorkspace():
                if self.result is not None:
                    yield CopyableText(
                        f"Former managed project path: {self.result.project_path}\n"
                        "Recoverable deletion destination: "
                        f"{self.result.trash_path}\n"
                        "External manuscript source (untouched): "
                        f"{self.result.source_path}\n"
                        f"Deleted at: {self.result.deleted_at}\n"
                        f"Recoverable: {'yes' if self.result.recoverable else 'no'}",
                        id="delete-project-result",
                        classes="success",
                        max_lines=8,
                    )
                    yield CopyableText(
                        "The external manuscript source was not changed, moved, or "
                        "deleted. The managed project can be recovered from the "
                        "returned destination until you manually remove that "
                        "destination.",
                        id="delete-project-result-safety",
                        max_lines=4,
                    )
                else:
                    source = (
                        self.inspection.source_path
                        if self.inspection is not None
                        else self.project.source_path
                    )
                    yield CopyableText(
                        f"Managed project: {self.project.project_path}\n"
                        "External manuscript source (untouched): "
                        f"{_path_text(source)}\n"
                        f"Error: {self.error or 'Deletion was not completed.'}",
                        id="delete-project-error",
                        classes="error",
                        max_lines=10,
                    )
            with ActionBar():
                yield Button(
                    "Return to refreshed projects",
                    id="deletion-projects",
                    variant="primary",
                )
                if self.result is None:
                    yield Button("Retry preflight", id="deletion-retry")
                yield CopyableText(
                    "Returning reloads the catalog.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_projects)

    def _focus_projects(self) -> None:
        buttons = self.query("#deletion-projects")
        if buttons.nodes:
            self.query_one("#deletion-projects", Button).focus()

    def action_projects(self) -> None:
        self.proof_app.show_welcome()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "deletion-projects":
            self.action_projects()
        elif event.button.id == "deletion-retry":
            self.proof_app.request_project_deletion(self.project)


class ManuscriptFolderPickerScreen(NoticeScreen):
    """SSH-safe directory traversal over backend-provided folder listings."""

    BINDINGS = [
        CANCEL.binding(),
        PARENT_FOLDER.binding(),
        HOME_FOLDER.binding(),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.listing: ManuscriptFolderListing | None = None
        self._rows: dict[str, ManuscriptFolderEntry] = {}
        self._selected: ManuscriptFolderEntry | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText("Choose manuscript source folder", classes="title")
                yield CopyableText(
                    "Loading the backend-owned starting folder…",
                    id="folder-picker-current",
                    max_lines=4,
                )
            with PageWorkspace():
                yield DataTable(
                    show_row_labels=False,
                    cursor_type="row",
                    zebra_stripes=True,
                    id="folder-picker-table",
                )
                yield CopyableText(
                    "No child folder selected.",
                    id="folder-picker-selection",
                    classes="muted",
                    max_lines=3,
                )
            with ActionBar(id="folder-picker-controls"):
                yield Button("Up", id="folder-picker-parent", disabled=True)
                yield Button("Home", id="folder-picker-home")
                yield Button(
                    "Open selected",
                    id="folder-picker-open",
                    disabled=True,
                )
                yield Button(
                    "Select current folder",
                    id="folder-picker-use",
                    variant="success",
                    disabled=True,
                )
                yield Button("Cancel", id="folder-picker-cancel")
                yield CopyableText(
                    "Enter opens; Backspace goes up; Ctrl+Home returns home.",
                    id="status-line",
                    classes="muted",
                    max_lines=3,
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        table = self.query_one("#folder-picker-table", DataTable)
        table.add_columns("Folder", "Resolved path")
        table.focus()
        self._load(None)

    def _load(self, directory: Path | None) -> None:
        self.show_notice("Loading folders…")
        self.query_one("#folder-picker-use", Button).disabled = True
        self.query_one("#folder-picker-open", Button).disabled = True

        def load() -> None:
            try:
                listing = self.proof_app.service.browse_manuscript_folders(directory)
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self.show_notice,
                    f"Could not browse folders: {exc}",
                    error=True,
                )
                return
            self.proof_app.call_from_thread(self._show_listing, listing)

        self.run_worker(load, thread=True, exclusive=True, group="folder-picker")

    def _show_listing(self, listing: ManuscriptFolderListing) -> None:
        self.listing = listing
        self._rows.clear()
        self._selected = None
        table = self.query_one("#folder-picker-table", DataTable)
        table.clear(columns=False)
        for index, folder in enumerate(listing.folders):
            key = f"folder:{index}"
            suffix = " →" if folder.symlink else ""
            table.add_row(folder.name + suffix, str(folder.path), key=key)
            self._rows[key] = folder
        origin = {
            "PREFERENCE": "saved manuscript-folder preference",
            "HOME_FALLBACK": "home fallback (no valid saved preference)",
            "REQUESTED": "selected navigation location",
        }.get(listing.origin.value, listing.origin.value)
        self.query_one(
            "#folder-picker-current", TextArea
        ).text = f"Current folder: {listing.directory}\nStarting source: {origin}"
        self.query_one("#folder-picker-selection", TextArea).text = (
            "No child folders are available. You may still use the current folder."
            if not listing.folders
            else "Highlight a child folder and press Enter to open it."
        )
        self.query_one("#folder-picker-parent", Button).disabled = (
            listing.parent is None
        )
        self.query_one("#folder-picker-use", Button).disabled = False
        self.query_one("#folder-picker-open", Button).disabled = True
        self.show_notice(f"{len(listing.folders)} child folder(s) available.")
        table.focus()
        if listing.folders:
            table.move_cursor(row=0, column=0, animate=False)

    def _highlight(self, key: str | None) -> None:
        self._selected = self._rows.get(key or "")
        self.query_one("#folder-picker-open", Button).disabled = self._selected is None
        self.query_one("#folder-picker-selection", TextArea).text = (
            f"Selected child folder: {self._selected.path}"
            if self._selected is not None
            else "No child folder selected."
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "folder-picker-table":
            self._highlight(event.row_key.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "folder-picker-table":
            return
        self._highlight(event.row_key.value)
        self._open_selected()

    def _open_selected(self) -> None:
        if self._selected is None:
            self.show_notice("Highlight a child folder to open it.", error=True)
            return
        self._load(self._selected.path)

    def action_parent(self) -> None:
        if self.listing is not None and self.listing.parent is not None:
            self._load(self.listing.parent)

    def action_home_folder(self) -> None:
        if self.listing is not None:
            self._load(self.listing.home)

    def _select_current(self) -> None:
        if self.listing is None:
            return
        selected = self.listing.directory
        self.query_one("#folder-picker-use", Button).disabled = True
        self.show_notice("Saving selected manuscript folder…")

        def save() -> None:
            try:
                persisted = self.proof_app.service.remember_manuscript_folder(selected)
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._selection_failed,
                    f"Could not select manuscript folder: {exc}",
                )
                return
            self.proof_app.call_from_thread(self.dismiss, persisted)

        self.run_worker(save, thread=True, exclusive=True, group="folder-picker-select")

    def _selection_failed(self, message: str) -> None:
        self.query_one("#folder-picker-use", Button).disabled = False
        self.show_notice(message, error=True)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "folder-picker-parent":
            self.action_parent()
        elif event.button.id == "folder-picker-home":
            self.action_home_folder()
        elif event.button.id == "folder-picker-open":
            self._open_selected()
        elif event.button.id == "folder-picker-use":
            self._select_current()
        elif event.button.id == "folder-picker-cancel":
            self.action_cancel()


class NewProjectScreen(NoticeScreen):
    """First wizard step: collect source, destination, and project task."""

    BINDINGS = [BACK.binding(), CONFIRM.binding(action="continue")]

    def __init__(self, draft: NewProjectDraft | None = None) -> None:
        super().__init__()
        self.draft = draft
        self._custom_task = draft is not None and draft.task_text is not None

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText("New verification project", classes="title")
                yield CopyableText(
                    "Step 1 of 3 · Source, destination, and verification task",
                    classes="muted",
                )
            with PageWorkspace():
                yield Label("Project name")
                yield Input(
                    value=(self.draft.name if self.draft is not None else ""),
                    placeholder="my-paper",
                    id="project-name",
                )
                yield Label("Existing manuscript source folder")
                with ResponsiveToolbar(id="source-folder-controls"):
                    yield Input(
                        value=(
                            str(self.draft.source_path)
                            if self.draft is not None
                            else ""
                        ),
                        placeholder="/absolute/path/to/manuscript",
                        id="source-path",
                    )
                    yield Button("Browse folders", id="browse-source")
                yield CopyableText(
                    "The source may be in Dropbox. Files are copied into a managed, "
                    "Git-versioned project before verification.",
                    classes="muted",
                )
                yield Label("Managed project folder (optional)")
                yield Input(
                    value=(
                        str(self.draft.project_path)
                        if self.draft is not None
                        and self.draft.project_path is not None
                        else ""
                    ),
                    placeholder="$HOME/proof-assistant/<project-name>",
                    id="project-path",
                )
                yield CopyableText(
                    "Managed projects, Python environments, and Lean caches must "
                    "not be in Dropbox.",
                    classes="warning",
                )
                yield CopyableText("Verification task", classes="section")
                with ResponsiveToolbar():
                    yield Button(
                        "Use default task", id="default-task", variant="primary"
                    )
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
            with ActionBar():
                yield Button(
                    "Continue: inspect source", id="continue", variant="success"
                )
                yield Button("Cancel", id="cancel")
                yield CopyableText(
                    "No project is created before review.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def action_back(self) -> None:
        self.proof_app.show_welcome()

    def action_continue(self) -> None:
        self._continue()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "cancel":
            self.action_back()
        elif button_id == "browse-source":
            self.proof_app.push_screen(
                ManuscriptFolderPickerScreen(),
                callback=self._folder_selected,
            )
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
            self.action_continue()

    def _folder_selected(self, folder: Path | None) -> None:
        if folder is None:
            self.show_notice("Folder selection canceled; the typed path is unchanged.")
            return
        self.query_one("#source-path", Input).value = str(folder)
        self.show_notice(f"Selected manuscript source folder: {folder}")

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
            settings=self.proof_app.service.default_verification_settings(),
        )
        self.proof_app.inspect_source_for_project(draft)


class MainFileSelectionScreen(NoticeScreen):
    """Require a deliberate main-file choice for an ambiguous source folder."""

    BINDINGS = [BACK.binding(), CONFIRM.binding()]

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
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText(
                    "Select the manuscript's main LaTeX file", classes="title"
                )
                yield CopyableText(
                    f"Step 2 of 3 · Source: {self.inspection.source_path}",
                    classes="muted",
                )
            with PageWorkspace():
                yield CopyableText(
                    f"Found {len(self.inspection.candidates)} LaTeX files. Select the "
                    "single root document Proof Assistant should verify. Its "
                    "recursive \\input and \\include files are resolved by the "
                    "backend."
                )
                yield CopyableText(
                    "Candidate LaTeX files:\n"
                    + "\n".join(
                        "  "
                        + _candidate_text(
                            candidate, self.inspection.suggested_main_file
                        )
                        for candidate in self.inspection.candidates
                    ),
                    id="main-file-candidates-copy",
                    max_lines=12,
                )
                buttons: list[RadioButton] = []
                for index, candidate in enumerate(self.inspection.candidates):
                    # Suggestions remain hints; ambiguous sources require a choice.
                    buttons.append(
                        RadioButton(
                            _candidate_text(
                                candidate, self.inspection.suggested_main_file
                            ),
                            value=candidate.relative_path == self.selected_main,
                            id=f"main-option-{index}",
                        )
                    )
                yield RadioSet(*buttons, id="main-file-options")
            with ActionBar():
                yield Button("Continue to review", id="select-main", variant="success")
                yield Button("Back", id="back")
                yield CopyableText(
                    (
                        f"Selected: {self.selected_main}"
                        if self.selected_main is not None
                        else "No file selected; suggestion is only a hint."
                    ),
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def action_back(self) -> None:
        self.proof_app.show_new_project(self.draft)

    def action_confirm(self) -> None:
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back()
            return
        if event.button.id == "select-main":
            self.action_confirm()


class ProjectReviewScreen(NoticeScreen):
    """Final wizard step before the first persistent project mutation."""

    BINDINGS = [BACK.binding(), CONFIRM.binding()]

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
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText("Review new verification project", classes="title")
                yield CopyableText(
                    "Step 3 of 3 · Confirm before managed storage is created",
                    classes="muted",
                )
            with PageWorkspace():
                if self.inspection.source_in_dropbox:
                    yield CopyableText(
                        "Dropbox source detected. This is supported: files will be "
                        "copied into managed project storage before verification.",
                        classes="warning",
                        id="dropbox-warning",
                    )
                yield CopyableText(self._detail(), id="project-review")
            with ActionBar():
                yield Button(
                    "Confirm, create, and verify",
                    id="confirm-create",
                    variant="success",
                )
                yield Button("Back", id="review-back")
                yield Button("Cancel", id="cancel")
                yield CopyableText(
                    "Confirm creates the project and starts verification.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

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

    def action_confirm(self) -> None:
        self.proof_app.create_project(
            self.draft.request(
                self.main_file,
                resolved_project_path=self.destination.project_path,
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-create":
            self.action_confirm()
        elif event.button.id == "review-back":
            self.action_back()
        elif event.button.id == "cancel":
            self.proof_app.show_welcome()


class ExistingProjectMainFileSelectionScreen(NoticeScreen):
    """Recover a catalogued legacy project through backend-provided candidates."""

    BINDINGS = [BACK.binding(), CONFIRM.binding()]

    def __init__(self, entry: ProjectCatalogEntry) -> None:
        super().__init__()
        if entry.availability != ProjectAvailability.NEEDS_MAIN_FILE:
            raise ValueError("existing-project selection requires NEEDS_MAIN_FILE")
        self.entry = entry

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText(
                    "Select a main file for the existing project", classes="title"
                )
                yield CopyableText(
                    f"Project: {self.entry.project_path}", classes="muted"
                )
            with PageWorkspace():
                yield CopyableText(
                    f"Source: {_path_text(self.entry.source_path)}\n"
                    f"Issue: {self.entry.issue or 'Main-file selection is required.'}"
                )
                yield CopyableText(
                    "Candidate LaTeX files:\n"
                    + "\n".join(
                        "  "
                        + _candidate_text(candidate, self.entry.suggested_main_file)
                        for candidate in self.entry.main_file_candidates
                    ),
                    id="existing-main-file-candidates-copy",
                    max_lines=12,
                )
                buttons: list[RadioButton] = []
                for index, candidate in enumerate(self.entry.main_file_candidates):
                    buttons.append(
                        RadioButton(
                            _candidate_text(candidate, self.entry.suggested_main_file),
                            value=False,
                            id=f"existing-main-option-{index}",
                        )
                    )
                yield RadioSet(*buttons, id="existing-main-file-options")
            with ActionBar():
                yield Button(
                    "Save selected main file",
                    id="confirm-existing-main",
                    variant="success",
                )
                yield Button("Projects", id="back")
                yield CopyableText(
                    "The backend persists selection before resume.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def action_back(self) -> None:
        self.proof_app.show_welcome()

    def action_confirm(self) -> None:
        index = self.query_one("#existing-main-file-options", RadioSet).pressed_index
        if index < 0:
            self.show_notice("Select one main LaTeX file first.", error=True)
            return
        main_file = self.entry.main_file_candidates[index].relative_path
        self.proof_app.select_existing_project_main_file(self.entry, main_file)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back()
            return
        if event.button.id == "confirm-existing-main":
            self.action_confirm()


class ProjectDestinationConflictScreen(NoticeScreen):
    """Show a backend-classified destination conflict without mutating it."""

    BINDINGS = [BACK.binding()]

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
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText(
                    "Managed project destination is unavailable", classes="title"
                )
            with PageWorkspace():
                yield CopyableText(
                    f"Resolved project path: {self.inspection.project_path}\n"
                    f"Classification: {self.inspection.availability.value}\n"
                    f"Issue: {self.inspection.issue or 'The destination is unavailable.'}",
                    classes="error",
                    id="destination-conflict",
                )
            with ActionBar():
                yield Button("Back to setup", id="back", variant="primary")
                yield Button("Return to projects", id="projects")
                yield Button("Open folder", id="open-folder")
                yield CopyableText(
                    "No source imported; no project created.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back()
        elif event.button.id == "projects":
            self.proof_app.show_welcome()
        elif event.button.id == "open-folder":
            self.proof_app.open_location(self.inspection.project_path)

    def action_back(self) -> None:
        self.proof_app.show_new_project(self.draft)


class DashboardScreen(NoticeScreen):
    """Project landing page between verification iterations."""

    BINDINGS = [
        VERIFY.binding(),
        CHECK_CHANGES.binding(),
        OPEN.binding(),
        SETTINGS.binding(),
        BACK.binding(),
    ]

    def __init__(self, snapshot: WorkflowSnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot

    def compose(self) -> ComposeResult:
        project = self.snapshot.project
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText(project.name, classes="title")
                yield CopyableText(
                    f"State: {self.snapshot.state.value} · Main: {project.main_file}",
                    classes="muted",
                )
            with PageWorkspace():
                yield CopyableText(f"Project: {project.project_path}")
                yield CopyableText(f"Authoritative source: {project.source_path}")
                inputs = ", ".join(project.input_files) or "none"
                yield CopyableText(f"Resolved inputs: {inputs}")
                warning = _dropbox_warning(project)
                if warning:
                    yield CopyableText(warning, classes="warning", id="dropbox-warning")
                if self.snapshot.error:
                    yield CopyableText(self.snapshot.error, classes="error")
            with ActionBar():
                yield Button("Start verification", id="verify", variant="success")
                yield Button(
                    "Check for source changes", id="check-changes", variant="primary"
                )
                yield Button("Open project folder", id="open-project")
                yield Button("Settings", id="settings")
                yield Button("Projects", id="projects")
                yield CopyableText("", id="status-line", classes="muted")
        yield CommandFooter()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "verify":
            self.action_verify()
        elif event.button.id == "check-changes":
            self.action_check_changes()
        elif event.button.id == "open-project":
            self.action_open()
        elif event.button.id == "settings":
            self.action_settings()
        elif event.button.id == "projects":
            self.action_back()

    def action_verify(self) -> None:
        self.proof_app.start_verification(self.snapshot.project, None)

    def action_check_changes(self) -> None:
        self.proof_app.check_for_changes(self.snapshot.project)

    def action_open(self) -> None:
        self.proof_app.open_location(self.snapshot.project.project_path)

    def action_settings(self) -> None:
        self.proof_app.show_settings(
            project=self.snapshot.project.project_path,
            return_to_project=True,
        )

    def action_back(self) -> None:
        self.proof_app.show_welcome()


class ProgressScreen(NoticeScreen):
    """Live view of typed progress events emitted by the workflow service."""

    CSS = """
    #progress-view-switcher {
        display: none;
        height: 3;
    }
    #progress-panels {
        width: 100%;
        height: auto;
        layout: vertical;
    }
    .progress-panel {
        width: 100%;
        height: auto;
    }
    #progress-live-panels {
        width: 100%;
        height: auto;
        layout: vertical;
    }
    #progress-actions {
        width: auto;
        height: 3;
    }
    ProgressScreen #status-line {
        width: 1fr;
        height: 3;
        margin: 0;
    }
    ProgressScreen.-h-compact #progress-workspace,
    ProgressScreen.compact-short #progress-workspace,
    ProgressScreen.wide #progress-workspace {
        overflow: hidden;
    }
    ProgressScreen.-h-compact #progress-view-switcher,
    ProgressScreen.compact-short #progress-view-switcher {
        display: block;
        layout: horizontal;
    }
    ProgressScreen.-h-compact #progress-panels,
    ProgressScreen.-h-compact #progress-live-panels,
    ProgressScreen.-h-compact .progress-panel,
    ProgressScreen.compact-short #progress-panels,
    ProgressScreen.compact-short #progress-live-panels,
    ProgressScreen.compact-short .progress-panel {
        height: 1fr;
    }
    ProgressScreen.-h-compact #progress-source-panel,
    ProgressScreen.-h-compact #progress-stage-panel,
    ProgressScreen.compact-short #progress-source-panel,
    ProgressScreen.compact-short #progress-stage-panel {
        display: none;
    }
    ProgressScreen.-h-compact.show-progress-stages #progress-event-panel,
    ProgressScreen.-h-compact.show-progress-sources #progress-event-panel,
    ProgressScreen.compact-short.show-progress-stages #progress-event-panel,
    ProgressScreen.compact-short.show-progress-sources #progress-event-panel {
        display: none;
    }
    ProgressScreen.-h-compact.show-progress-stages #progress-stage-panel,
    ProgressScreen.-h-compact.show-progress-sources #progress-source-panel,
    ProgressScreen.compact-short.show-progress-stages #progress-stage-panel,
    ProgressScreen.compact-short.show-progress-sources #progress-source-panel {
        display: block;
    }
    ProgressScreen.-h-compact #progress-sources,
    ProgressScreen.-h-compact #progress-stages,
    ProgressScreen.-h-compact #progress-log,
    ProgressScreen.compact-short #progress-sources,
    ProgressScreen.compact-short #progress-stages,
    ProgressScreen.compact-short #progress-log {
        height: 1fr;
        min-height: 1;
        margin-bottom: 0;
    }
    ProgressScreen.-h-compact #progress-actions-bar,
    ProgressScreen.compact-short #progress-actions-bar {
        height: 7;
        max-height: 7;
        layout: vertical;
    }
    ProgressScreen.-h-compact #progress-actions,
    ProgressScreen.compact-short #progress-actions {
        width: 100%;
    }
    ProgressScreen.wide #progress-panels {
        height: 1fr;
    }
    ProgressScreen.wide #progress-source-panel {
        height: auto;
        max-height: 12;
    }
    ProgressScreen.wide #progress-live-panels {
        height: 1fr;
        layout: horizontal;
    }
    ProgressScreen.wide #progress-stage-panel {
        width: 2fr;
        height: 1fr;
        padding-right: 1;
    }
    ProgressScreen.wide #progress-event-panel {
        width: 3fr;
        height: 1fr;
    }
    ProgressScreen.wide #progress-stages,
    ProgressScreen.wide #progress-log {
        height: 1fr;
        min-height: 1;
        margin-bottom: 0;
    }
    """

    BINDINGS = [CANCEL_JOB.binding(), DETACH_JOB.binding()]

    def on_mount(self) -> None:
        self._sync_composition(self.app.size.width, self.app.size.height)

    def on_resize(self, event: Resize) -> None:
        self._sync_composition(event.size.width, event.size.height)

    def _sync_composition(self, width: int, height: int) -> None:
        self.remove_class(*COMPOSITION_CLASSES)
        self.add_class(classify_viewport(width, height).value)

    def __init__(
        self,
        title: str,
        *,
        project: Path | None,
        cancellable: bool = False,
        source_in_dropbox: bool = False,
        main_file: str | None = None,
        input_files: tuple[str, ...] = (),
        detached_job: bool = False,
    ) -> None:
        super().__init__()
        self.heading = title
        self.project = project
        self.cancellable = cancellable
        self.source_in_dropbox = source_in_dropbox
        self.main_file = main_file
        self.input_files = input_files
        self.detached_job = detached_job
        self._lines: list[str] = []
        self._event_sequences: set[int] = set()
        self._observation: VerificationJobObservation | None = None
        self._observer_error: tuple[str, bool] | None = None
        self._client_detached = False
        self._current_phase: ProgressPhase | None = None
        self._seen_phases: set[ProgressPhase] = set()
        self._progress_percent = 0.0

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText(self.heading, classes="title")
                yield ProgressBar(total=100, show_eta=False, id="progress-bar")
            with PageWorkspace(id="progress-workspace"):
                with ResponsiveToolbar(id="progress-view-switcher"):
                    yield Button("Events", id="show-progress-events", variant="primary")
                    yield Button("Stages", id="show-progress-stages")
                    yield Button("Sources", id="show-progress-sources")
                with Vertical(id="progress-panels"):
                    with Vertical(id="progress-source-panel", classes="progress-panel"):
                        yield TextArea(
                            self._source_detail(),
                            read_only=True,
                            soft_wrap=False,
                            id="progress-sources",
                        )
                        if self.source_in_dropbox:
                            yield TextArea(
                                "Dropbox source detected. Proof Assistant is verifying a "
                                "stable managed snapshot; finish all related source edits "
                                "before the next change review.",
                                read_only=True,
                                soft_wrap=True,
                                classes="warning progress-warning",
                                id="dropbox-warning",
                            )
                    with Horizontal(id="progress-live-panels"):
                        with Vertical(
                            id="progress-stage-panel", classes="progress-panel"
                        ):
                            yield TextArea(
                                self._stage_detail(),
                                read_only=True,
                                soft_wrap=True,
                                id="progress-stages",
                            )
                        with Vertical(
                            id="progress-event-panel", classes="progress-panel"
                        ):
                            yield TextArea(
                                "Waiting for progress…",
                                read_only=True,
                                soft_wrap=True,
                                id="progress-log",
                            )
            with ActionBar(id="progress-actions-bar"):
                with Horizontal(id="progress-actions"):
                    yield Button(
                        "Request cooperative cancellation",
                        id="cancel",
                        variant="warning",
                        disabled=not self.cancellable or self.detached_job,
                    )
                    if self.detached_job:
                        yield Button(
                            "Detach to projects",
                            id="detach-observer",
                            variant="primary",
                        )
                yield TextArea(
                    self._observer_status(),
                    read_only=True,
                    soft_wrap=True,
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def _source_detail(self) -> str:
        inputs = "\n".join(f"  {path}" for path in self.input_files) or "  none"
        detail = (
            f"{self.heading}\n"
            f"Project: {_path_text(self.project)}\n"
            f"Main file: {self.main_file or 'not available'}\n"
            f"Resolved input files ({len(self.input_files)}):\n{inputs}"
        )
        if self._observation is None:
            return detail
        job = self._observation.job
        settings = job.settings
        attachment = (
            "legacy coarse read-only" if job.attached_legacy else "durable detached job"
        )
        launch_command = (
            shlex.join(job.launch_command) if job.launch_command else "not reported"
        )
        return (
            f"{detail}\n"
            f"Job ID: {job.job_id}\n"
            f"Job state: {job.state.value}\n"
            f"Attachment: {attachment}\n"
            f"Worker PID: {job.pid if job.pid is not None else 'not available'}\n"
            f"Worker log: {job.worker_log_path or 'not reported'}\n"
            f"Launch command: {launch_command}\n"
            f"Parallel proof jobs: {settings.jobs if settings is not None else 'not reported'}\n"
            f"Last heartbeat: {job.heartbeat_at or 'not reported'}\n"
            f"Job error: {job.error or 'none'}\n"
            f"Durable event cursor: {self._observation.next_sequence}"
        )

    def _observer_status(self) -> str:
        if not self.detached_job:
            return "The backend operation continues while this screen is open."
        if self._client_detached:
            return (
                "This TUI observer is detached. The backend-owned verification was "
                "not cancelled and may still be running. Use F2 Main menu and "
                "Resume / open to attach again."
            )
        if self._observer_error is not None:
            message, job_may_continue = self._observer_error
            continuation = (
                " Detached verification may still be running; this client has "
                "stopped polling only."
                if job_may_continue
                else " The detached job is terminal."
            )
            return f"Observer error: {message}.{continuation}"
        if self._observation is not None and self._observation.job.attached_legacy:
            return (
                "Coarse read-only legacy attachment. This TUI owns neither the "
                "verification nor its lock. Closing or detaching stops polling only; "
                "backend verification continues."
            )
        if (
            self._observation is not None
            and self._observation.job.state.value == "CANCEL_REQUESTED"
        ):
            return (
                "Persistent cancellation request recorded by the backend. The job "
                "remains active until it reaches a cooperative stop boundary. The "
                "request survives all clients; closing or detaching stops polling only."
            )
        return (
            "This TUI only observes a detached backend job. Closing or detaching "
            "stops polling only; verification continues. Cancellation requests are "
            "persisted by the backend and survive clients."
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
        return (
            f"{current}\nProgress: {self._progress_percent:.1f}%"
            "\n\nVerification stages\n" + "\n".join(rows)
        )

    def record_progress(self, event: ProgressEvent) -> None:
        if event.sequence in self._event_sequences:
            return
        self._event_sequences.add(event.sequence)
        self._current_phase = event.phase
        self._seen_phases.add(event.phase)
        claim = f" [{event.claim_id}]" if event.claim_id else ""
        self._lines.append(
            f"{event.sequence:04d} {event.phase.value}{claim}: {event.message}"
        )
        self._lines = self._lines[-200:]
        candidate_percent = self._event_progress_percent(event)
        self._progress_percent = max(self._progress_percent, candidate_percent)
        self.query_one("#progress-stages", TextArea).text = self._stage_detail()
        self.query_one("#progress-log", TextArea).text = "\n".join(self._lines)
        self.query_one("#progress-bar", ProgressBar).update(
            progress=self._progress_percent
        )

    def record_observation(self, observation: VerificationJobObservation) -> None:
        self._observation = observation
        self.heading = (
            "Observing legacy backend verification"
            if observation.job.attached_legacy
            else "Observing detached verification"
        )
        self.query_one(".title", TextArea).text = self.heading
        self.query_one("#progress-sources", TextArea).text = self._source_detail()
        cancel = self.query_one("#cancel", Button)
        cancel.disabled = (
            not observation.job.cancellable
            or observation.job.state.terminal
            or observation.job.state.value == "CANCEL_REQUESTED"
        )
        self.refresh_bindings()
        for event in observation.events:
            self.record_progress(event)
        if observation.job.attached_legacy and not self._lines:
            self.query_one("#progress-log", TextArea).text = (
                "Legacy backend activity is running. Durable per-stage events are "
                "not available; this client is polling coarse lifecycle state."
            )
        self.query_one("#status-line", TextArea).text = self._observer_status()

    def record_observer_note(self, note: str) -> None:
        self._lines.append(f"observer: {note}")
        self._lines = self._lines[-200:]
        self.query_one("#progress-log", TextArea).text = "\n".join(self._lines)
        self.query_one(
            "#status-line", TextArea
        ).text = f"{note}\n{self._observer_status()}"

    def record_cancellation_pending(self) -> None:
        self.query_one("#cancel", Button).disabled = True
        self.refresh_bindings()
        self.query_one("#status-line", TextArea).text = (
            "Submitting a persistent cancellation request. The detached job remains "
            "active until the backend records and reaches a cooperative stop boundary."
        )

    def record_client_detached(self) -> None:
        """Render durable-job semantics after global client navigation."""

        self._client_detached = True
        self._lines.append(
            "observer: detached locally; backend job ownership and state unchanged"
        )
        self._lines = self._lines[-200:]
        status_nodes = self.query("#status-line").nodes
        if status_nodes and isinstance(status_nodes[0], TextArea):
            status_nodes[0].text = self._observer_status()
        log_nodes = self.query("#progress-log").nodes
        if log_nodes and isinstance(log_nodes[0], TextArea):
            log_nodes[0].text = "\n".join(self._lines)
        for selector in ("#cancel", "#detach-observer"):
            nodes = self.query(selector).nodes
            if nodes and isinstance(nodes[0], Button):
                nodes[0].disabled = True
        if self.is_mounted:
            self.refresh_bindings()

    def record_polling_error(self, message: str, job_may_continue: bool) -> None:
        self._observer_error = (message, job_may_continue)
        self._lines.append(f"observer error: {message}")
        self._lines = self._lines[-200:]
        self.query_one("#progress-log", TextArea).text = "\n".join(self._lines)
        status = self.query_one("#status-line", TextArea)
        status.text = self._observer_status()
        status.remove_class("muted")
        status.add_class("error")

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
        if event.button.id == "show-progress-events":
            self._show_progress_view("events")
        elif event.button.id == "show-progress-stages":
            self._show_progress_view("stages")
        elif event.button.id == "show-progress-sources":
            self._show_progress_view("sources")
        elif event.button.id == "cancel" and self.cancellable:
            self.action_cancel_job()
        elif event.button.id == "detach-observer" and self.detached_job:
            self.action_detach_job()

    def _show_progress_view(self, view: str) -> None:
        """Select one compact progress peer without changing workflow state."""

        self.remove_class("show-progress-stages", "show-progress-sources")
        if view == "stages":
            self.add_class("show-progress-stages")
        elif view == "sources":
            self.add_class("show-progress-sources")
        variants = {
            "events": "primary" if view == "events" else "default",
            "stages": "primary" if view == "stages" else "default",
            "sources": "primary" if view == "sources" else "default",
        }
        for peer, variant in variants.items():
            self.query_one(f"#show-progress-{peer}", Button).variant = variant

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "cancel_job":
            buttons = self.query("#cancel").nodes
            return bool(
                self.cancellable
                and not self._client_detached
                and buttons
                and isinstance(buttons[0], Button)
                and not buttons[0].disabled
            )
        if action == "detach_job":
            return self.detached_job and not self._client_detached
        return True

    def action_cancel_job(self) -> None:
        if self.check_action("cancel_job", ()):
            self.proof_app.cancel_verification()

    def action_detach_job(self) -> None:
        if self.detached_job:
            self.proof_app.detach_verification_observer()


class ClarificationScreen(NoticeScreen):
    """Show exact source context for a persisted clarification request."""

    CSS = """
    #clarification-primary-actions,
    #clarification-navigation-actions {
        width: auto;
        height: 3;
        overflow: hidden;
    }
    #clarification-view-switcher {
        display: none;
        height: 3;
    }
    #clarification-panels {
        width: 100%;
        height: auto;
        layout: vertical;
    }
    .clarification-panel {
        width: 100%;
        height: auto;
    }
    ClarificationScreen #status-line {
        width: 1fr;
        height: 1;
        margin: 0;
    }
    ClarificationScreen.-h-compact #clarification-view-switcher,
    ClarificationScreen.compact-short #clarification-view-switcher {
        display: block;
        layout: horizontal;
    }
    ClarificationScreen.-h-compact #clarification-resolution-panel,
    ClarificationScreen.compact-short #clarification-resolution-panel {
        display: none;
    }
    ClarificationScreen.-h-compact.show-resolution #clarification-source-panel,
    ClarificationScreen.compact-short.show-resolution #clarification-source-panel {
        display: none;
    }
    ClarificationScreen.-h-compact.show-resolution #clarification-resolution-panel,
    ClarificationScreen.compact-short.show-resolution #clarification-resolution-panel {
        display: block;
    }
    ClarificationScreen.-h-compact #clarification-actions,
    ClarificationScreen.compact-short #clarification-actions {
        height: 7;
        max-height: 7;
        layout: vertical;
    }
    ClarificationScreen.-h-compact #clarification-primary-actions,
    ClarificationScreen.-h-compact #clarification-navigation-actions,
    ClarificationScreen.compact-short #clarification-primary-actions,
    ClarificationScreen.compact-short #clarification-navigation-actions {
        width: 100%;
    }
    ClarificationScreen.standard #clarification-panels,
    ClarificationScreen.wide #clarification-panels {
        layout: horizontal;
    }
    ClarificationScreen.standard .clarification-panel,
    ClarificationScreen.wide .clarification-panel {
        width: 1fr;
        padding-right: 1;
    }
    """

    BINDINGS = [
        PREVIOUS.binding(),
        NEXT.binding(),
        CHECK_CHANGES.binding(),
        OPEN.binding(),
    ]

    def on_mount(self) -> None:
        self._sync_composition(self.app.size.width, self.app.size.height)

    def on_resize(self, event: Resize) -> None:
        self._sync_composition(event.size.width, event.size.height)

    def _sync_composition(self, width: int, height: int) -> None:
        self.remove_class(*COMPOSITION_CLASSES)
        self.add_class(classify_viewport(width, height).value)

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
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText(
                    "Clarification required "
                    f"({self.index + 1}/{len(self.snapshot.clarifications)})",
                    classes="title",
                )
                yield CopyableText(
                    f"{question.claim_id} · {question.category} · "
                    f"{location.relative_path}:{location.start_line}",
                    classes="muted",
                )
            with PageWorkspace():
                with ResponsiveToolbar(id="clarification-view-switcher"):
                    yield Button(
                        "Source context",
                        id="show-clarification-source",
                        variant="primary",
                    )
                    yield Button(
                        "Resolution guidance", id="show-clarification-resolution"
                    )
                with Horizontal(id="clarification-panels"):
                    with Vertical(
                        id="clarification-source-panel", classes="clarification-panel"
                    ):
                        warning = _dropbox_warning(self.snapshot.project)
                        if warning:
                            yield CopyableText(
                                warning, classes="warning", id="dropbox-warning"
                            )
                        yield CopyableText(
                            f"Claim: {question.claim_id} · "
                            f"Category: {question.category}\n"
                            f"Source: {location.relative_path}:{location.start_line}:"
                            f"{location.start_column}\n"
                            f"Absolute path: {location.absolute_path}",
                            classes="section",
                            id="source-location",
                        )
                        yield Static(self._syntax(question), id="source-excerpt")
                        yield CopyableText(
                            location.excerpt,
                            id="source-excerpt-copy",
                            soft_wrap=True,
                            max_lines=10,
                        )
                    with Vertical(
                        id="clarification-resolution-panel",
                        classes="clarification-panel",
                    ):
                        yield CopyableText(
                            f"{question.headline}\n{question.explanation}"
                        )
                        yield CopyableText(
                            self._request_detail(question), id="clarification-detail"
                        )
            with ActionBar(id="clarification-actions"):
                with Horizontal(id="clarification-primary-actions"):
                    yield Button("Open exact file", id="open-file", variant="primary")
                    yield Button("Open source folder", id="open-folder")
                    yield Button(
                        "Check all files for changes",
                        id="check-changes",
                        variant="success",
                    )
                with Horizontal(id="clarification-navigation-actions"):
                    yield Button("Previous", id="previous", disabled=self.index == 0)
                    yield Button(
                        "Next",
                        id="next",
                        disabled=(self.index + 1 >= len(self.snapshot.clarifications)),
                    )
                yield CopyableText(
                    "Finish related edits, then check changes.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

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
        if event.button.id == "show-clarification-source":
            self.remove_class("show-resolution")
            self.query_one("#show-clarification-source", Button).variant = "primary"
            self.query_one("#show-clarification-resolution", Button).variant = "default"
            self.query_one(PageWorkspace).scroll_home(animate=False)
        elif event.button.id == "show-clarification-resolution":
            self.add_class("show-resolution")
            self.query_one("#show-clarification-source", Button).variant = "default"
            self.query_one("#show-clarification-resolution", Button).variant = "primary"
            self.query_one(PageWorkspace).scroll_home(animate=False)
        elif event.button.id == "open-file":
            self.action_open()
        elif event.button.id == "open-folder":
            self.proof_app.open_location(self.question.location.absolute_path.parent)
        elif event.button.id == "check-changes":
            self.action_check_changes()
        elif event.button.id == "previous" and self.index > 0:
            self.action_previous()
        elif event.button.id == "next" and self.index + 1 < len(
            self.snapshot.clarifications
        ):
            self.action_next()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "previous":
            return self.index > 0
        if action == "next":
            return self.index + 1 < len(self.snapshot.clarifications)
        return True

    def action_previous(self) -> None:
        if self.index > 0:
            self.proof_app.switch_screen(
                ClarificationScreen(self.snapshot, self.index - 1)
            )

    def action_next(self) -> None:
        if self.index + 1 < len(self.snapshot.clarifications):
            self.proof_app.switch_screen(
                ClarificationScreen(self.snapshot, self.index + 1)
            )

    def action_check_changes(self) -> None:
        self.proof_app.check_for_changes(self.snapshot.project)

    def action_open(self) -> None:
        self.proof_app.open_location(self.question.location.absolute_path)


class ChangeReviewScreen(NoticeScreen):
    """Require explicit confirmation of an immutable source-impact plan."""

    BINDINGS = [
        CONFIRM.binding(),
        BACK.binding(action="wait"),
        OPEN.binding(),
    ]

    def __init__(self, snapshot: WorkflowSnapshot) -> None:
        super().__init__()
        if snapshot.pending_plan is None:
            raise ValueError("change review requires a pending plan")
        self.snapshot = snapshot
        self.plan = snapshot.pending_plan

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText("Review manuscript changes", classes="title")
                yield CopyableText(
                    f"Plan {self.plan.plan_id} · {len(self.plan.file_changes)} file "
                    "change(s)",
                    classes="muted",
                )
            with PageWorkspace():
                warning = _dropbox_warning(self.plan)
                if warning:
                    yield CopyableText(warning, classes="warning", id="dropbox-warning")
                yield CopyableText(
                    "Review the complete stable change set. Confirmation revalidates "
                    "the source inventory before the next iteration starts."
                )
                yield CopyableText(self._detail(), id="impact-detail", expand=True)
            with ActionBar():
                yield Button("Start next iteration", id="confirm", variant="success")
                yield Button(
                    "Keep waiting for more edits", id="wait", variant="primary"
                )
                yield Button("Open source folder", id="open-source")
                yield Button("Projects", id="projects")
                yield CopyableText(
                    "Explicit confirmation required.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

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
            f"Managed project\n  {plan.project_path}\n\n"
            f"Authoritative source\n  {plan.source_path}\n\n"
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
            self.action_confirm()
        elif event.button.id == "wait":
            self.action_wait()
        elif event.button.id == "open-source":
            self.action_open()
        elif event.button.id == "projects":
            self.proof_app.show_welcome()

    def action_confirm(self) -> None:
        self.proof_app.start_verification(
            self.snapshot.project,
            self.plan.plan_id,
            main_file=self.plan.main_file,
            input_files=self.plan.input_files,
        )

    def action_wait(self) -> None:
        self.proof_app.switch_screen(DashboardScreen(self.snapshot))

    def action_open(self) -> None:
        self.proof_app.open_location(self.plan.source_path)


def _file_change_line(change: FileChange) -> str:
    rename = f" <- {change.old_path}" if change.old_path else ""
    return f"  {change.kind.value:<9} {change.path}{rename}"


class FindingsScreen(NoticeScreen):
    """Human-readable outcome and durable output locations."""

    BINDINGS = [
        CHECK_CHANGES.binding(),
        REPORT.binding(),
        FAILURES.binding(),
        OPEN.binding(),
        BACK.binding(),
    ]

    def __init__(self, snapshot: WorkflowSnapshot) -> None:
        super().__init__()
        if snapshot.findings is None:
            raise ValueError("findings screen requires findings")
        self.snapshot = snapshot
        self.findings = snapshot.findings

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText(
                    f"Verification finished: {self.findings.outcome}", classes="title"
                )
                yield CopyableText(
                    f"{len(self.findings.verified)} verified · "
                    f"{len(self.findings.unresolved)} unresolved · "
                    f"{len(self.findings.counterexamples)} counterexample(s)",
                    classes="muted",
                )
            with PageWorkspace():
                warning = _dropbox_warning(self.snapshot.project)
                if warning:
                    yield CopyableText(warning, classes="warning", id="dropbox-warning")
                yield CopyableText(self._detail(), id="findings-detail", expand=True)
            with ActionBar():
                yield Button(
                    "Check for manuscript changes",
                    id="check-changes",
                    variant="primary",
                )
                yield Button(
                    "View report in terminal",
                    id="open-report",
                    disabled=self.findings.report_path is None,
                )
                yield Button("Load failure analysis", id="open-failures")
                yield Button("Open project folder", id="open-project")
                yield Button("Projects", id="projects")
                yield CopyableText(
                    "Artifacts remain in the project folder.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

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
            self.action_check_changes()
        elif event.button.id == "open-report" and self.findings.report_path is not None:
            self.action_report()
        elif event.button.id == "open-failures":
            self.action_failures()
        elif event.button.id == "open-project":
            self.action_open()
        elif event.button.id == "projects":
            self.action_back()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "report":
            return self.findings.report_path is not None
        return True

    def action_check_changes(self) -> None:
        self.proof_app.check_for_changes(self.snapshot.project)

    def action_report(self) -> None:
        if self.findings.report_path is not None:
            self.proof_app.view_report(self.snapshot)

    def action_failures(self) -> None:
        self.proof_app.view_failure_report(self.snapshot)

    def action_open(self) -> None:
        self.proof_app.open_location(
            self.findings.project_path or self.snapshot.project.project_path
        )

    def action_back(self) -> None:
        self.proof_app.show_welcome()


_FAILURE_STATES = frozenset(
    {
        "FAILED_TECHNICAL",
        "FAILED_FORMALIZATION",
        "UNRESOLVED",
        "SUSPECT_FALSE",
        "COUNTEREXAMPLE_FOUND",
        "INVALIDATED",
    }
)
_BLOCKED_STATES = frozenset(
    {
        "BLOCKED_DEPENDENCY",
        "BLOCKED_BY_GLOBAL_INCIDENT",
        "NEEDS_CLARIFICATION",
        "DISCOVERED",
        "STATEMENT_DRAFTED",
        "STATEMENT_APPROVED",
        "READY_TO_PROVE",
        "PROVING",
        "DIRTY_SOURCE",
    }
)


def _failure_status(
    state: str, *, blocker: bool = False, incident_ids: tuple[int, ...] = ()
) -> tuple[str, str]:
    """Return a redundant text-and-color presentation for a backend state."""

    normalized = state.upper()
    if blocker or incident_ids or normalized in _FAILURE_STATES:
        return "FAIL", "bold red"
    if normalized in _BLOCKED_STATES:
        return "BLOCKED", "bold yellow"
    if normalized == "CERTIFIED":
        return "OK", "bold green"
    return "BLOCKED", "bold yellow"


def _status_label(
    label: str,
    state: str,
    *,
    blocker: bool = False,
    incident_ids: tuple[int, ...] = (),
) -> Text:
    status, style = _failure_status(state, blocker=blocker, incident_ids=incident_ids)
    return Text.assemble(Text(f"[{status}] ", style=style), Text(label))


class FailureDependencyScreen(NoticeScreen):
    """Interactive terminal explanation of one backend-owned failure report."""

    BINDINGS = [BACK.binding(), CLOSE.binding(), REPORT.binding()]

    def __init__(
        self,
        snapshot: WorkflowSnapshot,
        *,
        report: FailureDependencyReport | None = None,
        error: str | None = None,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.report = report
        self.error = error
        self._graph_nodes = (
            {node.claim_id: node for node in report.nodes} if report else {}
        )
        self._incidents = (
            {incident.incident_id: incident for incident in report.incidents}
            if report
            else {}
        )
        self._components = (
            {component.component_id: component for component in report.components}
            if report
            else {}
        )
        self._cycle_rows: dict[str, _FailureSelection] = {}
        self._first_tree_node: TreeNode[_FailureSelection] | None = None
        self._tree = self._make_tree() if report and not report.has_cycles else None
        self._component_table: DataTable[str | Text] | None = (
            self._make_component_table() if report and report.has_cycles else None
        )

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                if self.report is None:
                    yield CopyableText("Failure dependency analysis", classes="title")
                else:
                    yield CopyableText(
                        self._compact_header(),
                        id="failure-report-meta",
                        max_lines=4 if self.report.has_cycles else 3,
                    )
            with PageWorkspace():
                if self.report is None:
                    yield CopyableText(
                        f"Project: {self.snapshot.project.project_path}\n"
                        "Failure-report error: "
                        f"{self.error or 'No failure report is available.'}",
                        classes="error",
                        id="failure-report-error",
                        max_lines=10,
                    )
                else:
                    with TabbedContent(initial="failure-map-pane", id="failure-tabs"):
                        with TabPane(
                            (
                                "Cycle components"
                                if self.report.has_cycles
                                else "Proof tree"
                            ),
                            id="failure-map-pane",
                        ):
                            if self.report.has_cycles:
                                if self._component_table is not None:
                                    yield self._component_table
                            elif self._tree is not None:
                                yield self._tree
                        with TabPane("Exact reason", id="failure-detail-pane"):
                            yield CopyableText(
                                self._initial_detail(),
                                soft_wrap=True,
                                id="failure-detail",
                                expand=True,
                            )
                        with TabPane(
                            "Copyable full outline", id="failure-outline-pane"
                        ):
                            yield CopyableText(
                                self._full_outline(),
                                soft_wrap=True,
                                id="failure-outline",
                                expand=True,
                            )
            with ActionBar():
                yield Button("Back", id="failure-back", variant="primary")
                yield Button(
                    "View verification report",
                    id="failure-open-report",
                    disabled=(
                        self.snapshot.findings is None
                        or self.snapshot.findings.report_path is None
                    ),
                )
                yield Button("Close to projects", id="failure-close")
                yield CopyableText(
                    "Enter opens reason; Ctrl+A/C copies text.",
                    id="status-line",
                    classes="muted",
                    max_lines=2,
                )
        yield CommandFooter()

    def _make_tree(self) -> FailureTree:
        assert self.report is not None
        tree = FailureTree(
            Text.assemble(
                Text("[FAIL] ", style="bold red"),
                Text(f"Verification run {self.report.run_id}"),
            ),
            data=_FailureSelection(
                incident_ids=(
                    (self.report.primary_incident_id,)
                    if self.report.primary_incident_id is not None
                    else ()
                )
            ),
            id="failure-tree",
        )
        tree.auto_expand = False
        tree.root.expand()

        pending = [(tree.root, item, 0) for item in reversed(self.report.outline)]
        while pending:
            parent, item, depth = pending.pop()
            suffix = " (shared reference)" if item.shared_reference else ""
            selection = _FailureSelection(
                claim_id=item.claim_id,
                incident_ids=item.incident_ids,
                shared_reference=item.shared_reference,
            )
            node = parent.add(
                _status_label(
                    f"{item.claim_id}{suffix}",
                    item.state,
                    blocker=item.blocker,
                    incident_ids=item.incident_ids,
                ),
                data=selection,
                expand=depth < 1,
                allow_expand=bool(item.children),
            )
            if self._first_tree_node is None:
                self._first_tree_node = node
            pending.extend(
                (node, child, depth + 1) for child in reversed(item.children)
            )
        return tree

    def _make_component_table(self) -> DataTable[str | Text]:
        assert self.report is not None
        table: DataTable[str | Text] = DataTable(
            show_row_labels=False,
            cursor_type="row",
            zebra_stripes=True,
            id="failure-components",
        )
        table.add_columns("Status", "Component / claim", "State / role")
        for component in self.report.components:
            component_key = f"component:{component.component_id}"
            status, style = _failure_status(
                "BLOCKED_DEPENDENCY",
                blocker=component.blocker,
                incident_ids=component.incident_ids,
            )
            table.add_row(
                Text(f"[{status}]", style=style),
                component.component_id,
                "cyclic component" if component.cyclic else "component",
                key=component_key,
            )
            self._cycle_rows[component_key] = _FailureSelection(
                component_id=component.component_id,
                incident_ids=component.incident_ids,
            )
            for member_index, claim_id in enumerate(component.members):
                graph_node = self._graph_nodes.get(claim_id)
                state = graph_node.state if graph_node is not None else "BLOCKED"
                incidents = graph_node.incident_ids if graph_node is not None else ()
                member_status, member_style = _failure_status(
                    state,
                    blocker=component.blocker and bool(incidents),
                    incident_ids=incidents,
                )
                row_key = f"member:{component.component_id}:{member_index}:{claim_id}"
                table.add_row(
                    Text(f"[{member_status}]", style=member_style),
                    f"  {claim_id}",
                    state,
                    key=row_key,
                )
                self._cycle_rows[row_key] = _FailureSelection(
                    claim_id=claim_id,
                    incident_ids=incidents,
                )
        return table

    def _report_meta(self) -> str:
        assert self.report is not None
        first_blocker = self.report.first_blocker
        blocker_text = (
            " -> ".join(first_blocker.claims)
            if first_blocker is not None
            else "not available"
        )
        mode = "cycle component/edge fallback" if self.report.has_cycles else "tree"
        global_incidents = (
            ", ".join(str(value) for value in self.report.global_incident_ids) or "none"
        )
        return (
            f"Project: {self.snapshot.project.project_path}\n"
            f"Run ID: {self.report.run_id}\n"
            f"Snapshot: {self.report.snapshot or 'not available'}\n"
            f"Outcome: {self.report.outcome}\n"
            f"Graph presentation: {mode}\n"
            f"Run/batch incident IDs: {global_incidents}\n"
            f"First blocker path: {blocker_text}\n"
            f"Detail: {self.report.detail}"
        )

    def _compact_header(self) -> str:
        assert self.report is not None
        incidents = ",".join(str(value) for value in self.report.global_incident_ids)
        incident_text = f" | Incidents: {incidents}" if incidents else ""
        header = (
            "Failure dependency analysis\n"
            f"Project: {self.snapshot.project.project_path}\n"
            f"Run: {self.report.run_id} | Outcome: {self.report.outcome}"
            f"{incident_text}"
        )
        if self.report.has_cycles:
            return header + "\n[CYCLE] Flat components/edges; no inferred tree."
        return header + " | Mode: tree"

    def _initial_detail(self) -> str:
        assert self.report is not None
        if self.report.primary_incident_id is not None:
            incident = self._incidents.get(self.report.primary_incident_id)
            if incident is not None:
                return "Primary failure incident\n\n" + self._incident_text(incident)
        return self._report_meta()

    def _incident_text(self, incident: FailureIncident) -> str:
        claims = ", ".join(incident.claim_ids) or "none"
        parts = [
            f"Incident ID: {incident.incident_id}",
            f"Run ID: {incident.run_id}",
            f"Scope: {incident.scope.value}",
            f"Kind: {incident.kind.value}",
            f"Phase: {incident.phase}",
            f"Category: {incident.category}",
            f"Message: {incident.message}",
            f"Detail: {incident.detail or 'not available'}",
            f"Provenance: {incident.provenance}",
            f"Claim IDs: {claims}",
            f"Batch index: {incident.batch_index if incident.batch_index is not None else 'not available'}",
            f"Retryable: {'yes' if incident.retryable else 'no'}",
        ]
        if incident.artifacts:
            parts.append("Artifacts / logs:")
            for artifact in incident.artifacts:
                command = shlex.join(artifact.command) if artifact.command else "none"
                parts.extend(
                    [
                        f"  Label: {artifact.label}",
                        f"  Path: {artifact.path}",
                        f"  SHA-256: {artifact.sha256 or 'not available'}",
                        f"  Command: {command}",
                        f"  Exit code: {artifact.exit_code if artifact.exit_code is not None else 'not available'}",
                        f"  Timed out: {'yes' if artifact.timed_out else 'no'}",
                    ]
                )
        else:
            parts.append("Artifacts / logs: none")
        return "\n".join(parts)

    def _selection_detail(self, selection: _FailureSelection) -> str:
        if selection.component_id is not None:
            component = self._components.get(selection.component_id)
            if component is not None:
                return self._component_detail(component)
        if selection.claim_id is not None:
            return self._claim_detail(selection)
        incident_text = [
            self._incident_text(self._incidents[incident_id])
            for incident_id in selection.incident_ids
            if incident_id in self._incidents
        ]
        return "\n\n".join(incident_text) or self._initial_detail()

    def _claim_detail(self, selection: _FailureSelection) -> str:
        assert self.report is not None
        claim_id = selection.claim_id
        graph_node = self._graph_nodes.get(claim_id) if claim_id is not None else None
        incident_ids = (
            graph_node.incident_ids
            if graph_node is not None
            else selection.incident_ids
        )
        status, _ = _failure_status(
            graph_node.state if graph_node is not None else "BLOCKED",
            incident_ids=incident_ids,
        )
        parts = [
            f"Claim: {claim_id or 'not available'}",
            f"Display status: [{status}]",
            f"Verifier state: {graph_node.state if graph_node is not None else 'not available'}",
            f"Kind: {graph_node.kind if graph_node is not None else 'not available'}",
            f"Source file: {graph_node.source_file if graph_node is not None else 'not available'}",
            (
                "Statement lines: "
                f"{graph_node.statement_start}-{graph_node.statement_end}"
                if graph_node is not None
                else "Statement lines: not available"
            ),
            f"Shared dependency reference: {'yes' if selection.shared_reference else 'no'}",
        ]
        relevant_paths = [
            path
            for path in self.report.paths
            if claim_id is not None and claim_id in path.claims
        ]
        if relevant_paths:
            parts.append("Backend-recorded target-to-blocker paths:")
            parts.extend(f"  {' -> '.join(path.claims)}" for path in relevant_paths)
        incidents = [
            self._incidents[incident_id]
            for incident_id in incident_ids
            if incident_id in self._incidents
        ]
        if graph_node is None and incidents:
            parts = [
                f"Run/batch failure node: {claim_id or 'not available'}",
                "Display status: [FAIL]",
                "This synthetic root was supplied by the backend for a failure "
                "that is not owned by one manuscript claim.",
                "Exact reason / incidents:",
            ]
            parts.extend(self._incident_text(incident) for incident in incidents)
            return "\n\n".join(parts)
        if incidents:
            parts.append("Exact reason / incidents:")
            parts.extend(self._incident_text(incident) for incident in incidents)
        else:
            parts.append(
                "Exact reason: no direct incident is attached to this node; use the "
                "backend-recorded blocker path above."
            )
        return "\n\n".join(parts)

    def _component_detail(self, component: FailureComponent) -> str:
        members = "\n".join(f"  {member}" for member in component.members) or "  none"
        parts = [
            f"Component: {component.component_id}",
            f"Cyclic: {'yes' if component.cyclic else 'no'}",
            f"Contains a blocker: {'yes' if component.blocker else 'no'}",
            f"Members ({len(component.members)}):\n{members}",
        ]
        incidents = [
            self._incidents[incident_id]
            for incident_id in component.incident_ids
            if incident_id in self._incidents
        ]
        if incidents:
            parts.append("Exact reason / incidents:")
            parts.extend(self._incident_text(incident) for incident in incidents)
        else:
            parts.append("Exact reason: this component has no direct incident.")
        return "\n\n".join(parts)

    def _full_outline(self) -> str:
        assert self.report is not None
        lines = [self._report_meta(), "", "Targets:"]
        lines.extend(f"  {claim_id}" for claim_id in self.report.targets)
        if not self.report.targets:
            lines.append("  none")
        lines.append("Selected claims:")
        lines.extend(f"  {claim_id}" for claim_id in self.report.selected)
        if not self.report.selected:
            lines.append("  none")

        if self.report.has_cycles:
            lines.extend(["", "Cycle components (backend-computed):"])
            for component in self.report.components:
                status, _ = _failure_status(
                    "BLOCKED_DEPENDENCY",
                    blocker=component.blocker,
                    incident_ids=component.incident_ids,
                )
                cycle = "cyclic" if component.cyclic else "acyclic"
                lines.append(
                    f"  [{status}] {component.component_id} ({cycle}; "
                    f"members: {', '.join(component.members) or 'none'})"
                )
            lines.append("Component edges (dependent -> dependency):")
            lines.extend(
                f"  {edge.dependent_component} -> {edge.dependency_component}"
                for edge in self.report.component_edges
            )
            if not self.report.component_edges:
                lines.append("  none")
        else:
            lines.extend(["", "Proof tree (backend-provided outline):"])

            pending = [(item, 0) for item in reversed(self.report.outline)]
            while pending:
                item, depth = pending.pop()
                status, _ = _failure_status(
                    item.state,
                    blocker=item.blocker,
                    incident_ids=item.incident_ids,
                )
                shared = "; shared reference" if item.shared_reference else ""
                incidents = (
                    ",".join(str(value) for value in item.incident_ids) or "none"
                )
                lines.append(
                    f"  {'  ' * depth}[{status}] {item.claim_id} "
                    f"(state={item.state}; incidents={incidents}{shared})"
                )
                pending.extend((child, depth + 1) for child in reversed(item.children))

        lines.extend(["", "Backend-recorded target-to-blocker paths:"])
        lines.extend(
            f"  {path.target} -> {path.blocker}: {' -> '.join(path.claims)}"
            for path in self.report.paths
        )
        if not self.report.paths:
            lines.append("  none")
        lines.extend(["", "Exact incidents and artifacts:"])
        for incident in self.report.incidents:
            lines.extend([self._incident_text(incident), ""])
        if not self.report.incidents:
            lines.append("  none")
        return "\n".join(lines).rstrip()

    def _show_selection(
        self, selection: _FailureSelection, *, open_detail: bool
    ) -> None:
        detail = self.query_one("#failure-detail", TextArea)
        detail.text = self._selection_detail(selection)
        detail.scroll_home(animate=False)
        if open_detail:
            self.query_one(
                "#failure-tabs", TabbedContent
            ).active = "failure-detail-pane"
            detail.focus()

    def on_mount(self) -> None:
        # Textual 8 mounts the Screen before every composed descendant is
        # guaranteed to be queryable. Initialize focus and selection after the
        # first complete layout so detail widgets always exist.
        self.call_after_refresh(self._initialize_selection)

    def _initialize_selection(self) -> None:
        if not self.is_mounted or self.app.screen is not self:
            return
        if self.report is None:
            self.query_one("#failure-back", Button).focus()
        elif self.report.has_cycles and self._component_table is not None:
            self._component_table.focus()
            if self._cycle_rows:
                self._component_table.move_cursor(row=0, column=0, animate=False)
                first = next(iter(self._cycle_rows.values()))
                self._show_selection(first, open_detail=False)
        elif self._tree is not None:
            self._tree.focus()
            if self._first_tree_node is not None:
                self._tree.move_cursor(self._first_tree_node, animate=False)

    def on_tree_node_highlighted(
        self, event: Tree.NodeHighlighted[_FailureSelection]
    ) -> None:
        if event.control is self._tree and event.node.data is not None:
            self._show_selection(event.node.data, open_detail=False)

    def on_tree_node_selected(
        self, event: Tree.NodeSelected[_FailureSelection]
    ) -> None:
        if event.control is self._tree and event.node.data is not None:
            self._show_selection(event.node.data, open_detail=True)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if (
            event.data_table is self._component_table
            and event.row_key.value is not None
        ):
            selection = self._cycle_rows.get(event.row_key.value)
            if selection is not None:
                self._show_selection(selection, open_detail=False)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if (
            event.data_table is self._component_table
            and event.row_key.value is not None
        ):
            selection = self._cycle_rows.get(event.row_key.value)
            if selection is not None:
                self._show_selection(selection, open_detail=True)

    def action_back(self) -> None:
        if self.snapshot.findings is not None:
            self.proof_app.switch_screen(FindingsScreen(self.snapshot))
        else:
            self.proof_app.switch_screen(RecoveryScreen(self.snapshot))

    def action_close(self) -> None:
        self.proof_app.show_welcome()

    def action_report(self) -> None:
        self.proof_app.view_report(self.snapshot)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "failure-back":
            self.action_back()
        elif event.button.id == "failure-open-report":
            self.action_report()
        elif event.button.id == "failure-close":
            self.action_close()


class ReportViewerScreen(NoticeScreen):
    """Terminal-native rendered and copyable verification-report presentation."""

    BINDINGS = [BACK.binding(), CLOSE.binding()]

    def __init__(
        self,
        snapshot: WorkflowSnapshot,
        *,
        document: ReportDocument | None = None,
        error: str | None = None,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.document = document
        self.error = error

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText("Verification report", classes="title")
                if self.document is not None:
                    yield CopyableText(
                        f"Report path: {self.document.path}", id="report-path"
                    )
            with PageWorkspace():
                if self.document is not None:
                    with TabbedContent(
                        initial="report-rendered-pane", id="report-tabs"
                    ):
                        with TabPane("Rendered", id="report-rendered-pane"):
                            yield MarkdownViewer(
                                self.document.markdown,
                                show_table_of_contents=True,
                                open_links=False,
                                id="report-markdown",
                            )
                        with TabPane("Copyable source", id="report-source-pane"):
                            yield TextArea(
                                self.document.markdown,
                                language="markdown",
                                read_only=True,
                                soft_wrap=True,
                                id="report-source",
                            )
                else:
                    yield CopyableText(
                        f"Project: {self.snapshot.project.project_path}\n"
                        f"Report error: {self.error or 'Report is unavailable.'}",
                        classes="error",
                        id="report-error",
                        max_lines=12,
                    )
            with ActionBar():
                yield Button("Back to findings", id="back", variant="primary")
                yield Button("Close to projects", id="close")
                yield CopyableText(
                    "Tab focuses panes; Ctrl+A/C copies source.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        if self.document is not None:
            self.query_one("#report-markdown", MarkdownViewer).focus()

    def action_back(self) -> None:
        self.proof_app.switch_screen(FindingsScreen(self.snapshot))

    def action_close(self) -> None:
        self.proof_app.show_welcome()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back()
        elif event.button.id == "close":
            self.action_close()


class RecoveryScreen(NoticeScreen):
    """Interrupted, failed, or externally busy project recovery."""

    BINDINGS = [
        RETRY.binding(),
        FAILURES.binding(),
        OPEN.binding(),
        BACK.binding(),
    ]

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
        if (
            detail is None
            and snapshot is not None
            and snapshot.state == WorkflowState.BUSY_EXTERNAL
        ):
            self.detail = (
                "The backend reports project activity, but no attachable observation "
                "is currently available. This TUI owns neither verification nor lock."
            )
        else:
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
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText(self.heading, classes="title")
                yield CopyableText(
                    f"Project: {_path_text(self.project)}", classes="muted"
                )
            with PageWorkspace():
                if self._has_cancellation_report:
                    yield TextArea(
                        self._cancellation_detail(),
                        read_only=True,
                        soft_wrap=True,
                        id="cancellation-report",
                    )
                else:
                    yield CopyableText(self.detail, classes="error")
                if (
                    self.snapshot is not None
                    and self.snapshot.state == WorkflowState.BUSY_EXTERNAL
                ):
                    yield CopyableText(
                        "Backend activity is present. This replaceable client is "
                        "read-only and owns neither verification nor lock; Retry / "
                        "recover attempts to attach again.",
                        classes="warning",
                    )
            with ActionBar():
                yield Button(
                    "Retry / recover", id="retry", disabled=self.project is None
                )
                yield Button(
                    "Load failure analysis",
                    id="recovery-failures",
                    disabled=self.snapshot is None or self.project is None,
                )
                yield Button(
                    "Open project folder",
                    id="open-project",
                    disabled=self.project is None,
                )
                yield Button("Projects", id="projects", variant="primary")
                yield CopyableText(status, id="status-line", classes="muted")
        yield CommandFooter()

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
            self.action_retry()
        elif event.button.id == "recovery-failures" and self.snapshot is not None:
            self.action_failures()
        elif event.button.id == "open-project" and self.project is not None:
            self.action_open()
        elif event.button.id == "projects":
            self.action_back()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"retry", "open"}:
            return self.project is not None
        if action == "failures":
            return self.snapshot is not None and self.project is not None
        return True

    def action_retry(self) -> None:
        if self.project is not None:
            self.proof_app.resume_project(self.project)

    def action_failures(self) -> None:
        if self.snapshot is not None:
            self.proof_app.view_failure_report(self.snapshot)

    def action_open(self) -> None:
        if self.project is not None:
            self.proof_app.open_location(self.project)

    def action_back(self) -> None:
        self.proof_app.show_welcome()
