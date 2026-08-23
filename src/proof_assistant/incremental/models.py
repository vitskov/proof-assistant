from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

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


TERMINAL_CLAIM_STATES = frozenset(
    {
        ClaimState.CERTIFIED,
        ClaimState.NEEDS_CLARIFICATION,
        ClaimState.BLOCKED_DEPENDENCY,
        ClaimState.UNRESOLVED,
        ClaimState.COUNTEREXAMPLE_FOUND,
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


@dataclass(frozen=True)
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

    def export(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManuscriptEdge:
    src: str
    dst: str
    kind: str
    provenance: str
    approved: bool = True


@dataclass(frozen=True)
class LeanDeclaration:
    name: str
    kind: str
    type_hash: str
    value_hash: str | None
    direct_dependencies: tuple[str, ...]
    axioms: tuple[str, ...]

    def export(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Diagnostic:
    category: str
    message: str
    claim_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
