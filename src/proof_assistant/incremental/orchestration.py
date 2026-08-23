from __future__ import annotations

import os
import shutil
import subprocess
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from ..backend import CodexConfig
from ..cache import (
    COLD_DEPOT_RESERVE_GB,
    WARM_PROJECT_RESERVE_GB,
    CacheLayout,
    cache_policy,
    claim_dependency_depot,
    dependency_cache_key,
    dependency_depot_ready,
    dependency_depot_target,
    ensure_project_outside_dropbox,
    managed_project_session,
)
from ..environment import configure_lean_runtime, default_lean_memory_limit_gb
from ..integration import run_repoprover_agent
from ..manuscript import (
    bootstrap_lean_workspace,
    command_records_text,
    commit_bootstrap_state,
    run_command,
    serialize_command,
    serialize_tool_call,
)
from .agent import (
    IncrementalAgentContext,
    create_incremental_agent,
    write_batch_context,
)
from .certification import (
    CertificationResult,
    certify_current_correspondence,
    revalidate_unchanged_certificates,
)
from .diagnostics import classify_failure
from .graph import blocked_descendants, ready_frontier
from .io import atomic_write_json, atomic_write_text
from .lean import (
    environment_fingerprint,
    mathlib_revision,
    run_dependency_extractor,
)
from .locking import project_lock
from .models import ClaimState, ManuscriptEdge
from .reports import dependency_audit, render_report
from .session import IncrementalSession, PreparedPass, claim_module_path, utc_now
from .store import StateStore


@dataclass(frozen=True)
class VerifyOptions:
    model: str
    effort: str = "high"
    codex: str = "codex"
    cache_home: str | None = None
    jobs: int = 1
    batch_size: int = 8
    lean_pool_size: int = 1
    lean_memory_limit_gb: int | None = None
    setup_timeout: float = 1800.0
    request_timeout: float = 120.0
    turn_timeout: float = 3600.0
    gc_timeout: float = 900.0

    def validate(self) -> None:
        if not self.model:
            raise ValueError("A Codex model is required")
        if not 1 <= self.jobs <= 2:
            raise ValueError(
                "--jobs must be 1 or 2 (the package concurrency limit is 2)"
            )
        if self.batch_size < 1:
            raise ValueError("--batch-size must be positive")
        if self.lean_pool_size < 1:
            raise ValueError("--lean-pool-size must be positive")


@dataclass(frozen=True)
class BatchJob:
    index: int
    project: str
    workspace: str
    run_id: int
    snapshot: str
    previous_snapshot: str | None
    claims: tuple[str, ...]
    require_correspondence_review: bool
    pause_on_ambiguity: bool
    counterexample_search: bool
    options: VerifyOptions


@dataclass(frozen=True)
class BatchResult:
    index: int
    claims: tuple[str, ...]
    workspace: str
    base_commit: str
    final_commit: str
    git_status: str
    build_succeeded: bool
    provider_failure: str | None
    final_text: str
    thread_id: str | None
    turn_id: str | None
    tool_calls: int
    error: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    outcome: str
    detail: str
    exit_code: int
    run_id: int
    snapshot: str
    project: str
    certified: tuple[str, ...]
    reused: tuple[str, ...]
    reconciled: tuple[str, ...]
    questions: tuple[str, ...]
    counterexamples: tuple[str, ...]


class VerificationCancelled(RuntimeError):
    pass


def _git(path: Path, arguments: Sequence[str], *, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Git failed ({' '.join(arguments)}): "
            + (result.stderr or result.stdout).strip()
        )
    return result.stdout.strip()


def _runtime_environment(
    layout: CacheLayout, lean_cc: str | None = None
) -> tuple[dict[str, str], str]:
    layout.create()
    config = layout.load_config()
    layout.apply_runtime_environment(lean_cc=config.lean_cc if config else None)
    if lean_cc:
        os.environ["LEAN_CC"] = lean_cc
    compiler = configure_lean_runtime()
    if config is None or config.lean_cc != compiler.executable:
        layout.record_compiler(compiler)
    return (
        layout.runtime_environment(os.environ, lean_cc=compiler.executable),
        compiler.executable,
    )


