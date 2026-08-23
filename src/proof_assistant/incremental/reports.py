from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..workflow.contracts import (
    CONTRACT_SCHEMA_VERSION,
    FailureDependencyReport,
    FailureOutlineNode,
    contract_dict,
)
from .io import atomic_write_json, atomic_write_text, canonical_hash
from .lean import correspondence_discrepancies
from .models import LeanDeclaration, ManuscriptEdge
from .store import StateStore


def dependency_audit(
    store: StateStore,
    *,
    edges: Sequence[ManuscriptEdge],
    declarations: Sequence[LeanDeclaration],
) -> dict[str, Any]:
    correspondence = {
        str(row["claim_id"]): str(row["lean_declaration"])
        for row in store.correspondence_rows()
        if bool(row["approved"])
    }
    discrepancies = correspondence_discrepancies(
        [(edge.src, edge.dst) for edge in edges if edge.approved],
        declarations,
        correspondence,
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "discrepancies": discrepancies,
    }
    payload["sha256"] = canonical_hash(payload)
    return payload


def render_report(
    project: Path,
    store: StateStore,
    *,
    run_id: int,
    audit: dict[str, Any],
    reused: Sequence[str],
    reconciled: Sequence[str],
    invalidated: Sequence[str],
    failure_report: FailureDependencyReport | None = None,
) -> None:
    claims = store.current_claim_rows()
    questions = store.open_questions()
    lines = [
        "# Verification Report",
        "",
        "## Current result",
        "",
        f"- Snapshot: `{store.previous_snapshot() or 'none'}`",
        f"- Run: `{run_id}`",
        f"- Certificates reused unchanged: {len(reused)}",
        f"- Textually changed statements reconciled to unchanged formal types: {len(reconciled)}",
        f"- Claims invalidated: {len(invalidated)}",
        f"- Open clarifications: {len(questions)}",
    ]
    concurrency = store.run_concurrency(run_id)
    if concurrency is not None:
        configured = json.dumps(
            concurrency["configured"], ensure_ascii=False, sort_keys=True
        )
        initial = json.dumps(
            concurrency["initial_effective"], ensure_ascii=False, sort_keys=True
        )
        final = json.dumps(
            concurrency["final_effective"] or concurrency["initial_effective"],
            ensure_ascii=False,
            sort_keys=True,
        )
        lines.extend(
            [
                "",
                "## Concurrency provenance",
                "",
                "The limits below are operational settings; Lean certification "
                "remains authoritative regardless of scheduling.",
                "",
                f"- Configured: `{configured}`",
                f"- Effective at start: `{initial}`",
                f"- Effective at finish: `{final}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Claim status",
            "",
            "| Claim | Kind | State | Lean declaration |",
            "|---|---|---|---|",
        ]
    )
    for row in claims:
        declaration = row["lean_declaration"] or "—"
        lines.append(
            f"| `{row['claim_id']}` | {row['kind']} | `{row['status']}` | `{declaration}` |"
        )
    lines.extend(["", "## Dependency audit", ""])
    discrepancies = audit.get("discrepancies", [])
    if discrepancies:
        for item in discrepancies:
            lines.append(
                "- The formal proof of "
                f"`{item['claim']}` depends on `{item['missing_manuscript_dependency']}`, "
                "but the manuscript graph does not record that dependency."
            )
    else:
        lines.append("No mapped manuscript/Lean dependency discrepancy was detected.")
    if failure_report is not None:
        lines.extend(_failure_lines(failure_report))
    lines.extend(
        [
            "",
            "## Interpretation warning",
            "",
            "A failed or unresolved proof search is not evidence that a manuscript statement is false. "
            "Only `CERTIFIED` results have kernel-checked Lean evidence; only "
            "`COUNTEREXAMPLE_FOUND` results have kernel-checked counterexample evidence.",
            "",
        ]
    )
    atomic_write_text(project / "VERIFICATION_REPORT.md", "\n".join(lines))
    atomic_write_json(
        project / ".repoprover" / "exports" / "dependency-audit.json", audit
    )
    failure_payload = (
        contract_dict(failure_report) if failure_report is not None else None
    )
    atomic_write_json(
        project / ".repoprover" / "exports" / "failure-report.json",
        {
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "failure_report": failure_payload,
        },
    )
    if failure_report is not None:
        atomic_write_json(
            project / ".repoprover" / "runs" / f"{run_id:06d}" / "failure-report.json",
            {
                "contract_schema_version": CONTRACT_SCHEMA_VERSION,
                "failure_report": failure_payload,
            },
        )


