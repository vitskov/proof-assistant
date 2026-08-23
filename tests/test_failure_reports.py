from __future__ import annotations

import json
from pathlib import Path

import pytest

from proof_assistant.incremental.failures import build_failure_report
from proof_assistant.incremental.models import ClaimState, ManuscriptEdge, SourceObject
from proof_assistant.incremental.store import StateStore
from proof_assistant.workflow.contracts import (
    FailureKind,
    FailureOutlineNode,
    FailureScope,
    NewProjectRequest,
    contract_dict,
)
from proof_assistant.workflow.service import ProofAssistantWorkflow


def _source_object(claim_id: str, ordinal: int) -> SourceObject:
    line = ordinal * 10 + 1
    return SourceObject(
        claim_id=claim_id,
        kind="theorem" if claim_id.startswith("T") else "lemma",
        source_file="main.tex",
        environment="theorem",
        label=claim_id,
        ordinal=ordinal,
        statement_start=line,
        statement_end=line + 1,
        statement_byte_start=ordinal * 100,
        statement_byte_end=ordinal * 100 + 50,
        proof_start=None,
        proof_end=None,
        proof_byte_start=None,
        proof_byte_end=None,
        statement_hash=f"statement-{claim_id}",
        proof_hash=f"proof-{claim_id}",
        normalized_statement_hash=f"normalized-{claim_id}",
        statement_text=f"Statement {claim_id}",
        proof_text="",
        references=(),
    )


@pytest.fixture
def report_factory(tmp_path):
    stores: list[StateStore] = []

    def create(
        claim_ids: tuple[str, ...],
        *,
        targets: tuple[str, ...],
        edges: tuple[tuple[str, str], ...] = (),
        states: dict[str, ClaimState] | None = None,
        outcome: str = "lean_infrastructure_failure",
    ) -> tuple[Path, StateStore, int]:
        project = tmp_path / f"project-{len(stores) + 1}"
        project.mkdir()
        store = StateStore(project / "state.sqlite3")
        stores.append(store)
        run_id = store.begin_run(
            command="manuscript verify",
            started_at="2026-08-23T00:00:00+00:00",
            snapshot_commit="snapshot-1",
        )
        objects = tuple(
            _source_object(claim_id, ordinal)
            for ordinal, claim_id in enumerate(claim_ids, 1)
        )
        state_map = {
            claim_id: (states or {}).get(
                claim_id,
                ClaimState.BLOCKED_DEPENDENCY,
            )
            for claim_id in claim_ids
        }
        store.replace_current_claims(
            "snapshot-1",
            objects,
            run_id=run_id,
            state_updates=state_map,
        )
        store.record_run_scope(run_id, targets=targets, selected=claim_ids)
        store.replace_run_dependency_edges(
            run_id,
            tuple(
                ManuscriptEdge(src, dst, "explicit_ref", "test.fixture")
                for src, dst in edges
            ),
        )
        return project, store, run_id

    yield create
    for store in stores:
        store.close()


def _incident(
    store: StateStore,
    run_id: int,
    claim_ids: tuple[str, ...] = (),
    *,
    scope: FailureScope = FailureScope.CLAIM,
    kind: FailureKind = FailureKind.CLAIM_TECHNICAL,
    message: str = "Lean rejected the proof term",
    provenance: str = "test.kernel",
    retryable: bool = True,
    artifacts: tuple[dict[str, object], ...] = (),
) -> int:
    return store.add_failure_incident(
        run_id=run_id,
        scope=str(scope),
        failure_kind=str(kind),
        phase="CERTIFICATION",
        category="lean_elaboration",
        message=message,
        detail="Exact persisted technical detail",
        provenance=provenance,
        claim_ids=claim_ids,
        retryable=retryable,
        artifacts=artifacts,
    )


def _finish_and_build(
    project: Path, store: StateStore, run_id: int, *, outcome: str = "failed"
):
    store.record_run_claim_nodes(run_id)
    store.finish_run(
        run_id,
        status="FAILED",
        outcome=outcome,
        completed_at="2026-08-23T00:01:00+00:00",
        detail="Verification stopped at an exact persisted failure",
    )
    report = build_failure_report(project, store, run_id)
    assert report is not None
    return report


