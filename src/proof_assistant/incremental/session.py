from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..manuscript import (
    RAW_LAKEFILE,
    RAW_LEAN_TOOLCHAIN,
    ManuscriptInputError,
)
from ..workspace.management import (
    CURRENT_PROJECT_SCHEMA_VERSION,
    ProjectConfigurationError,
    load_or_migrate_project_config,
)
from .graph import (
    affected_claims,
    build_graph,
    canonical_cycles,
    conjectural_dependency_blockers,
    dependency_closure,
    manuscript_graph_export,
    source_changes,
)
from .io import atomic_write_json, atomic_write_text, canonical_hash
from .latex import (
    LatexIndexError,
    explicit_reference_graph,
    index_manuscript,
    resolve_latex_closure,
)
from .lean import install_dependency_extractor
from .locking import ProjectLockedError, project_lock
from .models import (
    ClaimState,
    ManuscriptEdge,
    Snapshot,
    SourceObject,
    TaskSpec,
    is_conjectural_assertion,
    proof_target_ids,
)
from .snapshot import SnapshotRepository, sync_project_manuscript
from .store import StateStore
from .task import parse_task_file, parse_task_text, task_document

PROJECT_CONFIG_VERSION = CURRENT_PROJECT_SCHEMA_VERSION
PROJECT_GITIGNORE = """\
/.lake/
/.repoprover/
/.repoprover-agent/
*.ilean
*.olean
*.trace
.DS_Store
"""
ROOT_MODULE = """\
import Formalization.All
"""
FOUNDATION_MODULE = """\
import Mathlib

namespace ManuscriptVerification

/- Shared definitions and assumptions may be placed here deliberately. -/

end ManuscriptVerification
"""


class IncrementalProjectError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedPass:
    run_id: int
    snapshot: Snapshot
    task: TaskSpec
    task_sha256: str
    objects: tuple[SourceObject, ...]
    edges: tuple[ManuscriptEdge, ...]
    targets: frozenset[str]
    selected: frozenset[str]
    skipped_unproved: frozenset[str]
    directly_changed: frozenset[str]
    proof_only_changed: frozenset[str]
    affected: frozenset[str]
    deleted: frozenset[str]
    unresolved_references: tuple[tuple[str, str], ...]
    cycles: tuple[tuple[str, ...], ...]
    source_diff: str
    baseline_commit: str
    main_file: str
    input_files: tuple[str, ...]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _source_object_from_version(row: sqlite3.Row) -> SourceObject:
    payload = dict(row)
    references = json.loads(str(payload["references_json"]))
    if not isinstance(references, list) or not all(
        isinstance(item, str) for item in references
    ):
        raise IncrementalProjectError("Persisted claim references are malformed")
    return SourceObject(
        claim_id=str(payload["claim_id"]),
        kind=str(payload["kind"]),
        source_file=str(payload["source_file"]),
        environment=str(payload["environment"]),
        label=(str(payload["label"]) if payload["label"] is not None else None),
        ordinal=int(payload["ordinal"]),
        statement_start=int(payload["statement_start"]),
        statement_end=int(payload["statement_end"]),
        statement_byte_start=int(payload["statement_byte_start"]),
        statement_byte_end=int(payload["statement_byte_end"]),
        proof_start=(
            int(payload["proof_start"])
            if payload["proof_start"] is not None
            else None
        ),
        proof_end=(
            int(payload["proof_end"]) if payload["proof_end"] is not None else None
        ),
        proof_byte_start=(
            int(payload["proof_byte_start"])
            if payload["proof_byte_start"] is not None
            else None
        ),
        proof_byte_end=(
            int(payload["proof_byte_end"])
            if payload["proof_byte_end"] is not None
            else None
        ),
        statement_hash=str(payload["statement_hash"]),
        proof_hash=str(payload["proof_hash"]),
        normalized_statement_hash=str(payload["normalized_statement_hash"]),
        statement_text=str(payload["statement_text"]),
        proof_text=str(payload["proof_text"]),
        references=tuple(references),
    )


