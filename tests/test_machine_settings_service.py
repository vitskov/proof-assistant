from __future__ import annotations

import stat
from dataclasses import replace

import pytest

from proof_assistant.concurrency import (
    CalibrationKey,
    CalibrationProfile,
    CalibrationStore,
    PressureState,
    ReplMemoryCalibration,
)
from proof_assistant.workflow import (
    BenchmarkKind,
    MachineSettingsUpdateRequest,
    ProofAssistantWorkflow,
    SettingsScopeKind,
)


def _service(tmp_path, monkeypatch) -> ProofAssistantWorkflow:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PROOF_ASSISTANT_AI_CONCURRENCY", raising=False)
    monkeypatch.delenv("PROOF_ASSISTANT_LEAN_POOL_SIZE", raising=False)
    monkeypatch.delenv("PROOF_ASSISTANT_MAX_BUILDS", raising=False)
    monkeypatch.delenv("PROOF_ASSISTANT_AGENTS_PER_TARGET", raising=False)
    monkeypatch.delenv("PROOF_ASSISTANT_CONCURRENCY_MODE", raising=False)
    return ProofAssistantWorkflow(
        cache_home=str(tmp_path / "cache"),
        catalog_root=tmp_path / "catalog",
        machine_config_path=tmp_path / "config" / "settings.yaml",
        use_codex_clarification=False,
    )


