from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, MutableMapping, Sequence

from .environment import (
    CompilerCheck,
    configure_portable_locale,
    ensure_lean_on_path,
    select_native_compiler,
)


CACHE_HOME_ENV = "REPOPROVER_CODEX_CACHE_HOME"
_CONFIG_SCHEMA = 1
_REMOTE_FILESYSTEM_MARKERS = (
    "remote:",
    "nfs",
    "smb",
    "cifs",
    "afp",
    "webdav",
    "sshfs",
    "rclone",
    "fuse",
)


class CacheLocationError(RuntimeError):
    """Raised when a cache root violates the local-storage policy."""


@dataclass(frozen=True)
class CacheConfig:
    schema_version: int
    cache_root: str
    filesystem_type: str
    lean_cc: str
    lean_compiler: bool
    compiler_fallback_used: bool


@dataclass(frozen=True)
class CacheLayout:
    """All large package-managed state under one validated local root."""

    root: Path
    filesystem_type: str
    user_home: Path
    dropbox_roots: tuple[Path, ...]

    @property
    def mathlib_downloads(self) -> Path:
        return self.root / "mathlib-downloads"

    @property
    def lake_system(self) -> Path:
        return self.root / "lake" / "system"

    @property
    def lake_dependencies(self) -> Path:
        return self.root / "lake" / "dependencies"

    @property
    def lake_builds(self) -> Path:
        return self.root / "lake" / "builds"

    @property
    def worktrees(self) -> Path:
        return self.root / "worktrees"

    @property
    def fixtures(self) -> Path:
        return self.root / "fixtures"

    @property
    def locks(self) -> Path:
        return self.root / "locks"

    @property
    def temporary(self) -> Path:
        return self.root / "tmp"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def directories(self) -> tuple[Path, ...]:
        return (
            self.mathlib_downloads,
            self.lake_system,
            self.lake_dependencies,
            self.lake_builds,
            self.worktrees,
            self.fixtures,
            self.locks,
            self.temporary,
        )

    @classmethod
    def discover(
        cls,
        cache_home: str | Path | None = None,
        *,
        user_home: str | Path | None = None,
        dropbox_roots: Sequence[str | Path] | None = None,
        filesystem_type: str | None = None,
    ) -> "CacheLayout":
        home = Path(user_home).expanduser() if user_home else Path.home()
        configured = cache_home or os.environ.get(CACHE_HOME_ENV)
        requested = (
            Path(configured).expanduser()
            if configured
            else home / ".cache" / "repoprover-codex"
        )
        resolved_home = home.resolve()
        resolved_dropbox = (
            tuple(Path(item).expanduser().resolve() for item in dropbox_roots)
            if dropbox_roots is not None
            else tuple(_registered_dropbox_roots(resolved_home))
        )
        root, detected = validate_cache_root(
            requested,
            user_home=resolved_home,
            dropbox_roots=resolved_dropbox,
            filesystem_type=filesystem_type,
        )
        return cls(
            root=root,
            filesystem_type=detected,
            user_home=resolved_home,
            dropbox_roots=resolved_dropbox,
        )

    def create(self) -> None:
        for directory in self.directories:
            directory.mkdir(parents=True, exist_ok=True)

    def runtime_environment(
        self,
        base: Mapping[str, str] | None = None,
        *,
        lean_cc: str | None = None,
    ) -> dict[str, str]:
        env = dict(os.environ if base is None else base)
        env["MATHLIB_CACHE_DIR"] = str(self.mathlib_downloads)
        env["LAKE_CACHE_DIR"] = str(self.lake_system)
        configure_portable_locale(env)
        if lean_cc:
            env["LEAN_CC"] = lean_cc
        return env

    def apply_runtime_environment(
        self,
        target: MutableMapping[str, str] | None = None,
        *,
        lean_cc: str | None = None,
    ) -> None:
        destination = os.environ if target is None else target
        destination.update(self.runtime_environment(destination, lean_cc=lean_cc))

    def load_config(self) -> CacheConfig | None:
        if not self.config_path.is_file():
            return None
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            config = CacheConfig(**raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CacheLocationError(
                f"Invalid cache configuration at {self.config_path}: {exc}"
            ) from exc
        if config.schema_version != _CONFIG_SCHEMA:
            raise CacheLocationError(
                f"Unsupported cache configuration schema {config.schema_version}"
            )
        if Path(config.cache_root).resolve() != self.root:
            raise CacheLocationError(
                "Cache configuration root does not match the validated cache root"
            )
        return config

    def record_compiler(self, check: CompilerCheck) -> CacheConfig:
        config = CacheConfig(
            schema_version=_CONFIG_SCHEMA,
            cache_root=str(self.root),
            filesystem_type=self.filesystem_type,
            lean_cc=check.executable,
            lean_compiler=check.lean_compiler,
            compiler_fallback_used=check.fallback_used,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.root,
                prefix="config-",
                suffix=".json.tmp",
                delete=False,
            ) as temporary:
                json.dump(asdict(config), temporary, indent=2, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.config_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return config


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _registered_dropbox_roots(home: Path) -> list[Path]:
    roots: list[Path] = []
    info = home / ".dropbox" / "info.json"
    if info.exists():
        try:
            payload = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheLocationError(
                "Dropbox is installed but its root locations could not be verified"
            ) from exc
        if not isinstance(payload, dict):
            raise CacheLocationError("Dropbox root metadata has an invalid shape")
        for account in payload.values():
            if isinstance(account, dict) and isinstance(account.get("path"), str):
                roots.append(Path(account["path"]).expanduser().resolve())

    try:
        roots.extend(
            entry.resolve()
            for entry in home.iterdir()
            if entry.name.casefold().startswith("dropbox")
        )
    except OSError as exc:
        raise CacheLocationError(
            f"Could not inspect the user home for Dropbox roots: {exc}"
        ) from exc
    return list(dict.fromkeys(roots))


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _macos_filesystem_type(path: Path, mount_table: str) -> str:
    matches: list[tuple[int, str, set[str]]] = []
    for line in mount_table.splitlines():
        if " on " not in line or " (" not in line or not line.endswith(")"):
            continue
        _device, mounted = line.split(" on ", 1)
        mount_text, option_text = mounted.rsplit(" (", 1)
        mount_point = Path(
            mount_text.replace("\\040", " ").replace("\\011", "\t")
        )
        try:
            within = _is_within(path, mount_point)
        except ValueError:
            within = False
        if not within:
            continue
        option_items = [
            item.strip().casefold() for item in option_text[:-1].split(",")
        ]
        options = set(option_items)
        filesystem = option_items[0] if option_items else ""
        if filesystem:
            matches.append((len(mount_point.parts), filesystem, options))
    if not matches:
        raise CacheLocationError(
            f"Cache path is absent from the macOS mount table: {path}"
        )
    _length, filesystem, options = max(matches, key=lambda item: item[0])
    if "local" not in options:
        return f"remote:{filesystem}"
    return filesystem


def _filesystem_type(path: Path) -> str:
    probe = _nearest_existing(path)
    if sys.platform == "darwin":
        command = ["/sbin/mount"]
    elif sys.platform.startswith("linux"):
        stat = shutil.which("stat")
        if not stat:
            raise CacheLocationError("Cannot verify cache filesystem: stat is missing")
        command = [stat, "-f", "-c", "%T", str(probe)]
    else:
        raise CacheLocationError(
            "Cannot verify local cache storage on unsupported platform "
            f"{sys.platform!r}"
        )
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CacheLocationError(f"Could not inspect cache filesystem: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or result.stdout).strip()
        raise CacheLocationError(
            f"Could not verify cache filesystem: {detail or result.returncode}"
        )
    if sys.platform == "darwin":
        return _macos_filesystem_type(probe, result.stdout)
    value = result.stdout.strip().casefold()
    return value


def validate_cache_root(
    requested: str | Path,
    *,
    user_home: str | Path | None = None,
    dropbox_roots: Sequence[str | Path] | None = None,
    filesystem_type: str | None = None,
) -> tuple[Path, str]:
    """Return a canonical cache root after enforcing home/local/Dropbox rules."""
    home = (
        Path(user_home).expanduser().resolve()
        if user_home
        else Path.home().resolve()
    )
    raw = Path(requested).expanduser()
    if not raw.is_absolute():
        raise CacheLocationError(
            "Cache root must be an absolute path inside the user home"
        )
    root = raw.resolve()
    if root == home or not _is_within(root, home):
        raise CacheLocationError(
            f"Cache root must be a dedicated directory inside the user home: {home}"
        )

    named_dropbox = any(part.casefold().startswith("dropbox") for part in root.parts)
    roots = (
        [Path(item).expanduser().resolve() for item in dropbox_roots]
        if dropbox_roots is not None
        else _registered_dropbox_roots(home)
    )
    if named_dropbox or any(_is_within(root, dropbox) for dropbox in roots):
        raise CacheLocationError(f"Cache root must not reside in Dropbox: {root}")

    detected = (filesystem_type or _filesystem_type(root)).casefold()
    if any(marker in detected for marker in _REMOTE_FILESYSTEM_MARKERS):
        raise CacheLocationError(
            f"Cache root must use a local filesystem, not {detected!r}: {root}"
        )
    return root, detected


def initialize_cache(
    layout: CacheLayout,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[CacheConfig, CompilerCheck]:
    """Create the layout and prove the selected native compiler works."""
    layout.create()
    runtime = layout.runtime_environment(env)
    ensure_lean_on_path(runtime)
    check = select_native_compiler(runtime)
    config = layout.record_compiler(check)
    return config, check


def project_cache_target(project: str | Path, layout: CacheLayout) -> Path:
    root = Path(project).expanduser().resolve()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    safe_name = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in root.name
    ).strip("-")
    return layout.lake_builds / f"{safe_name or 'project'}-{digest}"


def ensure_project_outside_dropbox(
    project: str | Path, layout: CacheLayout
) -> Path:
    """Return a canonical project path after enforcing the Dropbox policy."""
    root = Path(project).expanduser().resolve()
    named_dropbox = any(
        part.casefold().startswith("dropbox") for part in root.parts
    )
    if named_dropbox or any(
        _is_within(root, dropbox) for dropbox in layout.dropbox_roots
    ):
        raise CacheLocationError(
            f"Lean project must not reside in Dropbox: {root}. Create a Git "
            f"worktree under {layout.worktrees} instead."
        )
    return root


def ensure_project_cache_managed(
    project: str | Path,
    layout: CacheLayout,
) -> Path:
    """Fail closed unless the project's ``.lake`` data resolves into the cache."""
    root = Path(project).expanduser().resolve()
    ensure_project_outside_dropbox(root, layout)
    lake = root / ".lake"
    resolved = lake.resolve()
    if not _is_within(resolved, layout.root):
        raise CacheLocationError(
            f"Lean cache is outside the managed cache root: {lake}. Run "
            f"`repoprover-codex cache attach --project {root}` first."
        )
    return resolved


def _prepare_git_lake_exclusion(project: Path) -> None:
    try:
        inside = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise CacheLocationError(
            f"Could not inspect project Git metadata: {exc}"
        ) from exc
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return
    tracked = subprocess.run(
        ["git", "-C", str(project), "ls-files", "--error-unmatch", "--", ".lake"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if tracked.returncode == 0:
        raise CacheLocationError(
            "Refusing to attach .lake because the project tracks it in Git"
        )
    git_path = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--git-path", "info/exclude"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if git_path.returncode != 0 or not git_path.stdout.strip():
        raise CacheLocationError(
            "Could not locate the repository-local Git exclude file"
        )
    exclude = Path(git_path.stdout.strip())
    if not exclude.is_absolute():
        exclude = project / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if "/.lake" not in {line.strip() for line in existing.splitlines()}:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(existing + separator + "/.lake\n", encoding="utf-8")


def attach_project_cache(project: str | Path, layout: CacheLayout) -> Path:
    """Move a project's ``.lake`` tree centrally and replace it with a symlink."""
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise CacheLocationError(f"Lean project does not exist: {root}")
    ensure_project_outside_dropbox(root, layout)
    layout.create()
    _prepare_git_lake_exclusion(root)
    lake = root / ".lake"
    target = project_cache_target(root, layout)
    lock = layout.locks / f"attach-{target.name}.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise CacheLocationError(
            f"Another cache attach is already active for {root}"
        ) from exc
    try:
        if lake.is_symlink():
            resolved = lake.resolve()
            if not _is_within(resolved, layout.root):
                raise CacheLocationError(
                    "Existing .lake symlink points outside the managed cache: "
                    f"{resolved}"
                )
            return resolved
        if lake.exists() and not lake.is_dir():
            raise CacheLocationError(f"Expected a .lake directory, found: {lake}")
        if lake.exists() and target.exists():
            raise CacheLocationError(
                "Both source and managed cache target exist; refusing to merge: "
                f"{target}"
            )

        moved = False
        if lake.is_dir():
            shutil.move(str(lake), str(target))
            moved = True
        else:
            target.mkdir(parents=True, exist_ok=True)
        try:
            lake.symlink_to(target, target_is_directory=True)
        except Exception:
            if moved and target.exists() and not lake.exists():
                shutil.move(str(target), str(lake))
            raise
        return ensure_project_cache_managed(root, layout)
    finally:
        lock.rmdir()
