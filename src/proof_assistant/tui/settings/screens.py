"""Settings UI over the machine-scoped workflow service contract.

The widgets in this module never inspect hardware or configuration files.  They
only render immutable backend DTOs and submit revision-checked requests.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    Label,
    OptionList,
    Select,
)
from textual.widgets.option_list import Option

from proof_assistant.tui.commands import (
    BACK,
    CANCEL,
    REFRESH,
    SAVE,
    CommandFooter,
)
from proof_assistant.tui.commands import AppHeader as Header
from proof_assistant.tui.commands import (
    DesktopDataTable as DataTable,
)
from proof_assistant.tui.commands import (
    DesktopInput as Input,
)
from proof_assistant.tui.commands import (
    DesktopTextArea as TextArea,
)
from proof_assistant.tui.layout import (
    ActionBar,
    PageHeader,
    PageWorkspace,
    ResponsivePage,
    ResponsiveToolbar,
)
from proof_assistant.tui.screens import (
    CopyableText,
    NoticeScreen,
)
from proof_assistant.tui.settings.components import (
    DRIVER_LABELS,
    ROLE_LABELS,
    ProviderConnectionRoster,
    ProviderConnectionRow,
    RoleAssignmentRow,
    RoleRoster,
    SelectedRoleDetail,
    SettingsScopeSelector,
    driver_label,
)
from proof_assistant.workflow.contracts import (
    BenchmarkKind,
    BenchmarkResult,
    ConcurrencySettingsView,
    CredentialSource,
    Difficulty,
    DriverId,
    DriverStatus,
    InstallPlan,
    InstallResult,
    MachineSettingsSnapshot,
    MachineSettingsUpdateRequest,
    ProjectAIOverride,
    ProjectAIRoleOverride,
    ProjectVerificationSettingsSnapshot,
    ProviderConfig,
    ProviderSetupSnapshot,
    SecretSubmission,
    SettingsChangePreview,
    SettingsScopeKind,
    TaskKind,
    TaskModelPolicy,
    TaskPreference,
)

if TYPE_CHECKING:
    from proof_assistant.tui.app import ProofAssistantApp


_DRIVER_LABELS = DRIVER_LABELS
_API_DRIVERS = {
    DriverId.OPENAI_API,
    DriverId.ANTHROPIC_API,
    DriverId.GEMINI_API,
}
_AUTO_MODEL = "__proof_assistant_auto_model__"
_ROLE_LABELS = ROLE_LABELS


def _driver_label(driver: DriverId) -> str:
    return driver_label(driver)


def _status_for(snapshot: ProviderSetupSnapshot, driver: DriverId) -> DriverStatus:
    return next(item for item in snapshot.statuses if item.driver is driver)


def _provider_summary(snapshot: ProviderSetupSnapshot) -> str:
    lines = [
        "Machine-wide AI provider setup",
        f"Configuration revision: {snapshot.settings.revision}",
        f"Primary driver: {_driver_label(snapshot.primary_driver)}",
        f"Primary ready: {'yes' if snapshot.primary_ready else 'NO'}",
        f"Status: {snapshot.detail}",
        "",
    ]
    for status in snapshot.statuses:
        preference = snapshot.settings.config.preference_for(status.driver)
        catalog = status.catalog
        primary = " [PRIMARY]" if status.driver is snapshot.primary_driver else ""
        lines.extend(
            (
                f"{_driver_label(status.driver)}{primary}",
                f"  Driver ID: {status.driver.value}",
                f"  Transport: {status.transport.value}",
                f"  Installation: {status.installation.value}",
                f"  Authentication: {status.authentication.value}",
                f"  Credential source: {preference.credential_source.value}",
                f"  Executable: {status.executable or 'not applicable / not found'}",
                f"  Version: {status.version or 'not available'}",
                f"  Catalog: {catalog.source.value if catalog else 'unavailable'}",
                f"  Catalog contract: "
                f"{'approved' if catalog and catalog.contract_approved else 'not ready'}",
            )
        )
        if catalog is not None:
            lines.append(f"  Catalog detail: {catalog.detail}")
            if catalog.models:
                lines.append("  Available models and exact difficulties:")
                for model in catalog.models:
                    difficulties = ", ".join(item.value for item in model.difficulties)
                    lines.append(
                        f"    - {model.display_name} [{model.model_id}]: {difficulties}"
                    )
            else:
                lines.append("  Available models: none reported")
        lines.extend((f"  Next step: {status.detail or 'none'}", ""))
    return "\n".join(lines).rstrip()


def _task_policy_summary(policies: tuple[TaskModelPolicy, ...]) -> str:
    if not policies:
        return "Task-specific defaults are loading…"
    lines = [
        "Role-specific model policy (resolved by the backend)",
        "Primary proof and author clarification are active today; the other roles "
        "are frozen now for RepoProver dispatch as those lanes are enabled.",
    ]
    for policy in policies:
        lines.append(
            f"{_ROLE_LABELS[policy.task]} [{policy.task.value}]: "
            f"{_driver_label(policy.driver)} / "
            f"{policy.model or 'provider default'} / {policy.difficulty.value} "
            f"[{policy.model_source.value}]\n  {policy.explanation}"
        )
    return "\n".join(lines)


def _auto(value: int | None) -> str:
    return "Auto" if value is None else str(value)


def _configured(value: int | None) -> str:
    return "Auto" if value is None else f"{value} [Manual override]"


def _select_value(select: Select[str]) -> str:
    value = select.value
    if not isinstance(value, str):
        raise ValueError(f"Choose a value for {select.id or 'this setting'}.")
    return value


def _optional_positive(text: str, label: str) -> int | None:
    normalized = text.strip().casefold()
    if normalized in {"", "auto"}:
        return None
    try:
        value = int(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} must be Auto or a positive integer.") from exc
    if value < 1:
        raise ValueError(f"{label} must be Auto or a positive integer.")
    return value


def _positive(text: str, label: str) -> int:
    value = _optional_positive(text, label)
    if value is None:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _machine_summary(snapshot: MachineSettingsSnapshot) -> str:
    return (
        "Settings scope: MACHINE (shared by every local project)\n"
        f"Machine ID: {snapshot.machine_id}\n"
        f"Configuration: {snapshot.config_path}\n"
        f"Calibration/cache: {snapshot.cache_path}\n"
        f"Revision: {snapshot.revision}\n"
        f"Updated: {snapshot.updated_at}"
    )


def _concurrency_summary(snapshot: MachineSettingsSnapshot) -> str:
    configured = snapshot.configured
    effective = snapshot.effective
    return (
        "Configured policy and effective values\n"
        f"Mode: {configured.mode}    Resource profile: "
        f"{configured.resource_profile}\n"
        f"Codex plan: {configured.codex_plan}    Budget: "
        f"{configured.budget_policy}\n"
        f"AI concurrency: {_configured(configured.ai_initial)}    "
        f"Effective now: {effective.ai_limit}\n"
        f"AI hard ceiling: {_configured(configured.ai_hard_max)}    "
        f"Effective ceiling: {effective.ai_ceiling}\n"
        f"AI queued successes before growth: "
        f"{_configured(configured.ai_increase_after_successes)}\n"
        f"Lean REPL pool: {_configured(configured.lean_pool)}    "
        f"Effective now: {effective.lean_pool}\n"
        f"Lean pool maximum: {_configured(configured.lean_max)}    "
        f"Effective maximum: {effective.lean_max}\n"
        f"Concurrent builds: {_configured(configured.max_builds)}    "
        f"Effective now: {effective.build_limit}\n"
        f"Build hard ceiling: {configured.build_hard_max}    "
        f"Effective ceiling: {effective.build_ceiling}\n"
        f"Agents per target: max {configured.agents_per_target_max}    "
        f"Current target: {effective.agents_per_target_current}\n"
        f"Duplicate-agent escalation: "
        f"{'enabled' if configured.duplicate_agent_escalation else 'disabled'}\n"
        f"Lean memory calibration: "
        f"{'enabled' if configured.lean_memory_calibration else 'disabled'}\n"
        f"Dependency-priority scheduling: "
        f"{'enabled' if configured.dependency_priority else 'disabled'}\n"
        f"Adaptive controller: {'enabled' if configured.adaptive_controller else 'disabled'}\n"
        f"Hardware telemetry: {'enabled' if configured.hardware_telemetry else 'disabled'}"
    )


def _telemetry_text(snapshot: MachineSettingsSnapshot) -> str:
    telemetry = snapshot.telemetry
    load = (
        ", ".join(f"{value:.2f}" for value in telemetry.load_average)
        if telemetry.load_average is not None
        else "not available"
    )
    io_wait = (
        f"{telemetry.io_wait_percent:.1f}%"
        if telemetry.io_wait_percent is not None
        else "not available"
    )
    repl_rss = (
        f"{telemetry.lean_p95_rss_gib:.2f} GiB"
        if telemetry.lean_p95_rss_gib is not None
        else "not calibrated"
    )
    swap_out = (
        f"{telemetry.swap_out_mib_per_second:.2f} MiB/s"
        if telemetry.swap_out_mib_per_second is not None
        else "not available"
    )
    native_pressure = (
        str(telemetry.native_memory_pressure_level)
        if telemetry.native_memory_pressure_level is not None
        else "not available"
    )
    return (
        f"Hardware: {telemetry.os_name} / {telemetry.architecture} / "
        f"{telemetry.resource_profile}\n"
        f"CPU: {telemetry.physical_cpus} physical / "
        f"{telemetry.logical_cpus} usable logical; "
        f"{telemetry.cpu_percent:.1f}% utilized; load {load}\n"
        f"Memory: {telemetry.total_memory_gib:.2f} GiB total; "
        f"{telemetry.available_memory_gib:.2f} GiB available "
        f"({telemetry.memory_percent_available:.1f}%); pressure "
        f"{telemetry.memory_pressure}\n"
        f"Swap: {telemetry.swap_used_gib:.2f} GiB used; "
        f"active swap-out {swap_out}; I/O wait {io_wait}\n"
        f"Pressure source: {telemetry.memory_pressure_source}; "
        f"native level {native_pressure}\n"
        f"Codex: {telemetry.ai_active} active; {telemetry.ai_queued} queued; "
        f"throttles {telemetry.ai_throttles}; backoff "
        f"{telemetry.ai_backoff_until or 'none'}\n"
        f"Lean: {telemetry.lean_active} active; {telemetry.lean_queued} queued; "
        f"p95 REPL RSS {repl_rss}\n"
        f"Build: {telemetry.build_active} active; "
        f"{telemetry.build_queued} queued\n"
        f"Sampled: {telemetry.sampled_at}"
    )


def _resolution_text(snapshot: MachineSettingsSnapshot) -> str:
    rows = [
        f"{item.field}: configured={item.configured}; "
        f"effective={item.effective}; source={item.source}"
        for item in snapshot.resolution
    ]
    reasons = [f"- {reason}" for reason in snapshot.reasons]
    return (
        "Value sources\n"
        + ("\n".join(rows) if rows else "No resolution details reported.")
        + "\n\nWhy Auto chose these values\n"
        + ("\n".join(reasons) if reasons else "No decision reasons reported.")
    )


def _legacy_text(snapshot: MachineSettingsSnapshot) -> str:
    legacy = snapshot.legacy
    return (
        "Machine-wide legacy compatibility settings\n"
        f"Proof batch workers (jobs): {legacy.proof_jobs}\n"
        f"  Status: {legacy.proof_jobs_status}\n"
        f"Claims per batch: {legacy.batch_size}\n"
        f"  Status: {legacy.batch_size_status}\n"
        f"Lean REPLs per batch worker: {legacy.per_worker_lean_pool}\n"
        f"  Status: {legacy.per_worker_lean_pool_status}\n"
        f"Old process-local AI guard: {legacy.process_local_ai_status}\n"
        f"Old raw build concurrency: {legacy.raw_build_status}\n\n"
        "Legacy controls never form a separate resource controller. The status "
        "beside each value says whether it remains active or is superseded."
    )


class AIInstallConfirmationScreen(ModalScreen[bool]):
    """Cancel-first review of the exact backend-produced installation plan."""

    BINDINGS = [CANCEL.binding()]

    def __init__(self, plan: InstallPlan) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        commands = (
            "\n".join(
                f"{index}. {shlex.join(command.argv)}\n"
                f"   timeout: {command.timeout_seconds:g} seconds"
                for index, command in enumerate(self.plan.commands, start=1)
            )
            or "No command is required."
        )
        with Vertical(id="ai-install-dialog"):
            body = VerticalScroll(id="ai-install-body")
            body.styles.height = "1fr"
            with body:
                yield CopyableText("Review AI driver installation", classes="title")
                yield CopyableText(
                    f"Driver: {_driver_label(self.plan.driver)}\n"
                    f"Plan state: {self.plan.state.value}\n"
                    f"Expected executable: {self.plan.expected_executable or 'none'}\n"
                    f"User install bin: {self.plan.installer_bin or 'none'}\n"
                    f"Detail: {self.plan.detail}",
                    id="ai-install-detail",
                    soft_wrap=False,
                )
                yield CopyableText(
                    "Exact allowlisted commands\n" + commands,
                    id="ai-install-commands",
                    soft_wrap=False,
                    expand=True,
                )
                yield CopyableText(
                    "Installation changes this machine. Cancel is focused by default. "
                    "Continue only after reviewing every command above.",
                    classes="warning",
                )
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Cancel — install nothing",
                    id="ai-install-cancel",
                    variant="primary",
                )
                yield Button(
                    "Install reviewed driver",
                    id="ai-install-confirm",
                    variant="warning",
                    disabled=self.plan.state.value != "available",
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.set_timer(0.01, self._focus_cancel)

    def _focus_cancel(self) -> None:
        nodes = self.query("#ai-install-cancel").nodes
        if not nodes:
            self.set_timer(0.01, self._focus_cancel)
            return
        nodes[0].focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        if self.plan.state.value == "available":
            self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ai-install-cancel":
            self.action_cancel()
        elif event.button.id == "ai-install-confirm":
            self.action_confirm()


class AIAccountVerificationConfirmationScreen(ModalScreen[bool]):
    """Explicit consent for Copilot's necessarily billable account probe."""

    BINDINGS = [CANCEL.binding()]

    def compose(self) -> ComposeResult:
        with Vertical(id="ai-account-check-dialog"):
            body = VerticalScroll(id="ai-account-check-body")
            body.styles.height = "1fr"
            with body:
                yield CopyableText("Verify GitHub Copilot account?", classes="title")
                yield CopyableText(
                    "GitHub Copilot CLI has no documented non-billable authentication "
                    "status command. This check sends one tiny harmless model request "
                    "through the backend. It may count against your Copilot allowance. "
                    "It is never run automatically.",
                    classes="warning",
                )
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Cancel — send nothing",
                    id="ai-account-check-cancel",
                    variant="primary",
                )
                yield Button(
                    "Send one tiny request",
                    id="ai-account-check-confirm",
                    variant="warning",
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.set_timer(0.01, self._focus_cancel)

    def _focus_cancel(self) -> None:
        nodes = self.query("#ai-account-check-cancel").nodes
        if not nodes:
            self.set_timer(0.01, self._focus_cancel)
            return
        nodes[0].focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ai-account-check-cancel":
            self.action_cancel()
        elif event.button.id == "ai-account-check-confirm":
            self.action_confirm()


class UnsavedAISettingsConfirmationScreen(ModalScreen[str | None]):
    """Three-way guard for a dirty role-team draft."""

    BINDINGS = [CANCEL.binding()]

    def __init__(self, scope_label: str) -> None:
        super().__init__()
        self.scope_label = scope_label

    def compose(self) -> ComposeResult:
        with Vertical(id="ai-unsaved-dialog"):
            yield CopyableText("Unsaved role assignments", classes="title")
            yield CopyableText(
                f"The {self.scope_label} role team has unsaved changes. Save it, "
                "discard the draft, or continue editing."
            )
            with ResponsiveToolbar():
                yield Button("Save", id="ai-unsaved-save", variant="success")
                yield Button("Discard", id="ai-unsaved-discard", variant="warning")
                yield Button(
                    "Continue editing", id="ai-unsaved-continue", variant="primary"
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_continue)

    def _focus_continue(self) -> None:
        nodes = self.query("#ai-unsaved-continue").nodes
        if nodes and isinstance(nodes[0], Button):
            nodes[0].focus()
        else:
            self.set_timer(0.01, self._focus_continue)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id is None:
            return
        result = {
            "ai-unsaved-save": "save",
            "ai-unsaved-discard": "discard",
            "ai-unsaved-continue": None,
        }.get(button_id)
        if button_id in {
            "ai-unsaved-save",
            "ai-unsaved-discard",
            "ai-unsaved-continue",
        }:
            self.dismiss(result)


class ProjectInheritanceConfirmationScreen(ModalScreen[bool]):
    """Preview removal of a project role-team override before persistence."""

    BINDINGS = [CANCEL.binding()]

    def compose(self) -> ComposeResult:
        with Vertical(id="project-inheritance-dialog"):
            yield CopyableText(
                "Use machine defaults for this project?", classes="title"
            )
            yield CopyableText(
                "This removes the project-specific provider and eight role assignments. "
                "Future runs will inherit the current machine team."
            )
            with ResponsiveToolbar():
                yield Button("Cancel", id="project-inheritance-cancel")
                yield Button(
                    "Use machine defaults",
                    id="project-inheritance-confirm",
                    variant="warning",
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.query_one("#project-inheritance-cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "project-inheritance-cancel":
            self.action_cancel()
        elif event.button.id == "project-inheritance-confirm":
            self.action_confirm()


class DestructiveSettingsConfirmationScreen(ModalScreen[bool]):
    """Cancel-first review for an exact destructive settings reset."""

    AUTO_FOCUS = "#settings-destructive-cancel"
    BINDINGS = [CANCEL.binding()]

    def __init__(self, title: str, detail: str, confirm_label: str) -> None:
        super().__init__()
        self.heading = title
        self.detail = detail
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-destructive-dialog"):
            yield CopyableText(self.heading, classes="title")
            yield CopyableText(self.detail, id="settings-destructive-detail")
            with ResponsiveToolbar():
                yield Button(
                    "Cancel — keep current data",
                    id="settings-destructive-cancel",
                    variant="primary",
                )
                yield Button(
                    self.confirm_label,
                    id="settings-destructive-confirm",
                    variant="warning",
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_cancel)

    def _focus_cancel(self) -> None:
        nodes = self.query("#settings-destructive-cancel").nodes
        if nodes and isinstance(nodes[0], Button):
            nodes[0].focus()
        else:
            self.set_timer(0.01, self._focus_cancel)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-destructive-cancel":
            self.action_cancel()
        elif event.button.id == "settings-destructive-confirm":
            self.action_confirm()


class UnsavedSettingsConfirmationScreen(ModalScreen[str | None]):
    """Three-way guard for a dirty non-provider settings editor."""

    AUTO_FOCUS = "#settings-unsaved-continue"
    BINDINGS = [CANCEL.binding()]

    def __init__(self, editor_label: str) -> None:
        super().__init__()
        self.editor_label = editor_label

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-unsaved-dialog"):
            yield CopyableText("Unsaved settings", classes="title")
            yield CopyableText(
                f"{self.editor_label} has unsaved changes. Save them, discard the "
                "draft, or continue editing."
            )
            with ResponsiveToolbar():
                yield Button("Save", id="settings-unsaved-save", variant="success")
                yield Button(
                    "Discard", id="settings-unsaved-discard", variant="warning"
                )
                yield Button(
                    "Continue editing",
                    id="settings-unsaved-continue",
                    variant="primary",
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_continue)

    def _focus_continue(self) -> None:
        nodes = self.query("#settings-unsaved-continue").nodes
        if nodes and isinstance(nodes[0], Button):
            nodes[0].focus()
        else:
            self.set_timer(0.01, self._focus_continue)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id is None:
            return
        result = {
            "settings-unsaved-save": "save",
            "settings-unsaved-discard": "discard",
            "settings-unsaved-continue": None,
        }.get(button_id)
        if button_id in {
            "settings-unsaved-save",
            "settings-unsaved-discard",
            "settings-unsaved-continue",
        }:
            self.dismiss(result)


class AIProviderSettingsScreen(NoticeScreen):
    """Provider setup client over sanitized, UI-neutral workflow DTOs."""

    BINDINGS = [BACK.binding(), REFRESH.binding(), SAVE.binding()]

    def __init__(
        self,
        snapshot: ProviderSetupSnapshot | None = None,
        *,
        project: Path | None = None,
        first_run: bool = False,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.project = project
        self.first_run = first_run
        self._policies: tuple[TaskModelPolicy, ...] = ()
        self._machine_role_drafts: dict[TaskKind, tuple[str, Difficulty]] = {}
        self._project_role_drafts: dict[TaskKind, tuple[str, Difficulty]] = {}
        self._rendering_role_controls = False
        self._notice_generation = 0
        self._machine_defaults_generation = 0
        self._machine_draft_generation = 0
        self._setup_load_generation = 0
        self._task_policy_load_generation = 0
        self._project_defaults_generation = 0
        self._project_draft_generation = 0
        self._project_settings_load_generation = 0
        self._saved_machine_role_drafts: dict[TaskKind, tuple[str, Difficulty]] = {}
        self._saved_project_role_drafts: dict[TaskKind, tuple[str, Difficulty]] = {}
        self._saved_project_driver: DriverId | None = None
        self._machine_connection_dirty = False
        self._machine_save_in_flight = False
        self._project_save_in_flight = False
        self._credential_mutation_in_flight = False
        self._project_customizing = False
        self._machine_detail_open = False
        self._project_detail_open = False
        self._first_run_team_reviewed = not first_run
        self._pending_navigation: Callable[[], None] | None = None
        self._active_scope = SettingsScopeKind.MACHINE
        self._machine_undo_drafts: dict[TaskKind, tuple[str, Difficulty]] | None = None
        self._project_undo_drafts: dict[TaskKind, tuple[str, Difficulty]] | None = None
        self._machine_roster_signature: tuple[RoleAssignmentRow, ...] = ()
        self._project_roster_signature: tuple[RoleAssignmentRow, ...] = ()
        self._provider_roster_signature: tuple[ProviderConnectionRow, ...] = ()
        self._provider_controls_ready = False
        self._ignore_machine_highlight: TaskKind | None = None
        self._ignore_project_highlight: TaskKind | None = None
        self._ignore_provider_highlight: DriverId | None = None
        self._last_primary_driver = (
            snapshot.primary_driver if snapshot is not None else DriverId.CODEX_CLI
        )
        self._last_configure_driver = (
            snapshot.primary_driver if snapshot is not None else DriverId.CODEX_CLI
        )
        self._machine_draft_base_revision = (
            snapshot.settings.revision if snapshot is not None else 0
        )
        self.project_settings: ProjectVerificationSettingsSnapshot | None = None

    def show_notice(self, message: str, *, error: bool = False) -> None:
        """Publish a notice and supersede older background notice owners."""

        self._notice_generation += 1
        super().show_notice(message, error=error)

    def _begin_notice(self, message: str, *, error: bool = False) -> int:
        self.show_notice(message, error=error)
        return self._notice_generation

    def _complete_notice(
        self, generation: int, message: str, *, error: bool = False
    ) -> None:
        if generation == self._notice_generation:
            self.show_notice(message, error=error)

    def compose(self) -> ComposeResult:
        snapshot = self.snapshot
        configured_driver = (
            snapshot.primary_driver if snapshot is not None else DriverId.CODEX_CLI
        )
        navigation_options = (
            (
                Option("1  Choose provider", id="choose"),
                Option("2  Connect provider", id="connection"),
                Option("3  Review eight-role team", id="roles"),
            )
            if self.first_run
            else (
                Option("1  Role assignments", id="roles"),
                Option("2  Connections & credentials", id="connection"),
                Option("3  Provider diagnostics", id="diagnostics"),
            )
        )
        yield Header()
        initial_view = "choose-page" if self.first_run else "roles-page"
        with ResponsivePage(id="page", classes="settings-shell"):
            with PageHeader(id="ai-settings-header"):
                yield CopyableText(
                    "Set up verification AI" if self.first_run else "Verification AI",
                    classes="title",
                )
                yield CopyableText(
                    "Choose a provider, connect it, then review the complete eight-role "
                    "team before continuing."
                    if self.first_run
                    else "Assign a provider model and reasoning level to every "
                    "verification role. Credentials remain machine-owned.",
                    classes="muted",
                )
            with PageWorkspace(id="ai-settings-workspace"):
                yield OptionList(*navigation_options, id="ai-settings-nav")
                with ContentSwitcher(initial=initial_view, id="ai-settings-pages"):
                    if self.first_run:
                        with VerticalScroll(id="choose-page", classes="settings-page"):
                            yield CopyableText(
                                "Step 1 of 3 · Choose a provider", classes="section"
                            )
                            yield CopyableText(
                                "Choose the provider that will own the complete "
                                "verification team. The next step checks its local "
                                "connection and authentication.",
                                classes="muted",
                            )
                            yield Label("Provider")
                            yield Select(
                                tuple(
                                    (_driver_label(driver), driver.value)
                                    for driver in DriverId
                                ),
                                value=configured_driver.value,
                                allow_blank=False,
                                id="ai-configure-driver",
                                disabled=snapshot is None,
                            )
                            yield ProviderConnectionRoster(id="ai-provider-roster")
                    with VerticalScroll(id="roles-page", classes="settings-page"):
                        if self.first_run:
                            yield CopyableText(
                                "Step 3 of 3 · Review the eight-role team",
                                classes="section",
                            )
                        yield SettingsScopeSelector(
                            scope=SettingsScopeKind.MACHINE,
                            status=(
                                "Machine defaults apply to every local project."
                                if self.project is None
                                else "Choose machine defaults or this project's future runs."
                            ),
                            id="ai-scope-control",
                            disabled=self.project is None,
                        )
                        with Vertical(id="machine-ai-role-editor"):
                            yield Label("AI provider for machine defaults")
                            yield Select(
                                tuple(
                                    (_driver_label(driver), driver.value)
                                    for driver in DriverId
                                ),
                                value=configured_driver.value,
                                allow_blank=False,
                                id="ai-primary-driver",
                                disabled=snapshot is None,
                            )
                            with ResponsiveToolbar():
                                yield Button(
                                    "Use provider defaults for all 8 roles",
                                    id="ai-use-recommended",
                                    disabled=snapshot is None,
                                )
                                yield Button(
                                    "Undo defaults",
                                    id="ai-undo-recommended",
                                    disabled=True,
                                )
                                yield Button(
                                    "Manage provider connection",
                                    id="ai-manage-connection",
                                    disabled=snapshot is None,
                                )
                            yield CopyableText(
                                "Eight-role verification team", classes="section"
                            )
                            with Horizontal(
                                id="ai-machine-role-workspace",
                                classes="role-master-detail",
                            ):
                                yield RoleRoster(id="ai-role-roster")
                                with SelectedRoleDetail(id="ai-role-detail"):
                                    yield Button(
                                        "Back to role list",
                                        id="ai-role-detail-back",
                                        classes="role-detail-back",
                                    )
                                    yield Label("Selected role")
                                    yield Select(
                                        tuple(
                                            (label, task.value)
                                            for task, label in _ROLE_LABELS.items()
                                        ),
                                        value=TaskKind.PROOF.value,
                                        allow_blank=False,
                                        id="ai-role-task",
                                        disabled=snapshot is None,
                                    )
                                    yield Label("Model")
                                    yield Select(
                                        (("Loading role models…", "__loading__"),),
                                        allow_blank=False,
                                        id="ai-role-model",
                                        disabled=True,
                                    )
                                    yield Label("Reasoning effort")
                                    yield Select(
                                        (
                                            (
                                                "Loading role difficulties…",
                                                "__loading__",
                                            ),
                                        ),
                                        allow_blank=False,
                                        id="ai-role-difficulty",
                                        disabled=True,
                                    )
                        if self.project is not None:
                            with Vertical(id="project-ai-role-editor"):
                                yield Label("AI provider for future project runs")
                                yield Select(
                                    tuple(
                                        (_driver_label(driver), driver.value)
                                        for driver in DriverId
                                    ),
                                    value=configured_driver.value,
                                    allow_blank=False,
                                    id="project-ai-driver",
                                    disabled=True,
                                )
                                with ResponsiveToolbar():
                                    yield Button(
                                        "Customize this project",
                                        id="customize-project-ai",
                                        disabled=True,
                                    )
                                    yield Button(
                                        "Use provider defaults for all 8 roles",
                                        id="project-ai-use-recommended",
                                        disabled=True,
                                    )
                                    yield Button(
                                        "Undo defaults",
                                        id="project-ai-undo-recommended",
                                        disabled=True,
                                    )
                                    yield Button(
                                        "Use machine defaults",
                                        id="reset-project-ai",
                                        disabled=True,
                                    )
                                    yield Button(
                                        "Manage provider connection",
                                        id="project-ai-manage-connection",
                                        disabled=True,
                                    )
                                yield CopyableText(
                                    "Eight-role project team", classes="section"
                                )
                                with Horizontal(
                                    id="project-ai-role-workspace",
                                    classes="role-master-detail",
                                ):
                                    yield RoleRoster(id="project-ai-role-roster")
                                    with SelectedRoleDetail(
                                        id="project-ai-role-detail"
                                    ):
                                        yield Button(
                                            "Back to role list",
                                            id="project-ai-role-detail-back",
                                            classes="role-detail-back",
                                        )
                                        yield Label("Selected project role")
                                        yield Select(
                                            tuple(
                                                (label, task.value)
                                                for task, label in _ROLE_LABELS.items()
                                            ),
                                            value=TaskKind.PROOF.value,
                                            allow_blank=False,
                                            id="project-ai-role",
                                            disabled=True,
                                        )
                                        yield Label("Model")
                                        yield Select(
                                            (
                                                (
                                                    "Loading role models…",
                                                    "__loading__",
                                                ),
                                            ),
                                            allow_blank=False,
                                            id="project-ai-role-model",
                                            disabled=True,
                                        )
                                        yield Label("Reasoning effort")
                                        yield Select(
                                            (
                                                (
                                                    "Loading role difficulties…",
                                                    "__loading__",
                                                ),
                                            ),
                                            allow_blank=False,
                                            id="project-ai-role-difficulty",
                                            disabled=True,
                                        )
                                yield TextArea(
                                    "Loading the project-specific role team…",
                                    read_only=True,
                                    soft_wrap=True,
                                    id="project-ai-summary",
                                )
                    with VerticalScroll(id="connection-page", classes="settings-page"):
                        yield CopyableText(
                            (
                                "Step 2 of 3 · Connect the provider"
                                if self.first_run
                                else "Connections & credentials"
                            ),
                            classes="section",
                        )
                        if not self.first_run:
                            yield Label("Provider to inspect or configure")
                            yield Select(
                                tuple(
                                    (_driver_label(driver), driver.value)
                                    for driver in DriverId
                                ),
                                value=configured_driver.value,
                                allow_blank=False,
                                id="ai-configure-driver",
                                disabled=snapshot is None,
                            )
                            yield ProviderConnectionRoster(id="ai-provider-roster")
                        yield CopyableText(
                            "Advanced provider fallback", classes="section"
                        )
                        yield Label("Fallback model for this provider")
                        yield Select(
                            (("Automatic task-specific choice", _AUTO_MODEL),),
                            value=_AUTO_MODEL,
                            allow_blank=False,
                            id="ai-provider-model",
                            disabled=snapshot is None,
                        )
                        yield Label("Fallback reasoning / difficulty")
                        yield Select(
                            (("Auto", "auto"),),
                            value="auto",
                            allow_blank=False,
                            id="ai-provider-difficulty",
                            disabled=snapshot is None,
                        )
                        yield Label("API credential source")
                        yield Select(
                            (
                                (
                                    "Environment variable",
                                    CredentialSource.ENVIRONMENT.value,
                                ),
                                (
                                    "OS credential store / keyring",
                                    CredentialSource.CREDENTIAL_STORE.value,
                                ),
                            ),
                            value=CredentialSource.ENVIRONMENT.value,
                            allow_blank=False,
                            id="ai-credential-source",
                            disabled=True,
                        )
                        yield TextArea(
                            "Select a provider to see its exact authentication next step.",
                            read_only=True,
                            soft_wrap=False,
                            id="ai-auth-next-step",
                        )
                        yield Vertical(id="ai-api-key-slot")
                        with ResponsiveToolbar():
                            yield Button(
                                "Store key securely",
                                id="store-ai-key",
                                disabled=True,
                            )
                            yield Button(
                                "Remove stored key",
                                id="delete-ai-key",
                                disabled=True,
                                variant="warning",
                            )
                            yield Button(
                                "Review installation…",
                                id="install-ai-driver",
                                disabled=True,
                            )
                            yield Button(
                                "Verify Copilot account…",
                                id="verify-ai-account",
                                disabled=True,
                                variant="warning",
                            )
                    with VerticalScroll(id="diagnostics-page", classes="settings-page"):
                        yield TextArea(
                            _provider_summary(snapshot)
                            if snapshot is not None
                            else "Probing provider setup through the backend…",
                            read_only=True,
                            soft_wrap=False,
                            id="ai-provider-summary",
                        )
                        yield TextArea(
                            _task_policy_summary(self._policies),
                            read_only=True,
                            soft_wrap=False,
                            id="ai-task-policies",
                        )
                        if not self.first_run:
                            yield Button(
                                "Recheck all providers", id="recheck-ai-providers"
                            )
            with ActionBar(id="ai-settings-actions"):
                if self.first_run:
                    yield Button("Back", id="ai-first-run-back")
                    yield Button(
                        "Continue",
                        id="ai-first-run-next",
                        variant="primary",
                        disabled=snapshot is None,
                    )
                    yield Button("Recheck", id="recheck-ai-providers")
                yield Button(
                    "Save machine team" if not self.first_run else "Save team",
                    id="save-ai-settings",
                    variant="success",
                    disabled=snapshot is None,
                )
                if self.project is not None:
                    yield Button(
                        "Save project team",
                        id="save-project-ai",
                        variant="success",
                        disabled=True,
                    )
                yield Button(
                    "Finish setup" if self.first_run else "Main menu",
                    id="ai-setup-continue",
                    variant="primary",
                    disabled=(
                        self.first_run
                        and (snapshot is None or not snapshot.primary_ready)
                    ),
                )
                yield Button(
                    "Exit setup" if self.first_run else "Settings",
                    id="ai-provider-back",
                    disabled=False,
                )
                yield CopyableText(
                    "Loading provider state…" if snapshot is None else snapshot.detail,
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.set_timer(0.01, self._initialize_mounted_screen)

    def _initialize_mounted_screen(self) -> None:
        if not self.query("#ai-settings-workspace").nodes:
            if self.is_mounted:
                self.set_timer(0.01, self._initialize_mounted_screen)
            return
        self._apply_settings_geometry()
        self._sync_role_workspace_visibility()
        self.query_one("#ai-settings-nav", OptionList).highlighted = 0
        self._show_ai_view("choose" if self.first_run else "roles")
        self._render_scope(SettingsScopeKind.MACHINE)
        if self.snapshot is not None:
            self._record_setup(self.snapshot, self._policies)
            self._load_task_policies()
            self._load_project_settings()
        else:
            self.refresh_setup()

    def on_resize(self, event: Resize) -> None:
        del event
        self.call_after_refresh(self._sync_role_workspace_visibility)

    def _apply_settings_geometry(self) -> None:
        """Keep the complete role roster visible in a standard terminal."""

        workspace = self.query_one("#ai-settings-workspace", PageWorkspace)
        workspace.styles.overflow_y = "hidden"
        navigation = self.query_one("#ai-settings-nav", OptionList)
        navigation.styles.height = 5
        pages = self.query_one("#ai-settings-pages", ContentSwitcher)
        pages.styles.height = "1fr"
        scope = self.query_one("#ai-scope-control", SettingsScopeSelector)
        scope.display = self.project is not None
        scope.styles.height = 5
        scope.scope_select.styles.height = 3
        scope.status.styles.height = 2
        self.query_one("#machine-ai-role-editor", Vertical).styles.height = "auto"
        project_editors = self.query("#project-ai-role-editor").nodes
        if project_editors and isinstance(project_editors[0], Vertical):
            project_editors[0].styles.height = "auto"
        for roster in self.query(RoleRoster):
            roster.styles.height = 11
        for detail in self.query(SelectedRoleDetail):
            detail.styles.height = "auto"
            detail.body.styles.height = "auto"
        self.query_one(
            "#ai-provider-roster", ProviderConnectionRoster
        ).styles.height = 9
        self.query_one("#ai-api-key-slot", Vertical).styles.height = 3

    def _enable_provider_controls(self) -> None:
        self._provider_controls_ready = True

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "ai-settings-nav" and event.option.id is not None:
            current = (
                self.query_one("#ai-settings-pages", ContentSwitcher).current
                or "roles-page"
            )
            destination = event.option.id
            if (
                self.first_run
                and destination == "roles"
                and (self.snapshot is None or not self.snapshot.primary_ready)
            ):
                event.option_list.highlighted = 0 if current == "choose-page" else 1
                self.show_notice(
                    "Connect and save the selected provider before reviewing its "
                    "eight-role team.",
                    error=True,
                )
                return
            if current != f"{destination}-page" and self._displayed_scope_is_dirty():
                self._request_navigation(
                    lambda: self._show_ai_view(destination),
                    dirty=True,
                )
            else:
                self._show_ai_view(destination)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "ai-role-roster":
            self._select_roster_task(event.row_key.value, project=False)
            self._open_role_detail(project=False)
        elif event.data_table.id == "project-ai-role-roster":
            self._select_roster_task(event.row_key.value, project=True)
            self._open_role_detail(project=True)
        elif event.data_table.id == "ai-provider-roster":
            self._select_provider_roster_driver(event.row_key.value)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        if key is None or event.data_table.id not in {
            "ai-role-roster",
            "project-ai-role-roster",
            "ai-provider-roster",
        }:
            return
        if event.data_table.id == "ai-provider-roster":
            if self._ignore_provider_highlight is not None:
                if key == self._ignore_provider_highlight.value:
                    self._ignore_provider_highlight = None
                    return
                self._ignore_provider_highlight = None
            self._select_provider_roster_driver(key)
            return
        task = TaskKind(key)
        if event.data_table.id == "ai-role-roster":
            if self._ignore_machine_highlight is not None:
                if task is self._ignore_machine_highlight:
                    self._ignore_machine_highlight = None
                    return
                if event.cursor_row != event.data_table.cursor_row:
                    return
                self._ignore_machine_highlight = None
            self._select_roster_task(key, project=False)
        elif event.data_table.id == "project-ai-role-roster":
            if self._ignore_project_highlight is not None:
                if task is self._ignore_project_highlight:
                    self._ignore_project_highlight = None
                    return
                if event.cursor_row != event.data_table.cursor_row:
                    return
                self._ignore_project_highlight = None
            self._select_roster_task(key, project=True)

    def _show_ai_view(self, view: str) -> None:
        switcher = self.query_one("#ai-settings-pages", ContentSwitcher)
        target = f"{view}-page"
        if switcher.current == target:
            if view == "connection":
                self._mount_secret_input()
            elif view == "roles":
                self._mark_first_run_team_reviewed()
            self._sync_save_action_visibility(view)
            self._sync_first_run_actions(view)
            return
        self._remove_secret_input()
        switcher.current = target
        if view == "connection":
            self._mount_secret_input()
        elif view == "roles":
            self._mark_first_run_team_reviewed()
        self._sync_save_action_visibility(view)
        self._sync_first_run_actions(view)

    def _sync_save_action_visibility(self, view: str) -> None:
        """Show the save action owned by the visible settings editor."""

        if self.first_run:
            return
        roles_page = view == "roles"
        self.query_one("#save-ai-settings", Button).display = view == "connection" or (
            roles_page and self._active_scope is SettingsScopeKind.MACHINE
        )
        project_save = self.query("#save-project-ai").nodes
        if project_save and isinstance(project_save[0], Button):
            project_save[0].display = (
                roles_page and self._active_scope is SettingsScopeKind.PROJECT
            )

    def _sync_first_run_actions(self, view: str) -> None:
        """Keep the wizard's fixed action bar specific to its current step."""

        if not self.first_run:
            return
        back = self.query_one("#ai-first-run-back", Button)
        next_button = self.query_one("#ai-first-run-next", Button)
        save = self.query_one("#save-ai-settings", Button)
        finish = self.query_one("#ai-setup-continue", Button)
        back.disabled = view == "choose"
        back.display = view in {"choose", "connection", "roles"}
        next_button.display = view in {"choose", "connection"}
        next_button.label = (
            "Continue to connection" if view == "choose" else "Save and review team"
        )
        next_button.disabled = self.snapshot is None
        save.display = view == "roles"
        finish.display = view == "roles"

    def _first_run_next(self) -> None:
        switcher = self.query_one("#ai-settings-pages", ContentSwitcher)
        if switcher.current == "choose-page":
            self.query_one("#ai-settings-nav", OptionList).highlighted = 1
            self._show_ai_view("connection")
            return
        if switcher.current != "connection-page" or self.snapshot is None:
            return
        driver = self._selected_driver()
        if not _status_for(self.snapshot, driver).ready:
            self.show_notice(
                f"{_driver_label(driver)} is not connected yet. Complete the "
                "authentication step or installation, then Recheck.",
                error=True,
            )
            return
        self._save_settings()

    def _first_run_back(self) -> None:
        switcher = self.query_one("#ai-settings-pages", ContentSwitcher)
        if switcher.current == "roles-page":
            self.query_one("#ai-settings-nav", OptionList).highlighted = 1
            self._show_ai_view("connection")
        elif switcher.current == "connection-page":
            self.query_one("#ai-settings-nav", OptionList).highlighted = 0
            self._show_ai_view("choose")

    def _mark_first_run_team_reviewed(self) -> None:
        if not self.first_run:
            return
        self._first_run_team_reviewed = True
        if self.snapshot is not None and self.snapshot.primary_ready:
            self.query_one("#ai-setup-continue", Button).disabled = False

    def _mount_secret_input(
        self, preserved: tuple[str, str, str] | None = None
    ) -> None:
        if self.query("#ai-api-key").nodes:
            return
        slot = self.query_one("#ai-api-key-slot", Vertical)
        slot.mount(
            Input(
                placeholder="Paste key; cleared immediately after Store",
                password=True,
                id="ai-api-key",
                disabled=True,
            )
        )
        self.call_after_refresh(self._finish_secret_input_mount, preserved)

    def _finish_secret_input_mount(
        self, preserved: tuple[str, str, str] | None
    ) -> None:
        """Render the new secret field without replacing a newer control edit."""

        live_draft = (
            preserved if preserved is not None else self._connection_control_draft()
        )
        self._render_selected_provider(live_draft)

    def _connection_control_draft(self) -> tuple[str, str, str]:
        model = self.query_one("#ai-provider-model", Select).value
        difficulty = self.query_one("#ai-provider-difficulty", Select).value
        source = self.query_one("#ai-credential-source", Select).value
        return (
            _AUTO_MODEL if model is Select.NULL else str(model),
            Difficulty.AUTO.value if difficulty is Select.NULL else str(difficulty),
            CredentialSource.ENVIRONMENT.value
            if source is Select.NULL
            else str(source),
        )

    def _remove_secret_input(self) -> None:
        for key_input in self.query("#ai-api-key").nodes:
            if isinstance(key_input, Input):
                key_input.value = ""
                key_input.disabled = True
                key_input.remove()

    def clear_transient_secrets(self) -> None:
        """Clear one-shot credentials before any settings navigation boundary."""

        self._remove_secret_input()

    @property
    def first_run_navigation_ready(self) -> bool:
        """Return whether global navigation may complete the first-run gate."""

        return bool(
            self.first_run
            and self.snapshot is not None
            and self.snapshot.primary_ready
            and self._first_run_team_reviewed
        )

    def on_unmount(self) -> None:
        self.clear_transient_secrets()

    def _machine_draft_is_dirty(self) -> bool:
        snapshot = self.snapshot
        if snapshot is None or not self.query("#ai-primary-driver").nodes:
            return False
        selected = self.query_one("#ai-primary-driver", Select).value
        primary_dirty = (
            selected is not Select.NULL
            and str(selected) != snapshot.primary_driver.value
        )
        roles_dirty = bool(self._saved_machine_role_drafts) and (
            self._machine_role_drafts != self._saved_machine_role_drafts
        )
        return primary_dirty or roles_dirty or self._machine_connection_dirty

    def _project_draft_is_dirty(self) -> bool:
        if (
            self.project_settings is None
            or self._saved_project_driver is None
            or not self.query("#project-ai-driver").nodes
        ):
            return False
        selected = self.query_one("#project-ai-driver", Select).value
        return (
            selected is not Select.NULL
            and str(selected) != self._saved_project_driver.value
        ) or (
            bool(self._saved_project_role_drafts)
            and self._project_role_drafts != self._saved_project_role_drafts
        )

    def _displayed_scope_is_dirty(self) -> bool:
        if (
            self.query("#ai-settings-pages").nodes
            and self.query_one("#ai-settings-pages", ContentSwitcher).current
            == "connection-page"
        ):
            return self._machine_draft_is_dirty()
        return (
            self._project_draft_is_dirty()
            if self._active_scope is SettingsScopeKind.PROJECT
            else self._machine_draft_is_dirty()
        )

    def _displayed_settings_scope(self) -> SettingsScopeKind:
        """Return the persistence domain owned by the currently visible editor."""

        if (
            self.query("#ai-settings-pages").nodes
            and self.query_one("#ai-settings-pages", ContentSwitcher).current
            == "connection-page"
        ):
            return SettingsScopeKind.MACHINE
        return self._active_scope

    def _restore_scope_draft(self, scope: SettingsScopeKind) -> None:
        if scope is SettingsScopeKind.PROJECT:
            if self._saved_project_role_drafts:
                self._project_role_drafts = dict(self._saved_project_role_drafts)
            if self._saved_project_driver is not None:
                with self.prevent(Select.Changed):
                    self.query_one(
                        "#project-ai-driver", Select
                    ).value = self._saved_project_driver.value
            self._project_undo_drafts = None
            self._project_draft_generation += 1
            if self.project_settings is not None:
                self._project_customizing = not self.project_settings.inherited
                self.query_one(
                    "#reset-project-ai", Button
                ).disabled = self.project_settings.inherited
                self.query_one(
                    "#customize-project-ai", Button
                ).disabled = not self.project_settings.inherited
            self.query_one("#project-ai-undo-recommended", Button).disabled = True
            self._render_project_ai_choices(reset_driver=True)
            return
        if self._saved_machine_role_drafts:
            self._machine_role_drafts = dict(self._saved_machine_role_drafts)
        snapshot = self.snapshot
        if snapshot is not None:
            with self.prevent(Select.Changed):
                self.query_one(
                    "#ai-primary-driver", Select
                ).value = snapshot.primary_driver.value
                self.query_one(
                    "#ai-configure-driver", Select
                ).value = snapshot.primary_driver.value
            self._last_primary_driver = snapshot.primary_driver
        self._machine_connection_dirty = False
        self._machine_undo_drafts = None
        self.query_one("#ai-undo-recommended", Button).disabled = True
        self._render_selected_provider()
        self._render_machine_role_choices()
        self._refresh_machine_role_actions()

    def _request_navigation(
        self, destination: Callable[[], None], *, dirty: bool | None = None
    ) -> None:
        """Guard a navigation boundary without retaining credential input."""

        self.clear_transient_secrets()
        scope = self._displayed_settings_scope()
        if not (self._displayed_scope_is_dirty() if dirty is None else dirty):
            destination()
            return
        dialog = UnsavedAISettingsConfirmationScreen(
            "project" if scope is SettingsScopeKind.PROJECT else "machine"
        )

        def after_choice(choice: str | None) -> None:
            if choice == "discard":
                self._restore_scope_draft(scope)
                destination()
            elif choice == "save":
                self._pending_navigation = destination
                started = (
                    self._save_project_settings()
                    if scope is SettingsScopeKind.PROJECT
                    else self._save_settings()
                )
                if not started:
                    self._pending_navigation = None
            elif (
                self.query_one("#ai-settings-pages", ContentSwitcher).current
                == "connection-page"
            ):
                # The old secret was destroyed before opening the dialog. Return
                # to a fresh empty one-shot field when editing continues.
                preserved = self._connection_control_draft()
                self.call_after_refresh(
                    self._mount_secret_input,
                    preserved,
                )

        self.proof_app.push_screen(dialog, callback=after_choice)

    def _finish_pending_navigation(self) -> None:
        destination = self._pending_navigation
        self._pending_navigation = None
        if destination is not None:
            destination()

    def request_main_menu(self) -> None:
        if self._credential_mutation_in_flight:
            self.show_notice(
                "Wait for the credential change to finish before leaving Settings.",
                error=True,
            )
            return
        self._request_navigation(self.proof_app.finish_main_menu_navigation)

    def request_quit(self) -> None:
        if self._credential_mutation_in_flight:
            self.show_notice(
                "Wait for the credential change to finish before quitting.",
                error=True,
            )
            return
        self._request_navigation(self.proof_app.exit)

    def request_settings_home(self) -> None:
        if self._credential_mutation_in_flight:
            self.show_notice(
                "Wait for the credential change to finish before changing pages.",
                error=True,
            )
            return
        self._request_navigation(
            lambda: self.proof_app.show_settings(project=self.project)
        )

    def _select_roster_task(self, key: str | None, *, project: bool) -> None:
        if key is None:
            return
        try:
            task = TaskKind(key)
        except ValueError:
            return
        selector_id = "#project-ai-role" if project else "#ai-role-task"
        selector = self.query_one(selector_id, Select)
        if selector.value != task.value:
            selector.value = task.value

    def _select_provider_roster_driver(self, key: str | None) -> None:
        if key is None:
            return
        try:
            driver = DriverId(key)
        except ValueError:
            return
        selector = self.query_one("#ai-configure-driver", Select)
        if selector.value != driver.value:
            selector.value = driver.value

    def _open_role_detail(self, *, project: bool) -> None:
        if project:
            self._project_detail_open = True
        else:
            self._machine_detail_open = True
        self._sync_role_workspace_visibility()
        detail_id = "#project-ai-role-detail" if project else "#ai-role-detail"
        self.query_one(detail_id, SelectedRoleDetail).focus()

    def _close_role_detail(self, *, project: bool) -> None:
        if project:
            self._project_detail_open = False
        else:
            self._machine_detail_open = False
        self._sync_role_workspace_visibility()
        roster_id = "#project-ai-role-roster" if project else "#ai-role-roster"
        self.query_one(roster_id, RoleRoster).focus()

    def _sync_role_workspace_visibility(self) -> None:
        if not self.query("#ai-role-roster").nodes:
            return
        wide = self.proof_app.has_class("wide")
        machine_roster = self.query_one("#ai-role-roster", RoleRoster)
        machine_detail = self.query_one("#ai-role-detail", SelectedRoleDetail)
        machine_roster.display = wide or not self._machine_detail_open
        machine_detail.display = wide or self._machine_detail_open
        if self.query("#project-ai-role-roster").nodes:
            project_roster = self.query_one("#project-ai-role-roster", RoleRoster)
            project_detail = self.query_one(
                "#project-ai-role-detail", SelectedRoleDetail
            )
            project_roster.display = wide or not self._project_detail_open
            project_detail.display = wide or self._project_detail_open

    def _render_scope(self, scope: SettingsScopeKind) -> None:
        self._active_scope = scope
        machine = self.query_one("#machine-ai-role-editor", Vertical)
        machine.display = scope is SettingsScopeKind.MACHINE
        if self.project is None:
            current = (
                self.query_one("#ai-settings-pages", ContentSwitcher).current
                or "roles-page"
            )
            self._sync_save_action_visibility(current.removesuffix("-page"))
            return
        project = self.query_one("#project-ai-role-editor", Vertical)
        project.display = scope is SettingsScopeKind.PROJECT
        selector = self.query_one("#ai-scope-control", SettingsScopeSelector)
        inherited = self.project_settings is None or self.project_settings.inherited
        selector.update_scope(
            scope,
            status=(
                "Machine defaults apply to every local project."
                if scope is SettingsScopeKind.MACHINE
                else (
                    "Project scope · inherited from machine defaults."
                    if inherited
                    else "Project scope · custom complete role team."
                )
            ),
        )
        current = (
            self.query_one("#ai-settings-pages", ContentSwitcher).current
            or "roles-page"
        )
        self._sync_save_action_visibility(current.removesuffix("-page"))

    def _activate_scope(self, scope: SettingsScopeKind) -> None:
        selector = self.query_one("#settings-scope", Select)
        with self.prevent(Select.Changed):
            selector.value = scope.value
        self._render_scope(scope)

    def action_back(self) -> None:
        if self.first_run:
            if self.snapshot is None or not self.snapshot.primary_ready:
                self.clear_transient_secrets()
                self.show_notice(
                    "Choose a ready primary AI driver before continuing to new projects.",
                    error=True,
                )
                return
            self.request_main_menu()
            return
        self.request_settings_home()

    def action_refresh(self) -> None:
        self._request_navigation(self.refresh_setup)

    def action_save(self) -> None:
        if self._displayed_settings_scope() is SettingsScopeKind.PROJECT:
            self._save_project_settings()
        else:
            self._save_settings()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.value in {"__loading__", "__none__", "__needs_update__"}:
            return
        if self._rendering_role_controls and event.select.id in {
            "ai-role-task",
            "ai-role-model",
            "ai-role-difficulty",
            "project-ai-driver",
            "project-ai-role",
            "project-ai-role-model",
            "project-ai-role-difficulty",
        }:
            return
        if not self._provider_controls_ready and event.select.id in {
            "ai-primary-driver",
            "ai-configure-driver",
            "ai-provider-model",
            "ai-provider-difficulty",
        }:
            return
        if event.select.id == "settings-scope":
            value = event.select.value
            if value is not Select.NULL:
                scope = (
                    value
                    if isinstance(value, SettingsScopeKind)
                    else SettingsScopeKind(str(value))
                )
                if scope is self._active_scope:
                    return
                previous_scope = self._active_scope
                if self._displayed_scope_is_dirty():
                    with self.prevent(Select.Changed):
                        self.query_one(
                            "#ai-scope-control", SettingsScopeSelector
                        ).scope_select.value = previous_scope
                    self._request_navigation(
                        lambda: self._activate_scope(scope), dirty=True
                    )
                else:
                    self._render_scope(scope)
        elif event.select.id == "ai-primary-driver" and self.snapshot is not None:
            selected = event.select.value
            if selected is not Select.NULL:
                driver = DriverId(str(selected))
                if driver is self._last_primary_driver:
                    return
                self._last_primary_driver = driver
                self._machine_draft_generation += 1
                self._machine_role_drafts = {}
                self._machine_undo_drafts = None
                self.query_one("#ai-undo-recommended", Button).disabled = True
                with self.prevent(Select.Changed):
                    self.query_one("#ai-configure-driver", Select).value = str(selected)
                self._last_configure_driver = driver
                self._render_machine_policy_transition(driver)
                self._render_selected_provider()
                self._render_machine_role_choices()
                self._refresh_machine_role_actions()
                self._load_recommended_role_policies(driver, project=False)
        elif event.select.id == "ai-configure-driver" and self.snapshot is not None:
            selected = event.select.value
            if selected is Select.NULL:
                return
            driver = DriverId(str(selected))
            if driver is self._last_configure_driver:
                return
            self._last_configure_driver = driver
            self._machine_draft_generation += 1
            self._show_ai_view("connection")
            self._render_selected_provider()
            if self.first_run:
                self._machine_role_drafts = {}
                self._machine_undo_drafts = None
                self._last_primary_driver = driver
                with self.prevent(Select.Changed):
                    self.query_one("#ai-primary-driver", Select).value = str(selected)
                self.query_one("#ai-undo-recommended", Button).disabled = True
                self.query_one("#save-ai-settings", Button).disabled = True
                self._render_machine_role_choices()
                self._load_recommended_role_policies(driver, project=False)
        elif event.select.id == "ai-provider-model" and self.snapshot is not None:
            self._machine_draft_generation += 1
            self._render_difficulties()
            self._machine_connection_dirty = (
                self._machine_connection_dirty
                or self._connection_controls_differ_from_saved()
            )
        elif (
            event.select.id in {"ai-provider-difficulty", "ai-credential-source"}
            and self.snapshot is not None
        ):
            self._machine_draft_generation += 1
            self._machine_connection_dirty = (
                self._machine_connection_dirty
                or self._connection_controls_differ_from_saved()
            )
        elif event.select.id == "ai-role-task" and self.snapshot is not None:
            self._render_machine_role_choices()
        elif event.select.id == "ai-role-model" and self.snapshot is not None:
            self._record_machine_role_model()
        elif event.select.id == "ai-role-difficulty" and self.snapshot is not None:
            self._record_machine_role_difficulty()
        elif event.select.id == "project-ai-driver" and self.snapshot is not None:
            if not self._project_customizing or self.project_settings is None:
                return
            selected = event.select.value
            if selected is not Select.NULL:
                driver = DriverId(str(selected))
                self._project_draft_generation += 1
                self._project_role_drafts = {}
                self._project_undo_drafts = None
                self.query_one("#project-ai-undo-recommended", Button).disabled = True
                self._render_project_draft_summary(driver, loading=True)
                self._render_project_ai_choices()
                self._refresh_project_role_actions()
                self._load_recommended_role_policies(driver, project=True)
        elif event.select.id == "project-ai-role" and self.snapshot is not None:
            self._render_project_ai_choices()
        elif event.select.id == "project-ai-role-model" and self.snapshot is not None:
            self._record_project_role_model()
        elif (
            event.select.id == "project-ai-role-difficulty"
            and self.snapshot is not None
        ):
            self._record_project_role_difficulty()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "save-ai-settings":
            self._save_settings()
        elif button_id == "ai-use-recommended":
            self._load_recommended_role_policies(
                DriverId(_select_value(self.query_one("#ai-primary-driver", Select))),
                project=False,
            )
        elif button_id == "ai-undo-recommended":
            self._undo_recommended_role_policies(project=False)
        elif button_id == "ai-manage-connection":
            self._manage_selected_provider_connection(project=False)
        elif button_id == "ai-role-detail-back":
            self._close_role_detail(project=False)
        elif button_id == "recheck-ai-providers":
            self.action_refresh()
        elif button_id == "install-ai-driver":
            self._preview_install()
        elif button_id == "verify-ai-account":
            self._review_account_verification()
        elif button_id == "store-ai-key":
            self._store_credential()
        elif button_id == "delete-ai-key":
            self._review_delete_credential()
        elif button_id == "save-project-ai":
            self._save_project_settings()
        elif button_id == "customize-project-ai":
            self._customize_project_settings()
        elif button_id == "project-ai-use-recommended":
            self._load_recommended_role_policies(
                DriverId(_select_value(self.query_one("#project-ai-driver", Select))),
                project=True,
            )
        elif button_id == "project-ai-undo-recommended":
            self._undo_recommended_role_policies(project=True)
        elif button_id == "project-ai-manage-connection":
            self._manage_selected_provider_connection(project=True)
        elif button_id == "project-ai-role-detail-back":
            self._close_role_detail(project=True)
        elif button_id == "reset-project-ai":
            self._review_reset_project_settings()
        elif button_id == "ai-setup-continue":
            if self.snapshot is not None and self.snapshot.primary_ready:
                self._request_navigation(self.request_main_menu)
            else:
                self.show_notice("The primary AI driver is not ready yet.", error=True)
        elif button_id == "ai-first-run-next":
            self._first_run_next()
        elif button_id == "ai-first-run-back":
            self._first_run_back()
        elif button_id == "ai-provider-back":
            if self.first_run:
                self.clear_transient_secrets()
                self.proof_app.exit()
            else:
                self.action_back()

    def _selected_driver(self) -> DriverId:
        value = _select_value(self.query_one("#ai-configure-driver", Select))
        return DriverId(value)

    def _role_assignment_is_valid(
        self,
        driver: DriverId,
        assignment: tuple[str, Difficulty] | None,
    ) -> bool:
        """Return whether an assignment belongs to the selected provider catalog."""

        snapshot = self.snapshot
        if snapshot is None or assignment is None:
            return False
        catalog = _status_for(snapshot, driver).catalog
        if catalog is None:
            return False
        model, difficulty = assignment
        descriptor = next(
            (item for item in catalog.models if item.model_id == model), None
        )
        return descriptor is not None and difficulty in descriptor.difficulties

    def _role_team_is_valid(
        self,
        driver: DriverId,
        drafts: dict[TaskKind, tuple[str, Difficulty]],
    ) -> bool:
        return set(drafts) == set(TaskKind) and all(
            self._role_assignment_is_valid(driver, drafts.get(task))
            for task in TaskKind
        )

    def _update_provider_action_labels(
        self, driver: DriverId, *, project: bool
    ) -> None:
        prefix = "#project-ai" if project else "#ai"
        label = _driver_label(driver)
        self.query_one(
            f"{prefix}-use-recommended", Button
        ).label = f"Use recommended {label} defaults for all 8 roles"
        self.query_one(
            f"{prefix}-manage-connection", Button
        ).label = f"Manage {label} connection"

    def _render_machine_policy_transition(self, driver: DriverId) -> None:
        """Remove the previous provider from the supplemental policy display."""

        self.query_one("#ai-task-policies", TextArea).text = (
            f"Selected provider: {_driver_label(driver)} [{driver.value}]\n"
            "Unsaved role assignments: loading recommended defaults for all 8 roles."
        )

    def _render_project_draft_summary(
        self, driver: DriverId, *, loading: bool = False
    ) -> None:
        """Render only target-provider data for the unsaved project draft."""

        project_settings = self.project_settings
        if project_settings is None:
            return
        if loading:
            role_lines = "\n".join(
                f"  {_ROLE_LABELS[task]}: Awaiting assignment" for task in TaskKind
            )
            status = "Loading recommended defaults; Save is unavailable"
        else:
            role_lines = "\n".join(
                (
                    f"  {_ROLE_LABELS[task]}: {assignment[0]} / "
                    f"{assignment[1].value}"
                    if (
                        (assignment := self._project_role_drafts.get(task)) is not None
                        and self._role_assignment_is_valid(driver, assignment)
                    )
                    else f"  {_ROLE_LABELS[task]}: Awaiting assignment"
                )
                for task in TaskKind
            )
            status = (
                "Ready to save"
                if self._role_team_is_valid(driver, self._project_role_drafts)
                else "Incomplete; Save is unavailable"
            )
        self.query_one("#project-ai-summary", TextArea).text = (
            f"Project: {project_settings.project_path}\n"
            f"Saved revision: {project_settings.revision}\n"
            "Scope: unsaved project-specific provider and role team\n"
            f"Status: {status}\n"
            f"Selected provider: {driver.value}\n"
            f"Unsaved role assignments for the next run:\n{role_lines}"
        )

    def _refresh_machine_role_actions(self) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            return
        driver = DriverId(_select_value(self.query_one("#ai-primary-driver", Select)))
        status = _status_for(snapshot, driver)
        self._update_provider_action_labels(driver, project=False)
        self.query_one("#ai-use-recommended", Button).disabled = not (
            status.catalog is not None and bool(status.catalog.models)
        )
        self.query_one("#ai-manage-connection", Button).disabled = False
        self.query_one("#save-ai-settings", Button).disabled = not (
            status.ready and self._role_team_is_valid(driver, self._machine_role_drafts)
        )

    def _refresh_project_role_actions(self) -> None:
        snapshot = self.snapshot
        if snapshot is None or self.project_settings is None:
            return
        driver = DriverId(_select_value(self.query_one("#project-ai-driver", Select)))
        status = _status_for(snapshot, driver)
        self._update_provider_action_labels(driver, project=True)
        can_edit = self._project_customizing
        has_catalog = status.catalog is not None and bool(status.catalog.models)
        self.query_one("#project-ai-use-recommended", Button).disabled = not (
            can_edit and has_catalog
        )
        self.query_one("#project-ai-manage-connection", Button).disabled = False
        self.query_one("#save-project-ai", Button).disabled = not (
            can_edit
            and status.ready
            and self._role_team_is_valid(driver, self._project_role_drafts)
        )

    def _manage_selected_provider_connection(self, *, project: bool) -> None:
        selector = "#project-ai-driver" if project else "#ai-primary-driver"
        driver = DriverId(_select_value(self.query_one(selector, Select)))
        with self.prevent(Select.Changed):
            self.query_one("#ai-configure-driver", Select).value = driver.value
        self._last_configure_driver = driver
        self._render_selected_provider()
        if not self.first_run:
            self.query_one("#ai-settings-nav", OptionList).highlighted = 1
        self._show_ai_view("connection")

    def _connection_controls_differ_from_saved(self) -> bool:
        snapshot = self.snapshot
        if snapshot is None or not self.query("#ai-provider-model").nodes:
            return False
        driver = self._selected_driver()
        preference = snapshot.settings.config.preference_for(driver)
        model = self.query_one("#ai-provider-model", Select).value
        difficulty = self.query_one("#ai-provider-difficulty", Select).value
        source = self.query_one("#ai-credential-source", Select).value
        model_value = None if model == _AUTO_MODEL else str(model)
        source_dirty = (
            driver in _API_DRIVERS
            and source is not Select.NULL
            and str(source) != preference.credential_source.value
        )
        return (
            (model is not Select.NULL and model_value != preference.model)
            or (
                difficulty is not Select.NULL
                and str(difficulty) != preference.difficulty.value
            )
            or source_dirty
        )

    def refresh_setup(self) -> None:
        self._setup_load_generation += 1
        request_generation = self._setup_load_generation
        self.show_notice("Rechecking installation, authentication, and model access…")

        def refresh() -> None:
            try:
                snapshot = self.proof_app.service.get_ai_setup()
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._record_setup_load_error,
                    str(exc),
                    request_generation,
                )
                return
            self.proof_app.call_from_thread(
                self._record_setup_and_reload,
                snapshot,
                request_generation=request_generation,
            )

        self.run_worker(refresh, thread=True, exclusive=True, group="ai-provider-setup")

    def _record_setup_load_error(self, detail: str, request_generation: int) -> None:
        if request_generation != self._setup_load_generation:
            return
        self.show_notice(f"Provider setup check failed: {detail}", error=True)

    def _record_setup_and_reload(
        self,
        snapshot: ProviderSetupSnapshot,
        *,
        request_generation: int | None = None,
    ) -> None:
        """Render authoritative setup first; task-policy display is supplemental."""

        if (
            request_generation is not None
            and request_generation != self._setup_load_generation
        ):
            return
        if request_generation is None:
            # A save, account check, or credential mutation supersedes older reads.
            self._setup_load_generation += 1

        preserve_machine_draft = self._machine_draft_is_dirty()
        preserve_project_draft = self._project_draft_is_dirty()
        machine_roles = dict(self._machine_role_drafts)
        machine_undo = (
            None
            if self._machine_undo_drafts is None
            else dict(self._machine_undo_drafts)
        )
        connection_dirty = self._machine_connection_dirty
        primary = self.query_one("#ai-primary-driver", Select).value
        configure = self.query_one("#ai-configure-driver", Select).value
        model = self.query_one("#ai-provider-model", Select).value
        difficulty = self.query_one("#ai-provider-difficulty", Select).value
        credential_source = self.query_one("#ai-credential-source", Select).value
        preserved_connection = (
            _AUTO_MODEL if model is Select.NULL else str(model),
            Difficulty.AUTO.value if difficulty is Select.NULL else str(difficulty),
            CredentialSource.ENVIRONMENT.value
            if credential_source is Select.NULL
            else str(credential_source),
        )
        self._record_setup(
            snapshot,
            self._policies,
            update_draft_base=not preserve_machine_draft,
        )
        if preserve_machine_draft:
            with self.prevent(Select.Changed):
                if primary is not Select.NULL:
                    self.query_one("#ai-primary-driver", Select).value = str(primary)
                if configure is not Select.NULL:
                    self.query_one("#ai-configure-driver", Select).value = str(
                        configure
                    )
            self._last_primary_driver = DriverId(
                _select_value(self.query_one("#ai-primary-driver", Select))
            )
            self._last_configure_driver = self._selected_driver()
            self._machine_role_drafts = machine_roles
            self._machine_undo_drafts = machine_undo
            self._machine_connection_dirty = connection_dirty
            self._render_selected_provider(preserved_connection)
            self._render_machine_role_choices()
            self.query_one("#ai-undo-recommended", Button).disabled = (
                machine_undo is None
            )
            self._refresh_machine_role_actions()
            self.show_notice(
                "Provider status refreshed; the newer unsaved machine draft was "
                f"preserved against revision {self._machine_draft_base_revision}. "
                "If the authoritative revision changed, Save will report a conflict."
            )
        else:
            self._load_task_policies()
        if not preserve_project_draft:
            self._load_project_settings()

    def _load_project_settings(self) -> None:
        project = self.project
        if project is None:
            return
        self._project_settings_load_generation += 1
        request_generation = self._project_settings_load_generation
        draft_generation = self._project_draft_generation

        def load() -> None:
            try:
                project_settings = (
                    self.proof_app.service.get_project_verification_settings(project)
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._record_project_settings_load_error,
                    str(exc),
                    request_generation,
                    draft_generation,
                )
                return
            self.proof_app.call_from_thread(
                self._record_project_settings,
                project_settings,
                request_generation=request_generation,
                draft_generation=draft_generation,
            )

        self.run_worker(load, thread=True, exclusive=True, group="project-ai-settings")

    def _record_project_settings_load_error(
        self,
        detail: str,
        request_generation: int,
        draft_generation: int,
    ) -> None:
        if (
            request_generation != self._project_settings_load_generation
            or draft_generation != self._project_draft_generation
        ):
            return
        self.show_notice(
            f"Project AI settings could not be loaded: {detail}", error=True
        )

    def _record_project_settings(
        self,
        project_settings: ProjectVerificationSettingsSnapshot,
        *,
        request_generation: int | None = None,
        draft_generation: int | None = None,
    ) -> None:
        if request_generation is not None and (
            request_generation != self._project_settings_load_generation
            or draft_generation != self._project_draft_generation
        ):
            return
        if request_generation is None:
            # Save/reset results are authoritative and supersede any older read.
            self._project_settings_load_generation += 1
        if not self.is_mounted or not self.query("#project-ai-summary").nodes:
            return
        self.project_settings = project_settings
        self._project_role_drafts = {
            role.task: (role.model, Difficulty(role.effort))
            for role in project_settings.effective.role_settings
        }
        if not self._project_role_drafts:
            self._project_role_drafts[TaskKind.PROOF] = (
                project_settings.effective.model,
                Difficulty(project_settings.effective.effort),
            )
        self._saved_project_role_drafts = dict(self._project_role_drafts)
        self._saved_project_driver = DriverId(project_settings.effective.ai_driver)
        self._project_undo_drafts = None
        self._project_customizing = not project_settings.inherited
        self._render_project_ai_choices(reset_driver=True)
        scope = (
            "inherits the current machine proof defaults"
            if project_settings.inherited
            else "uses a project-specific override"
        )
        effective = project_settings.effective
        validity = (
            "ready"
            if project_settings.valid
            else f"needs attention: {project_settings.validation_error}"
        )
        role_lines = "\n".join(
            f"  {_ROLE_LABELS[role.task]}: {role.model} / {role.effort}"
            for role in project_settings.effective.role_settings
        )
        self.query_one("#project-ai-summary", TextArea).text = (
            f"Project: {project_settings.project_path}\n"
            f"Revision: {project_settings.revision}\n"
            f"Scope: {scope}\n"
            f"Status: {validity}\n"
            f"Provider: {effective.ai_driver}\n"
            f"Effective role assignments for the next run:\n{role_lines}"
        )
        self.query_one(
            "#reset-project-ai", Button
        ).disabled = project_settings.inherited
        self.query_one(
            "#customize-project-ai", Button
        ).disabled = not project_settings.inherited
        self._render_project_roster()
        self._render_scope(
            self.query_one("#ai-scope-control", SettingsScopeSelector).scope
        )

    def _render_project_ai_choices(self, *, reset_driver: bool = False) -> None:
        snapshot = self.snapshot
        project_settings = self.project_settings
        if (
            snapshot is None
            or project_settings is None
            or not self.query("#project-ai-driver").nodes
        ):
            return
        driver_select = self.query_one("#project-ai-driver", Select)
        if reset_driver:
            self._rendering_role_controls = True
            try:
                with self.prevent(Select.Changed):
                    driver_select.disabled = not self._project_customizing
                    driver_select.value = project_settings.effective.ai_driver
                    self.query_one(
                        "#project-ai-role", Select
                    ).disabled = not self._project_customizing
            finally:
                self._rendering_role_controls = False
        driver = DriverId(_select_value(driver_select))
        status = _status_for(snapshot, driver)
        catalog = status.catalog
        options: list[tuple[str, str]] = []
        if catalog is not None:
            options.extend(
                (f"{model.display_name} [{model.model_id}]", model.model_id)
                for model in catalog.models
            )
        task = TaskKind(_select_value(self.query_one("#project-ai-role", Select)))
        current = self._project_role_drafts.get(task)
        current_model = current[0] if current is not None else None
        available_models = {value for _, value in options}
        if current_model not in available_models:
            options.insert(0, ("Choose a supported model", "__needs_update__"))
        model_select = self.query_one("#project-ai-role-model", Select)
        self._rendering_role_controls = True
        try:
            with self.prevent(Select.Changed):
                model_select.set_options(options)
                model_select.disabled = (
                    not available_models or not self._project_customizing
                )
                if current_model in available_models:
                    model_select.value = current_model
                else:
                    model_select.value = "__needs_update__"
        finally:
            self._rendering_role_controls = False
        self._render_project_role_difficulties()
        difficulty_select = self.query_one("#project-ai-role-difficulty", Select)
        if not self._project_customizing:
            difficulty_select.disabled = True
        self._render_project_roster()
        self._refresh_project_role_actions()

    def _customize_project_settings(self) -> None:
        if self.project_settings is None:
            self.show_notice("Project AI settings are still loading.", error=True)
            return
        self._project_customizing = True
        self._project_draft_generation += 1
        self.query_one("#customize-project-ai", Button).disabled = True
        self._render_project_ai_choices(reset_driver=True)
        self.show_notice(
            "Project customization enabled. The inherited team is your editable draft."
        )

    def _render_project_role_difficulties(self) -> None:
        snapshot = self.snapshot
        if snapshot is None or not self.query("#project-ai-role-model").nodes:
            return
        driver = DriverId(_select_value(self.query_one("#project-ai-driver", Select)))
        status = _status_for(snapshot, driver)
        selected = self.query_one("#project-ai-role-model", Select).value
        task = TaskKind(_select_value(self.query_one("#project-ai-role", Select)))
        descriptor = None
        if status.catalog is not None and selected is not Select.NULL:
            descriptor = next(
                (
                    model
                    for model in status.catalog.models
                    if model.model_id == str(selected)
                ),
                None,
            )
        difficulties = descriptor.difficulties if descriptor is not None else ()
        difficulty_select = self.query_one("#project-ai-role-difficulty", Select)
        difficulty_options = tuple(
            (
                difficulty.value.replace("xhigh", "Extra high").title(),
                difficulty.value,
            )
            for difficulty in difficulties
        )
        self._rendering_role_controls = True
        try:
            with self.prevent(Select.Changed):
                current = self._project_role_drafts.get(task)
                current_is_supported = bool(
                    current is not None
                    and selected is not Select.NULL
                    and current[0] == str(selected)
                    and current[1] in difficulties
                )
                rendered_options = difficulty_options
                if difficulty_options and not current_is_supported:
                    rendered_options = (
                        ("Choose a supported difficulty", "__needs_update__"),
                        *difficulty_options,
                    )
                difficulty_select.set_options(
                    rendered_options
                    or (("Choose a provider and model first", "__loading__"),)
                )
                difficulty_select.disabled = not difficulties
                if difficulty_options:
                    difficulty_select.value = (
                        current[1].value
                        if current_is_supported and current is not None
                        else "__needs_update__"
                    )
        finally:
            self._rendering_role_controls = False

    def _project_draft_signature(self) -> tuple[object, ...]:
        driver = self.query_one("#project-ai-driver", Select).value
        return (
            None if driver is Select.NULL else str(driver),
            tuple(
                (
                    task.value,
                    self._project_role_drafts.get(task, ("", Difficulty.AUTO))[0],
                    self._project_role_drafts.get(task, ("", Difficulty.AUTO))[1].value,
                )
                for task in TaskKind
            ),
        )

    def _save_project_settings(self) -> bool:
        if self._project_save_in_flight:
            self.show_notice("Project role settings are already being saved.")
            return False
        project = self.project
        if project is None or self.project_settings is None:
            self.show_notice("Project AI settings are still loading.", error=True)
            return False
        selected_driver = DriverId(
            _select_value(self.query_one("#project-ai-driver", Select))
        )
        snapshot = self.snapshot
        if snapshot is None or not _status_for(snapshot, selected_driver).ready:
            self.show_notice(
                "The selected project provider is not ready. Recheck its "
                "installation and authentication before saving.",
                error=True,
            )
            self._refresh_project_role_actions()
            return False
        if not self._role_team_is_valid(selected_driver, self._project_role_drafts):
            self.show_notice(
                "Project team cannot be saved until all 8 roles use a model and "
                "reasoning effort supported by the selected provider.",
                error=True,
            )
            self._refresh_project_role_actions()
            return False
        try:
            override = ProjectAIOverride(
                ai_driver=selected_driver,
                roles=tuple(
                    ProjectAIRoleOverride(
                        task=task,
                        model=self._project_role_drafts[task][0],
                        difficulty=self._project_role_drafts[task][1],
                    )
                    for task in TaskKind
                ),
            )
        except (ValueError, LookupError) as exc:
            self.show_notice(f"Invalid project AI setting: {exc}", error=True)
            return False
        expected_revision = self.project_settings.revision
        submitted_signature = self._project_draft_signature()
        submitted_drafts = dict(self._project_role_drafts)
        submitted_driver = override.ai_driver
        self._project_save_in_flight = True
        self.show_notice("Saving the project-specific provider choice…")

        def save() -> None:
            try:
                updated = self.proof_app.service.update_project_verification_settings(
                    project,
                    override,
                    expected_revision=expected_revision,
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._navigation_save_failed,
                    f"Project AI settings were not saved: {exc}",
                    True,
                )
                return
            self.proof_app.call_from_thread(
                self._record_project_settings_after_save,
                updated,
                submitted_signature,
                submitted_drafts,
                submitted_driver,
            )

        self.run_worker(save, thread=True, exclusive=True, group="project-ai-settings")
        return True

    def _record_project_settings_after_save(
        self,
        project_settings: ProjectVerificationSettingsSnapshot,
        submitted_signature: tuple[object, ...],
        submitted_drafts: dict[TaskKind, tuple[str, Difficulty]],
        submitted_driver: DriverId,
    ) -> None:
        self._project_save_in_flight = False
        if self._project_draft_signature() != submitted_signature:
            live_drafts = dict(self._project_role_drafts)
            live_driver = self.query_one("#project-ai-driver", Select).value
            self._record_project_settings(project_settings)
            self._saved_project_role_drafts = dict(submitted_drafts)
            self._saved_project_driver = submitted_driver
            self._project_role_drafts = live_drafts
            if live_driver is not Select.NULL:
                with self.prevent(Select.Changed):
                    self.query_one("#project-ai-driver", Select).value = str(
                        live_driver
                    )
            self._render_project_ai_choices()
            self._pending_navigation = None
            self.show_notice(
                "The submitted project team was saved; newer edits remain unsaved."
            )
            return
        self._record_project_settings(project_settings)
        self.show_notice(
            "Project provider choice saved. It applies to the next verification run."
        )
        self._finish_pending_navigation()

    def _reset_project_settings(self) -> None:
        project = self.project
        if project is None or self.project_settings is None:
            self.show_notice("Project AI settings are still loading.", error=True)
            return
        expected_revision = self.project_settings.revision
        self.show_notice("Restoring this project to the machine proof defaults…")

        def reset() -> None:
            try:
                updated = self.proof_app.service.reset_project_verification_settings(
                    project,
                    expected_revision=expected_revision,
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self.show_notice,
                    f"Project AI settings were not reset: {exc}",
                    error=True,
                )
                return
            self.proof_app.call_from_thread(self._record_project_settings, updated)
            self.proof_app.call_from_thread(
                self.show_notice,
                "This project now inherits the current machine proof defaults.",
            )

        self.run_worker(reset, thread=True, exclusive=True, group="project-ai-settings")

    def _review_reset_project_settings(self) -> None:
        dialog = ProjectInheritanceConfirmationScreen()

        def after_confirmation(accepted: bool | None) -> None:
            if accepted:
                self._reset_project_settings()
            else:
                self.show_notice("Project override retained; no settings were changed.")

        self.proof_app.push_screen(dialog, callback=after_confirmation)

    def _load_recommended_role_policies(
        self, driver: DriverId, *, project: bool
    ) -> None:
        """Load a complete capability-checked default matrix for one provider."""

        target = "project" if project else "machine"
        if project:
            self._project_defaults_generation += 1
            request_generation = self._project_defaults_generation
            draft_generation = self._project_draft_generation
        else:
            self._machine_defaults_generation += 1
            request_generation = self._machine_defaults_generation
            draft_generation = self._machine_draft_generation
        notice_generation = self._begin_notice(
            f"Loading recommended {_driver_label(driver)} defaults for every role…"
        )

        def load() -> None:
            try:
                policies = self.proof_app.service.ai_task_policies(driver=driver)
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._record_recommended_role_load_error,
                    project,
                    notice_generation,
                    request_generation,
                    draft_generation,
                    driver,
                    str(exc),
                )
                return
            self.proof_app.call_from_thread(
                self._apply_recommended_role_policies,
                policies,
                project,
                notice_generation,
                request_generation,
                draft_generation,
                driver,
            )

        self.run_worker(
            load,
            thread=True,
            exclusive=True,
            group=f"{target}-ai-role-defaults",
        )

    def _record_recommended_role_load_error(
        self,
        project: bool,
        notice_generation: int,
        request_generation: int,
        draft_generation: int,
        expected_driver: DriverId,
        detail: str,
    ) -> None:
        if not self.is_mounted:
            return
        active_generation = (
            self._project_defaults_generation
            if project
            else self._machine_defaults_generation
        )
        active_draft_generation = (
            self._project_draft_generation
            if project
            else self._machine_draft_generation
        )
        selector_id = "#project-ai-driver" if project else "#ai-primary-driver"
        selector_nodes = self.query(selector_id).nodes
        if (
            request_generation != active_generation
            or draft_generation != active_draft_generation
            or not selector_nodes
            or not isinstance(selector_nodes[0], Select)
            or selector_nodes[0].value is Select.NULL
            or str(selector_nodes[0].value) != expected_driver.value
        ):
            return
        if project:
            self._render_project_draft_summary(expected_driver)
        else:
            self.query_one("#ai-task-policies", TextArea).text = (
                f"Selected provider: {_driver_label(expected_driver)} "
                f"[{expected_driver.value}]\n"
                "Recommended defaults could not be loaded; assignments remain "
                "incomplete."
            )
        self._complete_notice(
            notice_generation,
            f"Recommended role defaults could not be loaded: {detail}",
            error=True,
        )

    def _apply_recommended_role_policies(
        self,
        policies: tuple[TaskModelPolicy, ...],
        project: bool,
        notice_generation: int,
        request_generation: int,
        draft_generation: int,
        expected_driver: DriverId,
    ) -> None:
        if not self.is_mounted:
            return
        active_generation = (
            self._project_defaults_generation
            if project
            else self._machine_defaults_generation
        )
        active_draft_generation = (
            self._project_draft_generation
            if project
            else self._machine_draft_generation
        )
        selector_id = "#project-ai-driver" if project else "#ai-primary-driver"
        selector_nodes = self.query(selector_id).nodes
        if (
            request_generation != active_generation
            or draft_generation != active_draft_generation
            or not selector_nodes
            or not isinstance(selector_nodes[0], Select)
            or selector_nodes[0].value is Select.NULL
            or str(selector_nodes[0].value) != expected_driver.value
        ):
            self._complete_notice(
                notice_generation,
                "Recommended defaults were not applied because the provider or "
                "role assignments changed while they were loading.",
            )
            return
        tasks = tuple(policy.task for policy in policies)
        drafts = {
            policy.task: (policy.model or "", policy.difficulty) for policy in policies
        }
        valid_matrix = (
            len(tasks) == len(TaskKind)
            and len(set(tasks)) == len(tasks)
            and set(tasks) == set(TaskKind)
            and all(policy.driver is expected_driver for policy in policies)
            and all(
                self._role_assignment_is_valid(
                    expected_driver,
                    (policy.model or "", policy.difficulty),
                )
                for policy in policies
            )
        )
        if not valid_matrix:
            self._complete_notice(
                notice_generation,
                "The provider returned invalid recommended defaults. The role team "
                "was not changed.",
                error=True,
            )
            return
        if project:
            if set(self._project_role_drafts) == set(TaskKind):
                self._project_undo_drafts = dict(self._project_role_drafts)
            self._project_role_drafts = drafts
            self._project_draft_generation += 1
            self._render_project_draft_summary(expected_driver)
            self._render_project_ai_choices()
            self.query_one("#project-ai-undo-recommended", Button).disabled = (
                self._project_undo_drafts is None
            )
        else:
            if set(self._machine_role_drafts) == set(TaskKind):
                self._machine_undo_drafts = dict(self._machine_role_drafts)
            self._machine_role_drafts = drafts
            self._policies = policies
            self.query_one("#ai-task-policies", TextArea).text = _task_policy_summary(
                policies
            )
            self._render_machine_role_choices()
            self.query_one("#ai-undo-recommended", Button).disabled = (
                self._machine_undo_drafts is None
            )
            self._refresh_machine_role_actions()
        self._complete_notice(
            notice_generation, "Recommended defaults loaded for all eight roles."
        )

    def _undo_recommended_role_policies(self, *, project: bool) -> None:
        if project:
            if self._project_undo_drafts is None:
                return
            self._project_role_drafts = self._project_undo_drafts
            self._project_draft_generation += 1
            self._project_undo_drafts = None
            self.query_one("#project-ai-undo-recommended", Button).disabled = True
            driver = DriverId(
                _select_value(self.query_one("#project-ai-driver", Select))
            )
            self._render_project_draft_summary(driver)
            self._render_project_ai_choices()
        else:
            if self._machine_undo_drafts is None:
                return
            self._machine_role_drafts = self._machine_undo_drafts
            self._machine_draft_generation += 1
            self._machine_undo_drafts = None
            self.query_one("#ai-undo-recommended", Button).disabled = True
            self._render_machine_role_choices()
        self.show_notice("Restored the role team from before provider defaults.")

    def _render_machine_roster(self) -> None:
        driver = DriverId(_select_value(self.query_one("#ai-primary-driver", Select)))
        selected = TaskKind(_select_value(self.query_one("#ai-role-task", Select)))
        roster = self.query_one("#ai-role-roster", RoleRoster)
        rows: tuple[RoleAssignmentRow, ...] = tuple(
            RoleAssignmentRow(
                task=task,
                model=assignment[0] if assignment is not None and valid else None,
                effort=(
                    assignment[1]
                    if assignment is not None and valid
                    else Difficulty.AUTO
                ),
                state=("Custom" if self._machine_undo_drafts is not None else "Ready")
                if valid
                else "Awaiting assignment",
            )
            for task in TaskKind
            for assignment in (self._machine_role_drafts.get(task),)
            for valid in (self._role_assignment_is_valid(driver, assignment),)
        )
        if rows != self._machine_roster_signature:
            self._ignore_machine_highlight = selected
            roster.set_roles(rows, selected=selected)
            self._machine_roster_signature = rows
        elif roster.selected_task is not selected:
            roster.select_task(selected)
        assignment = self._machine_role_drafts.get(selected)
        valid = self._role_assignment_is_valid(driver, assignment)
        model, effort = (
            assignment if valid and assignment is not None else (None, Difficulty.AUTO)
        )
        self.query_one("#ai-role-detail", SelectedRoleDetail).update_role(
            task=selected,
            model=model,
            effort=effort,
            state=(
                "Complete machine assignment"
                if valid
                else "Awaiting assignment for selected provider"
            ),
        )

    def _render_project_roster(self) -> None:
        selected = TaskKind(_select_value(self.query_one("#project-ai-role", Select)))
        inherited = self.project_settings is None or self.project_settings.inherited
        driver = DriverId(_select_value(self.query_one("#project-ai-driver", Select)))
        roster = self.query_one("#project-ai-role-roster", RoleRoster)
        rows: tuple[RoleAssignmentRow, ...] = tuple(
            RoleAssignmentRow(
                task=task,
                model=assignment[0] if assignment is not None and valid else None,
                effort=(
                    assignment[1]
                    if assignment is not None and valid
                    else Difficulty.AUTO
                ),
                state=("Inherited" if inherited else "Project custom")
                if valid
                else "Awaiting assignment",
            )
            for task in TaskKind
            for assignment in (self._project_role_drafts.get(task),)
            for valid in (self._role_assignment_is_valid(driver, assignment),)
        )
        if rows != self._project_roster_signature:
            self._ignore_project_highlight = selected
            roster.set_roles(rows, selected=selected)
            self._project_roster_signature = rows
        elif roster.selected_task is not selected:
            roster.select_task(selected)
        assignment = self._project_role_drafts.get(selected)
        valid = self._role_assignment_is_valid(driver, assignment)
        model, effort = (
            assignment if valid and assignment is not None else (None, Difficulty.AUTO)
        )
        self.query_one("#project-ai-role-detail", SelectedRoleDetail).update_role(
            task=selected,
            model=model,
            effort=effort,
            state=("Inherited" if inherited else "Complete project assignment")
            if valid
            else "Awaiting assignment for selected provider",
        )

    def _render_machine_role_choices(self) -> None:
        snapshot = self.snapshot
        if snapshot is None or not self.query("#ai-role-model").nodes:
            return
        driver = DriverId(_select_value(self.query_one("#ai-primary-driver", Select)))
        task = TaskKind(_select_value(self.query_one("#ai-role-task", Select)))
        status = _status_for(snapshot, driver)
        options = (
            [
                (f"{item.display_name} [{item.model_id}]", item.model_id)
                for item in status.catalog.models
            ]
            if status.catalog is not None
            else []
        )
        current = self._machine_role_drafts.get(task)
        available_models = {value for _, value in options}
        if current is None or current[0] not in available_models:
            options.insert(0, ("Choose a supported model", "__needs_update__"))
        model_select = self.query_one("#ai-role-model", Select)
        self._rendering_role_controls = True
        try:
            with self.prevent(Select.Changed):
                model_select.set_options(options)
                model_select.disabled = not available_models
                if current is not None and current[0] in available_models:
                    model_select.value = current[0]
                else:
                    model_select.value = "__needs_update__"
        finally:
            self._rendering_role_controls = False
        self._render_machine_role_difficulties()
        self._render_machine_roster()
        self._refresh_machine_role_actions()

    def _render_machine_role_difficulties(self) -> None:
        snapshot = self.snapshot
        if snapshot is None or not self.query("#ai-role-model").nodes:
            return
        driver = DriverId(_select_value(self.query_one("#ai-primary-driver", Select)))
        task = TaskKind(_select_value(self.query_one("#ai-role-task", Select)))
        selected = self.query_one("#ai-role-model", Select).value
        descriptor = None
        status = _status_for(snapshot, driver)
        if status.catalog is not None and selected is not Select.NULL:
            descriptor = next(
                (
                    item
                    for item in status.catalog.models
                    if item.model_id == str(selected)
                ),
                None,
            )
        current = self._machine_role_drafts.get(task)
        difficulties = descriptor.difficulties if descriptor is not None else ()
        options = tuple(
            (item.value.replace("xhigh", "Extra high").title(), item.value)
            for item in difficulties
        )
        select = self.query_one("#ai-role-difficulty", Select)
        self._rendering_role_controls = True
        try:
            with self.prevent(Select.Changed):
                current_is_supported = bool(
                    current is not None
                    and selected is not Select.NULL
                    and current[0] == str(selected)
                    and current[1] in difficulties
                )
                rendered_options = options
                if options and not current_is_supported:
                    rendered_options = (
                        ("Choose a supported difficulty", "__needs_update__"),
                        *options,
                    )
                select.set_options(
                    rendered_options or (("No supported difficulty", "__none__"),)
                )
                select.disabled = not options
                if options:
                    select.value = (
                        current[1].value
                        if current_is_supported and current is not None
                        else "__needs_update__"
                    )
        finally:
            self._rendering_role_controls = False
        self._render_machine_roster()

    def _record_machine_role_model(self) -> None:
        if self._rendering_role_controls:
            return
        task = TaskKind(_select_value(self.query_one("#ai-role-task", Select)))
        model = _select_value(self.query_one("#ai-role-model", Select))
        current = self._machine_role_drafts.get(task)
        self._machine_role_drafts[task] = (
            model,
            current[1] if current is not None else Difficulty.AUTO,
        )
        self._machine_draft_generation += 1
        self._render_machine_role_difficulties()
        self._render_machine_roster()

    def _record_machine_role_difficulty(self) -> None:
        if self._rendering_role_controls:
            return
        task = TaskKind(_select_value(self.query_one("#ai-role-task", Select)))
        model = _select_value(self.query_one("#ai-role-model", Select))
        difficulty = Difficulty(
            _select_value(self.query_one("#ai-role-difficulty", Select))
        )
        self._machine_role_drafts[task] = (model, difficulty)
        self._machine_draft_generation += 1
        self._render_machine_roster()

    def _record_project_role_model(self) -> None:
        if self._rendering_role_controls:
            return
        task = TaskKind(_select_value(self.query_one("#project-ai-role", Select)))
        model = _select_value(self.query_one("#project-ai-role-model", Select))
        current = self._project_role_drafts.get(task)
        self._project_role_drafts[task] = (
            model,
            current[1] if current is not None else Difficulty.AUTO,
        )
        self._project_draft_generation += 1
        driver = DriverId(_select_value(self.query_one("#project-ai-driver", Select)))
        self._render_project_draft_summary(driver)
        self._render_project_role_difficulties()
        self._render_project_roster()

    def _record_project_role_difficulty(self) -> None:
        if self._rendering_role_controls:
            return
        task = TaskKind(_select_value(self.query_one("#project-ai-role", Select)))
        model = _select_value(self.query_one("#project-ai-role-model", Select))
        difficulty = Difficulty(
            _select_value(self.query_one("#project-ai-role-difficulty", Select))
        )
        self._project_role_drafts[task] = (model, difficulty)
        self._project_draft_generation += 1
        driver = DriverId(_select_value(self.query_one("#project-ai-driver", Select)))
        self._render_project_draft_summary(driver)
        self._render_project_roster()

    def _load_task_policies(self) -> None:
        """Load policy DTOs without redundantly probing every provider again."""

        self._task_policy_load_generation += 1
        load_generation = self._task_policy_load_generation
        request_generation = self._machine_draft_generation

        def load() -> None:
            try:
                policies = self.proof_app.service.ai_task_policies()
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._record_task_policy_load_error,
                    str(exc),
                    load_generation,
                    request_generation,
                )
                return
            self.proof_app.call_from_thread(
                self._record_task_policies,
                policies,
                load_generation,
                request_generation,
            )

        self.run_worker(load, thread=True, exclusive=True, group="ai-task-policies")

    def _record_task_policies(
        self,
        policies: tuple[TaskModelPolicy, ...],
        load_generation: int,
        request_generation: int,
    ) -> None:
        if not self.is_mounted:
            return
        if not self.query("#ai-task-policies").nodes:
            self.set_timer(
                0.01,
                lambda: self._record_task_policies(
                    policies, load_generation, request_generation
                ),
            )
            return
        if (
            load_generation != self._task_policy_load_generation
            or request_generation != self._machine_draft_generation
        ):
            return
        self._policies = policies
        self._machine_role_drafts = {
            policy.task: (policy.model or "", policy.difficulty) for policy in policies
        }
        self._saved_machine_role_drafts = dict(self._machine_role_drafts)
        self._machine_connection_dirty = False
        self._machine_undo_drafts = None
        self.query_one("#ai-undo-recommended", Button).disabled = True
        self.query_one("#ai-task-policies", TextArea).text = _task_policy_summary(
            policies
        )
        self._render_machine_role_choices()

    def _record_task_policy_load_error(
        self,
        detail: str,
        load_generation: int,
        request_generation: int,
    ) -> None:
        if (
            load_generation != self._task_policy_load_generation
            or request_generation != self._machine_draft_generation
        ):
            return
        self.show_notice(f"Task policy loading failed: {detail}", error=True)

    def _record_setup(
        self,
        snapshot: ProviderSetupSnapshot,
        policies: tuple[TaskModelPolicy, ...],
        *,
        update_draft_base: bool = True,
    ) -> None:
        if not self.is_mounted:
            return
        if not self.query("#ai-provider-summary").nodes:
            self.set_timer(0.01, lambda: self._record_setup(snapshot, policies))
            return
        previous_driver = None
        configured_nodes = self.query("#ai-configure-driver").nodes
        if configured_nodes and isinstance(configured_nodes[0], Select):
            value = configured_nodes[0].value
            if value is not Select.NULL:
                previous_driver = str(value)
        self.snapshot = snapshot
        if update_draft_base:
            self._machine_draft_base_revision = snapshot.settings.revision
        self._last_primary_driver = snapshot.primary_driver
        self._policies = policies
        self.proof_app.record_ai_setup(snapshot)
        self.query_one("#ai-provider-summary", TextArea).text = _provider_summary(
            snapshot
        )
        self.query_one("#ai-task-policies", TextArea).text = _task_policy_summary(
            policies
        )
        provider_rows = tuple(
            ProviderConnectionRow(
                driver=status.driver,
                transport=status.transport,
                connection=status.authentication.value,
                state=status.installation.value,
                primary=status.driver is snapshot.primary_driver,
            )
            for status in snapshot.statuses
        )
        if provider_rows != self._provider_roster_signature:
            roster_driver = (
                DriverId(previous_driver)
                if previous_driver in {driver.value for driver in DriverId}
                else snapshot.primary_driver
            )
            self._ignore_provider_highlight = roster_driver
            self.query_one(
                "#ai-provider-roster", ProviderConnectionRoster
            ).set_providers(provider_rows, selected=roster_driver)
            self._provider_roster_signature = provider_rows
        primary = self.query_one("#ai-primary-driver", Select)
        configure = self.query_one("#ai-configure-driver", Select)
        with self.prevent(Select.Changed):
            primary.disabled = False
            primary.value = snapshot.primary_driver.value
            configure.disabled = False
            configure.value = (
                previous_driver
                if previous_driver in {driver.value for driver in DriverId}
                else snapshot.primary_driver.value
            )
        self._last_configure_driver = DriverId(str(configure.value))
        self.query_one("#ai-role-task", Select).disabled = False
        self._refresh_machine_role_actions()
        continue_button = self.query_one("#ai-setup-continue", Button)
        continue_button.disabled = self.first_run and (
            not snapshot.primary_ready or not self._first_run_team_reviewed
        )
        self.query_one("#ai-provider-back", Button).disabled = False
        self._render_selected_provider()
        current_view = self.query_one("#ai-settings-pages", ContentSwitcher).current
        if current_view is not None:
            self._sync_first_run_actions(current_view.removesuffix("-page"))
        self.call_after_refresh(self._enable_provider_controls)
        self.show_notice(snapshot.detail, error=not snapshot.primary_ready)

    def _render_selected_provider(
        self,
        preserved: tuple[str, str, str] | None = None,
    ) -> None:
        snapshot = self.snapshot
        if snapshot is None or not self.query("#ai-configure-driver").nodes:
            return
        driver = self._selected_driver()
        status = _status_for(snapshot, driver)
        preference = snapshot.settings.config.preference_for(driver)
        catalog = status.catalog
        options: list[tuple[str, str]] = [
            ("Automatic task-specific choice", _AUTO_MODEL)
        ]
        if catalog is not None:
            options.extend(
                (f"{model.display_name} [{model.model_id}]", model.model_id)
                for model in catalog.models
            )
        if preference.model is not None and preference.model not in {
            value for _, value in options
        }:
            options.append(
                (
                    f"{preference.model} [configured; not in current catalog]",
                    preference.model,
                )
            )
        if (
            preserved is not None
            and preserved[0] != _AUTO_MODEL
            and preserved[0] not in {value for _, value in options}
        ):
            options.append(
                (
                    f"{preserved[0]} [unsaved draft; not in current catalog]",
                    preserved[0],
                )
            )
        model_select = self.query_one("#ai-provider-model", Select)
        with self.prevent(Select.Changed):
            model_select.set_options(options)
            model_select.disabled = False
            model_select.value = (
                preserved[0]
                if preserved is not None
                else preference.model or _AUTO_MODEL
            )

        credential_select = self.query_one("#ai-credential-source", Select)
        is_api = driver in _API_DRIVERS
        credential_select.disabled = not is_api
        if is_api:
            source_value = (
                preserved[2]
                if preserved is not None
                else preference.credential_source.value
            )
            with self.prevent(Select.Changed):
                credential_select.value = (
                    source_value
                    if source_value
                    in {
                        CredentialSource.ENVIRONMENT.value,
                        CredentialSource.CREDENTIAL_STORE.value,
                    }
                    else CredentialSource.ENVIRONMENT.value
                )
        key_nodes = self.query("#ai-api-key").nodes
        if key_nodes and isinstance(key_nodes[0], Input):
            key_nodes[0].value = ""
            key_nodes[0].disabled = not is_api
        self.query_one("#store-ai-key", Button).disabled = not is_api
        self.query_one("#delete-ai-key", Button).disabled = not is_api

        installation = status.installation.value
        self.query_one("#install-ai-driver", Button).disabled = not (
            status.transport.value == "cli" and installation in {"missing", "broken"}
        )
        self.query_one("#verify-ai-account", Button).disabled = not (
            driver is DriverId.COPILOT_CLI
            and installation == "installed"
            and status.authentication.value == "unknown"
        )
        self.query_one("#ai-auth-next-step", TextArea).text = (
            f"Provider: {_driver_label(driver)}\n"
            f"Authentication status: {status.authentication.value}\n"
            f"Configured credential source: {preference.credential_source.value}\n"
            f"Executable: {status.executable or 'not applicable / not found'}\n"
            f"Version: {status.version or 'not available'}\n"
            f"Next step: {status.detail or 'none'}"
        )
        self._render_difficulties(
            preserved_difficulty=(preserved[1] if preserved is not None else None)
        )

    def _render_difficulties(self, preserved_difficulty: str | None = None) -> None:
        snapshot = self.snapshot
        if snapshot is None or not self.query("#ai-provider-model").nodes:
            return
        driver = self._selected_driver()
        status = _status_for(snapshot, driver)
        preference = snapshot.settings.config.preference_for(driver)
        selected = self.query_one("#ai-provider-model", Select).value
        difficulties: tuple[Difficulty, ...] = ()
        if status.catalog is not None and selected is not Select.NULL:
            descriptor = next(
                (
                    model
                    for model in status.catalog.models
                    if model.model_id == str(selected)
                ),
                None,
            )
            if descriptor is not None:
                difficulties = descriptor.difficulties
        if not difficulties and status.catalog is not None and status.catalog.models:
            # With automatic model choice, only offer values valid for every
            # candidate the backend may select. Choosing an explicit model below
            # exposes that model's complete exact set.
            first_model, *remaining_models = status.catalog.models
            common = list(first_model.difficulties)
            for candidate in remaining_models:
                common = [
                    difficulty
                    for difficulty in common
                    if difficulty in candidate.difficulties
                ]
            difficulties = tuple(common)
        if not difficulties:
            difficulties = (preference.difficulty,)
        options = tuple(
            (difficulty.value.replace("xhigh", "Extra high").title(), difficulty.value)
            for difficulty in difficulties
        )
        if preserved_difficulty is not None and preserved_difficulty not in {
            value for _, value in options
        }:
            options = (
                *options,
                (
                    preserved_difficulty.replace("xhigh", "Extra high").title()
                    + " [unsaved draft]",
                    preserved_difficulty,
                ),
            )
        difficulty_select = self.query_one("#ai-provider-difficulty", Select)
        with self.prevent(Select.Changed):
            difficulty_select.set_options(options)
            difficulty_select.disabled = False
            allowed = {value for _, value in options}
            desired = preserved_difficulty or preference.difficulty.value
            difficulty_select.value = desired if desired in allowed else options[0][1]

    def _machine_draft_signature(self) -> tuple[object, ...]:
        def value(selector: str) -> str | None:
            selected = self.query_one(selector, Select).value
            return None if selected is Select.NULL else str(selected)

        return (
            value("#ai-primary-driver"),
            value("#ai-configure-driver"),
            value("#ai-provider-model"),
            value("#ai-provider-difficulty"),
            value("#ai-credential-source"),
            tuple(
                (
                    task.value,
                    self._machine_role_drafts.get(task, ("", Difficulty.AUTO))[0],
                    self._machine_role_drafts.get(task, ("", Difficulty.AUTO))[1].value,
                )
                for task in TaskKind
            ),
        )

    def _save_settings(self) -> bool:
        if self._machine_save_in_flight:
            self.show_notice("Machine role settings are already being saved.")
            return False
        snapshot = self.snapshot
        if snapshot is None:
            self.show_notice("Provider setup has not loaded yet.", error=True)
            return False
        try:
            primary = DriverId(
                _select_value(self.query_one("#ai-primary-driver", Select))
            )
            if not _status_for(snapshot, primary).ready:
                self.show_notice(
                    "The selected primary provider is not ready. Recheck its "
                    "installation and authentication before saving.",
                    error=True,
                )
                return False
            if not self._role_team_is_valid(primary, self._machine_role_drafts):
                self.show_notice(
                    "Machine team cannot be saved until all 8 roles use a model and "
                    "reasoning effort supported by the selected provider.",
                    error=True,
                )
                self._refresh_machine_role_actions()
                return False
            driver = self._selected_driver()
            model_value = _select_value(self.query_one("#ai-provider-model", Select))
            difficulty_value = _select_value(
                self.query_one("#ai-provider-difficulty", Select)
            )
            preference = snapshot.settings.config.preference_for(driver)
            source = preference.credential_source
            if driver in _API_DRIVERS:
                source = CredentialSource(
                    _select_value(self.query_one("#ai-credential-source", Select))
                )
            updated_preference = replace(
                preference,
                credential_source=source,
                model=None if model_value == _AUTO_MODEL else model_value,
                difficulty=Difficulty(difficulty_value),
            )
            drivers = tuple(
                updated_preference if item.driver is driver else item
                for item in snapshot.settings.config.drivers
            )
            if not any(item.driver is driver for item in drivers):
                drivers = (*drivers, updated_preference)
            missing_roles = set(TaskKind) - set(self._machine_role_drafts)
            if missing_roles:
                raise ValueError(
                    "Load defaults before saving; missing roles: "
                    + ", ".join(sorted(item.value for item in missing_roles))
                )
            config: ProviderConfig = replace(
                snapshot.settings.config,
                primary_driver=primary,
                drivers=drivers,
                tasks=tuple(
                    TaskPreference(
                        task=task,
                        driver=primary,
                        model=self._machine_role_drafts[task][0],
                        difficulty=self._machine_role_drafts[task][1],
                    )
                    for task in TaskKind
                ),
            )
        except (ValueError, LookupError) as exc:
            self.show_notice(f"Invalid provider setting: {exc}", error=True)
            return False
        submitted_signature = self._machine_draft_signature()
        submitted_drafts = dict(self._machine_role_drafts)
        self._machine_save_in_flight = True
        self.show_notice("Saving the revision-checked machine provider settings…")

        def save() -> None:
            try:
                result = self.proof_app.service.update_ai_settings(
                    config,
                    expected_revision=self._machine_draft_base_revision,
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._navigation_save_failed,
                    f"Provider settings were not saved: {exc}",
                    False,
                )
                return
            self.proof_app.call_from_thread(
                self._record_setup_after_save,
                result,
                submitted_signature,
                submitted_drafts,
            )

        self.run_worker(save, thread=True, exclusive=True, group="ai-provider-setup")
        return True

    def _navigation_save_failed(self, message: str, project: bool) -> None:
        if project:
            self._project_save_in_flight = False
        else:
            self._machine_save_in_flight = False
        self._pending_navigation = None
        self.show_notice(message, error=True)

    def _record_setup_after_save(
        self,
        snapshot: ProviderSetupSnapshot,
        submitted_signature: tuple[object, ...],
        submitted_drafts: dict[TaskKind, tuple[str, Difficulty]],
    ) -> None:
        self._machine_save_in_flight = False
        if self._machine_draft_signature() != submitted_signature:
            self.snapshot = snapshot
            self._machine_draft_base_revision = snapshot.settings.revision
            self.proof_app.record_ai_setup(snapshot)
            self._saved_machine_role_drafts = dict(submitted_drafts)
            self._machine_connection_dirty = (
                self._connection_controls_differ_from_saved()
            )
            self.query_one("#ai-provider-summary", TextArea).text = _provider_summary(
                snapshot
            )
            self._pending_navigation = None
            self.show_notice(
                "The submitted machine team was saved; newer edits remain unsaved."
            )
            return
        self._saved_machine_role_drafts = dict(submitted_drafts)
        self._machine_draft_base_revision = snapshot.settings.revision
        self._machine_connection_dirty = False
        self._machine_undo_drafts = None
        self._record_setup_and_reload(snapshot)
        if self.first_run:
            self.query_one("#ai-settings-nav", OptionList).highlighted = 2
            self._show_ai_view("roles")
            self.show_notice(
                "Provider saved. Review the complete eight-role team, then Continue."
            )
        self._finish_pending_navigation()

    def _preview_install(self) -> None:
        driver = self._selected_driver()
        self.show_notice(f"Loading the exact {_driver_label(driver)} install plan…")

        def preview() -> None:
            try:
                plan = self.proof_app.service.preview_ai_driver_install(driver)
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self.show_notice,
                    f"Installation preview failed: {exc}",
                    error=True,
                )
                return
            self.proof_app.call_from_thread(self._review_install, plan)

        self.run_worker(preview, thread=True, exclusive=True, group="ai-driver-install")

    def _review_install(self, plan: InstallPlan) -> None:
        dialog = AIInstallConfirmationScreen(plan)

        def after_review(accepted: bool | None) -> None:
            if accepted:
                self._install(plan)
            else:
                self.show_notice("Installation canceled; no command was run.")

        self.proof_app.push_screen(dialog, callback=after_review)

    def _install(self, plan: InstallPlan) -> None:
        self.show_notice("Installing the reviewed driver through the backend…")

        def install() -> None:
            try:
                result = self.proof_app.service.install_ai_driver(
                    plan,
                    consent_token=plan.consent_token,
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self.show_notice,
                    f"Driver installation failed: {exc}",
                    error=True,
                )
                return
            self.proof_app.call_from_thread(self._installation_finished, result)

        self.run_worker(install, thread=True, exclusive=True, group="ai-driver-install")

    def _installation_finished(self, result: InstallResult) -> None:
        self.show_notice(result.detail, error=not result.succeeded)
        self.refresh_setup()

    def _review_account_verification(self) -> None:
        if self._selected_driver() is not DriverId.COPILOT_CLI:
            self.show_notice(
                "Only Copilot needs an explicit model-request account check.",
                error=True,
            )
            return
        dialog = AIAccountVerificationConfirmationScreen()

        def after_review(accepted: bool | None) -> None:
            if accepted:
                self._verify_account()
            else:
                self.show_notice("Copilot account check canceled; no request was sent.")

        self.proof_app.push_screen(dialog, callback=after_review)

    def _verify_account(self) -> None:
        driver = DriverId.COPILOT_CLI
        self.show_notice("Sending the one explicitly approved tiny Copilot request…")

        def verify() -> None:
            try:
                snapshot = self.proof_app.service.verify_ai_driver_account(
                    driver, consent=True
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self.show_notice,
                    f"Copilot account check failed: {exc}",
                    error=True,
                )
                return
            self.proof_app.call_from_thread(self._record_setup_and_reload, snapshot)

        self.run_worker(verify, thread=True, exclusive=True, group="ai-account-check")

    def _store_credential(self) -> None:
        driver = self._selected_driver()
        if driver not in _API_DRIVERS:
            self.show_notice("CLI authentication is owned by its CLI.", error=True)
            return
        key_input = self.query_one("#ai-api-key", Input)
        if self._credential_mutation_in_flight:
            key_input.value = ""
            self.show_notice(
                "A credential change is already in progress; no second key was "
                "submitted.",
                error=True,
            )
            return
        credential = key_input.value.strip()
        key_input.value = ""
        if not credential:
            self.show_notice("Paste a non-empty API key before storing it.", error=True)
            return
        submission: SecretSubmission | None = SecretSubmission(credential)
        credential = ""
        self._set_credential_mutation_busy(True)
        self.show_notice(
            "Sending the one-shot credential submission to the backend keyring…"
        )

        def store() -> None:
            nonlocal submission
            try:
                if submission is None:
                    raise RuntimeError("credential submission is no longer available")
                snapshot = self.proof_app.service.store_ai_credential(
                    driver,
                    CredentialSource.CREDENTIAL_STORE,
                    submission,
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._credential_mutation_failed,
                    f"Credential was not stored: {exc}",
                )
                return
            finally:
                submission = None
            self.proof_app.call_from_thread(
                self._credential_mutation_finished, snapshot
            )

        self.run_worker(store, thread=True, exclusive=True, group="ai-credential")

    def _review_delete_credential(self) -> None:
        if self._credential_mutation_in_flight:
            self.show_notice(
                "Wait for the current credential change to finish before removing "
                "the stored key.",
                error=True,
            )
            return
        driver = self._selected_driver()
        if driver not in _API_DRIVERS:
            self.show_notice("CLI authentication is owned by its CLI.", error=True)
            return
        self.clear_transient_secrets()
        dialog = DestructiveSettingsConfirmationScreen(
            f"Remove {_driver_label(driver)} credential?",
            "This deletes the stored key for "
            f"{_driver_label(driver)} from the OS credential store. The key value "
            "is never displayed or retained by this dialog.",
            "Remove stored key",
        )

        def after_confirmation(accepted: bool | None) -> None:
            if accepted:
                self._delete_credential(driver)
            else:
                self._mount_secret_input()
                self.show_notice("Credential removal canceled; the stored key remains.")

        self.proof_app.push_screen(dialog, callback=after_confirmation)

    def _delete_credential(self, driver: DriverId) -> None:
        if self._credential_mutation_in_flight:
            self.show_notice("A credential change is already in progress.", error=True)
            return
        self._set_credential_mutation_busy(True)
        self.show_notice("Removing the provider key from the backend keyring…")

        def delete() -> None:
            try:
                snapshot = self.proof_app.service.delete_ai_credential(
                    driver,
                    CredentialSource.CREDENTIAL_STORE,
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._credential_mutation_failed,
                    f"Credential was not removed: {exc}",
                )
                return
            self.proof_app.call_from_thread(
                self._credential_mutation_finished, snapshot
            )

        self.run_worker(delete, thread=True, exclusive=True, group="ai-credential")

    def _set_credential_mutation_busy(self, busy: bool) -> None:
        self._credential_mutation_in_flight = busy
        for selector in ("#ai-primary-driver", "#ai-configure-driver"):
            nodes = self.query(selector).nodes
            if nodes and isinstance(nodes[0], Select):
                nodes[0].disabled = busy
        navigation = self.query("#ai-settings-nav").nodes
        if navigation and isinstance(navigation[0], OptionList):
            navigation[0].disabled = busy
        for selector in (
            "#store-ai-key",
            "#delete-ai-key",
            "#install-ai-driver",
            "#verify-ai-account",
            "#ai-first-run-back",
            "#ai-first-run-next",
            "#ai-provider-back",
            "#ai-setup-continue",
        ):
            nodes = self.query(selector).nodes
            if nodes and isinstance(nodes[0], Button):
                nodes[0].disabled = busy
        key_nodes = self.query("#ai-api-key").nodes
        if key_nodes and isinstance(key_nodes[0], Input):
            key_nodes[0].disabled = busy

    def _credential_mutation_failed(self, message: str) -> None:
        self._set_credential_mutation_busy(False)
        self._render_selected_provider()
        current_view = self.query_one("#ai-settings-pages", ContentSwitcher).current
        if current_view is not None:
            self._sync_first_run_actions(current_view.removesuffix("-page"))
        self.show_notice(message, error=True)

    def _credential_mutation_finished(self, snapshot: ProviderSetupSnapshot) -> None:
        # Keep the application-level sanitized cache authoritative even if a
        # lifecycle edge unmounted this page before the backend write completed.
        self.proof_app.record_ai_setup(snapshot)
        if not self.is_mounted:
            return
        self._set_credential_mutation_busy(False)
        self._record_setup_and_reload(snapshot)


class SettingsHomeScreen(NoticeScreen):
    """Machine settings landing page with distinct modern and legacy routes."""

    BINDINGS = [BACK.binding(), REFRESH.binding()]

    DEFAULT_CSS = """
    SettingsHomeScreen #settings-home-workspace {
        overflow: hidden;
    }
    SettingsHomeScreen #settings-home-layout {
        width: 100%;
        height: 1fr;
        layout: horizontal;
        overflow: hidden;
    }
    SettingsHomeScreen #settings-category-nav {
        width: 32;
        height: 1fr;
        min-height: 7;
        margin-right: 1;
        border: round $proof-panel-border;
        background: $proof-panel-background;
    }
    SettingsHomeScreen #settings-category-pages {
        width: 1fr;
        height: 1fr;
    }
    SettingsHomeScreen .settings-category-detail {
        width: 100%;
        height: 1fr;
        padding: 0 1;
        border: round $proof-panel-border;
        background: $proof-panel-background;
        overflow-y: auto;
    }
    SettingsHomeScreen .settings-category-detail Button {
        width: auto;
        min-width: 0;
        margin-top: 1;
    }
    SettingsHomeScreen .settings-category-state {
        margin-top: 1;
        color: $proof-muted;
    }
    SettingsHomeScreen #settings-machine-summary {
        height: 7;
        max-height: 7;
        margin-top: 1;
    }
    .compact SettingsHomeScreen #settings-home-layout,
    .compact-short SettingsHomeScreen #settings-home-layout {
        layout: vertical;
    }
    .compact SettingsHomeScreen #settings-category-nav,
    .compact-short SettingsHomeScreen #settings-category-nav {
        width: 100%;
        height: 7;
        min-height: 7;
        margin-right: 0;
        margin-bottom: 1;
    }
    .compact SettingsHomeScreen #settings-category-pages,
    .compact-short SettingsHomeScreen #settings-category-pages {
        width: 100%;
        height: 1fr;
    }
    """

    def __init__(
        self,
        snapshot: MachineSettingsSnapshot | None = None,
        *,
        project: Path | None = None,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.project = project

    def compose(self) -> ComposeResult:
        yield Header()
        with ResponsivePage(id="page", classes="settings-home"):
            with PageHeader():
                yield CopyableText("Settings", classes="title")
                yield CopyableText(
                    f"Project context · {self.project}"
                    if self.project is not None
                    else "Machine settings · no project selected",
                    classes="muted",
                )
            with PageWorkspace(id="settings-home-workspace"):
                with Horizontal(id="settings-home-layout"):
                    yield OptionList(
                        Option("Verification AI", id="verification-ai"),
                        Option("Runtime & resources", id="runtime-resources"),
                        Option("Advanced / compatibility", id="advanced-compatibility"),
                        id="settings-category-nav",
                    )
                    with ContentSwitcher(
                        initial="settings-ai-category",
                        id="settings-category-pages",
                    ):
                        with Vertical(
                            id="settings-ai-category",
                            classes="settings-category-detail",
                        ):
                            yield CopyableText("Verification AI", classes="section")
                            yield CopyableText(
                                "Choose a provider, connect its account, and assign "
                                "a model and reasoning level to each of the eight "
                                "verification roles.",
                                classes="muted",
                            )
                            yield CopyableText(
                                "Role policies may use machine defaults or this "
                                "project's overrides; credentials remain machine-owned.",
                                classes="settings-category-state",
                            )
                            yield Button(
                                "Open verification AI",
                                id="open-ai-provider-settings",
                                variant="primary",
                                disabled=not callable(
                                    getattr(
                                        self.proof_app.service,
                                        "get_ai_setup",
                                        None,
                                    )
                                ),
                            )
                        with Vertical(
                            id="settings-runtime-category",
                            classes="settings-category-detail",
                        ):
                            yield CopyableText(
                                "Runtime & resources",
                                classes="section",
                            )
                            yield CopyableText(
                                "Tune admission limits, resource policy, telemetry, "
                                "and calibration for this machine.",
                                classes="muted",
                            )
                            yield CopyableText(
                                self._runtime_state_text(),
                                id="settings-runtime-state",
                                classes="settings-category-state",
                            )
                            yield Button(
                                "Open runtime & resources",
                                id="open-concurrency-settings",
                                variant="primary",
                                disabled=self.snapshot is None,
                            )
                        with Vertical(
                            id="settings-advanced-category",
                            classes="settings-category-detail",
                        ):
                            yield CopyableText(
                                "Advanced / compatibility",
                                classes="section",
                            )
                            yield CopyableText(
                                "Review older coupled controls kept for compatibility "
                                "and inspect the machine settings source.",
                                classes="muted",
                            )
                            yield Button(
                                "Open advanced compatibility",
                                id="open-legacy-settings",
                                variant="primary",
                                disabled=self.snapshot is None,
                            )
                            yield TextArea(
                                (
                                    _machine_summary(self.snapshot)
                                    if self.snapshot is not None
                                    else "Loading machine settings…"
                                ),
                                read_only=True,
                                soft_wrap=False,
                                id="settings-machine-summary",
                            )
            with ActionBar():
                yield Button("Back", id="settings-back")
                yield CopyableText(
                    "Loading settings through the backend…"
                    if self.snapshot is None
                    else "Choose a settings category.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def on_mount(self) -> None:
        self.call_after_refresh(self._initialize_category_navigation)
        if self.snapshot is None:
            self.refresh_snapshot()

    def _initialize_category_navigation(self) -> None:
        navigation = self.query("#settings-category-nav").nodes
        if navigation and isinstance(navigation[0], OptionList):
            navigation[0].highlighted = 0
        elif self.is_mounted:
            self.set_timer(0.01, self._initialize_category_navigation)

    def _runtime_state_text(self) -> str:
        if self.snapshot is None:
            return "Loading machine policy and effective resource limits…"
        configured = self.snapshot.configured
        effective = self.snapshot.effective
        return (
            f"{configured.mode.title()} policy · {configured.resource_profile} profile\n"
            f"Effective now: {effective.ai_limit} AI · "
            f"{effective.lean_pool} Lean · {effective.build_limit} builds"
        )

    def _show_category(self, category: str) -> None:
        page_ids = {
            "verification-ai": "settings-ai-category",
            "runtime-resources": "settings-runtime-category",
            "advanced-compatibility": "settings-advanced-category",
        }
        page_id = page_ids.get(category)
        if page_id is None:
            return
        self.query_one("#settings-category-pages", ContentSwitcher).current = page_id

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "settings-category-nav":
            option_id = event.option.id
            if option_id is not None:
                self._show_category(option_id)

    def action_back(self) -> None:
        self.proof_app.close_settings()

    def action_refresh(self) -> None:
        self.refresh_snapshot()

    def refresh_snapshot(self) -> None:
        def load() -> None:
            try:
                snapshot = self.proof_app.service.get_machine_settings(
                    project=self.project
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self.show_notice,
                    f"Could not load machine settings: {exc}",
                    error=True,
                )
                return
            self.proof_app.call_from_thread(self._record_snapshot, snapshot)

        self.run_worker(load, thread=True, exclusive=True, group="machine-settings")

    def _record_snapshot(self, snapshot: MachineSettingsSnapshot) -> None:
        if not self.is_mounted or not self.query("#settings-machine-summary").nodes:
            self.set_timer(0.01, lambda: self._record_snapshot(snapshot))
            return
        self.snapshot = snapshot
        self.query_one("#settings-machine-summary", TextArea).text = _machine_summary(
            snapshot
        )
        self.query_one(
            "#settings-runtime-state", CopyableText
        ).text = self._runtime_state_text()
        self.query_one("#open-concurrency-settings", Button).disabled = False
        self.query_one("#open-legacy-settings", Button).disabled = False
        self.show_notice("Machine settings loaded through the backend.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-back":
            self.action_back()
        elif event.button.id == "open-concurrency-settings":
            if self.snapshot is not None:
                self.proof_app.show_concurrency_settings(
                    self.snapshot,
                    project=self.project,
                )
        elif event.button.id == "open-ai-provider-settings":
            self.proof_app.show_ai_provider_settings(project=self.project)
        elif event.button.id == "open-legacy-settings":
            if self.snapshot is not None:
                self.proof_app.show_legacy_settings(
                    self.snapshot,
                    project=self.project,
                )


class _SettingsEditorScreen(NoticeScreen):
    """Shared preview/apply flow for machine-scoped editors."""

    snapshot: MachineSettingsSnapshot

    def __init__(self) -> None:
        super().__init__()
        self._pending_navigation: Callable[[], None] | None = None

    def _draft_is_dirty(self) -> bool:
        raise NotImplementedError

    def _editor_label(self) -> str:
        raise NotImplementedError

    def action_save(self) -> None:
        raise NotImplementedError

    def _draft_signature(self) -> tuple[object, ...]:
        raise NotImplementedError

    def _restore_draft_signature(self, signature: tuple[object, ...]) -> None:
        raise NotImplementedError

    def _request_navigation(self, destination: Callable[[], None]) -> None:
        if not self._draft_is_dirty():
            destination()
            return
        dialog = UnsavedSettingsConfirmationScreen(self._editor_label())

        def after_choice(choice: str | None) -> None:
            if choice == "discard":
                destination()
            elif choice == "save":
                self._pending_navigation = destination
                self.action_save()

        self.proof_app.push_screen(dialog, callback=after_choice)

    def request_main_menu(self) -> None:
        self._request_navigation(self.proof_app.finish_main_menu_navigation)

    def request_quit(self) -> None:
        self._request_navigation(self.proof_app.exit)

    def request_settings_home(self) -> None:
        project = getattr(self, "project", None)
        self._request_navigation(
            lambda: self.proof_app.show_settings(self.snapshot, project=project)
        )

    def _cancel_pending_navigation(self) -> None:
        self._pending_navigation = None

    def _finish_pending_navigation(self) -> None:
        destination = self._pending_navigation
        self._pending_navigation = None
        if destination is not None:
            destination()

    def _preview(self, request: MachineSettingsUpdateRequest) -> None:
        submitted_signature = self._draft_signature()
        self.show_notice("Validating the revision and previewing machine-wide changes…")

        def preview() -> None:
            try:
                result = self.proof_app.service.preview_machine_settings(request)
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._preview_failed,
                    f"Settings preview failed: {exc}",
                )
                return
            self.proof_app.call_from_thread(
                self._review_preview, result, submitted_signature
            )

        self.run_worker(preview, thread=True, exclusive=True, group="machine-settings")

    def _preview_failed(self, message: str) -> None:
        self._cancel_pending_navigation()
        self.show_notice(message, error=True)

    def _review_preview(
        self,
        preview: SettingsChangePreview,
        submitted_signature: tuple[object, ...],
    ) -> None:
        if not preview.warnings:
            self._apply_preview(preview, (), submitted_signature)
            return
        dialog = SettingsWarningConfirmationScreen(preview)

        def after_confirmation(accepted: bool | None) -> None:
            if accepted:
                self._apply_preview(
                    preview,
                    tuple(warning.warning_id for warning in preview.warnings),
                    submitted_signature,
                )
            else:
                self._cancel_pending_navigation()
                self.show_notice("Settings change canceled; nothing was written.")

        self.proof_app.push_screen(dialog, callback=after_confirmation)

    def _apply_preview(
        self,
        preview: SettingsChangePreview,
        warning_ids: tuple[str, ...],
        submitted_signature: tuple[object, ...],
    ) -> None:
        self.show_notice("Persisting the machine-wide settings revision…")

        def apply() -> None:
            try:
                snapshot = self.proof_app.service.apply_machine_settings(
                    preview.preview_token,
                    warning_ids,
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._apply_failed,
                    f"Settings were not applied: {exc}",
                )
                return
            self.proof_app.call_from_thread(
                self._settings_applied,
                snapshot,
                preview,
                submitted_signature,
            )

        self.run_worker(apply, thread=True, exclusive=True, group="machine-settings")

    def _apply_failed(self, message: str) -> None:
        self._cancel_pending_navigation()
        self.show_notice(message, error=True)

    def _settings_applied(
        self,
        snapshot: MachineSettingsSnapshot,
        preview: SettingsChangePreview,
        submitted_signature: tuple[object, ...],
    ) -> None:
        raise NotImplementedError


class ConcurrencyResourcesScreen(_SettingsEditorScreen):
    """Editable policy plus live backend resource/controller observations."""

    BINDINGS = [BACK.binding(), SAVE.binding()]

    def __init__(
        self,
        snapshot: MachineSettingsSnapshot,
        *,
        project: Path | None = None,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.project = project
        self._refresh_in_progress = False
        self._runtime_initialized = False

    def compose(self) -> ComposeResult:
        configured = self.snapshot.configured
        yield Header()
        with ResponsivePage(id="page", classes="runtime-settings-page"):
            with PageHeader():
                yield CopyableText("Concurrency / Resources", classes="title")
                yield CopyableText(
                    "Machine-wide AI, Lean, and build admission policy."
                    + (
                        f" Project calibration: {self.project}"
                        if self.project is not None
                        else " Open from a project to calibrate its Lean workload."
                    ),
                    classes="muted",
                )
            yield self._runtime_workspace(configured)
            with ActionBar(id="runtime-settings-actions"):
                yield Button(
                    "Preview and save", id="save-concurrency", variant="success"
                )
                yield Button(
                    "Reset all to Auto", id="reset-concurrency", variant="warning"
                )
                yield Button("Settings", id="concurrency-back")
                yield CopyableText(
                    "Live status is sampled through the backend. Reducing a limit "
                    "stops new admissions; running work finishes safely.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def _runtime_workspace(self, configured: ConcurrencySettingsView) -> PageWorkspace:
        """Build peer policy, status, and calibration destinations."""

        return PageWorkspace(
            OptionList(
                Option("1  Machine policy", id="policy"),
                Option("2  Live overview", id="overview"),
                Option("3  Calibration and benchmarks", id="calibration"),
                id="runtime-settings-nav",
            ),
            ContentSwitcher(
                VerticalScroll(
                    CopyableText(
                        "Machine policy",
                        classes="section",
                    ),
                    CopyableText(
                        "Auto values are resolved independently by the AI, Lean, "
                        "and build admission controllers."
                    ),
                    Label("Concurrency mode"),
                    Select(
                        (
                            ("Auto / Adaptive", "adaptive"),
                            ("Fixed / Manual", "fixed"),
                        ),
                        value=configured.mode,
                        allow_blank=False,
                        id="concurrency-mode",
                    ),
                    Label("Resource profile"),
                    Select(
                        (
                            ("Auto", "auto"),
                            ("Interactive", "interactive"),
                            ("Server", "server"),
                        ),
                        value=configured.resource_profile,
                        allow_blank=False,
                        id="resource-profile",
                    ),
                    Label("Codex plan"),
                    Select(
                        (
                            ("Custom / Unknown", "unknown"),
                            ("Plus", "plus"),
                            ("Pro 5x", "pro_5x"),
                            ("Pro 20x", "pro_20x"),
                        ),
                        value=configured.codex_plan,
                        allow_blank=False,
                        id="codex-plan",
                    ),
                    Label("AI budget policy"),
                    Select(
                        (
                            ("Economy", "economy"),
                            ("Balanced", "balanced"),
                            ("Throughput", "throughput"),
                        ),
                        value=configured.budget_policy,
                        allow_blank=False,
                        id="budget-policy",
                    ),
                    Label("AI concurrency (Auto or integer)"),
                    Input(value=_auto(configured.ai_initial), id="ai-concurrency"),
                    Label("AI hard ceiling (Auto or integer)"),
                    Input(value=_auto(configured.ai_hard_max), id="ai-hard-max"),
                    Label("AI successful queued turns before growth (Auto or integer)"),
                    Input(
                        value=_auto(configured.ai_increase_after_successes),
                        id="ai-growth-successes",
                    ),
                    Label("Lean REPL pool (Auto or integer)"),
                    Input(value=_auto(configured.lean_pool), id="lean-pool"),
                    Label("Lean pool maximum (Auto or integer)"),
                    Input(value=_auto(configured.lean_max), id="lean-max"),
                    Label("Maximum concurrent builds (Auto or integer)"),
                    Input(value=_auto(configured.max_builds), id="max-builds"),
                    Label("Maximum agents per target"),
                    Input(
                        value=str(configured.agents_per_target_max),
                        id="agents-per-target",
                    ),
                    Checkbox(
                        "Duplicate-agent escalation",
                        value=configured.duplicate_agent_escalation,
                        id="duplicate-escalation",
                    ),
                    Checkbox(
                        "Lean REPL memory calibration",
                        value=configured.lean_memory_calibration,
                        id="lean-calibration",
                    ),
                    Checkbox(
                        "Dependency-priority scheduling",
                        value=configured.dependency_priority,
                        id="dependency-priority",
                    ),
                    Checkbox(
                        "Adaptive controller",
                        value=configured.adaptive_controller,
                        id="adaptive-controller",
                    ),
                    Checkbox(
                        "Hardware telemetry",
                        value=configured.hardware_telemetry,
                        id="hardware-telemetry",
                    ),
                    id="runtime-policy-page",
                    classes="settings-page",
                ),
                VerticalScroll(
                    CopyableText("Live overview", classes="section"),
                    TextArea(
                        _machine_summary(self.snapshot),
                        read_only=True,
                        soft_wrap=False,
                        id="settings-machine-summary",
                    ),
                    TextArea(
                        _concurrency_summary(self.snapshot),
                        read_only=True,
                        soft_wrap=False,
                        id="concurrency-summary",
                    ),
                    CopyableText("Live resources", classes="section"),
                    TextArea(
                        _telemetry_text(self.snapshot),
                        read_only=True,
                        soft_wrap=False,
                        id="resource-telemetry",
                    ),
                    TextArea(
                        _resolution_text(self.snapshot),
                        read_only=True,
                        soft_wrap=False,
                        id="settings-resolution",
                    ),
                    id="runtime-overview-page",
                    classes="settings-page",
                ),
                VerticalScroll(
                    CopyableText("Calibration and benchmarks", classes="section"),
                    CopyableText(
                        "Benchmarks report recommendations without changing policy. "
                        "Lean calibration is available when Settings was opened "
                        "from a project."
                    ),
                    Vertical(
                        Button("Benchmark Codex (no traffic)", id="benchmark-codex"),
                        Button(
                            "Benchmark Lean",
                            id="benchmark-lean",
                            disabled=self.project is None,
                        ),
                        Button("Benchmark builds", id="benchmark-build"),
                        Button(
                            "Reset project Lean calibration",
                            id="reset-lean-calibration",
                            disabled=self.project is None,
                            variant="warning",
                        ),
                        Button(
                            "Reset adaptive history",
                            id="reset-adaptive-history",
                            variant="warning",
                        ),
                        classes="settings-benchmark-toolbar",
                    ),
                    TextArea(
                        "No benchmark run in this client.",
                        read_only=True,
                        soft_wrap=True,
                        id="benchmark-result",
                    ),
                    id="runtime-calibration-page",
                    classes="settings-page",
                ),
                initial="runtime-policy-page",
                id="runtime-settings-pages",
            ),
            id="runtime-settings-workspace",
            classes="runtime-settings-workspace",
        )

    def on_mount(self) -> None:
        self.set_timer(0.01, self._initialize_runtime_screen)

    def _initialize_runtime_screen(self) -> None:
        if not self.query("#runtime-settings-workspace").nodes:
            if self.is_mounted:
                self.set_timer(0.01, self._initialize_runtime_screen)
            return
        if self._runtime_initialized:
            return
        self._runtime_initialized = True
        self._apply_runtime_geometry()
        self.query_one("#runtime-settings-nav", OptionList).highlighted = 0
        self.set_interval(2.0, self.refresh_status)

    def _apply_runtime_geometry(self) -> None:
        """Give one scroll owner to each focused runtime destination."""

        workspace = self.query_one("#runtime-settings-workspace", PageWorkspace)
        workspace.styles.overflow_y = "hidden"
        navigation = self.query_one("#runtime-settings-nav", OptionList)
        navigation.styles.height = 5
        self.query_one("#runtime-settings-pages", ContentSwitcher).styles.height = "1fr"

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "runtime-settings-nav" or event.option.id is None:
            return
        pages = self.query_one("#runtime-settings-pages", ContentSwitcher)
        pages.current = f"runtime-{event.option.id}-page"

    def action_back(self) -> None:
        self.request_settings_home()

    def action_save(self) -> None:
        self._save()

    def _editor_label(self) -> str:
        return "The concurrency and resources policy"

    def _draft_is_dirty(self) -> bool:
        try:
            return self._configured_from_form() != self.snapshot.configured
        except ValueError:
            return True

    def _draft_signature(self) -> tuple[object, ...]:
        return (
            *(
                str(self.query_one(f"#{widget_id}", Select).value)
                for widget_id in (
                    "concurrency-mode",
                    "resource-profile",
                    "codex-plan",
                    "budget-policy",
                )
            ),
            *(
                self.query_one(f"#{widget_id}", Input).value
                for widget_id in (
                    "ai-concurrency",
                    "ai-hard-max",
                    "ai-growth-successes",
                    "lean-pool",
                    "lean-max",
                    "max-builds",
                    "agents-per-target",
                )
            ),
            *(
                self.query_one(f"#{widget_id}", Checkbox).value
                for widget_id in (
                    "duplicate-escalation",
                    "lean-calibration",
                    "dependency-priority",
                    "adaptive-controller",
                    "hardware-telemetry",
                )
            ),
        )

    def _restore_draft_signature(self, signature: tuple[object, ...]) -> None:
        select_ids = (
            "concurrency-mode",
            "resource-profile",
            "codex-plan",
            "budget-policy",
        )
        input_ids = (
            "ai-concurrency",
            "ai-hard-max",
            "ai-growth-successes",
            "lean-pool",
            "lean-max",
            "max-builds",
            "agents-per-target",
        )
        checkbox_ids = (
            "duplicate-escalation",
            "lean-calibration",
            "dependency-priority",
            "adaptive-controller",
            "hardware-telemetry",
        )
        select_values = signature[: len(select_ids)]
        input_start = len(select_ids)
        checkbox_start = input_start + len(input_ids)
        with self.prevent(Select.Changed, Checkbox.Changed):
            for widget_id, value in zip(select_ids, select_values, strict=True):
                self.query_one(f"#{widget_id}", Select).value = str(value)
            for widget_id, value in zip(
                input_ids,
                signature[input_start:checkbox_start],
                strict=True,
            ):
                self.query_one(f"#{widget_id}", Input).value = str(value)
            for widget_id, value in zip(
                checkbox_ids,
                signature[checkbox_start:],
                strict=True,
            ):
                self.query_one(f"#{widget_id}", Checkbox).value = bool(value)

    def _configured_from_form(self) -> ConcurrencySettingsView:
        configured = self.snapshot.configured
        mode = _select_value(self.query_one("#concurrency-mode", Select))
        return replace(
            configured,
            mode=mode,
            resource_profile=_select_value(self.query_one("#resource-profile", Select)),
            codex_plan=_select_value(self.query_one("#codex-plan", Select)),
            budget_policy=_select_value(self.query_one("#budget-policy", Select)),
            ai_initial=_optional_positive(
                self.query_one("#ai-concurrency", Input).value,
                "AI concurrency",
            ),
            ai_hard_max=_optional_positive(
                self.query_one("#ai-hard-max", Input).value,
                "AI hard ceiling",
            ),
            ai_increase_after_successes=_optional_positive(
                self.query_one("#ai-growth-successes", Input).value,
                "AI successful queued turns before growth",
            ),
            lean_pool=_optional_positive(
                self.query_one("#lean-pool", Input).value,
                "Lean REPL pool",
            ),
            lean_max=_optional_positive(
                self.query_one("#lean-max", Input).value,
                "Lean pool maximum",
            ),
            max_builds=_optional_positive(
                self.query_one("#max-builds", Input).value,
                "Maximum concurrent builds",
            ),
            agents_per_target_max=_positive(
                self.query_one("#agents-per-target", Input).value,
                "Maximum agents per target",
            ),
            duplicate_agent_escalation=self.query_one(
                "#duplicate-escalation", Checkbox
            ).value,
            lean_memory_calibration=self.query_one("#lean-calibration", Checkbox).value,
            dependency_priority=self.query_one("#dependency-priority", Checkbox).value,
            adaptive_controller=mode == "adaptive",
            hardware_telemetry=self.query_one("#hardware-telemetry", Checkbox).value,
        )

    def _save(self) -> None:
        try:
            configured = self._configured_from_form()
        except ValueError as exc:
            self._cancel_pending_navigation()
            self.show_notice(str(exc), error=True)
            return
        self._preview(
            MachineSettingsUpdateRequest(
                expected_revision=self.snapshot.revision,
                configured=configured,
                legacy=self.snapshot.legacy,
            )
        )

    def _reset(self) -> None:
        self.show_notice("Resetting the machine policy to automatic defaults…")

        def reset() -> None:
            try:
                snapshot = self.proof_app.service.reset_machine_settings(
                    self.snapshot.revision
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self.show_notice,
                    f"Settings were not reset: {exc}",
                    error=True,
                )
                return
            self.proof_app.call_from_thread(self._replace_snapshot_and_form, snapshot)
            self.proof_app.call_from_thread(
                self.show_notice,
                "Machine policy reset to Auto. Effective values were recalculated.",
            )

        self.run_worker(reset, thread=True, exclusive=True, group="machine-settings")

    def _review_machine_reset(self) -> None:
        dialog = DestructiveSettingsConfirmationScreen(
            "Reset the machine policy to Auto?",
            "This replaces the complete machine-wide AI, Lean, and build admission "
            f"policy at revision {self.snapshot.revision}. Effective values will be "
            "recalculated by the backend.",
            "Reset all to Auto",
        )
        self._push_destructive_review(dialog, self._reset, "Machine reset canceled.")

    def refresh_status(self) -> None:
        if self._refresh_in_progress:
            return
        self._refresh_in_progress = True

        def refresh() -> None:
            try:
                snapshot = self.proof_app.service.get_machine_settings(
                    project=self.project
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self.show_notice,
                    f"Live resource refresh failed: {exc}",
                    error=True,
                )
            else:
                self.proof_app.call_from_thread(self._record_live_snapshot, snapshot)
            finally:
                self.proof_app.call_from_thread(self._finish_refresh)

        self.run_worker(
            refresh, thread=True, exclusive=True, group="settings-telemetry"
        )

    def _finish_refresh(self) -> None:
        self._refresh_in_progress = False

    def _record_live_snapshot(self, snapshot: MachineSettingsSnapshot) -> None:
        revision_changed = snapshot.revision != self.snapshot.revision
        self._render_snapshot(snapshot)
        if revision_changed:
            self.show_notice(
                "Machine settings changed in another client. This form keeps its "
                "original revision; return to Settings to reload before saving.",
                error=True,
            )
            return
        self.snapshot = snapshot

    def _render_snapshot(self, snapshot: MachineSettingsSnapshot) -> None:
        self.query_one("#settings-machine-summary", TextArea).text = _machine_summary(
            snapshot
        )
        self.query_one("#concurrency-summary", TextArea).text = _concurrency_summary(
            snapshot
        )
        self.query_one("#resource-telemetry", TextArea).text = _telemetry_text(snapshot)
        self.query_one("#settings-resolution", TextArea).text = _resolution_text(
            snapshot
        )

    def _replace_snapshot_and_form(self, snapshot: MachineSettingsSnapshot) -> None:
        self.snapshot = snapshot
        self._render_snapshot(snapshot)
        configured = snapshot.configured
        self.query_one("#concurrency-mode", Select).value = configured.mode
        self.query_one("#resource-profile", Select).value = configured.resource_profile
        self.query_one("#codex-plan", Select).value = configured.codex_plan
        self.query_one("#budget-policy", Select).value = configured.budget_policy
        self.query_one("#ai-concurrency", Input).value = _auto(configured.ai_initial)
        self.query_one("#ai-hard-max", Input).value = _auto(configured.ai_hard_max)
        self.query_one("#ai-growth-successes", Input).value = _auto(
            configured.ai_increase_after_successes
        )
        self.query_one("#lean-pool", Input).value = _auto(configured.lean_pool)
        self.query_one("#lean-max", Input).value = _auto(configured.lean_max)
        self.query_one("#max-builds", Input).value = _auto(configured.max_builds)
        self.query_one("#agents-per-target", Input).value = str(
            configured.agents_per_target_max
        )
        self.query_one(
            "#duplicate-escalation", Checkbox
        ).value = configured.duplicate_agent_escalation
        self.query_one(
            "#lean-calibration", Checkbox
        ).value = configured.lean_memory_calibration
        self.query_one(
            "#dependency-priority", Checkbox
        ).value = configured.dependency_priority
        self.query_one(
            "#adaptive-controller", Checkbox
        ).value = configured.adaptive_controller
        self.query_one(
            "#hardware-telemetry", Checkbox
        ).value = configured.hardware_telemetry

    def _settings_applied(
        self,
        snapshot: MachineSettingsSnapshot,
        preview: SettingsChangePreview,
        submitted_signature: tuple[object, ...],
    ) -> None:
        live_signature = self._draft_signature()
        self._replace_snapshot_and_form(snapshot)
        if live_signature != submitted_signature:
            self._restore_draft_signature(live_signature)
            self._cancel_pending_navigation()
            self.show_notice(
                f"Machine settings revision {snapshot.revision} saved; newer edits "
                "remain unsaved in this editor."
            )
            return
        live = ", ".join(preview.live_fields) or "none"
        next_run = ", ".join(preview.next_run_fields) or "none"
        self.show_notice(
            f"Machine settings revision {snapshot.revision} saved. "
            f"Applied live: {live}. Takes effect next run: {next_run}."
        )
        self._finish_pending_navigation()

    def _benchmark(self, kind: BenchmarkKind) -> None:
        result_widget = self.query_one("#benchmark-result", TextArea)
        result_widget.text = (
            f"Running {kind.value} through the backend. Codex traffic is disabled "
            "unless a separate explicit opt-in is introduced."
        )

        def run() -> None:
            try:
                result = self.proof_app.service.run_concurrency_benchmark(
                    kind,
                    project=(self.project if kind == BenchmarkKind.LEAN else None),
                    allow_codex_traffic=False,
                )
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._record_benchmark_error,
                    kind,
                    str(exc),
                )
                return
            self.proof_app.call_from_thread(self._record_benchmark, result)

        self.run_worker(run, thread=True, exclusive=True, group="settings-benchmark")

    def _record_benchmark(self, result: BenchmarkResult) -> None:
        self.query_one("#benchmark-result", TextArea).text = (
            f"Benchmark: {result.kind.value}\n"
            f"Tested: {', '.join(str(value) for value in result.tested_values)}\n"
            f"Recommended value: {result.recommendation}\n"
            f"Codex traffic used: {'yes' if result.used_codex_traffic else 'no'}\n"
            f"Calibration record: {result.calibration_path}\n"
            f"Detail: {result.detail}"
        )
        self.show_notice(
            "Benchmark complete. The recommendation is displayed; settings were "
            "not changed automatically."
        )
        self.refresh_status()

    def _record_benchmark_error(self, kind: BenchmarkKind, detail: str) -> None:
        self.query_one(
            "#benchmark-result", TextArea
        ).text = f"Benchmark {kind.value} failed: {detail}"
        self.show_notice(f"Benchmark failed: {detail}", error=True)

    def _reset_lean_calibration(self) -> None:
        project = self.project
        if project is None:
            self.show_notice(
                "Open Settings from a project dashboard to reset its Lean profile.",
                error=True,
            )
            return
        self.query_one(
            "#benchmark-result", TextArea
        ).text = f"Resetting the exact Lean calibration for {project}…"

        def reset() -> None:
            try:
                result = self.proof_app.service.reset_project_lean_calibration(project)
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._record_reset_error,
                    "Lean calibration reset",
                    str(exc),
                )
                return
            self.proof_app.call_from_thread(
                self._record_calibration_reset,
                result.project_path,
                result.profile_id,
                result.calibration_path,
                result.removed,
            )

        self.run_worker(reset, thread=True, exclusive=True, group="settings-benchmark")

    def _review_lean_calibration_reset(self) -> None:
        project = self.project
        if project is None:
            self.show_notice(
                "Open Settings from a project dashboard to reset its Lean profile.",
                error=True,
            )
            return
        dialog = DestructiveSettingsConfirmationScreen(
            "Reset this project's Lean calibration?",
            f"Project: {project}\n\nOnly the exact backend-owned calibration record "
            "for this project and machine profile will be removed. The project "
            "sources are not changed.",
            "Reset project calibration",
        )
        self._push_destructive_review(
            dialog,
            self._reset_lean_calibration,
            "Lean calibration reset canceled.",
        )

    def _record_calibration_reset(
        self,
        project: Path,
        profile_id: str,
        calibration_path: Path,
        removed: bool,
    ) -> None:
        self.query_one("#benchmark-result", TextArea).text = (
            "Lean calibration reset\n"
            f"Project: {project}\n"
            f"Profile: {profile_id}\n"
            f"Record: {calibration_path}\n"
            f"Removed: {'yes' if removed else 'no matching record existed'}"
        )
        self.show_notice(
            "Project Lean calibration reset. Auto tuning will use the conservative "
            "machine fallback until this project is calibrated again."
        )
        self.refresh_status()

    def _reset_adaptive_history(self) -> None:
        self.query_one(
            "#benchmark-result", TextArea
        ).text = "Resetting machine-wide adaptive controller history…"

        def reset() -> None:
            try:
                result = self.proof_app.service.reset_adaptive_history()
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self._record_reset_error,
                    "Adaptive history reset",
                    str(exc),
                )
                return
            self.proof_app.call_from_thread(
                self._record_adaptive_reset,
                result.reset_at,
                result.ai_limit,
                result.lean_pool,
                result.build_limit,
                result.in_flight_work_preserved,
            )

        self.run_worker(reset, thread=True, exclusive=True, group="settings-benchmark")

    def _review_adaptive_history_reset(self) -> None:
        dialog = DestructiveSettingsConfirmationScreen(
            "Reset machine-wide adaptive history?",
            "This removes learned controller history for AI, Lean, and build "
            "admission across projects. Currently admitted work is preserved.",
            "Reset adaptive history",
        )
        self._push_destructive_review(
            dialog,
            self._reset_adaptive_history,
            "Adaptive history reset canceled.",
        )

    def _push_destructive_review(
        self,
        dialog: DestructiveSettingsConfirmationScreen,
        operation: Callable[[], None],
        canceled_notice: str,
    ) -> None:
        def after_confirmation(accepted: bool | None) -> None:
            if accepted:
                operation()
            else:
                self.show_notice(canceled_notice)

        self.proof_app.push_screen(dialog, callback=after_confirmation)

    def _record_adaptive_reset(
        self,
        reset_at: str,
        ai_limit: int,
        lean_pool: int,
        build_limit: int,
        in_flight_work_preserved: bool,
    ) -> None:
        self.query_one("#benchmark-result", TextArea).text = (
            "Adaptive history reset\n"
            f"Reset at: {reset_at}\n"
            f"Effective AI / Lean / build: {ai_limit} / {lean_pool} / {build_limit}\n"
            "In-flight work preserved: "
            f"{'yes' if in_flight_work_preserved else 'no'}"
        )
        self.show_notice(
            "Adaptive history reset. Existing admitted work was not cancelled."
        )
        self.refresh_status()

    def _record_reset_error(self, operation: str, detail: str) -> None:
        self.query_one(
            "#benchmark-result", TextArea
        ).text = f"{operation} failed: {detail}"
        self.show_notice(f"{operation} failed: {detail}", error=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "save-concurrency":
            self.action_save()
        elif button_id == "reset-concurrency":
            self._review_machine_reset()
        elif button_id == "concurrency-back":
            self.action_back()
        elif button_id == "benchmark-codex":
            self._benchmark(BenchmarkKind.CODEX)
        elif button_id == "benchmark-lean":
            self._benchmark(BenchmarkKind.LEAN)
        elif button_id == "benchmark-build":
            self._benchmark(BenchmarkKind.BUILD)
        elif button_id == "reset-lean-calibration":
            self._review_lean_calibration_reset()
        elif button_id == "reset-adaptive-history":
            self._review_adaptive_history_reset()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "concurrency-mode":
            return
        nodes = self.query("#adaptive-controller").nodes
        if nodes and isinstance(nodes[0], Checkbox):
            nodes[0].value = event.value == "adaptive"

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id != "adaptive-controller":
            return
        nodes = self.query("#concurrency-mode").nodes
        if nodes and isinstance(nodes[0], Select):
            nodes[0].value = "adaptive" if event.value else "fixed"


class LegacySettingsScreen(_SettingsEditorScreen):
    """Visible home for old coupled controls during the migration period."""

    BINDINGS = [BACK.binding(), SAVE.binding()]

    def __init__(
        self,
        snapshot: MachineSettingsSnapshot,
        *,
        project: Path | None = None,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.project = project

    def compose(self) -> ComposeResult:
        legacy = self.snapshot.legacy
        yield Header()
        with ResponsivePage(id="page"):
            with PageHeader():
                yield CopyableText("Advanced / compatibility", classes="title")
                yield CopyableText(
                    "Older machine-wide controls retained for migration.",
                    classes="muted",
                )
            with PageWorkspace():
                yield TextArea(
                    _machine_summary(self.snapshot),
                    read_only=True,
                    soft_wrap=False,
                    id="settings-machine-summary",
                )
                yield TextArea(
                    _legacy_text(self.snapshot),
                    read_only=True,
                    soft_wrap=False,
                    id="legacy-settings-summary",
                )
                yield Label("Proof batch worker processes (jobs)")
                yield Input(value=str(legacy.proof_jobs), id="legacy-proof-jobs")
                yield Label("Claims per proof batch")
                yield Input(value=str(legacy.batch_size), id="legacy-batch-size")
                yield Label("Lean REPLs per batch worker")
                yield Input(
                    value=str(legacy.per_worker_lean_pool),
                    id="legacy-lean-pool",
                )
            with ActionBar():
                yield Button("Preview and save", id="save-legacy", variant="success")
                yield Button("Settings", id="legacy-back")
                yield CopyableText(
                    "The backend validates active, superseded, and next-run values.",
                    id="status-line",
                    classes="muted",
                )
        yield CommandFooter()

    def action_back(self) -> None:
        self.request_settings_home()

    def action_save(self) -> None:
        self._save()

    def _editor_label(self) -> str:
        return "The advanced compatibility policy"

    def _draft_is_dirty(self) -> bool:
        try:
            legacy = replace(
                self.snapshot.legacy,
                proof_jobs=_positive(
                    self.query_one("#legacy-proof-jobs", Input).value,
                    "Proof batch workers",
                ),
                batch_size=_positive(
                    self.query_one("#legacy-batch-size", Input).value,
                    "Claims per batch",
                ),
                per_worker_lean_pool=_positive(
                    self.query_one("#legacy-lean-pool", Input).value,
                    "Lean REPLs per batch worker",
                ),
            )
        except ValueError:
            return True
        return legacy != self.snapshot.legacy

    def _draft_signature(self) -> tuple[object, ...]:
        return tuple(
            self.query_one(f"#{widget_id}", Input).value
            for widget_id in (
                "legacy-proof-jobs",
                "legacy-batch-size",
                "legacy-lean-pool",
            )
        )

    def _restore_draft_signature(self, signature: tuple[object, ...]) -> None:
        for widget_id, value in zip(
            ("legacy-proof-jobs", "legacy-batch-size", "legacy-lean-pool"),
            signature,
            strict=True,
        ):
            self.query_one(f"#{widget_id}", Input).value = str(value)

    def _save(self) -> None:
        try:
            legacy = replace(
                self.snapshot.legacy,
                proof_jobs=_positive(
                    self.query_one("#legacy-proof-jobs", Input).value,
                    "Proof batch workers",
                ),
                batch_size=_positive(
                    self.query_one("#legacy-batch-size", Input).value,
                    "Claims per batch",
                ),
                per_worker_lean_pool=_positive(
                    self.query_one("#legacy-lean-pool", Input).value,
                    "Lean REPLs per batch worker",
                ),
            )
        except ValueError as exc:
            self._cancel_pending_navigation()
            self.show_notice(str(exc), error=True)
            return
        self._preview(
            MachineSettingsUpdateRequest(
                expected_revision=self.snapshot.revision,
                configured=self.snapshot.configured,
                legacy=legacy,
            )
        )

    def _settings_applied(
        self,
        snapshot: MachineSettingsSnapshot,
        preview: SettingsChangePreview,
        submitted_signature: tuple[object, ...],
    ) -> None:
        live_signature = self._draft_signature()
        self.snapshot = snapshot
        legacy = snapshot.legacy
        self.query_one("#settings-machine-summary", TextArea).text = _machine_summary(
            snapshot
        )
        self.query_one("#legacy-settings-summary", TextArea).text = _legacy_text(
            snapshot
        )
        self.query_one("#legacy-proof-jobs", Input).value = str(legacy.proof_jobs)
        self.query_one("#legacy-batch-size", Input).value = str(legacy.batch_size)
        self.query_one("#legacy-lean-pool", Input).value = str(
            legacy.per_worker_lean_pool
        )
        if live_signature != submitted_signature:
            self._restore_draft_signature(live_signature)
            self._cancel_pending_navigation()
            self.show_notice(
                f"Legacy settings revision {snapshot.revision} saved; newer edits "
                "remain unsaved in this editor."
            )
            return
        next_run = ", ".join(preview.next_run_fields) or "none"
        self.show_notice(
            f"Legacy machine settings revision {snapshot.revision} saved. "
            f"Takes effect next run: {next_run}."
        )
        self._finish_pending_navigation()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-legacy":
            self.action_save()
        elif event.button.id == "legacy-back":
            self.action_back()


class SettingsWarningConfirmationScreen(ModalScreen[bool]):
    """Cancel-first confirmation for backend-classified unsafe settings."""

    BINDINGS = [CANCEL.binding()]

    def __init__(self, preview: SettingsChangePreview) -> None:
        super().__init__()
        self.preview = preview

    @property
    def proof_app(self) -> ProofAssistantApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        warnings = "\n\n".join(
            f"Warning {warning.warning_id}: {warning.message}\n"
            f"Recommended: {warning.recommended_value}"
            for warning in self.preview.warnings
        )
        live = ", ".join(self.preview.live_fields) or "none"
        next_run = ", ".join(self.preview.next_run_fields) or "none"
        with VerticalScroll(id="settings-warning-dialog"):
            yield CopyableText("Confirm potentially unsafe settings?", classes="title")
            yield TextArea(
                warnings,
                read_only=True,
                soft_wrap=True,
                id="settings-warning-detail",
            )
            yield CopyableText(
                f"Applied live: {live}\nTakes effect next run: {next_run}",
                id="settings-warning-effects",
            )
            with Horizontal(classes="toolbar"):
                yield Button("Cancel", id="settings-warning-cancel", variant="primary")
                yield Button(
                    "Keep requested setting",
                    id="settings-warning-confirm",
                    variant="warning",
                )
        yield CommandFooter()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def on_mount(self) -> None:
        self.set_timer(0.01, self._focus_cancel)

    def _focus_cancel(self) -> None:
        buttons = self.query("#settings-warning-cancel").nodes
        if not buttons:
            self.set_timer(0.01, self._focus_cancel)
            return
        buttons[0].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-warning-confirm":
            self.action_confirm()
        elif event.button.id == "settings-warning-cancel":
            self.action_cancel()
