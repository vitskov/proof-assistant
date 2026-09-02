from __future__ import annotations

import pytest

from proof_assistant.incremental.io import canonical_hash
from proof_assistant.incremental.lean import (
    LeanExtractionError,
    _declaration_from_payload,
)
from proof_assistant.json_types import JSONObject


def _payload() -> JSONObject:
    return {
        "name": "ManuscriptVerification.example",
        "kind": "theorem",
        "type_expr": ["sort", ["succ", ["zero"]]],
        "value_expr": [
            "lam",
            "default",
            ["sort", ["zero"]],
            ["bvar", 0],
        ],
        "direct_dependencies": ["B", "A", "A"],
        "axioms": ["Classical.choice"],
    }


def test_declaration_accepts_structured_lean_expression_json() -> None:
    payload = _payload()

    declaration = _declaration_from_payload(payload)

    assert declaration.type_hash == canonical_hash(payload["type_expr"])
    assert declaration.value_hash == canonical_hash(payload["value_expr"])
    assert declaration.direct_dependencies == ("A", "B")


def test_declaration_accepts_null_value_for_axioms() -> None:
    payload = _payload()
    payload["kind"] = "axiom"
    payload["value_expr"] = None

    declaration = _declaration_from_payload(payload)

    assert declaration.value_hash is None


@pytest.mark.parametrize("key", ("type_expr", "value_expr"))
def test_declaration_rejects_non_structured_expression_json(key: str) -> None:
    payload = _payload()
    payload[key] = "not-structured-expression-json"

    with pytest.raises(LeanExtractionError, match=f"invalid {key}"):
        _declaration_from_payload(payload)
