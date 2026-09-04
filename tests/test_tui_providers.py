from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from textual.css.query import NoMatches
from textual.pilot import Pilot
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Input,
    OptionList,
    Select,
    Static,
    TextArea,
)
from textual.widgets.select import InvalidSelectValueError

from proof_assistant.ai import (
    AuthenticationState,
    CommandSpec,
    CredentialSource,
    Difficulty,
    DiscoverySource,
    DriverId,
    DriverStatus,
    DriverTransport,
    InstallationState,
    InstallPlan,
    InstallResult,
    MachineProviderSettings,
    ModelCatalog,
    ModelDescriptor,
    ProviderConfig,
    ProviderSetupSnapshot,
    SecretSubmission,
    SetupActionState,
    TaskKind,
    TaskModelPolicy,
)
from proof_assistant.tui import ProofAssistantApp
from proof_assistant.tui.commands import AppHeader
from proof_assistant.tui.screens import WelcomeScreen
from proof_assistant.tui.settings import (
    AIAccountVerificationConfirmationScreen,
    AIInstallConfirmationScreen,
    AIProviderSettingsScreen,
    ProjectInheritanceConfirmationScreen,
    SettingsHomeScreen,
    UnsavedAISettingsConfirmationScreen,
)
from proof_assistant.workflow import (
    ProjectAIOverride,
    ProjectAIRoleOverride,
    ProjectVerificationSettingsSnapshot,
    SettingsScopeKind,
    VerificationRoleSettings,
    VerificationSettings,
)


def async_test[Result](
    function: Callable[..., Awaitable[Result]],
) -> Callable[..., Result]:
    def run(*args: Any, **kwargs: Any) -> Result:
        return asyncio.run(function(*args, **kwargs))

    return run


async def wait_for(
    pilot: Pilot[None], predicate: Callable[[], bool], *, attempts: int = 200
) -> None:
    for _ in range(attempts):
        try:
            if predicate():
                return
        except NoMatches:
            # A newly switched Textual screen exists before its compose tree is
            # mounted; retry until the asserted widgets are available.
            pass
        await pilot.pause(0.01)
    raise AssertionError(f"condition not reached; screen={pilot.app.screen!r}")


def assert_select_accepts(select: Select[str], values: set[str]) -> None:
    """Check selectable values through Select's documented value API."""

    original = select.value
    for value in values:
        select.value = value
        assert select.value == value
    select.value = original


async def settle_screen(pilot: Pilot[None]) -> None:
    """Wait until the current screen's application header is mounted and laid out."""

    def header_is_ready() -> bool:
        headers = pilot.app.screen.query(AppHeader).nodes
        if not headers:
            return False
        header = headers[0]
        return bool(header.is_mounted and header.screen is pilot.app.screen)

    await wait_for(pilot, header_is_ready)
    await pilot.pause()


async def show_ai_settings_view(
    pilot: Pilot[None], screen: AIProviderSettingsScreen, view: str
) -> None:
    positions = {"roles": 0, "connection": 1, "diagnostics": 2}
    await wait_for(pilot, lambda: bool(screen.query("#ai-settings-nav").nodes))
    navigation = screen.query_one("#ai-settings-nav", OptionList)
    navigation.highlighted = positions[view]
    navigation.focus()
    await pilot.press("enter")
    await wait_for(
        pilot,
        lambda: (
            screen.query_one("#ai-settings-pages", ContentSwitcher).current
            == f"{view}-page"
        ),
    )


def notice_text(screen: AIProviderSettingsScreen) -> str:
    notice = screen.query_one("#status-line")
    if isinstance(notice, TextArea):
        return notice.text
    assert isinstance(notice, Static)
    content = notice.content
    return content.plain if hasattr(content, "plain") else str(content)


def _catalog(driver: DriverId) -> ModelCatalog:
    models = {
        DriverId.CODEX_CLI: (
            ModelDescriptor(
                "gpt-5.6-sol",
                "GPT-5.6 Sol",
                (Difficulty.AUTO, Difficulty.HIGH, Difficulty.XHIGH),
            ),
            ModelDescriptor(
                "gpt-5.6-terra",
                "GPT-5.6 Terra",
                (Difficulty.AUTO, Difficulty.LOW, Difficulty.HIGH),
            ),
        ),
        DriverId.CLAUDE_CLI: tuple(
            ModelDescriptor(
                model_id,
                f"Claude {display_name}",
                (
                    Difficulty.AUTO,
                    Difficulty.LOW,
                    Difficulty.MEDIUM,
                    Difficulty.HIGH,
                    Difficulty.XHIGH,
                    Difficulty.MAX,
                ),
            )
            for model_id, display_name in (
                ("best", "Best"),
                ("fable", "Fable"),
                ("opus", "Opus"),
                ("sonnet", "Sonnet"),
                ("haiku", "Haiku"),
            )
        ),
        DriverId.COPILOT_CLI: (
            ModelDescriptor("auto", "Automatic", (Difficulty.AUTO,)),
        ),
        DriverId.OPENAI_API: (
            ModelDescriptor(
                "gpt-5.6-sol",
                "GPT-5.6 Sol",
                (Difficulty.AUTO, Difficulty.HIGH, Difficulty.XHIGH),
            ),
        ),
        DriverId.ANTHROPIC_API: (
            ModelDescriptor(
                "claude-opus-4-6",
                "Claude Opus 4.6",
                (Difficulty.AUTO, Difficulty.HIGH, Difficulty.XHIGH),
            ),
        ),
        DriverId.GEMINI_API: (
            ModelDescriptor(
                "gemini-2.5-pro",
                "Gemini 2.5 Pro",
                (Difficulty.AUTO, Difficulty.HIGH),
            ),
        ),
    }[driver]
    return ModelCatalog(
        driver,
        models,
        DiscoverySource.LIVE_ACCOUNT,
        "Live test account catalog.",
        True,
    )


def _status(
    driver: DriverId,
    *,
    installed: bool = True,
    authentication: AuthenticationState = AuthenticationState.AUTHENTICATED,
) -> DriverStatus:
    cli = driver in {
        DriverId.CODEX_CLI,
        DriverId.CLAUDE_CLI,
        DriverId.COPILOT_CLI,
    }
    installation = (
        InstallationState.INSTALLED
        if cli and installed
        else InstallationState.MISSING
        if cli
        else InstallationState.NOT_APPLICABLE
    )
    executable = (
        f"/Users/test/.local/bin/{driver.value.removesuffix('_cli')}"
        if cli and installed
        else None
    )
    return DriverStatus(
        driver,
        DriverTransport.CLI if cli else DriverTransport.API,
        installation,
        authentication,
        executable=executable,
        version="test 1.0" if executable else None,
        detail=(
            "Account connection verified."
            if authentication is AuthenticationState.AUTHENTICATED
            else "Run the provider login command shown by the backend."
        ),
        catalog=_catalog(driver),
    )


def _snapshot(
    *,
    primary: DriverId = DriverId.CODEX_CLI,
    ready: bool = True,
    revision: int = 1,
    statuses: tuple[DriverStatus, ...] | None = None,
    config: ProviderConfig | None = None,
) -> ProviderSetupSnapshot:
    return ProviderSetupSnapshot(
        MachineProviderSettings(revision, config or ProviderConfig()),
        statuses
        or tuple(
            _status(
                driver,
                authentication=(
                    AuthenticationState.UNKNOWN
                    if driver is DriverId.COPILOT_CLI
                    else AuthenticationState.REQUIRED
                    if driver
                    in {
                        DriverId.OPENAI_API,
                        DriverId.ANTHROPIC_API,
                        DriverId.GEMINI_API,
                    }
                    else AuthenticationState.AUTHENTICATED
                ),
            )
            for driver in DriverId
        ),
        primary,
        ready,
        "Primary AI driver is ready." if ready else "Primary AI driver needs setup.",
    )


