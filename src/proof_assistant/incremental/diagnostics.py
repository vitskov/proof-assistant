from __future__ import annotations

import re

CLARIFICATION_CATEGORIES = frozenset(
    {
        "ambiguous_notation",
        "ambiguous_quantification",
        "missing_assumption",
        "missing_intermediate_lemma",
        "possible_counterexample",
        "undefined_term",
        "likely_typo",
        "formalization_mismatch",
        "formal_statement_review",
    }
)

REQUIRED_CLARIFICATION_DIAGNOSTICS = frozenset(
    {"lean_api_diagnosis", "assumption_sufficiency_check"}
)

CLARIFICATION_DIAGNOSTICS = frozenset(
    {
        "lean_api_diagnosis",
        "mathlib_search",
        "independent_formalization",
        "proof_decomposition",
        "assumption_sufficiency_check",
        "counterexample_search",
    }
)


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("lean_syntax", re.compile(r"unexpected token|parser error|invalid syntax", re.I)),
    (
        "missing_import",
        re.compile(r"unknown (?:constant|identifier|module)|unknown declaration", re.I),
    ),
    (
        "typeclass_failure",
        re.compile(r"failed to synthesize|type class instance", re.I),
    ),
    (
        "mathlib_lookup",
        re.compile(r"declaration has metavariables|invalid field notation", re.I),
    ),
    (
        "formalization_mismatch",
        re.compile(r"type mismatch|application type mismatch", re.I),
    ),
    (
        "possible_counterexample",
        re.compile(r"counterexample|not true|false under", re.I),
    ),
    (
        "missing_assumption",
        re.compile(r"missing assumption|insufficient assumption", re.I),
    ),
    ("ambiguous_notation", re.compile(r"ambiguous notation|undefined notation", re.I)),
    (
        "ambiguous_quantification",
        re.compile(r"free variable|unbound variable|quantif", re.I),
    ),
)


def classify_failure(message: str) -> str:
    for category, pattern in _RULES:
        if pattern.search(message):
            return category
    return "unknown"


def is_technical_category(category: str) -> bool:
    return category in {
        "lean_syntax",
        "mathlib_lookup",
        "missing_import",
        "typeclass_failure",
    }
