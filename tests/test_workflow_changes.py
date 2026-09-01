from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from proof_assistant.incremental.models import ClaimState
from proof_assistant.incremental.orchestration import (
    VerificationCancelled,
    VerificationResult,
)
from proof_assistant.incremental.session import IncrementalSession
from proof_assistant.incremental.store import StateStore
from proof_assistant.presentation.clarifications import ClarificationPresenter
from proof_assistant.workflow.contracts import (
    ClaimChangeKind,
    NewProjectRequest,
    ProgressPhase,
    VerificationSettings,
    WorkflowState,
)
from proof_assistant.workflow.service import (
    CancellationFlag,
    ProofAssistantWorkflow,
    StaleChangePlanError,
)

FIXTURE = Path(__file__).parent / "fixtures" / "incremental_manuscript"


def setup_project(tmp_path: Path, **kwargs):
    source = tmp_path / "paper"
    shutil.copytree(FIXTURE, source)
    (source / "VERIFY.yaml").unlink()
    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog",
        use_codex_clarification=False,
        **kwargs,
    )
    project = tmp_path / "project"
    workflow.create_project(
        NewProjectRequest(
            name="Paper",
            source_path=source,
            main_file="main.tex",
            project_path=project,
        )
    )
    return source, project, workflow


def change_lemma(source: Path, suffix: str) -> None:
    path = source / "main.tex"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "For every natural number $n$, one has $0+n=n$.",
            f"For every natural number $n$, one has $0+n=n$ {suffix}.",
        ),
        encoding="utf-8",
    )


def test_change_plan_is_nonmutating_and_includes_transitive_impact(tmp_path):
    source, project, workflow = setup_project(tmp_path)
    session = IncrementalSession(project)
    before_snapshot = session.status()["snapshot"]
    before_head = session._git(["rev-parse", "HEAD"])
    with StateStore(session.database_path) as store:
        before_runs = int(store.latest_run()["run_id"])

    change_lemma(source, "by induction")
    plan = workflow.plan_changes(project)

    assert plan is not None
    assert [(item.path, str(item.kind)) for item in plan.file_changes] == [
        ("main.tex", "MODIFIED")
    ]
    assert ("lem:zero-add", ClaimChangeKind.STATEMENT) in {
        (item.claim_id, item.kind) for item in plan.direct_claim_changes
    }
    assert plan.affected_claims == ("lem:zero-add", "thm:add-zero")
    assert session.status()["snapshot"] == before_snapshot
    assert session._git(["rev-parse", "HEAD"]) == before_head
    with StateStore(session.database_path) as store:
        assert int(store.latest_run()["run_id"]) == before_runs


def test_task_change_is_separate_from_external_file_changes(tmp_path):
    _source, project, workflow = setup_project(tmp_path)
    task = project / "VERIFY.yaml"
    task.write_text(
        task.read_text(encoding="utf-8").replace(
            "Verify every lemma", "Pay special attention and verify every lemma"
        ),
        encoding="utf-8",
    )
    plan = workflow.plan_changes(project)
    assert plan is not None
    assert plan.task_changed is True
    assert plan.file_changes == ()
    assert {item.kind for item in plan.direct_claim_changes} == {
        ClaimChangeKind.TASK_SCOPE
    }


def test_change_to_unselected_alternate_root_does_not_create_a_plan(tmp_path):
    source, project, workflow = setup_project(tmp_path)
    (source / "alternate.tex").write_text(
        r"\begin{theorem}\label{lem:zero-add}Outside selected closure.\end{theorem}",
        encoding="utf-8",
    )
    assert workflow.plan_changes(project) is None


