from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path


class EnvironmentCheckError(RuntimeError):
    """Raised when a required local runtime component is unusable."""


@dataclass(frozen=True)
class CompilerCheck:
    executable: str
    lean_compiler: bool
    fallback_used: bool


def _elan_bin() -> Path:
    return Path.home() / ".elan" / "bin"


def ensure_lean_on_path(env: MutableMapping[str, str] | None = None) -> bool:
    """Add the conventional elan bin directory to this process when needed."""
    target = env if env is not None else os.environ
    if shutil.which("lean", path=target.get("PATH")) and shutil.which(
        "lake", path=target.get("PATH")
    ):
        return False
    elan_bin = _elan_bin()
    if not (elan_bin / "lean").is_file() or not (elan_bin / "lake").is_file():
        return False
    current = target.get("PATH", "")
    target["PATH"] = f"{elan_bin}{os.pathsep}{current}" if current else str(elan_bin)
    return True


def configure_portable_locale(
    env: MutableMapping[str, str] | None = None,
) -> bool:
    """Avoid macOS' unsupported ``C.UTF-8`` locale in Lean cache tooling."""
    target = env if env is not None else os.environ
    if sys.platform != "darwin":
        return False
    lang = target.get("LC_ALL") or target.get("LANG") or ""
    if lang.upper().replace("_", "-") not in {"C.UTF-8", "C.UTF8"}:
        return False
    target["LANG"] = "C"
    target["LC_ALL"] = "C"
    return True


def _compiler_smoke(executable: str, *, timeout: float = 30.0) -> str | None:
    """Compile and execute a C program, returning an error or ``None``."""
    try:
        with tempfile.TemporaryDirectory(prefix="proof-assistant-cc-") as raw_tmp:
            tmp = Path(raw_tmp)
            source = tmp / "check.c"
            binary = tmp / "check"
            source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
            compiled = subprocess.run(
                [executable, str(source), "-o", str(binary)],
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if compiled.returncode != 0:
                detail = (compiled.stderr or compiled.stdout).strip()
                return detail or f"compiler exited {compiled.returncode}"
            executed = subprocess.run(
                [str(binary)],
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if executed.returncode != 0:
                detail = (executed.stderr or executed.stdout).strip()
                return detail or f"compiled program exited {executed.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return None


def _lean_bundled_compiler(env: MutableMapping[str, str]) -> str | None:
    lean = shutil.which("lean", path=env.get("PATH"))
    if not lean:
        return None
    try:
        result = subprocess.run(
            [lean, "--print-prefix"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=dict(env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    compiler = Path(result.stdout.strip()) / "bin" / "clang"
    return str(compiler) if compiler.is_file() else None


def select_native_compiler(
    env: MutableMapping[str, str] | None = None,
) -> CompilerCheck:
    """Select a working compiler, preferring Lean's compiler when available."""
    target = env if env is not None else os.environ
    ensure_lean_on_path(target)

    explicit = target.get("LEAN_CC")
    if explicit:
        error = _compiler_smoke(explicit)
        if error:
            raise EnvironmentCheckError(
                f"LEAN_CC compiler {explicit!r} failed its compile/run check: {error}"
            )
        return CompilerCheck(explicit, lean_compiler=True, fallback_used=False)

    bundled = _lean_bundled_compiler(target)
    if bundled:
        error = _compiler_smoke(bundled)
        if error is None:
            return CompilerCheck(bundled, lean_compiler=True, fallback_used=False)

    fallbacks: list[str] = []
    if sys.platform == "darwin" and Path("/usr/bin/clang").is_file():
        fallbacks.append("/usr/bin/clang")
    configured = target.get("CC")
    if configured:
        fallbacks.append(configured)
    discovered = shutil.which("cc", path=target.get("PATH"))
    if discovered:
        fallbacks.append(discovered)

    for candidate in dict.fromkeys(fallbacks):
        if _compiler_smoke(candidate) is None:
            return CompilerCheck(
                candidate,
                lean_compiler=bundled is not None,
                fallback_used=bundled is not None,
            )

    if bundled:
        raise EnvironmentCheckError(
            f"Lean's bundled compiler {bundled!r} failed its compile/run check, "
            "and no working fallback compiler was found"
        )
    raise EnvironmentCheckError(
        "No working native C compiler was found; install clang or set LEAN_CC"
    )


def configure_lean_runtime(
    env: MutableMapping[str, str] | None = None,
) -> CompilerCheck:
    """Configure child Lake processes after proving compiler usability."""
    target = env if env is not None else os.environ
    ensure_lean_on_path(target)
    configure_portable_locale(target)
    check = select_native_compiler(target)
    target["LEAN_CC"] = check.executable
    return check


def default_lean_memory_limit_gb() -> int:
    """RepoProver's RLIMIT_AS pre-exec limit is unsupported on macOS."""
    return 0 if sys.platform == "darwin" else 24