class ProviderWorkflowFake:
    def __init__(self, snapshot: ProviderSetupSnapshot) -> None:
        self.snapshot = snapshot
        self.setup_reads = 0
        self.updates: list[tuple[ProviderConfig, int]] = []
        self.project_snapshots: dict[Path, ProjectVerificationSettingsSnapshot] = {}
        self.project_updates: list[tuple[Path, ProjectAIOverride, int]] = []
        self.project_resets: list[tuple[Path, int]] = []
        self.install_previews: list[DriverId] = []
        self.install_calls: list[tuple[InstallPlan, str]] = []
        self.credential_calls: list[tuple[DriverId, CredentialSource]] = []
        self.credential_deleted: list[tuple[DriverId, CredentialSource]] = []
        self.secret_was_received = False
        self.account_verifications: list[DriverId] = []
        self.recommended_defaults_started = threading.Event()
        self.recommended_defaults_release: threading.Event | None = None
        self.recommended_defaults_started_by_driver = {
            driver: threading.Event() for driver in DriverId
        }
        self.recommended_defaults_completed_by_driver = {
            driver: threading.Event() for driver in DriverId
        }
        self.recommended_defaults_release_by_driver: dict[
            DriverId, threading.Event
        ] = {}
        self.initial_policies_started = threading.Event()
        self.initial_policies_completed = threading.Event()
        self.initial_policies_release: threading.Event | None = None
        self.machine_update_started = threading.Event()
        self.machine_update_release: threading.Event | None = None
        self.project_update_started = threading.Event()
        self.project_update_release: threading.Event | None = None
        self.plan = InstallPlan(
            DriverId.CLAUDE_CLI,
            SetupActionState.AVAILABLE,
            (
                CommandSpec(
                    (
                        "/usr/local/bin/npm",
                        "install",
                        "--global",
                        "--prefix",
                        "/Users/test/.local",
                        "@anthropic-ai/claude-code",
                    )
                ),
            ),
            "claude",
            "/Users/test/.local/bin",
            "consent-test-token",
            "Install Claude Code into the user-owned prefix.",
        )

    def _machine_effective_settings(self) -> VerificationSettings:
        policies = self.ai_task_policies()
        role_settings = tuple(
            VerificationRoleSettings(
                task=policy.task,
                ai_driver=policy.driver.value,
                model=policy.model or "auto",
                effort=policy.difficulty.value,
            )
            for policy in policies
        )
        proof = next(item for item in role_settings if item.task is TaskKind.PROOF)
        return VerificationSettings(
            ai_driver=proof.ai_driver,
            model=proof.model,
            effort=proof.effort,
            role_settings=role_settings,
        )

    def list_projects(self) -> tuple[()]:
        return ()

    def get_ai_setup(self) -> ProviderSetupSnapshot:
        self.setup_reads += 1
        return self.snapshot

    def ai_task_policies(
        self, driver: DriverId | None = None
    ) -> tuple[TaskModelPolicy, ...]:
        release = (
            self.recommended_defaults_release_by_driver.get(driver)
            if driver is not None
            else None
        )
        if driver is not None and release is not None:
            self.recommended_defaults_started_by_driver[driver].set()
            if not release.wait(timeout=5):
                raise AssertionError("timed out waiting to release role defaults")
        elif driver is not None and self.recommended_defaults_release is not None:
            self.recommended_defaults_started.set()
            if not self.recommended_defaults_release.wait(timeout=5):
                raise AssertionError("timed out waiting to release role defaults")
        elif driver is None and self.initial_policies_release is not None:
            self.initial_policies_started.set()
            if not self.initial_policies_release.wait(timeout=5):
                raise AssertionError("timed out waiting to release initial policies")
        selected_driver = driver or self.snapshot.primary_driver
        claude_defaults = {
            TaskKind.CLARIFICATION: ("opus", Difficulty.HIGH),
            TaskKind.DIAGNOSTIC: ("opus", Difficulty.HIGH),
            TaskKind.PROOF: ("best", Difficulty.HIGH),
            TaskKind.SKETCH: ("sonnet", Difficulty.MEDIUM),
            TaskKind.MAINTENANCE: ("sonnet", Difficulty.MEDIUM),
            TaskKind.REVIEW: ("opus", Difficulty.HIGH),
            TaskKind.DUPLICATE_PROOF: ("fable", Difficulty.XHIGH),
            TaskKind.REPORTING: ("haiku", Difficulty.LOW),
        }
        policies: list[TaskModelPolicy] = []
        for task in TaskKind:
            configured = self.snapshot.settings.config.task_preference_for(task)
            effective_driver = (
                configured.driver
                if driver is None
                and configured is not None
                and configured.driver is not None
                else selected_driver
            )
            if effective_driver is DriverId.CLAUDE_CLI:
                model, difficulty = claude_defaults[task]
            else:
                descriptor = _catalog(effective_driver).models[0]
                model = descriptor.model_id
                difficulty = (
                    Difficulty.HIGH
                    if Difficulty.HIGH in descriptor.difficulties
                    else Difficulty.AUTO
                )
            if driver is None and configured is not None:
                model = configured.model or model
                if configured.difficulty is not Difficulty.AUTO:
                    difficulty = configured.difficulty
            policies.append(
                TaskModelPolicy(
                    task,
                    effective_driver,
                    model,
                    difficulty,
                    DiscoverySource.LIVE_ACCOUNT,
                    "Uses the current role recommendation.",
                )
            )
        result = tuple(policies)
        if driver is None:
            self.initial_policies_completed.set()
        else:
            self.recommended_defaults_completed_by_driver[driver].set()
        return result

    def update_ai_settings(
        self, config: ProviderConfig, *, expected_revision: int
    ) -> ProviderSetupSnapshot:
        if self.machine_update_release is not None:
            self.machine_update_started.set()
            if not self.machine_update_release.wait(timeout=5):
                raise AssertionError("timed out waiting to release machine update")
        if expected_revision != self.snapshot.settings.revision:
            raise ValueError("provider settings revision changed; reload before saving")
        self.updates.append((config, expected_revision))
        self.snapshot = replace(
            self.snapshot,
            settings=MachineProviderSettings(expected_revision + 1, config),
            primary_driver=config.primary_driver,
            primary_ready=_status_by_driver(self.snapshot, config.primary_driver).ready,
        )
        return self.snapshot

    def get_project_verification_settings(
        self, project: Path
    ) -> ProjectVerificationSettingsSnapshot:
        return self.project_snapshots.setdefault(
            project,
            ProjectVerificationSettingsSnapshot(
                project_path=project,
                revision=0,
                override=None,
                effective=self._machine_effective_settings(),
            ),
        )

    def update_project_verification_settings(
        self,
        project: Path,
        override: ProjectAIOverride,
        *,
        expected_revision: int,
    ) -> ProjectVerificationSettingsSnapshot:
        if self.project_update_release is not None:
            self.project_update_started.set()
            if not self.project_update_release.wait(timeout=5):
                raise AssertionError("timed out waiting to release project update")
        current = self.get_project_verification_settings(project)
        assert current.revision == expected_revision
        self.project_updates.append((project, override, expected_revision))
        updated = ProjectVerificationSettingsSnapshot(
            project_path=project,
            revision=expected_revision + 1,
            override=override,
            effective=self._effective_project_override(current.effective, override),
        )
        self.project_snapshots[project] = updated
        return updated

    @staticmethod
    def _effective_project_override(
        current: VerificationSettings, override: ProjectAIOverride
    ) -> VerificationSettings:
        roles = tuple(
            VerificationRoleSettings(
                task=role.task,
                ai_driver=override.ai_driver.value,
                model=role.model,
                effort=role.difficulty.value,
            )
            for role in override.roles
        )
        proof = next(item for item in roles if item.task is TaskKind.PROOF)
        return replace(
            current,
            ai_driver=proof.ai_driver,
            model=proof.model,
            effort=proof.effort,
            role_settings=roles,
        )

    def reset_project_verification_settings(
        self, project: Path, *, expected_revision: int
    ) -> ProjectVerificationSettingsSnapshot:
        current = self.get_project_verification_settings(project)
        assert current.revision == expected_revision
        self.project_resets.append((project, expected_revision))
        updated = ProjectVerificationSettingsSnapshot(
            project_path=project,
            revision=expected_revision + 1,
            override=None,
            effective=self._machine_effective_settings(),
        )
        self.project_snapshots[project] = updated
        return updated

    def preview_ai_driver_install(self, driver: DriverId) -> InstallPlan:
        self.install_previews.append(driver)
        return replace(self.plan, driver=driver)

    def install_ai_driver(
        self, plan: InstallPlan, *, consent_token: str
    ) -> InstallResult:
        self.install_calls.append((plan, consent_token))
        installed = _status(plan.driver)
        self.snapshot = _replace_status(self.snapshot, installed)
        return InstallResult(
            plan.driver,
            True,
            True,
            installed,
            "Installation and executable identity checks succeeded.",
        )

    def verify_ai_driver_account(
        self, driver: DriverId, *, consent: bool
    ) -> ProviderSetupSnapshot:
        assert consent is True
        self.account_verifications.append(driver)
        self.snapshot = _replace_status(self.snapshot, _status(driver))
        return self.snapshot

    def store_ai_credential(
        self,
        driver: DriverId,
        source: CredentialSource,
        credential: SecretSubmission,
    ) -> ProviderSetupSnapshot:
        assert "sk-test-never-retain" not in repr(credential)
        self.secret_was_received = credential.consume() == "sk-test-never-retain"
        self.credential_calls.append((driver, source))
        preferences = tuple(
            replace(item, credential_source=source) if item.driver is driver else item
            for item in self.snapshot.settings.config.drivers
        )
        config = replace(self.snapshot.settings.config, drivers=preferences)
        self.snapshot = replace(
            _replace_status(self.snapshot, _status(driver)),
            settings=MachineProviderSettings(
                self.snapshot.settings.revision + 1, config
            ),
        )
        return self.snapshot

    def delete_ai_credential(
        self, driver: DriverId, source: CredentialSource
    ) -> ProviderSetupSnapshot:
        self.credential_deleted.append((driver, source))
        return self.snapshot


def _status_by_driver(
    snapshot: ProviderSetupSnapshot, driver: DriverId
) -> DriverStatus:
    return next(item for item in snapshot.statuses if item.driver is driver)


def _replace_status(
    snapshot: ProviderSetupSnapshot, status: DriverStatus
) -> ProviderSetupSnapshot:
    statuses = tuple(
        status if item.driver is status.driver else item for item in snapshot.statuses
    )
    primary = next(item for item in statuses if item.driver is snapshot.primary_driver)
    return replace(snapshot, statuses=statuses, primary_ready=primary.ready)


@async_test
async def test_first_run_routes_to_setup_and_blocks_main_menu() -> None:
    statuses = tuple(
        _status(driver, installed=driver is not DriverId.CODEX_CLI)
        if driver is DriverId.CODEX_CLI
        else _status(
            driver,
            authentication=(
                AuthenticationState.UNKNOWN
                if driver is DriverId.COPILOT_CLI
                else AuthenticationState.REQUIRED
                if driver
                in {
                    DriverId.OPENAI_API,
                    DriverId.ANTHROPIC_API,
                    DriverId.GEMINI_API,
                }
                else AuthenticationState.AUTHENTICATED
            ),
        )
        for driver in DriverId
    )
    service = ProviderWorkflowFake(
        _snapshot(ready=False, revision=0, statuses=statuses)
    )
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        assert screen.query_one("#ai-settings-nav", OptionList).highlighted == 0
        assert (
            screen.query_one("#ai-settings-pages", ContentSwitcher).current
            == "choose-page"
        )
        summary = screen.query_one("#ai-provider-summary", TextArea).text
        assert all(
            label in summary
            for label in (
                "OpenAI Codex CLI",
                "Anthropic Claude Code CLI",
                "GitHub Copilot CLI",
                "OpenAI API",
                "Anthropic API",
                "Google Gemini API",
            )
        )
        assert "Available models and exact difficulties" in summary
        await settle_screen(pilot)
        app.action_main_menu()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, AIProviderSettingsScreen)
                and "Finish primary AI setup" in notice_text(screen)
            ),
        )
        app.action_global_settings()
        await wait_for(
            pilot,
            lambda: (
                app.screen is screen
                and "Finish first-run provider setup" in notice_text(screen)
            ),
        )
        app.action_main_menu()
        await wait_for(pilot, lambda: app.screen is screen)


@async_test
async def test_first_run_ready_alternate_provider_can_be_selected_and_saved() -> None:
    statuses = tuple(
        _status(driver, installed=False)
        if driver is DriverId.CODEX_CLI
        else _status(
            driver,
            authentication=(
                AuthenticationState.UNKNOWN
                if driver is DriverId.COPILOT_CLI
                else AuthenticationState.REQUIRED
                if driver in _API_DRIVERS_FOR_TEST
                else AuthenticationState.AUTHENTICATED
            ),
        )
        for driver in DriverId
    )
    service = ProviderWorkflowFake(
        _snapshot(ready=False, revision=0, statuses=statuses)
    )
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-configure-driver", Select).disabled,
        )

        screen.query_one(
            "#ai-configure-driver", Select
        ).value = DriverId.CLAUDE_CLI.value
        await wait_for(
            pilot,
            lambda: (
                screen.query_one("#ai-primary-driver", Select).value
                == DriverId.CLAUDE_CLI.value
                and screen._machine_role_drafts.get(TaskKind.DUPLICATE_PROOF)
                == ("fable", Difficulty.XHIGH)
            ),
        )
        assert screen.query_one("#ai-setup-continue", Button).disabled

        screen.query_one("#ai-first-run-next", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen.query_one("#ai-settings-pages", ContentSwitcher).current
                == "connection-page"
            ),
        )
        screen.query_one("#ai-first-run-next", Button).press()
        await wait_for(
            pilot,
            lambda: (
                bool(service.updates)
                and screen.snapshot is not None
                and screen.snapshot.primary_ready
                and not screen.query_one("#ai-setup-continue", Button).disabled
            ),
        )
        assert service.snapshot.primary_driver is DriverId.CLAUDE_CLI
        assert (
            screen.query_one("#ai-settings-pages", ContentSwitcher).current
            == "roles-page"
        )
        assert screen.query_one("#ai-settings-nav", OptionList).highlighted == 2
        assert screen.query_one("#ai-role-roster", DataTable).row_count == len(TaskKind)
        screen.query_one("#ai-setup-continue", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))