def _run_batch_worker(job: BatchJob) -> BatchResult:
    workspace = Path(job.workspace)
    project = Path(job.project)
    base_commit = ""
    final_commit = ""
    git_status = ""
    final_text = ""
    thread_id: str | None = None
    turn_id: str | None = None
    tool_count = 0
    provider_failure: str | None = None
    try:
        layout = CacheLayout.discover(job.options.cache_home)
        runtime_env, _compiler = _runtime_environment(layout)
        policy = cache_policy(layout.load_config())
        with managed_project_session(
            workspace,
            layout,
            policy,
            attach=True,
            reserve_gb=WARM_PROJECT_RESERVE_GB,
            lease_timeout=job.options.setup_timeout,
            gc_timeout=job.options.gc_timeout,
        ):
            with claim_dependency_depot(
                workspace,
                layout,
                env=runtime_env,
                timeout=job.options.setup_timeout,
            ) as depot:
                setup = bootstrap_lean_workspace(
                    workspace,
                    env=runtime_env,
                    timeout=job.options.setup_timeout,
                    depot_claim=depot,
                )
                if any(record.required and not record.succeeded for record in setup):
                    return BatchResult(
                        job.index,
                        job.claims,
                        str(workspace),
                        "",
                        "",
                        "",
                        False,
                        None,
                        "",
                        None,
                        None,
                        0,
                        "Batch Lean bootstrap failed",
                    )
                base_commit = _git(workspace, ["rev-parse", "HEAD"])
                write_batch_context(
                    workspace,
                    run_id=job.run_id,
                    snapshot=job.snapshot,
                    claims=job.claims,
                    pause_on_ambiguity=job.pause_on_ambiguity,
                    counterexample_search=job.counterexample_search,
                )
                context = IncrementalAgentContext(
                    project=project,
                    workspace=workspace,
                    run_id=job.run_id,
                    snapshot=job.snapshot,
                    previous_snapshot=job.previous_snapshot,
                    allowed_claims=frozenset(job.claims),
                    require_correspondence_review=job.require_correspondence_review,
                    pause_on_ambiguity=job.pause_on_ambiguity,
                    counterexample_search=job.counterexample_search,
                )
                agent = create_incremental_agent(
                    workspace, context=context, claims=job.claims
                )
                from repoprover.agents.lean_tools import (
                    configure_global_pool,
                    shutdown_global_pool,
                )

                memory_limit = (
                    job.options.lean_memory_limit_gb
                    if job.options.lean_memory_limit_gb is not None
                    else default_lean_memory_limit_gb()
                )
                configure_global_pool(
                    workspace,
                    pool_size=job.options.lean_pool_size,
                    instance_mem_limit_gb=memory_limit,
                )
                try:
                    try:
                        wrapped = run_repoprover_agent(
                            agent,
                            run_kwargs={},
                            codex=CodexConfig(
                                executable=job.options.codex,
                                model=job.options.model,
                                effort=job.options.effort,
                                request_timeout=job.options.request_timeout,
                                turn_timeout=job.options.turn_timeout,
                                sandbox="read-only",
                            ),
                        )
                        final_text = wrapped.codex.final_text
                        thread_id = wrapped.codex.thread_id
                        turn_id = wrapped.codex.turn_id
                        tool_count = len(wrapped.codex.tool_calls)
                        run_directory = (
                            project
                            / ".repoprover"
                            / "runs"
                            / f"{job.run_id:06d}"
                            / f"batch-{job.index:04d}"
                        )
                        atomic_write_text(
                            run_directory / "final.md", final_text.rstrip() + "\n"
                        )
                        atomic_write_json(
                            run_directory / "events.json", wrapped.codex.events
                        )
                        atomic_write_json(
                            run_directory / "tool-calls.json",
                            [
                                serialize_tool_call(call)
                                for call in wrapped.codex.tool_calls
                            ],
                        )
                    except Exception as exc:
                        provider_failure = f"{type(exc).__name__}: {exc}"
                finally:
                    shutdown_global_pool()
                build = run_command(
                    ("lake", "build"),
                    cwd=workspace,
                    env=runtime_env,
                    timeout=job.options.setup_timeout,
                )
                final_commit = _git(workspace, ["rev-parse", "HEAD"])
                git_status = _git(workspace, ["status", "--porcelain=v1"])
                return BatchResult(
                    job.index,
                    job.claims,
                    str(workspace),
                    base_commit,
                    final_commit,
                    git_status,
                    build.succeeded,
                    provider_failure,
                    final_text,
                    thread_id,
                    turn_id,
                    tool_count,
                )
    except Exception as exc:
        return BatchResult(
            job.index,
            job.claims,
            str(workspace),
            base_commit,
            final_commit,
            git_status,
            False,
            provider_failure,
            final_text,
            thread_id,
            turn_id,
            tool_count,
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )


def _partition(values: Sequence[str], size: int) -> list[tuple[str, ...]]:
    return [
        tuple(values[index : index + size]) for index in range(0, len(values), size)
    ]


def _edges(store: StateStore) -> tuple[ManuscriptEdge, ...]:
    return tuple(
        ManuscriptEdge(
            str(row["src"]),
            str(row["dst"]),
            str(row["edge_kind"]),
            str(row["provenance"]),
            bool(row["approved"]),
        )
        for row in store.manuscript_edges()
    )


def _states(store: StateStore) -> dict[str, ClaimState]:
    return {
        str(row["claim_id"]): ClaimState(str(row["status"]))
        for row in store.current_claim_rows()
    }


def _create_worktree(project: Path, path: Path) -> str:
    if path.exists():
        _git(project, ["worktree", "remove", "--force", str(path)], check=False)
        shutil.rmtree(path, ignore_errors=True)
    base = _git(project, ["rev-parse", "HEAD"])
    _git(project, ["worktree", "add", "--quiet", "--detach", str(path), base])
    return base


def _merge_batch(project: Path, result: BatchResult) -> str | None:
    if (
        result.provider_failure
        or result.error
        or not result.build_succeeded
        or result.git_status
    ):
        return (
            result.provider_failure
            or result.error
            or (
                "Batch final build failed"
                if not result.build_succeeded
                else "Batch left uncommitted changes"
            )
        )
    if result.final_commit == result.base_commit:
        return None
    changed_paths = set(
        _git(
            Path(result.workspace),
            ["diff", "--name-only", result.base_commit, result.final_commit, "--"],
        ).splitlines()
    )
    allowed_paths = {
        claim_module_path(claim_id).as_posix() for claim_id in result.claims
    }
    forbidden_paths = sorted(changed_paths - allowed_paths)
    if forbidden_paths:
        return "Proof batch modified host-controlled paths: " + ", ".join(
            forbidden_paths
        )
    commits = _git(
        Path(result.workspace),
        ["rev-list", "--reverse", f"{result.base_commit}..{result.final_commit}"],
    ).splitlines()
    for commit in commits:
        cherry = subprocess.run(
            ["git", "-C", str(project), "cherry-pick", commit],
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if cherry.returncode != 0:
            detail = (cherry.stderr or cherry.stdout).strip()
            subprocess.run(
                ["git", "-C", str(project), "cherry-pick", "--abort"],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            return f"Deterministic batch merge conflict at {commit}: {detail}"
    return None


def _remove_worktree(project: Path, workspace: Path) -> None:
    _git(project, ["worktree", "remove", "--force", str(workspace)], check=False)
    shutil.rmtree(workspace, ignore_errors=True)
    _git(project, ["worktree", "prune"], check=False)


def verify_project(
    session: IncrementalSession,
    *,
    options: VerifyOptions,
    manuscript: str | Path | None = None,
    task_file: str | Path | None = None,
    expected_inventory_sha256: str | None = None,
    event_hook: Callable[[str, str, dict[str, object]], None] | None = None,
    cancellation_checkpoint: Callable[[], None] | None = None,
) -> VerificationResult:
    def checkpoint() -> None:
        if cancellation_checkpoint is not None:
            try:
                cancellation_checkpoint()
            except Exception as exc:
                raise VerificationCancelled(
                    str(exc) or "Verification cancelled"
                ) from exc

    def notify(phase: str, message: str, **details: object) -> None:
        if event_hook is not None:
            event_hook(phase, message, details)

    options.validate()
    checkpoint()
    notify("VALIDATING", "Validated verification options and project location")
    layout = CacheLayout.discover(options.cache_home)
    ensure_project_outside_dropbox(session.project, layout)
    with project_lock(session.project, exclusive=True):
        prepared: PreparedPass | None = None
        try:
            runtime_env, compiler = _runtime_environment(layout)
            notify("CACHE_SETUP", "Prepared the shared Lean cache runtime")
            provisional_environment, _inputs = environment_fingerprint(session.project)
            prepared = session.prepare_pass(
                manuscript=manuscript,
                task_file=task_file,
                environment_hash=provisional_environment,
                expected_inventory_sha256=expected_inventory_sha256,
                _already_locked=True,
            )
            checkpoint()
            notify(
                "IMPACT_ANALYSIS",
                "Imported the stable manuscript snapshot and computed affected claims",
                affected=len(prepared.affected),
                selected=len(prepared.selected),
            )
            run_directory = session.runs / f"{prepared.run_id:06d}"
            policy = cache_policy(layout.load_config())
            key = dependency_cache_key(session.project, env=runtime_env)
            target = dependency_depot_target(layout, key)
            dependency_ready = dependency_depot_ready(target)
            reserve = (
                WARM_PROJECT_RESERVE_GB if dependency_ready else COLD_DEPOT_RESERVE_GB
            )
            with managed_project_session(
                session.project,
                layout,
                policy,
                attach=True,
                reserve_gb=reserve,
                lease_timeout=options.setup_timeout,
                gc_timeout=options.gc_timeout,
            ):
                with claim_dependency_depot(
                    session.project,
                    layout,
                    env=runtime_env,
                    timeout=options.setup_timeout,
                ) as depot:
                    setup_records = bootstrap_lean_workspace(
                        session.project,
                        env=runtime_env,
                        timeout=options.setup_timeout,
                        depot_claim=depot,
                    )
                checkpoint()
                notify("LEAN_BUILD", "Completed Lean dependency setup")
                atomic_write_text(
                    run_directory / "setup.log", command_records_text(setup_records)
                )
                atomic_write_json(
                    run_directory / "setup.json",
                    [serialize_command(record) for record in setup_records],
                )
                failed = next(
                    (
                        record
                        for record in setup_records
                        if record.required and not record.succeeded
                    ),
                    None,
                )
                if failed:
                    raise RuntimeError(
                        f"Required Lean setup failed: {' '.join(failed.argv)}"
                    )
                commit_bootstrap_state(session.project)
                lean_version, baseline_declarations, baseline_process = (
                    run_dependency_extractor(
                        session.project,
                        env=runtime_env,
                        timeout=options.setup_timeout,
                    )
                )
                checkpoint()
                notify(
                    "LEAN_EXTRACTION",
                    "Extracted the baseline Lean dependency graph",
                    declarations=len(baseline_declarations),
                )
                baseline_axioms = {
                    item.name for item in baseline_declarations if item.kind == "axiom"
                }
                environment_hash, environment_inputs = environment_fingerprint(
                    session.project
                )
                atomic_write_json(
                    run_directory / "environment.json",
                    {
                        "schema_version": 1,
                        "hash": environment_hash,
                        "inputs": environment_inputs,
                        "lean_version": lean_version,
                        "mathlib_revision": mathlib_revision(session.project),
                        "native_compiler": compiler,
                    },
                )
                approved_initial = CertificationResult((), (), (), ())
                with StateStore(session.database_path) as store:
                    store.replace_lean_graph(
                        baseline_declarations, run_id=prepared.run_id
                    )
                    reused = list(
                        revalidate_unchanged_certificates(
                            store,
                            run_id=prepared.run_id,
                            snapshot=prepared.snapshot.commit,
                            objects=prepared.objects,
                            declarations=baseline_declarations,
                            environment_hash=environment_hash,
                            lean_version=lean_version,
                            mathlib_revision=mathlib_revision(session.project),
                        )
                    )
                    store.connection.execute(
                        """
                        UPDATE correspondence
                        SET last_updated_run = ?, status = 'approved'
                        WHERE status = 'approved_pending' AND approved = 1
                        """,
                        (prepared.run_id,),
                    )
                    initial_certified: set[str] = set()
                    initial_reconciled: set[str] = set()
                    initial_counterexamples: set[str] = set()
                    while True:
                        current = certify_current_correspondence(
                            store,
                            run_id=prepared.run_id,
                            snapshot=prepared.snapshot.commit,
                            objects=prepared.objects,
                            edges=_edges(store),
                            declarations=baseline_declarations,
                            environment_hash=environment_hash,
                            lean_version=lean_version,
                            mathlib_revision=mathlib_revision(session.project),
                            baseline_project_axioms=baseline_axioms,
                        )
                        new_certified = set(current.certified) - initial_certified
                        initial_certified.update(current.certified)
                        initial_reconciled.update(current.reconciled)
                        initial_counterexamples.update(current.counterexamples)
                        if not new_certified:
                            break
                    approved_initial = CertificationResult(
                        tuple(sorted(initial_certified)),
                        tuple(sorted(initial_reconciled)),
                        tuple(sorted(initial_counterexamples)),
                        (),
                    )

                all_certified: set[str] = set(approved_initial.certified)
                all_reconciled: set[str] = set(approved_initial.reconciled)
                all_counterexamples: set[str] = set(approved_initial.counterexamples)
                provider_errors: list[str] = []
                technical_errors: list[str] = []
                round_index = 0
                while round_index <= len(prepared.selected) + 1:
                    checkpoint()
                    round_index += 1
                    with StateStore(session.database_path) as store:
                        edges = _edges(store)
                        states = _states(store)
                        blockers = {
                            claim_id
                            for claim_id in prepared.selected
                            if states.get(claim_id) == ClaimState.NEEDS_CLARIFICATION
                        }
                        for claim_id in blocked_descendants(
                            blockers,
                            selected=set(prepared.selected),
                            edges=edges,
                        ):
                            if states.get(claim_id) != ClaimState.CERTIFIED:
                                store.set_claim_state(
                                    claim_id,
                                    ClaimState.BLOCKED_DEPENDENCY,
                                    run_id=prepared.run_id,
                                    action="block_dependency",
                                    reason="A manuscript dependency requires clarification",
                                )
                        states = _states(store)
                        ready = ready_frontier(
                            states,
                            selected=set(prepared.selected),
                            edges=edges,
                        )
                        if not ready:
                            break
                        notify(
                            "PROOF_BATCH",
                            "Scheduled the next dependency-ready proof frontier",
                            round=round_index,
                            claims=len(ready),
                        )
                        for claim_id in ready:
                            store.set_claim_state(
                                claim_id,
                                ClaimState.PROVING,
                                run_id=prepared.run_id,
                                action=f"schedule_round_{round_index}",
                                reason="All known manuscript dependencies are certified",
                            )

                    batches = _partition(ready, options.batch_size)
                    jobs: list[BatchJob] = []
                    for batch_index, claims in enumerate(batches, 1):
                        global_index = round_index * 1000 + batch_index
                        worktree = (
                            layout.worktrees
                            / "incremental"
                            / hashlib_sha(session.project)
                            / f"run-{prepared.run_id:06d}"
                            / f"batch-{global_index:04d}"
                        )
                        worktree.parent.mkdir(parents=True, exist_ok=True)
                        _create_worktree(session.project, worktree)
                        jobs.append(
                            BatchJob(
                                index=global_index,
                                project=str(session.project),
                                workspace=str(worktree),
                                run_id=prepared.run_id,
                                snapshot=prepared.snapshot.commit,
                                previous_snapshot=prepared.snapshot.previous_commit,
                                claims=claims,
                                require_correspondence_review=(
                                    prepared.task.policy.require_statement_correspondence_review
                                ),
                                pause_on_ambiguity=(
                                    prepared.task.policy.pause_on_ambiguity
                                ),
                                counterexample_search=(
                                    prepared.task.policy.counterexample_search
                                ),
                                options=options,
                            )
                        )
                    results: list[BatchResult] = []
                    if options.jobs == 1:
                        results = [_run_batch_worker(job) for job in jobs]
                    else:
                        with ProcessPoolExecutor(max_workers=options.jobs) as executor:
                            futures = {
                                executor.submit(_run_batch_worker, job): job
                                for job in jobs
                            }
                            for future in as_completed(futures):
                                results.append(future.result())
                    for result in sorted(results, key=lambda item: item.index):
                        error = _merge_batch(session.project, result)
                        if result.provider_failure:
                            provider_errors.append(result.provider_failure)
                        if error:
                            technical_errors.append(error)
                            with StateStore(session.database_path) as store:
                                for claim_id in result.claims:
                                    row = store.claim_row(claim_id)
                                    if row and row["status"] == ClaimState.PROVING:
                                        store.set_claim_state(
                                            claim_id,
                                            ClaimState.FAILED_TECHNICAL,
                                            run_id=prepared.run_id,
                                            action="batch_failure",
                                            reason=error,
                                        )
                                        store.add_diagnostic(
                                            run_id=prepared.run_id,
                                            claim_id=claim_id,
                                            category=classify_failure(error),
                                            message=error,
                                        )
                        _remove_worktree(session.project, Path(result.workspace))
                    checkpoint()

                    source_mutation = _git(
                        session.project,
                        [
                            "diff",
                            "--name-only",
                            prepared.baseline_commit,
                            "HEAD",
                            "--",
                            "manuscript",
                        ],
                    )
                    if source_mutation:
                        technical_errors.append(
                            "A proof batch modified the immutable manuscript snapshot: "
                            + source_mutation.replace("\n", ", ")
                        )
                        break

                    build = run_command(
                        ("lake", "build"),
                        cwd=session.project,
                        env=runtime_env,
                        timeout=options.setup_timeout,
                    )
                    notify(
                        "LEAN_BUILD",
                        "Independently built the merged proof frontier",
                        round=round_index,
                    )
                    atomic_write_text(
                        run_directory / f"round-{round_index:04d}-build.log",
                        command_records_text([build]),
                    )
                    if not build.succeeded:
                        technical_errors.append(
                            "Independent merged-project Lake build failed"
                        )
                        break
                    lean_version, declarations, _process = run_dependency_extractor(
                        session.project,
                        env=runtime_env,
                        timeout=options.setup_timeout,
                    )
                    environment_hash, _inputs = environment_fingerprint(session.project)
                    with StateStore(session.database_path) as store:
                        notify(
                            "CERTIFICATION",
                            "Checked kernel evidence for the merged proof frontier",
                            round=round_index,
                        )
                        store.replace_lean_graph(declarations, run_id=prepared.run_id)
                        certification = certify_current_correspondence(
                            store,
                            run_id=prepared.run_id,
                            snapshot=prepared.snapshot.commit,
                            objects=prepared.objects,
                            edges=_edges(store),
                            declarations=declarations,
                            environment_hash=environment_hash,
                            lean_version=lean_version,
                            mathlib_revision=mathlib_revision(session.project),
                            baseline_project_axioms=baseline_axioms,
                        )
                        all_certified.update(certification.certified)
                        all_reconciled.update(certification.reconciled)
                        all_counterexamples.update(certification.counterexamples)
                        for claim_id in ready:
                            row = store.claim_row(claim_id)
                            if row and row["status"] == ClaimState.PROVING:
                                store.set_claim_state(
                                    claim_id,
                                    ClaimState.UNRESOLVED,
                                    run_id=prepared.run_id,
                                    action="missing_result",
                                    reason="Proof batch produced no host-valid certificate or clarification",
                                )

                final_build = run_command(
                    ("lake", "build"),
                    cwd=session.project,
                    env=runtime_env,
                    timeout=options.setup_timeout,
                )
                checkpoint()
                notify("LEAN_BUILD", "Completed the final independent Lean build")
                atomic_write_text(
                    run_directory / "final-build.log",
                    command_records_text([final_build]),
                )
                if not final_build.succeeded:
                    technical_errors.append("Final independent Lake build failed")
                lean_version, final_declarations, _process = run_dependency_extractor(
                    session.project,
                    env=runtime_env,
                    timeout=options.setup_timeout,
                )
                with StateStore(session.database_path) as store:
                    store.replace_lean_graph(final_declarations, run_id=prepared.run_id)
                    audit = dependency_audit(
                        store,
                        edges=_edges(store),
                        declarations=final_declarations,
                    )
                    states = _states(store)
                    questions = tuple(
                        str(row["question_id"]) for row in store.open_questions()
                    )
                    target_states = {
                        claim_id: states.get(claim_id, ClaimState.UNRESOLVED)
                        for claim_id in prepared.targets
                    }
                    if provider_errors:
                        outcome, detail, exit_code = (
                            "provider_failure",
                            provider_errors[0],
                            21,
                        )
                    elif technical_errors or not final_build.succeeded:
                        outcome, detail, exit_code = (
                            "lean_infrastructure_failure",
                            technical_errors[0]
                            if technical_errors
                            else "Final build failed",
                            22,
                        )
                    elif any(
                        state == ClaimState.COUNTEREXAMPLE_FOUND
                        for state in target_states.values()
                    ):
                        outcome, detail, exit_code = (
                            "counterexample_found",
                            "A target has a kernel-checked counterexample certificate",
                            12,
                        )
                    elif questions:
                        outcome, detail, exit_code = (
                            "clarification_required",
                            "Author clarification is required; certified work was preserved",
                            10,
                        )
                    elif target_states and all(
                        state == ClaimState.CERTIFIED
                        for state in target_states.values()
                    ):
                        outcome, detail, exit_code = (
                            "verified",
                            "Every selected target has a current Lean certificate",
                            0,
                        )
                    else:
                        outcome, detail, exit_code = (
                            "partial_unresolved",
                            "Verification is incomplete; no falsity conclusion was made",
                            11,
                        )
                    render_report(
                        session.project,
                        store,
                        run_id=prepared.run_id,
                        audit=audit,
                        reused=reused,
                        reconciled=sorted(all_reconciled),
                        invalidated=sorted(prepared.affected),
                    )
                    notify("REPORTING", "Rendered verification reports and exports")
                    store.finish_run(
                        prepared.run_id,
                        status="COMPLETE" if exit_code in {0, 10, 11, 12} else "FAILED",
                        outcome=outcome,
                        completed_at=utc_now(),
                        detail=detail,
                    )
                    session._export_state(
                        store,
                        prepared.objects,
                        _edges(store),
                        prepared.snapshot,
                    )
                    session._write_status_files(store=store)
                payload = {
                    "schema_version": 1,
                    "command": "manuscript verify",
                    "run_id": prepared.run_id,
                    "snapshot": prepared.snapshot.commit,
                    "outcome": outcome,
                    "detail": detail,
                    "exit_code": exit_code,
                    "certified": sorted(all_certified),
                    "reused": sorted(reused),
                    "reconciled": sorted(all_reconciled),
                    "counterexamples": sorted(all_counterexamples),
                    "questions": list(questions),
                }
                atomic_write_json(run_directory / "run.json", payload)
                session._commit_host_changes(
                    f"Record incremental verification run {prepared.run_id:06d}: {outcome}"
                )
                notify("COMPLETE", detail, outcome=outcome, exit_code=exit_code)
                return VerificationResult(
                    outcome=outcome,
                    detail=detail,
                    exit_code=exit_code,
                    run_id=prepared.run_id,
                    snapshot=prepared.snapshot.commit,
                    project=str(session.project),
                    certified=tuple(sorted(all_certified)),
                    reused=tuple(sorted(reused)),
                    reconciled=tuple(sorted(all_reconciled)),
                    questions=questions,
                    counterexamples=tuple(sorted(all_counterexamples)),
                )
        except Exception as exc:
            if prepared is not None:
                with StateStore(session.database_path) as store:
                    store.finish_run(
                        prepared.run_id,
                        status=(
                            "INTERRUPTED"
                            if isinstance(exc, VerificationCancelled)
                            else "FAILED"
                        ),
                        outcome=(
                            "interrupted"
                            if isinstance(exc, VerificationCancelled)
                            else "setup_failure"
                        ),
                        completed_at=utc_now(),
                        detail=f"{type(exc).__name__}: {exc}",
                    )
            raise


def hashlib_sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