def _reconcile_conjectural_policy(
    store: StateStore,
    *,
    snapshot: str,
    objects: tuple[SourceObject, ...],
    edges: tuple[ManuscriptEdge, ...],
    selected: set[str],
    run_id: int,
) -> frozenset[str]:
    """Apply the non-negotiable host policy for unsupported assertions."""

    skipped = frozenset(
        item.claim_id for item in objects if is_conjectural_assertion(item)
    )
    blockers = conjectural_dependency_blockers(
        objects, selected=selected, edges=edges
    )
    open_by_claim: dict[str, list[sqlite3.Row]] = {}
    for question in store.open_questions():
        open_by_claim.setdefault(str(question["claim_id"]), []).append(question)

    by_id = {item.claim_id: item for item in objects}
    for claim_id in sorted(skipped):
        blocked_claims = blockers.get(claim_id, ())
        if blocked_claims:
            item = by_id[claim_id]
            category = "conjectural_dependency"
            passage = item.statement_text.strip()
            problem = (
                "This conjectural or unproved assertion is not a proof obligation, "
                "but proof-bearing manuscript statements rely on it: "
                + ", ".join(blocked_claims)
            )
            possible_resolutions = (
                (
                    "Reclassify this conjecture as a theorem-like assertion and "
                    "attach its manuscript proof."
                    if item.kind == "conjecture"
                    else "Add a manuscript proof for this assertion."
                ),
                "Remove the dependency from the proof-bearing statements.",
            )
            question_id = store.create_question(
                claim_id=claim_id,
                snapshot=snapshot,
                category=category,
                passage=passage,
                problem=problem,
                possible_resolutions=possible_resolutions,
                blocking_claims=blocked_claims,
                run_id=run_id,
            )
            store.refresh_open_question(
                claim_id,
                snapshot=snapshot,
                category=category,
                passage=passage,
                problem=problem,
                possible_resolutions=possible_resolutions,
                blocking_claims=blocked_claims,
            )
            store.set_claim_state(
                claim_id,
                ClaimState.NEEDS_CLARIFICATION,
                run_id=run_id,
                action="conjectural_dependency",
                reason=(
                    f"{question_id}: unsupported assertion blocks "
                    + ", ".join(blocked_claims)
                ),
            )
            continue

        for question in open_by_claim.get(claim_id, []):
            store.resolve_question(
                str(question["question_id"]),
                run_id=run_id,
                status="SUPERSEDED",
                resolution=(
                    "Conjectural and unproved assertions are skipped unless a "
                    "proof-bearing manuscript statement depends on them"
                ),
            )
        store.set_claim_state(
            claim_id,
            ClaimState.SKIPPED_UNPROVED,
            run_id=run_id,
            action="skip_unproved",
            reason=(
                "Host policy skips conjectures and theorem-like assertions "
                "without an attached manuscript proof"
            ),
        )
    return skipped


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _claim_module_name(claim_id: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "_", claim_id).strip("_") or "claim"
    if stem[0].isdigit():
        stem = "claim_" + stem
    suffix = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()[:8]
    return f"Claim_{stem[:60]}_{suffix}"


def claim_module_path(claim_id: str) -> Path:
    return Path("Formalization/Claims") / f"{_claim_module_name(claim_id)}.lean"


