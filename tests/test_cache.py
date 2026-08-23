from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from repoprover_codex import cache, environment
from repoprover_codex.cache import (
    CacheLayout,
    CacheLocationError,
    CachePolicy,
)
from repoprover_codex.environment import CompilerCheck


def layout_for(home: Path, requested: Path | None = None) -> CacheLayout:
    return CacheLayout.discover(
        requested or home / ".cache" / "repoprover-codex",
        user_home=home,
        dropbox_roots=[],
        filesystem_type="apfs",
    )


def test_default_cache_layout_is_centralized_under_home(tmp_path):
    layout = layout_for(tmp_path)
    assert layout.root == tmp_path / ".cache" / "repoprover-codex"
    assert layout.mathlib_downloads == layout.root / "mathlib-downloads"
    assert layout.lake_system == layout.root / "lake" / "system"
    assert layout.lake_dependencies == layout.root / "lake" / "dependencies"
    assert layout.lake_builds == layout.root / "lake" / "builds"
    assert layout.worktrees == layout.root / "worktrees"
    assert layout.fixtures == layout.root / "fixtures"


def test_explicit_cache_root_takes_precedence_over_environment(monkeypatch, tmp_path):
    configured = tmp_path / ".cache" / "configured"
    explicit = tmp_path / ".cache" / "explicit"
    monkeypatch.setenv(cache.CACHE_HOME_ENV, str(configured))

    from_environment = CacheLayout.discover(
        user_home=tmp_path,
        dropbox_roots=[],
        filesystem_type="apfs",
    )
    from_argument = CacheLayout.discover(
        explicit,
        user_home=tmp_path,
        dropbox_roots=[],
        filesystem_type="apfs",
    )

    assert from_environment.root == configured
    assert from_argument.root == explicit


def test_cache_root_outside_user_home_is_rejected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(CacheLocationError, match="inside the user home"):
        CacheLayout.discover(
            tmp_path / "elsewhere",
            user_home=home,
            dropbox_roots=[],
            filesystem_type="apfs",
        )


def test_named_dropbox_cache_root_is_rejected(tmp_path):
    forbidden = tmp_path / "Dropbox" / "cache"
    with pytest.raises(CacheLocationError, match="must not reside in Dropbox"):
        CacheLayout.discover(
            forbidden,
            user_home=tmp_path,
            dropbox_roots=[],
            filesystem_type="apfs",
        )


def test_registered_custom_dropbox_root_is_rejected(tmp_path):
    registered = tmp_path / "ResearchSync"
    with pytest.raises(CacheLocationError, match="must not reside in Dropbox"):
        CacheLayout.discover(
            registered / "cache",
            user_home=tmp_path,
            dropbox_roots=[registered],
            filesystem_type="apfs",
        )


def test_symlink_into_dropbox_is_rejected(tmp_path):
    dropbox = tmp_path / "Dropbox"
    dropbox.mkdir()
    link = tmp_path / "cache-link"
    link.symlink_to(dropbox, target_is_directory=True)
    with pytest.raises(CacheLocationError, match="must not reside in Dropbox"):
        CacheLayout.discover(
            link / "repoprover-codex",
            user_home=tmp_path,
            dropbox_roots=[dropbox],
            filesystem_type="apfs",
        )


def test_remote_filesystem_is_rejected(tmp_path):
    with pytest.raises(CacheLocationError, match="local filesystem"):
        CacheLayout.discover(
            tmp_path / ".cache" / "repoprover-codex",
            user_home=tmp_path,
            dropbox_roots=[],
            filesystem_type="nfs",
        )


def test_macos_mount_parser_uses_longest_mount_and_requires_local():
    mounts = """\
/dev/disk1s1s1 on / (apfs, sealed, local, read-only)
server:/home on /Users/example/remote (nfs, nodev)
"""
    assert cache._macos_filesystem_type(Path("/Users/example/cache"), mounts) == "apfs"
    assert (
        cache._macos_filesystem_type(Path("/Users/example/remote/cache"), mounts)
        == "remote:nfs"
    )


def test_runtime_environment_routes_supported_cache_variables(monkeypatch, tmp_path):
    monkeypatch.setattr(environment.sys, "platform", "darwin")
    layout = layout_for(tmp_path)
    env = layout.runtime_environment(
        {"PATH": "/usr/bin", "LANG": "C.UTF-8"}, lean_cc="/usr/bin/clang"
    )
    assert env["MATHLIB_CACHE_DIR"] == str(layout.mathlib_downloads)
    assert env["LAKE_CACHE_DIR"] == str(layout.lake_system)
    assert env["LANG"] == "C"
    assert env["LC_ALL"] == "C"
    assert env["LEAN_CC"] == "/usr/bin/clang"


