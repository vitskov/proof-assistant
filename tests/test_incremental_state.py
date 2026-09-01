from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from proof_assistant.incremental.agent import (
    IncrementalAgentContext,
    write_batch_context,
)
from proof_assistant.incremental.locking import project_lock
from proof_assistant.incremental.models import ClaimState
from proof_assistant.incremental.session import IncrementalSession, utc_now
from proof_assistant.incremental.store import StateStore

FIXTURE = Path(__file__).parent / "fixtures" / "incremental_manuscript"


def copy_fixture(tmp_path: Path) -> Path:
    source = tmp_path / "paper"
    shutil.copytree(FIXTURE, source)
    return source


def initialize(
    tmp_path: Path, *, argument_audit: bool = False
) -> tuple[Path, IncrementalSession]:
    source = copy_fixture(tmp_path)
    if argument_audit:
        task = source / "VERIFY.yaml"
        task.write_text(
            task.read_text(encoding="utf-8").replace(
                "mode: theorem", "mode: argument-audit"
            ),
            encoding="utf-8",
        )
    session = IncrementalSession.initialize(
        manuscript=source,
        task_file=source / "VERIFY.yaml",
        project=tmp_path / "verification",
        main_file="main.tex",
    )
    return source, session


def initialize_document(
    tmp_path: Path,
    document: str,
    *,
    targets: tuple[str, ...] = (),
) -> tuple[Path, IncrementalSession]:
    source = tmp_path / "paper"
    source.mkdir(parents=True)
    (source / "main.tex").write_text(document, encoding="utf-8")
    target_lines = "\n".join(f"  - {target}" for target in targets)
    targets_yaml = f"targets:\n{target_lines}" if targets else "targets: []"
    task = source / "VERIFY.yaml"
    task.write_text(
        "\n".join(
            (
                "schema: 1",
                "mode: theorem",
                targets_yaml,
                "policy:",
                "  pause_on_ambiguity: true",
                "  preserve_certified: true",
                "  counterexample_search: true",
                "  require_statement_correspondence_review: false",
                "instructions: Test the proof-obligation policy.",
                "",
            )
        ),
        encoding="utf-8",
    )
    session = IncrementalSession.initialize(
        manuscript=source,
        task_file=task,
        project=tmp_path / "verification",
        main_file="main.tex",
    )
    return source, session


def finish_prepared(session: IncrementalSession, run_id: int) -> None:
    with StateStore(session.database_path) as store:
        store.finish_run(
            run_id,
            status="COMPLETE",
            outcome="test",
            completed_at=utc_now(),
            detail="test finalized",
        )


