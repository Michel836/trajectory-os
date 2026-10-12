"""MVP — realistic, time-of-day aware day/week scheduler.

Inputs: available capacity, the explicit working window, hard calendar
constraints, ready tasks, effort estimates, deadlines and priorities.

Rules:

* never plan 100% of available time — the configured buffer (default 30%) is
  reserved unless the user sets it to zero;
* the working window (``Capacity.day_start_minutes`` .. ``day_end_minutes``)
  makes capacity *time-of-day aware*: at 15:00 with a 09:00-18:00 window only
  the last third of the day remains, and the buffer is recomputed on that
  remaining time;
* hard calendar commitments reduce the day's usable focus time first, and only
  the part of the window still ahead is counted for today;
* tasks are packed in priority order and never oversubscribe a day;
* a task without an effort estimate is reported separately (it can be done
  but cannot be time-boxed);
* output is realistic, not maximalist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
class DayBudget:
    """One day's nominal *and* remaining capacity (pure, deterministic).

    ``total_capacity``/``plan_capacity`` are the historical whole-day budget.
    The ``remaining_*`` fields describe what is actually left from the current
    instant onward inside the working window. For a future day they are equal
    to the nominal values; for today they shrink as the window elapses.
    """

    day: str
    is_today: bool
    total_capacity: int
    window_minutes: int
    elapsed_minutes: int
    remaining_window_minutes: int
    calendar_minutes: int
    remaining_calendar_minutes: int
    buffer_minutes: int
    plan_capacity: int
    remaining_buffer_minutes: int
    remaining_plan_capacity: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "is_today": self.is_today,
            "total_capacity": self.total_capacity,
            "window_minutes": self.window_minutes,
            "elapsed_minutes": self.elapsed_minutes,
            "remaining_window_minutes": self.remaining_window_minutes,
            "calendar_minutes": self.calendar_minutes,
            "remaining_calendar_minutes": self.remaining_calendar_minutes,
            "buffer_minutes": self.buffer_minutes,
            "plan_capacity": self.plan_capacity,
            "remaining_buffer_minutes": self.remaining_buffer_minutes,
            "remaining_plan_capacity": self.remaining_plan_capacity,
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
    is_today: bool
    window_minutes: int
    elapsed_minutes: int
    remaining_window_minutes: int
    remaining_calendar_minutes: int
    remaining_buffer_minutes: int
    remaining_plan_capacity: int

    @property
    def display_calendar_minutes(self) -> int:
        """Calendar time relevant to the capacity line for this day."""
        return (self.remaining_calendar_minutes if self.is_today
                else self.calendar_minutes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "total_capacity": self.total_capacity,
            "calendar_minutes": self.calendar_minutes,
            "display_calendar_minutes": self.display_calendar_minutes,
            "buffer_minutes": self.buffer_minutes,
            "plan_capacity": self.plan_capacity,
            "planned_minutes": self.planned_minutes,
            "planned": [item.to_dict() for item in self.planned],
            "deferred": [item.to_dict() for item in self.deferred],
            "unknown_effort": [item.to_dict()
                               for item in self.unknown_effort],
            "calendar": [event.to_dict() for event in self.calendar],
            "is_today": self.is_today,
            "window_minutes": self.window_minutes,
            "elapsed_minutes": self.elapsed_minutes,
            "remaining_window_minutes": self.remaining_window_minutes,
            "remaining_calendar_minutes": self.remaining_calendar_minutes,
            "remaining_buffer_minutes": self.remaining_buffer_minutes,
            "remaining_plan_capacity": self.remaining_plan_capacity,
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


def _overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b))


def _now_minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def day_budget(portfolio: model.Portfolio, day: date, *,
               now: datetime | None = None) -> DayBudget:
    """Compute one day's nominal and remaining capacity.

    The nominal budget is the historical behaviour: the configured daily
    capacity minus the day's calendar commitments, then minus the buffer.

    The remaining budget prorates the configured daily capacity across the
    working window and is therefore aware of the current instant: at 15:00
    with a 09:00-18:00 window, only the last third of the day is budgeted. The
    buffer is recomputed on the remaining available time so the scheduler can
    never plan 100% of what is left, and calendar commitments are only counted
    on the part of the window still ahead.

    ``now`` is injected for deterministic tests; it defaults to the local
    current time (never UTC, so the working window lines up with the user's
    clock).
    """
    capacity = portfolio.capacity
    day_str = day.isoformat()
    window = capacity.window_minutes
    events = _events_on(portfolio, day_str)
    calendar_minutes = _event_minutes(events)

    available = max(0, capacity.minutes_per_day - calendar_minutes)
    buffer_minutes = int(round(available * float(capacity.buffer_ratio)))
    plan_capacity = max(0, available - buffer_minutes)

    now = now or datetime.now()
    is_today = now.date() == day

    # Any day that is not today keeps the whole window: its remaining budget
    # is exactly its nominal budget. This keeps future-day planning unchanged.
    if not is_today:
        return DayBudget(
            day=day_str,
            is_today=False,
            total_capacity=capacity.minutes_per_day,
            window_minutes=window,
            elapsed_minutes=0,
            remaining_window_minutes=window,
            calendar_minutes=calendar_minutes,
            remaining_calendar_minutes=calendar_minutes,
            buffer_minutes=buffer_minutes,
            plan_capacity=plan_capacity,
            remaining_buffer_minutes=buffer_minutes,
            remaining_plan_capacity=plan_capacity,
        )

    now_min = _now_minutes(now)
    elapsed = min(max(0, now_min - capacity.day_start_minutes), window)
    remaining_window = window - elapsed

    if window > 0:
        remaining_focus = int(round(
            capacity.minutes_per_day * remaining_window / window))
    else:
        remaining_focus = 0
    # The configured capacity can never exceed the time actually left in the
    # working window.
    remaining_focus = min(remaining_focus, remaining_window)

    horizon_start = max(capacity.day_start_minutes, now_min)
    remaining_calendar = sum(
        _overlap(event.start_minutes, event.end_minutes,
                 horizon_start, capacity.day_end_minutes)
        for event in events)

    remaining_available = max(0, remaining_focus - remaining_calendar)
    remaining_buffer = int(round(
        remaining_available * float(capacity.buffer_ratio)))
    remaining_plan_capacity = max(0, remaining_available - remaining_buffer)

    return DayBudget(
        day=day_str,
        is_today=is_today,
        total_capacity=capacity.minutes_per_day,
        window_minutes=window,
        elapsed_minutes=elapsed,
        remaining_window_minutes=remaining_window,
        calendar_minutes=calendar_minutes,
        remaining_calendar_minutes=remaining_calendar,
        buffer_minutes=buffer_minutes,
        plan_capacity=plan_capacity,
        remaining_buffer_minutes=remaining_buffer,
        remaining_plan_capacity=remaining_plan_capacity,
    )


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


def _day_plan(budget: DayBudget,
              calendar: tuple[model.CalendarEvent, ...],
              planned: list[PlanItem],
              deferred: list[priority.PrioritizedTask],
              unknown_effort: list[priority.PrioritizedTask],
              planned_minutes: int) -> DayPlan:
    return DayPlan(
        day=budget.day,
        total_capacity=budget.total_capacity,
        calendar_minutes=budget.calendar_minutes,
        buffer_minutes=budget.buffer_minutes,
        plan_capacity=budget.plan_capacity,
        planned_minutes=planned_minutes,
        planned=tuple(planned),
        deferred=tuple(deferred),
        unknown_effort=tuple(unknown_effort),
        calendar=calendar,
        is_today=budget.is_today,
        window_minutes=budget.window_minutes,
        elapsed_minutes=budget.elapsed_minutes,
        remaining_window_minutes=budget.remaining_window_minutes,
        remaining_calendar_minutes=budget.remaining_calendar_minutes,
        remaining_buffer_minutes=budget.remaining_buffer_minutes,
        remaining_plan_capacity=budget.remaining_plan_capacity,
    )


def plan_day(portfolio: model.Portfolio,
             ranked: tuple[priority.PrioritizedTask, ...],
             day: date, *, now: datetime | None = None,
             nominal: bool = False) -> DayPlan:
    """Build one day's plan from ranked ready tasks.

    For today, only the *remaining* capacity is packed, so a plan rebuilt at
    15:00 never pretends the whole morning is still available. ``nominal`` is
    reserved for historical Review baselines: it reconstructs the day's full
    task set without advertising that capacity as still available now.
    """
    projects = portfolio.project_map()
    tasks = portfolio.task_map()
    ranked_map = {item.task_id: item for item in ranked}
    budget = day_budget(portfolio, day, now=now)
    max_minutes = (budget.plan_capacity if nominal
                   else budget.remaining_plan_capacity)

    planned: list[PlanItem] = []
    deferred: list[priority.PrioritizedTask] = []
    unknown_effort: list[priority.PrioritizedTask] = []
    used = 0
    for item in ranked:
        task = tasks[item.task_id]
        if task.effort_minutes <= 0:
            unknown_effort.append(item)
            continue
        if used + task.effort_minutes > max_minutes:
            deferred.append(item)
            continue
        planned.append(_item_for(item.task_id, ranked_map, tasks, projects))
        used += task.effort_minutes

    return _day_plan(budget, _events_on(portfolio, budget.day), planned,
                     deferred, unknown_effort, used)


def plan_week(portfolio: model.Portfolio,
              ranked: tuple[priority.PrioritizedTask, ...],
              start: date, *, now: datetime | None = None) -> WeekPlan:
    """Build a seven-day plan, respecting deadlines.

    Tasks are packed in priority order into the earliest day where they fit
    and are not past their deadline. A task whose deadline cannot be met is
    reported in ``at_risk``. Today's remaining capacity is honoured, so work
    that no longer fits today spills into the following days.
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
        budget = day_budget(portfolio, day, now=now)
        max_minutes = budget.remaining_plan_capacity

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
            if used + task.effort_minutes > max_minutes:
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

        day_plans.append(_day_plan(budget, _events_on(portfolio, budget.day),
                                   planned, deferred, unknown_effort, used))

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


__all__ = ["DayBudget", "DayPlan", "PlanItem", "WeekPlan", "day_budget",
           "plan_day", "plan_week"]
