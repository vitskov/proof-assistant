from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .integration import RepoProverAgent

if TYPE_CHECKING:
    from .cache import DependencyDepotClaim

MANUSCRIPT_SCHEMA_VERSION = 1
TASK_MAX_BYTES = 1024 * 1024
TASK_WORKSPACE_PATH = Path("RepoProverInput/TASK.md")
REPORT_WORKSPACE_PATH = Path("VERIFICATION_REPORT.md")
LATEX_SOURCE_SUFFIXES = frozenset({".tex", ".ltx"})

# These are local/runtime state, not manuscript inputs. Symlinks are preserved
# so the copy never follows a link into a Python environment or external tree.
IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".lake",
        ".claude",
        ".codex",
        ".copilot",
        ".ssh",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
IGNORED_FILE_NAMES = frozenset(
    {
        ".DS_Store",
        ".env",
        ".env.local",
        ".netrc",
        ".npmrc",
        "auth.json",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "service-account.json",
    }
)
IGNORED_FILE_SUFFIXES = frozenset(
    {
        ".aux",
        ".bbl",
        ".blg",
        ".fdb_latexmk",
        ".fls",
        ".log",
        ".nav",
        ".olean",
        ".out",
        ".pyc",
        ".pyo",
        ".snm",
        ".synctex.gz",
        ".toc",
        ".vrb",
    }
)


RAW_LEAN_TOOLCHAIN = "leanprover/lean4:v4.28.0\n"

RAW_LAKEFILE = """\
import Lake
open Lake DSL

package «manuscriptVerification» where
  leanOptions := #[
    ⟨`autoImplicit, false⟩
  ]

require mathlib from git
  "https://github.com/leanprover-community/mathlib4" @ "v4.28.0"

require REPL from git
  "https://github.com/leanprover-community/repl" @ "v4.28.0-rc1"

@[default_target]
lean_lib «ManuscriptVerification» where
  roots := #[`Manuscript]
  globs := #[.submodules `Formalization]
"""

RAW_ROOT_MODULE = """\
import Formalization.Verification
"""

RAW_VERIFICATION_MODULE = """\
import Mathlib

namespace ManuscriptVerification

/- The verification agent adds formal statements and complete proofs here. -/

end ManuscriptVerification
"""

WORKSPACE_GITIGNORE = """\
/.lake/
*.olean
*.ilean
*.trace
.DS_Store
"""

MANUSCRIPT_SYSTEM_PROMPT = """\
You are a RepoProver manuscript-verification agent working in a private,
isolated Git workspace. Your job is to execute the user's verification task
against the copied LaTeX manuscript and produce checkable Lean 4 evidence.

RepoProver's explicit file, Git, shell, Mathlib, and Lean tools are your only
mutation and execution interface. Manuscript source files are evidence and are
read-only. Do not modify anything in the manuscript source snapshot.

The authoritative task statement is in `RepoProverInput/TASK.md`. Read that
file completely before deciding what to formalize. Also read `CONTENTS.md` and
the relevant LaTeX sources. If the task is ambiguous, choose the narrowest
reasonable mathematical interpretation and state it explicitly in the report.

For a successful verification:

1. Translate the requested claim and all necessary assumptions faithfully.
2. Create or update Lean files in the existing project structure, using
   `Formalization/` for a generated manuscript workspace.
3. Do not use `sorry`, `admit`, new axioms, or an inconsistent assumption to
   manufacture a result.
4. Invoke `lean_check` on the central formalization and run `lake build`.
5. Write `VERIFICATION_REPORT.md` with sections: Task, Interpretation,
   Manuscript Evidence, Lean Formalization, Checks Performed, Outcome, and
   Limitations. Cite manuscript paths and the relevant LaTeX labels or lines.
6. Stage and commit all work with RepoProver's Git tools.

End the final response with exactly one marker on its own line:

`-- VERIFIED` means the requested statement has complete Lean evidence.
`-- UNVERIFIED` means the run did not establish it. This never by itself means
the statement is mathematically false.
`-- BLOCKED` means missing/ambiguous source material or infrastructure prevented
a meaningful verification.
"""


class ManuscriptInputError(RuntimeError):
    """Raised when a manuscript run cannot be prepared safely."""


@dataclass(frozen=True)
class ManuscriptWorkspace:
    output: Path
    workspace: Path
    artifacts: Path
    input_task: Path
    workspace_task: Path
    report: Path
    source_mode: str
    source_root: Path
    task_sha256: str
    baseline_commit: str
    latex_sources: tuple[str, ...]