def _flatten_outline(
    roots: tuple[FailureOutlineNode, ...],
) -> tuple[FailureOutlineNode, ...]:
    result: list[FailureOutlineNode] = []

    def visit(node: FailureOutlineNode) -> None:
        result.append(node)
        for child in node.children:
            visit(child)

    for root in roots:
        visit(root)
    return tuple(result)


def test_first_blocker_is_independent_of_incident_insertion_order(report_factory):
    project, store, run_id = report_factory(
        ("T", "A", "Z"), targets=("T",), edges=(("T", "Z"), ("T", "A"))
    )
    z_incident = _incident(store, run_id, ("Z",), message="z failure")
    a_incident = _incident(store, run_id, ("A",), message="a failure")

    report = _finish_and_build(project, store, run_id)

    assert z_incident < a_incident  # Deliberately opposite canonical order.
    assert report.first_blocker is not None
    assert report.first_blocker.claims == ("T", "A")
    assert report.first_blocker.blocker == "A"
    assert report.primary_incident_id == a_incident


def test_normal_dependency_tree_has_target_to_blocker_path_and_outline(
    report_factory,
):
    project, store, run_id = report_factory(
        ("T", "Middle", "Leaf"),
        targets=("T",),
        edges=(("T", "Middle"), ("Middle", "Leaf")),
        states={"Leaf": ClaimState.FAILED_TECHNICAL},
    )
    incident_id = _incident(store, run_id, ("Leaf",))

    report = _finish_and_build(project, store, run_id)

    assert report.first_blocker is not None
    assert report.first_blocker.claims == ("T", "Middle", "Leaf")
    assert report.paths == (report.first_blocker,)
    assert report.has_cycles is False
    assert report.components == ()
    assert report.outline[0].claim_id == "T"
    assert report.outline[0].children[0].claim_id == "Middle"
    leaf = report.outline[0].children[0].children[0]
    assert leaf.claim_id == "Leaf"
    assert leaf.blocker is True
    assert leaf.incident_ids == (incident_id,)


def test_diamond_preserves_shared_dependency_without_recursive_duplication(
    report_factory,
):
    project, store, run_id = report_factory(
        ("T", "Left", "Right", "Shared"),
        targets=("T",),
        edges=(
            ("T", "Left"),
            ("T", "Right"),
            ("Left", "Shared"),
            ("Right", "Shared"),
        ),
        states={"Shared": ClaimState.FAILED_TECHNICAL},
    )
    _incident(store, run_id, ("Shared",))

    report = _finish_and_build(project, store, run_id)
    occurrences = [
        node for node in _flatten_outline(report.outline) if node.claim_id == "Shared"
    ]

    assert len([node for node in report.nodes if node.claim_id == "Shared"]) == 1
    assert len(occurrences) == 2
    assert sum(node.shared_reference for node in occurrences) == 1
    assert all(not node.children for node in occurrences)
    assert report.first_blocker is not None
    assert report.first_blocker.claims == ("T", "Left", "Shared")


def test_cycle_uses_finite_component_fallback_instead_of_recursive_outline(
    report_factory,
):
    project, store, run_id = report_factory(
        ("T", "A", "B"),
        targets=("T",),
        edges=(("T", "A"), ("A", "B"), ("B", "A")),
        states={"B": ClaimState.FAILED_TECHNICAL},
    )
    incident_id = _incident(store, run_id, ("B",))

    report = _finish_and_build(project, store, run_id)

    assert report.has_cycles is True
    assert report.outline == ()
    cyclic = next(component for component in report.components if component.cyclic)
    assert cyclic.members == ("A", "B")
    assert cyclic.blocker is True
    assert cyclic.incident_ids == (incident_id,)
    assert len(report.components) == 2
    assert len(report.component_edges) == 1


