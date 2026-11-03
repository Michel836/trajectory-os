"""MVP — guided project review (GTD/OmniFocus-inspired).

One project at a time, with its health, objective, progress, next action and a
small set of explicit decisions. The review never deletes anything and never
changes a task: it only applies the human's project-level decision.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import (
    changes,
    model,
    mutations,
    priority,
    readiness,
    store,
)

DECISION_KEEP = "KEEP"
DECISION_DEFER = "DEFER"
DECISION_PARK_IDEA = "PARK_IDEA"
DECISION_PARK_SOMEDAY = "PARK_SOMEDAY"
DECISION_CLOSE = "CLOSE"
DECISION_SKIP = "SKIP"
DECISIONS = frozenset({
    DECISION_KEEP, DECISION_DEFER, DECISION_PARK_IDEA,
    DECISION_PARK_SOMEDAY, DECISION_CLOSE, DECISION_SKIP,
})

HEALTH_ORDER = {
    "AT_RISK": 0, "STALE": 1, "NO_NEXT_ACTION": 2, "OVERLOADED": 3,
    "BLOCKED": 4, "WAITING": 5, "ON_TRACK": 6, "DONE": 7,
}
PROJECT_HEALTH = tuple(HEALTH_ORDER)


def _suggested_decision(health: str) -> str:
    if health == "NO_NEXT_ACTION":
        return DECISION_PARK_IDEA
    if health == "STALE":
        return DECISION_DEFER
    if health == "DONE":
        return DECISION_CLOSE
    return DECISION_KEEP


def build_queue(root: str | Path, *, today: date | None = None
                ) -> dict[str, Any]:
    from trajectory_os.mvp import visualization

    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    now = today or date.today()
    readiness_map = {item.task_id: item
                     for item in readiness.evaluate(portfolio)}
    ranked = priority.rank_ready_tasks(portfolio, today=now)
    best_by_project: dict[str, priority.PrioritizedTask] = {}
    for item in ranked:
        best_by_project.setdefault(item.project_id, item)
    projects: list[dict[str, Any]] = []
    counts = {label: 0 for label in PROJECT_HEALTH}
    for project in portfolio.projects:
        tasks = tuple(task for task in portfolio.tasks
                      if task.project_id == project.project_id)
        health = visualization.project_health(
            project, tasks, readiness_map, now, portfolio.capacity)
        counts[health] += 1
        open_tasks = [task for task in tasks
                      if task.status not in model.TASK_TERMINAL_STATUSES]
        completed = sum(1 for task in tasks
                        if task.status == model.TS_COMPLETED)
        next_action = best_by_project.get(project.project_id)
        last_activity = max(
            (task.updated_at for task in tasks if task.updated_at),
            default="")
        projects.append({
            "project_id": project.project_id,
            "name": project.name,
            "objective": project.objective,
            "status": project.status,
            "health": health,
            "progress": (completed / len(tasks)) if tasks else None,
            "tasks": len(tasks),
            "open_tasks": len(open_tasks),
            "completed": completed,
            "deadline": project.deadline,
            "last_activity": last_activity,
            "next_action": next_action.title if next_action else "",
            "next_action_id": next_action.task_id if next_action else "",
            "suggested_decision": _suggested_decision(health),
        })
    projects.sort(key=lambda item: (
        HEALTH_ORDER.get(str(item["health"]), 99),
        str(item["deadline"] or "9999-12-31"),
        str(item["name"]).lower()))
    return {
        "generated_at": intel_model.utc_now(),
        "today": now.isoformat(),
        "counts": counts,
        "projects": projects,
    }


def decide(root: str | Path, project_id: str, decision: str,
           *, note: str = "") -> dict[str, Any]:
    if decision not in DECISIONS:
        raise model.MvpError(model.E_MALFORMED,
                             f"decision {decision!r} is not allowed")
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    project = portfolio.project_map().get(project_id)
    if project is None:
        raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
    if decision == DECISION_SKIP:
        return {"project_id": project_id, "decision": decision,
                "status": project.status, "changed": False}
    if decision == DECISION_KEEP:
        result = mutations.update_project(root, project_id,
                                          {"status": model.PS_ACTIVE})
    elif decision == DECISION_DEFER:
        result = mutations.update_project(root, project_id,
                                          {"status": model.PS_DEFERRED})
    elif decision == DECISION_CLOSE:
        result = mutations.update_project(root, project_id,
                                          {"status": model.PS_COMPLETED})
    elif decision == DECISION_PARK_IDEA:
        result = mutations.park_project(root, project_id,
                                        {"kind": "IDEA"})
    else:
        result = mutations.park_project(root, project_id,
                                        {"kind": "SOMEDAY"})
    change_id = ""
    try:
        change = changes.append_change(
            root, kind=changes.KIND_UPDATE,
            object_kind=changes.OBJECT_PROJECT, object_id=project_id,
            title=project.name, summary=f"revue projet · {decision}",
            before={"status": project.status},
            after={"decision": decision, "note": note[:1000]},
            source="review", undoable=False)
        change_id = change.change_id
    except (OSError, changes.ChangeError):
        pass
    return {
        "project_id": project_id, "decision": decision,
        "status": result.get("status", project.status),
        "changed": True, "change_id": change_id,
    }


__all__ = [
    "DECISION_CLOSE", "DECISION_DEFER", "DECISION_KEEP",
    "DECISION_PARK_IDEA", "DECISION_PARK_SOMEDAY", "DECISION_SKIP",
    "DECISIONS", "build_queue", "decide",
]
