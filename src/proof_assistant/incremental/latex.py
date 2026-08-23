from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from pylatexenc.latexwalker import (
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexNode,
    LatexWalker,
)

from .models import ManuscriptEdge, SourceObject
from .store import StateStore

THEOREM_ENVIRONMENTS: dict[str, str] = {
    "assumption": "assumption",
    "axiom": "assumption",
    "claim": "claim",
    "conjecture": "conjecture",
    "corollary": "corollary",
    "definition": "definition",
    "defn": "definition",
    "hypothesis": "assumption",
    "lemma": "lemma",
    "notation": "notation",
    "observation": "observation",
    "proposition": "proposition",
    "theorem": "theorem",
}
EQUATION_ENVIRONMENTS = frozenset(
    {
        "align",
        "align*",
        "equation",
        "equation*",
        "gather",
        "gather*",
        "multline",
        "multline*",
    }
)
PROOF_ENVIRONMENTS = frozenset({"proof", "proof*"})
REFERENCE_MACROS = frozenset({"ref", "eqref", "cref", "Cref", "autoref"})


class LatexIndexError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ObjectDraft:
    kind: str
    source_file: str
    environment: str
    label: str | None
    ordinal: int
    statement_start: int
    statement_end: int
    statement_byte_start: int
    statement_byte_end: int
    proof_start: int | None
    proof_end: int | None
    proof_byte_start: int | None
    proof_byte_end: int | None
    statement_hash: str
    proof_hash: str
    normalized_statement_hash: str
    statement_text: str
    proof_text: str
    references: tuple[str, ...]
    claim_id: str = ""

    def finalized(self) -> SourceObject:
        if not self.claim_id:
            raise LatexIndexError("Source object has no stable claim ID")
        return SourceObject(**self.__dict__)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        position = 0
        while True:
            position = line.find("%", position)
            if position < 0:
                break
            backslashes = 0
            cursor = position - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                line = line[:position]
                break
            position += 1
        lines.append(line)
    return "\n".join(lines)


