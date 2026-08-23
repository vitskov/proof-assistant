from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import proof_assistant
from proof_assistant.workflow.contracts import (
    NewProjectRequest,
    VerificationSettings,
    contract_dict,
)

ROOT = Path(__file__).parents[1]


def test_non_tui_code_has_no_textual_or_rich_imports():
    violations: list[str] = []
    package = ROOT / "src" / "proof_assistant"
    for path in package.rglob("*.py"):
        if "tui" in path.relative_to(package).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".", 1)[0] in {"rich", "textual"} for name in names):
                violations.append(str(path.relative_to(ROOT)))
    assert violations == []


def test_workflow_contract_values_are_json_serializable(tmp_path):
    request = NewProjectRequest(
        name="paper",
        source_path=tmp_path / "source",
        settings=VerificationSettings(model="model"),
    )
    payload = contract_dict(request)
    assert payload["source_path"] == str(tmp_path / "source")
    json.dumps(payload)


def test_distribution_identity_and_version_are_consistent():
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = configuration["project"]
    assert project["name"] == "proof-assistant"
    assert project["version"] == proof_assistant.__version__ == "0.1.0"
    assert project["scripts"]["proof-assistant"] == "proof_assistant.cli:main"
    assert project["scripts"]["repoprover-codex"] == "proof_assistant.cli:main"
    assert "textual>=1,<2" in project["dependencies"]
