"""Unit tests for the MVP domain model (validation, fail-closed)."""

from __future__ import annotations

import pytest

from trajectory_os.mvp import model


def _portfolio(tasks: tuple[model.Task, ...] = (),
               projects: tuple[model.Project, ...] | None = None) -> model.Portfolio:
    projects = projects or (
        model.Project(project_id="p1", name="Project One",
                      objective="Do the thing"),
    )
    return model.Portfolio(
        schema_version=model.SCHEMA_VERSION,
        name="test",
        projects=projects,
        tasks=tasks,
    )


def test_valid_minimal_portfolio_round_trips() -> None:
    portfolio = _portfolio()
    portfolio.validate()
    data = portfolio.to_dict()
    assert model.Portfolio.from_dict(data) == portfolio


def test_duplicate_task_ids_fail_closed() -> None:
    task = model.Task(task_id="t1", project_id="p1", title="a")
    portfolio = _portfolio(tasks=(task, task))
    with pytest.raises(model.MvpError) as exc:
        portfolio.validate()
    assert exc.value.code == model.E_DUPLICATE_ID


def test_unknown_project_fails_closed() -> None:
    portfolio = _portfolio(tasks=(model.Task(
        task_id="t1", project_id="missing", title="a"),))
    with pytest.raises(model.MvpError) as exc:
        portfolio.validate()
    assert exc.value.code == model.E_UNKNOWN_PROJECT


def test_self_dependency_fails_closed() -> None:
    portfolio = _portfolio(tasks=(model.Task(
        task_id="t1", project_id="p1", title="a",
        dependencies=("t1",)),))
    with pytest.raises(model.MvpError) as exc:
        portfolio.validate()
    assert exc.value.code == model.E_SELF_REFERENCE


def test_dependency_cycle_fails_closed() -> None:
    tasks = (
        model.Task(task_id="t1", project_id="p1", title="a",
                   dependencies=("t2",)),
        model.Task(task_id="t2", project_id="p1", title="b",
                   dependencies=("t1",)),
    )
    with pytest.raises(model.MvpError) as exc:
        _portfolio(tasks=tasks).validate()
    assert exc.value.code == model.E_DEPENDENCY_CYCLE


def test_invalid_deadline_fails_closed() -> None:
    with pytest.raises(model.MvpError) as exc:
        model.Task(task_id="t1", project_id="p1", title="a",
                   deadline="not-a-date").validate()
    assert exc.value.code == model.E_INVALID_DATE


def test_bad_id_fails_closed() -> None:
    with pytest.raises(model.MvpError) as exc:
        model.Task(task_id="Bad ID!", project_id="p1", title="a").validate()
    assert exc.value.code == model.E_MALFORMED


def test_unknown_resource_fails_closed() -> None:
    portfolio = _portfolio(tasks=(model.Task(
        task_id="t1", project_id="p1", title="a",
        resources=("missing",)),))
    with pytest.raises(model.MvpError) as exc:
        portfolio.validate()
    assert exc.value.code == model.E_UNKNOWN_RESOURCE


def test_unsupported_schema_fails_closed() -> None:
    data = _portfolio().to_dict()
    data["schema_version"] = 99
    with pytest.raises(model.MvpError) as exc:
        model.Portfolio.from_dict(data)
    assert exc.value.code == model.E_UNSUPPORTED_VERSION


def test_capacity_buffer_bounds() -> None:
    with pytest.raises(model.MvpError):
        model.Capacity(minutes_per_day=30, buffer_ratio=0.3).validate()
    with pytest.raises(model.MvpError):
        model.Capacity(minutes_per_day=360, buffer_ratio=1.0).validate()


def test_waiting_for_accepts_free_text() -> None:
    task = model.Task(task_id="t1", project_id="p1", title="a",
                      waiting_for=("a third party reply",))
    portfolio = _portfolio(tasks=(task,))
    portfolio.validate()
    assert portfolio.tasks[0].waiting_for == ("a third party reply",)
