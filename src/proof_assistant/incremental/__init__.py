"""Deterministic persistent manuscript-verification infrastructure."""

from .models import ClaimState, SourceObject, TaskSpec

__all__ = ["ClaimState", "IncrementalSession", "SourceObject", "TaskSpec"]


def __getattr__(name: str):
    """Load the session lazily so neutral submodules remain independently usable."""

    if name == "IncrementalSession":
        from .session import IncrementalSession

        return IncrementalSession
    raise AttributeError(name)