def test_change_plan_reports_selected_main_and_updated_recursive_inputs(tmp_path):
    source = tmp_path / "paper"
    (source / "parts").mkdir(parents=True)
    (source / "main.tex").write_text(r"\input{parts/a}", encoding="utf-8")
    (source / "parts/a.tex").write_text(
        r"\begin{lemma}\label{a}A.\end{lemma}", encoding="utf-8"
    )
    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog", use_codex_clarification=False
    )
    project = tmp_path / "project"
    workflow.create_project(
        NewProjectRequest("Paper", source, "main.tex", project_path=project)
    )
    (source / "parts/b.tex").write_text(
        r"\begin{lemma}\label{b}B.\end{lemma}", encoding="utf-8"
    )
    (source / "main.tex").write_text(
        r"\input{parts/a}\input{parts/b}", encoding="utf-8"
    )
    plan = workflow.plan_changes(project)
    assert plan is not None
    assert plan.main_file == "main.tex"
    assert plan.input_files == ("parts/a.tex", "parts/b.tex")
    prepared = IncrementalSession(project).prepare_pass()
    persisted = json.loads(
        (project / ".repoprover/config.json").read_text(encoding="utf-8")
    )
    assert persisted["input_files"] == ["parts/a.tex", "parts/b.tex"]
    with StateStore(project / ".repoprover/state.sqlite3") as store:
        assert json.loads(store.get_metadata("input_files") or "null") == [
            "parts/a.tex",
            "parts/b.tex",
        ]
        store.finish_run(
            prepared.run_id,
            status="COMPLETE",
            outcome="test",
            completed_at="2026-08-23T00:00:00+00:00",
            detail="test",
        )


def test_confirmation_rejects_a_plan_if_any_source_file_changed_again(tmp_path):
    source, project, workflow = setup_project(tmp_path)
    change_lemma(source, "first edit")
    plan = workflow.plan_changes(project)
    assert plan is not None
    (source / "notes.tex").write_text("second file edit", encoding="utf-8")
    with pytest.raises(StaleChangePlanError, match="changed after review"):
        workflow.confirm_and_verify(
            project, plan.plan_id, VerificationSettings(model="unused")
        )


def test_confirmed_plan_passes_reviewed_digest_and_emits_typed_progress(
    tmp_path, monkeypatch
):
    source, project, workflow = setup_project(tmp_path)
    change_lemma(source, "reviewed edit")
    plan = workflow.plan_changes(project)
    assert plan is not None
    observed = {}

    def fake_verify(_session, **kwargs):
        observed.update(kwargs)
        kwargs["event_hook"]("REPORTING", "fake report", {"test": True})
        return VerificationResult(
            "verified",
            "verified in test",
            0,
            2,
            "snapshot",
            str(project),
            ("lem:zero-add",),
            (),
            (),
            (),
            (),
        )

    monkeypatch.setattr("proof_assistant.workflow.service.verify_project", fake_verify)
    events = []
    result = workflow.confirm_and_verify(
        project,
        plan.plan_id,
        VerificationSettings(model="test"),
        progress=events.append,
    )
    assert result.state == WorkflowState.COMPLETED
    assert observed["expected_inventory_sha256"] == plan.candidate_inventory_sha256
    assert [event.phase for event in events] == [
        "VALIDATING",
        "REPORTING",
    ]
    assert events[0].details["main_file"] == "main.tex"
    assert events[0].details["input_files"] == ()


def test_progress_event_maps_typed_counts_and_claim_id(tmp_path):
    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog", use_codex_clarification=False
    )
    events = []
    workflow._emit(
        events.append,
        ProgressPhase.PROOF_BATCH,
        "Proving claim",
        details={"completed": 2, "total": 5, "claim_id": "lem:a"},
    )
    assert events[0].completed == 2
    assert events[0].total == 5
    assert events[0].claim_id == "lem:a"


def test_pre_run_cancellation_returns_durable_empty_recovery_report(tmp_path):
    _source, project, workflow = setup_project(tmp_path)
    cancellation = CancellationFlag()
    cancellation.cancel()
    result = workflow.confirm_and_verify(
        project,
        None,
        VerificationSettings(model="unused"),
        cancellation=cancellation,
    )
    assert result.state == WorkflowState.INTERRUPTED
    assert result.cancellation is not None
    assert result.cancellation.run_id is None
    assert result.cancellation.retryable_claims == ()
    assert result.cancellation.temporary_worktrees_cleaned is True


