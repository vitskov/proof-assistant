from __future__ import annotations

import json
from pathlib import Path

import pytest

from proof_assistant.ai import TaskKind
from proof_assistant.incremental.models import ManuscriptEdge
from proof_assistant.incremental.session import IncrementalSession
from proof_assistant.incremental.store import StateStore
from proof_assistant.presentation.clarification_analysis import validate_analysis
from proof_assistant.presentation.clarifications import ClarificationPresenter
from proof_assistant.workflow.contracts import (
    ClarificationAnalysisStatus,
    ClarificationOrigin,
    NewProjectRequest,
    VerificationRoleSettings,
)
from proof_assistant.workflow.service import ProofAssistantWorkflow
from tests.test_workflow_changes import setup_project


class RecordingAnalyzer:
    provider = "claude_cli"
    model = "fable"
    effort = "xhigh"

    def __init__(self) -> None:
        self.packets = []

    def analyze(self, packet):
        self.packets.append(packet)
        return {
            "hypothesis": "The first statement likely relies on the related theorem.",
            "confidence": "HIGH",
            "reasoning": [
                {
                    "statement": "The evidence records an explicit relationship.",
                    "evidence_ids": [packet.items[0].evidence_id],
                }
            ],
            "alternatives": ["The source may contain a typographical error."],
            "uncertainties": ["Author intent is not mechanically provable."],
            "recommended_author_check": "Confirm the intended theorem dependency.",
        }


class FailingAnalyzer(RecordingAnalyzer):
    def analyze(self, packet):
        self.packets.append(packet)
        raise RuntimeError("secret provider detail")


def _question(tmp_path: Path):
    source, project, _workflow = setup_project(tmp_path)
    session = IncrementalSession(project)
    prepared = session.prepare_pass()
    with StateStore(session.database_path) as store:
        version = store.claim_version(prepared.snapshot.commit, "lem:zero-add")
        assert version is not None
        store.create_question(
            claim_id="lem:zero-add",
            snapshot=prepared.snapshot.commit,
            category="ambiguous_statement",
            passage=str(version["statement_text"]).strip(),
            problem="Clarify the quantifier.",
            possible_resolutions=("State the quantifier explicitly.",),
            blocking_claims=("thm:add-zero",),
            run_id=prepared.run_id,
            origin="PROOF_WORKER",
        )
    return source, project, session, prepared


def _deferred_guided_question(tmp_path: Path):
    source = tmp_path / "guided-paper"
    source.mkdir()
    (source / "main.tex").write_text(
        r"""\documentclass{article}
\usepackage{amsthm}
\newtheorem{theorem}{Theorem}
\begin{document}
%% assistant: D is the abridged corollary of the stronger \ref{U}; this is author guidance only.
\begin{theorem}\label{D}Deferred statement D.\end{theorem}
\begin{theorem}\label{U}Stronger statement U.\end{theorem}
\begin{proof}Proof of U.\end{proof}
\begin{theorem}\label{F}Final theorem using \ref{D}.\end{theorem}
\begin{proof}Proof of F.\end{proof}
\begin{theorem}\label{X}Unrelated theorem X.\end{theorem}
\begin{proof}Proof of X.\end{proof}
\end{document}
""",
        encoding="utf-8",
    )
    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "guided-catalog", use_codex_clarification=False
    )
    project = tmp_path / "guided-project"
    workflow.create_project(
        NewProjectRequest(
            name="Guided",
            source_path=source,
            main_file="main.tex",
            project_path=project,
        )
    )
    session = IncrementalSession(project)
    status = session.status()
    snapshot = str(status["snapshot"])
    run_id = int(status["latest_run"]["run_id"])
    with StateStore(session.database_path) as store:
        version = store.claim_version(snapshot, "D")
        assert version is not None
        store.create_question(
            claim_id="D",
            snapshot=snapshot,
            category="deferred_proof",
            passage=str(version["statement_text"]).strip(),
            problem="D has no attached proof.",
            possible_resolutions=("Confirm the deferred proof-bearing theorem.",),
            blocking_claims=("F",),
            run_id=run_id,
            origin="HOST_POLICY",
        )
        store.add_diagnostic(
            run_id=run_id,
            claim_id="D",
            category="semantic_dependency",
            message="Formalization found the author-guided dependency on U.",
            details={"depends_on": "U", "kind": "semantic"},
        )
        store.add_diagnostic(
            run_id=run_id,
            claim_id="X",
            category="semantic_dependency",
            message="An unrelated theorem also depends on U.",
            details={"depends_on": "U", "kind": "semantic"},
        )
    return source, project, session


