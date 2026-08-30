import json
import subprocess
from dataclasses import replace

import pytest

from proof_assistant.ai import (
    AuthenticationState,
    CommandResult,
    CredentialSource,
    Difficulty,
    DiscoverySource,
    DriverId,
    DriverPreference,
    HttpResponse,
    InstallationState,
    InstallConsentError,
    MachineProviderConfigStore,
    ModelCatalog,
    ModelDescriptor,
    ProviderConfigError,
    ProviderService,
    SecretSubmission,
    SetupActionState,
    ShellPathManager,
    TaskKind,
    TaskPreference,
    UnsupportedDifficultyError,
)


class FakeExecutables:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def which(self, executable, *, path=None):
        self.calls.append((executable, path))
        return self.values.get(executable)


class FakeCommands:
    def __init__(self, handler=None):
        self.handler = handler or (lambda argv, input_text: CommandResult(0, "ok"))
        self.calls = []

    def run(
        self,
        argv,
        *,
        input_text=None,
        timeout_seconds=30.0,
        env=None,
    ):
        self.calls.append((argv, input_text, timeout_seconds, env))
        return self.handler(argv, input_text)


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(
        self,
        method,
        url,
        *,
        headers,
        body=None,
        timeout_seconds=30.0,
    ):
        self.calls.append((method, url, dict(headers), body, timeout_seconds))
        return self.response


class FakeCredentials:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def get(self, driver, source):
        self.calls.append(("get", driver, source))
        return self.values.get((driver, source))

    def set(self, driver, source, submission):
        self.calls.append(("set", driver, source))
        self.values[(driver, source)] = submission.consume()

    def delete(self, driver, source):
        self.calls.append(("delete", driver, source))
        return self.values.pop((driver, source), None) is not None


class FakePathManager:
    def __init__(self):
        self.paths = []

    def ensure(self, directory):
        self.paths.append(directory)


def service(tmp_path, **kwargs):
    return ProviderService(
        config_store=MachineProviderConfigStore(tmp_path / "providers.json"),
        home=tmp_path / "home",
        environment={"PATH": "/usr/bin", "SHELL": "/bin/zsh"},
        **kwargs,
    )


@pytest.mark.parametrize(
    ("driver", "version_argv", "version", "auth_argv"),
    [
        (
            DriverId.CODEX_CLI,
            ("/tools/codex", "--version"),
            "codex-cli 1.2.3",
            ("/tools/codex", "login", "status"),
        ),
        (
            DriverId.CLAUDE_CLI,
            ("/tools/claude", "--version"),
            "Claude Code 2.0",
            ("/tools/claude", "auth", "status", "--text"),
        ),
    ],
)
def test_cli_probe_checks_version_identity_and_nonbillable_auth(
    tmp_path, driver, version_argv, version, auth_argv
):
    executable = version_argv[0]

    def handler(argv, input_text):
        if argv == version_argv:
            return CommandResult(0, version)
        if argv == auth_argv:
            return CommandResult(0, "logged in")
        if argv == (executable, "app-server"):
            return CommandResult(1)
        raise AssertionError(argv)

    commands = FakeCommands(handler)
    status = service(
        tmp_path,
        commands=commands,
        executables=FakeExecutables({driver.value.removesuffix("_cli"): executable}),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    ).inspect_driver(driver)
    assert status.installation is InstallationState.INSTALLED
    assert status.authentication is AuthenticationState.AUTHENTICATED
    assert status.version == version
    assert any(call[0] == auth_argv for call in commands.calls)


def test_cli_probe_does_not_read_auth_files_and_reports_login_required(tmp_path):
    commands = FakeCommands(
        lambda argv, input_text: (
            CommandResult(0, "codex-cli 1")
            if argv[-1] == "--version"
            else CommandResult(1, "not logged in")
        )
    )
    status = service(
        tmp_path,
        commands=commands,
        executables=FakeExecutables({"codex": "/bin/codex"}),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    ).inspect_driver(DriverId.CODEX_CLI, discover_models=False)
    assert status.authentication is AuthenticationState.REQUIRED
    assert "codex login" in status.detail
    assert all("auth.json" not in " ".join(call[0]) for call in commands.calls)


def test_cli_setup_children_receive_only_provider_specific_environment(tmp_path):
    commands = FakeCommands(
        lambda argv, input_text: (
            CommandResult(0, "Claude Code 2.0")
            if argv[-1] == "--version"
            else CommandResult(0, "logged in")
        )
    )
    core = ProviderService(
        config_store=MachineProviderConfigStore(tmp_path / "providers.json"),
        home=tmp_path / "home",
        environment={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/bin",
            "LANG": "C",
            "OPENAI_API_KEY": "openai-must-not-cross",
            "ANTHROPIC_API_KEY": "anthropic-must-not-cross",
            "GEMINI_API_KEY": "gemini-must-not-cross",
            "UNRELATED_PRIVATE_TOKEN": "private-must-not-cross",
        },
        commands=commands,
        executables=FakeExecutables({"claude": "/bin/claude"}),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    )
    status = core.inspect_driver(DriverId.CLAUDE_CLI, discover_models=False)
    assert status.authentication is AuthenticationState.AUTHENTICATED
    assert commands.calls
    for _argv, _input, _timeout, child_environment in commands.calls:
        assert child_environment["HOME"] == str(tmp_path / "home")
        assert child_environment["PATH"].startswith(str(tmp_path / "home/.local/bin"))
        assert "OPENAI_API_KEY" not in child_environment
        assert "ANTHROPIC_API_KEY" not in child_environment
        assert "GEMINI_API_KEY" not in child_environment
        assert "UNRELATED_PRIVATE_TOKEN" not in child_environment