@async_test
async def test_landing_and_revisioned_model_difficulty_update() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, WelcomeScreen)
                and bool(app.screen.query("#landing-ai-provider-status").nodes)
                and "codex_cli"
                in app.screen.query_one("#landing-ai-provider-status", TextArea).text
            ),
        )
        await settle_screen(pilot)
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#save-ai-settings", Button).disabled,
        )
        await show_ai_settings_view(pilot, screen, "connection")
        model = screen.query_one("#ai-provider-model", Select)
        model.value = "gpt-5.6-sol"
        await pilot.pause()
        difficulty = screen.query_one("#ai-provider-difficulty", Select)
        assert_select_accepts(difficulty, {"auto", "high", "xhigh"})
        difficulty.value = "xhigh"
        screen._save_settings()
        await wait_for(pilot, lambda: bool(service.updates))
        config, revision = service.updates[-1]
        assert revision == 1
        preference = config.preference_for(DriverId.CODEX_CLI)
        assert preference.model == "gpt-5.6-sol"
        assert preference.difficulty is Difficulty.XHIGH
        task_text = screen.query_one("#ai-task-policies", TextArea).text
        assert "resolved by the backend" in task_text
        assert all(f"[{task.value}]" in task_text for task in TaskKind)
        assert_select_accepts(
            screen.query_one("#ai-role-task", Select),
            {task.value for task in TaskKind},
        )


@async_test
async def test_provider_roster_selection_updates_connection_inspector() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await show_ai_settings_view(pilot, screen, "connection")
        roster = screen.query_one("#ai-provider-roster", DataTable)
        await wait_for(pilot, lambda: roster.row_count == len(DriverId))

        roster.focus()
        roster.move_cursor(
            row=list(DriverId).index(DriverId.CLAUDE_CLI), column=0, animate=False
        )
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                screen.query_one("#ai-configure-driver", Select).value
                == DriverId.CLAUDE_CLI.value
            ),
        )
        assert (
            "Anthropic Claude Code CLI"
            in screen.query_one("#ai-auth-next-step", TextArea).text
        )


@async_test
async def test_every_provider_saves_a_complete_capability_valid_role_team() -> None:
    for driver in DriverId:
        service = ProviderWorkflowFake(
            _snapshot(statuses=tuple(_status(candidate) for candidate in DriverId))
        )
        app = ProofAssistantApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(140, 48)) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
            app.show_ai_provider_settings()
            await wait_for(
                pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen)
            )
            screen = app.screen
            assert isinstance(screen, AIProviderSettingsScreen)
            await wait_for(
                pilot,
                lambda: (
                    len(screen._machine_role_drafts) == len(TaskKind)
                    and not screen.query_one("#ai-primary-driver", Select).disabled
                ),
            )

            screen.query_one("#ai-primary-driver", Select).value = driver.value
            screen.query_one("#ai-use-recommended", Button).press()
            try:
                await wait_for(
                    pilot,
                    lambda: (
                        screen.query_one("#save-ai-settings", Button).disabled is False
                        and all(policy.driver is driver for policy in screen._policies)
                    ),
                )
            except AssertionError as exc:
                raise AssertionError(
                    f"provider defaults did not settle for {driver.value}: "
                    f"notice={notice_text(screen)!r}; policies={screen._policies!r}"
                ) from exc
            catalog = _catalog(driver)
            descriptors = {item.model_id: item for item in catalog.models}
            assert set(screen._machine_role_drafts) == set(TaskKind)
            for model, difficulty in screen._machine_role_drafts.values():
                assert model in descriptors
                assert difficulty in descriptors[model].difficulties

            screen.query_one("#save-ai-settings", Button).press()
            await wait_for(pilot, lambda: bool(service.updates))
            config, expected_revision = service.updates[-1]
            assert expected_revision == 1
            assert config.primary_driver is driver
            assert {item.task for item in config.tasks} == set(TaskKind)
            assert all(item.driver is driver for item in config.tasks)
            for item in config.tasks:
                assert item.model in descriptors
                assert item.difficulty in descriptors[item.model].difficulties


@async_test
async def test_provider_switch_and_one_click_defaults_are_visible_and_explicit() -> (
    None
):
    for size in ((80, 24), (120, 40)):
        service = ProviderWorkflowFake(_snapshot())
        app = ProofAssistantApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=size) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
            app.show_ai_provider_settings()
            await wait_for(
                pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen)
            )
            screen = app.screen
            assert isinstance(screen, AIProviderSettingsScreen)
            await wait_for(
                pilot,
                lambda: (
                    set(screen._machine_role_drafts) == set(TaskKind)
                    and not screen.query_one("#ai-primary-driver", Select).disabled
                ),
            )
            await pilot.pause()

            provider = screen.query_one("#ai-primary-driver", Select)
            defaults = screen.query_one("#ai-use-recommended", Button)
            roles_page = screen.query_one("#roles-page")
            assert provider.is_on_screen
            assert defaults.is_on_screen
            assert roles_page.region.contains_region(provider.region)
            assert roles_page.region.contains_region(defaults.region)

            original_drafts = dict(screen._machine_role_drafts)
            provider.value = DriverId.CLAUDE_CLI.value
            await wait_for(
                pilot,
                lambda: (
                    screen.query_one("#ai-role-model", Select).value
                    == "__needs_update__"
                    and screen.query_one("#save-ai-settings", Button).disabled
                ),
            )
            assert screen._machine_role_drafts == original_drafts
            assert "Claude Code CLI" in defaults.label.plain
            assert "all 8 roles" in defaults.label.plain
            with pytest.raises(InvalidSelectValueError):
                screen.query_one("#ai-role-model", Select).value = "gpt-5.6-sol"
            roster = screen.query_one("#ai-role-roster", DataTable)
            assert all(
                "Needs update" in {str(cell) for cell in roster.get_row(task.value)}
                for task in TaskKind
            )

            defaults.press()
            await wait_for(
                pilot,
                lambda: (
                    not screen.query_one("#save-ai-settings", Button).disabled
                    and screen._machine_role_drafts[TaskKind.DUPLICATE_PROOF]
                    == ("fable", Difficulty.XHIGH)
                ),
            )
            claude_models = {
                item.model_id for item in _catalog(DriverId.CLAUDE_CLI).models
            }
            assert set(screen._machine_role_drafts) == set(TaskKind)
            assert all(
                model in claude_models
                for model, _ in screen._machine_role_drafts.values()
            )

            screen.query_one("#ai-manage-connection", Button).press()
            await wait_for(
                pilot,
                lambda: (
                    screen.query_one("#ai-settings-pages", ContentSwitcher).current
                    == "connection-page"
                ),
            )
            assert (
                screen.query_one("#ai-configure-driver", Select).value
                == DriverId.CLAUDE_CLI.value
            )


@async_test
async def test_global_quit_preserves_dirty_provider_settings_guard() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]
    quit_requested = asyncio.Event()
    app.exit = lambda *args, **kwargs: quit_requested.set()  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-primary-driver", Select).disabled,
        )
        screen.query_one("#ai-primary-driver", Select).value = DriverId.CLAUDE_CLI.value

        await pilot.press("ctrl+q")
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, UnsavedAISettingsConfirmationScreen),
        )
        assert not quit_requested.is_set()
        app.screen.query_one("#ai-unsaved-discard", Button).press()
        await wait_for(pilot, quit_requested.is_set)


@async_test
async def test_claude_role_defaults_are_visible_and_one_role_can_be_overridden() -> (
    None
):
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-primary-driver", Select).disabled,
        )

        screen.query_one("#ai-primary-driver", Select).value = DriverId.CLAUDE_CLI.value
        await wait_for(
            pilot,
            lambda: (
                screen.query_one("#ai-configure-driver", Select).value
                == DriverId.CLAUDE_CLI.value
            ),
        )
        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: (
                set(screen._machine_role_drafts) == set(TaskKind)
                and screen._machine_role_drafts[TaskKind.PROOF][0] == "best"
            ),
        )
        expected = {
            TaskKind.CLARIFICATION: ("opus", Difficulty.HIGH),
            TaskKind.DIAGNOSTIC: ("opus", Difficulty.HIGH),
            TaskKind.PROOF: ("best", Difficulty.HIGH),
            TaskKind.SKETCH: ("sonnet", Difficulty.MEDIUM),
            TaskKind.MAINTENANCE: ("sonnet", Difficulty.MEDIUM),
            TaskKind.REVIEW: ("opus", Difficulty.HIGH),
            TaskKind.DUPLICATE_PROOF: ("fable", Difficulty.XHIGH),
            TaskKind.REPORTING: ("haiku", Difficulty.LOW),
        }
        assert screen._machine_role_drafts == expected

        role = screen.query_one("#ai-role-task", Select)
        role_model = screen.query_one("#ai-role-model", Select)
        role_difficulty = screen.query_one("#ai-role-difficulty", Select)
        roster = screen.query_one("#ai-role-roster", DataTable)
        await wait_for(pilot, lambda: roster.row_count == len(TaskKind))
        roster.focus()
        for task, (model, difficulty) in expected.items():
            roster.move_cursor(row=list(TaskKind).index(task), column=0, animate=False)
            await pilot.pause()
            await wait_for(
                pilot,
                lambda model=model, difficulty=difficulty: (
                    role.value == task.value
                    and role_model.value == model
                    and role_difficulty.value == difficulty.value
                ),
            )

        roster.move_cursor(
            row=list(TaskKind).index(TaskKind.REPORTING), column=0, animate=False
        )
        await wait_for(pilot, lambda: role_model.value == "haiku")
        role_model.value = "sonnet"
        await wait_for(
            pilot,
            lambda: screen._machine_role_drafts[TaskKind.REPORTING][0] == "sonnet",
        )
        assert_select_accepts(
            role_difficulty,
            {
                difficulty.value
                for difficulty in _catalog(DriverId.CLAUDE_CLI).models[3].difficulties
            },
        )
        role_difficulty.value = Difficulty.HIGH.value
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts[TaskKind.REPORTING]
                == ("sonnet", Difficulty.HIGH)
            ),
        )
        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts[TaskKind.REPORTING]
                == ("haiku", Difficulty.LOW)
            ),
        )
        screen.query_one("#ai-undo-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts[TaskKind.REPORTING]
                == ("sonnet", Difficulty.HIGH)
            ),
        )
        assert screen.query_one("#ai-undo-recommended", Button).disabled
        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts[TaskKind.REPORTING]
                == ("haiku", Difficulty.LOW)
            ),
        )
        role_model.value = "sonnet"
        await wait_for(
            pilot,
            lambda: screen._machine_role_drafts[TaskKind.REPORTING][0] == "sonnet",
        )
        role_difficulty.value = Difficulty.HIGH.value
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts[TaskKind.REPORTING]
                == ("sonnet", Difficulty.HIGH)
            ),
        )
        screen._save_settings()
        await wait_for(pilot, lambda: bool(service.updates))

        config, revision = service.updates[-1]
        assert revision == 1
        assert config.primary_driver is DriverId.CLAUDE_CLI
        saved_roles = {
            item.task: (item.model, item.difficulty) for item in config.tasks
        }
        assert saved_roles == {
            **expected,
            TaskKind.REPORTING: ("sonnet", Difficulty.HIGH),
        }
        assert all(item.driver is DriverId.CLAUDE_CLI for item in config.tasks)
        assert config.preference_for(DriverId.CLAUDE_CLI).model is None
        assert config.preference_for(DriverId.CODEX_CLI).model is None


