from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from proof_assistant.ai import (
    AuthenticationState,
    Difficulty,
    DiscoverySource,
    DriverId,
    DriverPreference,
    DriverStatus,
    DriverTransport,
    InstallationState,
    MachineProviderConfigStore,
    ModelCatalog,
    ModelDescriptor,
    ProviderConfig,
    ProviderConfigError,
    ProviderService,
    ProviderSetupSnapshot,
)
from proof_assistant.workflow import NewProjectRequest, ProjectAIOverride
from proof_assistant.workflow.service import ProofAssistantWorkflow

FIXTURE = Path(__file__).parent / "fixtures" / "incremental_manuscript"
ALL_DIFFICULTIES = tuple(Difficulty)


def _catalog_model(driver: DriverId) -> str:
    return "fable" if driver is DriverId.CLAUDE_CLI else f"{driver.value}-test-model"


class StaticCatalogProviderService(ProviderService):
    """Provider service whose model contract never probes a provider account."""

    def discover_models(
        self,
        driver: DriverId,
        *,
        preference: DriverPreference | None = None,
    ) -> ModelCatalog:
        del preference
        model = _catalog_model(driver)
        return ModelCatalog(
            driver=driver,
            models=(
                ModelDescriptor(
                    model_id=model,
                    display_name=f"Static test model for {driver.value}",
                    difficulties=ALL_DIFFICULTIES,
                ),
            ),
            source=DiscoverySource.CURATED_FALLBACK,
            detail="Static integration-test catalog.",
            contract_approved=True,
        )

    def inspect_driver(
        self,
        driver: DriverId,
        *,
        preference: DriverPreference | None = None,
        discover_models: bool = True,
    ) -> DriverStatus:
        catalog = (
            self.discover_models(driver, preference=preference)
            if discover_models
            else None
        )
        transport = (
            DriverTransport.API
            if driver.value.endswith("_api")
            else DriverTransport.CLI
        )
        installation = (
            InstallationState.NOT_APPLICABLE
            if transport is DriverTransport.API
            else InstallationState.INSTALLED
        )
        return DriverStatus(
            driver=driver,
            transport=transport,
            installation=installation,
            authentication=AuthenticationState.AUTHENTICATED,
            executable=None if transport is DriverTransport.API else f"/{driver.value}",
            version="static-test-provider",
            detail="Static integration-test provider is ready.",
            catalog=catalog,
        )

    def get_setup_snapshot(self) -> ProviderSetupSnapshot:
        settings = self.config_store.load()
        return ProviderSetupSnapshot(
            settings=settings,
            statuses=(),
            primary_driver=settings.config.primary_driver,
            primary_ready=True,
            detail="Static integration-test provider setup.",
        )


@pytest.fixture
def workflow_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    provider_config_path = tmp_path / "config" / "providers.json"

    def create(
        provider_service: ProviderService | None = None,
    ) -> ProofAssistantWorkflow:
        if provider_service is None:
            provider_service = StaticCatalogProviderService(
                config_store=MachineProviderConfigStore(provider_config_path),
                environment={},
                home=tmp_path,
            )
        return ProofAssistantWorkflow(
            cache_home=str(tmp_path / "cache"),
            catalog_root=tmp_path / "catalog",
            machine_config_path=tmp_path / "config" / "settings.yaml",
            provider_service=provider_service,
            preference_path=tmp_path / "config" / "preferences.json",
            use_codex_clarification=False,
        )

    return create


@pytest.fixture
def managed_project(tmp_path: Path, workflow_factory) -> Path:
    source = tmp_path / "source"
    shutil.copytree(FIXTURE, source)
    project = tmp_path / "managed"
    workflow_factory().create_project(
        NewProjectRequest(
            "Provider workflow",
            source,
            "main.tex",
            project_path=project,
        )
    )
    return project


def _set_machine_default(
    workflow: ProofAssistantWorkflow,
    driver: DriverId,
) -> ProviderConfig:
    current = workflow.get_ai_setup().settings
    preferences = tuple(
        replace(
            preference,
            model=_catalog_model(driver),
            difficulty=Difficulty.HIGH,
        )
        if preference.driver is driver
        else preference
        for preference in current.config.drivers
    )
    config = replace(
        current.config,
        primary_driver=driver,
        drivers=preferences,
        tasks=(),
    )
    return workflow.update_ai_settings(
        config, expected_revision=current.revision
    ).settings.config


