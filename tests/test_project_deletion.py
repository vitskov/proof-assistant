from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from proof_assistant.incremental.locking import project_lock
from proof_assistant.workflow.contracts import (
    NewProjectRequest,
    ProjectDeletionAvailability,
    contract_dict,
)
from proof_assistant.workflow.service import (
    ProjectDeletionError,
    ProofAssistantWorkflow,
)
from proof_assistant.workspace.management import ProjectManager


def _source_inventory(source: Path) -> dict[str, bytes]:
    return {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }


def _managed_project(
    tmp_path: Path, *, source_in_dropbox: bool = False
) -> tuple[Path, Path, Path, ProofAssistantWorkflow]:
    source_parent = tmp_path / ("Dropbox (Personal)" if source_in_dropbox else "input")
    source = source_parent / "paper"
    source.mkdir(parents=True)
    (source / "main.tex").write_text(
        r"\documentclass{article}"
        r"\newtheorem{theorem}{Theorem}"
        r"\begin{document}"
        r"\begin{theorem}\label{T}Statement.\end{theorem}"
        r"\end{document}",
        encoding="utf-8",
    )
    trash = tmp_path / "Trash"
    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog", use_codex_clarification=False
    )
    workflow.projects = ProjectManager(workflow.catalog, trash_root=trash)
    project = tmp_path / "managed-project"
    workflow.create_project(
        NewProjectRequest(
            "Paper One",
            source,
            "main.tex",
            project_path=project,
        )
    )
    return source, project, trash, workflow


def test_recoverable_delete_moves_only_managed_project_and_refreshes_catalog(
    tmp_path,
):
    source, project, trash, workflow = _managed_project(
        tmp_path, source_in_dropbox=True
    )
    source_before = _source_inventory(source)
    assert [entry.project_path for entry in workflow.list_projects()] == [project]

    inspection = workflow.inspect_project_deletion(project)
    assert inspection.availability == ProjectDeletionAvailability.READY
    assert inspection.source_path == source
    assert inspection.source_in_dropbox is True
    assert _source_inventory(source) == source_before

    result = workflow.delete_project(project)

    assert not project.exists()
    assert result.project_path == project
    assert result.source_path == source
    assert result.recoverable is True
    assert result.trash_path.is_dir()
    assert result.trash_path.is_relative_to(trash)
    assert (result.trash_path / ".repoprover/config.json").is_file()
    assert _source_inventory(source) == source_before
    assert workflow.list_projects() == ()
    catalog_text = workflow.catalog.path.read_text(encoding="utf-8")
    assert str(project) not in catalog_text

    payload = contract_dict(result)
    copied = json.dumps(payload, sort_keys=True)
    assert str(project) in copied
    assert str(source) in copied
    assert str(result.trash_path) in copied


@pytest.mark.parametrize("kind", ["unrelated", "incomplete", "missing"])
def test_delete_refuses_every_path_that_is_not_a_resumable_managed_project(
    tmp_path, kind
):
    workflow = ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog", use_codex_clarification=False
    )
    workflow.projects = ProjectManager(workflow.catalog, trash_root=tmp_path / "Trash")
    project = tmp_path / kind
    if kind == "unrelated":
        project.mkdir()
        (project / "private.txt").write_text("do not move", encoding="utf-8")
    elif kind == "incomplete":
        (project / ".repoprover").mkdir(parents=True)

    inspection = workflow.inspect_project_deletion(project)

    assert inspection.availability == ProjectDeletionAvailability.REFUSED
    assert inspection.can_delete is False
    with pytest.raises(ProjectDeletionError):
        workflow.delete_project(project)
    if kind != "missing":
        assert project.is_dir()
    if kind == "unrelated":
        assert (project / "private.txt").read_text(encoding="utf-8") == "do not move"


def test_active_project_lock_refuses_inspection_and_delete_without_mutation(tmp_path):
    source, project, trash, workflow = _managed_project(tmp_path)
    source_before = _source_inventory(source)

    with project_lock(project, exclusive=True):
        inspection = workflow.inspect_project_deletion(project)
        assert inspection.availability == ProjectDeletionAvailability.BUSY
        assert "already in use" in (inspection.issue or "")
        with pytest.raises(ProjectDeletionError) as raised:
            workflow.delete_project(project)
        assert raised.value.inspection.availability == ProjectDeletionAvailability.BUSY
        assert project.is_dir()
        assert not trash.exists()

    assert _source_inventory(source) == source_before
    assert [entry.project_path for entry in workflow.list_projects()] == [project]


def test_delete_rechecks_lock_after_an_earlier_ready_confirmation(tmp_path):
    source, project, trash, workflow = _managed_project(tmp_path)
    reviewed = workflow.inspect_project_deletion(project)
    assert reviewed.availability == ProjectDeletionAvailability.READY

    # Simulate another process starting while the confirmation dialog is open.
    with project_lock(project, exclusive=True):
        with pytest.raises(ProjectDeletionError) as raised:
            workflow.delete_project(project)
        assert raised.value.inspection.availability == ProjectDeletionAvailability.BUSY

    assert project.is_dir()
    assert source.is_dir()
    assert not trash.exists()


