from __future__ import annotations

import hashlib
import os
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

from ..manuscript import (
    IGNORED_DIRECTORY_NAMES,
    IGNORED_FILE_NAMES,
    IGNORED_FILE_SUFFIXES,
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
ASSISTANT_CONTEXT_EDGE_KIND = "assistant_context"
LATEX_SUFFIXES = frozenset({".tex", ".ltx"})
_SIMPLE_INCLUDE_MACROS = frozenset({"input", "include", "subfile"})
_DIRECTORY_INCLUDE_MACROS = frozenset({"import", "subimport"})
_EXTRA_IGNORED_DIRECTORIES = frozenset(
    {".build", ".repoprover", ".swiftpm", "build", "dist", "target"}
)


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
    assistant_context: str
    assistant_references: tuple[str, ...]
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


@dataclass(frozen=True)
class _AssistantContextBlock:
    end: int
    text: str


_ASSISTANT_CONTEXT_START = re.compile(
    r"^[ \t]*%%[ \t]*assistant:[ \t]*(.*?)(?:\r?\n)?$"
)
_ASSISTANT_CONTEXT_CONTINUATION = re.compile(
    r"^[ \t]*%%(?:[ \t]?(.*?))?(?:\r?\n)?$"
)
_ASSISTANT_REFERENCE = re.compile(
    r"\\(?:ref|eqref|cref|Cref|autoref)\s*\{([^{}]+)\}"
)


def _assistant_context_blocks(source: str) -> tuple[_AssistantContextBlock, ...]:
    """Parse consecutive ``%%`` author-to-assistant comment blocks.

    A block starts with ``%% assistant:`` and continues only across immediately
    following ``%%`` lines. The first ordinary LaTeX line ends the block. The
    block is later attached only when it immediately precedes an indexed object,
    so guidance cannot accidentally leak across manuscript prose.
    """

    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    blocks: list[_AssistantContextBlock] = []
    index = 0
    while index < len(lines):
        start = _ASSISTANT_CONTEXT_START.fullmatch(lines[index])
        if start is None:
            index += 1
            continue
        content = [start.group(1).rstrip()]
        index += 1
        while index < len(lines):
            if _ASSISTANT_CONTEXT_START.fullmatch(lines[index]) is not None:
                break
            continuation = _ASSISTANT_CONTEXT_CONTINUATION.fullmatch(lines[index])
            if continuation is None:
                break
            content.append((continuation.group(1) or "").rstrip())
            index += 1
        end = offsets[index] if index < len(lines) else len(source)
        text = "\n".join(content).strip()
        if text:
            blocks.append(_AssistantContextBlock(end=end, text=text))
    return tuple(blocks)


def _assistant_context_for_object(
    source: str,
    *,
    object_start: int,
    blocks: Sequence[_AssistantContextBlock],
) -> str:
    eligible = [
        block
        for block in blocks
        if block.end <= object_start and not source[block.end:object_start].strip()
    ]
    return eligible[-1].text if eligible else ""


def _assistant_references(context: str) -> tuple[str, ...]:
    references: set[str] = set()
    for match in _ASSISTANT_REFERENCE.finditer(context):
        references.update(
            item.strip() for item in match.group(1).split(",") if item.strip()
        )
    return tuple(sorted(references))


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


def discover_latex_sources(source_root: Path) -> tuple[tuple[str, bool], ...]:
    """Return deterministic, source-root-relative LaTeX candidates.

    Symlinks that leave the source tree are rejected instead of being silently
    followed.  The boolean records whether the file contains an uncommented
    ``\\documentclass`` declaration; it is a UI suggestion signal, never an
    implicit selection when several files are present.
    """

    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise LatexIndexError(f"LaTeX source folder does not exist: {root}")
    candidates: list[tuple[str, bool]] = []
    paths: list[Path] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in IGNORED_DIRECTORY_NAMES
            and name not in _EXTRA_IGNORED_DIRECTORIES
            and not (current_path / name).is_symlink()
        )
        paths.extend(
            current_path / name
            for name in sorted(file_names)
            if name not in IGNORED_FILE_NAMES
            and not name.casefold().startswith(".env.")
            and "".join(Path(name).suffixes).casefold() not in IGNORED_FILE_SUFFIXES
            and Path(name).suffix.casefold() in LATEX_SUFFIXES
        )
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise LatexIndexError(
                f"LaTeX source escapes the manuscript folder through a symlink: "
                f"{path.relative_to(root).as_posix()}"
            )
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise LatexIndexError(f"LaTeX source must be UTF-8: {path}") from exc
        candidates.append(
            (
                path.relative_to(root).as_posix(),
                re.search(
                    r"\\documentclass(?:\s*\[[^]]*\])?\s*\{",
                    _strip_comments(source),
                )
                is not None,
            )
        )
    if not candidates:
        raise LatexIndexError(
            f"No .tex or .ltx files were found under {root}; add a LaTeX "
            "manuscript file before creating the project"
        )
    return tuple(candidates)


