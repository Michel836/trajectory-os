"""MVP — validated, atomic mutations for the interactive dashboard.

Every mutation follows the same trust model as the rest of the MVP:

* the portfolio is loaded and validated before use (fail closed when absent);
* inputs are validated through the domain model (bad ids, dangling references,
  cycles, out-of-range values and unknown fields are rejected, never silently
  tolerated);
* a *new* portfolio is constructed and fully validated before it is written —
  the in-memory portfolio is never mutated in place;
* writes are atomic (``store.save_portfolio`` -> temp file + ``os.replace``);
* outcome records are only appended for real execution outcomes, and the
  append-only ledger is never rewritten.

Nothing here performs a Git write or mutates any store other than the chosen
data root.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import model, outcomes, store

#: Fields the dashboard may edit on a project (fail closed on anything else).
PROJECT_EDITABLE = frozenset({
    "name", "objective", "status", "domain", "urgency", "impact",
    "deadline", "description",
})

#: Fields the dashboard may edit on a task (fail closed on anything else).
TASK_EDITABLE = frozenset({
    "project_id", "title", "description", "workstream", "deliverable",
    "status", "estimated_minutes", "actual_minutes", "deadline", "urgency",
    "impact", "dependencies", "blocked_by", "waiting_for", "resources",
    "next_action",
})

#: Sequence-typed task fields (stored as tuples, editable as lists).
_SEQUENCE_FIELDS = frozenset({
    "dependencies", "blocked_by", "waiting_for", "resources",
})

_ID_RE = re.compile(r"[^a-z0-9._-]+")
_MAX_ID_LEN = 63


def _now() -> str:
    return intel_model.utc_now()


def _load(root: str | Path) -> model.Portfolio:
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    return portfolio


def _required_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise model.MvpError(model.E_MALFORMED, f"{key} is required")
    return value


def _value(payload: Mapping[str, Any], key: str,
           default: Any = None) -> Any:
    value = payload.get(key, default)
    return default if value is None else value


def _optional_blankable(value: Any) -> Any:
    """Treat an explicit empty string as ``None`` for optional fields."""
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _tuple_field(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value):
        raise model.MvpError(model.E_MALFORMED,
                             f"{key} must be a list of strings")
    return tuple(value)


def _slug(text: str, fallback: str) -> str:
    cleaned = _ID_RE.sub("-", text.lower()).strip(".-_") or fallback
    if len(cleaned) > _MAX_ID_LEN:
        cleaned = cleaned[:_MAX_ID_LEN].rstrip(".-_") or fallback
    if not cleaned or not cleaned[0].isalnum():
        cleaned = fallback
    return cleaned


def _unique_id(existing: set[str], base: str) -> str:
    candidate = base
    index = 2
    while candidate in existing:
        suffix = f"-{index}"
        candidate = base[:_MAX_ID_LEN - len(suffix)] + suffix
        index += 1
    return candidate


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str],
                    context: str) -> None:
    for key in payload:
        if key not in allowed:
            raise model.MvpError(
                model.E_MALFORMED, f"unknown field {key!r} for {context}")


def _updated_at(portfolio: model.Portfolio) -> str:
    return _now()


# --- project mutations --------------------------------------------------------


def create_project(root: str | Path,
                   payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create one project; ``project_id`` is optional (slug generated)."""
    portfolio = _load(root)
    _reject_unknown(payload, PROJECT_EDITABLE | {"project_id"}, "project")
    name = _required_str(payload, "name")
    objective = _required_str(payload, "objective")
    existing = {p.project_id for p in portfolio.projects}
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        project_id = _unique_id(existing, _slug(name, "project"))

    project = model.Project(
        project_id=project_id,
        name=name,
        objective=objective,
        status=str(_value(payload, "status", model.PS_ACTIVE)),
        domain=str(_value(payload, "domain", "personal")),
        urgency=str(_value(payload, "urgency", model.U_MEDIUM)),
        impact=str(_value(payload, "impact", model.I_MEDIUM)),
        deadline=_optional_blankable(_value(payload, "deadline")),
        description=str(_value(payload, "description", "")),
    )
    new_portfolio = replace(
        portfolio,
        projects=portfolio.projects + (project,),
        updated_at=_updated_at(portfolio),
    ).validate()
    store.save_portfolio(root, new_portfolio)
    return {"project_id": project_id, "name": name}


