from __future__ import annotations

import json
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..backend import CodexConfig
from ..incremental.graph import (
    affected_claims,
    dependency_closure,
    source_changes,
)
from ..incremental.io import atomic_write_json, atomic_write_text, canonical_hash
from ..incremental.latex import (
    LatexIndexError,
    discover_latex_sources,
    explicit_reference_graph,
    index_manuscript,
    resolve_latex_closure,
)
from ..incremental.models import (
    ClaimState,
    ManuscriptEdge,
    TaskPolicy,
    TaskSpec,
)
from ..incremental.orchestration import (
    VerificationCancelled,
    VerificationResult,
    VerifyOptions,
    verify_project,
)
from ..incremental.session import IncrementalSession
from ..incremental.snapshot import SourceInventoryEntry, StaleSourceError
from ..incremental.store import StateStore
from ..incremental.task import (
    DEFAULT_TASK_INSTRUCTIONS,
    parse_task_file,
    parse_task_text,
    task_document,
)
from ..presentation.clarifications import (
    ClarificationNarrator,
    ClarificationPresenter,
    IsolatedCodexClarificationNarrator,
)
from ..workspace.catalog import ProjectCatalog
from ..workspace.management import (
    ManagedProjectKind,
    ManagedProjectRecord,
    ProjectConfigurationError,
    ProjectManager,
)
from ..workspace.paths import (
    is_in_dropbox,
    validate_managed_project_path,
)
from ..workspace.source import compare_inventories, stable_source_copy
from .contracts import (
    CancellationReport,
    ChangeImpactPlan,
    ClaimChangeKind,
    ClaimImpact,
    FileChange,
    FileChangeKind,
    FindingSummary,
    LatexSourceCandidate,
    NewProjectRequest,
    ProgressEvent,
    ProgressPhase,
    ProgressSink,
    ProjectAvailability,
    ProjectCatalogEntry,
    ProjectDestinationInspection,
    ProjectSummary,
    SourceInspection,
    VerificationSettings,
    WorkflowSnapshot,
    WorkflowState,
)


class StaleChangePlanError(RuntimeError):
    pass


class WorkflowCancelled(RuntimeError):
    pass


class ProjectDestinationError(RuntimeError):
    """Creation/resumption conflict carrying the same typed catalog facts."""

    def __init__(self, inspection: ProjectDestinationInspection) -> None:
        self.inspection = inspection
        super().__init__(
            inspection.issue or "Managed project destination is unavailable"
        )


