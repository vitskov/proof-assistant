"""Small machine-local preferences that are independent of project state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from proof_assistant.workspace.paths import (
    ProofAssistantWritePathError,
    default_projects_root,
    is_in_dropbox,
    validate_proof_assistant_write_path,
)

PREFERENCES_SCHEMA_VERSION = 1


class LocalPreferenceLocationError(ValueError):
    """A preference store was placed in managed or synchronized storage."""


def default_local_preferences_path() -> Path:
    """Return a stable local path; never inherit a Dropbox-backed XDG path."""

    home_default = (
        Path.home() / ".config" / "proof-assistant" / "preferences.json"
    ).resolve(strict=False)
    projects = default_projects_root()
    configured_root = os.environ.get("XDG_CONFIG_HOME")
    if configured_root:
        candidate = (
            Path(configured_root).expanduser() / "proof-assistant" / "preferences.json"
        ).resolve(strict=False)
        if (
            not is_in_dropbox(candidate)
            and candidate != projects
            and not candidate.is_relative_to(projects)
        ):
            return candidate
    return home_default


class LocalPreferenceStore:
    """Atomic store for non-project convenience preferences.

    Invalid content is treated as absent because this data is advisory: it must
    never prevent the user from reaching the folder chooser or opening a
    project.  The selected manuscript itself may be in Dropbox; only this
    machine-local preference file is prohibited there.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = (
            (path or default_local_preferences_path())
            .expanduser()
            .resolve(strict=False)
        )
        projects = default_projects_root()
        try:
            validate_proof_assistant_write_path(
                self.path, purpose="Proof Assistant preferences"
            )
        except ProofAssistantWritePathError as exc:
            raise LocalPreferenceLocationError(str(exc)) from exc
        if self.path == projects or self.path.is_relative_to(projects):
            raise LocalPreferenceLocationError(
                "Proof Assistant preferences cannot reside inside managed projects"
            )

    def load_manuscript_folder(self) -> Path | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != PREFERENCES_SCHEMA_VERSION
                or not isinstance(payload.get("last_manuscript_folder"), str)
            ):
                return None
            return (
                Path(payload["last_manuscript_folder"])
                .expanduser()
                .resolve(strict=False)
            )
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError):
            return None

    def save_manuscript_folder(self, folder: Path) -> Path:
        resolved = folder.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Manuscript folder is not a directory: {resolved}")
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
                temporary_path = Path(temporary.name)
                json.dump(
                    {
                        "schema_version": PREFERENCES_SCHEMA_VERSION,
                        "last_manuscript_folder": str(resolved),
                    },
                    temporary,
                    indent=2,
                    sort_keys=True,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            assert temporary_path is not None
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return resolved
