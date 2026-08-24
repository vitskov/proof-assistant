from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from .lean import reject_forbidden_axioms
from .models import ClaimState, LeanDeclaration, ManuscriptEdge, SourceObject
from .store import StateStore


@dataclass(frozen=True)
class CertificationResult:
    certified: tuple[str, ...]
    reconciled: tuple[str, ...]
    counterexamples: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]


def certify_current_correspondence(
    store: StateStore,
    *,
    run_id: int,
    snapshot: str,
    objects: Sequence[SourceObject],
    edges: Sequence[ManuscriptEdge],
    declarations: Sequence[LeanDeclaration],
    environment_hash: str,
    lean_version: str,
    mathlib_revision: str | None,
    baseline_project_axioms: set[str],
) -> CertificationResult:
    object_map = {item.claim_id: item for item in objects}
    declaration_map = {item.name: item for item in declarations}
    dependencies: dict[str, list[str]] = {item.claim_id: [] for item in objects}
    for edge in edges:
        if edge.approved and edge.src in dependencies:
            dependencies[edge.src].append(edge.dst)
    certified: list[str] = []
    reconciled: list[str] = []
    counterexamples: list[str] = []
    rejected: list[tuple[str, str]] = []
    for correspondence in store.correspondence_rows():
        if int(correspondence["last_updated_run"]) != run_id:
            continue
        if not bool(correspondence["approved"]):
            continue
        claim_id = str(correspondence["claim_id"])
        declaration_name = str(correspondence["lean_declaration"])
        source = object_map.get(claim_id)
        declaration = declaration_map.get(declaration_name)
        if source is None:
            rejected.append((claim_id, "claim is not present in the current snapshot"))
            continue
        if declaration is None:
            rejected.append(
                (claim_id, f"Lean declaration does not exist: {declaration_name}")
            )
            continue
        if declaration.kind == "axiom":
            rejected.append(
                (claim_id, "a manuscript certificate cannot map directly to an axiom")
            )
            continue
        if declaration.value_hash is None:
            rejected.append((claim_id, "Lean declaration has no proof/value body"))
            continue
        uncertified_dependencies: list[str] = []
        for dependency in dependencies.get(claim_id, []):
            dependency_row = store.claim_row(dependency)
            if (
                dependency_row is None
                or dependency_row["status"] != ClaimState.CERTIFIED
            ):
                uncertified_dependencies.append(dependency)
        if (
            uncertified_dependencies
            and str(correspondence["status"]) != "counterexample"
        ):
            # A semantic edge may have been discovered during this batch. Keep
            # the correspondence pending and let the scheduler certify its new
            # prerequisites first.
            continue
        forbidden = reject_forbidden_axioms(
            declaration, baseline_project_axioms=baseline_project_axioms
        )
        if forbidden:
            rejected.append(
                (
                    claim_id,
                    "forbidden or newly introduced axioms: " + ", ".join(forbidden),
                )
            )
            continue
        previous = store.certificate(claim_id)
        claim_before = store.claim_row(claim_id)
        was_reconciled = bool(
            previous
            and previous["formal_type_hash"] == declaration.type_hash
            and previous["status"] == "CERTIFIED"
            and claim_before is not None
            and claim_before["status"]
            in {ClaimState.DIRTY_SOURCE, ClaimState.INVALIDATED}
        )
        status = str(correspondence["status"])
        is_counterexample = status == "counterexample"
        certificate_status = "COUNTEREXAMPLE" if is_counterexample else "CERTIFIED"
        store.upsert_certificate(
            {
                "claim_id": claim_id,
                "status": certificate_status,
                "manuscript_snapshot": snapshot,
                "statement_hash": source.statement_hash,
                "formal_type_hash": declaration.type_hash,
                "lean_declaration": declaration.name,
                "lean_value_hash": declaration.value_hash,
                "dependencies": sorted(set(dependencies.get(claim_id, []))),
                "lean_dependencies": declaration.direct_dependencies,
                "axioms": declaration.axioms,
                "environment_hash": environment_hash,
                "lean_version": lean_version,
                "mathlib_revision": mathlib_revision,
                "last_verified_run": run_id,
            }
        )
        if is_counterexample:
            store.set_claim_state(
                claim_id,
                ClaimState.COUNTEREXAMPLE_FOUND,
                run_id=run_id,
                action="certify_counterexample",
                reason=f"Lean certified counterexample declaration {declaration.name}",
            )
            counterexamples.append(claim_id)
        else:
            store.set_claim_state(
                claim_id,
                ClaimState.CERTIFIED,
                run_id=run_id,
                action="reconcile" if was_reconciled else "certify",
                reason=(
                    "Formal type unchanged; prior proof certificate retained"
                    if was_reconciled
                    else f"Lean accepted {declaration.name} and the independent project build passed"
                ),
                reused=was_reconciled,
            )
            certified.append(claim_id)
            if was_reconciled:
                reconciled.append(claim_id)
    for claim_id, reason in rejected:
        store.set_claim_state(
            claim_id,
            ClaimState.FAILED_FORMALIZATION,
            run_id=run_id,
            action="reject_certificate",
            reason=reason,
        )
        store.add_diagnostic(
            run_id=run_id,
            claim_id=claim_id,
            category="formalization_mismatch",
            message=reason,
        )
    return CertificationResult(
        certified=tuple(sorted(certified)),
        reconciled=tuple(sorted(reconciled)),
        counterexamples=tuple(sorted(counterexamples)),
        rejected=tuple(sorted(rejected)),
    )


def revalidate_unchanged_certificates(
    store: StateStore,
    *,
    run_id: int,
    snapshot: str,
    objects: Sequence[SourceObject],
    declarations: Sequence[LeanDeclaration],
    environment_hash: str,
    lean_version: str,
    mathlib_revision: str | None,
) -> tuple[str, ...]:
    """Retain certificates after environment checks when source/formal types agree."""
    object_map = {item.claim_id: item for item in objects}
    declaration_map = {item.name: item for item in declarations}
    reused: list[str] = []
    for certificate in store.certificate_rows():
        claim_id = str(certificate["claim_id"])
        claim = store.claim_row(claim_id)
        source = object_map.get(claim_id)
        declaration = declaration_map.get(str(certificate["lean_declaration"]))
        if (
            claim is None
            or claim["status"] != ClaimState.CERTIFIED
            or source is None
            or declaration is None
            or declaration.type_hash != certificate["formal_type_hash"]
            or declaration.value_hash is None
            or any(
                name == "sorryAx" or name.endswith(".sorryAx")
                for name in declaration.axioms
            )
        ):
            continue
        store.upsert_certificate(
            {
                "claim_id": claim_id,
                "status": certificate["status"],
                "manuscript_snapshot": snapshot,
                "statement_hash": source.statement_hash,
                "formal_type_hash": declaration.type_hash,
                "lean_declaration": declaration.name,
                "lean_value_hash": declaration.value_hash,
                "dependencies": json.loads(certificate["dependencies_json"]),
                "lean_dependencies": declaration.direct_dependencies,
                "axioms": declaration.axioms,
                "environment_hash": environment_hash,
                "lean_version": lean_version,
                "mathlib_revision": mathlib_revision,
                "last_verified_run": run_id,
            }
        )
        store.set_claim_state(
            claim_id,
            ClaimState.CERTIFIED,
            run_id=run_id,
            action="environment_revalidate",
            reason="Source and formal type unchanged; project rebuilt in the current environment",
            reused=True,
        )
        reused.append(claim_id)
    return tuple(sorted(reused))
