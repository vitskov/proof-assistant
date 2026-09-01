from __future__ import annotations

import json
import sqlite3

import pytest

from proof_assistant.incremental.models import ClaimState, SourceObject
from proof_assistant.incremental.orchestration import _preflight_proof_batches
from proof_assistant.incremental.session import IncrementalSession, utc_now
from proof_assistant.incremental.store import StateStore


def _claim(claim_id: str = "T") -> SourceObject:
    return SourceObject(
        claim_id=claim_id,
        kind="theorem",
        source_file="main.tex",
        environment="theorem",
        label=claim_id,
        ordinal=1,
        statement_start=10,
        statement_end=12,
        statement_byte_start=100,
        statement_byte_end=150,
        proof_start=None,
        proof_end=None,
        proof_byte_start=None,
        proof_byte_end=None,
        statement_hash="statement",
        proof_hash="proof",
        normalized_statement_hash="normalized",
        statement_text="Statement",
        proof_text="",
        references=(),
    )


def _run_with_claim(tmp_path, state: ClaimState) -> tuple[StateStore, int]:
    store = StateStore(tmp_path / "state.sqlite3")
    run_id = store.begin_run(
        command="manuscript verify",
        started_at="2026-08-23T00:00:00+00:00",
        snapshot_commit="snapshot",
    )
    store.replace_current_claims(
        "snapshot", (_claim(),), run_id=run_id, state_updates={"T": state}
    )
    store.record_run_scope(run_id, targets=("T",), selected=("T",))
    store.replace_run_dependency_edges(run_id, ())
    return store, run_id


