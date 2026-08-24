"""Coordinator-owned AI leases for distributed workers."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .admission import (
    AdmissionLease,
    AdmissionRequest,
    ResourceKind,
    SQLiteAdmissionStore,
)
from .ai import AIAdmissionController, AITaskClass
from .build import BuildAdmissionController
from .lean import LeanAdmissionController


@dataclass(frozen=True)
class DistributedAIRequest:
    worker_id: str
    task_id: str
    task_class: AITaskClass = AITaskClass.PROOF
    ttl_seconds: float = 120.0
    priority: int | None = None

    @property
    def owner(self) -> str:
        return f"{self.worker_id}:{self.task_id}"


class CoordinatorOwnedAILeases:
    """The only AI-token authority shared by all distributed workers."""

    def __init__(self, controller: AIAdmissionController) -> None:
        self.controller = controller

    def request(self, request: DistributedAIRequest) -> AdmissionLease | None:
        if not request.worker_id or not request.task_id:
            raise ValueError("distributed AI request needs worker and task identifiers")
        if request.priority is None:
            admission = self.controller.request(
                request.owner,
                request.task_class,
                ttl_seconds=request.ttl_seconds,
                request_id=request.owner,
            )
        else:
            admission = AdmissionRequest(
                ResourceKind.AI,
                request.priority,
                request.owner,
                request.ttl_seconds,
                request.owner,
            )
        return self.controller.try_acquire(admission)

    @staticmethod
    def _assert_worker(worker_id: str, lease: AdmissionLease) -> None:
        if not lease.owner.startswith(f"{worker_id}:"):
            raise PermissionError("AI lease belongs to a different worker")

    def heartbeat(
        self,
        worker_id: str,
        lease: AdmissionLease,
        *,
        ttl_seconds: float | None = None,
    ) -> AdmissionLease | None:
        self._assert_worker(worker_id, lease)
        return self.controller.heartbeat(lease, ttl_seconds=ttl_seconds)

    def release(self, worker_id: str, lease: AdmissionLease) -> bool:
        self._assert_worker(worker_id, lease)
        return self.controller.release(lease)

    @property
    def active(self) -> int:
        return self.controller.snapshot().active


# Descriptive alias for protocol adapters.
DistributedAILeaseCoordinator = CoordinatorOwnedAILeases


@dataclass(frozen=True)
class NodeResourceControllers:
    """Lean/build controllers whose databases and limits belong to one node."""

    node_id: str
    lean: LeanAdmissionController
    build: BuildAdmissionController

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        node_id: str,
        lean_limit: int,
        build_limit: int,
        clock: Callable[[], float] | None = None,
    ) -> NodeResourceControllers:
        if not node_id:
            raise ValueError("node identifier must not be empty")
        digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:16]
        path = (
            Path(root).expanduser().resolve() / "nodes" / digest / "admission.sqlite3"
        )
        store_kwargs = {} if clock is None else {"clock": clock}
        store = SQLiteAdmissionStore(path, **store_kwargs)
        lean = LeanAdmissionController(
            store,
            initial=lean_limit,
            maximum=max(lean_limit, 1),
            mode="fixed",
        )
        build = BuildAdmissionController(
            store,
            initial=build_limit,
            maximum=max(build_limit, 1),
            hard_maximum=max(8, build_limit),
            mode="fixed",
        )
        return cls(node_id=node_id, lean=lean, build=build)
