from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ProjectLockedError(RuntimeError):
    """Raised when a persistent verification project is already mutating."""


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
