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

import functools
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, cast

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import (
    calibration,
    classification,
    importer,
    model,
    outcomes,
    store,
)
from trajectory_os.mvp import (
    changes as changes_module,
)

#: Serialize in-process portfolio mutations. The local cockpit server is
#: threaded, so two read-modify-write requests could otherwise lose an update.
#: Cross-process writers (CLI) remain a documented limitation.
#: Reentrant so a serialized mutation may call another (e.g. decide ->
#: update_decision) without deadlocking.
_MUTATION_LOCK = threading.RLock()


def _serialized[F: Callable[..., Any]](func: F) -> F:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        root = args[0] if args else kwargs.get("root")
        if root is None:
            raise model.MvpError(model.E_MALFORMED, "root required")
        with _MUTATION_LOCK, store.portfolio_lock(root):
            return func(*args, **kwargs)
    return cast(F, wrapper)

#: Fields the dashboard may edit on a project (fail closed on anything else).
PROJECT_EDITABLE = frozenset({
    "name", "objective", "status", "domain", "urgency", "impact",
    "deadline", "description", "aliases",
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


# --- duplicate protection (manual creation) -----------------------------------
#
# Manual creation must not silently create an object that already exists. The
# comparison is the exact normalized form produced by the importer (the single
# source of truth shared with Capture), so casing, accents and punctuation are
# neutralised. Fuzzy similarity is deliberately NOT used here: only an exact
# normalized title (same project for tasks) is a strong duplicate. The caller
# can still create an intentional duplicate with ``allow_duplicate=True``.


def _normalized(value: str) -> str:
    return importer.normalize_text(value)


def _duplicate_task(portfolio: model.Portfolio, title: str,
                    project_id: str) -> model.Task | None:
    needle = _normalized(title)
    for task in portfolio.tasks:
        if task.project_id == project_id \
                and _normalized(task.title) == needle:
            return task
    return None


def _duplicate_project(portfolio: model.Portfolio,
                       name: str) -> model.Project | None:
    needle = _normalized(name)
    for project in portfolio.projects:
        if _normalized(project.name) == needle:
            return project
    return None


def _duplicate_metadata(*, obj_id: str, kind: str, ref_id: str,
                        title: str, status: str, project_id: str,
                        project: str) -> dict[str, Any]:
    return {"duplicate": {
        "id": obj_id, "type": kind, "ref_id": ref_id, "title": title,
        "status": status, "project_id": project_id, "project": project}}


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str],
                    context: str) -> None:
    for key in payload:
        if key not in allowed:
            raise model.MvpError(
                model.E_MALFORMED, f"unknown field {key!r} for {context}")


def _updated_at(portfolio: model.Portfolio) -> str:
    return _now()


def _journal(root: str | Path, *, kind: str, object_kind: str,
             object_id: str, title: str, summary: str,
             before: Mapping[str, Any] | None = None,
             after: Mapping[str, Any] | None = None,
             source: str = "human", undoable: bool = True,
             at: str = "") -> None:
    """Best-effort change journaling after a canonical write.

    A journal failure must never turn a successful, already-persisted human
    action into an error. The portfolio remains authoritative.
    """
    try:
        changes_module.append_change(
            root, kind=kind, object_kind=object_kind, object_id=object_id,
            title=title, summary=summary, before=before, after=after,
            source=source, undoable=undoable, at=at)
    except (OSError, changes_module.ChangeError):
        return


# --- project mutations --------------------------------------------------------


