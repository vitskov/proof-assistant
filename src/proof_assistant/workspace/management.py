"""Project-management backend: occupancy, reconciliation, and legacy recovery.

This module is deliberately UI-neutral.  It is the sole owner of deciding
whether a managed path is creatable, resumable, recoverable, incomplete, or
merely occupied.  Callers must never delete an occupied path to make it fit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .. import __version__
from ..incremental.io import atomic_write_json
from ..incremental.latex import (
    LatexIndexError,
    discover_latex_sources,
    resolve_latex_closure,
)
from ..incremental.store import StateStore
from .catalog import ProjectCatalog
from .paths import default_project_path, validate_managed_project_path

CURRENT_PROJECT_SCHEMA_VERSION = 2
_PROJECT_MARKERS = frozenset(
    {
        ".git",
        ".repoprover",
        "Formalization",
        "RepoProverInput",
        "VERIFY.yaml",
        "lakefile.lean",
        "lean-toolchain",
    }
)


class ManagedProjectKind(StrEnum):
    AVAILABLE = "AVAILABLE"
    RESUMABLE = "RESUMABLE"
    MIGRATION_READY = "MIGRATION_READY"
    NEEDS_MAIN_FILE = "NEEDS_MAIN_FILE"
    INCOMPLETE = "INCOMPLETE"
    OCCUPIED = "OCCUPIED"


class ProjectConfigurationError(RuntimeError):
    pass


class MainFileSelectionRequired(ProjectConfigurationError):
    def __init__(
        self,
        *,
        project_path: Path,
        source_path: Path,
        candidates: tuple[tuple[str, bool], ...],
        suggested_main_file: str,
    ) -> None:
        self.project_path = project_path
        self.source_path = source_path
        self.candidates = candidates
        self.suggested_main_file = suggested_main_file
        choices = ", ".join(path for path, _is_root in candidates)
        super().__init__(
            "This legacy project's main-file choice is ambiguous and needs an "
            "explicit selection before it can resume. "
            f"Candidates: {choices}"
        )


@dataclass(frozen=True)
class ManagedProjectRecord:
    project_path: Path
    kind: ManagedProjectKind
    name: str
    issue: str | None = None
    source_path: Path | None = None
    candidates: tuple[tuple[str, bool], ...] = ()
    suggested_main_file: str | None = None
    config: dict[str, Any] | None = None


def _suggest_main_file(candidates: tuple[tuple[str, bool], ...]) -> str:
    document_roots = tuple(item for item in candidates if item[1])
    pool = document_roots or candidates
    preferred_names = {
        "main.tex": 0,
        "main.ltx": 1,
        "paper.tex": 2,
        "paper.ltx": 3,
        "manuscript.tex": 4,
        "manuscript.ltx": 5,
        "article.tex": 6,
        "article.ltx": 7,
    }
    return min(
        pool,
        key=lambda item: (
            preferred_names.get(Path(item[0]).name.casefold(), len(preferred_names)),
            len(Path(item[0]).parts),
            item[0].casefold(),
            item[0],
        ),
    )[0]


def _read_config(project: Path) -> dict[str, Any]:
    config_path = project / ".repoprover" / "config.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProjectConfigurationError(
            f"Project configuration is missing or unreadable: {config_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProjectConfigurationError(
            f"Project configuration is invalid JSON: {config_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectConfigurationError("Project configuration must be a JSON object")
    if payload.get("schema_version") not in {1, CURRENT_PROJECT_SCHEMA_VERSION}:
        raise ProjectConfigurationError(
            "Project configuration uses an unsupported schema version"
        )
    if not isinstance(payload.get("manuscript"), str):
        raise ProjectConfigurationError(
            "Project configuration does not identify its manuscript source"
        )
    return payload


def _persist_main_selection(
    project: Path, config: dict[str, Any], source: Path, main_file: str
) -> dict[str, Any]:
    try:
        closure = resolve_latex_closure(source, main_file)
    except LatexIndexError as exc:
        raise ProjectConfigurationError(str(exc)) from exc
    updated = dict(config)
    updated.update(
        {
            "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
            "main_file": closure[0],
            "input_files": list(closure[1:]),
            "package_version": __version__,
        }
    )
    database = project / ".repoprover" / "state.sqlite3"
    if database.is_file():
        with StateStore(database) as store:
            prior_main = store.get_metadata("main_file")
            if prior_main != closure[0]:
                store.set_metadata(
                    "pending_main_file_change",
                    json.dumps({"old": prior_main, "new": closure[0]}),
                )
            store.set_metadata("main_file", closure[0])
            store.set_metadata("input_files", json.dumps(closure[1:]))
    # Persist the authoritative config last. If an earlier database update or
    # this atomic replacement fails, the legacy config still classifies as
    # recoverable and the explicit selection can safely be retried.
    atomic_write_json(project / ".repoprover" / "config.json", updated)
    return updated


def load_or_migrate_project_config(project: Path) -> dict[str, Any]:
    """Load a config, auto-migrating only a provably unambiguous legacy root."""

    project = project.expanduser().resolve(strict=False)
    config = _read_config(project)
    source = Path(str(config["manuscript"])).expanduser().resolve(strict=False)
    if not source.is_dir():
        raise ProjectConfigurationError(
            f"Authoritative manuscript source is unavailable: {source}"
        )
    main_file = config.get("main_file")
    if not isinstance(main_file, str) or not main_file.strip():
        try:
            candidates = discover_latex_sources(source)
        except LatexIndexError as exc:
            raise ProjectConfigurationError(str(exc)) from exc
        document_roots = tuple(path for path, is_root in candidates if is_root)
        if len(candidates) == 1:
            main_file = candidates[0][0]
        elif len(document_roots) == 1:
            main_file = document_roots[0]
        else:
            raise MainFileSelectionRequired(
                project_path=project,
                source_path=source,
                candidates=candidates,
                suggested_main_file=_suggest_main_file(candidates),
            )
        return _persist_main_selection(project, config, source, main_file)
    if config.get("schema_version") != CURRENT_PROJECT_SCHEMA_VERSION:
        return _persist_main_selection(project, config, source, main_file)
    input_files = config.get("input_files")
    if not isinstance(input_files, list) or not all(
        isinstance(item, str) for item in input_files
    ):
        raise ProjectConfigurationError(
            "Project configuration `input_files` must be a list of relative paths"
        )
    return config


class ProjectManager:
    """Reconciles the catalog and enforces one non-destructive preflight."""

    def __init__(self, catalog: ProjectCatalog) -> None:
        self.catalog = catalog

    def resolve_destination(self, name: str, project_path: Path | None = None) -> Path:
        return validate_managed_project_path(
            project_path if project_path is not None else default_project_path(name)
        )

    def inspect(self, project_path: Path) -> ManagedProjectRecord:
        """Classify a path without modifying its contents."""

        project = validate_managed_project_path(project_path)
        if not project.exists():
            return ManagedProjectRecord(
                project, ManagedProjectKind.AVAILABLE, project.name
            )
        if not project.is_dir():
            return ManagedProjectRecord(
                project,
                ManagedProjectKind.OCCUPIED,
                project.name,
                "The destination exists and is not a directory; it was not modified",
            )
        try:
            first = next(project.iterdir())
        except StopIteration:
            return ManagedProjectRecord(
                project, ManagedProjectKind.AVAILABLE, project.name
            )
        del first
        config_path = project / ".repoprover" / "config.json"
        if not config_path.is_file():
            names = {item.name for item in project.iterdir()}
            recognized = bool(names & _PROJECT_MARKERS)
            return ManagedProjectRecord(
                project,
                (
                    ManagedProjectKind.INCOMPLETE
                    if recognized
                    else ManagedProjectKind.OCCUPIED
                ),
                project.name,
                (
                    "The directory resembles an incomplete Proof Assistant project "
                    "but has no configuration; it was not modified"
                    if recognized
                    else "The nonempty destination is not a Proof Assistant project; "
                    "it was not modified"
                ),
            )
        try:
            config = _read_config(project)
        except ProjectConfigurationError as exc:
            return ManagedProjectRecord(
                project,
                ManagedProjectKind.INCOMPLETE,
                project.name,
                str(exc),
            )
        name = str(config.get("name") or project.name)
        source = Path(str(config["manuscript"])).expanduser().resolve(strict=False)
        if not source.is_dir():
            return ManagedProjectRecord(
                project,
                ManagedProjectKind.INCOMPLETE,
                name,
                f"Authoritative manuscript source is unavailable: {source}",
                source_path=source,
                config=config,
            )
        main_file = config.get("main_file")
        if not isinstance(main_file, str) or not main_file.strip():
            try:
                candidates = discover_latex_sources(source)
            except LatexIndexError as exc:
                return ManagedProjectRecord(
                    project,
                    ManagedProjectKind.INCOMPLETE,
                    name,
                    str(exc),
                    source_path=source,
                    config=config,
                )
            document_roots = tuple(path for path, is_root in candidates if is_root)
            if len(candidates) == 1:
                suggestion = candidates[0][0]
            elif len(document_roots) == 1:
                suggestion = document_roots[0]
            else:
                suggestion = _suggest_main_file(candidates)
                return ManagedProjectRecord(
                    project,
                    ManagedProjectKind.NEEDS_MAIN_FILE,
                    name,
                    "This legacy project's main-file choice is ambiguous and "
                    "needs an explicit selection before it can resume",
                    source_path=source,
                    candidates=candidates,
                    suggested_main_file=suggestion,
                    config=config,
                )
            return ManagedProjectRecord(
                project,
                ManagedProjectKind.MIGRATION_READY,
                name,
                "Legacy project has an unambiguous main file and can be migrated",
                source_path=source,
                candidates=candidates,
                suggested_main_file=suggestion,
                config=config,
            )
        if config.get("schema_version") != CURRENT_PROJECT_SCHEMA_VERSION:
            return ManagedProjectRecord(
                project,
                ManagedProjectKind.MIGRATION_READY,
                name,
                "Legacy project configuration can be migrated safely",
                source_path=source,
                suggested_main_file=main_file,
                config=config,
            )
        input_files = config.get("input_files")
        if not isinstance(input_files, list) or not all(
            isinstance(item, str) for item in input_files
        ):
            return ManagedProjectRecord(
                project,
                ManagedProjectKind.INCOMPLETE,
                name,
                "Project configuration `input_files` must be a list of relative paths",
                source_path=source,
                config=config,
            )
        required = (project / ".repoprover" / "state.sqlite3",)
        missing = tuple(path.name for path in required if not path.is_file())
        if missing:
            return ManagedProjectRecord(
                project,
                ManagedProjectKind.INCOMPLETE,
                name,
                "Recognized project is incomplete; missing: " + ", ".join(missing),
                source_path=source,
                config=config,
            )
        return ManagedProjectRecord(
            project,
            ManagedProjectKind.RESUMABLE,
            name,
            source_path=source,
            config=config,
        )

    def entries(self) -> tuple[ManagedProjectRecord, ...]:
        records: list[ManagedProjectRecord] = []
        for path in self.catalog.candidate_paths():
            record = self.inspect(path)
            if record.kind == ManagedProjectKind.MIGRATION_READY:
                try:
                    load_or_migrate_project_config(path)
                    record = self.inspect(path)
                except ProjectConfigurationError as exc:
                    record = ManagedProjectRecord(
                        path,
                        ManagedProjectKind.INCOMPLETE,
                        record.name,
                        str(exc),
                        source_path=record.source_path,
                    )
            records.append(record)
        return tuple(
            item for item in records if item.kind != ManagedProjectKind.AVAILABLE
        )

    def remember_occupied(self, record: ManagedProjectRecord) -> None:
        if record.kind != ManagedProjectKind.AVAILABLE:
            self.catalog.remember_path(record.project_path)

    def select_main_file(self, project_path: Path, main_file: str) -> None:
        project = validate_managed_project_path(project_path)
        record = self.inspect(project)
        if record.kind != ManagedProjectKind.NEEDS_MAIN_FILE:
            raise ProjectConfigurationError(
                "Explicit main-file recovery is only valid for a project that "
                f"needs selection; current state is {record.kind}"
            )
        config = _read_config(project)
        assert record.source_path is not None
        candidate_paths = {path for path, _is_root in record.candidates}
        selected = Path(str(main_file)).as_posix()
        if selected not in candidate_paths:
            choices = ", ".join(sorted(candidate_paths))
            raise ProjectConfigurationError(
                f"Selected main file is not a candidate: {selected!r}. "
                f"Choose one of: {choices}"
            )
        _persist_main_selection(project, config, record.source_path, selected)
        self.catalog.upsert(project)
