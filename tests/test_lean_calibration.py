from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from proof_assistant.concurrency import (
    CalibrationKey,
    CalibrationProfile,
    CalibrationStore,
    ConcurrencyConfig,
    ConcurrencyRuntimeSpec,
    HardwareResources,
    LeanCalibrationError,
    ReplMemoryCalibration,
    derive_auto_limits,
    measure_lean_repl_memory,
    project_calibration_key,
    project_import_profile,
    project_lean_version,
    summarize_repl_memory,
)

GIB = 1024**3


def _project(tmp_path):
    project = tmp_path / "project"
    formalization = project / "Formalization"
    formalization.mkdir(parents=True)
    (project / "lean-toolchain").write_text(
        "leanprover/lean4:v4.28.0\n", encoding="utf-8"
    )
    (project / "Manuscript.lean").write_text(
        "import Formalization.All\n", encoding="utf-8"
    )
    (formalization / "All.lean").write_text(
        "import Mathlib\n\nexample : True := by trivial\n", encoding="utf-8"
    )
    return project


def _hardware():
    return HardwareResources(
        "Darwin", "arm64", 10, 8, 10, 8, 32 * GIB, 32 * GIB, 24 * GIB, True
    )


def test_import_profile_tracks_import_workload_not_proof_edits(tmp_path):
    project = _project(tmp_path)
    initial = project_import_profile(project)
    assert initial.header == "import Formalization.All\nimport Mathlib"
    # A source file named Manuscript.lean is not necessarily a loadable Lake
    # module.  Calibrate from its declared imports, which are the actual memory
    # workload, instead of silently importing a missing Manuscript.olean.
    assert "import Manuscript" not in initial.header
    source = project / "Formalization" / "All.lean"
    source.write_text(
        "import Mathlib\n\nexample : False → False := by intro h; exact h\n",
        encoding="utf-8",
    )
    assert project_import_profile(project).identifier == initial.identifier
    source.write_text("import Mathlib.Data.Nat.Prime\n", encoding="utf-8")
    assert project_import_profile(project).identifier != initial.identifier


def test_calibration_key_contains_exact_project_environment(tmp_path):
    project = _project(tmp_path)
    (project / "lake-manifest.json").write_text(
        '{"packages":[{"name":"mathlib","rev":"abc123"}]}\n',
        encoding="utf-8",
    )
    key = project_calibration_key(
        project,
        resources=_hardware(),
        lean_version="Lean (version 4.28.0)",
    )
    assert key.lean_version == "Lean (version 4.28.0)"
    assert key.mathlib_revision == "abc123"
    assert key.import_profile.startswith("imports-")
    assert key.os_name == "Darwin"
    assert key.architecture == "arm64"