def test_initialize_creates_layout_and_records_compiler(monkeypatch, tmp_path):
    layout = layout_for(tmp_path)
    expected = CompilerCheck(
        executable="/usr/bin/clang",
        lean_compiler=True,
        fallback_used=True,
    )
    monkeypatch.setattr(cache, "ensure_lean_on_path", lambda env: False)
    monkeypatch.setattr(cache, "select_native_compiler", lambda env: expected)

    config, check = cache.initialize_cache(layout, env={"PATH": "/usr/bin"})

    assert check == expected
    assert all(directory.is_dir() for directory in layout.directories)
    assert config.cache_root == str(layout.root)
    assert config.lean_cc == "/usr/bin/clang"
    assert layout.load_config() == config
    raw = json.loads(layout.config_path.read_text())
    assert raw["schema_version"] == 2
    assert raw["max_bytes"] == 16 * 1024**3
    assert raw["min_free_bytes"] == 25 * 1024**3
    assert list(layout.root.glob("config-*.json.tmp")) == []


def test_config_with_different_root_fails_closed(tmp_path):
    layout = layout_for(tmp_path)
    layout.create()
    layout.config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cache_root": str(tmp_path / "other"),
                "filesystem_type": "apfs",
                "lean_cc": "/usr/bin/clang",
                "lean_compiler": True,
                "compiler_fallback_used": False,
            }
        )
    )
    with pytest.raises(CacheLocationError, match="does not match"):
        layout.load_config()


def test_attach_moves_existing_lake_tree_and_leaves_symlink(tmp_path):
    home = tmp_path / "home"
    project = home / "project"
    lake = project / ".lake"
    lake.mkdir(parents=True)
    (lake / "sentinel").write_text("cached")
    layout = layout_for(home)

    target = cache.attach_project_cache(project, layout)

    assert lake.is_symlink()
    assert lake.resolve() == target
    assert target.is_dir()
    assert (target / "sentinel").read_text() == "cached"
    assert cache.ensure_project_cache_managed(project, layout) == target


def test_attach_creates_managed_cache_for_unbuilt_project(tmp_path):
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    layout = layout_for(home)

    target = cache.attach_project_cache(project, layout)

    assert target.is_dir()
    assert (project / ".lake").is_symlink()
    assert (project / ".lake").resolve() == target


def test_unmanaged_project_cache_fails_closed(tmp_path):
    home = tmp_path / "home"
    project = home / "project"
    (project / ".lake").mkdir(parents=True)
    layout = layout_for(home)
    with pytest.raises(CacheLocationError, match="cache attach"):
        cache.ensure_project_cache_managed(project, layout)


def test_project_already_inside_cache_root_needs_no_symlink(tmp_path):
    layout = layout_for(tmp_path)
    project = layout.fixtures / "toy"
    lake = project / ".lake"
    lake.mkdir(parents=True)
    assert cache.ensure_project_cache_managed(project, layout) == lake


def test_attach_keeps_git_worktree_clean_with_local_exclude(tmp_path):
    home = tmp_path / "home"
    project = home / "project"
    (project / ".lake").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    layout = layout_for(home)

    cache.attach_project_cache(project, layout)

    status = subprocess.run(
        ["git", "-C", str(project), "status", "--short"],
        text=True,
        capture_output=True,
        check=True,
    )
    exclude = project / ".git" / "info" / "exclude"
    assert status.stdout == ""
    assert "/.lake" in exclude.read_text().splitlines()


def test_attach_refuses_git_tracked_lake(tmp_path):
    home = tmp_path / "home"
    project = home / "project"
    lake = project / ".lake"
    lake.mkdir(parents=True)
    (lake / "tracked").write_text("do not move")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        ["git", "-C", str(project), "add", "-f", ".lake/tracked"], check=True
    )
    layout = layout_for(home)

    with pytest.raises(CacheLocationError, match="tracks it in Git"):
        cache.attach_project_cache(project, layout)

    assert not (project / ".lake").is_symlink()
    assert (project / ".lake" / "tracked").read_text() == "do not move"


def test_attach_refuses_project_in_dropbox(tmp_path):
    home = tmp_path / "home"
    project = home / "Dropbox" / "project"
    project.mkdir(parents=True)
    layout = CacheLayout.discover(
        home / ".cache" / "repoprover-codex",
        user_home=home,
        dropbox_roots=[home / "Dropbox"],
        filesystem_type="apfs",
    )

    with pytest.raises(CacheLocationError, match="project must not reside in Dropbox"):
        cache.attach_project_cache(project, layout)

    assert not (project / ".lake").exists()