def test_machine_settings_preview_apply_persist_and_reset(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    initial = service.get_machine_settings()
    assert initial.scope == SettingsScopeKind.MACHINE
    assert initial.configured.ai_initial is None
    assert initial.effective.ai_limit >= 1

    configured = replace(
        initial.configured,
        ai_initial=2,
        ai_hard_max=3,
        ai_increase_after_successes=5,
        lean_pool=1,
        lean_max=2,
        max_builds=1,
    )
    request = MachineSettingsUpdateRequest(
        expected_revision=initial.revision,
        configured=configured,
        legacy=replace(initial.legacy, proof_jobs=3),
    )
    preview = service.preview_machine_settings(request)
    applied = service.apply_machine_settings(
        preview.preview_token,
        tuple(warning.warning_id for warning in preview.warnings),
    )
    assert applied.revision == initial.revision + 1
    assert applied.configured.ai_initial == 2
    assert applied.configured.ai_increase_after_successes == 5
    assert applied.legacy.proof_jobs == 3
    assert stat.S_IMODE(applied.config_path.stat().st_mode) == 0o600

    replacement_client = _service(tmp_path, monkeypatch)
    persisted = replacement_client.get_machine_settings()
    assert persisted.configured == applied.configured
    assert persisted.legacy.proof_jobs == 3

    reset = replacement_client.reset_machine_settings(persisted.revision)
    assert reset.configured.ai_initial is None
    assert reset.legacy.proof_jobs == 2


def test_project_scope_is_reserved_not_silently_persisted(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    snapshot = service.get_machine_settings()
    request = MachineSettingsUpdateRequest(
        expected_revision=snapshot.revision,
        configured=snapshot.configured,
        legacy=snapshot.legacy,
        scope=SettingsScopeKind.PROJECT,
    )
    with pytest.raises(ValueError, match="reserved but not implemented"):
        service.preview_machine_settings(request)


def test_manual_ai_warning_uses_plan_policy_ceiling(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    snapshot = service.get_machine_settings()
    configured = replace(
        snapshot.configured,
        codex_plan="plus",
        ai_initial=7,
        ai_hard_max=7,
    )
    preview = service.preview_machine_settings(
        MachineSettingsUpdateRequest(
            expected_revision=snapshot.revision,
            configured=configured,
            legacy=snapshot.legacy,
        )
    )
    warnings = {warning.warning_id: warning for warning in preview.warnings}
    assert warnings["quota-burn"].recommended_value == "6"


def test_manual_ai_hard_ceiling_alone_triggers_plan_warning(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    snapshot = service.get_machine_settings()
    configured = replace(
        snapshot.configured,
        codex_plan="plus",
        ai_initial=2,
        ai_hard_max=24,
    )
    preview = service.preview_machine_settings(
        MachineSettingsUpdateRequest(
            expected_revision=snapshot.revision,
            configured=configured,
            legacy=snapshot.legacy,
        )
    )
    assert "quota-burn" in {warning.warning_id for warning in preview.warnings}


def test_environment_override_is_visible_and_beats_machine_value(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    snapshot = service.get_machine_settings()
    configured = replace(snapshot.configured, ai_initial=3, ai_hard_max=3)
    preview = service.preview_machine_settings(
        MachineSettingsUpdateRequest(
            expected_revision=snapshot.revision,
            configured=configured,
            legacy=snapshot.legacy,
        )
    )
    service.apply_machine_settings(preview.preview_token)

    monkeypatch.setenv("PROOF_ASSISTANT_AI_CONCURRENCY", "1")
    overridden = service.get_machine_settings()
    assert overridden.effective.ai_limit == 1
    resolution = {item.field: item for item in overridden.resolution}
    assert resolution["ai.initial"].source == "ENVIRONMENT"


@pytest.mark.parametrize(
    "kind",
    (BenchmarkKind.CODEX, BenchmarkKind.LEAN, BenchmarkKind.BUILD),
)
def test_quota_safe_benchmarks_persist_machine_calibration(tmp_path, monkeypatch, kind):
    service = _service(tmp_path, monkeypatch)
    result = service.run_concurrency_benchmark(kind)
    assert result.recommendation >= 1
    assert result.used_codex_traffic is False
    assert result.calibration_path.is_file()


def test_project_lean_benchmark_persists_measured_rss(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    measurement = ReplMemoryCalibration(1.0, 1.4, 1.8, 2.0, 8)

    def key_for_project(_project, *, resources, codex_plan, codex_model):
        assert codex_model == ""
        return CalibrationKey(
            resources.os_name,
            resources.architecture,
            resources.usable_logical_cpus,
            resources.total_memory_bytes,
            "Lean 4.28.0",
            "mathlib-revision",
            "imports-profile",
            codex_plan,
            codex_model,
        )

    monkeypatch.setattr(
        "proof_assistant.workflow.service.project_calibration_key",
        key_for_project,
    )
    monkeypatch.setattr(
        "proof_assistant.concurrency.runtime.project_calibration_key",
        key_for_project,
    )
    monkeypatch.setattr(
        "proof_assistant.workflow.service.measure_lean_repl_memory",
        lambda selected: measurement if selected == project else None,
    )
    result = service.run_concurrency_benchmark(BenchmarkKind.LEAN, project=project)
    assert result.used_codex_traffic is False
    assert "8 working RSS samples" in result.detail
    payload = result.calibration_path.read_text(encoding="utf-8")
    assert '"p95_working_rss_gib": 1.8' in payload
    snapshot = service.get_machine_settings(project=project)
    assert snapshot.telemetry.lean_p95_rss_gib == 1.8


def test_project_lean_benchmark_refuses_to_overlap_active_lean_work(
    tmp_path, monkeypatch
):
    service = _service(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    runtime = service._runtime()
    called = False

    def should_not_measure(_project):
        nonlocal called
        called = True
        raise AssertionError("measurement must not start")

    monkeypatch.setattr(
        "proof_assistant.workflow.service.project_calibration_key",
        lambda _project, *, resources, codex_plan, codex_model: CalibrationKey(
            resources.os_name,
            resources.architecture,
            resources.usable_logical_cpus,
            resources.total_memory_bytes,
            "Lean 4.28.0",
            "mathlib-revision",
            "imports-profile",
            codex_plan,
            codex_model,
        ),
    )
    monkeypatch.setattr(
        "proof_assistant.workflow.service.measure_lean_repl_memory",
        should_not_measure,
    )
    request = runtime.lean.request("existing-lean")
    with runtime.lean.lease(request):
        with pytest.raises(RuntimeError, match="idle Lean admission queue"):
            service.run_concurrency_benchmark(BenchmarkKind.LEAN, project=project)
    assert called is False


def test_reset_exact_project_calibration_profile(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    resources = service._runtime().resources
    key = CalibrationKey(
        resources.os_name,
        resources.architecture,
        resources.usable_logical_cpus,
        resources.total_memory_bytes,
        "Lean 4.28.0",
        "mathlib-revision",
        "imports-profile",
        service._runtime().resolved.config.ai.plan.value,
    )
    store = CalibrationStore(tmp_path / "cache")
    store.save(
        CalibrationProfile(
            key=key,
            repl=ReplMemoryCalibration(1.0, 1.4, 1.8, 2.0, 8),
        )
    )
    monkeypatch.setattr(
        "proof_assistant.workflow.service.project_calibration_key",
        lambda *_args, **_kwargs: key,
    )
    result = service.reset_project_lean_calibration(project)
    assert result.project_path == project
    assert result.profile_id == key.identifier
    assert result.removed
    assert not result.calibration_path.exists()
    second = service.reset_project_lean_calibration(project)
    assert second.removed is False


def test_reset_project_calibration_refuses_active_lean_work(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    runtime = service._runtime()
    resources = runtime.resources
    key = CalibrationKey(
        resources.os_name,
        resources.architecture,
        resources.usable_logical_cpus,
        resources.total_memory_bytes,
        "Lean 4.28.0",
        "mathlib-revision",
        "imports-profile",
    )
    monkeypatch.setattr(
        "proof_assistant.workflow.service.project_calibration_key",
        lambda *_args, **_kwargs: key,
    )
    with runtime.lean.lease(runtime.lean.request("active-check")):
        with pytest.raises(RuntimeError, match="idle Lean admission queue"):
            service.reset_project_lean_calibration(project)


def test_reset_adaptive_history_restores_baseline_and_preserves_in_flight_lease(
    tmp_path, monkeypatch
):
    service = _service(tmp_path, monkeypatch)
    runtime = service._runtime()
    baseline = runtime.auto_limits
    request = runtime.ai.request("in-flight-proof", "proof")
    with runtime.ai.lease(request):
        runtime.ai.record_throttle(retry_after=30)
        runtime.lean.observe(
            pressure=PressureState.EMERGENCY,
            queue_depth=0,
            cpu_percent=10,
        )
        runtime.build.observe(
            pressure=PressureState.EMERGENCY,
            queue_depth=0,
        )

        result = service.reset_adaptive_history()
        assert result.in_flight_work_preserved
        assert runtime.ai.status().active == 1
        assert result.ai_limit == baseline.ai_initial
        assert result.lean_pool == baseline.lean_pool
        assert result.build_limit == baseline.build_concurrency
        assert runtime.ai.status().throttles == 0
        assert runtime.lean.status().pressure == "green"
        assert runtime.build.status().pressure == "green"
    assert runtime.ai.status().active == 0
