"""Reusable geometry assertions for Textual Pilot layout tests."""

from __future__ import annotations

from collections.abc import Iterable

from textual.app import App
from textual.geometry import Region
from textual.widget import Widget


def visible(widget: Widget) -> bool:
    """Return whether a mounted widget participates in the rendered layout."""

    return (
        widget.display
        and widget.visible
        and widget.region.width > 0
        and widget.region.height > 0
    )


def assert_inside_viewport(app: App[object], widget: Widget) -> None:
    """Assert that a visible widget is fully contained by the terminal viewport."""

    assert visible(widget), f"{widget!r} is not visible"
    viewport = Region(0, 0, app.size.width, app.size.height)
    assert viewport.contains_region(widget.region), (
        f"{widget!r} region {widget.region!r} exceeds viewport {viewport!r}"
    )


def assert_regions_do_not_overlap(first: Widget, second: Widget) -> None:
    """Assert that two visible widgets occupy disjoint screen regions."""

    assert visible(first), f"{first!r} is not visible"
    assert visible(second), f"{second!r} is not visible"
    assert not first.region.overlaps(second.region), (
        f"{first!r} region {first.region!r} overlaps "
        f"{second!r} region {second.region!r}"
    )


def assert_exactly_one_visible(widgets: Iterable[Widget]) -> Widget:
    """Return the only rendered widget, failing unless there is exactly one."""

    rendered = [widget for widget in widgets if visible(widget)]
    assert len(rendered) == 1, f"expected one visible widget, found {rendered!r}"
    return rendered[0]


def assert_focus_is_visible(app: App[object]) -> Widget:
    """Assert that keyboard focus belongs to a visible, on-screen widget."""

    focused = app.focused
    assert focused is not None, "application has no focused widget"
    assert_inside_viewport(app, focused)
    return focused
