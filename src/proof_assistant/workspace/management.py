"""Project-management backend: occupancy, reconciliation, and legacy recovery.

This module is deliberately UI-neutral.  It is the sole owner of deciding
whether a managed path is creatable, resumable, recoverable, incomplete, or
merely occupied.  Callers must never delete an occupied path to make it fit.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
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
from ..incremental.locking import (
    ProjectLockedError,
    acquire_worker_lease,
    project_lock,
    release_worker_lease,
)
from ..incremental.store import StateStore
from .catalog import ProjectCatalog
from .paths import (
    ManagedProjectPathError,
    default_project_path,
    is_in_dropbox,
    validate_managed_project_path,
)

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


class ManagedDeletionKind(StrEnum):
    READY = "READY"
    BUSY = "BUSY"
    REFUSED = "REFUSED"


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


@dataclass(frozen=True)
class ManagedDeletionInspection:
    project_path: Path
    source_path: Path | None
    kind: ManagedDeletionKind
    issue: str | None = None


@dataclass(frozen=True)
class ManagedDeletionResult:
    project_path: Path
    source_path: Path
    trash_path: Path
    deleted_at: str


class ManagedProjectDeletionError(ProjectConfigurationError):
    def __init__(self, inspection: ManagedDeletionInspection) -> None:
        self.inspection = inspection
        super().__init__(inspection.issue or "Managed project deletion was refused")


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

    def __init__(
        self, catalog: ProjectCatalog, *, trash_root: Path | None = None
    ) -> None:
        self.catalog = catalog
        self.trash_root = (
            trash_root.expanduser().resolve(strict=False)
            if trash_root is not None
            else self._default_trash_root()
        )

    @staticmethod
    def _default_trash_root() -> Path:
        """Return the platform's recoverable project-deletion area.

        macOS has a native per-user Trash directory. On other platforms we
        deliberately use a Proof Assistant-owned safe-home Trash instead of
        pretending that a bare move into freedesktop ``Trash/files`` is a
        desktop-trash operation: a conforming freedesktop deletion also needs
        a paired ``.trashinfo`` record. The safe-home area keeps the complete
        project indefinitely at the exact path returned by ``delete_project``
        and can therefore be restored with a plain atomic rename.
        """

        if sys.platform == "darwin":
            return (Path.home() / ".Trash").resolve(strict=False)
        # This is intentionally not freedesktop Trash/files. See the docstring:
        # without Trash/info metadata that location would not be recoverable by
        # a conforming desktop Trash implementation.
        xdg_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        return (
            (xdg_data / "proof-assistant" / "recoverable-trash")
            .expanduser()
            .resolve(strict=False)
        )

    @staticmethod
    def _existing_parent(path: Path) -> Path:
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate

    def _deletion_inspection(
        self, project_path: Path, *, check_lock: bool
    ) -> ManagedDeletionInspection:
        unresolved = Path(project_path).expanduser().resolve(strict=False)
        try:
            project = validate_managed_project_path(unresolved)
        except ManagedProjectPathError as exc:
            return ManagedDeletionInspection(
                unresolved, None, ManagedDeletionKind.REFUSED, str(exc)
            )
        record = self.inspect(project)
        if record.kind != ManagedProjectKind.RESUMABLE or record.source_path is None:
            return ManagedDeletionInspection(
                project,
                record.source_path,
                ManagedDeletionKind.REFUSED,
                "Only a validated, resumable Proof Assistant project can be "
                "moved to recoverable deletion storage; current classification "
                f"is {record.kind}",
            )
        source = record.source_path.resolve(strict=False)
        if (
            source == project
            or source.is_relative_to(project)
            or project.is_relative_to(source)
        ):
            return ManagedDeletionInspection(
                project,
                source,
                ManagedDeletionKind.REFUSED,
                "The manuscript source and managed project overlap; deletion "
                "was refused so the authoritative source cannot be moved",
            )
        trash = self.trash_root
        if is_in_dropbox(trash):
            return ManagedDeletionInspection(
                project,
                source,
                ManagedDeletionKind.REFUSED,
                f"The recoverable deletion location is inside Dropbox: {trash}",
            )
        if (
            trash == source
            or trash.is_relative_to(source)
            or source.is_relative_to(trash)
        ):
            return ManagedDeletionInspection(
                project,
                source,
                ManagedDeletionKind.REFUSED,
                "The manuscript source and recoverable deletion area overlap; "
                "deletion was refused so the authoritative source remains "
                "completely untouched",
            )
        if (
            trash == project
            or trash.is_relative_to(project)
            or project.is_relative_to(trash)
        ):
            return ManagedDeletionInspection(
                project,
                source,
                ManagedDeletionKind.REFUSED,
                "The managed project and recoverable deletion location overlap",
            )
        trash_parent = self._existing_parent(trash)
        if not trash_parent.is_dir():
            return ManagedDeletionInspection(
                project,
                source,
                ManagedDeletionKind.REFUSED,
                f"Recovery-location parent is not a directory: {trash_parent}",
            )
        try:
            same_device = project.stat().st_dev == trash_parent.stat().st_dev
        except OSError as exc:
            return ManagedDeletionInspection(
                project,
                source,
                ManagedDeletionKind.REFUSED,
                f"Could not inspect project/recovery-location filesystems: {exc}",
            )
        if not same_device:
            return ManagedDeletionInspection(
                project,
                source,
                ManagedDeletionKind.REFUSED,
                "The project and recoverable deletion location are on different "
                "filesystems; an atomic recoverable move is not possible",
            )
        if check_lock:
            try:
                worker_lease = acquire_worker_lease(project)
            except ProjectLockedError as exc:
                return ManagedDeletionInspection(
                    project, source, ManagedDeletionKind.BUSY, str(exc)
                )
            except OSError as exc:
                return ManagedDeletionInspection(
                    project,
                    source,
                    ManagedDeletionKind.REFUSED,
                    f"Could not inspect the project lock: {exc}",
                )
            try:
                try:
                    with project_lock(project, exclusive=True):
                        pass
                except ProjectLockedError as exc:
                    return ManagedDeletionInspection(
                        project, source, ManagedDeletionKind.BUSY, str(exc)
                    )
                except OSError as exc:
                    return ManagedDeletionInspection(
                        project,
                        source,
                        ManagedDeletionKind.REFUSED,
                        f"Could not inspect the project lock: {exc}",
                    )
            finally:
                release_worker_lease(worker_lease)
        return ManagedDeletionInspection(project, source, ManagedDeletionKind.READY)

    def inspect_deletion(self, project_path: Path) -> ManagedDeletionInspection:
        """Classify deletion without trusting catalog identity or stale UI state."""

        return self._deletion_inspection(project_path, check_lock=True)

    def _reserve_trash_destination(self, project: Path) -> tuple[Path, Path]:
        """Atomically reserve a unique recovery container and nonexisting child."""

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        while True:
            token = uuid.uuid4().hex[:12]
            reservation = (
                self.trash_root
                / f"{project.name}-{timestamp}-{token}.proof-assistant-trash"
            )
            try:
                reservation.mkdir()
            except FileExistsError:
                continue
            return reservation, reservation / project.name

    def delete_project(self, project_path: Path) -> ManagedDeletionResult:
        """Atomically move one validated project to recovery storage, then forget it."""

        initial = self.inspect_deletion(project_path)
        if initial.kind != ManagedDeletionKind.READY:
            raise ManagedProjectDeletionError(initial)
        project = initial.project_path
        try:
            worker_lease = acquire_worker_lease(project)
        except ProjectLockedError as exc:
            raise ManagedProjectDeletionError(
                ManagedDeletionInspection(
                    project,
                    initial.source_path,
                    ManagedDeletionKind.BUSY,
                    str(exc),
                )
            ) from exc
        session_entered = False
        try:
            try:
                lock_context = project_lock(project, exclusive=True)
                lock_context.__enter__()
                session_entered = True
            except ProjectLockedError as exc:
                raise ManagedProjectDeletionError(
                    ManagedDeletionInspection(
                        project,
                        initial.source_path,
                        ManagedDeletionKind.BUSY,
                        str(exc),
                    )
                ) from exc
            current = self._deletion_inspection(project, check_lock=False)
            if current.kind != ManagedDeletionKind.READY or current.source_path is None:
                raise ManagedProjectDeletionError(current)
            try:
                self.trash_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ManagedProjectDeletionError(
                    ManagedDeletionInspection(
                        project,
                        current.source_path,
                        ManagedDeletionKind.REFUSED,
                        f"Could not create the recoverable deletion directory: {exc}",
                    )
                ) from exc
            # Recheck the actual directory after creation. This closes the gap
            # between a preflight against its parent and the atomic rename.
            if project.stat().st_dev != self.trash_root.stat().st_dev:
                raise ManagedProjectDeletionError(
                    ManagedDeletionInspection(
                        project,
                        current.source_path,
                        ManagedDeletionKind.REFUSED,
                        "The project and recoverable deletion location are on "
                        "different filesystems",
                    )
                )
            try:
                reservation, destination = self._reserve_trash_destination(project)
            except OSError as exc:
                raise ManagedProjectDeletionError(
                    ManagedDeletionInspection(
                        project,
                        current.source_path,
                        ManagedDeletionKind.REFUSED,
                        f"Could not reserve a collision-safe recovery path: {exc}",
                    )
                ) from exc
            try:
                os.rename(project, destination)
            except OSError as exc:
                try:
                    reservation.rmdir()
                except OSError:
                    pass
                raise ManagedProjectDeletionError(
                    ManagedDeletionInspection(
                        project,
                        current.source_path,
                        ManagedDeletionKind.REFUSED,
                        f"Atomic move to recovery storage failed: {exc}",
                    )
                ) from exc
            try:
                self.catalog.forget_path(project)
            except Exception as exc:
                try:
                    if project.exists():
                        raise OSError(
                            "original project path became occupied before rollback"
                        )
                    os.rename(destination, project)
                except OSError as rollback_exc:
                    raise ManagedProjectDeletionError(
                        ManagedDeletionInspection(
                            project,
                            current.source_path,
                            ManagedDeletionKind.REFUSED,
                            "The project reached recovery storage but catalog "
                            "reconciliation and automatic rollback both failed. "
                            f"Recover it manually from {destination}: catalog error={exc}; "
                            f"rollback={rollback_exc}",
                        )
                    ) from rollback_exc
                # The rename above is the authoritative rollback. Finder,
                # indexers, or filesystem metadata may make removal of the now
                # empty reservation fail; that does not make the restored
                # project disappear and must not produce a false manual-
                # recovery instruction naming a path that no longer exists.
                try:
                    reservation.rmdir()
                except OSError:
                    pass
                raise ManagedProjectDeletionError(
                    ManagedDeletionInspection(
                        project,
                        current.source_path,
                        ManagedDeletionKind.REFUSED,
                        "Catalog reconciliation failed; the project was restored "
                        f"to {project}: {exc}",
                    )
                ) from exc
            return ManagedDeletionResult(
                project,
                current.source_path,
                destination,
                datetime.now(UTC).isoformat(),
            )
        finally:
            if session_entered:
                lock_context.__exit__(None, None, None)
            release_worker_lease(worker_lease)

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
