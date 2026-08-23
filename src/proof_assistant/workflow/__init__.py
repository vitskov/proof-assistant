"""UI-neutral application workflow for Proof Assistant."""

from .contracts import (
    ChangeImpactPlan,
    ClarificationPresentation,
    FileChange,
    FindingSummary,
    NewProjectRequest,
    ProgressEvent,
    ProjectSummary,
    SourceLocation,
    VerificationSettings,
    WorkflowSnapshot,
    WorkflowState,
)

__all__ = [
    "ChangeImpactPlan",
    "ClarificationPresentation",
    "FileChange",
    "FindingSummary",
    "NewProjectRequest",
    "ProgressEvent",
    "ProjectSummary",
    "SourceLocation",
    "VerificationSettings",
    "CancellationFlag",
    "ProofAssistantWorkflow",
    "StaleChangePlanError",
    "WorkflowSnapshot",
    "WorkflowState",
]


def __getattr__(name: str):
    if name in {"CancellationFlag", "ProofAssistantWorkflow", "StaleChangePlanError"}:
        from . import service

        return getattr(service, name)
    raise AttributeError(name)
