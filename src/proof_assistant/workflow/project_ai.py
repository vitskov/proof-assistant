"""Atomic, revisioned AI-provider overrides for one managed project."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .contracts import Difficulty, DriverId, ProjectAIOverride

PROJECT_AI_SETTINGS_SCHEMA_VERSION = 1
_DOCUMENT_FIELDS = {"schema_version", "scope", "revision", "override"}
_OVERRIDE_FIELDS = {"ai_driver", "model", "difficulty"}
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
    "accesstoken",
    "apikey",
    "bearertoken",
    "clientsecret",
    "credential",
    "password",
    "privatekey",
    "refreshtoken",
}


class ProjectAISettingsError(ValueError):
    """A project AI settings document or project path is invalid."""


class ProjectAISettingsRevisionError(ProjectAISettingsError):
    """The project AI settings changed after the caller loaded them."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Project AI settings revision conflict: expected {expected}, found {actual}"
        )


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
                raise ProjectAISettingsError(
                    f"{path}.{key} is forbidden; credentials must never be persisted"
                )
            _reject_secret_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, f"{path}[{index}]")


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProjectAISettingsError(f"{path} must be a mapping")
    return value


def _strict_json(text: str) -> object:
    def reject_duplicate_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ProjectAISettingsError(f"Duplicate settings field: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=reject_duplicate_fields)


class ProjectAISettingsStore:
    """Persist a secret-free provider override beneath a validated project root."""

    def __init__(self, project_path: Path) -> None:
        try:
            project = project_path.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProjectAISettingsError(
                "Project AI settings require an existing project directory"
            ) from exc
        if not project.is_dir():
            raise ProjectAISettingsError(
                "Project AI settings require an existing project directory"
            )
        self.project_path = project
        self.path = project / ".repoprover" / "verification-settings.json"
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load(self) -> tuple[int, ProjectAIOverride | None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                return self._load_unlocked()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def save(
        self,
        override: ProjectAIOverride | None,
        *,
        expected_revision: int,
    ) -> tuple[int, ProjectAIOverride | None]:
        if override is not None and not isinstance(override, ProjectAIOverride):
            raise ProjectAISettingsError("override must be a ProjectAIOverride or None")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                current_revision, _ = self._load_unlocked()
                if current_revision != expected_revision:
                    raise ProjectAISettingsRevisionError(
                        expected_revision, current_revision
                    )
                state = (current_revision + 1, override)
                self._write_unlocked(*state)
                return state
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _load_unlocked(self) -> tuple[int, ProjectAIOverride | None]:
        if not self.path.exists():
            return 0, None
        try:
            raw = _strict_json(self.path.read_text(encoding="utf-8"))
        except ProjectAISettingsError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectAISettingsError(
                f"Invalid project AI settings at {self.path}"
            ) from exc

        _reject_secret_fields(raw)
        document = _mapping(raw, "settings")
        unknown = sorted(set(document) - _DOCUMENT_FIELDS)
        missing = sorted(_DOCUMENT_FIELDS - set(document))
        if unknown:
            raise ProjectAISettingsError(
                "Unknown project AI settings field(s): " + ", ".join(unknown)
            )
        if missing:
            raise ProjectAISettingsError(
                "Missing project AI settings field(s): " + ", ".join(missing)
            )
        if document["schema_version"] != PROJECT_AI_SETTINGS_SCHEMA_VERSION:
            raise ProjectAISettingsError(
                "Unsupported project AI settings schema version"
            )
        if document["scope"] != "PROJECT":
            raise ProjectAISettingsError(
                "Project AI settings must declare scope: PROJECT"
            )
        revision = document["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ProjectAISettingsError(
                "Project AI settings revision must be non-negative"
            )
        return revision, self._override_from_value(document["override"])

    @staticmethod
    def _override_from_value(value: object) -> ProjectAIOverride | None:
        if value is None:
            return None
        item = _mapping(value, "settings.override")
        unknown = sorted(set(item) - _OVERRIDE_FIELDS)
        missing = sorted(_OVERRIDE_FIELDS - set(item))
        if unknown:
            raise ProjectAISettingsError(
                "Unknown project AI override field(s): " + ", ".join(unknown)
            )
        if missing:
            raise ProjectAISettingsError(
                "Missing project AI override field(s): " + ", ".join(missing)
            )
        driver_value = item["ai_driver"]
        model_value = item["model"]
        difficulty_value = item["difficulty"]
        if not (
            isinstance(driver_value, str)
            and isinstance(model_value, str)
            and isinstance(difficulty_value, str)
        ):
            raise ProjectAISettingsError("Project AI override fields must be strings")
        try:
            return ProjectAIOverride(
                ai_driver=DriverId(driver_value),
                model=model_value,
                difficulty=Difficulty(difficulty_value),
            )
        except (TypeError, ValueError) as exc:
            raise ProjectAISettingsError("Invalid project AI override") from exc

    def _write_unlocked(
        self, revision: int, override: ProjectAIOverride | None
    ) -> None:
        payload: dict[str, object] = {
            "schema_version": PROJECT_AI_SETTINGS_SCHEMA_VERSION,
            "scope": "PROJECT",
            "revision": revision,
            "override": (
                None
                if override is None
                else {
                    "ai_driver": override.ai_driver.value,
                    "model": override.model,
                    "difficulty": override.difficulty.value,
                }
            ),
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
