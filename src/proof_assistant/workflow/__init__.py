"""UI-neutral application workflow for Proof Assistant."""

from .contracts import (
    CancellationReport,
    ChangeImpactPlan,
    ClarificationPresentation,
    FileChange,
    FindingSummary,
    LatexSourceCandidate,
    NewProjectRequest,
    ProgressEvent,
    ProjectSummary,
    SourceInspection,
    SourceLocation,
    VerificationSettings,
    WorkflowSnapshot,
    WorkflowState,
)

__all__ = [
    "CancellationReport",
    "ChangeImpactPlan",
    "ClarificationPresentation",
    "FileChange",
    "FindingSummary",
    "LatexSourceCandidate",
    "NewProjectRequest",
    "ProgressEvent",
    "ProjectSummary",
    "SourceLocation",
    "SourceInspection",
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