def test_batch_context_records_effective_resource_admission_contract(tmp_path):
    path = write_batch_context(
        tmp_path,
        run_id=7,
        snapshot="snapshot-hash",
        claims=("claim:one",),
        pause_on_ambiguity=True,
        counterexample_search=False,
        concurrency={
            "configured": {"mode": "adaptive"},
            "effective": {"ai_limit": 4, "lean_pool": 2, "build_limit": 1},
        },
        admission_timeout=1800.0,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["resource_admission"] == {
        "timeout_seconds": 1800.0,
        "configured": {"mode": "adaptive"},
        "effective": {"ai_limit": 4, "lean_pool": 2, "build_limit": 1},
    }


def test_state_store_recovers_runs_and_rolls_back_transactions(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.begin_run(command="test", started_at=utc_now())
        assert store.recover_interrupted_runs(utc_now()) == 1
        assert store.latest_run()["status"] == "INTERRUPTED"
        with pytest.raises(RuntimeError):
            with store.transaction() as connection:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('rolled', 'back')"
                )
                raise RuntimeError("stop")
        assert store.get_metadata("rolled") is None


def test_project_initialization_creates_clean_persistent_layout(tmp_path):
    _source, session = initialize(tmp_path)
    status = session.status()
    assert status["claim_states"] == {"DISCOVERED": 2}
    assert status["certificates"] == 0
    assert (session.project / ".repoprover/state.sqlite3").is_file()
    assert (session.project / ".repoprover/snapshots/manuscript.git").is_dir()
    assert (session.project / ".repoprover/exports/claims.json").is_file()
    assert (session.project / "RepoProverSupport/DependencyExtractor.lean").is_file()
    assert (session.project / "VERIFICATION_STATUS.md").is_file()
    assert session._git(["status", "--porcelain=v1"]) == ""
    manifest = json.loads(
        (session.project / ".repoprover/exports/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(manifest["manuscript_graph_sha256"]) == 64
    assert manifest["lean_graph_sha256"] is None
    assert len(manifest["combined_graph_sha256"]) == 64


def test_unproved_assertions_are_always_skipped_even_when_explicitly_targeted(
    tmp_path,
):
    _source, session = initialize_document(
        tmp_path,
        r"""
\documentclass{article}
\usepackage{amsthm}
\newtheorem{conjecture}{Conjecture}
\newtheorem{theorem}{Theorem}
\begin{document}
\begin{conjecture}\label{conj:standalone}A conjectural assertion.\end{conjecture}
\begin{theorem}\label{thm:unproved}An assertion with no proof.\end{theorem}
\end{document}
""",
        targets=("conj:standalone", "thm:unproved"),
    )

    prepared = session.prepare_pass()
    try:
        assert prepared.targets == frozenset()
        assert prepared.selected == frozenset()
        assert prepared.skipped_unproved == frozenset(
            {"conj:standalone", "thm:unproved"}
        )
        with StateStore(session.database_path) as store:
            assert store.open_questions() == []
            assert {
                str(store.claim_row(claim_id)["status"])
                for claim_id in prepared.skipped_unproved
            } == {str(ClaimState.SKIPPED_UNPROVED)}
    finally:
        finish_prepared(session, prepared.run_id)


def test_unproved_dependency_asks_only_for_proof_bearing_dependents(tmp_path):
    _source, session = initialize_document(
        tmp_path,
        r"""
\documentclass{article}
\usepackage{amsthm}
\newtheorem{conjecture}{Conjecture}
\newtheorem{theorem}{Theorem}
\begin{document}
\begin{conjecture}\label{conj:needed}A needed conjecture.\end{conjecture}
\begin{theorem}\label{thm:dependent}The proved result.\end{theorem}
\begin{proof}Apply \cref{conj:needed}.\end{proof}
\end{document}
""",
        targets=("thm:dependent",),
    )

    prepared = session.prepare_pass()
    try:
        assert prepared.targets == frozenset({"thm:dependent"})
        assert prepared.selected == frozenset({"conj:needed", "thm:dependent"})
        with StateStore(session.database_path) as store:
            questions = store.open_questions()
            assert len(questions) == 1
            assert questions[0]["claim_id"] == "conj:needed"
            assert questions[0]["category"] == "conjectural_dependency"
            assert json.loads(str(questions[0]["blocking_claims_json"])) == [
                "thm:dependent"
            ]
            assert json.loads(str(questions[0]["resolutions_json"]))[0].startswith(
                "Reclassify this conjecture"
            )
            assert store.claim_row("conj:needed")["status"] == str(
                ClaimState.NEEDS_CLARIFICATION
            )
    finally:
        finish_prepared(session, prepared.run_id)


def test_legacy_standalone_conjecture_question_is_superseded(tmp_path):
    _source, session = initialize_document(
        tmp_path,
        r"""
\documentclass{article}
\usepackage{amsthm}
\newtheorem{conjecture}{Conjecture}
\begin{document}
\begin{conjecture}\label{conj:legacy}A standalone conjecture.\end{conjecture}
\end{document}
""",
    )
    with StateStore(session.database_path) as store:
        latest = store.latest_run()
        assert latest is not None
        run_id = int(latest["run_id"])
        snapshot = str(latest["snapshot_commit"])
        store.create_question(
            claim_id="conj:legacy",
            snapshot=snapshot,
            category="missing_assumption",
            passage="A standalone conjecture.",
            problem="Legacy verifier asked for a proof.",
            possible_resolutions=("Supply a proof.",),
            blocking_claims=("conj:legacy",),
            run_id=run_id,
        )
        store.set_claim_state(
            "conj:legacy",
            ClaimState.NEEDS_CLARIFICATION,
            run_id=run_id,
            action="legacy_question",
            reason="Legacy self-blocking clarification",
        )

    assert session.reconcile_conjectural_policy() is True
    assert session.reconcile_conjectural_policy() is False
    with StateStore(session.database_path) as store:
        assert store.open_questions() == []
        assert store.claim_row("conj:legacy")["status"] == str(
            ClaimState.SKIPPED_UNPROVED
        )
        assert store.question_row("Q0001")["status"] == "SUPERSEDED"


def test_resume_preserves_an_intentionally_empty_proof_target_scope(tmp_path):
    _source, session = initialize_document(
        tmp_path,
        r"""
\documentclass{article}
\usepackage{amsthm}
\newtheorem{conjecture}{Conjecture}
\newtheorem{theorem}{Theorem}
\begin{document}
\begin{conjecture}\label{conj:only-target}An unsupported premise.\end{conjecture}
\begin{theorem}\label{thm:not-targeted}An unselected proved result.\end{theorem}
\begin{proof}Apply \cref{conj:only-target}.\end{proof}
\end{document}
""",
        targets=("conj:only-target",),
    )
    prepared = session.prepare_pass()
    try:
        assert prepared.targets == frozenset()
        assert prepared.selected == frozenset()
        with StateStore(session.database_path) as store:
            assert store.open_questions() == []
    finally:
        finish_prepared(session, prepared.run_id)

    assert session.reconcile_conjectural_policy() is False
    with StateStore(session.database_path) as store:
        assert store.open_questions() == []
        assert store.claim_row("conj:only-target")["status"] == str(
            ClaimState.SKIPPED_UNPROVED
        )


def test_adding_proof_promotes_unproved_dependency_and_supersedes_question(tmp_path):
    source, session = initialize_document(
        tmp_path,
        r"""
\documentclass{article}
\usepackage{amsthm}
\newtheorem{lemma}{Lemma}
\newtheorem{theorem}{Theorem}
\begin{document}
\begin{lemma}\label{lem:promoted}An initially unsupported lemma.\end{lemma}
\begin{theorem}\label{thm:uses-promoted}A proved result.\end{theorem}
\begin{proof}Apply \cref{lem:promoted}.\end{proof}
\end{document}
""",
        targets=("thm:uses-promoted",),
    )
    first = session.prepare_pass()
    try:
        with StateStore(session.database_path) as store:
            assert len(store.open_questions()) == 1
            assert store.claim_row("lem:promoted")["status"] == str(
                ClaimState.NEEDS_CLARIFICATION
            )
    finally:
        finish_prepared(session, first.run_id)

    tex = source / "main.tex"
    tex.write_text(
        tex.read_text(encoding="utf-8").replace(
            "\\begin{lemma}\\label{lem:promoted}An initially unsupported lemma."
            "\\end{lemma}",
            "\\begin{lemma}\\label{lem:promoted}An initially unsupported lemma."
            "\\end{lemma}\n\\begin{proof}A newly supplied proof.\\end{proof}",
        ),
        encoding="utf-8",
    )

    second = session.prepare_pass()
    try:
        assert second.targets == frozenset({"thm:uses-promoted"})
        assert second.selected == frozenset(
            {"lem:promoted", "thm:uses-promoted"}
        )
        assert "lem:promoted" in second.directly_changed
        with StateStore(session.database_path) as store:
            assert store.open_questions() == []
            assert store.claim_row("lem:promoted")["status"] == str(
                ClaimState.DIRTY_SOURCE
            )
            assert store.question_row("Q0001")["status"] == "SUPERSEDED"
    finally:
        finish_prepared(session, second.run_id)


def test_prepare_pass_emits_real_source_pipeline_boundaries(tmp_path):
    _source, session = initialize(tmp_path)
    events = []
    prepared = session.prepare_pass(
        event_hook=lambda phase, message, details: events.append(
            (phase, message, details)
        )
    )
    finish_prepared(session, prepared.run_id)
    phases = [phase for phase, _message, _details in events]
    assert phases == [
        "OBSERVING_SOURCE",
        "IMPORTING_SOURCE",
        "IMPORTING_SOURCE",
        "INDEXING",
        "INDEXING",
        "IMPACT_ANALYSIS",
    ]
    assert events[0][2]["main_file"] == "main.tex"
    assert events[3][2]["files"] == 1


def test_status_remains_readable_while_a_verification_writer_holds_the_lock(tmp_path):
    _source, session = initialize(tmp_path)
    with project_lock(session.project, exclusive=True):
        status = session.status()
    assert status["mutation_in_progress"] is True
    assert status["claim_states"] == {"DISCOVERED": 2}


def test_identical_pass_reuses_snapshot_without_dirtying_claims(tmp_path):
    _source, session = initialize(tmp_path)
    previous = session.status()["snapshot"]
    prepared = session.prepare_pass()
    try:
        assert prepared.snapshot.identical
        assert prepared.snapshot.commit == previous
        assert prepared.directly_changed == frozenset()
        assert prepared.affected == frozenset()
        with StateStore(session.database_path) as store:
            assert {row["status"] for row in store.current_claim_rows()} == {
                "DISCOVERED"
            }
    finally:
        finish_prepared(session, prepared.run_id)


def test_statement_change_invalidates_only_reverse_dependency_slice(tmp_path):
    source, session = initialize(tmp_path)
    tex = source / "main.tex"
    tex.write_text(
        tex.read_text(encoding="utf-8").replace(
            "For every natural number $n$, one has $0+n=n$.",
            "For every natural number $n$, one has $0+n=n$ by induction.",
        ),
        encoding="utf-8",
    )
    prepared = session.prepare_pass()
    try:
        assert prepared.directly_changed == frozenset({"lem:zero-add"})
        assert prepared.affected == frozenset({"lem:zero-add", "thm:add-zero"})
        with StateStore(session.database_path) as store:
            assert store.claim_row("lem:zero-add")["status"] == "DIRTY_SOURCE"
            assert store.claim_row("thm:add-zero")["status"] == "INVALIDATED"
    finally:
        finish_prepared(session, prepared.run_id)


def test_proof_only_change_is_mode_dependent(tmp_path):
    source, session = initialize(tmp_path / "theorem")
    tex = source / "main.tex"
    tex.write_text(
        tex.read_text(encoding="utf-8").replace(
            "This is the left-zero law", "This follows directly"
        ),
        encoding="utf-8",
    )
    theorem_pass = session.prepare_pass()
    try:
        assert theorem_pass.proof_only_changed == frozenset()
        assert theorem_pass.affected == frozenset()
    finally:
        finish_prepared(session, theorem_pass.run_id)

    audit_source, audit_session = initialize(tmp_path / "audit", argument_audit=True)
    audit_tex = audit_source / "main.tex"
    audit_tex.write_text(
        audit_tex.read_text(encoding="utf-8").replace(
            "This is the left-zero law", "This follows directly"
        ),
        encoding="utf-8",
    )
    audit_pass = audit_session.prepare_pass()
    try:
        assert audit_pass.proof_only_changed == frozenset({"lem:zero-add"})
        assert audit_pass.affected == frozenset({"lem:zero-add", "thm:add-zero"})
    finally:
        finish_prepared(audit_session, audit_pass.run_id)


def test_clarification_requires_diagnostics_is_unique_and_source_change_supersedes(
    tmp_path,
):
    source, session = initialize(tmp_path)
    snapshot = session.status()["snapshot"]
    context = IncrementalAgentContext(
        project=session.project,
        workspace=session.project,
        run_id=1,
        snapshot=snapshot,
        previous_snapshot=None,
        allowed_claims=frozenset({"lem:zero-add"}),
    )
    request = {
        "claim_id": "lem:zero-add",
        "category": "missing_assumption",
        "passage": "For every natural number $n$, one has $0+n=n$.",
        "problem": "The intended number domain needs confirmation.",
        "possible_resolutions": ["Keep natural numbers.", "Use integers."],
    }
    with pytest.raises(ValueError, match="prior diagnostics"):
        context.clarification_request(
            {**request, "diagnostics_performed": ["lean_api_diagnosis"]}
        )
    diagnostics = ["lean_api_diagnosis", "assumption_sufficiency_check"]
    assert "Q0001" in context.clarification_request(
        {**request, "diagnostics_performed": diagnostics}
    )
    assert "Q0001" in context.clarification_request(
        {**request, "diagnostics_performed": diagnostics}
    )
    with StateStore(session.database_path) as store:
        assert len(store.open_questions()) == 1
    tex = source / "main.tex"
    tex.write_text(
        tex.read_text(encoding="utf-8").replace(
            "For every natural number", "For each natural number"
        ),
        encoding="utf-8",
    )
    prepared = session.prepare_pass()
    try:
        with StateStore(session.database_path) as store:
            assert store.open_questions() == []
            row = store.connection.execute(
                "SELECT status FROM clarifications WHERE question_id = 'Q0001'"
            ).fetchone()
            assert row["status"] == "SUPERSEDED"
    finally:
        finish_prepared(session, prepared.run_id)


def test_agent_dependency_tool_rejects_cycles_and_out_of_batch_mutation(tmp_path):
    _source, session = initialize(tmp_path)
    snapshot = session.status()["snapshot"]
    context = IncrementalAgentContext(
        project=session.project,
        workspace=session.project,
        run_id=1,
        snapshot=snapshot,
        previous_snapshot=None,
        allowed_claims=frozenset({"lem:zero-add"}),
    )
    with pytest.raises(ValueError, match="outside this proof batch"):
        context.claim_mark_formalized(
            {
                "claim_id": "thm:add-zero",
                "lean_declaration": "ManuscriptVerification.addZero",
                "result": "formalized",
            }
        )
    with pytest.raises(ValueError, match="create a cycle"):
        context.claim_propose_dependency(
            {
                "claim_id": "lem:zero-add",
                "depends_on": "thm:add-zero",
                "kind": "semantic",
                "reason": "Would reverse the explicit edge.",
            }
        )


def test_agent_discovered_dependency_on_unproved_claim_creates_question(tmp_path):
    _source, session = initialize_document(
        tmp_path,
        r"""
\documentclass{article}
\usepackage{amsthm}
\newtheorem{conjecture}{Conjecture}
\newtheorem{theorem}{Theorem}
\begin{document}
\begin{conjecture}\label{conj:semantic}An unsupported premise.\end{conjecture}
\begin{theorem}\label{thm:semantic}A proved conclusion.\end{theorem}
\begin{proof}The dependency is implicit in the prose.\end{proof}
\end{document}
""",
        targets=("thm:semantic",),
    )
    prepared = session.prepare_pass()
    context = IncrementalAgentContext(
        project=session.project,
        workspace=session.project,
        run_id=prepared.run_id,
        snapshot=prepared.snapshot.commit,
        previous_snapshot=prepared.snapshot.previous_commit,
        allowed_claims=frozenset({"thm:semantic"}),
    )

    try:
        context.claim_propose_dependency(
            {
                "claim_id": "thm:semantic",
                "depends_on": "conj:semantic",
                "kind": "semantic",
                "reason": "The written proof uses the unsupported premise.",
            }
        )
        with StateStore(session.database_path) as store:
            questions = store.open_questions()
            assert len(questions) == 1
            assert questions[0]["claim_id"] == "conj:semantic"
            assert json.loads(str(questions[0]["blocking_claims_json"])) == [
                "thm:semantic"
            ]
            assert store.claim_row("conj:semantic")["status"] == str(
                ClaimState.NEEDS_CLARIFICATION
            )
    finally:
        finish_prepared(session, prepared.run_id)


def test_editing_proof_retires_agent_discovered_dependency_and_question(tmp_path):
    source, session = initialize_document(
        tmp_path,
        r"""
\documentclass{article}
\usepackage{amsthm}
\newtheorem{conjecture}{Conjecture}
\newtheorem{theorem}{Theorem}
\begin{document}
\begin{conjecture}\label{conj:retired}An unsupported premise.\end{conjecture}
\begin{theorem}\label{thm:revised}A proved conclusion.\end{theorem}
\begin{proof}The dependency is implicit in the prose.\end{proof}
\end{document}
""",
        targets=("thm:revised",),
    )
    first = session.prepare_pass()
    context = IncrementalAgentContext(
        project=session.project,
        workspace=session.project,
        run_id=first.run_id,
        snapshot=first.snapshot.commit,
        previous_snapshot=first.snapshot.previous_commit,
        allowed_claims=frozenset({"thm:revised"}),
    )
    context.claim_propose_dependency(
        {
            "claim_id": "thm:revised",
            "depends_on": "conj:retired",
            "kind": "semantic",
            "reason": "The current proof uses the unsupported premise.",
        }
    )
    finish_prepared(session, first.run_id)

    tex = source / "main.tex"
    tex.write_text(
        tex.read_text(encoding="utf-8").replace(
            "The dependency is implicit in the prose.",
            "This revised proof is self-contained.",
        ),
        encoding="utf-8",
    )
    second = session.prepare_pass()
    try:
        assert second.proof_only_changed == frozenset({"thm:revised"})
        assert second.selected == frozenset({"thm:revised"})
        with StateStore(session.database_path) as store:
            assert store.open_questions() == []
            assert store.manuscript_edges() == []
            assert store.claim_row("conj:retired")["status"] == str(
                ClaimState.SKIPPED_UNPROVED
            )
            assert store.question_row("Q0001")["status"] == "SUPERSEDED"
    finally:
        finish_prepared(session, second.run_id)


def test_correspondence_review_policy_records_unapproved_draft(tmp_path):
    _source, session = initialize(tmp_path)
    snapshot = session.status()["snapshot"]
    context = IncrementalAgentContext(
        project=session.project,
        workspace=session.project,
        run_id=1,
        snapshot=snapshot,
        previous_snapshot=None,
        allowed_claims=frozenset({"lem:zero-add"}),
        require_correspondence_review=True,
    )
    context.claim_mark_formalized(
        {
            "claim_id": "lem:zero-add",
            "lean_declaration": "ManuscriptVerification.zeroAdd",
            "result": "formalized",
        }
    )
    with StateStore(session.database_path) as store:
        mapping = store.correspondence_rows()[0]
        assert mapping["status"] == "proposed_review"
        assert mapping["approved"] == 0
        assert store.claim_row("lem:zero-add")["status"] == "STATEMENT_DRAFTED"


def test_agent_policy_disables_pauses_and_counterexamples(tmp_path):
    _source, session = initialize(tmp_path)
    snapshot = session.status()["snapshot"]
    context = IncrementalAgentContext(
        project=session.project,
        workspace=session.project,
        run_id=1,
        snapshot=snapshot,
        previous_snapshot=None,
        allowed_claims=frozenset({"lem:zero-add"}),
        pause_on_ambiguity=False,
        counterexample_search=False,
    )
    with pytest.raises(ValueError, match="Clarification pauses are disabled"):
        context.clarification_request({})
    with pytest.raises(ValueError, match="Counterexample search is disabled"):
        context.claim_mark_formalized(
            {
                "claim_id": "lem:zero-add",
                "lean_declaration": "ManuscriptVerification.zeroAddCounterexample",
                "result": "counterexample",
            }
        )
    context.claim_report_unresolved(
        {
            "claim_id": "lem:zero-add",
            "message": "A possible counterexample remains to be checked.",
        }
    )
    with StateStore(session.database_path) as store:
        assert store.claim_row("lem:zero-add")["status"] == "SUSPECT_FALSE"


def test_preserve_certified_false_forces_selected_claim_reproof(tmp_path):
    source, session = initialize(tmp_path)
    snapshot = session.status()["snapshot"]
    with StateStore(session.database_path) as store:
        store.set_correspondence(
            "lem:zero-add", "ManuscriptVerification.zeroAdd", run_id=1
        )
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
        store.set_claim_state(
            "lem:zero-add",
            ClaimState.CERTIFIED,
            run_id=1,
            action="test_certificate",
            reason="Set up policy regression",
        )
    task = session.project / "VERIFY.yaml"
    task.write_text(
        task.read_text(encoding="utf-8").replace(
            "preserve_certified: true", "preserve_certified: false"
        ),
        encoding="utf-8",
    )
    prepared = session.prepare_pass()
    try:
        with StateStore(session.database_path) as store:
            assert store.certificate("lem:zero-add") is None
            assert store.claim_row("lem:zero-add")["status"] == "INVALIDATED"
            mapping = store.correspondence_rows()[0]
            assert mapping["status"] == "stale_reproof_requested"
            assert mapping["approved"] == 0
    finally:
        finish_prepared(session, prepared.run_id)
