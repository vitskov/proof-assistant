from __future__ import annotations

import json
import stat
from dataclasses import FrozenInstanceError

import pytest

from proof_assistant.ai import Difficulty, DriverId, TaskKind
from proof_assistant.workflow.contracts import (
    ProjectAIOverride,
    ProjectAIRoleOverride,
)
from proof_assistant.workflow.project_ai import (
    ProjectAISettingsError,
    ProjectAISettingsRevisionError,
    ProjectAISettingsStore,
)


def _role(task: TaskKind) -> ProjectAIRoleOverride:
    return ProjectAIRoleOverride(
        task=task,
        model=f"{task.value}-model",
        difficulty=Difficulty.MAX if task is TaskKind.PROOF else Difficulty.MEDIUM,
    )


def _override() -> ProjectAIOverride:
    return ProjectAIOverride(
        ai_driver=DriverId.CLAUDE_CLI,
        roles=tuple(_role(task) for task in TaskKind),
    )


def test_missing_settings_inherit_machine_defaults(tmp_path):
    store = ProjectAISettingsStore(tmp_path)

    assert store.load() == (0, None)
    assert not store.path.exists()


def test_schema_v2_round_trip_is_visible_across_store_clients(tmp_path):
    first = ProjectAISettingsStore(tmp_path)
    second = ProjectAISettingsStore(tmp_path)

    saved = first.save(_override(), expected_revision=0)

    assert saved == (1, _override())
    assert second.load() == saved
    assert json.loads(first.path.read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "scope": "PROJECT",
        "revision": 1,
        "override": {
            "ai_driver": "claude_cli",
            "roles": [
                {
                    "role": task.value,
                    "model": f"{task.value}-model",
                    "difficulty": "max" if task is TaskKind.PROOF else "medium",
                }
                for task in TaskKind
            ],
        },
    }


def test_schema_v1_single_model_migrates_to_proof_role_on_load(tmp_path):
    store = ProjectAISettingsStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope": "PROJECT",
                "revision": 7,
                "override": {
                    "ai_driver": "claude_cli",
                    "model": "fable",
                    "difficulty": "high",
                },
            }
        ),
        encoding="utf-8",
    )

    assert store.load() == (
        7,
        ProjectAIOverride(
            ai_driver=DriverId.CLAUDE_CLI,
            roles=(
                ProjectAIRoleOverride(
                    task=TaskKind.PROOF,
                    model="fable",
                    difficulty=Difficulty.HIGH,
                ),
            ),
        ),
    )


def test_new_save_requires_every_registered_task_role(tmp_path):
    store = ProjectAISettingsStore(tmp_path)
    incomplete = ProjectAIOverride(
        ai_driver=DriverId.CLAUDE_CLI,
        roles=(_role(TaskKind.PROOF),),
    )

    with pytest.raises(ProjectAISettingsError, match="every|complete|missing"):
        store.save(incomplete, expected_revision=0)

    assert store.load() == (0, None)
    assert not store.path.exists()


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
                "schema_version": 2,
                "scope": "PROJECT",
                "revision": 0,
                "override": None,
                "extra": True,
            }
        ),
        json.dumps(
            {
                "schema_version": 2,
                "scope": "PROJECT",
                "revision": 0,
                "override": {
                    "ai_driver": "claude_cli",
                    "roles": [
                        {
                            "role": "proof",
                            "model": "fable",
                            "difficulty": "high",
                            "api_key": "must-not-be-loaded",
                        }
                    ],
                },
            }
        ),
        json.dumps(
            {
                "schema_version": 2,
                "scope": "PROJECT",
                "revision": 0,
                "override": {
                    "ai_driver": "claude_cli",
                    "roles": [
                        {
                            "role": "proof",
                            "model": "model with unsafe spaces",
                            "difficulty": "high",
                        }
                    ],
                },
            }
        ),
        json.dumps(
            {
                "schema_version": 2,
                "scope": "PROJECT",
                "revision": 0,
                "override": {
                    "ai_driver": "claude_cli",
                    "roles": [
                        {
                            "role": "proof",
                            "model": "fable",
                            "difficulty": "high",
                        },
                        {
                            "role": "proof",
                            "model": "opus",
                            "difficulty": "medium",
                        },
                    ],
                },
            }
        ),
    ),
)
def test_malformed_secret_extra_unsafe_and_duplicate_documents_are_rejected(
    tmp_path, document
):
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


def test_override_and_roles_are_frozen(tmp_path):
    override = _override()

    with pytest.raises(FrozenInstanceError):
        override.ai_driver = DriverId.CODEX_CLI  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        override.roles[0].model = "mutated"  # type: ignore[misc]

    store = ProjectAISettingsStore(tmp_path)
    store.save(override, expected_revision=0)
    loaded = store.load()[1]
    assert loaded is not None
    with pytest.raises(FrozenInstanceError):
        loaded.roles[0].difficulty = Difficulty.LOW  # type: ignore[misc]


def test_store_requires_an_existing_project_directory(tmp_path):
    with pytest.raises(ProjectAISettingsError, match="existing project directory"):
        ProjectAISettingsStore(tmp_path / "missing")

    file_path = tmp_path / "file"
    file_path.write_text("not a project", encoding="utf-8")
    with pytest.raises(ProjectAISettingsError, match="existing project directory"):
        ProjectAISettingsStore(file_path)
