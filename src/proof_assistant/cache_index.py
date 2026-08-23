from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

INDEX_SCHEMA_VERSION = 2


class CacheIndexError(RuntimeError):
    """Raised when persistent cache accounting cannot be trusted."""


@dataclass(frozen=True)
class IndexedCacheEntry:
    path: Path
    kind: str
    allocated_bytes: int
    last_used: float
    signature: str
    lease_name: str
    state: str


@dataclass(frozen=True)
class IndexedReservation:
    identifier: str
    reserved_bytes: int
    lock_name: str
    created_at: float


class CacheIndex:
    """Transactional accounting for package-managed cache entries.

    The index deliberately stores one row per eviction unit, never one row per
    file inside a bulk cache. Recursive filesystem measurement is performed by
    the cache manager only when publishing or reconciling an entry.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _open_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            # Package operations are short and coordinated by cache leases. A
            # longer SQLite busy wait would undermine the user-visible GC
            # deadline if an unrelated or crashed client retained a DB lock.
            connection = sqlite3.connect(self.path, timeout=1.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._initialize(connection)
            return connection
        except CacheIndexError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise CacheIndexError(
                f"Could not open cache index {self.path}: {exc}"
            ) from exc
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a transaction and close its SQLite descriptor on every path."""

        connection = self._open_connection()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                path TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                allocated_bytes INTEGER NOT NULL CHECK (allocated_bytes >= 0),
                last_used REAL NOT NULL,
                signature TEXT NOT NULL,
                lease_name TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('ready', 'dirty', 'deleting')
                )
            );
            CREATE INDEX IF NOT EXISTS entries_lru
                ON entries(state, last_used, kind);
            CREATE TABLE IF NOT EXISTS reservations (
                identifier TEXT PRIMARY KEY,
                reserved_bytes INTEGER NOT NULL CHECK (reserved_bytes >= 0),
                lock_name TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL
            );
            """
        )
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(INDEX_SCHEMA_VERSION),),
            )
        elif row["value"] == "1":
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP INDEX IF EXISTS entries_lru;
                ALTER TABLE entries RENAME TO entries_v1;
                CREATE TABLE entries (
                    path TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    allocated_bytes INTEGER NOT NULL
                        CHECK (allocated_bytes >= 0),
                    last_used REAL NOT NULL,
                    signature TEXT NOT NULL,
                    lease_name TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('ready', 'dirty', 'deleting')
                    )
                );
                INSERT INTO entries(
                    path, kind, allocated_bytes, last_used, signature,
                    lease_name, state
                )
                SELECT path, kind, allocated_bytes, last_used, signature,
                       lease_name, state
                FROM entries_v1;
                DROP TABLE entries_v1;
                CREATE INDEX entries_lru
                    ON entries(state, last_used, kind);
                UPDATE metadata SET value = '2'
                    WHERE key = 'schema_version';
                COMMIT;
                """
            )
        elif row["value"] != str(INDEX_SCHEMA_VERSION):
            raise CacheIndexError(f"Unsupported cache index schema {row['value']!r}")
        connection.commit()

    def entries(self) -> tuple[IndexedCacheEntry, ...]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT path, kind, allocated_bytes, last_used, signature,
                           lease_name, state
                    FROM entries
                    ORDER BY last_used, kind, path
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise CacheIndexError(f"Could not read cache entries: {exc}") from exc
        return tuple(
            IndexedCacheEntry(
                path=Path(row["path"]),
                kind=row["kind"],
                allocated_bytes=int(row["allocated_bytes"]),
                last_used=float(row["last_used"]),
                signature=row["signature"],
                lease_name=row["lease_name"],
                state=row["state"],
            )
            for row in rows
        )

    def upsert_entry(self, entry: IndexedCacheEntry) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO entries(
                        path, kind, allocated_bytes, last_used, signature,
                        lease_name, state
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        kind = excluded.kind,
                        allocated_bytes = excluded.allocated_bytes,
                        last_used = excluded.last_used,
                        signature = excluded.signature,
                        lease_name = excluded.lease_name,
                        state = excluded.state
                    """,
                    (
                        str(entry.path),
                        entry.kind,
                        entry.allocated_bytes,
                        entry.last_used,
                        entry.signature,
                        entry.lease_name,
                        entry.state,
                    ),
                )
        except sqlite3.Error as exc:
            raise CacheIndexError(f"Could not update cache entry: {exc}") from exc

    def remove_entry(self, path: Path) -> None:
        try:
            with self._connection() as connection:
                connection.execute("DELETE FROM entries WHERE path = ?", (str(path),))
        except sqlite3.Error as exc:
            raise CacheIndexError(f"Could not remove cache entry: {exc}") from exc

    def remove_entries_not_in(self, paths: set[Path]) -> None:
        wanted = {str(path) for path in paths}
        try:
            with self._connection() as connection:
                rows = connection.execute("SELECT path FROM entries").fetchall()
                stale = [(row["path"],) for row in rows if row["path"] not in wanted]
                connection.executemany("DELETE FROM entries WHERE path = ?", stale)
        except sqlite3.Error as exc:
            raise CacheIndexError(f"Could not reconcile cache entries: {exc}") from exc

    def mark_deleting(self, path: Path) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    "UPDATE entries SET state = 'deleting' WHERE path = ?",
                    (str(path),),
                )
        except sqlite3.Error as exc:
            raise CacheIndexError(
                f"Could not mark cache entry deleting: {exc}"
            ) from exc

    def mark_dirty(
        self,
        path: Path,
        *,
        kind: str,
        signature: str,
        lease_name: str,
    ) -> None:
        """Record that an entry may grow while retaining its known byte count."""
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO entries(
                        path, kind, allocated_bytes, last_used, signature,
                        lease_name, state
                    ) VALUES(?, ?, 0, ?, ?, ?, 'dirty')
                    ON CONFLICT(path) DO UPDATE SET
                        kind = excluded.kind,
                        last_used = excluded.last_used,
                        signature = excluded.signature,
                        lease_name = excluded.lease_name,
                        state = 'dirty'
                    """,
                    (str(path), kind, time.time(), signature, lease_name),
                )
        except sqlite3.Error as exc:
            raise CacheIndexError(f"Could not dirty cache entry: {exc}") from exc

    def touch_entry(self, path: Path) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    "UPDATE entries SET last_used = ? WHERE path = ?",
                    (time.time(), str(path)),
                )
        except sqlite3.Error as exc:
            raise CacheIndexError(f"Could not touch cache entry: {exc}") from exc

    def reservations(self) -> tuple[IndexedReservation, ...]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT identifier, reserved_bytes, lock_name, created_at
                    FROM reservations ORDER BY created_at
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise CacheIndexError(f"Could not read cache reservations: {exc}") from exc
        return tuple(
            IndexedReservation(
                identifier=row["identifier"],
                reserved_bytes=int(row["reserved_bytes"]),
                lock_name=row["lock_name"],
                created_at=float(row["created_at"]),
            )
            for row in rows
        )

    def add_reservation(
        self,
        identifier: str,
        reserved_bytes: int,
        lock_name: str,
    ) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO reservations(
                        identifier, reserved_bytes, lock_name, created_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (identifier, reserved_bytes, lock_name, time.time()),
                )
        except sqlite3.Error as exc:
            raise CacheIndexError(f"Could not create cache reservation: {exc}") from exc

    def remove_reservation(self, identifier: str) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    "DELETE FROM reservations WHERE identifier = ?", (identifier,)
                )
        except sqlite3.Error as exc:
            raise CacheIndexError(f"Could not remove cache reservation: {exc}") from exc
