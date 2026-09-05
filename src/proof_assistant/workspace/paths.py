from __future__ import annotations

import json
import re
import tempfile
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path


class ProofAssistantWritePathError(ValueError):
    """Raised before Proof Assistant writes any state into Dropbox."""


class ManagedProjectPathError(ProofAssistantWritePathError):
    pass


def default_projects_root() -> Path:
    return (Path.home() / "proof-assistant").resolve()


def slugify_project_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").casefold()
    if not slug:
        raise ValueError("Project name must contain a letter or number")
    return slug[:80]


def default_project_path(name: str) -> Path:
    return default_projects_root() / slugify_project_name(name)


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _registered_dropbox_roots(home: Path, *, strict: bool) -> tuple[Path, ...]:
    roots: list[Path] = []
    info = home / ".dropbox" / "info.json"
    if info.exists():
        try:
            payload = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            if strict:
                raise ProofAssistantWritePathError(
                    "Dropbox is installed, but Proof Assistant could not verify its "
                    "registered storage roots; refusing to select a write location"
                ) from exc
        else:
            if not isinstance(payload, dict):
                if strict:
                    raise ProofAssistantWritePathError(
                        "Dropbox root metadata has an invalid shape; refusing to "
                        "select a Proof Assistant write location"
                    )
            else:
                invalid_account = not payload
                for account in payload.values():
                    registered_path = (
                        account.get("path") if isinstance(account, dict) else None
                    )
                    if (
                        not isinstance(registered_path, str)
                        or not registered_path.strip()
                    ):
                        invalid_account = True
                        continue
                    candidate = Path(registered_path).expanduser()
                    if not candidate.is_absolute():
                        invalid_account = True
                        continue
                    roots.append(candidate.resolve(strict=False))
                if invalid_account and strict:
                    raise ProofAssistantWritePathError(
                        "Dropbox root metadata contains an invalid account; refusing "
                        "to select a Proof Assistant write location"
                    )

    candidates = (home, home / "Library" / "CloudStorage")
    for parent in candidates:
        try:
            roots.extend(
                entry.resolve(strict=False)
                for entry in parent.iterdir()
                if entry.name.casefold().startswith("dropbox")
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            if strict:
                raise ProofAssistantWritePathError(
                    f"Could not inspect {parent} for Dropbox storage roots; "
                    "refusing to select a Proof Assistant write location"
                ) from exc
    return tuple(dict.fromkeys(roots))


def is_in_dropbox(
    path: str | Path,
    *,
    user_home: str | Path | None = None,
    dropbox_roots: Sequence[str | Path] | None = None,
) -> bool:
    """Recognize registered, classic, and macOS CloudStorage Dropbox trees."""
    resolved = Path(path).expanduser().resolve(strict=False)
    if any(part.casefold().startswith("dropbox") for part in resolved.parts):
        return True
    home = (
        Path(user_home).expanduser().resolve(strict=False)
        if user_home is not None
        else Path.home().resolve(strict=False)
    )
    roots = (
        tuple(Path(item).expanduser().resolve(strict=False) for item in dropbox_roots)
        if dropbox_roots is not None
        else _registered_dropbox_roots(home, strict=False)
    )
    return any(_is_within(resolved, root) for root in roots)


def validate_proof_assistant_write_path(
    path: str | Path,
    *,
    purpose: str = "Proof Assistant work and output",
    user_home: str | Path | None = None,
    dropbox_roots: Sequence[str | Path] | None = None,
) -> Path:
    """Resolve a write target and enforce Dropbox's read-only-input contract."""
    resolved = Path(path).expanduser().resolve(strict=False)
    home = (
        Path(user_home).expanduser().resolve(strict=False)
        if user_home is not None
        else Path.home().resolve(strict=False)
    )
    roots = (
        tuple(Path(item).expanduser().resolve(strict=False) for item in dropbox_roots)
        if dropbox_roots is not None
        else _registered_dropbox_roots(home, strict=True)
    )
    if any(part.casefold().startswith("dropbox") for part in resolved.parts) or any(
        _is_within(resolved, root) for root in roots
    ):
        raise ProofAssistantWritePathError(
            f"{purpose} cannot reside in Dropbox; this is prohibited by design. "
            "Dropbox is supported only as a read-only manuscript source. Choose "
            "a local write location outside Dropbox, such as $HOME/proof-assistant."
        )
    return resolved


def validate_managed_project_path(path: str | Path) -> Path:
    try:
        return validate_proof_assistant_write_path(
            path, purpose="Managed Proof Assistant projects"
        )
    except ProofAssistantWritePathError as exc:
        raise ManagedProjectPathError(str(exc)) from exc


def proof_assistant_temporary_root(
    *,
    system_temp: str | Path | None = None,
    user_home: str | Path | None = None,
    dropbox_roots: Sequence[str | Path] | None = None,
) -> Path:
    """Select a local temporary root without ever falling back to Dropbox."""
    home = (
        Path(user_home).expanduser().resolve(strict=False)
        if user_home is not None
        else Path.home().resolve(strict=False)
    )
    candidate = Path(system_temp or tempfile.gettempdir()).expanduser()
    try:
        root = validate_proof_assistant_write_path(
            candidate,
            purpose="Proof Assistant temporary work",
            user_home=home,
            dropbox_roots=dropbox_roots,
        )
    except ProofAssistantWritePathError:
        root = validate_proof_assistant_write_path(
            home / ".cache" / "proof-assistant" / "tmp",
            purpose="Proof Assistant temporary work",
            user_home=home,
            dropbox_roots=dropbox_roots,
        )
    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
    return root


@contextmanager
def proof_assistant_temporary_directory(*, prefix: str) -> Iterator[Path]:
    """Yield an automatically cleaned temporary directory outside Dropbox."""
    with tempfile.TemporaryDirectory(
        prefix=prefix, dir=proof_assistant_temporary_root()
    ) as raw:
        yield Path(raw)
