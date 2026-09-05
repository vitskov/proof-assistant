from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from proof_assistant.concurrency import (
    AIConcurrencyPatch,
    AutoValue,
    BuildConcurrencyPatch,
    CalibrationKey,
    CalibrationProfile,
    CalibrationStore,
    CodexPlan,
    ConcurrencyConfig,
    ConcurrencyConfigError,
    ConcurrencyConfigPatch,
    ConcurrencyMode,
    ConfigScope,
    LeanConcurrencyPatch,
    LegacyVerificationConfig,
    LegacyVerificationPatch,
    MachineConfigLocationError,
    MachineConfigRevisionError,
    MachineConfigStore,
    ReplMemoryCalibration,
    ResourceProfile,
    SchedulerConcurrencyPatch,
    default_machine_config_path,
    patch_from_mapping,
    resolve_concurrency_config,
)


def test_defaults_are_adaptive_machine_neutral_and_preserve_legacy_behavior():
    config = ConcurrencyConfig()
    assert config.mode == ConcurrencyMode.ADAPTIVE
    assert config.resource_profile == ResourceProfile.AUTO
    assert config.ai.plan == CodexPlan.UNKNOWN
    assert config.ai.initial == AutoValue.AUTO
    assert config.ai.increase_after_successes == AutoValue.AUTO
    assert config.lean.pool_size == AutoValue.AUTO
    assert config.build.max_concurrent == AutoValue.AUTO
    assert config.legacy == LegacyVerificationConfig(
        jobs=2, batch_size=8, lean_pool_size=1
    )


def test_machine_path_uses_xdg_and_refuses_dropbox(tmp_path):
    path = default_machine_config_path(
        environ={"XDG_CONFIG_HOME": str(tmp_path / "config")}, home=tmp_path
    )
    assert path == tmp_path / "config" / "proof-assistant" / "settings.yaml"
    with pytest.raises(MachineConfigLocationError, match="Dropbox"):
        MachineConfigStore(tmp_path / "Dropbox" / "settings.yaml")


