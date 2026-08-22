from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-dev.sh"


def test_installer_always_checks_compiler_before_tests():
    lines = [line.strip() for line in INSTALLER.read_text().splitlines()]
    install = 'uv pip install --python "${venv_path}/bin/python" -e "${project_root}[dev]"'
    compiler = '"${venv_path}/bin/repoprover-codex" compiler-check'
    cache_init = '"${venv_path}/bin/repoprover-codex" cache init'
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


def test_installer_rejects_dropbox_cache_before_creation(tmp_path):
    forbidden = tmp_path / "Dropbox" / "lean-cache"
    env = os.environ.copy()
    env["REPOPROVER_CODEX_CACHE_HOME"] = str(forbidden)
    env["REPOPROVER_CODEX_VENV"] = str(
        Path.home() / ".venvs" / "repoprover-codex"
    )
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
    env["REPOPROVER_CODEX_CACHE_HOME"] = str(forbidden)
    env["REPOPROVER_CODEX_VENV"] = str(venv)
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
