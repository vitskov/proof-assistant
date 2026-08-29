"""UI-neutral AI driver inspection, setup, discovery, and task policy service."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import unicodedata
from collections.abc import Mapping, MutableMapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from ..protocol import (
    AppServerClient,
    isolated_skill_config_args,
    isolated_tool_config_args,
)
from .catalog import driver_definition
from .config import MachineProviderConfigStore
from .contracts import (
    AuthenticationState,
    CommandSpec,
    CredentialSource,
    Difficulty,
    DiscoverySource,
    DriverId,
    DriverPreference,
    DriverStatus,
    DriverTransport,
    InstallationState,
    InstallConsentError,
    InstallPlan,
    InstallResult,
    MachineProviderSettings,
    ModelCatalog,
    ModelDescriptor,
    ProviderConfig,
    ProviderConfigError,
    ProviderSetupSnapshot,
    SetupActionState,
    TaskKind,
    TaskModelPolicy,
    UnsupportedDifficultyError,
)
from .runtime import (
    CommandResult,
    CommandRunner,
    CompositeCredentialStore,
    CredentialStore,
    ExecutableResolver,
    HttpResponse,
    HttpRunner,
    SecretSubmission,
    SubprocessCommandRunner,
    SystemExecutableResolver,
    UrllibHttpRunner,
)


class PathManager(Protocol):
    def ensure(self, directory: Path) -> None: ...


class ShellPathManager:
    """Persist a known installer bin directory without evaluating shell code."""

    MARKER = "# Added by Proof Assistant"
    INSTALLER_MARKER = "# Added by Proof Assistant installer"

    def __init__(
        self,
        *,
        environment: MutableMapping[str, str] | None = None,
        home: Path | None = None,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self._home = (home or Path.home()).expanduser().resolve(strict=False)

    def _profiles(self) -> tuple[Path, ...]:
        shell = Path(self._environment.get("SHELL", "")).name
        if shell == "zsh":
            root = Path(
                self._environment.get("ZDOTDIR") or str(self._home)
            ).expanduser()
            return (root / ".zprofile", root / ".zshrc")
        if shell == "bash":
            login = self._select_bash_login(
                (
                    self._home / ".bash_profile",
                    self._home / ".bash_login",
                    self._home / ".profile",
                )
            )
            return (login, self._home / ".bashrc")
        if shell == "fish":
            root = Path(
                self._environment.get("XDG_CONFIG_HOME")
                or str(self._home / ".config")
            ).expanduser()
            return (root / "fish" / "config.fish",)
        return (self._home / ".profile",)

    def _select_bash_login(self, candidates: tuple[Path, ...]) -> Path:
        for profile in candidates:
            if profile.is_symlink() and not profile.exists():
                continue
            if not profile.exists():
                continue
            if profile.is_file():
                if os.access(profile, os.R_OK):
                    return profile
                continue
            if os.access(profile, os.R_OK):
                raise OSError(
                    f"refusing readable non-regular Bash startup file: {profile}"
                )
        return self._home / ".profile"

    def _path_line(self, normalized: str) -> str:
        quoted = shlex.quote(normalized)
        if Path(self._environment.get("SHELL", "")).name == "fish":
            return f"fish_add_path --path {quoted}"
        return (
            f'case ":$PATH:" in *:{quoted}:*) ;; *) '
            f'export PATH={quoted}:"$PATH";; esac'
        )

    @staticmethod
    def _legacy_guarded_path_line(normalized: str) -> str:
        quoted = shlex.quote(normalized)
        return (
            f'case ":$PATH:" in *":{quoted}:"*) ;; *) '
            f'export PATH={quoted}:"$PATH";; esac'
        )

    def _upgrade_owned_path_line(
        self, profile: Path, existing: bytes, normalized: str, path_line: str
    ) -> bytes:
        quoted = shlex.quote(normalized)
        legacy_block = (
            f'{self.MARKER}\nexport PATH={quoted}:"$PATH"\n'.encode()
        )
        if legacy_block not in existing:
            return existing
        updated = existing.replace(
            legacy_block,
            f"{self.MARKER}\n{path_line}\n".encode(),
        )
        with profile.open("r+b") as stream:
            stream.write(updated)
            stream.truncate()
        return updated

    @staticmethod
    def _managed_guard(line: bytes) -> bytes | None:
        export_prefix = b"export PATH="
        export_suffix = b':"$PATH"'
        if line.startswith(export_prefix) and line.endswith(export_suffix):
            token = line[len(export_prefix) : -len(export_suffix)]
        else:
            case_prefix = b'case ":$PATH:" in '
            case_middle = b") ;; *) export PATH="
            case_suffix = b':"$PATH";; esac'
            if not line.startswith(case_prefix) or not line.endswith(case_suffix):
                return None
            before_export, separator, after_export = line.partition(case_middle)
            if not separator:
                return None
            token = after_export[: -len(case_suffix)]
            old_pattern = case_prefix + b'*":' + token + b':"*'
            new_pattern = case_prefix + b"*:" + token + b":*"
            if before_export not in {old_pattern, new_pattern}:
                return None
        if not token:
            return None
        return (
            b'case ":$PATH:" in *:'
            + token
            + b':*) ;; *) export PATH='
            + token
            + export_suffix
            + b";; esac"
        )

    def _managed_profile_guards(self, content: bytes) -> tuple[bytes, ...] | None:
        nonblank = [line for line in content.splitlines() if line.strip()]
        if not nonblank or len(nonblank) % 2:
            return None
        markers = {self.MARKER.encode(), self.INSTALLER_MARKER.encode()}
        guards: list[bytes] = []
        for index in range(0, len(nonblank), 2):
            if nonblank[index] not in markers:
                return None
            guard = self._managed_guard(nonblank[index + 1])
            if guard is None:
                return None
            if guard not in guards:
                guards.append(guard)
        return tuple(guards)

    def _append_managed_guards(self, profile: Path, guards: tuple[bytes, ...]) -> None:
        self._validate_profile(profile)
        profile.parent.mkdir(parents=True, exist_ok=True)
        existing = profile.read_bytes() if profile.exists() else b""
        lines = set(existing.splitlines())
        addition = bytearray()
        for guard in guards:
            if guard in lines:
                continue
            if existing or addition:
                addition.extend(b"\n")
            addition.extend(self.INSTALLER_MARKER.encode() + b"\n" + guard + b"\n")
            lines.add(guard)
        if addition:
            with profile.open("ab") as stream:
                stream.write(addition)

    def _migrate_legacy_bash_profile(self) -> None:
        if Path(self._environment.get("SHELL", "")).name != "bash":
            return
        profile = self._home / ".bash_profile"
        if (
            profile.is_symlink()
            or not profile.is_file()
            or not os.access(profile, os.R_OK)
        ):
            return
        content = profile.read_bytes()
        guards = self._managed_profile_guards(content)
        if guards is None:
            return

        target = self._select_bash_login(
            (self._home / ".bash_login", self._home / ".profile")
        )
        self._append_managed_guards(target, guards)

        backup = profile.with_name(".bash_profile.proof-assistant-backup")
        suffix = 0
        while backup.exists() or backup.is_symlink():
            suffix += 1
            backup = profile.with_name(
                f".bash_profile.proof-assistant-backup-{suffix}"
            )
        profile.replace(backup)

    @staticmethod
    def _validate_profile(profile: Path) -> None:
        if profile.is_symlink() and not profile.exists():
            raise OSError(f"refusing to update broken startup-file symlink: {profile}")
        if profile.exists() and not profile.is_file():
            raise OSError(f"refusing to update non-regular startup file: {profile}")

    def ensure(self, directory: Path) -> None:
        normalized = str(directory.expanduser().resolve(strict=False))
        current = self._environment.get("PATH", "")
        parts = [item for item in current.split(os.pathsep) if item]
        if normalized not in parts:
            self._environment["PATH"] = os.pathsep.join([normalized, *parts])

        self._migrate_legacy_bash_profile()
        path_line = self._path_line(normalized)
        encoded_line = path_line.encode("utf-8")
        marker = self.MARKER.encode("utf-8")
        for profile in self._profiles():
            self._validate_profile(profile)
            profile.parent.mkdir(parents=True, exist_ok=True)
            existing = profile.read_bytes() if profile.exists() else b""
            existing = self._upgrade_owned_path_line(
                profile, existing, normalized, path_line
            )
            equivalent_lines = {
                encoded_line,
                self._legacy_guarded_path_line(normalized).encode(),
            }
            if equivalent_lines.intersection(existing.splitlines()):
                continue
            prefix = b"" if not existing or existing.endswith(b"\n") else b"\n"
            with profile.open("ab") as stream:
                stream.write(prefix + marker + b"\n" + encoded_line + b"\n")


def _clean_version(result: CommandResult) -> str | None:
    text = (result.stdout or result.stderr).strip()
    if not text:
        return None
    line = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])", "", text.splitlines()[0])
    line = "".join(
        character
        for character in line
        if not unicodedata.category(character).startswith("C")
    ).strip()
    return line[:200] or None


def _replace_executable(argv: tuple[str, ...], executable: str) -> tuple[str, ...]:
    return (executable, *argv[1:])


def _catalog_fallback(driver: DriverId, detail: str) -> ModelCatalog:
    definition = driver_definition(driver)
    source = (
        DiscoverySource.CURATED_FALLBACK
        if definition.curated_models
        else DiscoverySource.UNAVAILABLE
    )
    return ModelCatalog(
        driver=driver,
        models=definition.curated_models,
        source=source,
        detail=detail,
        contract_approved=bool(definition.curated_models),
    )


def _claude_catalog_for_version(
    catalog: ModelCatalog, version: str | None
) -> ModelCatalog:
    """Hide Fable-era aliases from Claude Code releases that cannot use them."""

    if catalog.driver is not DriverId.CLAUDE_CLI:
        return catalog
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?", version or "")
    parsed = (
        tuple(int(part or 0) for part in match.groups()) if match is not None else None
    )
    if parsed is not None and parsed >= (2, 1, 170):
        return catalog
    models = tuple(
        model for model in catalog.models if model.model_id not in {"best", "fable"}
    )
    detail = (
        f"{catalog.detail} Claude Code 2.1.170 or newer is required for the "
        "best and fable aliases."
    )
    return ModelCatalog(
        driver=catalog.driver,
        models=models,
        source=catalog.source,
        detail=detail,
        contract_approved=catalog.contract_approved and bool(models),
    )


def _difficulty(value: object) -> Difficulty | None:
    raw = str(getattr(value, "value", value)).casefold().replace("-", "")
    aliases = {"extra_high": "xhigh", "extra-high": "xhigh"}
    raw = aliases.get(raw, raw)
    try:
        return Difficulty(raw)
    except ValueError:
        return None


def _copilot_probe_succeeded(output: str) -> bool:
    """Accept only a structured Copilot response whose answer is exactly OK."""

    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        candidates: list[object] = [event.get("content"), event.get("text")]
        message = event.get("message")
        if isinstance(message, Mapping):
            candidates.extend((message.get("content"), message.get("text")))
        if any(
            isinstance(candidate, str) and candidate.strip() == "OK"
            for candidate in candidates
        ):
            return True
    return False


class ProviderService:
    """One source of truth for provider setup, without owning provider secrets."""

    def __init__(
        self,
        *,
        config_store: MachineProviderConfigStore | None = None,
        commands: CommandRunner | None = None,
        http: HttpRunner | None = None,
        credentials: CredentialStore | None = None,
        executables: ExecutableResolver | None = None,
        path_manager: PathManager | None = None,
        environment: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> None:
        self.config_store = config_store or MachineProviderConfigStore()
        self.commands = commands or SubprocessCommandRunner()
        self.http = http or UrllibHttpRunner()
        self.credentials = credentials or CompositeCredentialStore()
        self.executables = executables or SystemExecutableResolver()
        self.environment = os.environ if environment is None else environment
        self.home = (home or Path.home()).expanduser().resolve(strict=False)
        self.installer_bin = self.home / ".local" / "bin"
        mutable_environment = (
            self.environment if isinstance(self.environment, MutableMapping) else None
        )
        self.path_manager = path_manager or ShellPathManager(
            environment=mutable_environment, home=self.home
        )

    def _effective_path(self) -> str:
        current = self.environment.get("PATH", "")
        return (
            os.pathsep.join([str(self.installer_bin), current])
            if current
            else str(self.installer_bin)
        )

    def _child_environment(self, driver: DriverId | None = None) -> dict[str, str]:
        """Return the minimal environment allowed across a provider child boundary.

        CLI subscription sessions remain owned by their native clients. In
        particular, API keys for this or another provider must not silently
        change billing identity during setup, discovery, installation, or an
        entitlement probe.
        """

        names = {
            "HOME",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        if driver is DriverId.COPILOT_CLI:
            names.update(
                {
                    "COPILOT_GITHUB_TOKEN",
                    "GH_TOKEN",
                    "GITHUB_TOKEN",
                    "GH_HOST",
                    "COPILOT_GH_HOST",
                }
            )
        child = {
            name: self.environment[name] for name in names if name in self.environment
        }
        child["PATH"] = self._effective_path()
        return child

    def _resolve_executable(self, driver: DriverId) -> str | None:
        executable = driver_definition(driver).executable
        if executable is None:
            return None
        return self.executables.which(executable, path=self._effective_path())

    def _preference(
        self, driver: DriverId, preference: DriverPreference | None
    ) -> DriverPreference:
        if preference is not None:
            if preference.driver is not driver:
                raise ValueError("driver preference does not match requested driver")
            return preference
        return self.config_store.load().config.preference_for(driver)

    def inspect_driver(
        self,
        driver: DriverId,
        *,
        preference: DriverPreference | None = None,
        discover_models: bool = True,
    ) -> DriverStatus:
        definition = driver_definition(driver)
        selected = self._preference(driver, preference)
        if definition.transport is DriverTransport.API:
            catalog = (
                self.discover_models(driver, preference=selected)
                if discover_models
                else None
            )
            if selected.credential_source is CredentialSource.NONE:
                auth = AuthenticationState.REQUIRED
                detail = definition.login_instruction
            else:
                try:
                    present = self.credentials.get(driver, selected.credential_source)
                except Exception:
                    present = None
                    auth = AuthenticationState.ERROR
                    detail = "The configured credential store is unavailable."
                else:
                    if not present:
                        auth = AuthenticationState.REQUIRED
                        detail = definition.login_instruction
                    elif catalog is None:
                        auth = AuthenticationState.UNKNOWN
                        detail = "Credential is configured but has not been validated."
                    elif catalog.source is DiscoverySource.LIVE_ACCOUNT:
                        auth = AuthenticationState.AUTHENTICATED
                        detail = "Credential and account model access were validated."
                    elif catalog.detail.startswith("Authentication failed"):
                        auth = AuthenticationState.REQUIRED
                        detail = definition.login_instruction
                    else:
                        auth = AuthenticationState.ERROR
                        detail = "Credential validation could not reach the provider catalog."
            return DriverStatus(
                driver=driver,
                transport=definition.transport,
                installation=InstallationState.NOT_APPLICABLE,
                authentication=auth,
                detail=detail,
                catalog=catalog,
            )

        executable = self._resolve_executable(driver)
        if executable is None:
            return DriverStatus(
                driver=driver,
                transport=definition.transport,
                installation=InstallationState.MISSING,
                authentication=AuthenticationState.REQUIRED,
                detail=f"{definition.display_name} is not installed.",
                catalog=_catalog_fallback(
                    driver, "Curated fallback; the CLI is not installed."
                )
                if discover_models
                else None,
            )

        try:
            version_result = self.commands.run(
                _replace_executable(definition.version_argv, executable),
                timeout_seconds=30.0,
                env=self._child_environment(driver),
            )
        except Exception:
            return DriverStatus(
                driver=driver,
                transport=definition.transport,
                installation=InstallationState.BROKEN,
                authentication=AuthenticationState.ERROR,
                executable=executable,
                detail="The executable could not complete its version check.",
                catalog=_catalog_fallback(driver, "CLI model discovery unavailable.")
                if discover_models
                else None,
            )

        version = _clean_version(version_result)
        identity_ok = version_result.returncode == 0 and bool(version)
        if driver is DriverId.CODEX_CLI:
            identity_ok = identity_ok and "codex" in (version or "").casefold()
        elif driver is DriverId.CLAUDE_CLI:
            identity_ok = identity_ok and "claude" in (version or "").casefold()
        elif driver is DriverId.COPILOT_CLI:
            try:
                help_result = self.commands.run(
                    (executable, "help"),
                    timeout_seconds=30.0,
                    env=self._child_environment(driver),
                )
                help_text = f"{help_result.stdout}\n{help_result.stderr}".casefold()
                identity_ok = (
                    identity_ok
                    and help_result.returncode == 0
                    and (
                        "github copilot" in help_text
                        or ("copilot" in help_text and "prompt" in help_text)
                    )
                )
            except Exception:
                identity_ok = False

        if not identity_ok:
            return DriverStatus(
                driver=driver,
                transport=definition.transport,
                installation=InstallationState.BROKEN,
                authentication=AuthenticationState.ERROR,
                executable=executable,
                version=version,
                detail="The executable failed its provider identity/version check.",
                catalog=_catalog_fallback(driver, "CLI model discovery unavailable.")
                if discover_models
                else None,
            )

        if definition.auth_argv:
            try:
                auth_result = self.commands.run(
                    _replace_executable(definition.auth_argv, executable),
                    timeout_seconds=30.0,
                    env=self._child_environment(driver),
                )
            except Exception:
                auth = AuthenticationState.ERROR
                detail = "The non-billable authentication status check failed."
            else:
                auth = (
                    AuthenticationState.AUTHENTICATED
                    if auth_result.returncode == 0
                    else AuthenticationState.REQUIRED
                )
                detail = (
                    "Account connection verified."
                    if auth is AuthenticationState.AUTHENTICATED
                    else definition.login_instruction
                )
        else:
            if (
                driver is DriverId.COPILOT_CLI
                and selected.runtime_verified_version == version
            ):
                auth = AuthenticationState.AUTHENTICATED
                detail = (
                    "Copilot account access was verified by an explicitly authorized "
                    "tiny runtime probe for this CLI version."
                )
            else:
                auth = AuthenticationState.UNKNOWN
                detail = (
                    definition.login_instruction
                    + " Then use Verify account; that explicit check sends one tiny "
                    "Copilot request because the CLI has no documented non-billable "
                    "authentication-status command."
                )

        catalog = (
            self.discover_models(driver, preference=selected)
            if discover_models
            else None
        )
        if catalog is not None and driver is DriverId.CLAUDE_CLI:
            catalog = _claude_catalog_for_version(catalog, version)
        return DriverStatus(
            driver=driver,
            transport=definition.transport,
            installation=InstallationState.INSTALLED,
            authentication=auth,
            executable=executable,
            version=version,
            detail=detail,
            catalog=catalog,
        )

    def _api_response(
        self, driver: DriverId, preference: DriverPreference
    ) -> HttpResponse | None:
        definition = driver_definition(driver)
        if definition.api_models_url is None:
            return None
        try:
            credential = self.credentials.get(driver, preference.credential_source)
        except Exception:
            return None
        if not credential:
            return None
        headers = {"Accept": "application/json"}
        if driver is DriverId.OPENAI_API:
            headers["Authorization"] = f"Bearer {credential}"
        elif driver is DriverId.ANTHROPIC_API:
            headers["x-api-key"] = credential
            headers["anthropic-version"] = "2023-06-01"
        elif driver is DriverId.GEMINI_API:
            headers["x-goog-api-key"] = credential
        try:
            return self.http.request(
                "GET",
                definition.api_models_url,
                headers=headers,
                timeout_seconds=30.0,
            )
        except Exception:
            return HttpResponse(status=0, body=b"")

    def discover_models(
        self,
        driver: DriverId,
        *,
        preference: DriverPreference | None = None,
    ) -> ModelCatalog:
        definition = driver_definition(driver)
        selected = self._preference(driver, preference)
        if definition.transport is DriverTransport.API:
            response = self._api_response(driver, selected)
            if response is None:
                return _catalog_fallback(
                    driver, "Credential missing; showing a curated, non-live fallback."
                )
            if response.status in {401, 403}:
                return _catalog_fallback(
                    driver,
                    "Authentication failed; showing a curated, non-live fallback.",
                )
            if not 200 <= response.status < 300:
                return _catalog_fallback(
                    driver,
                    "Provider catalog unavailable; showing a curated, non-live fallback.",
                )
            try:
                payload = response.json()
                models = self._parse_api_models(driver, payload)
            except (ValueError, TypeError, UnicodeDecodeError):
                return _catalog_fallback(
                    driver,
                    "Provider returned an invalid catalog; showing a curated fallback.",
                )
            if not models:
                return _catalog_fallback(
                    driver,
                    "Provider returned no usable text models; showing a curated fallback.",
                )
            return ModelCatalog(
                driver=driver,
                models=models,
                source=DiscoverySource.LIVE_ACCOUNT,
                detail="Live models available to the configured account.",
                contract_approved=True,
            )

        if driver is not DriverId.CODEX_CLI:
            return _catalog_fallback(
                driver,
                "The CLI has no documented noninteractive account model-list command; "
                "showing contract-approved aliases, not a live catalog.",
            )
        executable = self._resolve_executable(driver)
        if executable is None:
            return _catalog_fallback(
                driver, "Codex is not installed; showing a curated, non-live fallback."
            )
        if isinstance(self.commands, SubprocessCommandRunner):
            return self._discover_codex_models_native(executable)
        initialize = {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "proof-assistant", "version": "0.1.0"},
                "capabilities": {},
            },
        }
        model_list = {"id": 2, "method": "model/list", "params": {"limit": 100}}
        input_text = json.dumps(initialize) + "\n" + json.dumps(model_list) + "\n"
        try:
            result = self.commands.run(
                (executable, "app-server"),
                input_text=input_text,
                timeout_seconds=30.0,
                env=self._child_environment(driver),
            )
            models = self._parse_codex_models(result.stdout)
        except Exception:
            models = ()
        if not models:
            return _catalog_fallback(
                driver, "Live Codex model discovery failed; showing a curated fallback."
            )
        return ModelCatalog(
            driver=driver,
            models=models,
            source=DiscoverySource.LIVE_ACCOUNT,
            detail="Live models reported by Codex app-server.",
            contract_approved=True,
        )

    def discover_usable_models(
        self,
        driver: DriverId,
        *,
        preference: DriverPreference | None = None,
    ) -> ModelCatalog:
        """Apply installed-CLI capability gates to the provider model catalog."""

        selected = self._preference(driver, preference)
        if driver is DriverId.CLAUDE_CLI and self._resolve_executable(driver):
            status = self.inspect_driver(driver, preference=selected)
            if status.catalog is not None:
                return status.catalog
        return self.discover_models(driver, preference=selected)

    def _discover_codex_models_native(self, executable: str) -> ModelCatalog:
        """Use the long-lived app-server protocol and always close the child."""

        child_environment = self._child_environment(DriverId.CODEX_CLI)
        external_args = isolated_tool_config_args(
            executable,
            env=child_environment,
            inherit_environment=False,
        )
        extra_args = [
            *external_args,
            *isolated_skill_config_args(
                executable,
                cwd=None,
                external_tool_args=external_args,
                env=child_environment,
                inherit_environment=False,
            ),
        ]
        client = AppServerClient(
            executable,
            env=child_environment,
            inherit_environment=False,
            extra_args=extra_args,
        )
        try:
            client.start()
            client.request(
                "initialize",
                {
                    "clientInfo": {"name": "proof-assistant", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
                timeout=30.0,
            )
            client.notify("initialized", {})
            response = client.request("model/list", {"limit": 100}, timeout=30.0)
            output = json.dumps({"id": 2, "result": response})
            models = self._parse_codex_models(output)
        except Exception:
            models = ()
        finally:
            client.close()
        if not models:
            return _catalog_fallback(
                DriverId.CODEX_CLI,
                "Live Codex model discovery failed; showing a curated fallback.",
            )
        return ModelCatalog(
            driver=DriverId.CODEX_CLI,
            models=models,
            source=DiscoverySource.LIVE_ACCOUNT,
            detail="Live models reported by Codex app-server.",
            contract_approved=True,
        )

    def _parse_codex_models(self, output: str) -> tuple[ModelDescriptor, ...]:
        models: list[ModelDescriptor] = []
        for line in output.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != 2:
                continue
            result = message.get("result")
            if isinstance(result, dict):
                raw_models = result.get("data") or result.get("models") or []
            else:
                raw_models = result if isinstance(result, list) else []
            if not isinstance(raw_models, list):
                continue
            for raw in raw_models:
                if not isinstance(raw, dict):
                    continue
                model_id = (
                    raw.get("model")
                    or raw.get("id")
                    or raw.get("slug")
                    or raw.get("name")
                )
                if not isinstance(model_id, str) or not model_id.strip():
                    continue
                raw_efforts = (
                    raw.get("supportedReasoningEfforts")
                    or raw.get("supported_reasoning_efforts")
                    or []
                )
                efforts: list[Difficulty] = [Difficulty.AUTO]
                if isinstance(raw_efforts, list):
                    for raw_effort in raw_efforts:
                        value = raw_effort
                        if isinstance(raw_effort, dict):
                            value = (
                                raw_effort.get("reasoningEffort")
                                or raw_effort.get("effort")
                                or raw_effort.get("value")
                            )
                        parsed = _difficulty(value)
                        if parsed is not None and parsed not in efforts:
                            efforts.append(parsed)
                models.append(
                    ModelDescriptor(
                        model_id=model_id,
                        display_name=str(
                            raw.get("displayName") or raw.get("name") or model_id
                        ),
                        difficulties=tuple(efforts),
                    )
                )
        return tuple(models)

    def _parse_api_models(
        self, driver: DriverId, payload: object
    ) -> tuple[ModelDescriptor, ...]:
        if not isinstance(payload, dict):
            raise TypeError("catalog must be an object")
        raw_models = (
            payload.get("models")
            if driver is DriverId.GEMINI_API
            else payload.get("data")
        )
        if not isinstance(raw_models, list):
            raise TypeError("catalog models must be a list")
        models: list[ModelDescriptor] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            if driver is DriverId.GEMINI_API:
                methods = (
                    raw.get("supportedGenerationMethods")
                    or raw.get("supported_actions")
                    or []
                )
                if (
                    isinstance(methods, list)
                    and "generateContent" not in methods
                    and "generate_content" not in methods
                ):
                    continue
                raw_id = raw.get("name") or raw.get("id")
                model_id = str(raw_id or "").removeprefix("models/")
            else:
                model_id = str(raw.get("id") or raw.get("name") or "")
            if not model_id or not self._usable_model_id(driver, model_id):
                continue
            difficulties = self._api_model_difficulties(driver, raw)
            models.append(
                ModelDescriptor(
                    model_id=model_id,
                    display_name=str(
                        raw.get("display_name") or raw.get("displayName") or model_id
                    ),
                    difficulties=difficulties,
                )
            )
        return tuple(sorted(models, key=lambda item: item.model_id))

    @staticmethod
    def _api_model_difficulties(
        driver: DriverId, raw: Mapping[str, object]
    ) -> tuple[Difficulty, ...]:
        definition = driver_definition(driver)
        model_id = str(raw.get("id") or raw.get("name") or "").removeprefix("models/")
        if driver is DriverId.OPENAI_API:
            # OpenAI's account model-list endpoint reports availability, not the
            # per-model reasoning-effort enum.  Only attach the richer contract
            # to IDs whose current contract is curated explicitly; unknown
            # account models remain usable in provider-default (AUTO) mode.
            curated = next(
                (
                    item
                    for item in definition.curated_models
                    if item.model_id == model_id
                ),
                None,
            )
            return curated.difficulties if curated is not None else (Difficulty.AUTO,)
        if driver is DriverId.GEMINI_API:
            if raw.get("thinking") is False:
                return (Difficulty.AUTO,)
            # models.list reports whether thinking exists, but not the exact
            # supported level set.  GenerateContent uses named levels for the
            # Gemini 3 family and numeric budgets for Gemini 2.5.  Expose only
            # mappings that the execution adapter can implement without
            # pretending the metadata returned a finer-grained contract.
            if model_id.startswith("gemini-2.5-"):
                return (
                    Difficulty.AUTO,
                    Difficulty.LOW,
                    Difficulty.MEDIUM,
                    Difficulty.HIGH,
                )
            if model_id.startswith("gemini-3"):
                return ProviderService._gemini_3_difficulties(model_id)
            return (Difficulty.AUTO,)
        candidates: list[object] = [
            raw.get("supported_effort_levels"),
            raw.get("supportedEffortLevels"),
            raw.get("effort_levels"),
        ]
        capabilities = raw.get("capabilities")
        if isinstance(capabilities, Mapping):
            candidates.extend(
                (
                    capabilities.get("supported_effort_levels"),
                    capabilities.get("effort_levels"),
                )
            )
            effort = capabilities.get("effort")
            if isinstance(effort, Mapping):
                values = [Difficulty.AUTO]
                for level in (
                    Difficulty.LOW,
                    Difficulty.MEDIUM,
                    Difficulty.HIGH,
                    Difficulty.XHIGH,
                    Difficulty.MAX,
                ):
                    support = effort.get(level.value)
                    if (
                        isinstance(support, Mapping)
                        and support.get("supported") is True
                    ):
                        values.append(level)
                if len(values) > 1:
                    return tuple(values)
                candidates.extend(
                    (effort.get("levels"), effort.get("supported_levels"))
                )
        for candidate in candidates:
            if not isinstance(candidate, list):
                continue
            values = [Difficulty.AUTO]
            for item in candidate:
                parsed = _difficulty(item)
                if (
                    parsed is not None
                    and parsed in definition.difficulties
                    and parsed not in values
                ):
                    values.append(parsed)
            if len(values) > 1:
                return tuple(values)
        # A live list entry without machine-readable capability metadata does
        # not justify claiming that every provider-wide level is supported.
        return (Difficulty.AUTO,)

    @staticmethod
    def _gemini_3_difficulties(model_id: str) -> tuple[Difficulty, ...]:
        """Conservative named-level contracts for current Gemini 3 families."""

        lowered = model_id.casefold()
        if "flash-lite-image" in lowered:
            return (Difficulty.AUTO, Difficulty.HIGH)
        if lowered.startswith("gemini-3-pro-preview"):
            return (Difficulty.AUTO, Difficulty.LOW, Difficulty.HIGH)
        if lowered.startswith(
            (
                "gemini-3.1-pro",
                "gemini-3.5-flash",
                "gemini-3.6-flash",
                "gemini-3.7-flash",
            )
        ):
            return (
                Difficulty.AUTO,
                Difficulty.LOW,
                Difficulty.MEDIUM,
                Difficulty.HIGH,
            )
        # High is the only named setting common to the documented Gemini 3
        # families.  AUTO leaves provider-specific defaults intact.
        return (Difficulty.AUTO, Difficulty.HIGH)

    @staticmethod
    def _usable_model_id(driver: DriverId, model_id: str) -> bool:
        lowered = model_id.casefold()
        if driver is DriverId.OPENAI_API:
            excluded = (
                "embedding",
                "moderation",
                "whisper",
                "tts",
                "transcribe",
                "image",
                "dall-e",
                "realtime",
                "audio",
                "search-preview",
            )
            return not any(token in lowered for token in excluded)
        return True

    def preview_install(self, driver: DriverId) -> InstallPlan:
        definition = driver_definition(driver)
        if definition.transport is DriverTransport.API:
            return self._install_plan(
                driver,
                SetupActionState.NOT_NEEDED,
                (),
                None,
                None,
                "API drivers require a credential, not a CLI installation.",
            )
        if (
            self._resolve_executable(driver) is not None
            and self.inspect_driver(driver, discover_models=False).installation
            is InstallationState.INSTALLED
        ):
            return self._install_plan(
                driver,
                SetupActionState.NOT_NEEDED,
                (),
                definition.executable,
                str(self.installer_bin),
                f"{definition.display_name} is already installed.",
            )
        npm = self.executables.which("npm", path=self._effective_path())
        node = self.executables.which("node", path=self._effective_path())
        if npm is None or node is None or definition.install_npm_package is None:
            return self._install_plan(
                driver,
                SetupActionState.UNSUPPORTED,
                (),
                definition.executable,
                str(self.installer_bin),
                "A working Node.js/npm installation is required for this user-local installer.",
            )
        if driver in {DriverId.CLAUDE_CLI, DriverId.COPILOT_CLI}:
            try:
                node_version = self.commands.run(
                    (node, "--version"),
                    timeout_seconds=30.0,
                    env=self._child_environment(),
                )
                match = re.search(
                    r"v?(\d+)", node_version.stdout or node_version.stderr
                )
                modern_node = (
                    node_version.returncode == 0
                    and match is not None
                    and int(match.group(1)) >= 22
                )
            except Exception:
                modern_node = False
            if not modern_node:
                return self._install_plan(
                    driver,
                    SetupActionState.UNSUPPORTED,
                    (),
                    definition.executable,
                    str(self.installer_bin),
                    "Claude Code and GitHub Copilot CLI require Node.js 22 or newer.",
                )
        command = CommandSpec(
            (
                npm,
                "install",
                "--global",
                "--prefix",
                str(self.home / ".local"),
                definition.install_npm_package,
            ),
            timeout_seconds=600.0,
        )
        return self._install_plan(
            driver,
            SetupActionState.AVAILABLE,
            (command,),
            definition.executable,
            str(self.installer_bin),
            f"Install {definition.display_name} into {self.home / '.local'} without sudo.",
        )

    @staticmethod
    def _install_plan(
        driver: DriverId,
        state: SetupActionState,
        commands: tuple[CommandSpec, ...],
        expected_executable: str | None,
        installer_bin: str | None,
        detail: str,
    ) -> InstallPlan:
        payload = json.dumps(
            {
                "driver": driver.value,
                "state": state.value,
                "commands": [list(command.argv) for command in commands],
                "expected_executable": expected_executable,
                "installer_bin": installer_bin,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        token = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return InstallPlan(
            driver=driver,
            state=state,
            commands=commands,
            expected_executable=expected_executable,
            installer_bin=installer_bin,
            consent_token=token,
            detail=detail,
        )

    def execute_install(
        self, plan: InstallPlan, *, consent_token: str
    ) -> InstallResult:
        expected = self.preview_install(plan.driver)
        if plan != expected:
            raise InstallConsentError(
                "Install plan is stale or was not created by this service"
            )
        if consent_token != plan.consent_token:
            raise InstallConsentError(
                "Explicit consent for this exact install plan is required"
            )
        if plan.state is not SetupActionState.AVAILABLE:
            status = self.inspect_driver(plan.driver)
            return InstallResult(
                driver=plan.driver,
                attempted=False,
                succeeded=plan.state is SetupActionState.NOT_NEEDED,
                status=status,
                detail=plan.detail,
            )
        for command in plan.commands:
            try:
                result = self.commands.run(
                    command.argv,
                    timeout_seconds=command.timeout_seconds,
                    env=self._child_environment(),
                )
            except Exception:
                status = self.inspect_driver(plan.driver)
                return InstallResult(
                    driver=plan.driver,
                    attempted=True,
                    succeeded=False,
                    status=status,
                    detail="The allowlisted installer command could not be executed.",
                )
            if result.returncode != 0:
                status = self.inspect_driver(plan.driver)
                return InstallResult(
                    driver=plan.driver,
                    attempted=True,
                    succeeded=False,
                    status=status,
                    detail="The allowlisted installer command failed.",
                )
        if plan.installer_bin is not None:
            self.path_manager.ensure(Path(plan.installer_bin))
        status = self.inspect_driver(plan.driver)
        return InstallResult(
            driver=plan.driver,
            attempted=True,
            succeeded=status.installation is InstallationState.INSTALLED,
            status=status,
            detail=(
                "Installation and executable identity checks succeeded."
                if status.installation is InstallationState.INSTALLED
                else "Installation finished, but executable verification failed."
            ),
        )

    def store_credential(
        self,
        driver: DriverId,
        source: CredentialSource,
        submission: SecretSubmission,
    ) -> None:
        if driver_definition(driver).transport is not DriverTransport.API:
            raise ValueError("CLI authentication is owned by the provider CLI")
        self.credentials.set(driver, source, submission)

    def delete_credential(self, driver: DriverId, source: CredentialSource) -> bool:
        if driver_definition(driver).transport is not DriverTransport.API:
            return False
        return self.credentials.delete(driver, source)

    def verify_cli_account(self, driver: DriverId, *, consent: bool) -> DriverStatus:
        """Run an explicitly requested minimal entitlement probe where unavoidable."""

        if driver is not DriverId.COPILOT_CLI:
            return self.inspect_driver(driver)
        if not consent:
            raise InstallConsentError(
                "Explicit consent is required before the Copilot quota probe"
            )
        status = self.inspect_driver(driver, discover_models=False)
        if (
            status.installation is not InstallationState.INSTALLED
            or not status.executable
        ):
            return status
        result = self.commands.run(
            (
                status.executable,
                "-p",
                "Reply with exactly OK. Do not call tools.",
                "-s",
                "--output-format=json",
                "--no-ask-user",
                "--no-auto-update",
                "--no-custom-instructions",
                "--no-remote",
                "--no-remote-export",
                "--disable-builtin-mcps",
                "--available-tools=proof_assistant",
                "--deny-tool=shell,write,read,url,memory,skill,task",
                "--model=auto",
            ),
            timeout_seconds=120.0,
            env=self._child_environment(driver),
        )
        if result.returncode != 0 or not _copilot_probe_succeeded(result.stdout):
            return replace(
                status,
                authentication=AuthenticationState.REQUIRED,
                detail=(
                    "Copilot account verification failed. Run `copilot login`, check "
                    "plan/organization policy, then retry the explicit account check."
                ),
            )
        assert status.version is not None
        settings = self.config_store.load()
        preferences = tuple(
            replace(item, runtime_verified_version=status.version)
            if item.driver is driver
            else item
            for item in settings.config.drivers
        )
        self.config_store.save(
            replace(settings.config, drivers=preferences),
            expected_revision=settings.revision,
        )
        return self.inspect_driver(driver)

    def validate_difficulty(
        self,
        driver: DriverId,
        model: str | None,
        difficulty: Difficulty,
        *,
        catalog: ModelCatalog | None = None,
    ) -> None:
        allowed = driver_definition(driver).difficulties
        if catalog is not None and model is not None:
            descriptor = next(
                (item for item in catalog.models if item.model_id == model), None
            )
            if descriptor is None and catalog.contract_approved:
                raise ProviderConfigError(
                    f"Model {model!r} is not present in the "
                    f"{catalog.source.value} catalog for {driver.value}"
                )
            if descriptor is not None:
                allowed = descriptor.difficulties
        if difficulty not in allowed:
            raise UnsupportedDifficultyError(driver, model, difficulty, allowed)

    def validate_config(self, config: ProviderConfig) -> None:
        """Validate all explicit choices against one catalog per used driver."""

        settings = MachineProviderSettings(config=config)
        catalogs: dict[DriverId, ModelCatalog] = {}

        def catalog_for(driver: DriverId) -> ModelCatalog:
            catalog = catalogs.get(driver)
            if catalog is None:
                catalog = self.discover_usable_models(
                    driver, preference=config.preference_for(driver)
                )
                catalogs[driver] = catalog
            return catalog

        for preference in config.drivers:
            if not preference.enabled or preference.model is None:
                continue
            self.validate_difficulty(
                preference.driver,
                preference.model,
                preference.difficulty,
                catalog=catalog_for(preference.driver),
            )
        for task in TaskKind:
            task_preference = config.task_preference_for(task)
            driver = (
                task_preference.driver
                if task_preference is not None and task_preference.driver is not None
                else config.primary_driver
            )
            self.recommend_task_policy(
                task,
                settings=settings,
                catalog=catalog_for(driver),
            )

    def recommend_task_policy(
        self,
        task: TaskKind,
        *,
        settings: MachineProviderSettings | None = None,
        catalog: ModelCatalog | None = None,
    ) -> TaskModelPolicy:
        loaded = settings or self.config_store.load()
        task_preference = loaded.config.task_preference_for(task)
        driver = (
            task_preference.driver
            if task_preference is not None and task_preference.driver is not None
            else loaded.config.primary_driver
        )
        driver_preference = loaded.config.preference_for(driver)
        available = catalog or self.discover_usable_models(
            driver, preference=driver_preference
        )
        explicit_model = (
            task_preference.model if task_preference is not None else None
        ) or driver_preference.model
        model = explicit_model or self._recommended_model(
            task, driver, available.models
        )
        explicit_difficulty = (
            task_preference.difficulty
            if task_preference is not None
            and task_preference.difficulty is not Difficulty.AUTO
            else driver_preference.difficulty
        )
        difficulty = explicit_difficulty
        if difficulty is Difficulty.AUTO:
            recommended = self._recommended_difficulty(task, driver)
            descriptor = next(
                (item for item in available.models if item.model_id == model), None
            )
            difficulty = (
                recommended
                if descriptor is None or recommended in descriptor.difficulties
                else Difficulty.AUTO
            )
        self.validate_difficulty(driver, model, difficulty, catalog=available)
        return TaskModelPolicy(
            task=task,
            driver=driver,
            model=model,
            difficulty=difficulty,
            model_source=available.source,
            explanation=(
                "Uses an explicit machine/task override."
                if explicit_model is not None
                or explicit_difficulty is not Difficulty.AUTO
                else "Uses the task-class recommendation from the current model catalog."
            ),
        )

    @staticmethod
    def _recommended_model(
        task: TaskKind,
        driver: DriverId,
        models: tuple[ModelDescriptor, ...],
    ) -> str | None:
        if not models:
            return None
        if driver is DriverId.CLAUDE_CLI:
            available = {item.model_id for item in models}
            if task in {TaskKind.PROOF, TaskKind.DUPLICATE_PROOF}:
                preferences = ("best", "fable", "opus", "sonnet", "haiku")
            elif task in {
                TaskKind.CLARIFICATION,
                TaskKind.DIAGNOSTIC,
                TaskKind.REVIEW,
            }:
                preferences = ("opus", "best", "fable", "sonnet", "haiku")
            elif task in {TaskKind.SKETCH, TaskKind.MAINTENANCE}:
                preferences = ("sonnet", "opus", "best", "fable", "haiku")
            else:
                preferences = ("haiku", "sonnet", "opus", "best", "fable")
            alias_model = next(
                (model_id for model_id in preferences if model_id in available),
                None,
            )
            if alias_model is not None:
                return alias_model

        strong_tokens = ("sol", "opus", "pro", "reason", "o3", "o4")
        light_tokens = ("luna", "haiku", "flash", "mini", "nano")

        def strength(item: ModelDescriptor) -> int:
            lowered = item.model_id.casefold()
            if any(token in lowered for token in strong_tokens):
                return 2
            if any(token in lowered for token in light_tokens):
                return 0
            return 1

        strong_tasks = {
            TaskKind.CLARIFICATION,
            TaskKind.DIAGNOSTIC,
            TaskKind.PROOF,
            TaskKind.REVIEW,
            TaskKind.DUPLICATE_PROOF,
        }
        if task in strong_tasks:
            selected = max(
                enumerate(models), key=lambda pair: (strength(pair[1]), -pair[0])
            )[1]
        elif task is TaskKind.REPORTING:
            selected = min(
                enumerate(models), key=lambda pair: (strength(pair[1]), pair[0])
            )[1]
        else:
            selected = min(
                enumerate(models),
                key=lambda pair: (abs(strength(pair[1]) - 1), pair[0]),
            )[1]
        return selected.model_id

    @staticmethod
    def _recommended_difficulty(task: TaskKind, driver: DriverId) -> Difficulty:
        if task is TaskKind.REPORTING:
            return Difficulty.LOW
        if task in {TaskKind.SKETCH, TaskKind.MAINTENANCE}:
            return Difficulty.MEDIUM
        if driver is DriverId.GEMINI_API:
            return Difficulty.HIGH
        return Difficulty.HIGH

    def get_setup_snapshot(self) -> ProviderSetupSnapshot:
        settings = self.config_store.load()
        statuses = tuple(
            self.inspect_driver(
                driver,
                preference=settings.config.preference_for(driver),
            )
            for driver in DriverId
        )
        primary = next(
            item for item in statuses if item.driver is settings.config.primary_driver
        )
        model_ready = False
        model_detail = "Primary AI driver has no validated model catalog."
        if primary.catalog is not None and primary.catalog.contract_approved:
            try:
                policy = self.recommend_task_policy(
                    TaskKind.PROOF,
                    settings=settings,
                    catalog=primary.catalog,
                )
            except (ProviderConfigError, UnsupportedDifficultyError) as exc:
                model_detail = str(exc)
            else:
                model_ready = policy.model is not None
                model_detail = (
                    f"Proof policy resolves to {policy.model}."
                    if policy.model is not None
                    else "Primary AI driver has no usable proof model."
                )
        ready = primary.ready and model_ready
        return ProviderSetupSnapshot(
            settings=settings,
            statuses=statuses,
            primary_driver=settings.config.primary_driver,
            primary_ready=ready,
            detail=(
                f"Primary AI driver is ready. {model_detail}"
                if ready
                else "Primary AI driver needs installation, authentication, or a valid model contract. "
                + model_detail
            ),
        )
