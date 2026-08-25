from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from textual.pilot import Pilot
from textual.widgets import Button, Input, Select, TextArea

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
from proof_assistant.tui.screens import WelcomeScreen
from proof_assistant.tui.settings import (
    AIAccountVerificationConfirmationScreen,
    AIInstallConfirmationScreen,
    AIProviderSettingsScreen,
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
        except Exception:
            # A newly switched Textual screen exists before its compose tree is
            # mounted; retry until the asserted widgets are available.
            pass
        await pilot.pause(0.01)
    raise AssertionError(f"condition not reached; screen={pilot.app.screen!r}")


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
        DriverId.CLAUDE_CLI: (
            ModelDescriptor(
                "sonnet",
                "Claude Sonnet",
                (Difficulty.AUTO, Difficulty.HIGH),
            ),
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
        self.updates: list[tuple[ProviderConfig, int]] = []
        self.install_previews: list[DriverId] = []
        self.install_calls: list[tuple[InstallPlan, str]] = []
        self.credential_calls: list[tuple[DriverId, CredentialSource]] = []
        self.credential_deleted: list[tuple[DriverId, CredentialSource]] = []
        self.secret_was_received = False
        self.account_verifications: list[DriverId] = []
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

    def list_projects(self) -> tuple[()]:
        return ()

    def get_ai_setup(self) -> ProviderSetupSnapshot:
        return self.snapshot

    def ai_task_policies(self) -> tuple[TaskModelPolicy, ...]:
        return (
            TaskModelPolicy(
                TaskKind.PROOF,
                self.snapshot.primary_driver,
                "gpt-5.6-sol",
                Difficulty.HIGH,
                DiscoverySource.LIVE_ACCOUNT,
                "Uses the current proof-task recommendation.",
            ),
        )

    def update_ai_settings(
        self, config: ProviderConfig, *, expected_revision: int
    ) -> ProviderSetupSnapshot:
        self.updates.append((config, expected_revision))
        self.snapshot = replace(
            self.snapshot,
            settings=MachineProviderSettings(expected_revision + 1, config),
            primary_driver=config.primary_driver,
            primary_ready=_status_by_driver(self.snapshot, config.primary_driver).ready,
        )
        return self.snapshot

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
        await pilot.press("f2")
        assert isinstance(app.screen, AIProviderSettingsScreen)
        assert (
            "Finish primary AI setup" in screen.query_one("#status-line", TextArea).text
        )


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
        app.show_ai_provider_settings()
        await wait_for(pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen))
        screen = app.screen
        assert isinstance(screen, AIProviderSettingsScreen)
        await wait_for(
            pilot,
            lambda: not screen.query_one("#save-ai-settings", Button).disabled,
        )
        model = screen.query_one("#ai-provider-model", Select)
        model.value = "gpt-5.6-sol"
        await pilot.pause()
        difficulty = screen.query_one("#ai-provider-difficulty", Select)
        assert {str(value) for _, value in difficulty._options} == {
            "auto",
            "high",
            "xhigh",
        }
        difficulty.value = "xhigh"
        screen._save_settings()
        await wait_for(pilot, lambda: bool(service.updates))
        config, revision = service.updates[-1]
        assert revision == 1
        preference = config.preference_for(DriverId.CODEX_CLI)
        assert preference.model == "gpt-5.6-sol"
        assert preference.difficulty is Difficulty.XHIGH
        task_text = screen.query_one("#ai-task-policies", TextArea).text
        assert "proof:" in task_text and "resolved by the backend" in task_text


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
        screen._delete_credential()
        await wait_for(pilot, lambda: bool(service.credential_deleted))
        assert service.credential_deleted == [
            (DriverId.OPENAI_API, CredentialSource.CREDENTIAL_STORE)
        ]


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