class CancellationFlag:
    """Thread-safe token suitable for Textual workers and non-UI callers."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise WorkflowCancelled("Verification was cancelled")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _task_from_dict(payload: dict[str, Any]) -> TaskSpec:
    raw_policy = payload.get("policy") or {}
    return TaskSpec(
        mode=str(payload.get("mode", "theorem")),
        targets=tuple(str(item) for item in payload.get("targets", ())),
        policy=TaskPolicy(**raw_policy),
        free_form=str(payload.get("free_form", "")),
        source_format=str(payload.get("source_format", "yaml")),
    )


def _target_set(task: TaskSpec, objects: tuple[Any, ...]) -> set[str]:
    if task.targets:
        return set(task.targets)
    theorem_kinds = {
        "claim",
        "conjecture",
        "corollary",
        "lemma",
        "observation",
        "proposition",
        "theorem",
    }
    return {item.claim_id for item in objects if item.kind in theorem_kinds}


class ProofAssistantWorkflow:
    """Concrete UI-neutral application service implementing the public contract."""

    def __init__(
        self,
        *,
        catalog_root: Path | None = None,
        cache_home: str | None = None,
        codex: str = "codex",
        clarification_narrator: ClarificationNarrator | None = None,
        use_codex_clarification: bool = True,
        codex_model: str = "gpt-5.6-sol",
    ) -> None:
        catalog_path = None
        if catalog_root is not None:
            catalog_path = (
                catalog_root
                if catalog_root.suffix.casefold() == ".json"
                else catalog_root / "projects.json"
            )
        self.catalog = ProjectCatalog(catalog_path)
        self.projects = ProjectManager(self.catalog)
        self.cache_home = cache_home
        self.codex = codex
        self._provided_narrator = clarification_narrator
        self.use_codex_clarification = use_codex_clarification
        self.codex_model = codex_model
        self._sequence = 0

    def default_task_text(self) -> str:
        """Return the validated instructions seeded into the TUI task editor."""
        return DEFAULT_TASK_INSTRUCTIONS

    def inspect_source(self, source: Path) -> SourceInspection:
        source = source.expanduser().resolve()
        try:
            discovered = discover_latex_sources(source)
        except LatexIndexError as exc:
            raise ValueError(str(exc)) from exc
        candidates = tuple(
            LatexSourceCandidate(relative_path, has_documentclass)
            for relative_path, has_documentclass in discovered
        )
        document_roots = tuple(item for item in candidates if item.has_documentclass)
        suggestion_pool = document_roots or candidates
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
        suggested = min(
            suggestion_pool,
            key=lambda item: (
                preferred_names.get(
                    Path(item.relative_path).name.casefold(), len(preferred_names)
                ),
                len(Path(item.relative_path).parts),
                item.relative_path.casefold(),
                item.relative_path,
            ),
        )
        return SourceInspection(
            source_path=source,
            candidates=candidates,
            suggested_main_file=suggested.relative_path,
            source_in_dropbox=is_in_dropbox(source),
        )

    def inspect_project_destination(
        self, name: str, project_path: Path | None = None
    ) -> ProjectDestinationInspection:
        resolved = self.projects.resolve_destination(name, project_path)
        return self._destination_inspection(self.projects.inspect(resolved))

    def list_projects(self) -> tuple[ProjectCatalogEntry, ...]:
        entries: list[ProjectCatalogEntry] = []
        for record in self.projects.entries():
            if record.kind == ManagedProjectKind.RESUMABLE:
                try:
                    summary = self._summary(record.project_path)
                except Exception as exc:
                    entries.append(
                        self._catalog_entry(
                            ManagedProjectRecord(
                                record.project_path,
                                ManagedProjectKind.INCOMPLETE,
                                record.name,
                                f"Recognized project could not be opened: {exc}",
                                source_path=record.source_path,
                            )
                        )
                    )
                else:
                    entries.append(self._catalog_entry(record, project=summary))
            else:
                entries.append(self._catalog_entry(record))
        return tuple(entries)

    def create_project(self, request: NewProjectRequest) -> WorkflowSnapshot:
        inspection = self.inspect_project_destination(
            request.name, request.project_path
        )
        if not inspection.can_create:
            record = self.projects.inspect(inspection.project_path)
            self.projects.remember_occupied(record)
            raise ProjectDestinationError(inspection)
        source = request.source_path.expanduser().resolve()
        source_inspection = self.inspect_source(source)
        selected = Path(str(request.main_file)).as_posix()
        candidate_paths = {item.relative_path for item in source_inspection.candidates}
        if selected not in candidate_paths:
            choices = ", ".join(sorted(candidate_paths))
            raise ValueError(
                f"Selected main LaTeX file is not a source candidate: {selected!r}. "
                f"Choose one of: {choices}"
            )
        try:
            resolve_latex_closure(source, selected)
        except LatexIndexError as exc:
            raise ValueError(str(exc)) from exc
        project = inspection.project_path
        source_in_dropbox = is_in_dropbox(source)
        IncrementalSession.initialize(
            manuscript=source,
            task_text=request.task_text,
            project=project,
            project_name=request.name,
            source_in_dropbox=source_in_dropbox,
            main_file=selected,
        )
        self._record_workflow_state(project, WorkflowState.PROJECT_READY)
        self.catalog.upsert(project)
        return WorkflowSnapshot(
            state=WorkflowState.PROJECT_READY,
            project=self._summary(project),
        )

    def resume_project(self, project: Path) -> WorkflowSnapshot:
        project = validate_managed_project_path(project)
        managed = self.projects.inspect(project)
        if managed.kind == ManagedProjectKind.MIGRATION_READY:
            # Session loading performs the manager-owned unambiguous migration.
            IncrementalSession(project)._load_config()
            managed = self.projects.inspect(project)
        if managed.kind != ManagedProjectKind.RESUMABLE:
            raise ProjectDestinationError(self._destination_inspection(managed))
        self._ensure_project_task(project)
        summary = self._summary(project)
        session = IncrementalSession(project)
        status = session.status()
        if status["mutation_in_progress"]:
            return WorkflowSnapshot(WorkflowState.BUSY_EXTERNAL, summary)
        try:
            plan = self.plan_changes(project)
        except Exception as exc:
            return WorkflowSnapshot(
                WorkflowState.FAILED,
                summary,
                error=f"Could not observe the manuscript source: {exc}",
            )
        if plan is not None:
            state = WorkflowState.CHANGE_REVIEW
            self._record_workflow_state(project, state)
            return WorkflowSnapshot(state, self._summary(project), pending_plan=plan)
        if status["open_questions"]:
            state = WorkflowState.AWAITING_CLARIFICATION
            clarifications = self._presenter(project).present_all(
                project, summary.source_path
            )
            self._record_workflow_state(project, state)
            return WorkflowSnapshot(
                state, self._summary(project), clarifications=clarifications
            )
        latest = status["latest_run"] or {}
        if latest.get("status") == "INTERRUPTED":
            state = WorkflowState.INTERRUPTED
        elif latest.get("status") == "FAILED":
            state = WorkflowState.FAILED
        elif latest.get("outcome") in {
            "verified",
            "partial_unresolved",
            "counterexample_found",
        }:
            state = WorkflowState.COMPLETED
        else:
            state = WorkflowState.PROJECT_READY
        findings = (
            self._findings_from_store(project)
            if state == WorkflowState.COMPLETED
            else None
        )
        self._record_workflow_state(project, state)
        return WorkflowSnapshot(state, self._summary(project), findings=findings)

    def select_project_main_file(
        self, project: Path, main_file: str
    ) -> WorkflowSnapshot:
        project = validate_managed_project_path(project)
        try:
            self.projects.select_main_file(project, main_file)
        except ProjectConfigurationError as exc:
            record = self.projects.inspect(project)
            raise ProjectDestinationError(
                ProjectDestinationInspection(
                    project,
                    ProjectAvailability(record.kind.value),
                    str(exc),
                )
            ) from exc
        return self.resume_project(project)

    def plan_changes(self, project: Path) -> ChangeImpactPlan | None:
        """Compute a complete candidate plan without changing project authority."""
        project = validate_managed_project_path(project)
        session = IncrementalSession(project)
        config = session._load_config()
        source = Path(str(config["manuscript"])).expanduser().resolve()
        main_file = str(config["main_file"])
        task_path = session._task_path_from_config(config)
        _task_path, _task_text, task_sha, task = parse_task_file(task_path)

        with stable_source_copy(source) as (candidate_source, inventory):
            try:
                source_files = resolve_latex_closure(candidate_source, main_file)
            except LatexIndexError as exc:
                raise ValueError(str(exc)) from exc
            input_files = source_files[1:]
            with StateStore(session.database_path) as store:
                snapshot = store.previous_snapshot()
                previous_rows = (
                    {
                        str(row["claim_id"]): row
                        for row in store.claim_versions(snapshot)
                    }
                    if snapshot
                    else {}
                )
                prior_files = {
                    str(row["path"]): SourceInventoryEntry(
                        str(row["path"]),
                        str(row["sha256"]),
                        int(row["size"]),
                    )
                    for row in (store.source_file_rows(snapshot) if snapshot else [])
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
                old_task_payload = json.loads(store.get_metadata("task_spec") or "{}")
                old_task_sha = store.get_metadata("task_sha256")
                main_file_changed = bool(store.get_metadata("pending_main_file_change"))
                certificates = {
                    str(row["claim_id"]) for row in store.certificate_rows()
                }
                open_questions = [dict(row) for row in store.open_questions()]
                temporary = Path(tempfile.mkdtemp(prefix="proof-assistant-plan-"))
                database_copy = temporary / "state.sqlite3"
                store.backup_to(database_copy)
            try:
                with StateStore(database_copy) as candidate_store:
                    objects = index_manuscript(
                        candidate_source, candidate_store, main_file=main_file
                    )
                explicit_edges, _unresolved = explicit_reference_graph(objects)
            finally:
                import shutil

                shutil.rmtree(temporary, ignore_errors=True)

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
        statement, proof, deleted = source_changes(
            previous_rows, objects, mode=task.mode
        )
        added = {claim_id for claim_id in statement if claim_id not in previous_rows}
        statement -= added
        old_edge_keys = {(edge.src, edge.dst, edge.kind) for edge in old_edges}
        new_edge_keys = {(edge.src, edge.dst, edge.kind) for edge in edges}
        dependency_changed = {
            src
            for src, _dst, _kind in old_edge_keys ^ new_edge_keys
            if src in current_ids
        }
        union_ids = current_ids | set(previous_rows)
        union_edges = {
            (edge.src, edge.dst, edge.kind): edge for edge in (*old_edges, *edges)
        }
        affected = affected_claims(
            statement | added | proof | deleted | dependency_changed,
            claim_ids=union_ids,
            edges=union_edges.values(),
        )

        old_task = _task_from_dict(old_task_payload) if old_task_payload else task
        task_changed = old_task_sha != task_sha
        task_impacts: list[ClaimImpact] = []
        if task_changed:
            old_targets = _target_set(old_task, objects)
            new_targets = _target_set(task, objects)
            for claim_id in sorted(old_targets ^ new_targets):
                task_impacts.append(ClaimImpact(claim_id, ClaimChangeKind.TASK_SCOPE))
            if old_task.mode != task.mode:
                for claim_id in sorted(new_targets):
                    task_impacts.append(
                        ClaimImpact(claim_id, ClaimChangeKind.TASK_MODE)
                    )
            if old_task.policy != task.policy:
                for claim_id in sorted(new_targets):
                    task_impacts.append(ClaimImpact(claim_id, ClaimChangeKind.POLICY))
            if old_task.free_form != task.free_form:
                for claim_id in sorted(new_targets):
                    task_impacts.append(
                        ClaimImpact(claim_id, ClaimChangeKind.TASK_SCOPE)
                    )
            task_seed = {impact.claim_id for impact in task_impacts}
            affected.update(
                dependency_closure(task_seed, claim_ids=current_ids, edges=edges)
            )

        object_files = {item.claim_id: item.source_file for item in objects}
        direct: list[ClaimImpact] = [
            ClaimImpact(claim_id, ClaimChangeKind.ADDED, object_files.get(claim_id))
            for claim_id in sorted(added)
        ]
        direct.extend(
            ClaimImpact(claim_id, ClaimChangeKind.STATEMENT, object_files.get(claim_id))
            for claim_id in sorted(statement)
        )
        direct.extend(
            ClaimImpact(
                claim_id, ClaimChangeKind.PROOF_ONLY, object_files.get(claim_id)
            )
            for claim_id in sorted(proof)
        )
        direct.extend(
            ClaimImpact(
                claim_id, ClaimChangeKind.DEPENDENCY, object_files.get(claim_id)
            )
            for claim_id in sorted(dependency_changed)
        )
        direct.extend(
            ClaimImpact(
                claim_id,
                ClaimChangeKind.DELETED,
                str(previous_rows[claim_id]["source_file"]),
            )
            for claim_id in sorted(deleted)
        )
        direct.extend(task_impacts)
        relevant_files = {
            main_file,
            *input_files,
            *(str(value) for value in config.get("input_files", [])),
        }
        deltas = tuple(
            item
            for item in compare_inventories(prior_files, inventory)
            if item.path in relevant_files
            or (item.old_path is not None and item.old_path in relevant_files)
        )
        file_changes = tuple(
            FileChange(
                delta.path,
                FileChangeKind(delta.kind),
                delta.old_path,
                delta.old_sha256,
                delta.new_sha256,
            )
            for delta in deltas
        )
        if not file_changes and not task_changed and not main_file_changed:
            return None
        superseded = tuple(
            sorted(
                str(question["question_id"])
                for question in open_questions
                if str(question["claim_id"]) in statement | added | proof | deleted
            )
        )
        identity = {
            "schema_version": 1,
            "project": str(project),
            "source": str(source),
            "main_file": main_file,
            "input_files": list(input_files),
            "base_snapshot": snapshot,
            "candidate_inventory_sha256": inventory.sha256,
            "task_sha256": task_sha,
            "main_file_changed": main_file_changed,
            "file_changes": [
                {
                    "path": item.path,
                    "kind": str(item.kind),
                    "old_path": item.old_path,
                    "old_sha256": item.old_sha256,
                    "new_sha256": item.new_sha256,
                }
                for item in file_changes
            ],
            "claim_impacts": [
                {
                    "claim_id": item.claim_id,
                    "kind": str(item.kind),
                    "source_file": item.source_file,
                }
                for item in direct
            ],
            "affected": sorted(affected),
        }
        return ChangeImpactPlan(
            plan_id=canonical_hash(identity),
            project_path=project,
            source_path=source,
            main_file=main_file,
            input_files=input_files,
            base_snapshot=snapshot,
            candidate_inventory_sha256=inventory.sha256,
            file_changes=file_changes,
            direct_claim_changes=tuple(direct),
            affected_claims=tuple(sorted(affected)),
            unaffected_certificates=tuple(sorted(certificates - affected)),
            superseded_questions=superseded,
            task_changed=task_changed,
            source_in_dropbox=is_in_dropbox(source),
            created_at=_now(),
            main_file_changed=main_file_changed,
        )

    def confirm_and_verify(
        self,
        project: Path,
        plan_id: str | None,
        settings: VerificationSettings,
        *,
        progress: ProgressSink | None = None,
        cancellation: Any | None = None,
    ) -> WorkflowSnapshot:
        project = validate_managed_project_path(project)
        current = self.plan_changes(project)
        if plan_id is not None and (current is None or current.plan_id != plan_id):
            raise StaleChangePlanError(
                "The manuscript or task changed after review; inspect the new impact plan"
            )
        if plan_id is None and current is not None:
            raise StaleChangePlanError(
                "A manuscript or task change requires explicit review and confirmation"
            )
        try:
            self._checkpoint(cancellation)
        except WorkflowCancelled as exc:
            return self._interrupted_snapshot(project, exc)
        self._record_workflow_state(project, WorkflowState.VERIFYING)
        project_summary = self._summary(project)
        active_main_file = current.main_file if current else project_summary.main_file
        active_input_files = (
            current.input_files if current else project_summary.input_files
        )
        self._emit(
            progress,
            ProgressPhase.VALIDATING,
            f"Validated {active_main_file} and "
            f"{len(active_input_files)} recursive input file(s)",
            details={
                "main_file": active_main_file,
                "input_files": active_input_files,
                "source_path": str(project_summary.source_path),
            },
        )

        def event_hook(phase: str, message: str, details: dict[str, Any]) -> None:
            try:
                mapped = ProgressPhase(phase)
            except ValueError:
                mapped = ProgressPhase.PROOF_BATCH
            self._emit(progress, mapped, message, details=details)

        try:
            result = verify_project(
                IncrementalSession(project),
                options=VerifyOptions(
                    model=settings.model,
                    effort=settings.effort,
                    codex=self.codex,
                    cache_home=self.cache_home,
                    jobs=settings.jobs,
                    batch_size=settings.batch_size,
                    lean_pool_size=settings.lean_pool_size,
                    setup_timeout=settings.setup_timeout,
                    request_timeout=settings.request_timeout,
                    turn_timeout=settings.turn_timeout,
                    gc_timeout=settings.gc_timeout,
                ),
                expected_inventory_sha256=(
                    current.candidate_inventory_sha256 if current else None
                ),
                event_hook=event_hook,
                cancellation_checkpoint=(
                    cancellation.raise_if_cancelled
                    if cancellation is not None
                    else None
                ),
            )
        except StaleSourceError as exc:
            self._record_workflow_state(project, WorkflowState.CHANGE_REVIEW)
            raise StaleChangePlanError(str(exc)) from exc
        except (WorkflowCancelled, VerificationCancelled) as exc:
            return self._interrupted_snapshot(project, exc)
        except Exception as exc:
            self._record_workflow_state(project, WorkflowState.FAILED, error=str(exc))
            return WorkflowSnapshot(
                WorkflowState.FAILED, self._summary(project), error=str(exc)
            )

        return self._snapshot_for_result(
            project, result, clarification_model=settings.model
        )

    def _snapshot_for_result(
        self,
        project: Path,
        result: VerificationResult,
        *,
        clarification_model: str | None = None,
    ) -> WorkflowSnapshot:
        summary = self._summary(project)
        findings = FindingSummary(
            outcome=result.outcome,
            detail=result.detail,
            verified=result.certified,
            reused=result.reused,
            reconciled=result.reconciled,
            counterexamples=result.counterexamples,
            report_path=project / "VERIFICATION_REPORT.md",
            project_path=project,
        )
        if result.outcome == "clarification_required":
            state = WorkflowState.AWAITING_CLARIFICATION
            clarifications = self._presenter(
                project, model=clarification_model
            ).present_all(project, summary.source_path)
            self._record_workflow_state(project, state)
            return WorkflowSnapshot(
                state,
                self._summary(project),
                clarifications=clarifications,
                findings=findings,
            )
        state = (
            WorkflowState.COMPLETED
            if result.exit_code in {0, 11, 12}
            else WorkflowState.FAILED
        )
        self._record_workflow_state(project, state)
        return WorkflowSnapshot(state, self._summary(project), findings=findings)

    def _presenter(
        self, project: Path, *, model: str | None = None
    ) -> ClarificationPresenter:
        narrator = self._provided_narrator
        if narrator is None and self.use_codex_clarification:
            narrator = IsolatedCodexClarificationNarrator(
                CodexConfig(
                    executable=self.codex,
                    model=model or self.codex_model,
                    effort="low",
                    sandbox="read-only",
                    isolate_external_tools=True,
                ),
                cwd=project,
            )
        return ClarificationPresenter(narrator)

    def _summary(self, project: Path) -> ProjectSummary:
        session = IncrementalSession(project)
        config = session._load_config()
        status = session.status()
        latest = status["latest_run"] or {}
        persisted = self._read_workflow_state(project)
        if status["mutation_in_progress"]:
            state = WorkflowState.BUSY_EXTERNAL
        else:
            try:
                state = WorkflowState(str(persisted.get("state", "PROJECT_READY")))
            except ValueError:
                state = WorkflowState.PROJECT_READY
        return ProjectSummary(
            project_id=str(config.get("project_id") or project.resolve()),
            name=str(config.get("name") or project.name),
            project_path=project.resolve(),
            source_path=Path(str(config["manuscript"])).expanduser().resolve(),
            main_file=str(config["main_file"]),
            input_files=tuple(str(value) for value in config.get("input_files", [])),
            last_opened_at=str(
                persisted.get("updated_at") or config.get("created_at") or ""
            ),
            workflow_state=state,
            latest_outcome=latest.get("outcome"),
            open_questions=len(status["open_questions"]),
            source_in_dropbox=bool(
                config.get("source_in_dropbox", is_in_dropbox(config["manuscript"]))
            ),
        )

    @staticmethod
    def _destination_inspection(
        record: ManagedProjectRecord,
    ) -> ProjectDestinationInspection:
        return ProjectDestinationInspection(
            project_path=record.project_path,
            availability=ProjectAvailability(record.kind.value),
            issue=record.issue,
        )

    @staticmethod
    def _catalog_entry(
        record: ManagedProjectRecord, *, project: ProjectSummary | None = None
    ) -> ProjectCatalogEntry:
        return ProjectCatalogEntry(
            name=record.name,
            project_path=record.project_path,
            availability=ProjectAvailability(record.kind.value),
            project=project,
            issue=record.issue,
            source_path=record.source_path,
            main_file_candidates=tuple(
                LatexSourceCandidate(path, has_documentclass)
                for path, has_documentclass in record.candidates
            ),
            suggested_main_file=record.suggested_main_file,
        )

    def _findings_from_store(self, project: Path) -> FindingSummary:
        session = IncrementalSession(project)
        with StateStore(session.database_path) as store:
            latest = store.latest_run()
            rows = store.current_claim_rows()
        states: dict[str, list[str]] = {}
        for row in rows:
            states.setdefault(str(row["status"]), []).append(str(row["claim_id"]))
        return FindingSummary(
            outcome=str(latest["outcome"] if latest else "unknown"),
            detail=str(latest["detail"] if latest else "No verification run"),
            verified=tuple(sorted(states.get(str(ClaimState.CERTIFIED), []))),
            unresolved=tuple(sorted(states.get(str(ClaimState.UNRESOLVED), []))),
            suspect_false=tuple(sorted(states.get(str(ClaimState.SUSPECT_FALSE), []))),
            counterexamples=tuple(
                sorted(states.get(str(ClaimState.COUNTEREXAMPLE_FOUND), []))
            ),
            report_path=project / "VERIFICATION_REPORT.md",
            project_path=project,
        )

    def _interrupted_snapshot(
        self, project: Path, exc: WorkflowCancelled | VerificationCancelled
    ) -> WorkflowSnapshot:
        if isinstance(exc, VerificationCancelled) and exc.run_id is not None:
            report = CancellationReport(
                run_id=exc.run_id,
                detail=str(exc),
                preserved_certificates=tuple(exc.preserved_certificates),
                retryable_claims=tuple(exc.retryable_claims),
                temporary_worktrees_cleaned=exc.temporary_worktrees_cleaned,
            )
        else:
            session = IncrementalSession(project)
            with StateStore(session.database_path) as store:
                preserved = tuple(
                    sorted(str(row["claim_id"]) for row in store.certificate_rows())
                )
            report = CancellationReport(
                run_id=None,
                detail=str(exc),
                preserved_certificates=preserved,
                retryable_claims=(),
                temporary_worktrees_cleaned=(
                    exc.temporary_worktrees_cleaned
                    if isinstance(exc, VerificationCancelled)
                    else True
                ),
            )
        self._record_workflow_state(
            project, WorkflowState.INTERRUPTED, error=report.detail
        )
        return WorkflowSnapshot(
            WorkflowState.INTERRUPTED,
            self._summary(project),
            error=report.detail,
            cancellation=report,
        )

    def _ensure_project_task(self, project: Path) -> None:
        session = IncrementalSession(project)
        config = session._load_config()
        owned = project / "VERIFY.yaml"
        configured = session._task_path_from_config(config)
        if owned.is_file() and configured == owned.resolve():
            return
        if configured.is_file():
            text = configured.read_text(encoding="utf-8")
        elif (project / "RepoProverInput" / "TASK.md").is_file():
            text = (project / "RepoProverInput" / "TASK.md").read_text(encoding="utf-8")
        else:
            text = task_document()
        try:
            parse_task_text(text)
        except Exception:
            text = task_document(text)
            parse_task_text(text)
        atomic_write_text(owned, text)
        config["task_file"] = "VERIFY.yaml"
        config["package_version"] = "0.1.0"
        atomic_write_json(session.config_path, config)
        session._commit_host_changes("Migrate to project-owned Proof Assistant task")

    def _record_workflow_state(
        self, project: Path, state: WorkflowState, *, error: str | None = None
    ) -> None:
        atomic_write_json(
            project / ".repoprover" / "workflow.json",
            {
                "schema_version": 1,
                "state": str(state),
                "updated_at": _now(),
                "error": error,
            },
        )
        try:
            self.catalog.upsert(project)
        except Exception:
            pass

    @staticmethod
    def _read_workflow_state(project: Path) -> dict[str, Any]:
        try:
            payload = json.loads(
                (project / ".repoprover" / "workflow.json").read_text(encoding="utf-8")
            )
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _checkpoint(cancellation: Any | None) -> None:
        if cancellation is not None:
            cancellation.raise_if_cancelled()

    def _emit(
        self,
        sink: ProgressSink | None,
        phase: ProgressPhase,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        if sink is None:
            return
        self._sequence += 1
        payload = details or {}
        completed = payload.get("completed")
        total = payload.get("total")
        claim_id = payload.get("claim_id")
        sink(
            ProgressEvent(
                self._sequence,
                phase,
                message,
                completed=(
                    completed
                    if isinstance(completed, int) and not isinstance(completed, bool)
                    else None
                ),
                total=(
                    total
                    if isinstance(total, int) and not isinstance(total, bool)
                    else None
                ),
                claim_id=claim_id if isinstance(claim_id, str) else None,
                details=payload,
            )
        )
