"""Proof Assistant's Textual application.

No code in this package imports the verifier, its database, or its Git support.
All long-running work crosses ``WorkflowServiceContract`` on a worker thread.
"""

from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Static
from textual.worker import Worker, get_current_worker

from proof_assistant.tui import layout as responsive_layout
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
    AIProviderSettingsScreen,
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
    ProviderSetupSnapshot,
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


class ResizeNeededScreen(ModalScreen[None]):
    """Block editing while the terminal is smaller than the supported floor."""

    def __init__(self, width: int, height: int) -> None:
        super().__init__()
        self._viewport = (width, height)

    def compose(self) -> ComposeResult:
        with Vertical(id="resize-needed-dialog"):
            yield Static("Resize terminal", classes="title")
            yield Static(self._message(), id="resize-needed-message")

    def _message(self) -> str:
        width, height = self._viewport
        return (
            f"Current viewport: {width}x{height}\n"
            "Proof Assistant needs at least 80x24 before editable controls are "
            "available. Resize the terminal to continue. Ctrl+Q exits safely."
        )

    def update_viewport(self, width: int, height: int) -> None:
        self._viewport = (width, height)
        nodes = self.query("#resize-needed-message").nodes
        if nodes and isinstance(nodes[0], Static):
            nodes[0].update(self._message())