def test_copilot_probe_rejects_wrong_executable_identity(tmp_path):
    def handler(argv, input_text):
        if argv == ("/bin/copilot", "version"):
            return CommandResult(0, "v1.0")
        if argv == ("/bin/copilot", "help"):
            return CommandResult(0, "AWS deployment helper")
        raise AssertionError(argv)

    status = service(
        tmp_path,
        commands=FakeCommands(handler),
        executables=FakeExecutables({"copilot": "/bin/copilot"}),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    ).inspect_driver(DriverId.COPILOT_CLI)
    assert status.installation is InstallationState.BROKEN


def test_copilot_probe_does_not_claim_auth_or_make_billable_call(tmp_path):
    def handler(argv, input_text):
        if argv[-1] == "version":
            return CommandResult(0, "1.0")
        if argv[-1] == "help":
            return CommandResult(0, "GitHub Copilot prompt agent")
        raise AssertionError(argv)

    commands = FakeCommands(handler)
    status = service(
        tmp_path,
        commands=commands,
        executables=FakeExecutables({"copilot": "/bin/copilot"}),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    ).inspect_driver(DriverId.COPILOT_CLI)
    assert status.installation is InstallationState.INSTALLED
    assert status.authentication is AuthenticationState.UNKNOWN
    assert all("-p" not in call[0] for call in commands.calls)
    assert status.catalog.source is DiscoverySource.CURATED_FALLBACK
    assert status.catalog.contract_approved


def test_copilot_account_probe_is_explicit_minimal_and_version_bound(tmp_path):
    def handler(argv, input_text):
        del input_text
        if argv == ("/bin/copilot", "version"):
            return CommandResult(0, "GitHub Copilot CLI 1.2")
        if argv == ("/bin/copilot", "help"):
            return CommandResult(0, "GitHub Copilot prompt agent")
        if "-p" in argv:
            return CommandResult(0, '{"content":"OK"}\n')
        raise AssertionError(argv)

    commands = FakeCommands(handler)
    core = service(
        tmp_path,
        commands=commands,
        executables=FakeExecutables({"copilot": "/bin/copilot"}),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    )
    before = core.inspect_driver(DriverId.COPILOT_CLI)
    assert before.authentication is AuthenticationState.UNKNOWN
    assert all("-p" not in call[0] for call in commands.calls)

    with pytest.raises(InstallConsentError):
        core.verify_cli_account(DriverId.COPILOT_CLI, consent=False)
    assert all("-p" not in call[0] for call in commands.calls)

    verified = core.verify_cli_account(DriverId.COPILOT_CLI, consent=True)
    assert verified.authentication is AuthenticationState.AUTHENTICATED
    probe = next(call[0] for call in commands.calls if "-p" in call[0])
    assert "--disable-builtin-mcps" in probe
    assert "--no-custom-instructions" in probe
    assert "--available-tools=proof_assistant" in probe
    assert (
        core.config_store.load()
        .config.preference_for(DriverId.COPILOT_CLI)
        .runtime_verified_version
        == "GitHub Copilot CLI 1.2"
    )


def test_shell_path_manager_updates_login_and_interactive_zsh_profiles(tmp_path):
    environment = {"PATH": "/usr/bin", "SHELL": "/bin/zsh"}
    manager = ShellPathManager(environment=environment, home=tmp_path)
    bin_path = tmp_path / ".local" / "bin"
    manager.ensure(bin_path)
    manager.ensure(bin_path)
    assert environment["PATH"].split(":")[0] == str(bin_path)
    for profile_name in (".zprofile", ".zshrc"):
        text = (tmp_path / profile_name).read_text()
        assert text.count("# Added by Proof Assistant") == 1
        assert str(bin_path) in text
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            '. "$HOME/.zprofile"; . "$HOME/.zshrc"; printf "%s" "$PATH"',
        ],
        check=True,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin"},
    )
    assert result.stdout.split(":").count(str(bin_path)) == 1


