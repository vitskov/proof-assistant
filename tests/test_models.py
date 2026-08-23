import pytest

from proof_assistant.models import (
    model_id,
    supported_efforts,
    validate_model_effort,
)

CATALOG = [
    {
        "id": "model-row-id",
        "model": "gpt-test",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Fast"},
            {"reasoningEffort": "high", "description": "Deep"},
        ],
    }
]


def test_model_helpers_match_current_catalog_shape():
    assert model_id(CATALOG[0]) == "gpt-test"
    assert supported_efforts(CATALOG[0]) == ["low", "high"]


def test_validate_accepts_supported_pair():
    validate_model_effort(CATALOG, model="gpt-test", effort="high")


def test_validate_rejects_unknown_model():
    with pytest.raises(ValueError, match="not advertised"):
        validate_model_effort(CATALOG, model="missing", effort="high")


def test_validate_rejects_bad_effort():
    with pytest.raises(ValueError, match="Supported"):
        validate_model_effort(CATALOG, model="gpt-test", effort="xhigh")
