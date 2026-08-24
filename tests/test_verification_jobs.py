from __future__ import annotations

import inspect
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from proof_assistant.cli import build_parser
from proof_assistant.incremental.locking import (
    acquire_worker_lease,
    project_lock,
    release_worker_lease,
    worker_lease_active,
)
from proof_assistant.incremental.orchestration import VerifyOptions
from proof_assistant.incremental.store import StateStore
from proof_assistant.workflow.contracts import (
    NewProjectRequest,
    ProgressEvent,
    ProgressPhase,
    VerificationJobState,
    VerificationSettings,
    WorkflowServiceContract,
    WorkflowState,
)
from proof_assistant.workflow.jobs import VerificationJobStore
from proof_assistant.workflow.service import (
    ProofAssistantWorkflow,
    VerificationJobConflictError,
    VerificationJobNotCancellableError,
)
from proof_assistant.workspace.management import ProjectManager


def _workflow(tmp_path: Path) -> tuple[ProofAssistantWorkflow, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        r"\documentclass{article}"
        r"\newtheorem{theorem}{Theorem}"
        r"\begin{document}"
        r"\begin{theorem}\label{one}One.\end{theorem}"
        r"\end{document}",
        encoding="utf-8",
    )
    service = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog",
        machine_config_path=tmp_path / "settings.yaml",
        use_codex_clarification=False,
    )
    # These tests exercise detached-worker and mutation-lease behavior, not the
    # host's default recoverable-trash mount. Keep both paths on pytest's
    # temporary filesystem so a split /tmp and $HOME layout cannot mask the
    # lease state with an unrelated cross-filesystem deletion refusal.
    service.projects = ProjectManager(
        service.catalog,
        trash_root=tmp_path / "recoverable-trash",
    )
    project = tmp_path / "project"
    service.create_project(
        NewProjectRequest("Project", source, "main.tex", project_path=project)
    )
    return service, project


class _FakeDetachedProcesses:
    def __init__(self) -> None:
        self.descriptors: list[int] = []
        self.commands: list[tuple[str, ...]] = []
        self._lock = threading.Lock()

    def popen(self, command, **kwargs):
        assert "shell" not in kwargs
        descriptor = os.dup(kwargs["pass_fds"][0])
        with self._lock:
            self.descriptors.append(descriptor)
            self.commands.append(tuple(command))
        return SimpleNamespace(pid=4321)

    def close(self) -> None:
        while self.descriptors:
            os.close(self.descriptors.pop())


def test_start_is_detached_idempotent_and_conflicts_on_different_request(
    tmp_path, monkeypatch
):
    credential_sentinel = "must-not-appear-in-persisted-worker-command"
    monkeypatch.setenv("OPENAI_API_KEY", credential_sentinel)
    service, project = _workflow(tmp_path)
    processes = _FakeDetachedProcesses()
    monkeypatch.setattr(service, "plan_changes", lambda _project: None)
    monkeypatch.setattr(
        "proof_assistant.workflow.service.subprocess.Popen", processes.popen
    )
    try:
        first = service.start_verification(
            project, None, VerificationSettings(model="test")
        )
        replacement_client = ProofAssistantWorkflow(
            catalog_root=tmp_path / "catalog", use_codex_clarification=False
        )
        attached = replacement_client.start_verification(
            project, None, VerificationSettings(model="test")
        )

        assert first.started is True
        assert first.attached is False
        assert first.job.state == VerificationJobState.STARTING
        assert attached.started is False
        assert attached.attached is True
        assert attached.job.job_id == first.job.job_id
        assert len(processes.commands) == 1
        command = processes.commands[0]
        assert command[:3] == (os.sys.executable, "-m", "proof_assistant")
        assert "_project-worker" in command
        catalog_index = command.index("--catalog-file")
        assert Path(command[catalog_index + 1]) == service.catalog.path
        settings_index = command.index("--machine-config-file")
        assert Path(command[settings_index + 1]) == service._machine_config_store.path
        assert first.job.launch_command == command
        assert credential_sentinel not in command
        assert credential_sentinel not in first.job.launch_command
        assert first.job.worker_log_path is not None

        with pytest.raises(VerificationJobConflictError) as raised:
            service.start_verification(
                project, None, VerificationSettings(model="different")
            )
        assert raised.value.observation.job.job_id == first.job.job_id
    finally:
        processes.close()


