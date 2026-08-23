from __future__ import annotations

import re
import unicodedata
from pathlib import Path


class ManagedProjectPathError(ValueError):
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


def is_in_dropbox(path: str | Path) -> bool:
    """Recognize classic and macOS CloudStorage Dropbox trees."""
    resolved = Path(path).expanduser().resolve(strict=False)
    return any(part.casefold().startswith("dropbox") for part in resolved.parts)


def validate_managed_project_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve(strict=False)
    if is_in_dropbox(resolved):
        raise ManagedProjectPathError(
            "Managed Proof Assistant projects cannot reside in Dropbox; "
            "choose a location such as $HOME/proof-assistant"
        )
    return resolved
