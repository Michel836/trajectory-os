"""MVP — explainable prioritisation.

Priorities are computed from explicit, supplied signals only:

* urgency (critical/high/medium/low);
* impact (high/medium/low);
* deadline pressure (distance to an explicit deadline);
* dependency-unblocking value (how many downstream tasks become free);
* effort (small wins first, as a soft tie-breaker);
* project importance (project urgency + impact).

The internal score is an integer used for deterministic ordering only. It is
**never** shown to the user as a pseudo-precise number. The user-facing output
is always a set of plain-language reasons ("takes ~20 min", "unlocks 3
downstream actions", "deadline in 2 days", ...).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from trajectory_os.mvp import graph as graph_module
from trajectory_os.mvp import model, readiness

#: Contribution weights (internal only; never surfaced as precision).
W_URGENCY = 30
W_IMPACT = 20
W_PROJECT = 5
W_UNBLOCK = 8
W_DEADLINE_MAX = 40


def days_until(deadline: str, today: date) -> int:
    """Days from ``today`` until ``deadline`` (negative when overdue)."""
    return (date.fromisoformat(deadline) - today).days


def _deadline_pressure(deadline: str | None, today: date) -> tuple[int, str]:
    if deadline is None:
        return 0, "no deadline set"
    delta = days_until(deadline, today)
    if delta < 0:
        return W_DEADLINE_MAX, f"deadline overdue by {-delta} day(s)"
    if delta == 0:
        return W_DEADLINE_MAX, "deadline is today"
    if delta <= 2:
        return int(W_DEADLINE_MAX * 0.9), f"deadline in {delta} day(s)"
    if delta <= 7:
        return int(W_DEADLINE_MAX * 0.6), f"deadline in {delta} day(s)"
    if delta <= 14:
        return int(W_DEADLINE_MAX * 0.3), f"deadline in {delta} day(s)"
    return int(W_DEADLINE_MAX * 0.1), f"deadline in {delta} day(s)"


def _effort_preference(effort_minutes: int) -> tuple[int, str]:
    if effort_minutes <= 0:
        return 0, "effort unknown"
    pref = min(20, max(0, 120 // max(1, effort_minutes)))
    return pref, f"takes ~{effort_minutes} min"


@dataclass(frozen=True)
class PrioritizedTask:
    """One ready task with its deterministic rank and explanations."""

    task_id: str
    project_id: str
    title: str
    rank: int
    score: int
    urgency: str
    impact: str
    effort_minutes: int
    deadline: str | None
    unblocks: int
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "title": self.title,
            "rank": self.rank,
            "urgency": self.urgency,
            "impact": self.impact,
            "effort_minutes": self.effort_minutes,
            "deadline": self.deadline,
            "unblocks": self.unblocks,
            "reasons": list(self.reasons),
        }


def rank_ready_tasks(portfolio: model.Portfolio, *,
                     today: date | None = None) -> tuple[PrioritizedTask, ...]:
    """Rank the ready tasks, most important first, with explanations."""
    today = today or date.today()
    projects = portfolio.project_map()
    tasks = portfolio.task_map()
    dep_graph = graph_module.build_graph(portfolio)
    ready = {item.task_id: item for item in readiness.ready_tasks(portfolio)}

    scored: list[PrioritizedTask] = []
    for task_id, ready_task in ready.items():
        task = tasks[task_id]
        project = projects[task.project_id]
        deadline_score, deadline_reason = _deadline_pressure(task.deadline,
                                                             today)
        effort_score, effort_reason = _effort_preference(task.effort_minutes)
        unblocks = dep_graph.transitive_unblocks(task_id)
        project_score = (model.URGENCY_RANK[project.urgency]
                         + model.IMPACT_RANK[project.impact]) * W_PROJECT
        score = (
            model.URGENCY_RANK[task.urgency] * W_URGENCY
            + model.IMPACT_RANK[task.impact] * W_IMPACT
            + project_score
            + deadline_score
            + min(unblocks, 5) * W_UNBLOCK
            + effort_score
        )
        reasons = _build_reasons(task, project, ready_task, unblocks,
                                 deadline_reason, effort_reason)
        scored.append(PrioritizedTask(
            task_id=task_id,
            project_id=task.project_id,
            title=task.title,
            rank=0,
            score=score,
            urgency=task.urgency,
            impact=task.impact,
            effort_minutes=task.effort_minutes,
            deadline=task.deadline,
            unblocks=unblocks,
            reasons=reasons,
        ))

    scored.sort(key=lambda item: (-item.score, item.task_id))
    ranked = tuple(
        PrioritizedTask(
            task_id=item.task_id, project_id=item.project_id,
            title=item.title, rank=index + 1, score=item.score,
            urgency=item.urgency, impact=item.impact,
            effort_minutes=item.effort_minutes, deadline=item.deadline,
            unblocks=item.unblocks, reasons=item.reasons)
        for index, item in enumerate(scored))
    return ranked


def _build_reasons(
    task: model.Task,
    project: model.Project,
    ready_task: readiness.TaskReadiness,
    unblocks: int,
    deadline_reason: str,
    effort_reason: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if ready_task.state == readiness.RS_IN_PROGRESS:
        reasons.append("already in progress")
    if task.urgency in (model.U_CRITICAL, model.U_HIGH):
        reasons.append(f"{task.urgency.lower()} urgency")
    if task.impact == model.I_HIGH:
        reasons.append("high impact")
    if project.urgency in (model.U_CRITICAL, model.U_HIGH) \
            or project.impact == model.I_HIGH:
        reasons.append(f"important project: {project.name}")
    if task.effort_minutes:
        reasons.append(effort_reason)
    if task.deadline:
        reasons.append(deadline_reason)
    if unblocks:
        reasons.append(f"unlocks {unblocks} downstream action(s)")
    if task.next_action:
        reasons.append(f"next action: {task.next_action}")
    if not reasons:
        reasons.append("no pressing constraint; safe to do when free")
    return tuple(reasons)


def deferrable_tasks(portfolio: model.Portfolio, *,
                     today: date | None = None) -> tuple[PrioritizedTask, ...]:
    """Ready tasks that can safely be deferred (low urgency/impact, no
    deadline pressure, no downstream unlock value)."""
    today = today or date.today()
    tasks = portfolio.task_map()
    dep_graph = graph_module.build_graph(portfolio)
    ready = readiness.ready_tasks(portfolio)

    result: list[PrioritizedTask] = []
    for item in ready:
        task = tasks[item.task_id]
        deadline_score, _ = _deadline_pressure(task.deadline, today)
        unblocks = dep_graph.transitive_unblocks(task.task_id)
        low = (task.urgency in (model.U_LOW, model.U_MEDIUM)
               and task.impact == model.I_LOW
               and deadline_score <= int(W_DEADLINE_MAX * 0.3)
               and unblocks == 0)
        if not low:
            continue
        result.append(PrioritizedTask(
            task_id=task.task_id, project_id=task.project_id,
            title=task.title, rank=0, score=0, urgency=task.urgency,
            impact=task.impact, effort_minutes=task.effort_minutes,
            deadline=task.deadline, unblocks=unblocks,
            reasons=("low urgency and impact; no deadline pressure; "
                     "safe to defer",),
        ))
    return tuple(result)


__all__ = [
    "PrioritizedTask",
    "days_until",
    "deferrable_tasks",
    "rank_ready_tasks",
]