def test_machine_store_rejects_custom_registered_dropbox_root(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    registered = tmp_path / "company-sync"
    (home / ".dropbox").mkdir(parents=True)
    (home / ".dropbox" / "info.json").write_text(
        json.dumps({"business": {"path": str(registered)}}), encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    with pytest.raises(MachineConfigLocationError, match="Dropbox"):
        MachineConfigStore(registered / "proof-assistant" / "settings.yaml")


def test_machine_yaml_round_trip_is_revisioned_atomic_and_private(tmp_path):
    store = MachineConfigStore(tmp_path / "settings.yaml")
    empty = store.load()
    assert empty.scope == ConfigScope.MACHINE
    assert empty.revision == 0

    patch = ConcurrencyConfigPatch(
        mode=ConcurrencyMode.ADAPTIVE,
        ai=AIConcurrencyPatch(plan=CodexPlan.PRO_20X, hard_max=24),
        legacy=LegacyVerificationPatch(jobs=1),
    )
    saved = store.save(patch, expected_revision=0)
    assert saved.revision == 1
    assert store.load() == saved
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    parsed = store.path.read_text(encoding="utf-8")
    assert "scope: MACHINE" in parsed
    assert "revision: 1" in parsed
    assert not tuple(store.path.parent.glob("*.tmp"))

    with pytest.raises(MachineConfigRevisionError) as raised:
        store.save(ConcurrencyConfigPatch(), expected_revision=0)
    assert raised.value.actual == 1
    assert store.load() == saved


def test_resolver_precedence_and_per_field_sources_are_explicit():
    machine = ConcurrencyConfigPatch(
        resource_profile=ResourceProfile.INTERACTIVE,
        ai=AIConcurrencyPatch(plan=CodexPlan.PLUS, initial=2),
        lean=LeanConcurrencyPatch(pool_size=2),
        scheduler=SchedulerConcurrencyPatch(agents_per_target_max=2),
    )
    project = ConcurrencyConfigPatch(
        ai=AIConcurrencyPatch(initial=3),
        lean=LeanConcurrencyPatch(pool_size=3),
    )
    cli = ConcurrencyConfigPatch(
        ai=AIConcurrencyPatch(initial=7, hard_max=9),
        build=BuildConcurrencyPatch(max_concurrent=2),
    )
    resolved = resolve_concurrency_config(
        machine=machine,
        project=project,
        environ={
            "PROOF_ASSISTANT_AI_CONCURRENCY": "6",
            "PROOF_ASSISTANT_LEAN_POOL_SIZE": "4",
            "PROOF_ASSISTANT_AGENTS_PER_TARGET": "3",
        },
        cli=cli,
    )
    assert resolved.config.ai.initial == 7
    assert resolved.config.ai.hard_max == 9
    assert resolved.config.lean.pool_size == 4
    assert resolved.config.scheduler.agents_per_target_max == 3
    assert resolved.config.resource_profile == ResourceProfile.INTERACTIVE
    assert resolved.source_for("ai.initial") == ConfigScope.CLI
    assert resolved.source_for("ai.hard_max") == ConfigScope.CLI
    assert resolved.source_for("lean.pool_size") == ConfigScope.ENVIRONMENT
    assert (
        resolved.source_for("scheduler.agents_per_target_max")
        == ConfigScope.ENVIRONMENT
    )
    assert resolved.source_for("resource_profile") == ConfigScope.MACHINE
    assert resolved.source_for("build.hard_max") == ConfigScope.DEFAULT


def test_environment_fixed_mode_requires_and_supplies_all_resource_limits():
    resolved = resolve_concurrency_config(
        environ={
            "PROOF_ASSISTANT_CONCURRENCY_MODE": "fixed",
            "PROOF_ASSISTANT_AI_CONCURRENCY": "5",
            "PROOF_ASSISTANT_LEAN_POOL_SIZE": "3",
            "PROOF_ASSISTANT_MAX_BUILDS": "2",
            "PROOF_ASSISTANT_AGENTS_PER_TARGET": "2",
        }
    )
    assert resolved.config.mode == ConcurrencyMode.FIXED
    assert resolved.config.ai.initial == resolved.config.ai.hard_max == 5
    assert resolved.config.lean.pool_size == resolved.config.lean.max_pool == 3
    assert resolved.config.build.max_concurrent == 2
    assert resolved.config.build.hard_max == 2
    with pytest.raises(ConcurrencyConfigError, match="Fixed concurrency mode"):
        resolve_concurrency_config(
            machine=ConcurrencyConfigPatch(mode=ConcurrencyMode.FIXED), environ={}
        )


@pytest.mark.parametrize(
    "environment",
    [
        {"PROOF_ASSISTANT_AI_CONCURRENCY": "0"},
        {"PROOF_ASSISTANT_LEAN_POOL_SIZE": "many"},
        {"PROOF_ASSISTANT_CONCURRENCY_MODE": "automatic"},
    ],
)
def test_invalid_environment_overrides_fail_closed(environment):
    with pytest.raises(ConcurrencyConfigError):
        resolve_concurrency_config(environ=environment)


def test_yaml_mapping_parser_is_strict_but_supports_explicit_auto():
    patch = patch_from_mapping(
        {
            "ai": {
                "initial": "auto",
                "plan": "pro_5x",
                "increase_after_successes": 17,
            },
            "lean": {"pool_size": 4},
        }
    )
    assert patch.ai.initial == AutoValue.AUTO
    assert patch.ai.plan == CodexPlan.PRO_5X
    assert patch.ai.increase_after_successes == 17
    assert patch.lean.pool_size == 4
    with pytest.raises(ConcurrencyConfigError, match="Unknown concurrency.ai"):
        patch_from_mapping({"ai": {"mystery": 4}})


def test_calibration_profiles_are_environment_keyed_under_existing_cache(tmp_path):
    store = CalibrationStore(tmp_path / "existing-cache")
    key = CalibrationKey(
        os_name="Darwin",
        architecture="arm64",
        usable_logical_cpus=10,
        total_memory_bytes=32 * 1024**3,
        lean_version="4.28.0",
        mathlib_revision="abc123",
        import_profile="Mathlib",
        codex_plan="pro_20x",
        codex_model="gpt-5.6-sol",
    )
    repl = ReplMemoryCalibration(1.0, 1.5, 2.0, 2.8, 20)
    saved = store.save(
        CalibrationProfile(
            key=key,
            repl=repl,
            recommended_lean_pool=4,
            recommended_build_concurrency=1,
            recommended_ai_concurrency=8,
        )
    )
    assert saved.measured_at
    assert saved.repl is not None and saved.repl.budget_gib == 3.0
    assert store.path_for(key).parent == (
        tmp_path / "existing-cache" / "concurrency" / "calibration"
    )
    assert store.load(key) == saved
    raw = json.loads(store.path_for(key).read_text(encoding="utf-8"))
    assert raw["key"]["mathlib_revision"] == "abc123"
    assert store.reset(key) is True
    assert store.load(key) is None
