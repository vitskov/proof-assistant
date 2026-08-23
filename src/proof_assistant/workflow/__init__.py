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
    ProjectAvailability,
    ProjectCatalogEntry,
    ProjectDestinationInspection,
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
    "ProjectAvailability",
    "ProjectCatalogEntry",
    "ProjectDestinationInspection",
    "ProjectSummary",
    "SourceLocation",
    "SourceInspection",
    "VerificationSettings",
    "CancellationFlag",
    "ProofAssistantWorkflow",
    "ProjectDestinationError",
    "StaleChangePlanError",
    "WorkflowSnapshot",
    "WorkflowState",
]


def __getattr__(name: str):
    if name in {
        "CancellationFlag",
        "ProofAssistantWorkflow",
        "ProjectDestinationError",
        "StaleChangePlanError",
    }:
        from . import service

        return getattr(service, name)
    raise AttributeError(name)