def test_deep_acyclic_report_and_copy_payload_do_not_use_python_recursion(
    report_factory,
):
    claim_ids = tuple(f"N{index:04d}" for index in range(1205))
    project, store, run_id = report_factory(
        claim_ids,
        targets=(claim_ids[0],),
        edges=tuple(zip(claim_ids[:-1], claim_ids[1:], strict=True)),
        states={claim_ids[-1]: ClaimState.FAILED_TECHNICAL},
    )
    _incident(store, run_id, (claim_ids[-1],))

    report = _finish_and_build(project, store, run_id)
    payload = contract_dict(report)

    assert report.has_cycles is False
    assert report.first_blocker is not None
    assert len(report.first_blocker.claims) == len(claim_ids)
    assert len(payload["outline"]) == len(claim_ids)
    assert payload["outline_format"] == "flat_parent_indexed"
    assert json.loads(json.dumps(payload))["outline"][-1]["depth"] == 1204


@pytest.mark.parametrize("scope", [FailureScope.BATCH, FailureScope.RUN])
def test_batch_and_run_incidents_are_explicit_global_roots(report_factory, scope):
    project, store, run_id = report_factory(("T", "A"), targets=("T",))
    affected = ("A",) if scope == FailureScope.BATCH else ()
    incident_id = _incident(
        store,
        run_id,
        affected,
        scope=scope,
        kind=(
            FailureKind.BATCH_TECHNICAL
            if scope == FailureScope.BATCH
            else FailureKind.INFRASTRUCTURE
        ),
    )

    report = _finish_and_build(project, store, run_id)

    assert report.global_incident_ids == (incident_id,)
    assert report.primary_incident_id == incident_id
    assert report.first_blocker is None
    pseudo_root = report.outline[0]
    assert pseudo_root.claim_id == f"incident:{incident_id}"
    assert pseudo_root.incident_ids == (incident_id,)
    assert tuple(node.claim_id for node in pseudo_root.children) == (affected or ("T",))


def test_blocked_ancestors_inherit_exact_reason_only_through_provenance_path(
    report_factory,
):
    project, store, run_id = report_factory(
        ("T", "Dependent", "Cause"),
        targets=("T",),
        edges=(("T", "Dependent"), ("Dependent", "Cause")),
        states={"Cause": ClaimState.FAILED_TECHNICAL},
    )
    incident_id = _incident(
        store,
        run_id,
        ("Cause",),
        message="Compiler exhausted memory at the recorded command",
        provenance="orchestration.independent_build",
    )

    report = _finish_and_build(project, store, run_id)
    nodes = {node.claim_id: node for node in report.nodes}
    incident = next(
        item for item in report.incidents if item.incident_id == incident_id
    )

    assert nodes["T"].incident_ids == ()
    assert nodes["Dependent"].incident_ids == ()
    assert nodes["Cause"].incident_ids == (incident_id,)
    assert report.first_blocker is not None
    assert report.first_blocker.claims == ("T", "Dependent", "Cause")
    assert incident.message == "Compiler exhausted memory at the recorded command"
    assert incident.provenance == "orchestration.independent_build"


def test_historical_technical_failure_is_retryable_and_state_is_immutable(
    report_factory,
):
    project, store, run_id = report_factory(
        ("T",),
        targets=("T",),
        states={"T": ClaimState.FAILED_TECHNICAL},
    )
    _incident(store, run_id, ("T",), retryable=True)
    original = _finish_and_build(project, store, run_id)

    retry_run = store.begin_run(
        command="manuscript verify",
        started_at="2026-08-23T00:02:00+00:00",
        snapshot_commit="snapshot-1",
    )
    store.set_claim_state(
        "T",
        ClaimState.INVALIDATED,
        run_id=retry_run,
        action="retry",
        reason="Retry the exact technical failure",
    )
    store.finish_run(
        retry_run,
        status="INTERRUPTED",
        outcome="interrupted",
        completed_at="2026-08-23T00:03:00+00:00",
        detail="Safe cancellation before retry",
    )

    historical = build_failure_report(project, store, run_id)
    latest_failure = build_failure_report(project, store)

    assert historical is not None
    assert latest_failure is not None
    assert historical.nodes[0].state == ClaimState.FAILED_TECHNICAL
    assert historical.incidents[0].retryable is True
    assert contract_dict(historical) == contract_dict(original)
    assert latest_failure.run_id == run_id


