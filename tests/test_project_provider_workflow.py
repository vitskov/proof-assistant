from __future__ import annotations

import json
import shutil
from dataclasses import FrozenInstanceError, replace
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
    TaskKind,
    TaskPreference,
)
from proof_assistant.workflow import (
    NewProjectRequest,
    ProjectAIOverride,
    ProjectAIRoleOverride,
)
from proof_assistant.workflow.jobs import VerificationJobStore, request_fingerprint
from proof_assistant.workflow.service import ProofAssistantWorkflow

FIXTURE = Path(__file__).parent / "fixtures" / "incremental_manuscript"
ALL_DIFFICULTIES = tuple(Difficulty)

CLAUDE_ROLE_MODELS = {
    TaskKind.CLARIFICATION: "opus",
    TaskKind.DIAGNOSTIC: "opus",
    TaskKind.PROOF: "fable",
    TaskKind.SKETCH: "sonnet",
    TaskKind.MAINTENANCE: "sonnet",
    TaskKind.REVIEW: "opus",
    TaskKind.DUPLICATE_PROOF: "fable",
    TaskKind.REPORTING: "haiku",
}


def _role_model(driver: DriverId, task: TaskKind) -> str:
    if driver is DriverId.CLAUDE_CLI:
        return CLAUDE_ROLE_MODELS[task]
    return f"{driver.value}-{task.value}-model"


def _role_difficulty(task: TaskKind) -> Difficulty:
    if task in {TaskKind.PROOF, TaskKind.DUPLICATE_PROOF}:
        return Difficulty.MAX
    if task in {TaskKind.CLARIFICATION, TaskKind.DIAGNOSTIC, TaskKind.REVIEW}:
        return Difficulty.HIGH
    if task in {TaskKind.SKETCH, TaskKind.MAINTENANCE}:
        return Difficulty.MEDIUM
    return Difficulty.LOW


def _project_override(driver: DriverId) -> ProjectAIOverride:
    return ProjectAIOverride(
        ai_driver=driver,
        roles=tuple(
            ProjectAIRoleOverride(
                task=task,
                model=_role_model(driver, task),
                difficulty=_role_difficulty(task),
            )
            for task in TaskKind
        ),
    )


class StaticCatalogProviderService(ProviderService):
    """Provider service whose model contract never probes a provider account."""

    def discover_models(
        self,
        driver: DriverId,
        *,
        preference: DriverPreference | None = None,
    ) -> ModelCatalog:
        del preference
        models = tuple(dict.fromkeys(_role_model(driver, task) for task in TaskKind))
        return ModelCatalog(
            driver=driver,
            models=tuple(
                ModelDescriptor(
                    model_id=model,
                    display_name=f"Static test model {model}",
                    difficulties=ALL_DIFFICULTIES,
                )
                for model in models
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
        NewProjectRequest("Provider workflow", source, "main.tex", project_path=project)
    )
    return project


def _set_machine_default(
    workflow: ProofAssistantWorkflow,
    driver: DriverId,
) -> ProviderConfig:
    current = workflow.get_ai_setup().settings
    config = replace(
        current.config,
        primary_driver=driver,
        tasks=tuple(
            TaskPreference(
                task=task,
                driver=driver,
                model=_role_model(driver, task),
                difficulty=_role_difficulty(task),
            )
            for task in TaskKind
        ),
    )
    return workflow.update_ai_settings(
        config, expected_revision=current.revision
    ).settings.config


def _assert_role_settings(settings, override: ProjectAIOverride) -> None:
    assert len(settings.role_settings) == len(TaskKind)
    for role in override.roles:
        effective = settings.for_task(role.task)
        assert effective.ai_driver == override.ai_driver.value
        assert effective.model == role.model
        assert effective.effort == role.difficulty.value


def test_project_roles_round_trip_and_reset_restore_machine_inheritance(
    workflow_factory,
    managed_project: Path,
) -> None:
    workflow = workflow_factory()
    machine_config = _set_machine_default(workflow, DriverId.CODEX_CLI)
    machine_defaults = workflow.default_verification_settings()

    inherited = workflow.get_project_verification_settings(managed_project)
    assert inherited.inherited
    assert inherited.revision == 0
    assert inherited.effective == machine_defaults

    override = _project_override(DriverId.CLAUDE_CLI)
    saved = workflow.update_project_verification_settings(
        managed_project, override, expected_revision=inherited.revision
    )
    settings_path = managed_project / ".repoprover" / "verification-settings.json"
    document = json.loads(settings_path.read_text(encoding="utf-8"))
    assert document == {
        "schema_version": 2,
        "scope": "PROJECT",
        "revision": 1,
        "override": {
            "ai_driver": "claude_cli",
            "roles": [
                {
                    "role": role.task.value,
                    "model": role.model,
                    "difficulty": role.difficulty.value,
                }
                for role in override.roles
            ],
        },
    }
    assert "ai_driver" not in document["override"]["roles"][0]
    assert saved.override == override
    _assert_role_settings(saved.effective, override)
    proof = override.role_for(TaskKind.PROOF)
    assert proof is not None
    assert saved.effective.ai_driver == DriverId.CLAUDE_CLI.value
    assert saved.effective.model == proof.model
    assert saved.effective.effort == proof.difficulty.value
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
        managed_project, expected_revision=round_tripped.revision
    )
    assert reset.inherited
    assert reset.revision == 2
    assert reset.effective == replacement_client.default_verification_settings()


