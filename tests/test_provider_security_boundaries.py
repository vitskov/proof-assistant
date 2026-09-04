from __future__ import annotations

import ast
import inspect
import json
import pickle
from dataclasses import fields
from pathlib import Path

import pytest

from proof_assistant.ai import (
    AuthenticationState,
    CredentialSource,
    DiscoverySource,
    DriverId,
    DriverPreference,
    DriverStatus,
    DriverTransport,
    InstallationState,
    MachineProviderConfigStore,
    MachineProviderSettings,
    ModelCatalog,
    ModelDescriptor,
    ProviderConfig,
    ProviderConfigError,
    ProviderSetupSnapshot,
    SecretSubmission,
)
from proof_assistant.ai.execution import AIBackend, AIBackendConfig, _redacted
from proof_assistant.ai.runtime import CommandResult, NullCredentialStore
from proof_assistant.ai.service import ProviderService, _clean_version
from proof_assistant.cli import build_parser
from proof_assistant.manuscript import prepare_manuscript_workspace
from proof_assistant.workflow.contracts import WorkflowServiceContract, contract_dict

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "src" / "proof_assistant"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_provider_tui_is_a_thin_workflow_client() -> None:
    """Provider UI must not grow a second setup/runtime implementation."""

    forbidden_roots = {
        "httpx",
        "keyring",
        "requests",
        "subprocess",
        "urllib",
    }
    forbidden_internal = {
        "proof_assistant.ai",
        "proof_assistant.incremental",
        "proof_assistant.workspace",
    }
    violations: list[str] = []
    for path in (PACKAGE / "tui").rglob("*.py"):
        for imported in _imports(path):
            root = imported.split(".", 1)[0]
            if root in forbidden_roots or any(
                imported == prefix or imported.startswith(prefix + ".")
                for prefix in forbidden_internal
            ):
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
    assert violations == []


def test_provider_runtime_never_names_or_reads_native_cli_auth_files() -> None:
    """CLI account checks must use documented commands, never auth-store parsing."""

    forbidden_names = (
        "auth.json",
        ".claude.json",
        "hosts.yml",
        "copilot-internal",
    )
    violations: list[str] = []
    for path in (PACKAGE / "ai").rglob("*.py"):
        source = path.read_text(encoding="utf-8").casefold()
        for name in forbidden_names:
            if name.casefold() in source:
                violations.append(f"{path.relative_to(ROOT)} names {name}")
    assert violations == []


@pytest.mark.parametrize(
    "secret_field",
    (
        "access_token",
        "refreshToken",
        "client_secret",
        "privateKey",
        "bearer-token",
    ),
)
def test_machine_config_rejects_ignored_or_camel_case_secret_fields(
    tmp_path: Path, secret_field: str
) -> None:
    secret = "provider-secret-must-not-survive"
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope": "MACHINE",
                "revision": 0,
                "providers": {},
                secret_field: secret,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProviderConfigError) as caught:
        MachineProviderConfigStore(path).load()
    assert secret not in str(caught.value)


def test_sanitized_setup_contract_has_no_credential_value_slot() -> None:
    model = ModelDescriptor("account-model", "Account model")
    catalog = ModelCatalog(
        driver=DriverId.CODEX_CLI,
        models=(model,),
        source=DiscoverySource.LIVE_ACCOUNT,
        contract_approved=True,
    )
    status = DriverStatus(
        driver=DriverId.CODEX_CLI,
        transport=DriverTransport.CLI,
        installation=InstallationState.INSTALLED,
        authentication=AuthenticationState.AUTHENTICATED,
        catalog=catalog,
    )
    settings = MachineProviderSettings(config=ProviderConfig())
    snapshot = ProviderSetupSnapshot(
        settings=settings,
        statuses=(status,),
        primary_driver=DriverId.CODEX_CLI,
        primary_ready=True,
        detail="ready",
    )

    payload = contract_dict(snapshot)
    flattened_names = {
        field.name.casefold()
        for contract in (
            ModelCatalog,
            DriverStatus,
            DriverPreference,
            ProviderConfig,
            MachineProviderSettings,
            ProviderSetupSnapshot,
        )
        for field in fields(contract)
    }
    assert not flattened_names.intersection(
        {"api_key", "credential", "password", "secret", "token"}
    )
    assert "credential_value" not in repr(payload).casefold()