def test_shell_path_manager_does_not_shadow_existing_bash_profile(tmp_path):
    profile = tmp_path / ".profile"
    bashrc = tmp_path / ".bashrc"
    profile.write_text(
        'export PROFILE_SENTINEL=1\n. "$HOME/.bashrc"\n', encoding="utf-8"
    )
    bashrc.write_text("export BASHRC_SENTINEL=1\n", encoding="utf-8")
    environment = {"PATH": "/usr/bin", "SHELL": "/bin/bash"}
    manager = ShellPathManager(environment=environment, home=tmp_path)
    bin_path = tmp_path / ".local" / "bin"

    manager.ensure(bin_path)
    first_profile = profile.read_bytes()
    first_bashrc = bashrc.read_bytes()
    manager.ensure(bin_path)

    assert not (tmp_path / ".bash_profile").exists()
    assert not (tmp_path / ".bash_login").exists()
    installed_profile = profile.read_text(encoding="utf-8")
    installed_bashrc = bashrc.read_text(encoding="utf-8")
    assert installed_profile.startswith(
        'export PROFILE_SENTINEL=1\n. "$HOME/.bashrc"\n'
    )
    assert installed_bashrc.startswith("export BASHRC_SENTINEL=1\n")
    assert installed_profile.count("# Added by Proof Assistant") == 1
    assert installed_bashrc.count("# Added by Proof Assistant") == 1
    assert profile.read_bytes() == first_profile
    assert bashrc.read_bytes() == first_bashrc

    result = subprocess.run(
        ["/bin/bash", "-c", '. "$HOME/.profile"; printf "%s" "$PATH"'],
        check=True,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin"},
    )
    assert result.stdout.split(":").count(str(bin_path)) == 1


def test_shell_path_manager_uses_existing_bash_login_file(tmp_path):
    bash_login = tmp_path / ".bash_login"
    profile = tmp_path / ".profile"
    bash_login.write_text("export LOGIN_SENTINEL=1\n", encoding="utf-8")
    profile.write_text("export PROFILE_SENTINEL=1\n", encoding="utf-8")
    environment = {"PATH": "/usr/bin", "SHELL": "/bin/bash"}
    manager = ShellPathManager(environment=environment, home=tmp_path)

    manager.ensure(tmp_path / ".local" / "bin")

    assert not (tmp_path / ".bash_profile").exists()
    assert "# Added by Proof Assistant" in bash_login.read_text(encoding="utf-8")
    assert profile.read_text(encoding="utf-8") == "export PROFILE_SENTINEL=1\n"


def test_shell_path_manager_migrates_legacy_owned_bash_profile(tmp_path):
    bin_path = tmp_path / ".local" / "bin"
    legacy = (
        b"# Added by Proof Assistant\n" + f'export PATH={bin_path}:"$PATH"\n'.encode()
    )
    bash_profile = tmp_path / ".bash_profile"
    bash_profile.write_bytes(legacy)
    profile = tmp_path / ".profile"
    profile.write_bytes(b"export PROFILE_SENTINEL=1\n")
    manager = ShellPathManager(
        environment={"PATH": "/usr/bin", "SHELL": "/bin/bash"}, home=tmp_path
    )

    manager.ensure(bin_path)

    assert not bash_profile.exists()
    assert (tmp_path / ".bash_profile.proof-assistant-backup").read_bytes() == legacy
    assert b"# Added by Proof Assistant" in profile.read_bytes()


def test_shell_path_manager_transfers_other_managed_path_before_migration(tmp_path):
    bash_profile = tmp_path / ".bash_profile"
    other_path = tmp_path / ".venvs" / "proof-assistant" / "bin"
    current_path = tmp_path / ".local" / "bin"
    legacy = (
        b"# Added by Proof Assistant installer\n"
        + f'case ":$PATH:" in *":{other_path}:"*) ;; *) '.encode()
        + f'export PATH={other_path}:"$PATH";; esac\n'.encode()
        + b"\n# Added by Proof Assistant\n"
        + f'export PATH={current_path}:"$PATH"\n'.encode()
    )
    bash_profile.write_bytes(legacy)
    manager = ShellPathManager(
        environment={"PATH": "/usr/bin", "SHELL": "/bin/bash"}, home=tmp_path
    )

    manager.ensure(current_path)

    assert not bash_profile.exists()
    assert (tmp_path / ".bash_profile.proof-assistant-backup").read_bytes() == legacy
    installed = (tmp_path / ".profile").read_text(encoding="utf-8")
    assert str(other_path) in installed
    assert str(current_path) in installed


def test_shell_path_manager_refuses_readable_nonregular_bash_candidate(tmp_path):
    (tmp_path / ".bash_profile").symlink_to(tmp_path, target_is_directory=True)
    manager = ShellPathManager(
        environment={"PATH": "/usr/bin", "SHELL": "/bin/bash"}, home=tmp_path
    )

    with pytest.raises(OSError, match="readable non-regular Bash startup file"):
        manager.ensure(tmp_path / ".local" / "bin")


def test_shell_path_manager_requires_exact_active_path_line(tmp_path):
    profile = tmp_path / ".profile"
    manager = ShellPathManager(
        environment={"PATH": "/usr/bin", "SHELL": "/bin/bash"}, home=tmp_path
    )
    bin_path = tmp_path / ".local" / "bin"
    line = manager._path_line(str(bin_path))
    profile.write_text(f"# example only: {line}\n", encoding="utf-8")

    manager.ensure(bin_path)

    installed = profile.read_text(encoding="utf-8")
    assert installed.startswith(f"# example only: {line}\n")
    assert installed.splitlines().count(line) == 1