def test_hidden_worker_cli_consumes_explicit_state_paths(tmp_path, monkeypatch):
    captured = {}

    class FakeWorkflow:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def _run_verification_job(self, project, job_id, lease_fd):
            captured["call"] = (project, job_id, lease_fd)
            return 17

    monkeypatch.setattr(
        "proof_assistant.workflow.service.ProofAssistantWorkflow", FakeWorkflow
    )
    catalog = tmp_path / "catalog" / "projects.json"
    settings = tmp_path / "config" / "settings.yaml"
    project = tmp_path / "project"
    args = build_parser().parse_args(
        [
            "_project-worker",
            "--project",
            str(project),
            "--job-id",
            "job-1",
            "--lease-fd",
            "19",
            "--catalog-file",
            str(catalog),
            "--machine-config-file",
            str(settings),
        ]
    )

    assert args.func(args) == 17
    assert captured["kwargs"]["catalog_root"] == catalog.resolve()
    assert captured["kwargs"]["machine_config_path"] == settings.resolve()
    assert captured["call"] == (project.resolve(), "job-1", 19)


def test_concurrent_equivalent_submissions_spawn_exactly_one_worker(
    tmp_path, monkeypatch
):
    service, project = _workflow(tmp_path)
    processes = _FakeDetachedProcesses()
    monkeypatch.setattr(service, "plan_changes", lambda _project: None)
    monkeypatch.setattr(
        "proof_assistant.workflow.service.subprocess.Popen", processes.popen
    )
    settings = VerificationSettings(model="test")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            observations = tuple(
                pool.map(
                    lambda _index: service.start_verification(project, None, settings),
                    range(2),
                )
            )
        assert len({item.job.job_id for item in observations}) == 1
        assert sum(item.started for item in observations) == 1
        assert len(processes.commands) == 1
    finally:
        processes.close()


def test_progress_cursor_cancel_and_crash_reconciliation_are_durable(
    tmp_path, monkeypatch
):
    service, project = _workflow(tmp_path)
    processes = _FakeDetachedProcesses()
    monkeypatch.setattr(service, "plan_changes", lambda _project: None)
    monkeypatch.setattr(
        "proof_assistant.workflow.service.subprocess.Popen", processes.popen
    )
    started = service.start_verification(
        project, None, VerificationSettings(model="test")
    )
    store = VerificationJobStore(project)
    stored = store.append_event(
        started.job.job_id,
        ProgressEvent(99, ProgressPhase.INDEXING, "Indexed selected sources", 1, 3),
    )

    replay = service.observe_verification(project, after_sequence=0)
    assert replay is not None
    assert replay.events == (stored,)
    assert replay.next_sequence == stored.sequence
    assert (
        service.observe_verification(project, after_sequence=stored.sequence).events
        == ()
    )

    cancelled = service.request_verification_cancel(project, started.job.job_id)
    assert cancelled.job.state == VerificationJobState.CANCEL_REQUESTED
    assert store.cancellation_requested(started.job.job_id)

    processes.close()
    repaired = service.observe_verification(project, after_sequence=stored.sequence)
    assert repaired is not None
    assert repaired.job.state == VerificationJobState.INTERRUPTED
    assert "mutation lease is free" in (repaired.job.error or "")

    resumed = service.resume_project(project)
    assert resumed.state == WorkflowState.INTERRUPTED
    assert resumed.cancellation is None
    assert "mutation lease is free" in (resumed.error or "")