class IncrementalSession:
    """Persistent deterministic control plane for manuscript verification."""

    def __init__(self, project: Path) -> None:
        self.project = project.expanduser().resolve()
        self.state_root = self.project / ".repoprover"
        self.config_path = self.state_root / "config.json"
        self.database_path = self.state_root / "state.sqlite3"
        self.exports = self.state_root / "exports"
        self.runs = self.state_root / "runs"
        self.snapshots = SnapshotRepository(self.project)

    @classmethod
    def initialize(
        cls,
        *,
        manuscript: str | Path,
        task_file: str | Path | None = None,
        task_text: str | None = None,
        project: str | Path,
        project_name: str | None = None,
        project_id: str | None = None,
        source_in_dropbox: bool = False,
        main_file: str,
    ) -> IncrementalSession:
        source = Path(manuscript).expanduser().resolve()
        destination = Path(project).expanduser().resolve()
        if not source.is_dir():
            raise ManuscriptInputError(f"Manuscript directory does not exist: {source}")
        if _is_within(destination, source) or _is_within(source, destination):
            raise ManuscriptInputError(
                "Manuscript and persistent project directories must not contain one another"
            )
        try:
            source_files = resolve_latex_closure(source, main_file)
        except LatexIndexError as exc:
            raise ManuscriptInputError(str(exc)) from exc
        normalized_main = source_files[0]
        input_files = source_files[1:]
        if destination.exists():
            if not destination.is_dir() or any(destination.iterdir()):
                raise IncrementalProjectError(
                    f"Project directory must be new or empty: {destination}"
                )
            destination.rmdir()
        if task_file is not None and task_text is not None:
            raise ManuscriptInputError("Specify task_file or task_text, not both")
        if task_file is not None:
            _source_task_path, initial_task_text, task_sha256, task = parse_task_file(
                task_file
            )
        else:
            initial_task_text = task_document(task_text)
            task_sha256, task = parse_task_text(initial_task_text)
        configured_task_path = "VERIFY.yaml"
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.initializing-", dir=destination.parent
            )
        )
        session = cls(staging)
        try:
            session._write_scaffold()
            atomic_write_text(staging / "VERIFY.yaml", initial_task_text)
            config = {
                "schema_version": PROJECT_CONFIG_VERSION,
                "created_at": utc_now(),
                "manuscript": str(source),
                "main_file": normalized_main,
                "input_files": list(input_files),
                "task_file": configured_task_path,
                "name": project_name or destination.name,
                "project_id": project_id
                or hashlib.sha256(str(destination).encode("utf-8")).hexdigest()[:16],
                "source_in_dropbox": source_in_dropbox,
                "package_version": __version__,
            }
            atomic_write_json(session.config_path, config)
            atomic_write_text(
                staging / "RepoProverInput" / "TASK.md", initial_task_text
            )
            with StateStore(session.database_path) as store:
                store.set_metadata("manuscript", str(source))
                store.set_metadata("main_file", normalized_main)
                store.set_metadata("input_files", json.dumps(input_files))
                store.set_metadata("task_file", configured_task_path)
                store.set_metadata("task_sha256", task_sha256)
                store.set_metadata(
                    "task_spec", json.dumps(task.to_dict(), sort_keys=True)
                )
                run_id = store.begin_run(
                    command="manuscript init",
                    started_at=utc_now(),
                    task_sha256=task_sha256,
                    mode=task.mode,
                )
                snapshot = session.snapshots.create(source, run_id=run_id)
                store.record_snapshot(
                    snapshot,
                    source_root=source,
                    task_sha256=task_sha256,
                    created_at=utc_now(),
                )
                store.update_run_snapshot(
                    run_id,
                    snapshot_commit=snapshot.commit,
                    previous_snapshot_commit=snapshot.previous_commit,
                )
                manuscript_copy = sync_project_manuscript(
                    session.snapshots, snapshot.commit, staging
                )
                objects = index_manuscript(
                    manuscript_copy, store, main_file=normalized_main
                )
                edges, unresolved = explicit_reference_graph(objects)
                cycles = canonical_cycles(
                    build_graph((item.claim_id for item in objects), edges)
                )
                store.replace_current_claims(
                    snapshot.commit,
                    objects,
                    run_id=run_id,
                    state_updates={
                        item.claim_id: (
                            ClaimState.SKIPPED_UNPROVED
                            if is_conjectural_assertion(item)
                            else ClaimState.DISCOVERED
                        )
                        for item in objects
                    },
                )
                store.replace_manuscript_edges(snapshot.commit, edges)
                for src, reference in unresolved:
                    store.add_diagnostic(
                        run_id=run_id,
                        claim_id=src,
                        category="unresolved_reference",
                        message=f"LaTeX reference does not identify an indexed object: {reference}",
                    )
                for cycle in cycles:
                    store.add_diagnostic(
                        run_id=run_id,
                        category="dependency_cycle",
                        message="Dependency cycle: " + " -> ".join(cycle),
                        details={"claims": cycle},
                    )
                session._ensure_claim_modules(objects)
                session._export_state(store, objects, edges, snapshot)
                store.finish_run(
                    run_id,
                    status="COMPLETE",
                    outcome="initialized",
                    completed_at=utc_now(),
                    detail=f"Indexed {len(objects)} manuscript objects",
                )
            session._write_status_files()
            session._initialize_project_git()
            staging.rename(destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return cls(destination)

    def _write_scaffold(self) -> None:
        self.project.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.exports.mkdir(parents=True, exist_ok=True)
        self.runs.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.project / "lean-toolchain", RAW_LEAN_TOOLCHAIN)
        atomic_write_text(self.project / "lakefile.lean", RAW_LAKEFILE)
        atomic_write_text(self.project / "Manuscript.lean", ROOT_MODULE)
        atomic_write_text(self.project / ".gitignore", PROJECT_GITIGNORE)
        atomic_write_text(
            self.project / "Formalization" / "Foundation.lean", FOUNDATION_MODULE
        )
        install_dependency_extractor(self.project)

    def _initialize_project_git(self) -> None:
        result = subprocess.run(
            ["git", "-C", str(self.project), "init", "-q", "-b", "main"],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise IncrementalProjectError((result.stderr or result.stdout).strip())
        self._git(["config", "user.name", "Proof Assistant"])
        self._git(["config", "user.email", "proof-assistant@localhost"])
        self._git(["add", "--all"])
        self._git(
            ["commit", "-q", "-m", "Initialize incremental manuscript verification"]
        )

    def _git(self, arguments: Sequence[str], *, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.project), *arguments],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise IncrementalProjectError(
                f"Project Git command failed ({' '.join(arguments)}): {detail or result.returncode}"
            )
        return result.stdout.strip()

    def _load_config(self) -> dict[str, Any]:
        try:
            return load_or_migrate_project_config(self.project)
        except ProjectConfigurationError as exc:
            raise IncrementalProjectError(str(exc)) from exc

    def _task_path_from_config(self, config: dict[str, Any]) -> Path:
        value = Path(str(config["task_file"])).expanduser()
        return (
            (self.project / value).resolve()
            if not value.is_absolute()
            else value.resolve()
        )

    def _ensure_claim_modules(self, objects: Sequence[SourceObject]) -> None:
        claim_directory = self.project / "Formalization" / "Claims"
        claim_directory.mkdir(parents=True, exist_ok=True)
        imports = ["import Formalization.Foundation"]
        for item in sorted(objects, key=lambda value: value.claim_id):
            relative = claim_module_path(item.claim_id)
            module = relative.with_suffix("").as_posix().replace("/", ".")
            imports.append(f"import {module}")
            path = self.project / relative
            if not path.exists():
                atomic_write_text(
                    path,
                    "import Formalization.Foundation\n\n"
                    "namespace ManuscriptVerification\n\n"
                    f"/- Formal counterpart for manuscript object `{item.claim_id}`. -/\n\n"
                    "end ManuscriptVerification\n",
                )
        atomic_write_text(
            self.project / "Formalization" / "All.lean", "\n".join(imports) + "\n"
        )

    def prepare_pass(
        self,
        *,
        manuscript: str | Path | None = None,
        task_file: str | Path | None = None,
        environment_hash: str | None = None,
        expected_inventory_sha256: str | None = None,
        event_hook: Callable[[str, str, dict[str, object]], None] | None = None,
        _already_locked: bool = False,
    ) -> PreparedPass:
        def notify(phase: str, message: str, **details: object) -> None:
            if event_hook is not None:
                event_hook(phase, message, details)

        config = self._load_config()
        source = Path(manuscript or config["manuscript"]).expanduser().resolve()
        main_file = str(config["main_file"])
        try:
            source_files = resolve_latex_closure(source, main_file)
        except LatexIndexError as exc:
            raise ManuscriptInputError(str(exc)) from exc
        input_files = source_files[1:]
        task_path_value = task_file or self._task_path_from_config(config)
        task_path, task_text, task_sha256, task = parse_task_file(task_path_value)
        if not source.is_dir():
            raise ManuscriptInputError(f"Manuscript directory does not exist: {source}")
        lock_context = (
            nullcontext()
            if _already_locked
            else project_lock(self.project, exclusive=True)
        )
        with lock_context:
            with StateStore(self.database_path) as store:
                store.recover_interrupted_runs(utc_now())
                previous_snapshot = store.previous_snapshot()
                run_id = store.begin_run(
                    command="manuscript verify",
                    started_at=utc_now(),
                    previous_snapshot_commit=previous_snapshot,
                    task_sha256=task_sha256,
                    mode=task.mode,
                    environment_hash=environment_hash,
                )
                try:
                    notify(
                        "OBSERVING_SOURCE",
                        "Reading a stable inventory of the authoritative source",
                        source_path=str(source),
                        main_file=main_file,
                        input_files=input_files,
                    )
                    snapshot = self.snapshots.create(
                        source,
                        run_id=run_id,
                        expected_inventory_sha256=expected_inventory_sha256,
                    )
                    store.record_snapshot(
                        snapshot,
                        source_root=source,
                        task_sha256=task_sha256,
                        created_at=utc_now(),
                    )
                    store.update_run_snapshot(
                        run_id,
                        snapshot_commit=snapshot.commit,
                        previous_snapshot_commit=previous_snapshot,
                    )
                    previous_versions = {
                        str(row["claim_id"]): row
                        for row in (
                            store.claim_versions(previous_snapshot)
                            if previous_snapshot is not None
                            else []
                        )
                    }
                    old_edges = tuple(
                        ManuscriptEdge(
                            str(row["src"]),
                            str(row["dst"]),
                            str(row["edge_kind"]),
                            str(row["provenance"]),
                            bool(row["approved"]),
                        )
                        for row in store.manuscript_edges()
                    )
                    notify(
                        "IMPORTING_SOURCE",
                        "Importing the reviewed source snapshot into the managed project",
                        snapshot=snapshot.commit,
                        main_file=main_file,
                        input_files=input_files,
                    )
                    manuscript_copy = sync_project_manuscript(
                        self.snapshots, snapshot.commit, self.project
                    )
                    notify(
                        "IMPORTING_SOURCE",
                        "Imported the reviewed source snapshot into the managed project",
                        snapshot=snapshot.commit,
                        main_file=main_file,
                        input_files=input_files,
                    )
                    atomic_write_text(
                        self.project / "RepoProverInput" / "TASK.md", task_text
                    )
                    notify(
                        "INDEXING",
                        "Indexing the selected main file and recursive input closure",
                        main_file=main_file,
                        input_files=input_files,
                        files=1 + len(input_files),
                    )
                    objects = index_manuscript(
                        manuscript_copy, store, main_file=main_file
                    )
                    notify(
                        "INDEXING",
                        "Indexed mathematical statements from the selected source",
                        objects=len(objects),
                        files=1 + len(input_files),
                    )
                    explicit_edges, unresolved = explicit_reference_graph(objects)
                    current_ids = {item.claim_id for item in objects}
                    current_by_id = {item.claim_id: item for item in objects}
                    rediscover_semantic_sources = {
                        claim_id
                        for claim_id, item in current_by_id.items()
                        if (old := previous_versions.get(claim_id)) is not None
                        and (
                            old["normalized_statement_hash"]
                            != item.normalized_statement_hash
                            or old["proof_hash"] != item.proof_hash
                        )
                    }
                    invalidated_semantic_sources = {
                        edge.src
                        for edge in old_edges
                        if edge.provenance == "formalization_discovered"
                        and edge.src in rediscover_semantic_sources
                    }
                    persistent_edges = tuple(
                        edge
                        for edge in old_edges
                        if edge.kind != "explicit_ref"
                        and edge.src in current_ids
                        and edge.dst in current_ids
                        and not (
                            edge.provenance == "formalization_discovered"
                            and edge.src in rediscover_semantic_sources
                        )
                    )
                    edge_map = {
                        (edge.src, edge.dst, edge.kind): edge
                        for edge in (*explicit_edges, *persistent_edges)
                    }
                    edges = tuple(edge_map[key] for key in sorted(edge_map))
                    cycles = canonical_cycles(build_graph(current_ids, edges))
                    statement_changed, proof_changed, deleted = source_changes(
                        previous_versions, objects, mode=task.mode
                    )
                    # Agent-discovered semantic edges describe the current
                    # manuscript argument. Any edit to their source assertion
                    # retires those edges and schedules the source for fresh
                    # dependency discovery, including in theorem mode.
                    proof_changed.update(invalidated_semantic_sources)
                    union_ids = current_ids | set(previous_versions)
                    union_edge_map = {
                        (edge.src, edge.dst, edge.kind): edge
                        for edge in (*old_edges, *edges)
                    }
                    affected = affected_claims(
                        statement_changed | proof_changed | deleted,
                        claim_ids=union_ids,
                        edges=union_edge_map.values(),
                    )
                    state_updates: dict[str, ClaimState] = {}
                    for claim_id in affected & current_ids:
                        if claim_id in statement_changed or claim_id in proof_changed:
                            state_updates[claim_id] = ClaimState.DIRTY_SOURCE
                        else:
                            state_updates[claim_id] = ClaimState.INVALIDATED
                    store.replace_current_claims(
                        snapshot.commit,
                        objects,
                        run_id=run_id,
                        state_updates=state_updates,
                    )
                    store.replace_manuscript_edges(snapshot.commit, edges)
                    for question in store.open_questions():
                        claim_id = str(question["claim_id"])
                        if (
                            claim_id in statement_changed
                            or claim_id in proof_changed
                            or claim_id in deleted
                        ):
                            store.resolve_question(
                                str(question["question_id"]),
                                run_id=run_id,
                                status="SUPERSEDED",
                                resolution="A later manuscript snapshot changed the associated source object",
                            )
                    for src, reference in unresolved:
                        store.add_diagnostic(
                            run_id=run_id,
                            claim_id=src,
                            category="unresolved_reference",
                            message=f"LaTeX reference does not identify an indexed object: {reference}",
                        )
                    for cycle in cycles:
                        store.add_diagnostic(
                            run_id=run_id,
                            category="dependency_cycle",
                            message="Dependency cycle: " + " -> ".join(cycle),
                            details={"claims": cycle},
                        )
                    if task.targets:
                        unknown_targets = sorted(set(task.targets) - current_ids)
                        if unknown_targets:
                            raise ManuscriptInputError(
                                "Task targets are not indexed manuscript IDs: "
                                + ", ".join(unknown_targets)
                            )
                    targets = proof_target_ids(task, objects)
                    selected = dependency_closure(
                        targets,
                        claim_ids=current_ids,
                        edges=edges,
                    )
                    if not selected:
                        selected = set(targets)
                    skipped_unproved = _reconcile_conjectural_policy(
                        store,
                        snapshot=snapshot.commit,
                        objects=objects,
                        edges=edges,
                        selected=selected,
                        run_id=run_id,
                    )
                    store.record_run_scope(
                        run_id,
                        targets=tuple(targets),
                        selected=tuple(selected),
                    )
                    store.replace_run_dependency_edges(run_id, edges)
                    for cycle in cycles:
                        if not set(cycle).issubset(selected):
                            continue
                        store.add_failure_incident(
                            run_id=run_id,
                            scope="COMPONENT",
                            failure_kind="DEPENDENCY_CYCLE",
                            phase="INDEXING",
                            category="dependency_cycle",
                            message="Dependency cycle: " + " -> ".join(cycle),
                            detail=(
                                "The dependency scheduler cannot establish a "
                                "dependency-first proof frontier for this component"
                            ),
                            provenance="session.explicit_reference_graph",
                            claim_ids=cycle,
                            retryable=False,
                        )
                    notify(
                        "IMPACT_ANALYSIS",
                        "Computed direct changes, descendants, and proof targets",
                        directly_changed=len(statement_changed | proof_changed),
                        deleted=len(deleted),
                        affected=len(affected),
                        selected=len(selected),
                    )
                    if not task.policy.preserve_certified:
                        for claim_id in sorted(selected):
                            if store.certificate(claim_id) is None:
                                continue
                            store.discard_certificate_for_reproof(
                                claim_id, run_id=run_id
                            )
                            store.set_claim_state(
                                claim_id,
                                ClaimState.INVALIDATED,
                                run_id=run_id,
                                action="policy_reproof",
                                reason=(
                                    "Task policy preserve_certified=false requires "
                                    "fresh proof validation"
                                ),
                            )
                    source_diff = self.snapshots.diff(
                        previous_snapshot, snapshot.commit
                    )
                    run_directory = self.runs / f"{run_id:06d}"
                    atomic_write_text(run_directory / "source-diff.patch", source_diff)
                    atomic_write_json(
                        run_directory / "affected-claims.json",
                        {
                            "schema_version": 1,
                            "direct_statement_changes": sorted(statement_changed),
                            "proof_only_changes": sorted(proof_changed),
                            "deleted": sorted(deleted),
                            "affected": sorted(affected),
                            "selected": sorted(selected),
                        },
                    )
                    store.set_metadata("manuscript", str(source))
                    store.set_metadata("main_file", main_file)
                    store.set_metadata("input_files", json.dumps(input_files))
                    store.set_metadata("pending_main_file_change", "")
                    stored_task_path = (
                        "VERIFY.yaml"
                        if task_path.resolve()
                        == (self.project / "VERIFY.yaml").resolve()
                        else str(task_path)
                    )
                    store.set_metadata("task_file", stored_task_path)
                    store.set_metadata("task_sha256", task_sha256)
                    store.set_metadata(
                        "task_spec", json.dumps(task.to_dict(), sort_keys=True)
                    )
                    config.update(
                        {
                            "manuscript": str(source),
                            "main_file": main_file,
                            "input_files": list(input_files),
                            "task_file": stored_task_path,
                            "package_version": __version__,
                        }
                    )
                    atomic_write_json(self.config_path, config)
                    self._ensure_claim_modules(objects)
                    self._export_state(store, objects, edges, snapshot)
                    self._write_status_files(store=store)
                    baseline_commit = self._commit_host_changes(
                        f"Import manuscript snapshot {snapshot.commit[:12]}"
                    )
                    return PreparedPass(
                        run_id=run_id,
                        snapshot=snapshot,
                        task=task,
                        task_sha256=task_sha256,
                        objects=objects,
                        edges=edges,
                        targets=frozenset(targets),
                        selected=frozenset(selected),
                        skipped_unproved=skipped_unproved,
                        directly_changed=frozenset(statement_changed),
                        proof_only_changed=frozenset(proof_changed),
                        affected=frozenset(affected),
                        deleted=frozenset(deleted),
                        unresolved_references=unresolved,
                        cycles=cycles,
                        source_diff=source_diff,
                        baseline_commit=baseline_commit,
                        main_file=main_file,
                        input_files=input_files,
                    )
                except Exception as exc:
                    store.add_failure_incident(
                        run_id=run_id,
                        scope="RUN",
                        failure_kind="INFRASTRUCTURE",
                        phase="PREPARING",
                        category=type(exc).__name__,
                        message=str(exc) or type(exc).__name__,
                        detail=f"{type(exc).__name__}: {exc}",
                        provenance="session.prepare_pass",
                        retryable=True,
                    )
                    store.finish_run(
                        run_id,
                        status="FAILED",
                        outcome="setup_failure",
                        completed_at=utc_now(),
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                    raise

    def _commit_host_changes(self, message: str) -> str:
        self._git(["add", "--all"])
        result = subprocess.run(
            ["git", "-C", str(self.project), "diff", "--cached", "--quiet"],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode == 1:
            self._git(["commit", "-q", "-m", message])
        elif result.returncode != 0:
            raise IncrementalProjectError("Could not inspect staged project changes")
        return self._git(["rev-parse", "HEAD"])

    def _export_state(
        self,
        store: StateStore,
        objects: Sequence[SourceObject],
        edges: Sequence[ManuscriptEdge],
        snapshot: Snapshot,
    ) -> None:
        claims = {
            "schema_version": 1,
            "snapshot": snapshot.commit,
            "claims": [
                item.export()
                for item in sorted(objects, key=lambda value: value.claim_id)
            ],
        }
        graph = manuscript_graph_export(objects, edges)
        correspondence = {
            "schema_version": 1,
            "mappings": [dict(row) for row in store.correspondence_rows()],
        }
        certificates = {
            "schema_version": 1,
            "certificates": [dict(row) for row in store.certificate_rows()],
        }
        lean_graph_path = self.exports / "lean-graph.json"
        lean_graph_sha256 = None
        if lean_graph_path.is_file():
            lean_graph = json.loads(lean_graph_path.read_text(encoding="utf-8"))
            if isinstance(lean_graph, dict):
                lean_graph_sha256 = lean_graph.get("sha256")
        combined_graph_sha256 = canonical_hash(
            {
                "schema_version": 1,
                "snapshot_commit": snapshot.commit,
                "manuscript_graph_sha256": graph["sha256"],
                "lean_graph_sha256": lean_graph_sha256,
                "factory_version": __version__,
            }
        )
        manifest = {
            "schema_version": 1,
            "snapshot_commit": snapshot.commit,
            "snapshot_tree": snapshot.tree,
            "claims_sha256": canonical_hash(claims),
            "manuscript_graph_sha256": graph["sha256"],
            "lean_graph_sha256": lean_graph_sha256,
            "combined_graph_sha256": combined_graph_sha256,
            "correspondence_sha256": canonical_hash(correspondence),
            "certificates_sha256": canonical_hash(certificates),
            "factory_version": __version__,
        }
        atomic_write_json(self.exports / "claims.json", claims)
        atomic_write_json(self.exports / "manuscript-graph.json", graph)
        atomic_write_json(self.exports / "correspondence.json", correspondence)
        atomic_write_json(self.exports / "certificates.json", certificates)
        atomic_write_json(self.exports / "manifest.json", manifest)

    def _write_status_files(self, *, store: StateStore | None = None) -> None:
        owns_store = store is None
        if store is None:
            store = StateStore(self.database_path)
        try:
            latest = store.latest_run()
            counts = store.summary_counts()
            questions = store.open_questions()
            lines = [
                "# Verification Status",
                "",
                f"Snapshot: `{store.previous_snapshot() or 'none'}`",
                f"Latest run: `{latest['run_id'] if latest else 'none'}`",
                "",
                "## Claim states",
                "",
            ]
            if counts:
                lines.extend(
                    f"- {state}: {count}" for state, count in sorted(counts.items())
                )
            else:
                lines.append("- No indexed claims")
            lines.extend(["", "## Open clarifications", ""])
            if questions:
                lines.extend(
                    f"- `{row['question_id']}` — `{row['claim_id']}`: {row['problem']}"
                    for row in questions
                )
            else:
                lines.append("No clarification is currently open.")
            atomic_write_text(
                self.project / "VERIFICATION_STATUS.md", "\n".join(lines) + "\n"
            )
            clarification_lines = ["# Clarification Requests", ""]
            if not questions:
                clarification_lines.append("No clarification is currently required.")
            for row in questions:
                resolutions = json.loads(row["resolutions_json"])
                clarification_lines.extend(
                    [
                        f"## {row['question_id']}: {row['claim_id']}",
                        "",
                        f"Category: `{row['category']}`",
                        "",
                        str(row["problem"]),
                        "",
                        "> " + str(row["passage"]).replace("\n", "\n> "),
                        "",
                        "Possible resolutions:",
                        "",
                        *[
                            f"{index}. {value}"
                            for index, value in enumerate(resolutions, 1)
                        ],
                        "",
                        "Edit the authoritative manuscript, then rerun the same verify command.",
                        "",
                    ]
                )
            atomic_write_text(
                self.project / "CLARIFICATION_REQUEST.md",
                "\n".join(clarification_lines).rstrip() + "\n",
            )
            if not (self.project / "VERIFICATION_REPORT.md").exists():
                atomic_write_text(
                    self.project / "VERIFICATION_REPORT.md",
                    "# Verification Report\n\nNo verification pass has completed yet.\n",
                )
        finally:
            if owns_store:
                store.close()

    def status(self) -> dict[str, Any]:
        config = self._load_config()
        mutation_in_progress = False
        try:
            lock_context = project_lock(self.project, exclusive=False)
            lock_context.__enter__()
        except ProjectLockedError:
            # SQLite WAL readers and atomic report/export renames are safe while
            # a backend worker holds the mutation lock. Monitoring must remain
            # available during a day-long Codex turn.
            mutation_in_progress = True
            lock_context = None
        try:
            with StateStore(self.database_path) as store:
                latest = store.latest_run()
                return {
                    "project": str(self.project),
                    "main_file": str(config["main_file"]),
                    "input_files": tuple(config.get("input_files", [])),
                    "mutation_in_progress": mutation_in_progress,
                    "snapshot": store.previous_snapshot(),
                    "latest_run": dict(latest) if latest else None,
                    "claim_states": store.summary_counts(),
                    "open_questions": [dict(row) for row in store.open_questions()],
                    "certificates": len(store.certificate_rows()),
                }
        finally:
            if lock_context is not None:
                lock_context.__exit__(None, None, None)

    def reconcile_conjectural_policy(self) -> bool:
        """Migrate persisted claim/question state to the unproved-skip contract."""

        changed = False
        config = self._load_config()
        _task_path, _task_text, _task_sha256, task = parse_task_file(
            self._task_path_from_config(config)
        )
        with project_lock(self.project, exclusive=True):
            with StateStore(self.database_path) as store:
                snapshot = store.previous_snapshot()
                latest = store.latest_run()
                if snapshot is None or latest is None:
                    return False
                objects = tuple(
                    _source_object_from_version(row)
                    for row in store.claim_versions(snapshot)
                )
                edges = tuple(
                    ManuscriptEdge(
                        str(row["src"]),
                        str(row["dst"]),
                        str(row["edge_kind"]),
                        str(row["provenance"]),
                        bool(row["approved"]),
                    )
                    for row in store.manuscript_edges()
                )
                targets = proof_target_ids(task, objects)
                selected = dependency_closure(
                    targets,
                    claim_ids={item.claim_id for item in objects},
                    edges=edges,
                )
                if not selected:
                    selected = set(targets)
                conjectural = {
                    item.claim_id
                    for item in objects
                    if is_conjectural_assertion(item)
                }
                before_states = {
                    claim_id: str(row["status"])
                    for claim_id in conjectural
                    if (row := store.claim_row(claim_id)) is not None
                }
                before_questions = tuple(
                    (
                        str(row["question_id"]),
                        str(row["snapshot_commit"]),
                        str(row["category"]),
                        str(row["passage"]),
                        str(row["problem"]),
                        str(row["resolutions_json"]),
                        str(row["blocking_claims_json"]),
                    )
                    for row in store.open_questions()
                    if str(row["claim_id"]) in conjectural
                )
                _reconcile_conjectural_policy(
                    store,
                    snapshot=snapshot,
                    objects=objects,
                    edges=edges,
                    selected=selected,
                    run_id=int(latest["run_id"]),
                )
                after_states = {
                    claim_id: str(row["status"])
                    for claim_id in conjectural
                    if (row := store.claim_row(claim_id)) is not None
                }
                after_questions = tuple(
                    (
                        str(row["question_id"]),
                        str(row["snapshot_commit"]),
                        str(row["category"]),
                        str(row["passage"]),
                        str(row["problem"]),
                        str(row["resolutions_json"]),
                        str(row["blocking_claims_json"]),
                    )
                    for row in store.open_questions()
                    if str(row["claim_id"]) in conjectural
                )
                changed = (
                    before_states != after_states
                    or before_questions != after_questions
                )
                if changed:
                    self._write_status_files(store=store)
        if changed:
            self._commit_host_changes("Reconcile conjectural assertion policy")
        return changed
