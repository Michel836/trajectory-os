"""MVP — realistic day/week scheduler.

Inputs: available capacity, hard calendar constraints, ready tasks, effort
estimates, deadlines and priorities.

Rules:

* never plan 100% of available time — the configured buffer (default 30%) is
  reserved unless the user sets it to zero;
* hard calendar commitments reduce the day's usable focus time first;
* tasks are packed in priority order and never oversubscribe a day;
* a task without an effort estimate is reported separately (it can be done
  but cannot be time-boxed);
* output is realistic, not maximalist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from trajectory_os.mvp import model, priority


@dataclass(frozen=True)
class PlanItem:
    """One task scheduled into a day."""

    task_id: str
    title: str
    project_id: str
    project_name: str
    estimated_minutes: int
    deadline: str | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "estimated_minutes": self.estimated_minutes,
            "deadline": self.deadline,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class DayPlan:
    """One day's realistic plan."""

    day: str
    total_capacity: int
    calendar_minutes: int
    buffer_minutes: int
    plan_capacity: int
    planned_minutes: int
    planned: tuple[PlanItem, ...]
    deferred: tuple[priority.PrioritizedTask, ...]
    unknown_effort: tuple[priority.PrioritizedTask, ...]
    calendar: tuple[model.CalendarEvent, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "total_capacity": self.total_capacity,
            "calendar_minutes": self.calendar_minutes,
            "buffer_minutes": self.buffer_minutes,
            "plan_capacity": self.plan_capacity,
            "planned_minutes": self.planned_minutes,
            "planned": [item.to_dict() for item in self.planned],
            "deferred": [item.to_dict() for item in self.deferred],
            "unknown_effort": [item.to_dict()
                               for item in self.unknown_effort],
            "calendar": [event.to_dict() for event in self.calendar],
        }


@dataclass(frozen=True)
class WeekPlan:
    """A seven-day plan plus tasks that could not be fitted before a deadline."""

    start_day: str
    days: tuple[DayPlan, ...]
    at_risk: tuple[priority.PrioritizedTask, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_day": self.start_day,
            "days": [day.to_dict() for day in self.days],
            "at_risk": [item.to_dict() for item in self.at_risk],
        }


def _events_on(portfolio: model.Portfolio, day: str) -> tuple[model.CalendarEvent, ...]:
    return tuple(sorted(
        (event for event in portfolio.calendar if event.day == day),
        key=lambda event: (event.start_minutes, event.event_id)))


def _event_minutes(events: tuple[model.CalendarEvent, ...]) -> int:
    return sum(max(0, event.end_minutes - event.start_minutes)
               for event in events)


def _item_for(task_id: str, ranked: dict[str, priority.PrioritizedTask],
              tasks: dict[str, model.Task],
              projects: dict[str, model.Project]) -> PlanItem:
    ranked_item = ranked[task_id]
    task = tasks[task_id]
    project = projects[task.project_id]
    return PlanItem(
        task_id=task_id,
        title=task.title,
        project_id=task.project_id,
        project_name=project.name,
        estimated_minutes=task.effort_minutes,
        deadline=task.deadline,
        reasons=ranked_item.reasons,
    )


def plan_day(portfolio: model.Portfolio,
             ranked: tuple[priority.PrioritizedTask, ...],
             day: date) -> DayPlan:
    """Build one day's plan from ranked ready tasks."""
    day_str = day.isoformat()
    capacity = portfolio.capacity
    projects = portfolio.project_map()
    tasks = portfolio.task_map()
    ranked_map = {item.task_id: item for item in ranked}

    calendar = _events_on(portfolio, day_str)
    calendar_minutes = _event_minutes(calendar)
    available = max(0, capacity.minutes_per_day - calendar_minutes)
    buffer_minutes = int(round(available * float(capacity.buffer_ratio)))
    plan_capacity = max(0, available - buffer_minutes)

    planned: list[PlanItem] = []
    deferred: list[priority.PrioritizedTask] = []
    unknown_effort: list[priority.PrioritizedTask] = []
    used = 0
    for item in ranked:
        task = tasks[item.task_id]
        if task.effort_minutes <= 0:
            unknown_effort.append(item)
            continue
        if used + task.effort_minutes > plan_capacity:
            deferred.append(item)
            continue
        planned.append(_item_for(item.task_id, ranked_map, tasks, projects))
        used += task.effort_minutes

    return DayPlan(
        day=day_str,
        total_capacity=capacity.minutes_per_day,
        calendar_minutes=calendar_minutes,
        buffer_minutes=buffer_minutes,
        plan_capacity=plan_capacity,
        planned_minutes=used,
        planned=tuple(planned),
        deferred=tuple(deferred),
        unknown_effort=tuple(unknown_effort),
        calendar=calendar,
    )


def plan_week(portfolio: model.Portfolio,
              ranked: tuple[priority.PrioritizedTask, ...],
              start: date) -> WeekPlan:
    """Build a seven-day plan, respecting deadlines.

    Tasks are packed in priority order into the earliest day where they fit
    and are not past their deadline. A task whose deadline cannot be met is
    reported in ``at_risk``.
    """
    projects = portfolio.project_map()
    tasks = portfolio.task_map()
    ranked_map = {item.task_id: item for item in ranked}

    days: list[date] = [start + timedelta(days=offset)
                        for offset in range(7)]
    day_plans: list[DayPlan] = []
    scheduled: set[str] = set()
    at_risk: list[priority.PrioritizedTask] = []

    for day in days:
        day_str = day.isoformat()
        capacity = portfolio.capacity
        calendar = _events_on(portfolio, day_str)
        calendar_minutes = _event_minutes(calendar)
        available = max(0, capacity.minutes_per_day - calendar_minutes)
        buffer_minutes = int(round(available * float(capacity.buffer_ratio)))
        plan_capacity = max(0, available - buffer_minutes)

        planned: list[PlanItem] = []
        deferred: list[priority.PrioritizedTask] = []
        unknown_effort: list[priority.PrioritizedTask] = []
        used = 0
        for item in ranked:
            if item.task_id in scheduled:
                continue
            task = tasks[item.task_id]
            if task.effort_minutes <= 0:
                unknown_effort.append(item)
                continue
            if used + task.effort_minutes > plan_capacity:
                deferred.append(item)
                continue
            if task.deadline is not None and task.deadline < day_str:
                at_risk.append(item)
                scheduled.add(item.task_id)
                continue
            planned.append(_item_for(item.task_id, ranked_map, tasks,
                                     projects))
            scheduled.add(item.task_id)
            used += task.effort_minutes

        day_plans.append(DayPlan(
            day=day_str,
            total_capacity=capacity.minutes_per_day,
            calendar_minutes=calendar_minutes,
            buffer_minutes=buffer_minutes,
            plan_capacity=plan_capacity,
            planned_minutes=used,
            planned=tuple(planned),
            deferred=tuple(deferred),
            unknown_effort=tuple(unknown_effort),
            calendar=calendar,
        ))

    # Tasks that never fit before a deadline inside the plan window are at
    # risk of missing it.
    last_day = days[-1].isoformat()
    for item in ranked:
        if item.task_id in scheduled or item.effort_minutes <= 0:
            continue
        if item.deadline is not None and item.deadline <= last_day:
            at_risk.append(item)

    return WeekPlan(start_day=start.isoformat(), days=tuple(day_plans),
                    at_risk=tuple(at_risk))


__all__ = ["DayPlan", "PlanItem", "WeekPlan", "plan_day", "plan_week"]
