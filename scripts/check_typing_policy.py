#!/usr/bin/env python3
"""Enforce the repository's bounded ``Any`` policy.

Strict mypy prevents implicit and unchecked typing mistakes, but deliberate
``Any`` annotations remain legal.  This small gate keeps the remaining dynamic
surface from growing and protects the protocol/persistence seams that have been
fully narrowed.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "proof_assistant"
MAX_EXPLICIT_ANY = 98
ANY_FREE_MODULES = (
    "backend.py",
    "integration.py",
    "json_types.py",
    "models.py",
    "protocol.py",
    "tools.py",
    "incremental/io.py",
    "incremental/lean.py",
    "incremental/task.py",
    "workflow/service.py",
    "workspace/catalog.py",
)


def explicit_any_count(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(
        isinstance(node, ast.Name) and node.id == "Any" for node in ast.walk(tree)
    )


def main() -> int:
    counts = {
        path.relative_to(SOURCE_ROOT).as_posix(): explicit_any_count(path)
        for path in SOURCE_ROOT.rglob("*.py")
    }
    total = sum(counts.values())
    violations = [name for name in ANY_FREE_MODULES if counts.get(name, 0)]
    if total > MAX_EXPLICIT_ANY or violations:
        if total > MAX_EXPLICIT_ANY:
            print(
                f"Explicit Any budget exceeded: {total} > {MAX_EXPLICIT_ANY}. "
                "Narrow the new use or reduce existing debt."
            )
        for name in violations:
            print(f"Explicit Any is forbidden in typed boundary module: {name}")
        return 1
    print(
        f"Typing policy passed: {total}/{MAX_EXPLICIT_ANY} explicit Any uses; "
        f"{len(ANY_FREE_MODULES)} boundary modules are Any-free."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