@dataclass(frozen=True)
class CommandRecord:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    required: bool = True

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class ManuscriptEvaluation:
    outcome: str
    detail: str
    exit_code: int
    marker: str | None
    successful_lean_checks: int
    report_present: bool
    commit_created: bool
    worktree_clean: bool
    independent_build_succeeded: bool


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_task_file(path: str | Path) -> tuple[Path, str, str]:
    task_path = Path(path).expanduser().resolve()
    if not task_path.is_file():
        raise ManuscriptInputError(f"Task file does not exist: {task_path}")
    size = task_path.stat().st_size
    if size > TASK_MAX_BYTES:
        raise ManuscriptInputError(
            f"Task file exceeds the {TASK_MAX_BYTES}-byte limit: {task_path}"
        )
    try:
        raw = task_path.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManuscriptInputError(
            f"Task file must be valid UTF-8: {task_path}"
        ) from exc
    if "\x00" in text:
        raise ManuscriptInputError("Task file must not contain NUL bytes")
    if not text.strip():
        raise ManuscriptInputError(f"Task file is empty: {task_path}")
    return task_path, text, hashlib.sha256(raw).hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    ignored_directories = {name.casefold() for name in IGNORED_DIRECTORY_NAMES}
    ignored_files = {name.casefold() for name in IGNORED_FILE_NAMES}
    for name in names:
        folded = name.casefold()
        suffixes = Path(name).suffixes
        combined_suffix = "".join(suffixes).casefold()
        if (
            folded in ignored_directories
            or folded in ignored_files
            or folded.startswith(".env.")
            or combined_suffix in IGNORED_FILE_SUFFIXES
        ):
            ignored.add(name)
    return ignored


def _copy_directory_contents(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name in _copy_ignore(str(source), [entry.name]):
            continue
        target = destination / entry.name
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(
                entry,
                target,
                symlinks=True,
                ignore=_copy_ignore,
            )
        elif entry.is_symlink():
            target.symlink_to(os.readlink(entry), target_is_directory=entry.is_dir())
        else:
            shutil.copy2(entry, target, follow_symlinks=False)


def _validate_source_symlinks(source: Path) -> None:
    for path in source.rglob("*"):
        if not path.is_symlink():
            continue
        if Path(os.readlink(path)).is_absolute():
            raise ManuscriptInputError(
                f"Absolute source symlink is not portable into the output: {path}"
            )
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ManuscriptInputError(f"Broken source symlink: {path}") from exc
        if not _is_within(resolved, source):
            raise ManuscriptInputError(
                f"Source symlink escapes the manuscript directory: {path}"
            )


def _latex_files(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() in LATEX_SOURCE_SUFFIXES
        ),
        key=lambda path: path.as_posix(),
    )


def _git_output(
    workspace: Path,
    args: Sequence[str],
    *,
    check: bool = True,
) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ManuscriptInputError(
            f"Git command failed ({' '.join(args)}): {detail or result.returncode}"
        )
    return result.stdout.strip()


def _source_git_metadata(source: Path) -> dict[str, Any]:
    inside = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--is-inside-work-tree"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"is_git_repository": False, "head": None, "dirty": None}
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    status = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain=v1"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return {
        "is_git_repository": True,
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_json(path: str | Path, payload: Any) -> None:
    """Write a stable UTF-8 JSON artifact atomically."""
    _write_json(Path(path), payload)


def _generated_contents(latex_paths: Sequence[str]) -> str:
    source_lines = "\n".join(f"- `{path}`" for path in latex_paths)
    return f"""\
# Contents

## Manuscript sources

{source_lines}

## Verification task

- Authoritative task: `{TASK_WORKSPACE_PATH.as_posix()}`
- Human-readable result: `{REPORT_WORKSPACE_PATH.as_posix()}`

## Lean formalization

- Entry point: `Manuscript.lean`
- Verification modules: `Formalization/`
"""


def _write_raw_lean_scaffold(workspace: Path, latex_paths: Sequence[str]) -> None:
    (workspace / "lean-toolchain").write_text(RAW_LEAN_TOOLCHAIN, encoding="utf-8")
    (workspace / "lakefile.lean").write_text(RAW_LAKEFILE, encoding="utf-8")
    (workspace / "Manuscript.lean").write_text(RAW_ROOT_MODULE, encoding="utf-8")
    formalization = workspace / "Formalization"
    formalization.mkdir()
    (formalization / "Verification.lean").write_text(
        RAW_VERIFICATION_MODULE, encoding="utf-8"
    )
    (workspace / "CONTENTS.md").write_text(
        _generated_contents(latex_paths), encoding="utf-8"
    )


