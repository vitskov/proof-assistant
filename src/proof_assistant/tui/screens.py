"""Screens for the Proof Assistant terminal interface."""

from __future__ import annotations

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
    Static,
    TextArea,
)

from proof_assistant.workflow.contracts import (
    ChangeImpactPlan,
    ClarificationPresentation,
    FileChange,
    NewProjectRequest,
    ProgressEvent,
    ProjectSummary,
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

    async def _render_projects(self, projects: tuple[ProjectSummary, ...]) -> None:
        container = self.query_one("#project-list", Vertical)
        await container.remove_children()
        if not projects:
            await container.mount(
                Static("No projects yet. Choose New project to begin.")
            )
        for index, project in enumerate(projects):
            warning = " · Dropbox source" if project.source_in_dropbox else ""
            detail = Static(
                f"{project.name}\n{project.project_path}\n"
                f"{project.workflow_state.value}{warning}",
                classes="project-summary",
            )
            button = Button("Resume", id=f"resume-{index}")
            button.data = project.project_path
            await container.mount(Horizontal(detail, button, classes="project-row"))
        self.show_notice(f"{len(projects)} project(s) available.")


class NewProjectScreen(NoticeScreen):
    """Collect source, destination, and a project-owned task."""

    BINDINGS = [("escape", "back", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._custom_task = False

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="page"):
            yield Static("New verification project", classes="title")
            yield Label("Project name")
            yield Input(placeholder="my-paper", id="project-name")
            yield Label("Existing manuscript source folder")
            yield Input(placeholder="/absolute/path/to/manuscript", id="source-path")
            yield Static(
                "The source may be in Dropbox. Files are copied into a managed, "
                "Git-versioned "
                "project before verification.",
                classes="muted",
            )
            yield Label("Managed project folder (optional)")
            yield Input(
                placeholder="$HOME/proof-assistant/<project-name>", id="project-path"
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
                self.proof_app.service.default_task_text(),
                id="task-editor",
                language="markdown",
                show_line_numbers=True,
                disabled=True,
            )
            with Horizontal(classes="toolbar"):
                yield Button("Create and verify", id="create", variant="success")
                yield Button("Cancel", id="cancel")
            yield Static("", id="status-line", classes="muted")
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
            editor.disabled = True
            self.show_notice("The maintained default verification task will be used.")
        elif button_id == "custom-task":
            self._custom_task = True
            editor = self.query_one("#task-editor", TextArea)
            editor.disabled = False
            editor.focus()
            self.show_notice("Edit the project-owned task below.")
        elif button_id == "create":
            self._create()

    def _create(self) -> None:
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
        request = NewProjectRequest(
            name=name,
            source_path=Path(source_text).expanduser(),
            project_path=project_path,
            task_text=task_text,
            settings=VerificationSettings(),
        )
        self.proof_app.create_project(request)


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
    ) -> None:
        super().__init__()
        self.heading = title
        self.project = project
        self.cancellable = cancellable
        self.source_in_dropbox = source_in_dropbox
        self._lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="page"):
            yield Static(self.heading, classes="title")
            yield Static(f"Project: {_path_text(self.project)}")
            if self.source_in_dropbox:
                yield Static(
                    "Dropbox source detected. Proof Assistant is verifying a stable "
                    "managed snapshot; finish all related source edits before the next "
                    "change review.",
                    classes="warning",
                    id="dropbox-warning",
                )
            yield ProgressBar(total=100, show_eta=False, id="progress-bar")
            yield Static("Waiting for progress…", id="progress-log")
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Cancel safely",
                    id="cancel",
                    variant="warning",
                    disabled=not self.cancellable,
                )
            yield Static(
                (
                    "Work continues in a background thread; cancellation is checked "
                    "at safe boundaries."
                    if self.cancellable
                    else "Work continues in a background thread."
                ),
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def record_progress(self, event: ProgressEvent) -> None:
        claim = f" [{event.claim_id}]" if event.claim_id else ""
        self._lines.append(
            f"{event.sequence:04d} {event.phase.value}{claim}: {event.message}"
        )
        self._lines = self._lines[-200:]
        self.query_one("#progress-log", Static).update("\n".join(self._lines))
        if event.completed is not None and event.total:
            percent = max(0.0, min(100.0, 100.0 * event.completed / event.total))
            self.query_one("#progress-bar", ProgressBar).update(progress=percent)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" and self.cancellable:
            self.proof_app.cancel_verification()
            event.button.disabled = True
            self.show_notice("Cancellation requested; waiting for a safe boundary.")


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
        return (
            f"Files\n{files}\n\nDirect claim changes\n{direct}\n\n"
            f"Full affected proof-tree closure\n{affected}\n\n"
            f"Certificates expected to remain unaffected\n{unaffected}\n\n"
            f"Clarifications superseded by this change\n{superseded}\n\n"
            f"Task changed: {'yes' if plan.task_changed else 'no'}\n"
            f"Plan: {plan.plan_id}\nInventory: {plan.candidate_inventory_sha256}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.proof_app.start_verification(
                self.snapshot.project, self.plan.plan_id, self.proof_app.settings
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
        with Vertical(id="page"):
            yield Static(self.heading, classes="title")
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
            yield Static(
                "Existing project state is preserved.",
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "retry" and self.project is not None:
            self.proof_app.resume_project(self.project)
        elif event.button.id == "open-project" and self.project is not None:
            self.proof_app.open_location(self.project)
        elif event.button.id == "projects":
            self.proof_app.show_welcome()
