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
    ReportDocument,
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
    "ReportDocument",
    "SourceLocation",
    "SourceInspection",
    "VerificationSettings",
    "CancellationFlag",
    "ProofAssistantWorkflow",
    "ProjectDestinationError",
    "ReportUnavailableError",
    "StaleChangePlanError",
    "WorkflowSnapshot",
    "WorkflowState",
]


def __getattr__(name: str):
    if name in {
        "CancellationFlag",
        "ProofAssistantWorkflow",
        "ProjectDestinationError",
        "ReportUnavailableError",
        "StaleChangePlanError",
    }:
        from . import service

        return getattr(service, name)
    raise AttributeError(name)
