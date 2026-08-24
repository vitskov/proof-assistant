"""Environment-keyed concurrency calibration in the validated package cache."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import statistics
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import psutil

from ..cache import CacheLayout

CALIBRATION_SCHEMA_VERSION = 1
DEFAULT_CALIBRATION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
GIB = 1024**3


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class CalibrationKey:
    os_name: str
    architecture: str
    usable_logical_cpus: int
    total_memory_bytes: int
    lean_version: str
    mathlib_revision: str | None
    import_profile: str
    codex_plan: str = "unknown"
    codex_model: str = ""
    codex_backend: str = "codex_subscription"

    @property
    def identifier(self) -> str:
        encoded = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:32]


@dataclass(frozen=True)
class ReplMemoryCalibration:
    warm_idle_rss_gib: float
    median_working_rss_gib: float
    p95_working_rss_gib: float
    maximum_observed_rss_gib: float
    samples: int

    @property
    def budget_gib(self) -> float:
        return max(2.0, 1.5 * self.p95_working_rss_gib)


@dataclass(frozen=True)
class CalibrationProfile:
    key: CalibrationKey
    repl: ReplMemoryCalibration | None = None
    recommended_lean_pool: int | None = None
    recommended_build_concurrency: int | None = None
    recommended_ai_concurrency: int | None = None
    tested_ai_ceiling: int | None = None
    measured_at: str = ""
    revision: int = 1
    schema_version: int = CALIBRATION_SCHEMA_VERSION


class CalibrationError(ValueError):
    pass


class LeanCalibrationError(RuntimeError):
    """A project could not produce trustworthy REPL memory measurements."""


@dataclass(frozen=True)
class LeanImportProfile:
    """Stable import workload used both for the cache key and REPL probe."""

    identifier: str
    header: str
    imports: tuple[str, ...]
    source_files: int


def _project_lean_files(project: Path) -> tuple[Path, ...]:
    roots = [project / "Formalization"]
    roots.extend(
        path
        for path in project.glob("*.lean")
        if path.is_file() and path.name != "lakefile.lean"
    )
    files: set[Path] = set()
    for root in roots:
        if root.is_file():
            files.add(root)
        elif root.is_dir():
            files.update(path for path in root.rglob("*.lean") if path.is_file())
    return tuple(sorted(files))


def project_import_profile(project: str | Path) -> LeanImportProfile:
    """Fingerprint the representative, source-defined Lean import workload."""

    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise LeanCalibrationError(f"Lean calibration project does not exist: {root}")
    imports: set[str] = set()
    files = _project_lean_files(root)
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise LeanCalibrationError(
                f"Cannot read Lean calibration source {path}: {exc}"
            ) from exc
        for line in source.splitlines():
            match = re.match(r"^\s*import\s+(.+?)\s*(?:--.*)?$", line)
            if match:
                modules = " ".join(match.group(1).split())
                if modules:
                    imports.add(f"import {modules}")
    if imports:
        header = "\n".join(sorted(imports))
    else:
        header = "import Mathlib"
    payload = {
        "header": header,
        "imports": sorted(imports),
        "sources": [path.relative_to(root).as_posix() for path in files],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return LeanImportProfile(
        identifier="imports-" + hashlib.sha256(encoded).hexdigest()[:24],
        header=header,
        imports=tuple(sorted(imports)),
        source_files=len(files),
    )


def _project_mathlib_revision(project: Path) -> str | None:
    manifest = project / "lake-manifest.json"
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    packages = payload.get("packages", []) if isinstance(payload, dict) else []
    for package in packages:
        if isinstance(package, dict) and package.get("name") == "mathlib":
            revision = package.get("rev") or package.get("version")
            return str(revision) if revision else None
    return None


def _version_stamp(root: Path) -> tuple[int, int]:
    values: list[int] = []
    for name in ("lean-toolchain", "lake-manifest.json"):
        try:
            values.append((root / name).stat().st_mtime_ns)
        except OSError:
            values.append(0)
    return values[0], values[1]


def _read_project_lean_version(
    root: Path, runner: Callable[..., subprocess.CompletedProcess[str]]
) -> str:
    """Read the selected Lean version without asking Lake to load the project.

    ``lake env`` is not an observational command for a fresh project: Lake may
    resolve the manifest and materialize ``.lake/packages``.  Runtime
    auto-tuning happens before Proof Assistant enters its managed cache/depot
    boundary, so version discovery must invoke only the toolchain selector.
    The ordinary ``lean`` launcher honors ``lean-toolchain`` when provided by
    Elan and cannot hydrate Lake dependencies.
    """

    try:
        result = runner(
            ("lean", "--version"),
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        version = (result.stdout or result.stderr).strip().splitlines()
        if version:
            return version[0]
    toolchain = root / "lean-toolchain"
    try:
        pinned = toolchain.read_text(encoding="utf-8").strip()
    except OSError:
        pinned = ""
    return pinned or "unknown"


@lru_cache(maxsize=128)
def _cached_project_lean_version(
    project: str, _toolchain_stamp: int, _manifest_stamp: int
) -> str:
    return _read_project_lean_version(Path(project), subprocess.run)


def project_lean_version(
    project: str | Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Return the effective Lean version, falling back to the pinned toolchain.

    Default subprocess discovery is cached by the two files that define the
    environment, so repeated admission handles never spawn repeated Lake
    processes. Supplying a runner bypasses the cache for deterministic tests.
    """

    root = Path(project).expanduser().resolve()
    if runner is not None:
        return _read_project_lean_version(root, runner)
    return _cached_project_lean_version(str(root), *_version_stamp(root))


