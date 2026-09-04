import json
import stat
from dataclasses import replace

import pytest

from proof_assistant.ai import (
    CredentialSource,
    Difficulty,
    DriverId,
    DriverPreference,
    MachineProviderConfigStore,
    ModelDescriptor,
    ProviderConfig,
    ProviderConfigError,
    ProviderConfigRevisionError,
    TaskKind,
    TaskPreference,
    config_from_mapping,
    config_to_mapping,
    default_provider_config_path,
)


def test_default_provider_config_path_is_machine_scoped(tmp_path):
    assert default_provider_config_path(home=tmp_path) == (
        tmp_path / ".config" / "proof-assistant" / "providers.json"
    )
    assert (
        default_provider_config_path(
            environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, home=tmp_path
        )
        == tmp_path / "xdg" / "proof-assistant" / "providers.json"
    )


def test_machine_store_rejects_dropbox(tmp_path):
    with pytest.raises(ProviderConfigError, match="Dropbox"):
        MachineProviderConfigStore(tmp_path / "Dropbox" / "providers.json")


def test_config_round_trip_is_atomic_private_and_revisioned(tmp_path):
    path = tmp_path / "config" / "providers.json"
    store = MachineProviderConfigStore(path)
    initial = store.load()
    assert initial.revision == 0
    assert initial.config.primary_driver is DriverId.CODEX_CLI

    preferences = tuple(
        replace(
            item,
            credential_source=(
                CredentialSource.CREDENTIAL_STORE
                if item.driver is DriverId.ANTHROPIC_API
                else item.credential_source
            ),
            model="claude-opus-4-6"
            if item.driver is DriverId.ANTHROPIC_API
            else item.model,
        )
        for item in initial.config.drivers
    ) + (
        DriverPreference(
            driver=DriverId.ANTHROPIC_API,
            credential_source=CredentialSource.CREDENTIAL_STORE,
            model="claude-opus-4-6",
        ),
    )
    config = ProviderConfig(
        primary_driver=DriverId.ANTHROPIC_API,
        drivers=preferences,
        tasks=(
            TaskPreference(
                TaskKind.PROOF,
                driver=DriverId.ANTHROPIC_API,
                difficulty=Difficulty.HIGH,
            ),
        ),
    )
    saved = store.save(config, expected_revision=0)
    assert saved.revision == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600
    assert store.load() == saved
    payload = path.read_text(encoding="utf-8")
    assert "claude-opus-4-6" in payload
    assert "api_key" not in payload.casefold()
    assert not list(path.parent.glob("*.tmp"))

    with pytest.raises(ProviderConfigRevisionError) as caught:
        store.save(config, expected_revision=0)
    assert caught.value.actual == 1


def test_machine_store_rejects_secret_fields_without_echoing_value(tmp_path):
    secret = "sk-super-secret-value"
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope": "MACHINE",
                "revision": 1,
                "providers": {"api_key": secret},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProviderConfigError) as caught:
        MachineProviderConfigStore(path).load()
    assert secret not in str(caught.value)


def test_config_mapping_is_strict_and_never_has_secret_slots():
    config = ProviderConfig(
        primary_driver=DriverId.GEMINI_API,
        drivers=(
            DriverPreference(
                DriverId.GEMINI_API,
                credential_source=CredentialSource.CREDENTIAL_STORE,
                model="gemini-live",
                difficulty=Difficulty.HIGH,
            ),
        ),
        tasks=(TaskPreference(TaskKind.REPORTING, difficulty=Difficulty.LOW),),
    )
    payload = config_to_mapping(config)
    assert config_from_mapping(payload) == config
    assert "secret" not in json.dumps(payload).casefold()
    assert "token" not in json.dumps(payload).casefold()

    with pytest.raises(ProviderConfigError, match="Unknown provider"):
        config_from_mapping({"unknown": True})
    with pytest.raises(ProviderConfigError, match="Unknown providers.drivers"):
        config_from_mapping(
            {
                "drivers": [
                    {
                        "driver": "openai_api",
                        "credential_source": "environment",
                        "api_key": "forbidden",
                    }
                ]
            }
        )


def test_provider_config_rejects_duplicate_contract_entries():
    duplicate = DriverPreference(DriverId.CODEX_CLI, CredentialSource.NONE)
    with pytest.raises(ValueError, match="unique"):
        ProviderConfig(drivers=(duplicate, duplicate))
    task = TaskPreference(TaskKind.PROOF)
    with pytest.raises(ValueError, match="unique"):
        ProviderConfig(tasks=(task, task))


@pytest.mark.parametrize(
    "model_id",
    (
        "",
        "-leading-hyphen",
        "model with spaces",
        "model+unsafe",
        "model`command`",
        "model\nsecond-line",
        "model\rreturn",
        "model\tfield",
        "model\x1b[31mred",
        "m" * 257,
        "模型",
    ),
)
def test_all_configured_model_contracts_reject_unsafe_provider_ids(model_id):
    with pytest.raises(ValueError, match="safe provider identifier"):
        ModelDescriptor(model_id, "Display name")
    with pytest.raises(ValueError, match="safe provider identifier"):
        DriverPreference(DriverId.CODEX_CLI, model=model_id)
    with pytest.raises(ValueError, match="safe provider identifier"):
        TaskPreference(TaskKind.PROOF, model=model_id)


def test_all_configured_model_contracts_accept_the_same_safe_provider_id():
    model_id = "provider/models:model.v1_2-release"
    assert ModelDescriptor(model_id, "Display name").model_id == model_id
    assert DriverPreference(DriverId.CODEX_CLI, model=model_id).model == model_id
    assert TaskPreference(TaskKind.PROOF, model=model_id).model == model_id


@pytest.mark.parametrize(
    ("section", "model_id"),
    (
        ("drivers", "model\x1b[2Jhostile"),
        ("drivers", "model\nsecond-line"),
        ("drivers", "model with spaces"),
        ("tasks", "model`command`"),
        ("tasks", "model\tfield"),
        ("tasks", "m" * 257),
    ),
)
def test_deserialization_rejects_unsafe_configured_model_ids(section, model_id):
    payload = {
        "primary_driver": "codex_cli",
        "drivers": [
            {
                "driver": "codex_cli",
                "credential_source": "none",
                "model": model_id if section == "drivers" else None,
                "runtime_verified_version": "Codex CLI 1.2.3+build (arm64)",
            }
        ],
        "tasks": [
            {
                "task": "proof",
                "model": model_id if section == "tasks" else None,
            }
        ],
    }

    with pytest.raises(ProviderConfigError, match="safe provider identifier") as caught:
        config_from_mapping(payload)
    assert model_id not in str(caught.value)