def test_shell_path_manager_upgrades_its_owned_legacy_export(tmp_path):
    profile = tmp_path / ".profile"
    manager = ShellPathManager(
        environment={"PATH": "/usr/bin", "SHELL": "/bin/bash"}, home=tmp_path
    )
    bin_path = tmp_path / ".local" / "bin"
    legacy = (
        "export SENTINEL=1\n"
        "# Added by Proof Assistant\n"
        f'export PATH={bin_path}:"$PATH"\n'
    )
    profile.write_text(legacy, encoding="utf-8")

    manager.ensure(bin_path)
    manager.ensure(bin_path)

    installed = profile.read_text(encoding="utf-8")
    assert installed.startswith("export SENTINEL=1\n")
    assert f'export PATH={bin_path}:"$PATH"\n' not in installed
    assert installed.splitlines().count(manager._path_line(str(bin_path))) == 1


def test_shell_path_manager_skips_broken_bash_login_candidate(tmp_path):
    (tmp_path / ".bash_profile").symlink_to("missing-profile")
    profile = tmp_path / ".profile"
    profile.write_bytes(b"\xffPROFILE_SENTINEL\n")
    manager = ShellPathManager(
        environment={"PATH": "/usr/bin", "SHELL": "/bin/bash"}, home=tmp_path
    )

    manager.ensure(tmp_path / ".local" / "bin")

    assert (tmp_path / ".bash_profile").is_symlink()
    assert not (tmp_path / "missing-profile").exists()
    assert profile.read_bytes().startswith(b"\xffPROFILE_SENTINEL\n")


def test_shell_path_manager_honors_custom_zsh_and_fish_roots(tmp_path):
    bin_path = tmp_path / ".local" / "bin"
    zdotdir = tmp_path / "zsh-config"
    zsh = ShellPathManager(
        environment={
            "PATH": "/usr/bin",
            "SHELL": "/bin/zsh",
            "ZDOTDIR": str(zdotdir),
        },
        home=tmp_path,
    )
    zsh.ensure(bin_path)
    for name in (".zprofile", ".zshrc"):
        assert (zdotdir / name).is_file()

    xdg_config = tmp_path / "xdg"
    fish = ShellPathManager(
        environment={
            "PATH": "/usr/bin",
            "SHELL": "/usr/bin/fish",
            "XDG_CONFIG_HOME": str(xdg_config),
        },
        home=tmp_path,
    )
    fish.ensure(bin_path)
    fish.ensure(bin_path)
    fish_config = (xdg_config / "fish" / "config.fish").read_text(encoding="utf-8")
    assert fish_config.count("# Added by Proof Assistant") == 1
    assert "fish_add_path --path" in fish_config


def test_codex_live_model_discovery_uses_app_server_contract(tmp_path):
    response = {
        "id": 2,
        "result": {
            "data": [
                {
                    "model": "gpt-account-model",
                    "displayName": "Account Model",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low"},
                        {"reasoningEffort": "xhigh"},
                    ],
                }
            ]
        },
    }
    commands = FakeCommands(
        lambda argv, input_text: CommandResult(0, json.dumps(response) + "\n")
    )
    catalog = service(
        tmp_path,
        commands=commands,
        executables=FakeExecutables({"codex": "/bin/codex"}),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    ).discover_models(DriverId.CODEX_CLI)
    assert catalog.source is DiscoverySource.LIVE_ACCOUNT
    assert catalog.models[0].model_id == "gpt-account-model"
    assert catalog.models[0].difficulties == (
        Difficulty.AUTO,
        Difficulty.LOW,
        Difficulty.XHIGH,
    )
    _, sent, _, _ = commands.calls[0]
    assert '"method": "model/list"' in sent


