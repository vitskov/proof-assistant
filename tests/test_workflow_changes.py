from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from proof_assistant.incremental.models import ClaimState
from proof_assistant.incremental.orchestration import VerificationResult
from proof_assistant.incremental.session import IncrementalSession
from proof_assistant.incremental.store import StateStore
from proof_assistant.workflow.contracts import (
    ClaimChangeKind,
    NewProjectRequest,
    VerificationSettings,
    WorkflowState,
)
from proof_assistant.workflow.service import (
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
    workflow.create_project(NewProjectRequest("Paper", source, project))
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
            "Verify every claimed", "Pay special attention and verify every claimed"
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


class BadNarrator:
    name = "invalid-test-narrator"

    def narrate(self, _facts):
        return {"headline": "tries to omit the strict fields"}


def test_resume_question_uses_exact_multifile_location_and_fallback(tmp_path):
    source, project, _workflow = setup_project(tmp_path)
    section = source / "sections"
    section.mkdir()
    main = source / "main.tex"
    moved = section / "results.tex"
    moved.write_text(main.read_text(encoding="utf-8"), encoding="utf-8")
    main.unlink()
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
    assert presentation.generated_by == "deterministic-fallback"
    assert presentation.location.start_line <= presentation.location.end_line
    persisted = json.loads(
        (project / ".repoprover/presentations/clarifications.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["clarifications"][0]["provenance_sha256"]


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
