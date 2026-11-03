"""MVP — durable soft-delete (corbeille) for tasks and projects.

Deleting is a human decision, not a destructive side effect. A trashed object
is removed from the canonical portfolio but its full payload is written to a
bounded ``trash/`` sidecar and can be restored later. Dependencies that point
at a trashed task are removed from the remaining tasks so the portfolio stays
valid.

Invariants:

* every removal is journaled as ``DELETE`` with a reference to the trash entry;
* restoring is journaled as ``RESTORE``;
* a project can only be trashed with its tasks when ``cascade=True`` is
  explicit; otherwise a non-empty project fails closed;
* restore validates the candidate portfolio before writing and drops
  dependencies that no longer exist, reporting them explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import changes, model, store

TRASH_DIR = "trash"
TRASH_SCHEMA_VERSION = 1
MAX_TRASH_ENTRIES = 500

_SAFE_ID = re.compile(r"[^a-z0-9._-]+")


class TrashError(model.MvpError):
    """A trash operation cannot be satisfied (fail closed)."""


@dataclass(frozen=True)
class TrashEntry:
    trash_id: str
    kind: str
    object_id: str
    title: str
    deleted_at: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = TRASH_SCHEMA_VERSION

    def validate(self) -> TrashEntry:
        if self.schema_version != TRASH_SCHEMA_VERSION:
            raise TrashError(model.E_UNSUPPORTED_VERSION,
                             str(self.schema_version))
        if self.kind not in ("task", "project"):
            raise TrashError(model.E_MALFORMED, f"kind {self.kind!r}")
        if not self.trash_id or not self.object_id:
            raise TrashError(model.E_MALFORMED, "identity required")
        if not isinstance(self.payload, dict):
            raise TrashError(model.E_MALFORMED, "payload required")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trash_id": self.trash_id,
            "kind": self.kind,
            "object_id": self.object_id,
            "title": self.title,
            "deleted_at": self.deleted_at,
            "reason": self.reason,
            "payload": self.payload,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TrashEntry:
        raw_payload = data.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        return TrashEntry(
            trash_id=str(data.get("trash_id", "")),
            kind=str(data.get("kind", "")),
            object_id=str(data.get("object_id", "")),
            title=str(data.get("title", "")),
            deleted_at=str(data.get("deleted_at", "")),
            reason=str(data.get("reason", "")),
            payload=payload,
            schema_version=int(data.get("schema_version",
                                       TRASH_SCHEMA_VERSION)),
        ).validate()


def trash_dir(root: str | Path) -> Path:
    return Path(root) / TRASH_DIR


def _entry_path(root: str | Path, trash_id: str) -> Path:
    return trash_dir(root) / f"{trash_id}.json"


def _safe(value: str) -> str:
    return _SAFE_ID.sub("-", value.lower()).strip("-")[:80] or "item"


def _new_trash_id(kind: str, object_id: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    return f"{stamp}-{kind}-{_safe(object_id)}"


def _write_entry(root: str | Path, entry: TrashEntry) -> None:
    intel_model.write_json(_entry_path(root, entry.trash_id),
                           entry.to_dict())


def _remove_entry(root: str | Path, trash_id: str) -> None:
    path = _entry_path(root, trash_id)
    if path.is_file():
        path.unlink()


def _reapply_edge(task: model.Task, removed_id: str,
                  edge: dict[str, Any], stamp: str) -> model.Task:
    dependencies = task.dependencies
    blocked_by = task.blocked_by
    if edge.get("dependencies") is True and removed_id not in dependencies:
        dependencies = dependencies + (removed_id,)
    if edge.get("blocked_by") is True and removed_id not in blocked_by:
        blocked_by = blocked_by + (removed_id,)
    if dependencies == task.dependencies and blocked_by == task.blocked_by:
        return task
    return replace(task, dependencies=dependencies, blocked_by=blocked_by,
                   updated_at=stamp)


def list_trash(root: str | Path) -> tuple[TrashEntry, ...]:
    folder = trash_dir(root)
    if not folder.is_dir():
        return ()
    entries: list[TrashEntry] = []
    for path in sorted(folder.glob("*.json"), reverse=True):
        document = intel_model.read_json(path)
        if not isinstance(document, dict):
            continue
        try:
            entries.append(TrashEntry.from_dict(document))
        except (TrashError, ValueError):
            continue
    return tuple(entries[:MAX_TRASH_ENTRIES])


def _clean_task_refs(tasks: Sequence[model.Task],
                     removed: set[str]) -> tuple[model.Task, ...]:
    cleaned: list[model.Task] = []
    for task in tasks:
        dependencies = tuple(dep for dep in task.dependencies
                             if dep not in removed)
        blocked_by = tuple(dep for dep in task.blocked_by
                           if dep not in removed)
        if dependencies != task.dependencies or blocked_by != task.blocked_by:
            task = model.Task(
                **{**task.to_dict(), "dependencies": dependencies,
                   "blocked_by": blocked_by})
        cleaned.append(task)
    return tuple(cleaned)


def _journal_delete(root: str | Path, *, kind: str, object_id: str,
                    title: str, before: dict[str, Any], trash_id: str,
                    at: str) -> None:
    changes.append_change(
        root, kind=changes.KIND_DELETE,
        object_kind=(changes.OBJECT_TASK if kind == "task"
                     else changes.OBJECT_PROJECT),
        object_id=object_id, title=title, summary="mis à la corbeille",
        before=before, source="human", undoable=True, references=trash_id,
        at=at)


def trash_task(root: str | Path, task_id: str,
               *, reason: str = "manual") -> dict[str, Any]:
    """Move one task to the trash and clean incoming references."""
    with store.portfolio_lock(root):
        portfolio = store.load_portfolio(root)
        if portfolio is None:
            raise TrashError(model.E_MALFORMED, "no portfolio loaded")
        task = portfolio.task_map().get(task_id)
        if task is None:
            raise TrashError(model.E_UNKNOWN_ID, task_id)
        incoming = [
            {"task_id": other.task_id,
             "dependencies": task_id in other.dependencies,
             "blocked_by": task_id in other.blocked_by}
            for other in portfolio.tasks
            if other.task_id != task_id
            and (task_id in other.dependencies
                 or task_id in other.blocked_by)
        ]
        stamp = intel_model.utc_now()
        remaining = tuple(t for t in portfolio.tasks
                          if t.task_id != task_id)
        new_portfolio = replace(
            portfolio, tasks=_clean_task_refs(remaining, {task_id}),
            updated_at=stamp).validate()
        entry = TrashEntry(
            trash_id=_new_trash_id("task", task_id), kind="task",
            object_id=task_id, title=task.title, deleted_at=stamp,
            reason=reason,
            payload={"task": task.to_dict(),
                     "incoming": incoming}).validate()
        _write_entry(root, entry)
        try:
            store.save_portfolio(root, new_portfolio)
        except Exception:
            _remove_entry(root, entry.trash_id)
            raise
        _journal_delete(root, kind="task", object_id=task_id,
                        title=task.title, before=task.to_dict(),
                        trash_id=entry.trash_id, at=stamp)
        return entry.to_dict()


def trash_project(root: str | Path, project_id: str,
                  *, reason: str = "manual",
                  cascade: bool = False) -> dict[str, Any]:
    """Move a project (and optionally all its tasks) to the trash."""
    with store.portfolio_lock(root):
        portfolio = store.load_portfolio(root)
        if portfolio is None:
            raise TrashError(model.E_MALFORMED, "no portfolio loaded")
        project = portfolio.project_map().get(project_id)
        if project is None:
            raise TrashError(model.E_UNKNOWN_PROJECT, project_id)
        project_tasks = tuple(t for t in portfolio.tasks
                              if t.project_id == project_id)
        if project_tasks and not cascade:
            raise TrashError(
                model.E_MALFORMED,
                "project has tasks; pass cascade=true to trash them")
        removed = {task.task_id for task in project_tasks}
        incoming: list[dict[str, Any]] = []
        for other in portfolio.tasks:
            if other.project_id == project_id:
                continue
            for removed_id in removed:
                if removed_id in other.dependencies \
                        or removed_id in other.blocked_by:
                    incoming.append({
                        "task_id": other.task_id,
                        "removed": removed_id,
                        "dependencies": removed_id in other.dependencies,
                        "blocked_by": removed_id in other.blocked_by,
                    })
        stamp = intel_model.utc_now()
        remaining = tuple(t for t in portfolio.tasks
                          if t.project_id != project_id)
        new_portfolio = replace(
            portfolio,
            projects=tuple(p for p in portfolio.projects
                           if p.project_id != project_id),
            tasks=_clean_task_refs(remaining, removed),
            updated_at=stamp).validate()
        entry = TrashEntry(
            trash_id=_new_trash_id("project", project_id), kind="project",
            object_id=project_id, title=project.name, deleted_at=stamp,
            reason=reason,
            payload={"project": project.to_dict(),
                     "tasks": [t.to_dict() for t in project_tasks],
                     "incoming": incoming}).validate()
        _write_entry(root, entry)
        try:
            store.save_portfolio(root, new_portfolio)
        except Exception:
            _remove_entry(root, entry.trash_id)
            raise
        _journal_delete(root, kind="project", object_id=project_id,
                        title=project.name,
                        before={"project": project.to_dict(),
                                "task_ids": sorted(removed)},
                        trash_id=entry.trash_id, at=stamp)
        return entry.to_dict()


def restore_trash(root: str | Path, trash_id: str) -> dict[str, Any]:
    """Restore one trash entry, dropping dangling dependency references."""
    with store.portfolio_lock(root):
        path = _entry_path(root, trash_id)
        document = intel_model.read_json(path)
        if not isinstance(document, dict):
            raise TrashError(model.E_UNKNOWN_ID, trash_id)
        entry = TrashEntry.from_dict(document)
        portfolio = store.load_portfolio(root)
        if portfolio is None:
            raise TrashError(model.E_MALFORMED, "no portfolio loaded")
        stamp = intel_model.utc_now()
        existing_tasks = portfolio.task_map()
        existing_projects = portfolio.project_map()
        dropped: list[str] = []

        if entry.kind == "task":
            raw = entry.payload.get("task")
            if not isinstance(raw, dict):
                raise TrashError(model.E_MALFORMED, "task payload missing")
            task = model.Task.from_dict(raw)
            if task.task_id in existing_tasks:
                raise TrashError(model.E_DUPLICATE_OBJECT, task.task_id)
            allowed = set(existing_tasks) | {task.task_id}
            dependencies = tuple(d for d in task.dependencies
                                 if d in allowed)
            blocked_by = tuple(d for d in task.blocked_by if d in allowed)
            dropped = sorted(set(task.dependencies) - set(dependencies)) + \
                sorted(set(task.blocked_by) - set(blocked_by))
            if task.project_id not in existing_projects:
                raise TrashError(model.E_UNKNOWN_PROJECT, task.project_id)
            restored_task = model.Task(**{
                **task.to_dict(), "dependencies": dependencies,
                "blocked_by": blocked_by, "updated_at": stamp})
            restored_tasks = [restored_task]
            all_tasks = list(portfolio.tasks) + restored_tasks
            incoming = entry.payload.get("incoming", [])
            if isinstance(incoming, list):
                for edge in incoming:
                    if not isinstance(edge, dict):
                        continue
                    target_id = edge.get("task_id")
                    for index, candidate in enumerate(all_tasks):
                        if candidate.task_id != target_id:
                            continue
                        all_tasks[index] = _reapply_edge(
                            candidate, restored_task.task_id, edge,
                            stamp)
            new_portfolio = replace(
                portfolio, tasks=tuple(all_tasks),
                updated_at=stamp).validate()
        else:
            raw_project = entry.payload.get("project")
            raw_tasks = entry.payload.get("tasks", [])
            if not isinstance(raw_project, dict) or not isinstance(raw_tasks,
                                                                 list):
                raise TrashError(model.E_MALFORMED, "project payload missing")
            project = model.Project.from_dict(raw_project)
            if project.project_id in existing_projects:
                raise TrashError(model.E_DUPLICATE_OBJECT, project.project_id)
            restored_tasks = [model.Task.from_dict(item)
                              for item in raw_tasks if isinstance(item, dict)]
            allowed = set(existing_tasks) | {t.task_id for t in restored_tasks}
            cleaned: list[model.Task] = []
            for task in restored_tasks:
                dependencies = tuple(d for d in task.dependencies
                                     if d in allowed)
                blocked_by = tuple(d for d in task.blocked_by
                                   if d in allowed)
                dropped.extend(sorted(set(task.dependencies)
                                      - set(dependencies)))
                dropped.extend(sorted(set(task.blocked_by)
                                      - set(blocked_by)))
                cleaned.append(model.Task(**{
                    **task.to_dict(), "dependencies": dependencies,
                    "blocked_by": blocked_by, "updated_at": stamp}))
            all_tasks = list(portfolio.tasks) + cleaned
            incoming = entry.payload.get("incoming", [])
            if isinstance(incoming, list):
                for edge in incoming:
                    if not isinstance(edge, dict):
                        continue
                    target_id = edge.get("task_id")
                    removed_id = edge.get("removed")
                    if not isinstance(removed_id, str):
                        continue
                    for index, candidate in enumerate(all_tasks):
                        if candidate.task_id != target_id:
                            continue
                        all_tasks[index] = _reapply_edge(
                            candidate, removed_id, edge, stamp)
            new_portfolio = replace(
                portfolio, projects=portfolio.projects + (project,),
                tasks=tuple(all_tasks),
                updated_at=stamp).validate()

        store.save_portfolio(root, new_portfolio)
        _remove_entry(root, trash_id)
        changes.append_change(
            root, kind=changes.KIND_RESTORE,
            object_kind=(changes.OBJECT_TASK if entry.kind == "task"
                         else changes.OBJECT_PROJECT),
            object_id=entry.object_id, title=entry.title,
            summary="restauré depuis la corbeille",
            after={"dropped_dependencies": dropped},
            source="human", undoable=False, references=trash_id, at=stamp)
        return {
            "restored": entry.object_id,
            "kind": entry.kind,
            "dropped_dependencies": sorted(set(dropped)),
        }


def purge_trash(root: str | Path, trash_id: str) -> dict[str, Any]:
    """Permanently remove one trash entry (explicit human action)."""
    with store.portfolio_lock(root):
        path = _entry_path(root, trash_id)
        if not path.is_file():
            raise TrashError(model.E_UNKNOWN_ID, trash_id)
        path.unlink()
    return {"purged": trash_id}


__all__ = [
    "MAX_TRASH_ENTRIES", "TRASH_DIR", "TRASH_SCHEMA_VERSION", "TrashEntry",
    "TrashError", "list_trash", "purge_trash", "restore_trash", "trash_dir",
    "trash_project", "trash_task",
]
