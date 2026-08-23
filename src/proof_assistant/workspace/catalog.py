from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..incremental.io import atomic_write_json
from .paths import default_projects_root

CATALOG_SCHEMA_VERSION = 1


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
        self.path = (
            (
                path
                or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                / "proof-assistant"
                / "projects.json"
            )
            .expanduser()
            .resolve(strict=False)
        )

    @staticmethod
    def _record_from_project(project: Path) -> CatalogProject | None:
        config_path = project / ".repoprover" / "config.json"
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            source = Path(str(payload["manuscript"])).expanduser().resolve()
            project_id = str(payload.get("project_id") or project.resolve())
            name = str(payload.get("name") or project.name)
            workflow_path = project / ".repoprover" / "workflow.json"
            try:
                workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                workflow = {}
            last_opened = str(
                workflow.get("updated_at")
                or payload.get("last_opened_at")
                or payload["created_at"]
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return CatalogProject(project_id, name, project.resolve(), source, last_opened)

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": CATALOG_SCHEMA_VERSION, "projects": []}
        if payload.get("schema_version") != CATALOG_SCHEMA_VERSION or not isinstance(
            payload.get("projects"), list
        ):
            return {"schema_version": CATALOG_SCHEMA_VERSION, "projects": []}
        return payload

    def records(self) -> tuple[CatalogProject, ...]:
        paths: set[Path] = set()
        for item in self._load()["projects"]:
            if isinstance(item, dict) and isinstance(item.get("project_path"), str):
                paths.add(Path(item["project_path"]).expanduser().resolve(strict=False))
        root = default_projects_root()
        if root.is_dir():
            paths.update(
                config.parent.parent.resolve()
                for config in root.glob("*/.repoprover/config.json")
            )
        records = [
            record
            for record in (self._record_from_project(path) for path in paths)
            if record is not None
        ]
        records.sort(key=lambda item: (item.last_opened_at, item.name), reverse=True)
        self._write(records)
        return tuple(records)

    def upsert(self, project: Path) -> CatalogProject:
        record = self._record_from_project(project.resolve())
        if record is None:
            raise ValueError(f"Not a Proof Assistant project: {project}")
        records = {
            item.project_path: item
            for item in self.records()
            if item.project_path != project
        }
        records[record.project_path] = record
        self._write(records.values())
        return record

    def _write(self, records: object) -> None:
        values = sorted(
            list(records),
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