@async_test
async def test_dirty_machine_role_team_guards_navigation_and_can_discard() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-primary-driver", Select).disabled,
        )
        screen.query_one("#ai-primary-driver", Select).value = DriverId.CLAUDE_CLI.value
        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts.get(TaskKind.PROOF)
                == ("best", Difficulty.HIGH)
            ),
        )

        screen.action_back()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, UnsavedAISettingsConfirmationScreen)
                and bool(app.screen.query("#ai-unsaved-continue").nodes)
            ),
        )
        app.screen.query_one("#ai-unsaved-continue", Button).press()
        await wait_for(pilot, lambda: app.screen is screen)
        assert screen._machine_draft_is_dirty()

        screen.action_back()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, UnsavedAISettingsConfirmationScreen)
                and bool(app.screen.query("#ai-unsaved-discard").nodes)
            ),
        )
        app.screen.query_one("#ai-unsaved-discard", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        assert service.updates == []


@async_test
async def test_dirty_machine_role_team_can_save_then_navigate() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-primary-driver", Select).disabled,
        )
        screen.query_one("#ai-primary-driver", Select).value = DriverId.CLAUDE_CLI.value
        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts.get(TaskKind.PROOF)
                == ("best", Difficulty.HIGH)
            ),
        )

        screen.action_back()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, UnsavedAISettingsConfirmationScreen)
                and bool(app.screen.query("#ai-unsaved-save").nodes)
            ),
        )
        app.screen.query_one("#ai-unsaved-save", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        assert service.updates[-1][0].primary_driver is DriverId.CLAUDE_CLI


@async_test
async def test_machine_edits_made_during_save_remain_dirty_and_visible() -> None:
    service = ProviderWorkflowFake(_snapshot())
    service.machine_update_release = threading.Event()
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: set(screen._machine_role_drafts) == set(TaskKind))
        screen._machine_role_drafts[TaskKind.REPORTING] = (
            "gpt-5.6-terra",
            Difficulty.HIGH,
        )
        screen._render_machine_role_choices()
        screen.action_back()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, UnsavedAISettingsConfirmationScreen)
                and bool(app.screen.query("#ai-unsaved-save").nodes)
            ),
        )
        app.screen.query_one("#ai-unsaved-save", Button).press()
        await wait_for(pilot, service.machine_update_started.is_set)

        screen._machine_role_drafts[TaskKind.REPORTING] = (
            "gpt-5.6-luna",
            Difficulty.LOW,
        )
        screen._render_machine_role_choices()
        service.machine_update_release.set()
        await wait_for(pilot, lambda: not screen._machine_save_in_flight)

        assert app.screen is screen
        saved = {
            item.task: (item.model, item.difficulty)
            for item in service.updates[-1][0].tasks
        }
        assert saved[TaskKind.REPORTING] == (
            "gpt-5.6-terra",
            Difficulty.HIGH,
        )
        assert screen._machine_role_drafts[TaskKind.REPORTING] == (
            "gpt-5.6-luna",
            Difficulty.LOW,
        )
        assert screen._machine_draft_is_dirty()
        assert "newer edits remain unsaved" in notice_text(screen)


@async_test
async def test_refresh_guard_preserves_unsaved_machine_and_project_drafts() -> None:
    project = Path("/test/refresh-guard-project")
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 52)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: (
                set(screen._machine_role_drafts) == set(TaskKind)
                and screen.project_settings is not None
            ),
        )

        machine_draft = ("gpt-5.6-terra", Difficulty.HIGH)
        screen._machine_role_drafts[TaskKind.REPORTING] = machine_draft
        screen.action_refresh()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, UnsavedAISettingsConfirmationScreen)
                and bool(app.screen.query("#ai-unsaved-continue").nodes)
            ),
        )
        app.screen.query_one("#ai-unsaved-continue", Button).press()
        await wait_for(pilot, lambda: app.screen is screen)
        assert screen._machine_role_drafts[TaskKind.REPORTING] == machine_draft

        screen._restore_scope_draft(SettingsScopeKind.MACHINE)
        screen.query_one("#settings-scope", Select).value = SettingsScopeKind.PROJECT
        screen.query_one("#customize-project-ai", Button).press()
        await wait_for(
            pilot,
            lambda: not screen.query_one("#project-ai-driver", Select).disabled,
        )
        project_draft = ("gpt-5.6-terra", Difficulty.HIGH)
        screen._project_role_drafts[TaskKind.REPORTING] = project_draft
        screen.action_refresh()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, UnsavedAISettingsConfirmationScreen)
                and bool(app.screen.query("#ai-unsaved-continue").nodes)
            ),
        )
        app.screen.query_one("#ai-unsaved-continue", Button).press()
        await wait_for(pilot, lambda: app.screen is screen)
        assert screen._project_role_drafts[TaskKind.REPORTING] == project_draft


@async_test
async def test_recommended_defaults_notice_advances_from_loading_to_success() -> None:
    service = ProviderWorkflowFake(_snapshot())
    service.recommended_defaults_release = threading.Event()
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-use-recommended", Button).disabled,
        )
        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(pilot, service.recommended_defaults_started.is_set)
        assert (
            "Loading recommended OpenAI Codex CLI defaults for every role"
            in notice_text(screen)
        )
        service.recommended_defaults_release.set()
        await wait_for(
            pilot,
            lambda: (
                "Recommended defaults loaded for all eight roles."
                in notice_text(screen)
            ),
        )


@async_test
async def test_stale_machine_provider_defaults_cannot_overwrite_newer_provider() -> (
    None
):
    service = ProviderWorkflowFake(_snapshot())
    claude_release = threading.Event()
    service.recommended_defaults_release_by_driver[DriverId.CLAUDE_CLI] = claude_release
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-primary-driver", Select).disabled,
        )

        screen.query_one("#ai-primary-driver", Select).value = DriverId.CLAUDE_CLI.value
        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            service.recommended_defaults_started_by_driver[DriverId.CLAUDE_CLI].is_set,
        )
        screen.query_one("#ai-primary-driver", Select).value = DriverId.CODEX_CLI.value
        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts.get(TaskKind.PROOF)
                == ("gpt-5.6-sol", Difficulty.HIGH)
            ),
        )

        claude_release.set()
        await wait_for(
            pilot,
            service.recommended_defaults_completed_by_driver[
                DriverId.CLAUDE_CLI
            ].is_set,
        )
        await pilot.pause()
        assert screen.query_one("#ai-primary-driver", Select).value == "codex_cli"
        assert screen._machine_role_drafts[TaskKind.PROOF] == (
            "gpt-5.6-sol",
            Difficulty.HIGH,
        )


@async_test
async def test_stale_project_provider_defaults_cannot_overwrite_newer_provider() -> (
    None
):
    project = Path("/test/race-project")
    service = ProviderWorkflowFake(_snapshot())
    claude_release = threading.Event()
    service.recommended_defaults_release_by_driver[DriverId.CLAUDE_CLI] = claude_release
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: (
                screen.project_settings is not None
                and not screen.query_one("#customize-project-ai", Button).disabled
            ),
        )
        assert screen.query_one("#project-ai-driver", Select).disabled
        screen.query_one("#customize-project-ai", Button).press()
        await wait_for(
            pilot,
            lambda: not screen.query_one("#project-ai-driver", Select).disabled,
        )

        project_drafts = dict(screen._project_role_drafts)
        screen.query_one("#project-ai-driver", Select).value = DriverId.CLAUDE_CLI.value
        await wait_for(
            pilot,
            lambda: (
                screen.query_one("#project-ai-role-model", Select).value
                == "__needs_update__"
                and screen.query_one("#save-project-ai", Button).disabled
            ),
        )
        assert screen._project_role_drafts == project_drafts
        defaults = screen.query_one("#project-ai-use-recommended", Button)
        assert "Claude Code CLI" in defaults.label.plain
        assert "all 8 roles" in defaults.label.plain
        screen.query_one("#project-ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            service.recommended_defaults_started_by_driver[DriverId.CLAUDE_CLI].is_set,
        )
        screen.query_one("#project-ai-driver", Select).value = DriverId.CODEX_CLI.value
        screen.query_one("#project-ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen._project_role_drafts.get(TaskKind.PROOF)
                == ("gpt-5.6-sol", Difficulty.HIGH)
            ),
        )

        claude_release.set()
        await wait_for(
            pilot,
            service.recommended_defaults_completed_by_driver[
                DriverId.CLAUDE_CLI
            ].is_set,
        )
        await pilot.pause()
        assert screen.query_one("#project-ai-driver", Select).value == "codex_cli"
        assert screen._project_role_drafts[TaskKind.PROOF] == (
            "gpt-5.6-sol",
            Difficulty.HIGH,
        )


@async_test
async def test_initial_policy_load_cannot_overwrite_newer_provider_defaults() -> None:
    service = ProviderWorkflowFake(_snapshot())
    service.initial_policies_release = threading.Event()
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, service.initial_policies_started.is_set)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-primary-driver", Select).disabled,
        )

        screen.query_one("#ai-primary-driver", Select).value = DriverId.CLAUDE_CLI.value
        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            service.recommended_defaults_completed_by_driver[
                DriverId.CLAUDE_CLI
            ].is_set,
        )
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts.get(TaskKind.DUPLICATE_PROOF)
                == ("fable", Difficulty.XHIGH)
            ),
        )

        service.initial_policies_release.set()
        await wait_for(pilot, service.initial_policies_completed.is_set)
        await pilot.pause()
        assert screen.query_one("#ai-primary-driver", Select).value == "claude_cli"
        assert len(screen._machine_role_drafts) == len(TaskKind) == 8
        assert screen._machine_role_drafts[TaskKind.DUPLICATE_PROOF] == (
            "fable",
            Difficulty.XHIGH,
        )
        assert screen._machine_draft_is_dirty()