@_serialized
def create_project(root: str | Path,
                   payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create one project; ``project_id`` is optional (slug generated)."""
    portfolio = _load(root)
    _reject_unknown(payload,
                    PROJECT_EDITABLE | {"project_id", "allow_duplicate"},
                    "project")
    name = _required_str(payload, "name")
    objective = _required_str(payload, "objective")
    if payload.get("allow_duplicate") is not True:
        duplicate = _duplicate_project(portfolio, name)
        if duplicate is not None:
            raise model.MvpError(
                model.E_DUPLICATE_OBJECT,
                "A project with the same normalized name already exists.",
                metadata=_duplicate_metadata(
                    obj_id="project:" + duplicate.project_id, kind="project",
                    ref_id=duplicate.project_id, title=duplicate.name,
                    status=duplicate.status,
                    project_id=duplicate.project_id, project=duplicate.name))
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
        aliases=_aliases_payload(payload.get("aliases")),
    )
    new_portfolio = replace(
        portfolio,
        projects=portfolio.projects + (project,),
        updated_at=_updated_at(portfolio),
    ).validate()
    store.save_portfolio(root, new_portfolio)
    _journal(root, kind=changes_module.KIND_CREATE,
             object_kind=changes_module.OBJECT_PROJECT,
             object_id=project_id, title=name, summary="projet créé",
             after={"name": name, "status": project.status},
             at=new_portfolio.updated_at)
    return {"project_id": project_id, "name": name}


def _aliases_payload(value: Any) -> tuple[str, ...]:
    """Normalize an alias payload into the validated tuple form.

    Accepts a list of strings, or a single comma/semicolon separated string
    (what a human types in one inline field). Validation of length, count and
    duplicates stays in ``model.Project.validate`` so there is one authority.
    """
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        parts = [part.strip() for part in re.split(r"[,;\n]", value)]
        return tuple(part for part in parts if part)
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value
                     if str(item).strip())
    raise model.MvpError(model.E_MALFORMED, "aliases must be a list or string")


@_serialized
def set_aliases(root: str | Path, project_id: str,
                payload: Mapping[str, Any]) -> dict[str, Any]:
    """Replace one project's declared search aliases (explicit, journaled).

    Aliases are how a word that exists only in the user's head (``zephyr``)
    becomes an exact recall match for a dossier. They are declared by the
    human, never inferred, so setting them is a first-class journaled
    mutation rather than a side effect of free-text editing.
    """
    portfolio = _load(root)
    _reject_unknown(payload, frozenset({"aliases"}), "aliases")
    projects = portfolio.project_map()
    if project_id not in projects:
        raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
    current = projects[project_id]
    aliases = _aliases_payload(payload.get("aliases"))
    if tuple(model.fold_text(alias) for alias in aliases) == tuple(
            model.fold_text(alias) for alias in current.aliases):
        return {"project_id": project_id, "aliases": list(current.aliases),
                "changed": False}
    updated = replace(current, aliases=aliases)
    new_projects = tuple(updated if p.project_id == project_id else p
                         for p in portfolio.projects)
    new_portfolio = replace(portfolio, projects=new_projects,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    _journal(root, kind=changes_module.KIND_UPDATE,
             object_kind=changes_module.OBJECT_PROJECT,
             object_id=project_id, title=updated.name,
             summary="alias de recherche mis à jour",
             before={"aliases": list(current.aliases)},
             after={"aliases": list(updated.aliases)},
             at=new_portfolio.updated_at)
    return {"project_id": project_id, "aliases": list(updated.aliases),
            "changed": True}


@_serialized
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
        elif key == "aliases":
            changes[key] = _aliases_payload(value)
        else:
            changes[key] = value
    updated = replace(current, **changes)
    new_projects = tuple(updated if p.project_id == project_id else p
                         for p in portfolio.projects)
    new_portfolio = replace(portfolio, projects=new_projects,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    _journal(root, kind=changes_module.KIND_UPDATE,
             object_kind=changes_module.OBJECT_PROJECT,
             object_id=project_id, title=updated.name,
             summary="projet mis à jour",
             before={key: getattr(current, key) for key in changes},
             after={key: getattr(updated, key) for key in changes})
    return {"project_id": project_id, "status": updated.status}


# --- project / task conversion & parking --------------------------------------
#
# The taxonomy (``classification``) says when something *should* be a project
# or a task; these operations let the human *apply* that decision without
# losing data. Every operation is validated, atomic and reversible.


@_serialized
def park_project(root: str | Path, project_id: str,
                 payload: Mapping[str, Any]) -> dict[str, Any]:
    """Park a project in the Inbox, or bring it back out.

    ``payload["kind"]`` selects the parked status (``IDEA`` by default, or
    ``SOMEDAY``); an explicit ``status`` (any valid project status, e.g.
    ``ACTIVE``) overrides it, which is how the project leaves the Inbox. A
    parked project stays visible but is excluded from readiness, priority and
    scheduling.
    """
    portfolio = _load(root)
    _reject_unknown(payload, frozenset({"kind", "status"}), "park")
    projects = portfolio.project_map()
    if project_id not in projects:
        raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
    kind = _value(payload, "kind", classification.IDEA)
    parked_status = {
        classification.IDEA: model.PS_IDEA,
        classification.SOMEDAY: model.PS_SOMEDAY,
    }
    if kind not in parked_status:
        raise model.MvpError(model.E_MALFORMED,
                             "kind must be IDEA or SOMEDAY")
    status = str(_value(payload, "status", parked_status[kind]))
    updated = replace(projects[project_id], status=status)
    new_projects = tuple(updated if p.project_id == project_id else p
                         for p in portfolio.projects)
    new_portfolio = replace(portfolio, projects=new_projects,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    _journal(root, kind=changes_module.KIND_UPDATE,
             object_kind=changes_module.OBJECT_PROJECT,
             object_id=project_id, title=updated.name,
             summary="projet parqué" if updated.parked else "projet activé",
             before={"status": projects[project_id].status},
             after={"status": updated.status})
    return {"project_id": project_id, "status": updated.status,
            "parked": updated.parked}


@_serialized
def promote_task(root: str | Path, task_id: str,
                 payload: Mapping[str, Any]) -> dict[str, Any]:
    """Promote a task into its own project and move the task into it.

    Non-destructive and reversible: the task keeps every field and simply
    gains a new ``project_id``; ``demote_project`` moves it back and removes
    the wrapper project. Optional ``name`` / ``objective`` / ``domain``
    override the values derived from the task.
    """
    portfolio = _load(root)
    _reject_unknown(payload, frozenset({"name", "objective", "domain"}),
                    "promote")
    tasks = portfolio.task_map()
    if task_id not in tasks:
        raise model.MvpError(model.E_UNKNOWN_ID, task_id)
    task = tasks[task_id]
    name = _value(payload, "name", task.title)
    if not isinstance(name, str) or not name.strip():
        raise model.MvpError(model.E_MALFORMED, "name must be a string")
    name = name.strip()
    # No allow_duplicate bypass: a retried promotion fails closed instead of
    # creating a second, empty project with the same normalized name.
    duplicate = _duplicate_project(portfolio, name)
    if duplicate is not None:
        raise model.MvpError(
            model.E_DUPLICATE_OBJECT,
            "A project with the same normalized name already exists.",
            metadata=_duplicate_metadata(
                obj_id="project:" + duplicate.project_id, kind="project",
                ref_id=duplicate.project_id, title=duplicate.name,
                status=duplicate.status,
                project_id=duplicate.project_id, project=duplicate.name))
    parent = portfolio.project_map()[task.project_id]
    objective = _value(payload, "objective", task.description or task.title)
    if not isinstance(objective, str) or not objective.strip():
        objective = task.title
    domain = _value(payload, "domain", parent.domain)
    existing = {p.project_id for p in portfolio.projects}
    project_id = _unique_id(existing, _slug(name, "project"))
    stamp = _updated_at(portfolio)
    project = model.Project(
        project_id=project_id, name=name, objective=objective.strip(),
        status=model.PS_ACTIVE, domain=str(domain))
    moved = replace(task, project_id=project_id, updated_at=stamp)
    new_tasks = tuple(moved if t.task_id == task_id else t
                      for t in portfolio.tasks)
    new_portfolio = replace(
        portfolio, projects=portfolio.projects + (project,), tasks=new_tasks,
        updated_at=stamp).validate()
    store.save_portfolio(root, new_portfolio)
    _journal(root, kind=changes_module.KIND_CREATE,
             object_kind=changes_module.OBJECT_PROJECT,
             object_id=project_id, title=name,
             summary="projet créé par promotion",
             after={"name": name, "status": project.status})
    _journal(root, kind=changes_module.KIND_UPDATE,
             object_kind=changes_module.OBJECT_TASK,
             object_id=task_id, title=task.title,
             summary="tâche promue en projet",
             before={"project_id": task.project_id},
             after={"project_id": project_id})
    return {"task_id": task_id, "project_id": project_id, "name": name,
            "previous_project_id": task.project_id}


@_serialized
def demote_project(root: str | Path, project_id: str,
                   payload: Mapping[str, Any]) -> dict[str, Any]:
    """Move a project's tasks to another project and remove the empty one.

    The reverse of :func:`promote_task`: a project that turned out to be a
    single task (or a thin grouping) is folded back into an existing project.
    Fails closed if the target is missing or is the project itself; the
    project is only removed once none of its tasks reference it.
    """
    portfolio = _load(root)
    _reject_unknown(payload,
                    frozenset({"target_project_id", "confirm_discard"}),
                    "demote")
    projects = portfolio.project_map()
    if project_id not in projects:
        raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
    target = _required_str(payload, "target_project_id")
    if target == project_id:
        raise model.MvpError(
            model.E_MALFORMED,
            "target_project_id must differ from project_id")
    if target not in projects:
        raise model.MvpError(model.E_UNKNOWN_PROJECT, target)
    current = projects[project_id]
    has_metadata = (bool(current.description.strip())
                    or current.deadline is not None
                    or current.objective.strip() not in ("", current.name))
    if has_metadata and payload.get("confirm_discard") is not True:
        raise model.MvpError(
            model.E_MALFORMED,
            "project has metadata (objective/description/deadline); pass "
            "confirm_discard=true to drop it on demotion")
    stamp = _updated_at(portfolio)
    moved = 0
    new_tasks: list[model.Task] = []
    for task in portfolio.tasks:
        if task.project_id == project_id:
            new_tasks.append(replace(task, project_id=target,
                                     updated_at=stamp))
            moved += 1
        else:
            new_tasks.append(task)
    new_projects = tuple(p for p in portfolio.projects
                         if p.project_id != project_id)
    new_portfolio = replace(
        portfolio, projects=new_projects, tasks=tuple(new_tasks),
        updated_at=stamp).validate()
    store.save_portfolio(root, new_portfolio)
    _journal(root, kind=changes_module.KIND_UPDATE,
             object_kind=changes_module.OBJECT_PROJECT,
             object_id=project_id, title=current.name,
             summary=f"projet fusionné dans {target}",
             before={"status": current.status},
             after={"status": "REMOVED", "target_project_id": target})
    return {"project_id": project_id, "target_project_id": target,
            "moved_tasks": moved}


# --- calendar & days off ------------------------------------------------------

EVENT_EDITABLE = frozenset({
    "title", "day", "start_minutes", "end_minutes", "all_day",
    "recurrence",
})


def _event_from(payload: Mapping[str, Any],
                event_id: str) -> model.CalendarEvent:
    return model.CalendarEvent(
        event_id=event_id,
        title=_required_str(payload, "title"),
        day=_required_str(payload, "day"),
        start_minutes=int(payload.get("start_minutes", 0)),
        end_minutes=int(payload.get("end_minutes", 0)),
        all_day=bool(payload.get("all_day", False)),
        recurrence=str(_value(payload, "recurrence", "")),
    ).validate()


def create_event(root: str | Path,
                 payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create one one-off or weekly recurring calendar event."""
    portfolio = _load(root)
    _reject_unknown(payload, EVENT_EDITABLE | frozenset({"event_id"}),
                    "event")
    title = _required_str(payload, "title")
    day = _required_str(payload, "day")
    existing = {e.event_id for e in portfolio.calendar}
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        event_id = _unique_id(existing, _slug(f"{day}-{title}", "event"))
    event = _event_from(payload, event_id)
    new_portfolio = replace(
        portfolio, calendar=portfolio.calendar + (event,),
        updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"event_id": event_id, "day": day,
            "recurrence": event.recurrence}


def update_event(root: str | Path, event_id: str,
                 payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a partial, validated update to one calendar event."""
    portfolio = _load(root)
    _reject_unknown(payload, EVENT_EDITABLE, "event")
    events = {e.event_id: e for e in portfolio.calendar}
    if event_id not in events:
        raise model.MvpError(model.E_UNKNOWN_ID, event_id)
    current = events[event_id]
    merged = {
        "title": current.title, "day": current.day,
        "start_minutes": current.start_minutes,
        "end_minutes": current.end_minutes, "all_day": current.all_day,
        "recurrence": current.recurrence,
    }
    merged.update({key: payload[key] for key in EVENT_EDITABLE
                   if key in payload})
    updated = _event_from(merged, event_id)
    new_calendar = tuple(updated if e.event_id == event_id else e
                         for e in portfolio.calendar)
    new_portfolio = replace(portfolio, calendar=new_calendar,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"event_id": event_id, "day": updated.day}


def delete_event(root: str | Path, event_id: str) -> dict[str, Any]:
    """Remove one calendar event (fail closed if unknown)."""
    portfolio = _load(root)
    if event_id not in {e.event_id for e in portfolio.calendar}:
        raise model.MvpError(model.E_UNKNOWN_ID, event_id)
    new_calendar = tuple(e for e in portfolio.calendar
                         if e.event_id != event_id)
    new_portfolio = replace(portfolio, calendar=new_calendar,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"event_id": event_id, "deleted": True}


def set_day_off(root: str | Path, day: str, *, off: bool) -> dict[str, Any]:
    """Add or remove an explicit day off (zero plan capacity on that day)."""
    portfolio = _load(root)
    model._require_deadline(day, "day")
    current = set(portfolio.days_off)
    if off:
        current.add(day)
    else:
        current.discard(day)
    new_portfolio = replace(
        portfolio, days_off=tuple(sorted(current)),
        updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"day": day, "off": off, "days_off": list(new_portfolio.days_off)}


# --- routines -----------------------------------------------------------------

ROUTINE_EDITABLE = frozenset({
    "title", "project_id", "weekdays", "estimated_minutes", "workstream",
    "active",
})


def _weekdays_field(payload: Mapping[str, Any],
                    default: tuple[int, ...]) -> tuple[int, ...]:
    if "weekdays" not in payload:
        return default
    value = payload["weekdays"]
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(day, int) and not isinstance(day, bool)
            for day in value):
        raise model.MvpError(model.E_MALFORMED,
                             "weekdays must be a list of integers")
    return tuple(value)


@_serialized
def create_routine(root: str | Path,
                   payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create one recurring routine template."""
    portfolio = _load(root)
    _reject_unknown(payload, ROUTINE_EDITABLE | frozenset({"routine_id"}),
                    "routine")
    title = _required_str(payload, "title")
    project_id = _required_str(payload, "project_id")
    if project_id not in portfolio.project_map():
        raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
    existing = {r.routine_id for r in portfolio.routines}
    routine_id = payload.get("routine_id")
    if not isinstance(routine_id, str) or not routine_id.strip():
        routine_id = _unique_id(existing, _slug(title, "routine"))
    routine = model.Routine(
        routine_id=routine_id, title=title, project_id=project_id,
        weekdays=_weekdays_field(payload, (0,)),
        estimated_minutes=_optional_blankable(
            _value(payload, "estimated_minutes")),
        workstream=str(_value(payload, "workstream", "")),
        active=bool(_value(payload, "active", True)),
    ).validate()
    new_portfolio = replace(
        portfolio, routines=portfolio.routines + (routine,),
        updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"routine_id": routine_id, "project_id": project_id}


@_serialized
def update_routine(root: str | Path, routine_id: str,
                   payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a partial, validated update to one routine."""
    portfolio = _load(root)
    _reject_unknown(payload, ROUTINE_EDITABLE, "routine")
    routines = {r.routine_id: r for r in portfolio.routines}
    if routine_id not in routines:
        raise model.MvpError(model.E_UNKNOWN_ID, routine_id)
    current = routines[routine_id]
    merged: dict[str, Any] = {
        "title": current.title, "project_id": current.project_id,
        "weekdays": list(current.weekdays),
        "estimated_minutes": current.estimated_minutes,
        "workstream": current.workstream, "active": current.active,
    }
    merged.update({key: payload[key] for key in ROUTINE_EDITABLE
                   if key in payload})
    if merged["project_id"] not in portfolio.project_map():
        raise model.MvpError(model.E_UNKNOWN_PROJECT,
                             str(merged["project_id"]))
    updated = model.Routine(
        routine_id=routine_id, title=str(merged["title"]),
        project_id=str(merged["project_id"]),
        weekdays=tuple(merged["weekdays"]),
        estimated_minutes=merged["estimated_minutes"],
        workstream=str(merged["workstream"]),
        active=bool(merged["active"]),
    ).validate()
    new_routines = tuple(updated if r.routine_id == routine_id else r
                         for r in portfolio.routines)
    new_portfolio = replace(portfolio, routines=new_routines,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"routine_id": routine_id, "active": updated.active}


@_serialized
def delete_routine(root: str | Path, routine_id: str) -> dict[str, Any]:
    portfolio = _load(root)
    if routine_id not in {r.routine_id for r in portfolio.routines}:
        raise model.MvpError(model.E_UNKNOWN_ID, routine_id)
    new_routines = tuple(r for r in portfolio.routines
                         if r.routine_id != routine_id)
    new_portfolio = replace(portfolio, routines=new_routines,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"routine_id": routine_id, "deleted": True}


@_serialized
def materialize_routines(root: str | Path, day: str) -> dict[str, Any]:
    """Create the tasks due from active routines on ``day`` (idempotent).

    The created task id is deterministic (``<routine_id>-<day>``), so running
    this again for the same day creates nothing.
    """
    portfolio = _load(root)
    try:
        target = date.fromisoformat(day)
    except ValueError as exc:
        raise model.MvpError(model.E_MALFORMED,
                             "day must be YYYY-MM-DD") from exc
    projects = portfolio.project_map()
    existing_ids = {t.task_id for t in portfolio.tasks}
    stamp = _updated_at(portfolio)
    created: list[model.Task] = []
    for routine in portfolio.routines:
        if routine.project_id not in projects or not routine.due_on(target):
            continue
        task_id = _slug(f"{routine.routine_id}-{day}", "routine")
        if task_id in existing_ids:
            continue
        existing_ids.add(task_id)
        created.append(model.Task(
            task_id=task_id, project_id=routine.project_id,
            title=routine.title, workstream=routine.workstream,
            estimated_minutes=routine.estimated_minutes,
            status=model.TS_TODO, created_at=stamp, updated_at=stamp))
    if created:
        new_portfolio = replace(
            portfolio, tasks=portfolio.tasks + tuple(created),
            updated_at=stamp).validate()
        store.save_portfolio(root, new_portfolio)
    return {"day": day, "created_tasks": len(created),
            "task_ids": [t.task_id for t in created]}


# --- decisions ----------------------------------------------------------------

DECISION_EDITABLE = frozenset({
    "title", "question", "status", "choice", "rationale", "options",
    "project_id", "deadline",
})


def _decision_from(decision_id: str, payload: Mapping[str, Any],
                   portfolio: model.Portfolio, *,
                   decided_at: str) -> model.Decision:
    project_id = str(_value(payload, "project_id", ""))
    if project_id and project_id not in portfolio.project_map():
        raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
    return model.Decision(
        decision_id=decision_id,
        title=_required_str(payload, "title"),
        question=str(_value(payload, "question", "")),
        status=str(_value(payload, "status", model.DS_OPEN)),
        choice=str(_value(payload, "choice", "")),
        rationale=str(_value(payload, "rationale", "")),
        options=_tuple_field(payload, "options"),
        project_id=project_id,
        deadline=_optional_blankable(_value(payload, "deadline")),
        decided_at=decided_at,
    ).validate()


@_serialized
def create_decision(root: str | Path,
                    payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create one human-authored decision record."""
    portfolio = _load(root)
    _reject_unknown(payload,
                    DECISION_EDITABLE | frozenset({"decision_id"}), "decision")
    existing = {d.decision_id for d in portfolio.decisions}
    decision_id = payload.get("decision_id")
    title = _required_str(payload, "title")
    if not isinstance(decision_id, str) or not decision_id.strip():
        decision_id = _unique_id(existing, _slug(title, "decision"))
    stamp = _updated_at(portfolio)
    decided_at = (stamp if str(_value(payload, "status", model.DS_OPEN))
                  in model.DECISION_TERMINAL_STATUSES else "")
    decision = _decision_from(decision_id, payload, portfolio,
                              decided_at=decided_at)
    new_portfolio = replace(
        portfolio, decisions=portfolio.decisions + (decision,),
        updated_at=stamp).validate()
    store.save_portfolio(root, new_portfolio)
    return {"decision_id": decision_id, "status": decision.status}


@_serialized
def update_decision(root: str | Path, decision_id: str,
                    payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a partial, validated update to one decision."""
    portfolio = _load(root)
    _reject_unknown(payload, DECISION_EDITABLE, "decision")
    decisions = {d.decision_id: d for d in portfolio.decisions}
    if decision_id not in decisions:
        raise model.MvpError(model.E_UNKNOWN_ID, decision_id)
    current = decisions[decision_id]
    merged: dict[str, Any] = {
        "title": current.title, "question": current.question,
        "status": current.status, "choice": current.choice,
        "rationale": current.rationale, "options": list(current.options),
        "project_id": current.project_id, "deadline": current.deadline,
    }
    merged.update({key: payload[key] for key in DECISION_EDITABLE
                   if key in payload})
    settled = str(merged["status"]) in model.DECISION_TERMINAL_STATUSES
    decided_at = current.decided_at
    if settled and not decided_at:
        decided_at = _now()
    elif not settled:
        decided_at = ""
    updated = _decision_from(decision_id, merged, portfolio,
                             decided_at=decided_at)
    new_decisions = tuple(updated if d.decision_id == decision_id else d
                          for d in portfolio.decisions)
    new_portfolio = replace(portfolio, decisions=new_decisions,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"decision_id": decision_id, "status": updated.status}


@_serialized
def decide(root: str | Path, decision_id: str,
           payload: Mapping[str, Any]) -> dict[str, Any]:
    """Record the human's choice for a decision."""
    _reject_unknown(payload, frozenset({"choice", "rationale", "options"}),
                    "decide")
    choice = _required_str(payload, "choice")
    update: dict[str, Any] = {"status": model.DS_DECIDED, "choice": choice}
    if "rationale" in payload:
        update["rationale"] = payload["rationale"]
    if "options" in payload:
        update["options"] = payload["options"]
    return update_decision(root, decision_id, update)


@_serialized
def delete_decision(root: str | Path, decision_id: str) -> dict[str, Any]:
    portfolio = _load(root)
    if decision_id not in {d.decision_id for d in portfolio.decisions}:
        raise model.MvpError(model.E_UNKNOWN_ID, decision_id)
    new_decisions = tuple(d for d in portfolio.decisions
                          if d.decision_id != decision_id)
    new_portfolio = replace(portfolio, decisions=new_decisions,
                            updated_at=_updated_at(portfolio)).validate()
    store.save_portfolio(root, new_portfolio)
    return {"decision_id": decision_id, "deleted": True}


# --- task mutations -----------------------------------------------------------


@_serialized
def create_task(root: str | Path,
                payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create one task; ``task_id`` is optional (slug generated)."""
    portfolio = _load(root)
    _reject_unknown(payload,
                    TASK_EDITABLE | {"task_id", "allow_duplicate"}, "task")
    title = _required_str(payload, "title")
    project_id = _required_str(payload, "project_id")
    if payload.get("allow_duplicate") is not True:
        duplicate = _duplicate_task(portfolio, title, project_id)
        if duplicate is not None:
            project = portfolio.project_map().get(project_id)
            raise model.MvpError(
                model.E_DUPLICATE_OBJECT,
                "A task with the same normalized title already exists in "
                "this project.",
                metadata=_duplicate_metadata(
                    obj_id="task:" + duplicate.task_id, kind="task",
                    ref_id=duplicate.task_id, title=duplicate.title,
                    status=duplicate.status,
                    project_id=duplicate.project_id,
                    project=project.name if project is not None else ""))
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
    _journal(root, kind=changes_module.KIND_CREATE,
             object_kind=changes_module.OBJECT_TASK, object_id=task_id,
             title=title, summary="tâche créée",
             after={"title": title, "project_id": project_id,
                    "status": task.status}, at=stamp)
    return {"task_id": task_id, "project_id": project_id}


@_serialized
def apply_calibrated_estimate(root: str | Path,
                              task_id: str) -> dict[str, Any]:
    """Replace one task's estimate with its calibrated value.

    Human-triggered only. Fails closed when the task has no estimate or when
    no scope (project / domain / global) has enough real outcomes.
    """
    portfolio = _load(root)
    tasks = portfolio.task_map()
    if task_id not in tasks:
        raise model.MvpError(model.E_UNKNOWN_ID, task_id)
    task = tasks[task_id]
    if not task.estimated_minutes:
        raise model.MvpError(model.E_MALFORMED,
                             "task has no estimate to calibrate")
    result = calibration.calibration_for_task(
        portfolio, store.read_outcomes(root), task_id)
    if result is None or not result.sufficient:
        raise model.MvpError(
            model.E_MALFORMED,
            "insufficient outcome data to calibrate this task")
    adjusted = result.adjusted(task.estimated_minutes)
    assert adjusted is not None
    stamp = _updated_at(portfolio)
    updated = replace(task, estimated_minutes=adjusted, updated_at=stamp)
    new_tasks = tuple(updated if t.task_id == task_id else t
                      for t in portfolio.tasks)
    new_portfolio = replace(portfolio, tasks=new_tasks,
                            updated_at=stamp).validate()
    store.save_portfolio(root, new_portfolio)
    _journal(root, kind=changes_module.KIND_UPDATE,
             object_kind=changes_module.OBJECT_TASK, object_id=task_id,
             title=task.title, summary="estimation calibrée",
             before={"estimated_minutes": task.estimated_minutes},
             after={"estimated_minutes": adjusted}, at=stamp)
    return {
        "task_id": task_id,
        "previous_minutes": task.estimated_minutes,
        "estimated_minutes": adjusted,
        "factor": result.factor,
        "samples": result.samples,
        "scope": result.scope,
    }


@_serialized
def add_note(root: str | Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Attach one free note to a dossier (journaled, idempotent by content).

    This is the write behind "paste a message, validate, it lands in the
    dossier". The text is stored verbatim (P04); the note never becomes a task
    and never becomes a project. Re-pasting the same text into the same dossier
    returns the existing note instead of creating a duplicate.
    """
    portfolio = _load(root)
    _reject_unknown(payload, frozenset({
        "project_id", "text", "kind", "source_ref", "sensitivity",
        "content_hash", "allow_duplicate"}), "note")
    project_id = _required_str(payload, "project_id")
    if project_id not in portfolio.project_map():
        raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
    text = _required_str(payload, "text")
    if len(text) > model.MAX_NOTE_TEXT:
        raise model.MvpError(
            model.E_MALFORMED,
            f"text exceeds {model.MAX_NOTE_TEXT} chars")
    kind = str(_value(payload, "kind", model.NK_NOTE))
    source_ref = str(_value(payload, "source_ref", ""))
    sensitivity = str(_value(payload, "sensitivity", "INTERNAL"))
    content_hash = str(_value(payload, "content_hash", ""))
    if content_hash and payload.get("allow_duplicate") is not True:
        for existing in portfolio.notes:
            if (existing.project_id == project_id
                    and existing.content_hash == content_hash):
                return {"note_id": existing.note_id, "duplicate": True,
                        "project_id": project_id}
    existing_ids = {note.note_id for note in portfolio.notes}
    note_id = _unique_id(existing_ids, "note-" + project_id[:24])
    stamp = _updated_at(portfolio)
    note = model.Note(
        note_id=note_id, project_id=project_id, text=text, kind=kind,
        captured_at=stamp, source_ref=source_ref,
        content_hash=content_hash, sensitivity=sensitivity).validate()
    new_portfolio = replace(
        portfolio, notes=portfolio.notes + (note,), updated_at=stamp
    ).validate()
    store.save_portfolio(root, new_portfolio)
    _journal(root, kind=changes_module.KIND_CAPTURE,
             object_kind=changes_module.OBJECT_NOTE, object_id=note_id,
             title=note.title, summary=f"note {kind.lower()} ajoutée",
             after={"project_id": project_id, "kind": kind,
                    "source_ref": source_ref},
             at=stamp)
    return {"note_id": note_id, "project_id": project_id,
            "duplicate": False, "kind": kind, "captured_at": stamp}


@_serialized
def update_note(root: str | Path, note_id: str,
                payload: Mapping[str, Any]) -> dict[str, Any]:
    """Edit a note's text or kind (the raw capture is never rewritten silently)."""
    portfolio = _load(root)
    _reject_unknown(payload, frozenset({"text", "kind"}), "note")
    notes = portfolio.note_map()
    if note_id not in notes:
        raise model.MvpError(model.E_UNKNOWN_ID, note_id)
    current = notes[note_id]
    changes: dict[str, Any] = {}
    for key in ("text", "kind"):
        if key in payload:
            changes[key] = payload[key]
    updated = replace(current, **changes)
    stamp = _updated_at(portfolio)
    new_notes = tuple(updated if note.note_id == note_id else note
                      for note in portfolio.notes)
    new_portfolio = replace(portfolio, notes=new_notes,
                            updated_at=stamp).validate()
    store.save_portfolio(root, new_portfolio)
    _journal(root, kind=changes_module.KIND_UPDATE,
             object_kind=changes_module.OBJECT_NOTE, object_id=note_id,
             title=updated.title, summary="note mise à jour",
             before={key: getattr(current, key) for key in changes},
             after={key: getattr(updated, key) for key in changes}, at=stamp)
    return {"note_id": note_id, "kind": updated.kind}


@_serialized
def delete_note(root: str | Path, note_id: str) -> dict[str, Any]:
    """Remove a note (journaled; the reversal is an explicit re-add)."""
    portfolio = _load(root)
    notes = portfolio.note_map()
    if note_id not in notes:
        raise model.MvpError(model.E_UNKNOWN_ID, note_id)
    current = notes[note_id]
    stamp = _updated_at(portfolio)
    new_portfolio = replace(
        portfolio,
        notes=tuple(note for note in portfolio.notes
                    if note.note_id != note_id),
        updated_at=stamp,
    ).validate()
    store.save_portfolio(root, new_portfolio)
    _journal(root, kind=changes_module.KIND_DELETE,
             object_kind=changes_module.OBJECT_NOTE, object_id=note_id,
             title=current.title, summary="note supprimée",
             before={"project_id": current.project_id,
                     "kind": current.kind}, at=stamp)
    return {"note_id": note_id, "deleted": True}


@_serialized
@_serialized
def rename_domain(root: str | Path, *, old: str, new: str) -> dict[str, Any]:
    """Rename one domain across every project (explicit human decision)."""
    if not isinstance(old, str) or not old.strip():
        raise model.MvpError(model.E_MALFORMED, "old domain required")
    if not isinstance(new, str) or not new.strip():
        raise model.MvpError(model.E_MALFORMED, "new domain required")
    old = old.strip()
    new = new.strip()
    if len(new) > model.MAX_STR_LEN:
        raise model.MvpError(model.E_MALFORMED, "new domain is too long")
    portfolio = _load(root)
    changed = [p for p in portfolio.projects if p.domain == old]
    if not changed:
        return {"updated": 0, "from": old, "to": new}
    stamp = _updated_at(portfolio)
    projects = tuple(replace(project, domain=new)
                     if project.domain == old else project
                     for project in portfolio.projects)
    candidate = replace(portfolio, projects=projects,
                        updated_at=stamp).validate()
    store.backup_portfolio(root, reason="bulk-domain-rename")
    store.save_portfolio(root, candidate)
    for project in changed:
        _journal(root, kind=changes_module.KIND_UPDATE,
                 object_kind=changes_module.OBJECT_PROJECT,
                 object_id=project.project_id, title=project.name,
                 summary="domaine renommé",
                 before={"domain": old}, after={"domain": new}, at=stamp)
    return {"updated": len(changed), "from": old, "to": new}


@_serialized
def estimate_tasks(root: str | Path,
                   updates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply a bounded batch of effort estimates in one atomic write.

    ``updates`` is a sequence of ``{"task_id": ..., "estimated_minutes": N}``
    where ``estimated_minutes`` is ``None`` (clear) or a positive integer.
    Duplicate ids and unknown tasks fail closed before anything is written.
    """
    if isinstance(updates, (str, bytes)) or not isinstance(updates, Sequence):
        raise model.MvpError(model.E_MALFORMED, "updates must be a list")
    if len(updates) > 500:
        raise model.MvpError(model.E_OVERFLOW, "too many estimates")
    portfolio = _load(root)
    tasks = portfolio.task_map()
    stamp = _updated_at(portfolio)
    seen: set[str] = set()
    indexed = {task.task_id: index for index, task in enumerate(
        portfolio.tasks)}
    updated_tasks = list(portfolio.tasks)
    changes: list[tuple[model.Task, model.Task]] = []
    for raw in updates:
        if not isinstance(raw, Mapping):
            raise model.MvpError(model.E_MALFORMED, "update must be an object")
        task_id = raw.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise model.MvpError(model.E_MALFORMED, "task_id required")
        if task_id in seen:
            raise model.MvpError(model.E_MALFORMED,
                                 f"duplicate estimate for {task_id!r}")
        seen.add(task_id)
        if task_id not in tasks:
            raise model.MvpError(model.E_UNKNOWN_ID, task_id)
        minutes = raw.get("estimated_minutes")
        if minutes is not None:
            if isinstance(minutes, bool) or not isinstance(minutes, int):
                raise model.MvpError(model.E_MALFORMED,
                                     "estimated_minutes must be an integer")
            if not 1 <= minutes <= 1440:
                raise model.MvpError(model.E_MALFORMED,
                                     "estimated_minutes out of bounds")
        current = tasks[task_id]
        if current.estimated_minutes == minutes:
            continue
        updated = replace(current, estimated_minutes=minutes,
                          updated_at=stamp).validate()
        updated_tasks[indexed[task_id]] = updated
        changes.append((current, updated))
    if not changes:
        return {"updated": 0, "task_ids": []}
    candidate = replace(portfolio, tasks=tuple(updated_tasks),
                        updated_at=stamp).validate()
    store.backup_portfolio(root, reason="bulk-estimate")
    store.save_portfolio(root, candidate)
    for before, after in changes:
        _journal(root, kind=changes_module.KIND_UPDATE,
                 object_kind=changes_module.OBJECT_TASK,
                 object_id=after.task_id, title=after.title,
                 summary="estimation mise à jour",
                 before={"estimated_minutes": before.estimated_minutes},
                 after={"estimated_minutes": after.estimated_minutes},
                 at=stamp)
    return {"updated": len(changes),
            "task_ids": [after.task_id for _, after in changes]}


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
    _journal(root, kind=changes_module.KIND_UPDATE,
             object_kind=changes_module.OBJECT_TASK, object_id=task_id,
             title=updated.title, summary="tâche mise à jour",
             before={key: getattr(current, key) for key in changes},
             after={key: getattr(updated, key) for key in changes},
             at=candidate.updated_at)
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


@_serialized
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


@_serialized
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
    "add_note",
    "create_project",
    "create_task",
    "delete_note",
    "estimate_tasks",
    "remove_dependency",
    "rename_domain",
    "set_aliases",
    "update_note",
    "update_project",
    "update_task",
]
