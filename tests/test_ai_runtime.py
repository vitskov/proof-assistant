import sys
from types import SimpleNamespace

import pytest

from proof_assistant.ai import (
    CompositeCredentialStore,
    CredentialSource,
    CredentialStoreUnavailableError,
    DriverId,
    EnvironmentCredentialStore,
    SecretSubmission,
    ShellPathManager,
    SystemKeyringCredentialStore,
)


def test_secret_submission_is_redacted_and_one_shot():
    secret = "anthropic-test-secret"
    submission = SecretSubmission(secret)
    assert secret not in repr(submission)
    assert secret not in str(submission)
    assert submission.consume() == secret
    with pytest.raises(RuntimeError, match="already"):
        submission.consume()


def test_environment_store_uses_fixed_names_and_cannot_write():
    store = EnvironmentCredentialStore(
        {"OPENAI_API_KEY": "openai-secret", "MADE_UP_KEY": "wrong"}
    )
    assert (
        store.get(DriverId.OPENAI_API, CredentialSource.ENVIRONMENT) == "openai-secret"
    )
    assert store.get(DriverId.GEMINI_API, CredentialSource.ENVIRONMENT) is None
    assert store.get(DriverId.OPENAI_API, CredentialSource.CREDENTIAL_STORE) is None
    with pytest.raises(CredentialStoreUnavailableError):
        store.set(
            DriverId.OPENAI_API,
            CredentialSource.ENVIRONMENT,
            SecretSubmission("new-secret"),
        )


def test_system_keyring_store_never_exposes_secret(monkeypatch):
    saved = {}

    class PasswordDeleteError(Exception):
        pass

    fake = SimpleNamespace(
        errors=SimpleNamespace(PasswordDeleteError=PasswordDeleteError),
        get_password=lambda service, account: saved.get((service, account)),
        set_password=lambda service, account, value: saved.__setitem__(
            (service, account), value
        ),
        delete_password=lambda service, account: saved.pop((service, account)),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    store = SystemKeyringCredentialStore()
    submission = SecretSubmission("gemini-secret")
    store.set(
        DriverId.GEMINI_API,
        CredentialSource.CREDENTIAL_STORE,
        submission,
    )
    assert (
        store.get(DriverId.GEMINI_API, CredentialSource.CREDENTIAL_STORE)
        == "gemini-secret"
    )
    assert store.delete(DriverId.GEMINI_API, CredentialSource.CREDENTIAL_STORE)


def test_composite_store_routes_by_explicit_source():
    calls = []

    class FakeStore:
        def __init__(self, name):
            self.name = name

        def get(self, driver, source):
            calls.append((self.name, "get", driver, source))
            return self.name

        def set(self, driver, source, submission):
            calls.append((self.name, "set", driver, source, submission.consume()))

        def delete(self, driver, source):
            calls.append((self.name, "delete", driver, source))
            return True

    store = CompositeCredentialStore(
        environment=FakeStore("env"), keyring=FakeStore("keyring")
    )
    assert store.get(DriverId.OPENAI_API, CredentialSource.ENVIRONMENT) == "env"
    assert (
        store.get(DriverId.OPENAI_API, CredentialSource.CREDENTIAL_STORE) == "keyring"
    )
    store.set(
        DriverId.OPENAI_API,
        CredentialSource.CREDENTIAL_STORE,
        SecretSubmission("one-shot"),
    )
    assert calls[-1][0:2] == ("keyring", "set")


def test_shell_path_manager_updates_process_and_profile_idempotently(tmp_path):
    environment = {"PATH": "/usr/bin", "SHELL": "/bin/zsh"}
    manager = ShellPathManager(environment=environment, home=tmp_path)
    installer_bin = tmp_path / "local tools" / "bin"
    manager.ensure(installer_bin)
    manager.ensure(installer_bin)
    assert environment["PATH"].split(":", 1)[0] == str(installer_bin)
    profile = (tmp_path / ".zprofile").read_text(encoding="utf-8")
    assert profile.count("Added by Proof Assistant") == 1
    assert "local tools" in profile
