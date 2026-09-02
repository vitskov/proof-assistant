from __future__ import annotations

import pytest

from proof_assistant.incremental.graph import (
    affected_claims,
    build_graph,
    canonical_cycles,
    dependency_closure,
    ready_frontier,
    source_changes,
    unsatisfied_dependencies,
)
from proof_assistant.incremental.latex import (
    LatexIndexError,
    discover_latex_sources,
    explicit_reference_graph,
    index_manuscript,
    normalize_latex_statement,
    resolve_latex_closure,
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
        objects = index_manuscript(source, store, main_file="main.tex")
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
        objects = index_manuscript(source, store, main_file="main.tex")
    assert objects[0].proof_text == ""
    edges, unresolved = explicit_reference_graph(objects)
    assert edges == ()
    assert unresolved == (("t", "missing"),)


def test_assistant_comment_block_attaches_only_to_next_object_and_adds_advisory_edge(
    tmp_path,
):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_text(
        r"""
%% assistant: The abridged theorem is a corollary of
%% the stronger proved \Cref{thm:full} below.
\begin{theorem}\label{thm:abridged}Abridged.\end{theorem}
Ordinary manuscript prose ends any association.
%% assistant: This note must not leak.
Ordinary manuscript prose.
\begin{theorem}\label{thm:full}Full.\end{theorem}
\begin{proof}Proof.\end{proof}
""",
        encoding="utf-8",
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        objects = index_manuscript(source, store, main_file="main.tex")
    by_id = {item.claim_id: item for item in objects}
    assert by_id["thm:abridged"].assistant_context == (
        "The abridged theorem is a corollary of\n"
        "the stronger proved \\Cref{thm:full} below."
    )
    assert by_id["thm:abridged"].assistant_references == ("thm:full",)
    assert by_id["thm:full"].assistant_context == ""
    edges, unresolved = explicit_reference_graph(objects)
    assert unresolved == ()
    assert [
        (edge.src, edge.dst, edge.kind, edge.provenance) for edge in edges
    ] == [
        (
            "thm:abridged",
            "thm:full",
            "assistant_context",
            "assistant_annotation",
        )
    ]


def test_assistant_context_change_invalidates_ai_input_not_statement_hash(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    tex = source / "main.tex"
    tex.write_text(
        "%% assistant: First note.\n"
        r"\begin{theorem}\label{t}Claim.\end{theorem}",
        encoding="utf-8",
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        first = index_manuscript(source, store, main_file="main.tex")
        store.replace_current_claims(
            "snapshot-a",
            first,
            run_id=1,
            state_updates={"t": ClaimState.SKIPPED_UNPROVED},
        )
        previous = {"t": store.claim_version("snapshot-a", "t")}
        tex.write_text(
            "%% assistant: Revised note.\n"
            r"\begin{theorem}\label{t}Claim.\end{theorem}",
            encoding="utf-8",
        )
        second = index_manuscript(source, store, main_file="main.tex")
    assert first[0].statement_hash == second[0].statement_hash
    statement, assistant, proof, deleted = source_changes(
        previous, second, mode="theorem"
    )
    assert statement == set()
    assert assistant == {"t"}
    assert proof == set()
    assert deleted == set()


def test_guided_unproved_dependency_is_transparent_only_after_anchor_certified():
    edges = (
        ManuscriptEdge("dependent", "abridged", "explicit_ref", "latex_ref"),
        ManuscriptEdge(
            "abridged",
            "full",
            "assistant_context",
            "assistant_annotation",
        ),
    )
    selected = {"dependent", "abridged", "full"}
    states = {
        "dependent": ClaimState.DISCOVERED,
        "abridged": ClaimState.SKIPPED_UNPROVED,
        "full": ClaimState.DISCOVERED,
    }
    assert ready_frontier(states, selected=selected, edges=edges) == ("full",)
    assert unsatisfied_dependencies("dependent", states=states, edges=edges) == (
        "abridged",
    )
    states["full"] = ClaimState.CERTIFIED
    assert ready_frontier(states, selected=selected, edges=edges) == ("dependent",)
    assert unsatisfied_dependencies("dependent", states=states, edges=edges) == ()


def test_duplicate_labels_fail_closed(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_text(
        r"\begin{lemma}\label{x}A\end{lemma}\begin{theorem}\label{x}B\end{theorem}",
        encoding="utf-8",
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        with pytest.raises(LatexIndexError, match="Duplicate LaTeX label"):
            index_manuscript(source, store, main_file="main.tex")


def test_unlabeled_ids_survive_insertions_via_persistent_sidecar(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    tex = source / "main.tex"
    tex.write_text(
        r"\begin{lemma}Alpha.\end{lemma}\begin{lemma}Beta.\end{lemma}",
        encoding="utf-8",
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        first = index_manuscript(source, store, main_file="main.tex")
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
        second = index_manuscript(source, store, main_file="main.tex")
    second_ids = {item.statement_text.strip(): item.claim_id for item in second}
    assert second_ids["Alpha."] == ids["Alpha."]
    assert second_ids["Beta."] == ids["Beta."]
    assert second_ids["New."] not in set(ids.values())


def test_selected_main_indexes_only_recursive_closure_and_inherits_theorems(tmp_path):
    source = tmp_path / "paper"
    (source / "sections").mkdir(parents=True)
    (source / "main.tex").write_text(
        r"\newtheorem{result}{Theorem}\input{sections/results}", encoding="utf-8"
    )
    (source / "sections/results.tex").write_text(
        r"\begin{result}\label{selected}Chosen.\end{result}", encoding="utf-8"
    )
    (source / "alternate.tex").write_text(
        r"\begin{theorem}\label{selected}Duplicate outside closure.\end{theorem}",
        encoding="utf-8",
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        objects = index_manuscript(source, store, main_file="main.tex")
    assert [(item.claim_id, item.source_file, item.kind) for item in objects] == [
        ("selected", "sections/results.tex", "theorem")
    ]


def test_recursive_closure_is_cycle_safe_and_shared_inputs_are_indexed_once(tmp_path):
    source = tmp_path / "paper"
    (source / "parts").mkdir(parents=True)
    (source / "main.tex").write_text(
        r"\input{parts/a}\input{parts/b}", encoding="utf-8"
    )
    (source / "parts/a.tex").write_text(
        r"\input{shared}\begin{lemma}\label{a}A.\end{lemma}", encoding="utf-8"
    )
    (source / "parts/b.tex").write_text(
        r"\input{shared}\begin{lemma}\label{b}B.\end{lemma}", encoding="utf-8"
    )
    (source / "parts/shared.tex").write_text(
        r"\input{a}\begin{lemma}\label{shared}Shared.\end{lemma}",
        encoding="utf-8",
    )
    assert resolve_latex_closure(source, "main.tex") == (
        "main.tex",
        "parts/a.tex",
        "parts/shared.tex",
        "parts/b.tex",
    )
    with StateStore(tmp_path / "state.sqlite3") as store:
        objects = index_manuscript(source, store, main_file="main.tex")
    assert {item.claim_id for item in objects} == {"a", "b", "shared"}


@pytest.mark.parametrize(
    ("main_text", "message"),
    [
        (r"\input{missing}", "does not exist"),
        (r"\input{../../outside}", "escapes the source folder"),
        (r"\input{\chosen}", "Dynamic"),
    ],
)
def test_recursive_closure_fails_closed_for_unresolved_inputs(
    tmp_path, main_text, message
):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_text(main_text, encoding="utf-8")
    with pytest.raises(LatexIndexError, match=message):
        resolve_latex_closure(source, "main.tex")


def test_simple_input_resolution_fails_if_local_and_root_paths_are_ambiguous(tmp_path):
    source = tmp_path / "paper"
    (source / "parts").mkdir(parents=True)
    (source / "main.tex").write_text(r"\input{parts/chapter}", encoding="utf-8")
    (source / "parts/chapter.tex").write_text(r"\input{shared}", encoding="utf-8")
    (source / "parts/shared.tex").write_text("local", encoding="utf-8")
    (source / "shared.tex").write_text("root", encoding="utf-8")
    with pytest.raises(LatexIndexError, match="Ambiguous"):
        resolve_latex_closure(source, "main.tex")


def test_subfile_and_import_forms_follow_including_file_semantics(tmp_path):
    source = tmp_path / "paper"
    (source / "parts/nested").mkdir(parents=True)
    (source / "main.tex").write_text(r"\subfile{parts/a}", encoding="utf-8")
    (source / "parts/a.tex").write_text(r"\import{nested/}{b}", encoding="utf-8")
    (source / "parts/nested/b.tex").write_text("body", encoding="utf-8")
    assert resolve_latex_closure(source, "main.tex") == (
        "main.tex",
        "parts/a.tex",
        "parts/nested/b.tex",
    )


def test_source_discovery_ignores_generated_and_managed_directories(tmp_path):
    source = tmp_path / "paper"
    (source / "build").mkdir(parents=True)
    (source / ".repoprover").mkdir()
    (source / "main.tex").write_text(r"\documentclass{article}", encoding="utf-8")
    (source / "build/generated.tex").write_text("generated", encoding="utf-8")
    (source / ".repoprover/internal.tex").write_text("internal", encoding="utf-8")
    assert discover_latex_sources(source) == (("main.tex", True),)


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
