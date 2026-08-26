import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from proof_assistant import environment


def test_compiler_smoke_compiles_program_using_standard_header(monkeypatch):
    compiled_sources: list[str] = []

    def fake_run(command, **_kwargs):
        if command[0] == "/toolchain/cc":
            compiled_sources.append(Path(command[1]).read_text(encoding="utf-8"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(environment.subprocess, "run", fake_run)

    assert environment._compiler_smoke("/toolchain/cc") is None
    assert len(compiled_sources) == 1
    assert "#include <stddef.h>" in compiled_sources[0]


def test_configure_portable_locale_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    env = {"LANG": "C.UTF-8"}
    assert environment.configure_portable_locale(env) is True
    assert env["LANG"] == "C"
    assert env["LC_ALL"] == "C"


def test_select_compiler_falls_back_when_lean_clang_is_broken(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(environment, "ensure_lean_on_path", lambda env: False)
    monkeypatch.setattr(
        environment,
        "_lean_bundled_compiler",
        lambda env, **_kwargs: "/lean/bin/clang",
    )
    monkeypatch.setattr(
        environment,
        "_compiler_smoke",
        lambda executable, **_kwargs: (
            "dyld symbol missing"
            if executable == "/lean/bin/leanc"
            and not (_kwargs.get("env") or {}).get("LEAN_CC")
            else None
        ),
    )

    result = environment.select_native_compiler({"PATH": "/usr/bin"})
    assert result.executable == "/usr/bin/clang"
    assert result.fallback_used is True


def test_select_compiler_uses_configured_fallback_when_lean_clang_lacks_headers(
    monkeypatch,
):
    monkeypatch.setattr(environment, "ensure_lean_on_path", lambda env: False)
    monkeypatch.setattr(
        environment,
        "_lean_bundled_compiler",
        lambda env, **_kwargs: "/lean/bin/clang",
    )
    monkeypatch.setattr(
        environment,
        "_compiler_smoke",
        lambda executable, **_kwargs: (
            "stddef.h not found"
            if executable == "/lean/bin/leanc"
            and not (_kwargs.get("env") or {}).get("LEAN_CC")
            else None
        ),
    )

    result = environment.select_native_compiler(
        {"PATH": "/portable/bin", "CC": "/toolchain/cc"}
    )

    assert result.executable == "/toolchain/cc"
    assert result.fallback_used is True


def test_explicit_broken_lean_cc_fails_closed(monkeypatch):
    monkeypatch.setattr(environment, "ensure_lean_on_path", lambda env: False)
    monkeypatch.setattr(
        environment, "_compiler_smoke", lambda executable, **_kwargs: "broken"
    )
    with pytest.raises(environment.EnvironmentCheckError, match="compile/run"):
        environment.select_native_compiler({"LEAN_CC": "/bad/clang"})


def test_bundled_compiler_uses_leanc_without_lean_cc(monkeypatch):
    seen: list[tuple[str, str | None, bool, bool]] = []
    monkeypatch.setattr(environment, "ensure_lean_on_path", lambda env: False)
    monkeypatch.setattr(
        environment,
        "_lean_bundled_compiler",
        lambda env, **_kwargs: "/lean/bin/clang",
    )

    def fake_smoke(executable, **kwargs):
        seen.append(
            (
                executable,
                (kwargs.get("env") or {}).get("LEAN_CC"),
                kwargs.get("lean_header", False),
                kwargs.get("compile_only", False),
            )
        )
        return None

    monkeypatch.setattr(environment, "_compiler_smoke", fake_smoke)
    runtime = {"PATH": "/portable/bin"}

    result = environment.configure_lean_runtime(runtime)

    assert result.executable == "/lean/bin/clang"
    assert result.lean_compiler is True
    assert result.lean_cc is None
    assert "LEAN_CC" not in runtime
    assert seen == [
        ("/lean/bin/leanc", None, True, True),
        ("/lean/bin/clang", None, False, False),
    ]


def test_explicit_bundled_clang_is_rejected(monkeypatch):
    monkeypatch.setattr(environment, "ensure_lean_on_path", lambda env: False)
    monkeypatch.setattr(
        environment,
        "_lean_bundled_compiler",
        lambda env, **_kwargs: "/lean/bin/clang",
    )

    with pytest.raises(environment.EnvironmentCheckError, match="unset LEAN_CC"):
        environment.select_native_compiler(
            {"PATH": "/portable/bin", "LEAN_CC": "/lean/bin/clang"}
        )


def test_bundled_compiler_discovery_uses_project_directory(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    prefix = tmp_path / "toolchain"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "include" / "lean").mkdir(parents=True)
    for path in (
        prefix / "bin" / "clang",
        prefix / "bin" / "leanc",
        prefix / "include" / "lean" / "lean.h",
    ):
        path.touch()
    seen_cwd: list[Path | None] = []

    monkeypatch.setattr(environment.shutil, "which", lambda *_args, **_kwargs: "lean")

    def fake_run(_command, **kwargs):
        seen_cwd.append(kwargs.get("cwd"))
        return SimpleNamespace(returncode=0, stdout=str(prefix), stderr="")

    monkeypatch.setattr(environment.subprocess, "run", fake_run)

    compiler = environment._lean_bundled_compiler({"PATH": "/bin"}, cwd=project)

    assert compiler == str(prefix / "bin" / "clang")
    assert seen_cwd == [project]


def test_macos_disables_repoprover_address_space_limit(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert environment.default_lean_memory_limit_gb() == 0