@async_test
async def test_connection_draft_is_guarded_and_survives_status_reload() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await show_ai_settings_view(pilot, screen, "connection")
        await wait_for(
            pilot,
            lambda: (
                screen._provider_controls_ready
                and not screen.query_one("#ai-provider-model", Select).disabled
                and bool(screen.query("#ai-api-key").nodes)
            ),
        )
        await pilot.pause()
        model = screen.query_one("#ai-provider-model", Select)
        model.value = "gpt-5.6-sol"
        await wait_for(pilot, screen._machine_draft_is_dirty)

        navigation = screen.query_one("#ai-settings-nav", OptionList)
        navigation.highlighted = 2
        navigation.focus()
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#ai-unsaved-continue").nodes),
        )
        assert (
            screen.query_one("#ai-settings-pages", ContentSwitcher).current
            == "connection-page"
        )
        app.screen.query_one("#ai-unsaved-continue", Button).press()
        await wait_for(pilot, lambda: app.screen is screen)
        assert model.value == "gpt-5.6-sol"

        screen._record_setup_and_reload(service.snapshot)
        await pilot.pause()
        assert model.value == "gpt-5.6-sol"
        assert screen._machine_draft_is_dirty()

        base_revision = screen._machine_draft_base_revision
        service.snapshot = replace(
            service.snapshot,
            settings=replace(
                service.snapshot.settings,
                revision=base_revision + 1,
            ),
        )
        screen._record_setup_and_reload(service.snapshot)
        await pilot.pause()
        assert screen.snapshot is not None
        assert screen.snapshot.settings.revision == base_revision + 1
        assert screen._machine_draft_base_revision == base_revision
        assert model.value == "gpt-5.6-sol"
        screen.query_one("#save-ai-settings", Button).press()
        await wait_for(
            pilot,
            lambda: (
                not screen._machine_save_in_flight
                and "revision changed" in notice_text(screen)
            ),
        )
        assert service.updates == []

        reads_before = service.setup_reads
        screen.action_refresh()
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#ai-unsaved-discard").nodes),
        )
        assert service.setup_reads == reads_before
        app.screen.query_one("#ai-unsaved-discard", Button).press()
        await wait_for(pilot, lambda: service.setup_reads > reads_before)
        await wait_for(pilot, lambda: not screen._machine_draft_is_dirty())
        assert model.value != "gpt-5.6-sol"


@async_test
async def test_project_connection_page_ctrl_s_saves_machine_settings() -> None:
    project = Path("/test/project-connection-save")
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: screen.project_settings is not None)
        screen.query_one("#settings-scope", Select).value = SettingsScopeKind.PROJECT
        await wait_for(pilot, lambda: screen._active_scope is SettingsScopeKind.PROJECT)
        screen.query_one("#project-ai-manage-connection", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen.query_one("#ai-settings-pages", ContentSwitcher).current
                == "connection-page"
            ),
        )
        await wait_for(
            pilot,
            lambda: (
                screen._provider_controls_ready
                and not screen.query_one("#ai-provider-model", Select).disabled
            ),
        )
        screen.query_one("#ai-provider-model", Select).value = "gpt-5.6-terra"
        screen.query_one("#ai-provider-difficulty", Select).value = "high"
        await wait_for(pilot, screen._machine_draft_is_dirty)

        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: bool(service.updates))
        assert service.project_updates == []
        assert service.updates[-1][0].preference_for(DriverId.CODEX_CLI).model == (
            "gpt-5.6-terra"
        )


@async_test
async def test_project_connection_page_visible_save_button_saves_machine_settings() -> (
    None
):
    project = Path("/test/project-connection-button-save")
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: screen.project_settings is not None)
        screen.query_one("#settings-scope", Select).value = SettingsScopeKind.PROJECT
        await wait_for(pilot, lambda: screen._active_scope is SettingsScopeKind.PROJECT)
        screen.query_one("#project-ai-manage-connection", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen.query_one("#ai-settings-pages", ContentSwitcher).current
                == "connection-page"
            ),
        )
        await wait_for(
            pilot,
            lambda: (
                screen._provider_controls_ready
                and not screen.query_one("#ai-provider-model", Select).disabled
                and bool(screen.query("#ai-api-key").nodes)
            ),
        )
        await pilot.pause()
        screen.query_one("#ai-provider-model", Select).value = "gpt-5.6-terra"
        screen.query_one("#ai-provider-difficulty", Select).value = "high"
        await wait_for(pilot, screen._machine_draft_is_dirty)

        machine_save = screen.query_one("#save-ai-settings", Button)
        project_save = screen.query_one("#save-project-ai", Button)
        assert machine_save.display
        assert not project_save.display
        machine_save.press()
        await wait_for(pilot, lambda: bool(service.updates))
        assert service.project_updates == []
        assert service.updates[-1][0].preference_for(DriverId.CODEX_CLI).model == (
            "gpt-5.6-terra"
        )


@async_test
async def test_project_connection_page_ctrl_q_guards_machine_settings() -> None:
    project = Path("/test/project-connection-quit")
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]
    quit_requested = asyncio.Event()
    app.exit = lambda *args, **kwargs: quit_requested.set()  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: screen.project_settings is not None)
        screen.query_one("#settings-scope", Select).value = SettingsScopeKind.PROJECT
        await wait_for(pilot, lambda: screen._active_scope is SettingsScopeKind.PROJECT)
        screen.query_one("#project-ai-manage-connection", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen.query_one("#ai-settings-pages", ContentSwitcher).current
                == "connection-page"
            ),
        )
        await wait_for(
            pilot,
            lambda: (
                screen._provider_controls_ready
                and not screen.query_one("#ai-provider-model", Select).disabled
                and bool(screen.query("#ai-api-key").nodes)
            ),
        )
        await pilot.pause()
        screen.query_one("#ai-provider-model", Select).value = "gpt-5.6-terra"
        screen.query_one("#ai-provider-difficulty", Select).value = "high"
        await wait_for(pilot, screen._machine_draft_is_dirty)

        await pilot.press("ctrl+q")
        await wait_for(
            pilot, lambda: isinstance(app.screen, UnsavedAISettingsConfirmationScreen)
        )
        assert not quit_requested.is_set()
        dialog = app.screen
        assert isinstance(dialog, UnsavedAISettingsConfirmationScreen)
        assert dialog.scope_label == "machine"
        dialog.query_one("#ai-unsaved-save", Button).press()
        await wait_for(pilot, quit_requested.is_set)
        assert service.project_updates == []
        assert len(service.updates) == 1


@async_test
async def test_machine_defaults_completion_cannot_overwrite_newer_role_edit() -> None:
    service = ProviderWorkflowFake(_snapshot())
    service.recommended_defaults_release = threading.Event()
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot, lambda: not screen.query_one("#ai-use-recommended", Button).disabled
        )
        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(pilot, service.recommended_defaults_started.is_set)
        screen.query_one("#ai-role-task", Select).value = TaskKind.REPORTING.value
        await pilot.pause()
        screen.query_one("#ai-role-model", Select).value = "gpt-5.6-terra"
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts[TaskKind.REPORTING][0] == "gpt-5.6-terra"
            ),
        )
        edited = dict(screen._machine_role_drafts)
        service.recommended_defaults_release.set()
        await wait_for(
            pilot,
            service.recommended_defaults_completed_by_driver[DriverId.CODEX_CLI].is_set,
        )
        await pilot.pause()
        assert screen._machine_role_drafts == edited
        assert "not applied" in notice_text(screen)


@async_test
async def test_project_defaults_completion_cannot_overwrite_newer_role_edit() -> None:
    project = Path("/test/project-default-edit-race")
    service = ProviderWorkflowFake(_snapshot())
    service.recommended_defaults_release = threading.Event()
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: screen.project_settings is not None)
        screen.query_one("#customize-project-ai", Button).press()
        await wait_for(
            pilot,
            lambda: (
                not screen.query_one("#project-ai-use-recommended", Button).disabled
            ),
        )
        screen.query_one("#project-ai-use-recommended", Button).press()
        await wait_for(pilot, service.recommended_defaults_started.is_set)
        screen.query_one("#project-ai-role", Select).value = TaskKind.REPORTING.value
        await pilot.pause()
        screen.query_one("#project-ai-role-model", Select).value = "gpt-5.6-terra"
        await wait_for(
            pilot,
            lambda: (
                screen._project_role_drafts[TaskKind.REPORTING][0] == "gpt-5.6-terra"
            ),
        )
        edited = dict(screen._project_role_drafts)
        service.recommended_defaults_release.set()
        await wait_for(
            pilot,
            service.recommended_defaults_completed_by_driver[DriverId.CODEX_CLI].is_set,
        )
        await pilot.pause()
        assert screen._project_role_drafts == edited
        assert "not applied" in notice_text(screen)


@async_test
async def test_provider_switch_preserves_overlapping_model_with_unsupported_effort() -> (
    None
):
    claude_catalog = ModelCatalog(
        DriverId.CLAUDE_CLI,
        (ModelDescriptor("gpt-5.6-sol", "Overlap", (Difficulty.AUTO,)),),
        DiscoverySource.LIVE_ACCOUNT,
        "Overlapping test catalog.",
        True,
    )
    statuses = tuple(
        replace(_status(driver), catalog=claude_catalog)
        if driver is DriverId.CLAUDE_CLI
        else _status(driver)
        for driver in DriverId
    )
    for project in (False, True):
        service = ProviderWorkflowFake(_snapshot(statuses=statuses))
        path = Path("/test/overlap") if project else None
        app = ProofAssistantApp(service)  # type: ignore[arg-type]

        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
            app.show_ai_provider_settings(project=path)
            await wait_for(
                pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen)
            )
            screen = app.screen
            assert isinstance(screen, AIProviderSettingsScreen)
            if project:
                await wait_for(pilot, lambda: screen.project_settings is not None)
                screen.query_one("#customize-project-ai", Button).press()
                await wait_for(
                    pilot,
                    lambda: not screen.query_one("#project-ai-driver", Select).disabled,
                )
                before = dict(screen._project_role_drafts)
                screen.query_one("#project-ai-driver", Select).value = "claude_cli"
                await wait_for(
                    pilot,
                    lambda: (
                        screen.query_one("#project-ai-role-difficulty", Select).value
                        == "__needs_update__"
                    ),
                )
                assert screen._project_role_drafts == before
            else:
                await wait_for(
                    pilot,
                    lambda: not screen.query_one("#ai-primary-driver", Select).disabled,
                )
                before = dict(screen._machine_role_drafts)
                screen.query_one("#ai-primary-driver", Select).value = "claude_cli"
                await wait_for(
                    pilot,
                    lambda: (
                        screen.query_one("#ai-role-difficulty", Select).value
                        == "__needs_update__"
                    ),
                )
                assert screen._machine_role_drafts == before


@async_test
async def test_invalid_recommended_default_matrix_never_mutates_draft() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: set(screen._machine_role_drafts) == set(TaskKind))
        for corruption in (
            "wrong_driver",
            "missing_task",
            "duplicate_task",
            "unknown_model",
            "bad_effort",
        ):
            before = dict(screen._machine_role_drafts)
            policies = list(service.ai_task_policies(driver=DriverId.CODEX_CLI))
            if corruption == "wrong_driver":
                policies[0] = replace(policies[0], driver=DriverId.CLAUDE_CLI)
            elif corruption == "missing_task":
                policies.pop()
            elif corruption == "duplicate_task":
                policies[-1] = replace(policies[-1], task=policies[0].task)
            elif corruption == "unknown_model":
                policies[0] = replace(policies[0], model="not-in-catalog")
            else:
                policies[0] = replace(policies[0], difficulty=Difficulty.MEDIUM)
            notice_generation = screen._begin_notice("Testing invalid defaults…")
            screen._apply_recommended_role_policies(
                tuple(policies),
                False,
                notice_generation,
                screen._machine_defaults_generation,
                screen._machine_draft_generation,
                DriverId.CODEX_CLI,
            )
            await pilot.pause()
            assert screen._machine_role_drafts == before
            assert "invalid recommended defaults" in notice_text(screen)