def test_starting_worker_lease_blocks_project_deletion(tmp_path, monkeypatch):
    service, project = _workflow(tmp_path)
    processes = _FakeDetachedProcesses()
    monkeypatch.setattr(service, "plan_changes", lambda _project: None)
    monkeypatch.setattr(
        "proof_assistant.workflow.service.subprocess.Popen", processes.popen
    )
    try:
        service.start_verification(project, None, VerificationSettings(model="test"))
        inspection = service.inspect_project_deletion(project)
        assert inspection.availability == "BUSY"
        assert "mutation lease" in (inspection.issue or "")
    finally:
        processes.close()


def test_legacy_session_lock_is_attachable_but_not_remotely_cancellable(tmp_path):
    service, project = _workflow(tmp_path)

    with project_lock(project, exclusive=True):
        observation = service.observe_verification(project)
        assert observation is not None
        assert observation.job.attached_legacy is True
        assert observation.job.cancellable is False
        assert observation.job.state == VerificationJobState.RUNNING
        assert service.resume_project(project).state == WorkflowState.VERIFYING
        with pytest.raises(VerificationJobNotCancellableError):
            service.request_verification_cancel(project, observation.job.job_id)


def test_abandoned_legacy_running_row_routes_to_interrupted_without_false_owner(
    tmp_path,
):
    service, project = _workflow(tmp_path)
    with StateStore(project / ".repoprover" / "state.sqlite3") as store:
        store.begin_run(command="legacy TUI verification", started_at="2026-08-23")
    service._record_workflow_state(project, WorkflowState.VERIFYING)

    entry = service.list_projects()[0]
    assert entry.project is not None
    assert entry.project.workflow_state == WorkflowState.INTERRUPTED
    snapshot = service.resume_project(project)

    assert snapshot.state == WorkflowState.INTERRUPTED
    assert snapshot.cancellation is None
    assert "no backend worker or project mutation lease remains" in (
        snapshot.error or ""
    )
    assert "owns" not in (snapshot.error or "").lower()


def test_worker_persists_progress_and_terminal_result_without_ui(tmp_path, monkeypatch):
    service, project = _workflow(tmp_path)
    store = VerificationJobStore(project)
    job = store.create(
        request_fingerprint="fingerprint",
        plan_id=None,
        settings=VerificationSettings(model="test"),
    )
    lease_fd = acquire_worker_lease(project)

    def fake_confirm(
        _project, _plan_id, _settings, *, progress=None, cancellation=None
    ):
        assert cancellation.cancelled is False
        assert progress is not None
        progress(ProgressEvent(1, ProgressPhase.VALIDATING, "Worker is alive"))
        return SimpleNamespace(state=WorkflowState.COMPLETED, error=None)

    monkeypatch.setattr(service, "confirm_and_verify", fake_confirm)

    assert service._run_verification_job(project, job.job_id, lease_fd) == 0
    finished = store.job(job.job_id)
    assert finished is not None
    assert finished.state == VerificationJobState.SUCCEEDED
    assert finished.pid == os.getpid()
    assert [event.message for event in store.events(job.job_id, 0)] == [
        "Worker is alive"
    ]


def test_launch_failure_is_terminal_and_releases_mutation_lease(tmp_path, monkeypatch):
    service, project = _workflow(tmp_path)
    monkeypatch.setattr(service, "plan_changes", lambda _project: None)

    def fail_launch(*_args, **_kwargs):
        raise OSError("exec unavailable")

    monkeypatch.setattr(
        "proof_assistant.workflow.service.subprocess.Popen", fail_launch
    )

    with pytest.raises(OSError, match="exec unavailable"):
        service.start_verification(project, None, VerificationSettings(model="test"))

    job = VerificationJobStore(project).latest()
    assert job is not None
    assert job.state == VerificationJobState.FAILED
    assert "Could not launch detached verification" in (job.error or "")
    assert worker_lease_active(project) is False
    assert service.inspect_project_deletion(project).can_delete is True


