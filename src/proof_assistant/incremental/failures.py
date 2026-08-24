from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import networkx as nx

from ..workflow.contracts import (
    FailureArtifact,
    FailureComponent,
    FailureComponentEdge,
    FailureDependencyReport,
    FailureGraphEdge,
    FailureGraphNode,
    FailureIncident,
    FailureKind,
    FailureOutlineNode,
    FailurePath,
    FailureScope,
)
from .models import ClaimState
from .store import StateStore


def _enum_or_unknown(enum_type: type[Any], value: object, unknown: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return unknown


def _artifact_path(project: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (project / path).resolve()


def _incidents(
    project: Path, store: StateStore, run_id: int
) -> tuple[FailureIncident, ...]:
    incidents: list[FailureIncident] = []
    for row in store.failure_incident_rows(run_id):
        failure_id = int(row["failure_id"])
        artifacts = tuple(
            FailureArtifact(
                path=_artifact_path(project, artifact["path"]),
                label=str(artifact["label"]),
                sha256=artifact["sha256"],
                command=tuple(json.loads(artifact["command_json"])),
                exit_code=artifact["exit_code"],
                timed_out=bool(artifact["timed_out"]),
            )
            for artifact in store.failure_artifact_rows(failure_id)
        )
        incidents.append(
            FailureIncident(
                incident_id=failure_id,
                run_id=run_id,
                scope=_enum_or_unknown(FailureScope, row["scope"], FailureScope.RUN),
                kind=_enum_or_unknown(
                    FailureKind, row["failure_kind"], FailureKind.UNKNOWN
                ),
                phase=str(row["phase"]),
                category=str(row["category"]),
                message=str(row["message"]),
                detail=str(row["detail"]) if row["detail"] is not None else None,
                provenance=str(row["provenance"]),
                claim_ids=tuple(
                    str(claim["claim_id"])
                    for claim in store.failure_claim_rows(failure_id)
                ),
                batch_index=(
                    int(row["batch_index"]) if row["batch_index"] is not None else None
                ),
                retryable=bool(row["retryable"]),
                artifacts=artifacts,
            )
        )
    return tuple(incidents)


def _legacy_incidents(
    store: StateStore, run_id: int, *, outcome: str, detail: str
) -> tuple[FailureIncident, ...]:
    """Best-effort legacy rendering without pretending historical graph fidelity."""

    diagnostics_by_claim: dict[str, list[Any]] = defaultdict(list)
    for row in store.diagnostics_for_run(run_id):
        if row["claim_id"] is not None:
            diagnostics_by_claim[str(row["claim_id"])].append(row)
    failed_rows = [
        row
        for row in store.run_claim_rows(run_id)
        if str(row["state_after"]) == str(ClaimState.FAILED_TECHNICAL)
    ]
    batch_groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    claim_rows: list[Any] = []
    for row in failed_rows:
        claim_id = str(row["claim_id"])
        diagnostic = next(iter(diagnostics_by_claim.get(claim_id, ())), None)
        message = str(
            row["reason"] or (diagnostic["message"] if diagnostic else detail)
        )
        action = str(row["action"])
        if action in {"batch_failure", "provider_failure"}:
            batch_groups[(action, message)].append(row)
        else:
            claim_rows.append(row)

    incidents: list[FailureIncident] = []
    next_id = -1
    for (action, message), rows in sorted(batch_groups.items()):
        claim_ids = tuple(sorted(str(row["claim_id"]) for row in rows))
        categories = sorted(
            {
                str(diagnostic["category"])
                for claim_id in claim_ids
                for diagnostic in diagnostics_by_claim.get(claim_id, ())
            }
        )
        provider_failure = action == "provider_failure"
        environment_failure = (
            "repoprover is not importable" in message.casefold()
            or "no module named 'repoprover'" in message.casefold()
        )
        incidents.append(
            FailureIncident(
                incident_id=next_id,
                run_id=run_id,
                scope=(FailureScope.RUN if environment_failure else FailureScope.BATCH),
                kind=(
                    FailureKind.PROVIDER
                    if provider_failure
                    else (
                        FailureKind.INFRASTRUCTURE
                        if environment_failure
                        else FailureKind.BATCH_TECHNICAL
                    )
                ),
                phase="LEGACY",
                category=(
                    "repoprover_import"
                    if environment_failure
                    else (categories[0] if len(categories) == 1 else "legacy_batch")
                ),
                message=message,
                detail=(
                    "Coalesced from legacy batch transitions with the same exact "
                    "reason. The old schema copied this shared failure onto every "
                    "affected claim and did not preserve a batch identifier."
                ),
                provenance="legacy.run_claims.coalesced_batch",
                claim_ids=claim_ids,
                batch_index=None,
                retryable=True,
            )
        )
        next_id -= 1

    for row in claim_rows:
        claim_id = str(row["claim_id"])
        diagnostic = next(iter(diagnostics_by_claim.get(claim_id, ())), None)
        message = str(
            row["reason"] or (diagnostic["message"] if diagnostic else detail)
        )
        incidents.append(
            FailureIncident(
                incident_id=next_id,
                run_id=run_id,
                scope=FailureScope.CLAIM,
                kind=FailureKind.CLAIM_TECHNICAL,
                phase="LEGACY",
                category=str(diagnostic["category"] if diagnostic else "unknown"),
                message=message,
                detail=(
                    "Migrated from legacy run_claims; exact original graph and "
                    "failure phase were not persisted"
                ),
                provenance="legacy.run_claims",
                claim_ids=(claim_id,),
                batch_index=None,
                retryable=True,
            )
        )
        next_id -= 1
    if not incidents and outcome in {
        "provider_failure",
        "lean_infrastructure_failure",
        "setup_failure",
    }:
        kind = (
            FailureKind.PROVIDER
            if outcome == "provider_failure"
            else FailureKind.INFRASTRUCTURE
        )
        incidents.append(
            FailureIncident(
                incident_id=next_id,
                run_id=run_id,
                scope=FailureScope.RUN,
                kind=kind,
                phase="LEGACY",
                category=outcome,
                message=detail or outcome,
                detail=(
                    "Migrated from the legacy run summary; exact artifacts and "
                    "affected claims were not persisted"
                ),
                provenance="legacy.runs",
                claim_ids=(),
                batch_index=None,
                retryable=True,
            )
        )
    return tuple(incidents)


def _scope(store: StateStore, run_id: int, role: str) -> tuple[str, ...]:
    return tuple(str(row["claim_id"]) for row in store.run_scope_rows(run_id, role))


def _legacy_selected(project: Path, run_id: int) -> tuple[str, ...]:
    path = project / ".repoprover" / "runs" / f"{run_id:06d}" / "affected-claims.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("selected", [])
        if isinstance(values, list) and all(isinstance(value, str) for value in values):
            return tuple(sorted(set(values)))
    except (OSError, ValueError, TypeError):
        pass
    return ()


def _nodes(
    store: StateStore,
    run_id: int,
    *,
    snapshot: str | None,
    selected: tuple[str, ...],
    incidents: tuple[FailureIncident, ...],
) -> tuple[FailureGraphNode, ...]:
    incident_ids: dict[str, list[int]] = defaultdict(list)
    for incident in incidents:
        if incident.scope in {FailureScope.CLAIM, FailureScope.COMPONENT}:
            for claim_id in incident.claim_ids:
                incident_ids[claim_id].append(incident.incident_id)

    frozen = {str(row["claim_id"]): row for row in store.run_claim_node_rows(run_id)}
    legacy_states: dict[str, str] = {}
    for row in store.run_claim_rows(run_id):
        if row["state_after"] is not None:
            claim_id = str(row["claim_id"])
            state = str(row["state_after"])
            # Legacy transitions have no event sequence; lexical action order
            # can otherwise make an earlier `schedule_* -> PROVING` overwrite a
            # later batch failure. For a failure report, retain the explicit
            # failure observation whenever one exists.
            if state == str(ClaimState.FAILED_TECHNICAL):
                legacy_states[claim_id] = state
            else:
                legacy_states.setdefault(claim_id, state)
    versions = {
        str(row["claim_id"]): row
        for row in (store.claim_versions(snapshot) if snapshot else ())
    }
    claim_ids = set(selected) | set(incident_ids)
    result: list[FailureGraphNode] = []
    for claim_id in sorted(claim_ids):
        frozen_row = frozen.get(claim_id)
        version = versions.get(claim_id)
        current = store.claim_row(claim_id)
        if frozen_row is not None:
            kind = str(frozen_row["kind"])
            source_file = str(frozen_row["source_file"])
            statement_start = int(frozen_row["statement_start"])
            statement_end = int(frozen_row["statement_end"])
            state = str(frozen_row["state"])
        elif version is not None:
            kind = str(version["kind"])
            source_file = str(version["source_file"])
            statement_start = int(version["statement_start"])
            statement_end = int(version["statement_end"])
            state = legacy_states.get(
                claim_id,
                str(current["status"]) if current is not None else "UNKNOWN",
            )
        else:
            kind = str(current["kind"]) if current is not None else "unknown"
            source_file = str(current["source_file"]) if current is not None else ""
            statement_start = 0
            statement_end = 0
            state = legacy_states.get(
                claim_id,
                str(current["status"]) if current is not None else "UNKNOWN",
            )
        result.append(
            FailureGraphNode(
                claim_id=claim_id,
                kind=kind,
                source_file=source_file,
                statement_start=statement_start,
                statement_end=statement_end,
                state=state,
                incident_ids=tuple(sorted(incident_ids[claim_id])),
            )
        )
    return tuple(result)


def _edges(
    store: StateStore,
    run_id: int,
    *,
    snapshot: str | None,
) -> tuple[FailureGraphEdge, ...]:
    rows = store.run_dependency_edge_rows(run_id)
    if not rows and snapshot == store.previous_snapshot():
        rows = store.manuscript_edges()
    grouped: dict[tuple[str, str], dict[str, set[str]]] = {}
    for row in rows:
        if not bool(row["approved"]):
            continue
        key = (str(row["src"]), str(row["dst"]))
        values = grouped.setdefault(key, {"kinds": set(), "provenances": set()})
        values["kinds"].add(str(row["edge_kind"]))
        values["provenances"].add(str(row["provenance"]))
    return tuple(
        FailureGraphEdge(
            dependent=src,
            dependency=dst,
            kinds=tuple(sorted(values["kinds"])),
            provenances=tuple(sorted(values["provenances"])),
        )
        for (src, dst), values in sorted(grouped.items())
    )


def _graph(
    nodes: tuple[FailureGraphNode, ...], edges: tuple[FailureGraphEdge, ...]
) -> nx.DiGraph[str]:
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(node.claim_id for node in nodes)
    graph.add_edges_from(
        (edge.dependent, edge.dependency)
        for edge in edges
        if edge.dependent in graph and edge.dependency in graph
    )
    return graph


def _paths(
    graph: nx.DiGraph[str],
    targets: tuple[str, ...],
    blockers: set[str],
) -> tuple[FailurePath, ...]:
    result: list[FailurePath] = []
    for target in targets:
        if target not in graph:
            continue
        predecessor: dict[str, str | None] = {target: None}
        queue: deque[str] = deque((target,))
        while queue:
            node = queue.popleft()
            for child in sorted(graph.successors(node)):
                if child in predecessor:
                    continue
                predecessor[child] = node
                queue.append(child)
        for blocker in sorted(blockers):
            if blocker not in predecessor:
                continue
            reverse_path: list[str] = []
            cursor: str | None = blocker
            while cursor is not None:
                reverse_path.append(cursor)
                cursor = predecessor[cursor]
            result.append(FailurePath(target, blocker, tuple(reversed(reverse_path))))
    return tuple(
        sorted(
            result,
            key=lambda item: (
                targets.index(item.target),
                len(item.claims),
                item.claims,
                item.blocker,
            ),
        )
    )


def _outline(
    graph: nx.DiGraph[str],
    *,
    targets: tuple[str, ...],
    node_map: dict[str, FailureGraphNode],
    blockers: set[str],
    incidents: tuple[FailureIncident, ...],
) -> tuple[FailureOutlineNode, ...]:
    expanded: set[str] = set()
    globally_affected = {
        claim_id
        for incident in incidents
        if incident.scope in {FailureScope.RUN, FailureScope.BATCH}
        for claim_id in (incident.claim_ids or targets)
    }

    def display_state(claim_id: str, node: FailureGraphNode) -> str:
        if (
            claim_id in globally_affected
            and claim_id not in blockers
            and node.state == str(ClaimState.FAILED_TECHNICAL)
        ):
            return "BLOCKED_BY_GLOBAL_INCIDENT"
        return node.state

    def visit(claim_id: str) -> FailureOutlineNode:
        if claim_id in expanded:
            node = node_map[claim_id]
            return FailureOutlineNode(
                claim_id,
                display_state(claim_id, node),
                claim_id in blockers,
                node.incident_ids,
                True,
            )
        expanded.add(claim_id)
        # Each frame is [claim_id, ordered children, next index, built children].
        # This iterative construction is safe for arbitrarily deep valid DAGs.
        stack: list[list[Any]] = [
            [claim_id, tuple(sorted(graph.successors(claim_id))), 0, []]
        ]
        while stack:
            current, children, child_index, built = stack[-1]
            if child_index < len(children):
                child = children[child_index]
                stack[-1][2] = child_index + 1
                child_node = node_map[child]
                if child in expanded:
                    built.append(
                        FailureOutlineNode(
                            child,
                            display_state(child, child_node),
                            child in blockers,
                            child_node.incident_ids,
                            True,
                        )
                    )
                    continue
                expanded.add(child)
                stack.append([child, tuple(sorted(graph.successors(child))), 0, []])
                continue
            current_node = node_map[current]
            completed = FailureOutlineNode(
                current,
                display_state(current, current_node),
                current in blockers,
                current_node.incident_ids,
                False,
                tuple(built),
            )
            stack.pop()
            if stack:
                stack[-1][3].append(completed)
            else:
                return completed
        raise AssertionError("outline stack unexpectedly exhausted")

    roots: list[FailureOutlineNode] = []
    global_incidents = tuple(
        incident
        for incident in incidents
        if incident.scope in {FailureScope.RUN, FailureScope.BATCH}
    )
    for incident in global_incidents:
        affected = tuple(
            claim_id
            for claim_id in (incident.claim_ids or targets)
            if claim_id in node_map
        )
        roots.append(
            FailureOutlineNode(
                claim_id=f"incident:{incident.incident_id}",
                state=f"{incident.scope}:{incident.kind}",
                blocker=True,
                incident_ids=(incident.incident_id,),
                shared_reference=False,
                children=tuple(visit(claim_id) for claim_id in affected),
            )
        )
    for target in targets:
        if target in node_map:
            roots.append(visit(target))
    return tuple(roots)


def _components(
    graph: nx.DiGraph[str],
    *,
    node_map: dict[str, FailureGraphNode],
    blockers: set[str],
) -> tuple[tuple[FailureComponent, ...], tuple[FailureComponentEdge, ...]]:
    raw = sorted(
        tuple(sorted(component))
        for component in nx.strongly_connected_components(graph)
    )
    component_for: dict[str, str] = {}
    components: list[FailureComponent] = []
    for index, members in enumerate(raw, 1):
        component_id = f"component:{index:04d}"
        for member in members:
            component_for[member] = component_id
        cyclic = len(members) > 1 or graph.has_edge(members[0], members[0])
        components.append(
            FailureComponent(
                component_id=component_id,
                members=members,
                cyclic=cyclic,
                blocker=bool(set(members) & blockers),
                incident_ids=tuple(
                    sorted(
                        {
                            incident_id
                            for member in members
                            for incident_id in node_map[member].incident_ids
                        }
                    )
                ),
            )
        )
    component_edges = tuple(
        FailureComponentEdge(src_component, dst_component)
        for src_component, dst_component in sorted(
            {
                (component_for[src], component_for[dst])
                for src, dst in graph.edges
                if component_for[src] != component_for[dst]
            }
        )
    )
    return tuple(components), component_edges


def _primary_incident(
    incidents: tuple[FailureIncident, ...], first_blocker: FailurePath | None
) -> int | None:
    if not incidents:
        return None
    candidates = list(incidents)
    if first_blocker is not None:
        matching = [
            incident
            for incident in incidents
            if first_blocker.blocker in incident.claim_ids
            and incident.scope in {FailureScope.CLAIM, FailureScope.COMPONENT}
        ]
        if matching:
            candidates = matching
    else:
        global_failures = [
            incident
            for incident in incidents
            if incident.scope in {FailureScope.RUN, FailureScope.BATCH}
        ]
        if global_failures:
            candidates = global_failures
    return min(
        candidates,
        key=lambda item: (
            str(item.scope),
            str(item.kind),
            item.phase,
            item.category,
            item.message,
            item.claim_ids,
            item.incident_id,
        ),
    ).incident_id


def build_failure_report(
    project: Path,
    store: StateStore,
    run_id: int | None = None,
) -> FailureDependencyReport | None:
    """Build a deterministic, immutable failure report from backend-owned state."""

    run = store.run_row(run_id) if run_id is not None else store.latest_failure_run()
    if run is None and run_id is None:
        latest = store.latest_run()
        if latest is not None and str(latest["status"]) == "FAILED":
            run = latest
    if run is None:
        return None
    selected_run_id = int(run["run_id"])
    outcome = str(run["outcome"] or run["status"])
    detail = str(run["detail"] or "No further detail was persisted")
    snapshot = str(run["snapshot_commit"]) if run["snapshot_commit"] else None
    incidents = _incidents(project, store, selected_run_id)
    if not incidents:
        incidents = _legacy_incidents(
            store, selected_run_id, outcome=outcome, detail=detail
        )
    if not incidents:
        return None

    targets = _scope(store, selected_run_id, "TARGET")
    selected = _scope(store, selected_run_id, "SELECTED")
    if not selected:
        selected = _legacy_selected(project, selected_run_id)
    incident_claims = tuple(
        sorted({claim_id for incident in incidents for claim_id in incident.claim_ids})
    )
    if not selected:
        selected = incident_claims
    if not targets:
        targets = incident_claims or selected

    nodes = _nodes(
        store,
        selected_run_id,
        snapshot=snapshot,
        selected=selected,
        incidents=incidents,
    )
    edges = _edges(store, selected_run_id, snapshot=snapshot)
    node_ids = {node.claim_id for node in nodes}
    edges = tuple(
        edge
        for edge in edges
        if edge.dependent in node_ids and edge.dependency in node_ids
    )
    graph = _graph(nodes, edges)
    node_map = {node.claim_id: node for node in nodes}
    blockers = {
        claim_id
        for incident in incidents
        if incident.scope in {FailureScope.CLAIM, FailureScope.COMPONENT}
        for claim_id in incident.claim_ids
        if claim_id in graph
    }
    paths = _paths(graph, targets, blockers)
    first_blocker = paths[0] if paths else None
    has_cycles = not nx.is_directed_acyclic_graph(graph)
    if has_cycles:
        outline: tuple[FailureOutlineNode, ...] = ()
        components, component_edges = _components(
            graph, node_map=node_map, blockers=blockers
        )
    else:
        outline = _outline(
            graph,
            targets=targets,
            node_map=node_map,
            blockers=blockers,
            incidents=incidents,
        )
        components = ()
        component_edges = ()
    global_incident_ids = tuple(
        incident.incident_id
        for incident in incidents
        if incident.scope in {FailureScope.RUN, FailureScope.BATCH}
    )
    return FailureDependencyReport(
        run_id=selected_run_id,
        snapshot=snapshot,
        outcome=outcome,
        detail=detail,
        targets=targets,
        selected=selected,
        nodes=nodes,
        edges=edges,
        incidents=incidents,
        global_incident_ids=global_incident_ids,
        primary_incident_id=_primary_incident(incidents, first_blocker),
        first_blocker=first_blocker,
        paths=paths,
        has_cycles=has_cycles,
        outline=outline,
        components=components,
        component_edges=component_edges,
    )


def artifact_record(
    path: Path,
    *,
    label: str,
    command: tuple[str, ...] = (),
    exit_code: int | None = None,
    timed_out: bool = False,
) -> dict[str, Any]:
    """Return a persistence payload with a content hash when the artifact exists."""

    digest: str | None = None
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pass
    return {
        "path": str(path),
        "label": label,
        "sha256": digest,
        "command": command,
        "exit_code": exit_code,
        "timed_out": timed_out,
    }