@async_test
async def test_project_save_backend_guard_rejects_provider_that_is_not_ready() -> None:
    project = Path("/test/not-ready-project")
    statuses = tuple(
        _status(driver, authentication=AuthenticationState.REQUIRED)
        if driver is DriverId.CLAUDE_CLI
        else _status(driver)
        for driver in DriverId
    )
    service = ProviderWorkflowFake(_snapshot(statuses=statuses))
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: screen.project_settings is not None)
        screen.query_one("#customize-project-ai", Button).press()
        screen.query_one("#project-ai-driver", Select).value = "claude_cli"
        screen._project_role_drafts = {
            policy.task: (policy.model or "", policy.difficulty)
            for policy in service.ai_task_policies(driver=DriverId.CLAUDE_CLI)
        }
        assert not screen._save_project_settings()
        assert service.project_updates == []
        assert "not ready" in notice_text(screen)


@async_test
async def test_newer_f2_notice_survives_stale_defaults_completion() -> None:
    statuses = tuple(
        _status(driver, installed=False)
        if driver is DriverId.CODEX_CLI
        else _status(driver)
        for driver in DriverId
    )
    service = ProviderWorkflowFake(
        _snapshot(ready=False, revision=0, statuses=statuses)
    )
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await settle_screen(pilot)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-use-recommended", Button).disabled,
        )
        service.recommended_defaults_release = threading.Event()
        screen._machine_role_drafts[TaskKind.PROOF] = (
            "gpt-5.6-terra",
            Difficulty.HIGH,
        )

        screen.query_one("#ai-use-recommended", Button).press()
        await wait_for(pilot, service.recommended_defaults_started.is_set)
        app.action_main_menu()
        await wait_for(
            pilot,
            lambda: (
                "Finish primary AI setup and review the complete eight-role team"
                in notice_text(screen)
            ),
        )
        service.recommended_defaults_release.set()
        await wait_for(
            pilot,
            lambda: (
                screen._machine_role_drafts[TaskKind.PROOF]
                == ("gpt-5.6-sol", Difficulty.HIGH)
            ),
        )
        assert (
            "Finish primary AI setup and review the complete eight-role team"
            in notice_text(screen)
        )


@async_test
async def test_project_provider_override_is_isolated_and_can_reset_to_inheritance() -> (
    None
):
    project = Path("/test/managed-project")
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 52)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: (
                screen.project_settings is not None
                and not screen.query_one("#customize-project-ai", Button).disabled
            ),
        )

        loaded = screen.project_settings
        assert loaded is not None and loaded.inherited
        assert screen.query_one("#project-ai-driver", Select).disabled
        assert loaded.effective.ai_driver == DriverId.CODEX_CLI.value
        assert screen.query_one("#project-ai-driver", Select).value == "codex_cli"
        assert_select_accepts(
            screen.query_one("#project-ai-role", Select),
            {task.value for task in TaskKind},
        )
        assert all(
            f"{task.value.replace('_', ' ')}"
            in screen.query_one("#project-ai-summary", TextArea).text.casefold()
            or f"[{task.value}]" in screen.query_one("#ai-task-policies", TextArea).text
            for task in TaskKind
        )
        screen.query_one("#settings-scope", Select).value = SettingsScopeKind.PROJECT
        await pilot.pause()
        screen.query_one("#customize-project-ai", Button).press()
        await wait_for(
            pilot,
            lambda: not screen.query_one("#project-ai-driver", Select).disabled,
        )

        screen.query_one("#project-ai-driver", Select).value = DriverId.CLAUDE_CLI.value
        screen.query_one("#project-ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: (
                set(screen._project_role_drafts) == set(TaskKind)
                and screen._project_role_drafts[TaskKind.PROOF]
                == ("best", Difficulty.HIGH)
                and screen._project_role_drafts[TaskKind.REPORTING]
                == ("haiku", Difficulty.LOW)
            ),
        )
        project_role = screen.query_one("#project-ai-role", Select)
        project_roster = screen.query_one("#project-ai-role-roster", DataTable)
        await wait_for(pilot, lambda: project_roster.row_count == len(TaskKind))
        project_roster.focus()
        project_roster.move_cursor(
            row=list(TaskKind).index(TaskKind.REPORTING), column=0, animate=False
        )
        await pilot.pause()
        await wait_for(
            pilot,
            lambda: (
                project_role.value == TaskKind.REPORTING.value
                and screen.query_one("#project-ai-role-model", Select).value == "haiku"
            ),
        )
        screen.query_one("#project-ai-role-model", Select).value = "fable"
        await wait_for(
            pilot,
            lambda: screen._project_role_drafts[TaskKind.REPORTING][0] == "fable",
        )
        assert_select_accepts(
            screen.query_one("#project-ai-role-difficulty", Select),
            {
                difficulty.value
                for difficulty in _catalog(DriverId.CLAUDE_CLI).models[1].difficulties
            },
        )
        screen.query_one("#project-ai-role-difficulty", Select).value = "high"
        await wait_for(
            pilot,
            lambda: (
                screen._project_role_drafts[TaskKind.REPORTING]
                == ("fable", Difficulty.HIGH)
            ),
        )
        screen.action_save()
        await wait_for(pilot, lambda: bool(service.project_updates))

        saved_project, override, revision = service.project_updates[-1]
        assert (saved_project, revision) == (project, 0)
        assert override.ai_driver is DriverId.CLAUDE_CLI
        assert override.complete
        assert override.role_for(TaskKind.PROOF) == ProjectAIRoleOverride(
            TaskKind.PROOF, "best", Difficulty.HIGH
        )
        assert override.role_for(TaskKind.REPORTING) == ProjectAIRoleOverride(
            TaskKind.REPORTING, "fable", Difficulty.HIGH
        )
        assert service.updates == []
        assert service.snapshot.primary_driver is DriverId.CODEX_CLI
        assert service.project_snapshots[project].effective.ai_driver == "claude_cli"
        assert service.project_snapshots[project].effective.model == "best"
        assert (
            service.project_snapshots[project]
            .effective.for_task(TaskKind.REPORTING)
            .model
            == "fable"
        )

        await wait_for(
            pilot,
            lambda: not screen.query_one("#reset-project-ai", Button).disabled,
        )
        screen.query_one("#reset-project-ai", Button).press()
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, ProjectInheritanceConfirmationScreen),
        )
        dialog = app.screen
        assert isinstance(dialog, ProjectInheritanceConfirmationScreen)
        dialog.query_one("#project-inheritance-confirm", Button).press()
        await wait_for(pilot, lambda: bool(service.project_resets))
        reset = service.project_snapshots[project]
        assert service.project_resets == [(project, 1)]
        assert reset.inherited
        assert reset.revision == 2
        assert reset.effective.ai_driver == DriverId.CODEX_CLI.value
        assert reset.effective.model == "gpt-5.6-sol"


@async_test
async def test_switching_project_provider_removes_stale_codex_model_ids() -> None:
    project = Path("/test/managed-project")
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 52)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: (
                screen.project_settings is not None
                and screen.query_one("#project-ai-role-model", Select).value
                == "gpt-5.6-sol"
            ),
        )
        screen.query_one("#customize-project-ai", Button).press()
        await wait_for(
            pilot,
            lambda: not screen.query_one("#project-ai-driver", Select).disabled,
        )

        screen.query_one("#project-ai-driver", Select).value = DriverId.CLAUDE_CLI.value
        screen.query_one("#project-ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: screen.query_one("#project-ai-role-model", Select).value == "best",
        )
        model_select = screen.query_one("#project-ai-role-model", Select)
        assert_select_accepts(
            model_select, {"best", "fable", "opus", "sonnet", "haiku"}
        )
        with pytest.raises(InvalidSelectValueError):
            model_select.value = "gpt-5.6-sol"
        with pytest.raises(InvalidSelectValueError):
            model_select.value = "gpt-5.6-terra"


@async_test
async def test_project_provider_defaults_undo_restores_exact_project_draft() -> None:
    project = Path("/test/project-defaults-undo")
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: (
                screen.project_settings is not None
                and not screen.query_one("#customize-project-ai", Button).disabled
                and set(screen._machine_role_drafts) == set(TaskKind)
            ),
        )
        machine_before = dict(screen._machine_role_drafts)
        screen.query_one("#settings-scope", Select).value = SettingsScopeKind.PROJECT
        screen.query_one("#customize-project-ai", Button).press()
        await wait_for(
            pilot,
            lambda: not screen.query_one("#project-ai-driver", Select).disabled,
        )
        screen.query_one("#project-ai-driver", Select).value = DriverId.CLAUDE_CLI.value
        await pilot.pause()
        project_before = dict(screen._project_role_drafts)

        screen.query_one("#project-ai-use-recommended", Button).press()
        await wait_for(
            pilot,
            lambda: (
                screen._project_role_drafts.get(TaskKind.DUPLICATE_PROOF)
                == ("fable", Difficulty.XHIGH)
                and not screen.query_one(
                    "#project-ai-undo-recommended", Button
                ).disabled
            ),
        )
        screen.query_one("#project-ai-undo-recommended", Button).press()
        await wait_for(pilot, lambda: screen._project_role_drafts == project_before)
        assert screen._machine_role_drafts == machine_before
        assert screen.query_one("#project-ai-undo-recommended", Button).disabled


@async_test
async def test_project_edits_made_during_save_remain_dirty_and_visible() -> None:
    project = Path("/test/save-race-project")
    service = ProviderWorkflowFake(_snapshot())
    service.project_update_release = threading.Event()
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 52)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: (
                screen.project_settings is not None
                and not screen.query_one("#customize-project-ai", Button).disabled
            ),
        )
        screen.query_one("#settings-scope", Select).value = SettingsScopeKind.PROJECT
        screen.query_one("#customize-project-ai", Button).press()
        await wait_for(
            pilot,
            lambda: not screen.query_one("#project-ai-driver", Select).disabled,
        )
        screen._project_role_drafts[TaskKind.REPORTING] = (
            "gpt-5.6-terra",
            Difficulty.HIGH,
        )
        screen._render_project_ai_choices()

        screen.action_back()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, UnsavedAISettingsConfirmationScreen)
                and bool(app.screen.query("#ai-unsaved-save").nodes)
            ),
        )
        app.screen.query_one("#ai-unsaved-save", Button).press()
        await wait_for(pilot, service.project_update_started.is_set)

        screen._project_role_drafts[TaskKind.REPORTING] = (
            "gpt-5.6-luna",
            Difficulty.LOW,
        )
        screen._render_project_ai_choices()
        service.project_update_release.set()
        await wait_for(pilot, lambda: not screen._project_save_in_flight)

        assert app.screen is screen
        saved_override = service.project_updates[-1][1]
        assert saved_override.role_for(TaskKind.REPORTING) == ProjectAIRoleOverride(
            TaskKind.REPORTING, "gpt-5.6-terra", Difficulty.HIGH
        )
        assert screen._project_role_drafts[TaskKind.REPORTING] == (
            "gpt-5.6-luna",
            Difficulty.LOW,
        )
        assert screen._project_draft_is_dirty()
        assert "newer edits remain unsaved" in notice_text(screen)