def normalize_latex_statement(text: str) -> str:
    text = _strip_comments(text)
    text = re.sub(r"\\label\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _raw_argument(source: str, node: LatexNode | None) -> str | None:
    if node is None:
        return None
    raw = source[node.pos : node.pos + node.len]
    if isinstance(node, LatexGroupNode) and len(raw) >= 2:
        return raw[1:-1].strip()
    return raw.strip()


def _macro_argument(source: str, node: LatexMacroNode) -> str | None:
    arguments = getattr(getattr(node, "nodeargd", None), "argnlist", None) or []
    for argument in reversed(arguments):
        value = _raw_argument(source, argument)
        if value is not None:
            return value
    return None


def _walk_nodes(nodes: Iterable[LatexNode]) -> Iterable[LatexNode]:
    for node in nodes:
        yield node
        child_lists: list[Sequence[LatexNode]] = []
        nodelist = getattr(node, "nodelist", None)
        if nodelist:
            child_lists.append(nodelist)
        nodeargd = getattr(node, "nodeargd", None)
        for argument in getattr(nodeargd, "argnlist", None) or []:
            argument_nodes = getattr(argument, "nodelist", None)
            if argument_nodes:
                child_lists.append(argument_nodes)
        for children in child_lists:
            yield from _walk_nodes(children)


def _labels_and_references(
    source: str, nodes: Iterable[LatexNode]
) -> tuple[list[str], list[str]]:
    labels: list[str] = []
    references: list[str] = []
    for node in _walk_nodes(nodes):
        if not isinstance(node, LatexMacroNode):
            continue
        argument = _macro_argument(source, node)
        if not argument:
            continue
        if node.macroname == "label":
            labels.append(argument)
        elif node.macroname in REFERENCE_MACROS:
            references.extend(
                item.strip() for item in argument.split(",") if item.strip()
            )
    return labels, references


def _custom_theorem_environments(source: str) -> dict[str, str]:
    environments: dict[str, str] = {}
    # This scanner only discovers declarations; the actual object topology and
    # spans come from pylatexenc's balanced structural parser.
    pattern = re.compile(
        r"\\newtheorem\*?\s*\{([^{}]+)\}(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}"
    )
    for match in pattern.finditer(_strip_comments(source)):
        name = match.group(1).strip()
        title = match.group(2).strip().casefold()
        kind = next(
            (value for key, value in THEOREM_ENVIRONMENTS.items() if key in title),
            "claim",
        )
        environments[name] = kind
    return environments


def _byte_offsets(text: str) -> list[int]:
    result = [0]
    total = 0
    for character in text:
        total += len(character.encode("utf-8"))
        result.append(total)
    return result


def _body_span(node: LatexEnvironmentNode) -> tuple[int, int]:
    nodes = node.nodelist or []
    if not nodes:
        return node.pos, node.pos + node.len
    return nodes[0].pos, nodes[-1].pos + nodes[-1].len


def _proof_for(
    source: str,
    theorem: LatexEnvironmentNode,
    environments: Sequence[LatexEnvironmentNode],
) -> LatexEnvironmentNode | None:
    theorem_end = theorem.pos + theorem.len
    for candidate in environments:
        if candidate.pos < theorem_end:
            continue
        gap = _strip_comments(source[theorem_end : candidate.pos]).strip()
        if gap:
            return None
        return candidate if candidate.environmentname in PROOF_ENVIRONMENTS else None
    return None


def extract_file(path: Path, relative_path: str) -> list[_ObjectDraft]:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise LatexIndexError(f"LaTeX source must be UTF-8: {path}") from exc
    try:
        nodes, _position, _length = LatexWalker(source).get_latex_nodes(pos=0)
    except Exception as exc:
        raise LatexIndexError(
            f"Could not structurally parse {relative_path}: {exc}"
        ) from exc

    theorem_kinds = dict(THEOREM_ENVIRONMENTS)
    theorem_kinds.update(_custom_theorem_environments(source))
    all_nodes = list(_walk_nodes(nodes))
    all_environments = sorted(
        (node for node in all_nodes if isinstance(node, LatexEnvironmentNode)),
        key=lambda node: node.pos,
    )
    proof_environments = [
        node for node in all_environments if node.environmentname in PROOF_ENVIRONMENTS
    ]
    byte_offsets = _byte_offsets(source)
    drafts: list[_ObjectDraft] = []
    ordinal = 0
    for node in all_environments:
        environment = node.environmentname
        labels, references = _labels_and_references(source, node.nodelist or [])
        if environment in theorem_kinds:
            kind = theorem_kinds[environment]
        elif environment in EQUATION_ENVIRONMENTS and labels:
            kind = "equation"
        else:
            continue
        ordinal += 1
        start, end = _body_span(node)
        statement = source[start:end]
        proof_node = _proof_for(source, node, proof_environments)
        if proof_node is not None:
            proof_start, proof_end = _body_span(proof_node)
            proof_text = source[proof_start:proof_end]
            _proof_labels, proof_references = _labels_and_references(
                source, proof_node.nodelist or []
            )
            references.extend(proof_references)
        else:
            proof_start = proof_end = None
            proof_text = ""
        normalized = normalize_latex_statement(statement)
        drafts.append(
            _ObjectDraft(
                kind=kind,
                source_file=relative_path,
                environment=environment,
                label=labels[0] if labels else None,
                ordinal=ordinal,
                statement_start=start,
                statement_end=end,
                statement_byte_start=byte_offsets[start],
                statement_byte_end=byte_offsets[end],
                proof_start=proof_start,
                proof_end=proof_end,
                proof_byte_start=(
                    byte_offsets[proof_start] if proof_start is not None else None
                ),
                proof_byte_end=(
                    byte_offsets[proof_end] if proof_end is not None else None
                ),
                statement_hash=_sha256(statement),
                proof_hash=_sha256(proof_text),
                normalized_statement_hash=_sha256(normalized),
                statement_text=statement,
                proof_text=proof_text,
                references=tuple(sorted(set(references))),
            )
        )
    return drafts


def _assign_stable_ids(
    drafts: list[_ObjectDraft], store: StateStore
) -> list[_ObjectDraft]:
    seen_labels: dict[str, str] = {}
    assigned: list[_ObjectDraft] = []
    unlabeled: list[_ObjectDraft] = []
    for draft in drafts:
        if draft.label:
            prior_file = seen_labels.get(draft.label)
            if prior_file is not None:
                raise LatexIndexError(
                    f"Duplicate LaTeX label {draft.label!r} in {prior_file} and {draft.source_file}"
                )
            seen_labels[draft.label] = draft.source_file
            assigned.append(replace(draft, claim_id=draft.label))
        else:
            unlabeled.append(draft)

    prior_rows = [row for row in store.current_claim_rows() if row["label"] is None]
    available = {str(row["claim_id"]): row for row in prior_rows}
    pending: list[_ObjectDraft] = []
    for draft in unlabeled:
        exact = sorted(
            (
                row
                for row in available.values()
                if row["source_file"] == draft.source_file
                and row["kind"] == draft.kind
                and row["normalized_statement_hash"] == draft.normalized_statement_hash
            ),
            key=lambda row: (abs(int(row["ordinal"]) - draft.ordinal), row["claim_id"]),
        )
        if exact:
            row = exact[0]
            available.pop(str(row["claim_id"]))
            assigned.append(replace(draft, claim_id=str(row["claim_id"])))
        else:
            pending.append(draft)

    for draft in pending:
        candidates = sorted(
            (
                row
                for row in available.values()
                if row["source_file"] == draft.source_file and row["kind"] == draft.kind
            ),
            key=lambda row: (abs(int(row["ordinal"]) - draft.ordinal), row["claim_id"]),
        )
        if candidates:
            row = candidates[0]
            available.pop(str(row["claim_id"]))
            claim_id = str(row["claim_id"])
        else:
            claim_id = store.allocate_claim_id(draft.kind)
        assigned.append(replace(draft, claim_id=claim_id))
    return sorted(
        assigned, key=lambda item: (item.source_file, item.ordinal, item.claim_id)
    )


def index_manuscript(source_root: Path, store: StateStore) -> tuple[SourceObject, ...]:
    drafts: list[_ObjectDraft] = []
    for path in sorted(
        (
            item
            for item in source_root.rglob("*")
            if item.is_file() and item.suffix.casefold() in {".tex", ".ltx"}
        ),
        key=lambda item: item.relative_to(source_root).as_posix(),
    ):
        drafts.extend(extract_file(path, path.relative_to(source_root).as_posix()))
    if not drafts:
        raise LatexIndexError(f"No mathematical objects found under {source_root}")
    return tuple(item.finalized() for item in _assign_stable_ids(drafts, store))


def explicit_reference_graph(
    objects: Sequence[SourceObject],
) -> tuple[tuple[ManuscriptEdge, ...], tuple[tuple[str, str], ...]]:
    ids = {item.claim_id for item in objects}
    labels = {item.label: item.claim_id for item in objects if item.label}
    edges: set[tuple[str, str, str]] = set()
    unresolved: set[tuple[str, str]] = set()
    for item in objects:
        for reference in item.references:
            destination = labels.get(reference, reference if reference in ids else None)
            if destination is None:
                unresolved.add((item.claim_id, reference))
            elif destination != item.claim_id:
                edges.add((item.claim_id, destination, "explicit_ref"))
    return (
        tuple(
            ManuscriptEdge(src, dst, kind, "latex_ref")
            for src, dst, kind in sorted(edges)
        ),
        tuple(sorted(unresolved)),
    )