def project_calibration_key(
    project: str | Path,
    *,
    resources: Any,
    codex_plan: str = "unknown",
    codex_model: str = "",
    codex_backend: str = "codex_subscription",
    lean_version: str | None = None,
) -> CalibrationKey:
    """Build the exact key shared by measurement and runtime lookup."""

    root = Path(project).expanduser().resolve()
    profile = project_import_profile(root)
    return CalibrationKey(
        os_name=str(resources.os_name),
        architecture=str(resources.architecture),
        usable_logical_cpus=int(resources.usable_logical_cpus),
        total_memory_bytes=int(resources.total_memory_bytes),
        lean_version=lean_version or project_lean_version(root),
        mathlib_revision=_project_mathlib_revision(root),
        import_profile=profile.identifier,
        codex_plan=codex_plan,
        codex_model=codex_model,
        codex_backend=codex_backend,
    )


def summarize_repl_memory(
    warm_idle_rss_gib: Sequence[float], working_rss_gib: Sequence[float]
) -> ReplMemoryCalibration:
    """Summarize positive RSS samples with a conservative nearest-rank p95."""

    warm = tuple(float(item) for item in warm_idle_rss_gib)
    working = tuple(float(item) for item in working_rss_gib)
    if not warm or not working or any(item <= 0 for item in (*warm, *working)):
        raise LeanCalibrationError("Lean REPL calibration needs positive RSS samples")
    ordered = sorted(working)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return ReplMemoryCalibration(
        warm_idle_rss_gib=statistics.median(warm),
        median_working_rss_gib=statistics.median(working),
        p95_working_rss_gib=p95,
        maximum_observed_rss_gib=max((*warm, *working)),
        samples=len(working),
    )


