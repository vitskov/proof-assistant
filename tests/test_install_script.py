from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
ONE_LINE_INSTALL = (
    'bash -c \'set -o pipefail; curl --proto "=https" --tlsv1.2 -fsSL '
    "https://raw.githubusercontent.com/vitskov/proof-assistant/main/install.sh | bash'"
)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _clean_install_environment() -> dict[str, str]:
    """Return the host environment without parent installer overrides."""
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("PROOF_ASSISTANT_") or key.startswith("REPOPROVER_CODEX_"):
            env.pop(key)
    return env


def _relax_hardware_gate(env: dict[str, str]) -> dict[str, str]:
    """Disable the CPU/RAM floor for tests unrelated to the hardware gate."""
    env["PROOF_ASSISTANT_MIN_CPU_CORES"] = "1"
    env["PROOF_ASSISTANT_MIN_MEMORY_GIB"] = "1"
    return env


def _fake_command_path(
    root: Path,
    *,
    available_editors: tuple[str, ...] = ("nano",),
) -> Path:
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
        "git",
        "id",
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
    for editor in available_editors:
        _write_executable(commands / editor, "#!/bin/sh\nexit 0\n")
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


def _fake_repoprover_checkout(tmp_path: Path) -> tuple[Path, str]:
    checkout = tmp_path / "repoprover"
    checkout.mkdir()
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "repoprover"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Proof Assistant Tests",
            "-c",
            "user.email=tests@invalid.example",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return checkout, revision


