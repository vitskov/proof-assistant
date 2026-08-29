import json
import stat

import pytest

from proof_assistant.workflow.contracts import Difficulty, DriverId, ProjectAIOverride
from proof_assistant.workflow.project_ai import (
    ProjectAISettingsError,
    ProjectAISettingsRevisionError,
    ProjectAISettingsStore,
)


def _override() -> ProjectAIOverride:
    return ProjectAIOverride(
        ai_driver=DriverId.CLAUDE_CLI,
        model="claude-opus-4-1",
        difficulty=Difficulty.HIGH,
    )


def test_missing_settings_inherit_machine_defaults(tmp_path):
    store = ProjectAISettingsStore(tmp_path)

    assert store.load() == (0, None)
    assert not store.path.exists()


def test_round_trip_is_visible_across_store_clients(tmp_path):
    first = ProjectAISettingsStore(tmp_path)
    second = ProjectAISettingsStore(tmp_path)

    saved = first.save(_override(), expected_revision=0)

    assert saved == (1, _override())
    assert second.load() == saved
    assert json.loads(first.path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "scope": "PROJECT",
        "revision": 1,
        "override": {
            "ai_driver": "claude_cli",
            "model": "claude-opus-4-1",
            "difficulty": "high",
        },
    }


def test_reset_persists_null_override_and_advances_revision(tmp_path):
    store = ProjectAISettingsStore(tmp_path)
    store.save(_override(), expected_revision=0)

    assert store.save(None, expected_revision=1) == (2, None)
    assert ProjectAISettingsStore(tmp_path).load() == (2, None)
    assert json.loads(store.path.read_text(encoding="utf-8"))["override"] is None


def test_stale_revision_does_not_mutate_settings(tmp_path):
    store = ProjectAISettingsStore(tmp_path)
    current = store.save(_override(), expected_revision=0)
    before = store.path.read_bytes()

    with pytest.raises(ProjectAISettingsRevisionError) as caught:
        store.save(None, expected_revision=0)

    assert caught.value.expected == 0
    assert caught.value.actual == 1
    assert store.path.read_bytes() == before
    assert store.load() == current


def test_settings_and_lock_permissions_are_private(tmp_path):
    store = ProjectAISettingsStore(tmp_path)
    store.save(_override(), expected_revision=0)

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "document",
    (
        "{broken",
        json.dumps(
            {
                "schema_version": 1,
                "scope": "PROJECT",
                "revision": 0,
                "override": None,
                "extra": True,
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "scope": "PROJECT",
                "revision": 0,
                "override": {
                    "ai_driver": "claude_cli",
                    "model": "claude-opus-4-1",
                    "difficulty": "high",
                    "api_key": "must-not-be-loaded",
                },
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "scope": "PROJECT",
                "revision": 0,
                "override": {
                    "ai_driver": "claude_cli",
                    "model": "model with unsafe spaces",
                    "difficulty": "high",
                },
            }
        ),
    ),
)
def test_malformed_secret_extra_and_unsafe_documents_are_rejected(tmp_path, document):
    store = ProjectAISettingsStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(document, encoding="utf-8")

    with pytest.raises(ProjectAISettingsError) as caught:
        store.load()

    assert "must-not-be-loaded" not in str(caught.value)
    assert "model with unsafe spaces" not in str(caught.value)


def test_atomic_writes_leave_no_temporary_files(tmp_path):
    store = ProjectAISettingsStore(tmp_path)

    store.save(_override(), expected_revision=0)
    store.save(None, expected_revision=1)

    assert not list(store.path.parent.glob(f".{store.path.name}.*.tmp"))


def test_store_requires_an_existing_project_directory(tmp_path):
    with pytest.raises(ProjectAISettingsError, match="existing project directory"):
        ProjectAISettingsStore(tmp_path / "missing")

    file_path = tmp_path / "file"
    file_path.write_text("not a project", encoding="utf-8")
    with pytest.raises(ProjectAISettingsError, match="existing project directory"):
        ProjectAISettingsStore(file_path)
