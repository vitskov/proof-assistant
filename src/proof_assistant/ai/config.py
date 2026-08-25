"""Atomic, revisioned, machine-scoped AI provider preferences.

The store never accepts or serializes secret values.  API credentials remain in
environment variables or an injected operating-system credential store.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .contracts import (
    CredentialSource,
    Difficulty,
    DriverId,
    DriverPreference,
    MachineProviderSettings,
    ProviderConfig,
    ProviderConfigError,
    ProviderConfigRevisionError,
    TaskKind,
    TaskPreference,
    validate_model_identifier,
)

PROVIDER_CONFIG_SCHEMA_VERSION = 1
_FORBIDDEN_KEYS = {
    "api_key",
    "apikey",
    "credential",
    "password",
    "secret",
    "token",
    "value",
}
_FORBIDDEN_COMPACT_KEYS = {
    "apikey",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
    "privatekey",
    "bearertoken",
    "password",
    "credential",
}


def default_provider_config_path(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    source = os.environ if environ is None else environ
    explicit_xdg = source.get("XDG_CONFIG_HOME") if environ is not None else None
    root = Path(
        explicit_xdg
        or ((home / ".config") if home is not None else None)
        or source.get("XDG_CONFIG_HOME")
        or Path.home() / ".config"
    ).expanduser()
    return root / "proof-assistant" / "providers.json"


def _reject_secret_fields(value: object, path: str = "settings") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
            compact = normalized.replace("_", "")
            if (
                normalized in _FORBIDDEN_KEYS
                or normalized.endswith(("_key", "_token", "_secret"))
                or compact in _FORBIDDEN_COMPACT_KEYS
            ):
                raise ProviderConfigError(
                    f"{path}.{key} is forbidden; credentials must never be persisted"
                )
            _reject_secret_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, f"{path}[{index}]")


def _expect_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProviderConfigError(f"{path} must be a mapping")
    return value


def _enum(
    enum_type: type[DriverId]
    | type[CredentialSource]
    | type[Difficulty]
    | type[TaskKind],
    value: object,
    path: str,
) -> object:
    if not isinstance(value, str):
        raise ProviderConfigError(f"{path} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        choices = ", ".join(item.value for item in enum_type)
        raise ProviderConfigError(f"{path} must be one of: {choices}") from exc


def _optional_model(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderConfigError(f"{path} must be null or a safe provider identifier")
    try:
        return validate_model_identifier(value, field_name=path)
    except ValueError as exc:
        raise ProviderConfigError(str(exc)) from exc


def _optional_nonempty_string(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigError(f"{path} must be null or a non-empty string")
    return value


def config_from_mapping(payload: Mapping[str, object]) -> ProviderConfig:
    allowed = {"primary_driver", "drivers", "tasks"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ProviderConfigError("Unknown provider setting(s): " + ", ".join(unknown))

    primary = _enum(
        DriverId,
        payload.get("primary_driver", DriverId.CODEX_CLI.value),
        "providers.primary_driver",
    )

    raw_drivers = payload.get("drivers", [])
    if not isinstance(raw_drivers, list):
        raise ProviderConfigError("providers.drivers must be a list")
    drivers: list[DriverPreference] = []
    for index, raw_driver in enumerate(raw_drivers):
        path = f"providers.drivers[{index}]"
        item = _expect_mapping(raw_driver, path)
        unknown_item = sorted(
            set(item)
            - {
                "driver",
                "credential_source",
                "model",
                "difficulty",
                "enabled",
                "runtime_verified_version",
            }
        )
        if unknown_item:
            raise ProviderConfigError(
                f"Unknown {path} field(s): " + ", ".join(unknown_item)
            )
        driver = _enum(DriverId, item.get("driver"), f"{path}.driver")
        source = _enum(
            CredentialSource,
            item.get("credential_source", CredentialSource.NONE.value),
            f"{path}.credential_source",
        )
        difficulty = _enum(
            Difficulty,
            item.get("difficulty", Difficulty.AUTO.value),
            f"{path}.difficulty",
        )
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ProviderConfigError(f"{path}.enabled must be true or false")
        drivers.append(
            DriverPreference(
                driver=driver,  # type: ignore[arg-type]
                credential_source=source,  # type: ignore[arg-type]
                model=_optional_model(item.get("model"), f"{path}.model"),
                difficulty=difficulty,  # type: ignore[arg-type]
                enabled=enabled,
                runtime_verified_version=_optional_nonempty_string(
                    item.get("runtime_verified_version"),
                    f"{path}.runtime_verified_version",
                ),
            )
        )

    raw_tasks = payload.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raise ProviderConfigError("providers.tasks must be a list")
    tasks: list[TaskPreference] = []
    for index, raw_task in enumerate(raw_tasks):
        path = f"providers.tasks[{index}]"
        item = _expect_mapping(raw_task, path)
        unknown_item = sorted(set(item) - {"task", "driver", "model", "difficulty"})
        if unknown_item:
            raise ProviderConfigError(
                f"Unknown {path} field(s): " + ", ".join(unknown_item)
            )
        raw_driver = item.get("driver")
        task_driver = (
            None
            if raw_driver is None
            else _enum(DriverId, raw_driver, f"{path}.driver")
        )
        tasks.append(
            TaskPreference(
                task=_enum(TaskKind, item.get("task"), f"{path}.task"),  # type: ignore[arg-type]
                driver=task_driver,  # type: ignore[arg-type]
                model=_optional_model(item.get("model"), f"{path}.model"),
                difficulty=_enum(  # type: ignore[arg-type]
                    Difficulty,
                    item.get("difficulty", Difficulty.AUTO.value),
                    f"{path}.difficulty",
                ),
            )
        )

    if not drivers:
        drivers = list(ProviderConfig().drivers)
    return ProviderConfig(
        primary_driver=primary,  # type: ignore[arg-type]
        drivers=tuple(drivers),
        tasks=tuple(tasks),
    )


def config_to_mapping(config: ProviderConfig) -> dict[str, object]:
    return {
        "primary_driver": config.primary_driver.value,
        "drivers": [
            {
                "driver": item.driver.value,
                "credential_source": item.credential_source.value,
                "model": item.model,
                "difficulty": item.difficulty.value,
                "enabled": item.enabled,
                "runtime_verified_version": item.runtime_verified_version,
            }
            for item in config.drivers
        ],
        "tasks": [
            {
                "task": item.task.value,
                "driver": item.driver.value if item.driver is not None else None,
                "model": item.model,
                "difficulty": item.difficulty.value,
            }
            for item in config.tasks
        ],
    }


class MachineProviderConfigStore:
    """Store machine-wide provider preferences with optimistic revision checks."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (
            (path or default_provider_config_path()).expanduser().resolve(strict=False)
        )
        if any(part.casefold().startswith("dropbox") for part in self.path.parts):
            raise ProviderConfigError(
                "Machine provider settings cannot reside in Dropbox"
            )
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _load_unlocked(self) -> MachineProviderSettings:
        if not self.path.exists():
            return MachineProviderSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderConfigError(
                f"Invalid machine provider settings at {self.path}: {exc}"
            ) from exc
        _reject_secret_fields(raw)
        document = _expect_mapping(raw, "settings")
        unknown_document = sorted(
            set(document) - {"schema_version", "scope", "revision", "providers"}
        )
        if unknown_document:
            raise ProviderConfigError(
                "Unknown machine provider document field(s): "
                + ", ".join(unknown_document)
            )
        if document.get("schema_version") != PROVIDER_CONFIG_SCHEMA_VERSION:
            raise ProviderConfigError(
                "Unsupported machine provider settings schema version"
            )
        if document.get("scope") != "MACHINE":
            raise ProviderConfigError("Provider settings must declare scope: MACHINE")
        revision = document.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ProviderConfigError("Provider settings revision must be non-negative")
        providers = _expect_mapping(document.get("providers", {}), "providers")
        return MachineProviderSettings(
            revision=revision,
            config=config_from_mapping(providers),
            schema_version=PROVIDER_CONFIG_SCHEMA_VERSION,
        )

    def load(self) -> MachineProviderSettings:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                return self._load_unlocked()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def save(
        self, config: ProviderConfig, *, expected_revision: int
    ) -> MachineProviderSettings:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                current = self._load_unlocked()
                if current.revision != expected_revision:
                    raise ProviderConfigRevisionError(
                        expected_revision, current.revision
                    )
                candidate = MachineProviderSettings(
                    revision=current.revision + 1,
                    config=config,
                    schema_version=PROVIDER_CONFIG_SCHEMA_VERSION,
                )
                self._write_unlocked(candidate)
                return candidate
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _write_unlocked(self, settings: MachineProviderSettings) -> None:
        payload: dict[str, object] = {
            "schema_version": settings.schema_version,
            "scope": "MACHINE",
            "revision": settings.revision,
            "providers": config_to_mapping(settings.config),
        }
        _reject_secret_fields(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(payload, temporary, indent=2, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                os.fchmod(temporary.fileno(), 0o600)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
