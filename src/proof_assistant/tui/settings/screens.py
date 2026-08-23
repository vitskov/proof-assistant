"""Settings UI over the machine-scoped workflow service contract.

The widgets in this module never inspect hardware or configuration files.  They
only render immutable backend DTOs and submit revision-checked requests.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    Select,
    TextArea,
)

from proof_assistant.tui.screens import CopyableText, NoticeScreen
from proof_assistant.workflow.contracts import (
    BenchmarkKind,
    BenchmarkResult,
    ConcurrencySettingsView,
    MachineSettingsSnapshot,
    MachineSettingsUpdateRequest,
    SettingsChangePreview,
)

if TYPE_CHECKING:
    from proof_assistant.tui.app import ProofAssistantApp


def _auto(value: int | None) -> str:
    return "Auto" if value is None else str(value)


def _configured(value: int | None) -> str:
    return "Auto" if value is None else f"{value} [Manual override]"


def _select_value(select: Select[str]) -> str:
    value = select.value
    if value is Select.BLANK:
        raise ValueError(f"Choose a value for {select.id or 'this setting'}.")
    return str(value)


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
        f"sample delta {telemetry.swap_delta_gib:+.3f} GiB; I/O wait {io_wait}\n"
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


class SettingsHomeScreen(NoticeScreen):
    """Machine settings landing page with distinct modern and legacy routes."""

    BINDINGS = [("escape", "back", "Back")]

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
        with VerticalScroll(id="page"):
            yield CopyableText("Settings", classes="title")
            yield CopyableText(
                "Settings are machine-wide and apply to every Proof Assistant "
                "project on this machine. Project-specific overlays are not enabled.\n"
                + (
                    f"Calibration context: {self.project}"
                    if self.project is not None
                    else "Calibration context: none. Open Settings from a project "
                    "dashboard to benchmark or reset that project's Lean profile."
                )
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
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Concurrency / Resources",
                    id="open-concurrency-settings",
                    variant="primary",
                    disabled=self.snapshot is None,
                )
                yield Button(
                    "Legacy settings",
                    id="open-legacy-settings",
                    disabled=self.snapshot is None,
                )
                yield Button("Back", id="settings-back")
            yield CopyableText(
                "Loading settings through the backend…"
                if self.snapshot is None
                else "Choose a settings section.",
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def on_mount(self) -> None:
        if self.snapshot is None:
            self.refresh_snapshot()

    def action_back(self) -> None:
        self.proof_app.close_settings()

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
        elif event.button.id == "open-legacy-settings":
            if self.snapshot is not None:
                self.proof_app.show_legacy_settings(
                    self.snapshot,
                    project=self.project,
                )


class _SettingsEditorScreen(NoticeScreen):
    """Shared preview/apply flow for machine-scoped editors."""

    snapshot: MachineSettingsSnapshot

    def _preview(self, request: MachineSettingsUpdateRequest) -> None:
        self.show_notice("Validating the revision and previewing machine-wide changes…")

        def preview() -> None:
            try:
                result = self.proof_app.service.preview_machine_settings(request)
            except Exception as exc:
                self.proof_app.call_from_thread(
                    self.show_notice,
                    f"Settings preview failed: {exc}",
                    error=True,
                )
                return
            self.proof_app.call_from_thread(self._review_preview, result)

        self.run_worker(preview, thread=True, exclusive=True, group="machine-settings")

    def _review_preview(self, preview: SettingsChangePreview) -> None:
        if not preview.warnings:
            self._apply_preview(preview, ())
            return
        dialog = SettingsWarningConfirmationScreen(preview)

        def after_confirmation(accepted: bool | None) -> None:
            if accepted:
                self._apply_preview(
                    preview,
                    tuple(warning.warning_id for warning in preview.warnings),
                )
            else:
                self.show_notice("Settings change canceled; nothing was written.")

        self.proof_app.push_screen(dialog, callback=after_confirmation)

    def _apply_preview(
        self, preview: SettingsChangePreview, warning_ids: tuple[str, ...]
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
                    self.show_notice,
                    f"Settings were not applied: {exc}",
                    error=True,
                )
                return
            self.proof_app.call_from_thread(
                self._settings_applied,
                snapshot,
                preview,
            )

        self.run_worker(apply, thread=True, exclusive=True, group="machine-settings")

    def _settings_applied(
        self,
        snapshot: MachineSettingsSnapshot,
        preview: SettingsChangePreview,
    ) -> None:
        raise NotImplementedError


class ConcurrencyResourcesScreen(_SettingsEditorScreen):
    """Editable policy plus live backend resource/controller observations."""

    BINDINGS = [("escape", "back", "Settings")]

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

    def compose(self) -> ComposeResult:
        configured = self.snapshot.configured
        yield Header()
        with VerticalScroll(id="page"):
            yield CopyableText("Concurrency / Resources", classes="title")
            yield CopyableText(
                "This is the machine-wide policy. Auto values are resolved by "
                "separate AI, Lean, and build admission controllers.\n"
                + (
                    f"Project calibration context: {self.project}"
                    if self.project is not None
                    else "No project calibration context. Open Settings from a "
                    "project dashboard to run or reset a real Lean RSS calibration."
                )
            )
            yield TextArea(
                _machine_summary(self.snapshot),
                read_only=True,
                soft_wrap=False,
                id="settings-machine-summary",
            )
            yield TextArea(
                _concurrency_summary(self.snapshot),
                read_only=True,
                soft_wrap=False,
                id="concurrency-summary",
            )
            yield CopyableText("Live resources", classes="section")
            yield TextArea(
                _telemetry_text(self.snapshot),
                read_only=True,
                soft_wrap=False,
                id="resource-telemetry",
            )
            yield TextArea(
                _resolution_text(self.snapshot),
                read_only=True,
                soft_wrap=False,
                id="settings-resolution",
            )

            yield CopyableText("Machine policy", classes="section")
            yield Label("Concurrency mode")
            yield Select(
                (("Auto / Adaptive", "adaptive"), ("Fixed / Manual", "fixed")),
                value=configured.mode,
                allow_blank=False,
                id="concurrency-mode",
            )
            yield Label("Resource profile")
            yield Select(
                (
                    ("Auto", "auto"),
                    ("Interactive", "interactive"),
                    ("Server", "server"),
                ),
                value=configured.resource_profile,
                allow_blank=False,
                id="resource-profile",
            )
            yield Label("Codex plan")
            yield Select(
                (
                    ("Custom / Unknown", "unknown"),
                    ("Plus", "plus"),
                    ("Pro 5x", "pro_5x"),
                    ("Pro 20x", "pro_20x"),
                ),
                value=configured.codex_plan,
                allow_blank=False,
                id="codex-plan",
            )
            yield Label("AI budget policy")
            yield Select(
                (
                    ("Economy", "economy"),
                    ("Balanced", "balanced"),
                    ("Throughput", "throughput"),
                ),
                value=configured.budget_policy,
                allow_blank=False,
                id="budget-policy",
            )
            yield Label("AI concurrency (Auto or integer)")
            yield Input(value=_auto(configured.ai_initial), id="ai-concurrency")
            yield Label("AI hard ceiling (Auto or integer)")
            yield Input(value=_auto(configured.ai_hard_max), id="ai-hard-max")
            yield Label("AI successful queued turns before growth (Auto or integer)")
            yield Input(
                value=_auto(configured.ai_increase_after_successes),
                id="ai-growth-successes",
            )
            yield Label("Lean REPL pool (Auto or integer)")
            yield Input(value=_auto(configured.lean_pool), id="lean-pool")
            yield Label("Lean pool maximum (Auto or integer)")
            yield Input(value=_auto(configured.lean_max), id="lean-max")
            yield Label("Maximum concurrent builds (Auto or integer)")
            yield Input(value=_auto(configured.max_builds), id="max-builds")
            yield Label("Maximum agents per target")
            yield Input(
                value=str(configured.agents_per_target_max),
                id="agents-per-target",
            )
            yield Checkbox(
                "Duplicate-agent escalation",
                value=configured.duplicate_agent_escalation,
                id="duplicate-escalation",
            )
            yield Checkbox(
                "Lean REPL memory calibration",
                value=configured.lean_memory_calibration,
                id="lean-calibration",
            )
            yield Checkbox(
                "Dependency-priority scheduling",
                value=configured.dependency_priority,
                id="dependency-priority",
            )
            yield Checkbox(
                "Adaptive controller",
                value=configured.adaptive_controller,
                id="adaptive-controller",
            )
            yield Checkbox(
                "Hardware telemetry",
                value=configured.hardware_telemetry,
                id="hardware-telemetry",
            )
            with Horizontal(classes="toolbar"):
                yield Button(
                    "Preview and save", id="save-concurrency", variant="success"
                )
                yield Button(
                    "Reset all to Auto", id="reset-concurrency", variant="warning"
                )
                yield Button("Settings", id="concurrency-back")

            yield CopyableText("Benchmarks / calibration", classes="section")
            with Vertical(classes="settings-benchmark-toolbar"):
                yield Button("Benchmark Codex (no traffic)", id="benchmark-codex")
                yield Button(
                    "Benchmark Lean",
                    id="benchmark-lean",
                    disabled=self.project is None,
                )
                yield Button("Benchmark builds", id="benchmark-build")
                yield Button(
                    "Reset project Lean calibration",
                    id="reset-lean-calibration",
                    disabled=self.project is None,
                    variant="warning",
                )
                yield Button(
                    "Reset adaptive history",
                    id="reset-adaptive-history",
                    variant="warning",
                )
            yield TextArea(
                "No benchmark run in this client.",
                read_only=True,
                soft_wrap=True,
                id="benchmark-result",
            )
            yield CopyableText(
                "Live status is sampled through the backend. Reducing a limit stops "
                "new admissions; work already running is allowed to finish safely.",
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2.0, self.refresh_status)

    def action_back(self) -> None:
        self.proof_app.show_settings(self.snapshot, project=self.project)

    def _configured_from_form(self) -> ConcurrencySettingsView:
        configured = self.snapshot.configured
        return replace(
            configured,
            mode=_select_value(self.query_one("#concurrency-mode", Select)),
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
            adaptive_controller=self.query_one("#adaptive-controller", Checkbox).value,
            hardware_telemetry=self.query_one("#hardware-telemetry", Checkbox).value,
        )

    def _save(self) -> None:
        try:
            configured = self._configured_from_form()
        except ValueError as exc:
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
    ) -> None:
        self._replace_snapshot_and_form(snapshot)
        live = ", ".join(preview.live_fields) or "none"
        next_run = ", ".join(preview.next_run_fields) or "none"
        self.show_notice(
            f"Machine settings revision {snapshot.revision} saved. "
            f"Applied live: {live}. Takes effect next run: {next_run}."
        )

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
        if self.project is None:
            self.show_notice(
                "Open Settings from a project dashboard to reset its Lean profile.",
                error=True,
            )
            return
        self.query_one(
            "#benchmark-result", TextArea
        ).text = f"Resetting the exact Lean calibration for {self.project}…"

        def reset() -> None:
            try:
                result = self.proof_app.service.reset_project_lean_calibration(
                    self.project
                )
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
            self._save()
        elif button_id == "reset-concurrency":
            self._reset()
        elif button_id == "concurrency-back":
            self.action_back()
        elif button_id == "benchmark-codex":
            self._benchmark(BenchmarkKind.CODEX)
        elif button_id == "benchmark-lean":
            self._benchmark(BenchmarkKind.LEAN)
        elif button_id == "benchmark-build":
            self._benchmark(BenchmarkKind.BUILD)
        elif button_id == "reset-lean-calibration":
            self._reset_lean_calibration()
        elif button_id == "reset-adaptive-history":
            self._reset_adaptive_history()

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

    BINDINGS = [("escape", "back", "Settings")]

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
        with VerticalScroll(id="page"):
            yield CopyableText("Legacy settings", classes="title")
            yield CopyableText(
                "These machine-wide compatibility values describe the coupled "
                "concurrency knobs inherited from the previous verifier design."
            )
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
            with Horizontal(classes="toolbar"):
                yield Button("Preview and save", id="save-legacy", variant="success")
                yield Button("Settings", id="legacy-back")
            yield CopyableText(
                "The backend validates these values and reports which are active, "
                "superseded, or next-run only.",
                id="status-line",
                classes="muted",
            )
        yield Footer()

    def action_back(self) -> None:
        self.proof_app.show_settings(self.snapshot, project=self.project)

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
    ) -> None:
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
        next_run = ", ".join(preview.next_run_fields) or "none"
        self.show_notice(
            f"Legacy machine settings revision {snapshot.revision} saved. "
            f"Takes effect next run: {next_run}."
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-legacy":
            self._save()
        elif event.button.id == "legacy-back":
            self.action_back()


class SettingsWarningConfirmationScreen(ModalScreen[bool]):
    """Cancel-first confirmation for backend-classified unsafe settings."""

    BINDINGS = [("escape", "cancel", "Cancel")]

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

    def action_cancel(self) -> None:
        self.dismiss(False)

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
            self.dismiss(True)
        elif event.button.id == "settings-warning-cancel":
            self.dismiss(False)
