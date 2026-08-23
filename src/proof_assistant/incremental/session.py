from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
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
from .graph import (
    affected_claims,
    build_graph,
    canonical_cycles,
    dependency_closure,
    manuscript_graph_export,
    source_changes,
)
from .io import atomic_write_json, atomic_write_text, canonical_hash
from .latex import explicit_reference_graph, index_manuscript
from .lean import install_dependency_extractor
from .locking import ProjectLockedError, project_lock
from .models import ClaimState, ManuscriptEdge, Snapshot, SourceObject, TaskSpec
from .snapshot import SnapshotRepository, sync_project_manuscript
from .store import StateStore
from .task import parse_task_file, parse_task_text, task_document

PROJECT_CONFIG_VERSION = 1
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
    directly_changed: frozenset[str]
    proof_only_changed: frozenset[str]
    affected: frozenset[str]
    deleted: frozenset[str]
    unresolved_references: tuple[tuple[str, str], ...]
    cycles: tuple[tuple[str, ...], ...]
    source_diff: str
    baseline_commit: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
    ) -> IncrementalSession:
        source = Path(manuscript).expanduser().resolve()
        destination = Path(project).expanduser().resolve()
        if not source.is_dir():
            raise ManuscriptInputError(f"Manuscript directory does not exist: {source}")
        if _is_within(destination, source) or _is_within(source, destination):
            raise ManuscriptInputError(
                "Manuscript and persistent project directories must not contain one another"
            )
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
                objects = index_manuscript(manuscript_copy, store)
                edges, unresolved = explicit_reference_graph(objects)
                cycles = canonical_cycles(
                    build_graph((item.claim_id for item in objects), edges)
                )
                store.replace_current_claims(
                    snapshot.commit,
                    objects,
                    run_id=run_id,
                    state_updates={
                        item.claim_id: ClaimState.DISCOVERED for item in objects
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
        if not self.config_path.is_file():
            raise IncrementalProjectError(
                f"Not an incremental RepoProver project: {self.project}"
            )
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IncrementalProjectError(
                f"Invalid project configuration: {exc}"
            ) from exc
        if config.get("schema_version") != PROJECT_CONFIG_VERSION:
            raise IncrementalProjectError("Unsupported incremental project schema")
        return config

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
        _already_locked: bool = False,
    ) -> PreparedPass:
        config = self._load_config()
        source = Path(manuscript or config["manuscript"]).expanduser().resolve()
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
                    manuscript_copy = sync_project_manuscript(
                        self.snapshots, snapshot.commit, self.project
                    )
                    atomic_write_text(
                        self.project / "RepoProverInput" / "TASK.md", task_text
                    )
                    objects = index_manuscript(manuscript_copy, store)
                    explicit_edges, unresolved = explicit_reference_graph(objects)
                    current_ids = {item.claim_id for item in objects}
                    persistent_edges = tuple(
                        edge
                        for edge in old_edges
                        if edge.kind != "explicit_ref"
                        and edge.src in current_ids
                        and edge.dst in current_ids
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
                        targets = set(task.targets)
                    else:
                        targets = {
                            item.claim_id
                            for item in objects
                            if item.kind
                            in {
                                "claim",
                                "conjecture",
                                "corollary",
                                "lemma",
                                "observation",
                                "proposition",
                                "theorem",
                            }
                        }
                    selected = dependency_closure(
                        targets,
                        claim_ids=current_ids,
                        edges=edges,
                    )
                    if not selected:
                        selected = set(targets)
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
                        directly_changed=frozenset(statement_changed),
                        proof_only_changed=frozenset(proof_changed),
                        affected=frozenset(affected),
                        deleted=frozenset(deleted),
                        unresolved_references=unresolved,
                        cycles=cycles,
                        source_diff=source_diff,
                        baseline_commit=baseline_commit,
                    )
                except Exception as exc:
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
        self._load_config()
        mutation_in_progress = False
        try:
            lock_context = project_lock(self.project, exclusive=False)
            lock_context.__enter__()
        except ProjectLockedError:
            # SQLite WAL readers and atomic report/export renames are safe while
            # a writer owns the project lock. Monitoring must remain available
            # during a day-long Codex turn.
            mutation_in_progress = True
            lock_context = None
        try:
            with StateStore(self.database_path) as store:
                latest = store.latest_run()
                return {
                    "project": str(self.project),
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
