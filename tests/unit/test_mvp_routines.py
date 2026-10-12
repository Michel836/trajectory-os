"""Tests for routines (B2): recurring templates materialized idempotently."""

from __future__ import annotations

from datetime import date

import pytest

from trajectory_os.mvp import model, mutations, store

MONDAY = date(2026, 10, 12)
TUESDAY = date(2026, 10, 13)


def _seed(root: str) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p1", name="P1", objective="o"),),
        tasks=(),
    ))


def test_routine_due_on() -> None:
    routine = model.Routine(routine_id="r", title="Weekly review",
                            project_id="p1", weekdays=(0,)).validate()
    assert routine.due_on(MONDAY) is True
    assert routine.due_on(TUESDAY) is False
    inactive = model.Routine(routine_id="r", title="x", project_id="p1",
                             weekdays=(0,), active=False).validate()
    assert inactive.due_on(MONDAY) is False


def test_routine_validation() -> None:
    with pytest.raises(model.MvpError):
        model.Routine(routine_id="r", title="x", project_id="p1",
                      weekdays=()).validate()
    with pytest.raises(model.MvpError):
        model.Routine(routine_id="r", title="x", project_id="p1",
                      weekdays=(0, 0)).validate()
    with pytest.raises(model.MvpError):
        model.Routine(routine_id="r", title="x", project_id="p1",
                      weekdays=(7,)).validate()


def test_create_and_materialize_idempotent(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.create_routine(root, {
        "title": "Weekly review", "project_id": "p1", "weekdays": [0],
        "estimated_minutes": 30})
    first = mutations.materialize_routines(root, MONDAY.isoformat())
    assert first["created_tasks"] == 1
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    task = portfolio.task_map()[first["task_ids"][0]]
    assert task.title == "Weekly review"
    assert task.estimated_minutes == 30
    # Re-running is a no-op.
    second = mutations.materialize_routines(root, MONDAY.isoformat())
    assert second["created_tasks"] == 0


def test_materialize_skips_non_due_day(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.create_routine(root, {
        "title": "Weekly review", "project_id": "p1", "weekdays": [0]})
    result = mutations.materialize_routines(root, TUESDAY.isoformat())
    assert result["created_tasks"] == 0


def test_update_and_delete_routine(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    created = mutations.create_routine(root, {
        "title": "Standup", "project_id": "p1", "weekdays": [0, 2, 4]})
    routine_id = str(created["routine_id"])
    mutations.update_routine(root, routine_id, {"active": False})
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.routines[0].active is False
    assert mutations.materialize_routines(root, MONDAY.isoformat()) == {
        "day": MONDAY.isoformat(), "created_tasks": 0, "task_ids": []}
    mutations.delete_routine(root, routine_id)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.routines == ()


def test_create_routine_unknown_project(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(model.MvpError) as exc:
        mutations.create_routine(root, {
            "title": "x", "project_id": "missing", "weekdays": [0]})
    assert exc.value.code == model.E_UNKNOWN_PROJECT


def test_portfolio_without_routines_loads(tmp_path: object) -> None:
    data = model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p1", name="P", objective="o"),),
        tasks=()).to_dict()
    data.pop("routines")
    loaded = model.Portfolio.from_dict(data)
    assert loaded.routines == ()
