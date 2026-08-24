"""UI-neutral durable control plane for detached verification workers.

The manuscript database remains authoritative for proof state. This separate
SQLite store owns only process lifecycle, replayable progress, and cooperative
cancellation, so replacing or terminating a UI cannot terminate verification.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..incremental.io import atomic_write_text
from .contracts import (
    ProgressEvent,
    ProgressPhase,
    VerificationJob,
    VerificationJobObservation,
    VerificationJobState,
    VerificationSettings,
)

_ACTIVE_STATES = (
    str(VerificationJobState.STARTING),
    str(VerificationJobState.RUNNING),
    str(VerificationJobState.CANCEL_REQUESTED),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class VerificationJobStore:
    """Small transactional job/event store scoped to one managed project."""

    def __init__(self, project: Path) -> None:
        self.project = project.expanduser().resolve(strict=False)
        self.root = self.project / ".repoprover" / "jobs"
        self.database = self.root / "control.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open one transactional connection and always close its descriptor."""

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.database, timeout=30.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            with connection:
                yield connection
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self) -> None:
        # SQLite serializes ordinary writers, but WAL-mode negotiation itself
        # can fail before busy_timeout applies when two fresh clients open the
        # project simultaneously. A dedicated short-lived filesystem lease
        # makes first-open schema negotiation race-free across processes.
        init_lock = self.root / "control.init.lock"
        with init_lock.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                self._initialize_locked()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _initialize_locked(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    plan_id TEXT,
                    settings_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('STARTING', 'RUNNING', 'CANCEL_REQUESTED',
                                  'SUCCEEDED', 'FAILED', 'INTERRUPTED')
                    ),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    heartbeat_at TEXT,
                    pid INTEGER,
                    error TEXT,
                    worker_log_path TEXT,
                    launch_command_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_verification_job
                ON jobs((1))
                WHERE state IN ('STARTING', 'RUNNING', 'CANCEL_REQUESTED');
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    created_at TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    message TEXT NOT NULL,
                    completed INTEGER,
                    total INTEGER,
                    claim_id TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_job_sequence
                ON events(job_id, sequence);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "worker_log_path" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN worker_log_path TEXT")
            if "launch_command_json" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN launch_command_json TEXT "
                    "NOT NULL DEFAULT '[]'"
                )

    @staticmethod
    def _settings_json(settings: VerificationSettings) -> str:
        return json.dumps(asdict(settings), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> VerificationJob:
        settings_payload = json.loads(str(row["settings_json"]))
        settings = VerificationSettings(**settings_payload)
        state = VerificationJobState(str(row["state"]))
        return VerificationJob(
            job_id=str(row["job_id"]),
            project_path=Path(str(row["project_path"])),
            state=state,
            request_fingerprint=str(row["request_fingerprint"]),
            plan_id=row["plan_id"],
            settings=settings,
            created_at=str(row["created_at"]),
            started_at=row["started_at"],
            updated_at=str(row["updated_at"]),
            completed_at=row["completed_at"],
            heartbeat_at=row["heartbeat_at"],
            pid=int(row["pid"]) if row["pid"] is not None else None,
            error=row["error"],
            cancellable=not state.terminal,
            attached_legacy=False,
            worker_log_path=(
                Path(str(row["worker_log_path"]))
                if row["worker_log_path"] is not None
                else None
            ),
            launch_command=tuple(json.loads(str(row["launch_command_json"]))),
        )

    def create(
        self,
        *,
        request_fingerprint: str,
        plan_id: str | None,
        settings: VerificationSettings,
    ) -> VerificationJob:
        now = utc_now()
        job_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, project_path, request_fingerprint, plan_id,
                    settings_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(self.project),
                    request_fingerprint,
                    plan_id,
                    self._settings_json(settings),
                    str(VerificationJobState.STARTING),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_job(row)

    def job(self, job_id: str) -> VerificationJob | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def active(self) -> VerificationJob | None:
        placeholders = ",".join("?" for _ in _ACTIVE_STATES)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) "
                "ORDER BY created_at DESC LIMIT 1",
                _ACTIVE_STATES,
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def latest(self) -> VerificationJob | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def mark_running(self, job_id: str, *, pid: int) -> VerificationJob:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state = CASE WHEN state = 'CANCEL_REQUESTED'
                                 THEN state ELSE 'RUNNING' END,
                    started_at = COALESCE(started_at, ?), updated_at = ?,
                    heartbeat_at = ?, pid = ?
                WHERE job_id = ? AND state IN ('STARTING', 'CANCEL_REQUESTED')
                """,
                (now, now, now, pid, job_id),
            )
        job = self.job(job_id)
        if job is None:
            raise ValueError(f"Unknown verification job: {job_id}")
        return job

    def record_spawn(
        self,
        job_id: str,
        *,
        pid: int,
        command: tuple[str, ...],
        worker_log_path: Path,
    ) -> VerificationJob:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET pid = ?, launch_command_json = ?, worker_log_path = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    pid,
                    json.dumps(command, separators=(",", ":")),
                    str(worker_log_path),
                    now,
                    job_id,
                ),
            )
        job = self.job(job_id)
        if job is None:
            raise ValueError(f"Unknown verification job: {job_id}")
        return job

    def heartbeat(self, job_id: str) -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE jobs SET heartbeat_at = ?, updated_at = ?
                WHERE job_id = ?
                  AND state IN ('STARTING', 'RUNNING', 'CANCEL_REQUESTED')
                """,
                (now, now, job_id),
            )

    def finish(
        self, job_id: str, state: VerificationJobState, *, error: str | None = None
    ) -> VerificationJob:
        if not state.terminal:
            raise ValueError(f"Job finish state must be terminal: {state}")
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, updated_at = ?, completed_at = ?, heartbeat_at = ?,
                    error = ?
                WHERE job_id = ?
                  AND state IN ('STARTING', 'RUNNING', 'CANCEL_REQUESTED')
                """,
                (str(state), now, now, now, error, job_id),
            )
        job = self.job(job_id)
        if job is None:
            raise ValueError(f"Unknown verification job: {job_id}")
        return job

    def append_event(self, job_id: str, event: ProgressEvent) -> ProgressEvent:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events(
                    job_id, created_at, phase, message, completed, total,
                    claim_id, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    utc_now(),
                    str(event.phase),
                    event.message,
                    event.completed,
                    event.total,
                    event.claim_id,
                    json.dumps(dict(event.details), sort_keys=True, default=str),
                ),
            )
            sequence = cursor.lastrowid
            if sequence is None:
                raise RuntimeError("SQLite event insert did not produce a sequence")
        self.heartbeat(job_id)
        return ProgressEvent(
            sequence,
            event.phase,
            event.message,
            event.completed,
            event.total,
            event.claim_id,
            event.details,
        )

    def events(self, job_id: str, after_sequence: int) -> tuple[ProgressEvent, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE job_id = ? AND sequence > ? ORDER BY sequence
                """,
                (job_id, after_sequence),
            ).fetchall()
        return tuple(
            ProgressEvent(
                sequence=int(row["sequence"]),
                phase=ProgressPhase(str(row["phase"])),
                message=str(row["message"]),
                completed=(
                    int(row["completed"]) if row["completed"] is not None else None
                ),
                total=int(row["total"]) if row["total"] is not None else None,
                claim_id=row["claim_id"],
                details=json.loads(str(row["details_json"])),
            )
            for row in rows
        )

    def observe(
        self,
        job: VerificationJob,
        *,
        after_sequence: int = 0,
        started: bool = False,
        attached: bool = True,
    ) -> VerificationJobObservation:
        if after_sequence < 0:
            raise ValueError("after_sequence must be nonnegative")
        events = self.events(job.job_id, after_sequence)
        next_sequence = events[-1].sequence if events else after_sequence
        return VerificationJobObservation(
            job=job,
            events=events,
            after_sequence=after_sequence,
            next_sequence=next_sequence,
            started=started,
            attached=attached,
        )

    def request_cancel(self, job_id: str) -> VerificationJob:
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown verification job: {job_id}")
            state = VerificationJobState(str(row["state"]))
            if not state.terminal:
                connection.execute(
                    """
                    UPDATE jobs SET state = 'CANCEL_REQUESTED', updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now, job_id),
                )
                atomic_write_text(self.cancel_path(job_id), now + "\n")
        job = self.job(job_id)
        assert job is not None
        return job

    def cancel_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.cancel"

    def cancellation_requested(self, job_id: str) -> bool:
        return self.cancel_path(job_id).is_file()

    def worker_log(self, job_id: str) -> Path:
        return self.root / f"{job_id}.worker.log"


def request_fingerprint(
    project: Path,
    plan_id: str | None,
    settings: VerificationSettings,
    *,
    codex: str,
    cache_home: str | None,
) -> str:
    from ..incremental.io import canonical_hash

    payload: dict[str, Any] = {
        "project": str(project.expanduser().resolve(strict=False)),
        "plan_id": plan_id,
        # Machine resource policy is deliberately excluded.  A replacement TUI
        # must attach to the same proof request even if a safe live limit was
        # changed after launch.  The complete configured/effective concurrency
        # snapshot is persisted as run provenance instead.
        "proof_request": {
            "model": settings.model,
            "effort": settings.effort,
        },
        "codex": codex,
        "cache_home": cache_home,
    }
    return canonical_hash(payload)
