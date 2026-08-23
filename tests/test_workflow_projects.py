from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

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
        NewProjectRequest("My Manuscript", source, project)
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
    assert workflow.list_projects()[0].project_path == project
    assert workflow.plan_changes(project) is None


def test_dropbox_source_warns_but_managed_dropbox_project_is_rejected(tmp_path):
    dropbox = tmp_path / "Dropbox (Personal)"
    dropbox.mkdir()
    source = source_without_task(dropbox)
    workflow = service(tmp_path)
    project = tmp_path / "managed"
    snapshot = workflow.create_project(
        NewProjectRequest("Dropbox Paper", source, project)
    )
    assert snapshot.project.source_in_dropbox is True

    with pytest.raises(ManagedProjectPathError, match="cannot reside in Dropbox"):
        workflow.create_project(
            NewProjectRequest("Bad", source, dropbox / "managed-project")
        )


def test_custom_task_text_is_validated_and_stored_as_project_yaml(tmp_path):
    source = source_without_task(tmp_path)
    workflow = service(tmp_path)
    project = tmp_path / "managed"
    workflow.create_project(
        NewProjectRequest(
            "Custom",
            source,
            project,
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
    workflow.create_project(NewProjectRequest("Legacy", source, project))
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