class ProofAssistantApp(App[None]):
    """A thin, dependency-injected UI over ``WorkflowServiceContract``."""

    TITLE = "Proof Assistant"
    SUB_TITLE = "Persistent manuscript verification"
    BINDINGS = GLOBAL_BINDINGS
    HORIZONTAL_BREAKPOINTS = responsive_layout.HORIZONTAL_BREAKPOINTS
    VERTICAL_BREAKPOINTS = responsive_layout.VERTICAL_BREAKPOINTS
    CSS = """
    Screen {
        background: $proof-page-background;
        color: $foreground;
    }
    Header, AppHeader {
        background: $proof-chrome-background;
        color: $proof-chrome-foreground;
    }
    Footer {
        background: $footer-background;
        color: $footer-foreground;
    }
    FooterKey:hover { background: $proof-info-background; }
    ResizeNeededScreen {
        align: center middle; background: $proof-overlay;
    }
    #resize-needed-dialog {
        width: 72; max-width: 96%; height: auto;
        border: round $warning; background: $proof-dialog-background; padding: 1 2;
    }
    ResponsivePage, .responsive-page {
        width: 100%; height: 100%;
        layout: vertical; overflow: hidden;
        background: $proof-page-background;
    }
    PageHeader, .page-header {
        width: 100%; height: auto; max-height: 4;
        overflow: hidden;
    }
    PageWorkspace, .page-workspace {
        width: 100%; height: 1fr; min-height: 1;
        overflow-x: hidden; overflow-y: auto;
    }
    ActionBar, .action-bar {
        width: 100%; height: auto; max-height: 4;
        layout: horizontal; overflow: hidden;
        align-vertical: middle;
    }
    ActionBar Button, .action-bar Button,
    ResponsiveToolbar Button, .responsive-toolbar Button {
        width: auto; min-width: 0; margin-right: 1;
    }
    ResponsiveToolbar, .responsive-toolbar {
        width: 100%; height: auto;
        layout: horizontal; overflow: hidden;
    }
    .role-master-detail {
        width: 100%; height: auto; layout: vertical; overflow: hidden;
    }
    .role-detail-back { width: auto; }
    .wide .role-master-detail { layout: horizontal; }
    .wide .role-master-detail RoleRoster { width: 2fr; }
    .wide .role-master-detail SelectedRoleDetail { width: 1fr; }
    .wide .role-detail-back { display: none; }
    ScrollableDialogBody, .dialog-body {
        width: 100%; height: 1fr; min-height: 1;
        overflow-x: hidden; overflow-y: auto;
    }
    .compact ResponsivePage, .compact .responsive-page,
    .compact-short ResponsivePage, .compact-short .responsive-page,
    .resize-needed ResponsivePage, .resize-needed .responsive-page {
        padding: 0 1;
    }
    .standard ResponsivePage, .standard .responsive-page {
        padding: 1 2;
    }
    .wide ResponsivePage, .wide .responsive-page {
        padding: 1 3;
    }
    .compact ResponsiveToolbar, .compact .responsive-toolbar,
    .compact-short ResponsiveToolbar, .compact-short .responsive-toolbar {
        layout: vertical;
    }
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
    #ai-provider-summary {
        height: 26; border: round $proof-focus;
        background: $proof-info-background;
    }
    #ai-auth-next-step { height: 8; border: round $proof-panel-border; }
    #ai-task-policies { height: 18; border: round $proof-panel-border; }
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
    AIInstallConfirmationScreen, AIAccountVerificationConfirmationScreen,
    UnsavedAISettingsConfirmationScreen, ProjectInheritanceConfirmationScreen,
    DestructiveSettingsConfirmationScreen, UnsavedSettingsConfirmationScreen {
        align: center middle; background: $proof-overlay;
    }
    #ai-install-dialog, #ai-account-check-dialog {
        width: 96%; max-width: 104; height: 92%; max-height: 32;
        border: round $warning; background: $proof-dialog-background; padding: 1 2;
    }
    #ai-unsaved-dialog {
        width: 92%; max-width: 76; height: auto;
        border: round $warning; background: $proof-dialog-background; padding: 1 2;
    }
    #project-inheritance-dialog {
        width: 92%; max-width: 76; height: auto;
        border: round $warning; background: $proof-dialog-background; padding: 1 2;
    }
    #settings-destructive-dialog {
        width: 92%; max-width: 82; height: auto;
        border: round $warning; background: $proof-dialog-background; padding: 1 2;
    }
    #settings-unsaved-dialog {
        width: 92%; max-width: 76; height: auto;
        border: round $warning; background: $proof-dialog-background; padding: 1 2;
    }
    #ai-install-commands { height: 1fr; min-height: 8; }
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
        self._settings_overlay_active = False
        self._ai_setup_snapshot: ProviderSetupSnapshot | None = None
        self._ai_setup_supported = callable(getattr(service, "get_ai_setup", None))
        self._active_observation: VerificationJobObservation | None = None
        self._observer_worker: Worker[None] | None = None
        self._progress_screen: ProgressScreen | None = None
        self._pending_settings_snapshot: WorkflowSnapshot | None = None
        self._pending_settings_navigation: Callable[[], None] | None = None

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return THEME_VARIABLE_DEFAULTS

    @property
    def viewport_composition(self) -> responsive_layout.ViewportComposition:
        """Return the composition represented by the managed root class."""

        return responsive_layout.classify_viewport(self.size.width, self.size.height)

    def _apply_viewport_composition(self, width: int, height: int) -> None:
        composition_class = responsive_layout.classify_viewport(width, height).value
        self.remove_class(*responsive_layout.COMPOSITION_CLASSES)
        self.add_class(composition_class)

    def on_mount(self) -> None:
        self._apply_viewport_composition(self.size.width, self.size.height)
        for theme in PROOF_THEMES:
            self.register_theme(theme)
        self.theme = DEFAULT_PROOF_THEME
        self.push_screen(WelcomeScreen(ai_setup_supported=self._ai_setup_supported))
        self.call_after_refresh(
            self._sync_resize_gate, self.size.width, self.size.height
        )
        if self._ai_setup_supported:
            self._probe_ai_setup_on_startup()

    def on_resize(self, event: Resize) -> None:
        """Keep one conjunctive root composition in sync with native breakpoints."""

        self._apply_viewport_composition(event.size.width, event.size.height)
        self.call_after_refresh(
            self._sync_resize_gate, event.size.width, event.size.height
        )

    def _sync_resize_gate(self, width: int, height: int) -> None:
        composition = responsive_layout.classify_viewport(width, height)
        if composition is responsive_layout.ViewportComposition.RESIZE_NEEDED:
            if isinstance(self.screen, ResizeNeededScreen):
                self.screen.update_viewport(width, height)
            else:
                self.push_screen(ResizeNeededScreen(width, height))
            return
        if isinstance(self.screen, ResizeNeededScreen):
            self.screen.dismiss(None)

    def _probe_ai_setup_on_startup(self) -> None:
        """Probe through the workflow service without delaying Textual startup."""

        def probe() -> None:
            try:
                snapshot = self.service.get_ai_setup()
            except Exception as exc:
                self.call_from_thread(self._record_ai_setup_error, str(exc))
                return
            self.call_from_thread(self._route_after_startup_ai_probe, snapshot)

        self.run_worker(probe, thread=True, exclusive=True, group="ai-startup")

    def _record_ai_setup_error(self, detail: str) -> None:
        if isinstance(self.screen, WelcomeScreen):
            self.screen.record_ai_setup_error(detail)

    def _route_after_startup_ai_probe(self, snapshot: ProviderSetupSnapshot) -> None:
        self.record_ai_setup(snapshot)
        if self._defer_navigation_while_settings(
            lambda: self._route_after_startup_ai_probe(snapshot),
            notice="AI provider setup finished loading. Close Settings to continue.",
        ):
            return
        # Revision zero identifies setup that has never been confirmed. A later
        # outage must not hide durable projects or their reports from the user.
        if not snapshot.primary_ready and snapshot.settings.revision == 0:
            self.switch_screen(AIProviderSettingsScreen(snapshot, first_run=True))
            return
        if isinstance(self.screen, WelcomeScreen):
            self.screen.record_ai_setup(snapshot)

    def record_ai_setup(self, snapshot: ProviderSetupSnapshot) -> None:
        """Cache only the sanitized provider DTO used by landing/navigation UI."""

        self._ai_setup_snapshot = snapshot

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

    def _dismiss_modal_before_global_navigation(self) -> bool:
        """Make global navigation cancel-first for confirmation and help dialogs."""

        screen = self.screen
        if not isinstance(screen, ModalScreen):
            return False
        if isinstance(screen, ResizeNeededScreen):
            return True
        screen.dismiss(None)
        return True

    def action_main_menu(self) -> None:
        """Return to the landing screen without changing backend-owned work."""

        if self._dismiss_modal_before_global_navigation():
            return
        if isinstance(self.screen, AIProviderSettingsScreen):
            if self.screen.first_run and not self.screen.first_run_navigation_ready:
                notice = (
                    "Finish primary AI setup and review the complete eight-role "
                    "team before leaving."
                )
                self.screen.clear_transient_secrets()
                self.screen.show_notice(notice, error=True)
                return
            self.screen.request_main_menu()
            return
        if isinstance(self.screen, (ConcurrencyResourcesScreen, LegacySettingsScreen)):
            self.screen.request_main_menu()
            return
        if self._settings_overlay_active:
            self._pending_settings_snapshot = None
            self._pending_settings_navigation = None
            self.pop_screen()
            self._settings_overlay_active = False
        self.show_welcome()

    def finish_main_menu_navigation(self) -> None:
        """Complete a settings-owned, already-guarded return to the landing page."""

        self._pending_settings_snapshot = None
        self._pending_settings_navigation = None
        if self._settings_overlay_active:
            self.pop_screen()
            self._settings_overlay_active = False
        self.show_welcome()

    def action_global_settings(self) -> None:
        """Open machine settings from every ordinary screen."""

        if self._dismiss_modal_before_global_navigation():
            return
        if isinstance(self.screen, SettingsHomeScreen):
            return
        if isinstance(self.screen, AIProviderSettingsScreen):
            if self.screen.first_run:
                self.screen.clear_transient_secrets()
                self.screen.show_notice(
                    "Finish first-run provider setup here; machine Settings opens "
                    "after the complete role team is ready.",
                    error=True,
                )
                return
            self.screen.request_settings_home()
            return
        if isinstance(self.screen, (ConcurrencyResourcesScreen, LegacySettingsScreen)):
            self.screen.request_settings_home()
            return
        screen_snapshot = getattr(self.screen, "snapshot", None)
        machine_snapshot = (
            screen_snapshot
            if isinstance(screen_snapshot, MachineSettingsSnapshot)
            else None
        )
        project = None
        if (
            not isinstance(self.screen, WelcomeScreen)
            and self.current_snapshot is not None
        ):
            project = self.current_snapshot.project.project_path
        self.show_settings(machine_snapshot, project=project)

    def show_welcome(self) -> None:
        if isinstance(self.screen, AIProviderSettingsScreen):
            self.screen.clear_transient_secrets()
        self._detach_active_verification_client()
        self.switch_screen(
            WelcomeScreen(
                self._ai_setup_snapshot,
                ai_setup_supported=self._ai_setup_supported,
            )
        )

    def show_settings(
        self,
        snapshot: MachineSettingsSnapshot | None = None,
        *,
        project: Path | None = None,
        return_to_project: bool | None = None,
    ) -> None:
        """Open machine settings with optional project calibration context."""

        del return_to_project  # Settings now preserves the exact underlying screen.
        self._open_settings_screen(SettingsHomeScreen(snapshot, project=project))

    def _open_settings_screen(
        self,
        screen: (
            SettingsHomeScreen
            | AIProviderSettingsScreen
            | ConcurrencyResourcesScreen
            | LegacySettingsScreen
        ),
    ) -> None:
        """Push settings once, then replace only pages within that overlay."""

        if isinstance(self.screen, AIProviderSettingsScreen):
            self.screen.clear_transient_secrets()
        if self._settings_overlay_active:
            self.switch_screen(screen)
            return
        self.push_screen(screen)
        self._settings_overlay_active = True

    def show_ai_provider_settings(
        self,
        snapshot: ProviderSetupSnapshot | None = None,
        *,
        project: Path | None = None,
    ) -> None:
        """Open provider setup using the latest sanitized backend snapshot."""

        self._open_settings_screen(
            AIProviderSettingsScreen(
                snapshot or self._ai_setup_snapshot,
                project=project,
            )
        )

    def show_concurrency_settings(
        self,
        snapshot: MachineSettingsSnapshot,
        *,
        project: Path | None = None,
    ) -> None:
        self._open_settings_screen(
            ConcurrencyResourcesScreen(snapshot, project=project)
        )

    def show_legacy_settings(
        self,
        snapshot: MachineSettingsSnapshot,
        *,
        project: Path | None = None,
    ) -> None:
        self._open_settings_screen(LegacySettingsScreen(snapshot, project=project))

    def close_settings(self) -> None:
        if not self._settings_overlay_active:
            self.show_welcome()
            return
        self._settings_overlay_active = False
        self.pop_screen()
        pending_navigation = self._pending_settings_navigation
        self._pending_settings_snapshot = None
        self._pending_settings_navigation = None
        if pending_navigation is not None:
            self.call_after_refresh(pending_navigation)

    def _defer_navigation_while_settings(
        self,
        navigation: Callable[[], None],
        *,
        snapshot: WorkflowSnapshot | None = None,
        notice: str = "Background work finished while Settings was open. Close "
        "Settings to view the result.",
    ) -> bool:
        """Preserve the settings overlay until a background result is acknowledged."""

        if not self._settings_overlay_active:
            return False
        self._pending_settings_snapshot = snapshot
        self._pending_settings_navigation = navigation
        active_screen = self.screen
        if hasattr(active_screen, "show_notice"):
            active_screen.show_notice(notice)
        return True

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
        if self._defer_navigation_while_settings(
            lambda: self._confirm_project_deletion(project, inspection),
            notice="Project deletion preflight finished. Close Settings to review "
            "the confirmation.",
        ):
            return
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
        if self._defer_navigation_while_settings(
            lambda: self._show_project_deletion_outcome(
                project, inspection, result, error
            )
        ):
            return
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
        if self._defer_navigation_while_settings(
            lambda: self._show_report_viewer(snapshot, document, error)
        ):
            return
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
        if self._defer_navigation_while_settings(
            lambda: self._show_failure_report(snapshot, report, error)
        ):
            return
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
        if self._defer_navigation_while_settings(
            lambda: self._after_source_inspection(draft, inspection, destination)
        ):
            return
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
        if self._defer_navigation_while_settings(
            lambda: self._show_destination_conflict(draft, inspection)
        ):
            return
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
        if self._defer_navigation_while_settings(
            lambda: self._after_create(snapshot), snapshot=snapshot
        ):
            return
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
        if self._defer_navigation_while_settings(
            lambda: self._after_plan(project, plan)
        ):
            return
        if plan is None or not plan.has_changes:
            if (
                self.current_snapshot is not None
                and self.current_snapshot.clarifications
            ):
                clarification_screen = ClarificationScreen(self.current_snapshot)
                self.switch_screen(clarification_screen)
                self.call_after_refresh(
                    clarification_screen.show_notice,
                    "No stable manuscript changes detected yet.",
                )
            else:
                dashboard_screen = DashboardScreen(self._snapshot_for_project(project))
                self.switch_screen(dashboard_screen)
                self.call_after_refresh(
                    dashboard_screen.show_notice,
                    "No stable manuscript changes detected.",
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

        if settings is None:
            try:
                chosen_settings = self.service.default_verification_settings(
                    project.project_path
                )
            except Exception as exc:
                self.show_error(
                    "Verification AI settings are not usable",
                    str(exc),
                    project.project_path,
                )
                return
        else:
            chosen_settings = settings
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
                attached_observation = getattr(exc, "observation", None)
                if isinstance(attached_observation, VerificationJobObservation):
                    self.call_from_thread(
                        self._activate_observation,
                        progress_screen,
                        attached_observation,
                        "An active job has different requested settings; attached "
                        "to the backend-owned job without replacing it.",
                    )
                    self._poll_verification(
                        project, progress_screen, attached_observation
                    )
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

        self._detach_active_verification_client()
        self.show_welcome()

    def _detach_active_verification_client(self) -> None:
        """Detach only this TUI observer, preserving the durable backend job."""

        screen = self._progress_screen
        worker = self._observer_worker
        # Clear identity first: callbacks already queued on the UI thread are then
        # harmless, including a terminal callback racing with global navigation.
        self._observer_worker = None
        self._active_observation = None
        self._progress_screen = None
        if worker is not None:
            worker.cancel()
        if screen is not None:
            screen.record_client_detached()

    def _attach_to_verification(
        self,
        project: ProjectSummary,
        observation: VerificationJobObservation,
    ) -> None:
        if self._defer_navigation_while_settings(
            lambda: self._attach_to_verification(project, observation)
        ):
            return
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
                    self.call_from_thread(self._finish_observed_job, screen, snapshot)
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

    def _finish_observed_job(
        self, screen: ProgressScreen, snapshot: WorkflowSnapshot
    ) -> None:
        if self._progress_screen is not screen:
            return
        self._active_observation = None
        self._observer_worker = None
        self._progress_screen = None
        self.show_snapshot(snapshot)

    def show_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        self.current_snapshot = snapshot
        if self._defer_navigation_while_settings(
            lambda: self.show_snapshot(snapshot),
            snapshot=snapshot,
            notice="Verification finished while Settings was open. Close Settings "
            "to view the result.",
        ):
            return
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
            self.call_from_thread(self._show_unattachable_activity, snapshot)

        self.run_worker(
            discover,
            thread=True,
            exclusive=True,
            group="verification-discovery",
        )

    def _show_unattachable_activity(self, snapshot: WorkflowSnapshot) -> None:
        if self._defer_navigation_while_settings(
            lambda: self._show_unattachable_activity(snapshot),
            snapshot=snapshot,
            notice="Verification discovery needs attention. Close Settings to view "
            "the recovery details.",
        ):
            return
        self.switch_screen(
            RecoveryScreen(
                snapshot,
                detail=(
                    "The backend reports project activity, but no attachable "
                    "verification observation is currently available. This TUI "
                    "owns neither the verification nor its lock."
                ),
            )
        )

    def show_error(self, title: str, detail: str, project: Path | None) -> None:
        if self._defer_navigation_while_settings(
            lambda: self.show_error(title, detail, project),
            notice="Background work needs attention. Close Settings to view the "
            "details.",
        ):
            return
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