def update_project(root: str | Path, project_id: str,
                   payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a partial, validated update to one project."""
    portfolio = _load(root)
    _reject_unknown(payload, PROJECT_EDITABLE, "project")
    projects = portfolio.project_map()
    if project_id not in projects:
        raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
    current = projects[project_id]
    changes: dict[str, Any] = {}
    for key in PROJECT_EDITABLE:
        if key not in payload:
            continue
        value = payload[key]
        if key == "deadline":
            changes[key] = _optional_blankable(value)
        else:
            changes[key] = value
    updated = replace(current, **changes)
    new_projects = tuple(updated if p.project_id == project_id else p
                         for p in portfolio.projects)
    new_portfolio = replace(portfolio, projects=new_projects,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"project_id": project_id, "status": updated.status}


# --- task mutations -----------------------------------------------------------


def create_task(root: str | Path,
                payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create one task; ``task_id`` is optional (slug generated)."""
    portfolio = _load(root)
    _reject_unknown(payload, TASK_EDITABLE | {"task_id"}, "task")
    title = _required_str(payload, "title")
    project_id = _required_str(payload, "project_id")
    existing = {t.task_id for t in portfolio.tasks}
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        task_id = _unique_id(existing, _slug(title, "task"))

    stamp = _updated_at(portfolio)
    task = model.Task(
        task_id=task_id,
        project_id=project_id,
        title=title,
        description=str(_value(payload, "description", "")),
        workstream=str(_value(payload, "workstream", "")),
        deliverable=str(_value(payload, "deliverable", "")),
        status=str(_value(payload, "status", model.TS_TODO)),
        estimated_minutes=_optional_blankable(
            _value(payload, "estimated_minutes")),
        actual_minutes=_optional_blankable(_value(payload, "actual_minutes")),
        deadline=_optional_blankable(_value(payload, "deadline")),
        urgency=str(_value(payload, "urgency", model.U_MEDIUM)),
        impact=str(_value(payload, "impact", model.I_MEDIUM)),
        dependencies=_tuple_field(payload, "dependencies"),
        blocked_by=_tuple_field(payload, "blocked_by"),
        waiting_for=_tuple_field(payload, "waiting_for"),
        resources=_tuple_field(payload, "resources"),
        next_action=str(_value(payload, "next_action", "")),
        created_at=stamp,
        updated_at=stamp,
    )
    new_portfolio = replace(
        portfolio,
        tasks=portfolio.tasks + (task,),
        updated_at=stamp,
    ).validate()
    store.save_portfolio(root, new_portfolio)
    return {"task_id": task_id, "project_id": project_id}


def update_task(root: str | Path, task_id: str,
                payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a partial, validated update to one task.

    A direct ``status`` change is treated like a decision: the affected
    project's status is re-derived from the new task states (never invented),
    but no outcome is appended to the ledger — that is reserved for explicit
    outcome recording.
    """
    portfolio = _load(root)
    _reject_unknown(payload, TASK_EDITABLE, "task")
    tasks = portfolio.task_map()
    if task_id not in tasks:
        raise model.MvpError(model.E_UNKNOWN_ID, task_id)
    current = tasks[task_id]

    changes: dict[str, Any] = {}
    for key in TASK_EDITABLE:
        if key not in payload:
            continue
        value = payload[key]
        if key in _SEQUENCE_FIELDS:
            changes[key] = _tuple_field(payload, key)
        elif key in ("estimated_minutes", "actual_minutes", "deadline"):
            changes[key] = _optional_blankable(value)
        else:
            changes[key] = value

    updated = replace(current, **changes)
    new_tasks = tuple(updated if t.task_id == task_id else t
                      for t in portfolio.tasks)

    # Validate the candidate *before* deriving project status: derivation is a
    # read-only projection that assumes a valid portfolio, so malformed input
    # must fail closed here rather than crash inside readiness evaluation.
    candidate = replace(portfolio, tasks=new_tasks,
                        updated_at=_updated_at(portfolio)).validate()

    if "status" in changes or "project_id" in changes:
        affected = {str(updated.project_id), current.project_id}
        new_projects = _derive_projects(candidate, new_tasks, affected)
        candidate = replace(candidate, projects=new_projects,
                            updated_at=_updated_at(portfolio)).validate()

    store.save_portfolio(root, candidate)
    return {"task_id": task_id, "status": updated.status}


def _derive_projects(portfolio: model.Portfolio,
                     tasks: tuple[model.Task, ...],
                     project_ids: set[str]) -> tuple[model.Project, ...]:
    projects = list(portfolio.projects)
    for pid in sorted(project_ids):
        if pid not in portfolio.project_map():
            continue
        derived = outcomes.derive_project_status(portfolio, tasks, pid)
        projects = [derived if p.project_id == pid else p for p in projects]
    return tuple(projects)


# --- dependency mutations -----------------------------------------------------


def add_dependency(root: str | Path, task_id: str,
                   dependency: str) -> dict[str, Any]:
    """Add one dependency edge (validated; cycles fail closed)."""
    portfolio = _load(root)
    tasks = portfolio.task_map()
    if task_id not in tasks:
        raise model.MvpError(model.E_UNKNOWN_ID, task_id)
    if dependency not in tasks:
        raise model.MvpError(model.E_UNKNOWN_ID, dependency)
    if task_id == dependency:
        raise model.MvpError(model.E_SELF_REFERENCE, task_id)
    current = tasks[task_id]
    if dependency in current.dependencies:
        return {"task_id": task_id, "dependency": dependency,
                "added": False}
    if len(current.dependencies) >= model.MAX_DEPENDENCIES_PER_TASK:
        raise model.MvpError(model.E_OVERFLOW, "too many dependencies")
    updated = replace(current, dependencies=current.dependencies
                      + (dependency,))
    new_tasks = tuple(updated if t.task_id == task_id else t
                      for t in portfolio.tasks)
    new_portfolio = replace(portfolio, tasks=new_tasks,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"task_id": task_id, "dependency": dependency, "added": True}


def remove_dependency(root: str | Path, task_id: str,
                      dependency: str) -> dict[str, Any]:
    """Remove one dependency edge from a task's dependencies."""
    portfolio = _load(root)
    tasks = portfolio.task_map()
    if task_id not in tasks:
        raise model.MvpError(model.E_UNKNOWN_ID, task_id)
    current = tasks[task_id]
    if dependency not in current.dependencies:
        return {"task_id": task_id, "dependency": dependency,
                "removed": False}
    updated = replace(current, dependencies=tuple(
        dep for dep in current.dependencies if dep != dependency))
    new_tasks = tuple(updated if t.task_id == task_id else t
                      for t in portfolio.tasks)
    new_portfolio = replace(portfolio, tasks=new_tasks,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"task_id": task_id, "dependency": dependency, "removed": True}


__all__ = [
    "PROJECT_EDITABLE",
    "TASK_EDITABLE",
    "add_dependency",
    "create_project",
    "create_task",
    "remove_dependency",
    "update_project",
    "update_task",
]
