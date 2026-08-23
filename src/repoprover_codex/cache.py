from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Self

from .cache_index import CacheIndex, CacheIndexError, IndexedCacheEntry
from .environment import (
    CompilerCheck,
    configure_portable_locale,
    ensure_lean_on_path,
    select_native_compiler,
)

CACHE_HOME_ENV = "REPOPROVER_CODEX_CACHE_HOME"
CACHE_MAX_GB_ENV = "REPOPROVER_CODEX_CACHE_MAX_GB"
MIN_FREE_GB_ENV = "REPOPROVER_CODEX_MIN_FREE_GB"
DEFAULT_CACHE_MAX_GB = 16.0
DEFAULT_MIN_FREE_GB = 25.0
COLD_DEPOT_RESERVE_GB = 10.0
WARM_PROJECT_RESERVE_GB = 1.0
DEFAULT_GC_TIMEOUT_SECONDS = 900.0
_GIB = 1024**3
_CONFIG_SCHEMA = 2
_DEPOT_SCHEMA = 2
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


class CacheCapacityError(CacheLocationError):
    """Raised before work when cache limits cannot be satisfied safely."""


@dataclass(frozen=True)
class CacheConfig:
    schema_version: int
    cache_root: str
    filesystem_type: str
    lean_cc: str
    lean_compiler: bool
    compiler_fallback_used: bool
    max_bytes: int
    min_free_bytes: int


@dataclass(frozen=True)
class CachePolicy:
    max_bytes: int
    min_free_bytes: int

    @property
    def max_gb(self) -> float:
        return self.max_bytes / _GIB

    @property
    def min_free_gb(self) -> float:
        return self.min_free_bytes / _GIB


@dataclass(frozen=True)
class CacheUsage:
    managed_bytes: int
    free_bytes: int
    dependency_bytes: int
    build_bytes: int
    download_bytes: int
    lake_system_bytes: int
    temporary_bytes: int
    reserved_bytes: int = 0


@dataclass(frozen=True)
class CacheGcResult:
    before: CacheUsage
    after: CacheUsage
    removed: tuple[str, ...]
    skipped_active: tuple[str, ...]
    recursive_measurements: int = 0


@dataclass
class CacheLease:
    """A process-scoped advisory lock released automatically on process death."""

    path: Path
    handle: IO[str]
    exclusive: bool

    def downgrade(self) -> None:
        if self.exclusive:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_SH)
            self.exclusive = False

    def close(self) -> None:
        if not self.handle.closed:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


@dataclass
class CacheReservation:
    """Capacity held for one process and recovered through its OS lease."""

    index: CacheIndex
    identifier: str
    lease: CacheLease

    def close(self) -> None:
        lock_path = self.lease.path
        try:
            self.index.remove_reservation(self.identifier)
        finally:
            self.lease.close()
            lock_path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


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
    def trash(self) -> Path:
        return self.root / "trash"

    @property
    def index_path(self) -> Path:
        return self.root / "cache-index.sqlite3"

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
            self.trash,
        )

    @classmethod
    def discover(
        cls,
        cache_home: str | Path | None = None,
        *,
        user_home: str | Path | None = None,
        dropbox_roots: Sequence[str | Path] | None = None,
        filesystem_type: str | None = None,
    ) -> CacheLayout:
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
            if raw.get("schema_version") == 1:
                raw = {
                    **raw,
                    "schema_version": _CONFIG_SCHEMA,
                    "max_bytes": int(DEFAULT_CACHE_MAX_GB * _GIB),
                    "min_free_bytes": int(DEFAULT_MIN_FREE_GB * _GIB),
                }
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

    def record_compiler(
        self,
        check: CompilerCheck,
        *,
        policy: CachePolicy | None = None,
    ) -> CacheConfig:
        if policy is None:
            existing = self.load_config()
            policy = cache_policy(existing)
        config = CacheConfig(
            schema_version=_CONFIG_SCHEMA,
            cache_root=str(self.root),
            filesystem_type=self.filesystem_type,
            lean_cc=check.executable,
            lean_compiler=check.lean_compiler,
            compiler_fallback_used=check.fallback_used,
            max_bytes=policy.max_bytes,
            min_free_bytes=policy.min_free_bytes,
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


def _positive_gb(value: str | float, name: str) -> int:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CacheLocationError(f"{name} must be a positive number of GiB") from exc
    if parsed <= 0:
        raise CacheLocationError(f"{name} must be a positive number of GiB")
    return int(parsed * _GIB)


def cache_policy(
    config: CacheConfig | None = None,
    *,
    env: Mapping[str, str] | None = None,
    max_gb: float | None = None,
    min_free_gb: float | None = None,
) -> CachePolicy:
    """Resolve stored limits with explicit/environment overrides."""
    source = os.environ if env is None else env
    stored_max = config.max_bytes if config else int(DEFAULT_CACHE_MAX_GB * _GIB)
    stored_free = config.min_free_bytes if config else int(DEFAULT_MIN_FREE_GB * _GIB)
    max_value: str | float | None = max_gb
    free_value: str | float | None = min_free_gb
    if max_value is None:
        max_value = source.get(CACHE_MAX_GB_ENV)
    if free_value is None:
        free_value = source.get(MIN_FREE_GB_ENV)
    return CachePolicy(
        max_bytes=(
            _positive_gb(max_value, CACHE_MAX_GB_ENV)
            if max_value is not None
            else stored_max
        ),
        min_free_bytes=(
            _positive_gb(free_value, MIN_FREE_GB_ENV)
            if free_value is not None
            else stored_free
        ),
    )


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
        mount_point = Path(mount_text.replace("\\040", " ").replace("\\011", "\t"))
        try:
            within = _is_within(path, mount_point)
        except ValueError:
            within = False
        if not within:
            continue
        option_items = [item.strip().casefold() for item in option_text[:-1].split(",")]
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
        Path(user_home).expanduser().resolve() if user_home else Path.home().resolve()
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
    max_gb: float | None = None,
    min_free_gb: float | None = None,
) -> tuple[CacheConfig, CompilerCheck]:
    """Create the layout and prove the selected native compiler works."""
    layout.create()
    runtime = layout.runtime_environment(env)
    ensure_lean_on_path(runtime)
    check = select_native_compiler(runtime)
    policy = cache_policy(
        layout.load_config(), env=env, max_gb=max_gb, min_free_gb=min_free_gb
    )
    config = layout.record_compiler(check, policy=policy)
    return config, check


