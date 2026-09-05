"""Project discovery and external manuscript workspace management."""

from .catalog import ProjectCatalog
from .paths import (
    ProofAssistantWritePathError,
    default_project_path,
    default_projects_root,
    is_in_dropbox,
    proof_assistant_temporary_directory,
    proof_assistant_temporary_root,
    slugify_project_name,
    validate_managed_project_path,
    validate_proof_assistant_write_path,
)
from .source import InventoryDelta, stable_source_copy

__all__ = [
    "InventoryDelta",
    "ProjectCatalog",
    "ProofAssistantWritePathError",
    "default_project_path",
    "default_projects_root",
    "is_in_dropbox",
    "proof_assistant_temporary_directory",
    "proof_assistant_temporary_root",
    "slugify_project_name",
    "stable_source_copy",
    "validate_managed_project_path",
    "validate_proof_assistant_write_path",
]
