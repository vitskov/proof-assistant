from __future__ import annotations

import sqlite3
from contextlib import closing
from types import ModuleType

import pytest

import proof_assistant.cache_index as cache_index_module
import proof_assistant.workflow.jobs as jobs_module
from proof_assistant.cache_index import (
    CacheIndex,
    CacheIndexError,
    IndexedCacheEntry,
)
from proof_assistant.workflow.contracts import (
    ProgressEvent,
    ProgressPhase,
    VerificationJobState,
    VerificationSettings,
)
from proof_assistant.workflow.jobs import VerificationJobStore


class TrackingConnection(sqlite3.Connection):
    close_calls: int

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


def track_connections(monkeypatch, sqlite_module: ModuleType):
    original_connect = sqlite_module.connect
    opened: list[TrackingConnection] = []

    def connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite_module, "connect", connect)
    return opened, original_connect


def assert_every_connection_closed(opened: list[TrackingConnection]) -> None:
    assert opened
    assert all(connection.close_calls == 1 for connection in opened)
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_repeated_job_polling_closes_init_read_write_and_exception_connections(
    tmp_path, monkeypatch
):
    opened, _original = track_connections(monkeypatch, jobs_module.sqlite3)
    project = tmp_path / "project"
    store = VerificationJobStore(project)
    job = store.create(
        request_fingerprint="fingerprint",
        plan_id=None,
        settings=VerificationSettings(model="test"),
    )
    event = store.append_event(
        job.job_id,
        ProgressEvent(0, ProgressPhase.INDEXING, "Indexed manuscript", 1, 1),
    )

    for _ in range(40):
        assert store.active() is not None
        assert store.latest() is not None
        assert store.job(job.job_id) is not None
        assert store.events(job.job_id, 0) == (event,)

    finished = store.finish(job.job_id, VerificationJobState.SUCCEEDED)
    assert finished.state == VerificationJobState.SUCCEEDED
    # Terminal cancellation takes the early-return branch inside its transaction.
    assert store.request_cancel(job.job_id).state == VerificationJobState.SUCCEEDED
    # An exception raised while a transaction is active must also close the handle.
    with pytest.raises(ValueError, match="Unknown verification job"):
        store.request_cancel("missing-job")

    assert_every_connection_closed(opened)


def test_repeated_cache_index_operations_close_success_and_error_connections(
    tmp_path, monkeypatch
):
    opened, original_connect = track_connections(
        monkeypatch, cache_index_module.sqlite3
    )
    database = tmp_path / "cache" / "index.sqlite3"
    index = CacheIndex(database)
    entry = IndexedCacheEntry(
        path=tmp_path / "entry",
        kind="build",
        allocated_bytes=128,
        last_used=10.0,
        signature="signature",
        lease_name="entry-lease",
        state="ready",
    )
    index.upsert_entry(entry)
    index.add_reservation("reservation", 256, "reservation-lock")

    for _ in range(40):
        entries = index.entries()
        assert len(entries) == 1
        assert entries[0].path == entry.path
        assert len(index.reservations()) == 1
        index.touch_entry(entry.path)

    # A body error rolls back and closes before it is translated for callers.
    with pytest.raises(CacheIndexError, match="create cache reservation"):
        index.add_reservation("other", 64, "reservation-lock")
    index.remove_reservation("reservation")
    index.remove_entry(entry.path)

    # Opening/initialization errors close the connection before propagating too.
    with closing(original_connect(database)) as connection:
        connection.execute(
            "UPDATE metadata SET value = '999' WHERE key = 'schema_version'"
        )
        connection.commit()
    with pytest.raises(CacheIndexError, match="Unsupported cache index schema"):
        index.entries()

    assert_every_connection_closed(opened)


def test_cache_index_closes_connection_on_unexpected_initialization_exception(
    tmp_path, monkeypatch
):
    opened, _original = track_connections(monkeypatch, cache_index_module.sqlite3)

    def fail_initialization(_connection) -> None:
        raise RuntimeError("unexpected initialization failure")

    monkeypatch.setattr(CacheIndex, "_initialize", staticmethod(fail_initialization))
    with pytest.raises(RuntimeError, match="unexpected initialization failure"):
        CacheIndex(tmp_path / "index.sqlite3").entries()
    assert_every_connection_closed(opened)