def test_project_override_round_trips_and_reset_restores_machine_inheritance(
    workflow_factory,
    managed_project: Path,
) -> None:
    workflow = workflow_factory()
    machine_config = _set_machine_default(workflow, DriverId.CODEX_CLI)
    machine_defaults = workflow.default_verification_settings()

    inherited = workflow.get_project_verification_settings(managed_project)
    assert inherited.inherited is True
    assert inherited.revision == 0
    assert inherited.effective == machine_defaults

    override = ProjectAIOverride(
        ai_driver=DriverId.CLAUDE_CLI,
        model="fable",
        difficulty=Difficulty.HIGH,
    )
    saved = workflow.update_project_verification_settings(
        managed_project,
        override,
        expected_revision=inherited.revision,
    )
    settings_path = managed_project / ".repoprover" / "verification-settings.json"
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "scope": "PROJECT",
        "revision": 1,
        "override": {
            "ai_driver": "claude_cli",
            "model": "fable",
            "difficulty": "high",
        },
    }
    assert saved.override == override
    assert saved.effective.ai_driver == DriverId.CLAUDE_CLI.value
    assert saved.effective.model == "fable"
    assert saved.effective.effort == Difficulty.HIGH.value
    assert workflow.default_verification_settings(managed_project) == saved.effective
    assert workflow.default_verification_settings() == machine_defaults
    assert workflow.get_ai_setup().settings.config == machine_config
    assert workflow.get_ai_setup().primary_driver is DriverId.CODEX_CLI

    replacement_client = workflow_factory()
    round_tripped = replacement_client.get_project_verification_settings(
        managed_project
    )
    assert round_tripped == saved

    reset = replacement_client.reset_project_verification_settings(
        managed_project,
        expected_revision=round_tripped.revision,
    )
    assert reset.inherited is True
    assert reset.revision == 2
    assert reset.effective == replacement_client.default_verification_settings()
    assert replacement_client.default_verification_settings(managed_project) == (
        replacement_client.default_verification_settings()
    )


def test_machine_default_changes_only_projects_that_inherit(
    tmp_path: Path,
    workflow_factory,
    managed_project: Path,
) -> None:
    workflow = workflow_factory()
    _set_machine_default(workflow, DriverId.CODEX_CLI)

    second_source = tmp_path / "second-source"
    shutil.copytree(FIXTURE, second_source)
    overridden_project = tmp_path / "overridden"
    workflow.create_project(
        NewProjectRequest(
            "Overridden provider",
            second_source,
            "main.tex",
            project_path=overridden_project,
        )
    )
    override = ProjectAIOverride(
        ai_driver=DriverId.CLAUDE_CLI,
        model="fable",
        difficulty=Difficulty.HIGH,
    )
    workflow.update_project_verification_settings(
        overridden_project,
        override,
        expected_revision=0,
    )

    _set_machine_default(workflow, DriverId.GEMINI_API)

    inheriting = workflow.get_project_verification_settings(managed_project)
    overridden = workflow.get_project_verification_settings(overridden_project)
    assert inheriting.effective.ai_driver == DriverId.GEMINI_API.value
    assert inheriting.effective.model == _catalog_model(DriverId.GEMINI_API)
    assert overridden.override == override
    assert overridden.effective.ai_driver == DriverId.CLAUDE_CLI.value
    assert overridden.effective.model == "fable"


def test_invalid_catalog_model_is_rejected_without_mutation(
    workflow_factory,
    managed_project: Path,
) -> None:
    workflow = workflow_factory()
    valid = workflow.update_project_verification_settings(
        managed_project,
        ProjectAIOverride(
            ai_driver=DriverId.CLAUDE_CLI,
            model="fable",
            difficulty=Difficulty.HIGH,
        ),
        expected_revision=0,
    )
    settings_path = managed_project / ".repoprover" / "verification-settings.json"
    before = settings_path.read_bytes()

    with pytest.raises(ProviderConfigError, match="not present"):
        workflow.update_project_verification_settings(
            managed_project,
            ProjectAIOverride(
                ai_driver=DriverId.CLAUDE_CLI,
                model="not-in-static-catalog",
                difficulty=Difficulty.HIGH,
            ),
            expected_revision=valid.revision,
        )

    assert settings_path.read_bytes() == before
    assert workflow.get_project_verification_settings(managed_project) == valid