def test_packet_includes_full_direct_dependency_endpoint(tmp_path):
    source, project, session, prepared = _question(tmp_path)
    with StateStore(session.database_path) as store:
        store.add_manuscript_edge(
            ManuscriptEdge(
                "lem:zero-add",
                "thm:add-zero",
                "assistant_context",
                "assistant_annotation",
                True,
            ),
            snapshot=prepared.snapshot.commit,
        )

    analyzer = RecordingAnalyzer()
    presentation = ClarificationPresenter(analyzer=analyzer).present_all(
        project, source
    )[0]

    assert presentation.analysis is not None
    assert presentation.analysis.status is ClarificationAnalysisStatus.AVAILABLE
    assert presentation.analysis.origin is ClarificationOrigin.PROOF_WORKER
    related = [
        json.loads(item.content)
        for item in analyzer.packets[0].items
        if item.kind == "related_claim"
    ]
    theorem = next(item for item in related if item["claim_id"] == "thm:add-zero")
    assert theorem["statement"]
    assert "proof" in theorem
    paths = [
        json.loads(item.content)
        for item in analyzer.packets[0].items
        if item.kind == "blocking_path"
    ]
    assert paths == [
        {
            "blocked_claim": "thm:add-zero",
            "dependency_to_dependent_path": ["lem:zero-add", "thm:add-zero"],
        }
    ]


def test_deferred_guided_dependency_closure_contains_d_u_f_evidence(tmp_path):
    source, project, _session = _deferred_guided_question(tmp_path)
    analyzer = RecordingAnalyzer()

    ClarificationPresenter(analyzer=analyzer).present_all(project, source)

    items = analyzer.packets[0].items
    decoded = [(item.kind, json.loads(item.content)) for item in items]
    claim = next(value for kind, value in decoded if kind == "claim_source")
    assert claim["claim_id"] == "D"
    assert claim["assistant_references"] == ["U"]
    assert claim["assistant_context_authority"] == "AUTHOR_ADVISORY_NON_PROOF"
    assistant_edge = next(
        value
        for kind, value in decoded
        if kind == "dependency" and value["kind"] == "assistant_context"
    )
    assert assistant_edge == {
        "approved": True,
        "authority": "AUTHOR_ADVISORY_NON_PROOF",
        "dst": "U",
        "kind": "assistant_context",
        "provenance": "assistant_annotation",
        "src": "D",
    }
    related = {
        value["claim_id"]: value for kind, value in decoded if kind == "related_claim"
    }
    assert set(related) == {"F", "U"}
    assert all("references" in value for value in related.values())
    semantic = [
        value
        for kind, value in decoded
        if kind == "diagnostic" and value["category"] == "semantic_dependency"
    ]
    assert semantic == [
        {
            "category": "semantic_dependency",
            "details": {"depends_on": "U", "kind": "semantic"},
            "message": "Formalization found the author-guided dependency on U.",
        }
    ]


def test_unavailable_does_not_suppress_later_fresh_analysis(tmp_path):
    source, project, session, _prepared = _question(tmp_path)

    unavailable = ClarificationPresenter().present_all(project, source)[0].analysis
    assert unavailable is not None
    assert unavailable.status is ClarificationAnalysisStatus.UNAVAILABLE
    with StateStore(session.database_path) as store:
        count = store.connection.execute(
            "SELECT COUNT(*) FROM clarification_analyses"
        ).fetchone()[0]
    assert count == 0

    analyzer = RecordingAnalyzer()
    available = (
        ClarificationPresenter(analyzer=analyzer)
        .present_all(project, source)[0]
        .analysis
    )
    assert available is not None
    assert available.status is ClarificationAnalysisStatus.AVAILABLE
    assert len(analyzer.packets) == 1