def test_trash_collision_never_overwrites_existing_content(tmp_path):
    source, project, trash, workflow = _managed_project(tmp_path)
    collision = trash / project.name
    collision.mkdir(parents=True)
    sentinel = collision / "unrelated.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    source_before = _source_inventory(source)

    result = workflow.delete_project(project)

    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert result.trash_path != collision
    assert result.trash_path.is_dir()
    assert result.trash_path.parent != collision
    assert _source_inventory(source) == source_before


def test_overlapping_source_configuration_is_refused_fail_closed(tmp_path):
    original_source, project, trash, workflow = _managed_project(tmp_path)
    copied_source = project / "manuscript"
    config_path = project / ".repoprover" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["manuscript"] = str(copied_source)
    config_path.write_text(json.dumps(config), encoding="utf-8")

    inspection = workflow.inspect_project_deletion(project)

    assert inspection.availability == ProjectDeletionAvailability.REFUSED
    assert "overlap" in (inspection.issue or "")
    with pytest.raises(ProjectDeletionError):
        workflow.delete_project(project)
    assert project.is_dir()
    assert original_source.is_dir()
    assert not trash.exists()


@pytest.mark.parametrize("trash_relation", ["inside-source", "contains-source"])
def test_recovery_area_overlapping_external_source_is_refused_without_mutation(
    tmp_path, trash_relation
):
    source, project, _trash, workflow = _managed_project(tmp_path)
    source_before = _source_inventory(source)
    trash = source / "recovery" if trash_relation == "inside-source" else source.parent
    workflow.projects = ProjectManager(workflow.catalog, trash_root=trash)

    inspection = workflow.inspect_project_deletion(project)

    assert inspection.availability == ProjectDeletionAvailability.REFUSED
    assert "source and recoverable deletion area overlap" in (inspection.issue or "")
    with pytest.raises(ProjectDeletionError):
        workflow.delete_project(project)
    assert project.is_dir()
    assert _source_inventory(source) == source_before
    if trash_relation == "inside-source":
        assert not trash.exists()


def test_catalog_failure_rolls_project_back_and_preserves_source(tmp_path, monkeypatch):
    source, project, trash, workflow = _managed_project(tmp_path)
    source_before = _source_inventory(source)

    def fail_catalog(_project: Path) -> None:
        raise OSError("catalog unavailable")

    monkeypatch.setattr(workflow.catalog, "forget_path", fail_catalog)

    with pytest.raises(ProjectDeletionError, match="project was restored"):
        workflow.delete_project(project)

    assert project.is_dir()
    assert (project / ".repoprover/config.json").is_file()
    assert _source_inventory(source) == source_before
    assert not any(path.name == project.name for path in trash.rglob("*"))


def test_successful_rollback_stays_authoritative_if_container_cleanup_fails(
    tmp_path, monkeypatch
):
    source, project, trash, workflow = _managed_project(tmp_path)
    source_before = _source_inventory(source)

    def fail_catalog(_project: Path) -> None:
        raise OSError("catalog unavailable")

    original_rmdir = Path.rmdir

    def fail_reservation_cleanup(path: Path) -> None:
        if path.name.endswith(".proof-assistant-trash"):
            raise OSError("filesystem metadata kept the reservation busy")
        original_rmdir(path)

    monkeypatch.setattr(workflow.catalog, "forget_path", fail_catalog)
    monkeypatch.setattr(Path, "rmdir", fail_reservation_cleanup)

    with pytest.raises(ProjectDeletionError) as raised:
        workflow.delete_project(project)

    assert "project was restored" in str(raised.value)
    assert "Recover it manually" not in str(raised.value)
    assert project.is_dir()
    assert _source_inventory(source) == source_before
    assert [entry.project_path for entry in workflow.list_projects()] == [project]
    assert tuple(trash.iterdir())  # Empty reservation cleanup was best-effort.


def test_linux_default_is_safe_home_trash_not_incomplete_freedesktop_trash(
    tmp_path, monkeypatch
):
    xdg_data = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", os.fspath(xdg_data))
    monkeypatch.setattr(sys, "platform", "linux")

    trash = ProjectManager._default_trash_root()

    assert trash == xdg_data / "proof-assistant" / "recoverable-trash"
    assert trash != xdg_data / "Trash" / "files"


def test_tui_deletion_code_has_no_direct_filesystem_mutation() -> None:
    root = Path(__file__).parents[1] / "src/proof_assistant/tui"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py"))
    )

    assert "self.service.inspect_project_deletion" in source
    assert "self.service.delete_project" in source
    for forbidden in (
        "os.rename(",
        "os.remove(",
        "shutil.rmtree(",
        ".unlink(",
        ".rmdir(",
    ):
        assert forbidden not in source