def test_schema_one_database_migrates_to_current_failure_and_run_schema(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata(key, value) VALUES ('schema_version', '1');
        """
    )
    connection.close()

    with StateStore(database) as store:
        assert store.get_metadata("schema_version") == "3"
        tables = {
            str(row[0])
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "run_scope",
        "run_dependency_edges",
        "run_claim_nodes",
        "failure_incidents",
        "failure_incident_claims",
        "failure_artifacts",
        "run_concurrency",
    } <= tables


def test_run_concurrency_provenance_records_initial_and_final_state(tmp_path):
    store, run_id = _run_with_claim(tmp_path, ClaimState.INVALIDATED)
    try:
        store.record_run_concurrency(
            run_id,
            configured={"mode": "adaptive", "ai": {"plan": "plus"}},
            initial_effective={"ai_limit": 2, "lean_pool": 1, "build_limit": 1},
            telemetry={"pressure": "GREEN"},
        )
        store.finish_run_concurrency(
            run_id,
            final_effective={"ai_limit": 3, "lean_pool": 1, "build_limit": 1},
            telemetry={"pressure": "GREEN", "ai_peak_active": 2},
        )

        provenance = store.run_concurrency(run_id)
        assert provenance is not None
        assert provenance["configured"]["mode"] == "adaptive"
        assert provenance["initial_effective"]["ai_limit"] == 2
        assert provenance["final_effective"]["ai_limit"] == 3
        assert provenance["telemetry"]["ai_peak_active"] == 2
    finally:
        store.close()


def test_missing_repoprover_is_one_run_incident_and_does_not_rewrite_claim(
    tmp_path, monkeypatch
):
    store, run_id = _run_with_claim(tmp_path, ClaimState.FAILED_TECHNICAL)
    monkeypatch.setattr(
        "proof_assistant.incremental.orchestration._repoprover_preflight",
        lambda: "ModuleNotFoundError: repoprover",
    )
    try:
        error = _preflight_proof_batches(store, run_id=run_id, selected={"T"})
        incidents = store.failure_incident_rows(run_id)

        assert error is not None and "ModuleNotFoundError" in error
        assert len(incidents) == 1
        assert incidents[0]["scope"] == "RUN"
        assert incidents[0]["failure_kind"] == "INFRASTRUCTURE"
        assert store.failure_claim_rows(int(incidents[0]["failure_id"])) == []
        assert store.claim_row("T")["status"] == ClaimState.FAILED_TECHNICAL
    finally:
        store.close()


def test_successful_preflight_makes_prior_technical_failure_retryable(
    tmp_path, monkeypatch
):
    store, run_id = _run_with_claim(tmp_path, ClaimState.FAILED_TECHNICAL)
    monkeypatch.setattr(
        "proof_assistant.incremental.orchestration._repoprover_preflight",
        lambda: None,
    )
    try:
        assert _preflight_proof_batches(store, run_id=run_id, selected={"T"}) is None
        assert store.claim_row("T")["status"] == ClaimState.INVALIDATED
        transition = next(
            row
            for row in store.run_claim_rows(run_id)
            if row["action"] == "retry_technical_failure"
        )
        assert "preflight succeeded" in transition["reason"]
        assert store.failure_incident_rows(run_id) == []
    finally:
        store.close()


def test_certified_only_run_does_not_require_repoprover_import(tmp_path, monkeypatch):
    store, run_id = _run_with_claim(tmp_path, ClaimState.CERTIFIED)

    def unexpected() -> str | None:
        raise AssertionError("certificate-only run must not import RepoProver")

    monkeypatch.setattr(
        "proof_assistant.incremental.orchestration._repoprover_preflight",
        unexpected,
    )
    try:
        assert _preflight_proof_batches(store, run_id=run_id, selected={"T"}) is None
        assert store.failure_incident_rows(run_id) == []
    finally:
        store.close()


def test_failure_claim_attribution_must_stay_inside_run_scope(tmp_path):
    store, run_id = _run_with_claim(tmp_path, ClaimState.INVALIDATED)
    other = _claim("Other")
    store.replace_current_claims(
        "snapshot",
        (_claim(), other),
        run_id=run_id,
        state_updates={"T": ClaimState.INVALIDATED, "Other": ClaimState.INVALIDATED},
    )
    try:
        with pytest.raises(ValueError, match="outside this run's selected scope"):
            store.add_failure_incident(
                run_id=run_id,
                scope="CLAIM",
                failure_kind="CLAIM_TECHNICAL",
                phase="PROOF_BATCH",
                category="test",
                message="Out-of-scope attribution",
                provenance="test",
                claim_ids=("Other",),
            )
    finally:
        store.close()


def test_contract_schema_version_is_persisted_as_integer(tmp_path):
    store, _run_id = _run_with_claim(tmp_path, ClaimState.INVALIDATED)
    try:
        assert json.loads(store.get_metadata("schema_version") or "0") == 3
    finally:
        store.close()


def test_unrelated_dependency_cycle_does_not_abort_selected_prepare_pass(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_text(
        r"""
\documentclass{article}
\newtheorem{theorem}{Theorem}
\begin{document}
\begin{theorem}\label{target}Target.\end{theorem}
\begin{proof}Direct.\end{proof}
\begin{theorem}\label{cycle-a}A uses \ref{cycle-b}.\end{theorem}
\begin{theorem}\label{cycle-b}B uses \ref{cycle-a}.\end{theorem}
\end{document}
""",
        encoding="utf-8",
    )
    task = source / "VERIFY.yaml"
    task.write_text(
        """schema: 1
mode: theorem
targets: [target]
instructions: Verify only the selected target.
""",
        encoding="utf-8",
    )
    session = IncrementalSession.initialize(
        manuscript=source,
        main_file="main.tex",
        task_file=task,
        project=tmp_path / "project",
    )

    prepared = session.prepare_pass()
    try:
        assert prepared.selected == frozenset({"target"})
        assert ("cycle-a", "cycle-b") in prepared.cycles
        with StateStore(session.database_path) as store:
            assert store.failure_incident_rows(prepared.run_id) == []
            store.finish_run(
                prepared.run_id,
                status="COMPLETE",
                outcome="test",
                completed_at=utc_now(),
                detail="test finalized",
            )
    finally:
        # Keep the managed project recoverable if an assertion fails mid-test.
        with StateStore(session.database_path) as store:
            latest = store.latest_run()
            if latest is not None and latest["status"] == "RUNNING":
                store.finish_run(
                    int(latest["run_id"]),
                    status="INTERRUPTED",
                    outcome="interrupted",
                    completed_at=utc_now(),
                    detail="test cleanup",
                )
