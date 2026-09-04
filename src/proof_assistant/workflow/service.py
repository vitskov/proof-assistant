from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from ..ai import (
    SUPPORTED_DRIVERS,
    CredentialSource,
    Difficulty,
    DriverId,
    InstallPlan,
    InstallResult,
    MachineProviderConfigStore,
    ModelCatalog,
    ProviderConfig,
    ProviderService,
    ProviderSetupSnapshot,
    SecretSubmission,
    TaskKind,
    TaskModelPolicy,
)
from ..ai.execution import AIBackendConfig
from ..backend import CodexBackend, CodexConfig
from ..cache import CacheLayout
from ..concurrency import (
    AIConcurrencyPatch,
    AITaskClass,
    AutoInt,
    AutoValue,
    BudgetPolicy,
    BuildConcurrencyPatch,
    CalibrationKey,
    CalibrationProfile,
    CalibrationStore,
    CodexPlan,
    ConcurrencyConfig,
    ConcurrencyConfigPatch,
    ConcurrencyMode,
    ConcurrencyRuntime,
    ConcurrencyRuntimeSpec,
    LeanCalibrationError,
    LeanConcurrencyPatch,
    LegacyVerificationPatch,
    MachineConcurrencySettings,
    MachineConfigStore,
    QueueDepths,
    ResolvedConcurrencyConfig,
    ResourceProfile,
    SchedulerConcurrencyPatch,
    TelemetryCollector,
    derive_auto_limits,
    detect_hardware,
    measure_lean_repl_memory,
    patch_to_mapping,
    project_calibration_key,
    resolve_concurrency_config,
)
from ..incremental.failures import build_failure_report
from ..incremental.graph import (
    affected_claims,
    dependency_closure,
    source_changes,
)
from ..incremental.io import atomic_write_json, atomic_write_text, canonical_hash
from ..incremental.latex import (
    LatexIndexError,
    discover_latex_sources,
    explicit_reference_graph,
    index_manuscript,
    resolve_latex_closure,
)
from ..incremental.locking import (
    ProjectLockedError,
    acquire_worker_lease,
    project_session_active,
    release_worker_lease,
    worker_lease_active,
    worker_lock_path,
)
from ..incremental.models import (
    ClaimState,
    ManuscriptEdge,
    SourceObject,
    TaskPolicy,
    TaskSpec,
    proof_target_ids,
)
from ..incremental.orchestration import (
    VerificationCancelled,
    VerificationResult,
    VerifyOptions,
    verify_project,
)
from ..incremental.session import IncrementalSession
from ..incremental.snapshot import SourceInventoryEntry, StaleSourceError
from ..incremental.store import StateStore
from ..incremental.task import (
    DEFAULT_TASK_INSTRUCTIONS,
    parse_task_file,
    parse_task_text,
    task_document,
)
from ..json_types import JSONObject, json_object, load_json
from ..presentation.clarification_analysis import IsolatedAIClarificationAnalyzer
from ..presentation.clarifications import (
    ClarificationNarrator,
    ClarificationPresenter,
    IsolatedAIClarificationNarrator,
)
from ..workspace.catalog import ProjectCatalog
from ..workspace.management import (
    ManagedDeletionInspection,
    ManagedDeletionKind,
    ManagedProjectDeletionError,
    ManagedProjectKind,
    ManagedProjectRecord,
    ProjectConfigurationError,
    ProjectManager,
)
from ..workspace.paths import (
    ManagedProjectPathError,
    is_in_dropbox,
    validate_managed_project_path,
)
from ..workspace.source import compare_inventories, stable_source_copy
from .contracts import (
    AdaptiveHistoryResetResult,
    BenchmarkKind,
    BenchmarkResult,
    CalibrationResetResult,
    CancellationReport,
    CancellationToken,
    ChangeImpactPlan,
    ClaimChangeKind,
    ClaimImpact,
    ConcurrencySettingsView,
    EffectiveConcurrencyView,
    FailureDependencyReport,
    FileChange,
    FileChangeKind,
    FindingSummary,
    LatexSourceCandidate,
    LegacySettingsView,
    MachineSettingsSnapshot,
    MachineSettingsUpdateRequest,
    ManuscriptFolderEntry,
    ManuscriptFolderListing,
    ManuscriptFolderOrigin,
    NewProjectRequest,
    ProgressEvent,
    ProgressPhase,
    ProgressSink,
    ProjectAIOverride,
    ProjectAvailability,
    ProjectCatalogEntry,
    ProjectDeletionAvailability,
    ProjectDeletionInspection,
    ProjectDeletionResult,
    ProjectDestinationInspection,
    ProjectSummary,
    ProjectVerificationSettingsSnapshot,
    ReportDocument,
    ResourceTelemetryView,
    SettingResolution,
    SettingsChangePreview,
    SettingsScopeKind,
    SettingsWarning,
    SourceInspection,
    VerificationJob,
    VerificationJobObservation,
    VerificationJobState,
    VerificationRoleSettings,
    VerificationSettings,
    WorkflowSnapshot,
    WorkflowState,
)
from .jobs import VerificationJobStore, request_fingerprint
from .preferences import LocalPreferenceStore
from .project_ai import ProjectAISettingsStore

_EnumT = TypeVar("_EnumT", bound=StrEnum)


class StaleChangePlanError(RuntimeError):
    pass


class WorkflowCancelled(RuntimeError):
    pass


class VerificationJobConflictError(RuntimeError):
    """A different active verification request already holds the mutation lease."""

    def __init__(self, observation: VerificationJobObservation) -> None:
        self.observation = observation
        super().__init__(
            "A different backend verification request is already active for "
            f"{observation.job.project_path}"
        )


class VerificationJobNotCancellableError(RuntimeError):
    def __init__(self, observation: VerificationJobObservation) -> None:
        self.observation = observation
        super().__init__(
            "This attached verification predates persistent job control and "
            "cannot be cancelled through this client"
        )


class VerificationJobNotFoundError(RuntimeError):
    pass


class ProjectDestinationError(RuntimeError):
    """Creation/resumption conflict carrying the same typed catalog facts."""

    def __init__(self, inspection: ProjectDestinationInspection) -> None:
        self.inspection = inspection
        super().__init__(
            inspection.issue or "Managed project destination is unavailable"
        )


class ProjectDeletionError(RuntimeError):
    """A recoverable deletion refusal carrying backend-owned preflight facts."""

    def __init__(self, inspection: ProjectDeletionInspection) -> None:
        self.inspection = inspection
        super().__init__(inspection.issue or "Managed project deletion was refused")


class ReportUnavailableError(RuntimeError):
    """A canonical verification report could not be loaded for presentation."""


