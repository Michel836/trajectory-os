"""MVP — deterministic, read-only project explanations.

This is the safe precursor to the bounded assistant: no LLM, no tools, no
writes. It composes the canonical portfolio, readiness, priority, change
journal and inbound signals into one human-readable answer to:

    "Where does this project stand, why, and what should happen next?"
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import (
    changes,
    model,
    priority,
    readiness,
    signals,
    store,
    visualization,
)


def explain_project(root: str | Path, project_id: str,
                    *, today: date | None = None) -> dict[str, Any]:
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    project = portfolio.project_map().get(project_id)
    if project is None:
        raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
    now = today or date.today()
    tasks = tuple(task for task in portfolio.tasks
                  if task.project_id == project_id)
    readiness_map = {item.task_id: item
                     for item in readiness.evaluate(portfolio)}
    ranked = priority.rank_ready_tasks(portfolio, today=now)
    next_actions = [
        {
            "task_id": item.task_id, "title": item.title,
            "effort_minutes": item.effort_minutes,
            "reasons": list(item.reasons),
        }
        for item in ranked if item.project_id == project_id][:3]
    blockers = [
        {
            "task_id": task.task_id, "title": task.title,
            "state": readiness_map[task.task_id].state,
            "reasons": list(readiness_map[task.task_id].reasons),
        }
        for task in tasks
        if readiness_map[task.task_id].state in (
            readiness.RS_BLOCKED, readiness.RS_WAITING)]
    open_tasks = [task for task in tasks
                  if task.status not in model.TASK_TERMINAL_STATUSES]
    completed = sum(1 for task in tasks if task.status == model.TS_COMPLETED)
    progress = (completed / len(tasks)) if tasks else None
    health = visualization.project_health(
        project, tasks, readiness_map, now, portfolio.capacity)
    object_ids = {project_id} | {task.task_id for task in tasks}
    recent = [
        change.to_dict()
        for change in changes.read_changes(root, limit=100)
        if change.object_id in object_ids][:10]
    inbound = [
        signal.to_dict()
        for signal in signals.read_signals(root, limit=100)
        if signal.project_hint == project_id][:10]
    risks: list[str] = []
    if health == visualization.HEALTH_AT_RISK:
        risks.append("l'échéance approche alors que la progression est faible")
    if health == visualization.HEALTH_STALE:
        risks.append("aucune activité récente")
    if health == visualization.HEALTH_NO_NEXT_ACTION:
        risks.append("aucune tâche ouverte : ajouter une prochaine action ou parquer")
    if health == visualization.HEALTH_OVERLOADED:
        risks.append("la charge estimée dépasse la capacité d'une semaine")
    if project.deadline is None:
        risks.append("aucune échéance explicite")
    return {
        "schema_version": model.SCHEMA_VERSION,
        "kind": "project_explanation",
        "canonical": False,
        "generated_at": intel_model.utc_now(),
        "project": project.to_dict(),
        "health": health,
        "progress": progress,
        "tasks": len(tasks),
        "open_tasks": len(open_tasks),
        "completed_tasks": completed,
        "next_actions": next_actions,
        "blockers": blockers,
        "risks": risks,
        "recent_changes": recent,
        "signals": inbound,
    }


__all__ = ["explain_project"]
