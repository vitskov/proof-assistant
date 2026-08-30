"""Shared responsive layout primitives for the Textual interface.

Textual applies horizontal and vertical breakpoint classes independently to a
screen.  The application uses :func:`classify_viewport` to add one additional,
mutually exclusive composition class to its root.  Layout CSS consumes only
that derived class, so a wide but short terminal cannot accidentally select a
wide composition.
"""

from __future__ import annotations

from enum import StrEnum

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget

HORIZONTAL_BREAKPOINTS = [
    (0, "-h-under-min"),
    (80, "-h-compact"),
    (120, "-h-standard"),
    (140, "-h-wide"),
]
"""Native Textual width breakpoints shared by every screen."""

VERTICAL_BREAKPOINTS = [
    (0, "-v-under-min"),
    (24, "-v-compact"),
    (32, "-v-standard"),
    (40, "-v-wide"),
]
"""Native Textual height breakpoints shared by every screen."""


class ViewportComposition(StrEnum):
    """The single layout composition selected for the current viewport."""

    RESIZE_NEEDED = "resize-needed"
    COMPACT = "compact"
    COMPACT_SHORT = "compact-short"
    STANDARD = "standard"
    WIDE = "wide"


COMPOSITION_CLASSES = frozenset(
    composition.value for composition in ViewportComposition
)
"""Every app-root class managed by the responsive layout controller."""


def classify_viewport(width: int, height: int) -> ViewportComposition:
    """Return the deterministic, conjunctive composition for a viewport."""

    if width < 80 or height < 24:
        return ViewportComposition.RESIZE_NEEDED
    if width >= 140 and height >= 40:
        return ViewportComposition.WIDE
    if 120 <= width < 140 and height >= 32:
        return ViewportComposition.STANDARD
    if 80 <= width < 120:
        return ViewportComposition.COMPACT
    return ViewportComposition.COMPACT_SHORT


composition_for = classify_viewport
"""Design-contract name retained as an explicit alias."""


def _semantic_classes(semantic_class: str, classes: str | None) -> str:
    """Add a required semantic class without discarding caller classes."""

    return semantic_class if classes is None else f"{semantic_class} {classes}"


class ResponsivePage(Vertical):
    """A full-height shell containing fixed regions and one workspace."""

    def __init__(
        self,
        *children: Widget,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            *children,
            name=name,
            id=id,
            classes=_semantic_classes("responsive-page", classes),
            disabled=disabled,
        )


class PageHeader(Vertical):
    """A fixed-height page title, breadcrumb, and compact-context region."""

    def __init__(
        self,
        *children: Widget,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            *children,
            name=name,
            id=id,
            classes=_semantic_classes("page-header", classes),
            disabled=disabled,
        )


class PageWorkspace(VerticalScroll):
    """The sole primary vertical scroll owner within a responsive page."""

    def __init__(
        self,
        *children: Widget,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            *children,
            name=name,
            id=id,
            classes=_semantic_classes("page-workspace", classes),
            disabled=disabled,
        )


class ActionBar(Horizontal):
    """A fixed page region for primary actions and persistent status."""

    def __init__(
        self,
        *children: Widget,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            *children,
            name=name,
            id=id,
            classes=_semantic_classes("action-bar", classes),
            disabled=disabled,
        )


class ResponsiveToolbar(Horizontal):
    """A toolbar that becomes sequential in compact compositions."""

    def __init__(
        self,
        *children: Widget,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            *children,
            name=name,
            id=id,
            classes=_semantic_classes("responsive-toolbar", classes),
            disabled=disabled,
        )


class ScrollableDialogBody(VerticalScroll):
    """The bounded scroll owner between a dialog's fixed title and actions."""

    def __init__(
        self,
        *children: Widget,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            *children,
            name=name,
            id=id,
            classes=_semantic_classes("dialog-body", classes),
            disabled=disabled,
        )
