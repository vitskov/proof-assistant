from __future__ import annotations

from importlib.resources import files

import pytest

from proof_assistant.incremental.io import canonical_json_bytes
from proof_assistant.incremental.models import (
    LeanDeclaration,
    ManuscriptEdge,
    SourceObject,
)
from proof_assistant.json_types import JSONTypeError, as_json_value, json_object


def _source_object() -> SourceObject:
    return SourceObject(
        "claim",
        "theorem",
        "paper.tex",
        "theorem",
        "main",
        1,
        1,
        2,
        0,
        10,
        3,
        4,
        11,
        20,
        "statement",
        "proof",
        "normalized",
        "x = x",
        "by rfl",
        ("dep",),
    )


def test_json_boundary_validates_and_normalizes_nested_values() -> None:
    assert as_json_value({"items": (1, "two", {"ok": True})}) == {
        "items": [1, "two", {"ok": True}]
    }
    assert json_object({"schema": 1}) == {"schema": 1}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({1: "bad key"}, "non-string object key"),
        ({"bad": object()}, "non-JSON value object"),
        ({"bad": float("inf")}, "non-finite float"),
        (["not", "an", "object"], "must be a JSON object"),
    ],
)
def test_json_boundary_rejects_invalid_contracts(value: object, message: str) -> None:
    with pytest.raises(JSONTypeError, match=message):
        json_object(value)


def test_canonical_json_writer_uses_validated_boundary() -> None:
    assert canonical_json_bytes({"b": 2, "a": (1,)}) == b'{"a":[1],"b":2}\n'
    with pytest.raises(JSONTypeError, match=r"\$\.bad"):
        canonical_json_bytes({"bad": object()})


def test_high_volume_incremental_records_are_slotted_and_serializable() -> None:
    records = (
        _source_object(),
        ManuscriptEdge("claim", "dep", "explicit_ref", "latex_ref"),
        LeanDeclaration("claim", "theorem", "type", "value", ("dep",), ()),
    )
    assert all(not hasattr(record, "__dict__") for record in records)
    assert records[0].export()["claim_id"] == "claim"
    assert records[2].export()["direct_dependencies"] == ["dep"]


def test_distribution_declares_inline_typing_support() -> None:
    assert files("proof_assistant").joinpath("py.typed").is_file()
