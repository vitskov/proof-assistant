from __future__ import annotations

import pytest

from proof_assistant.incremental.graph import (
    affected_claims,
    build_graph,
    canonical_cycles,
    dependency_closure,
    ready_frontier,
)
from proof_assistant.incremental.latex import (
    LatexIndexError,
    explicit_reference_graph,
    index_manuscript,
    normalize_latex_statement,
)
from proof_assistant.incremental.models import ClaimState, ManuscriptEdge
from proof_assistant.incremental.snapshot import SnapshotRepository
from proof_assistant.incremental.store import StateStore
from proof_assistant.incremental.task import parse_task_file
from proof_assistant.manuscript import ManuscriptInputError


def test_normalization_removes_comments_labels_and_layout():
    first = "A  statement % prose comment\n \\label{thm:a} with   spaces"
    second = "A statement with spaces"
    assert normalize_latex_statement(first) == second


def test_structural_index_extracts_custom_environments_proofs_refs_and_byte_spans(
    tmp_path,
):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_text(
        r"""
\newtheorem{result}{Theorem}
é
\begin{lemma}
  \label
    {lem:first}
  First statement.
\end{lemma}
\begin{proof}
  Direct.
\end{proof}
\begin{result}\label{thm:second}
  Second statement.
\end{result}
\begin{proof}
  Use \cref{lem:first,eq:key}.
\end{proof}
\begin{equation}\label{eq:key} 1=1 \end{equation}
""",
        encoding="utf-8",
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        objects = index_manuscript(source, store)
    by_id = {item.claim_id: item for item in objects}
    assert set(by_id) == {"lem:first", "thm:second", "eq:key"}
    assert by_id["thm:second"].kind == "theorem"
    assert "Direct." in by_id["lem:first"].proof_text
    assert by_id["thm:second"].references == ("eq:key", "lem:first")
    statement = by_id["lem:first"]
    assert statement.statement_byte_start > statement.statement_start
    edges, unresolved = explicit_reference_graph(objects)
    assert {(edge.src, edge.dst) for edge in edges} == {
        ("thm:second", "lem:first"),
        ("thm:second", "eq:key"),
    }
    assert unresolved == ()


def test_theorem_without_proof_and_unresolved_reference_are_preserved(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_text(
        r"\begin{theorem}\label{t} Claim \ref{missing}.\end{theorem}",
        encoding="utf-8",
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        objects = index_manuscript(source, store)
    assert objects[0].proof_text == ""
    edges, unresolved = explicit_reference_graph(objects)
    assert edges == ()
    assert unresolved == (("t", "missing"),)


def test_duplicate_labels_fail_closed(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_text(
        r"\begin{lemma}\label{x}A\end{lemma}\begin{theorem}\label{x}B\end{theorem}",
        encoding="utf-8",
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        with pytest.raises(LatexIndexError, match="Duplicate LaTeX label"):
            index_manuscript(source, store)


def test_unlabeled_ids_survive_insertions_via_persistent_sidecar(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    tex = source / "main.tex"
    tex.write_text(
        r"\begin{lemma}Alpha.\end{lemma}\begin{lemma}Beta.\end{lemma}",
        encoding="utf-8",
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        first = index_manuscript(source, store)
        store.replace_current_claims(
            "snapshot-a",
            first,
            run_id=1,
            state_updates={item.claim_id: ClaimState.DISCOVERED for item in first},
        )
        ids = {item.statement_text.strip(): item.claim_id for item in first}
        tex.write_text(
            r"\begin{lemma}New.\end{lemma}"
            r"\begin{lemma}Alpha.\end{lemma}\begin{lemma}Beta.\end{lemma}",
            encoding="utf-8",
        )
        second = index_manuscript(source, store)
    second_ids = {item.statement_text.strip(): item.claim_id for item in second}
    assert second_ids["Alpha."] == ids["Alpha."]
    assert second_ids["Beta."] == ids["Beta."]
    assert second_ids["New."] not in set(ids.values())


def test_graph_slice_frontier_cycles_and_canonical_export(tmp_path):
    edges = (
        ManuscriptEdge("T", "B", "explicit_ref", "latex_ref"),
        ManuscriptEdge("B", "A", "explicit_ref", "latex_ref"),
        ManuscriptEdge("U", "C", "explicit_ref", "latex_ref"),
    )
    assert affected_claims({"A"}, claim_ids={"A", "B", "T", "C", "U"}, edges=edges) == {
        "A",
        "B",
        "T",
    }
    assert dependency_closure(
        {"T"}, claim_ids={"A", "B", "T", "C", "U"}, edges=edges
    ) == {
        "A",
        "B",
        "T",
    }
    states = {key: ClaimState.DISCOVERED for key in ("A", "B", "T")}
    assert ready_frontier(states, selected=set(states), edges=edges) == ("A",)
    states["A"] = ClaimState.CERTIFIED
    assert ready_frontier(states, selected=set(states), edges=edges) == ("B",)
    cyclic = build_graph(
        {"A", "B"},
        [
            ManuscriptEdge("A", "B", "semantic", "test"),
            ManuscriptEdge("B", "A", "semantic", "test"),
        ],
    )
    assert canonical_cycles(cyclic) == (("A", "B"),)


def test_task_parser_supports_yaml_and_free_form(tmp_path):
    yaml_task = tmp_path / "VERIFY.yaml"
    yaml_task.write_text(
        "schema: 1\nmode: argument-audit\ntargets: [thm:main]\n"
        "policy:\n  preserve_certified: true\ninstructions: Check each step.\n",
        encoding="utf-8",
    )
    _path, _text, _digest, task = parse_task_file(yaml_task)
    assert task.mode == "argument-audit"
    assert task.targets == ("thm:main",)
    assert task.free_form == "Check each step."
    plain = tmp_path / "TASK.md"
    plain.write_text("Check everything.", encoding="utf-8")
    assert parse_task_file(plain)[3].free_form == "Check everything."


@pytest.mark.parametrize(
    "contents, message",
    [
        ("schema: 2\n", "schema: 1"),
        ("schema: 1\nmode: mystery\n", "Task mode"),
        ("schema: 1\ntargets: nope\n", "targets"),
        ("schema: 1\npolicy:\n  surprise: true\n", "Unknown task policy"),
    ],
)
def test_invalid_structured_tasks_fail_closed(tmp_path, contents, message):
    task = tmp_path / "VERIFY.yaml"
    task.write_text(contents, encoding="utf-8")
    with pytest.raises(ManuscriptInputError, match=message):
        parse_task_file(task)


def test_snapshot_is_content_addressed_filtered_and_diffable(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_text("first", encoding="utf-8")
    (source / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (source / "target").mkdir()
    (source / "target" / "artifact").write_text("large", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    snapshots = SnapshotRepository(project)
    first = snapshots.create(source, run_id=1)
    second = snapshots.create(source, run_id=2)
    assert second.identical
    assert second.commit == first.commit
    assert [item.path for item in first.files] == ["main.tex"]
    (source / "main.tex").write_text("second", encoding="utf-8")
    third = snapshots.create(source, run_id=3)
    assert third.commit != first.commit
    assert snapshots.changed_paths(first.commit, third.commit) == ("main.tex",)
    assert "-first" in snapshots.diff(first.commit, third.commit)
    assert "+second" in snapshots.diff(first.commit, third.commit)
