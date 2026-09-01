from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from .models import ClaimState, LeanDeclaration, ManuscriptEdge, Snapshot, SourceObject

DATABASE_SCHEMA_VERSION = 3
FAILURE_SCOPES = frozenset({"RUN", "BATCH", "CLAIM", "COMPONENT"})
FAILURE_KINDS = frozenset(
    {
        "CLAIM_TECHNICAL",
        "BATCH_TECHNICAL",
        "PROVIDER",
        "INFRASTRUCTURE",
        "SOURCE_INTEGRITY",
        "DEPENDENCY_CYCLE",
        "UNKNOWN",
    }
)


def _row(cursor: sqlite3.Cursor) -> sqlite3.Row | None:
    """Narrow sqlite's dynamically typed ``fetchone`` at one audited seam."""

    return cast(sqlite3.Row | None, cursor.fetchone())


def _last_row_id(cursor: sqlite3.Cursor) -> int:
    value = cursor.lastrowid
    if value is None:
        raise RuntimeError("SQLite insert did not produce a row id")
    return value


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    snapshot_commit TEXT,
    previous_snapshot_commit TEXT,
    task_sha256 TEXT,
    mode TEXT,
    environment_hash TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_commit TEXT PRIMARY KEY,
    tree_hash TEXT NOT NULL,
    previous_commit TEXT,
    source_root TEXT NOT NULL,
    task_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    manifest_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_files (
    snapshot_commit TEXT NOT NULL REFERENCES snapshots(snapshot_commit),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    git_blob TEXT NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY (snapshot_commit, path)
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    source_file TEXT NOT NULL,
    environment TEXT NOT NULL,
    label TEXT,
    ordinal INTEGER NOT NULL,
    current_statement_hash TEXT NOT NULL,
    current_proof_hash TEXT NOT NULL,
    normalized_statement_hash TEXT NOT NULL,
    current_snapshot TEXT NOT NULL,
    status TEXT NOT NULL,
    lean_declaration TEXT,
    last_changed_run INTEGER,
    retired INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS claims_label_unique
ON claims(label) WHERE label IS NOT NULL AND retired = 0;

CREATE TABLE IF NOT EXISTS claim_versions (
    snapshot_commit TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_file TEXT NOT NULL,
    environment TEXT NOT NULL,
    label TEXT,
    ordinal INTEGER NOT NULL,
    statement_start INTEGER NOT NULL,
    statement_end INTEGER NOT NULL,
    statement_byte_start INTEGER NOT NULL,
    statement_byte_end INTEGER NOT NULL,
    proof_start INTEGER,
    proof_end INTEGER,
    proof_byte_start INTEGER,
    proof_byte_end INTEGER,
    statement_hash TEXT NOT NULL,
    proof_hash TEXT NOT NULL,
    normalized_statement_hash TEXT NOT NULL,
    statement_text TEXT NOT NULL,
    proof_text TEXT NOT NULL,
    references_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_commit, claim_id)
);

CREATE TABLE IF NOT EXISTS manuscript_edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    provenance TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 1,
    first_seen_snapshot TEXT NOT NULL,
    last_seen_snapshot TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (src, dst, edge_kind)
);

CREATE TABLE IF NOT EXISTS lean_declarations (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    type_hash TEXT NOT NULL,
    value_hash TEXT,
    axioms_json TEXT NOT NULL,
    last_seen_run INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS lean_edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    last_seen_run INTEGER NOT NULL,
    PRIMARY KEY (src, dst)
);

