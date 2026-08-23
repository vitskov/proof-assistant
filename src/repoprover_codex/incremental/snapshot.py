from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from ..manuscript import (
    IGNORED_DIRECTORY_NAMES,
    IGNORED_FILE_NAMES,
    IGNORED_FILE_SUFFIXES,
    ManuscriptInputError,
    _validate_source_symlinks,
)
from .io import sha256_bytes, sha256_path
from .models import Snapshot, SourceFile

EXTRA_IGNORED_DIRECTORIES = frozenset(
    {
        ".build",
        ".repoprover",
        ".swiftpm",
        "build",
        "dist",
        "target",
    }
)


def _ignored(path: Path) -> bool:
    name = path.name
    folded = name.casefold()
    suffix = "".join(path.suffixes).casefold()
    return (
        name in IGNORED_DIRECTORY_NAMES
        or name in EXTRA_IGNORED_DIRECTORIES
        or name in IGNORED_FILE_NAMES
        or folded.startswith(".env.")
        or suffix in IGNORED_FILE_SUFFIXES
    )


def _copy_snapshot_inputs(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for root, directory_names, file_names in os.walk(source, followlinks=False):
        root_path = Path(root)
        symlink_directories = [
            name for name in directory_names if (root_path / name).is_symlink()
        ]
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in symlink_directories and not _ignored(root_path / name)
        )
        relative_root = root_path.relative_to(source)
        target_root = destination / relative_root
        target_root.mkdir(parents=True, exist_ok=True)
        for name in sorted(symlink_directories):
            source_path = root_path / name
            if not _ignored(source_path):
                (target_root / name).symlink_to(os.readlink(source_path))
        for name in sorted(file_names):
            source_path = root_path / name
            if _ignored(source_path):
                continue
            target = target_root / name
            if source_path.is_symlink():
                target.symlink_to(os.readlink(source_path))
            else:
                shutil.copy2(source_path, target, follow_symlinks=False)


class SnapshotError(RuntimeError):
    pass