def test_schema_v1_project_overrides_only_proof_and_fills_other_roles(
    workflow_factory,
    managed_project: Path,
) -> None:
    workflow = workflow_factory()
    settings_path = managed_project / ".repoprover" / "verification-settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope": "PROJECT",
                "revision": 4,
                "override": {
                    "ai_driver": "claude_cli",
                    "model": "fable",
                    "difficulty": "high",
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = workflow.get_project_verification_settings(managed_project)

    assert snapshot.revision == 4
    assert snapshot.override == ProjectAIOverride(
        ai_driver=DriverId.CLAUDE_CLI,
        roles=(
            ProjectAIRoleOverride(
                task=TaskKind.PROOF,
                model="fable",
                difficulty=Difficulty.HIGH,
            ),
        ),
    )
    assert snapshot.effective.for_task(TaskKind.PROOF).model == "fable"
    assert snapshot.effective.for_task(TaskKind.PROOF).effort == "high"
    assert len(snapshot.effective.role_settings) == len(TaskKind)
    assert all(
        item.ai_driver == DriverId.CLAUDE_CLI.value
        for item in snapshot.effective.role_settings
    )
    assert snapshot.effective.for_task(TaskKind.SKETCH).model == "sonnet"


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
    override = _project_override(DriverId.CLAUDE_CLI)
    workflow.update_project_verification_settings(
        overridden_project, override, expected_revision=0
    )

    _set_machine_default(workflow, DriverId.GEMINI_API)

    inheriting = workflow.get_project_verification_settings(managed_project)
    overridden = workflow.get_project_verification_settings(overridden_project)
    assert all(
        item.ai_driver == DriverId.GEMINI_API.value
        for item in inheriting.effective.role_settings
    )
    assert overridden.override == override
    _assert_role_settings(overridden.effective, override)


def test_invalid_role_model_is_rejected_without_mutation(
    workflow_factory,
    managed_project: Path,
) -> None:
    workflow = workflow_factory()
    valid_override = _project_override(DriverId.CLAUDE_CLI)
    valid = workflow.update_project_verification_settings(
        managed_project, valid_override, expected_revision=0
    )
    settings_path = managed_project / ".repoprover" / "verification-settings.json"
    before = settings_path.read_bytes()
    invalid_roles = tuple(
        replace(role, model="not-in-static-catalog")
        if role.task is TaskKind.REVIEW
        else role
        for role in valid_override.roles
    )

    with pytest.raises(ProviderConfigError, match="not present"):
        workflow.update_project_verification_settings(
            managed_project,
            replace(valid_override, roles=invalid_roles),
            expected_revision=valid.revision,
        )

    assert settings_path.read_bytes() == before
    assert workflow.get_project_verification_settings(managed_project) == valid


def test_complete_role_update_authenticates_provider_once(
    tmp_path: Path,
    workflow_factory,
    managed_project: Path,
) -> None:
    class CountingProvider(StaticCatalogProviderService):
        inspected: list[DriverId] = []

        def inspect_driver(
            self,
            driver: DriverId,
            *,
            preference: DriverPreference | None = None,
            discover_models: bool = True,
        ) -> DriverStatus:
            self.inspected.append(driver)
            return super().inspect_driver(
                driver,
                preference=preference,
                discover_models=discover_models,
            )

    provider = CountingProvider(
        config_store=MachineProviderConfigStore(tmp_path / "config" / "providers.json"),
        environment={},
        home=tmp_path,
    )
    workflow = workflow_factory(provider)

    saved = workflow.update_project_verification_settings(
        managed_project,
        _project_override(DriverId.CLAUDE_CLI),
        expected_revision=0,
    )

    assert provider.inspected == [DriverId.CLAUDE_CLI]
    assert saved.revision == 1
    assert saved.valid


def test_invalidated_role_override_remains_visible_and_resettable(
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
        _project_override(DriverId.CLAUDE_CLI),
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
            version="static-test-provider",
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
        managed_project, expected_revision=stale.revision
    )
    assert reset.inherited
    assert reset.valid


def test_effective_role_settings_are_frozen(
    workflow_factory,
    managed_project: Path,
) -> None:
    saved = workflow_factory().update_project_verification_settings(
        managed_project,
        _project_override(DriverId.CLAUDE_CLI),
        expected_revision=0,
    )

    with pytest.raises(FrozenInstanceError):
        saved.effective.model = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        saved.effective.role_settings[0].effort = "low"  # type: ignore[misc]


def test_job_freezes_all_role_settings_and_fingerprint_is_role_sensitive(
    workflow_factory,
    managed_project: Path,
) -> None:
    workflow = workflow_factory()
    saved = workflow.update_project_verification_settings(
        managed_project,
        _project_override(DriverId.CLAUDE_CLI),
        expected_revision=0,
    )
    frozen = saved.effective
    fingerprint = request_fingerprint(
        managed_project,
        None,
        frozen,
        codex=workflow.codex,
        cache_home=workflow.cache_home,
    )
    store = VerificationJobStore(managed_project)
    job = store.create(
        request_fingerprint=fingerprint,
        plan_id=None,
        settings=frozen,
    )

    reset = workflow.reset_project_verification_settings(
        managed_project, expected_revision=saved.revision
    )
    persisted = store.job(job.job_id)
    assert reset.inherited
    assert persisted is not None
    assert persisted.settings == frozen
    assert persisted.request_fingerprint == fingerprint

    review = frozen.for_task(TaskKind.REVIEW)
    changed_roles = tuple(
        replace(item, model="different-review-model")
        if item.task is review.task
        else item
        for item in frozen.role_settings
    )
    changed = replace(frozen, role_settings=changed_roles)
    assert (
        request_fingerprint(
            managed_project,
            None,
            changed,
            codex=workflow.codex,
            cache_home=workflow.cache_home,
        )
        != fingerprint
    )


@pytest.mark.parametrize("driver", tuple(DriverId))
def test_every_registered_driver_accepts_a_complete_role_catalog(
    driver: DriverId,
    workflow_factory,
    managed_project: Path,
) -> None:
    workflow = workflow_factory()
    override = _project_override(driver)

    saved = workflow.update_project_verification_settings(
        managed_project, override, expected_revision=0
    )

    assert saved.override == override
    _assert_role_settings(saved.effective, override)