def _editor_package(manager: str, editor: str) -> str:
    if editor == "nano":
        return "nano"
    if editor == "pico":
        return "alpine-pico" if manager == "apt-get" else "alpine"
    if editor == "micro":
        return "micro-editor" if manager == "zypper" else "micro"
    raise AssertionError(f"Unsupported editor: {editor}")


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
    managed_repoprover: bool = False,
    available_editors: tuple[str, ...] = ("nano",),
    editor_package_manager: str | None = None,
    editor_install_succeeds_for: str | None = "nano",
    editor_privilege: str = "allowed",
    simulated_os: str | None = None,
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
    install_dir = (
        custom_install_dir or home / ".local" / "share" / "proof-assistant" / "uv"
    )
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
    commands = _fake_command_path(
        tmp_path,
        available_editors=available_editors,
    )
    if simulated_os is not None:
        (commands / "uname").unlink()
        _write_executable(
            commands / "uname",
            _UNAME_STUB.format(
                os_name=simulated_os,
                os_release="24.6.0" if simulated_os == "Darwin" else "6.8.0",
            ),
        )
    if simulated_os == "Darwin":
        if (commands / "sysctl").exists() or (commands / "sysctl").is_symlink():
            (commands / "sysctl").unlink()
        _write_executable(
            commands / "sysctl",
            _SYSCTL_STUB.format(cores=8, mem_bytes=32 * 1024**3),
        )
    if editor_package_manager is not None:
        manager = editor_package_manager
        package_script = (
            "#!/bin/sh\n"
            f'printf \'package:{manager}:%s\\n\' "$*" >> "$FAKE_LOG"\n'
            'package=""\n'
            'for argument in "$@"; do package="$argument"; done\n'
            'if [ -n "${FAKE_EDITOR_SUCCESS_PACKAGE:-}" ] '
            '&& [ "$package" = "$FAKE_EDITOR_SUCCESS_PACKAGE" ]; then\n'
            "  printf '#!/bin/sh\\nexit 0\\n' "
            '> "$FAKE_COMMAND_DIR/$FAKE_EDITOR_SUCCESS_EDITOR"\n'
            '  chmod +x "$FAKE_COMMAND_DIR/$FAKE_EDITOR_SUCCESS_EDITOR"\n'
            "fi\n"
            "exit 0\n"
        )
        _write_executable(commands / manager, package_script)
        if manager != "brew" and editor_privilege != "missing":
            sudo_status = 0 if editor_privilege == "allowed" else 1
            _write_executable(
                commands / "sudo",
                "#!/bin/sh\n"
                'printf \'sudo:%s\\n\' "$*" >> "$FAKE_LOG"\n'
                'if [ "${1:-}" = "-n" ]; then shift; fi\n'
                f'if [ "${{1:-}}" = "true" ]; then exit {sudo_status}; fi\n'
                f'if [ "${{1:-}}" = "-v" ]; then exit {sudo_status}; fi\n'
                'exec "$@"\n',
            )
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
    sandbox_root = tmp_path / "installer-project"
    (sandbox_root / "scripts").mkdir(parents=True)
    (sandbox_root / "src" / "proof_assistant").mkdir(parents=True)
    shutil.copy2(INSTALLER, sandbox_root / "install.sh")
    shutil.copy2(ROOT / "lean-toolchain", sandbox_root / "lean-toolchain")
    (sandbox_root / "pyproject.toml").write_text(
        '[project]\nname = "proof-assistant"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    fake_bootstrap = sandbox_root / "scripts" / "bootstrap-uv.sh"
    create = (
        'mkdir -p "$1"\ncp "$FAKE_UV_TEMPLATE" "$1/uv"\nchmod +x "$1/uv"\n'
        if installer_creates_uv
        else ":\n"
    )
    _write_executable(
        fake_bootstrap,
        "#!/bin/sh\n"
        'printf \'bootstrap:%s\\n\' "$1" >> "$FAKE_LOG"\n'
        + ("exit 55\n" if not downloader_succeeds or downloader == "none" else create)
        + "printf '%s\\n' \"$1/uv\"\n",
    )
    if downloader not in {"curl", "wget", "none"}:
        raise AssertionError(f"Unknown bootstrap mode: {downloader}")

    env = _clean_install_environment()
    repoprover, repoprover_revision = _fake_repoprover_checkout(tmp_path)
    elan_home = home / ".elan"
    elan_home.mkdir()
    _write_executable(
        elan_home / "elan",
        "#!/bin/sh\n"
        'printf \'elan:%s|ELAN_HOME=%s\\n\' "$*" "$ELAN_HOME" >> "$FAKE_LOG"\n'
        'if [ "$1 $2" = "toolchain list" ]; then\n'
        '  if [ -f "$ELAN_HOME/fake-toolchain-installed" ]; then\n'
        "    printf 'leanprover/lean4:v4.28.0\\n'\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1 $2" = "toolchain install" ]; then\n'
        '  : > "$ELAN_HOME/fake-toolchain-installed"\n'
        "fi\n"
        "exit 0\n",
    )
    (elan_home / "bin").mkdir()
    (elan_home / "bin" / "elan").symlink_to(elan_home / "elan")
    env.update(
        {
            "HOME": str(home),
            "SHELL": shell,
            "PATH": str(commands),
            "FAKE_LOG": str(log),
            "FAKE_PROOF_TEMPLATE": str(proof_template),
            "FAKE_PYTHON_TEMPLATE": str(python_template),
            "FAKE_UV_TEMPLATE": str(installed_template),
            "FAKE_COMMAND_DIR": str(commands),
            "FAKE_EDITOR_SUCCESS_EDITOR": editor_install_succeeds_for or "",
            "FAKE_EDITOR_SUCCESS_PACKAGE": (
                _editor_package(editor_package_manager, editor_install_succeeds_for)
                if editor_package_manager is not None
                and editor_install_succeeds_for is not None
                else ""
            ),
            "PROOF_ASSISTANT_VENV": str(home / ".venvs/proof-assistant"),
            "PROOF_ASSISTANT_CACHE_HOME": str(home / ".cache/repoprover-codex"),
            "PROOF_ASSISTANT_UV_HOME": str(install_dir),
            "PROOF_ASSISTANT_REPOPROVER_REF": repoprover_revision,
            "PROOF_ASSISTANT_REPOPROVER_URL": str(repoprover),
            "PROOF_ASSISTANT_ELAN_HOME": str(elan_home),
            # Bootstrap tests exercise uv discovery, not the hardware gate;
            # relax the floor so they pass regardless of the test host's specs.
            "PROOF_ASSISTANT_MIN_CPU_CORES": "1",
            "PROOF_ASSISTANT_MIN_MEMORY_GIB": "1",
        }
    )
    if not managed_repoprover:
        env["PROOF_ASSISTANT_REPOPROVER_SOURCE"] = str(repoprover)
    env.update(environment or {})
    result: subprocess.CompletedProcess[str] | None = None
    for _ in range(runs):
        result = subprocess.run(
            [str(sandbox_root / "install.sh")],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            break
    assert result is not None
    return result, log, home, install_dir


def test_installer_always_checks_compiler_before_tests():
    lines = [line.strip() for line in INSTALLER.read_text().splitlines()]
    install = '"${uv_bin}" pip install --python "${venv_path}/bin/python" \\'
    repoprover = '-e "${repoprover_source}" \\'
    project = '-e "${project_root}[dev]"'
    compiler = '"${venv_path}/bin/proof-assistant" compiler-check'
    cache_init = '"${venv_path}/bin/proof-assistant" cache init'
    tests = '"${venv_path}/bin/python" -m pytest -q "${project_root}/tests"'

    assert lines.count(compiler) == 1
    assert lines.count(cache_init) == 1
    assert (
        lines.index(install)
        < lines.index(repoprover)
        < lines.index(project)
        < lines.index(compiler)
        < lines.index(cache_init)
        < lines.index(tests)
    )


def test_installer_prefers_pico_to_micro_when_nano_is_unavailable(
    tmp_path: Path,
) -> None:
    result, _log, _home, _install_dir = _bootstrap_harness(
        tmp_path,
        existing_uv="working",
        available_editors=("micro", "pico"),
    )

    assert result.returncode == 0, result.stderr
    assert "Using terminal editor:" in result.stdout
    assert result.stdout.split("Using terminal editor: ", 1)[1].splitlines()[0].endswith(
        "/pico"
    )


@pytest.mark.parametrize("editor", ("nano", "pico", "micro"))
@pytest.mark.parametrize(
    ("manager", "simulated_os", "command_prefix"),
    (
        ("apt-get", "Linux", "install -y"),
        ("dnf", "Linux", "install -y"),
        ("yum", "Linux", "install -y"),
        ("pacman", "Linux", "--noconfirm --needed -S"),
        ("zypper", "Linux", "--non-interactive install"),
        ("brew", "Darwin", "install"),
        ("port", "Darwin", "-N install"),
    ),
)
def test_installer_package_manager_commands_and_editor_mappings(
    tmp_path: Path,
    editor: str,
    manager: str,
    simulated_os: str | None,
    command_prefix: str,
) -> None:
    result, log, _home, _install_dir = _bootstrap_harness(
        tmp_path,
        existing_uv="working",
        available_editors=(),
        editor_package_manager=manager,
        editor_install_succeeds_for=editor,
        simulated_os=simulated_os,
    )

    assert result.returncode == 0, result.stderr
    assert "No supported terminal editor was found" in result.stdout
    attempted_editors = ("nano", "pico", "micro")
    attempts = attempted_editors[: attempted_editors.index(editor) + 1]
    package_calls = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"package:{manager}:")
    ]
    assert package_calls == [
        f"package:{manager}:{command_prefix} {_editor_package(manager, attempt)}"
        for attempt in attempts
    ]
    assert "Installed terminal editor:" in result.stdout
    assert result.stdout.split("Installed terminal editor: ", 1)[1].splitlines()[
        0
    ].endswith(f"/{editor}")


