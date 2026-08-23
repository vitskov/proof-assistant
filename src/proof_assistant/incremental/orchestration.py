from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import threading
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
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
from ..concurrency import (
    ConcurrencyRuntime,
    ConcurrencyRuntimeSpec,
    PressureState,
    QueueDepths,
    ResolvedConcurrencyConfig,
    ScheduledTask,
    TelemetryCollector,
    detect_hardware,
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
from .failures import artifact_record, build_failure_report
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
    jobs: int = 2
    batch_size: int = 8
    lean_pool_size: int = 1
    lean_memory_limit_gb: int | None = None
    setup_timeout: float = 1800.0
    request_timeout: float = 120.0
    turn_timeout: float = 3600.0
    gc_timeout: float = 900.0
    concurrency: ConcurrencyRuntimeSpec = field(default_factory=ConcurrencyRuntimeSpec)

    def validate(self) -> None:
        if not self.model:
            raise ValueError("A Codex model is required")
        if not 1 <= self.jobs <= 128:
            raise ValueError("--jobs must be between 1 and 128")
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
    build_returncode: int | None = None
    build_timed_out: bool = False
    build_log: str | None = None


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
    """Cooperative cancellation with durable recovery facts for UI clients."""

    def __init__(
        self,
        message: str = "Verification cancelled",
        *,
        run_id: int | None = None,
        preserved_certificates: tuple[str, ...] = (),
        retryable_claims: tuple[str, ...] = (),
        temporary_worktrees_cleaned: bool = True,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.preserved_certificates = preserved_certificates
        self.retryable_claims = retryable_claims
        self.temporary_worktrees_cleaned = temporary_worktrees_cleaned

    def record_recovery(
        self,
        *,
        run_id: int,
        preserved_certificates: Sequence[str],
        retryable_claims: Sequence[str],
        temporary_worktrees_cleaned: bool,
    ) -> None:
        self.run_id = run_id
        self.preserved_certificates = tuple(sorted(preserved_certificates))
        self.retryable_claims = tuple(sorted(retryable_claims))
        self.temporary_worktrees_cleaned = temporary_worktrees_cleaned


class _ConcurrencyMonitor:
    """Low-cost run telemetry feeding the same machine-global controllers.

    A verification process can outlive the TUI process that started it.  The
    machine settings file is therefore the authority for every sample, rather
    than the ``ResolvedConcurrencyConfig`` captured when the run began.  A
    changed configuration is applied before telemetry can adapt a controller;
    if refreshing fails, the whole sample is skipped so stale bounds cannot be
    used to resize shared machine capacity.
    """

    def __init__(
        self,
        runtime: ConcurrencyRuntime,
        spec: ConcurrencyRuntimeSpec,
        *,
        resolver: Callable[[], ResolvedConcurrencyConfig] | None = None,
    ) -> None:
        self.runtime = runtime
        self.spec = spec
        self._resolver = resolver or spec.resolve
        self.enabled = runtime.resolved.config.telemetry_enabled
        self.collector = TelemetryCollector()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.samples = 0
        self.config_refreshes = 0
        self.config_refresh_failures = 0
        self.peak_active = {"ai": 0, "lean": 0, "build": 0}
        self.sum_active = {"ai": 0, "lean": 0, "build": 0}
        self.peak_queued = {"ai": 0, "lean": 0, "build": 0}
        self.pressure_events: list[dict[str, object]] = []
        self.latest: dict[str, object] = {}

    def _refresh_runtime(self) -> None:
        try:
            resolved = self._resolver()
            current = self.runtime.resolved
            if (
                resolved.machine_revision != current.machine_revision
                or resolved.config != current.config
            ):
                self.runtime.apply_resolved(resolved)
                with self._lock:
                    self.config_refreshes += 1
            self.enabled = resolved.config.telemetry_enabled
        except Exception:
            with self._lock:
                self.config_refresh_failures += 1
            raise

    def _sample(self) -> None:
        # Reload first.  In particular, never let a detached process adapt
        # limits from an old machine-settings revision.
        self._refresh_runtime()
        ai = self.runtime.ai.status()
        lean = self.runtime.lean.status()
        build = self.runtime.build.status()
        telemetry = self.collector.sample(
            queues=QueueDepths(ai=ai.queued, lean=lean.queued, build=build.queued)
        )
        allocation = detect_hardware()
        total = min(telemetry.total_memory_bytes, allocation.total_memory_bytes)
        available = min(
            telemetry.available_memory_bytes,
            allocation.available_memory_bytes,
            total,
        )
        ratio = available / max(1, total)
        pressure = telemetry.pressure
        if ratio < 0.08:
            pressure = PressureState.EMERGENCY
        elif ratio < 0.15:
            pressure = PressureState.RED
        elif ratio <= 0.30 and pressure == PressureState.GREEN:
            pressure = PressureState.YELLOW
        telemetry = replace(
            telemetry,
            total_memory_bytes=total,
            available_memory_bytes=available,
            available_memory_ratio=ratio,
            pressure=pressure,
        )
        if self.enabled:
            self.runtime.observe_telemetry(telemetry)
        current_active = {"ai": ai.active, "lean": lean.active, "build": build.active}
        current_queued = {"ai": ai.queued, "lean": lean.queued, "build": build.queued}
        with self._lock:
            self.samples += 1
            for name in self.peak_active:
                self.peak_active[name] = max(
                    self.peak_active[name], current_active[name]
                )
                self.sum_active[name] += current_active[name]
                self.peak_queued[name] = max(
                    self.peak_queued[name], current_queued[name]
                )
            if telemetry.pressure.value != "green":
                self.pressure_events.append(
                    {
                        "sample": self.samples,
                        "state": telemetry.pressure.value,
                        "available_memory_ratio": telemetry.available_memory_ratio,
                        "swap_rate_bytes_per_second": (
                            telemetry.swap_rate_bytes_per_second
                        ),
                    }
                )
                self.pressure_events = self.pressure_events[-100:]
            self.latest = {
                "cpu_percent": telemetry.cpu_percent,
                "available_memory_ratio": telemetry.available_memory_ratio,
                "swap_used_bytes": telemetry.swap_used_bytes,
                "swap_rate_bytes_per_second": telemetry.swap_rate_bytes_per_second,
                "disk_iowait_percent": telemetry.disk_iowait_percent,
                "pressure": telemetry.pressure.value,
                "ai_throttles": ai.throttles,
                "ai_transient_failures": ai.transient_failures,
                "ai_backoff_until": ai.backoff_until,
                "lean_pressure": lean.pressure,
                "build_pressure": build.pressure,
            }

    def _run(self) -> None:
        while not self.stop.wait(5.0):
            try:
                self._sample()
            except Exception:
                # Telemetry may disappear on constrained hosts. Verification
                # remains correct and the next sample retries independently.
                pass

    def start(self) -> None:
        try:
            self._sample()
        except Exception:
            pass
        self.thread = threading.Thread(
            target=self._run,
            name="proof-assistant-resource-monitor",
            daemon=True,
        )
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=6.0)

    def provenance(self) -> dict[str, object]:
        with self._lock:
            return {
                "samples": self.samples,
                "machine_revision": self.runtime.resolved.machine_revision,
                "telemetry_enabled": self.enabled,
                "config_refreshes": self.config_refreshes,
                "config_refresh_failures": self.config_refresh_failures,
                "peak_active": dict(self.peak_active),
                "mean_active": {
                    name: (
                        self.sum_active[name] / self.samples if self.samples else 0.0
                    )
                    for name in self.sum_active
                },
                "peak_queued": dict(self.peak_queued),
                "pressure_events": list(self.pressure_events),
                "latest": dict(self.latest),
            }


@contextmanager
def _monitor_concurrency(runtime: ConcurrencyRuntime, spec: ConcurrencyRuntimeSpec):
    monitor = _ConcurrencyMonitor(runtime, spec)
    monitor.start()
    try:
        yield monitor
    finally:
        monitor.close()


def _run_lake_build(
    runtime: ConcurrencyRuntime,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    owner: str,
    full_build: bool,
):
    request = runtime.build.request(
        owner,
        full_build=full_build,
        ttl_seconds=max(120.0, min(timeout, 900.0)),
    )
    with runtime.build.lease(request, timeout=timeout):
        return run_command(argv, cwd=cwd, env=env, timeout=timeout)


def _run_lean_operation(
    runtime: ConcurrencyRuntime,
    operation: Callable[[], object],
    *,
    owner: str,
    timeout: float,
):
    request = runtime.lean.request(
        owner,
        ttl_seconds=max(120.0, min(timeout, 600.0)),
    )
    with runtime.lean.lease(request, timeout=timeout):
        return operation()


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


def _repoprover_preflight() -> str | None:
    """Import worker dependencies before attributing failures to claims."""

    try:
        importlib.import_module("repoprover.agents.contributor")
        importlib.import_module("repoprover.agents.lean_tools")
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


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
        concurrency = job.options.concurrency.create()
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
                setup_request = concurrency.build.request(
                    f"batch-bootstrap:{job.run_id}:{job.index}",
                    full_build=True,
                    ttl_seconds=max(120.0, min(job.options.setup_timeout, 900.0)),
                )
                with concurrency.build.lease(
                    setup_request,
                    timeout=job.options.setup_timeout,
                ):
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
                run_directory = (
                    project
                    / ".repoprover"
                    / "runs"
                    / f"{job.run_id:06d}"
                    / f"batch-{job.index:04d}"
                )
                write_batch_context(
                    workspace,
                    run_id=job.run_id,
                    snapshot=job.snapshot,
                    claims=job.claims,
                    pause_on_ambiguity=job.pause_on_ambiguity,
                    counterexample_search=job.counterexample_search,
                    concurrency=concurrency.provenance(),
                    admission_timeout=job.options.turn_timeout,
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
                    concurrency=job.options.concurrency,
                    admission_timeout=job.options.turn_timeout,
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
                                concurrency=job.options.concurrency,
                            ),
                        )
                        final_text = wrapped.codex.final_text
                        thread_id = wrapped.codex.thread_id
                        turn_id = wrapped.codex.turn_id
                        tool_count = len(wrapped.codex.tool_calls)
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
                build = _run_lake_build(
                    concurrency,
                    ("lake", "build", "ManuscriptVerification"),
                    cwd=workspace,
                    env=runtime_env,
                    timeout=job.options.setup_timeout,
                    owner=f"batch-final:{job.run_id}:{job.index}",
                    full_build=False,
                )
                build_log = run_directory / "build.log"
                atomic_write_text(build_log, command_records_text([build]))
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
                    None,
                    build.returncode,
                    build.returncode == 124,
                    str(build_log),
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