def _read_braced_argument(
    source: str, start: int, *, macro: str, file: str
) -> tuple[str, int]:
    cursor = start
    while cursor < len(source) and source[cursor].isspace():
        cursor += 1
    if cursor >= len(source) or source[cursor] != "{":
        raise LatexIndexError(
            f"Dynamic or malformed \\{macro} in {file}; included LaTeX paths "
            "must be literal braced paths"
        )
    depth = 1
    end = cursor + 1
    while end < len(source) and depth:
        if source[end] == "{" and (end == 0 or source[end - 1] != "\\"):
            depth += 1
        elif source[end] == "}" and (end == 0 or source[end - 1] != "\\"):
            depth -= 1
        end += 1
    if depth:
        raise LatexIndexError(f"Unclosed argument to \\{macro} in {file}")
    value = source[cursor + 1 : end - 1].strip()
    if not value or any(marker in value for marker in ("\\", "$", "#", "{", "}")):
        raise LatexIndexError(
            f"Dynamic \\{macro} path in {file} is unsupported: {value!r}; "
            "use a literal path so the complete manuscript can be verified"
        )
    return value, end


def _include_arguments(source: str, relative_path: str) -> tuple[tuple[str, str], ...]:
    stripped = _strip_comments(source)
    pattern = re.compile(r"(?<!\\)\\(input|include|subfile|import|subimport)\b")
    includes: list[tuple[str, str]] = []
    for match in pattern.finditer(stripped):
        macro = match.group(1)
        first, cursor = _read_braced_argument(
            stripped, match.end(), macro=macro, file=relative_path
        )
        if macro in _DIRECTORY_INCLUDE_MACROS:
            second, _cursor = _read_braced_argument(
                stripped, cursor, macro=macro, file=relative_path
            )
            includes.append((str(Path(first) / second), macro))
        else:
            includes.append((first, macro))
    return tuple(includes)


def _validated_main_path(source_root: Path, main_file: str) -> tuple[Path, str]:
    root = source_root.expanduser().resolve()
    value = Path(str(main_file))
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise LatexIndexError(
            f"Main LaTeX file must be a relative path inside the source folder: {main_file!r}"
        )
    candidate = root / value
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise LatexIndexError(
            f"Main LaTeX file escapes the source folder: {main_file!r}"
        )
    if not candidate.is_file() or candidate.suffix.casefold() not in LATEX_SUFFIXES:
        raise LatexIndexError(
            f"Selected main LaTeX file does not exist: {value.as_posix()}"
        )
    return candidate, candidate.relative_to(root).as_posix()


