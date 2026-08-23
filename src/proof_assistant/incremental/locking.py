from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ProjectLockedError(RuntimeError):
    """Raised when a persistent verification project is already mutating."""


def worker_lock_path(project: Path) -> Path:
    return project / ".repoprover" / "jobs" / "worker.lock"


def acquire_worker_lease(project: Path) -> int:
    """Acquire the detached-worker lifetime lease and return its inheritable fd."""

    path = worker_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise ProjectLockedError(
            "An active backend verification worker holds this project's "
            f"mutation lease: {project}"
        ) from exc
    os.set_inheritable(descriptor, True)
    return descriptor


def release_worker_lease(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def worker_lease_active(project: Path) -> bool:
    try:
        descriptor = acquire_worker_lease(project)
    except ProjectLockedError:
        return True
    release_worker_lease(descriptor)
    return False


def project_session_active(project: Path) -> bool:
    try:
        with project_lock(project, exclusive=True):
            return False
    except ProjectLockedError:
        return True


@contextmanager
def project_lock(
    project: Path, *, exclusive: bool, wait: bool = False
) -> Iterator[None]:
    lock_path = project / ".repoprover" / "session.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not wait:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(stream.fileno(), operation)
        except BlockingIOError as exc:
            raise ProjectLockedError(
                f"Verification project is already in use: {project}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