@pytest.mark.parametrize(
    ("model_id", "display_name"),
    (
        ("model\x1b[2J", "Model"),
        ("model", "Model\x1b]8;;https://attacker.invalid\x07click"),
        ("model\nforged-log-line", "Model"),
        ("model", "Model\rforged-status"),
    ),
)
def test_model_contract_rejects_terminal_and_log_control_characters(
    model_id: str, display_name: str
) -> None:
    with pytest.raises(ValueError, match="control|character|model"):
        ModelDescriptor(model_id, display_name)


def test_provider_error_redaction_removes_terminal_escape_sequences() -> None:
    secret = "api-secret"
    raw = f"\x1b[2Jauthorization: Bearer {secret}\nordinary detail"
    sanitized = _redacted(raw, (secret,))
    assert secret not in sanitized
    assert "\x1b" not in sanitized
    assert "ordinary detail" in sanitized


def test_cli_version_probe_removes_terminal_escape_sequences() -> None:
    version = _clean_version(CommandResult(0, "\x1b[2Jcodex-cli 1.2\x07"))
    assert version is not None
    assert "codex-cli 1.2" in version
    assert "\x1b" not in version
    assert "\x07" not in version


def test_secret_submission_is_redacted_one_shot_and_not_serializable() -> None:
    secret = "test-secret-never-serialize"
    submission = SecretSubmission(secret)
    assert secret not in repr(submission)
    assert secret not in str(submission)
    with pytest.raises((TypeError, pickle.PicklingError)):
        pickle.dumps(submission)
    assert submission.consume() == secret
    with pytest.raises(RuntimeError, match="already been consumed"):
        submission.consume()


def test_workflow_credential_boundary_uses_one_shot_secret_contract() -> None:
    """A raw string must not become a serializable UI/backend DTO argument."""

    parameter = inspect.signature(
        WorkflowServiceContract.store_ai_credential
    ).parameters["credential"]
    assert parameter.annotation in {SecretSubmission, "SecretSubmission"}


def test_account_verification_backend_contract_requires_explicit_consent() -> None:
    """A caller must not trigger even a tiny quota request by a bare method call."""

    service_parameters = inspect.signature(
        ProviderService.verify_cli_account
    ).parameters
    workflow_parameters = inspect.signature(
        WorkflowServiceContract.verify_ai_driver_account
    ).parameters
    consent_names = {"approved", "consent", "consent_token"}
    assert consent_names.intersection(service_parameters)
    assert consent_names.intersection(workflow_parameters)


def test_copilot_account_probe_requires_an_exact_success_response(
    tmp_path: Path,
) -> None:
    class Executables:
        def which(self, executable: str, *, path: str | None = None) -> str | None:
            del path
            return "/tools/copilot" if executable == "copilot" else None

    class Commands:
        def run(
            self,
            argv: tuple[str, ...],
            *,
            input_text: str | None = None,
            timeout_seconds: float = 30.0,
            env: object = None,
        ) -> CommandResult:
            del input_text, timeout_seconds, env
            if argv == ("/tools/copilot", "version"):
                return CommandResult(0, "GitHub Copilot CLI 1.0")
            if argv == ("/tools/copilot", "help"):
                return CommandResult(0, "GitHub Copilot prompt agent")
            if "-p" in argv:
                return CommandResult(0, '{"content":"NOT OK"}\n')
            raise AssertionError(argv)

    service = ProviderService(
        config_store=MachineProviderConfigStore(tmp_path / "providers.json"),
        commands=Commands(),
        executables=Executables(),
        credentials=NullCredentialStore(),
        environment={"PATH": "/usr/bin"},
        home=tmp_path / "home",
    )

    status = service.verify_cli_account(DriverId.COPILOT_CLI, consent=True)

    assert status.authentication is not AuthenticationState.AUTHENTICATED
    assert (
        service.config_store.load()
        .config.preference_for(DriverId.COPILOT_CLI)
        .runtime_verified_version
        is None
    )


