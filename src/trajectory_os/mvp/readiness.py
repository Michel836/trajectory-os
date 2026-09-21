"""MVP — ready-task engine.

A task is *not* ready merely because it exists. Readiness is derived
deterministically from:

* project state (open vs deferred/completed);
* task state (terminal/deferred/blocked/waiting);
* dependency completion;
* explicit blockers;
* external waiting states;
* required resource availability.

Every non-ready task carries one or more human-readable reasons. Nothing is
inferred from silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trajectory_os.mvp import graph as graph_module
from trajectory_os.mvp import model

# --- readiness states ---------------------------------------------------------

RS_READY = "READY"
RS_IN_PROGRESS = "IN_PROGRESS"
RS_BLOCKED = "BLOCKED"
RS_WAITING = "WAITING"
RS_DEFERRED = "DEFERRED"
RS_COMPLETED = "COMPLETED"
RS_ABANDONED = "ABANDONED"
RS_PROJECT_CLOSED = "PROJECT_CLOSED"

READY_STATES = frozenset({RS_READY, RS_IN_PROGRESS})


@dataclass(frozen=True)
class TaskReadiness:
    """Deterministic readiness of one task."""

    task_id: str
    project_id: str
    state: str
    ready: bool
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    missing_resources: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "state": self.state,
            "ready": self.ready,
            "reasons": list(self.reasons),
            "blockers": list(self.blockers),
            "missing_resources": list(self.missing_resources),
        }


def evaluate(portfolio: model.Portfolio) -> tuple[TaskReadiness, ...]:
    """Evaluate readiness for every task in the portfolio."""
    projects = portfolio.project_map()
    tasks = portfolio.task_map()
    resources = portfolio.resource_map()
    dep_graph = graph_module.build_graph(portfolio)
    result: list[TaskReadiness] = []
    for task in sorted(portfolio.tasks,
                       key=lambda t: (t.project_id, t.task_id)):
        result.append(_evaluate_task(task, projects, tasks, resources,
                                     dep_graph))
    return tuple(result)


def _evaluate_task(
    task: model.Task,
    projects: dict[str, model.Project],
    tasks: dict[str, model.Task],
    resources: dict[str, model.Resource],
    dep_graph: graph_module.DependencyGraph,
) -> TaskReadiness:
    reasons: list[str] = []
    blockers: list[str] = []
    missing: list[str] = []
    has_incomplete_dependency = False

    project = projects[task.project_id]
    if task.status == model.TS_COMPLETED:
        return TaskReadiness(task.task_id, task.project_id, RS_COMPLETED,
                             False, ("task already completed",), (), ())
    if task.status == model.TS_ABANDONED:
        return TaskReadiness(task.task_id, task.project_id, RS_ABANDONED,
                             False, ("task abandoned",), (), ())
    if task.status == model.TS_DEFERRED:
        return TaskReadiness(task.task_id, task.project_id, RS_DEFERRED,
                             False, ("task deferred by decision",), (), ())
    if not project.open:
        reasons.append(f"project {project.name!r} is {project.status}")

    for dep in dep_graph.upstream(task.task_id):
        dep_task = tasks[dep]
        if dep_task.status == model.TS_COMPLETED:
            continue
        if dep in task.dependencies:
            has_incomplete_dependency = True
            reasons.append(f"dependency not complete: {dep}")
        if dep in task.blocked_by:
            blockers.append(dep)
            reasons.append(f"blocked by: {dep}")
    if task.status == model.TS_BLOCKED and not blockers:
        reasons.append("task marked blocked")

    if task.waiting_for:
        reasons.append("waiting for: " + ", ".join(task.waiting_for))

    for resource_id in task.resources:
        resource = resources[resource_id]
        if not resource.available:
            missing.append(resource_id)
            reasons.append(f"resource unavailable: {resource.name}")

    if missing or blockers or has_incomplete_dependency \
            or task.status == model.TS_BLOCKED:
        state = RS_BLOCKED
    elif task.waiting_for:
        state = RS_WAITING
    elif not project.open:
        state = RS_PROJECT_CLOSED
    elif task.status == model.TS_IN_PROGRESS:
        state = RS_IN_PROGRESS
    else:
        state = RS_READY

    return TaskReadiness(
        task_id=task.task_id,
        project_id=task.project_id,
        state=state,
        ready=state in READY_STATES,
        reasons=tuple(reasons),
        blockers=tuple(blockers),
        missing_resources=tuple(missing),
    )


def ready_tasks(portfolio: model.Portfolio) -> tuple[TaskReadiness, ...]:
    """Return only ready/continuing tasks, deterministically ordered."""
    return tuple(item for item in evaluate(portfolio) if item.ready)


def blocked_tasks(portfolio: model.Portfolio) -> tuple[TaskReadiness, ...]:
    return tuple(item for item in evaluate(portfolio)
                 if item.state == RS_BLOCKED)


def waiting_tasks(portfolio: model.Portfolio) -> tuple[TaskReadiness, ...]:
    return tuple(item for item in evaluate(portfolio)
                 if item.state == RS_WAITING)


__all__ = [
    "RS_ABANDONED", "RS_BLOCKED", "RS_COMPLETED", "RS_DEFERRED",
    "RS_IN_PROGRESS", "RS_PROJECT_CLOSED", "RS_READY", "RS_WAITING",
    "TaskReadiness",
    "blocked_tasks",
    "evaluate",
    "ready_tasks",
    "waiting_tasks",
]
