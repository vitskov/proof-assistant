from __future__ import annotations

import json
from pathlib import Path

import pytest

from proof_assistant.workspace import (
    CatalogLocationError,
    ProjectCatalog,
    ProofAssistantWritePathError,
    is_in_dropbox,
    validate_proof_assistant_write_path,
)


def _write_dropbox_info(home: Path, payload: object) -> None:
    metadata = home / ".dropbox" / "info.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps(payload), encoding="utf-8")


def test_custom_registered_dropbox_root_is_rejected(tmp_path):
    home = tmp_path / "home"
    registered = tmp_path / "company-sync"
    home.mkdir()
    _write_dropbox_info(home, {"business": {"path": str(registered)}})

    assert is_in_dropbox(registered / "project", user_home=home)
    with pytest.raises(ProofAssistantWritePathError, match="cannot reside in Dropbox"):
        validate_proof_assistant_write_path(
            registered / "project", user_home=home
        )


def test_project_catalog_rejects_custom_registered_dropbox_root(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    registered = tmp_path / "company-sync"
    home.mkdir()
    _write_dropbox_info(home, {"business": {"path": str(registered)}})
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    with pytest.raises(CatalogLocationError, match="Dropbox"):
        ProjectCatalog(registered / "proof-assistant" / "projects.json")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"personal": None},
        {"personal": {}},
        {"personal": {"path": ""}},
        {"personal": {"path": "relative/dropbox"}},
        {"personal": {"path": "bad\0path"}},
    ],
)
def test_malformed_dropbox_metadata_fails_closed_for_writes(tmp_path, payload):
    home = tmp_path / "home"
    home.mkdir()
    _write_dropbox_info(home, payload)

    with pytest.raises(ProofAssistantWritePathError, match="Dropbox root metadata"):
        validate_proof_assistant_write_path(tmp_path / "local-output", user_home=home)
