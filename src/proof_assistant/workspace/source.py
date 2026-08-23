from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..incremental.snapshot import (
    SourceInventory,
    SourceInventoryEntry,
    stage_stable_source,
)


@dataclass(frozen=True)
class InventoryDelta:
    path: str
    kind: str
    old_path: str | None
    old_sha256: str | None
    new_sha256: str | None


def compare_inventories(
    previous: dict[str, SourceInventoryEntry], candidate: SourceInventory
) -> tuple[InventoryDelta, ...]:
    current = candidate.by_path()
    deleted = set(previous) - set(current)
    added = set(current) - set(previous)
    deltas: list[InventoryDelta] = []

    # Git-style conservative rename recognition: only unique equal-content pairs.
    deleted_by_hash: dict[str, list[str]] = {}
    added_by_hash: dict[str, list[str]] = {}
    for path in deleted:
        deleted_by_hash.setdefault(previous[path].sha256, []).append(path)
    for path in added:
        added_by_hash.setdefault(current[path].sha256, []).append(path)
    for digest in sorted(set(deleted_by_hash) & set(added_by_hash)):
        old_paths = sorted(deleted_by_hash[digest])
        new_paths = sorted(added_by_hash[digest])
        if len(old_paths) == len(new_paths) == 1:
            old_path = old_paths[0]
            new_path = new_paths[0]
            deleted.remove(old_path)
            added.remove(new_path)
            deltas.append(InventoryDelta(new_path, "RENAMED", old_path, digest, digest))

    for path in sorted(set(previous) & set(current)):
        if (
            previous[path].sha256 != current[path].sha256
            or previous[path].size != current[path].size
        ):
            deltas.append(
                InventoryDelta(
                    path,
                    "MODIFIED",
                    None,
                    previous[path].sha256,
                    current[path].sha256,
                )
            )
    for path in sorted(deleted):
        deltas.append(
            InventoryDelta(path, "DELETED", None, previous[path].sha256, None)
        )
    for path in sorted(added):
        deltas.append(InventoryDelta(path, "ADDED", None, None, current[path].sha256))
    return tuple(sorted(deltas, key=lambda item: (item.path, item.kind)))


@contextmanager
def stable_source_copy(
    source: Path, *, expected_inventory_sha256: str | None = None
) -> Iterator[tuple[Path, SourceInventory]]:
    """Yield a verified staged source tree and always remove it afterward."""
    parent = Path(tempfile.mkdtemp(prefix="proof-assistant-source-"))
    staged: Path | None = None
    try:
        staged, inventory = stage_stable_source(
            source,
            parent,
            expected_inventory_sha256=expected_inventory_sha256,
        )
        yield staged, inventory
    finally:
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)
        shutil.rmtree(parent, ignore_errors=True)
