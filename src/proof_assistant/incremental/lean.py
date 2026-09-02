from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path

from ..json_types import JSONObject, JSONValue, json_object, load_json
from .io import atomic_write_bytes, atomic_write_json, canonical_hash, sha256_path
from .models import LeanDeclaration

OUTPUT_PREFIX = "REPOPROVER_DEPENDENCIES_JSON:"


class LeanExtractionError(RuntimeError):
    pass


def install_dependency_extractor(project: Path) -> Path:
    destination = project / "RepoProverSupport" / "DependencyExtractor.lean"
    resource = files("proof_assistant.lean").joinpath("DependencyExtractor.lean")
    atomic_write_bytes(destination, resource.read_bytes())
    return destination


def _required_string(payload: JSONObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise LeanExtractionError(f"Lean extractor returned invalid {key}")
    return value


def _required_expression(payload: JSONObject, key: str) -> list[JSONValue]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise LeanExtractionError(f"Lean extractor returned invalid {key}")
    return value


def _declaration_from_payload(payload: JSONObject) -> LeanDeclaration:
    required = {
        "name",
        "kind",
        "type_expr",
        "value_expr",
        "direct_dependencies",
        "axioms",
    }
    missing = required - set(payload)
    if missing:
        raise LeanExtractionError(
            "Lean extractor omitted: " + ", ".join(sorted(missing))
        )
    dependencies = payload["direct_dependencies"]
    axioms = payload["axioms"]
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise LeanExtractionError("Lean extractor returned invalid dependencies")
    if not isinstance(axioms, list) or not all(
        isinstance(item, str) for item in axioms
    ):
        raise LeanExtractionError("Lean extractor returned invalid axioms")
    type_expr = _required_expression(payload, "type_expr")
    value_expr = payload["value_expr"]
    if value_expr is not None and (
        not isinstance(value_expr, list) or not value_expr
    ):
        raise LeanExtractionError("Lean extractor returned invalid value_expr")
    return LeanDeclaration(
        name=_required_string(payload, "name"),
        kind=_required_string(payload, "kind"),
        type_hash=canonical_hash(type_expr),
        value_hash=None if value_expr is None else canonical_hash(value_expr),
        direct_dependencies=tuple(sorted(set(dependencies))),
        axioms=tuple(sorted(set(axioms))),
    )


def run_dependency_extractor(
    project: Path,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 600.0,
) -> tuple[str, tuple[LeanDeclaration, ...], subprocess.CompletedProcess[str]]:
    helper = install_dependency_extractor(project)
    result = subprocess.run(
        ["lake", "env", "lean", str(helper.relative_to(project))],
        cwd=project,
        env=dict(env or os.environ),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LeanExtractionError(
            f"Lean dependency extractor failed ({result.returncode}): {detail}"
        )
    lines = [line for line in result.stdout.splitlines() if OUTPUT_PREFIX in line]
    if len(lines) != 1:
        raise LeanExtractionError(
            "Lean dependency extractor returned no unique JSON payload"
        )
    try:
        payload = json_object(
            load_json(lines[0].split(OUTPUT_PREFIX, 1)[1]),
            path="Lean extractor payload",
        )
    except ValueError as exc:
        raise LeanExtractionError(f"Lean dependency JSON is malformed: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise LeanExtractionError("Lean dependency extractor schema mismatch")
    raw_declarations = payload.get("declarations")
    if not isinstance(raw_declarations, list):
        raise LeanExtractionError(
            "Lean dependency extractor returned invalid declarations"
        )
    declarations = tuple(
        sorted(
            (
                _declaration_from_payload(item)
                for item in raw_declarations
                if isinstance(item, dict)
            ),
            key=lambda item: item.name,
        )
    )
    if len(declarations) != len(raw_declarations):
        raise LeanExtractionError(
            "Lean dependency extractor returned a non-object declaration"
        )
    raw_lean_version = payload.get("lean_version")
    lean_version = raw_lean_version if isinstance(raw_lean_version, str) else "unknown"
    export: JSONObject = {
        "schema_version": 1,
        "lean_version": lean_version,
        "declarations": [item.export() for item in declarations],
    }
    export["sha256"] = canonical_hash(export)
    atomic_write_json(project / ".repoprover" / "exports" / "lean-graph.json", export)
    return lean_version, declarations, result


def mathlib_revision(project: Path) -> str | None:
    manifest = project / "lake-manifest.json"
    if not manifest.is_file():
        return None
    try:
        payload = json_object(load_json(manifest.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    packages = payload.get("packages", [])
    if not isinstance(packages, list):
        return None
    for package in packages:
        if isinstance(package, dict) and package.get("name") == "mathlib":
            revision = package.get("rev") or package.get("version")
            return revision if isinstance(revision, str) and revision else None
    return None


def environment_fingerprint(project: Path) -> tuple[str, JSONObject]:
    inputs: JSONObject = {}
    for name in (
        "lean-toolchain",
        "lakefile.lean",
        "lakefile.toml",
        "lake-manifest.json",
    ):
        path = project / name
        if path.is_file():
            inputs[name] = sha256_path(path)
    inputs["formalization_sources"] = [
        {
            "path": path.relative_to(project).as_posix(),
            "sha256": sha256_path(path),
        }
        for path in sorted((project / "Formalization").rglob("*.lean"))
    ]
    return canonical_hash(inputs), inputs


def reject_forbidden_axioms(
    declaration: LeanDeclaration,
    *,
    baseline_project_axioms: set[str],
) -> tuple[str, ...]:
    forbidden = {
        name
        for name in declaration.axioms
        if name == "sorryAx"
        or name.endswith(".sorryAx")
        or (
            name.startswith("ManuscriptVerification.")
            and name not in baseline_project_axioms
        )
    }
    return tuple(sorted(forbidden))


def correspondence_discrepancies(
    manuscript_edges: Sequence[tuple[str, str]],
    declarations: Sequence[LeanDeclaration],
    correspondence: dict[str, str],
) -> list[dict[str, str]]:
    reverse = {declaration: claim for claim, declaration in correspondence.items()}
    explicit = set(manuscript_edges)
    declaration_map = {item.name: item for item in declarations}
    discrepancies: list[dict[str, str]] = []
    for claim, declaration_name in sorted(correspondence.items()):
        declaration = declaration_map.get(declaration_name)
        if declaration is None:
            continue
        for dependency in declaration.direct_dependencies:
            dependency_claim = reverse.get(dependency)
            if dependency_claim and (claim, dependency_claim) not in explicit:
                discrepancies.append(
                    {
                        "claim": claim,
                        "lean_declaration": declaration_name,
                        "missing_manuscript_dependency": dependency_claim,
                        "lean_dependency": dependency,
                    }
                )
    return discrepancies
