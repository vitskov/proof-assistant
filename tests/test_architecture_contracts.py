from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import proof_assistant
from proof_assistant.workflow.contracts import (
    NewProjectRequest,
    ProjectAvailability,
    ProjectCatalogEntry,
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


def test_tui_cannot_bypass_project_management_backend():
    violations: list[str] = []
    tui = ROOT / "src" / "proof_assistant" / "tui"
    forbidden = {
        "proof_assistant.incremental",
        "proof_assistant.workspace",
    }
    for path in tui.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(
                name == prefix or name.startswith(prefix + ".")
                for name in names
                for prefix in forbidden
            ):
                violations.append(str(path.relative_to(ROOT)))
    assert violations == []


def test_project_management_is_a_distinct_backend_component():
    service = (ROOT / "src/proof_assistant/workflow/service.py").read_text(
        encoding="utf-8"
    )
    management = ROOT / "src/proof_assistant/workspace/management.py"
    assert management.is_file()
    assert "ProjectManager" in service
    assert "default_project_path" not in service


def test_project_management_import_is_clean_in_a_fresh_interpreter():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from proof_assistant.workspace.management import ProjectManager",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_workflow_contract_values_are_json_serializable(tmp_path):
    request = NewProjectRequest(
        name="paper",
        source_path=tmp_path / "source",
        main_file="main.tex",
        settings=VerificationSettings(model="model"),
    )
    payload = contract_dict(request)
    assert payload["source_path"] == str(tmp_path / "source")
    json.dumps(payload)


def test_project_catalog_contract_rejects_impossible_tagged_states(tmp_path):
    with pytest.raises(ValueError, match="RESUMABLE"):
        ProjectCatalogEntry(
            name="broken",
            project_path=tmp_path / "broken",
            availability=ProjectAvailability.RESUMABLE,
        )
    with pytest.raises(ValueError, match="source path and candidate"):
        ProjectCatalogEntry(
            name="legacy",
            project_path=tmp_path / "legacy",
            availability=ProjectAvailability.NEEDS_MAIN_FILE,
        )


def test_distribution_identity_and_version_are_consistent():
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = configuration["project"]
    assert project["name"] == "proof-assistant"
    assert project["version"] == proof_assistant.__version__ == "0.1.0"
    assert project["license"] == "CC-BY-NC-4.0"
    assert project["scripts"]["proof-assistant"] == "proof_assistant.cli:main"
    assert project["scripts"]["repoprover-codex"] == "proof_assistant.cli:main"
    assert "textual>=1,<2" in project["dependencies"]