CREATE TABLE IF NOT EXISTS correspondence (
    claim_id TEXT PRIMARY KEY,
    lean_declaration TEXT NOT NULL,
    status TEXT NOT NULL,
    provenance TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 1,
    last_updated_run INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS certificates (
    claim_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    manuscript_snapshot TEXT NOT NULL,
    statement_hash TEXT NOT NULL,
    formal_type_hash TEXT NOT NULL,
    lean_declaration TEXT NOT NULL,
    lean_value_hash TEXT,
    dependencies_json TEXT NOT NULL,
    lean_dependencies_json TEXT NOT NULL,
    axioms_json TEXT NOT NULL,
    environment_hash TEXT NOT NULL,
    lean_version TEXT NOT NULL,
    mathlib_revision TEXT,
    last_verified_run INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clarifications (
    question_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    snapshot_commit TEXT NOT NULL,
    category TEXT NOT NULL,
    passage TEXT NOT NULL,
    problem TEXT NOT NULL,
    resolutions_json TEXT NOT NULL,
    blocking_claims_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_run INTEGER NOT NULL,
    resolved_run INTEGER,
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS run_claims (
    run_id INTEGER NOT NULL,
    claim_id TEXT NOT NULL,
    action TEXT NOT NULL,
    state_before TEXT,
    state_after TEXT,
    reason TEXT,
    reused INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, claim_id, action)
);

CREATE TABLE IF NOT EXISTS diagnostics (
    diagnostic_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    claim_id TEXT,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS edge_dst_active ON manuscript_edges(dst, active);
CREATE INDEX IF NOT EXISTS edge_src_active ON manuscript_edges(src, active);
CREATE INDEX IF NOT EXISTS claim_versions_snapshot ON claim_versions(snapshot_commit);
CREATE INDEX IF NOT EXISTS clarification_open ON clarifications(status, claim_id);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_question_per_claim
ON clarifications(claim_id) WHERE status = 'OPEN';
"""

MIGRATION_2_SQL = """
CREATE TABLE IF NOT EXISTS run_scope (
    run_id INTEGER NOT NULL,
    claim_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('TARGET', 'SELECTED')),
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (run_id, role, claim_id),
    UNIQUE (run_id, role, ordinal)
);

CREATE TABLE IF NOT EXISTS run_dependency_edges (
    run_id INTEGER NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    provenance TEXT NOT NULL,
    approved INTEGER NOT NULL,
    PRIMARY KEY (run_id, src, dst, edge_kind)
);

CREATE TABLE IF NOT EXISTS run_claim_nodes (
    run_id INTEGER NOT NULL,
    claim_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_file TEXT NOT NULL,
    statement_start INTEGER NOT NULL,
    statement_end INTEGER NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (run_id, claim_id)
);

CREATE TABLE IF NOT EXISTS failure_incidents (
    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    failure_kind TEXT NOT NULL,
    phase TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT,
    provenance TEXT NOT NULL,
    batch_index INTEGER,
    retryable INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS failure_incident_claims (
    failure_id INTEGER NOT NULL REFERENCES failure_incidents(failure_id)
        ON DELETE CASCADE,
    claim_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (failure_id, claim_id),
    UNIQUE (failure_id, ordinal)
);

CREATE TABLE IF NOT EXISTS failure_artifacts (
    failure_id INTEGER NOT NULL REFERENCES failure_incidents(failure_id)
        ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    path TEXT NOT NULL,
    label TEXT NOT NULL,
    sha256 TEXT,
    command_json TEXT NOT NULL,
    exit_code INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (failure_id, ordinal)
);

CREATE INDEX IF NOT EXISTS run_scope_run_role
ON run_scope(run_id, role, ordinal);
CREATE INDEX IF NOT EXISTS run_dependency_edges_run
ON run_dependency_edges(run_id, src, dst, edge_kind);
CREATE INDEX IF NOT EXISTS run_claim_nodes_run
ON run_claim_nodes(run_id, claim_id);
CREATE INDEX IF NOT EXISTS failure_incidents_run
ON failure_incidents(run_id, failure_id);
CREATE INDEX IF NOT EXISTS failure_incident_claims_claim
ON failure_incident_claims(claim_id, failure_id);
"""

MIGRATION_3_SQL = """
CREATE TABLE IF NOT EXISTS run_concurrency (
    run_id INTEGER PRIMARY KEY,
    configured_json TEXT NOT NULL,
    initial_effective_json TEXT NOT NULL,
    final_effective_json TEXT,
    telemetry_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
"""


class StateStore:
    """SQLite authority for deterministic incremental verification state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.executescript(SCHEMA_SQL)
        self.set_metadata_default("schema_version", "1")
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        raw_version = self.get_metadata("schema_version") or "1"
        try:
            version = int(raw_version)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid verification database schema version: {raw_version!r}"
            ) from exc
        if version > DATABASE_SCHEMA_VERSION:
            raise RuntimeError(
                "Verification database schema is newer than this package "
                f"({version} > {DATABASE_SCHEMA_VERSION})"
            )
        # Every statement is idempotent, so an interrupted migration is safely
        # completed on the next open before the version is advanced. Running
        # current migration bodies for an existing database also permit
        # additive tables introduced by pre-release builds.
        self.connection.executescript(MIGRATION_2_SQL)
        self.connection.executescript(MIGRATION_3_SQL)
        if version < DATABASE_SCHEMA_VERSION:
            self.set_metadata("schema_version", str(DATABASE_SCHEMA_VERSION))

    def close(self) -> None:
        self.connection.close()

    def backup_to(self, path: Path) -> None:
        """Create a transactionally consistent SQLite copy for read-only planning."""
        path.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(path)
        try:
            self.connection.backup(destination)
        finally:
            destination.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )

    def set_metadata_default(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)", (key, value)
        )

    def get_metadata(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_metadata(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def allocate_claim_id(self, kind: str) -> str:
        with self.transaction():
            current = int(self.get_metadata("next_generated_claim_id") or "1")
            self.set_metadata("next_generated_claim_id", str(current + 1))
        return f"{kind}:auto-{current:06d}"

    def recover_interrupted_runs(self, now: str) -> int:
        """Recover abandoned runs and make every in-flight claim retryable.

        This method is called while the project writer lock is held, before a
        new run is allocated.  Consequently, any remaining ``PROVING`` state
        belongs to an abandoned writer and must never survive recovery.
        ``INVALIDATED`` is deliberately used as the conservative retry state:
        it carries no certification authority and is part of the scheduler's
        ready frontier.
        """
        with self.transaction() as connection:
            running = list(
                connection.execute(
                    "SELECT run_id FROM runs WHERE status = 'RUNNING' ORDER BY run_id"
                )
            )
            running_ids = {int(row["run_id"]) for row in running}
            proving_run_ids = {
                int(row["last_changed_run"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT last_changed_run FROM claims
                    WHERE status = ? AND retired = 0
                      AND last_changed_run IS NOT NULL
                    """,
                    (str(ClaimState.PROVING),),
                )
            }
            for run_id in sorted(proving_run_ids):
                self.reset_in_flight_claims(
                    run_id=run_id,
                    action=(
                        "recover_interrupted"
                        if run_id in running_ids
                        else "recover_orphaned_proving"
                    ),
                    reason=(
                        "Recovered after process interruption; proof must be retried"
                        if run_id in running_ids
                        else "Recovered stale in-flight state; proof must be retried"
                    ),
                    connection=connection,
                )
            # A PROVING row without provenance is malformed legacy state, but
            # it is still non-authoritative and must not strand the scheduler.
            connection.execute(
                """
                UPDATE claims SET status = ?
                WHERE status = ? AND retired = 0 AND last_changed_run IS NULL
                """,
                (str(ClaimState.INVALIDATED), str(ClaimState.PROVING)),
            )
            cursor = connection.execute(
                """
                UPDATE runs SET status = 'INTERRUPTED', outcome = 'interrupted',
                    completed_at = ?, detail = 'Recovered after process interruption'
                WHERE status = 'RUNNING'
                """,
                (now,),
            )
        return cursor.rowcount

    def reset_in_flight_claims(
        self,
        *,
        run_id: int,
        action: str,
        reason: str,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[str, ...]:
        """Reset this run's non-authoritative ``PROVING`` claims for retry."""
        database = connection or self.connection
        rows = list(
            database.execute(
                """
                SELECT claim_id FROM claims
                WHERE status = ? AND last_changed_run = ? AND retired = 0
                ORDER BY claim_id
                """,
                (str(ClaimState.PROVING), run_id),
            )
        )
        for row in rows:
            claim_id = str(row["claim_id"])
            database.execute(
                """
                UPDATE claims SET status = ?, last_changed_run = ?
                WHERE claim_id = ?
                """,
                (str(ClaimState.INVALIDATED), run_id, claim_id),
            )
            database.execute(
                """
                INSERT OR REPLACE INTO run_claims(
                    run_id, claim_id, action, state_before, state_after, reason, reused
                ) VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    run_id,
                    claim_id,
                    action,
                    str(ClaimState.PROVING),
                    str(ClaimState.INVALIDATED),
                    reason,
                ),
            )
        return tuple(str(row["claim_id"]) for row in rows)

    def begin_run(
        self,
        *,
        command: str,
        started_at: str,
        snapshot_commit: str | None = None,
        previous_snapshot_commit: str | None = None,
        task_sha256: str | None = None,
        mode: str | None = None,
        environment_hash: str | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO runs(
                command, status, started_at, snapshot_commit,
                previous_snapshot_commit, task_sha256, mode, environment_hash
            ) VALUES (?, 'RUNNING', ?, ?, ?, ?, ?, ?)
            """,
            (
                command,
                started_at,
                snapshot_commit,
                previous_snapshot_commit,
                task_sha256,
                mode,
                environment_hash,
            ),
        )
        return _last_row_id(cursor)

    def update_run_snapshot(
        self,
        run_id: int,
        *,
        snapshot_commit: str,
        previous_snapshot_commit: str | None,
    ) -> None:
        self.connection.execute(
            "UPDATE runs SET snapshot_commit = ?, previous_snapshot_commit = ? WHERE run_id = ?",
            (snapshot_commit, previous_snapshot_commit, run_id),
        )

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        outcome: str,
        completed_at: str,
        detail: str,
    ) -> None:
        self.connection.execute(
            """
            UPDATE runs SET status = ?, outcome = ?, completed_at = ?, detail = ?
            WHERE run_id = ?
            """,
            (status, outcome, completed_at, detail, run_id),
        )

    def latest_run(self) -> sqlite3.Row | None:
        return _row(
            self.connection.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1")
        )

    def run_row(self, run_id: int) -> sqlite3.Row | None:
        return _row(
            self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        )

    def record_run_concurrency(
        self,
        run_id: int,
        *,
        configured: Any,
        initial_effective: Any,
        telemetry: Any,
    ) -> None:
        """Persist the configured and initially effective resource policy.

        Concurrency is operational provenance, not proof authority.  It is kept
        beside the run so an adaptive execution remains explainable without
        changing certificate semantics.
        """

        self.connection.execute(
            """
            INSERT INTO run_concurrency(
                run_id, configured_json, initial_effective_json, telemetry_json
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                configured_json = excluded.configured_json,
                initial_effective_json = excluded.initial_effective_json,
                telemetry_json = excluded.telemetry_json
            """,
            (
                run_id,
                self._json(configured),
                self._json(initial_effective),
                self._json(telemetry),
            ),
        )

    def finish_run_concurrency(
        self,
        run_id: int,
        *,
        final_effective: Any,
        telemetry: Any,
    ) -> None:
        self.connection.execute(
            """
            UPDATE run_concurrency
            SET final_effective_json = ?, telemetry_json = ?
            WHERE run_id = ?
            """,
            (self._json(final_effective), self._json(telemetry), run_id),
        )

    def run_concurrency(self, run_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM run_concurrency WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "configured": json.loads(str(row["configured_json"])),
            "initial_effective": json.loads(str(row["initial_effective_json"])),
            "final_effective": (
                json.loads(str(row["final_effective_json"]))
                if row["final_effective_json"] is not None
                else None
            ),
            "telemetry": json.loads(str(row["telemetry_json"])),
        }

    def latest_failure_run(self) -> sqlite3.Row | None:
        return _row(
            self.connection.execute(
                """
            SELECT runs.* FROM runs
            WHERE EXISTS (
                SELECT 1 FROM failure_incidents
                WHERE failure_incidents.run_id = runs.run_id
            )
               OR EXISTS (
                SELECT 1 FROM run_claims
                WHERE run_claims.run_id = runs.run_id
                  AND run_claims.state_after = ?
            )
               OR runs.outcome IN (
                'provider_failure',
                'lean_infrastructure_failure',
                'setup_failure'
            )
            ORDER BY runs.run_id DESC LIMIT 1
            """,
                (str(ClaimState.FAILED_TECHNICAL),),
            )
        )

    def record_run_scope(
        self,
        run_id: int,
        *,
        targets: Sequence[str],
        selected: Sequence[str],
    ) -> None:
        """Persist canonical target and selected-claim order for one run."""

        with self.transaction() as connection:
            connection.execute("DELETE FROM run_scope WHERE run_id = ?", (run_id,))
            for role, values in (
                ("TARGET", tuple(sorted(set(targets)))),
                ("SELECTED", tuple(sorted(set(selected)))),
            ):
                connection.executemany(
                    """
                    INSERT INTO run_scope(run_id, claim_id, role, ordinal)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (run_id, claim_id, role, ordinal)
                        for ordinal, claim_id in enumerate(values)
                    ],
                )

    def run_scope_rows(self, run_id: int, role: str) -> list[sqlite3.Row]:
        if role not in {"TARGET", "SELECTED"}:
            raise ValueError(f"Invalid run-scope role: {role}")
        return list(
            self.connection.execute(
                """
                SELECT * FROM run_scope WHERE run_id = ? AND role = ?
                ORDER BY ordinal, claim_id
                """,
                (run_id, role),
            )
        )

    def replace_run_dependency_edges(
        self, run_id: int, edges: Sequence[ManuscriptEdge]
    ) -> None:
        """Record the immutable final manuscript graph used by one run."""

        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM run_dependency_edges WHERE run_id = ?", (run_id,)
            )
            connection.executemany(
                """
                INSERT INTO run_dependency_edges(
                    run_id, src, dst, edge_kind, provenance, approved
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        edge.src,
                        edge.dst,
                        edge.kind,
                        edge.provenance,
                        int(edge.approved),
                    )
                    for edge in sorted(
                        edges, key=lambda item: (item.src, item.dst, item.kind)
                    )
                ],
            )

    def run_dependency_edge_rows(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM run_dependency_edges WHERE run_id = ?
                ORDER BY src, dst, edge_kind
                """,
                (run_id,),
            )
        )

    def record_run_claim_nodes(self, run_id: int) -> None:
        """Freeze selected claim metadata and state at this run's report boundary."""

        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM run_claim_nodes WHERE run_id = ?", (run_id,)
            )
            connection.execute(
                """
                WITH included(claim_id) AS (
                    SELECT claim_id FROM run_scope WHERE run_id = ?
                    UNION
                    SELECT failure_incident_claims.claim_id
                    FROM failure_incident_claims
                    INNER JOIN failure_incidents
                        ON failure_incidents.failure_id =
                           failure_incident_claims.failure_id
                    WHERE failure_incidents.run_id = ?
                    UNION
                    SELECT src FROM run_dependency_edges WHERE run_id = ?
                    UNION
                    SELECT dst FROM run_dependency_edges WHERE run_id = ?
                )
                INSERT INTO run_claim_nodes(
                    run_id, claim_id, kind, source_file,
                    statement_start, statement_end, state
                )
                SELECT ?, claims.claim_id, versions.kind, versions.source_file,
                       versions.statement_start, versions.statement_end,
                       claims.status
                FROM included
                INNER JOIN claims ON claims.claim_id = included.claim_id
                INNER JOIN runs ON runs.run_id = ?
                INNER JOIN claim_versions AS versions
                    ON versions.claim_id = claims.claim_id
                   AND versions.snapshot_commit = runs.snapshot_commit
                WHERE claims.retired = 0
                ORDER BY claims.claim_id
                """,
                (run_id, run_id, run_id, run_id, run_id, run_id),
            )

    def run_claim_node_rows(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM run_claim_nodes WHERE run_id = ? ORDER BY claim_id
                """,
                (run_id,),
            )
        )

    def add_failure_incident(
        self,
        *,
        run_id: int,
        scope: str,
        failure_kind: str,
        phase: str,
        category: str,
        message: str,
        provenance: str,
        claim_ids: Sequence[str] = (),
        detail: str | None = None,
        batch_index: int | None = None,
        retryable: bool = False,
        artifacts: Sequence[dict[str, Any]] = (),
    ) -> int:
        """Append one exact, structured failure observation and its evidence."""

        if scope not in FAILURE_SCOPES:
            raise ValueError(f"Invalid failure scope: {scope}")
        if failure_kind not in FAILURE_KINDS:
            raise ValueError(f"Invalid failure kind: {failure_kind}")
        if not phase or not category or not message or not provenance:
            raise ValueError(
                "Failure phase, category, message, and provenance must be non-empty"
            )
        unknown_claims = sorted(
            claim_id for claim_id in set(claim_ids) if self.claim_row(claim_id) is None
        )
        if unknown_claims:
            raise ValueError(
                "Failure incident identifies unknown claims: "
                + ", ".join(unknown_claims)
            )
        selected = {
            str(row["claim_id"]) for row in self.run_scope_rows(run_id, "SELECTED")
        }
        outside_scope = sorted(set(claim_ids) - selected) if selected else []
        if outside_scope:
            raise ValueError(
                "Failure incident identifies claims outside this run's selected scope: "
                + ", ".join(outside_scope)
            )
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO failure_incidents(
                    run_id, scope, failure_kind, phase, category, message,
                    detail, provenance, batch_index, retryable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    scope,
                    failure_kind,
                    phase,
                    category,
                    message,
                    detail,
                    provenance,
                    batch_index,
                    int(retryable),
                ),
            )
            failure_id = _last_row_id(cursor)
            unique_claims = tuple(sorted(set(claim_ids)))
            connection.executemany(
                """
                INSERT INTO failure_incident_claims(failure_id, claim_id, ordinal)
                VALUES (?, ?, ?)
                """,
                [
                    (failure_id, claim_id, ordinal)
                    for ordinal, claim_id in enumerate(unique_claims)
                ],
            )
            connection.executemany(
                """
                INSERT INTO failure_artifacts(
                    failure_id, ordinal, path, label, sha256, command_json,
                    exit_code, timed_out
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        failure_id,
                        ordinal,
                        str(artifact["path"]),
                        str(artifact.get("label") or "Failure artifact"),
                        artifact.get("sha256"),
                        self._json(tuple(artifact.get("command") or ())),
                        artifact.get("exit_code"),
                        int(bool(artifact.get("timed_out", False))),
                    )
                    for ordinal, artifact in enumerate(artifacts)
                ],
            )
        return failure_id

    def failure_incident_rows(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM failure_incidents WHERE run_id = ?
                ORDER BY failure_id
                """,
                (run_id,),
            )
        )

    def failure_claim_rows(self, failure_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM failure_incident_claims WHERE failure_id = ?
                ORDER BY ordinal, claim_id
                """,
                (failure_id,),
            )
        )

    def failure_artifact_rows(self, failure_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM failure_artifacts WHERE failure_id = ?
                ORDER BY ordinal
                """,
                (failure_id,),
            )
        )

    def record_snapshot(
        self,
        snapshot: Snapshot,
        *,
        source_root: Path,
        task_sha256: str,
        created_at: str,
    ) -> None:
        manifest = {
            "commit": snapshot.commit,
            "tree": snapshot.tree,
            "files": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "git_blob": item.git_blob,
                    "size": item.size,
                }
                for item in snapshot.files
            ],
        }
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO snapshots(
                    snapshot_commit, tree_hash, previous_commit, source_root,
                    task_sha256, created_at, manifest_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.commit,
                    snapshot.tree,
                    snapshot.previous_commit,
                    str(source_root),
                    task_sha256,
                    created_at,
                    self._json(manifest),
                ),
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO source_files(
                    snapshot_commit, path, sha256, git_blob, size
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.commit,
                        item.path,
                        item.sha256,
                        item.git_blob,
                        item.size,
                    )
                    for item in snapshot.files
                ],
            )

    def previous_snapshot(self) -> str | None:
        return self.get_metadata("current_snapshot")

    def snapshot_row(self, snapshot: str) -> sqlite3.Row | None:
        return _row(
            self.connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_commit = ?", (snapshot,)
            )
        )

    def source_file_rows(self, snapshot: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM source_files WHERE snapshot_commit = ? ORDER BY path",
                (snapshot,),
            )
        )

    def current_claim_rows(self, *, include_retired: bool = False) -> list[sqlite3.Row]:
        suffix = "" if include_retired else " WHERE retired = 0"
        return list(
            self.connection.execute(
                "SELECT * FROM claims"
                + suffix
                + " ORDER BY source_file, ordinal, claim_id"
            )
        )

    def claim_row(self, claim_id: str) -> sqlite3.Row | None:
        return _row(
            self.connection.execute(
                "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
            )
        )

    def claim_version(self, snapshot: str, claim_id: str) -> sqlite3.Row | None:
        return _row(
            self.connection.execute(
                "SELECT * FROM claim_versions WHERE snapshot_commit = ? AND claim_id = ?",
                (snapshot, claim_id),
            )
        )

    def claim_versions(self, snapshot: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM claim_versions WHERE snapshot_commit = ?
                ORDER BY source_file, ordinal, claim_id
                """,
                (snapshot,),
            )
        )

    def replace_current_claims(
        self,
        snapshot: str,
        objects: Sequence[SourceObject],
        *,
        run_id: int,
        state_updates: dict[str, ClaimState],
    ) -> None:
        active_ids = {item.claim_id for item in objects}
        with self.transaction() as connection:
            connection.execute("UPDATE claims SET retired = 1")
            for item in objects:
                prior = connection.execute(
                    "SELECT status, lean_declaration FROM claims WHERE claim_id = ?",
                    (item.claim_id,),
                ).fetchone()
                state = state_updates.get(
                    item.claim_id,
                    ClaimState(prior["status"]) if prior else ClaimState.DISCOVERED,
                )
                lean_declaration = prior["lean_declaration"] if prior else None
                connection.execute(
                    """
                    INSERT INTO claims(
                        claim_id, kind, source_file, environment, label, ordinal,
                        current_statement_hash, current_proof_hash,
                        normalized_statement_hash, current_snapshot, status,
                        lean_declaration, last_changed_run, retired
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(claim_id) DO UPDATE SET
                        kind = excluded.kind,
                        source_file = excluded.source_file,
                        environment = excluded.environment,
                        label = excluded.label,
                        ordinal = excluded.ordinal,
                        current_statement_hash = excluded.current_statement_hash,
                        current_proof_hash = excluded.current_proof_hash,
                        normalized_statement_hash = excluded.normalized_statement_hash,
                        current_snapshot = excluded.current_snapshot,
                        status = excluded.status,
                        lean_declaration = COALESCE(claims.lean_declaration, excluded.lean_declaration),
                        last_changed_run = excluded.last_changed_run,
                        retired = 0
                    """,
                    (
                        item.claim_id,
                        item.kind,
                        item.source_file,
                        item.environment,
                        item.label,
                        item.ordinal,
                        item.statement_hash,
                        item.proof_hash,
                        item.normalized_statement_hash,
                        snapshot,
                        str(state),
                        lean_declaration,
                        run_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO claim_versions(
                        snapshot_commit, claim_id, kind, source_file, environment,
                        label, ordinal, statement_start, statement_end,
                        statement_byte_start, statement_byte_end, proof_start,
                        proof_end, proof_byte_start, proof_byte_end, statement_hash,
                        proof_hash, normalized_statement_hash, statement_text,
                        proof_text, references_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot,
                        item.claim_id,
                        item.kind,
                        item.source_file,
                        item.environment,
                        item.label,
                        item.ordinal,
                        item.statement_start,
                        item.statement_end,
                        item.statement_byte_start,
                        item.statement_byte_end,
                        item.proof_start,
                        item.proof_end,
                        item.proof_byte_start,
                        item.proof_byte_end,
                        item.statement_hash,
                        item.proof_hash,
                        item.normalized_statement_hash,
                        item.statement_text,
                        item.proof_text,
                        self._json(item.references),
                    ),
                )
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                connection.execute(
                    f"UPDATE claims SET retired = 1 WHERE claim_id NOT IN ({placeholders})",
                    tuple(sorted(active_ids)),
                )
            self.set_metadata("current_snapshot", snapshot)

    def set_claim_state(
        self,
        claim_id: str,
        state: ClaimState,
        *,
        run_id: int,
        action: str,
        reason: str,
        reused: bool = False,
    ) -> None:
        prior = self.claim_row(claim_id)
        before = str(prior["status"]) if prior else None
        self.connection.execute(
            "UPDATE claims SET status = ?, last_changed_run = ? WHERE claim_id = ?",
            (str(state), run_id, claim_id),
        )
        self.connection.execute(
            """
            INSERT OR REPLACE INTO run_claims(
                run_id, claim_id, action, state_before, state_after, reason, reused
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, claim_id, action, before, str(state), reason, int(reused)),
        )

    def replace_manuscript_edges(
        self, snapshot: str, edges: Sequence[ManuscriptEdge]
    ) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE manuscript_edges SET active = 0")
            for edge in edges:
                connection.execute(
                    """
                    INSERT INTO manuscript_edges(
                        src, dst, edge_kind, provenance, approved,
                        first_seen_snapshot, last_seen_snapshot, active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(src, dst, edge_kind) DO UPDATE SET
                        provenance = excluded.provenance,
                        approved = excluded.approved,
                        last_seen_snapshot = excluded.last_seen_snapshot,
                        active = 1
                    """,
                    (
                        edge.src,
                        edge.dst,
                        edge.kind,
                        edge.provenance,
                        int(edge.approved),
                        snapshot,
                        snapshot,
                    ),
                )

    def manuscript_edges(self, *, active_only: bool = True) -> list[sqlite3.Row]:
        where = " WHERE active = 1" if active_only else ""
        return list(
            self.connection.execute(
                "SELECT * FROM manuscript_edges"
                + where
                + " ORDER BY src, dst, edge_kind"
            )
        )

    def add_manuscript_edge(
        self,
        edge: ManuscriptEdge,
        *,
        snapshot: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO manuscript_edges(
                src, dst, edge_kind, provenance, approved,
                first_seen_snapshot, last_seen_snapshot, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(src, dst, edge_kind) DO UPDATE SET
                provenance = excluded.provenance,
                approved = excluded.approved,
                last_seen_snapshot = excluded.last_seen_snapshot,
                active = 1
            """,
            (
                edge.src,
                edge.dst,
                edge.kind,
                edge.provenance,
                int(edge.approved),
                snapshot,
                snapshot,
            ),
        )

    def replace_lean_graph(
        self, declarations: Sequence[LeanDeclaration], *, run_id: int
    ) -> None:
        names = {item.name for item in declarations}
        with self.transaction() as connection:
            for declaration in declarations:
                connection.execute(
                    """
                    INSERT INTO lean_declarations(
                        name, kind, type_hash, value_hash, axioms_json, last_seen_run
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        kind = excluded.kind,
                        type_hash = excluded.type_hash,
                        value_hash = excluded.value_hash,
                        axioms_json = excluded.axioms_json,
                        last_seen_run = excluded.last_seen_run
                    """,
                    (
                        declaration.name,
                        declaration.kind,
                        declaration.type_hash,
                        declaration.value_hash,
                        self._json(declaration.axioms),
                        run_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM lean_edges WHERE src = ?", (declaration.name,)
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO lean_edges(src, dst, last_seen_run) VALUES (?, ?, ?)",
                    [
                        (declaration.name, dependency, run_id)
                        for dependency in declaration.direct_dependencies
                    ],
                )
            if names:
                placeholders = ",".join("?" for _ in names)
                connection.execute(
                    f"DELETE FROM lean_edges WHERE src NOT IN ({placeholders})",
                    tuple(sorted(names)),
                )

    def lean_declaration(self, name: str) -> sqlite3.Row | None:
        return _row(
            self.connection.execute(
                "SELECT * FROM lean_declarations WHERE name = ?", (name,)
            )
        )

    def lean_dependencies(self, name: str) -> tuple[str, ...]:
        return tuple(
            row["dst"]
            for row in self.connection.execute(
                "SELECT dst FROM lean_edges WHERE src = ? ORDER BY dst", (name,)
            )
        )

    def set_correspondence(
        self,
        claim_id: str,
        lean_declaration: str,
        *,
        run_id: int,
        status: str = "proposed",
        provenance: str = "agent",
        approved: bool = True,
    ) -> None:
        if self.claim_row(claim_id) is None:
            raise KeyError(f"Unknown claim: {claim_id}")
        self.connection.execute(
            """
            INSERT INTO correspondence(
                claim_id, lean_declaration, status, provenance, approved, last_updated_run
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(claim_id) DO UPDATE SET
                lean_declaration = excluded.lean_declaration,
                status = excluded.status,
                provenance = excluded.provenance,
                approved = excluded.approved,
                last_updated_run = excluded.last_updated_run
            """,
            (claim_id, lean_declaration, status, provenance, int(approved), run_id),
        )
        self.connection.execute(
            "UPDATE claims SET lean_declaration = ? WHERE claim_id = ?",
            (lean_declaration, claim_id),
        )

    def correspondence_rows(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute("SELECT * FROM correspondence ORDER BY claim_id")
        )

    def certificate(self, claim_id: str) -> sqlite3.Row | None:
        return _row(
            self.connection.execute(
                "SELECT * FROM certificates WHERE claim_id = ?", (claim_id,)
            )
        )

    def upsert_certificate(self, payload: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO certificates(
                claim_id, status, manuscript_snapshot, statement_hash,
                formal_type_hash, lean_declaration, lean_value_hash,
                dependencies_json, lean_dependencies_json, axioms_json,
                environment_hash, lean_version, mathlib_revision, last_verified_run
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(claim_id) DO UPDATE SET
                status = excluded.status,
                manuscript_snapshot = excluded.manuscript_snapshot,
                statement_hash = excluded.statement_hash,
                formal_type_hash = excluded.formal_type_hash,
                lean_declaration = excluded.lean_declaration,
                lean_value_hash = excluded.lean_value_hash,
                dependencies_json = excluded.dependencies_json,
                lean_dependencies_json = excluded.lean_dependencies_json,
                axioms_json = excluded.axioms_json,
                environment_hash = excluded.environment_hash,
                lean_version = excluded.lean_version,
                mathlib_revision = excluded.mathlib_revision,
                last_verified_run = excluded.last_verified_run
            """,
            (
                payload["claim_id"],
                payload.get("status", "CERTIFIED"),
                payload["manuscript_snapshot"],
                payload["statement_hash"],
                payload["formal_type_hash"],
                payload["lean_declaration"],
                payload.get("lean_value_hash"),
                self._json(payload.get("dependencies", [])),
                self._json(payload.get("lean_dependencies", [])),
                self._json(payload.get("axioms", [])),
                payload["environment_hash"],
                payload["lean_version"],
                payload.get("mathlib_revision"),
                payload["last_verified_run"],
            ),
        )

    def certificate_rows(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute("SELECT * FROM certificates ORDER BY claim_id")
        )

    def discard_certificate_for_reproof(self, claim_id: str, *, run_id: int) -> None:
        """Remove reusable proof authority while retaining the historical run log."""
        self.connection.execute(
            "DELETE FROM certificates WHERE claim_id = ?", (claim_id,)
        )
        self.connection.execute(
            """
            UPDATE correspondence
            SET status = 'stale_reproof_requested', approved = 0,
                last_updated_run = ?
            WHERE claim_id = ?
            """,
            (run_id, claim_id),
        )

    def next_question_id(self) -> str:
        row = self.connection.execute(
            "SELECT MAX(CAST(SUBSTR(question_id, 2) AS INTEGER)) AS maximum FROM clarifications"
        ).fetchone()
        return f"Q{int(row['maximum'] or 0) + 1:04d}"

    def create_question(
        self,
        *,
        claim_id: str,
        snapshot: str,
        category: str,
        passage: str,
        problem: str,
        possible_resolutions: Sequence[str],
        blocking_claims: Sequence[str],
        run_id: int,
    ) -> str:
        if self.claim_row(claim_id) is None:
            raise KeyError(f"Unknown claim: {claim_id}")
        existing = self.connection.execute(
            "SELECT question_id FROM clarifications WHERE claim_id = ? AND status = 'OPEN'",
            (claim_id,),
        ).fetchone()
        if existing is not None:
            return str(existing["question_id"])
        question_id = self.next_question_id()
        self.connection.execute(
            """
            INSERT INTO clarifications(
                question_id, claim_id, snapshot_commit, category, passage,
                problem, resolutions_json, blocking_claims_json, status, created_run
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (
                question_id,
                claim_id,
                snapshot,
                category,
                passage,
                problem,
                self._json(tuple(possible_resolutions)),
                self._json(tuple(blocking_claims)),
                run_id,
            ),
        )
        return question_id

    def open_questions(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM clarifications WHERE status = 'OPEN' ORDER BY question_id"
            )
        )

    def refresh_open_question(
        self,
        claim_id: str,
        *,
        snapshot: str,
        category: str,
        passage: str,
        problem: str,
        possible_resolutions: Sequence[str],
        blocking_claims: Sequence[str],
    ) -> None:
        """Refresh deterministic facts for an existing policy question."""

        self.connection.execute(
            """
            UPDATE clarifications SET
                snapshot_commit = ?, category = ?, passage = ?, problem = ?,
                resolutions_json = ?, blocking_claims_json = ?
            WHERE claim_id = ? AND status = 'OPEN'
            """,
            (
                snapshot,
                category,
                passage,
                problem,
                self._json(tuple(possible_resolutions)),
                self._json(tuple(sorted(set(blocking_claims)))),
                claim_id,
            ),
        )

    def question_row(self, question_id: str) -> sqlite3.Row | None:
        return _row(
            self.connection.execute(
                "SELECT * FROM clarifications WHERE question_id = ?", (question_id,)
            )
        )

    def run_claim_rows(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM run_claims WHERE run_id = ? ORDER BY claim_id, action",
                (run_id,),
            )
        )

    def diagnostics_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM diagnostics WHERE run_id = ? ORDER BY diagnostic_id",
                (run_id,),
            )
        )

    def resolve_question(
        self, question_id: str, *, run_id: int | None, resolution: str, status: str
    ) -> bool:
        if status not in {"RESOLVED", "DISMISSED", "SUPERSEDED"}:
            raise ValueError(f"Invalid question resolution state: {status}")
        cursor = self.connection.execute(
            """
            UPDATE clarifications SET status = ?, resolved_run = ?, resolution = ?
            WHERE question_id = ? AND status = 'OPEN'
            """,
            (status, run_id, resolution, question_id),
        )
        return cursor.rowcount == 1

    def add_diagnostic(
        self,
        *,
        run_id: int,
        category: str,
        message: str,
        claim_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO diagnostics(run_id, claim_id, category, message, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, claim_id, category, message, self._json(details or {})),
        )

    def summary_counts(self) -> dict[str, int]:
        return {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                """
                SELECT status, COUNT(*) AS count FROM claims
                WHERE retired = 0 GROUP BY status ORDER BY status
                """
            )
        }
