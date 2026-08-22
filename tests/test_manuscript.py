from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from repoprover_codex.cli import build_parser
from repoprover_codex.manuscript import (
    CommandRecord,
    ManuscriptInputError,
    evaluate_manuscript_run,
    prepare_manuscript_workspace,
    read_task_file,
)


def _raw_inputs(tmp_path):
    manuscript = tmp_path / "source"
    manuscript.mkdir()
    (manuscript / "main.tex").write_text(
        r"\begin{theorem}For every $n$, $n=n$.\end{theorem}" + "\n",
        encoding="utf-8",
    )
    task = tmp_path / "task.md"
    task.write_text(
        "Verify in Lean that the displayed theorem holds for natural numbers.\n",
        encoding="utf-8",
    )
    return manuscript, task


def test_prepare_raw_manuscript_creates_isolated_lean_workspace(tmp_path):
    manuscript, task = _raw_inputs(tmp_path)
    (manuscript / ".lake").mkdir()
    (manuscript / ".lake" / "private-cache").write_text("exclude me")
    (manuscript / ".env").write_text("SECRET=exclude-me\n")
    output = tmp_path / "run"

    prepared = prepare_manuscript_workspace(manuscript, output, task)

    assert prepared.source_mode == "generated_lean_project"
    assert prepared.workspace == output / "workspace"
    assert (output / "TASK.md").read_text() == task.read_text()
    assert (
        prepared.workspace / "RepoProverInput/TASK.md"
    ).read_text() == task.read_text()
    assert (prepared.workspace / "manuscript/main.tex").is_file()
    assert not (prepared.workspace / "manuscript/.lake").exists()
    assert not (prepared.workspace / "manuscript/.env").exists()
    assert (prepared.workspace / "lean-toolchain").is_file()
    assert (prepared.workspace / "lakefile.lean").is_file()
    assert (prepared.workspace / "Formalization/Verification.lean").is_file()
    assert (output / "INPUT_MANIFEST.json").is_file()
    assert prepared.latex_sources == ("manuscript/main.tex",)

    status = subprocess.run(
        ["git", "-C", str(prepared.workspace), "status", "--short"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert status.stdout == ""
    assert (
        subprocess.run(
            ["git", "-C", str(prepared.workspace), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        == prepared.baseline_commit
    )

    manifest = json.loads((output / "INPUT_MANIFEST.json").read_text())
    assert manifest["snapshot"]["task_sha256"] == prepared.task_sha256
    assert manifest["snapshot"]["latex_sources"][0]["path"] == "manuscript/main.tex"


def test_prepare_existing_lean_project_preserves_root_layout(tmp_path):
    manuscript, task = _raw_inputs(tmp_path)
    (manuscript / "lean-toolchain").write_text("leanprover/lean4:v4.28.0\n")
    (manuscript / "lakefile.lean").write_text("import Lake\n")
    (manuscript / "Book.lean").write_text("theorem existing : True := by trivial\n")

    prepared = prepare_manuscript_workspace(manuscript, tmp_path / "run", task)

    assert prepared.source_mode == "existing_lean_project"
    assert (prepared.workspace / "main.tex").is_file()
    assert not (prepared.workspace / "manuscript").exists()
    assert (prepared.workspace / "Book.lean").is_file()
    assert prepared.latex_sources == ("main.tex",)


def test_prepare_refuses_nonempty_output(tmp_path):
    manuscript, task = _raw_inputs(tmp_path)
    output = tmp_path / "run"
    output.mkdir()
    (output / "keep.txt").write_text("user data")

    with pytest.raises(ManuscriptInputError, match="refusing to overwrite"):
        prepare_manuscript_workspace(manuscript, output, task)

    assert (output / "keep.txt").read_text() == "user data"


def test_prepare_requires_latex_source(tmp_path):
    manuscript = tmp_path / "source"
    manuscript.mkdir()
    task = tmp_path / "task.md"
    task.write_text("Verify something.\n")

    with pytest.raises(ManuscriptInputError, match="no .tex or .ltx"):
        prepare_manuscript_workspace(manuscript, tmp_path / "run", task)


def test_prepare_requires_disjoint_source_and_output(tmp_path):
    manuscript, task = _raw_inputs(tmp_path)
    with pytest.raises(ManuscriptInputError, match="must not contain"):
        prepare_manuscript_workspace(manuscript, manuscript / "run", task)


def test_prepare_refuses_source_symlink_escape(tmp_path):
    manuscript, task = _raw_inputs(tmp_path)
    external = tmp_path / "external.tex"
    external.write_text("external source\n")
    (manuscript / "external.tex").symlink_to(external)

    with pytest.raises(ManuscriptInputError, match="symlink"):
        prepare_manuscript_workspace(manuscript, tmp_path / "run", task)


def test_task_file_must_be_nonempty_utf8(tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text(" \n")
    with pytest.raises(ManuscriptInputError, match="empty"):
        read_task_file(empty)

    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff\xfe")
    with pytest.raises(ManuscriptInputError, match="UTF-8"):
        read_task_file(invalid)


def test_verified_requires_lean_report_commit_clean_tree_and_build(tmp_path):
    report = tmp_path / "VERIFICATION_REPORT.md"
    report.write_text("# Verification Report\n\nOutcome: verified.\n")
    build = CommandRecord(("lake", "build"), 0, "Build completed", "")
    lean_call = SimpleNamespace(name="lean_check", success=True)

    result = evaluate_manuscript_run(
        final_text="Completed.\n-- VERIFIED",
        tool_calls=[lean_call],
        report=report,
        baseline_commit="a",
        final_commit="b",
        git_status="",
        independent_build=build,
    )

    assert result.outcome == "verified"
    assert result.exit_code == 0


def test_verified_marker_without_evidence_is_unverified(tmp_path):
    build = CommandRecord(("lake", "build"), 0, "Build completed", "")
    result = evaluate_manuscript_run(
        final_text="-- VERIFIED",
        tool_calls=[],
        report=tmp_path / "missing.md",
        baseline_commit="a",
        final_commit="a",
        git_status="?? proof.lean",
        independent_build=build,
    )
    assert result.outcome == "unverified"
    assert result.exit_code == 4
    assert "lean_check" in result.detail


def test_failed_independent_build_is_tool_failure(tmp_path):
    build = CommandRecord(("lake", "build"), 1, "", "compile error")
    result = evaluate_manuscript_run(
        final_text="-- UNVERIFIED",
        tool_calls=[],
        report=tmp_path / "missing.md",
        baseline_commit="a",
        final_commit="a",
        git_status="",
        independent_build=build,
    )
    assert result.outcome == "tool_failure"
    assert result.exit_code == 5


def test_cli_exposes_file_based_manuscript_interface():
    args = build_parser().parse_args(
        [
            "manuscript-run",
            "--manuscript",
            "/input/book",
            "--task-file",
            "/input/task.md",
            "--output",
            "/output/run",
            "--model",
            "gpt-test",
        ]
    )
    assert args.manuscript == "/input/book"
    assert args.task_file == "/input/task.md"
    assert args.output == "/output/run"
    assert args.turn_timeout == 3600.0
