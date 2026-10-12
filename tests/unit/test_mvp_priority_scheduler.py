"""Unit tests for MVP prioritisation and day/week scheduling."""

from __future__ import annotations

from datetime import date, timedelta

from trajectory_os.mvp import model, priority, scheduler


def _portfolio(tasks: tuple[model.Task, ...],
               projects: tuple[model.Project, ...] | None = None,
               calendar: tuple[model.CalendarEvent, ...] = ()) -> model.Portfolio:
    projects = projects or (
        model.Project(project_id="p1", name="P1", objective="o"),
    )
    return model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t", projects=projects,
        tasks=tasks, calendar=calendar)


TODAY = date(2026, 9, 20)


def test_ranking_orders_by_urgency_and_deadline() -> None:
    tasks = (
        model.Task(task_id="low", project_id="p1", title="low",
                   urgency=model.U_LOW, impact=model.I_LOW),
        model.Task(task_id="critical", project_id="p1", title="critical",
                   urgency=model.U_CRITICAL, impact=model.I_HIGH,
                   deadline=(TODAY + timedelta(days=1)).isoformat()),
    )
    ranked = priority.rank_ready_tasks(_portfolio(tasks=tasks), today=TODAY)
    assert ranked[0].task_id == "critical"
    assert any("deadline" in reason for reason in ranked[0].reasons)


def test_ranking_reports_unblock_value() -> None:
    tasks = (
        model.Task(task_id="a", project_id="p1", title="a",
                   urgency=model.U_HIGH),
        model.Task(task_id="b", project_id="p1", title="b",
                   dependencies=("a",)),
    )
    ranked = priority.rank_ready_tasks(_portfolio(tasks=tasks), today=TODAY)
    item = next(r for r in ranked if r.task_id == "a")
    assert item.unblocks == 1
    assert any("unlocks 1" in reason for reason in item.reasons)


def test_deferrable_tasks() -> None:
    tasks = (
        model.Task(task_id="quiet", project_id="p1", title="quiet",
                   urgency=model.U_LOW, impact=model.I_LOW),
        model.Task(task_id="busy", project_id="p1", title="busy",
                   urgency=model.U_HIGH, impact=model.I_HIGH),
    )
    deferrable = priority.deferrable_tasks(_portfolio(tasks=tasks), today=TODAY)
    assert [item.task_id for item in deferrable] == ["quiet"]


def test_day_plan_reserves_buffer() -> None:
    tasks = tuple(
        model.Task(task_id=f"t{i}", project_id="p1", title=f"t{i}",
                   estimated_minutes=60) for i in range(10))
    day = scheduler.plan_day(_portfolio(tasks=tasks),
                             priority.rank_ready_tasks(
                                 _portfolio(tasks=tasks), today=TODAY),
                             TODAY)
    # 360 capacity, no calendar -> 30% buffer = 108 -> 252 plan capacity.
    assert day.buffer_minutes == 108
    assert day.plan_capacity == 252
    assert day.planned_minutes <= day.plan_capacity


def test_day_plan_honours_calendar() -> None:
    tasks = (model.Task(task_id="t1", project_id="p1", title="t1",
                        estimated_minutes=60),)
    event = model.CalendarEvent(event_id="e", title="meeting",
                                day=TODAY.isoformat(),
                                start_minutes=600, end_minutes=720)
    portfolio = _portfolio(tasks=tasks, calendar=(event,))
    day = scheduler.plan_day(portfolio,
                             priority.rank_ready_tasks(portfolio, today=TODAY),
                             TODAY)
    assert day.calendar_minutes == 120
    assert day.plan_capacity == 168  # (360-120) * 0.7


def test_unknown_effort_separated() -> None:
    tasks = (model.Task(task_id="noeffort", project_id="p1",
                        title="noeffort", estimated_minutes=None),)
    portfolio = _portfolio(tasks=tasks)
    day = scheduler.plan_day(portfolio,
                             priority.rank_ready_tasks(portfolio, today=TODAY),
                             TODAY)
    assert day.planned == ()
    assert len(day.unknown_effort) == 1


def test_week_plan_respects_deadline_and_flags_risk() -> None:
    tasks = (
        model.Task(task_id="big", project_id="p1", title="big",
                   estimated_minutes=300, urgency=model.U_CRITICAL,
                   deadline=(TODAY + timedelta(days=1)).isoformat()),
    )
    portfolio = _portfolio(tasks=tasks)
    week = scheduler.plan_week(portfolio,
                               priority.rank_ready_tasks(portfolio,
                                                         today=TODAY),
                               TODAY)
    # 300m does not fit any day (max 252 plan capacity) before the deadline.
    assert len(week.at_risk) == 1
    assert week.at_risk[0].task_id == "big"