@async_test
async def test_later_provider_degradation_keeps_project_landing_accessible() -> None:
    service = ProviderWorkflowFake(_snapshot(ready=False, revision=4))
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, WelcomeScreen)
                and bool(app.screen.query("#landing-ai-provider-status").nodes)
                and "NOT READY"
                in app.screen.query_one("#landing-ai-provider-status", TextArea).text
            ),
        )
        assert isinstance(app.screen, WelcomeScreen)
        assert app.screen.query_one("#new-project", Button).disabled
        assert not app.screen.query_one("#refresh-projects", Button).disabled
        assert bool(app.screen.query("#project-list").nodes)


@async_test
async def test_api_key_is_one_shot_and_never_remains_in_dom_or_screen_repr() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-configure-driver", Select).disabled,
        )
        screen.query_one(
            "#ai-configure-driver", Select
        ).value = DriverId.OPENAI_API.value
        await pilot.pause()
        secret = "sk-test-never-retain"
        key_input = screen.query_one("#ai-api-key", Input)
        key_input.value = secret
        screen._store_credential()
        assert key_input.value == ""
        await wait_for(pilot, lambda: service.secret_was_received)
        assert service.credential_calls == [
            (DriverId.OPENAI_API, CredentialSource.CREDENTIAL_STORE)
        ]
        displayed = "\n".join(
            [node.value for node in screen.query(Input)]
            + [node.text for node in screen.query(TextArea)]
        )
        assert secret not in displayed
        assert secret not in repr(screen)
        assert (
            "credential_store" in screen.query_one("#ai-auth-next-step", TextArea).text
        )
        screen._review_delete_credential()
        await wait_for(
            pilot,
            lambda: (
                bool(app.screen.query("#settings-destructive-cancel").nodes)
                and app.screen.focused
                is app.screen.query_one("#settings-destructive-cancel", Button)
            ),
        )
        assert service.credential_deleted == []
        assert app.screen.focused is app.screen.query_one(
            "#settings-destructive-cancel", Button
        )
        await pilot.press("enter")
        await wait_for(pilot, lambda: app.screen is screen)
        assert service.credential_deleted == []

        screen._review_delete_credential()
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-destructive-cancel").nodes),
        )
        app.action_main_menu()
        await wait_for(pilot, lambda: app.screen is screen)
        assert service.credential_deleted == []

        screen._review_delete_credential()
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-destructive-confirm").nodes),
        )
        app.screen.query_one("#settings-destructive-confirm", Button).press()
        await wait_for(pilot, lambda: bool(service.credential_deleted))
        assert service.credential_deleted == [
            (DriverId.OPENAI_API, CredentialSource.CREDENTIAL_STORE)
        ]


@async_test
async def test_api_key_is_destroyed_when_leaving_provider_connection() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: bool(screen.query("#ai-settings-nav").nodes))
        await show_ai_settings_view(pilot, screen, "connection")
        screen.query_one(
            "#ai-configure-driver", Select
        ).value = DriverId.OPENAI_API.value
        await wait_for(
            pilot,
            lambda: (
                bool(screen.query("#ai-api-key").nodes)
                and not screen.query_one("#ai-api-key", Input).disabled
            ),
        )
        sentinel = "sk-navigation-secret-must-disappear"
        screen.query_one("#ai-api-key", Input).value = sentinel

        await show_ai_settings_view(pilot, screen, "roles")
        await wait_for(pilot, lambda: not screen.query("#ai-api-key").nodes)
        assert sentinel not in repr(screen)
        assert all(sentinel not in node.value for node in screen.query(Input))

        await show_ai_settings_view(pilot, screen, "connection")
        await wait_for(pilot, lambda: bool(screen.query("#ai-api-key").nodes))
        assert screen.query_one("#ai-api-key", Input).value == ""


@async_test
async def test_api_key_is_destroyed_across_back_and_global_navigation() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))

        async def open_with_secret(
            secret: str,
        ) -> tuple[AIProviderSettingsScreen, Input]:
            app.show_ai_provider_settings()
            await wait_for(
                pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen)
            )
            screen = app.screen
            assert isinstance(screen, AIProviderSettingsScreen)
            await wait_for(pilot, lambda: bool(screen.query("#ai-settings-nav").nodes))
            await show_ai_settings_view(pilot, screen, "connection")
            screen.query_one(
                "#ai-configure-driver", Select
            ).value = DriverId.OPENAI_API.value
            await wait_for(
                pilot,
                lambda: (
                    bool(screen.query("#ai-api-key").nodes)
                    and not screen.query_one("#ai-api-key", Input).disabled
                ),
            )
            key_input = screen.query_one("#ai-api-key", Input)
            key_input.value = secret
            return screen, key_input

        screen, retained_input = await open_with_secret("secret-back")
        screen.action_back()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        assert retained_input.value == ""

        _screen, retained_input = await open_with_secret("secret-f2")
        app.action_main_menu()
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        assert retained_input.value == ""

        _screen, retained_input = await open_with_secret("secret-f3")
        app.action_global_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
        assert retained_input.value == ""


@async_test
async def test_install_plan_is_exact_cancel_first_and_only_backend_executes() -> None:
    statuses = tuple(
        _status(driver, installed=False)
        if driver is DriverId.CLAUDE_CLI
        else _status(
            driver,
            authentication=(
                AuthenticationState.UNKNOWN
                if driver is DriverId.COPILOT_CLI
                else AuthenticationState.REQUIRED
                if driver in _API_DRIVERS_FOR_TEST
                else AuthenticationState.AUTHENTICATED
            ),
        )
        for driver in DriverId
    )
    service = ProviderWorkflowFake(_snapshot(statuses=statuses))
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        await settle_screen(pilot)
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-configure-driver", Select).disabled,
        )
        screen.query_one(
            "#ai-configure-driver", Select
        ).value = DriverId.CLAUDE_CLI.value
        await pilot.pause()
        screen._preview_install()
        await wait_for(
            pilot, lambda: isinstance(app.screen, AIInstallConfirmationScreen)
        )
        dialog = app.screen
        assert isinstance(dialog, AIInstallConfirmationScreen)
        await wait_for(
            pilot,
            lambda: dialog.query_one("#ai-install-cancel", Button).has_focus,
        )
        command_text = dialog.query_one("#ai-install-commands", TextArea).text
        assert "@anthropic-ai/claude-code" in command_text
        assert service.install_calls == []
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        assert service.install_calls == []
        await settle_screen(pilot)

        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        screen._preview_install()
        await wait_for(
            pilot, lambda: isinstance(app.screen, AIInstallConfirmationScreen)
        )
        dialog = app.screen
        assert isinstance(dialog, AIInstallConfirmationScreen)
        dialog.action_confirm()
        await wait_for(pilot, lambda: bool(service.install_calls))
        plan, token = service.install_calls[-1]
        assert plan.commands == service.plan.commands
        assert token == plan.consent_token == "consent-test-token"


_API_DRIVERS_FOR_TEST = {
    DriverId.OPENAI_API,
    DriverId.ANTHROPIC_API,
    DriverId.GEMINI_API,
}


@async_test
async def test_copilot_account_check_is_never_automatic_and_requires_consent() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        assert service.account_verifications == []
        await settle_screen(pilot)
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-configure-driver", Select).disabled,
        )
        screen.query_one(
            "#ai-configure-driver", Select
        ).value = DriverId.COPILOT_CLI.value
        await pilot.pause()
        assert not screen.query_one("#verify-ai-account", Button).disabled
        assert service.account_verifications == []

        screen._review_account_verification()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, AIAccountVerificationConfirmationScreen)
                and bool(app.screen.query(".warning").nodes)
            ),
        )
        warning = app.screen.query_one(".warning", TextArea).text
        assert "one tiny harmless model request" in warning
        assert "never run automatically" in warning
        await wait_for(
            pilot,
            lambda: app.screen.query_one("#ai-account-check-cancel", Button).has_focus,
        )
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        assert service.account_verifications == []
        await settle_screen(pilot)

        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        screen._review_account_verification()
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, AIAccountVerificationConfirmationScreen),
        )
        dialog = app.screen
        assert isinstance(dialog, AIAccountVerificationConfirmationScreen)
        dialog.action_confirm()
        await wait_for(pilot, lambda: bool(service.account_verifications))
        assert service.account_verifications == [DriverId.COPILOT_CLI]


@async_test
async def test_overlapping_task_policy_loads_cannot_finish_out_of_order() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: set(screen._machine_role_drafts) == set(TaskKind))

        base = service.ai_task_policies()
        older = tuple(replace(policy, model="gpt-5.6-sol") for policy in base)
        newer = tuple(replace(policy, model="gpt-5.6-terra") for policy in base)
        older_started = threading.Event()
        older_release = threading.Event()
        load_calls = 0

        def overlapping_load(
            driver: DriverId | None = None,
        ) -> tuple[TaskModelPolicy, ...]:
            nonlocal load_calls
            assert driver is None
            load_calls += 1
            if load_calls == 1:
                older_started.set()
                if not older_release.wait(timeout=5):
                    raise AssertionError("timed out waiting to release old policies")
                return older
            return newer

        record_calls = 0
        original_record = screen._record_task_policies

        def tracking_record(
            policies: tuple[TaskModelPolicy, ...],
            load_generation: int,
            request_generation: int,
        ) -> None:
            nonlocal record_calls
            original_record(policies, load_generation, request_generation)
            record_calls += 1

        service.ai_task_policies = overlapping_load  # type: ignore[method-assign]
        screen._record_task_policies = tracking_record  # type: ignore[method-assign]
        screen._load_task_policies()
        await wait_for(pilot, older_started.is_set)
        screen._load_task_policies()
        await wait_for(
            pilot,
            lambda: screen._machine_role_drafts[TaskKind.PROOF][0] == "gpt-5.6-terra",
        )

        older_release.set()
        await wait_for(pilot, lambda: record_calls == 2)
        assert screen._machine_role_drafts[TaskKind.PROOF][0] == "gpt-5.6-terra"