@pytest.mark.parametrize(
    ("driver", "credential", "payload", "expected_model", "secret_header"),
    [
        (
            DriverId.OPENAI_API,
            "openai-private",
            {"data": [{"id": "gpt-proof"}, {"id": "text-embedding-3-small"}]},
            "gpt-proof",
            "Authorization",
        ),
        (
            DriverId.ANTHROPIC_API,
            "anthropic-private",
            {"data": [{"id": "claude-account", "display_name": "Claude"}]},
            "claude-account",
            "x-api-key",
        ),
        (
            DriverId.GEMINI_API,
            "gemini-private",
            {
                "models": [
                    {
                        "name": "models/gemini-account",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/embed-only",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
            "gemini-account",
            "x-goog-api-key",
        ),
    ],
)
def test_api_model_discovery_is_live_account_aware_and_secret_safe(
    tmp_path, driver, credential, payload, expected_model, secret_header
):
    http = FakeHttp(HttpResponse(200, json.dumps(payload).encode()))
    credentials = FakeCredentials({(driver, CredentialSource.ENVIRONMENT): credential})
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=http,
        credentials=credentials,
        path_manager=FakePathManager(),
    )
    catalog = core.discover_models(driver)
    assert catalog.source is DiscoverySource.LIVE_ACCOUNT
    assert [item.model_id for item in catalog.models] == [expected_model]
    method, url, headers, _, _ = http.calls[0]
    assert method == "GET"
    assert credential not in url
    assert headers[secret_header].endswith(credential)
    assert credential not in repr(catalog)
    status = core.inspect_driver(driver)
    assert status.authentication is AuthenticationState.AUTHENTICATED
    assert credential not in repr(status)


def test_api_missing_or_rejected_credential_is_not_reported_ready(tmp_path):
    http = FakeHttp(HttpResponse(401, b'{"error":"contains private provider text"}'))
    credentials = FakeCredentials(
        {(DriverId.OPENAI_API, CredentialSource.ENVIRONMENT): "private-key"}
    )
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=http,
        credentials=credentials,
        path_manager=FakePathManager(),
    )
    rejected = core.inspect_driver(DriverId.OPENAI_API)
    assert rejected.authentication is AuthenticationState.REQUIRED
    assert "private" not in rejected.detail
    missing = core.inspect_driver(
        DriverId.GEMINI_API,
        preference=DriverPreference(DriverId.GEMINI_API, CredentialSource.ENVIRONMENT),
    )
    assert missing.authentication is AuthenticationState.REQUIRED


def test_install_requires_exact_plan_consent_and_user_local_allowlist(tmp_path):
    executables = FakeExecutables({"npm": "/tools/npm", "node": "/tools/node"})

    def handler(argv, input_text):
        if argv == ("/tools/node", "--version"):
            return CommandResult(0, "v22.8.0")
        if argv[:5] == (
            "/tools/npm",
            "install",
            "--global",
            "--prefix",
            str(tmp_path / "home" / ".local"),
        ):
            executables.values["claude"] = str(
                tmp_path / "home" / ".local" / "bin" / "claude"
            )
            return CommandResult(0, "installed")
        if argv[-1] == "--version":
            return CommandResult(0, "Claude Code 2.0")
        if argv[-3:] == ("status", "--text"):
            return CommandResult(1, "login required")
        raise AssertionError(argv)

    paths = FakePathManager()
    core = service(
        tmp_path,
        commands=FakeCommands(handler),
        executables=executables,
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=paths,
    )
    plan = core.preview_install(DriverId.CLAUDE_CLI)
    assert plan.state is SetupActionState.AVAILABLE
    assert plan.commands[0].argv == (
        "/tools/npm",
        "install",
        "--global",
        "--prefix",
        str(tmp_path / "home" / ".local"),
        "@anthropic-ai/claude-code",
    )
    with pytest.raises(InstallConsentError):
        core.execute_install(plan, consent_token="wrong")

    result = core.execute_install(plan, consent_token=plan.consent_token)
    assert result.attempted
    assert result.succeeded
    assert result.status.installation is InstallationState.INSTALLED
    assert paths.paths == [tmp_path / "home" / ".local" / "bin"]


def test_install_preview_rejects_old_node_without_running_install(tmp_path):
    commands = FakeCommands(lambda argv, input_text: CommandResult(0, "v20.0.0"))
    plan = service(
        tmp_path,
        commands=commands,
        executables=FakeExecutables({"npm": "/bin/npm", "node": "/bin/node"}),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    ).preview_install(DriverId.COPILOT_CLI)
    assert plan.state is SetupActionState.UNSUPPORTED
    assert "22" in plan.detail
    assert len(commands.calls) == 1


def test_install_preview_does_not_accept_executable_name_collision(tmp_path):
    commands = FakeCommands(
        lambda argv, input_text: (
            CommandResult(0, "AWS deployment helper")
            if argv == ("/wrong/copilot", "help")
            else CommandResult(0, "v22.0.0")
        )
    )
    plan = service(
        tmp_path,
        commands=commands,
        executables=FakeExecutables(
            {
                "copilot": "/wrong/copilot",
                "npm": "/bin/npm",
                "node": "/bin/node",
            }
        ),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    ).preview_install(DriverId.COPILOT_CLI)
    assert plan.state is SetupActionState.AVAILABLE
    assert plan.commands[0].argv[-1] == "@github/copilot"


def test_store_credential_is_one_shot_and_never_part_of_config(tmp_path):
    credentials = FakeCredentials()
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=credentials,
        path_manager=FakePathManager(),
    )
    secret = "api-super-secret"
    submission = SecretSubmission(secret)
    core.store_credential(
        DriverId.ANTHROPIC_API,
        CredentialSource.CREDENTIAL_STORE,
        submission,
    )
    assert (
        credentials.values[(DriverId.ANTHROPIC_API, CredentialSource.CREDENTIAL_STORE)]
        == secret
    )
    assert secret not in repr(core.config_store.load())


def test_task_policy_uses_task_override_catalog_and_validates_difficulty(tmp_path):
    store = MachineProviderConfigStore(tmp_path / "providers.json")
    initial = store.load()
    config = replace(
        initial.config,
        primary_driver=DriverId.OPENAI_API,
        tasks=(),
    )
    settings = store.save(config, expected_revision=0)
    core = ProviderService(
        config_store=store,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
        home=tmp_path / "home",
        environment={"PATH": "/usr/bin"},
    )
    catalog = ModelCatalog(
        DriverId.OPENAI_API,
        models=(
            ModelDescriptor("gpt-mini", "Mini", (Difficulty.AUTO, Difficulty.LOW)),
            ModelDescriptor("gpt-sol", "Sol", (Difficulty.AUTO, Difficulty.HIGH)),
        ),
        source=DiscoverySource.LIVE_ACCOUNT,
        contract_approved=True,
    )
    proof = core.recommend_task_policy(
        TaskKind.PROOF, settings=settings, catalog=catalog
    )
    assert proof.model == "gpt-sol"
    assert proof.difficulty is Difficulty.HIGH
    reporting = core.recommend_task_policy(
        TaskKind.REPORTING, settings=settings, catalog=catalog
    )
    assert reporting.model == "gpt-mini"
    assert reporting.difficulty is Difficulty.LOW

    with pytest.raises(UnsupportedDifficultyError):
        core.validate_difficulty(DriverId.GEMINI_API, "gemini", Difficulty.XHIGH)


