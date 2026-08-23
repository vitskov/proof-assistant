from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from proof_assistant.incremental.session import (
    IncrementalProjectError,
    IncrementalSession,
)
from proof_assistant.incremental.store import StateStore
from proof_assistant.workflow.contracts import NewProjectRequest, WorkflowState
from proof_assistant.workflow.service import ProofAssistantWorkflow
from proof_assistant.workspace.paths import ManagedProjectPathError

FIXTURE = Path(__file__).parent / "fixtures" / "incremental_manuscript"


def source_without_task(root: Path) -> Path:
    source = root / "paper"
    shutil.copytree(FIXTURE, source)
    (source / "VERIFY.yaml").unlink()
    return source


def service(root: Path, **kwargs) -> ProofAssistantWorkflow:
    return ProofAssistantWorkflow(
        catalog_root=root / "catalog",
        use_codex_clarification=False,
        **kwargs,
    )


def test_new_project_owns_default_task_and_catalog_is_disposable(tmp_path):
    source = source_without_task(tmp_path)
    workflow = service(tmp_path)
    project = tmp_path / "managed"
    snapshot = workflow.create_project(
        NewProjectRequest("My Manuscript", source, "main.tex", project_path=project)
    )

    assert snapshot.state == WorkflowState.PROJECT_READY
    assert snapshot.project.name == "My Manuscript"
    task = project / "VERIFY.yaml"
    assert task.is_file()
    task_payload = yaml.safe_load(task.read_text(encoding="utf-8"))
    assert task_payload["instructions"] == workflow.default_task_text()
    config = json.loads(
        (project / ".repoprover/config.json").read_text(encoding="utf-8")
    )
    assert config["task_file"] == "VERIFY.yaml"
    assert config["main_file"] == "main.tex"
    assert config["input_files"] == []
    assert snapshot.project.main_file == "main.tex"
    assert snapshot.project.input_files == ()
    with StateStore(project / ".repoprover/state.sqlite3") as store:
        assert store.get_metadata("main_file") == "main.tex"
        assert json.loads(store.get_metadata("input_files") or "null") == []
    assert workflow.list_projects()[0].project_path == project
    assert workflow.plan_changes(project) is None


def test_source_inspection_lists_all_candidates_and_suggests_document_root(tmp_path):
    source = tmp_path / "paper"
    (source / "sections").mkdir(parents=True)
    (source / "main.tex").write_text(
        r"\documentclass{article}\input{sections/body}", encoding="utf-8"
    )
    (source / "sections/body.tex").write_text("body", encoding="utf-8")
    inspection = service(tmp_path).inspect_source(source)
    assert [item.relative_path for item in inspection.candidates] == [
        "main.tex",
        "sections/body.tex",
    ]
    assert inspection.suggested_main_file == "main.tex"
    assert inspection.selection_required is True


def test_source_inspection_prefers_conventional_root_name_without_selecting_it(
    tmp_path,
):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "alternate.tex").write_text(r"\documentclass{article}", encoding="utf-8")
    (source / "paper.tex").write_text(r"\documentclass{article}", encoding="utf-8")
    inspection = service(tmp_path).inspect_source(source)
    assert inspection.suggested_main_file == "paper.tex"
    assert inspection.selection_required is True


def test_source_inspection_without_latex_has_actionable_error(tmp_path):
    source = tmp_path / "paper"
    source.mkdir()
    (source / "README.md").write_text("nothing", encoding="utf-8")
    with pytest.raises(ValueError, match="No \\.tex or \\.ltx files"):
        service(tmp_path).inspect_source(source)


