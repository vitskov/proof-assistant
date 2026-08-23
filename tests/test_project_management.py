from __future__ import annotations

import json
from pathlib import Path

import pytest

from proof_assistant.incremental.store import StateStore
from proof_assistant.workflow.contracts import (
    NewProjectRequest,
    ProjectAvailability,
)
from proof_assistant.workflow.service import (
    ProjectDestinationError,
    ProofAssistantWorkflow,
)


def manuscript(root: Path, *, alternate: bool = False) -> Path:
    source = root / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        r"\documentclass{article}"
        r"\begin{theorem}\label{main}Main.\end{theorem}",
        encoding="utf-8",
    )
    if alternate:
        (source / "alternate.tex").write_text(
            r"\documentclass{article}"
            r"\begin{theorem}\label{alternate}Alternate.\end{theorem}",
            encoding="utf-8",
        )
    return source


def workflow(root: Path) -> ProofAssistantWorkflow:
    return ProofAssistantWorkflow(
        catalog_root=root / "catalog", use_codex_clarification=False
    )


def entry_for(service: ProofAssistantWorkflow, project: Path):
    entries = service.list_projects()
    assert len(entries) == 1
    assert entries[0].project_path == project
    return entries[0]


def create_project(
    root: Path, *, alternate: bool = False
) -> tuple[ProofAssistantWorkflow, Path, Path]:
    source = manuscript(root, alternate=alternate)
    project = root / "managed"
    service = workflow(root)
    service.create_project(
        NewProjectRequest("Paper", source, "main.tex", project_path=project)
    )
    return service, source, project


def test_destination_resolution_keeps_default_path_ownership_in_backend(
    tmp_path, monkeypatch
):
    expected = tmp_path / "backend-selected"
    monkeypatch.setattr(
        "proof_assistant.workspace.management.default_project_path",
        lambda name: expected,
    )
    inspection = workflow(tmp_path).inspect_project_destination("Human Name")
    assert inspection.project_path == expected
    assert inspection.availability == ProjectAvailability.AVAILABLE
    assert inspection.can_create is True


def test_occupied_destination_is_remembered_surfaced_and_never_deleted(tmp_path):
    source = manuscript(tmp_path)
    project = tmp_path / "occupied"
    project.mkdir()
    sentinel = project / "private-notes.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    service = workflow(tmp_path)

    inspection = service.inspect_project_destination("Paper", project)
    assert inspection.availability == ProjectAvailability.OCCUPIED
    with pytest.raises(ProjectDestinationError) as caught:
        service.create_project(
            NewProjectRequest("Paper", source, "main.tex", project_path=project)
        )
    assert caught.value.inspection == inspection
    assert sentinel.read_text(encoding="utf-8") == "keep me"

    entry = entry_for(service, project)
    assert entry.availability == ProjectAvailability.OCCUPIED
    assert entry.project is None
    assert "not a Proof Assistant project" in (entry.issue or "")


def test_incomplete_project_is_distinct_from_unrecognized_occupancy(tmp_path):
    project = tmp_path / "partial"
    (project / ".repoprover").mkdir(parents=True)
    (project / "VERIFY.yaml").write_text("unfinished", encoding="utf-8")
    service = workflow(tmp_path)
    service.catalog.remember_path(project)

    entry = entry_for(service, project)
    assert entry.availability == ProjectAvailability.INCOMPLETE
    assert "no configuration" in (entry.issue or "")
    with pytest.raises(ProjectDestinationError):
        service.resume_project(project)
    assert (project / "VERIFY.yaml").read_text(encoding="utf-8") == "unfinished"


def test_valid_existing_project_is_always_a_resumable_catalog_entry(tmp_path):
    service, _source, project = create_project(tmp_path)
    destination = service.inspect_project_destination("Paper", project)
    assert destination.availability == ProjectAvailability.RESUMABLE

    entry = entry_for(service, project)
    assert entry.availability == ProjectAvailability.RESUMABLE
    assert entry.resumable is True
    assert entry.project is not None
    assert entry.project.project_path == project
    assert entry.project.main_file == "main.tex"

    with pytest.raises(ProjectDestinationError, match="unavailable"):
        service.create_project(
            NewProjectRequest(
                "Paper", entry.project.source_path, "main.tex", project_path=project
            )
        )


def test_ambiguous_legacy_project_is_visible_and_explicit_selection_repairs_it(
    tmp_path,
):
    service, _source, project = create_project(tmp_path, alternate=True)
    config_path = project / ".repoprover/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["schema_version"] = 1
    config.pop("main_file")
    config.pop("input_files")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    before = config_path.read_bytes()

    inspection = service.inspect_project_destination("Paper", project)
    assert inspection.availability == ProjectAvailability.NEEDS_MAIN_FILE
    assert config_path.read_bytes() == before
    entry = entry_for(service, project)
    assert entry.availability == ProjectAvailability.NEEDS_MAIN_FILE
    assert entry.project is None
    assert {item.relative_path for item in entry.main_file_candidates} == {
        "alternate.tex",
        "main.tex",
    }
    assert entry.suggested_main_file == "main.tex"

    snapshot = service.select_project_main_file(project, "alternate.tex")
    assert snapshot.project.main_file == "alternate.tex"
    assert snapshot.pending_plan is not None
    assert snapshot.pending_plan.main_file_changed is True
    repaired = json.loads(config_path.read_text(encoding="utf-8"))
    assert repaired["schema_version"] == 2
    assert repaired["main_file"] == "alternate.tex"
    with StateStore(project / ".repoprover/state.sqlite3") as store:
        assert store.get_metadata("main_file") == "alternate.tex"
    assert entry_for(service, project).resumable is True


def test_invalid_legacy_selection_is_nonmutating_and_remains_recoverable(tmp_path):
    service, _source, project = create_project(tmp_path, alternate=True)
    config_path = project / ".repoprover/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["schema_version"] = 1
    config.pop("main_file")
    config.pop("input_files")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    before = config_path.read_bytes()

    with pytest.raises(ProjectDestinationError, match="not a candidate"):
        service.select_project_main_file(project, "missing.tex")
    assert config_path.read_bytes() == before
    assert (
        entry_for(service, project).availability == ProjectAvailability.NEEDS_MAIN_FILE
    )


def test_malformed_catalogued_project_is_not_silently_dropped(tmp_path):
    project = tmp_path / "broken"
    (project / ".repoprover").mkdir(parents=True)
    (project / ".repoprover/config.json").write_text("{broken", encoding="utf-8")
    service = workflow(tmp_path)
    service.catalog.remember_path(project)

    first = entry_for(service, project)
    second = entry_for(service, project)
    assert first.availability == second.availability == ProjectAvailability.INCOMPLETE
    assert first.project_path == project
    assert "invalid JSON" in (first.issue or "")