class CancellationFlag:
    """Thread-safe token suitable for Textual workers and non-UI callers."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise WorkflowCancelled("Verification was cancelled")


class _JobCancellationFlag:
    def __init__(self, store: VerificationJobStore, job_id: str) -> None:
        self.store = store
        self.job_id = job_id

    @property
    def cancelled(self) -> bool:
        return self.store.cancellation_requested(self.job_id)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise WorkflowCancelled("Detached verification cancellation requested")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _task_from_dict(payload: JSONObject) -> TaskSpec:
    raw_policy_value = payload.get("policy") or {}
    if not isinstance(raw_policy_value, dict):
        raise ValueError("Persisted task policy must be an object")
    raw_targets = payload.get("targets", [])
    if not isinstance(raw_targets, list) or not all(
        isinstance(item, str) for item in raw_targets
    ):
        raise ValueError("Persisted task targets must be a string list")

    def text_field(key: str, default: str) -> str:
        value = payload.get(key, default)
        if not isinstance(value, str):
            raise ValueError(f"Persisted task {key} must be a string")
        return value

    def policy_field(key: str, default: bool) -> bool:
        value = raw_policy_value.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"Persisted task policy {key} must be a boolean")
        return value

    return TaskSpec(
        mode=text_field("mode", "theorem"),
        targets=tuple(raw_targets),
        policy=TaskPolicy(
            pause_on_ambiguity=policy_field("pause_on_ambiguity", True),
            preserve_certified=policy_field("preserve_certified", True),
            counterexample_search=policy_field("counterexample_search", True),
            require_statement_correspondence_review=policy_field(
                "require_statement_correspondence_review", False
            ),
        ),
        free_form=text_field("free_form", ""),
        source_format=text_field("source_format", "yaml"),
    )


def _target_set(task: TaskSpec, objects: tuple[SourceObject, ...]) -> set[str]:
    return proof_target_ids(task, objects)


class ProofAssistantWorkflow:
    """Concrete UI-neutral application service implementing the public contract."""

    def __init__(
        self,
        *,
        catalog_root: Path | None = None,
        cache_home: str | None = None,
        codex: str = "codex",
        clarification_narrator: ClarificationNarrator | None = None,
        use_codex_clarification: bool = True,
        codex_model: str = "gpt-5.6-sol",
        machine_config_path: Path | None = None,
        provider_config_path: Path | None = None,
        provider_service: ProviderService | None = None,
        preference_path: Path | None = None,
    ) -> None:
        catalog_path = None
        if catalog_root is not None:
            catalog_path = (
                catalog_root
                if catalog_root.suffix.casefold() == ".json"
                else catalog_root / "projects.json"
            )
        self.catalog = ProjectCatalog(catalog_path)
        self.projects = ProjectManager(self.catalog)
        self.cache_home = cache_home
        self.codex = codex
        self._provided_narrator = clarification_narrator
        self.use_codex_clarification = use_codex_clarification
        self.codex_model = codex_model
        self._sequence = 0
        self._machine_config_store = MachineConfigStore(machine_config_path)
        if provider_service is None:
            if provider_config_path is None and machine_config_path is not None:
                provider_config_path = (
                    Path(machine_config_path).expanduser().resolve(strict=False).parent
                    / "providers.json"
                )
            provider_service = ProviderService(
                config_store=MachineProviderConfigStore(provider_config_path)
            )
        self._provider_service = provider_service
        self._local_preferences = LocalPreferenceStore(preference_path)
        self._telemetry = TelemetryCollector()
        self._concurrency_runtime: ConcurrencyRuntime | None = None
        self._settings_previews: dict[
            str, tuple[SettingsChangePreview, ConcurrencyConfigPatch]
        ] = {}
        self._settings_lock = threading.RLock()

    def default_task_text(self) -> str:
        """Return the validated instructions seeded into the TUI task editor."""
        return DEFAULT_TASK_INSTRUCTIONS

    def default_verification_settings(
        self, project: Path | None = None
    ) -> VerificationSettings:
        """Resolve current machine defaults plus an optional project AI override."""

        if project is not None:
            snapshot = self.get_project_verification_settings(project)
            if snapshot.validation_error is not None:
                raise ValueError(
                    "The project's AI override is not currently usable: "
                    + snapshot.validation_error
                )
            return snapshot.effective
        return self._machine_verification_settings()

    def _machine_verification_settings(self) -> VerificationSettings:
        """Return run defaults resolved only from machine-scoped policy."""

        base = self._base_verification_settings()
        selected_driver = (
            self._provider_service.config_store.load().config.primary_driver
        )
        if selected_driver not in SUPPORTED_DRIVERS:
            choices = ", ".join(driver.value for driver in SUPPORTED_DRIVERS)
            raise ValueError(
                f"Unsupported AI provider {selected_driver.value!r}; "
                f"choose one of: {choices}"
            )
        policies = self.ai_task_policies()
        policy_drivers = {policy.driver for policy in policies}
        if policy_drivers != {selected_driver}:
            raise ValueError(
                "Machine AI settings require one selected provider for every role"
            )
        role_settings = tuple(
            VerificationRoleSettings(
                task=policy.task,
                ai_driver=policy.driver.value,
                model=policy.model or self.codex_model,
                effort=policy.difficulty.value,
            )
            for policy in policies
        )
        proof = next(item for item in role_settings if item.task is TaskKind.PROOF)
        return replace(
            base,
            ai_driver=proof.ai_driver,
            model=proof.model,
            effort=proof.effort,
            role_settings=role_settings,
        )

    def _base_verification_settings(self) -> VerificationSettings:
        """Resolve non-AI run defaults without requiring the primary provider."""

        legacy = self._resolved_concurrency().config.legacy
        return VerificationSettings(
            jobs=legacy.jobs,
            batch_size=legacy.batch_size,
            lean_pool_size=legacy.lean_pool_size,
        )

    @staticmethod
    def _project_ai_store(project: Path) -> ProjectAISettingsStore:
        managed = validate_managed_project_path(project)
        IncrementalSession(managed)._load_config()
        return ProjectAISettingsStore(managed)

    def _validate_project_ai_override(
        self, override: ProjectAIOverride, *, require_complete: bool = False
    ) -> None:
        if override.ai_driver not in SUPPORTED_DRIVERS:
            choices = ", ".join(driver.value for driver in SUPPORTED_DRIVERS)
            raise ValueError(
                f"Unsupported AI provider {override.ai_driver.value!r}; "
                f"choose one of: {choices}"
            )
        if require_complete and not override.complete:
            missing = sorted(
                task.value
                for task in set(TaskKind) - {item.task for item in override.roles}
            )
            raise ValueError(
                "Project AI settings require a model and difficulty for every role; "
                "missing: " + ", ".join(missing)
            )
        settings = self._provider_service.config_store.load()
        preference = settings.config.preference_for(override.ai_driver)
        if not preference.enabled:
            raise ValueError(
                f"AI driver {override.ai_driver.value} is disabled in machine settings"
            )
        status = self._provider_service.inspect_driver(
            override.ai_driver,
            preference=preference,
        )
        if not status.ready:
            raise ValueError(
                f"AI driver {override.ai_driver.value} is not installed and authenticated"
            )
        catalog = status.catalog
        if catalog is None:
            raise ValueError(
                f"AI driver {override.ai_driver.value} has no model catalog"
            )
        if not catalog.contract_approved:
            raise ValueError(
                f"AI driver {override.ai_driver.value} has no validated model catalog"
            )
        for role in override.roles:
            self._provider_service.validate_difficulty(
                override.ai_driver,
                role.model,
                role.difficulty,
                catalog=catalog,
            )

    def _validate_frozen_role_settings(self, settings: VerificationSettings) -> None:
        """Revalidate a complete submitted role map before creating a job."""

        if not settings.role_settings:
            raise ValueError(
                "New verification jobs require a frozen model and effort for every role"
            )
        role_drivers = {DriverId(role.ai_driver) for role in settings.role_settings}
        selected_driver = DriverId(settings.ai_driver)
        if selected_driver not in SUPPORTED_DRIVERS:
            choices = ", ".join(driver.value for driver in SUPPORTED_DRIVERS)
            raise ValueError(
                f"Unsupported AI provider {selected_driver.value!r}; "
                f"choose one of: {choices}"
            )
        if role_drivers != {selected_driver}:
            raise ValueError(
                "New verification jobs require one selected provider for every role"
            )
        machine = self._provider_service.config_store.load().config
        preference = machine.preference_for(selected_driver)
        if not preference.enabled:
            raise ValueError(f"AI driver {selected_driver.value} is disabled")
        status = self._provider_service.inspect_driver(
            selected_driver, preference=preference
        )
        if not status.ready:
            raise ValueError(
                f"AI driver {selected_driver.value} is not ready for a new job"
            )
        catalog = status.catalog
        if catalog is None:
            raise ValueError(
                f"AI driver {selected_driver.value} has no model catalog"
            )
        if not catalog.contract_approved:
            raise ValueError(
                f"AI driver {selected_driver.value} has no validated model catalog"
            )
        for role in settings.role_settings:
            self._provider_service.validate_difficulty(
                selected_driver,
                role.model,
                Difficulty(role.effort),
                catalog=catalog,
            )

    def _project_settings_snapshot(
        self,
        store: ProjectAISettingsStore,
        revision: int,
        override: ProjectAIOverride | None,
        *,
        validate: bool,
        machine_settings: VerificationSettings | None = None,
    ) -> ProjectVerificationSettingsSnapshot:
        effective = machine_settings or (
            self._machine_verification_settings()
            if override is None
            else self._base_verification_settings()
        )
        validation_error = None
        if override is not None and override.ai_driver not in SUPPORTED_DRIVERS:
            effective = machine_settings or self._machine_verification_settings()
            return ProjectVerificationSettingsSnapshot(
                project_path=store.project_path,
                revision=revision,
                override=override,
                effective=effective,
                validation_error=(
                    "The saved project provider is no longer available. Choose "
                    "Codex CLI or Claude CLI, or reset to machine defaults."
                ),
            )
        if override is not None:
            try:
                recommended = (
                    ()
                    if override.complete
                    else self.ai_task_policies(driver=override.ai_driver)
                )
                recommended_by_task = {policy.task: policy for policy in recommended}
                resolved_roles: list[VerificationRoleSettings] = []
                for task in TaskKind:
                    explicit = override.role_for(task)
                    policy = recommended_by_task.get(task)
                    if explicit is None and policy is None:
                        raise ValueError(
                            f"No effective project AI assignment exists for {task.value}"
                        )
                    if explicit is not None:
                        model = explicit.model
                        effort = explicit.difficulty.value
                    else:
                        assert policy is not None
                        model = policy.model or self.codex_model
                        effort = policy.difficulty.value
                    resolved_roles.append(
                        VerificationRoleSettings(
                            task=task,
                            ai_driver=override.ai_driver.value,
                            model=model,
                            effort=effort,
                        )
                    )
                role_settings = tuple(resolved_roles)
                proof = next(
                    item for item in role_settings if item.task is TaskKind.PROOF
                )
                effective = replace(
                    effective,
                    ai_driver=proof.ai_driver,
                    model=proof.model,
                    effort=proof.effort,
                    role_settings=role_settings,
                )
            except Exception as exc:
                validation_error = str(exc)
            if validate:
                try:
                    self._validate_project_ai_override(override)
                except ValueError as exc:
                    validation_error = str(exc)
        return ProjectVerificationSettingsSnapshot(
            project_path=store.project_path,
            revision=revision,
            override=override,
            effective=effective,
            validation_error=validation_error,
        )

    def get_project_verification_settings(
        self, project: Path
    ) -> ProjectVerificationSettingsSnapshot:
        """Return the resolved AI choice for future runs of one managed project."""

        store = self._project_ai_store(project)
        revision, override = store.load()
        return self._project_settings_snapshot(store, revision, override, validate=True)

    def update_project_verification_settings(
        self,
        project: Path,
        override: ProjectAIOverride,
        *,
        expected_revision: int,
    ) -> ProjectVerificationSettingsSnapshot:
        """Persist a validated, secret-free project AI override."""

        machine_settings = self._base_verification_settings()
        self._validate_project_ai_override(override, require_complete=True)
        store = self._project_ai_store(project)
        revision, saved = store.save(override, expected_revision=expected_revision)
        return self._project_settings_snapshot(
            store,
            revision,
            saved,
            validate=False,
            machine_settings=machine_settings,
        )

    def reset_project_verification_settings(
        self, project: Path, *, expected_revision: int
    ) -> ProjectVerificationSettingsSnapshot:
        """Restore machine inheritance without changing any active job."""

        machine_settings = self._machine_verification_settings()
        store = self._project_ai_store(project)
        revision, override = store.save(None, expected_revision=expected_revision)
        return self._project_settings_snapshot(
            store,
            revision,
            override,
            validate=False,
            machine_settings=machine_settings,
        )

    def _configured_task_policy(self, task: TaskKind) -> TaskModelPolicy:
        """Resolve a task against the account-visible catalog when available."""

        settings = self._provider_service.config_store.load()
        preference = settings.config.task_preference_for(task)
        driver = (
            preference.driver
            if preference is not None and preference.driver is not None
            else settings.config.primary_driver
        )
        catalog = self._provider_service.discover_usable_models(
            driver,
            preference=settings.config.preference_for(driver),
        )
        return self._provider_service.recommend_task_policy(
            task, settings=settings, catalog=catalog
        )

    def get_ai_setup(self) -> ProviderSetupSnapshot:
        """Return a freshly probed, sanitized machine-wide provider snapshot."""

        return self._provider_service.get_setup_snapshot()

    def update_ai_settings(
        self, config: ProviderConfig, *, expected_revision: int
    ) -> ProviderSetupSnapshot:
        if config.tasks:
            configured_tasks = {preference.task for preference in config.tasks}
            if configured_tasks != set(TaskKind):
                missing = sorted(
                    task.value for task in set(TaskKind) - configured_tasks
                )
                raise ValueError(
                    "Machine AI settings require a model and difficulty for every "
                    "role; missing: " + ", ".join(missing)
                )
            foreign_drivers = {
                preference.driver
                for preference in config.tasks
                if preference.driver is not None
                and preference.driver is not config.primary_driver
            }
            if foreign_drivers:
                raise ValueError(
                    "Machine AI settings require one selected provider for every role"
                )
        self._provider_service.validate_config(config)
        self._provider_service.config_store.save(
            config, expected_revision=expected_revision
        )
        return self._provider_service.get_setup_snapshot()

    def ai_task_policies(
        self, driver: DriverId | None = None
    ) -> tuple[TaskModelPolicy, ...]:
        """Resolve configured policies or clean recommendations for one provider."""

        settings = self._provider_service.config_store.load()
        if driver is None and settings.config.primary_driver not in SUPPORTED_DRIVERS:
            driver = DriverId.CODEX_CLI
        if driver is not None:
            return self._provider_service.recommend_driver_task_policies(
                driver, settings=settings
            )
        catalogs: dict[DriverId, ModelCatalog] = {}
        policies: list[TaskModelPolicy] = []
        for task in TaskKind:
            preference = settings.config.task_preference_for(task)
            driver = (
                preference.driver
                if preference is not None and preference.driver is not None
                else settings.config.primary_driver
            )
            catalog = catalogs.get(driver)
            if catalog is None:
                catalog = self._provider_service.discover_usable_models(
                    driver,
                    preference=settings.config.preference_for(driver),
                )
                catalogs[driver] = catalog
            policies.append(
                self._provider_service.recommend_task_policy(
                    task,
                    settings=settings,
                    catalog=catalog,
                )
            )
        return tuple(policies)

    def preview_ai_driver_install(self, driver: DriverId) -> InstallPlan:
        return self._provider_service.preview_install(driver)

    def install_ai_driver(
        self, plan: InstallPlan, *, consent_token: str
    ) -> InstallResult:
        return self._provider_service.execute_install(plan, consent_token=consent_token)

    def verify_ai_driver_account(
        self, driver: DriverId, *, consent: bool
    ) -> ProviderSetupSnapshot:
        self._provider_service.verify_cli_account(driver, consent=consent)
        return self._provider_service.get_setup_snapshot()

    def store_ai_credential(
        self,
        driver: DriverId,
        source: CredentialSource,
        credential: SecretSubmission,
    ) -> ProviderSetupSnapshot:
        self._provider_service.store_credential(driver, source, credential)
        settings = self._provider_service.config_store.load()
        preference = settings.config.preference_for(driver)
        if preference.credential_source is not source:
            updated_preferences = tuple(
                replace(item, credential_source=source)
                if item.driver is driver
                else item
                for item in settings.config.drivers
            )
            self._provider_service.config_store.save(
                replace(settings.config, drivers=updated_preferences),
                expected_revision=settings.revision,
            )
        return self._provider_service.get_setup_snapshot()

    def delete_ai_credential(
        self, driver: DriverId, source: CredentialSource
    ) -> ProviderSetupSnapshot:
        self._provider_service.delete_credential(driver, source)
        return self._provider_service.get_setup_snapshot()

    def browse_manuscript_folders(
        self, directory: Path | None = None
    ) -> ManuscriptFolderListing:
        """Enumerate directories without exposing filesystem access to a UI."""

        origin = ManuscriptFolderOrigin.REQUESTED
        if directory is None:
            preferred = self._local_preferences.load_manuscript_folder()
            if preferred is not None and preferred.is_dir():
                directory = preferred
                origin = ManuscriptFolderOrigin.PREFERENCE
            else:
                directory = Path.home()
                origin = ManuscriptFolderOrigin.HOME_FALLBACK
        try:
            resolved = directory.expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"Could not resolve manuscript folder: {directory}"
            ) from exc
        if not resolved.is_dir():
            raise ValueError(f"Manuscript folder is not a directory: {resolved}")
        try:
            children = tuple(
                sorted(
                    resolved.iterdir(),
                    key=lambda child: (child.name.casefold(), child.name),
                )
            )
        except OSError as exc:
            raise ValueError(
                f"Could not read manuscript folder: {resolved}: {exc}"
            ) from exc
        entries: list[ManuscriptFolderEntry] = []
        seen: set[Path] = set()
        for child in children:
            try:
                if not child.is_dir():
                    continue
                target = child.resolve()
            except (OSError, RuntimeError):
                continue
            if target in seen:
                continue
            seen.add(target)
            entries.append(
                ManuscriptFolderEntry(
                    name=child.name,
                    path=target,
                    symlink=child.is_symlink(),
                )
            )
        entries.sort(key=lambda item: (item.name.casefold(), item.name, str(item.path)))
        parent = None if resolved == resolved.parent else resolved.parent
        return ManuscriptFolderListing(
            directory=resolved,
            parent=parent,
            home=Path.home().resolve(),
            folders=tuple(entries),
            origin=origin,
        )

    def remember_manuscript_folder(self, directory: Path) -> Path:
        """Persist one successfully inspected manuscript folder machine-locally."""

        return self._local_preferences.save_manuscript_folder(directory)

    def _concurrency_spec(
        self,
        *,
        cli_patch: ConcurrencyConfigPatch | None = None,
        project: Path | None = None,
    ) -> ConcurrencyRuntimeSpec:
        return ConcurrencyRuntimeSpec(
            cache_home=self.cache_home,
            machine_config_path=str(self._machine_config_store.path),
            cli_patch=cli_patch,
            project_path=str(project.resolve()) if project is not None else None,
        )

    def _resolved_concurrency(self) -> ResolvedConcurrencyConfig:
        return self._concurrency_spec().resolve()

    def _runtime(self) -> ConcurrencyRuntime:
        """Return this client's handle to the machine-global controllers."""

        resolved = self._resolved_concurrency()
        if self._concurrency_runtime is None:
            self._concurrency_runtime = self._concurrency_spec().create()
        elif (
            self._concurrency_runtime.resolved.machine_revision
            != resolved.machine_revision
            or self._concurrency_runtime.resolved.config != resolved.config
        ):
            # Runtime applies are non-destructive: lower limits stop new
            # admissions and leave every in-flight lease alive.
            self._concurrency_runtime.apply_resolved(resolved)
        return self._concurrency_runtime

    def _runtime_for_settings(self, project: Path | None) -> ConcurrencyRuntime:
        """Return a project-aware view over the same machine-global controllers."""

        if project is None:
            return self._runtime()
        project = project.expanduser().resolve()
        if not project.is_dir():
            raise ValueError(f"Managed project directory does not exist: {project}")
        machine_runtime = self._runtime()
        return self._concurrency_spec(project=project).create(
            resources=machine_runtime.resources
        )

    @staticmethod
    def _auto_view(value: AutoInt) -> int | None:
        return value if isinstance(value, int) else None

    @classmethod
    def _configured_view(cls, config: ConcurrencyConfig) -> ConcurrencySettingsView:
        return ConcurrencySettingsView(
            mode=config.mode.value,
            resource_profile=config.resource_profile.value,
            codex_plan=config.ai.plan.value,
            budget_policy=config.ai.budget_policy.value,
            ai_initial=cls._auto_view(config.ai.initial),
            ai_hard_max=cls._auto_view(config.ai.hard_max),
            ai_minimum=config.ai.minimum,
            ai_increase_after_successes=cls._auto_view(
                config.ai.increase_after_successes
            ),
            lean_pool=cls._auto_view(config.lean.pool_size),
            lean_max=cls._auto_view(config.lean.max_pool),
            lean_minimum=config.lean.min_pool,
            lean_memory_calibration=config.lean.memory_calibration,
            fallback_memory_per_repl_gib=config.lean.fallback_memory_per_repl_gib,
            max_builds=cls._auto_view(config.build.max_concurrent),
            build_hard_max=config.build.hard_max,
            agents_per_target_initial=(config.scheduler.agents_per_target_initial),
            agents_per_target_max=config.scheduler.agents_per_target_max,
            duplicate_agent_escalation=(config.scheduler.duplicate_agent_escalation),
            dependency_priority=config.scheduler.dependency_priority,
            adaptive_controller=config.mode == ConcurrencyMode.ADAPTIVE,
            hardware_telemetry=config.telemetry_enabled,
        )

    @staticmethod
    def _legacy_view(config: ConcurrencyConfig) -> LegacySettingsView:
        return LegacySettingsView(
            proof_jobs=config.legacy.jobs,
            batch_size=config.legacy.batch_size,
            per_worker_lean_pool=config.legacy.lean_pool_size,
        )

    @staticmethod
    def _effective_view(runtime: ConcurrencyRuntime) -> EffectiveConcurrencyView:
        ai = runtime.ai.status()
        lean = runtime.lean.status()
        build = runtime.build.status()
        return EffectiveConcurrencyView(
            ai_limit=ai.current_limit,
            ai_ceiling=ai.ceiling,
            lean_pool=lean.current_limit,
            lean_max=lean.maximum,
            build_limit=build.current_limit,
            build_ceiling=build.hard_maximum,
            agents_per_target_current=(
                runtime.resolved.config.scheduler.agents_per_target_initial
            ),
            agents_per_target_max=(
                runtime.resolved.config.scheduler.agents_per_target_max
            ),
        )

    def _telemetry_view(self, runtime: ConcurrencyRuntime) -> ResourceTelemetryView:
        ai = runtime.ai.status()
        lean = runtime.lean.status()
        build = runtime.build.status()
        allocation = detect_hardware()
        sample = self._telemetry.sample(
            queues=QueueDepths(ai=ai.queued, lean=lean.queued, build=build.queued),
            memory_allocation=allocation,
        )
        if runtime.resolved.config.telemetry_enabled:
            runtime.observe_telemetry(sample)
            # Adaptation may have changed the displayed limits, but active and
            # queued facts in this immutable sample remain the same instant.
            ai = runtime.ai.status()
            lean = runtime.lean.status()
            build = runtime.build.status()
        resources = runtime.resources
        backoff = (
            datetime.fromtimestamp(ai.backoff_until, tz=UTC).isoformat()
            if ai.backoff_until > time.time()
            else None
        )
        return ResourceTelemetryView(
            os_name=resources.os_name,
            architecture=resources.architecture,
            resource_profile=runtime.auto_limits.resource_profile.value,
            physical_cpus=resources.usable_physical_cpus,
            logical_cpus=resources.usable_logical_cpus,
            cpu_percent=sample.cpu_percent,
            total_memory_gib=resources.total_memory_gib,
            available_memory_gib=min(
                resources.total_memory_gib,
                sample.available_memory_bytes / (1024**3),
            ),
            memory_percent_available=100.0 * sample.available_memory_ratio,
            swap_used_gib=sample.swap_used_bytes / (1024**3),
            swap_out_mib_per_second=(
                None
                if sample.swap_out_rate_bytes_per_second is None
                else sample.swap_out_rate_bytes_per_second / (1024**2)
            ),
            memory_pressure=sample.pressure.value,
            memory_pressure_source=sample.memory_pressure_source.value,
            native_memory_pressure_level=sample.native_memory_pressure_level,
            load_average=sample.load_average,
            io_wait_percent=sample.disk_iowait_percent,
            ai_active=ai.active,
            ai_queued=ai.queued,
            ai_throttles=ai.throttles,
            ai_backoff_until=backoff,
            lean_active=lean.active,
            lean_queued=lean.queued,
            lean_p95_rss_gib=(
                runtime.calibration_profile.repl.p95_working_rss_gib
                if runtime.calibration_profile is not None
                and runtime.calibration_profile.repl is not None
                else None
            ),
            build_active=build.active,
            build_queued=build.queued,
            sampled_at=_now(),
        )

    @staticmethod
    def _configured_text(value: object) -> str:
        if value == AutoValue.AUTO:
            return "Auto"
        return str(getattr(value, "value", value))

    def _resolution(self, runtime: ConcurrencyRuntime) -> tuple[SettingResolution, ...]:
        config = runtime.resolved.config
        effective = self._effective_view(runtime)
        rows = (
            ("ai.initial", config.ai.initial, effective.ai_limit),
            ("ai.hard_max", config.ai.hard_max, effective.ai_ceiling),
            (
                "ai.increase_after_successes",
                config.ai.increase_after_successes,
                runtime.ai.success_threshold,
            ),
            ("lean.pool_size", config.lean.pool_size, effective.lean_pool),
            ("lean.max_pool", config.lean.max_pool, effective.lean_max),
            (
                "build.max_concurrent",
                config.build.max_concurrent,
                effective.build_limit,
            ),
            (
                "scheduler.agents_per_target_max",
                config.scheduler.agents_per_target_max,
                effective.agents_per_target_max,
            ),
            ("legacy.jobs", config.legacy.jobs, config.legacy.jobs),
            ("legacy.batch_size", config.legacy.batch_size, config.legacy.batch_size),
        )
        return tuple(
            SettingResolution(
                field=field,
                configured=self._configured_text(configured),
                effective=str(effective_value),
                source=runtime.resolved.source_for(field).value,
            )
            for field, configured, effective_value in rows
        )

    def get_machine_settings(
        self, *, project: Path | None = None
    ) -> MachineSettingsSnapshot:
        """Return one backend-owned, UI-neutral machine settings snapshot."""

        with self._settings_lock:
            runtime = self._runtime_for_settings(project)
            resources = runtime.resources
            identity = (
                f"{resources.os_name}|{resources.architecture}|"
                f"{resources.host_logical_cpus}|{resources.host_total_memory_bytes}"
            )
            layout = CacheLayout.discover(self.cache_home)
            reasons = list(runtime.auto_limits.reasons)
            if not runtime.resolved.config.telemetry_enabled:
                reasons.append("live adaptive hardware telemetry is disabled")
            return MachineSettingsSnapshot(
                scope=SettingsScopeKind.MACHINE,
                machine_id=hashlib.sha256(identity.encode()).hexdigest()[:16],
                config_path=self._machine_config_store.path,
                cache_path=layout.root / "concurrency",
                revision=runtime.resolved.machine_revision,
                configured=self._configured_view(runtime.resolved.config),
                effective=self._effective_view(runtime),
                telemetry=self._telemetry_view(runtime),
                legacy=self._legacy_view(runtime.resolved.config),
                resolution=self._resolution(runtime),
                reasons=tuple(reasons),
                updated_at=_now(),
            )

    @staticmethod
    def _enum_value(enum_type: type[_EnumT], value: str) -> _EnumT:
        normalized = value.casefold().replace("-", "_").replace(" ", "_")
        return enum_type(normalized)

    def _request_patch(
        self, request: MachineSettingsUpdateRequest
    ) -> ConcurrencyConfigPatch:
        if request.scope != SettingsScopeKind.MACHINE:
            raise ValueError(
                "Project concurrency overlays are reserved but not implemented; "
                "save these settings at machine scope"
            )
        view = request.configured
        mode = self._enum_value(ConcurrencyMode, view.mode)
        if view.adaptive_controller != (mode == ConcurrencyMode.ADAPTIVE):
            raise ValueError(
                "Adaptive controller and concurrency mode disagree; choose "
                "Adaptive/Auto or Fixed/Manual consistently"
            )

        def automatic(value: int | None) -> AutoInt:
            return AutoValue.AUTO if value is None else value

        return ConcurrencyConfigPatch(
            mode=mode,
            resource_profile=self._enum_value(ResourceProfile, view.resource_profile),
            telemetry_enabled=view.hardware_telemetry,
            ai=AIConcurrencyPatch(
                plan=self._enum_value(CodexPlan, view.codex_plan),
                initial=automatic(view.ai_initial),
                hard_max=automatic(view.ai_hard_max),
                minimum=view.ai_minimum,
                budget_policy=self._enum_value(BudgetPolicy, view.budget_policy),
                increase_after_successes=automatic(view.ai_increase_after_successes),
            ),
            lean=LeanConcurrencyPatch(
                pool_size=automatic(view.lean_pool),
                min_pool=view.lean_minimum,
                max_pool=automatic(view.lean_max),
                memory_calibration=view.lean_memory_calibration,
                fallback_memory_per_repl_gib=(view.fallback_memory_per_repl_gib),
            ),
            build=BuildConcurrencyPatch(
                max_concurrent=automatic(view.max_builds),
                hard_max=view.build_hard_max,
            ),
            scheduler=SchedulerConcurrencyPatch(
                agents_per_target_initial=view.agents_per_target_initial,
                agents_per_target_max=view.agents_per_target_max,
                dependency_priority=view.dependency_priority,
                duplicate_agent_escalation=view.duplicate_agent_escalation,
            ),
            legacy=LegacyVerificationPatch(
                jobs=request.legacy.proof_jobs,
                batch_size=request.legacy.batch_size,
                lean_pool_size=request.legacy.per_worker_lean_pool,
            ),
        )

    def preview_machine_settings(
        self, request: MachineSettingsUpdateRequest
    ) -> SettingsChangePreview:
        with self._settings_lock:
            current = self._machine_config_store.load()
            if current.revision != request.expected_revision:
                raise ValueError(
                    "Machine settings changed in another client; reload Settings"
                )
            patch = self._request_patch(request)
            # Resolve validates every cross-field invariant without writing.
            resolved = resolve_concurrency_config(
                machine=MachineConcurrencySettings(
                    revision=current.revision,
                    patch=patch,
                )
            )
            hardware = detect_hardware()
            tuned = derive_auto_limits(hardware, resolved.config)
            lean_max = (
                tuned.lean_pool
                if resolved.config.lean.max_pool == AutoValue.AUTO
                else int(resolved.config.lean.max_pool)
            )
            effective = EffectiveConcurrencyView(
                ai_limit=tuned.ai_initial,
                ai_ceiling=tuned.ai_ceiling,
                lean_pool=tuned.lean_pool,
                lean_max=lean_max,
                build_limit=tuned.build_concurrency,
                build_ceiling=resolved.config.build.hard_max,
                agents_per_target_current=(
                    resolved.config.scheduler.agents_per_target_initial
                ),
                agents_per_target_max=(resolved.config.scheduler.agents_per_target_max),
            )
            warnings: list[SettingsWarning] = []
            if request.configured.lean_pool is not None and (
                request.configured.lean_pool > max(2, tuned.lean_cpu_cap)
                or request.configured.lean_pool > max(2, tuned.lean_memory_cap)
            ):
                warnings.append(
                    SettingsWarning(
                        "unsafe-lean-pool",
                        "The manual Lean pool exceeds the detected CPU or RAM "
                        "recommendation and may cause swapping or OOM.",
                        str(
                            min(
                                tuned.lean_cpu_cap,
                                tuned.lean_memory_cap,
                                resolved.config.lean.initial_auto_cap,
                            )
                        ),
                    )
                )
            plan_policy_config = replace(
                resolved.config,
                ai=replace(
                    resolved.config.ai,
                    initial=AutoValue.AUTO,
                    hard_max=AutoValue.AUTO,
                ),
            )
            plan_policy_ceiling = derive_auto_limits(
                hardware, plan_policy_config
            ).ai_ceiling
            manual_ai_values = tuple(
                value
                for value in (
                    request.configured.ai_initial,
                    request.configured.ai_hard_max,
                )
                if value is not None
            )
            if manual_ai_values and max(manual_ai_values) > plan_policy_ceiling:
                warnings.append(
                    SettingsWarning(
                        "quota-burn",
                        "The manual AI concurrency exceeds the configured plan "
                        "policy ceiling and may consume quota rapidly.",
                        str(plan_policy_ceiling),
                    )
                )
            automatic_builds = derive_auto_limits(
                hardware,
                resolve_concurrency_config(
                    machine=ConcurrencyConfigPatch(
                        resource_profile=resolved.config.resource_profile,
                        build=BuildConcurrencyPatch(
                            hard_max=resolved.config.build.hard_max
                        ),
                    ),
                    environ={},
                ).config,
            ).build_concurrency
            if request.configured.max_builds is not None and (
                request.configured.max_builds > automatic_builds
            ):
                warnings.append(
                    SettingsWarning(
                        "build-pressure",
                        "The manual full-build limit exceeds the conservative "
                        "hardware recommendation.",
                        str(automatic_builds),
                    )
                )
            token = canonical_hash(
                {
                    "request": repr(request),
                    "patch": patch_to_mapping(patch),
                    "nonce": os.urandom(16).hex(),
                }
            )
            preview = SettingsChangePreview(
                preview_token=token,
                requested=request,
                effective_if_applied=effective,
                warnings=tuple(warnings),
                live_fields=(
                    "mode",
                    "ai concurrency",
                    "Lean admission",
                    "build admission",
                    "budget policy",
                    "hardware telemetry",
                ),
                next_run_fields=(
                    "resource profile",
                    "Codex plan",
                    "agents per target",
                    "legacy proof jobs",
                    "legacy batch size",
                    "legacy per-worker Lean pool",
                ),
            )
            self._settings_previews[token] = (preview, patch)
            return preview

    def apply_machine_settings(
        self,
        preview_token: str,
        accepted_warning_ids: tuple[str, ...] = (),
    ) -> MachineSettingsSnapshot:
        with self._settings_lock:
            try:
                preview, patch = self._settings_previews.pop(preview_token)
            except KeyError as exc:
                raise ValueError(
                    "Settings preview is missing or already applied"
                ) from exc
            required = {warning.warning_id for warning in preview.warnings}
            missing = required - set(accepted_warning_ids)
            if missing:
                raise ValueError(
                    "Explicit confirmation is required for: "
                    + ", ".join(sorted(missing))
                )
            self._machine_config_store.save(
                patch, expected_revision=preview.requested.expected_revision
            )
            resolved = self._resolved_concurrency()
            if self._concurrency_runtime is None:
                self._concurrency_runtime = self._concurrency_spec().create()
            else:
                self._concurrency_runtime.apply_resolved(resolved)
            return self.get_machine_settings()

    def reset_machine_settings(self, expected_revision: int) -> MachineSettingsSnapshot:
        with self._settings_lock:
            self._machine_config_store.save(
                ConcurrencyConfigPatch(), expected_revision=expected_revision
            )
            resolved = self._resolved_concurrency()
            if self._concurrency_runtime is None:
                self._concurrency_runtime = self._concurrency_spec().create()
            else:
                self._concurrency_runtime.apply_resolved(resolved)
            return self.get_machine_settings()

    def run_concurrency_benchmark(
        self,
        kind: BenchmarkKind,
        *,
        project: Path | None = None,
        allow_codex_traffic: bool = False,
    ) -> BenchmarkResult:
        """Run the quota-safe calibration boundary used by CLI and TUI.

        Codex traffic is never generated unless it is explicitly authorized.
        A project-backed Lean benchmark starts disposable representative REPLs
        and records real RSS measurements; no-project Lean/build actions retain
        the conservative policy-only fallback.
        """

        runtime = self._runtime()
        resources = runtime.resources
        if project is not None:
            project = project.expanduser().resolve()
            key = project_calibration_key(
                project,
                resources=resources,
                codex_plan=runtime.resolved.config.ai.plan.value,
                codex_model=(self.codex_model if kind == BenchmarkKind.CODEX else ""),
            )
        else:
            key = CalibrationKey(
                os_name=resources.os_name,
                architecture=resources.architecture,
                usable_logical_cpus=resources.usable_logical_cpus,
                total_memory_bytes=resources.total_memory_bytes,
                lean_version="unknown",
                mathlib_revision=None,
                import_profile="machine-default",
                codex_plan=runtime.resolved.config.ai.plan.value,
                codex_model=(self.codex_model if kind == BenchmarkKind.CODEX else ""),
            )
        store = CalibrationStore.discover(self.cache_home)
        current = store.load(key) or CalibrationProfile(key=key)
        if kind == BenchmarkKind.CODEX:
            recommendation = runtime.auto_limits.ai_initial
            used_codex_traffic = False
            if allow_codex_traffic:
                status = runtime.ai.status()
                if status.active or status.queued:
                    raise RuntimeError(
                        "Codex concurrency benchmarking requires an idle AI queue"
                    )
                from concurrent.futures import ThreadPoolExecutor

                def harmless_probe(index: int) -> str:
                    backend = CodexBackend(
                        CodexConfig(
                            executable=self.codex,
                            model=self.codex_model,
                            effort="low",
                            sandbox="read-only",
                            isolate_external_tools=True,
                            concurrency=self._concurrency_spec(project=project),
                            ai_task_class=AITaskClass.DIAGNOSTIC,
                        )
                    )
                    try:
                        result = backend.run(
                            system_prompt=(
                                "This is a harmless concurrency health probe. "
                                "Return exactly OK and perform no other work."
                            ),
                            user_prompt=f"Probe {index}: return OK.",
                            tools=[],
                            tool_handler=lambda _name, _arguments: "Tools are disabled",
                        )
                    finally:
                        backend.close()
                    return result.final_text.strip()

                maximum_probe = min(2, status.current_limit)
                tested = tuple(range(1, maximum_probe + 1))
                responses: list[str] = []
                for width in tested:
                    with ThreadPoolExecutor(max_workers=width) as executor:
                        responses.extend(
                            executor.map(
                                harmless_probe,
                                range(1, width + 1),
                            )
                        )
                if not responses or any(item != "OK" for item in responses):
                    raise RuntimeError(
                        "Codex benchmark probe returned an unexpected response"
                    )
                used_codex_traffic = True
                detail = (
                    "Explicitly authorized tiny Codex probes completed at widths "
                    + ", ".join(str(item) for item in tested)
                    + "; adaptive throttle/latency history was updated."
                )
            else:
                tested = (1,)
                detail = "Quota-safe policy calibration; no Codex request was sent."
            profile = CalibrationProfile(
                key=key,
                repl=current.repl,
                recommended_lean_pool=current.recommended_lean_pool,
                recommended_build_concurrency=current.recommended_build_concurrency,
                recommended_ai_concurrency=recommendation,
                tested_ai_ceiling=(max(tested) if used_codex_traffic else None),
                revision=current.revision + (1 if current.measured_at else 0),
            )
        elif kind == BenchmarkKind.LEAN:
            used_codex_traffic = False
            repl = None
            if project is not None:
                try:
                    owner = (
                        "lean-calibration:"
                        + hashlib.sha256(str(project).encode()).hexdigest()[:16]
                    )
                    with runtime.lean.exclusive_calibration_lease(owner):
                        repl = measure_lean_repl_memory(project)
                except LeanCalibrationError:
                    raise
                except Exception as exc:
                    raise LeanCalibrationError(
                        f"Lean REPL calibration failed: {exc}"
                    ) from exc
            recommendation = derive_auto_limits(
                resources,
                runtime.resolved.config,
                calibrated_repl_p95_gib=(
                    repl.p95_working_rss_gib if repl is not None else None
                ),
            ).lean_pool
            tested = tuple(
                value
                for value in (1, 2, 4, 8, 16, 32)
                if value <= max(1, recommendation)
            ) or (1,)
            detail = (
                "Measured two sequential project REPLs without Codex traffic "
                f"({repl.samples} working RSS samples; p95 "
                f"{repl.p95_working_rss_gib:.3f} GiB). "
                if repl is not None
                else "No project was supplied; recorded the conservative "
                "uncalibrated hardware policy. "
            ) + "No proof state was modified."
            profile = CalibrationProfile(
                key=key,
                repl=repl,
                recommended_lean_pool=recommendation,
                recommended_build_concurrency=current.recommended_build_concurrency,
                recommended_ai_concurrency=current.recommended_ai_concurrency,
                tested_ai_ceiling=current.tested_ai_ceiling,
                revision=current.revision + (1 if current.measured_at else 0),
            )
        else:
            used_codex_traffic = False
            recommendation = runtime.auto_limits.build_concurrency
            tested = tuple(range(1, recommendation + 1))
            detail = "Conservative CPU/RAM build calibration; no concurrent build was started."
            profile = CalibrationProfile(
                key=key,
                repl=current.repl,
                recommended_lean_pool=current.recommended_lean_pool,
                recommended_build_concurrency=recommendation,
                recommended_ai_concurrency=current.recommended_ai_concurrency,
                tested_ai_ceiling=current.tested_ai_ceiling,
                revision=current.revision + (1 if current.measured_at else 0),
            )
        saved = store.save(profile)
        # A newly saved profile may lower the machine-global Lean budget.  Any
        # subsequent handle must re-resolve the conservative profile set.
        self._concurrency_runtime = None
        return BenchmarkResult(
            kind=kind,
            recommendation=recommendation,
            tested_values=tested,
            detail=detail,
            used_codex_traffic=used_codex_traffic,
            calibration_path=store.path_for(saved.key),
        )

    def reset_project_lean_calibration(self, project: Path) -> CalibrationResetResult:
        """Delete the exact fresh/stale Lean calibration for one project key."""

        project = project.expanduser().resolve()
        if not project.is_dir():
            raise ValueError(f"Managed project directory does not exist: {project}")
        runtime = self._runtime()
        key = project_calibration_key(
            project,
            resources=runtime.resources,
            codex_plan=runtime.resolved.config.ai.plan.value,
            codex_model="",
        )
        store = CalibrationStore.discover(self.cache_home)
        path = store.path_for(key)
        owner = f"lean-calibration-reset:{key.identifier}"
        with runtime.lean.exclusive_calibration_lease(owner):
            removed = store.reset(key)
        self._concurrency_runtime = None
        return CalibrationResetResult(
            project_path=project,
            profile_id=key.identifier,
            calibration_path=path,
            removed=removed,
        )

    def reset_adaptive_history(self) -> AdaptiveHistoryResetResult:
        """Clear machine adaptive evidence without cancelling in-flight work."""

        with self._settings_lock:
            runtime = self._runtime()
            runtime.reset_adaptive_history()
            effective = self._effective_view(runtime)
            return AdaptiveHistoryResetResult(
                reset_at=_now(),
                ai_limit=effective.ai_limit,
                lean_pool=effective.lean_pool,
                build_limit=effective.build_limit,
            )

    def inspect_source(self, source: Path) -> SourceInspection:
        source = source.expanduser().resolve()
        try:
            discovered = discover_latex_sources(source)
        except LatexIndexError as exc:
            raise ValueError(str(exc)) from exc
        candidates = tuple(
            LatexSourceCandidate(relative_path, has_documentclass)
            for relative_path, has_documentclass in discovered
        )
        document_roots = tuple(item for item in candidates if item.has_documentclass)
        suggestion_pool = document_roots or candidates
        preferred_names = {
            "main.tex": 0,
            "main.ltx": 1,
            "paper.tex": 2,
            "paper.ltx": 3,
            "manuscript.tex": 4,
            "manuscript.ltx": 5,
            "article.tex": 6,
            "article.ltx": 7,
        }
        suggested = min(
            suggestion_pool,
            key=lambda item: (
                preferred_names.get(
                    Path(item.relative_path).name.casefold(), len(preferred_names)
                ),
                len(Path(item.relative_path).parts),
                item.relative_path.casefold(),
                item.relative_path,
            ),
        )
        return SourceInspection(
            source_path=source,
            candidates=candidates,
            suggested_main_file=suggested.relative_path,
            source_in_dropbox=is_in_dropbox(source),
        )

    def inspect_project_destination(
        self, name: str, project_path: Path | None = None
    ) -> ProjectDestinationInspection:
        resolved = self.projects.resolve_destination(name, project_path)
        return self._destination_inspection(self.projects.inspect(resolved))

    def inspect_project_deletion(self, project: Path) -> ProjectDeletionInspection:
        return self._deletion_inspection(self.projects.inspect_deletion(project))

    def delete_project(self, project: Path) -> ProjectDeletionResult:
        try:
            result = self.projects.delete_project(project)
        except ManagedProjectDeletionError as exc:
            raise ProjectDeletionError(
                self._deletion_inspection(exc.inspection)
            ) from exc
        return ProjectDeletionResult(
            project_path=result.project_path,
            source_path=result.source_path,
            trash_path=result.trash_path,
            deleted_at=result.deleted_at,
            recoverable=True,
        )

    def list_projects(self) -> tuple[ProjectCatalogEntry, ...]:
        entries: list[ProjectCatalogEntry] = []
        for record in self.projects.entries():
            if record.kind == ManagedProjectKind.RESUMABLE:
                try:
                    summary = self._summary(record.project_path)
                except Exception as exc:
                    entries.append(
                        self._catalog_entry(
                            ManagedProjectRecord(
                                record.project_path,
                                ManagedProjectKind.INCOMPLETE,
                                record.name,
                                f"Recognized project could not be opened: {exc}",
                                source_path=record.source_path,
                            )
                        )
                    )
                else:
                    entries.append(self._catalog_entry(record, project=summary))
            else:
                entries.append(self._catalog_entry(record))
        return tuple(entries)

    def create_project(self, request: NewProjectRequest) -> WorkflowSnapshot:
        inspection = self.inspect_project_destination(
            request.name, request.project_path
        )
        if not inspection.can_create:
            record = self.projects.inspect(inspection.project_path)
            self.projects.remember_occupied(record)
            raise ProjectDestinationError(inspection)
        source = request.source_path.expanduser().resolve()
        source_inspection = self.inspect_source(source)
        selected = Path(str(request.main_file)).as_posix()
        candidate_paths = {item.relative_path for item in source_inspection.candidates}
        if selected not in candidate_paths:
            choices = ", ".join(sorted(candidate_paths))
            raise ValueError(
                f"Selected main LaTeX file is not a source candidate: {selected!r}. "
                f"Choose one of: {choices}"
            )
        try:
            resolve_latex_closure(source, selected)
        except LatexIndexError as exc:
            raise ValueError(str(exc)) from exc
        project = inspection.project_path
        source_in_dropbox = is_in_dropbox(source)
        IncrementalSession.initialize(
            manuscript=source,
            task_text=request.task_text,
            project=project,
            project_name=request.name,
            source_in_dropbox=source_in_dropbox,
            main_file=selected,
        )
        self._record_workflow_state(project, WorkflowState.PROJECT_READY)
        self.catalog.upsert(project)
        return WorkflowSnapshot(
            state=WorkflowState.PROJECT_READY,
            project=self._summary(project),
        )

    def resume_project(self, project: Path) -> WorkflowSnapshot:
        project = validate_managed_project_path(project)
        managed = self.projects.inspect(project)
        if managed.kind == ManagedProjectKind.MIGRATION_READY:
            # Session loading performs the manager-owned unambiguous migration.
            IncrementalSession(project)._load_config()
            managed = self.projects.inspect(project)
        if managed.kind != ManagedProjectKind.RESUMABLE:
            raise ProjectDestinationError(self._destination_inspection(managed))
        self._ensure_project_task(project)
        summary = self._summary(project)
        job_observation = self.observe_verification(project, after_sequence=sys.maxsize)
        if job_observation is not None:
            job = job_observation.job
            if not job.state.terminal:
                return WorkflowSnapshot(WorkflowState.VERIFYING, summary)
            workflow = self._read_workflow_state(project)
            workflow_updated = str(workflow.get("updated_at") or "")
            job_is_newer = bool(
                job.completed_at and job.completed_at >= workflow_updated
            )
            if job_is_newer and job.state in {
                VerificationJobState.FAILED,
                VerificationJobState.INTERRUPTED,
            }:
                state = (
                    WorkflowState.INTERRUPTED
                    if job.state == VerificationJobState.INTERRUPTED
                    else WorkflowState.FAILED
                )
                self._record_workflow_state(project, state, error=job.error)
                return WorkflowSnapshot(state, self._summary(project), error=job.error)
        session = IncrementalSession(project)
        status = session.status()
        if status["mutation_in_progress"]:
            return WorkflowSnapshot(WorkflowState.VERIFYING, summary)
        try:
            plan = self.plan_changes(project)
        except Exception as exc:
            return WorkflowSnapshot(
                WorkflowState.FAILED,
                summary,
                error=f"Could not observe the manuscript source: {exc}",
            )
        if plan is not None:
            state = WorkflowState.CHANGE_REVIEW
            self._record_workflow_state(project, state)
            return WorkflowSnapshot(state, self._summary(project), pending_plan=plan)
        try:
            session.reconcile_conjectural_policy()
            status = session.status()
        except Exception as exc:
            return WorkflowSnapshot(
                WorkflowState.FAILED,
                self._summary(project),
                error=f"Could not reconcile conjectural claim policy: {exc}",
            )
        if status["open_questions"]:
            state = WorkflowState.AWAITING_CLARIFICATION
            clarifications = ClarificationPresenter().load_or_present_all(
                project, summary.source_path
            )
            self._record_workflow_state(project, state)
            return WorkflowSnapshot(
                state, self._summary(project), clarifications=clarifications
            )
        latest = status["latest_run"] or {}
        error: str | None = None
        if latest.get("status") == "RUNNING":
            state = WorkflowState.INTERRUPTED
            error = (
                "A legacy verification run was left RUNNING, but no backend "
                "worker or project mutation lease remains. Its durable evidence "
                "is preserved; retry will recover the interrupted run before "
                "scheduling more work."
            )
        elif latest.get("status") == "INTERRUPTED":
            state = WorkflowState.INTERRUPTED
        elif latest.get("status") == "FAILED":
            state = WorkflowState.FAILED
        elif latest.get("outcome") in {
            "verified",
            "no_proof_obligations",
            "partial_unresolved",
            "counterexample_found",
        }:
            state = WorkflowState.COMPLETED
        else:
            state = WorkflowState.PROJECT_READY
        findings = (
            self._findings_from_store(project)
            if state in {WorkflowState.COMPLETED, WorkflowState.FAILED}
            else None
        )
        self._record_workflow_state(project, state, error=error)
        return WorkflowSnapshot(
            state, self._summary(project), findings=findings, error=error
        )

    def select_project_main_file(
        self, project: Path, main_file: str
    ) -> WorkflowSnapshot:
        project = validate_managed_project_path(project)
        try:
            self.projects.select_main_file(project, main_file)
        except ProjectConfigurationError as exc:
            record = self.projects.inspect(project)
            raise ProjectDestinationError(
                ProjectDestinationInspection(
                    project,
                    ProjectAvailability(record.kind.value),
                    str(exc),
                )
            ) from exc
        return self.resume_project(project)

    def load_report(self, project: Path) -> ReportDocument:
        """Load the canonical project report without delegating to a GUI opener."""

        try:
            project = validate_managed_project_path(project)
            managed = self.projects.inspect(project)
        except (ManagedProjectPathError, OSError) as exc:
            raise ReportUnavailableError(
                f"Cannot load a report from unmanaged project root {project}: {exc}"
            ) from exc
        allowed = {
            ManagedProjectKind.RESUMABLE,
            ManagedProjectKind.MIGRATION_READY,
            ManagedProjectKind.NEEDS_MAIN_FILE,
        }
        if managed.kind not in allowed:
            detail = managed.issue or f"project classification is {managed.kind.value}"
            raise ReportUnavailableError(
                f"Cannot load a report from unmanaged project root {project}: {detail}"
            )
        report_path = project / "VERIFICATION_REPORT.md"
        try:
            resolved_report = report_path.resolve(strict=False)
        except OSError as exc:
            raise ReportUnavailableError(
                f"Could not resolve verification report: {report_path}: {exc}"
            ) from exc
        if not resolved_report.is_relative_to(project):
            raise ReportUnavailableError(
                "Verification report escapes the managed project root: "
                f"{report_path} -> {resolved_report}"
            )
        try:
            markdown = resolved_report.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ReportUnavailableError(
                f"Verification report does not exist: {report_path}"
            ) from exc
        except UnicodeError as exc:
            raise ReportUnavailableError(
                f"Verification report is not valid UTF-8: {report_path}: {exc}"
            ) from exc
        except OSError as exc:
            raise ReportUnavailableError(
                f"Could not read verification report: {report_path}: {exc}"
            ) from exc
        return ReportDocument(resolved_report, markdown)

    def load_failure_report(
        self, project: Path, run_id: int | None = None
    ) -> FailureDependencyReport | None:
        """Load a backend-built immutable failure graph for one historical run."""

        project = validate_managed_project_path(project)
        managed = self.projects.inspect(project)
        if managed.kind != ManagedProjectKind.RESUMABLE:
            raise ProjectDestinationError(self._destination_inspection(managed))
        session = IncrementalSession(project)
        with StateStore(session.database_path) as store:
            return build_failure_report(project, store, run_id)

    def plan_changes(self, project: Path) -> ChangeImpactPlan | None:
        """Compute a complete candidate plan without changing project authority."""
        project = validate_managed_project_path(project)
        session = IncrementalSession(project)
        config = session._load_config()
        source = Path(str(config["manuscript"])).expanduser().resolve()
        main_file = str(config["main_file"])
        task_path = session._task_path_from_config(config)
        _task_path, _task_text, task_sha, task = parse_task_file(task_path)

        with stable_source_copy(source) as (candidate_source, inventory):
            try:
                source_files = resolve_latex_closure(candidate_source, main_file)
            except LatexIndexError as exc:
                raise ValueError(str(exc)) from exc
            input_files = source_files[1:]
            with StateStore(session.database_path) as store:
                snapshot = store.previous_snapshot()
                previous_rows = (
                    {
                        str(row["claim_id"]): row
                        for row in store.claim_versions(snapshot)
                    }
                    if snapshot
                    else {}
                )
                prior_files = {
                    str(row["path"]): SourceInventoryEntry(
                        str(row["path"]),
                        str(row["sha256"]),
                        int(row["size"]),
                    )
                    for row in (store.source_file_rows(snapshot) if snapshot else [])
                }
                old_edges = tuple(
                    ManuscriptEdge(
                        str(row["src"]),
                        str(row["dst"]),
                        str(row["edge_kind"]),
                        str(row["provenance"]),
                        bool(row["approved"]),
                    )
                    for row in store.manuscript_edges()
                )
                old_task_payload = json_object(
                    load_json(store.get_metadata("task_spec") or "{}"),
                    path="persisted task",
                )
                old_task_sha = store.get_metadata("task_sha256")
                main_file_changed = bool(store.get_metadata("pending_main_file_change"))
                certificates = {
                    str(row["claim_id"]) for row in store.certificate_rows()
                }
                open_questions = [dict(row) for row in store.open_questions()]
                temporary = Path(tempfile.mkdtemp(prefix="proof-assistant-plan-"))
                database_copy = temporary / "state.sqlite3"
                store.backup_to(database_copy)
            try:
                with StateStore(database_copy) as candidate_store:
                    objects = index_manuscript(
                        candidate_source, candidate_store, main_file=main_file
                    )
                explicit_edges, _unresolved = explicit_reference_graph(objects)
            finally:
                import shutil

                shutil.rmtree(temporary, ignore_errors=True)

        current_ids = {item.claim_id for item in objects}
        persistent_edges = tuple(
            edge
            for edge in old_edges
            if edge.kind not in {"explicit_ref", "assistant_context"}
            and edge.src in current_ids
            and edge.dst in current_ids
        )
        edge_map = {
            (edge.src, edge.dst, edge.kind): edge
            for edge in (*explicit_edges, *persistent_edges)
        }
        edges = tuple(edge_map[key] for key in sorted(edge_map))
        statement, assistant_context, proof, deleted = source_changes(
            previous_rows, objects, mode=task.mode
        )
        added = {claim_id for claim_id in statement if claim_id not in previous_rows}
        statement -= added
        old_edge_keys = {(edge.src, edge.dst, edge.kind) for edge in old_edges}
        new_edge_keys = {(edge.src, edge.dst, edge.kind) for edge in edges}
        dependency_changed = {
            src
            for src, _dst, _kind in old_edge_keys ^ new_edge_keys
            if src in current_ids
        }
        union_ids = current_ids | set(previous_rows)
        union_edges = {
            (edge.src, edge.dst, edge.kind): edge for edge in (*old_edges, *edges)
        }
        affected = affected_claims(
            statement
            | assistant_context
            | added
            | proof
            | deleted
            | dependency_changed,
            claim_ids=union_ids,
            edges=union_edges.values(),
        )

        old_task = _task_from_dict(old_task_payload) if old_task_payload else task
        task_changed = old_task_sha != task_sha
        task_impacts: list[ClaimImpact] = []
        if task_changed:
            old_targets = _target_set(old_task, objects)
            new_targets = _target_set(task, objects)
            for claim_id in sorted(old_targets ^ new_targets):
                task_impacts.append(ClaimImpact(claim_id, ClaimChangeKind.TASK_SCOPE))
            if old_task.mode != task.mode:
                for claim_id in sorted(new_targets):
                    task_impacts.append(
                        ClaimImpact(claim_id, ClaimChangeKind.TASK_MODE)
                    )
            if old_task.policy != task.policy:
                for claim_id in sorted(new_targets):
                    task_impacts.append(ClaimImpact(claim_id, ClaimChangeKind.POLICY))
            if old_task.free_form != task.free_form:
                for claim_id in sorted(new_targets):
                    task_impacts.append(
                        ClaimImpact(claim_id, ClaimChangeKind.TASK_SCOPE)
                    )
            task_seed = {impact.claim_id for impact in task_impacts}
            affected.update(
                dependency_closure(task_seed, claim_ids=current_ids, edges=edges)
            )

        object_files = {item.claim_id: item.source_file for item in objects}
        direct: list[ClaimImpact] = [
            ClaimImpact(claim_id, ClaimChangeKind.ADDED, object_files.get(claim_id))
            for claim_id in sorted(added)
        ]
        direct.extend(
            ClaimImpact(claim_id, ClaimChangeKind.STATEMENT, object_files.get(claim_id))
            for claim_id in sorted(statement)
        )
        direct.extend(
            ClaimImpact(
                claim_id,
                ClaimChangeKind.ASSISTANT_CONTEXT,
                object_files.get(claim_id),
            )
            for claim_id in sorted(assistant_context)
        )
        direct.extend(
            ClaimImpact(
                claim_id, ClaimChangeKind.PROOF_ONLY, object_files.get(claim_id)
            )
            for claim_id in sorted(proof)
        )
        direct.extend(
            ClaimImpact(
                claim_id, ClaimChangeKind.DEPENDENCY, object_files.get(claim_id)
            )
            for claim_id in sorted(dependency_changed)
        )
        direct.extend(
            ClaimImpact(
                claim_id,
                ClaimChangeKind.DELETED,
                str(previous_rows[claim_id]["source_file"]),
            )
            for claim_id in sorted(deleted)
        )
        direct.extend(task_impacts)
        relevant_files = {
            main_file,
            *input_files,
            *(str(value) for value in config.get("input_files", [])),
        }
        deltas = tuple(
            item
            for item in compare_inventories(prior_files, inventory)
            if item.path in relevant_files
            or (item.old_path is not None and item.old_path in relevant_files)
        )
        file_changes = tuple(
            FileChange(
                delta.path,
                FileChangeKind(delta.kind),
                delta.old_path,
                delta.old_sha256,
                delta.new_sha256,
            )
            for delta in deltas
        )
        if not file_changes and not task_changed and not main_file_changed:
            return None
        superseded = tuple(
            sorted(
                str(question["question_id"])
                for question in open_questions
                if str(question["claim_id"])
                in statement | assistant_context | added | proof | deleted
            )
        )
        identity = {
            "schema_version": 1,
            "project": str(project),
            "source": str(source),
            "main_file": main_file,
            "input_files": list(input_files),
            "base_snapshot": snapshot,
            "candidate_inventory_sha256": inventory.sha256,
            "task_sha256": task_sha,
            "main_file_changed": main_file_changed,
            "file_changes": [
                {
                    "path": item.path,
                    "kind": str(item.kind),
                    "old_path": item.old_path,
                    "old_sha256": item.old_sha256,
                    "new_sha256": item.new_sha256,
                }
                for item in file_changes
            ],
            "claim_impacts": [
                {
                    "claim_id": item.claim_id,
                    "kind": str(item.kind),
                    "source_file": item.source_file,
                }
                for item in direct
            ],
            "affected": sorted(affected),
        }
        return ChangeImpactPlan(
            plan_id=canonical_hash(identity),
            project_path=project,
            source_path=source,
            main_file=main_file,
            input_files=input_files,
            base_snapshot=snapshot,
            candidate_inventory_sha256=inventory.sha256,
            file_changes=file_changes,
            direct_claim_changes=tuple(direct),
            affected_claims=tuple(sorted(affected)),
            unaffected_certificates=tuple(sorted(certificates - affected)),
            superseded_questions=superseded,
            task_changed=task_changed,
            source_in_dropbox=is_in_dropbox(source),
            created_at=_now(),
            main_file_changed=main_file_changed,
        )

    def confirm_and_verify(
        self,
        project: Path,
        plan_id: str | None,
        settings: VerificationSettings,
        *,
        progress: ProgressSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> WorkflowSnapshot:
        project = validate_managed_project_path(project)
        current = self.plan_changes(project)
        if plan_id is not None and (current is None or current.plan_id != plan_id):
            raise StaleChangePlanError(
                "The manuscript or task changed after review; inspect the new impact plan"
            )
        if plan_id is None and current is not None:
            raise StaleChangePlanError(
                "A manuscript or task change requires explicit review and confirmation"
            )
        try:
            self._checkpoint(cancellation)
        except WorkflowCancelled as exc:
            return self._interrupted_snapshot(project, exc)
        self._record_workflow_state(project, WorkflowState.VERIFYING)
        project_summary = self._summary(project)
        active_main_file = current.main_file if current else project_summary.main_file
        active_input_files = (
            current.input_files if current else project_summary.input_files
        )
        self._emit(
            progress,
            ProgressPhase.VALIDATING,
            f"Validated {active_main_file} and "
            f"{len(active_input_files)} recursive input file(s)",
            details={
                "main_file": active_main_file,
                "input_files": active_input_files,
                "source_path": str(project_summary.source_path),
            },
        )

        def event_hook(phase: str, message: str, details: dict[str, object]) -> None:
            try:
                mapped = ProgressPhase(phase)
            except ValueError:
                mapped = ProgressPhase.PROOF_BATCH
            self._emit(progress, mapped, message, details=details)

        proof_settings = settings.for_task(TaskKind.PROOF)
        try:
            result = verify_project(
                IncrementalSession(project),
                options=VerifyOptions(
                    ai_driver=proof_settings.ai_driver,
                    model=proof_settings.model,
                    effort=proof_settings.effort,
                    codex=self.codex,
                    provider_config_path=str(self._provider_service.config_store.path),
                    cache_home=self.cache_home,
                    jobs=settings.jobs,
                    batch_size=settings.batch_size,
                    lean_pool_size=settings.lean_pool_size,
                    setup_timeout=settings.setup_timeout,
                    request_timeout=settings.request_timeout,
                    turn_timeout=settings.turn_timeout,
                    gc_timeout=settings.gc_timeout,
                    concurrency=self._concurrency_spec(project=project),
                ),
                expected_inventory_sha256=(
                    current.candidate_inventory_sha256 if current else None
                ),
                event_hook=event_hook,
                cancellation_checkpoint=(
                    cancellation.raise_if_cancelled
                    if cancellation is not None
                    else None
                ),
            )
        except StaleSourceError as exc:
            self._record_workflow_state(project, WorkflowState.CHANGE_REVIEW)
            raise StaleChangePlanError(str(exc)) from exc
        except (WorkflowCancelled, VerificationCancelled) as exc:
            return self._interrupted_snapshot(project, exc)
        except Exception as exc:
            self._record_workflow_state(project, WorkflowState.FAILED, error=str(exc))
            return WorkflowSnapshot(
                WorkflowState.FAILED, self._summary(project), error=str(exc)
            )

        return self._snapshot_for_result(project, result, settings=settings)

    def _job_store(self, project: Path) -> VerificationJobStore:
        project = validate_managed_project_path(project)
        managed = self.projects.inspect(project)
        if managed.kind != ManagedProjectKind.RESUMABLE:
            raise ProjectDestinationError(self._destination_inspection(managed))
        return VerificationJobStore(project)

    @staticmethod
    def _legacy_job(project: Path, *, starting: bool = False) -> VerificationJob:
        now = _now()
        identity = canonical_hash({"legacy_project": str(project)})[:20]
        return VerificationJob(
            job_id=f"legacy-{identity}",
            project_path=project,
            state=(
                VerificationJobState.STARTING
                if starting
                else VerificationJobState.RUNNING
            ),
            request_fingerprint="legacy-unavailable",
            plan_id=None,
            settings=None,
            created_at=now,
            started_at=None if starting else now,
            updated_at=now,
            completed_at=None,
            heartbeat_at=None,
            pid=None,
            error=(
                "An active backend verification predates durable job control; "
                "only coarse attachment is available"
            ),
            cancellable=False,
            attached_legacy=True,
        )

    @classmethod
    def _legacy_observation(
        cls, project: Path, *, starting: bool = False
    ) -> VerificationJobObservation:
        return VerificationJobObservation(
            job=cls._legacy_job(project, starting=starting),
            events=(),
            after_sequence=0,
            next_sequence=0,
            started=False,
            attached=True,
        )

    @staticmethod
    def _assert_matching_request(
        observation: VerificationJobObservation, fingerprint: str
    ) -> VerificationJobObservation:
        if (
            not observation.job.attached_legacy
            and observation.job.request_fingerprint != fingerprint
        ):
            raise VerificationJobConflictError(observation)
        return observation

    def observe_verification(
        self, project: Path, after_sequence: int = 0
    ) -> VerificationJobObservation | None:
        """Replay durable progress without acquiring the worker's mutation lease."""

        store = self._job_store(project)
        project = store.project
        active = store.active()
        if active is not None:
            # A worker is considered crashed only after both independent
            # lifetime signals are free. This never guesses from PID/heartbeat.
            if not worker_lease_active(project) and not project_session_active(project):
                active = store.finish(
                    active.job_id,
                    VerificationJobState.INTERRUPTED,
                    error=(
                        "The detached verification worker exited without recording "
                        "a terminal result; the project mutation lease is free"
                    ),
                )
            return store.observe(active, after_sequence=after_sequence)
        if project_session_active(project):
            return self._legacy_observation(project)
        if worker_lease_active(project):
            return self._legacy_observation(project, starting=True)
        latest = store.latest()
        return (
            store.observe(latest, after_sequence=after_sequence)
            if latest is not None
            else None
        )

    def start_verification(
        self,
        project: Path,
        plan_id: str | None,
        settings: VerificationSettings,
    ) -> VerificationJobObservation:
        """Start once or idempotently attach to an equivalent detached request."""

        store = self._job_store(project)
        project = store.project
        fingerprint = request_fingerprint(
            project,
            plan_id,
            settings,
            codex=self.codex,
            cache_home=self.cache_home,
        )
        existing = store.active()
        if existing is not None and worker_lease_active(project):
            return self._assert_matching_request(store.observe(existing), fingerprint)
        try:
            lease_fd = acquire_worker_lease(project)
        except ProjectLockedError:
            # The launcher transfers the lease before returning. Its durable row
            # normally already exists; tolerate the very small pre-insert window.
            for _attempt in range(20):
                existing = store.active()
                if existing is not None:
                    return self._assert_matching_request(
                        store.observe(existing), fingerprint
                    )
                if project_session_active(project):
                    return self._legacy_observation(project)
                time.sleep(0.05)
            return self._legacy_observation(project, starting=True)

        transferred = False
        try:
            existing = store.active()
            if existing is not None:
                if not project_session_active(project):
                    existing = store.finish(
                        existing.job_id,
                        VerificationJobState.INTERRUPTED,
                        error=(
                            "A previous detached worker lost both lifecycle leases "
                            "before recording a terminal result"
                        ),
                    )
                else:
                    return self._assert_matching_request(
                        store.observe(existing), fingerprint
                    )
            if project_session_active(project):
                return self._legacy_observation(project)

            # Fail before persistence/spawn if the request is already stale.
            current = self.plan_changes(project)
            if plan_id is not None and (current is None or current.plan_id != plan_id):
                raise StaleChangePlanError(
                    "The manuscript or task changed after review; inspect the new "
                    "impact plan"
                )
            if plan_id is None and current is not None:
                raise StaleChangePlanError(
                    "A manuscript or task change requires explicit review and "
                    "confirmation"
                )
            self._validate_frozen_role_settings(settings)
            proof_settings = settings.for_task(TaskKind.PROOF)
            VerifyOptions(
                ai_driver=proof_settings.ai_driver,
                model=proof_settings.model,
                jobs=settings.jobs,
                batch_size=settings.batch_size,
                lean_pool_size=settings.lean_pool_size,
            ).validate()
            job = store.create(
                request_fingerprint=fingerprint,
                plan_id=plan_id,
                settings=settings,
            )
            log_path = store.worker_log(job.job_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab", buffering=0) as log:
                command = [
                    sys.executable,
                    "-m",
                    "proof_assistant",
                    "--codex",
                    self.codex,
                ]
                if self.cache_home is not None:
                    command.extend(("--cache-home", self.cache_home))
                command.extend(
                    (
                        "_project-worker",
                        "--project",
                        str(project),
                        "--job-id",
                        job.job_id,
                        "--lease-fd",
                        str(lease_fd),
                        "--catalog-file",
                        str(self.catalog.path),
                        "--machine-config-file",
                        str(self._machine_config_store.path),
                        "--provider-config-file",
                        str(self._provider_service.config_store.path),
                    )
                )
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        close_fds=True,
                        pass_fds=(lease_fd,),
                        start_new_session=True,
                    )
                except Exception as exc:
                    store.finish(
                        job.job_id,
                        VerificationJobState.FAILED,
                        error=f"Could not launch detached verification: {exc}",
                    )
                    raise
            # Closing, rather than unlocking, the parent's copy transfers the
            # open-file-description lease solely to the detached child.
            os.close(lease_fd)
            transferred = True
            try:
                started_job = store.record_spawn(
                    job.job_id,
                    pid=process.pid,
                    command=tuple(command),
                    worker_log_path=log_path,
                )
            except Exception:
                # The worker already holds the lifetime lease and remains the
                # authority. A transient provenance-write failure must never
                # unlock or terminate it; the child will still record its PID,
                # heartbeat, progress, and terminal state.
                fallback_job = store.job(job.job_id)
                if fallback_job is None:
                    raise VerificationJobNotFoundError(job.job_id)
                started_job = fallback_job
            return store.observe(started_job, started=True, attached=False)
        finally:
            if not transferred:
                release_worker_lease(lease_fd)

    def request_verification_cancel(
        self, project: Path, job_id: str
    ) -> VerificationJobObservation:
        store = self._job_store(project)
        job = store.job(job_id)
        if job is None:
            observed = self.observe_verification(project)
            if observed is not None and observed.job.job_id == job_id:
                if observed.job.attached_legacy:
                    raise VerificationJobNotCancellableError(observed)
                return observed
            raise VerificationJobNotFoundError(f"Unknown verification job: {job_id}")
        if not job.state.terminal:
            job = store.request_cancel(job_id)
        return store.observe(job)

    def _run_verification_job(self, project: Path, job_id: str, lease_fd: int) -> int:
        """Hidden worker entrypoint; the inherited fd is its lifetime authority."""

        store = self._job_store(project)
        job = store.job(job_id)
        if job is None or job.settings is None:
            raise VerificationJobNotFoundError(f"Unknown verification job: {job_id}")
        try:
            inherited = os.fstat(lease_fd)
            expected = worker_lock_path(store.project).stat()
            if (inherited.st_dev, inherited.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                raise OSError("descriptor does not identify this project's worker lock")
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            store.finish(
                job_id,
                VerificationJobState.FAILED,
                error=f"Inherited worker mutation lease is invalid: {exc}",
            )
            return 2

        store.mark_running(job_id, pid=os.getpid())
        stopped = threading.Event()

        def heartbeat() -> None:
            while not stopped.wait(2.0):
                try:
                    store.heartbeat(job_id)
                except Exception:
                    pass

        heartbeat_thread = threading.Thread(
            target=heartbeat, name=f"verification-heartbeat-{job_id[:8]}", daemon=True
        )
        heartbeat_thread.start()
        try:

            def record_progress(event: ProgressEvent) -> None:
                store.append_event(job_id, event)

            snapshot = self.confirm_and_verify(
                store.project,
                job.plan_id,
                job.settings,
                progress=record_progress,
                cancellation=_JobCancellationFlag(store, job_id),
            )
            if snapshot.state == WorkflowState.INTERRUPTED:
                terminal = VerificationJobState.INTERRUPTED
                error = snapshot.error
            elif snapshot.state == WorkflowState.FAILED:
                terminal = VerificationJobState.FAILED
                error = snapshot.error
            else:
                terminal = VerificationJobState.SUCCEEDED
                error = None
            store.finish(job_id, terminal, error=error)
            return 0 if terminal == VerificationJobState.SUCCEEDED else 1
        except BaseException as exc:
            terminal = (
                VerificationJobState.INTERRUPTED
                if isinstance(exc, (KeyboardInterrupt, SystemExit))
                else VerificationJobState.FAILED
            )
            try:
                store.finish(job_id, terminal, error=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
            return 1
        finally:
            stopped.set()
            heartbeat_thread.join(timeout=3.0)
            try:
                os.close(lease_fd)
            except OSError:
                pass

    def _snapshot_for_result(
        self,
        project: Path,
        result: VerificationResult,
        *,
        settings: VerificationSettings,
    ) -> WorkflowSnapshot:
        summary = self._summary(project)
        findings = FindingSummary(
            outcome=result.outcome,
            detail=result.detail,
            verified=result.certified,
            reused=result.reused,
            reconciled=result.reconciled,
            counterexamples=result.counterexamples,
            skipped_unproved=result.skipped_unproved,
            report_path=project / "VERIFICATION_REPORT.md",
            project_path=project,
            failure_report=self.load_failure_report(project, result.run_id),
        )
        if result.outcome == "clarification_required":
            state = WorkflowState.AWAITING_CLARIFICATION
            clarifications = self._presenter(
                project,
                clarification_role=settings.for_task(TaskKind.CLARIFICATION),
                diagnostic_role=settings.for_task(TaskKind.DIAGNOSTIC),
            ).present_all(project, summary.source_path)
            self._record_workflow_state(project, state)
            return WorkflowSnapshot(
                state,
                self._summary(project),
                clarifications=clarifications,
                findings=findings,
            )
        state = (
            WorkflowState.COMPLETED
            if result.exit_code in {0, 11, 12}
            else WorkflowState.FAILED
        )
        self._record_workflow_state(project, state)
        return WorkflowSnapshot(state, self._summary(project), findings=findings)

    def _presenter(
        self,
        project: Path,
        *,
        clarification_role: VerificationRoleSettings | None = None,
        diagnostic_role: VerificationRoleSettings | None = None,
    ) -> ClarificationPresenter:
        narrator = self._provided_narrator
        analyzer = None
        if self.use_codex_clarification:

            def role_config(
                task: TaskKind, role: VerificationRoleSettings | None
            ) -> AIBackendConfig:
                if role is not None:
                    if role.task is not task:
                        raise ValueError(
                            f"Expected the frozen {task.value} role, got "
                            f"{role.task.value}"
                        )
                    driver = DriverId(role.ai_driver)
                    model = role.model
                    difficulty = Difficulty(role.effort)
                else:
                    policy = self._configured_task_policy(task)
                    driver = policy.driver
                    model = policy.model or self.codex_model
                    difficulty = policy.difficulty
                return AIBackendConfig(
                    driver=driver,
                    model=model,
                    difficulty=difficulty,
                    executable=(self.codex if driver is DriverId.CODEX_CLI else None),
                    concurrency=self._concurrency_spec(project=project),
                    task_kind=task,
                    provider_config_path=self._provider_service.config_store.path,
                )

            if narrator is None:
                try:
                    clarification_config = role_config(
                        TaskKind.CLARIFICATION, clarification_role
                    )
                except Exception:
                    # Never cross providers or roles silently. Deterministic
                    # rendering remains available when narration cannot resolve.
                    narrator = None
                else:
                    narrator = IsolatedAIClarificationNarrator(
                        clarification_config, cwd=project
                    )
            try:
                diagnostic_config = role_config(
                    TaskKind.DIAGNOSTIC, diagnostic_role
                )
            except Exception:
                analyzer = None
            else:
                analyzer = IsolatedAIClarificationAnalyzer(
                    diagnostic_config, cwd=project
                )
        return ClarificationPresenter(narrator, analyzer)

    def _summary(self, project: Path) -> ProjectSummary:
        session = IncrementalSession(project)
        config = session._load_config()
        status = session.status()
        latest = status["latest_run"] or {}
        persisted = self._read_workflow_state(project)
        if status["mutation_in_progress"]:
            state = WorkflowState.VERIFYING
        else:
            try:
                state = WorkflowState(str(persisted.get("state", "PROJECT_READY")))
            except ValueError:
                state = WorkflowState.PROJECT_READY
            if state == WorkflowState.VERIFYING and latest.get("status") == "RUNNING":
                # A current verifier would hold the project session lock above.
                # Preserve the abandoned run for explicit recovery without
                # misrepresenting a dead TUI-era worker as active ownership.
                state = WorkflowState.INTERRUPTED
        return ProjectSummary(
            project_id=str(config.get("project_id") or project.resolve()),
            name=str(config.get("name") or project.name),
            project_path=project.resolve(),
            source_path=Path(str(config["manuscript"])).expanduser().resolve(),
            main_file=str(config["main_file"]),
            input_files=tuple(str(value) for value in config.get("input_files", [])),
            last_opened_at=str(
                persisted.get("updated_at") or config.get("created_at") or ""
            ),
            workflow_state=state,
            latest_outcome=latest.get("outcome"),
            open_questions=len(status["open_questions"]),
            source_in_dropbox=bool(
                config.get("source_in_dropbox", is_in_dropbox(config["manuscript"]))
            ),
        )

    @staticmethod
    def _destination_inspection(
        record: ManagedProjectRecord,
    ) -> ProjectDestinationInspection:
        return ProjectDestinationInspection(
            project_path=record.project_path,
            availability=ProjectAvailability(record.kind.value),
            issue=record.issue,
        )

    @staticmethod
    def _deletion_inspection(
        record: ManagedDeletionInspection,
    ) -> ProjectDeletionInspection:
        return ProjectDeletionInspection(
            project_path=record.project_path,
            source_path=record.source_path,
            availability=ProjectDeletionAvailability(
                ProjectDeletionAvailability.READY
                if record.kind == ManagedDeletionKind.READY
                else (
                    ProjectDeletionAvailability.BUSY
                    if record.kind == ManagedDeletionKind.BUSY
                    else ProjectDeletionAvailability.REFUSED
                )
            ),
            issue=record.issue,
            source_in_dropbox=(
                is_in_dropbox(record.source_path)
                if record.source_path is not None
                else False
            ),
        )

    @staticmethod
    def _catalog_entry(
        record: ManagedProjectRecord, *, project: ProjectSummary | None = None
    ) -> ProjectCatalogEntry:
        return ProjectCatalogEntry(
            name=record.name,
            project_path=record.project_path,
            availability=ProjectAvailability(record.kind.value),
            project=project,
            issue=record.issue,
            source_path=record.source_path,
            main_file_candidates=tuple(
                LatexSourceCandidate(path, has_documentclass)
                for path, has_documentclass in record.candidates
            ),
            suggested_main_file=record.suggested_main_file,
        )

    def _findings_from_store(self, project: Path) -> FindingSummary:
        session = IncrementalSession(project)
        with StateStore(session.database_path) as store:
            latest = store.latest_run()
            rows = store.current_claim_rows()
            failure_report = (
                build_failure_report(project, store, int(latest["run_id"]))
                if latest is not None
                else None
            )
        states: dict[str, list[str]] = {}
        for row in rows:
            states.setdefault(str(row["status"]), []).append(str(row["claim_id"]))
        return FindingSummary(
            outcome=str(latest["outcome"] if latest else "unknown"),
            detail=str(latest["detail"] if latest else "No verification run"),
            verified=tuple(sorted(states.get(str(ClaimState.CERTIFIED), []))),
            unresolved=tuple(sorted(states.get(str(ClaimState.UNRESOLVED), []))),
            suspect_false=tuple(sorted(states.get(str(ClaimState.SUSPECT_FALSE), []))),
            counterexamples=tuple(
                sorted(states.get(str(ClaimState.COUNTEREXAMPLE_FOUND), []))
            ),
            skipped_unproved=tuple(
                sorted(states.get(str(ClaimState.SKIPPED_UNPROVED), []))
            ),
            report_path=project / "VERIFICATION_REPORT.md",
            project_path=project,
            failure_report=failure_report,
        )

    def _interrupted_snapshot(
        self, project: Path, exc: WorkflowCancelled | VerificationCancelled
    ) -> WorkflowSnapshot:
        if isinstance(exc, VerificationCancelled) and exc.run_id is not None:
            report = CancellationReport(
                run_id=exc.run_id,
                detail=str(exc),
                preserved_certificates=tuple(exc.preserved_certificates),
                retryable_claims=tuple(exc.retryable_claims),
                temporary_worktrees_cleaned=exc.temporary_worktrees_cleaned,
            )
        else:
            session = IncrementalSession(project)
            with StateStore(session.database_path) as store:
                preserved = tuple(
                    sorted(str(row["claim_id"]) for row in store.certificate_rows())
                )
            report = CancellationReport(
                run_id=None,
                detail=str(exc),
                preserved_certificates=preserved,
                retryable_claims=(),
                temporary_worktrees_cleaned=(
                    exc.temporary_worktrees_cleaned
                    if isinstance(exc, VerificationCancelled)
                    else True
                ),
            )
        self._record_workflow_state(
            project, WorkflowState.INTERRUPTED, error=report.detail
        )
        return WorkflowSnapshot(
            WorkflowState.INTERRUPTED,
            self._summary(project),
            error=report.detail,
            cancellation=report,
        )

    def _ensure_project_task(self, project: Path) -> None:
        session = IncrementalSession(project)
        config = session._load_config()
        owned = project / "VERIFY.yaml"
        configured = session._task_path_from_config(config)
        if owned.is_file() and configured == owned.resolve():
            return
        if configured.is_file():
            text = configured.read_text(encoding="utf-8")
        elif (project / "RepoProverInput" / "TASK.md").is_file():
            text = (project / "RepoProverInput" / "TASK.md").read_text(encoding="utf-8")
        else:
            text = task_document()
        try:
            parse_task_text(text)
        except Exception:
            text = task_document(text)
            parse_task_text(text)
        atomic_write_text(owned, text)
        config["task_file"] = "VERIFY.yaml"
        config["package_version"] = "0.1.0"
        atomic_write_json(session.config_path, config)
        session._commit_host_changes("Migrate to project-owned Proof Assistant task")

    def _record_workflow_state(
        self, project: Path, state: WorkflowState, *, error: str | None = None
    ) -> None:
        atomic_write_json(
            project / ".repoprover" / "workflow.json",
            {
                "schema_version": 1,
                "state": str(state),
                "updated_at": _now(),
                "error": error,
            },
        )
        try:
            self.catalog.upsert(project)
        except Exception:
            pass

    @staticmethod
    def _read_workflow_state(project: Path) -> JSONObject:
        try:
            return json_object(
                load_json(
                    (project / ".repoprover" / "workflow.json").read_text(
                        encoding="utf-8"
                    )
                ),
                path="workflow state",
            )
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _checkpoint(cancellation: CancellationToken | None) -> None:
        if cancellation is not None:
            cancellation.raise_if_cancelled()

    def _emit(
        self,
        sink: ProgressSink | None,
        phase: ProgressPhase,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if sink is None:
            return
        self._sequence += 1
        payload = details or {}
        completed = payload.get("completed")
        total = payload.get("total")
        claim_id = payload.get("claim_id")
        sink(
            ProgressEvent(
                self._sequence,
                phase,
                message,
                completed=(
                    completed
                    if isinstance(completed, int) and not isinstance(completed, bool)
                    else None
                ),
                total=(
                    total
                    if isinstance(total, int) and not isinstance(total, bool)
                    else None
                ),
                claim_id=claim_id if isinstance(claim_id, str) else None,
                details=payload,
            )
        )
