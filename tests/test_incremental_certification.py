from __future__ import annotations

import json
from pathlib import Path

from proof_assistant.incremental.certification import (
    certify_current_correspondence,
    revalidate_unchanged_certificates,
)
from proof_assistant.incremental.diagnostics import classify_failure
from proof_assistant.incremental.lean import correspondence_discrepancies
from proof_assistant.incremental.models import (
    ClaimState,
    LeanDeclaration,
    ManuscriptEdge,
    SourceObject,
)
from proof_assistant.incremental.store import StateStore


def source_object(claim_id: str, statement_hash: str = "statement") -> SourceObject:
    return SourceObject(
        claim_id=claim_id,
        kind="lemma" if claim_id == "A" else "theorem",
        source_file="main.tex",
        environment="lemma" if claim_id == "A" else "theorem",
        label=claim_id,
        ordinal=1 if claim_id == "A" else 2,
        statement_start=0,
        statement_end=10,
        statement_byte_start=0,
        statement_byte_end=10,
        proof_start=None,
        proof_end=None,
        proof_byte_start=None,
        proof_byte_end=None,
        statement_hash=statement_hash,
        proof_hash="proof",
        normalized_statement_hash=statement_hash,
        statement_text=f"Statement {claim_id}",
        proof_text="",
        references=(),
    )


def declaration(
    name: str,
    *,
    type_hash: str = "type",
    value_hash: str | None = "proof",
    dependencies: tuple[str, ...] = (),
    axioms: tuple[str, ...] = (),
    kind: str = "theorem",
) -> LeanDeclaration:
    return LeanDeclaration(name, kind, type_hash, value_hash, dependencies, axioms)


def initialized_store(tmp_path: Path):
    store = StateStore(tmp_path / "state.sqlite3")
    run_id = store.begin_run(command="verify", started_at="now")
    objects = (source_object("A"), source_object("B"))
    store.replace_current_claims(
        "snapshot",
        objects,
        run_id=run_id,
        state_updates={item.claim_id: ClaimState.PROVING for item in objects},
    )
    edges = (ManuscriptEdge("B", "A", "explicit_ref", "latex_ref"),)
    store.replace_manuscript_edges("snapshot", edges)
    return store, run_id, objects, edges


def test_certification_requires_dependencies_then_records_kernel_provenance(tmp_path):
    store, run_id, objects, edges = initialized_store(tmp_path)
    try:
        store.set_correspondence("A", "ManuscriptVerification.A", run_id=run_id)
        store.set_correspondence("B", "ManuscriptVerification.B", run_id=run_id)
        declarations = (
            declaration("ManuscriptVerification.A", axioms=("propext",)),
            declaration(
                "ManuscriptVerification.B",
                dependencies=("ManuscriptVerification.A",),
            ),
        )
        result = certify_current_correspondence(
            store,
            run_id=run_id,
            snapshot="snapshot",
            objects=objects,
            edges=edges,
            declarations=declarations,
            environment_hash="environment",
            lean_version="4.28.0",
            mathlib_revision="mathlib",
            baseline_project_axioms=set(),
        )
        assert result.certified == ("A", "B")
        repeated = certify_current_correspondence(
            store,
            run_id=run_id,
            snapshot="snapshot",
            objects=objects,
            edges=edges,
            declarations=declarations,
            environment_hash="environment",
            lean_version="4.28.0",
            mathlib_revision="mathlib",
            baseline_project_axioms=set(),
        )
        assert repeated.reconciled == ()
        certificate = store.certificate("B")
        assert certificate["formal_type_hash"] == "type"
        assert json.loads(certificate["dependencies_json"]) == ["A"]
        assert json.loads(certificate["lean_dependencies_json"]) == [
            "ManuscriptVerification.A"
        ]
        assert store.claim_row("B")["status"] == "CERTIFIED"
    finally:
        store.close()


def test_certification_rejects_sorry_new_axioms_direct_axioms_and_missing_values(
    tmp_path,
):
    cases = (
        (declaration("ManuscriptVerification.A", axioms=("sorryAx",)), "forbidden"),
        (
            declaration(
                "ManuscriptVerification.A",
                axioms=("ManuscriptVerification.fabricated",),
            ),
            "newly introduced",
        ),
        (declaration("ManuscriptVerification.A", kind="axiom"), "directly to an axiom"),
        (declaration("ManuscriptVerification.A", value_hash=None), "no proof/value"),
    )
    for index, (bad_declaration, expected) in enumerate(cases):
        root = tmp_path / str(index)
        root.mkdir()
        store, run_id, objects, edges = initialized_store(root)
        try:
            store.set_correspondence("A", bad_declaration.name, run_id=run_id)
            result = certify_current_correspondence(
                store,
                run_id=run_id,
                snapshot="snapshot",
                objects=objects,
                edges=edges,
                declarations=(bad_declaration,),
                environment_hash="environment",
                lean_version="4.28.0",
                mathlib_revision=None,
                baseline_project_axioms=set(),
            )
            assert expected in result.rejected[0][1]
            assert store.certificate("A") is None
            assert store.claim_row("A")["status"] == "FAILED_FORMALIZATION"
        finally:
            store.close()


