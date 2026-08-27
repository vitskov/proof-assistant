from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-dev.sh"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _relax_hardware_gate(env: dict[str, str]) -> dict[str, str]:
    """Disable the CPU/RAM floor for tests unrelated to the hardware gate."""
    env["PROOF_ASSISTANT_MIN_CPU_CORES"] = "1"
    env["PROOF_ASSISTANT_MIN_MEMORY_GIB"] = "1"
    return env


def _fake_command_path(root: Path) -> Path:
    commands = root / "commands"
    commands.mkdir()
    required = (
        "awk",
        "bash",
        "chmod",
        "cp",
        "dirname",
        "env",
        "getconf",
        "grep",
        "mkdir",
        "mv",
        "sh",
        "uname",
    )
    # sysctl is only exercised on the macOS branch of the hardware gate; it is
    # not guaranteed to exist on every Linux test host.
    optional = ("sysctl", "lscpu")
    for name in required:
        target = shutil.which(name)
        assert target is not None
        (commands / name).symlink_to(target)
    for name in optional:
        target = shutil.which(name)
        if target is not None:
            (commands / name).symlink_to(target)
    return commands


def _fake_venv(home: Path, log: Path) -> Path:
    venv = home / ".venvs" / "proof-assistant"
    binary = venv / "bin"
    binary.mkdir(parents=True)
    _write_executable(
        binary / "python",
        '#!/bin/sh\nprintf \'python:%s\\n\' "$*" >> "$FAKE_LOG"\n',
    )
    _write_executable(
        binary / "proof-assistant",
        '#!/bin/sh\nprintf \'proof:%s|PATH=%s\\n\' "$*" "$PATH" >> "$FAKE_LOG"\n',
    )
    log.touch()
    return venv


def _fake_uv(path: Path, *, tag: str, working: bool = True) -> None:
    version_status = 0 if working else 19
    _write_executable(
        path,
        "#!/bin/sh\n"
        f'printf \'{tag}:%s\\n\' "$*" >> "$FAKE_LOG"\n'
        'if [ "${1:-}" = "--version" ]; then\n'
        f"  exit {version_status}\n"
        "fi\n"
        'if [ "${1:-}" = "venv" ]; then\n'
        '  mkdir -p "$4/bin"\n'
        '  cp "$FAKE_PYTHON_TEMPLATE" "$4/bin/python"\n'
        '  cp "$FAKE_PROOF_TEMPLATE" "$4/bin/proof-assistant"\n'
        '  chmod +x "$4/bin/python" "$4/bin/proof-assistant"\n'
        "fi\n"
        "exit 0\n",
    )