def _ensure_workspace_metadata(workspace: Path, latex_paths: Sequence[str]) -> None:
    contents = workspace / "CONTENTS.md"
    if not contents.exists():
        contents.write_text(_generated_contents(latex_paths), encoding="utf-8")
    gitignore = workspace / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    lines = existing.splitlines()
    additions = [
        line for line in WORKSPACE_GITIGNORE.splitlines() if line and line not in lines
    ]
    if additions:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        gitignore.write_text(
            existing + separator + "\n".join(additions) + "\n",
            encoding="utf-8",
        )


def prepare_manuscript_workspace(
    manuscript: str | Path,
    output: str | Path,
    task_file: str | Path,
) -> ManuscriptWorkspace:
    """Copy inputs into a new, isolated, Git-backed verification workspace."""
    # Import lazily: workspace.source imports this module for shared constants.
    from .workspace.paths import ProofAssistantWritePathError, validate_proof_assistant_write_path

    source = Path(manuscript).expanduser().resolve()
    try:
        destination = validate_proof_assistant_write_path(
            output, purpose="Manuscript-run work and output"
        )
    except ProofAssistantWritePathError as exc:
        raise ManuscriptInputError(str(exc)) from exc
    if not source.is_dir():
        raise ManuscriptInputError(f"Manuscript directory does not exist: {source}")
    _validate_source_symlinks(source)
    task_path, task_text, task_sha256 = read_task_file(task_file)
    source_latex = _latex_files(source)
    if not source_latex:
        raise ManuscriptInputError(
            f"Manuscript directory contains no .tex or .ltx source: {source}"
        )
    if _is_within(destination, source) or _is_within(source, destination):
        raise ManuscriptInputError(
            "Manuscript and output directories must not contain one another"
        )
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise ManuscriptInputError(
                f"Output path must be a new directory: {destination}"
            )
        if any(destination.iterdir()):
            raise ManuscriptInputError(
                f"Output directory is not empty; refusing to overwrite: {destination}"
            )
        destination.rmdir()

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.preparing-", dir=destination.parent
        )
    )
    try:
        workspace = staging / "workspace"
        artifacts = staging / "artifacts"
        workspace.mkdir()
        artifacts.mkdir()

        is_lean_project = (source / "lean-toolchain").is_file() and (
            (source / "lakefile.lean").is_file() or (source / "lakefile.toml").is_file()
        )
        if is_lean_project:
            source_mode = "existing_lean_project"
            _copy_directory_contents(source, workspace)
            latex_root = workspace
        else:
            source_mode = "generated_lean_project"
            latex_root = workspace / "manuscript"
            _copy_directory_contents(source, latex_root)

        copied_latex = _latex_files(latex_root)
        relative_latex = tuple(
            path.relative_to(workspace).as_posix() for path in copied_latex
        )
        if not relative_latex:
            raise ManuscriptInputError(
                "No LaTeX sources remained after excluding runtime/build files"
            )
        if source_mode == "generated_lean_project":
            _write_raw_lean_scaffold(workspace, relative_latex)
        _ensure_workspace_metadata(workspace, relative_latex)

        workspace_task = workspace / TASK_WORKSPACE_PATH
        workspace_task.parent.mkdir(parents=True, exist_ok=True)
        workspace_task.write_text(task_text, encoding="utf-8")
        input_task = staging / "TASK.md"
        input_task.write_text(task_text, encoding="utf-8")

        source_meta = _source_git_metadata(source)
        manifest = {
            "schema_version": MANUSCRIPT_SCHEMA_VERSION,
            "created_at": utc_now(),
            "source": {
                "manuscript_directory": str(source),
                "task_file": str(task_path),
                **source_meta,
            },
            "snapshot": {
                "mode": source_mode,
                "latex_sources": [
                    {
                        "path": path.relative_to(workspace).as_posix(),
                        "sha256": sha256_file(path),
                    }
                    for path in copied_latex
                ],
                "task_sha256": task_sha256,
                "excluded_directory_names": sorted(IGNORED_DIRECTORY_NAMES),
                "excluded_file_names": sorted(IGNORED_FILE_NAMES),
                "excluded_file_suffixes": sorted(IGNORED_FILE_SUFFIXES),
            },
        }
        _write_json(staging / "INPUT_MANIFEST.json", manifest)
        shutil.copy2(
            staging / "INPUT_MANIFEST.json", workspace / "RepoProverInput/MANIFEST.json"
        )

        _git_output(workspace, ["init", "-q", "-b", "main"])
        _git_output(workspace, ["config", "user.name", "Proof Assistant"])
        _git_output(
            workspace,
            ["config", "user.email", "proof-assistant@localhost"],
        )
        _git_output(workspace, ["add", "--all"])
        _git_output(
            workspace,
            ["commit", "-q", "-m", "Import manuscript verification inputs"],
        )
        baseline_commit = _git_output(workspace, ["rev-parse", "HEAD"])

        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    final_workspace = destination / "workspace"
    return ManuscriptWorkspace(
        output=destination,
        workspace=final_workspace,
        artifacts=destination / "artifacts",
        input_task=destination / "TASK.md",
        workspace_task=final_workspace / TASK_WORKSPACE_PATH,
        report=final_workspace / REPORT_WORKSPACE_PATH,
        source_mode=source_mode,
        source_root=source,
        task_sha256=task_sha256,
        baseline_commit=baseline_commit,
        latex_sources=relative_latex,
    )


