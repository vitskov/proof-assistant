"""Typed, machine-scoped concurrency configuration.

The persisted file contains only machine preferences.  Runtime measurements and
calibration live in the cache, while a future project overlay can be supplied to
``resolve_concurrency_config`` without changing persistence or precedence.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import cast

import yaml

MACHINE_CONFIG_SCHEMA_VERSION = 1


class ConcurrencyMode(StrEnum):
    ADAPTIVE = "adaptive"
    FIXED = "fixed"


class ResourceProfile(StrEnum):
    AUTO = "auto"
    INTERACTIVE = "interactive"
    SERVER = "server"


class CodexPlan(StrEnum):
    UNKNOWN = "unknown"
    PLUS = "plus"
    PRO_5X = "pro_5x"
    PRO_20X = "pro_20x"
    API = "api"


class BudgetPolicy(StrEnum):
    ECONOMY = "economy"
    BALANCED = "balanced"
    THROUGHPUT = "throughput"


class PressureState(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    EMERGENCY = "emergency"


class AutoValue(StrEnum):
    AUTO = "auto"


class ConfigScope(StrEnum):
    DEFAULT = "DEFAULT"
    MACHINE = "MACHINE"
    PROJECT = "PROJECT"
    ENVIRONMENT = "ENVIRONMENT"
    CLI = "CLI"


AutoInt = int | AutoValue


@dataclass(frozen=True)
class AIConcurrencyConfig:
    plan: CodexPlan = CodexPlan.UNKNOWN
    initial: AutoInt = AutoValue.AUTO
    hard_max: AutoInt = AutoValue.AUTO
    minimum: int = 1
    budget_policy: BudgetPolicy = BudgetPolicy.BALANCED
    increase_after_successes: AutoInt = AutoValue.AUTO
    increase_cooldown_seconds: float = 60.0
    throttle_multiplier: float = 0.5


@dataclass(frozen=True)
class LeanConcurrencyConfig:
    pool_size: AutoInt = AutoValue.AUTO
    min_pool: int = 1
    max_pool: AutoInt = AutoValue.AUTO
    memory_calibration: bool = True
    fallback_memory_per_repl_gib: float = 3.0
    p95_safety_multiplier: float = 1.5
    initial_auto_cap: int = 32


@dataclass(frozen=True)
class BuildConcurrencyConfig:
    max_concurrent: AutoInt = AutoValue.AUTO
    min_concurrent: int = 1
    hard_max: int = 8


@dataclass(frozen=True)
class SchedulerConcurrencyConfig:
    agents_per_target_initial: int = 1
    agents_per_target_max: int = 4
    dependency_priority: bool = True
    duplicate_agent_escalation: bool = True


@dataclass(frozen=True)
class LegacyVerificationConfig:
    """Compatibility knobs used by the pre-controller verification path."""

    jobs: int = 2
    batch_size: int = 8
    lean_pool_size: int = 1


@dataclass(frozen=True)
class ConcurrencyConfig:
    mode: ConcurrencyMode = ConcurrencyMode.ADAPTIVE
    resource_profile: ResourceProfile = ResourceProfile.AUTO
    telemetry_enabled: bool = True
    ai: AIConcurrencyConfig = field(default_factory=AIConcurrencyConfig)
    lean: LeanConcurrencyConfig = field(default_factory=LeanConcurrencyConfig)
    build: BuildConcurrencyConfig = field(default_factory=BuildConcurrencyConfig)
    scheduler: SchedulerConcurrencyConfig = field(
        default_factory=SchedulerConcurrencyConfig
    )
    legacy: LegacyVerificationConfig = field(default_factory=LegacyVerificationConfig)


@dataclass(frozen=True)
class AIConcurrencyPatch:
    plan: CodexPlan | None = None
    initial: AutoInt | None = None
    hard_max: AutoInt | None = None
    minimum: int | None = None
    budget_policy: BudgetPolicy | None = None
    increase_after_successes: AutoInt | None = None
    increase_cooldown_seconds: float | None = None
    throttle_multiplier: float | None = None


@dataclass(frozen=True)
class LeanConcurrencyPatch:
    pool_size: AutoInt | None = None
    min_pool: int | None = None
    max_pool: AutoInt | None = None
    memory_calibration: bool | None = None
    fallback_memory_per_repl_gib: float | None = None
    p95_safety_multiplier: float | None = None
    initial_auto_cap: int | None = None


@dataclass(frozen=True)
class BuildConcurrencyPatch:
    max_concurrent: AutoInt | None = None
    min_concurrent: int | None = None
    hard_max: int | None = None


@dataclass(frozen=True)
class SchedulerConcurrencyPatch:
    agents_per_target_initial: int | None = None
    agents_per_target_max: int | None = None
    dependency_priority: bool | None = None
    duplicate_agent_escalation: bool | None = None


@dataclass(frozen=True)
class LegacyVerificationPatch:
    jobs: int | None = None
    batch_size: int | None = None
    lean_pool_size: int | None = None


@dataclass(frozen=True)
class ConcurrencyConfigPatch:
    mode: ConcurrencyMode | None = None
    resource_profile: ResourceProfile | None = None
    telemetry_enabled: bool | None = None
    ai: AIConcurrencyPatch = field(default_factory=AIConcurrencyPatch)
    lean: LeanConcurrencyPatch = field(default_factory=LeanConcurrencyPatch)
    build: BuildConcurrencyPatch = field(default_factory=BuildConcurrencyPatch)
    scheduler: SchedulerConcurrencyPatch = field(
        default_factory=SchedulerConcurrencyPatch
    )
    legacy: LegacyVerificationPatch = field(default_factory=LegacyVerificationPatch)


@dataclass(frozen=True)
class MachineConcurrencySettings:
    revision: int
    patch: ConcurrencyConfigPatch
    scope: ConfigScope = ConfigScope.MACHINE
    schema_version: int = MACHINE_CONFIG_SCHEMA_VERSION


@dataclass(frozen=True)
class ResolvedConcurrencyConfig:
    config: ConcurrencyConfig
    sources: Mapping[str, ConfigScope]
    machine_revision: int = 0

    def source_for(self, field_path: str) -> ConfigScope:
        try:
            return self.sources[field_path]
        except KeyError as exc:
            raise KeyError(f"Unknown concurrency setting: {field_path}") from exc


class ConcurrencyConfigError(ValueError):
    pass


class MachineConfigRevisionError(ConcurrencyConfigError):
    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Machine concurrency settings changed (expected revision {expected}, "
            f"found {actual})"
        )


class MachineConfigLocationError(ConcurrencyConfigError):
    pass


_NESTED_PATCH_TYPES = {
    "ai": AIConcurrencyPatch,
    "lean": LeanConcurrencyPatch,
    "build": BuildConcurrencyPatch,
    "scheduler": SchedulerConcurrencyPatch,
    "legacy": LegacyVerificationPatch,
}
_ENUM_FIELDS = {
    "mode": ConcurrencyMode,
    "resource_profile": ResourceProfile,
    "ai.plan": CodexPlan,
    "ai.budget_policy": BudgetPolicy,
}
_AUTO_FIELDS = {
    "ai.initial",
    "ai.hard_max",
    "ai.increase_after_successes",
    "lean.pool_size",
    "lean.max_pool",
    "build.max_concurrent",
}


def default_machine_config_path(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    source = os.environ if environ is None else environ
    root = Path(
        source.get("XDG_CONFIG_HOME") or (home or Path.home()) / ".config"
    ).expanduser()
    return root / "proof-assistant" / "settings.yaml"


def _coerce_auto_int(value: object, field_path: str) -> AutoInt:
    if value == AutoValue.AUTO:
        return AutoValue.AUTO
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConcurrencyConfigError(f"{field_path} must be 'auto' or an integer")
    return value


def _coerce_patch_value(field_path: str, value: object) -> object:
    if field_path in _AUTO_FIELDS:
        return _coerce_auto_int(value, field_path)
    enum_type = _ENUM_FIELDS.get(field_path)
    if enum_type is not None:
        if not isinstance(value, str):
            raise ConcurrencyConfigError(f"{field_path} must be a string")
        try:
            return enum_type(value)
        except (TypeError, ValueError) as exc:
            choices = ", ".join(item.value for item in enum_type)
            raise ConcurrencyConfigError(
                f"{field_path} must be one of: {choices}"
            ) from exc
    return value


def patch_from_mapping(payload: Mapping[str, object]) -> ConcurrencyConfigPatch:
    allowed = {item.name for item in fields(ConcurrencyConfigPatch)}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ConcurrencyConfigError(
            "Unknown concurrency setting(s): " + ", ".join(unknown)
        )
    arguments: dict[str, object] = {}
    for name in ("mode", "resource_profile", "telemetry_enabled"):
        if name in payload:
            arguments[name] = _coerce_patch_value(name, payload[name])
    for section, patch_type in _NESTED_PATCH_TYPES.items():
        raw = payload.get(section)
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise ConcurrencyConfigError(f"concurrency.{section} must be a mapping")
        allowed_section = {item.name for item in fields(patch_type)}
        unknown_section = sorted(set(raw) - allowed_section)
        if unknown_section:
            raise ConcurrencyConfigError(
                f"Unknown concurrency.{section} setting(s): "
                + ", ".join(unknown_section)
            )
        constructor = cast(Callable[..., object], patch_type)
        arguments[section] = constructor(
            **{
                key: _coerce_patch_value(f"{section}.{key}", value)
                for key, value in raw.items()
            }
        )
    constructor = cast(Callable[..., ConcurrencyConfigPatch], ConcurrencyConfigPatch)
    return constructor(**arguments)


def patch_to_mapping(patch: ConcurrencyConfigPatch) -> dict[str, object]:
    payload: dict[str, object] = {}
    if patch.mode is not None:
        payload["mode"] = patch.mode.value
    if patch.resource_profile is not None:
        payload["resource_profile"] = patch.resource_profile.value
    if patch.telemetry_enabled is not None:
        payload["telemetry_enabled"] = patch.telemetry_enabled
    for section in _NESTED_PATCH_TYPES:
        section_patch = getattr(patch, section)
        values: dict[str, object] = {}
        for item in fields(section_patch):
            value = getattr(section_patch, item.name)
            if value is None:
                continue
            values[item.name] = value.value if isinstance(value, StrEnum) else value
        if values:
            payload[section] = values
    return payload


class MachineConfigStore:
    """Atomic revisioned store for settings shared by all local projects."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (
            (path or default_machine_config_path()).expanduser().resolve(strict=False)
        )
        if any(part.casefold().startswith("dropbox") for part in self.path.parts):
            raise MachineConfigLocationError(
                "Machine concurrency settings cannot reside in Dropbox"
            )
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _load_unlocked(self) -> MachineConcurrencySettings:
        if not self.path.exists():
            return MachineConcurrencySettings(0, ConcurrencyConfigPatch())
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConcurrencyConfigError(
                f"Invalid machine concurrency settings at {self.path}: {exc}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise ConcurrencyConfigError("Machine settings must be a YAML mapping")
        if raw.get("schema_version") != MACHINE_CONFIG_SCHEMA_VERSION:
            raise ConcurrencyConfigError(
                "Unsupported machine concurrency settings schema version"
            )
        if raw.get("scope") != ConfigScope.MACHINE:
            raise ConcurrencyConfigError("Machine settings must declare scope: MACHINE")
        revision = raw.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ConcurrencyConfigError(
                "Machine settings revision must be non-negative"
            )
        concurrency = raw.get("concurrency", {})
        if not isinstance(concurrency, Mapping):
            raise ConcurrencyConfigError(
                "Machine concurrency settings must be a mapping"
            )
        return MachineConcurrencySettings(
            revision=revision,
            patch=patch_from_mapping(concurrency),
        )

    def load(self) -> MachineConcurrencySettings:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                return self._load_unlocked()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def save(
        self, patch: ConcurrencyConfigPatch, *, expected_revision: int
    ) -> MachineConcurrencySettings:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                current = self._load_unlocked()
                if current.revision != expected_revision:
                    raise MachineConfigRevisionError(
                        expected_revision, current.revision
                    )
                candidate = MachineConcurrencySettings(
                    revision=current.revision + 1,
                    patch=patch,
                )
                # Resolve and validate before replacing the last known-good file.
                resolve_concurrency_config(machine=candidate)
                self._write_unlocked(candidate)
                return candidate
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _write_unlocked(self, settings: MachineConcurrencySettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": settings.schema_version,
            "scope": settings.scope.value,
            "revision": settings.revision,
            "concurrency": patch_to_mapping(settings.patch),
        }
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
                yaml.safe_dump(payload, temporary, sort_keys=False)
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


def _iter_patch_values(
    patch: ConcurrencyConfigPatch,
) -> Iterator[tuple[str, object]]:
    for name in ("mode", "resource_profile", "telemetry_enabled"):
        value = getattr(patch, name)
        if value is not None:
            yield name, value
    for section in _NESTED_PATCH_TYPES:
        section_patch = getattr(patch, section)
        for item in fields(section_patch):
            value = getattr(section_patch, item.name)
            if value is not None:
                yield f"{section}.{item.name}", value


def _config_leaf_values(config: ConcurrencyConfig) -> Iterator[tuple[str, object]]:
    yield "mode", config.mode
    yield "resource_profile", config.resource_profile
    yield "telemetry_enabled", config.telemetry_enabled
    for section in _NESTED_PATCH_TYPES:
        value = getattr(config, section)
        for item in fields(value):
            yield f"{section}.{item.name}", getattr(value, item.name)


def _set_leaf(config: ConcurrencyConfig, path: str, value: object) -> ConcurrencyConfig:
    replace_config = cast(Callable[..., ConcurrencyConfig], replace)
    if "." not in path:
        return replace_config(config, **{path: value})
    section, name = path.split(".", 1)
    section_value = getattr(config, section)
    replace_section = cast(Callable[..., object], replace)
    return replace_config(
        config, **{section: replace_section(section_value, **{name: value})}
    )


def _positive_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConcurrencyConfigError(f"{path} must be a positive integer")
    return value


def _positive_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConcurrencyConfigError(f"{path} must be a positive number")
    return float(value)


def _validate_config(config: ConcurrencyConfig) -> None:
    if not isinstance(config.telemetry_enabled, bool):
        raise ConcurrencyConfigError("telemetry_enabled must be true or false")
    auto_paths = {
        path: value
        for path, value in _config_leaf_values(config)
        if path in _AUTO_FIELDS
    }
    for path, value in auto_paths.items():
        if value != AutoValue.AUTO:
            _positive_integer(value, path)

    for path in (
        "ai.minimum",
        "lean.min_pool",
        "lean.initial_auto_cap",
        "build.min_concurrent",
        "build.hard_max",
        "scheduler.agents_per_target_initial",
        "scheduler.agents_per_target_max",
        "legacy.jobs",
        "legacy.batch_size",
        "legacy.lean_pool_size",
    ):
        section, name = path.split(".", 1)
        _positive_integer(getattr(getattr(config, section), name), path)
    cooldown = _positive_number(
        config.ai.increase_cooldown_seconds, "ai.increase_cooldown_seconds"
    )
    throttle = _positive_number(config.ai.throttle_multiplier, "ai.throttle_multiplier")
    _positive_number(
        config.lean.fallback_memory_per_repl_gib,
        "lean.fallback_memory_per_repl_gib",
    )
    safety = _positive_number(
        config.lean.p95_safety_multiplier, "lean.p95_safety_multiplier"
    )
    if throttle >= 1:
        raise ConcurrencyConfigError("ai.throttle_multiplier must be between 0 and 1")
    if cooldown <= 0:
        raise ConcurrencyConfigError("ai.increase_cooldown_seconds must be positive")
    if safety < 1:
        raise ConcurrencyConfigError("lean.p95_safety_multiplier must be at least 1")
    for path in (
        "lean.memory_calibration",
        "scheduler.dependency_priority",
        "scheduler.duplicate_agent_escalation",
    ):
        section, name = path.split(".", 1)
        if not isinstance(getattr(getattr(config, section), name), bool):
            raise ConcurrencyConfigError(f"{path} must be true or false")
    if (
        config.scheduler.agents_per_target_initial
        > config.scheduler.agents_per_target_max
    ):
        raise ConcurrencyConfigError(
            "scheduler.agents_per_target_initial cannot exceed its maximum"
        )
    if isinstance(config.ai.hard_max, int) and config.ai.minimum > config.ai.hard_max:
        raise ConcurrencyConfigError("ai.minimum cannot exceed ai.hard_max")
    if (
        isinstance(config.lean.max_pool, int)
        and config.lean.min_pool > config.lean.max_pool
    ):
        raise ConcurrencyConfigError("lean.min_pool cannot exceed lean.max_pool")
    if config.build.min_concurrent > config.build.hard_max:
        raise ConcurrencyConfigError(
            "build.min_concurrent cannot exceed build.hard_max"
        )
    if config.legacy.jobs > 128:
        raise ConcurrencyConfigError(
            "legacy.jobs must be between 1 and 128 for the compatibility verifier"
        )
    if (
        isinstance(config.ai.initial, int)
        and isinstance(config.ai.hard_max, int)
        and config.ai.initial > config.ai.hard_max
    ):
        raise ConcurrencyConfigError("ai.initial cannot exceed ai.hard_max")
    if (
        isinstance(config.lean.pool_size, int)
        and isinstance(config.lean.max_pool, int)
        and config.lean.pool_size > config.lean.max_pool
    ):
        raise ConcurrencyConfigError("lean.pool_size cannot exceed lean.max_pool")
    if (
        isinstance(config.build.max_concurrent, int)
        and config.build.max_concurrent > config.build.hard_max
    ):
        raise ConcurrencyConfigError(
            "build.max_concurrent cannot exceed build.hard_max"
        )
    if config.mode == ConcurrencyMode.FIXED:
        fixed = {
            "ai.initial": config.ai.initial,
            "lean.pool_size": config.lean.pool_size,
            "build.max_concurrent": config.build.max_concurrent,
        }
        missing = [path for path, value in fixed.items() if value == AutoValue.AUTO]
        if missing:
            raise ConcurrencyConfigError(
                "Fixed concurrency mode requires numeric values for: "
                + ", ".join(missing)
            )


def environment_patch(
    environ: Mapping[str, str] | None = None,
) -> ConcurrencyConfigPatch:
    source = os.environ if environ is None else environ

    def integer(name: str) -> int | None:
        raw = source.get(name)
        if raw is None:
            return None
        try:
            parsed = int(raw)
        except ValueError as exc:
            raise ConcurrencyConfigError(f"{name} must be a positive integer") from exc
        return _positive_integer(parsed, name)

    mode: ConcurrencyMode | None = None
    if raw_mode := source.get("PROOF_ASSISTANT_CONCURRENCY_MODE"):
        try:
            mode = ConcurrencyMode(raw_mode.casefold())
        except ValueError as exc:
            raise ConcurrencyConfigError(
                "PROOF_ASSISTANT_CONCURRENCY_MODE must be adaptive or fixed"
            ) from exc
    ai = integer("PROOF_ASSISTANT_AI_CONCURRENCY")
    lean = integer("PROOF_ASSISTANT_LEAN_POOL_SIZE")
    builds = integer("PROOF_ASSISTANT_MAX_BUILDS")
    agents = integer("PROOF_ASSISTANT_AGENTS_PER_TARGET")
    return ConcurrencyConfigPatch(
        mode=mode,
        ai=AIConcurrencyPatch(initial=ai, hard_max=ai),
        lean=LeanConcurrencyPatch(pool_size=lean, max_pool=lean),
        build=BuildConcurrencyPatch(
            max_concurrent=builds,
            hard_max=builds,
        ),
        scheduler=SchedulerConcurrencyPatch(agents_per_target_max=agents),
    )


def resolve_concurrency_config(
    *,
    machine: MachineConcurrencySettings | ConcurrencyConfigPatch | None = None,
    project: ConcurrencyConfigPatch | None = None,
    environ: Mapping[str, str] | None = None,
    cli: ConcurrencyConfigPatch | None = None,
    defaults: ConcurrencyConfig | None = None,
) -> ResolvedConcurrencyConfig:
    """Resolve deterministic scope precedence, retaining each leaf's source."""

    config = defaults or ConcurrencyConfig()
    sources = {
        path: ConfigScope.DEFAULT for path, _value in _config_leaf_values(config)
    }
    machine_revision = 0
    if isinstance(machine, MachineConcurrencySettings):
        machine_revision = machine.revision
        machine_patch: ConcurrencyConfigPatch | None = machine.patch
    else:
        machine_patch = machine
    layers = (
        (ConfigScope.MACHINE, machine_patch),
        (ConfigScope.PROJECT, project),
        (ConfigScope.ENVIRONMENT, environment_patch(environ)),
        (ConfigScope.CLI, cli),
    )
    for scope, patch in layers:
        if patch is None:
            continue
        for path, value in _iter_patch_values(patch):
            config = _set_leaf(config, path, value)
            sources[path] = scope
    _validate_config(config)
    return ResolvedConcurrencyConfig(
        config=config,
        sources=MappingProxyType(sources),
        machine_revision=machine_revision,
    )