class SnapshotRepository:
    """Content-addressed manuscript snapshots stored in a private bare Git repo."""

    def __init__(self, project: Path) -> None:
        self.project = project
        self.root = project / ".repoprover" / "snapshots"
        self.git_dir = self.root / "manuscript.git"
        self.index = self.root / "snapshot.index"

    def _git(
        self,
        arguments: Iterable[str],
        *,
        work_tree: Path | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", f"--git-dir={self.git_dir}"]
        if work_tree is not None:
            command.append(f"--work-tree={work_tree}")
        command.extend(arguments)
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(self.index)
        environment.update(
            {
                "GIT_AUTHOR_NAME": "RepoProver Codex",
                "GIT_AUTHOR_EMAIL": "repoprover-codex@localhost",
                "GIT_COMMITTER_NAME": "RepoProver Codex",
                "GIT_COMMITTER_EMAIL": "repoprover-codex@localhost",
            }
        )
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            env=environment,
            timeout=120,
            check=False,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise SnapshotError(
                f"Snapshot Git command failed ({' '.join(command)}): "
                f"{detail or result.returncode}"
            )
        return result

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.git_dir.exists():
            result = subprocess.run(
                [
                    "git",
                    "init",
                    "--bare",
                    "--quiet",
                    "--initial-branch=main",
                    str(self.git_dir),
                ],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                raise SnapshotError((result.stderr or result.stdout).strip())

    def current_commit(self) -> str | None:
        self.initialize()
        result = self._git(["rev-parse", "--verify", "refs/heads/main"], check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def create(self, source: Path, *, run_id: int) -> Snapshot:
        source = source.expanduser().resolve()
        if not source.is_dir():
            raise ManuscriptInputError(f"Manuscript directory does not exist: {source}")
        _validate_source_symlinks(source)
        self.initialize()
        staging = Path(tempfile.mkdtemp(prefix="source-", dir=self.root))
        try:
            _copy_snapshot_inputs(source, staging)
            self.index.unlink(missing_ok=True)
            self._git(["read-tree", "--empty"])
            self._git(["add", "-A", "--", "."], work_tree=staging)
            tree = self._git(["write-tree"]).stdout.strip()
            previous = self.current_commit()
            if previous:
                previous_tree = self._git(
                    ["rev-parse", f"{previous}^{{tree}}"]
                ).stdout.strip()
            else:
                previous_tree = None

            identical = tree == previous_tree
            if identical and previous is not None:
                commit = previous
            else:
                arguments = ["commit-tree", tree]
                if previous:
                    arguments.extend(["-p", previous])
                commit = self._git(
                    arguments,
                    input_text=f"Snapshot manuscript for run {run_id:06d}\n",
                ).stdout.strip()
                self._git(["update-ref", "refs/heads/main", commit])
            self._git(["update-ref", f"refs/tags/snapshot/{run_id:06d}", commit])

            listing = self._git(["ls-tree", "-r", "-z", "--long", commit]).stdout
            records: list[SourceFile] = []
            for entry in listing.split("\0"):
                if not entry:
                    continue
                metadata, path_text = entry.split("\t", 1)
                _mode, object_type, blob, size_text = metadata.split()
                if object_type != "blob":
                    continue
                staged_path = staging / path_text
                records.append(
                    SourceFile(
                        path=path_text,
                        sha256=(
                            sha256_bytes(os.readlink(staged_path).encode("utf-8"))
                            if staged_path.is_symlink()
                            else sha256_path(staged_path)
                        ),
                        git_blob=blob,
                        size=int(size_text),
                    )
                )
            return Snapshot(
                commit=commit,
                tree=tree,
                previous_commit=previous,
                identical=identical,
                files=tuple(sorted(records, key=lambda item: item.path)),
            )
        finally:
            self.index.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

    def checkout(self, commit: str, destination: Path) -> None:
        self.initialize()
        if destination.exists():
            raise SnapshotError(f"Snapshot checkout destination exists: {destination}")
        destination.mkdir(parents=True)
        checkout_index = self.root / f"checkout-{os.getpid()}.index"
        prior_index = self.index
        self.index = checkout_index
        try:
            self._git(["read-tree", commit])
            prefix = str(destination) + os.sep
            self._git(
                ["checkout-index", "--all", "--force", f"--prefix={prefix}"],
                work_tree=destination,
            )
        finally:
            checkout_index.unlink(missing_ok=True)
            self.index = prior_index

    def diff(self, old: str | None, new: str) -> str:
        if old is None or old == new:
            return ""
        return self._git(["diff", "--binary", "--find-renames", old, new, "--"]).stdout

    def changed_paths(self, old: str | None, new: str) -> tuple[str, ...]:
        if old is None:
            return tuple(item.path for item in self._files_at(new))
        if old == new:
            return ()
        output = self._git(
            ["diff", "--name-only", "-z", "--find-renames", old, new, "--"]
        ).stdout
        return tuple(sorted(path for path in output.split("\0") if path))

    def _files_at(self, commit: str) -> tuple[SourceFile, ...]:
        output = self._git(["ls-tree", "-r", "-z", "--long", commit]).stdout
        files: list[SourceFile] = []
        for entry in output.split("\0"):
            if not entry:
                continue
            metadata, path_text = entry.split("\t", 1)
            _mode, object_type, blob, size_text = metadata.split()
            if object_type == "blob":
                files.append(SourceFile(path_text, "", blob, int(size_text)))
        return tuple(files)


def sync_project_manuscript(
    snapshots: SnapshotRepository, commit: str, project: Path
) -> Path:
    """Atomically replace the read-only manuscript copy with an exact snapshot."""
    state = project / ".repoprover"
    target = project / "manuscript"
    staging = state / f"manuscript-sync-{os.getpid()}"
    backup = state / "manuscript-sync-backup"
    if staging.exists():
        shutil.rmtree(staging)
    if backup.exists() and not target.exists():
        os.replace(backup, target)
    elif backup.exists():
        shutil.rmtree(backup)
    snapshots.checkout(commit, staging)
    if target.exists():
        os.replace(target, backup)
    os.replace(staging, target)
    shutil.rmtree(backup, ignore_errors=True)
    return target