def _preflight_proof_batches(
    store: StateStore,
    *,
    run_id: int,
    selected: set[str],
) -> str | None:
    """Validate RepoProver once, then make prior technical failures retryable."""

    states = _states(store)
    edges = _edges(store)
    needs_agent = bool(ready_frontier(states, selected=selected, edges=edges)) or any(
        states.get(claim_id) == ClaimState.FAILED_TECHNICAL for claim_id in selected
    )
    if not needs_agent:
        return None
    repoprover_error = _repoprover_preflight()
    if repoprover_error is not None:
        detail = (
            "RepoProver host preflight failed before scheduling proof claims: "
            f"{repoprover_error}"
        )
        store.add_failure_incident(
            run_id=run_id,
            scope="RUN",
            failure_kind="INFRASTRUCTURE",
            phase="PROOF_PREFLIGHT",
            category="repoprover_import",
            message=detail,
            provenance="orchestration.repoprover_preflight",
            retryable=True,
        )
        return detail
    for claim_id in sorted(selected):
        row = store.claim_row(claim_id)
        if row is None or str(row["status"]) != str(ClaimState.FAILED_TECHNICAL):
            continue
        store.set_claim_state(
            claim_id,
            ClaimState.INVALIDATED,
            run_id=run_id,
            action="retry_technical_failure",
            reason=(
                "RepoProver host preflight succeeded; retrying a prior technical failure"
            ),
        )
    return None


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