def test_post_spawn_status_write_failure_never_releases_child_lease(
    tmp_path, monkeypatch
):
    service, project = _workflow(tmp_path)
    processes = _FakeDetachedProcesses()
    monkeypatch.setattr(service, "plan_changes", lambda _project: None)
    monkeypatch.setattr(
        "proof_assistant.workflow.service.subprocess.Popen", processes.popen
    )

    def fail_record_spawn(*_args, **_kwargs):
        raise OSError("status filesystem unavailable")

    monkeypatch.setattr(VerificationJobStore, "record_spawn", fail_record_spawn)
    try:
        observation = service.start_verification(
            project, None, VerificationSettings(model="test")
        )
        assert observation.started is True
        assert worker_lease_active(project) is True
        assert service.inspect_project_deletion(project).can_delete is False
    finally:
        processes.close()


def test_worker_rejects_fd_that_is_not_the_project_worker_lease(tmp_path):
    service, project = _workflow(tmp_path)
    store = VerificationJobStore(project)
    job = store.create(
        request_fingerprint="fingerprint",
        plan_id=None,
        settings=VerificationSettings(model="test"),
    )
    actual_lease = acquire_worker_lease(project)
    release_worker_lease(actual_lease)
    unrelated = tmp_path / "unrelated.lock"
    descriptor = os.open(unrelated, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert service._run_verification_job(project, job.job_id, descriptor) == 2
    finally:
        os.close(descriptor)
    failed = store.job(job.job_id)
    assert failed is not None
    assert failed.state == VerificationJobState.FAILED
    assert "does not identify" in (failed.error or "")


def test_hidden_worker_survives_launcher_fd_close_and_honors_persisted_cancel(
    tmp_path, monkeypatch
):
    production_config = tmp_path / "production-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(production_config))
    _service, project = _workflow(tmp_path)
    store = VerificationJobStore(project)
    job = store.create(
        request_fingerprint="fingerprint",
        plan_id=None,
        settings=VerificationSettings(model="test"),
    )
    store.request_cancel(job.job_id)
    lease_fd = acquire_worker_lease(project)
    command = [
        sys.executable,
        "-m",
        "proof_assistant",
        "_project-worker",
        "--project",
        str(project),
        "--job-id",
        job.job_id,
        "--lease-fd",
        str(lease_fd),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(lease_fd,),
        start_new_session=True,
    )
    os.close(lease_fd)  # The launching client may exit immediately after transfer.

    assert process.wait(timeout=20) == 1
    finished = store.job(job.job_id)
    assert finished is not None
    assert finished.state == VerificationJobState.INTERRUPTED
    assert worker_lease_active(project) is False
    assert not (production_config / "proof-assistant" / "projects.json").exists()
    assert (project / ".repoprover" / "jobs" / "worker-catalog.json").is_file()


def test_bounded_two_job_parallelism_is_the_default_everywhere() -> None:
    assert VerificationSettings().jobs == 2
    assert VerifyOptions(model="test").jobs == 2
    args = build_parser().parse_args(
        ["manuscript", "verify", "--project", "/tmp/project", "--model", "test"]
    )
    assert args.jobs == 2
    explicit = build_parser().parse_args(
        [
            "manuscript",
            "verify",
            "--project",
            "/tmp/project",
            "--model",
            "test",
            "--jobs",
            "1",
        ]
    )
    assert explicit.jobs == 1
    assert "_project-worker" not in build_parser().format_help()


def test_public_ui_contract_and_production_tui_have_no_synchronous_verify_call() -> (
    None
):
    assert "confirm_and_verify" not in inspect.getsource(WorkflowServiceContract)
    tui_root = Path(__file__).parents[1] / "src/proof_assistant/tui"
    production_source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(tui_root.glob("*.py"))
    )
    assert ".confirm_and_verify(" not in production_source