def test_update_validates_once_before_atomic_project_save(
    tmp_path: Path,
    workflow_factory,
    managed_project: Path,
) -> None:
    class FlappingCatalogProvider(StaticCatalogProviderService):
        catalog_calls = 0

        def discover_models(
            self,
            driver: DriverId,
            *,
            preference: DriverPreference | None = None,
        ) -> ModelCatalog:
            self.catalog_calls += 1
            if self.catalog_calls > 2:
                return ModelCatalog(
                    driver=driver,
                    source=DiscoverySource.UNAVAILABLE,
                    detail="Simulated catalog drift after validation.",
                )
            return super().discover_models(driver, preference=preference)

    provider = FlappingCatalogProvider(
        config_store=MachineProviderConfigStore(tmp_path / "config" / "providers.json"),
        environment={},
        home=tmp_path,
    )
    workflow = workflow_factory(provider)

    saved = workflow.update_project_verification_settings(
        managed_project,
        ProjectAIOverride(
            ai_driver=DriverId.CLAUDE_CLI,
            model="fable",
            difficulty=Difficulty.HIGH,
        ),
        expected_revision=0,
    )

    assert provider.catalog_calls == 2
    assert saved.revision == 1
    assert saved.valid
    assert (
        workflow_factory()
        .reset_project_verification_settings(managed_project, expected_revision=1)
        .inherited
    )


def test_invalidated_override_remains_visible_and_resettable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow_factory,
    managed_project: Path,
) -> None:
    provider = StaticCatalogProviderService(
        config_store=MachineProviderConfigStore(tmp_path / "config" / "providers.json"),
        environment={},
        home=tmp_path,
    )
    workflow = workflow_factory(provider)
    saved = workflow.update_project_verification_settings(
        managed_project,
        ProjectAIOverride(
            ai_driver=DriverId.CLAUDE_CLI,
            model="fable",
            difficulty=Difficulty.HIGH,
        ),
        expected_revision=0,
    )

    def unavailable_driver(
        driver: DriverId,
        *,
        preference: DriverPreference | None = None,
        discover_models: bool = True,
    ) -> DriverStatus:
        catalog = (
            provider.discover_models(driver, preference=preference)
            if discover_models
            else None
        )
        return DriverStatus(
            driver=driver,
            transport=DriverTransport.CLI,
            installation=InstallationState.INSTALLED,
            authentication=AuthenticationState.REQUIRED,
            executable="/claude",
            version="2.1.251 (Claude Code)",
            detail="Authentication expired.",
            catalog=catalog,
        )

    monkeypatch.setattr(provider, "inspect_driver", unavailable_driver)
    stale = workflow.get_project_verification_settings(managed_project)

    assert stale.revision == saved.revision
    assert stale.override == saved.override
    assert not stale.valid
    assert "not installed and authenticated" in (stale.validation_error or "")
    with pytest.raises(ValueError, match="not currently usable"):
        workflow.default_verification_settings(managed_project)

    reset = workflow.reset_project_verification_settings(
        managed_project,
        expected_revision=stale.revision,
    )
    assert reset.inherited
    assert reset.valid


@pytest.mark.parametrize("driver", tuple(DriverId))
def test_every_registered_driver_accepts_a_catalog_model(
    driver: DriverId,
    workflow_factory,
    managed_project: Path,
) -> None:
    workflow = workflow_factory()
    override = ProjectAIOverride(
        ai_driver=driver,
        model=_catalog_model(driver),
        difficulty=Difficulty.HIGH,
    )

    saved = workflow.update_project_verification_settings(
        managed_project,
        override,
        expected_revision=0,
    )

    assert saved.override == override
    assert saved.effective.ai_driver == driver.value
    assert saved.effective.model == _catalog_model(driver)