def _remove_worktree(project: Path, workspace: Path) -> bool:
    try:
        _git(project, ["worktree", "remove", "--force", str(workspace)], check=False)
    except Exception:
        pass
    shutil.rmtree(workspace, ignore_errors=True)
    try:
        _git(project, ["worktree", "prune"], check=False)
    except Exception:
        pass
    return not workspace.exists()


def _execute_batch_round(
    project: Path,
    jobs: Sequence[BatchJob],
    *,
    max_workers: int,
    checkpoint: Callable[[], None],
) -> list[tuple[BatchResult, str | None]]:
    """Run, boundary-check, and merge one proof frontier with assured cleanup.

    Cancellation is observed after all workers have stopped but *before* any
    candidate commit is merged into the authoritative project.  Once merging
    begins there is intentionally no cancellation checkpoint: the caller must
    finish the independent build and kernel-certification boundary before it
    observes cancellation again.
    """
    results: list[BatchResult] = []
    try:
        if max_workers == 1:
            results = [_run_batch_worker(job) for job in jobs]
        else:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_run_batch_worker, job): job for job in jobs}
                for future in as_completed(futures):
                    results.append(future.result())
        checkpoint()
        return [
            (result, _merge_batch(project, result))
            for result in sorted(results, key=lambda item: item.index)
        ]
    finally:
        for job in jobs:
            _remove_worktree(project, Path(job.workspace))