class _LeanReplProbe:
    """One disposable `lake exe repl` process with bounded protocol reads."""

    def __init__(
        self,
        project: Path,
        *,
        env: Mapping[str, str] | None,
        timeout: float,
        psutil_module: Any,
        popen: Callable[..., subprocess.Popen[bytes]],
    ) -> None:
        self.project = project
        self.env = dict(env or os.environ)
        self.timeout = timeout
        self.psutil = psutil_module
        self.popen = popen
        self.process: subprocess.Popen[bytes] | None = None
        self.stderr = bytearray()

    def __enter__(self) -> _LeanReplProbe:
        try:
            self.process = self.popen(
                ("lake", "exe", "repl"),
                cwd=self.project,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise LeanCalibrationError(f"Could not start Lean REPL: {exc}") from exc
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def request(self, payload: Mapping[str, object]) -> dict[str, object]:
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            raise LeanCalibrationError("Lean REPL probe is not running")
        if process.poll() is not None:
            raise LeanCalibrationError("Lean REPL exited before calibration")
        command = json.dumps(dict(payload), separators=(",", ":")).encode() + b"\n\n"
        try:
            process.stdin.write(command)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise LeanCalibrationError("Lean REPL closed its input") from exc

        selector = selectors.DefaultSelector()
        stdout_buffer = bytearray()
        deadline = time.monotonic() + self.timeout
        try:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            if process.stderr is not None:
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LeanCalibrationError(
                        f"Lean REPL calibration timed out after {self.timeout:g}s"
                    )
                ready = selector.select(remaining)
                if not ready:
                    raise LeanCalibrationError(
                        f"Lean REPL calibration timed out after {self.timeout:g}s"
                    )
                for key, _events in ready:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if key.data == "stderr":
                        self.stderr.extend(chunk)
                        if len(self.stderr) > 131072:
                            del self.stderr[:-131072]
                        continue
                    if not chunk:
                        detail = self.stderr.decode(errors="replace").strip()
                        raise LeanCalibrationError(
                            "Lean REPL exited during calibration"
                            + (f": {detail}" if detail else "")
                        )
                    stdout_buffer.extend(chunk)
                    if b"\n\n" not in stdout_buffer:
                        continue
                    raw, _separator, _remainder = stdout_buffer.partition(b"\n\n")
                    try:
                        response = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise LeanCalibrationError(
                            "Lean REPL returned malformed calibration JSON"
                        ) from exc
                    if not isinstance(response, dict):
                        raise LeanCalibrationError(
                            "Lean REPL returned a non-object calibration response"
                        )
                    return response
        finally:
            selector.close()

    def rss_gib(self) -> float:
        process = self.process
        if process is None or process.poll() is not None:
            raise LeanCalibrationError("Lean REPL exited before RSS sampling")
        try:
            root = self.psutil.Process(process.pid)
            processes = [root, *root.children(recursive=True)]
            rss = sum(item.memory_info().rss for item in processes if item.is_running())
        except (self.psutil.Error, OSError) as exc:
            raise LeanCalibrationError(
                f"Could not sample Lean REPL RSS: {exc}"
            ) from exc
        if rss <= 0:
            raise LeanCalibrationError("Lean REPL reported non-positive RSS")
        return rss / GIB

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            root = self.psutil.Process(process.pid)
            children = root.children(recursive=True)
            for child in reversed(children):
                child.terminate()
            root.terminate()
            _gone, alive = self.psutil.wait_procs([*children, root], timeout=3.0)
            for remaining in alive:
                remaining.kill()
            self.psutil.wait_procs(alive, timeout=2.0)
        except (self.psutil.Error, OSError):
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def measure_lean_repl_memory(
    project: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    process_count: int = 2,
    checks_per_process: int = 4,
    timeout: float = 180.0,
    settle_seconds: float = 0.1,
    psutil_module: Any = psutil,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
) -> ReplMemoryCalibration:
    """Measure sequential representative REPLs without Codex or proof mutation."""

    if process_count < 1 or checks_per_process < 1 or timeout <= 0:
        raise LeanCalibrationError("Lean REPL calibration bounds must be positive")
    root = Path(project).expanduser().resolve()
    profile = project_import_profile(root)
    warm_samples: list[float] = []
    working_samples: list[float] = []
    checks = (
        "example (n : Nat) : n = n := by rfl",
        "example (a b : Prop) (ha : a) (hb : b) : a ∧ b := by exact ⟨ha, hb⟩",
        "example (xs : List Nat) : xs.reverse.reverse = xs := by simp",
        "example (n : Nat) : n + 0 = n := by simp",
    )
    for _index in range(process_count):
        with _LeanReplProbe(
            root,
            env=env,
            timeout=timeout,
            psutil_module=psutil_module,
            popen=popen,
        ) as probe:
            response = probe.request(
                {"cmd": profile.header + "\n\nexample (n : Nat) : n = n := by rfl"}
            )
            if response.get("messages"):
                raise LeanCalibrationError(
                    "Representative project imports failed in the Lean REPL"
                )
            environment = response.get("env")
            if not isinstance(environment, int):
                raise LeanCalibrationError(
                    "Lean REPL calibration did not return an environment"
                )
            sleep(settle_seconds)
            warm_samples.append(probe.rss_gib())
            for check_index in range(checks_per_process):
                result = probe.request(
                    {
                        "cmd": checks[check_index % len(checks)],
                        "env": environment,
                    }
                )
                if result.get("messages"):
                    raise LeanCalibrationError(
                        "Representative Lean calibration check failed"
                    )
                sleep(settle_seconds)
                working_samples.append(probe.rss_gib())
    return summarize_repl_memory(warm_samples, working_samples)


def _validate_profile(profile: CalibrationProfile) -> None:
    if profile.schema_version != CALIBRATION_SCHEMA_VERSION:
        raise CalibrationError("Unsupported calibration schema version")
    if profile.revision < 1:
        raise CalibrationError("Calibration revision must be positive")
    if (
        isinstance(profile.key.usable_logical_cpus, bool)
        or not isinstance(profile.key.usable_logical_cpus, int)
        or profile.key.usable_logical_cpus < 1
        or isinstance(profile.key.total_memory_bytes, bool)
        or not isinstance(profile.key.total_memory_bytes, int)
        or profile.key.total_memory_bytes < 1
    ):
        raise CalibrationError("Calibration hardware allocation must be positive")
    for name in (
        "recommended_lean_pool",
        "recommended_build_concurrency",
        "recommended_ai_concurrency",
        "tested_ai_ceiling",
    ):
        value = getattr(profile, name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise CalibrationError(f"{name} must be a positive integer")
    if profile.repl is not None:
        repl = profile.repl
        values = (
            repl.warm_idle_rss_gib,
            repl.median_working_rss_gib,
            repl.p95_working_rss_gib,
            repl.maximum_observed_rss_gib,
        )
        if repl.samples < 1 or any(value <= 0 for value in values):
            raise CalibrationError("REPL calibration measurements must be positive")
        if not (
            repl.warm_idle_rss_gib <= repl.maximum_observed_rss_gib
            and repl.median_working_rss_gib <= repl.maximum_observed_rss_gib
            and repl.p95_working_rss_gib <= repl.maximum_observed_rss_gib
        ):
            raise CalibrationError(
                "REPL maximum must be at least every recorded summary measurement"
            )


class CalibrationStore:
    """One atomic JSON profile per environment fingerprint."""

    def __init__(self, cache_root: Path) -> None:
        self.root = cache_root.expanduser().resolve(strict=False) / "concurrency"
        self.profiles = self.root / "calibration"
        self.lock_path = self.root / "calibration.lock"

    @classmethod
    def discover(cls, cache_home: str | Path | None = None) -> CalibrationStore:
        layout = CacheLayout.discover(cache_home)
        return cls(layout.root)

    def path_for(self, key: CalibrationKey) -> Path:
        return self.profiles / f"{key.identifier}.json"

    def load(self, key: CalibrationKey) -> CalibrationProfile | None:
        path = self.path_for(key)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                if not path.is_file():
                    return None
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    stored_key = CalibrationKey(**raw["key"])
                    repl = (
                        ReplMemoryCalibration(**raw["repl"])
                        if raw.get("repl") is not None
                        else None
                    )
                    profile = CalibrationProfile(
                        key=stored_key,
                        repl=repl,
                        recommended_lean_pool=raw.get("recommended_lean_pool"),
                        recommended_build_concurrency=raw.get(
                            "recommended_build_concurrency"
                        ),
                        recommended_ai_concurrency=raw.get(
                            "recommended_ai_concurrency"
                        ),
                        tested_ai_ceiling=raw.get("tested_ai_ceiling"),
                        measured_at=str(raw.get("measured_at") or ""),
                        revision=int(raw.get("revision", 1)),
                        schema_version=int(raw.get("schema_version", 0)),
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise CalibrationError(
                        f"Invalid concurrency calibration at {path}: {exc}"
                    ) from exc
                if stored_key != key or path.stem != key.identifier:
                    raise CalibrationError("Calibration environment key does not match")
                _validate_profile(profile)
                return profile
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def load_fresh(
        self,
        key: CalibrationKey,
        *,
        max_age_seconds: float = DEFAULT_CALIBRATION_MAX_AGE_SECONDS,
        now: datetime | None = None,
    ) -> CalibrationProfile | None:
        """Load an exact environment match only while its evidence is fresh."""

        if max_age_seconds <= 0:
            raise CalibrationError("Calibration maximum age must be positive")
        profile = self.load(key)
        if profile is None or not profile.measured_at:
            return None
        try:
            measured = datetime.fromisoformat(profile.measured_at)
        except ValueError as exc:
            raise CalibrationError("Calibration timestamp is invalid") from exc
        if measured.tzinfo is None:
            measured = measured.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        age = (current - measured.astimezone(UTC)).total_seconds()
        if age < 0 or age > max_age_seconds:
            return None
        return profile

    def conservative_repl_p95_gib(
        self,
        *,
        os_name: str,
        architecture: str,
        usable_logical_cpus: int,
        total_memory_bytes: int,
        fallback_budget_gib: float,
        safety_multiplier: float,
        max_age_seconds: float = DEFAULT_CALIBRATION_MAX_AGE_SECONDS,
        now: datetime | None = None,
    ) -> float | None:
        """Return the safest fresh REPL evidence for this machine allocation.

        Lean admission is machine-global, so it cannot safely oscillate with
        whichever project happened to construct the latest runtime handle.
        Every fresh project profile for the same effective hardware allocation
        participates, and the uncalibrated fallback remains a budget floor.
        """

        if fallback_budget_gib <= 0 or safety_multiplier <= 0:
            raise CalibrationError("REPL memory policy values must be positive")
        if not self.profiles.is_dir():
            return None
        current = now or datetime.now(UTC)
        observed: list[float] = []
        for path in tuple(self.profiles.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                key = CalibrationKey(**raw["key"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                key.os_name != os_name
                or key.architecture != architecture
                or key.usable_logical_cpus != usable_logical_cpus
                or key.total_memory_bytes != total_memory_bytes
            ):
                continue
            try:
                profile = self.load_fresh(
                    key,
                    max_age_seconds=max_age_seconds,
                    now=current,
                )
            except CalibrationError:
                continue
            if profile is not None and profile.repl is not None:
                observed.append(profile.repl.p95_working_rss_gib)
        if not observed:
            return None
        fallback_p95 = fallback_budget_gib / safety_multiplier
        return max(fallback_p95, *observed)

    def save(self, profile: CalibrationProfile) -> CalibrationProfile:
        if not profile.measured_at:
            profile = replace(profile, measured_at=_utc_now())
        _validate_profile(profile)
        self.profiles.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                path = self.path_for(profile.key)
                payload = asdict(profile)
                temporary_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        "w",
                        encoding="utf-8",
                        dir=self.profiles,
                        prefix=f".{profile.key.identifier}.",
                        suffix=".tmp",
                        delete=False,
                    ) as temporary:
                        json.dump(payload, temporary, indent=2, sort_keys=True)
                        temporary.write("\n")
                        temporary.flush()
                        os.fsync(temporary.fileno())
                        temporary_path = Path(temporary.name)
                    os.replace(temporary_path, path)
                finally:
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
                return profile
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def reset(self, key: CalibrationKey) -> bool:
        path = self.path_for(key)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                existed = path.exists()
                path.unlink(missing_ok=True)
                return existed
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