def test_installer_continues_from_failed_nano_to_successful_pico(
    tmp_path: Path,
) -> None:
    result, log, _home, _install_dir = _bootstrap_harness(
        tmp_path,
        existing_uv="working",
        available_editors=(),
        editor_package_manager="apt-get",
        editor_install_succeeds_for="pico",
        simulated_os="Linux",
    )

    assert result.returncode == 0, result.stderr
    assert "Trying to install nano (nano) with apt-get." in result.stdout
    assert "Trying to install pico (alpine-pico) with apt-get." in result.stdout
    assert "nano installation did not produce a usable editor" in result.stderr
    assert "package:apt-get:install -y nano" in log.read_text(encoding="utf-8")
    assert "package:apt-get:install -y alpine-pico" in log.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("privilege", ("missing", "denied"))
def test_privilege_failure_stops_editor_installation_after_one_attempt(
    tmp_path: Path,
    privilege: str,
) -> None:
    result, log, _home, _install_dir = _bootstrap_harness(
        tmp_path,
        existing_uv="working",
        available_editors=(),
        editor_package_manager="apt-get",
        editor_privilege=privilege,
        simulated_os="Linux",
    )

    assert result.returncode == 2
    assert result.stdout.count("Trying to install") == 1
    assert "required administrative access" in result.stderr
    assert "package:apt-get:" not in log.read_text(encoding="utf-8")


