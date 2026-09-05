from __future__ import annotations

import json
from pathlib import Path

import pytest

from proof_assistant.workflow.contracts import ManuscriptFolderOrigin
from proof_assistant.workflow.preferences import (
    LocalPreferenceLocationError,
    LocalPreferenceStore,
    default_local_preferences_path,
)
from proof_assistant.workflow.service import ProofAssistantWorkflow


def _workflow(tmp_path: Path, preference_path: Path) -> ProofAssistantWorkflow:
    return ProofAssistantWorkflow(
        catalog_root=tmp_path / "catalog",
        machine_config_path=tmp_path / "machine-settings.yaml",
        preference_path=preference_path,
        use_codex_clarification=False,
    )


def test_folder_listing_falls_back_home_then_persists_across_clients(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    alpha = home / "alpha"
    beta = home / "Beta"
    alpha.mkdir()
    beta.mkdir()
    (home / "not-a-folder.txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    preference_path = tmp_path / "local-config" / "preferences.json"

    first = _workflow(tmp_path, preference_path)
    initial = first.browse_manuscript_folders()

    assert initial.directory == home
    assert initial.home == home
    assert initial.origin == ManuscriptFolderOrigin.HOME_FALLBACK
    assert tuple(item.name for item in initial.folders) == ("alpha", "Beta")
    assert all(item.path.is_dir() for item in initial.folders)
    assert not preference_path.exists()

    assert first.remember_manuscript_folder(alpha) == alpha
    payload = json.loads(preference_path.read_text(encoding="utf-8"))
    assert payload == {
        "last_manuscript_folder": str(alpha),
        "schema_version": 1,
    }
    assert preference_path.stat().st_mode & 0o777 == 0o600

    replacement_client = _workflow(tmp_path, preference_path)
    resumed = replacement_client.browse_manuscript_folders()
    assert resumed.directory == alpha
    assert resumed.origin == ManuscriptFolderOrigin.PREFERENCE
    assert resumed.parent == home


def test_absent_or_invalid_saved_folder_uses_home_without_rewriting_preference(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    missing = tmp_path / "missing-manuscript"
    preference_path = tmp_path / "config" / "preferences.json"
    preference_path.parent.mkdir()
    preference_path.write_text(
        json.dumps({"schema_version": 1, "last_manuscript_folder": str(missing)}),
        encoding="utf-8",
    )
    original = preference_path.read_bytes()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    listing = _workflow(tmp_path, preference_path).browse_manuscript_folders()

    assert listing.directory == home
    assert listing.origin == ManuscriptFolderOrigin.HOME_FALLBACK
    assert preference_path.read_bytes() == original

    preference_path.write_text("not valid json", encoding="utf-8")
    malformed = _workflow(tmp_path, preference_path).browse_manuscript_folders()
    assert malformed.directory == home
    assert malformed.origin == ManuscriptFolderOrigin.HOME_FALLBACK


def test_requested_navigation_and_invalid_selection_are_backend_validated(tmp_path):
    preference_path = tmp_path / "config" / "preferences.json"
    root = tmp_path / "sources"
    child = root / "paper"
    child.mkdir(parents=True)
    workflow = _workflow(tmp_path, preference_path)

    listing = workflow.browse_manuscript_folders(root)
    assert listing.directory == root
    assert listing.origin == ManuscriptFolderOrigin.REQUESTED
    assert listing.parent == tmp_path
    assert tuple(item.path for item in listing.folders) == (child,)

    with pytest.raises(ValueError, match="not a directory"):
        workflow.remember_manuscript_folder(tmp_path / "does-not-exist")
    with pytest.raises(ValueError, match="not a directory"):
        workflow.browse_manuscript_folders(tmp_path / "does-not-exist")
    assert not preference_path.exists()


def test_folder_listing_sorts_and_deduplicates_symlink_targets(tmp_path):
    preference_path = tmp_path / "config" / "preferences.json"
    root = tmp_path / "sources"
    target = root / "zeta"
    beta = root / "Beta"
    target.mkdir(parents=True)
    beta.mkdir()
    alias = root / "alpha-alias"
    alias.symlink_to(target, target_is_directory=True)

    listing = _workflow(tmp_path, preference_path).browse_manuscript_folders(root)

    assert tuple(item.name for item in listing.folders) == ("alpha-alias", "Beta")
    assert listing.folders[0].path == target
    assert listing.folders[0].symlink
    assert len({item.path for item in listing.folders}) == len(listing.folders)
    assert not preference_path.exists()


def test_default_preference_path_uses_safe_xdg_and_rejects_unsafe_xdg(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    safe_xdg = tmp_path / "local-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(safe_xdg))
    assert default_local_preferences_path() == (
        safe_xdg / "proof-assistant" / "preferences.json"
    )

    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "Dropbox" / "config"))
    assert default_local_preferences_path() == (
        home / ".config" / "proof-assistant" / "preferences.json"
    )

    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "proof-assistant"))
    assert default_local_preferences_path() == (
        home / ".config" / "proof-assistant" / "preferences.json"
    )


def test_preference_store_refuses_dropbox_and_managed_project_locations(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    with pytest.raises(LocalPreferenceLocationError, match="Dropbox"):
        LocalPreferenceStore(
            home / "Dropbox (Personal)" / "proof-assistant" / "preferences.json"
        )
    with pytest.raises(LocalPreferenceLocationError, match="managed projects"):
        LocalPreferenceStore(home / "proof-assistant" / "paper" / ".preferences.json")


def test_preference_store_refuses_custom_registered_dropbox_root(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    registered = tmp_path / "company-sync"
    (home / ".dropbox").mkdir(parents=True)
    (home / ".dropbox" / "info.json").write_text(
        json.dumps({"business": {"path": str(registered)}}), encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    with pytest.raises(LocalPreferenceLocationError, match="Dropbox"):
        LocalPreferenceStore(registered / "proof-assistant" / "preferences.json")