def create_manuscript_agent(workspace: Path) -> RepoProverAgent:
    """Construct a free-form agent while keeping RepoProver optional at import."""
    try:
        from repoprover.agents.contributor import ContributorAgent, ContributorTask
    except ImportError as exc:  # pragma: no cover - exercised through CLI
        raise ManuscriptInputError(
            "RepoProver is not importable in the active Python environment"
        ) from exc

    class ManuscriptAgent(ContributorAgent):
        agent_type = "manuscript_verifier"

        def get_system_prompt(self) -> str:
            return MANUSCRIPT_SYSTEM_PROMPT

        def build_user_prompt(self, **_kwargs: object) -> str:
            sources = "\n".join(f"- `{item}`" for item in self._latex_sources)
            return f"""\
Execute the complete free-form verification task stored in
`{TASK_WORKSPACE_PATH.as_posix()}`.

Read the task file to EOF using repeated `file_read` calls if necessary. Its
contents, not this wrapper prompt, define what must be verified.

Available LaTeX entry files in the immutable input snapshot:
{sources}

Place the durable explanation in `{REPORT_WORKSPACE_PATH.as_posix()}`, retain
the Lean evidence in the workspace, run the required checks, and commit the
result. Do not report VERIFIED unless the requested claim has complete Lean
evidence and no `sorry`, `admit`, or new axioms.
"""

    agent = ManuscriptAgent(
        task=ContributorTask.fix(),
        repo_root=workspace,
    )
    agent._latex_sources = tuple(
        path.relative_to(workspace).as_posix() for path in _latex_files(workspace)
    )
    return agent


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    required: bool = True,
) -> CommandRecord:
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return CommandRecord(
            argv=tuple(argv),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            required=required,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        )
        return CommandRecord(
            argv=tuple(argv),
            returncode=124,
            stdout=stdout,
            stderr=(stderr + f"\nTimed out after {timeout} seconds").strip(),
            required=required,
        )
    except OSError as exc:
        return CommandRecord(
            argv=tuple(argv),
            returncode=127,
            stdout="",
            stderr=str(exc),
            required=required,
        )


def bootstrap_lean_workspace(
    workspace: Path,
    *,
    env: Mapping[str, str],
    timeout: float,
    depot_claim: DependencyDepotClaim | None = None,
) -> list[CommandRecord]:
    """Fetch/build the dependencies required by RepoProver's Lean REPL."""
    records: list[CommandRecord] = []

    initial_commands: tuple[tuple[tuple[str, ...], bool], ...]
    if depot_claim is not None and depot_claim.ready:
        initial_commands = ((("lake", "build", "repl"), True),)
    else:
        initial_commands = (
            (("lake", "update"), True),
            (("lake", "exe", "cache", "get"), False),
            (("lake", "build", "repl"), True),
        )
    for argv, required in initial_commands:
        record = run_command(
            argv,
            cwd=workspace,
            env=env,
            timeout=timeout,
            required=required,
        )
        records.append(record)
        if required and not record.succeeded:
            return records

    if depot_claim is not None and not depot_claim.ready:
        depot_claim.promote()
        relocated_repl = run_command(
            ("lake", "build", "repl"),
            cwd=workspace,
            env=env,
            timeout=timeout,
        )
        records.append(relocated_repl)
        if not relocated_repl.succeeded:
            depot_claim.rollback()
            return records

    root_build = run_command(
        ("lake", "build"),
        cwd=workspace,
        env=env,
        timeout=timeout,
    )
    records.append(root_build)
    if not root_build.succeeded:
        if depot_claim is not None and not depot_claim.ready:
            depot_claim.rollback()
        return records
    if depot_claim is not None and not depot_claim.ready:
        depot_claim.commit()
    return records


