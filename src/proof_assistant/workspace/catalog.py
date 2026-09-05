from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from ..incremental.io import atomic_write_json
from ..json_types import JSONValue, load_json
from .paths import (
    ProofAssistantWritePathError,
    default_projects_root,
    validate_proof_assistant_write_path,
)

CATALOG_SCHEMA_VERSION = 1


class CatalogLocationError(ValueError):
    """Raised when the machine-local catalog is placed in Dropbox."""


class _CatalogPayload(TypedDict):
    schema_version: int
    projects: list[JSONValue]


def _catalog_project_path(item: JSONValue) -> Path | None:
    if not isinstance(item, dict):
        return None
    value = item.get("project_path")
    if not isinstance(value, str):
        return None
    return Path(value).expanduser().resolve(strict=False)


@dataclass(frozen=True)
class CatalogProject:
    project_id: str
    name: str
    project_path: Path
    source_path: Path
    last_opened_at: str


class ProjectCatalog:
    """A disposable convenience index; every project remains self-describing."""

    def __init__(self, path: Path | None = None) -> None:
        self.discover_default_root = path is None
        candidate = (
            (
                path
                or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                / "proof-assistant"
                / "projects.json"
            )
            .expanduser()
            .resolve(strict=False)
        )
        try:
            self.path = validate_proof_assistant_write_path(
                candidate, purpose="The Proof Assistant project catalog"
            )
        except ProofAssistantWritePathError as exc:
            raise CatalogLocationError(str(exc)) from exc

    @staticmethod
    def _record_from_project(project: Path) -> CatalogProject | None:
        config_path = project / ".repoprover" / "config.json"
        try:
            payload = load_json(config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            source = Path(str(payload["manuscript"])).expanduser().resolve()
            project_id = str(payload.get("project_id") or project.resolve())
            name = str(payload.get("name") or project.name)
            workflow_path = project / ".repoprover" / "workflow.json"
            try:
                decoded_workflow = load_json(workflow_path.read_text(encoding="utf-8"))
                workflow = (
                    decoded_workflow if isinstance(decoded_workflow, dict) else {}
                )
            except (OSError, ValueError):
                workflow = {}
            last_opened = str(
                workflow.get("updated_at")
                or payload.get("last_opened_at")
                or payload["created_at"]
            )
        except (OSError, KeyError, TypeError, ValueError):
            return None
        return CatalogProject(project_id, name, project.resolve(), source, last_opened)

    def _load(self) -> _CatalogPayload:
        try:
            decoded = load_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema_version": CATALOG_SCHEMA_VERSION, "projects": []}
        if not isinstance(decoded, dict):
            return {"schema_version": CATALOG_SCHEMA_VERSION, "projects": []}
        projects = decoded.get("projects")
        if decoded.get("schema_version") != CATALOG_SCHEMA_VERSION or not isinstance(
            projects, list
        ):
            return {"schema_version": CATALOG_SCHEMA_VERSION, "projects": []}
        return {"schema_version": CATALOG_SCHEMA_VERSION, "projects": projects}

    def candidate_paths(self) -> tuple[Path, ...]:
        """Return every path the catalog must account for, valid or not.

        A malformed or incomplete project is deliberately retained.  The
        workflow service owns classification; this convenience index must not
        make occupied paths disappear merely because their config cannot be
        parsed.
        """

        paths: set[Path] = set()
        for item in self._load()["projects"]:
            candidate = _catalog_project_path(item)
            if candidate is not None:
                paths.add(candidate)
        root = default_projects_root()
        if self.discover_default_root and root.is_dir():
            paths.update(
                item.resolve(strict=False) for item in root.iterdir() if item.is_dir()
            )
        return tuple(sorted(paths, key=lambda item: str(item).casefold()))

    def remember_path(self, project: Path) -> None:
        """Retain an occupied path without claiming that it is a valid project."""

        resolved = project.expanduser().resolve(strict=False)
        payload = self._load()
        projects = [
            item
            for item in payload["projects"]
            if _catalog_project_path(item) not in {None, resolved}
        ]
        projects.append({"project_path": str(resolved)})
        atomic_write_json(
            self.path,
            {"schema_version": CATALOG_SCHEMA_VERSION, "projects": projects},
        )

    def records(self) -> tuple[CatalogProject, ...]:
        records = [
            record
            for record in (
                self._record_from_project(path) for path in self.candidate_paths()
            )
            if record is not None
        ]
        records.sort(key=lambda item: (item.last_opened_at, item.name), reverse=True)
        return tuple(records)

    def upsert(self, project: Path) -> CatalogProject:
        record = self._record_from_project(project.resolve())
        if record is None:
            raise ValueError(f"Not a Proof Assistant project: {project}")
        payload = self._load()
        retained = [
            item
            for item in payload["projects"]
            if _catalog_project_path(item) not in {None, record.project_path}
        ]
        retained.append(
            {
                "project_id": record.project_id,
                "name": record.name,
                "project_path": str(record.project_path),
                "source_path": str(record.source_path),
                "last_opened_at": record.last_opened_at,
            }
        )
        atomic_write_json(
            self.path,
            {"schema_version": CATALOG_SCHEMA_VERSION, "projects": retained},
        )
        return record

    def forget_path(self, project: Path) -> None:
        """Remove exactly one moved project path from the disposable index."""

        resolved = project.expanduser().resolve(strict=False)
        payload = self._load()
        retained: list[JSONValue] = []
        for item in payload["projects"]:
            candidate = _catalog_project_path(item)
            if candidate is None:
                retained.append(item)
                continue
            if candidate != resolved:
                retained.append(item)
        atomic_write_json(
            self.path,
            {"schema_version": CATALOG_SCHEMA_VERSION, "projects": retained},
        )

    def _write(self, records: Iterable[CatalogProject]) -> None:
        values = sorted(
            records,
            key=lambda item: (item.last_opened_at, item.name),
            reverse=True,
        )
        atomic_write_json(
            self.path,
            {
                "schema_version": CATALOG_SCHEMA_VERSION,
                "projects": [
                    {
                        "project_id": item.project_id,
                        "name": item.name,
                        "project_path": str(item.project_path),
                        "source_path": str(item.source_path),
                        "last_opened_at": item.last_opened_at,
                    }
                    for item in values
                ],
            },
        )