def _resolve_include_path(
    source_root: Path,
    including_file: Path,
    raw_path: str,
    *,
    macro: str,
    macro_source: str,
) -> tuple[Path, str]:
    raw = Path(raw_path)
    if raw.is_absolute():
        raise LatexIndexError(
            f"Included LaTeX path escapes the source folder in {macro_source}: {raw_path!r}"
        )
    root = source_root.resolve()
    bases = [including_file.parent / raw]
    if macro in _SIMPLE_INCLUDE_MACROS:
        bases.append(root / raw)
    found: dict[Path, Path] = {}
    escaped = False
    for base in bases:
        choices = (
            (base,)
            if base.suffix
            else (base, base.with_suffix(".tex"), base.with_suffix(".ltx"))
        )
        for choice in choices:
            resolved = choice.resolve()
            if resolved != root and root not in resolved.parents:
                escaped = True
                continue
            if resolved.is_file():
                if resolved.suffix.casefold() not in LATEX_SUFFIXES:
                    raise LatexIndexError(
                        "Included source is not a .tex or .ltx file in "
                        f"{macro_source}: {raw_path!r}"
                    )
                found[resolved] = choice
                break
    if len(found) > 1:
        rendered_choices = ", ".join(
            path.relative_to(root).as_posix() for path in sorted(found)
        )
        raise LatexIndexError(
            f"Ambiguous \\{macro} path in {macro_source}: {raw_path!r} "
            f"matches {rendered_choices}"
        )
    if found:
        resolved = next(iter(found))
        return resolved, resolved.relative_to(root).as_posix()
    if escaped:
        raise LatexIndexError(
            f"Included LaTeX path escapes the source folder in {macro_source}: {raw_path!r}"
        )
    raise LatexIndexError(
        f"Included LaTeX file referenced by {macro_source} does not exist: {raw_path!r}"
    )


def resolve_latex_closure(source_root: Path, main_file: str) -> tuple[str, ...]:
    """Resolve ``main_file`` and every literal recursive LaTeX include once."""

    root = source_root.expanduser().resolve()
    main_path, normalized_main = _validated_main_path(root, main_file)
    ordered: list[str] = []
    visited: set[str] = set()

    def visit(path: Path, relative_path: str) -> None:
        if relative_path in visited:
            return
        visited.add(relative_path)
        ordered.append(relative_path)
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise LatexIndexError(f"LaTeX source must be UTF-8: {path}") from exc
        for raw_include, macro in _include_arguments(source, relative_path):
            child_path, child_relative = _resolve_include_path(
                root,
                path,
                raw_include,
                macro=macro,
                macro_source=relative_path,
            )
            visit(child_path, child_relative)

    visit(main_path, normalized_main)
    return tuple(ordered)


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


def extract_file(
    path: Path,
    relative_path: str,
    *,
    inherited_theorem_environments: dict[str, str] | None = None,
) -> list[_ObjectDraft]:
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
    theorem_kinds.update(inherited_theorem_environments or {})
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
    assistant_blocks = _assistant_context_blocks(source)
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
        proof_start: int | None
        proof_end: int | None
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
        assistant_context = _assistant_context_for_object(
            source, object_start=node.pos, blocks=assistant_blocks
        )
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
                assistant_context=assistant_context,
                assistant_references=_assistant_references(assistant_context),
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


def index_manuscript(
    source_root: Path, store: StateStore, *, main_file: str
) -> tuple[SourceObject, ...]:
    closure = resolve_latex_closure(source_root, main_file)
    inherited: dict[str, str] = {}
    for relative_path in closure:
        path = source_root / relative_path
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise LatexIndexError(f"LaTeX source must be UTF-8: {path}") from exc
        inherited.update(_custom_theorem_environments(source))
    drafts: list[_ObjectDraft] = []
    for relative_path in closure:
        drafts.extend(
            extract_file(
                source_root / relative_path,
                relative_path,
                inherited_theorem_environments=inherited,
            )
        )
    if not drafts:
        raise LatexIndexError(
            "No mathematical objects found in the selected LaTeX closure "
            f"rooted at {main_file!r}"
        )
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
        for reference in item.assistant_references:
            destination = labels.get(reference, reference if reference in ids else None)
            if destination is None:
                unresolved.add((item.claim_id, reference))
            elif destination != item.claim_id:
                edges.add(
                    (item.claim_id, destination, ASSISTANT_CONTEXT_EDGE_KIND)
                )
    return (
        tuple(
            ManuscriptEdge(
                src,
                dst,
                kind,
                (
                    "assistant_annotation"
                    if kind == ASSISTANT_CONTEXT_EDGE_KIND
                    else "latex_ref"
                ),
            )
            for src, dst, kind in sorted(edges)
        ),
        tuple(sorted(unresolved)),
    )