def test_claude_catalog_is_non_live_and_documents_current_aliases(tmp_path):
    catalog = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    ).discover_models(DriverId.CLAUDE_CLI)

    assert catalog.source is DiscoverySource.CURATED_FALLBACK
    assert not catalog.live
    assert "not a live catalog" in catalog.detail
    assert [item.model_id for item in catalog.models] == [
        "best",
        "fable",
        "opus",
        "sonnet",
        "haiku",
    ]
    by_id = {item.model_id: item for item in catalog.models}
    assert "Fable when entitled; otherwise Opus" in by_id["best"].display_name
    assert "entitlement required" in by_id["fable"].display_name
    assert all(
        item.difficulties
        == (
            Difficulty.AUTO,
            Difficulty.LOW,
            Difficulty.MEDIUM,
            Difficulty.HIGH,
            Difficulty.XHIGH,
            Difficulty.MAX,
        )
        for item in catalog.models
    )


@pytest.mark.parametrize(
    ("version", "expected_models"),
    (
        ("2.1.169 (Claude Code)", ("opus", "sonnet", "haiku")),
        (
            "2.1.170 (Claude Code)",
            ("best", "fable", "opus", "sonnet", "haiku"),
        ),
    ),
)
def test_claude_inspection_gates_fable_aliases_by_cli_version(
    tmp_path, version, expected_models
):
    def handler(argv, input_text):
        del input_text
        if argv == ("/tools/claude", "--version"):
            return CommandResult(0, version)
        if argv == ("/tools/claude", "auth", "status", "--text"):
            return CommandResult(0, "logged in")
        raise AssertionError(argv)

    status = service(
        tmp_path,
        commands=FakeCommands(handler),
        executables=FakeExecutables({"claude": "/tools/claude"}),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    ).inspect_driver(DriverId.CLAUDE_CLI)

    assert status.ready
    assert status.catalog is not None
    assert tuple(model.model_id for model in status.catalog.models) == expected_models
    if "fable" not in expected_models:
        assert "2.1.170 or newer" in status.catalog.detail


def test_machine_defaults_reject_fable_after_claude_cli_downgrade(tmp_path):
    from proof_assistant.workflow.service import ProofAssistantWorkflow

    def handler(argv, input_text):
        del input_text
        if argv == ("/tools/claude", "--version"):
            return CommandResult(0, "2.1.169 (Claude Code)")
        if argv == ("/tools/claude", "auth", "status", "--text"):
            return CommandResult(0, "logged in")
        raise AssertionError(argv)

    core = service(
        tmp_path,
        commands=FakeCommands(handler),
        executables=FakeExecutables({"claude": "/tools/claude"}),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    )
    current = core.config_store.load()
    preferences = tuple(
        replace(
            preference,
            model="fable",
            difficulty=Difficulty.HIGH,
        )
        if preference.driver is DriverId.CLAUDE_CLI
        else preference
        for preference in current.config.drivers
    )
    core.config_store.save(
        replace(
            current.config,
            primary_driver=DriverId.CLAUDE_CLI,
            drivers=preferences,
        ),
        expected_revision=current.revision,
    )
    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog.json",
        cache_home=str(tmp_path / "cache"),
        machine_config_path=tmp_path / "machine" / "settings.yaml",
        provider_service=core,
        use_codex_clarification=False,
    )

    with pytest.raises(ProviderConfigError, match="not present"):
        workflow.default_verification_settings()


@pytest.mark.parametrize(
    ("task", "expected_model", "expected_difficulty"),
    [
        (TaskKind.PROOF, "best", Difficulty.HIGH),
        (TaskKind.DUPLICATE_PROOF, "fable", Difficulty.XHIGH),
        (TaskKind.CLARIFICATION, "opus", Difficulty.HIGH),
        (TaskKind.DIAGNOSTIC, "opus", Difficulty.HIGH),
        (TaskKind.REVIEW, "opus", Difficulty.HIGH),
        (TaskKind.SKETCH, "sonnet", Difficulty.MEDIUM),
        (TaskKind.MAINTENANCE, "sonnet", Difficulty.MEDIUM),
        (TaskKind.REPORTING, "haiku", Difficulty.LOW),
    ],
)
def test_claude_task_policy_routes_by_task(
    tmp_path, task, expected_model, expected_difficulty
):
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    )
    current = core.config_store.load()
    settings = core.config_store.save(
        replace(current.config, primary_driver=DriverId.CLAUDE_CLI),
        expected_revision=current.revision,
    )
    policy = core.recommend_task_policy(task, settings=settings)

    assert policy.model == expected_model
    assert policy.difficulty is expected_difficulty