def test_analysis_reused_by_evidence_hash_and_changed_evidence_reanalyzes(tmp_path):
    source, project, session, prepared = _question(tmp_path)
    analyzer = RecordingAnalyzer()
    presenter = ClarificationPresenter(analyzer=analyzer)

    first = presenter.present_all(project, source)[0].analysis
    second = presenter.present_all(project, source)[0].analysis
    assert first == second
    assert len(analyzer.packets) == 1
    original_ids = {item.evidence_id for item in analyzer.packets[0].items}

    with StateStore(session.database_path) as store:
        store.add_diagnostic(
            run_id=prepared.run_id,
            claim_id="lem:zero-add",
            category="new_evidence",
            message="A later deterministic diagnostic was recorded.",
            details={"result": "changed"},
        )
    third = presenter.present_all(project, source)[0].analysis
    assert third is not None and first is not None
    assert third.evidence_sha256 != first.evidence_sha256
    assert len(analyzer.packets) == 2
    assert original_ids < {item.evidence_id for item in analyzer.packets[1].items}


def test_resume_cache_rebuild_never_calls_analyzer(tmp_path):
    source, project, _session, _prepared = _question(tmp_path)
    analyzer = RecordingAnalyzer()
    cache = project / ".repoprover" / "presentations" / "clarifications.json"
    assert not cache.exists()

    presentation = ClarificationPresenter(analyzer=analyzer).load_or_present_all(
        project, source
    )[0]

    assert analyzer.packets == []
    assert presentation.analysis is not None
    assert presentation.analysis.status is ClarificationAnalysisStatus.UNAVAILABLE


def test_analysis_rejects_unknown_evidence_ids(tmp_path):
    source, project, _session, _prepared = _question(tmp_path)
    analyzer = RecordingAnalyzer()
    ClarificationPresenter(analyzer=analyzer).present_all(project, source)
    packet = analyzer.packets[0]
    payload = analyzer.analyze(packet)
    payload["reasoning"][0]["evidence_ids"] = ["E9999"]

    with pytest.raises(ValueError, match="unsupported"):
        validate_analysis(
            payload,
            packet=packet,
            provider=analyzer.provider,
            model=analyzer.model,
            effort=analyzer.effort,
        )


def test_presentation_provenance_covers_generated_analysis(tmp_path):
    source, project, _session, _prepared = _question(tmp_path)
    analyzer = RecordingAnalyzer()
    ClarificationPresenter(analyzer=analyzer).present_all(project, source)
    cache = project / ".repoprover" / "presentations" / "clarifications.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["clarifications"][0]["analysis"]["hypothesis"] = "tampered"
    cache.write_text(json.dumps(payload), encoding="utf-8")

    rebuilt = ClarificationPresenter(analyzer=analyzer).load_or_present_all(
        project, source
    )[0]

    assert rebuilt.analysis is not None
    assert rebuilt.analysis.hypothesis != "tampered"
    assert len(analyzer.packets) == 1


def test_analysis_failure_is_sanitized_and_does_not_suppress_other_questions(tmp_path):
    source, project, session, prepared = _question(tmp_path)
    with StateStore(session.database_path) as store:
        version = store.claim_version(prepared.snapshot.commit, "thm:add-zero")
        assert version is not None
        store.create_question(
            claim_id="thm:add-zero",
            snapshot=prepared.snapshot.commit,
            category="ambiguous_statement",
            passage=str(version["statement_text"]).strip(),
            problem="Clarify the theorem.",
            possible_resolutions=("State the intended theorem.",),
            blocking_claims=("thm:add-zero",),
            run_id=prepared.run_id,
            origin="HOST_POLICY",
        )
    analyzer = FailingAnalyzer()

    presentations = ClarificationPresenter(analyzer=analyzer).present_all(
        project, source
    )

    assert len(presentations) == 2
    assert len(analyzer.packets) == 2
    assert all(
        item.analysis is not None
        and item.analysis.status is ClarificationAnalysisStatus.FAILED
        and item.analysis.failure_detail
        == "Clarification analysis failed or returned invalid data."
        for item in presentations
    )
    assert all("secret" not in item.analysis.failure_detail for item in presentations)