def project_cache_target(project: str | Path, layout: CacheLayout) -> Path:
    root = Path(project).expanduser().resolve()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    safe_name = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in root.name
    ).strip("-")
    return layout.lake_builds / f"{safe_name or 'project'}-{digest}"


def ensure_project_outside_dropbox(project: str | Path, layout: CacheLayout) -> Path:
    """Return a canonical project path after enforcing the Dropbox policy."""
    root = Path(project).expanduser().resolve()
    named_dropbox = any(part.casefold().startswith("dropbox") for part in root.parts)
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
            resolved.mkdir(parents=True, exist_ok=True)
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


def _allocated_size(
    root: Path,
    *,
    deadline: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Return allocated bytes in one bounded traversal without following links."""
    try:
        initial = root.lstat()
    except FileNotFoundError:
        return 0
    total = initial.st_blocks * 512
    if root.is_symlink() or not root.is_dir():
        return total
    processed = 0
    last_report = time.monotonic()
    stack = [root]
    while stack:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise CacheCapacityError(
                f"Cache accounting exceeded its time limit while measuring {root}"
            )
        if progress is not None and now - last_report >= 30.0:
            progress(f"cache GC measuring {root.name}: {processed} nodes inspected")
            last_report = now
        directory = stack.pop()
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            continue
        with entries:
            for entry in entries:
                if deadline is not None and time.monotonic() >= deadline:
                    raise CacheCapacityError(
                        "Cache accounting exceeded its time limit while measuring "
                        f"{root}"
                    )
                item = Path(entry.path)
                try:
                    stat = item.lstat()
                except FileNotFoundError:
                    continue
                total += stat.st_blocks * 512
                processed += 1
                if entry.is_dir(follow_symlinks=False):
                    stack.append(item)
    return total


def _entry_signature(path: Path) -> str:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    return ":".join(
        str(value)
        for value in (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    )


def _cache_entry_specs(
    layout: CacheLayout,
) -> list[tuple[Path, str, str, float, str]]:
    """Return one specification per eviction unit, never per bulk-cache file."""
    specs: list[tuple[Path, str, str, float, str]] = []
    child_groups = (
        (layout.lake_builds, "build"),
        (layout.lake_dependencies, "depot"),
        (layout.temporary, "temporary"),
        (layout.trash, "trash"),
    )
    for parent, kind in child_groups:
        for item in _direct_children(parent):
            try:
                modified = item.lstat().st_mtime
            except FileNotFoundError:
                continue
            lease_name = (
                "global-cache"
                if kind in {"temporary", "trash"}
                or (kind == "depot" and item.name.startswith("."))
                else f"{kind}-{item.name}"
            )
            specs.append((item, kind, lease_name, modified, _entry_signature(item)))

    bulk_groups = (
        (layout.mathlib_downloads, "download"),
        (layout.lake_system, "lake-system"),
    )
    for root, kind in bulk_groups:
        if not _direct_children(root):
            continue
        try:
            modified = root.lstat().st_mtime
        except FileNotFoundError:
            continue
        specs.append((root, kind, "global-cache", modified, _entry_signature(root)))
    return specs


def reconcile_cache_index(
    layout: CacheLayout,
    *,
    deadline: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Reconcile top-level eviction units, measuring each changed unit once."""
    layout.create()
    index = CacheIndex(layout.index_path)
    try:
        existing = {entry.path: entry for entry in index.entries()}
        seen: set[Path] = set()
        measurements = 0
        for path, kind, lease_name, modified, signature in _cache_entry_specs(layout):
            seen.add(path)
            current = existing.get(path)
            if (
                current is not None
                and current.signature == signature
                and current.kind == kind
                and current.state == "ready"
            ):
                continue
            lease = try_cache_lease(layout, lease_name, exclusive=True)
            if lease is None:
                index.mark_dirty(
                    path,
                    kind=kind,
                    signature=signature,
                    lease_name=lease_name,
                )
                continue
            try:
                allocated = _allocated_size(path, deadline=deadline, progress=progress)
            finally:
                lease.close()
            measurements += 1
            index.upsert_entry(
                IndexedCacheEntry(
                    path=path,
                    kind=kind,
                    allocated_bytes=allocated,
                    last_used=modified,
                    signature=signature,
                    lease_name=lease_name,
                    state="ready",
                )
            )
        index.remove_entries_not_in(seen)
        return measurements
    except CacheIndexError as exc:
        raise CacheLocationError(str(exc)) from exc


def refresh_cache_index_entry(
    layout: CacheLayout,
    path: Path,
    *,
    kind: str,
    lease_name: str,
    deadline: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Record a known-mutated entry with one post-operation measurement."""
    index = CacheIndex(layout.index_path)
    try:
        if not path.exists() and not path.is_symlink():
            index.remove_entry(path)
            return
        stat = path.lstat()
        index.upsert_entry(
            IndexedCacheEntry(
                path=path,
                kind=kind,
                allocated_bytes=_allocated_size(
                    path, deadline=deadline, progress=progress
                ),
                last_used=stat.st_mtime,
                signature=_entry_signature(path),
                lease_name=lease_name,
                state="ready",
            )
        )
    except CacheIndexError as exc:
        raise CacheLocationError(str(exc)) from exc


def cache_usage(
    layout: CacheLayout,
    *,
    reconcile: bool = True,
    timeout: float = DEFAULT_GC_TIMEOUT_SECONDS,
    progress: Callable[[str], None] | None = None,
) -> CacheUsage:
    if reconcile:
        reconcile_cache_index(
            layout,
            deadline=time.monotonic() + timeout,
            progress=progress,
        )
    index = CacheIndex(layout.index_path)
    try:
        entries = index.entries()
        reserved = sum(item.reserved_bytes for item in index.reservations())
    except CacheIndexError as exc:
        raise CacheLocationError(str(exc)) from exc
    totals: dict[str, int] = {}
    for entry in entries:
        totals[entry.kind] = totals.get(entry.kind, 0) + entry.allocated_bytes
    dependency = totals.get("depot", 0)
    builds = totals.get("build", 0)
    downloads = totals.get("download", 0)
    lake_system = totals.get("lake-system", 0)
    temporary = totals.get("temporary", 0) + totals.get("trash", 0)
    return CacheUsage(
        managed_bytes=dependency + builds + downloads + lake_system + temporary,
        free_bytes=shutil.disk_usage(layout.root).free,
        dependency_bytes=dependency,
        build_bytes=builds,
        download_bytes=downloads,
        lake_system_bytes=lake_system,
        temporary_bytes=temporary,
        reserved_bytes=reserved,
    )


def _lease_name(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    ).strip("-")
    if not safe:
        raise CacheLocationError("Cache lease name must not be empty")
    return safe[:180]


def try_cache_lease(
    layout: CacheLayout,
    name: str,
    *,
    exclusive: bool,
) -> CacheLease | None:
    layout.locks.mkdir(parents=True, exist_ok=True)
    lock_path = layout.locks / f"{_lease_name(name)}.lease"
    handle = lock_path.open("a+", encoding="utf-8")
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return CacheLease(lock_path, handle, exclusive)


def acquire_cache_lease(
    layout: CacheLayout,
    name: str,
    *,
    exclusive: bool,
    timeout: float = 600.0,
) -> CacheLease:
    deadline = time.monotonic() + timeout
    while True:
        lease = try_cache_lease(layout, name, exclusive=exclusive)
        if lease is not None:
            return lease
        if time.monotonic() >= deadline:
            raise CacheLocationError(
                f"Timed out waiting for active cache lease: {_lease_name(name)}"
            )
        time.sleep(0.1)


@dataclass(frozen=True)
class _CacheCandidate:
    path: Path
    kind: str
    allocated_bytes: int
    lease_name: str
    label: str
    modified: float
    state: str


def _direct_children(parent: Path) -> list[Path]:
    try:
        return list(parent.iterdir())
    except FileNotFoundError:
        return []


def _gc_candidates(layout: CacheLayout) -> list[_CacheCandidate]:
    try:
        entries = CacheIndex(layout.index_path).entries()
    except CacheIndexError as exc:
        raise CacheLocationError(str(exc)) from exc
    # Plan from coarse eviction units. In particular, the complete download
    # namespace is one candidate regardless of how many .ltar files it holds.
    priority = {
        "trash": 0,
        "temporary": 1,
        "build": 2,
        "depot": 3,
        "download": 4,
        "lake-system": 5,
    }
    candidates = [
        _CacheCandidate(
            path=entry.path,
            kind=entry.kind,
            allocated_bytes=entry.allocated_bytes,
            lease_name=entry.lease_name,
            label=f"{entry.kind}:{entry.path.name}",
            modified=entry.last_used,
            state=entry.state,
        )
        for entry in entries
        if entry.state in {"ready", "dirty", "deleting"}
    ]
    return sorted(
        candidates,
        key=lambda item: (priority.get(item.kind, 99), item.modified, item.label),
    )


def _make_tree_writable(root: Path) -> None:
    if root.is_symlink() or not root.exists():
        return
    for directory, directories, files in os.walk(root):
        for name in directories:
            item = Path(directory) / name
            try:
                item.chmod(item.stat().st_mode | 0o700)
            except FileNotFoundError:
                pass
        for name in files:
            item = Path(directory) / name
            try:
                item.chmod(item.stat().st_mode | 0o600)
            except FileNotFoundError:
                pass
    root.chmod(root.stat().st_mode | 0o700)


def _remove_cache_path(path: Path, layout: CacheLayout) -> None:
    if path.parent not in {
        layout.lake_builds,
        layout.lake_dependencies,
        layout.mathlib_downloads,
        layout.lake_system,
        layout.temporary,
        layout.trash,
    }:
        raise CacheLocationError(f"Refusing to remove unmanaged cache path: {path}")
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        _make_tree_writable(path)
        shutil.rmtree(path)


def _delete_tree_bounded(
    root: Path,
    *,
    deadline: float,
    progress: Callable[[str], None] | None,
) -> None:
    """Delete one tree in a single traversal with deadline/progress checks."""
    if root.is_symlink() or root.is_file():
        root.unlink(missing_ok=True)
        return
    if not root.exists():
        return
    processed = 0
    last_report = time.monotonic()

    def checkpoint() -> None:
        nonlocal last_report
        now = time.monotonic()
        if now >= deadline:
            raise CacheCapacityError(
                "Cache garbage collection exceeded its time limit while "
                f"deleting {root}; partial data remains safely quarantined"
            )
        if progress is not None and now - last_report >= 30.0:
            progress(f"cache GC deleting {root.name}: {processed} nodes removed")
            last_report = now

    stack: list[tuple[Path, bool]] = [(root, False)]
    while stack:
        checkpoint()
        path, visited = stack.pop()
        if visited:
            try:
                path.rmdir()
            except FileNotFoundError:
                pass
            processed += 1
            continue
        try:
            path.chmod(path.stat().st_mode | 0o700)
        except FileNotFoundError:
            continue
        stack.append((path, True))
        with os.scandir(path) as entries:
            for entry in entries:
                checkpoint()
                item = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    stack.append((item, False))
                    continue
                try:
                    item.unlink()
                except PermissionError:
                    item.chmod(item.stat().st_mode | 0o600)
                    item.unlink()
                except FileNotFoundError:
                    pass
                processed += 1
    if root.exists():
        # The traversal normally removes the root itself. Reaching this branch
        # means an external writer raced deletion despite the cache lease.
        raise CacheCapacityError(
            f"Cache entry changed while it was being deleted: {root}"
        )


def _quarantine_and_delete(
    candidate: _CacheCandidate,
    layout: CacheLayout,
    *,
    deadline: float,
    progress: Callable[[str], None] | None,
) -> None:
    path = candidate.path
    if not path.exists() and not path.is_symlink():
        return
    if path.parent == layout.trash:
        quarantined = path
    else:
        allowed_children = {
            layout.lake_builds,
            layout.lake_dependencies,
            layout.temporary,
        }
        allowed_bulk = {layout.mathlib_downloads, layout.lake_system}
        if path.parent not in allowed_children and path not in allowed_bulk:
            raise CacheLocationError(
                f"Refusing to delete unmanaged cache entry: {path}"
            )
        quarantined = layout.trash / f"gc-{uuid.uuid4().hex}-{path.name}"
        os.replace(path, quarantined)
        if path in allowed_bulk:
            path.mkdir(parents=True, exist_ok=True)
    _delete_tree_bounded(quarantined, deadline=deadline, progress=progress)


def _usage_after_removal(
    usage: CacheUsage,
    candidate: _CacheCandidate,
    layout: CacheLayout,
) -> CacheUsage:
    amount = candidate.allocated_bytes
    changes: dict[str, int] = {
        "managed_bytes": max(0, usage.managed_bytes - amount),
        "free_bytes": shutil.disk_usage(layout.root).free,
    }
    field = {
        "depot": "dependency_bytes",
        "build": "build_bytes",
        "download": "download_bytes",
        "lake-system": "lake_system_bytes",
        "temporary": "temporary_bytes",
        "trash": "temporary_bytes",
    }.get(candidate.kind)
    if field is not None:
        changes[field] = max(0, getattr(usage, field) - amount)
    return replace(usage, **changes)


def _clear_stale_reservations(layout: CacheLayout, index: CacheIndex) -> None:
    for reservation in index.reservations():
        lease = try_cache_lease(layout, reservation.lock_name, exclusive=True)
        if lease is None:
            continue
        try:
            index.remove_reservation(reservation.identifier)
        finally:
            lease.close()
            lease.path.unlink(missing_ok=True)
    live_lock_names = {item.lock_name for item in index.reservations()}
    for lock_path in layout.locks.glob("reservation-*.lease"):
        lock_name = lock_path.name.removesuffix(".lease")
        if lock_name in live_lock_names:
            continue
        lease = try_cache_lease(layout, lock_name, exclusive=True)
        if lease is None:
            continue
        lease.close()
        lock_path.unlink(missing_ok=True)


def _garbage_collect_cache_locked(
    layout: CacheLayout,
    policy: CachePolicy,
    *,
    reserve_bytes: int,
    protected: Sequence[Path],
    strict: bool,
    timeout: float,
    progress: Callable[[str], None] | None,
) -> CacheGcResult:
    deadline = time.monotonic() + timeout
    if progress is not None:
        progress("cache GC reconciling coarse cache index")
    measurements = reconcile_cache_index(
        layout,
        deadline=deadline,
        progress=progress,
    )
    index = CacheIndex(layout.index_path)
    _clear_stale_reservations(layout, index)
    before = cache_usage(layout, reconcile=False)
    current = before
    removed: list[str] = []
    skipped: list[str] = []
    protected_paths = {item.absolute() for item in protected}
    dirty_paths = {entry.path for entry in index.entries() if entry.state == "dirty"}

    def safe(usage: CacheUsage) -> bool:
        requested = reserve_bytes + usage.reserved_bytes
        return (
            (not dirty_paths or usage.reserved_bytes > 0)
            and usage.managed_bytes + requested <= policy.max_bytes
            and usage.free_bytes - requested >= policy.min_free_bytes
        )

    requested = reserve_bytes + current.reserved_bytes
    impossible_without_external_space = (
        requested > policy.max_bytes
        or current.free_bytes + current.managed_bytes - requested
        < policy.min_free_bytes
    )
    if strict and impossible_without_external_space:
        raise CacheCapacityError(
            "Cache safety limits cannot be satisfied even after deleting all "
            "inactive managed data: "
            f"managed={current.managed_bytes / _GIB:.1f} GiB, "
            f"limit={policy.max_gb:.1f} GiB, "
            f"free={current.free_bytes / _GIB:.1f} GiB, "
            f"active/requested reservations={requested / _GIB:.1f} GiB, "
            f"minimum free={policy.min_free_gb:.1f} GiB"
        )

    if not safe(current) and progress is not None:
        required = max(
            0,
            current.managed_bytes
            + current.reserved_bytes
            + reserve_bytes
            - policy.max_bytes,
            policy.min_free_bytes
            + current.reserved_bytes
            + reserve_bytes
            - current.free_bytes,
        )
        progress(
            "cache GC required: "
            f"reclaim at least {required / _GIB:.2f} GiB from coarse entries"
        )

    for candidate in _gc_candidates(layout):
        if safe(current):
            break
        if time.monotonic() >= deadline:
            raise CacheCapacityError("Cache garbage collection exceeded its time limit")
        if candidate.path.absolute() in protected_paths:
            continue
        lease = try_cache_lease(layout, candidate.lease_name, exclusive=True)
        if lease is None:
            skipped.append(candidate.label)
            continue
        try:
            if progress is not None:
                progress(
                    f"cache GC evicting {candidate.label} "
                    f"({candidate.allocated_bytes / _GIB:.2f} GiB)"
                )
            index.mark_deleting(candidate.path)
            _quarantine_and_delete(
                candidate,
                layout,
                deadline=deadline,
                progress=progress,
            )
            index.remove_entry(candidate.path)
            dirty_paths.discard(candidate.path)
            removed.append(candidate.label)
            current = _usage_after_removal(current, candidate, layout)
        finally:
            lease.close()

    if strict and not safe(current):
        requested = reserve_bytes + current.reserved_bytes
        raise CacheCapacityError(
            "Cache safety limits cannot be satisfied: "
            f"managed={current.managed_bytes / _GIB:.1f} GiB, "
            f"limit={policy.max_gb:.1f} GiB, "
            f"free={current.free_bytes / _GIB:.1f} GiB, "
            f"active/requested reservations={requested / _GIB:.1f} GiB, "
            f"minimum free={policy.min_free_gb:.1f} GiB. "
            "Active cache entries were not evicted."
        )
    return CacheGcResult(
        before,
        current,
        tuple(removed),
        tuple(skipped),
        recursive_measurements=measurements,
    )


def garbage_collect_cache(
    layout: CacheLayout,
    policy: CachePolicy,
    *,
    reserve_bytes: int = 0,
    protected: Sequence[Path] = (),
    strict: bool = True,
    timeout: float = DEFAULT_GC_TIMEOUT_SECONDS,
    progress: Callable[[str], None] | None = None,
) -> CacheGcResult:
    """Evict coarse inactive entries without rescanning inside the loop."""
    layout.create()
    started = time.monotonic()
    admission = acquire_cache_lease(
        layout, "admission", exclusive=True, timeout=timeout
    )
    try:
        remaining = max(0.0, timeout - (time.monotonic() - started))
        return _garbage_collect_cache_locked(
            layout,
            policy,
            reserve_bytes=reserve_bytes,
            protected=protected,
            strict=strict,
            timeout=remaining,
            progress=progress,
        )
    finally:
        admission.close()


def ensure_cache_capacity(
    layout: CacheLayout,
    policy: CachePolicy,
    *,
    reserve_gb: float,
    protected: Sequence[Path] = (),
    timeout: float = DEFAULT_GC_TIMEOUT_SECONDS,
    progress: Callable[[str], None] | None = None,
) -> CacheGcResult:
    return garbage_collect_cache(
        layout,
        policy,
        reserve_bytes=int(reserve_gb * _GIB),
        protected=protected,
        strict=True,
        timeout=timeout,
        progress=progress,
    )


def reserve_cache_capacity(
    layout: CacheLayout,
    policy: CachePolicy,
    *,
    reserve_gb: float,
    protected: Sequence[Path] = (),
    timeout: float = DEFAULT_GC_TIMEOUT_SECONDS,
    progress: Callable[[str], None] | None = None,
) -> tuple[CacheGcResult, CacheReservation]:
    """Atomically collect space and publish a crash-recoverable reservation."""
    layout.create()
    started = time.monotonic()
    admission = acquire_cache_lease(
        layout, "admission", exclusive=True, timeout=timeout
    )
    reservation_lease: CacheLease | None = None
    try:
        remaining = max(0.0, timeout - (time.monotonic() - started))
        result = _garbage_collect_cache_locked(
            layout,
            policy,
            reserve_bytes=int(reserve_gb * _GIB),
            protected=protected,
            strict=True,
            timeout=remaining,
            progress=progress,
        )
        identifier = uuid.uuid4().hex
        lock_name = f"reservation-{identifier}"
        reservation_lease = acquire_cache_lease(
            layout,
            lock_name,
            exclusive=True,
            timeout=max(0.0, timeout - (time.monotonic() - started)),
        )
        index = CacheIndex(layout.index_path)
        index.add_reservation(identifier, int(reserve_gb * _GIB), lock_name)
        return result, CacheReservation(index, identifier, reservation_lease)
    except CacheIndexError as exc:
        if reservation_lease is not None:
            reservation_lease.close()
        raise CacheLocationError(str(exc)) from exc
    except Exception:
        if reservation_lease is not None:
            reservation_lease.close()
        raise
    finally:
        admission.close()


@contextmanager
def managed_project_session(
    project: str | Path,
    layout: CacheLayout,
    policy: CachePolicy,
    *,
    attach: bool,
    reserve_gb: float,
    lease_timeout: float = 600.0,
    gc_timeout: float = DEFAULT_GC_TIMEOUT_SECONDS,
    progress: Callable[[str], None] | None = None,
) -> Iterator[Path]:
    """Protect global caches and one project build for the complete operation."""
    target = project_cache_target(project, layout)
    _gc_result, reservation = reserve_cache_capacity(
        layout,
        policy,
        reserve_gb=reserve_gb,
        protected=(target,),
        timeout=gc_timeout,
        progress=progress,
    )
    global_lease: CacheLease | None = None
    build_lease: CacheLease | None = None
    try:
        global_lease = acquire_cache_lease(
            layout, "global-cache", exclusive=False, timeout=lease_timeout
        )
        build_lease = acquire_cache_lease(
            layout, f"build-{target.name}", exclusive=False, timeout=lease_timeout
        )
        managed = (
            attach_project_cache(project, layout)
            if attach
            else ensure_project_cache_managed(project, layout)
        )
        index = CacheIndex(layout.index_path)
        try:
            index.mark_dirty(
                target,
                kind="build",
                signature=_entry_signature(target),
                lease_name=f"build-{target.name}",
            )
            for path, kind in (
                (layout.mathlib_downloads, "download"),
                (layout.lake_system, "lake-system"),
            ):
                index.mark_dirty(
                    path,
                    kind=kind,
                    signature=_entry_signature(path),
                    lease_name="global-cache",
                )
        except CacheIndexError as exc:
            raise CacheLocationError(str(exc)) from exc
        os.utime(managed, None)
        yield managed
    finally:
        try:
            refresh_cache_index_entry(
                layout,
                target,
                kind="build",
                lease_name=f"build-{target.name}",
                deadline=time.monotonic() + gc_timeout,
                progress=progress,
            )
            for path, kind in (
                (layout.mathlib_downloads, "download"),
                (layout.lake_system, "lake-system"),
            ):
                if _direct_children(path):
                    refresh_cache_index_entry(
                        layout,
                        path,
                        kind=kind,
                        lease_name="global-cache",
                        deadline=time.monotonic() + gc_timeout,
                        progress=progress,
                    )
        finally:
            if build_lease is not None:
                build_lease.close()
            if global_lease is not None:
                global_lease.close()
            reservation.close()
        try:
            garbage_collect_cache(
                layout,
                policy,
                strict=False,
                timeout=gc_timeout,
                progress=progress,
            )
        except CacheLocationError as exc:
            # Admission already protected the operation. Post-run GC is a
            # maintenance optimization and must not erase a successful result.
            if progress is not None:
                progress(f"WARNING: post-run cache GC failed: {exc}")


def dependency_cache_key(
    project: str | Path,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Hash dependency declarations, toolchain, platform, and compiler identity."""
    root = Path(project).expanduser().resolve()
    toolchain = root / "lean-toolchain"
    lakefile_lean = root / "lakefile.lean"
    lakefile_toml = root / "lakefile.toml"
    lakefile = lakefile_lean if lakefile_lean.is_file() else lakefile_toml
    if not lakefile.is_file():
        raise CacheLocationError(f"Lean project has no lakefile: {root}")

    contents = lakefile.read_text(encoding="utf-8")
    requirements: list[dict[str, str]] = []
    if lakefile == lakefile_lean:
        pattern = re.compile(
            r"(?ms)^\s*require\s+(?P<name>«[^»]+»|[A-Za-z0-9_.-]+)\s+"
            r"from\s+git\s*\"(?P<url>[^\"]+)\"\s*@\s*\"(?P<rev>[^\"]+)\""
        )
        requirements = [
            {
                "name": match.group("name").strip("«»"),
                "url": match.group("url"),
                "revision": match.group("rev"),
            }
            for match in pattern.finditer(contents)
        ]
    dependency_configuration: object
    if requirements:
        dependency_configuration = sorted(
            requirements, key=lambda item: (item["name"], item["url"], item["revision"])
        )
    else:
        # Unknown Lake syntax fails conservatively: project-only changes may
        # reduce reuse, but can never alias distinct dependency configurations.
        dependency_configuration = {
            "file": lakefile.name,
            "sha256": hashlib.sha256(lakefile.read_bytes()).hexdigest(),
        }
    source = os.environ if env is None else env
    identity = {
        "schema": _DEPOT_SCHEMA,
        "lean_toolchain": (
            hashlib.sha256(toolchain.read_bytes()).hexdigest()
            if toolchain.is_file()
            else ""
        ),
        "dependencies": dependency_configuration,
        "system": platform.system(),
        "machine": platform.machine(),
        "lean_cc": source.get("LEAN_CC", ""),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def dependency_depot_target(layout: CacheLayout, key: str) -> Path:
    if not key or any(character not in "0123456789abcdef" for character in key):
        raise CacheLocationError(f"Invalid dependency cache key: {key!r}")
    return layout.lake_dependencies / f"deps-{key}"


def dependency_depot_ready(target: Path) -> bool:
    return (
        (target / "READY").is_file()
        and (target / "packages").is_dir()
        and (target / "lake-manifest.json").is_file()
    )


def _write_json_atomic(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _seal_tree_read_only(root: Path) -> None:
    for directory, directories, files in os.walk(root):
        for name in files:
            item = Path(directory) / name
            item.chmod(item.stat().st_mode & ~0o222)
        for name in directories:
            item = Path(directory) / name
            item.chmod(item.stat().st_mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)


@dataclass
class DependencyDepotClaim:
    project: Path
    layout: CacheLayout
    key: str
    target: Path
    lease: CacheLease
    ready: bool
    promoted: bool = False

    def __enter__(self) -> Self:
        """Make every acquired depot claim usable with deterministic cleanup."""
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def packages_link(self) -> Path:
        return self.project / ".lake" / "packages"

    @property
    def project_manifest(self) -> Path:
        return self.project / "lake-manifest.json"

    def _attach_ready(self) -> None:
        manifest = self.target / "lake-manifest.json"
        if self.project_manifest.exists():
            if self.project_manifest.read_bytes() != manifest.read_bytes():
                raise CacheLocationError(
                    "Project manifest differs from the matching dependency depot"
                )
        else:
            shutil.copy2(manifest, self.project_manifest)
        link = self.packages_link
        if link.is_symlink():
            if link.resolve() != (self.target / "packages").resolve():
                raise CacheLocationError(
                    f"Existing packages symlink points outside its depot: {link}"
                )
        elif link.exists():
            if any(link.iterdir()):
                raise CacheLocationError(
                    f"Refusing to replace a nonempty project packages directory: {link}"
                )
            link.rmdir()
            link.symlink_to(self.target / "packages", target_is_directory=True)
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(self.target / "packages", target_is_directory=True)
        try:
            CacheIndex(self.layout.index_path).touch_entry(self.target)
        except CacheIndexError as exc:
            raise CacheLocationError(str(exc)) from exc

    def promote(self) -> None:
        if self.ready:
            return
        packages = self.packages_link
        if packages.is_symlink() or not packages.is_dir():
            raise CacheLocationError(
                f"Lake did not materialize a local packages directory: {packages}"
            )
        if not self.project_manifest.is_file():
            raise CacheLocationError("lake update did not create lake-manifest.json")
        staging = self.layout.lake_dependencies / (
            f".{self.target.name}.staging-{os.getpid()}-{uuid.uuid4().hex}"
        )
        staging.mkdir()
        try:
            shutil.move(str(packages), str(staging / "packages"))
            shutil.copy2(self.project_manifest, staging / "lake-manifest.json")
            _write_json_atomic(
                staging / "metadata.json",
                {
                    "schema_version": _DEPOT_SCHEMA,
                    "key": self.key,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "platform": platform.system(),
                    "architecture": platform.machine(),
                    "manifest_sha256": hashlib.sha256(
                        self.project_manifest.read_bytes()
                    ).hexdigest(),
                },
            )
            os.replace(staging, self.target)
            packages.symlink_to(self.target / "packages", target_is_directory=True)
            self.promoted = True
        except Exception:
            if packages.is_symlink():
                packages.unlink()
            source = (
                self.target / "packages"
                if (self.target / "packages").exists()
                else staging / "packages"
            )
            if source.exists() and not packages.exists():
                _make_tree_writable(source)
                shutil.move(str(source), str(packages))
            if self.target.exists():
                _remove_cache_path(self.target, self.layout)
            if staging.exists():
                _remove_cache_path(staging, self.layout)
            raise

    def commit(self) -> None:
        if self.ready:
            return
        if not self.promoted:
            raise CacheLocationError("Dependency depot has not been promoted")
        _seal_tree_read_only(self.target / "packages")
        (self.target / "READY").write_text("ready\n", encoding="utf-8")
        os.utime(self.target, None)
        refresh_cache_index_entry(
            self.layout,
            self.target,
            kind="depot",
            lease_name=f"depot-{self.target.name}",
        )
        self.ready = True
        self.lease.downgrade()

    def rollback(self) -> None:
        if self.ready or not self.promoted:
            return
        packages = self.packages_link
        if packages.is_symlink():
            packages.unlink()
        source = self.target / "packages"
        if source.exists():
            _make_tree_writable(source)
            shutil.move(str(source), str(packages))
        if self.target.exists():
            _remove_cache_path(self.target, self.layout)
        self.promoted = False

    def close(self) -> None:
        if not self.ready:
            self.rollback()
        self.lease.close()


def claim_dependency_depot(
    project: str | Path,
    layout: CacheLayout,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 600.0,
) -> DependencyDepotClaim:
    root = Path(project).expanduser().resolve()
    key = dependency_cache_key(root, env=env)
    target = dependency_depot_target(layout, key)
    lease_name = f"depot-{target.name}"

    # Ready depots are immutable, so compatible jobs can safely share them.
    # A shared lease also waits for an active builder to publish its atomic
    # READY marker. If no depot exists, callers race non-blockingly for the
    # exclusive builder lease; losers return to waiting in shared mode.
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        lease = acquire_cache_lease(
            layout, lease_name, exclusive=False, timeout=remaining
        )
        ready = dependency_depot_ready(target)
        if ready:
            break
        lease.close()
        lease = try_cache_lease(layout, lease_name, exclusive=True)
        if lease is not None:
            ready = dependency_depot_ready(target)
            break
        if time.monotonic() >= deadline:
            raise CacheLocationError(
                f"Timed out waiting for active cache lease: {lease_name}"
            )
    claim = DependencyDepotClaim(
        project=root,
        layout=layout,
        key=key,
        target=target,
        lease=lease,
        ready=ready,
    )
    try:
        if claim.ready:
            claim._attach_ready()
            lease.downgrade()
        elif target.exists():
            _remove_cache_path(target, layout)
        return claim
    except Exception:
        lease.close()
        raise