def test_verifier_cancellation_facts_are_preserved_in_workflow_contract(
    tmp_path, monkeypatch
):
    _source, project, workflow = setup_project(tmp_path)

    def cancel_verify(*_args, **_kwargs):
        raise VerificationCancelled(
            "Stopped at a safe boundary",
            run_id=7,
            preserved_certificates=("lem:a",),
            retryable_claims=("thm:b",),
            temporary_worktrees_cleaned=True,
        )

    monkeypatch.setattr(
        "proof_assistant.workflow.service.verify_project", cancel_verify
    )
    result = workflow.confirm_and_verify(
        project, None, VerificationSettings(model="unused")
    )
    assert result.state == WorkflowState.INTERRUPTED
    assert result.cancellation is not None
    assert result.cancellation.run_id == 7
    assert result.cancellation.preserved_certificates == ("lem:a",)
    assert result.cancellation.retryable_claims == ("thm:b",)
    assert result.cancellation.temporary_worktrees_cleaned is True


def test_prepared_none_verifier_cancellation_reads_persisted_certificates(
    tmp_path, monkeypatch
):
    _source, project, workflow = setup_project(tmp_path)
    session = IncrementalSession(project)
    snapshot = session.status()["snapshot"]
    with StateStore(session.database_path) as store:
        store.upsert_certificate(
            {
                "claim_id": "lem:zero-add",
                "manuscript_snapshot": snapshot,
                "statement_hash": "statement",
                "formal_type_hash": "type",
                "lean_declaration": "ManuscriptVerification.zeroAdd",
                "lean_value_hash": "value",
                "environment_hash": "environment",
                "lean_version": "4.28.0",
                "last_verified_run": 1,
            }
        )

    def cancel_before_prepare(*_args, **_kwargs):
        raise VerificationCancelled(
            "Stopped before preparation",
            preserved_certificates=("not-authoritative",),
            retryable_claims=("not-in-flight",),
            temporary_worktrees_cleaned=True,
        )

    monkeypatch.setattr(
        "proof_assistant.workflow.service.verify_project", cancel_before_prepare
    )
    result = workflow.confirm_and_verify(
        project, None, VerificationSettings(model="unused")
    )
    assert result.cancellation is not None
    assert result.cancellation.run_id is None
    assert result.cancellation.preserved_certificates == ("lem:zero-add",)
    assert result.cancellation.retryable_claims == ()
    assert result.cancellation.temporary_worktrees_cleaned is True


class BadNarrator:
    name = "invalid-test-narrator"

    def narrate(self, _facts):
        return {"headline": "tries to omit the strict fields"}