def _bootstrap_harness(
    tmp_path: Path,
    *,
    existing_uv: str = "missing",
    downloader: str = "curl",
    custom_install_dir: Path | None = None,
    installer_creates_uv: bool = True,
    installed_uv_working: bool = True,
    downloader_succeeds: bool = True,
    block_install_dir: bool = False,
    precreate_venv: bool = True,
    preexisting_local_uv: bool = False,
    shell: str = "/bin/bash",
    startup_files: dict[str, str] | None = None,
    startup_links: dict[str, str] | None = None,
    environment: dict[str, str] | None = None,
    runs: int = 1,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    for relative, content in (startup_files or {}).items():
        startup = home / relative
        startup.parent.mkdir(parents=True, exist_ok=True)
        startup.write_text(content, encoding="utf-8")
    for relative, target in (startup_links or {}).items():
        startup = home / relative
        startup.parent.mkdir(parents=True, exist_ok=True)
        startup.symlink_to(target)
    install_dir = custom_install_dir or home / ".local/bin"
    if block_install_dir:
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        install_dir.write_text("not a directory", encoding="utf-8")
    log = tmp_path / "calls.log"
    python_template = tmp_path / "fake-python"
    proof_template = tmp_path / "fake-proof-assistant"
    _write_executable(
        python_template,
        '#!/bin/sh\nprintf \'python:%s\\n\' "$*" >> "$FAKE_LOG"\n',
    )
    _write_executable(
        proof_template,
        '#!/bin/sh\nprintf \'proof:%s|PATH=%s\\n\' "$*" "$PATH" >> "$FAKE_LOG"\n',
    )
    if precreate_venv:
        _fake_venv(home, log)
    else:
        log.touch()
    commands = _fake_command_path(tmp_path)
    if existing_uv != "missing":
        if existing_uv == "install_dir":
            install_dir.mkdir(parents=True)
            _fake_uv(install_dir / "uv", tag="existing-local")
        else:
            _fake_uv(
                commands / "uv",
                tag="existing",
                working=existing_uv == "working",
            )
    if preexisting_local_uv:
        install_dir.mkdir(parents=True, exist_ok=True)
        _fake_uv(install_dir / "uv", tag="existing-local")

    installed_template = tmp_path / "installed-uv"
    _fake_uv(installed_template, tag="installed", working=installed_uv_working)
    fake_installer = tmp_path / "uv-install.sh"
    create = (
        'mkdir -p "$UV_INSTALL_DIR"\n'
        'cp "$FAKE_UV_TEMPLATE" "$UV_INSTALL_DIR/uv"\n'
        'chmod +x "$UV_INSTALL_DIR/uv"\n'
        if installer_creates_uv
        else ":\n"
    )
    _write_executable(
        fake_installer,
        "#!/bin/sh\n"
        "printf 'installer:dir=%s:no_modify=%s\\n' "
        '"$UV_INSTALL_DIR" "$UV_NO_MODIFY_PATH" >> "$FAKE_LOG"\n' + create,
    )

    def add_downloader(name: str) -> None:
        action = '/bin/cat "$FAKE_INSTALLER"\n' if downloader_succeeds else "exit 55\n"
        _write_executable(
            commands / name,
            f'#!/bin/sh\nprintf \'{name}:%s\\n\' "$*" >> "$FAKE_LOG"\n' + action,
        )

    if downloader == "curl":
        add_downloader("curl")
        add_downloader("wget")
    elif downloader == "wget":
        add_downloader("wget")
    elif downloader != "none":
        raise AssertionError(f"Unknown fake downloader: {downloader}")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "SHELL": shell,
            "PATH": str(commands),
            "FAKE_INSTALLER": str(fake_installer),
            "FAKE_LOG": str(log),
            "FAKE_PROOF_TEMPLATE": str(proof_template),
            "FAKE_PYTHON_TEMPLATE": str(python_template),
            "FAKE_UV_TEMPLATE": str(installed_template),
            "PROOF_ASSISTANT_VENV": str(home / ".venvs/proof-assistant"),
            "PROOF_ASSISTANT_CACHE_HOME": str(home / ".cache/repoprover-codex"),
            "PROOF_ASSISTANT_UV_INSTALL_DIR": str(install_dir),
            # Bootstrap tests exercise uv discovery, not the hardware gate;
            # relax the floor so they pass regardless of the test host's specs.
            "PROOF_ASSISTANT_MIN_CPU_CORES": "1",
            "PROOF_ASSISTANT_MIN_MEMORY_GIB": "1",
        }
    )
    env.update(environment or {})
    result: subprocess.CompletedProcess[str] | None = None
    for _ in range(runs):
        result = subprocess.run(
            [str(INSTALLER)], text=True, capture_output=True, check=False, env=env
        )
        if result.returncode != 0:
            break
    assert result is not None
    return result, log, home, install_dir


def test_installer_always_checks_compiler_before_tests():
    lines = [line.strip() for line in INSTALLER.read_text().splitlines()]
    install = '"${uv_bin}" pip install --python "${venv_path}/bin/python" -e "${project_root}[dev]"'
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
    env = _relax_hardware_gate(os.environ.copy())
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
    env = _relax_hardware_gate(os.environ.copy())
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
    env = _relax_hardware_gate(os.environ.copy())
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


