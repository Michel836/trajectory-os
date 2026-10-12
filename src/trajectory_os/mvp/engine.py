"""MVP — orchestration: turn a portfolio into the daily decision cockpit.

This module composes the reusable pieces (WBS, dependency graph, readiness,
prioritisation, scheduler, outcomes) into a single read-only projection that
answers the ten daily questions, and persists today/week plan snapshots plus a
cockpit document under the data root.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import graph as graph_module
from trajectory_os.mvp import (
    model,
    outcomes,
    priority,
    readiness,
    scheduler,
    store,
    wbs,
)

#: How the cockpit counts project statuses.
PROJECT_STATUS_ORDER = (
    model.PS_ACTIVE, model.PS_WAITING, model.PS_BLOCKED,
    model.PS_DEFERRED, model.PS_COMPLETED,
)


@dataclass(frozen=True)
class Cockpit:
    """The complete daily cockpit projection."""

    generated_at: str
    today: str
    name: str
    capacity: dict[str, Any]
    portfolio: dict[str, Any]
    projects: tuple[dict[str, Any], ...]
    ready: tuple[dict[str, Any], ...]
    blocked: tuple[dict[str, Any], ...]
    waiting: tuple[dict[str, Any], ...]
    deferrable: tuple[dict[str, Any], ...]
    today_plan: dict[str, Any]
    week_plan: dict[str, Any]
    planned_vs_actual: dict[str, Any]
    recent_outcomes: tuple[dict[str, Any], ...]
    wbs: dict[str, Any]
    dependency_graph: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "mvp_version": model.MVP_VERSION,
            "kind": "mvp_cockpit",
            "generated_at": self.generated_at,
            "today": self.today,
            "name": self.name,
            "capacity": self.capacity,
            "portfolio": self.portfolio,
            "projects": list(self.projects),
            "ready": list(self.ready),
            "blocked": list(self.blocked),
            "waiting": list(self.waiting),
            "deferrable": list(self.deferrable),
            "today_plan": self.today_plan,
            "week_plan": self.week_plan,
            "planned_vs_actual": self.planned_vs_actual,
            "recent_outcomes": list(self.recent_outcomes),
            "wbs": self.wbs,
            "dependency_graph": self.dependency_graph,
        }


def _portfolio_summary(portfolio: model.Portfolio) -> dict[str, Any]:
    counts = {status: 0 for status in PROJECT_STATUS_ORDER}
    for project in portfolio.projects:
        counts[project.status] = counts.get(project.status, 0) + 1
    task_counts = {status: 0 for status in model.TASK_STATUSES}
    for task in portfolio.tasks:
        task_counts[task.status] += 1
    return {
        "projects": len(portfolio.projects),
        "tasks": len(portfolio.tasks),
        "project_status": counts,
        "task_status": task_counts,
    }


def _project_views(portfolio: model.Portfolio,
                   ready_ranked: tuple[priority.PrioritizedTask, ...],
                   readiness_map: dict[str, readiness.TaskReadiness],
                   ) -> tuple[dict[str, Any], ...]:
    ready_by_project: dict[str, priority.PrioritizedTask] = {}
    for item in ready_ranked:
        if item.project_id not in ready_by_project:
            ready_by_project[item.project_id] = item

    views: list[dict[str, Any]] = []
    for project in sorted(portfolio.projects, key=lambda p: p.project_id):
        project_tasks = [t for t in portfolio.tasks
                         if t.project_id == project.project_id]
        completed = sum(1 for t in project_tasks
                        if t.status == model.TS_COMPLETED)
        progress = (completed / len(project_tasks)) if project_tasks else None
        next_item = ready_by_project.get(project.project_id)
        views.append({
            "project_id": project.project_id,
            "name": project.name,
            "objective": project.objective,
            "domain": project.domain,
            "status": project.status,
            "urgency": project.urgency,
            "impact": project.impact,
            "deadline": project.deadline,
            "tasks": len(project_tasks),
            "completed": completed,
            "progress": (round(progress, 3) if progress is not None else None),
            "next_action": (next_item.title if next_item is not None
                            else None),
        })
    return tuple(views)


def _blocked_views(portfolio: model.Portfolio,
                   readiness_map: dict[str, readiness.TaskReadiness],
                   ) -> tuple[dict[str, Any], ...]:
    tasks = portfolio.task_map()
    projects = portfolio.project_map()
    views: list[dict[str, Any]] = []
    for item in readiness_map.values():
        if item.state != readiness.RS_BLOCKED:
            continue
        task = tasks[item.task_id]
        project = projects[task.project_id]
        suggested = _suggested_action(item)
        views.append({
            "task_id": item.task_id,
            "title": task.title,
            "project": project.name,
            "blockers": list(item.blockers),
            "missing_resources": list(item.missing_resources),
            "reason": "; ".join(item.reasons),
            "suggested_action": suggested,
        })
    return tuple(sorted(views, key=lambda v: str(v["task_id"])))


def _waiting_views(portfolio: model.Portfolio,
                   readiness_map: dict[str, readiness.TaskReadiness],
                   ) -> tuple[dict[str, Any], ...]:
    tasks = portfolio.task_map()
    projects = portfolio.project_map()
    views: list[dict[str, Any]] = []
    for item in readiness_map.values():
        if item.state != readiness.RS_WAITING:
            continue
        task = tasks[item.task_id]
        project = projects[task.project_id]
        views.append({
            "task_id": item.task_id,
            "title": task.title,
            "project": project.name,
            "waiting_for": list(task.waiting_for),
            "reason": "; ".join(item.reasons),
            "suggested_action": ("follow up on: "
                                 + (task.waiting_for[0]
                                    if task.waiting_for else "external party")),
        })
    return tuple(sorted(views, key=lambda v: str(v["task_id"])))


def _suggested_action(item: readiness.TaskReadiness) -> str:
    if item.blockers:
        return "complete blocker(s) first: " + ", ".join(item.blockers)
    if item.missing_resources:
        return "secure or replace resource(s): " + ", ".join(
            item.missing_resources)
    if any("dependency not complete" in reason for reason in item.reasons):
        return "finish incomplete dependency first"
    return "review scope or record a decision"


def _dependency_views(portfolio: model.Portfolio,
                      dep_graph: graph_module.DependencyGraph) -> dict[str, Any]:
    tasks = portfolio.task_map()
    unlocks: list[dict[str, Any]] = []
    for task_id in sorted(tasks):
        downstream = dep_graph.downstream(task_id)
        if downstream:
            unlocks.append({
                "task_id": task_id,
                "title": tasks[task_id].title,
                "unblocks": [{"task_id": dep, "title": tasks[dep].title}
                             for dep in downstream],
            })
    return {
        "edges": [
            {"from": source, "to": target}
            for source, targets in sorted(dep_graph.successors.items())
            for target in targets
        ],
        "what_unblocks": unlocks,
    }


def build_cockpit(root: str | Path, *, today: date | None = None,
                  persist: bool = True) -> Cockpit:
    """Build the full cockpit projection (read-only over the portfolio)."""
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    today = today or date.today()
    stamp = intel_model.utc_now()

    ready_ranked = priority.rank_ready_tasks(portfolio, today=today)
    readiness_map = {item.task_id: item
                     for item in readiness.evaluate(portfolio)}
    dep_graph = graph_module.build_graph(portfolio)
    outcomes_records = store.read_outcomes(root)
    pva = outcomes.planned_vs_actual(portfolio, outcomes_records)
    recent_outcomes = tuple(record.to_dict()
                            for record in reversed(outcomes_records[-10:]))
    day = scheduler.plan_day(portfolio, ready_ranked, today)
    week = scheduler.plan_week(portfolio, ready_ranked, today)
    deferrable = priority.deferrable_tasks(portfolio, today=today)

    cockpit = Cockpit(
        generated_at=stamp,
        today=today.isoformat(),
        name=portfolio.name,
        capacity=portfolio.capacity.to_dict(),
        portfolio=_portfolio_summary(portfolio),
        projects=_project_views(portfolio, ready_ranked, readiness_map),
        ready=tuple(item.to_dict() for item in ready_ranked),
        blocked=_blocked_views(portfolio, readiness_map),
        waiting=_waiting_views(portfolio, readiness_map),
        deferrable=tuple(item.to_dict() for item in deferrable),
        today_plan=day.to_dict(),
        week_plan=week.to_dict(),
        planned_vs_actual=pva.to_dict(),
        recent_outcomes=recent_outcomes,
        wbs=wbs.tree(portfolio),
        dependency_graph=_dependency_views(portfolio, dep_graph),
    )
    if persist:
        store.save_plan(root, "today", cockpit.today_plan)
        store.save_plan(root, "week", cockpit.week_plan)
        intel_model.write_json(Path(root) / "cockpit.json", cockpit.to_dict())
    return cockpit


__all__ = ["Cockpit", "build_cockpit"]