def test_installer_fails_clearly_when_editor_and_package_manager_are_missing(
    tmp_path: Path,
) -> None:
    result, _log, _home, _install_dir = _bootstrap_harness(
        tmp_path,
        existing_uv="working",
        available_editors=(),
    )

    assert result.returncode == 2
    assert "nano, pico, and micro are unavailable" in result.stderr
    assert "Install nano, pico, or micro and rerun install.sh" in result.stderr


def test_root_installer_is_the_single_documented_install_entrypoint() -> None:
    assert INSTALLER.is_file()
    assert os.access(INSTALLER, os.X_OK)
    assert not (ROOT / "scripts" / "install-dev.sh").exists()
    assert ONE_LINE_INSTALL in (ROOT / "README.md").read_text(encoding="utf-8")
    assert ONE_LINE_INSTALL in (ROOT / "docs" / "INSTALLATION.md").read_text(
        encoding="utf-8"
    )


def test_documented_one_line_command_propagates_download_failure(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "curl", "#!/bin/sh\nexit 55\n")
    env = _clean_install_environment()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        ["bash", "-c", ONE_LINE_INSTALL],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 55


def test_installer_pins_and_bootstraps_lean_and_repoprover() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert "386adba3df572cb71df534add2c764e071898a2e" in text
    assert "0e36a07b9bbcc5381fa6250df109f9a4f94d7bac" in text
    assert "--no-modify-path --default-toolchain none" in text
    assert 'toolchain install "${lean_toolchain}"' in text
    assert '-e "${repoprover_source}"' in text
    assert "scripts/bootstrap-uv.sh" in text


def test_installer_rejects_dropbox_environment_before_creation(tmp_path):
    forbidden = tmp_path / "Dropbox" / "forbidden-venv"
    env = _relax_hardware_gate(_clean_install_environment())
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
    env = _relax_hardware_gate(_clean_install_environment())
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
    env = _relax_hardware_gate(_clean_install_environment())
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


