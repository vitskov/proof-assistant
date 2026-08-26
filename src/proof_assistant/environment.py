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
    lean_cc: str | None = None


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


def _compiler_smoke(
    executable: str,
    *,
    timeout: float = 30.0,
    env: MutableMapping[str, str] | None = None,
    compile_only: bool = False,
    standard_header: bool = True,
    lean_header: bool = False,
    lean_include: Path | None = None,
) -> str | None:
    """Compile and execute a representative C program."""
    try:
        with tempfile.TemporaryDirectory(prefix="proof-assistant-cc-") as raw_tmp:
            tmp = Path(raw_tmp)
            source = tmp / "check.c"
            binary = tmp / ("check.o" if compile_only else "check")
            includes = "#include <stddef.h>\n" if standard_header else ""
            if lean_header:
                includes += "#include <lean/lean.h>\n"
            body = "return sizeof(size_t) == 0;" if standard_header else "return 0;"
            source.write_text(
                f"{includes}int main(void) {{ {body} }}\n",
                encoding="utf-8",
            )
            command = [executable, str(source), "-o", str(binary)]
            if compile_only:
                command.append("-c")
            if lean_include is not None:
                command.extend(("-I", str(lean_include)))
            compiled = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=dict(env) if env is not None else None,
            )
            if compiled.returncode != 0:
                detail = (compiled.stderr or compiled.stdout).strip()
                return detail or f"compiler exited {compiled.returncode}"
            if compile_only:
                return None
            executed = subprocess.run(
                [str(binary)],
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=dict(env) if env is not None else None,
            )
            if executed.returncode != 0:
                detail = (executed.stderr or executed.stdout).strip()
                return detail or f"compiled program exited {executed.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return None


def _lean_bundled_compiler(
    env: MutableMapping[str, str], *, cwd: Path | None = None
) -> str | None:
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
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    prefix = Path(result.stdout.strip())
    compiler = prefix / "bin" / "clang"
    driver = prefix / "bin" / "leanc"
    lean_header = prefix / "include" / "lean" / "lean.h"
    if not compiler.is_file() or not driver.is_file() or not lean_header.is_file():
        return None
    return str(compiler)


def _lean_driver(bundled_compiler: str) -> tuple[str, Path]:
    prefix = Path(bundled_compiler).parent.parent
    return str(prefix / "bin" / "leanc"), prefix / "include"


def _external_compiler_smoke(
    executable: str,
    *,
    env: MutableMapping[str, str],
    bundled_compiler: str | None,
) -> str | None:
    """Validate an external compiler directly and through Lean when available."""
    error = _compiler_smoke(executable)
    if error is not None or bundled_compiler is None:
        return error
    driver, lean_include = _lean_driver(bundled_compiler)
    runtime = dict(env)
    runtime["LEAN_CC"] = executable
    return _compiler_smoke(
        driver,
        env=runtime,
        compile_only=True,
        lean_header=True,
        lean_include=lean_include,
    )


def select_native_compiler(
    env: MutableMapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
) -> CompilerCheck:
    """Select a working compiler, preferring Lean's compiler when available."""
    target = env if env is not None else os.environ
    ensure_lean_on_path(target)

    bundled = _lean_bundled_compiler(target, cwd=cwd)
    explicit = target.get("LEAN_CC")
    if explicit:
        if bundled and Path(explicit).resolve() == Path(bundled).resolve():
            raise EnvironmentCheckError(
                "LEAN_CC points to Lean's bundled clang; unset LEAN_CC so leanc "
                "can supply the toolchain's required compiler and linker flags"
            )
        error = _external_compiler_smoke(
            explicit,
            env=target,
            bundled_compiler=bundled,
        )
        if error:
            raise EnvironmentCheckError(
                f"LEAN_CC compiler {explicit!r} failed its compile/run check: {error}"
            )
        return CompilerCheck(
            explicit,
            lean_compiler=False,
            fallback_used=False,
            lean_cc=explicit,
        )

    if bundled:
        driver, _lean_include = _lean_driver(bundled)
        runtime = dict(target)
        runtime.pop("LEAN_CC", None)
        error = _compiler_smoke(
            driver,
            env=runtime,
            compile_only=True,
            lean_header=True,
        )
        if error is None:
            error = _compiler_smoke(bundled, standard_header=False)
        if error is None:
            return CompilerCheck(
                bundled,
                lean_compiler=True,
                fallback_used=False,
                lean_cc=None,
            )

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
        if (
            _external_compiler_smoke(
                candidate,
                env=target,
                bundled_compiler=bundled,
            )
            is None
        ):
            return CompilerCheck(
                candidate,
                lean_compiler=False,
                fallback_used=bundled is not None,
                lean_cc=candidate,
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
    *,
    cwd: Path | None = None,
) -> CompilerCheck:
    """Configure child Lake processes after proving compiler usability."""
    target = env if env is not None else os.environ
    ensure_lean_on_path(target)
    configure_portable_locale(target)
    check = select_native_compiler(target, cwd=cwd)
    if check.lean_cc is None:
        target.pop("LEAN_CC", None)
    else:
        target["LEAN_CC"] = check.lean_cc
    return check


def default_lean_memory_limit_gb() -> int:
    """RepoProver's RLIMIT_AS pre-exec limit is unsupported on macOS."""
    return 0 if sys.platform == "darwin" else 24