def test_same_formal_type_reconciles_changed_source_without_reproof(tmp_path):
    store, run_id, objects, edges = initialized_store(tmp_path)
    try:
        store.set_correspondence("A", "ManuscriptVerification.A", run_id=run_id)
        decl = declaration("ManuscriptVerification.A", type_hash="stable-type")
        first = certify_current_correspondence(
            store,
            run_id=run_id,
            snapshot="snapshot",
            objects=objects,
            edges=edges,
            declarations=(decl,),
            environment_hash="env-a",
            lean_version="4.28.0",
            mathlib_revision=None,
            baseline_project_axioms=set(),
        )
        assert first.certified == ("A",)
        second_run = store.begin_run(command="verify", started_at="later")
        changed = (source_object("A", "changed-source"), source_object("B"))
        store.replace_current_claims(
            "snapshot-2",
            changed,
            run_id=second_run,
            state_updates={"A": ClaimState.DIRTY_SOURCE},
        )
        store.set_correspondence("A", decl.name, run_id=second_run)
        second = certify_current_correspondence(
            store,
            run_id=second_run,
            snapshot="snapshot-2",
            objects=changed,
            edges=edges,
            declarations=(decl,),
            environment_hash="env-b",
            lean_version="4.28.0",
            mathlib_revision=None,
            baseline_project_axioms=set(),
        )
        assert second.reconciled == ("A",)
        assert store.certificate("A")["statement_hash"] == "changed-source"
    finally:
        store.close()


def test_environment_revalidation_reuses_only_matching_formal_types(tmp_path):
    store, run_id, objects, edges = initialized_store(tmp_path)
    try:
        store.set_correspondence("A", "ManuscriptVerification.A", run_id=run_id)
        decl = declaration("ManuscriptVerification.A", type_hash="stable")
        certify_current_correspondence(
            store,
            run_id=run_id,
            snapshot="snapshot",
            objects=objects,
            edges=edges,
            declarations=(decl,),
            environment_hash="old-env",
            lean_version="4.28.0",
            mathlib_revision=None,
            baseline_project_axioms=set(),
        )
        new_run = store.begin_run(command="verify", started_at="later")
        reused = revalidate_unchanged_certificates(
            store,
            run_id=new_run,
            snapshot="snapshot",
            objects=objects,
            declarations=(decl,),
            environment_hash="new-env",
            lean_version="4.28.0",
            mathlib_revision="new-mathlib",
        )
        assert reused == ("A",)
        assert store.certificate("A")["environment_hash"] == "new-env"
        assert (
            revalidate_unchanged_certificates(
                store,
                run_id=new_run,
                snapshot="snapshot",
                objects=objects,
                declarations=(declaration(decl.name, type_hash="different"),),
                environment_hash="newer",
                lean_version="4.28.0",
                mathlib_revision=None,
            )
            == ()
        )
    finally:
        store.close()


def test_dependency_discrepancy_uses_actual_lean_edges():
    declarations = (
        declaration(
            "ManuscriptVerification.T",
            dependencies=("ManuscriptVerification.A", "ManuscriptVerification.B"),
        ),
    )
    discrepancies = correspondence_discrepancies(
        [("T", "A")],
        declarations,
        {
            "T": "ManuscriptVerification.T",
            "A": "ManuscriptVerification.A",
            "B": "ManuscriptVerification.B",
        },
    )
    assert discrepancies == [
        {
            "claim": "T",
            "lean_declaration": "ManuscriptVerification.T",
            "missing_manuscript_dependency": "B",
            "lean_dependency": "ManuscriptVerification.B",
        }
    ]


def test_failure_classifier_never_equates_unknown_proof_failure_with_falsehood():
    assert classify_failure("failed to synthesize Fintype X") == "typeclass_failure"
    assert classify_failure("unexpected token 'end'") == "lean_syntax"
    assert classify_failure("proof search exhausted") == "unknown"
