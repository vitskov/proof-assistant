import sys

import pytest

from repoprover_codex import environment


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
        environment, "_lean_bundled_compiler", lambda env: "/lean/bin/clang"
    )
    monkeypatch.setattr(
        environment,
        "_compiler_smoke",
        lambda executable, timeout=30.0: (
            "dyld symbol missing" if executable == "/lean/bin/clang" else None
        ),
    )

    result = environment.select_native_compiler({"PATH": "/usr/bin"})
    assert result.executable == "/usr/bin/clang"
    assert result.fallback_used is True


def test_explicit_broken_lean_cc_fails_closed(monkeypatch):
    monkeypatch.setattr(environment, "ensure_lean_on_path", lambda env: False)
    monkeypatch.setattr(
        environment, "_compiler_smoke", lambda executable, timeout=30.0: "broken"
    )
    with pytest.raises(environment.EnvironmentCheckError, match="compile/run"):
        environment.select_native_compiler({"LEAN_CC": "/bad/clang"})


def test_macos_disables_repoprover_address_space_limit(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert environment.default_lean_memory_limit_gb() == 0