def _cleanup_run_worktrees(project: Path, layout: CacheLayout, *, run_id: int) -> bool:
    """Best-effort recovery sweep for every temporary worktree in one run."""
    root = layout.worktrees / "incremental" / hashlib_sha(project) / f"run-{run_id:06d}"
    if root.is_dir():
        try:
            workspaces = sorted(root.iterdir())
        except OSError:
            workspaces = []
        for workspace in workspaces:
            if workspace.is_dir():
                _remove_worktree(project, workspace)
        shutil.rmtree(root, ignore_errors=True)
    try:
        _git(project, ["worktree", "prune"], check=False)
    except Exception:
        pass
    return not root.exists()


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

    def notify(
        phase: str,
        message: str,
        details: dict[str, object] | None = None,
        **extra_details: object,
    ) -> None:
        if event_hook is not None:
            event_hook(phase, message, {**(details or {}), **extra_details})

    options.validate()
    checkpoint()
    notify("VALIDATING", "Validated verification options and project location")
    layout = CacheLayout.discover(options.cache_home)
    ensure_project_outside_dropbox(session.project, layout)
    concurrency = options.concurrency.create()
    with (
        _monitor_concurrency(concurrency, options.concurrency) as concurrency_monitor,
        project_lock(session.project, exclusive=True),
    ):
        prepared: PreparedPass | None = None
        try:
            runtime_env, compiler = _runtime_environment(layout)
            notify(
                "VALIDATING",
                "Validated the Lean runtime and native compiler",
                compiler=compiler,
            )
            provisional_environment, _inputs = environment_fingerprint(session.project)
            prepared = session.prepare_pass(
                manuscript=manuscript,
                task_file=task_file,
                environment_hash=provisional_environment,
                expected_inventory_sha256=expected_inventory_sha256,
                event_hook=notify,
                _already_locked=True,
            )
            initial_concurrency = concurrency.provenance()
            with StateStore(session.database_path) as store:
                store.record_run_concurrency(
                    prepared.run_id,
                    configured=initial_concurrency["configured"],
                    initial_effective=initial_concurrency["effective"],
                    telemetry=concurrency_monitor.provenance(),
                )
            checkpoint()
            notify(
                "CACHE_SETUP",
                "Preparing the shared Lean cache and dependency depot",
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
                    setup_request = concurrency.build.request(
                        f"run-bootstrap:{prepared.run_id}",
                        full_build=True,
                        ttl_seconds=max(120.0, min(options.setup_timeout, 900.0)),
                    )
                    with concurrency.build.lease(
                        setup_request,
                        timeout=options.setup_timeout,
                    ):
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
                    setup_detail = (
                        f"Required Lean setup failed: {' '.join(failed.argv)}"
                    )
                    with StateStore(session.database_path) as store:
                        store.add_failure_incident(
                            run_id=prepared.run_id,
                            scope="RUN",
                            failure_kind="INFRASTRUCTURE",
                            phase="CACHE_SETUP",
                            category="lean_setup",
                            message=setup_detail,
                            detail=(failed.stderr or failed.stdout or None),
                            provenance="orchestration.bootstrap_lean_workspace",
                            retryable=True,
                            artifacts=(
                                artifact_record(
                                    run_directory / "setup.log",
                                    label="Lean setup log",
                                    command=failed.argv,
                                    exit_code=failed.returncode,
                                    timed_out=failed.returncode == 124,
                                ),
                            ),
                        )
                    raise RuntimeError(setup_detail)
                commit_bootstrap_state(session.project)
                lean_version, baseline_declarations, baseline_process = (
                    _run_lean_operation(
                        concurrency,
                        lambda: run_dependency_extractor(
                            session.project,
                            env=runtime_env,
                            timeout=options.setup_timeout,
                        ),
                        owner=f"dependency-extractor-baseline:{prepared.run_id}",
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
                        "concurrency": initial_concurrency,
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
                with StateStore(session.database_path) as store:
                    repoprover_error = _preflight_proof_batches(
                        store,
                        run_id=prepared.run_id,
                        selected=set(prepared.selected),
                    )
                    if repoprover_error is not None:
                        technical_errors.append(repoprover_error)
                round_index = 0
                while round_index <= len(prepared.selected) + 1:
                    checkpoint()
                    if technical_errors or provider_errors:
                        break
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
                        if (
                            ready
                            and concurrency.resolved.config.scheduler.dependency_priority
                        ):
                            dependencies = {
                                claim_id: tuple(
                                    edge.dst for edge in edges if edge.src == claim_id
                                )
                                for claim_id in prepared.selected
                            }
                            blocked_requested = {
                                claim_id
                                for claim_id in prepared.selected
                                if states.get(claim_id) != ClaimState.CERTIFIED
                            }
                            ready = tuple(
                                task.claim_id
                                for task in concurrency.scheduler.prioritize(
                                    (
                                        ScheduledTask(claim_id, ready_order=index)
                                        for index, claim_id in enumerate(ready)
                                    ),
                                    requested=prepared.targets,
                                    dependencies=dependencies,
                                    blocked=blocked_requested,
                                )
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
                    merged_results = _execute_batch_round(
                        session.project,
                        jobs,
                        # Logical worker processes are distinct from active
                        # remote turns. The legacy setting remains a minimum
                        # compatibility fan-out; the machine AI controller is
                        # the authoritative active-turn budget.
                        max_workers=min(
                            len(jobs),
                            max(options.jobs, concurrency.ai.status().current_limit),
                        ),
                        checkpoint=checkpoint,
                    )
                    for result, error in merged_results:
                        artifacts = (
                            (
                                artifact_record(
                                    Path(result.build_log),
                                    label="Batch Lean build log",
                                    command=("lake", "build"),
                                    exit_code=result.build_returncode,
                                    timed_out=result.build_timed_out,
                                )
                            )
                            if result.build_log
                            else ()
                        )
                        if result.provider_failure:
                            provider_errors.append(result.provider_failure)
                            with StateStore(session.database_path) as store:
                                store.add_failure_incident(
                                    run_id=prepared.run_id,
                                    scope="BATCH",
                                    failure_kind="PROVIDER",
                                    phase="PROOF_BATCH",
                                    category="provider_failure",
                                    message=result.provider_failure,
                                    provenance="orchestration.batch_provider",
                                    claim_ids=result.claims,
                                    batch_index=result.index,
                                    retryable=True,
                                    artifacts=artifacts,
                                )
                        if error:
                            provider_only = bool(
                                result.provider_failure
                                and error == result.provider_failure
                            )
                            if not provider_only:
                                technical_errors.append(error)
                            with StateStore(session.database_path) as store:
                                if not provider_only:
                                    store.add_failure_incident(
                                        run_id=prepared.run_id,
                                        scope="BATCH",
                                        failure_kind="BATCH_TECHNICAL",
                                        phase="PROOF_BATCH",
                                        category=classify_failure(error),
                                        message=error,
                                        provenance="orchestration.batch_boundary",
                                        claim_ids=result.claims,
                                        batch_index=result.index,
                                        retryable=True,
                                        artifacts=artifacts,
                                    )
                                for claim_id in result.claims:
                                    row = store.claim_row(claim_id)
                                    if row and row["status"] == ClaimState.PROVING:
                                        store.set_claim_state(
                                            claim_id,
                                            (
                                                ClaimState.INVALIDATED
                                                if provider_only
                                                else ClaimState.FAILED_TECHNICAL
                                            ),
                                            run_id=prepared.run_id,
                                            action=(
                                                "provider_failure_retry"
                                                if provider_only
                                                else "batch_failure"
                                            ),
                                            reason=error,
                                        )
                                        if not provider_only:
                                            store.add_diagnostic(
                                                run_id=prepared.run_id,
                                                claim_id=claim_id,
                                                category=classify_failure(error),
                                                message=error,
                                            )
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
                        source_detail = (
                            "A proof batch modified the immutable manuscript snapshot: "
                            + source_mutation.replace("\n", ", ")
                        )
                        technical_errors.append(source_detail)
                        source_log = run_directory / "source-integrity.log"
                        atomic_write_text(source_log, source_detail + "\n")
                        with StateStore(session.database_path) as store:
                            store.add_failure_incident(
                                run_id=prepared.run_id,
                                scope="RUN",
                                failure_kind="SOURCE_INTEGRITY",
                                phase="PROOF_BATCH",
                                category="immutable_source_mutation",
                                message=source_detail,
                                provenance="orchestration.source_boundary",
                                retryable=False,
                                artifacts=(
                                    artifact_record(
                                        source_log,
                                        label="Immutable source boundary violation",
                                    ),
                                ),
                            )
                        break

                    build = _run_lake_build(
                        concurrency,
                        ("lake", "build", "ManuscriptVerification"),
                        cwd=session.project,
                        env=runtime_env,
                        timeout=options.setup_timeout,
                        owner=f"merged-frontier:{prepared.run_id}:{round_index}",
                        full_build=False,
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
                        build_detail = "Independent merged-project Lake build failed"
                        technical_errors.append(build_detail)
                        with StateStore(session.database_path) as store:
                            store.add_failure_incident(
                                run_id=prepared.run_id,
                                scope="RUN",
                                failure_kind="INFRASTRUCTURE",
                                phase="LEAN_BUILD",
                                category="merged_build_failure",
                                message=build_detail,
                                detail=build.stderr or build.stdout or None,
                                provenance="orchestration.merged_build",
                                retryable=True,
                                artifacts=(
                                    artifact_record(
                                        run_directory
                                        / f"round-{round_index:04d}-build.log",
                                        label="Merged-project Lean build log",
                                        command=build.argv,
                                        exit_code=build.returncode,
                                        timed_out=build.returncode == 124,
                                    ),
                                ),
                            )
                        break
                    lean_version, declarations, _process = _run_lean_operation(
                        concurrency,
                        lambda: run_dependency_extractor(
                            session.project,
                            env=runtime_env,
                            timeout=options.setup_timeout,
                        ),
                        owner=(
                            f"dependency-extractor-round:{prepared.run_id}:"
                            f"{round_index}"
                        ),
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

                final_build = _run_lake_build(
                    concurrency,
                    ("lake", "build"),
                    cwd=session.project,
                    env=runtime_env,
                    timeout=options.setup_timeout,
                    owner=f"final-certification:{prepared.run_id}",
                    full_build=True,
                )
                checkpoint()
                notify("LEAN_BUILD", "Completed the final independent Lean build")
                atomic_write_text(
                    run_directory / "final-build.log",
                    command_records_text([final_build]),
                )
                if not final_build.succeeded:
                    final_detail = "Final independent Lake build failed"
                    technical_errors.append(final_detail)
                    with StateStore(session.database_path) as store:
                        store.add_failure_incident(
                            run_id=prepared.run_id,
                            scope="RUN",
                            failure_kind="INFRASTRUCTURE",
                            phase="LEAN_BUILD",
                            category="final_build_failure",
                            message=final_detail,
                            detail=final_build.stderr or final_build.stdout or None,
                            provenance="orchestration.final_build",
                            retryable=True,
                            artifacts=(
                                artifact_record(
                                    run_directory / "final-build.log",
                                    label="Final independent Lean build log",
                                    command=final_build.argv,
                                    exit_code=final_build.returncode,
                                    timed_out=final_build.returncode == 124,
                                ),
                            ),
                        )
                lean_version, final_declarations, _process = _run_lean_operation(
                    concurrency,
                    lambda: run_dependency_extractor(
                        session.project,
                        env=runtime_env,
                        timeout=options.setup_timeout,
                    ),
                    owner=f"dependency-extractor-final:{prepared.run_id}",
                    timeout=options.setup_timeout,
                )
                with StateStore(session.database_path) as store:
                    store.replace_lean_graph(final_declarations, run_id=prepared.run_id)
                    final_edges = _edges(store)
                    store.replace_run_dependency_edges(prepared.run_id, final_edges)
                    audit = dependency_audit(
                        store,
                        edges=final_edges,
                        declarations=final_declarations,
                    )
                    if provider_errors or technical_errors or not final_build.succeeded:
                        store.reset_in_flight_claims(
                            run_id=prepared.run_id,
                            action="failed_run_retry",
                            reason=(
                                "Verification ended before certification; proof "
                                "remains retryable"
                            ),
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
                    final_concurrency = concurrency.provenance()
                    store.finish_run_concurrency(
                        prepared.run_id,
                        final_effective=final_concurrency["effective"],
                        telemetry=concurrency_monitor.provenance(),
                    )
                    store.finish_run(
                        prepared.run_id,
                        status="COMPLETE" if exit_code in {0, 10, 11, 12} else "FAILED",
                        outcome=outcome,
                        completed_at=utc_now(),
                        detail=detail,
                    )
                    store.record_run_claim_nodes(prepared.run_id)
                    failure_report = build_failure_report(
                        session.project, store, prepared.run_id
                    )
                    render_report(
                        session.project,
                        store,
                        run_id=prepared.run_id,
                        audit=audit,
                        reused=reused,
                        reconciled=sorted(all_reconciled),
                        invalidated=sorted(prepared.affected),
                        failure_report=failure_report,
                    )
                    notify("REPORTING", "Rendered verification reports and exports")
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
                    "concurrency": {
                        **final_concurrency,
                        "telemetry": concurrency_monitor.provenance(),
                    },
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
                cancelled = isinstance(exc, VerificationCancelled)
                temporary_worktrees_cleaned = _cleanup_run_worktrees(
                    session.project, layout, run_id=prepared.run_id
                )
                exception_artifacts: tuple[dict[str, object], ...] = ()
                if not cancelled:
                    exception_log = (
                        session.runs / f"{prepared.run_id:06d}" / "exception.log"
                    )
                    try:
                        atomic_write_text(exception_log, traceback.format_exc())
                        exception_artifacts = (
                            artifact_record(
                                exception_log,
                                label="Unhandled verification exception",
                            ),
                        )
                    except OSError:
                        pass
                with StateStore(session.database_path) as store:
                    with store.transaction() as connection:
                        retryable_claims = store.reset_in_flight_claims(
                            run_id=prepared.run_id,
                            action=(
                                "cancel_retry" if cancelled else "failed_run_retry"
                            ),
                            reason=(
                                "Verification was safely cancelled; proof must be retried"
                                if cancelled
                                else "Verification failed before certification; proof must be retried"
                            ),
                            connection=connection,
                        )
                        store.finish_run(
                            prepared.run_id,
                            status="INTERRUPTED" if cancelled else "FAILED",
                            outcome="interrupted" if cancelled else "setup_failure",
                            completed_at=utc_now(),
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                        failed_concurrency = concurrency.provenance()
                        store.finish_run_concurrency(
                            prepared.run_id,
                            final_effective=failed_concurrency["effective"],
                            telemetry=concurrency_monitor.provenance(),
                        )
                    if not cancelled:
                        store.add_failure_incident(
                            run_id=prepared.run_id,
                            scope="RUN",
                            failure_kind="INFRASTRUCTURE",
                            phase="ORCHESTRATION",
                            category=type(exc).__name__,
                            message=str(exc) or type(exc).__name__,
                            detail=f"{type(exc).__name__}: {exc}",
                            provenance="orchestration.unhandled_exception",
                            retryable=True,
                            artifacts=exception_artifacts,
                        )
                    store.replace_run_dependency_edges(prepared.run_id, _edges(store))
                    store.record_run_claim_nodes(prepared.run_id)
                    if not cancelled:
                        failure_report = build_failure_report(
                            session.project, store, prepared.run_id
                        )
                        try:
                            render_report(
                                session.project,
                                store,
                                run_id=prepared.run_id,
                                audit={
                                    "schema_version": 1,
                                    "discrepancies": [],
                                    "unavailable": (
                                        "Verification ended before the final "
                                        "dependency audit"
                                    ),
                                },
                                reused=(),
                                reconciled=(),
                                invalidated=sorted(prepared.affected),
                                failure_report=failure_report,
                            )
                            atomic_write_json(
                                session.runs / f"{prepared.run_id:06d}" / "run.json",
                                {
                                    "schema_version": 2,
                                    "command": "manuscript verify",
                                    "run_id": prepared.run_id,
                                    "snapshot": prepared.snapshot.commit,
                                    "outcome": "setup_failure",
                                    "detail": f"{type(exc).__name__}: {exc}",
                                    "exit_code": 22,
                                    "failure_incidents": [
                                        incident.incident_id
                                        for incident in (
                                            failure_report.incidents
                                            if failure_report is not None
                                            else ()
                                        )
                                    ],
                                    "concurrency": {
                                        **failed_concurrency,
                                        "telemetry": concurrency_monitor.provenance(),
                                    },
                                },
                            )
                        except Exception:
                            # SQLite remains authoritative when secondary report
                            # rendering is itself blocked (for example disk full).
                            pass
                    preserved_certificates = tuple(
                        sorted(str(row["claim_id"]) for row in store.certificate_rows())
                    )
                    try:
                        session._export_state(
                            store,
                            prepared.objects,
                            _edges(store),
                            prepared.snapshot,
                        )
                        session._write_status_files(store=store)
                    except Exception:
                        # Recovery state is authoritative in SQLite even when a
                        # secondary human-readable export cannot be refreshed.
                        pass
                if cancelled:
                    cancellation = exc
                    assert isinstance(cancellation, VerificationCancelled)
                    cancellation.record_recovery(
                        run_id=prepared.run_id,
                        preserved_certificates=preserved_certificates,
                        retryable_claims=retryable_claims,
                        temporary_worktrees_cleaned=temporary_worktrees_cleaned,
                    )
                    try:
                        atomic_write_json(
                            session.runs / f"{prepared.run_id:06d}" / "run.json",
                            {
                                "schema_version": 1,
                                "command": "manuscript verify",
                                "run_id": prepared.run_id,
                                "snapshot": prepared.snapshot.commit,
                                "outcome": "interrupted",
                                "detail": str(cancellation),
                                "preserved_certificates": list(
                                    cancellation.preserved_certificates
                                ),
                                "retryable_claims": list(cancellation.retryable_claims),
                                "temporary_worktrees_cleaned": (
                                    cancellation.temporary_worktrees_cleaned
                                ),
                                "concurrency": {
                                    **failed_concurrency,
                                    "telemetry": concurrency_monitor.provenance(),
                                },
                            },
                        )
                    except Exception:
                        # SQLite recovery and the typed exception still carry
                        # the interruption facts if the filesystem is full.
                        pass
                    try:
                        session._commit_host_changes(
                            "Record safely interrupted verification run "
                            f"{prepared.run_id:06d}"
                        )
                    except Exception:
                        # SQLite and the atomic run artifact already contain
                        # the durable interruption result.
                        pass
            raise


def hashlib_sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