def test_resume_question_uses_exact_multifile_location_without_narrator(tmp_path):
    source, project, _workflow = setup_project(tmp_path)
    section = source / "sections"
    section.mkdir()
    main = source / "main.tex"
    moved = section / "results.tex"
    moved.write_text(main.read_text(encoding="utf-8"), encoding="utf-8")
    main.write_text(r"\input{sections/results}", encoding="utf-8")
    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog-two",
        clarification_narrator=BadNarrator(),
        use_codex_clarification=False,
    )
    initial_plan = workflow.plan_changes(project)
    assert initial_plan is not None

    # Import the multi-file source through the deterministic preparation seam.
    prepared = IncrementalSession(project).prepare_pass()
    with StateStore(IncrementalSession(project).database_path) as store:
        version = store.claim_version(prepared.snapshot.commit, "lem:zero-add")
        store.create_question(
            claim_id="lem:zero-add",
            snapshot=prepared.snapshot.commit,
            category="ambiguous_statement",
            passage=str(version["statement_text"]).strip(),
            problem="Clarify the quantifier.",
            possible_resolutions=("State the quantifier explicitly.",),
            blocking_claims=("thm:add-zero",),
            run_id=prepared.run_id,
        )
        store.set_claim_state(
            "lem:zero-add",
            ClaimState.NEEDS_CLARIFICATION,
            run_id=prepared.run_id,
            action="test_question",
            reason="test",
        )
        store.finish_run(
            prepared.run_id,
            status="COMPLETE",
            outcome="clarification_required",
            completed_at="2026-08-23T00:00:00+00:00",
            detail="test",
        )

    resumed = workflow.resume_project(project)
    assert resumed.state == WorkflowState.AWAITING_CLARIFICATION
    presentation = resumed.clarifications[0]
    assert presentation.location.relative_path == "sections/results.tex"
    assert presentation.location.absolute_path == moved.resolve()
    assert presentation.generated_by == "deterministic"
    assert presentation.location.start_line <= presentation.location.end_line
    persisted = json.loads(
        (project / ".repoprover/presentations/clarifications.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["clarifications"][0]["provenance_sha256"]


class CountingNarrator:
    name = "counting-test-narrator"

    def __init__(self) -> None:
        self.calls = 0

    def narrate(self, _facts):
        self.calls += 1
        return {
            "headline": "Clarify the quantifier",
            "explanation": "The quantifier is not explicit.",
            "requested_actions": ["State the quantifier explicitly."],
        }


def test_resume_reuses_persisted_clarification_without_narrating(tmp_path):
    source, project, _workflow = setup_project(tmp_path)
    prepared = IncrementalSession(project).prepare_pass()
    with StateStore(IncrementalSession(project).database_path) as store:
        version = store.claim_version(prepared.snapshot.commit, "lem:zero-add")
        store.create_question(
            claim_id="lem:zero-add",
            snapshot=prepared.snapshot.commit,
            category="ambiguous_statement",
            passage=str(version["statement_text"]).strip(),
            problem="Clarify the quantifier.",
            possible_resolutions=("State the quantifier explicitly.",),
            blocking_claims=("thm:add-zero",),
            run_id=prepared.run_id,
        )
        store.set_claim_state(
            "lem:zero-add",
            ClaimState.NEEDS_CLARIFICATION,
            run_id=prepared.run_id,
            action="test_question",
            reason="test",
        )
        store.finish_run(
            prepared.run_id,
            status="COMPLETE",
            outcome="clarification_required",
            completed_at="2026-08-23T00:00:00+00:00",
            detail="test",
        )

    narrator = CountingNarrator()
    generated = ClarificationPresenter(narrator).present_all(project, source)
    assert narrator.calls == 1

    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog-two",
        clarification_narrator=narrator,
        use_codex_clarification=False,
    )
    resumed = workflow.resume_project(project)
    assert resumed.state == WorkflowState.AWAITING_CLARIFICATION
    assert resumed.clarifications == generated
    assert narrator.calls == 1

    presentation_path = (
        project / ".repoprover" / "presentations" / "clarifications.json"
    )
    stale = json.loads(presentation_path.read_text(encoding="utf-8"))
    stale["clarifications"][0]["question_id"] = "stale-question"
    presentation_path.write_text(json.dumps(stale), encoding="utf-8")

    recovered = workflow.resume_project(project)
    assert recovered.state == WorkflowState.AWAITING_CLARIFICATION
    assert recovered.clarifications[0].question_id == "Q0001"
    assert recovered.clarifications[0].generated_by == "deterministic"
    assert narrator.calls == 1


def test_open_question_with_new_source_returns_to_change_review(tmp_path):
    source, project, workflow = setup_project(tmp_path)
    session = IncrementalSession(project)
    snapshot = session.status()["snapshot"]
    with StateStore(session.database_path) as store:
        version = store.claim_version(snapshot, "lem:zero-add")
        store.create_question(
            claim_id="lem:zero-add",
            snapshot=snapshot,
            category="ambiguous_statement",
            passage=str(version["statement_text"]).strip(),
            problem="Clarify this.",
            possible_resolutions=("Edit it.",),
            blocking_claims=("thm:add-zero",),
            run_id=1,
        )
    change_lemma(source, "clarified")
    resumed = workflow.resume_project(project)
    assert resumed.state == WorkflowState.CHANGE_REVIEW
    assert resumed.pending_plan is not None
    assert resumed.clarifications == ()