def test_project_version_probe_cannot_materialize_lake_packages(tmp_path):
    project = _project(tmp_path)
    calls = []

    def runner(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return type(
            "Result",
            (),
            {"returncode": 0, "stdout": "Lean (version 4.28.0)\n", "stderr": ""},
        )()

    assert project_lean_version(project, runner=runner) == "Lean (version 4.28.0)"
    assert [call[0] for call in calls] == [("lean", "--version")]
    assert calls[0][1]["cwd"] == project
    assert not (project / ".lake").exists()


def test_repl_summary_uses_nearest_rank_p95_and_validates_samples():
    summary = summarize_repl_memory(
        (1.0, 1.2),
        (1.3, 1.4, 1.5, 1.7, 2.1),
    )
    assert summary.warm_idle_rss_gib == pytest.approx(1.1)
    assert summary.median_working_rss_gib == 1.5
    assert summary.p95_working_rss_gib == 2.1
    assert summary.maximum_observed_rss_gib == 2.1
    assert summary.samples == 5
    with pytest.raises(LeanCalibrationError, match="positive RSS"):
        summarize_repl_memory((), (1.0,))


def test_measurement_runs_disposable_representative_repls(tmp_path, monkeypatch):
    project = _project(tmp_path)
    instances = []

    class FakeProbe:
        def __init__(self, selected, **_kwargs):
            assert selected == project
            self.samples = iter((1.0, 1.5, 1.6))
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def request(self, payload):
            assert "cmd" in payload
            return {"env": 1}

        def rss_gib(self):
            return next(self.samples)

    monkeypatch.setattr(
        "proof_assistant.concurrency.calibration._LeanReplProbe", FakeProbe
    )
    result = measure_lean_repl_memory(
        project,
        process_count=2,
        checks_per_process=2,
        settle_seconds=0,
        sleep=lambda _seconds: None,
    )
    assert len(instances) == 2
    assert result.warm_idle_rss_gib == 1.0
    assert result.samples == 4
    assert result.p95_working_rss_gib == 1.6


def test_runtime_loads_only_fresh_exact_p95_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = _project(tmp_path)
    resources = _hardware()
    key = project_calibration_key(
        project, resources=resources, lean_version="Lean 4.28.0"
    )
    profile = CalibrationProfile(
        key=key,
        repl=ReplMemoryCalibration(1.0, 2.0, 4.0, 4.5, 8),
    )
    store = CalibrationStore(tmp_path / "cache")
    saved = store.save(profile)
    monkeypatch.setattr(
        "proof_assistant.concurrency.runtime.project_calibration_key",
        lambda *_args, **_kwargs: key,
    )
    runtime = ConcurrencyRuntimeSpec(
        cache_home=str(tmp_path / "cache"),
        machine_config_path=str(tmp_path / "settings.yaml"),
        project_path=str(project),
    ).create(environ={}, resources=resources)
    assert runtime.calibration_profile == saved
    assert runtime.auto_limits.repl_memory_budget_gib == 6.0
    assert any("calibrated p95" in reason for reason in runtime.auto_limits.reasons)

    stale = CalibrationProfile(
        key=key,
        repl=profile.repl,
        measured_at=(datetime.now(UTC) - timedelta(days=31)).isoformat(),
    )
    store.save(stale)
    fallback = ConcurrencyRuntimeSpec(
        cache_home=str(tmp_path / "cache"),
        machine_config_path=str(tmp_path / "settings.yaml"),
        project_path=str(project),
    ).create(environ={}, resources=resources)
    assert fallback.calibration_profile is None
    assert fallback.auto_limits.repl_memory_budget_gib == 3.0


def test_project_runtimes_share_most_conservative_fresh_memory_budget(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    resources = _hardware()
    project_a = _project(tmp_path / "a")
    project_b = _project(tmp_path / "b")

    def key(import_profile: str) -> CalibrationKey:
        return CalibrationKey(
            resources.os_name,
            resources.architecture,
            resources.usable_logical_cpus,
            resources.total_memory_bytes,
            "Lean 4.28.0",
            "mathlib-revision",
            import_profile,
        )

    key_a = key("heavy-imports")
    key_b = key("unknown-imports")
    monkeypatch.setattr(
        "proof_assistant.concurrency.runtime.project_calibration_key",
        lambda project, **_kwargs: key_a if project == project_a else key_b,
    )
    common = {
        "cache_home": str(tmp_path / "cache"),
        "machine_config_path": str(tmp_path / "settings.yaml"),
    }
    before_calibration = ConcurrencyRuntimeSpec(
        **common,
        project_path=str(project_b),
    ).create(environ={}, resources=resources)
    assert before_calibration.auto_limits.repl_memory_budget_gib == 3.0
    before_calibration.ai.set_limit(2)

    CalibrationStore(tmp_path / "cache").save(
        CalibrationProfile(
            key=key_a,
            repl=ReplMemoryCalibration(2.0, 5.0, 6.0, 6.5, 8),
        )
    )
    heavy = ConcurrencyRuntimeSpec(
        **common,
        project_path=str(project_a),
    ).create(environ={}, resources=resources)
    assert heavy.auto_limits.repl_memory_budget_gib == 9.0
    assert heavy.lean.limit == 1
    assert heavy.ai.limit == 2

    unknown = ConcurrencyRuntimeSpec(
        **common,
        project_path=str(project_b),
    ).create(environ={}, resources=resources)
    assert unknown.calibration_profile is None
    assert unknown.auto_limits.repl_memory_budget_gib == 9.0
    assert unknown.lean.limit == 1
    assert unknown.ai.limit == 2


def test_calibrated_budget_never_drops_below_uncalibrated_fallback():
    resources = _hardware()
    limits = derive_auto_limits(
        resources,
        ConcurrencyConfig(),
        calibrated_repl_p95_gib=0.5,
    )
    assert limits.repl_memory_budget_gib == 3.0


def test_exclusive_calibration_blocks_new_managed_lean_work(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = ConcurrencyRuntimeSpec(
        cache_home=str(tmp_path / "cache"),
        machine_config_path=str(tmp_path / "settings.yaml"),
    ).create(environ={}, resources=_hardware())
    with runtime.lean.exclusive_calibration_lease("benchmark"):
        ordinary = runtime.lean.request("ordinary-check")
        assert runtime.lean.try_acquire(ordinary) is None
        assert runtime.lean.status().active == 1
    assert runtime.lean.status().active == 0


def test_fresh_loader_rejects_future_or_expired_profiles(tmp_path):
    project = _project(tmp_path)
    key = project_calibration_key(
        project, resources=_hardware(), lean_version="Lean 4.28.0"
    )
    now = datetime.now(UTC)
    store = CalibrationStore(tmp_path / "cache")
    profile = CalibrationProfile(
        key=key,
        repl=ReplMemoryCalibration(1.0, 1.5, 2.0, 2.5, 4),
        measured_at=now.isoformat(),
    )
    store.save(profile)
    assert store.load_fresh(key, now=now) is not None
    assert store.load_fresh(key, now=now + timedelta(days=31)) is None
    assert store.load_fresh(key, now=now - timedelta(seconds=1)) is None
