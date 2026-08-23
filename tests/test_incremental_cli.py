from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from proof_assistant.cli import build_parser
from proof_assistant.incremental.orchestration import (
    BatchResult,
    VerifyOptions,
    _create_worktree,
    _merge_batch,
    _partition,
    _remove_worktree,
)
from proof_assistant.incremental.session import IncrementalSession, claim_module_path

FIXTURE = Path(__file__).parent / "fixtures" / "incremental_manuscript"


@pytest.mark.parametrize(
    "command, function_name",
    [
        (
            [
                "manuscript",
                "init",
                "--manuscript",
                "m",
                "--main-file",
                "main.tex",
                "--project",
                "p",
            ],
            "cmd_manuscript_init",
        ),
        (
            ["manuscript", "verify", "--project", "p", "--model", "gpt-5.6-sol"],
            "cmd_manuscript_verify",
        ),
        (["manuscript", "status", "--project", "p"], "cmd_manuscript_status"),
        (["manuscript", "graph", "--project", "p"], "cmd_manuscript_graph"),
        (["manuscript", "questions", "--project", "p"], "cmd_manuscript_questions"),
        (["manuscript", "diff", "--project", "p"], "cmd_manuscript_diff"),
        (
            ["manuscript", "invalidate", "--project", "p", "--claim", "t"],
            "cmd_manuscript_invalidate",
        ),
        (["manuscript", "audit", "--project", "p"], "cmd_manuscript_audit"),
        (
            ["manuscript", "correspondence", "--project", "p"],
            "cmd_manuscript_correspondence",
        ),
    ],
)
def test_cli_exposes_incremental_command_family(command, function_name):
    args = build_parser().parse_args(command)
    assert args.func.__name__ == function_name


def test_public_project_commands_do_not_accept_external_task_files():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "manuscript",
                "init",
                "--manuscript",
                "m",
                "--project",
                "p",
                "--task-file",
                "external.yaml",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "manuscript",
                "verify",
                "--project",
                "p",
                "--model",
                "gpt-5.6-sol",
                "--task-file",
                "external.yaml",
            ]
        )


def test_manuscript_init_requires_explicit_main_file():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "manuscript",
                "init",
                "--manuscript",
                "m",
                "--project",
                "p",
            ]
        )


def test_parallelism_is_bounded_and_batches_are_deterministic():
    assert _partition(("A", "B", "C", "D", "E"), 2) == [
        ("A", "B"),
        ("C", "D"),
        ("E",),
    ]
    VerifyOptions(model="model", jobs=2).validate()
    with pytest.raises(ValueError, match="1 or 2"):
        VerifyOptions(model="model", jobs=3).validate()


def _commit(worktree: Path, relative: Path, contents: str, message: str) -> str:
    path = worktree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "--", str(relative)], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-m", message], check=True
    )
    return subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def test_deterministic_merge_accepts_only_assigned_claim_modules(tmp_path):
    project = tmp_path / "project"
    IncrementalSession.initialize(
        manuscript=FIXTURE,
        main_file="main.tex",
        task_file=FIXTURE / "VERIFY.yaml",
        project=project,
    )
    worktree = tmp_path / "worktree"
    base = _create_worktree(project, worktree)
    relative = claim_module_path("lem:zero-add")
    final = _commit(
        worktree,
        relative,
        "import Formalization.Foundation\nnamespace ManuscriptVerification\n"
        "theorem zeroAdd (n : Nat) : 0 + n = n := Nat.zero_add n\n"
        "end ManuscriptVerification\n",
        "Prove zero add",
    )
    result = BatchResult(
        index=1,
        claims=("lem:zero-add",),
        workspace=str(worktree),
        base_commit=base,
        final_commit=final,
        git_status="",
        build_succeeded=True,
        provider_failure=None,
        final_text="-- VERIFIED",
        thread_id="thread",
        turn_id="turn",
        tool_calls=1,
    )
    assert _merge_batch(project, result) is None
    assert "zeroAdd" in (project / relative).read_text(encoding="utf-8")
    _remove_worktree(project, worktree)

    forbidden_worktree = tmp_path / "forbidden-worktree"
    forbidden_base = _create_worktree(project, forbidden_worktree)
    forbidden_final = _commit(
        forbidden_worktree,
        Path("manuscript/main.tex"),
        "unauthorized source rewrite",
        "Rewrite manuscript",
    )
    forbidden = BatchResult(
        index=2,
        claims=("thm:add-zero",),
        workspace=str(forbidden_worktree),
        base_commit=forbidden_base,
        final_commit=forbidden_final,
        git_status="",
        build_succeeded=True,
        provider_failure=None,
        final_text="-- VERIFIED",
        thread_id="thread",
        turn_id="turn",
        tool_calls=1,
    )
    assert "host-controlled paths" in _merge_batch(project, forbidden)
    _remove_worktree(project, forbidden_worktree)
