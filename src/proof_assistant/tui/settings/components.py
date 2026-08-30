"""Reusable, backend-free widgets for role-aware AI settings.

The value objects in this module deliberately contain no credential fields.
Screens translate workflow snapshots into these display-only contracts and keep
all persistence and provider operations outside the widgets.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Self

from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import DataTable, Select, Static

from proof_assistant.workflow.contracts import (
    Difficulty,
    DriverId,
    DriverTransport,
    SettingsScopeKind,
    TaskKind,
    validate_model_identifier,
)

ROLE_LABELS: Mapping[TaskKind, str] = MappingProxyType(
    {
        TaskKind.CLARIFICATION: "Author clarification",
        TaskKind.DIAGNOSTIC: "Scan / triage diagnostics",
        TaskKind.PROOF: "Primary prove agent",
        TaskKind.SKETCH: "Sketch agent",
        TaskKind.MAINTENANCE: "Maintain / fix agent",
        TaskKind.REVIEW: "Math and engineering reviewers",
        TaskKind.DUPLICATE_PROOF: "Independent prove agent",
        TaskKind.REPORTING: "Progress / reporting agent",
    }
)

DRIVER_LABELS: Mapping[DriverId, str] = MappingProxyType(
    {
        DriverId.CODEX_CLI: "OpenAI Codex CLI",
        DriverId.CLAUDE_CLI: "Anthropic Claude Code CLI",
        DriverId.COPILOT_CLI: "GitHub Copilot CLI",
        DriverId.OPENAI_API: "OpenAI API",
        DriverId.ANTHROPIC_API: "Anthropic API",
        DriverId.GEMINI_API: "Google Gemini API",
    }
)

_TASK_ORDER = {task: index for index, task in enumerate(TaskKind)}
_DRIVER_ORDER = {driver: index for index, driver in enumerate(DriverId)}


def role_label(task: TaskKind) -> str:
    """Return the canonical user-facing label for a verification role."""

    return ROLE_LABELS[task]


def driver_label(driver: DriverId) -> str:
    """Return the canonical user-facing label for an AI provider driver."""

    return DRIVER_LABELS[driver]


@dataclass(frozen=True, slots=True)
class RoleAssignmentRow:
    """One fully resolved role assignment rendered by :class:`RoleRoster`."""

    task: TaskKind
    model: str | None
    effort: Difficulty
    state: str

    def __post_init__(self) -> None:
        if self.model is not None:
            validate_model_identifier(self.model, field_name="model")
        if not self.state.strip():
            raise ValueError("role state must not be empty")


@dataclass(frozen=True, slots=True)
class ProviderConnectionRow:
    """Credential-free provider connection state for the provider roster."""

    driver: DriverId
    transport: DriverTransport
    connection: str
    state: str
    primary: bool = False

    def __post_init__(self) -> None:
        if not self.connection.strip():
            raise ValueError("provider connection must not be empty")
        if not self.state.strip():
            raise ValueError("provider state must not be empty")


@dataclass(frozen=True, slots=True)
class RoleDraft:
    """A credential-free editable assignment for exactly one role."""

    task: TaskKind
    driver: DriverId
    model: str | None
    effort: Difficulty

    def __post_init__(self) -> None:
        if self.model is not None:
            validate_model_identifier(self.model, field_name="model")


def _complete_role_draft(draft: Iterable[RoleDraft]) -> tuple[RoleDraft, ...]:
    values = tuple(draft)
    tasks = tuple(item.task for item in values)
    if len(tasks) != len(set(tasks)):
        raise ValueError("role draft must contain each task exactly once")
    missing = set(TaskKind) - set(tasks)
    extra = set(tasks) - set(TaskKind)
    if missing or extra:
        missing_names = ", ".join(task.value for task in TaskKind if task in missing)
        extra_names = ", ".join(sorted(task.value for task in extra))
        detail = "; ".join(
            part
            for part in (
                f"missing: {missing_names}" if missing_names else "",
                f"unknown: {extra_names}" if extra_names else "",
            )
            if part
        )
        raise ValueError(f"role draft must cover all eight roles ({detail})")
    return tuple(sorted(values, key=lambda item: _TASK_ORDER[item.task]))


@dataclass(frozen=True, slots=True)
class ProviderDefaultsPreviewState:
    """Complete role draft plus a one-step, credential-free defaults undo."""

    draft: tuple[RoleDraft, ...]
    undo_draft: tuple[RoleDraft, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "draft", _complete_role_draft(self.draft))
        if self.undo_draft is not None:
            object.__setattr__(
                self, "undo_draft", _complete_role_draft(self.undo_draft)
            )

    @classmethod
    def from_draft(cls, draft: Iterable[RoleDraft]) -> Self:
        return cls(draft=_complete_role_draft(draft))

    @property
    def can_undo(self) -> bool:
        return self.undo_draft is not None

    def for_task(self, task: TaskKind) -> RoleDraft:
        return next(item for item in self.draft if item.task is task)

    def preview(self, defaults: Iterable[RoleDraft]) -> Self:
        return type(self)(
            draft=_complete_role_draft(defaults),
            undo_draft=self.draft,
        )

    def undo(self) -> Self:
        if self.undo_draft is None:
            return self
        return type(self)(draft=self.undo_draft)

    def accept(self) -> Self:
        return type(self)(draft=self.draft)


def selected_row_key(table: DataTable[str]) -> str | None:
    """Return the selected row key without relying on DataTable internals."""

    if not 0 <= table.cursor_row < table.row_count:
        return None
    return table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value


def replace_rows_preserving_selection(
    table: DataTable[str],
    rows: Iterable[tuple[str, Sequence[str]]],
    *,
    selected_key: str | None = None,
) -> str | None:
    """Replace table rows and restore selection by stable row key.

    ``selected_key`` overrides the table's current selection. If the requested
    row disappeared, the first row is selected. The selected key is returned.
    """

    previous_key = selected_key if selected_key is not None else selected_row_key(table)
    materialized = tuple(rows)
    table.clear(columns=False)
    keys: list[str] = []
    for key, cells in materialized:
        if key in keys:
            raise ValueError(f"duplicate table row key: {key}")
        keys.append(key)
        table.add_row(*cells, key=key)
    if not keys:
        return None
    restored_key = previous_key if previous_key in keys else keys[0]
    table.move_cursor(row=keys.index(restored_key), column=0, animate=False)
    return restored_key


class RoleRoster(DataTable[str]):
    """Selectable all-role roster with stable :class:`TaskKind` row keys."""

    def __init__(
        self,
        rows: Iterable[RoleAssignmentRow] = (),
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            cursor_type="row",
            zebra_stripes=True,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self._rows = self._validate_rows(rows)
        self._columns_ready = False

    @staticmethod
    def _validate_rows(
        rows: Iterable[RoleAssignmentRow],
    ) -> tuple[RoleAssignmentRow, ...]:
        values = tuple(rows)
        tasks = tuple(item.task for item in values)
        if len(tasks) != len(set(tasks)):
            raise ValueError("role roster rows must have unique tasks")
        if values and set(tasks) != set(TaskKind):
            raise ValueError("role roster must show all eight roles")
        return tuple(sorted(values, key=lambda item: _TASK_ORDER[item.task]))

    def on_mount(self) -> None:
        self.add_columns("Role", "Model", "Effort", "State")
        self._columns_ready = True
        self._render_rows()

    @property
    def selected_task(self) -> TaskKind | None:
        key = selected_row_key(self)
        return TaskKind(key) if key is not None else None

    def set_roles(
        self,
        rows: Iterable[RoleAssignmentRow],
        *,
        selected: TaskKind | None = None,
    ) -> None:
        self._rows = self._validate_rows(rows)
        if self._columns_ready:
            self._render_rows(selected=selected)

    def update_role(self, row: RoleAssignmentRow) -> None:
        current = {item.task: item for item in self._rows}
        if set(current) != set(TaskKind):
            raise ValueError("cannot update a role before the complete roster is set")
        current[row.task] = row
        self.set_roles(current.values(), selected=self.selected_task)

    def select_task(self, task: TaskKind) -> None:
        if task not in {item.task for item in self._rows}:
            raise ValueError(f"role is not present in roster: {task.value}")
        self.move_cursor(
            row=next(
                index for index, item in enumerate(self._rows) if item.task is task
            ),
            column=0,
            animate=False,
        )

    def _render_rows(self, *, selected: TaskKind | None = None) -> None:
        replace_rows_preserving_selection(
            self,
            (
                (
                    row.task.value,
                    (
                        role_label(row.task),
                        row.model or "Provider default",
                        row.effort.value,
                        row.state,
                    ),
                )
                for row in self._rows
            ),
            selected_key=selected.value if selected is not None else None,
        )


class SelectedRoleDetail(Vertical):
    """Layout shell for controls belonging to the selected role."""

    def __init__(
        self,
        *children: Widget,
        task: TaskKind = TaskKind.PROOF,
        model: str | None = None,
        effort: Difficulty = Difficulty.AUTO,
        state: str = "Not loaded",
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        self._title = Static(classes="role-detail-title")
        self._summary = Static(classes="role-detail-summary")
        self._body = Vertical(*children, classes="role-detail-body")
        super().__init__(
            self._title,
            self._summary,
            self._body,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self.update_role(task=task, model=model, effort=effort, state=state)

    @property
    def body(self) -> Vertical:
        return self._body

    def update_role(
        self,
        *,
        task: TaskKind,
        model: str | None,
        effort: Difficulty,
        state: str,
    ) -> None:
        self._title.update(role_label(task))
        self._summary.update(
            f"{model or 'Provider default'} · {effort.value} · {state}"
        )


class ProviderConnectionRoster(DataTable[str]):
    """Selectable provider roster with stable :class:`DriverId` row keys."""

    def __init__(
        self,
        rows: Iterable[ProviderConnectionRow] = (),
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            cursor_type="row",
            zebra_stripes=True,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self._rows = self._validate_rows(rows)
        self._columns_ready = False

    @staticmethod
    def _validate_rows(
        rows: Iterable[ProviderConnectionRow],
    ) -> tuple[ProviderConnectionRow, ...]:
        values = tuple(rows)
        drivers = tuple(item.driver for item in values)
        if len(drivers) != len(set(drivers)):
            raise ValueError("provider roster rows must have unique drivers")
        return tuple(sorted(values, key=lambda item: _DRIVER_ORDER[item.driver]))

    def on_mount(self) -> None:
        self.add_columns("Provider", "Type", "Connection", "State")
        self._columns_ready = True
        self._render_rows()

    @property
    def selected_driver(self) -> DriverId | None:
        key = selected_row_key(self)
        return DriverId(key) if key is not None else None

    def set_providers(
        self,
        rows: Iterable[ProviderConnectionRow],
        *,
        selected: DriverId | None = None,
    ) -> None:
        self._rows = self._validate_rows(rows)
        if self._columns_ready:
            self._render_rows(selected=selected)

    def update_provider(self, row: ProviderConnectionRow) -> None:
        current = {item.driver: item for item in self._rows}
        current[row.driver] = row
        self.set_providers(current.values(), selected=self.selected_driver)

    def select_driver(self, driver: DriverId) -> None:
        if driver not in {item.driver for item in self._rows}:
            raise ValueError(f"provider is not present in roster: {driver.value}")
        self.move_cursor(
            row=next(
                index for index, item in enumerate(self._rows) if item.driver is driver
            ),
            column=0,
            animate=False,
        )

    def _render_rows(self, *, selected: DriverId | None = None) -> None:
        replace_rows_preserving_selection(
            self,
            (
                (
                    row.driver.value,
                    (
                        driver_label(row.driver)
                        + (" (primary)" if row.primary else ""),
                        row.transport.value.upper(),
                        row.connection,
                        row.state,
                    ),
                )
                for row in self._rows
            ),
            selected_key=selected.value if selected is not None else None,
        )


class SettingsScopeSelector(Vertical):
    """Explicit machine/project scope control paired with persistent status."""

    def __init__(
        self,
        *,
        scope: SettingsScopeKind = SettingsScopeKind.MACHINE,
        status: str = "Machine settings apply to every local project.",
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        self.scope_select = Select[SettingsScopeKind](
            (
                ("Machine · all local projects", SettingsScopeKind.MACHINE),
                ("Project · current project only", SettingsScopeKind.PROJECT),
            ),
            allow_blank=False,
            value=scope,
            id="settings-scope",
            classes="settings-scope-select",
        )
        self.status = Static(status, classes="settings-scope-status")
        super().__init__(
            self.scope_select,
            self.status,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )

    @property
    def scope(self) -> SettingsScopeKind:
        value = self.scope_select.value
        if not isinstance(value, SettingsScopeKind):
            raise RuntimeError("settings scope selector unexpectedly has no value")
        return value

    def update_scope(self, scope: SettingsScopeKind, *, status: str) -> None:
        self.scope_select.value = scope
        self.status.update(status)