def test_default_loader_finds_legacy_failure_before_newer_nonfailure_run(
    report_factory,
):
    project, store, failed_run = report_factory(
        ("T", "A"),
        targets=("T",),
        edges=(("T", "A"),),
        states={
            "T": ClaimState.FAILED_TECHNICAL,
            "A": ClaimState.FAILED_TECHNICAL,
        },
    )
    reason = (
        "RuntimeError: RepoProver is not importable in the active Python "
        "environment\nModuleNotFoundError: No module named 'repoprover'"
    )
    for claim_id in ("T", "A"):
        store.set_claim_state(
            claim_id,
            ClaimState.FAILED_TECHNICAL,
            run_id=failed_run,
            action="batch_failure",
            reason=reason,
        )
    store.finish_run(
        failed_run,
        status="INTERRUPTED",
        outcome="interrupted",
        completed_at="2026-08-23T00:01:00+00:00",
        detail="Legacy run was cancelled after the technical failure",
    )

    newer_run = store.begin_run(
        command="manuscript verify",
        started_at="2026-08-23T00:02:00+00:00",
        snapshot_commit="snapshot-1",
    )
    store.finish_run(
        newer_run,
        status="COMPLETE",
        outcome="partial_unresolved",
        completed_at="2026-08-23T00:03:00+00:00",
        detail="A newer run completed without new-format failure incidents",
    )

    report = build_failure_report(project, store)

    assert report is not None
    assert report.run_id == failed_run
    assert len(report.incidents) == 1
    assert report.incidents[0].message == reason
    assert report.incidents[0].scope == FailureScope.RUN
    assert report.incidents[0].kind == FailureKind.INFRASTRUCTURE
    assert report.incidents[0].claim_ids == ("A", "T")
    assert report.incidents[0].provenance == "legacy.run_claims.coalesced_batch"
    assert report.first_blocker is None
    assert report.outline[0].claim_id == "incident:-1"
    flattened = _flatten_outline(report.outline)
    affected = [node for node in flattened if node.claim_id in {"A", "T"}]
    assert affected
    assert all(node.state == "BLOCKED_BY_GLOBAL_INCIDENT" for node in affected)
    assert all(node.blocker is False for node in affected)


def test_legacy_batch_failure_stays_batch_scoped_without_claim_blame(
    report_factory,
):
    project, store, run_id = report_factory(
        ("T", "A"),
        targets=("T",),
        edges=(("T", "A"),),
        states={
            "T": ClaimState.FAILED_TECHNICAL,
            "A": ClaimState.FAILED_TECHNICAL,
        },
    )
    reason = "Batch final build failed"
    for claim_id in ("T", "A"):
        store.set_claim_state(
            claim_id,
            ClaimState.FAILED_TECHNICAL,
            run_id=run_id,
            action="batch_failure",
            reason=reason,
        )
    store.finish_run(
        run_id,
        status="FAILED",
        outcome="lean_infrastructure_failure",
        completed_at="2026-08-23T00:01:00+00:00",
        detail=reason,
    )

    report = build_failure_report(project, store, run_id)

    assert report is not None
    assert len(report.incidents) == 1
    incident = report.incidents[0]
    assert incident.scope == FailureScope.BATCH
    assert incident.kind == FailureKind.BATCH_TECHNICAL
    assert incident.claim_ids == ("A", "T")
    assert incident.batch_index is None
    assert "did not preserve a batch identifier" in (incident.detail or "")
    assert report.first_blocker is None
    affected = [
        node for node in _flatten_outline(report.outline) if node.claim_id in {"A", "T"}
    ]
    assert affected
    assert all(node.state == "BLOCKED_BY_GLOBAL_INCIDENT" for node in affected)
    assert all(node.blocker is False for node in affected)


def test_legacy_unknown_incident_kind_degrades_and_copy_payload_is_json(
    report_factory,
):
    project, store, run_id = report_factory(("T",), targets=("T",))
    cursor = store.connection.execute(
        """
        INSERT INTO failure_incidents(
            run_id, scope, failure_kind, phase, category, message,
            detail, provenance, batch_index, retryable
        ) VALUES (?, 'FUTURE_SCOPE', 'FUTURE_KIND', 'FUTURE_PHASE',
                  'future', 'A future producer reason', NULL, 'legacy.db', NULL, 1)
        """,
        (run_id,),
    )
    incident_id = int(cursor.lastrowid)
    report = _finish_and_build(project, store, run_id)

    assert report.incidents[0].incident_id == incident_id
    assert report.incidents[0].scope == FailureScope.RUN
    assert report.incidents[0].kind == FailureKind.UNKNOWN
    payload = contract_dict(report)
    copied = json.dumps(payload, sort_keys=True)
    assert "A future producer reason" in copied
    assert "FUTURE_PHASE" in copied

    with pytest.raises(ValueError, match="Invalid failure kind"):
        store.add_failure_incident(
            run_id=run_id,
            scope="RUN",
            failure_kind="NOT_A_KIND",
            phase="REPORTING",
            category="invalid",
            message="invalid",
            provenance="test",
        )


