"""Immutable, provider-neutral contracts for AI setup and model selection.

These types deliberately contain no credential values.  They are safe to pass
to the TUI, serialize into run metadata, and include in diagnostics.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


def _contains_terminal_controls(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def validate_model_identifier(value: str, *, field_name: str) -> str:
    """Return a provider model ID only when it is safe for logs and terminal UIs."""

    if not _MODEL_ID.fullmatch(value):
        raise ValueError(f"{field_name} must be a safe provider identifier")
    return value


class DriverId(StrEnum):
    CODEX_CLI = "codex_cli"
    CLAUDE_CLI = "claude_cli"
    COPILOT_CLI = "copilot_cli"
    OPENAI_API = "openai_api"
    ANTHROPIC_API = "anthropic_api"
    GEMINI_API = "gemini_api"


class DriverTransport(StrEnum):
    CLI = "cli"
    API = "api"


class CredentialSource(StrEnum):
    NONE = "none"
    ENVIRONMENT = "environment"
    CREDENTIAL_STORE = "credential_store"


class InstallationState(StrEnum):
    INSTALLED = "installed"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"
    BROKEN = "broken"


class AuthenticationState(StrEnum):
    AUTHENTICATED = "authenticated"
    REQUIRED = "required"
    UNKNOWN = "unknown"
    ERROR = "error"


class DiscoverySource(StrEnum):
    LIVE_ACCOUNT = "live_account"
    CURATED_FALLBACK = "curated_fallback"
    UNAVAILABLE = "unavailable"


class Difficulty(StrEnum):
    AUTO = "auto"
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class TaskKind(StrEnum):
    CLARIFICATION = "clarification"
    DIAGNOSTIC = "diagnostic"
    PROOF = "proof"
    SKETCH = "sketch"
    MAINTENANCE = "maintenance"
    REVIEW = "review"
    DUPLICATE_PROOF = "duplicate_proof"
    REPORTING = "reporting"


class SetupActionState(StrEnum):
    AVAILABLE = "available"
    NOT_NEEDED = "not_needed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    model_id: str
    display_name: str
    difficulties: tuple[Difficulty, ...] = (Difficulty.AUTO,)
    input_modalities: tuple[str, ...] = ("text",)

    def __post_init__(self) -> None:
        validate_model_identifier(self.model_id, field_name="model_id")
        if not self.display_name.strip():
            raise ValueError("display_name must not be empty")
        if _contains_terminal_controls(self.display_name):
            raise ValueError(
                "display_name must not contain terminal control characters"
            )
        if len(set(self.difficulties)) != len(self.difficulties):
            raise ValueError("model difficulties must be unique")


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    driver: DriverId
    models: tuple[ModelDescriptor, ...] = ()
    source: DiscoverySource = DiscoverySource.UNAVAILABLE
    detail: str = "Model discovery has not run."
    contract_approved: bool = False

    def __post_init__(self) -> None:
        if self.source is DiscoverySource.LIVE_ACCOUNT and not self.models:
            raise ValueError("a live model catalog must contain at least one model")
        if self.contract_approved and self.source is DiscoverySource.UNAVAILABLE:
            raise ValueError("an unavailable catalog cannot be contract approved")

    @property
    def live(self) -> bool:
        return self.source is DiscoverySource.LIVE_ACCOUNT


@dataclass(frozen=True, slots=True)
class DriverStatus:
    driver: DriverId
    transport: DriverTransport
    installation: InstallationState
    authentication: AuthenticationState
    executable: str | None = None
    version: str | None = None
    detail: str = ""
    catalog: ModelCatalog | None = None

    @property
    def ready(self) -> bool:
        installation_ready = self.installation in {
            InstallationState.INSTALLED,
            InstallationState.NOT_APPLICABLE,
        }
        return (
            installation_ready
            and self.authentication is AuthenticationState.AUTHENTICATED
        )


@dataclass(frozen=True, slots=True)
class ProviderSetupSnapshot:
    settings: MachineProviderSettings
    statuses: tuple[DriverStatus, ...]
    primary_driver: DriverId
    primary_ready: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DriverPreference:
    driver: DriverId
    credential_source: CredentialSource = CredentialSource.ENVIRONMENT
    model: str | None = None
    difficulty: Difficulty = Difficulty.AUTO
    enabled: bool = True
    runtime_verified_version: str | None = None

    def __post_init__(self) -> None:
        if self.model is not None:
            validate_model_identifier(self.model, field_name="model")
        if (
            self.runtime_verified_version is not None
            and not self.runtime_verified_version.strip()
        ):
            raise ValueError(
                "runtime_verified_version must be None or a non-empty version"
            )


@dataclass(frozen=True, slots=True)
class TaskPreference:
    task: TaskKind
    driver: DriverId | None = None
    model: str | None = None
    difficulty: Difficulty = Difficulty.AUTO

    def __post_init__(self) -> None:
        if self.model is not None:
            validate_model_identifier(self.model, field_name="model")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    primary_driver: DriverId = DriverId.CODEX_CLI
    drivers: tuple[DriverPreference, ...] = field(
        default_factory=lambda: tuple(
            DriverPreference(
                driver=driver,
                credential_source=(
                    CredentialSource.NONE
                    if driver
                    in {
                        DriverId.CODEX_CLI,
                        DriverId.CLAUDE_CLI,
                        DriverId.COPILOT_CLI,
                    }
                    else CredentialSource.ENVIRONMENT
                ),
            )
            for driver in DriverId
        )
    )
    tasks: tuple[TaskPreference, ...] = ()

    def __post_init__(self) -> None:
        driver_ids = tuple(item.driver for item in self.drivers)
        if len(driver_ids) != len(set(driver_ids)):
            raise ValueError("driver preferences must be unique")
        primary = next(
            (item for item in self.drivers if item.driver is self.primary_driver),
            None,
        )
        if primary is None:
            raise ValueError("primary driver must have a driver preference")
        if not primary.enabled:
            raise ValueError("primary driver must be enabled")
        task_ids = tuple(item.task for item in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task preferences must be unique")

    def preference_for(self, driver: DriverId) -> DriverPreference:
        for preference in self.drivers:
            if preference.driver is driver:
                return preference
        return DriverPreference(driver=driver)

    def task_preference_for(self, task: TaskKind) -> TaskPreference | None:
        for preference in self.tasks:
            if preference.task is task:
                return preference
        return None


@dataclass(frozen=True, slots=True)
class MachineProviderSettings:
    revision: int = 0
    config: ProviderConfig = field(default_factory=ProviderConfig)
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class CommandSpec:
    argv: tuple[str, ...]
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.argv or any(not item for item in self.argv):
            raise ValueError("command arguments must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("command timeout must be positive")


@dataclass(frozen=True, slots=True)
class InstallPlan:
    driver: DriverId
    state: SetupActionState
    commands: tuple[CommandSpec, ...]
    expected_executable: str | None
    installer_bin: str | None
    consent_token: str
    detail: str


@dataclass(frozen=True, slots=True)
class InstallResult:
    driver: DriverId
    attempted: bool
    succeeded: bool
    status: DriverStatus
    detail: str


@dataclass(frozen=True, slots=True)
class TaskModelPolicy:
    task: TaskKind
    driver: DriverId
    model: str | None
    difficulty: Difficulty
    model_source: DiscoverySource
    explanation: str


class ProviderError(RuntimeError):
    """Base class for redacted provider setup failures."""


class ProviderConfigError(ProviderError, ValueError):
    pass


class ProviderConfigRevisionError(ProviderConfigError):
    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Provider settings changed (expected revision {expected}, found {actual})"
        )


class InstallConsentError(ProviderError, ValueError):
    pass


class UnsupportedDifficultyError(ProviderError, ValueError):
    def __init__(
        self,
        driver: DriverId,
        model: str | None,
        difficulty: Difficulty,
        allowed: tuple[Difficulty, ...],
    ) -> None:
        self.driver = driver
        self.model = model
        self.difficulty = difficulty
        self.allowed = allowed
        joined = ", ".join(item.value for item in allowed)
        super().__init__(
            f"{difficulty.value!r} is not supported by {driver.value}"
            f"{f' model {model!r}' if model else ''}; choose one of: {joined}"
        )