def test_corrupt_first_persisted_analysis_does_not_hide_second_on_resume(tmp_path):
    source, project, session, prepared = _question(tmp_path)
    with StateStore(session.database_path) as store:
        version = store.claim_version(prepared.snapshot.commit, "thm:add-zero")
        assert version is not None
        store.create_question(
            claim_id="thm:add-zero",
            snapshot=prepared.snapshot.commit,
            category="ambiguous_statement",
            passage=str(version["statement_text"]).strip(),
            problem="Clarify the theorem.",
            possible_resolutions=("State the intended theorem.",),
            blocking_claims=("thm:add-zero",),
            run_id=prepared.run_id,
            origin="HOST_POLICY",
        )
    analyzer = RecordingAnalyzer()
    generated = ClarificationPresenter(analyzer=analyzer).present_all(project, source)
    assert len(generated) == 2
    with StateStore(session.database_path) as store:
        store.connection.execute(
            """
            UPDATE clarification_analyses SET analysis_json = 'not-json'
            WHERE question_id = 'Q0001'
            """
        )
    cache = project / ".repoprover" / "presentations" / "clarifications.json"
    cache.unlink()
    resume_analyzer = RecordingAnalyzer()

    resumed = ClarificationPresenter(analyzer=resume_analyzer).load_or_present_all(
        project, source
    )

    assert resume_analyzer.packets == []
    assert len(resumed) == 2
    assert resumed[0].analysis is not None
    assert resumed[0].analysis.status is ClarificationAnalysisStatus.UNAVAILABLE
    assert resumed[1].analysis == generated[1].analysis


def test_forged_persisted_evidence_id_is_unavailable_on_network_free_resume(tmp_path):
    source, project, session, _prepared = _question(tmp_path)
    analyzer = RecordingAnalyzer()
    generated = ClarificationPresenter(analyzer=analyzer).present_all(project, source)
    assert generated[0].analysis is not None
    with StateStore(session.database_path) as store:
        row = store.connection.execute(
            "SELECT analysis_json FROM clarification_analyses WHERE question_id = 'Q0001'"
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["analysis_json"]))
        payload["reasoning"][0]["evidence_ids"] = ["E-FORGED"]
        store.connection.execute(
            """
            UPDATE clarification_analyses SET analysis_json = ?
            WHERE question_id = 'Q0001'
            """,
            (json.dumps(payload),),
        )
    cache = project / ".repoprover" / "presentations" / "clarifications.json"
    cache.unlink()
    resume_analyzer = RecordingAnalyzer()

    resumed = ClarificationPresenter(analyzer=resume_analyzer).load_or_present_all(
        project, source
    )

    assert resume_analyzer.packets == []
    assert resumed[0].analysis is not None
    assert resumed[0].analysis.status is ClarificationAnalysisStatus.UNAVAILABLE
    assert resumed[0].analysis.reasoning == ()
    assert "invalid" in (resumed[0].analysis.failure_detail or "").lower()


def test_clarification_origin_cannot_be_rewritten(tmp_path):
    _source, _project, session, prepared = _question(tmp_path)
    with StateStore(session.database_path) as store:
        with pytest.raises(ValueError, match="immutable"):
            store.refresh_open_question(
                "lem:zero-add",
                snapshot=prepared.snapshot.commit,
                category="ambiguous_statement",
                passage="passage",
                problem="problem",
                possible_resolutions=("repair",),
                blocking_claims=("thm:add-zero",),
                origin="HOST_POLICY",
            )


def test_presenter_consumes_exact_frozen_clarification_role(tmp_path):
    _source, project, workflow = setup_project(tmp_path)
    workflow.use_codex_clarification = True
    role = VerificationRoleSettings(
        task=TaskKind.CLARIFICATION,
        ai_driver="claude_cli",
        model="fable",
        effort="xhigh",
    )

    presenter = workflow._presenter(project, role=role)

    assert presenter.analyzer is not None
    assert presenter.analyzer.provider == "claude_cli"
    assert presenter.analyzer.model == "fable"
    assert presenter.analyzer.effort == "xhigh"
