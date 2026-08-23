"""Dependency-aware scheduling and duplicate-agent escalation policy."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from .ai import TASK_PRIORITIES, AITaskClass


class TaskPriority(IntEnum):
    CLARIFICATION_DIAGNOSIS = 0
    PREREQUISITE_PROOF = 10
    CERTIFICATION_REVIEW = 20
    DUPLICATE_PROOF = 30
    BACKGROUND_REPORTING = 40


@dataclass(frozen=True)
class ScheduledTask:
    claim_id: str
    task_class: AITaskClass = AITaskClass.PROOF
    ready_order: int = 0


def _policy_name(policy: Any) -> str:
    return str(getattr(policy, "value", policy)).casefold().replace("-", "_")


class DuplicateEscalationPolicy:
    """Treat agents-per-target as a ceiling, never an eager fan-out count."""

    def __init__(
        self,
        *,
        budget_policy: Any = "balanced",
        maximum_agents: int = 4,
        enabled: bool = True,
    ) -> None:
        if maximum_agents < 1:
            raise ValueError("maximum agents per target must be positive")
        self.budget_policy = budget_policy
        self.maximum_agents = maximum_agents
        self.enabled = enabled

    def agents_for_target(
        self,
        *,
        failed_attempts: int,
        repair_attempted: bool,
        independent_attempt_useful: bool,
    ) -> int:
        if failed_attempts < 0:
            raise ValueError("failed attempts must not be negative")
        if (
            not self.enabled
            or failed_attempts == 0
            or not repair_attempted
            or not independent_attempt_useful
        ):
            return 1

        policy = _policy_name(self.budget_policy)
        if policy == "economy":
            desired = 2 if failed_attempts >= 3 else 1
        elif policy == "throughput":
            desired = (
                1
                + (failed_attempts >= 1)
                + (failed_attempts >= 3)
                + (failed_attempts >= 5)
            )
        else:  # balanced and unknown policies are deliberately conservative
            desired = (
                1
                + (failed_attempts >= 2)
                + (failed_attempts >= 4)
                + (failed_attempts >= 6)
            )
        return min(self.maximum_agents, desired)


class DependencyScheduler:
    """Ranks ready claims by the number of requested descendants they unblock.

    ``dependencies`` maps each claim to its direct prerequisites.  Traversal is
    cycle-safe so malformed or mutually-dependent manuscripts cannot hang the
    scheduler.
    """

    @staticmethod
    def unlock_scores(
        claims: Iterable[str],
        *,
        requested: Iterable[str],
        dependencies: Mapping[str, Iterable[str]],
        blocked: Iterable[str] | None = None,
    ) -> dict[str, int]:
        requested_set = set(requested)
        blocked_set = requested_set if blocked is None else set(blocked)
        reverse: dict[str, set[str]] = defaultdict(set)
        for dependent, prerequisites in dependencies.items():
            for prerequisite in prerequisites:
                reverse[str(prerequisite)].add(str(dependent))

        scores: dict[str, int] = {}
        for claim in claims:
            seen = {claim}
            descendants: set[str] = set()
            queue = deque(reverse.get(claim, ()))
            while queue:
                current = queue.popleft()
                if current in seen:
                    continue
                seen.add(current)
                if current in requested_set and current in blocked_set:
                    descendants.add(current)
                queue.extend(reverse.get(current, ()))
            scores[claim] = len(descendants)
        return scores

    def prioritize(
        self,
        tasks: Iterable[ScheduledTask],
        *,
        requested: Iterable[str],
        dependencies: Mapping[str, Iterable[str]],
        blocked: Iterable[str] | None = None,
    ) -> tuple[ScheduledTask, ...]:
        materialized = tuple(tasks)
        scores = self.unlock_scores(
            (task.claim_id for task in materialized),
            requested=requested,
            dependencies=dependencies,
            blocked=blocked,
        )
        return tuple(
            sorted(
                materialized,
                key=lambda task: (
                    TASK_PRIORITIES[task.task_class],
                    -scores[task.claim_id],
                    task.ready_order,
                    task.claim_id,
                ),
            )
        )


# The public name leaves room for other schedulers without changing callers.
ConcurrencyScheduler = DependencyScheduler
