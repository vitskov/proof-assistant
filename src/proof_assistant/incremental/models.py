from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum

from ..json_types import JSONObject, json_object

SCHEMA_VERSION = 1


class ClaimState(StrEnum):
    DISCOVERED = "DISCOVERED"
    STATEMENT_DRAFTED = "STATEMENT_DRAFTED"
    STATEMENT_APPROVED = "STATEMENT_APPROVED"
    READY_TO_PROVE = "READY_TO_PROVE"
    PROVING = "PROVING"
    CERTIFIED = "CERTIFIED"
    DIRTY_SOURCE = "DIRTY_SOURCE"
    INVALIDATED = "INVALIDATED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    BLOCKED_DEPENDENCY = "BLOCKED_DEPENDENCY"
    FAILED_TECHNICAL = "FAILED_TECHNICAL"
    FAILED_FORMALIZATION = "FAILED_FORMALIZATION"
    UNRESOLVED = "UNRESOLVED"
    SUSPECT_FALSE = "SUSPECT_FALSE"
    COUNTEREXAMPLE_FOUND = "COUNTEREXAMPLE_FOUND"
    SKIPPED_UNPROVED = "SKIPPED_UNPROVED"


TERMINAL_CLAIM_STATES = frozenset(
    {
        ClaimState.CERTIFIED,
        ClaimState.NEEDS_CLARIFICATION,
        ClaimState.BLOCKED_DEPENDENCY,
        ClaimState.UNRESOLVED,
        ClaimState.COUNTEREXAMPLE_FOUND,
        ClaimState.SKIPPED_UNPROVED,
    }
)


ASSERTION_KINDS = frozenset(
    {
        "claim",
        "conjecture",
        "corollary",
        "lemma",
        "observation",
        "proposition",
        "theorem",
    }
)


@dataclass(frozen=True)
class TaskPolicy:
    pause_on_ambiguity: bool = True
    preserve_certified: bool = True
    counterexample_search: bool = True
    require_statement_correspondence_review: bool = False


@dataclass(frozen=True)
class TaskSpec:
    mode: str = "theorem"
    targets: tuple[str, ...] = ()
    policy: TaskPolicy = field(default_factory=TaskPolicy)
    free_form: str = ""
    source_format: str = "text"

    def to_dict(self) -> JSONObject:
        return json_object(asdict(self))


@dataclass(frozen=True)
class SourceFile:
    path: str
    sha256: str
    git_blob: str
    size: int


@dataclass(frozen=True)
class Snapshot:
    commit: str
    tree: str
    previous_commit: str | None
    identical: bool
    files: tuple[SourceFile, ...]


@dataclass(frozen=True, slots=True)
class SourceObject:
    claim_id: str
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

    def export(self) -> JSONObject:
        return json_object(asdict(self))


def is_conjectural_assertion_shape(kind: str, proof_start: int | None) -> bool:
    """Classify the source shape that is never an independent proof target."""

    return kind in ASSERTION_KINDS and (kind == "conjecture" or proof_start is None)


def is_conjectural_assertion(item: SourceObject) -> bool:
    """Return whether host policy must skip this unsupported assertion.

    A conjecture is never a proof obligation. Other theorem-like assertions
    become proof obligations only when the manuscript structurally attaches a
    proof environment. Assumptions, definitions, notation, and equations are
    supporting objects and are intentionally outside this classification.
    """

    return is_conjectural_assertion_shape(item.kind, item.proof_start)


def is_proof_bearing_assertion(item: SourceObject) -> bool:
    """Return whether the manuscript presents an assertion with a proof."""

    return item.kind in ASSERTION_KINDS and not is_conjectural_assertion(item)


def proof_target_ids(task: TaskSpec, objects: tuple[SourceObject, ...]) -> set[str]:
    """Resolve proof targets while always excluding conjectural assertions."""

    requested = (
        set(task.targets)
        if task.targets
        else {item.claim_id for item in objects if item.kind in ASSERTION_KINDS}
    )
    return {
        item.claim_id
        for item in objects
        if item.claim_id in requested and is_proof_bearing_assertion(item)
    }


@dataclass(frozen=True, slots=True)
class ManuscriptEdge:
    src: str
    dst: str
    kind: str
    provenance: str
    approved: bool = True


@dataclass(frozen=True, slots=True)
class LeanDeclaration:
    name: str
    kind: str
    type_hash: str
    value_hash: str | None
    direct_dependencies: tuple[str, ...]
    axioms: tuple[str, ...]

    def export(self) -> JSONObject:
        return json_object(asdict(self))


@dataclass(frozen=True)
class Diagnostic:
    category: str
    message: str
    claim_id: str | None = None
    details: JSONObject = field(default_factory=dict)
