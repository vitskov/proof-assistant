from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import networkx as nx

from .io import canonical_hash
from .models import (
    ClaimState,
    ManuscriptEdge,
    SourceObject,
    is_conjectural_assertion,
    is_conjectural_assertion_shape,
    is_proof_bearing_assertion,
)


class DependencyCycleError(RuntimeError):
    def __init__(self, cycles: Sequence[Sequence[str]]) -> None:
        self.cycles = tuple(tuple(cycle) for cycle in cycles)
        rendered = "; ".join(" -> ".join(cycle) for cycle in self.cycles)
        super().__init__(f"Manuscript dependency graph contains cycles: {rendered}")


def build_graph(
    claim_ids: Iterable[str], edges: Iterable[ManuscriptEdge]
) -> nx.DiGraph[str]:
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(sorted(set(claim_ids)))
    graph.add_edges_from((edge.src, edge.dst) for edge in edges if edge.approved)
    return graph


def canonical_cycles(graph: nx.DiGraph[str]) -> tuple[tuple[str, ...], ...]:
    cycles: list[tuple[str, ...]] = []
    for component in nx.strongly_connected_components(graph):
        if len(component) > 1:
            cycles.append(tuple(sorted(component)))
        elif component:
            node = next(iter(component))
            if graph.has_edge(node, node):
                cycles.append((node,))
    return tuple(sorted(cycles))


def affected_claims(
    changed: Iterable[str],
    *,
    claim_ids: Iterable[str],
    edges: Iterable[ManuscriptEdge],
) -> set[str]:
    """Return changed nodes and all claims that transitively depend on them."""
    graph = build_graph(claim_ids, edges).reverse(copy=False)
    result: set[str] = set()
    for claim_id in changed:
        result.add(claim_id)
        if claim_id in graph:
            result.update(nx.descendants(graph, claim_id))
    return result


def dependency_closure(
    targets: Iterable[str],
    *,
    claim_ids: Iterable[str],
    edges: Iterable[ManuscriptEdge],
) -> set[str]:
    graph = build_graph(claim_ids, edges)
    result: set[str] = set()
    for claim_id in targets:
        if claim_id not in graph:
            continue
        result.add(claim_id)
        result.update(nx.descendants(graph, claim_id))
    return result


def ready_frontier(
    states: dict[str, ClaimState],
    *,
    selected: set[str],
    edges: Iterable[ManuscriptEdge],
) -> tuple[str, ...]:
    dependencies: dict[str, set[str]] = {claim_id: set() for claim_id in selected}
    for edge in edges:
        if edge.approved and edge.src in selected and edge.dst in selected:
            dependencies[edge.src].add(edge.dst)
    ready_states = {
        ClaimState.DISCOVERED,
        ClaimState.STATEMENT_APPROVED,
        ClaimState.READY_TO_PROVE,
        ClaimState.DIRTY_SOURCE,
        ClaimState.INVALIDATED,
        ClaimState.FAILED_FORMALIZATION,
        ClaimState.UNRESOLVED,
    }
    return tuple(
        sorted(
            claim_id
            for claim_id in selected
            if states.get(claim_id, ClaimState.DISCOVERED) in ready_states
            and all(
                states.get(dependency) == ClaimState.CERTIFIED
                for dependency in dependencies[claim_id]
            )
        )
    )


def blocked_descendants(
    blockers: Iterable[str],
    *,
    selected: set[str],
    edges: Iterable[ManuscriptEdge],
) -> set[str]:
    graph = build_graph(selected, edges).reverse(copy=False)
    result: set[str] = set()
    for blocker in blockers:
        if blocker in graph:
            result.update(nx.descendants(graph, blocker))
    return result & selected


def conjectural_dependency_blockers(
    objects: Sequence[SourceObject],
    *,
    selected: set[str],
    edges: Iterable[ManuscriptEdge],
) -> dict[str, tuple[str, ...]]:
    """Map unsupported assertions to selected proof-bearing dependents.

    Edges point from a dependent claim to its dependency. The reverse graph
    therefore identifies every direct or transitive proof-bearing statement
    whose verification would rely on an unsupported assertion.
    """

    by_id = {item.claim_id: item for item in objects}
    graph = build_graph(by_id, edges).reverse(copy=False)
    result: dict[str, tuple[str, ...]] = {}
    for item in objects:
        if not is_conjectural_assertion(item):
            continue
        dependents = nx.descendants(graph, item.claim_id)
        blockers = tuple(
            sorted(
                claim_id
                for claim_id in dependents & selected
                if claim_id in by_id and is_proof_bearing_assertion(by_id[claim_id])
            )
        )
        if blockers:
            result[item.claim_id] = blockers
    return result


def source_changes(
    previous: dict[str, Any],
    current: Sequence[SourceObject],
    *,
    mode: str,
) -> tuple[set[str], set[str], set[str]]:
    """Return (statement changes, proof-only changes, deleted claims)."""
    now = {item.claim_id: item for item in current}
    statement_changes: set[str] = set()
    proof_changes: set[str] = set()
    for claim_id, item in now.items():
        old = previous.get(claim_id)
        if old is None:
            statement_changes.add(claim_id)
        elif old["normalized_statement_hash"] != item.normalized_statement_hash:
            statement_changes.add(claim_id)
        elif is_conjectural_assertion_shape(
            str(old["kind"]),
            int(old["proof_start"]) if old["proof_start"] is not None else None,
        ) != is_conjectural_assertion(item):
            # Gaining or losing proof-obligation status must reschedule in every
            # task mode even though ordinary proof-prose edits are ignored in
            # theorem mode.
            statement_changes.add(claim_id)
        elif old["proof_hash"] != item.proof_hash and mode == "argument-audit":
            proof_changes.add(claim_id)
    deleted = set(previous) - set(now)
    return statement_changes, proof_changes, deleted


def manuscript_graph_export(
    objects: Sequence[SourceObject], edges: Sequence[ManuscriptEdge]
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "nodes": [
            {
                "id": item.claim_id,
                "kind": item.kind,
                "file": item.source_file,
                "label": item.label,
                "statement_hash": item.statement_hash,
                "proof_hash": item.proof_hash,
                "normalized_statement_hash": item.normalized_statement_hash,
            }
            for item in sorted(objects, key=lambda value: value.claim_id)
        ],
        "edges": [
            {
                "from": edge.src,
                "to": edge.dst,
                "kind": edge.kind,
                "provenance": edge.provenance,
                "approved": edge.approved,
            }
            for edge in sorted(
                edges, key=lambda value: (value.src, value.dst, value.kind)
            )
        ],
    }
    payload["sha256"] = canonical_hash(payload)
    return payload


def graph_to_dot(
    objects: Sequence[SourceObject], edges: Sequence[ManuscriptEdge]
) -> str:
    kinds = {item.claim_id: item.kind for item in objects}
    lines = ["digraph manuscript {", "  rankdir=LR;"]
    for claim_id in sorted(kinds):
        label = f"{claim_id}\\n[{kinds[claim_id]}]".replace('"', '\\"')
        identifier = claim_id.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  "{identifier}" [label="{label}"];')
    for edge in sorted(edges, key=lambda item: (item.src, item.dst, item.kind)):
        src = edge.src.replace("\\", "\\\\").replace('"', '\\"')
        dst = edge.dst.replace("\\", "\\\\").replace('"', '\\"')
        kind = edge.kind.replace('"', '\\"')
        lines.append(f'  "{src}" -> "{dst}" [label="{kind}"];')
    lines.append("}")
    return "\n".join(lines) + "\n"
