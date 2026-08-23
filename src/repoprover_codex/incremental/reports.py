from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

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
        "",
        "## Claim status",
        "",
        "| Claim | Kind | State | Lean declaration |",
        "|---|---|---|---|",
    ]
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
