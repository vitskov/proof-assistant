from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-dev.sh"


def test_installer_always_checks_compiler_before_tests():
    lines = [line.strip() for line in INSTALLER.read_text().splitlines()]
    install = (
        'uv pip install --python "${venv_path}/bin/python" -e "${project_root}[dev]"'
    )
    compiler = '"${venv_path}/bin/proof-assistant" compiler-check'
    cache_init = '"${venv_path}/bin/proof-assistant" cache init'
    tests = '"${venv_path}/bin/python" -m pytest -q "${project_root}/tests"'

    assert lines.count(compiler) == 1
    assert lines.count(cache_init) == 1
    assert (
        lines.index(install)
        < lines.index(compiler)
        < lines.index(cache_init)
        < lines.index(tests)
    )


def test_installer_rejects_dropbox_environment_before_creation(tmp_path):
    forbidden = tmp_path / "Dropbox" / "forbidden-venv"
    env = os.environ.copy()
    env["PROOF_ASSISTANT_VENV"] = str(forbidden)
    result = subprocess.run(
        [str(INSTALLER)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 2
    assert "must not reside in Dropbox" in result.stderr
    assert not forbidden.exists()


def test_installer_rejects_dropbox_cache_before_creation(tmp_path):
    forbidden = tmp_path / "Dropbox" / "lean-cache"
    env = os.environ.copy()
    env["PROOF_ASSISTANT_CACHE_HOME"] = str(forbidden)
    env["PROOF_ASSISTANT_VENV"] = str(Path.home() / ".venvs" / "proof-assistant")
    result = subprocess.run(
        [str(INSTALLER)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 2
    assert "caches must not reside in Dropbox" in result.stderr
    assert not forbidden.exists()


def test_installer_rejects_cache_outside_home_before_creation(tmp_path):
    venv = tmp_path / "external-venv"
    forbidden = tmp_path / "forbidden-cache"
    env = os.environ.copy()
    env["PROOF_ASSISTANT_CACHE_HOME"] = str(forbidden)
    env["PROOF_ASSISTANT_VENV"] = str(venv)
    result = subprocess.run(
        [str(INSTALLER)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 2
    assert "caches must reside inside the user home" in result.stderr
    assert not venv.exists()
    assert not forbidden.exists()


def test_installer_keeps_existing_cache_path_and_supports_legacy_overrides():
    text = INSTALLER.read_text()

    assert "${HOME}/.cache/repoprover-codex" in text
    assert "${HOME}/.venvs/proof-assistant" in text
    assert "PROOF_ASSISTANT_VENV" in text
    assert "PROOF_ASSISTANT_CACHE_HOME" in text
    assert "PROOF_ASSISTANT_PYTHON" in text
    assert "REPOPROVER_CODEX_VENV" in text
    assert "REPOPROVER_CODEX_CACHE_HOME" in text
    assert "REPOPROVER_CODEX_PYTHON" in text


def test_installer_honors_legacy_environment_override(tmp_path):
    forbidden = tmp_path / "Dropbox" / "legacy-venv"
    env = os.environ.copy()
    env.pop("PROOF_ASSISTANT_VENV", None)
    env["REPOPROVER_CODEX_VENV"] = str(forbidden)
    result = subprocess.run(
        [str(INSTALLER)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 2
    assert "must not reside in Dropbox" in result.stderr
    assert not forbidden.exists()