def _outline_lines(roots: Sequence[FailureOutlineNode]) -> list[str]:
    lines: list[str] = []
    stack = [(root, 0) for root in reversed(roots)]
    while stack:
        node, depth = stack.pop()
        marker = " ↩ shared reference" if node.shared_reference else ""
        blocker = " — blocker" if node.blocker else ""
        lines.append(
            f"{'  ' * depth}- `{node.claim_id}` [{node.state}]{blocker}{marker}"
        )
        stack.extend((child, depth + 1) for child in reversed(node.children))
    return lines


def _failure_lines(report: FailureDependencyReport) -> list[str]:
    lines = ["", "## Failure explanation", ""]
    incident_by_id = {incident.incident_id: incident for incident in report.incidents}
    primary = (
        incident_by_id.get(report.primary_incident_id)
        if report.primary_incident_id is not None
        else None
    )
    if primary is not None:
        lines.extend(
            [
                f"Primary incident: `{primary.scope}/{primary.kind}`",
                "",
                f"- Reason: {primary.message}",
                f"- Provenance: `{primary.provenance}`",
                f"- Phase: `{primary.phase}`",
                f"- Retryable: {'yes' if primary.retryable else 'no'}",
            ]
        )
        if primary.detail:
            lines.append(f"- Detail: {primary.detail}")
        for artifact in primary.artifacts:
            lines.append(f"- Artifact ({artifact.label}): `{artifact.path}`")
    if report.first_blocker is not None:
        lines.extend(
            [
                "",
                "First deterministic blocker path: "
                + " → ".join(f"`{claim}`" for claim in report.first_blocker.claims),
            ]
        )
    if report.has_cycles:
        lines.extend(["", "### Cyclic component view", ""])
        for component in report.components:
            qualifiers = []
            if component.cyclic:
                qualifiers.append("cyclic")
            if component.blocker:
                qualifiers.append("blocker")
            suffix = f" ({', '.join(qualifiers)})" if qualifiers else ""
            lines.append(
                f"- `{component.component_id}`{suffix}: "
                + ", ".join(f"`{member}`" for member in component.members)
            )
        if report.component_edges:
            lines.extend(["", "Component dependencies:", ""])
            lines.extend(
                f"- `{edge.dependent_component}` → `{edge.dependency_component}`"
                for edge in report.component_edges
            )
    else:
        lines.extend(["", "### Dependency outline", ""])
        lines.extend(
            _outline_lines(report.outline) or ["No claim dependency path was recorded."]
        )
    lines.extend(["", "### All failure incidents", ""])
    for incident in report.incidents:
        claims = ", ".join(f"`{claim}`" for claim in incident.claim_ids) or "run-wide"
        lines.append(
            f"- `{incident.incident_id}` `{incident.scope}/{incident.kind}` "
            f"({claims}): {incident.message} — `{incident.provenance}`"
        )
        if incident.detail:
            lines.append(f"  - Detail: {incident.detail}")
        lines.append(f"  - Phase: `{incident.phase}`")
        lines.append(f"  - Retryable: {'yes' if incident.retryable else 'no'}")
        for artifact in incident.artifacts:
            lines.append(f"  - Artifact ({artifact.label}): `{artifact.path}`")
            if artifact.command:
                lines.append("    - Command: `" + " ".join(artifact.command) + "`")
            if artifact.exit_code is not None:
                lines.append(f"    - Exit code: `{artifact.exit_code}`")
            if artifact.sha256:
                lines.append(f"    - SHA-256: `{artifact.sha256}`")
    return lines
