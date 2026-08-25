from types import SimpleNamespace

import pytest

from proof_assistant.backend import CodexToolCall
from proof_assistant.cli import (
    _run_status_is_terminal,
    _target_declaration,
    _verify_repoprover_proof,
)


def test_bare_parser_defaults_to_tui_at_main_dispatch_boundary():
    from proof_assistant.cli import build_parser

    args = build_parser().parse_args([])
    assert args.command is None


def test_explicit_tui_command_is_available():
    from proof_assistant.cli import build_parser

    args = build_parser().parse_args(["tui"])
    assert args.func.__name__ == "cmd_tui"


def test_version_is_product_version(capsys):
    from proof_assistant.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out == "Proof Assistant 0.1.0\n"


class FakeAgent:
    def __init__(self, result="Compiles successfully"):
        self.result = result
        self.calls = []

    def handle_tool_call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def wrapped_with(*calls):
    return SimpleNamespace(codex=SimpleNamespace(tool_calls=list(calls)))


LEAN_OK = CodexToolCall(
    name="lean_check",
    arguments={"code": "example : True := by trivial"},
    result="Compiles successfully",
    success=True,
)


def test_target_declaration_stops_at_next_theorem():
    source = "theorem first : True := by trivial\n\ntheorem second : True := by sorry\n"
    assert "sorry" not in _target_declaration(source, "first")


def test_final_proof_verification_accepts_compiled_target(tmp_path):
    lean_file = tmp_path / "Toy.lean"
    lean_file.write_text("theorem toy_theorem : True := by trivial\n")
    outcome, detail = _verify_repoprover_proof(
        FakeAgent(), wrapped_with(LEAN_OK), lean_file, "toy_theorem"
    )
    assert outcome == "proved"
    assert detail == "Compiles successfully"


def test_unproved_is_not_reported_as_false(tmp_path):
    lean_file = tmp_path / "Toy.lean"
    lean_file.write_text("theorem toy_theorem : True := by sorry\n")
    outcome, detail = _verify_repoprover_proof(
        FakeAgent(), wrapped_with(LEAN_OK), lean_file, "toy_theorem"
    )
    assert outcome == "unproved"
    assert "still contains" in detail


def test_missing_theorem_is_formalization_mismatch(tmp_path):
    lean_file = tmp_path / "Toy.lean"
    lean_file.write_text("theorem another_theorem : True := by trivial\n")
    outcome, _detail = _verify_repoprover_proof(
        FakeAgent(), wrapped_with(LEAN_OK), lean_file, "toy_theorem"
    )
    assert outcome == "formalization_mismatch"


def test_failed_lean_check_is_tool_failure(tmp_path):
    lean_file = tmp_path / "Toy.lean"
    lean_file.write_text("theorem toy_theorem : True := by trivial\n")
    failed = CodexToolCall(
        name="lean_check",
        arguments={"code": "bad"},
        result="Error: rejected",
        success=False,
    )
    outcome, _detail = _verify_repoprover_proof(
        FakeAgent(), wrapped_with(failed), lean_file, "toy_theorem"
    )
    assert outcome == "tool_failure"


def test_terminal_run_status_cannot_be_replaced_by_cleanup_progress(tmp_path):
    status = tmp_path / "RUN_STATUS.json"
    assert not _run_status_is_terminal(status)

    status.write_text('{"outcome": "running", "phase": "codex_turn"}')
    assert not _run_status_is_terminal(status)

    status.write_text('{"outcome": "unverified", "exit_code": 4}')
    assert _run_status_is_terminal(status)


def test_ai_status_for_one_driver_does_not_probe_every_provider(
    monkeypatch, capsys, tmp_path
):
    import proof_assistant.cli as cli
    from proof_assistant.ai import (
        AuthenticationState,
        Difficulty,
        DiscoverySource,
        DriverId,
        DriverStatus,
        DriverTransport,
        InstallationState,
        MachineProviderSettings,
        ModelCatalog,
        ModelDescriptor,
        TaskKind,
        TaskModelPolicy,
    )

    settings = MachineProviderSettings()
    status = DriverStatus(
        driver=DriverId.CLAUDE_CLI,
        transport=DriverTransport.CLI,
        installation=InstallationState.INSTALLED,
        authentication=AuthenticationState.AUTHENTICATED,
        executable="/bin/claude",
        version="Claude Code 2.0",
        catalog=ModelCatalog(
            driver=DriverId.CLAUDE_CLI,
            models=(ModelDescriptor("sonnet", "Sonnet"),),
            source=DiscoverySource.CURATED_FALLBACK,
            contract_approved=True,
        ),
    )

    class FakeConfigStore:
        path = tmp_path / "providers.json"

        def load(self):
            return settings

    class FakeService:
        config_store = FakeConfigStore()

        def get_setup_snapshot(self):
            raise AssertionError("single-driver status must not probe every provider")

        def inspect_driver(self, driver, *, preference):
            assert driver is DriverId.CLAUDE_CLI
            assert preference.driver is driver
            return status

        def recommend_task_policy(self, task, *, settings, catalog):
            assert task is TaskKind.PROOF
            assert settings.config.primary_driver is DriverId.CLAUDE_CLI
            assert catalog is status.catalog
            return TaskModelPolicy(
                task=task,
                driver=DriverId.CLAUDE_CLI,
                model="sonnet",
                difficulty=Difficulty.HIGH,
                model_source=DiscoverySource.CURATED_FALLBACK,
                explanation="test",
            )

    monkeypatch.setattr(cli, "_ai_provider_service", lambda: FakeService())
    result = cli.cmd_ai_status(SimpleNamespace(driver=DriverId.CLAUDE_CLI.value))
    assert result == 0
    output = capsys.readouterr().out
    assert "selected ready: yes" in output
    assert "claude_cli" in output
    assert "openai_api" not in output
