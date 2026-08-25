"""Injectable process, HTTP, executable, and credential boundaries.

Default implementations are intentionally small and do not read provider auth
files.  Tests and callers can replace every external effect with a fake.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn, Protocol, SupportsIndex

from .contracts import CredentialSource, DriverId


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_text: str | None = None,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_text: str | None = None,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            argv,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            env=None if env is None else dict(env),
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class ExecutableResolver(Protocol):
    def which(self, executable: str, *, path: str | None = None) -> str | None: ...


class SystemExecutableResolver:
    def which(self, executable: str, *, path: str | None = None) -> str | None:
        return shutil.which(executable, path=path)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))


class HttpRunner(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float = 30.0,
    ) -> HttpResponse: ...


class UrllibHttpRunner:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float = 30.0,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status=int(response.status),
                    body=response.read(),
                    headers=tuple(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=int(exc.code),
                body=exc.read(),
                headers=tuple(exc.headers.items()) if exc.headers else (),
            )


class CredentialStore(Protocol):
    def get(self, driver: DriverId, source: CredentialSource) -> str | None: ...

    def set(
        self,
        driver: DriverId,
        source: CredentialSource,
        submission: SecretSubmission,
    ) -> None: ...

    def delete(self, driver: DriverId, source: CredentialSource) -> bool: ...


class SecretSubmission:
    """A one-shot credential value whose representation is always redacted."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value or not value.strip():
            raise ValueError("credential must not be empty")
        self._value: str | None = value

    def consume(self) -> str:
        value = self._value
        if value is None:
            raise RuntimeError("credential submission has already been consumed")
        self._value = None
        return value

    def __repr__(self) -> str:
        return "SecretSubmission(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("SecretSubmission cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise TypeError("SecretSubmission cannot be serialized")


class CredentialStoreUnavailableError(RuntimeError):
    pass


_ENVIRONMENT_KEYS: dict[DriverId, str] = {
    DriverId.OPENAI_API: "OPENAI_API_KEY",
    DriverId.ANTHROPIC_API: "ANTHROPIC_API_KEY",
    DriverId.GEMINI_API: "GEMINI_API_KEY",
}


class EnvironmentCredentialStore:
    """Read API keys only from their fixed, documented environment variables."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def get(self, driver: DriverId, source: CredentialSource) -> str | None:
        if source is not CredentialSource.ENVIRONMENT:
            return None
        variable = _ENVIRONMENT_KEYS.get(driver)
        if variable is None:
            return None
        value = self._environment.get(variable)
        return value if value and value.strip() else None

    def set(
        self,
        driver: DriverId,
        source: CredentialSource,
        submission: SecretSubmission,
    ) -> None:
        raise CredentialStoreUnavailableError(
            "Environment credentials cannot be changed by Proof Assistant"
        )

    def delete(self, driver: DriverId, source: CredentialSource) -> bool:
        raise CredentialStoreUnavailableError(
            "Environment credentials cannot be changed by Proof Assistant"
        )


class SystemKeyringCredentialStore:
    """Optional OS-keyring adapter; no credential is ever written to config."""

    SERVICE = "proof-assistant"

    @staticmethod
    def _account(driver: DriverId) -> str:
        if driver not in _ENVIRONMENT_KEYS:
            raise CredentialStoreUnavailableError(
                f"{driver.value} does not use an API-key credential"
            )
        return driver.value

    @staticmethod
    def _keyring() -> object:
        try:
            import keyring
        except ImportError as exc:
            raise CredentialStoreUnavailableError(
                "OS keyring support is unavailable; install the `keyring` package "
                "or use the documented environment variable"
            ) from exc
        return keyring

    def get(self, driver: DriverId, source: CredentialSource) -> str | None:
        if source is not CredentialSource.CREDENTIAL_STORE:
            return None
        keyring = self._keyring()
        value = keyring.get_password(self.SERVICE, self._account(driver))  # type: ignore[attr-defined]
        return value if value and value.strip() else None

    def set(
        self,
        driver: DriverId,
        source: CredentialSource,
        submission: SecretSubmission,
    ) -> None:
        if source is not CredentialSource.CREDENTIAL_STORE:
            raise CredentialStoreUnavailableError(
                "Only credential_store submissions can be written to the OS keyring"
            )
        keyring = self._keyring()
        value = submission.consume()
        try:
            keyring.set_password(self.SERVICE, self._account(driver), value)  # type: ignore[attr-defined]
        except Exception:
            raise CredentialStoreUnavailableError(
                "The operating-system credential store rejected the credential"
            ) from None

    def delete(self, driver: DriverId, source: CredentialSource) -> bool:
        if source is not CredentialSource.CREDENTIAL_STORE:
            return False
        keyring = self._keyring()
        try:
            keyring.delete_password(self.SERVICE, self._account(driver))  # type: ignore[attr-defined]
        except keyring.errors.PasswordDeleteError:  # type: ignore[attr-defined]
            return False
        return True


class CompositeCredentialStore:
    def __init__(
        self,
        *,
        environment: CredentialStore | None = None,
        keyring: CredentialStore | None = None,
    ) -> None:
        self._environment = environment or EnvironmentCredentialStore()
        self._keyring = keyring or SystemKeyringCredentialStore()

    def _store(self, source: CredentialSource) -> CredentialStore:
        if source is CredentialSource.ENVIRONMENT:
            return self._environment
        if source is CredentialSource.CREDENTIAL_STORE:
            return self._keyring
        raise CredentialStoreUnavailableError("No credential source is configured")

    def get(self, driver: DriverId, source: CredentialSource) -> str | None:
        if source is CredentialSource.NONE:
            return None
        return self._store(source).get(driver, source)

    def set(
        self,
        driver: DriverId,
        source: CredentialSource,
        submission: SecretSubmission,
    ) -> None:
        self._store(source).set(driver, source, submission)

    def delete(self, driver: DriverId, source: CredentialSource) -> bool:
        if source is CredentialSource.NONE:
            return False
        return self._store(source).delete(driver, source)


class NullCredentialStore:
    def get(self, driver: DriverId, source: CredentialSource) -> str | None:
        return None

    def set(
        self,
        driver: DriverId,
        source: CredentialSource,
        submission: SecretSubmission,
    ) -> None:
        raise CredentialStoreUnavailableError("No credential store is configured")

    def delete(self, driver: DriverId, source: CredentialSource) -> bool:
        return False
