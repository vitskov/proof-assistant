from __future__ import annotations

from pathlib import Path

import pytest

from proof_assistant.incremental.graph import ready_frontier
from proof_assistant.incremental.models import ClaimState
from proof_assistant.incremental.orchestration import (
    BatchJob,
    BatchResult,
    VerificationCancelled,
    VerifyOptions,
    _execute_batch_round,
    verify_project,
)
from proof_assistant.incremental.session import IncrementalSession, utc_now
from proof_assistant.incremental.store import StateStore


def _insert_claim(
    store: StateStore, claim_id: str, state: ClaimState, *, run_id: int
) -> None:
    store.connection.execute(
        """
        INSERT INTO claims(
            claim_id, kind, source_file, environment, label, ordinal,
            current_statement_hash, current_proof_hash,
            normalized_statement_hash, current_snapshot, status,
            last_changed_run, retired
        ) VALUES (?, 'lemma', 'main.tex', 'lemma', ?, 1,
                  'statement', 'proof', 'normalized', 'snapshot', ?, ?, 0)
        """,
        (claim_id, claim_id, str(state), run_id),
    )


def _job(tmp_path: Path, index: int) -> BatchJob:
    workspace = tmp_path / f"batch-{index}"
    workspace.mkdir()
    return BatchJob(
        index=index,
        project=str(tmp_path / "project"),
        workspace=str(workspace),
        run_id=1,
        snapshot="snapshot",
        previous_snapshot=None,
        claims=(f"claim-{index}",),
        require_correspondence_review=False,
        pause_on_ambiguity=True,
        counterexample_search=True,
        options=VerifyOptions(model="test"),
    )


def _result(job: BatchJob) -> BatchResult:
    return BatchResult(
        index=job.index,
        claims=job.claims,
        workspace=job.workspace,
        base_commit="base",
        final_commit="final",
        git_status="",
        build_succeeded=True,
        provider_failure=None,
        final_text="done",
        thread_id="thread",
        turn_id="turn",
        tool_calls=1,
    )


def test_recovery_resets_abandoned_proving_claims_to_retryable_state(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as store:
        run_id = store.begin_run(command="verify", started_at=utc_now())
        _insert_claim(store, "in-flight", ClaimState.PROVING, run_id=run_id)
        _insert_claim(store, "durable", ClaimState.CERTIFIED, run_id=run_id)

        assert store.recover_interrupted_runs(utc_now()) == 1

        assert store.latest_run()["status"] == "INTERRUPTED"
        assert store.claim_row("in-flight")["status"] == ClaimState.INVALIDATED
        assert store.claim_row("durable")["status"] == ClaimState.CERTIFIED
        recovery = store.connection.execute(
            """
            SELECT * FROM run_claims
            WHERE run_id = ? AND claim_id = 'in-flight'
              AND action = 'recover_interrupted'
            """,
            (run_id,),
        ).fetchone()
        assert recovery["state_before"] == ClaimState.PROVING
        assert recovery["state_after"] == ClaimState.INVALIDATED

    assert ready_frontier(
        {"in-flight": ClaimState.INVALIDATED},
        selected={"in-flight"},
        edges=(),
    ) == ("in-flight",)


def test_recovery_also_repairs_proving_state_from_an_already_interrupted_run(
    tmp_path,
):
    with StateStore(tmp_path / "state.sqlite3") as store:
        run_id = store.begin_run(command="verify", started_at=utc_now())
        _insert_claim(store, "orphan", ClaimState.PROVING, run_id=run_id)
        store.finish_run(
            run_id,
            status="INTERRUPTED",
            outcome="interrupted",
            completed_at=utc_now(),
            detail="legacy interruption left PROVING behind",
        )

        assert store.recover_interrupted_runs(utc_now()) == 0
        assert store.claim_row("orphan")["status"] == ClaimState.INVALIDATED
        recovery = store.connection.execute(
            """
            SELECT * FROM run_claims
            WHERE run_id = ? AND claim_id = 'orphan'
              AND action = 'recover_orphaned_proving'
            """,
            (run_id,),
        ).fetchone()
        assert recovery is not None


def test_cancellation_before_run_allocation_reports_no_temporary_worktrees(tmp_path):
    def cancel() -> None:
        raise InterruptedError("stop before setup")

    with pytest.raises(VerificationCancelled) as raised:
        verify_project(
            IncrementalSession(tmp_path / "not-created"),
            options=VerifyOptions(model="test"),
            cancellation_checkpoint=cancel,
        )

    assert raised.value.run_id is None
    assert raised.value.retryable_claims == ()
    assert raised.value.temporary_worktrees_cleaned is True


def test_cancel_after_workers_discards_candidates_before_merge_and_cleans_all(
    tmp_path, monkeypatch
):
    jobs = [_job(tmp_path, 1), _job(tmp_path, 2)]
    merged: list[int] = []
    cleaned: list[str] = []

    monkeypatch.setattr(
        "proof_assistant.incremental.orchestration._run_batch_worker", _result
    )
    monkeypatch.setattr(
        "proof_assistant.incremental.orchestration._merge_batch",
        lambda _project, result: merged.append(result.index),
    )
    monkeypatch.setattr(
        "proof_assistant.incremental.orchestration._remove_worktree",
        lambda _project, workspace: cleaned.append(workspace.name) or True,
    )

    def cancel_at_safe_boundary() -> None:
        raise VerificationCancelled("requested")

    with pytest.raises(VerificationCancelled, match="requested"):
        _execute_batch_round(
            tmp_path / "project",
            jobs,
            max_workers=1,
            checkpoint=cancel_at_safe_boundary,
        )

    assert merged == []
    assert cleaned == ["batch-1", "batch-2"]


def test_worker_failure_still_cleans_every_temporary_worktree(tmp_path, monkeypatch):
    jobs = [_job(tmp_path, 1), _job(tmp_path, 2)]
    cleaned: list[str] = []

    def fail_worker(_job: BatchJob) -> BatchResult:
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(
        "proof_assistant.incremental.orchestration._run_batch_worker", fail_worker
    )
    monkeypatch.setattr(
        "proof_assistant.incremental.orchestration._remove_worktree",
        lambda _project, workspace: cleaned.append(workspace.name) or True,
    )

    with pytest.raises(RuntimeError, match="worker crashed"):
        _execute_batch_round(
            tmp_path / "project",
            jobs,
            max_workers=1,
            checkpoint=lambda: None,
        )

    assert cleaned == ["batch-1", "batch-2"]


def test_cancellation_facts_are_sorted_and_explicit():
    cancellation = VerificationCancelled("safe stop")
    assert cancellation.temporary_worktrees_cleaned is True
    cancellation.record_recovery(
        run_id=17,
        preserved_certificates=("z", "a"),
        retryable_claims=("b", "a"),
        temporary_worktrees_cleaned=True,
    )

    assert cancellation.run_id == 17
    assert cancellation.preserved_certificates == ("a", "z")
    assert cancellation.retryable_claims == ("a", "b")
    assert cancellation.temporary_worktrees_cleaned is True