def _lean_project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.28.0\n")
    (root / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage «toy»\n")
    return root


def test_dependency_key_reuses_compatible_projects_and_tracks_compiler(tmp_path):
    first = _lean_project(tmp_path / "first")
    second = _lean_project(tmp_path / "second")

    first_key = cache.dependency_cache_key(first, env={"LEAN_CC": "/usr/bin/clang"})
    assert first_key == cache.dependency_cache_key(
        second, env={"LEAN_CC": "/usr/bin/clang"}
    )
    assert first_key != cache.dependency_cache_key(
        second, env={"LEAN_CC": "/opt/other/clang"}
    )

    (second / "lean-toolchain").write_text("leanprover/lean4:v4.29.0\n")
    assert first_key != cache.dependency_cache_key(
        second, env={"LEAN_CC": "/usr/bin/clang"}
    )


def test_dependency_depot_is_shared_but_root_builds_are_isolated(tmp_path):
    home = tmp_path / "home"
    first = _lean_project(home / "first")
    second = _lean_project(home / "second")
    layout = layout_for(home)
    first_build = cache.attach_project_cache(first, layout)
    packages = first / ".lake" / "packages"
    (packages / "mathlib").mkdir(parents=True)
    artifact = packages / "mathlib" / "Mathlib.olean"
    artifact.write_text("shared artifact")
    (first / "lake-manifest.json").write_text('{"version": "1.1.0"}\n')

    first_claim = cache.claim_dependency_depot(
        first, layout, env={"LEAN_CC": "/usr/bin/clang"}
    )
    assert not first_claim.ready
    first_claim.promote()
    first_claim.commit()
    depot = first_claim.target
    first_claim.close()

    second_build = cache.attach_project_cache(second, layout)
    second_claim = cache.claim_dependency_depot(
        second, layout, env={"LEAN_CC": "/usr/bin/clang"}
    )
    assert second_claim.ready
    assert second_claim.target == depot
    assert (first / ".lake" / "packages").resolve() == depot / "packages"
    assert (second / ".lake" / "packages").resolve() == depot / "packages"
    assert first_build != second_build
    assert first_build.parent == second_build.parent == layout.lake_builds
    assert not (artifact.stat().st_mode & 0o222)
    assert (second / "lake-manifest.json").is_file()

    third = _lean_project(home / "third")
    cache.attach_project_cache(third, layout)
    concurrent_claim = cache.claim_dependency_depot(
        third,
        layout,
        env={"LEAN_CC": "/usr/bin/clang"},
        timeout=0.1,
    )
    assert concurrent_claim.ready
    assert concurrent_claim.target == depot
    concurrent_claim.close()
    second_claim.close()


def test_attach_repairs_a_managed_symlink_after_gc(tmp_path):
    home = tmp_path / "home"
    project = _lean_project(home / "project")
    layout = layout_for(home)
    target = cache.attach_project_cache(project, layout)
    target.rmdir()

    repaired = cache.attach_project_cache(project, layout)

    assert repaired == target
    assert repaired.is_dir()


def test_gc_evicts_lru_but_never_an_active_build(tmp_path):
    home = tmp_path / "home"
    layout = layout_for(home)
    layout.create()
    old = layout.lake_builds / "old"
    active = layout.lake_builds / "active"
    old.mkdir()
    active.mkdir()
    (old / "data").write_bytes(b"x" * 8192)
    (active / "data").write_bytes(b"y" * 8192)
    os.utime(old, (1, 1))
    os.utime(active, (2, 2))
    lease = cache.acquire_cache_lease(
        layout, "build-active", exclusive=False, timeout=1
    )
    try:
        result = cache.garbage_collect_cache(
            layout,
            CachePolicy(max_bytes=1, min_free_bytes=1),
            strict=False,
        )
    finally:
        lease.close()

    assert not old.exists()
    assert active.exists()
    assert "build:old" in result.removed
    assert "build:active" in result.skipped_active


def test_capacity_check_fails_when_only_active_data_remains(tmp_path):
    home = tmp_path / "home"
    layout = layout_for(home)
    layout.create()
    active = layout.lake_builds / "active"
    active.mkdir()
    (active / "data").write_bytes(b"z" * 8192)
    lease = cache.acquire_cache_lease(
        layout, "build-active", exclusive=False, timeout=1
    )
    try:
        with pytest.raises(cache.CacheCapacityError, match="cannot be satisfied"):
            cache.garbage_collect_cache(
                layout,
                CachePolicy(max_bytes=1, min_free_bytes=1),
                strict=True,
            )
    finally:
        lease.close()