def test_project_persists_recursive_inputs_and_summary_contract(tmp_path):
    source = tmp_path / "paper"
    (source / "parts").mkdir(parents=True)
    (source / "main.tex").write_text(
        r"\input{parts/a}\begin{theorem}\label{root}Root.\end{theorem}",
        encoding="utf-8",
    )
    (source / "parts/a.tex").write_text(
        r"\input{b}\begin{lemma}\label{a}A.\end{lemma}", encoding="utf-8"
    )
    (source / "parts/b.tex").write_text(
        r"\begin{lemma}\label{b}B.\end{lemma}", encoding="utf-8"
    )
    workflow = service(tmp_path)
    project = tmp_path / "managed"
    created = workflow.create_project(
        NewProjectRequest("Nested", source, "main.tex", project_path=project)
    )
    assert created.project.input_files == ("parts/a.tex", "parts/b.tex")
    config = json.loads(
        (project / ".repoprover/config.json").read_text(encoding="utf-8")
    )
    assert config["main_file"] == "main.tex"
    assert config["input_files"] == ["parts/a.tex", "parts/b.tex"]
    resumed = workflow.resume_project(project)
    assert resumed.project.main_file == "main.tex"
    assert resumed.project.input_files == ("parts/a.tex", "parts/b.tex")


def test_legacy_main_file_migration_is_only_automatic_when_unambiguous(tmp_path):
    source = source_without_task(tmp_path)
    workflow = service(tmp_path)
    project = tmp_path / "managed"
    workflow.create_project(
        NewProjectRequest("Legacy", source, "main.tex", project_path=project)
    )
    config_path = project / ".repoprover/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["schema_version"] = 1
    config.pop("main_file")
    config.pop("input_files")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    migrated = IncrementalSession(project)._load_config()
    assert migrated["schema_version"] == 2
    assert migrated["main_file"] == "main.tex"
    assert IncrementalSession(project)._git(["status", "--porcelain=v1"]) == ""

    (source / "main.tex").write_text(
        r"\documentclass{article}\begin{theorem}\label{x}X.\end{theorem}",
        encoding="utf-8",
    )
    (source / "other.tex").write_text(
        r"\documentclass{article}\begin{theorem}\label{y}Y.\end{theorem}",
        encoding="utf-8",
    )
    config.pop("main_file", None)
    config.pop("input_files", None)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(IncrementalProjectError, match="choice is ambiguous"):
        IncrementalSession(project)._load_config()


def test_dropbox_source_warns_but_managed_dropbox_project_is_rejected(tmp_path):
    dropbox = tmp_path / "Dropbox (Personal)"
    dropbox.mkdir()
    source = source_without_task(dropbox)
    workflow = service(tmp_path)
    project = tmp_path / "managed"
    snapshot = workflow.create_project(
        NewProjectRequest("Dropbox Paper", source, "main.tex", project_path=project)
    )
    assert snapshot.project.source_in_dropbox is True

    with pytest.raises(ManagedProjectPathError, match="cannot reside in Dropbox"):
        workflow.create_project(
            NewProjectRequest(
                "Bad",
                source,
                "main.tex",
                project_path=dropbox / "managed-project",
            )
        )


def test_custom_task_text_is_validated_and_stored_as_project_yaml(tmp_path):
    source = source_without_task(tmp_path)
    workflow = service(tmp_path)
    project = tmp_path / "managed"
    workflow.create_project(
        NewProjectRequest(
            "Custom",
            source,
            "main.tex",
            project_path=project,
            task_text="Focus especially on hidden assumptions.",
        )
    )
    text = (project / "VERIFY.yaml").read_text(encoding="utf-8")
    assert "Focus especially on hidden assumptions." in text
    assert "schema: 1" in text


def test_legacy_external_task_is_migrated_without_recreating_project(tmp_path):
    source = source_without_task(tmp_path)
    workflow = service(tmp_path)
    project = tmp_path / "managed"
    workflow.create_project(
        NewProjectRequest("Legacy", source, "main.tex", project_path=project)
    )
    task = project / "VERIFY.yaml"
    external = tmp_path / "old-task.md"
    external.write_text("Check all claims.", encoding="utf-8")
    task.unlink()
    config_path = project / ".repoprover/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["task_file"] = str(external)
    config_path.write_text(json.dumps(config), encoding="utf-8")

    resumed = workflow.resume_project(project)

    assert resumed.state == WorkflowState.CHANGE_REVIEW
    assert (project / "VERIFY.yaml").is_file()
    assert "Check all claims." in (project / "VERIFY.yaml").read_text(encoding="utf-8")
