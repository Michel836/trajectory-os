"""Tests for the real agenda and the realistic week (A1/A2).

The app must hold the user's *actual* time constraints so plans are truthful:
one-off and weekly events, explicit days off, and an explicit set of working
weekdays (weekends off by default). Every capacity computation must stay
deterministic.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from trajectory_os.mvp import model, mutations, priority, scheduler, store

WEDNESDAY = date(2026, 10, 14)  # a working day
THURSDAY = date(2026, 10, 15)
SATURDAY = date(2026, 10, 17)  # a non-working day by default
NEXT_WEDNESDAY = date(2026, 10, 21)


def _portfolio() -> model.Portfolio:
    tasks = tuple(
        model.Task(task_id=f"t{i}", project_id="p1", title=f"T{i}",
                   estimated_minutes=120) for i in range(6))
    return model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p1", name="P1", objective="o"),),
        tasks=tasks,
        capacity=model.Capacity(minutes_per_day=360, buffer_ratio=0.30),
    )


def _seed(root: str) -> None:
    store.save_portfolio(root, _portfolio())


def _budget(root: str, day: date,
            now: datetime | None = None) -> scheduler.DayBudget:
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    return scheduler.day_budget(portfolio, day,
                                now=now or datetime(2026, 10, 14, 9, 0))


# --- model --------------------------------------------------------------------


def test_calendar_event_recurrence_validation() -> None:
    model.CalendarEvent(event_id="e", title="x", day="2026-10-14",
                        start_minutes=540, end_minutes=600,
                        recurrence="WEEKLY").validate()
    with pytest.raises(model.MvpError):
        model.CalendarEvent(event_id="e", title="x", day="2026-10-14",
                            start_minutes=540, end_minutes=600,
                            recurrence="DAILY").validate()
    with pytest.raises(model.MvpError):
        model.CalendarEvent(event_id="e", title="x", day="2026-10-14",
                            start_minutes=600, end_minutes=540).validate()


def test_capacity_working_days_validation() -> None:
    assert model.Capacity().working_days == (0, 1, 2, 3, 4)
    with pytest.raises(model.MvpError):
        model.Capacity(working_days=()).validate()
    with pytest.raises(model.MvpError):
        model.Capacity(working_days=(0, 0)).validate()
    with pytest.raises(model.MvpError):
        model.Capacity(working_days=(7,)).validate()


def test_portfolio_days_off_defaults_and_backward_compat() -> None:
    data = _portfolio().to_dict()
    data.pop("days_off")
    data["capacity"].pop("working_days")
    loaded = model.Portfolio.from_dict(data)
    assert loaded.days_off == ()
    assert loaded.capacity.working_days == (0, 1, 2, 3, 4)


def test_portfolio_rejects_bad_day_off() -> None:
    with pytest.raises(model.MvpError):
        model.Portfolio(
            schema_version=model.SCHEMA_VERSION, name="t",
            projects=(model.Project(project_id="p1", name="P", objective="o"),),
            tasks=(), days_off=("not-a-date",)).validate()


# --- weekly recurrence --------------------------------------------------------


def test_weekly_event_repeats_on_its_weekday(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.create_event(root, {
        "title": "Standup", "day": WEDNESDAY.isoformat(),
        "start_minutes": 540, "end_minutes": 555, "recurrence": "WEEKLY"})
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    on_wed = scheduler._events_on(portfolio, WEDNESDAY)
    on_next = scheduler._events_on(portfolio, NEXT_WEDNESDAY)
    on_thu = scheduler._events_on(portfolio, THURSDAY)
    assert [e.title for e in on_wed] == ["Standup"]
    assert [e.title for e in on_next] == ["Standup"]
    assert on_thu == ()


def test_one_off_event_does_not_recur(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.create_event(root, {
        "title": "One-off", "day": WEDNESDAY.isoformat(),
        "start_minutes": 540, "end_minutes": 600})
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert scheduler._events_on(portfolio, WEDNESDAY)
    assert scheduler._events_on(portfolio, NEXT_WEDNESDAY) == ()


def test_weekend_is_not_a_working_day(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    budget = _budget(root, SATURDAY)
    assert budget.is_working_day is False
    assert budget.plan_capacity == 0
    assert budget.remaining_plan_capacity == 0


# --- days off -----------------------------------------------------------------


def test_day_off_zeroes_capacity_but_keeps_events(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.create_event(root, {
        "title": "Meeting", "day": WEDNESDAY.isoformat(),
        "start_minutes": 600, "end_minutes": 660})
    assert _budget(root, WEDNESDAY).is_working_day is True
    mutations.set_day_off(root, WEDNESDAY.isoformat(), off=True)
    budget = _budget(root, WEDNESDAY)
    assert budget.is_working_day is False
    assert budget.plan_capacity == 0
    assert budget.calendar_minutes == 60  # the agenda stays truthful


def test_set_day_off_is_reversible(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.set_day_off(root, WEDNESDAY.isoformat(), off=True)
    result = mutations.set_day_off(root, WEDNESDAY.isoformat(), off=False)
    assert result["days_off"] == []
    assert _budget(root, WEDNESDAY).is_working_day is True


# --- week planning ------------------------------------------------------------


def test_plan_week_spills_around_non_working_days(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    ranked = priority.rank_ready_tasks(portfolio, today=WEDNESDAY)
    week = scheduler.plan_week(portfolio, ranked, WEDNESDAY,
                               now=datetime(2026, 10, 14, 8, 0))
    by_day = {d.day: d for d in week.days}
    assert by_day[SATURDAY.isoformat()].is_working_day is False
    assert by_day[SATURDAY.isoformat()].planned_minutes == 0
    assert by_day[THURSDAY.isoformat()].planned_minutes == 240
    assert sum(d.planned_minutes for d in week.days) == 720


# --- event mutations ----------------------------------------------------------


def test_create_update_delete_event(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    created = mutations.create_event(root, {
        "title": "Sync", "day": WEDNESDAY.isoformat(),
        "start_minutes": 540, "end_minutes": 600})
    event_id = str(created["event_id"])
    mutations.update_event(root, event_id, {"end_minutes": 630})
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    event = next(e for e in portfolio.calendar if e.event_id == event_id)
    assert event.end_minutes == 630
    mutations.delete_event(root, event_id)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.calendar == ()


def test_event_mutations_fail_closed(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(model.MvpError):
        mutations.create_event(root, {
            "title": "Bad", "day": WEDNESDAY.isoformat(),
            "start_minutes": 600, "end_minutes": 540})
    with pytest.raises(model.MvpError):
        mutations.delete_event(root, "missing")
    with pytest.raises(model.MvpError):
        mutations.update_event(root, "missing", {"end_minutes": 600})