def test_role_recommendation_never_selects_none(tmp_path):
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    )
    current = core.config_store.load()
    catalog = ModelCatalog(
        driver=DriverId.COPILOT_CLI,
        models=(
            ModelDescriptor(
                "auto",
                "Automatic",
                (Difficulty.NONE, Difficulty.AUTO),
            ),
        ),
        source=DiscoverySource.CURATED_FALLBACK,
        contract_approved=True,
    )
    settings = replace(
        current,
        config=replace(current.config, primary_driver=DriverId.COPILOT_CLI),
    )

    policy = core.recommend_task_policy(
        TaskKind.REPORTING, settings=settings, catalog=catalog
    )

    assert policy.difficulty is Difficulty.AUTO


@pytest.mark.parametrize(
    ("models", "expected_model"),
    [
        (("fable", "opus"), "fable"),
        (("opus", "sonnet"), "opus"),
        (("sonnet", "haiku"), "sonnet"),
    ],
)
def test_claude_proof_policy_falls_back_deterministically(
    tmp_path, models, expected_model
):
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    )
    current = core.config_store.load()
    settings = core.config_store.save(
        replace(current.config, primary_driver=DriverId.CLAUDE_CLI),
        expected_revision=current.revision,
    )
    catalog = ModelCatalog(
        driver=DriverId.CLAUDE_CLI,
        models=tuple(
            ModelDescriptor(model_id, model_id, (Difficulty.AUTO, Difficulty.HIGH))
            for model_id in models
        ),
        source=DiscoverySource.CURATED_FALLBACK,
        contract_approved=True,
    )

    policy = core.recommend_task_policy(
        TaskKind.PROOF, settings=settings, catalog=catalog
    )
    assert policy.model == expected_model


def test_claude_task_model_override_wins_over_provider_override(tmp_path):
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    )
    current = core.config_store.load()
    preferences = tuple(
        replace(item, model="sonnet") if item.driver is DriverId.CLAUDE_CLI else item
        for item in current.config.drivers
    )
    settings = core.config_store.save(
        replace(
            current.config,
            primary_driver=DriverId.CLAUDE_CLI,
            drivers=preferences,
            tasks=(TaskPreference(TaskKind.PROOF, model="haiku"),),
        ),
        expected_revision=current.revision,
    )

    policy = core.recommend_task_policy(TaskKind.PROOF, settings=settings)
    assert policy.model == "haiku"
    assert policy.difficulty is Difficulty.HIGH
    assert policy.explanation == "Uses an explicit machine/task override."


def test_provider_default_matrix_ignores_stale_global_and_task_overrides(tmp_path):
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    )
    current = core.config_store.load()
    drivers = tuple(
        replace(item, model="haiku", difficulty=Difficulty.LOW)
        if item.driver is DriverId.CLAUDE_CLI
        else item
        for item in current.config.drivers
    )
    settings = replace(
        current,
        config=replace(
            current.config,
            primary_driver=DriverId.CLAUDE_CLI,
            drivers=drivers,
            tasks=(
                TaskPreference(
                    TaskKind.PROOF,
                    driver=DriverId.CODEX_CLI,
                    model="gpt-5.6-luna",
                    difficulty=Difficulty.LOW,
                ),
            ),
        ),
    )

    policies = core.recommend_driver_task_policies(
        DriverId.CLAUDE_CLI, settings=settings
    )

    resolved = {
        item.task: (item.driver, item.model, item.difficulty) for item in policies
    }
    assert set(resolved) == set(TaskKind)
    assert resolved[TaskKind.PROOF] == (
        DriverId.CLAUDE_CLI,
        "best",
        Difficulty.HIGH,
    )
    assert resolved[TaskKind.DUPLICATE_PROOF] == (
        DriverId.CLAUDE_CLI,
        "fable",
        Difficulty.XHIGH,
    )
    assert resolved[TaskKind.REPORTING] == (
        DriverId.CLAUDE_CLI,
        "haiku",
        Difficulty.LOW,
    )


def test_setup_snapshot_requires_auth_and_contract_approved_catalog(tmp_path):
    # No executables and no keys: all returned statuses are sanitized and the
    # configured primary is correctly not ready.
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(HttpResponse(500, b"")),
        credentials=FakeCredentials(),
        path_manager=FakePathManager(),
    )
    snapshot = core.get_setup_snapshot()
    assert len(snapshot.statuses) == 6
    assert snapshot.primary_driver is DriverId.CODEX_CLI
    assert not snapshot.primary_ready
    assert all("secret" not in repr(item).casefold() for item in snapshot.statuses)


def test_live_anthropic_catalog_uses_per_model_effort_capabilities(tmp_path):
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(
            HttpResponse(
                200,
                json.dumps(
                    {
                        "data": [
                            {
                                "id": "claude-capable",
                                "display_name": "Capable Claude",
                                "capabilities": {
                                    "effort": {
                                        "supported": True,
                                        "low": {"supported": True},
                                        "medium": {"supported": False},
                                        "high": {"supported": True},
                                        "xhigh": None,
                                        "max": {"supported": False},
                                    }
                                },
                            }
                        ]
                    }
                ).encode(),
            )
        ),
        credentials=FakeCredentials(
            {
                (
                    DriverId.ANTHROPIC_API,
                    CredentialSource.ENVIRONMENT,
                ): "secret"
            }
        ),
        path_manager=FakePathManager(),
    )
    catalog = core.discover_models(DriverId.ANTHROPIC_API)
    assert catalog.source is DiscoverySource.LIVE_ACCOUNT
    assert catalog.models[0].difficulties == (
        Difficulty.AUTO,
        Difficulty.LOW,
        Difficulty.HIGH,
    )