def _stub_path_env(tmp_path: Path, stubs: dict[str, str]) -> dict[str, str]:
    """Prepend fake OS-query binaries to PATH; everything else stays real."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    for name, script in stubs.items():
        _write_executable(stub_dir / name, script)
    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    return env


_UNAME_STUB = (
    "#!/bin/sh\n"
    'if [ "$1" = "-s" ]; then echo "{os_name}"; exit 0; fi\n'
    'if [ "$1" = "-r" ]; then echo "{os_release}"; exit 0; fi\n'
    "exit 1\n"
)

_SYSCTL_STUB = (
    "#!/bin/sh\n"
    'case "$2" in\n'
    '  hw.physicalcpu|hw.ncpu) echo "{cores}" ;;\n'
    '  hw.memsize) echo "{mem_bytes}" ;;\n'
    "  *) exit 1 ;;\n"
    "esac\n"
)

_GETCONF_STUB = (
    "#!/bin/sh\n"
    'if [ "$1" = "GNU_LIBC_VERSION" ]; then echo "glibc {glibc}"; exit 0; fi\n'
    "exit 1\n"
)

_GETCONF_MISSING_STUB = "#!/bin/sh\nexit 1\n"


def test_system_check_rejects_unsupported_operating_system(tmp_path):
    env = _stub_path_env(
        tmp_path,
        {"uname": _UNAME_STUB.format(os_name="SunOS", os_release="5.11")},
    )
    result = subprocess.run(
        [str(INSTALLER)], text=True, capture_output=True, check=False, env=env
    )
    assert result.returncode == 2
    assert "Unsupported operating system: SunOS" in result.stderr


def test_system_check_rejects_macos_older_than_monterey(tmp_path):
    env = _stub_path_env(
        tmp_path,
        {"uname": _UNAME_STUB.format(os_name="Darwin", os_release="20.6.0")},
    )
    result = subprocess.run(
        [str(INSTALLER)], text=True, capture_output=True, check=False, env=env
    )
    assert result.returncode == 2
    assert "macOS 12 Monterey (Darwin 21) or newer is required" in result.stderr
    assert "detected Darwin 20.6.0" in result.stderr


def test_system_check_rejects_linux_with_old_glibc(tmp_path):
    env = _stub_path_env(
        tmp_path,
        {
            "uname": _UNAME_STUB.format(os_name="Linux", os_release="5.4.0"),
            "getconf": _GETCONF_STUB.format(glibc="2.27"),
        },
    )
    result = subprocess.run(
        [str(INSTALLER)], text=True, capture_output=True, check=False, env=env
    )
    assert result.returncode == 2
    assert "glibc 2.31" in result.stderr
    assert "detected glibc 2.27" in result.stderr


def test_system_check_rejects_linux_when_glibc_cannot_be_detected(tmp_path):
    env = _stub_path_env(
        tmp_path,
        {
            "uname": _UNAME_STUB.format(os_name="Linux", os_release="5.4.0"),
            "getconf": _GETCONF_MISSING_STUB,
        },
    )
    result = subprocess.run(
        [str(INSTALLER)], text=True, capture_output=True, check=False, env=env
    )
    assert result.returncode == 2
    assert "Unable to determine glibc version" in result.stderr


def test_system_check_rejects_too_few_cpu_cores(tmp_path):
    env = _stub_path_env(
        tmp_path,
        {
            "uname": _UNAME_STUB.format(os_name="Darwin", os_release="21.6.0"),
            "sysctl": _SYSCTL_STUB.format(cores=2, mem_bytes=17179869184),
        },
    )
    result = subprocess.run(
        [str(INSTALLER)], text=True, capture_output=True, check=False, env=env
    )
    assert result.returncode == 2
    assert "at least 4 CPU cores are required; detected 2" in result.stderr


def test_system_check_rejects_too_little_memory(tmp_path):
    env = _stub_path_env(
        tmp_path,
        {
            "uname": _UNAME_STUB.format(os_name="Darwin", os_release="21.6.0"),
            "sysctl": _SYSCTL_STUB.format(cores=8, mem_bytes=4 * 1024**3),
        },
    )
    result = subprocess.run(
        [str(INSTALLER)], text=True, capture_output=True, check=False, env=env
    )
    assert result.returncode == 2
    assert "at least 16 GiB of RAM is required; detected 4 GiB" in result.stderr


def test_system_check_honors_custom_minimums(tmp_path):
    env = _stub_path_env(
        tmp_path,
        {
            "uname": _UNAME_STUB.format(os_name="Darwin", os_release="21.6.0"),
            "sysctl": _SYSCTL_STUB.format(cores=2, mem_bytes=4 * 1024**3),
        },
    )
    env["PROOF_ASSISTANT_MIN_CPU_CORES"] = "1"
    env["PROOF_ASSISTANT_MIN_MEMORY_GIB"] = "1"
    env["PROOF_ASSISTANT_VENV"] = str(tmp_path / "Dropbox" / "venv")
    result = subprocess.run(
        [str(INSTALLER)], text=True, capture_output=True, check=False, env=env
    )
    # Falls through the (relaxed) hardware gate straight into the next check.
    assert result.returncode == 2
    assert "must not reside in Dropbox" in result.stderr


def test_system_check_passes_and_recommends_more_hardware_below_recommended_tier(
    tmp_path,
):
    env = _stub_path_env(
        tmp_path,
        {
            "uname": _UNAME_STUB.format(os_name="Darwin", os_release="21.6.0"),
            "sysctl": _SYSCTL_STUB.format(cores=4, mem_bytes=16 * 1024**3),
        },
    )
    env["PROOF_ASSISTANT_VENV"] = str(tmp_path / "Dropbox" / "venv")
    result = subprocess.run(
        [str(INSTALLER)], text=True, capture_output=True, check=False, env=env
    )
    assert result.returncode == 2
    assert "must not reside in Dropbox" in result.stderr
    assert "System check: Darwin 21.6.0, 4 CPU cores, 16 GiB RAM" in result.stdout
    assert "8+ CPU cores and 32+ GiB RAM are recommended" in result.stderr


def test_installer_keeps_existing_cache_path_and_supports_legacy_overrides():
    text = INSTALLER.read_text()

    assert "${HOME}/.cache/repoprover-codex" in text
    assert "${HOME}/.venvs/proof-assistant" in text
    assert "PROOF_ASSISTANT_VENV" in text
    assert "PROOF_ASSISTANT_CACHE_HOME" in text
    assert "PROOF_ASSISTANT_PYTHON" in text
    assert "PROOF_ASSISTANT_UV_INSTALL_DIR" in text
    assert "REPOPROVER_CODEX_VENV" in text
    assert "REPOPROVER_CODEX_CACHE_HOME" in text
    assert "REPOPROVER_CODEX_PYTHON" in text


def test_uv_bootstrap_never_uses_privilege_or_system_package_managers():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "UV_NO_MODIFY_PATH=1" in text
    assert "https://astral.sh/uv/install.sh" in text
    assert 'uv_bin="$(command -v uv)"' in text
    for forbidden in ("sudo ", "brew ", "apt ", "apt-get ", "cargo install"):
        assert forbidden not in text


def test_installer_honors_legacy_environment_override(tmp_path):
    forbidden = tmp_path / "Dropbox" / "legacy-venv"
    env = _relax_hardware_gate(os.environ.copy())
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


def test_installer_reuses_a_working_uv_without_downloading(tmp_path):
    result, log, _home, _install_dir = _bootstrap_harness(
        tmp_path, existing_uv="working", downloader="curl"
    )
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "Using uv:" in result.stdout
    assert "existing:--version" in calls
    assert "existing:pip install --python" in calls
    assert "curl:" not in calls
    assert "wget:" not in calls
    assert "installed:" not in calls


def test_installer_reuses_prior_local_bootstrap_even_when_not_on_shell_path(tmp_path):
    result, log, _home, install_dir = _bootstrap_harness(
        tmp_path, existing_uv="install_dir", downloader="none"
    )
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert f"Using uv: {install_dir / 'uv'}" in result.stdout
    assert "existing-local:--version" in calls
    assert "existing-local:pip install --python" in calls
    assert f"PATH={install_dir}:" in calls


def test_broken_path_uv_does_not_shadow_a_working_local_bootstrap(tmp_path):
    result, log, _home, install_dir = _bootstrap_harness(
        tmp_path,
        existing_uv="broken",
        downloader="none",
        preexisting_local_uv=True,
    )
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert f"Using uv: {install_dir / 'uv'}" in result.stdout
    assert "existing:--version" in calls
    assert "existing-local:--version" in calls
    assert "existing-local:pip install --python" in calls


def test_missing_uv_bootstraps_with_curl_and_uses_exact_installed_binary(tmp_path):
    result, log, home, install_dir = _bootstrap_harness(tmp_path, downloader="curl")
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert install_dir == home / ".local/bin"
    assert (install_dir / "uv").is_file()
    assert "curl:-LsSf https://astral.sh/uv/install.sh" in calls
    assert "wget:" not in calls
    assert f"installer:dir={install_dir}:no_modify=1" in calls
    assert "installed:--version" in calls
    assert "installed:pip install --python" in calls
    assert f"PATH={install_dir}:" in calls
    startup_files = [
        home / ".zprofile",
        home / ".zshrc",
        home / ".bash_profile",
        home / ".bashrc",
        home / ".profile",
        home / ".config/fish/config.fish",
    ]
    configured = [
        path.read_text(encoding="utf-8") for path in startup_files if path.exists()
    ]
    assert any("proof-assistant" in text for text in configured)


def test_bootstrapped_uv_creates_python_environment_before_install_and_checks(
    tmp_path,
):
    result, log, home, _install_dir = _bootstrap_harness(
        tmp_path, downloader="curl", precreate_venv=False
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    assert result.returncode == 0, result.stderr
    venv_call = next(index for index, line in enumerate(calls) if ":venv " in line)
    pip_call = next(index for index, line in enumerate(calls) if ":pip install" in line)
    compiler = next(
        index
        for index, line in enumerate(calls)
        if line.startswith("proof:compiler-check|PATH=")
    )
    assert venv_call < pip_call < compiler
    assert (home / ".venvs/proof-assistant/bin/python").is_file()


def test_broken_uv_is_replaced_in_custom_install_dir(tmp_path):
    install_dir = tmp_path / "private-bin"
    result, log, _home, resolved = _bootstrap_harness(
        tmp_path,
        existing_uv="broken",
        downloader="curl",
        custom_install_dir=install_dir,
    )
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert resolved == install_dir
    assert "existing:--version" in calls
    assert "existing:pip" not in calls
    assert f"installer:dir={install_dir}:no_modify=1" in calls
    assert "installed:pip install --python" in calls


def test_wget_is_used_only_when_curl_is_unavailable(tmp_path):
    result, log, _home, _install_dir = _bootstrap_harness(tmp_path, downloader="wget")
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "curl:" not in calls
    assert "wget:-qO- https://astral.sh/uv/install.sh" in calls


def test_no_downloader_exits_two_without_creating_uv(tmp_path):
    result, _log, _home, install_dir = _bootstrap_harness(tmp_path, downloader="none")
    assert result.returncode == 2
    assert "neither curl nor wget" in result.stderr
    assert not (install_dir / "uv").exists()


def test_bootstrap_must_produce_a_working_uv_before_environment_changes(tmp_path):
    result, log, home, install_dir = _bootstrap_harness(
        tmp_path, downloader="curl", installer_creates_uv=False
    )
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 2
    assert "did not produce a working uv" in result.stderr
    assert "proof:" not in calls
    assert "python:" not in calls
    assert not (install_dir / "uv").exists()
    assert (home / ".venvs/proof-assistant/bin/python").is_file()


def test_bootstrap_rejects_an_installed_uv_that_fails_verification(tmp_path):
    result, log, _home, install_dir = _bootstrap_harness(
        tmp_path,
        downloader="curl",
        installed_uv_working=False,
    )
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 2
    assert "did not produce a working uv" in result.stderr
    assert "installed:--version" in calls
    assert "installed:pip" not in calls
    assert (install_dir / "uv").is_file()


def test_downloader_or_installer_failure_has_clear_error_and_exit_two(tmp_path):
    result, log, _home, install_dir = _bootstrap_harness(
        tmp_path, downloader="curl", downloader_succeeds=False
    )
    assert result.returncode == 2
    assert "Failed to download or run Astral's uv installer with curl" in result.stderr
    assert "curl:" in log.read_text(encoding="utf-8")
    assert not (install_dir / "uv").exists()


def test_uncreatable_uv_install_directory_exits_two_before_download(tmp_path):
    install_dir = tmp_path / "blocked"
    result, log, _home, _resolved = _bootstrap_harness(
        tmp_path,
        downloader="curl",
        custom_install_dir=install_dir,
        block_install_dir=True,
    )
    assert result.returncode == 2
    assert "Cannot create the uv install directory" in result.stderr
    assert "curl:" not in log.read_text(encoding="utf-8")


def test_installer_configures_the_detected_shell_startup_path(tmp_path):
    result, _log, home, _install_dir = _bootstrap_harness(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (home / ".bash_profile").exists()
    assert not (home / ".bash_login").exists()
    assert (home / ".profile").is_file()
    assert (home / ".bashrc").is_file()
    startup_files = [
        home / ".zprofile",
        home / ".zshrc",
        home / ".bash_profile",
        home / ".bashrc",
        home / ".profile",
        home / ".config/fish/config.fish",
    ]
    configured = [
        path.read_text(encoding="utf-8") for path in startup_files if path.exists()
    ]
    assert any(".venvs/proof-assistant/bin" in text for text in configured)


def test_installer_does_not_duplicate_shell_startup_path(tmp_path):
    result, _log, home, _install_dir = _bootstrap_harness(tmp_path, runs=2)
    assert result.returncode == 0, result.stderr
    startup_files = [
        home / ".zprofile",
        home / ".zshrc",
        home / ".bash_profile",
        home / ".bashrc",
        home / ".profile",
        home / ".config/fish/config.fish",
    ]
    configured = [
        path.read_text(encoding="utf-8") for path in startup_files if path.exists()
    ]
    assert all(
        text.count("Added by Proof Assistant installer") == 1 for text in configured
    )


def test_installer_preserves_existing_bash_profile_and_bashrc(tmp_path):
    bash_profile = 'export EXISTING_LOGIN_SETTING="keep me"\n. "$HOME/.bashrc"'
    bashrc = 'alias existing_alias="printf preserved"\n'
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        startup_files={
            ".bash_profile": bash_profile,
            ".bashrc": bashrc,
        },
        runs=2,
    )

    assert result.returncode == 0, result.stderr
    installed_profile = (home / ".bash_profile").read_text(encoding="utf-8")
    installed_bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert installed_profile.startswith(bash_profile + "\n")
    assert installed_bashrc.startswith(bashrc)
    assert installed_profile.count("EXISTING_LOGIN_SETTING") == 1
    assert installed_profile.count('. "$HOME/.bashrc"') == 1
    assert installed_bashrc.count("existing_alias") == 1
    assert installed_profile.count("Added by Proof Assistant installer") == 1
    assert installed_bashrc.count("Added by Proof Assistant installer") == 1


def test_installer_recognizes_previous_guard_without_appending(tmp_path):
    path_dir = tmp_path / "home" / ".venvs" / "proof-assistant" / "bin"
    previous_guard = (
        "# Added by Proof Assistant installer\n"
        f'case ":$PATH:" in *":{path_dir}:"*) ;; *) '
        f'export PATH={path_dir}:"$PATH";; esac\n'
    )
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        startup_files={
            ".bash_profile": "export LOGIN_SENTINEL=1\n" + previous_guard,
            ".bashrc": "export BASHRC_SENTINEL=1\n" + previous_guard,
        },
        runs=2,
    )

    assert result.returncode == 0, result.stderr
    assert (home / ".bash_profile").read_text(encoding="utf-8") == (
        "export LOGIN_SENTINEL=1\n" + previous_guard
    )
    assert (home / ".bashrc").read_text(encoding="utf-8") == (
        "export BASHRC_SENTINEL=1\n" + previous_guard
    )


def test_installer_uses_profile_without_creating_higher_priority_bash_file(
    tmp_path,
):
    profile = 'export PROFILE_ONLY_SETTING="preserved"\n. "$HOME/.bashrc"\n'
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        startup_files={".profile": profile},
    )

    assert result.returncode == 0, result.stderr
    assert not (home / ".bash_profile").exists()
    assert not (home / ".bash_login").exists()
    installed = (home / ".profile").read_text(encoding="utf-8")
    assert installed.startswith(profile)
    assert installed.count("PROFILE_ONLY_SETTING") == 1
    assert installed.count("Added by Proof Assistant installer") == 1


def test_installer_respects_existing_bash_login_precedence(tmp_path):
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        startup_files={
            ".bash_login": "export LOGIN_SENTINEL=1\n",
            ".profile": "export PROFILE_SENTINEL=1\n",
        },
    )

    assert result.returncode == 0, result.stderr
    assert not (home / ".bash_profile").exists()
    assert "Added by Proof Assistant installer" in (
        home / ".bash_login"
    ).read_text(encoding="utf-8")
    assert (home / ".profile").read_text(encoding="utf-8") == (
        "export PROFILE_SENTINEL=1\n"
    )


def test_installer_preserves_existing_zsh_profiles(tmp_path):
    zprofile = "export ZPROFILE_SENTINEL=1"
    zshrc = "export ZSHRC_SENTINEL=1\n"
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        shell="/bin/zsh",
        startup_files={".zprofile": zprofile, ".zshrc": zshrc},
        runs=2,
    )

    assert result.returncode == 0, result.stderr
    installed_zprofile = (home / ".zprofile").read_text(encoding="utf-8")
    installed_zshrc = (home / ".zshrc").read_text(encoding="utf-8")
    assert installed_zprofile.startswith(zprofile + "\n")
    assert installed_zshrc.startswith(zshrc)
    assert installed_zprofile.count("ZPROFILE_SENTINEL") == 1
    assert installed_zshrc.count("ZSHRC_SENTINEL") == 1
    assert installed_zprofile.count("Added by Proof Assistant installer") == 1
    assert installed_zshrc.count("Added by Proof Assistant installer") == 1


def test_installer_migrates_only_legacy_owned_bash_profile(tmp_path):
    path_dir = tmp_path / "home" / ".venvs" / "proof-assistant" / "bin"
    legacy = (
        "\n# Added by Proof Assistant installer\n"
        f'case ":$PATH:" in *":{path_dir}:"*) ;; *) '
        f'export PATH={path_dir}:"$PATH";; esac\n'
    )
    profile = 'export PROFILE_SENTINEL=1\n. "$HOME/.bashrc"\n'
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        startup_files={".bash_profile": legacy, ".profile": profile},
    )

    assert result.returncode == 0, result.stderr
    assert not (home / ".bash_profile").exists()
    assert (home / ".bash_profile.proof-assistant-backup").read_text(
        encoding="utf-8"
    ) == legacy
    installed = (home / ".profile").read_text(encoding="utf-8")
    assert installed.startswith(profile)
    assert installed.count("Added by Proof Assistant installer") == 1


def test_installer_transfers_other_managed_path_before_migration(tmp_path):
    other_path = tmp_path / "home" / ".local" / "bin"
    installer_path = tmp_path / "home" / ".venvs" / "proof-assistant" / "bin"
    legacy = (
        "# Added by Proof Assistant\n"
        f'export PATH={other_path}:"$PATH"\n'
        "\n# Added by Proof Assistant installer\n"
        f'case ":$PATH:" in *":{installer_path}:"*) ;; *) '
        f'export PATH={installer_path}:"$PATH";; esac\n'
    )
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        startup_files={".bash_profile": legacy},
    )

    assert result.returncode == 0, result.stderr
    assert not (home / ".bash_profile").exists()
    assert (home / ".bash_profile.proof-assistant-backup").read_text(
        encoding="utf-8"
    ) == legacy
    installed = (home / ".profile").read_text(encoding="utf-8")
    assert str(other_path) in installed
    assert str(installer_path) in installed


def test_installer_refuses_readable_nonregular_bash_candidate(tmp_path):
    result, _log, _home, _install_dir = _bootstrap_harness(
        tmp_path,
        startup_links={".bash_profile": "."},
    )

    assert result.returncode == 2
    assert "Refusing readable non-regular Bash startup file" in result.stderr


def test_installer_skips_broken_bash_login_candidate(tmp_path):
    profile = "export PROFILE_SENTINEL=1\n"
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        startup_files={".profile": profile},
        startup_links={".bash_profile": "missing-profile"},
    )

    assert result.returncode == 0, result.stderr
    assert (home / ".bash_profile").is_symlink()
    assert not (home / "missing-profile").exists()
    installed = (home / ".profile").read_text(encoding="utf-8")
    assert installed.startswith(profile)
    assert installed.count("Added by Proof Assistant installer") == 1


def test_installer_honors_zdotdir(tmp_path):
    zdotdir = tmp_path / "home" / "shell" / "zsh"
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        shell="/bin/zsh",
        environment={"ZDOTDIR": str(zdotdir)},
    )

    assert result.returncode == 0, result.stderr
    assert (zdotdir / ".zprofile").is_file()
    assert (zdotdir / ".zshrc").is_file()
    assert not (home / ".zprofile").exists()
    assert not (home / ".zshrc").exists()


def test_installer_honors_fish_xdg_config_home(tmp_path):
    config_home = tmp_path / "home" / "xdg"
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        shell="/usr/bin/fish",
        environment={"XDG_CONFIG_HOME": str(config_home)},
        runs=2,
    )

    assert result.returncode == 0, result.stderr
    fish_config = config_home / "fish" / "config.fish"
    installed = fish_config.read_text(encoding="utf-8")
    assert installed.count("Added by Proof Assistant installer") == 1
    assert "fish_add_path --path" in installed
    assert not (home / ".config" / "fish" / "config.fish").exists()
