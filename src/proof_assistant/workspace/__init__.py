"""Project discovery and external manuscript workspace management."""

from .catalog import ProjectCatalog
from .paths import (
    default_project_path,
    default_projects_root,
    is_in_dropbox,
    slugify_project_name,
    validate_managed_project_path,
)
from .source import InventoryDelta, stable_source_copy

__all__ = [
    "InventoryDelta",
    "ProjectCatalog",
    "default_project_path",
    "default_projects_root",
    "is_in_dropbox",
    "slugify_project_name",
    "stable_source_copy",
    "validate_managed_project_path",
]
