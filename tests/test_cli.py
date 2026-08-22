from types import SimpleNamespace

from repoprover_codex.backend import CodexToolCall
from repoprover_codex.cli import _target_declaration, _verify_repoprover_proof


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