def test_installer_rejects_relative_override_paths_before_creation(tmp_path: Path):
    env = _relax_hardware_gate(_clean_install_environment())
    env["PROOF_ASSISTANT_VENV"] = "relative/venv"

    result = subprocess.run(
        ["/bin/bash", str(INSTALLER)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 2
    assert "must be an absolute path: relative/venv" in result.stderr
    assert not (tmp_path / "relative").exists()


def test_installer_rejects_path_symlinked_into_dropbox(tmp_path: Path):
    home = tmp_path / "home"
    dropbox = home / "Dropbox" / "environments"
    dropbox.mkdir(parents=True)
    (home / "safe-looking").symlink_to(dropbox, target_is_directory=True)
    env = _relax_hardware_gate(_clean_install_environment())
    env["HOME"] = str(home)
    env["PROOF_ASSISTANT_VENV"] = str(home / "safe-looking" / "venv")

    result = subprocess.run(
        [str(INSTALLER)], text=True, capture_output=True, check=False, env=env
    )

    assert result.returncode == 2
    assert "must not reside in Dropbox" in result.stderr
    assert not (dropbox / "venv").exists()


def test_installer_rejects_checkout_reached_through_symlink_into_dropbox(
    tmp_path: Path,
):
    home = tmp_path / "home"
    checkout = home / "Dropbox" / "proof-assistant"
    (checkout / "src" / "proof_assistant").mkdir(parents=True)
    shutil.copy2(INSTALLER, checkout / "install.sh")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "proof-assistant"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    safe_link = tmp_path / "safe-looking-checkout"
    safe_link.symlink_to(checkout, target_is_directory=True)
    env = _relax_hardware_gate(_clean_install_environment())
    env["HOME"] = str(home)

    result = subprocess.run(
        [str(safe_link / "install.sh")],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 2
    assert "Proof Assistant source must not reside in Dropbox" in result.stderr
    assert not (home / ".local").exists()


def _stub_path_env(tmp_path: Path, stubs: dict[str, str]) -> dict[str, str]:
    """Prepend fake OS-query binaries to PATH; everything else stays real."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    for name, script in stubs.items():
        _write_executable(stub_dir / name, script)
    env = _clean_install_environment()
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


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="the /proc/cpuinfo fallback is Linux-specific",
)
def test_system_check_uses_proc_cpuinfo_when_lscpu_is_unavailable(tmp_path: Path):
    command_dir = tmp_path / "commands-without-lscpu"
    command_dir.mkdir()
    for name in ("awk", "dirname", "getconf"):
        target = shutil.which(name)
        assert target is not None
        (command_dir / name).symlink_to(target)
    _write_executable(
        command_dir / "uname",
        _UNAME_STUB.format(os_name="Linux", os_release="6.8.0"),
    )
    env = _clean_install_environment()
    env["PATH"] = str(command_dir)
    env["PROOF_ASSISTANT_MIN_CPU_CORES"] = "1"
    env["PROOF_ASSISTANT_MIN_MEMORY_GIB"] = "1"
    env["PROOF_ASSISTANT_VENV"] = str(tmp_path / "Dropbox" / "venv")

    result = subprocess.run(
        ["/bin/bash", str(INSTALLER)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 2
    assert "System check: Linux 6.8.0" in result.stdout
    assert "must not reside in Dropbox" in result.stderr


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
    assert "PROOF_ASSISTANT_SOURCE_DIR" in text
    assert "PROOF_ASSISTANT_REPOPROVER_SOURCE" in text
    assert "PROOF_ASSISTANT_REPOPROVER_REF" in text
    assert "PROOF_ASSISTANT_ELAN_HOME" in text
    assert "REPOPROVER_CODEX_VENV" in text
    assert "REPOPROVER_CODEX_CACHE_HOME" in text
    assert "REPOPROVER_CODEX_PYTHON" in text


def _remote_installer_fixture(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "remote-source"
    repository.mkdir()
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    stub = repository / "install.sh"
    _write_executable(
        stub,
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf 'checked-out-installer:%s\\n' \"$(pwd)\"\n",
    )
    subprocess.run(["git", "add", "install.sh"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Proof Assistant Tests",
            "-c",
            "user.email=tests@invalid.example",
            "commit",
            "-m",
            "installer fixture",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return repository, "main"


def _run_streamed_installer(
    tmp_path: Path,
    repository: Path,
    ref: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    home = tmp_path / "streamed-home"
    home.mkdir(exist_ok=True)
    source = home / ".local" / "share" / "proof-assistant" / "source"
    env = _clean_install_environment()
    env.update(
        {
            "HOME": str(home),
            "PROOF_ASSISTANT_REPOSITORY_URL": str(repository),
            "PROOF_ASSISTANT_REF": ref,
            "PROOF_ASSISTANT_SOURCE_DIR": str(source),
            "PROOF_ASSISTANT_MIN_CPU_CORES": "1",
            "PROOF_ASSISTANT_MIN_MEMORY_GIB": "1",
        }
    )
    result = subprocess.run(
        ["bash"],
        input=INSTALLER.read_text(encoding="utf-8"),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    return result, source


def test_streamed_one_line_installer_clones_and_executes_checked_out_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROOF_ASSISTANT_VENV", "relative/parent")
    repository, ref = _remote_installer_fixture(tmp_path)

    result, source = _run_streamed_installer(tmp_path, repository, ref)

    assert result.returncode == 0, result.stderr
    assert "checked-out-installer:" in result.stdout
    assert (source / ".git").is_dir()
    assert (source / "install.sh").is_file()


def test_streamed_installer_refuses_to_replace_local_checkout_changes(
    tmp_path: Path,
) -> None:
    repository, ref = _remote_installer_fixture(tmp_path)
    first, source = _run_streamed_installer(tmp_path, repository, ref)
    assert first.returncode == 0, first.stderr
    (source / "local-note.txt").write_text("preserve me\n", encoding="utf-8")

    second, _source = _run_streamed_installer(tmp_path, repository, ref)

    assert second.returncode == 2
    assert "checkout has local changes" in second.stderr
    assert (source / "local-note.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_installer_creates_pinned_managed_repoprover_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PROOF_ASSISTANT_REPOPROVER_SOURCE", "/parent-installer/repoprover"
    )
    result, log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        managed_repoprover=True,
    )
    checkout = home / ".local" / "share" / "proof-assistant" / "repoprover"

    assert result.returncode == 0, result.stderr
    assert (checkout / ".git").is_dir()
    assert (
        subprocess.run(
            ["git", "remote", "get-url", "--push", "origin"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "DISABLED"
    )
    assert f"-e {checkout}" in log.read_text(encoding="utf-8")


def test_installer_accepts_explicit_repoprover_linked_worktree(tmp_path: Path) -> None:
    origin_parent = tmp_path / "origin-fixture"
    origin_parent.mkdir()
    origin, revision = _fake_repoprover_checkout(origin_parent)
    worktree = tmp_path / "linked-repoprover"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), revision],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
    )
    assert (worktree / ".git").is_file()

    result, log, _home, _install_dir = _bootstrap_harness(
        tmp_path,
        existing_uv="working",
        environment={
            "PROOF_ASSISTANT_REPOPROVER_SOURCE": str(worktree),
            "PROOF_ASSISTANT_REPOPROVER_REF": revision,
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"-e {worktree}" in log.read_text(encoding="utf-8")


def test_uv_bootstrap_never_uses_privilege_or_system_package_managers():
    bootstrap = ROOT / "scripts" / "bootstrap-uv.sh"
    text = bootstrap.read_text(encoding="utf-8")
    assert bootstrap.is_file()
    assert "https://astral.sh/uv/install.sh" not in text
    assert 'uv_bin="$(command -v uv)"' not in text
    for forbidden in ("sudo ", "brew ", "apt ", "apt-get ", "cargo install"):
        assert forbidden not in text


@pytest.mark.parametrize(
    ("os_name", "machine", "target"),
    (
        ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
        ("Linux", "arm64", "aarch64-unknown-linux-gnu"),
        ("Darwin", "arm64", "aarch64-apple-darwin"),
        ("Darwin", "x86_64", "x86_64-apple-darwin"),
    ),
)
def test_checksum_verified_uv_bootstrap_rejects_corrupt_archive(
    tmp_path: Path,
    os_name: str,
    machine: str,
    target: str,
):
    fixture = tmp_path / "bootstrap-fixture"
    (fixture / "scripts").mkdir(parents=True)
    (fixture / "requirements").mkdir()
    shutil.copy2(ROOT / "scripts" / "bootstrap-uv.sh", fixture / "scripts")
    (fixture / "requirements" / "uv-0.12.0-sha256.txt").write_text(
        f"{'0' * 64}  uv-{target}.tar.gz\n", encoding="utf-8"
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "curl",
        "#!/bin/sh\n"
        "output=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then shift; output=$1; fi\n'
        "  shift\n"
        "done\n"
        "printf 'corrupt archive' > \"$output\"\n",
    )
    _write_executable(
        fake_bin / "uname",
        "#!/bin/sh\n"
        f'if [ "$1" = "-s" ]; then echo "{os_name}"; exit 0; fi\n'
        f'if [ "$1" = "-m" ]; then echo "{machine}"; exit 0; fi\n'
        "exit 1\n",
    )
    env = _clean_install_environment()
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        [str(fixture / "scripts" / "bootstrap-uv.sh"), str(tmp_path / "uv")],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 2
    assert "SHA-256 mismatch" in result.stderr


def test_uv_checksum_manifest_includes_linux_aarch64_release() -> None:
    manifest = (ROOT / "requirements" / "uv-0.12.0-sha256.txt").read_text(
        encoding="utf-8"
    )
    assert (
        "2c5d6e3092cc5223b10ff403880cc75121bf64e84644e7a0c69f643b0d89ac95  "
        "uv-aarch64-unknown-linux-gnu.tar.gz"
    ) in manifest


def test_installer_honors_legacy_environment_override(tmp_path):
    forbidden = tmp_path / "Dropbox" / "legacy-venv"
    env = _relax_hardware_gate(_clean_install_environment())
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
    assert "bootstrap:" not in calls
    assert "installed:" not in calls


def test_installer_exports_selected_elan_home_for_every_elan_call(tmp_path: Path):
    result, log, home, _install_dir = _bootstrap_harness(
        tmp_path, existing_uv="working"
    )
    elan_calls = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("elan:")
    ]

    assert result.returncode == 0, result.stderr
    assert elan_calls
    assert all(f"ELAN_HOME={home / '.elan'}" in line for line in elan_calls)


def test_repeated_install_skips_already_installed_lean_toolchain(tmp_path: Path):
    result, log, _home, _install_dir = _bootstrap_harness(
        tmp_path, existing_uv="working", runs=2
    )
    elan_calls = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("elan:")
    ]

    assert result.returncode == 0, result.stderr
    assert sum("toolchain list" in line for line in elan_calls) == 2
    assert sum("toolchain install" in line for line in elan_calls) == 1


def test_installer_reuses_prior_local_bootstrap_even_when_not_on_shell_path(tmp_path):
    result, log, _home, install_dir = _bootstrap_harness(
        tmp_path, existing_uv="install_dir", downloader="none"
    )
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert f"Using uv: {install_dir / 'uv'}" in result.stdout
    assert "existing-local:--version" in calls
    assert "existing-local:pip install --python" in calls


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


def test_missing_uv_uses_checksum_verified_bootstrap_and_exact_binary(tmp_path):
    result, log, home, install_dir = _bootstrap_harness(tmp_path, downloader="curl")
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert install_dir == home / ".local/share/proof-assistant/uv"
    assert (install_dir / "uv").is_file()
    assert f"bootstrap:{install_dir}" in calls
    assert "installed:--version" in calls
    assert "installed:pip install --python" in calls
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
    assert f"bootstrap:{install_dir}" in calls
    assert "installed:pip install --python" in calls


def test_failed_verified_bootstrap_exits_two_without_creating_uv(tmp_path):
    result, _log, _home, install_dir = _bootstrap_harness(tmp_path, downloader="none")
    assert result.returncode == 2
    assert "checksum-verified uv bootstrap failed" in result.stderr
    assert not (install_dir / "uv").exists()


def test_bootstrap_must_produce_a_working_uv_before_environment_changes(tmp_path):
    result, log, home, install_dir = _bootstrap_harness(
        tmp_path, downloader="curl", installer_creates_uv=False
    )
    calls = log.read_text(encoding="utf-8")
    assert result.returncode == 2
    assert "did not produce a working executable" in result.stderr
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
    assert "did not produce a working executable" in result.stderr
    assert "installed:--version" in calls
    assert "installed:pip" not in calls
    assert (install_dir / "uv").is_file()


def test_uv_bootstrap_failure_has_clear_error_and_exit_two(tmp_path):
    result, log, _home, install_dir = _bootstrap_harness(
        tmp_path, downloader="curl", downloader_succeeds=False
    )
    assert result.returncode == 2
    assert "checksum-verified uv bootstrap failed" in result.stderr
    assert "bootstrap:" in log.read_text(encoding="utf-8")
    assert not (install_dir / "uv").exists()


def test_uncreatable_uv_bootstrap_directory_exits_two_before_download(tmp_path):
    install_dir = tmp_path / "blocked"
    result, log, _home, _resolved = _bootstrap_harness(
        tmp_path,
        downloader="curl",
        custom_install_dir=install_dir,
        block_install_dir=True,
    )
    assert result.returncode == 2
    assert "uv bootstrap directory has a non-directory path component" in result.stderr
    assert "bootstrap:" not in log.read_text(encoding="utf-8")


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
        text.count("Added by Proof Assistant installer") == 2 for text in configured
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
    assert installed_profile.count("Added by Proof Assistant installer") == 2
    assert installed_bashrc.count("Added by Proof Assistant installer") == 2


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
    installed_profile = (home / ".bash_profile").read_text(encoding="utf-8")
    installed_bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert installed_profile.startswith("export LOGIN_SENTINEL=1\n" + previous_guard)
    assert installed_bashrc.startswith("export BASHRC_SENTINEL=1\n" + previous_guard)
    assert installed_profile.count(str(path_dir)) == 2
    assert installed_bashrc.count(str(path_dir)) == 2
    assert installed_profile.count("Added by Proof Assistant installer") == 2
    assert installed_bashrc.count("Added by Proof Assistant installer") == 2
    assert ".elan/bin" in installed_profile
    assert ".elan/bin" in installed_bashrc


def test_installer_recognizes_existing_home_relative_multiline_guard(tmp_path):
    portable_guard = (
        "# Proof Assistant and its Lean toolchain.\n"
        'case ":$PATH:" in\n'
        '    *:"$HOME/.venvs/proof-assistant/bin":*) ;;\n'
        '    *) export PATH="$HOME/.venvs/proof-assistant/bin:$PATH" ;;\n'
        "esac\n"
    )
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        startup_files={
            ".bash_profile": "export LOGIN_SENTINEL=1\n" + portable_guard,
            ".bashrc": "export BASHRC_SENTINEL=1\n" + portable_guard,
        },
        runs=2,
    )

    assert result.returncode == 0, result.stderr
    installed_profile = (home / ".bash_profile").read_text(encoding="utf-8")
    installed_bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert installed_profile.startswith("export LOGIN_SENTINEL=1\n" + portable_guard)
    assert installed_bashrc.startswith("export BASHRC_SENTINEL=1\n" + portable_guard)
    assert str(home / ".venvs/proof-assistant/bin") not in installed_profile
    assert str(home / ".venvs/proof-assistant/bin") not in installed_bashrc
    assert installed_profile.count("Added by Proof Assistant installer") == 1
    assert installed_bashrc.count("Added by Proof Assistant installer") == 1
    assert ".elan/bin" in installed_profile
    assert ".elan/bin" in installed_bashrc


def test_installer_does_not_mistake_path_mentions_or_removals_for_setup(tmp_path):
    misleading_lines = (
        'echo "$HOME/.venvs/proof-assistant/bin is not present in PATH"\n'
        r'PATH="${PATH//$HOME\/.venvs\/proof-assistant\/bin:/}"' "\n"
        "PATH=/usr/bin:$PATH # $HOME/.venvs/proof-assistant/bin:$PATH\n"
        "fish_add_path /usr/local/bin # $HOME/.venvs/proof-assistant/bin\n"
        'PATH=/usr/bin:$PATH; echo "$HOME/.venvs/proof-assistant/bin:$PATH"\n'
        'fish_add_path /usr/local/bin; echo "$HOME/.venvs/proof-assistant/bin"\n'
    )
    result, _log, home, _install_dir = _bootstrap_harness(
        tmp_path,
        startup_files={
            ".bash_profile": "export LOGIN_SENTINEL=1\n" + misleading_lines,
            ".bashrc": "export BASHRC_SENTINEL=1\n" + misleading_lines,
        },
        runs=2,
    )

    assert result.returncode == 0, result.stderr
    installed_profile = (home / ".bash_profile").read_text(encoding="utf-8")
    installed_bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    expected_path = str(home / ".venvs/proof-assistant/bin")
    assert expected_path in installed_profile
    assert expected_path in installed_bashrc
    assert installed_profile.count("Added by Proof Assistant installer") == 2
    assert installed_bashrc.count("Added by Proof Assistant installer") == 2


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
    assert installed.count("Added by Proof Assistant installer") == 2


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
    assert "Added by Proof Assistant installer" in (home / ".bash_login").read_text(
        encoding="utf-8"
    )
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
    assert installed_zprofile.count("Added by Proof Assistant installer") == 2
    assert installed_zshrc.count("Added by Proof Assistant installer") == 2


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
    assert installed.count("Added by Proof Assistant installer") == 2


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
    assert installed.count("Added by Proof Assistant installer") == 2


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
    assert installed.count("Added by Proof Assistant installer") == 2
    assert "fish_add_path --path" in installed
    assert not (home / ".config" / "fish" / "config.fish").exists()