def test_copy_payload_preserves_exact_artifact_command_and_exit_status(
    report_factory,
):
    project, store, run_id = report_factory(
        ("T",), targets=("T",), states={"T": ClaimState.FAILED_TECHNICAL}
    )
    log = project / "round-build.log"
    log.write_text("exact Lean failure\n", encoding="utf-8")
    incident_id = _incident(
        store,
        run_id,
        ("T",),
        artifacts=(
            {
                "path": log,
                "label": "Merged Lean build",
                "sha256": "abc123",
                "command": ("lake", "build"),
                "exit_code": 1,
                "timed_out": False,
            },
        ),
    )
    report = _finish_and_build(project, store, run_id)
    payload = contract_dict(report)
    incident = next(
        item for item in payload["incidents"] if item["incident_id"] == incident_id
    )

    assert incident["artifacts"] == [
        {
            "path": str(log),
            "label": "Merged Lean build",
            "sha256": "abc123",
            "command": ["lake", "build"],
            "exit_code": 1,
            "timed_out": False,
        }
    ]
    assert "lake" in json.dumps(payload)


def test_safe_cancellation_without_failure_incident_does_not_invent_a_blocker(
    report_factory,
):
    project, store, run_id = report_factory(
        ("T",), targets=("T",), states={"T": ClaimState.INVALIDATED}
    )
    store.record_run_claim_nodes(run_id)
    store.finish_run(
        run_id,
        status="INTERRUPTED",
        outcome="interrupted",
        completed_at="2026-08-23T00:01:00+00:00",
        detail="Safely cancelled before a failure occurred",
    )

    assert build_failure_report(project, store, run_id) is None


def test_workflow_service_loads_exact_historical_failure_report(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_text(
        r"\documentclass{article}"
        r"\newtheorem{theorem}{Theorem}"
        r"\begin{document}"
        r"\begin{theorem}\label{T}Statement.\end{theorem}"
        r"\end{document}",
        encoding="utf-8",
    )
    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog", use_codex_clarification=False
    )
    project = tmp_path / "managed"
    workflow.create_project(
        NewProjectRequest("Failure report", source, "main.tex", project_path=project)
    )

    database = project / ".repoprover" / "state.sqlite3"
    with StateStore(database) as store:
        snapshot = store.previous_snapshot()
        assert snapshot is not None
        run_id = store.begin_run(
            command="manuscript verify",
            started_at="2026-08-23T00:02:00+00:00",
            snapshot_commit=snapshot,
        )
        store.record_run_scope(run_id, targets=("T",), selected=("T",))
        store.replace_run_dependency_edges(run_id, ())
        store.set_claim_state(
            "T",
            ClaimState.FAILED_TECHNICAL,
            run_id=run_id,
            action="test_failure",
            reason="Exact service-boundary failure",
        )
        incident_id = _incident(
            store,
            run_id,
            ("T",),
            message="Exact service-boundary failure",
        )
        store.record_run_claim_nodes(run_id)
        store.finish_run(
            run_id,
            status="FAILED",
            outcome="lean_infrastructure_failure",
            completed_at="2026-08-23T00:03:00+00:00",
            detail="Exact service-boundary failure",
        )

    loaded = workflow.load_failure_report(project, run_id)
    latest = workflow.load_failure_report(project)

    assert loaded is not None
    assert latest is not None
    assert loaded.run_id == run_id
    assert loaded.primary_incident_id == incident_id
    assert loaded.first_blocker is not None
    assert loaded.first_blocker.claims == ("T",)
    assert latest == loaded
    assert workflow.load_failure_report(project, run_id + 1000) is None
