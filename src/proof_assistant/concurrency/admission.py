"""Cross-process admission leases for machine-scoped concurrency limits.

The database is deliberately small and independent of project state.  A lease
is an admission token, not ownership of a task: lowering a limit never revokes
leases which are already running.  Expired leases and abandoned queue entries
are reclaimed in the same transaction that admits new work.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class ResourceKind(StrEnum):
    AI = "ai"
    LEAN = "lean"
    BUILD = "build"


Clock = Callable[[], float]
LimitProvider = Callable[[], int]


def _clock_value(clock: Clock | Any) -> float:
    if callable(clock):
        return float(clock())
    return float(clock.time())


def _resource_value(resource: ResourceKind | str) -> str:
    return str(resource.value if isinstance(resource, ResourceKind) else resource)


@dataclass(frozen=True)
class AdmissionRequest:
    """One request for a resource slot.

    Smaller priority values run first.  ``owner`` must identify a logical unit
    of work and is also the idempotency key unless ``request_id`` is supplied.
    """

    resource: ResourceKind
    priority: int
    owner: str
    ttl_seconds: float
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not self.owner.strip():
            raise ValueError("admission owner must not be empty")
        if self.ttl_seconds <= 0:
            raise ValueError("admission TTL must be positive")

    @property
    def key(self) -> str:
        return self.request_id or self.owner


@dataclass(frozen=True)
class AdmissionLease:
    lease_id: str
    resource: ResourceKind
    owner: str
    request_id: str
    priority: int
    acquired_at: float
    heartbeat_at: float
    expires_at: float
    ttl_seconds: float


@dataclass(frozen=True)
class AdmissionSnapshot:
    resource: ResourceKind
    limit: int
    active: int
    queued: int
    oldest_wait_seconds: float


class SQLiteAdmissionStore:
    """SQLite-backed resource limits, wait queues, and expiring leases."""

    def __init__(self, path: str | Path, *, clock: Clock | Any = time.time) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._initialize()

    def now(self) -> float:
        return _clock_value(self.clock)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield one database connection and unconditionally close it.

        ``sqlite3.Connection``'s own context manager only controls the
        transaction; it deliberately does not close the connection.  Routing
        every operation through this wrapper makes descriptor cleanup an
        invariant, including on exceptions and early returns.
        """

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        # SQLite's first WAL/schema setup can race before busy_timeout is useful.
        # Serialize only initialization; normal admissions remain transactional.
        lock_path = self.path.with_name(f"{self.path.name}.init.lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with self._connection() as connection:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.executescript(
                        """
                CREATE TABLE IF NOT EXISTS resource_limits (
                    resource TEXT PRIMARY KEY,
                    limit_value INTEGER NOT NULL CHECK(limit_value >= 0),
                    generation INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS waiters (
                    ticket INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    requested_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    ttl_seconds REAL NOT NULL,
                    UNIQUE(resource, request_id)
                );
                CREATE INDEX IF NOT EXISTS waiters_order
                    ON waiters(resource, priority, ticket);

                CREATE TABLE IF NOT EXISTS leases (
                    lease_id TEXT PRIMARY KEY,
                    resource TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    acquired_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    ttl_seconds REAL NOT NULL,
                    UNIQUE(resource, request_id)
                );
                CREATE INDEX IF NOT EXISTS leases_expiry
                    ON leases(resource, expires_at);

                CREATE TABLE IF NOT EXISTS controller_state (
                    resource TEXT NOT NULL,
                    state_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(resource, state_key)
                );
                        """
                    )
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _lease(row: sqlite3.Row) -> AdmissionLease:
        return AdmissionLease(
            lease_id=str(row["lease_id"]),
            resource=ResourceKind(str(row["resource"])),
            owner=str(row["owner"]),
            request_id=str(row["request_id"]),
            priority=int(row["priority"]),
            acquired_at=float(row["acquired_at"]),
            heartbeat_at=float(row["heartbeat_at"]),
            expires_at=float(row["expires_at"]),
            ttl_seconds=float(row["ttl_seconds"]),
        )

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _prune(connection: sqlite3.Connection, now: float) -> None:
        connection.execute("DELETE FROM leases WHERE expires_at <= ?", (now,))
        connection.execute("DELETE FROM waiters WHERE expires_at <= ?", (now,))

    def ensure_limit(self, resource: ResourceKind, limit: int) -> int:
        if limit < 0:
            raise ValueError("admission limit must not be negative")
        now = self.now()
        name = _resource_value(resource)
        with self._connection() as connection:
            self._begin(connection)
            connection.execute(
                """
                INSERT INTO resource_limits(resource, limit_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(resource) DO NOTHING
                """,
                (name, limit, now),
            )
            row = connection.execute(
                "SELECT limit_value FROM resource_limits WHERE resource = ?", (name,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return int(row["limit_value"])

    def set_limit(self, resource: ResourceKind, limit: int) -> int:
        """Publish a new admission limit without disturbing active leases."""

        if limit < 0:
            raise ValueError("admission limit must not be negative")
        now = self.now()
        name = _resource_value(resource)
        with self._connection() as connection:
            self._begin(connection)
            connection.execute(
                """
                INSERT INTO resource_limits(resource, limit_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(resource) DO UPDATE SET
                    limit_value = excluded.limit_value,
                    generation = resource_limits.generation + 1,
                    updated_at = excluded.updated_at
                """,
                (name, limit, now),
            )
            connection.commit()
        return limit

    def limit(self, resource: ResourceKind) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT limit_value FROM resource_limits WHERE resource = ?",
                (_resource_value(resource),),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"no admission limit configured for {resource}")
        return int(row["limit_value"])

    def try_acquire(self, request: AdmissionRequest) -> AdmissionLease | None:
        now = self.now()
        resource = _resource_value(request.resource)
        waiter_expiry = now + max(60.0, request.ttl_seconds * 4.0)
        with self._connection() as connection:
            self._begin(connection)
            self._prune(connection, now)

            existing = connection.execute(
                """
                SELECT * FROM leases
                WHERE resource = ? AND request_id = ?
                """,
                (resource, request.key),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._lease(existing)

            connection.execute(
                """
                INSERT INTO waiters(
                    resource, request_id, owner, priority, requested_at,
                    expires_at, ttl_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource, request_id) DO UPDATE SET
                    owner = excluded.owner,
                    priority = excluded.priority,
                    expires_at = excluded.expires_at,
                    ttl_seconds = excluded.ttl_seconds
                """,
                (
                    resource,
                    request.key,
                    request.owner,
                    request.priority,
                    now,
                    waiter_expiry,
                    request.ttl_seconds,
                ),
            )
            waiter = connection.execute(
                """
                SELECT ticket, priority FROM waiters
                WHERE resource = ? AND request_id = ?
                """,
                (resource, request.key),
            ).fetchone()
            limit_row = connection.execute(
                "SELECT limit_value FROM resource_limits WHERE resource = ?",
                (resource,),
            ).fetchone()
            if waiter is None or limit_row is None:
                connection.rollback()
                raise RuntimeError(f"admission resource {resource!r} is not configured")

            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM leases WHERE resource = ?", (resource,)
                ).fetchone()[0]
            )
            available = max(0, int(limit_row["limit_value"]) - active)
            earlier = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM waiters
                    WHERE resource = ? AND (
                        priority < ? OR (priority = ? AND ticket < ?)
                    )
                    """,
                    (
                        resource,
                        int(waiter["priority"]),
                        int(waiter["priority"]),
                        int(waiter["ticket"]),
                    ),
                ).fetchone()[0]
            )
            if available == 0 or earlier >= available:
                connection.commit()
                return None

            lease_id = uuid.uuid4().hex
            expires_at = now + request.ttl_seconds
            connection.execute(
                """
                INSERT INTO leases(
                    lease_id, resource, request_id, owner, priority,
                    acquired_at, heartbeat_at, expires_at, ttl_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    resource,
                    request.key,
                    request.owner,
                    request.priority,
                    now,
                    now,
                    expires_at,
                    request.ttl_seconds,
                ),
            )
            connection.execute(
                "DELETE FROM waiters WHERE resource = ? AND request_id = ?",
                (resource, request.key),
            )
            row = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._lease(row)

    def heartbeat(
        self, lease: AdmissionLease | str, *, ttl_seconds: float | None = None
    ) -> AdmissionLease | None:
        lease_id = lease.lease_id if isinstance(lease, AdmissionLease) else lease
        now = self.now()
        with self._connection() as connection:
            self._begin(connection)
            self._prune(connection, now)
            row = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            ttl = float(ttl_seconds or row["ttl_seconds"])
            if ttl <= 0:
                connection.rollback()
                raise ValueError("admission TTL must be positive")
            connection.execute(
                """
                UPDATE leases
                SET heartbeat_at = ?, expires_at = ?, ttl_seconds = ?
                WHERE lease_id = ?
                """,
                (now, now + ttl, ttl, lease_id),
            )
            updated = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            connection.commit()
        assert updated is not None
        return self._lease(updated)

    def release(self, lease: AdmissionLease | str) -> bool:
        lease_id = lease.lease_id if isinstance(lease, AdmissionLease) else lease
        with self._connection() as connection:
            self._begin(connection)
            cursor = connection.execute(
                "DELETE FROM leases WHERE lease_id = ?", (lease_id,)
            )
            connection.commit()
        return cursor.rowcount > 0

    def cancel(self, request: AdmissionRequest) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM waiters WHERE resource = ? AND request_id = ?",
                (_resource_value(request.resource), request.key),
            )
        return cursor.rowcount > 0

    def snapshot(self, resource: ResourceKind) -> AdmissionSnapshot:
        now = self.now()
        name = _resource_value(resource)
        with self._connection() as connection:
            self._begin(connection)
            self._prune(connection, now)
            limit_row = connection.execute(
                "SELECT limit_value FROM resource_limits WHERE resource = ?", (name,)
            ).fetchone()
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM leases WHERE resource = ?", (name,)
                ).fetchone()[0]
            )
            queued_row = connection.execute(
                """
                SELECT COUNT(*) AS count, MIN(requested_at) AS oldest
                FROM waiters WHERE resource = ?
                """,
                (name,),
            ).fetchone()
            connection.commit()
        if limit_row is None:
            raise RuntimeError(f"no admission limit configured for {resource}")
        queued = int(queued_row["count"])
        oldest = float(queued_row["oldest"]) if queued_row["oldest"] else now
        return AdmissionSnapshot(
            resource=resource,
            limit=int(limit_row["limit_value"]),
            active=active,
            queued=queued,
            oldest_wait_seconds=max(0.0, now - oldest) if queued else 0.0,
        )

    def set_state(
        self, resource: ResourceKind, key: str, payload: dict[str, Any]
    ) -> None:
        now = self.now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO controller_state(resource, state_key, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(resource, state_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    _resource_value(resource),
                    key,
                    json.dumps(payload, sort_keys=True),
                    now,
                ),
            )

    def get_state(self, resource: ResourceKind, key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM controller_state
                WHERE resource = ? AND state_key = ?
                """,
                (_resource_value(resource), key),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return payload if isinstance(payload, dict) else None

    def update_state(
        self,
        resource: ResourceKind,
        key: str,
        update: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically mutate shared controller state across local processes."""

        now = self.now()
        name = _resource_value(resource)
        with self._connection() as connection:
            self._begin(connection)
            row = connection.execute(
                """
                SELECT payload_json FROM controller_state
                WHERE resource = ? AND state_key = ?
                """,
                (name, key),
            ).fetchone()
            current: dict[str, Any] = {}
            if row is not None:
                decoded = json.loads(str(row["payload_json"]))
                if isinstance(decoded, dict):
                    current = decoded
            candidate = update(dict(current))
            if not isinstance(candidate, dict):
                connection.rollback()
                raise TypeError("controller state updater must return a mapping")
            connection.execute(
                """
                INSERT INTO controller_state(resource, state_key, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(resource, state_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (name, key, json.dumps(candidate, sort_keys=True), now),
            )
            connection.commit()
        return candidate

    def sync_limits_if_state_changed(
        self,
        *,
        state_resource: ResourceKind,
        state_key: str,
        payload: dict[str, Any],
        limits: dict[ResourceKind, int],
    ) -> bool:
        """Atomically publish a new resolved config exactly once.

        Batch workers created from the same frozen runtime specification must
        not repeatedly reset an adaptive limit.  A changed machine/env/CLI
        fingerprint updates all three limits and its marker in one transaction.
        """

        if any(limit < 0 for limit in limits.values()):
            raise ValueError("admission limits must not be negative")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        now = self.now()
        with self._connection() as connection:
            self._begin(connection)
            row = connection.execute(
                """
                SELECT payload_json FROM controller_state
                WHERE resource = ? AND state_key = ?
                """,
                (_resource_value(state_resource), state_key),
            ).fetchone()
            if row is not None and str(row["payload_json"]) == encoded:
                connection.commit()
                return False
            for resource, limit in limits.items():
                connection.execute(
                    """
                    INSERT INTO resource_limits(resource, limit_value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(resource) DO UPDATE SET
                        limit_value = excluded.limit_value,
                        generation = resource_limits.generation + 1,
                        updated_at = excluded.updated_at
                    """,
                    (_resource_value(resource), limit, now),
                )
            connection.execute(
                """
                INSERT INTO controller_state(
                    resource, state_key, payload_json, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(resource, state_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (_resource_value(state_resource), state_key, encoded, now),
            )
            connection.commit()
        return True


class AdmissionController:
    """Resource-specific facade over :class:`SQLiteAdmissionStore`."""

    def __init__(
        self,
        store: SQLiteAdmissionStore,
        resource: ResourceKind,
        limit_provider: LimitProvider | int,
        *,
        poll_interval: float = 0.1,
    ) -> None:
        self.store = store
        self.resource = resource
        self._limit_provider = (
            limit_provider if callable(limit_provider) else lambda: int(limit_provider)
        )
        self.poll_interval = poll_interval
        self.store.ensure_limit(resource, int(self._limit_provider()))

    @property
    def limit(self) -> int:
        return self.store.limit(self.resource)

    def set_limit(self, limit: int) -> int:
        return self.store.set_limit(self.resource, limit)

    def refresh_limit(self) -> int:
        return self.set_limit(int(self._limit_provider()))

    def try_acquire(self, request: AdmissionRequest) -> AdmissionLease | None:
        if request.resource != self.resource:
            raise ValueError(
                f"request is for {request.resource}, controller is {self.resource}"
            )
        return self.store.try_acquire(request)

    def acquire(
        self, request: AdmissionRequest, *, timeout: float | None = None
    ) -> AdmissionLease:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            lease = self.try_acquire(request)
            if lease is not None:
                return lease
            if deadline is not None and time.monotonic() >= deadline:
                self.store.cancel(request)
                raise TimeoutError(
                    f"timed out waiting for {self.resource.value} admission"
                )
            time.sleep(self.poll_interval)

    @contextmanager
    def lease(
        self,
        request: AdmissionRequest,
        *,
        timeout: float | None = None,
        heartbeat: bool = True,
    ):
        """Acquire, keep alive, and reliably release one admission lease.

        Long Codex turns and builds can exceed their initial TTL.  The daemon
        heartbeat is an implementation detail of the admission boundary and
        stops without killing in-flight work.  Deterministic fake-clock tests
        may pass ``heartbeat=False``.
        """

        acquired = self.acquire(request, timeout=timeout)
        stop = threading.Event()
        thread: threading.Thread | None = None
        if heartbeat:
            interval = max(0.005, min(30.0, request.ttl_seconds / 4.0))

            def maintain() -> None:
                while not stop.wait(interval):
                    if self.heartbeat(acquired) is None:
                        return

            thread = threading.Thread(
                target=maintain,
                name=f"{self.resource.value}-lease-heartbeat",
                daemon=True,
            )
            thread.start()
        try:
            yield acquired
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=max(0.1, min(1.0, request.ttl_seconds / 3.0)))
            self.release(acquired)

    def heartbeat(
        self, lease: AdmissionLease, *, ttl_seconds: float | None = None
    ) -> AdmissionLease | None:
        if lease.resource != self.resource:
            raise ValueError("lease belongs to a different resource controller")
        return self.store.heartbeat(lease, ttl_seconds=ttl_seconds)

    def release(self, lease: AdmissionLease) -> bool:
        if lease.resource != self.resource:
            raise ValueError("lease belongs to a different resource controller")
        return self.store.release(lease)

    def snapshot(self) -> AdmissionSnapshot:
        return self.store.snapshot(self.resource)
