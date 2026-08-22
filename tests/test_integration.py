from repoprover_codex.integration import run_repoprover_agent


def test_integration_surface_imports():
    assert callable(run_repoprover_agent)
