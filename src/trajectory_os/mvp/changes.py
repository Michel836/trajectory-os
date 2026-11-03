"""MVP — append-only change journal for the daily cockpit.

The portfolio remains the single source of truth. This module only records
*what changed, when, and from which before/after values* so the cockpit can
answer "what changed?" and offer a safe undo for human decisions.

Design invariants:

* append-only JSONL — a change is never rewritten or deleted;
* bounded reads — the cockpit only ever reads a small recent window;
* provenance is explicit — every record carries its kind, object identity and
  source (human, capture, import);
* undoing is a new record that references the original change, never a
  deletion of history;
* journaling is best-effort after the canonical portfolio write: a journal
  failure must never turn a successful human action into an error.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import model, store

#: Journal filename relative to the data root.
CHANGES_NAME = "changes.jsonl"

#: Journal schema version (independent from the portfolio schema).
CHANGE_SCHEMA_VERSION = 1

#: Change kinds (closed set).
KIND_OUTCOME = "OUTCOME"
KIND_CREATE = "CREATE"
KIND_UPDATE = "UPDATE"
KIND_UNDO = "UNDO"
KIND_IMPORT = "IMPORT"
KIND_CAPTURE = "CAPTURE"
KIND_DELETE = "DELETE"
KIND_RESTORE = "RESTORE"
KIND_SIGNAL = "SIGNAL"
KINDS = frozenset({
    KIND_OUTCOME, KIND_CREATE, KIND_UPDATE, KIND_UNDO, KIND_IMPORT,
    KIND_CAPTURE, KIND_DELETE, KIND_RESTORE, KIND_SIGNAL,
})

#: Object kinds (closed set).
OBJECT_TASK = "task"
OBJECT_PROJECT = "project"
OBJECT_BATCH = "batch"
OBJECT_KINDS = frozenset({OBJECT_TASK, OBJECT_PROJECT, OBJECT_BATCH})

#: Defensive bound for one read (the UI never needs the full history).
MAX_READ = 500
#: Default bound on the recent-change feed exposed to the cockpit.
RECENT_LIMIT = 50


class ChangeError(model.MvpError):
    """A change record is malformed (fail closed)."""


def changes_path(root: str | Path) -> Path:
    return Path(root) / CHANGES_NAME


def _optional_map(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): value[key] for key in value}


@dataclass(frozen=True)
class ChangeRecord:
    """One durable, human-readable change entry."""

    change_id: str
    at: str
    kind: str
    object_kind: str
    object_id: str
    title: str
    summary: str
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    source: str = "human"
    undoable: bool = False
    references: str = ""
    schema_version: int = CHANGE_SCHEMA_VERSION

    def validate(self) -> ChangeRecord:
        if self.schema_version != CHANGE_SCHEMA_VERSION:
            raise ChangeError(model.E_UNSUPPORTED_VERSION,
                              str(self.schema_version))
        if self.kind not in KINDS:
            raise ChangeError(model.E_MALFORMED, f"kind {self.kind!r}")
        if self.object_kind not in OBJECT_KINDS:
            raise ChangeError(model.E_MALFORMED,
                              f"object_kind {self.object_kind!r}")
        if not self.change_id or not self.object_id or not self.at:
            raise ChangeError(model.E_MALFORMED, "identity required")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "change_id": self.change_id,
            "at": self.at,
            "kind": self.kind,
            "object_kind": self.object_kind,
            "object_id": self.object_id,
            "title": self.title,
            "summary": self.summary,
            "before": dict(self.before),
            "after": dict(self.after),
            "source": self.source,
            "undoable": self.undoable,
            "references": self.references,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ChangeRecord:
        return ChangeRecord(
            change_id=str(data.get("change_id", "")),
            at=str(data.get("at", "")),
            kind=str(data.get("kind", "")),
            object_kind=str(data.get("object_kind", "")),
            object_id=str(data.get("object_id", "")),
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            before=_optional_map(data.get("before")),
            after=_optional_map(data.get("after")),
            source=str(data.get("source", "human")),
            undoable=bool(data.get("undoable", False)),
            references=str(data.get("references", "")),
            schema_version=int(data.get("schema_version",
                                       CHANGE_SCHEMA_VERSION)),
        ).validate()


def append_change(
    root: str | Path,
    *,
    kind: str,
    object_kind: str,
    object_id: str,
    title: str,
    summary: str,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    source: str = "human",
    undoable: bool = False,
    references: str = "",
    at: str = "",
) -> ChangeRecord:
    """Append one validated change record; return it.

    The caller is responsible for having already persisted the canonical
    portfolio change. This function performs no business mutation.
    """
    record = ChangeRecord(
        change_id=uuid.uuid4().hex[:16],
        at=at or intel_model.utc_now(),
        kind=kind,
        object_kind=object_kind,
        object_id=object_id,
        title=title,
        summary=summary,
        before=dict(before or {}),
        after=dict(after or {}),
        source=source,
        undoable=undoable,
        references=references,
    ).validate()
    intel_model.append_jsonl(changes_path(root), record.to_dict())
    return record


def read_changes(
    root: str | Path,
    *,
    limit: int = 50,
    since: str = "",
) -> tuple[ChangeRecord, ...]:
    """Return the most recent changes, newest first, bounded by ``limit``."""
    if limit <= 0:
        return ()
    bound = min(int(limit), MAX_READ)
    records: list[ChangeRecord] = []
    for document in intel_model.read_jsonl(changes_path(root)):
        try:
            record = ChangeRecord.from_dict(document)
        except (ChangeError, ValueError):
            continue
        if since and record.at < since:
            continue
        records.append(record)
    records.reverse()
    return tuple(records[:bound])


def undone_change_ids(root: str | Path) -> set[str]:
    """Ids of changes already undone by a later UNDO record."""
    return {
        record.references
        for record in read_changes(root, limit=MAX_READ)
        if record.kind == KIND_UNDO and record.references
    }


def last_undoable(
    root: str | Path,
    object_kind: str,
    object_id: str,
) -> ChangeRecord | None:
    """Return the most recent not-yet-undone undoable change for an object."""
    undone = undone_change_ids(root)
    for record in read_changes(root, limit=MAX_READ):
        if record.object_kind != object_kind:
            continue
        if record.object_id != object_id:
            continue
        if record.kind == KIND_UNDO or not record.undoable:
            continue
        if record.change_id in undone:
            continue
        return record
    return None


def _find_change(root: str | Path, change_id: str) -> ChangeRecord:
    for record in read_changes(root, limit=MAX_READ):
        if record.change_id == change_id:
            return record
    raise ChangeError(model.E_UNKNOWN_ID, change_id)


def _append_undo(root: str | Path, record: ChangeRecord, *,
                 before: Mapping[str, Any], after: Mapping[str, Any],
                 summary: str) -> ChangeRecord:
    return append_change(
        root, kind=KIND_UNDO, object_kind=record.object_kind,
        object_id=record.object_id, title=record.title, summary=summary,
        before=before, after=after, source="human", undoable=False,
        references=record.change_id)


def undo_change(root: str | Path, change_id: str) -> dict[str, Any]:
    """Undo one journaled change, safely and explicitly.

    Supported: outcomes, field updates, created objects, capture/import
    batches and soft deletes. The operation is journaled as a new ``UNDO``
    record; history is never rewritten.
    """
    from trajectory_os.mvp import outcomes, trash  # local: avoid cycles

    record = _find_change(root, change_id)
    if not record.undoable or record.kind in (KIND_UNDO, KIND_RESTORE):
        raise ChangeError(model.E_MALFORMED,
                          f"change {change_id!r} is not undoable")
    if change_id in undone_change_ids(root):
        raise ChangeError(model.E_MALFORMED,
                          f"change {change_id!r} is already undone")

    if record.kind == KIND_DELETE:
        restored_entry = trash.restore_trash(root, record.references)
        _append_undo(root, record, before={"trashed": record.object_id},
                     after=restored_entry, summary="suppression annulée")
        return {"undone": change_id, "kind": record.kind,
                "restored": record.object_id}

    if record.kind in (KIND_CAPTURE, KIND_IMPORT):
        created_tasks = [str(value) for value in
                         record.after.get("created_task_ids", [])]
        created_projects = [str(value) for value in
                            record.after.get("created_project_ids", [])]
        trashed: list[str] = []
        for task_id in created_tasks:
            try:
                trash.trash_task(root, task_id, reason="undo-batch")
                trashed.append(task_id)
            except model.MvpError:
                continue
        for project_id in created_projects:
            try:
                trash.trash_project(root, project_id, reason="undo-batch",
                                    cascade=True)
                trashed.append(project_id)
            except model.MvpError:
                continue
        _append_undo(root, record, before={"created": list(trashed)},
                     after={}, summary="lot annulé")
        return {"undone": change_id, "kind": record.kind,
                "trashed": trashed}

    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise ChangeError(model.E_MALFORMED, "no portfolio loaded")
    stamp = intel_model.utc_now()

    if record.kind == KIND_OUTCOME:
        if record.object_kind != OBJECT_TASK:
            raise ChangeError(model.E_MALFORMED, "outcome target must be task")
        outcome_task = portfolio.task_map().get(record.object_id)
        if outcome_task is None:
            raise ChangeError(model.E_UNKNOWN_ID, record.object_id)
        if outcome_task.status != record.after.get("status") or \
                outcome_task.actual_minutes != record.after.get(
                    "actual_minutes"):
            raise ChangeError(model.E_MALFORMED,
                              "task changed since the recorded outcome")
        restored_outcome = model.Task.from_dict({
            **outcome_task.to_dict(), "status": record.before.get("status"),
            "actual_minutes": record.before.get("actual_minutes"),
            "updated_at": stamp})
        outcome_tasks = tuple(
            restored_outcome if task.task_id == outcome_task.task_id else task
            for task in portfolio.tasks)
        outcome_project = outcomes.derive_project_status(
            portfolio, outcome_tasks, outcome_task.project_id)
        outcome_projects = tuple(
            outcome_project if project.project_id == outcome_task.project_id
            else project for project in portfolio.projects)
        new_portfolio = replace(
            portfolio, tasks=outcome_tasks, projects=outcome_projects,
            updated_at=stamp).validate()
        store.save_portfolio(root, new_portfolio)
        _append_undo(
            root, record,
            before={"status": outcome_task.status,
                    "actual_minutes": outcome_task.actual_minutes},
            after={"status": restored_outcome.status,
                   "actual_minutes": restored_outcome.actual_minutes},
            summary="résultat annulé")
        return {"undone": change_id, "kind": record.kind,
                "restored_status": restored_outcome.status}

    if record.kind == KIND_UPDATE and record.object_kind == OBJECT_TASK:
        update_task = portfolio.task_map().get(record.object_id)
        if update_task is None:
            raise ChangeError(model.E_UNKNOWN_ID, record.object_id)
        restored_task = model.Task.from_dict({
            **update_task.to_dict(), **record.before, "updated_at": stamp})
        update_tasks = tuple(
            restored_task if task.task_id == update_task.task_id else task
            for task in portfolio.tasks)
        update_project = outcomes.derive_project_status(
            portfolio, update_tasks, update_task.project_id)
        update_projects = tuple(
            update_project if project.project_id == update_task.project_id
            else project for project in portfolio.projects)
        new_portfolio = replace(
            portfolio, tasks=update_tasks, projects=update_projects,
            updated_at=stamp).validate()
        store.save_portfolio(root, new_portfolio)
        _append_undo(
            root, record,
            before={key: getattr(update_task, key) for key in record.before},
            after={key: getattr(restored_task, key) for key in record.before},
            summary="modification annulée")
        return {"undone": change_id, "kind": record.kind,
                "restored": record.object_id}

    if record.kind == KIND_UPDATE and record.object_kind == OBJECT_PROJECT:
        source_project = portfolio.project_map().get(record.object_id)
        if source_project is None:
            raise ChangeError(model.E_UNKNOWN_PROJECT, record.object_id)
        restored_project = model.Project.from_dict({
            **source_project.to_dict(), **record.before})
        project_updates = tuple(
            restored_project if project.project_id == source_project.project_id
            else project for project in portfolio.projects)
        new_portfolio = replace(
            portfolio, projects=project_updates,
            updated_at=stamp).validate()
        store.save_portfolio(root, new_portfolio)
        _append_undo(
            root, record,
            before={key: getattr(source_project, key) for key in record.before},
            after={key: getattr(restored_project, key)
                   for key in record.before},
            summary="modification annulée")
        return {"undone": change_id, "kind": record.kind,
                "restored": record.object_id}

    if record.kind == KIND_CREATE:
        if record.object_kind == OBJECT_TASK:
            trash.trash_task(root, record.object_id, reason="undo-create")
        elif record.object_kind == OBJECT_PROJECT:
            trash.trash_project(root, record.object_id, reason="undo-create",
                                cascade=True)
        else:
            raise ChangeError(model.E_MALFORMED, "unsupported created object")
        _append_undo(root, record, before={"created": record.object_id},
                     after={}, summary="création annulée")
        return {"undone": change_id, "kind": record.kind,
                "trashed": record.object_id}

    raise ChangeError(model.E_MALFORMED, f"kind {record.kind!r} not undoable")


def recent_changes(
    root: str | Path,
    portfolio: model.Portfolio,
    *,
    limit: int = RECENT_LIMIT,
    since: str = "",
) -> list[dict[str, Any]]:
    """Merge the journal with pre-journal outcome history (read-only).

    Outcomes recorded before the oldest journal entry are projected as
    legacy, non-undoable entries so the cockpit answers "what changed?"
    immediately after the journal is introduced. No file is written.
    """
    if limit <= 0:
        return []
    bound = min(int(limit), MAX_READ)
    journal = list(read_changes(root, limit=bound, since=since))
    oldest_journal = journal[-1].at if journal else ""
    tasks = portfolio.task_map()
    legacy: list[dict[str, Any]] = []
    for record in reversed(store.read_outcomes(root)):
        if since and record.recorded_at < since:
            continue
        if oldest_journal and record.recorded_at >= oldest_journal:
            continue
        task = tasks.get(record.task_id)
        summary = record.outcome
        if record.actual_minutes is not None:
            summary += f" · {record.actual_minutes}m"
        legacy.append({
            "schema_version": CHANGE_SCHEMA_VERSION,
            "change_id": f"legacy-{record.task_id}-{record.recorded_at}",
            "at": record.recorded_at,
            "kind": KIND_OUTCOME,
            "object_kind": OBJECT_TASK,
            "object_id": record.task_id,
            "title": task.title if task is not None else record.task_id,
            "summary": summary,
            "before": {},
            "after": {},
            "source": "legacy",
            "undoable": False,
            "references": "",
        })
        if len(journal) + len(legacy) >= bound:
            break
    merged = [record.to_dict() for record in journal] + legacy
    merged.sort(key=lambda item: str(item.get("at", "")), reverse=True)
    return merged[:bound]


__all__ = [
    "CHANGES_NAME", "CHANGE_SCHEMA_VERSION", "ChangeError", "ChangeRecord",
    "KINDS", "KIND_CAPTURE", "KIND_CREATE", "KIND_DELETE", "KIND_IMPORT",
    "KIND_OUTCOME", "KIND_RESTORE", "KIND_SIGNAL", "KIND_UNDO",
    "KIND_UPDATE", "MAX_READ", "OBJECT_BATCH", "OBJECT_KINDS",
    "OBJECT_PROJECT", "OBJECT_TASK", "RECENT_LIMIT", "append_change",
    "changes_path", "last_undoable", "read_changes", "recent_changes",
    "undone_change_ids",
]