def test_live_catalog_does_not_invent_unknown_openai_effort_contract(tmp_path):
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(
            HttpResponse(
                200,
                json.dumps(
                    {
                        "data": [
                            {"id": "unknown-future-text-model"},
                            {"id": "gpt-5.6-sol"},
                        ]
                    }
                ).encode(),
            )
        ),
        credentials=FakeCredentials(
            {(DriverId.OPENAI_API, CredentialSource.ENVIRONMENT): "secret"}
        ),
        path_manager=FakePathManager(),
    )
    catalog = core.discover_models(DriverId.OPENAI_API)
    by_id = {item.model_id: item for item in catalog.models}
    assert by_id["unknown-future-text-model"].difficulties == (Difficulty.AUTO,)
    assert Difficulty.MAX in by_id["gpt-5.6-sol"].difficulties


def test_live_catalog_drives_auto_policy_and_rejects_unavailable_override(tmp_path):
    credentials = FakeCredentials(
        {(DriverId.OPENAI_API, CredentialSource.ENVIRONMENT): "test-credential"}
    )
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(
            HttpResponse(
                200,
                json.dumps({"data": [{"id": "account-only-model"}]}).encode(),
            )
        ),
        credentials=credentials,
        path_manager=FakePathManager(),
    )
    current = core.config_store.load()
    automatic = replace(current.config, primary_driver=DriverId.OPENAI_API)
    core.config_store.save(automatic, expected_revision=current.revision)

    snapshot = core.get_setup_snapshot()
    assert snapshot.primary_ready
    catalog = next(
        status.catalog
        for status in snapshot.statuses
        if status.driver is DriverId.OPENAI_API
    )
    assert catalog is not None
    policy = core.recommend_task_policy(
        TaskKind.PROOF,
        settings=snapshot.settings,
        catalog=catalog,
    )
    assert policy.model == "account-only-model"
    assert policy.difficulty is Difficulty.AUTO

    preferences = tuple(
        replace(item, model="not-in-account-catalog")
        if item.driver is DriverId.OPENAI_API
        else item
        for item in automatic.drivers
    )
    invalid = replace(automatic, drivers=preferences)
    with pytest.raises(ProviderConfigError, match="not present"):
        core.validate_config(invalid)

    core.config_store.save(
        invalid,
        expected_revision=core.config_store.load().revision,
    )
    unavailable = core.get_setup_snapshot()
    assert not unavailable.primary_ready
    assert "not present" in unavailable.detail


def test_workflow_defaults_use_the_live_primary_catalog(tmp_path):
    from proof_assistant.workflow.service import ProofAssistantWorkflow

    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(
            HttpResponse(
                200,
                json.dumps({"data": [{"id": "account-only-model"}]}).encode(),
            )
        ),
        credentials=FakeCredentials(
            {(DriverId.OPENAI_API, CredentialSource.ENVIRONMENT): "test-credential"}
        ),
        path_manager=FakePathManager(),
    )
    current = core.config_store.load()
    core.config_store.save(
        replace(current.config, primary_driver=DriverId.OPENAI_API),
        expected_revision=current.revision,
    )
    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog.json",
        cache_home=str(tmp_path / "cache"),
        machine_config_path=tmp_path / "machine" / "settings.yaml",
        provider_service=core,
        use_codex_clarification=False,
    )
    defaults = workflow.default_verification_settings()
    assert defaults.ai_driver == DriverId.OPENAI_API.value
    assert defaults.model == "account-only-model"
    assert defaults.effort == Difficulty.AUTO.value


def test_live_gemini_catalog_only_exposes_implemented_difficulty_mappings(tmp_path):
    core = service(
        tmp_path,
        commands=FakeCommands(),
        executables=FakeExecutables(),
        http=FakeHttp(
            HttpResponse(
                200,
                json.dumps(
                    {
                        "models": [
                            {
                                "name": "models/gemini-2.5-pro",
                                "supportedGenerationMethods": ["generateContent"],
                                "thinking": True,
                            },
                            {
                                "name": "models/gemini-3.1-flash-lite-image",
                                "supportedGenerationMethods": ["generateContent"],
                                "thinking": True,
                            },
                            {
                                "name": "models/gemini-nonthinking",
                                "supportedGenerationMethods": ["generateContent"],
                                "thinking": False,
                            },
                        ]
                    }
                ).encode(),
            )
        ),
        credentials=FakeCredentials(
            {(DriverId.GEMINI_API, CredentialSource.ENVIRONMENT): "secret"}
        ),
        path_manager=FakePathManager(),
    )
    catalog = core.discover_models(DriverId.GEMINI_API)
    by_id = {item.model_id: item for item in catalog.models}
    assert by_id["gemini-2.5-pro"].difficulties == (
        Difficulty.AUTO,
        Difficulty.LOW,
        Difficulty.MEDIUM,
        Difficulty.HIGH,
    )
    assert by_id["gemini-3.1-flash-lite-image"].difficulties == (
        Difficulty.AUTO,
        Difficulty.HIGH,
    )
    assert by_id["gemini-nonthinking"].difficulties == (Difficulty.AUTO,)