@async_test
async def test_project_loads_reject_out_of_order_results_and_post_request_edits() -> (
    None
):
    project = Path("/test/project-load-generation")
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: screen.project_settings is not None)
        assert screen.project_settings is not None
        base = screen.project_settings

        def snapshot_with_model(
            revision: int, model: str
        ) -> ProjectVerificationSettingsSnapshot:
            roles = tuple(
                replace(role, model=model) for role in base.effective.role_settings
            )
            effective = replace(base.effective, model=model, role_settings=roles)
            return replace(base, revision=revision, effective=effective)

        older = snapshot_with_model(1, "gpt-5.6-sol")
        newer = snapshot_with_model(2, "gpt-5.6-terra")
        edited_stale = snapshot_with_model(3, "gpt-5.6-sol")
        first_started = threading.Event()
        first_release = threading.Event()
        edit_started = threading.Event()
        edit_release = threading.Event()
        load_calls = 0

        def overlapping_project_load(
            requested_project: Path,
        ) -> ProjectVerificationSettingsSnapshot:
            nonlocal load_calls
            assert requested_project == project
            load_calls += 1
            if load_calls == 1:
                first_started.set()
                if not first_release.wait(timeout=5):
                    raise AssertionError("timed out waiting to release old project")
                return older
            if load_calls == 2:
                return newer
            edit_started.set()
            if not edit_release.wait(timeout=5):
                raise AssertionError("timed out waiting to release edited project")
            return edited_stale

        record_calls = 0
        original_record = screen._record_project_settings

        def tracking_project_record(
            project_settings: ProjectVerificationSettingsSnapshot,
            *,
            request_generation: int | None = None,
            draft_generation: int | None = None,
        ) -> None:
            nonlocal record_calls
            original_record(
                project_settings,
                request_generation=request_generation,
                draft_generation=draft_generation,
            )
            record_calls += 1

        service.get_project_verification_settings = (  # type: ignore[method-assign]
            overlapping_project_load
        )
        screen._record_project_settings = tracking_project_record  # type: ignore[method-assign]
        screen._load_project_settings()
        await wait_for(pilot, first_started.is_set)
        screen._load_project_settings()
        await wait_for(
            pilot,
            lambda: (
                screen.project_settings is not None
                and screen.project_settings.revision == 2
            ),
        )
        first_release.set()
        await wait_for(pilot, lambda: record_calls == 2)
        assert screen.project_settings is not None
        assert screen.project_settings.revision == 2

        screen._load_project_settings()
        await wait_for(pilot, edit_started.is_set)
        screen._customize_project_settings()
        screen.query_one("#project-ai-role-model", Select).value = "gpt-5.6-sol"
        await wait_for(
            pilot,
            lambda: screen._project_role_drafts[TaskKind.PROOF][0] == "gpt-5.6-sol",
        )
        edit_release.set()
        await wait_for(pilot, lambda: record_calls == 3)

        assert screen.project_settings.revision == 2
        assert screen._project_role_drafts[TaskKind.PROOF][0] == "gpt-5.6-sol"


@async_test
async def test_setup_refreshes_reject_out_of_order_and_pre_mutation_results() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: screen.snapshot is not None)
        assert screen.snapshot is not None
        base = screen.snapshot

        def at_revision(revision: int) -> ProviderSetupSnapshot:
            return replace(
                base,
                settings=replace(base.settings, revision=revision),
                detail=f"setup revision {revision}",
            )

        older = at_revision(1)
        newer = at_revision(2)
        pre_mutation = at_revision(3)
        authoritative = at_revision(4)
        first_started = threading.Event()
        first_release = threading.Event()
        third_started = threading.Event()
        third_release = threading.Event()
        load_calls = 0

        def overlapping_setup_load() -> ProviderSetupSnapshot:
            nonlocal load_calls
            load_calls += 1
            if load_calls == 1:
                first_started.set()
                if not first_release.wait(timeout=5):
                    raise AssertionError("timed out waiting to release old setup")
                return older
            if load_calls == 2:
                return newer
            third_started.set()
            if not third_release.wait(timeout=5):
                raise AssertionError("timed out waiting to release pre-mutation setup")
            return pre_mutation

        record_calls = 0
        original_record = screen._record_setup_and_reload

        def tracking_setup_record(
            snapshot: ProviderSetupSnapshot,
            *,
            request_generation: int | None = None,
        ) -> None:
            nonlocal record_calls
            original_record(snapshot, request_generation=request_generation)
            record_calls += 1

        service.get_ai_setup = overlapping_setup_load  # type: ignore[method-assign]
        screen._record_setup_and_reload = tracking_setup_record  # type: ignore[method-assign]
        screen.refresh_setup()
        await wait_for(pilot, first_started.is_set)
        screen.refresh_setup()
        await wait_for(
            pilot,
            lambda: (
                screen.snapshot is not None and screen.snapshot.settings.revision == 2
            ),
        )
        first_release.set()
        await wait_for(pilot, lambda: record_calls == 2)
        assert screen.snapshot is not None
        assert screen.snapshot.settings.revision == 2

        screen.refresh_setup()
        await wait_for(pilot, third_started.is_set)
        screen._record_setup_and_reload(authoritative)
        assert screen.snapshot.settings.revision == 4
        third_release.set()
        await wait_for(pilot, lambda: record_calls == 4)
        assert screen.snapshot.settings.revision == 4


@async_test
async def test_credential_store_serializes_backend_mutations() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]
    store_started = threading.Event()
    store_release = threading.Event()
    original_store = service.store_ai_credential

    def blocking_store(
        driver: DriverId,
        source: CredentialSource,
        credential: SecretSubmission,
    ) -> ProviderSetupSnapshot:
        store_started.set()
        if not store_release.wait(timeout=5):
            raise AssertionError("timed out waiting to release credential store")
        return original_store(driver, source, credential)

    service.store_ai_credential = blocking_store  # type: ignore[method-assign]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#ai-configure-driver", Select).disabled,
        )
        await show_ai_settings_view(pilot, screen, "connection")
        screen.query_one(
            "#ai-configure-driver", Select
        ).value = DriverId.OPENAI_API.value
        await wait_for(
            pilot,
            lambda: (
                bool(screen.query("#ai-api-key").nodes)
                and not screen.query_one("#store-ai-key", Button).disabled
                and not screen.query_one("#ai-api-key", Input).disabled
            ),
        )
        key_input = screen.query_one("#ai-api-key", Input)
        key_input.value = "sk-test-never-retain"
        screen.query_one("#store-ai-key", Button).press()
        await wait_for(pilot, store_started.is_set)

        assert screen._credential_mutation_in_flight
        assert screen.query_one("#store-ai-key", Button).disabled
        assert screen.query_one("#delete-ai-key", Button).disabled
        assert screen.query_one("#ai-configure-driver", Select).disabled
        assert screen.query_one("#ai-settings-nav", OptionList).disabled

        app.action_main_menu()
        await wait_for(pilot, lambda: app.screen is screen)
        app.action_global_settings()
        await wait_for(pilot, lambda: app.screen is screen)

        key_input.value = "second-secret-must-not-submit"
        screen._store_credential()
        assert key_input.value == ""
        assert service.credential_calls == []

        store_release.set()
        await wait_for(pilot, lambda: not screen._credential_mutation_in_flight)
        assert service.credential_calls == [
            (DriverId.OPENAI_API, CredentialSource.CREDENTIAL_STORE)
        ]


@async_test
async def test_project_discard_restores_inherited_read_only_editor_state() -> None:
    project = Path("/test/project-discard-inheritance")
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(project=project)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: screen.project_settings is not None)

        screen.query_one("#settings-scope", Select).value = SettingsScopeKind.PROJECT
        await wait_for(
            pilot,
            lambda: screen._active_scope is SettingsScopeKind.PROJECT,
        )
        screen.query_one("#customize-project-ai", Button).press()
        await wait_for(
            pilot,
            lambda: not screen.query_one("#project-ai-driver", Select).disabled,
        )
        screen.query_one("#project-ai-role-model", Select).value = "gpt-5.6-terra"
        await wait_for(pilot, screen._project_draft_is_dirty)

        screen.query_one("#settings-scope", Select).value = SettingsScopeKind.MACHINE
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, UnsavedAISettingsConfirmationScreen)
                and bool(app.screen.query("#ai-unsaved-discard").nodes)
            ),
        )
        app.screen.query_one("#ai-unsaved-discard", Button).press()
        await wait_for(
            pilot,
            lambda: (
                app.screen is screen
                and screen._active_scope is SettingsScopeKind.MACHINE
            ),
        )

        screen.query_one("#settings-scope", Select).value = SettingsScopeKind.PROJECT
        await wait_for(
            pilot,
            lambda: screen._active_scope is SettingsScopeKind.PROJECT,
        )
        assert not screen._project_customizing
        assert screen.query_one("#project-ai-driver", Select).disabled
        assert not screen.query_one("#customize-project-ai", Button).disabled
        assert screen.query_one("#reset-project-ai", Button).disabled
        assert not screen._project_draft_is_dirty()


@async_test
async def test_degraded_provider_discard_keeps_machine_save_disabled() -> None:
    snapshot = _replace_status(
        _snapshot(),
        _status(
            DriverId.CODEX_CLI,
            authentication=AuthenticationState.REQUIRED,
        ),
    )
    snapshot = replace(snapshot, primary_ready=False)
    service = ProviderWorkflowFake(snapshot)
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings(snapshot)
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: set(screen._machine_role_drafts) == set(TaskKind))

        screen._machine_role_drafts[TaskKind.REPORTING] = (
            "gpt-5.6-terra",
            Difficulty.HIGH,
        )
        screen._restore_scope_draft(SettingsScopeKind.MACHINE)

        assert screen.query_one("#save-ai-settings", Button).disabled
        assert not screen._save_settings()
        assert service.updates == []


@async_test
async def test_continue_editing_remounts_an_empty_one_shot_secret_field() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 48)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, WelcomeScreen))
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(pilot, lambda: set(screen._machine_role_drafts) == set(TaskKind))
        await show_ai_settings_view(pilot, screen, "connection")
        screen.query_one(
            "#ai-configure-driver", Select
        ).value = DriverId.OPENAI_API.value
        await wait_for(
            pilot,
            lambda: (
                bool(screen.query("#ai-api-key").nodes)
                and not screen.query_one("#ai-api-key", Input).disabled
            ),
        )
        old_input = screen.query_one("#ai-api-key", Input)
        old_input.value = "secret-that-must-be-destroyed"
        screen._machine_role_drafts[TaskKind.REPORTING] = (
            "gpt-5.6-terra",
            Difficulty.HIGH,
        )

        app.action_main_menu()
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, UnsavedAISettingsConfirmationScreen)
                and bool(app.screen.query("#ai-unsaved-continue").nodes)
            ),
        )
        app.screen.query_one("#ai-unsaved-continue", Button).press()
        await wait_for(
            pilot,
            lambda: (
                app.screen is screen
                and bool(screen.query("#ai-api-key").nodes)
                and not screen.query_one("#ai-api-key", Input).disabled
            ),
        )
        new_input = screen.query_one("#ai-api-key", Input)
        assert new_input is not old_input
        assert new_input.value == ""
        assert not new_input.disabled
        screen.query_one("#store-ai-key", Button).press()
        await wait_for(
            pilot,
            lambda: "Paste a non-empty API key" in notice_text(screen),
        )