def test_primary_driver_contract_rejects_missing_or_disabled_preference() -> None:
    with pytest.raises(ValueError, match="primary"):
        ProviderConfig(
            primary_driver=DriverId.CODEX_CLI,
            drivers=(DriverPreference(DriverId.CLAUDE_CLI),),
        )
    with pytest.raises(ValueError, match="primary"):
        ProviderConfig(
            primary_driver=DriverId.CODEX_CLI,
            drivers=(
                DriverPreference(
                    DriverId.CODEX_CLI,
                    credential_source=CredentialSource.NONE,
                    enabled=False,
                ),
            ),
        )


def test_model_catalog_rejects_impossible_provenance_states() -> None:
    model = ModelDescriptor("model", "Model")
    with pytest.raises(ValueError, match="live|LIVE|model"):
        ModelCatalog(
            driver=DriverId.OPENAI_API,
            source=DiscoverySource.LIVE_ACCOUNT,
            contract_approved=True,
        )
    with pytest.raises(ValueError, match="approved|contract|unavailable"):
        ModelCatalog(
            driver=DriverId.OPENAI_API,
            models=(model,),
            source=DiscoverySource.UNAVAILABLE,
            contract_approved=True,
        )


def test_cli_has_no_api_key_command_line_argument() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["ai", "credential", "codex_cli", "--stdin"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["ai", "select", "codex_cli", "--api-key", "must-not-parse"]
        )


def test_claude_cli_does_not_silently_inherit_anthropic_api_billing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingCommand:
        def __init__(self) -> None:
            self.environment: dict[str, str] = {}

        def run(
            self,
            argv: tuple[str, ...],
            *,
            input_text: str | None = None,
            timeout_seconds: float = 30.0,
            env: object = None,
        ) -> CommandResult:
            del argv, input_text, timeout_seconds
            self.environment = dict(env or {})  # type: ignore[arg-type]
            return CommandResult(
                0,
                '{"result":"done","session_id":"session"}',
                "",
            )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key-must-not-cross-cli-boundary")
    command = RecordingCommand()
    backend = AIBackend(
        AIBackendConfig(
            driver=DriverId.CLAUDE_CLI,
            model="sonnet",
            provider_config_path=tmp_path / "providers.json",
        ),
        command_runner=command,
    )
    backend.run(
        system_prompt="system",
        user_prompt="task",
        tools=(),
        tool_handler=lambda _name, _arguments: "unused",
    )
    assert "ANTHROPIC_API_KEY" not in command.environment
    assert "CLAUDE_CODE_USE_BEDROCK" not in command.environment
    assert "CLAUDE_CODE_USE_VERTEX" not in command.environment


@pytest.mark.parametrize(
    "private_path",
    (
        ".ENV",
        ".Env.Local",
        "AUTH.JSON",
        "Credentials.Json",
        "ID_RSA",
        "Service-Account.JSON",
        ".SSH/config",
        ".Claude/settings.json",
        ".CODEX/auth.json",
        ".Copilot/config.json",
    ),
)
def test_manuscript_snapshot_excludes_private_machine_files_case_insensitively(
    tmp_path: Path, private_path: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text("Manuscript\n", encoding="utf-8")
    private_file = source / private_path
    private_file.parent.mkdir(parents=True, exist_ok=True)
    private_file.write_text("private-machine-data\n", encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text("Verify the manuscript.\n", encoding="utf-8")

    prepared = prepare_manuscript_workspace(source, tmp_path / "output", task)

    copied_root = (
        prepared.workspace / "manuscript"
        if prepared.source_mode == "generated_lean_project"
        else prepared.workspace
    )
    assert not (copied_root / private_path).exists()
    assert "private-machine-data" not in "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in copied_root.rglob("*")
        if path.is_file()
    )