def commit_bootstrap_state(workspace: Path) -> str:
    """Commit a generated/updated Lake manifest as part of the run baseline."""
    manifest = workspace / "lake-manifest.json"
    if manifest.is_file():
        _git_output(workspace, ["add", "--", "lake-manifest.json"])
        staged = subprocess.run(
            ["git", "-C", str(workspace), "diff", "--cached", "--quiet"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if staged.returncode == 1:
            _git_output(
                workspace,
                ["commit", "-q", "-m", "Record resolved Lean dependencies"],
            )
        elif staged.returncode != 0:
            detail = (staged.stderr or staged.stdout).strip()
            raise ManuscriptInputError(
                f"Could not inspect the bootstrap Git state: {detail}"
            )
    return _git_output(workspace, ["rev-parse", "HEAD"])


def workspace_git_state(workspace: Path) -> tuple[str, str]:
    """Return (HEAD, porcelain status) for an output workspace."""
    return (
        _git_output(workspace, ["rev-parse", "HEAD"]),
        _git_output(workspace, ["status", "--porcelain=v1"]),
    )


def command_records_text(records: Sequence[CommandRecord]) -> str:
    chunks: list[str] = []
    for record in records:
        chunks.append(f"$ {' '.join(record.argv)}")
        chunks.append(f"exit: {record.returncode}")
        if record.stdout:
            chunks.append("[stdout]\n" + record.stdout.rstrip())
        if record.stderr:
            chunks.append("[stderr]\n" + record.stderr.rstrip())
        chunks.append("")
    return "\n".join(chunks)


_MARKER_RE = re.compile(r"(?m)^--[ \t]+(VERIFIED|UNVERIFIED|BLOCKED)[ \t]*$")


def evaluate_manuscript_run(
    *,
    final_text: str,
    tool_calls: Sequence[Any],
    report: Path,
    baseline_commit: str,
    final_commit: str,
    git_status: str,
    independent_build: CommandRecord,
) -> ManuscriptEvaluation:
    markers = _MARKER_RE.findall(final_text)
    marker = markers[-1] if markers else None
    successful_lean_checks = sum(
        1
        for call in tool_calls
        if getattr(call, "name", None) == "lean_check"
        and bool(getattr(call, "success", False))
    )
    report_present = report.is_file() and bool(
        report.read_text(encoding="utf-8").strip()
    )
    commit_created = bool(final_commit) and final_commit != baseline_commit
    worktree_clean = not git_status.strip()
    build_ok = independent_build.succeeded

    if not build_ok:
        outcome = "tool_failure"
        detail = "independent final `lake build` failed"
        exit_code = 5
    elif marker == "VERIFIED":
        missing: list[str] = []
        if successful_lean_checks == 0:
            missing.append("a successful RepoProver lean_check")
        if not report_present:
            missing.append("VERIFICATION_REPORT.md")
        if not commit_created:
            missing.append("a task result commit")
        if not worktree_clean:
            missing.append("a clean committed workspace")
        if missing:
            outcome = "unverified"
            detail = "VERIFIED marker lacked " + ", ".join(missing)
            exit_code = 4
        else:
            outcome = "verified"
            detail = "Lean evidence, report, commit, and independent build are present"
            exit_code = 0
    elif marker == "BLOCKED":
        outcome = "blocked"
        detail = "agent reported that the verification is blocked"
        exit_code = 4
    elif marker == "UNVERIFIED":
        outcome = "unverified"
        detail = "agent did not establish the requested statement"
        exit_code = 4
    else:
        outcome = "incomplete"
        detail = "agent returned no recognized completion marker"
        exit_code = 4

    return ManuscriptEvaluation(
        outcome=outcome,
        detail=detail,
        exit_code=exit_code,
        marker=marker,
        successful_lean_checks=successful_lean_checks,
        report_present=report_present,
        commit_created=commit_created,
        worktree_clean=worktree_clean,
        independent_build_succeeded=build_ok,
    )


def serialize_command(record: CommandRecord) -> dict[str, Any]:
    return asdict(record)


def serialize_tool_call(call: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(call, "name", "")),
        "arguments": getattr(call, "arguments", None),
        "result": str(getattr(call, "result", "")),
        "success": bool(getattr(call, "success", False)),
    }
