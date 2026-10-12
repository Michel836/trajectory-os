"""Unit tests for MVP outcomes/replanning and persistence."""

from __future__ import annotations

from trajectory_os.mvp import model, outcomes, readiness, store


def _tmp_root(tmp_path: object) -> str:
    return str(tmp_path)


def _portfolio() -> model.Portfolio:
    return model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p1", name="P1",
                                objective="o"),),
        tasks=(
            model.Task(task_id="a", project_id="p1", title="a",
                       estimated_minutes=30),
            model.Task(task_id="b", project_id="p1", title="b",
                       dependencies=("a",), estimated_minutes=20),
        ),
    )


def test_store_round_trip(tmp_path: object) -> None:
    root = _tmp_root(tmp_path)
    store.save_portfolio(root, _portfolio())
    loaded = store.load_portfolio(root)
    assert loaded is not None
    assert loaded.tasks[0].task_id == "a"


def test_apply_outcome_updates_task_and_project(tmp_path: object) -> None:
    root = _tmp_root(tmp_path)
    store.save_portfolio(root, _portfolio())
    result = outcomes.record_and_save(root, task_id="a",
                                      outcome=model.O_COMPLETED,
                                      actual_minutes=40)
    assert result["task_status"] == model.TS_COMPLETED
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    # Completing 'a' releases 'b' (readiness turns READY).
    states = {item.task_id: item for item in readiness.evaluate(portfolio)}
    assert states["b"].ready is True


def test_apply_outcome_derives_project_completed(tmp_path: object) -> None:
    root = _tmp_root(tmp_path)
    store.save_portfolio(root, _portfolio())
    outcomes.record_and_save(root, task_id="a", outcome=model.O_COMPLETED,
                             actual_minutes=40)
    outcomes.record_and_save(root, task_id="b", outcome=model.O_COMPLETED,
                             actual_minutes=25)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.projects[0].status == model.PS_COMPLETED


def test_planned_vs_actual_statistics(tmp_path: object) -> None:
    root = _tmp_root(tmp_path)
    store.save_portfolio(root, _portfolio())
    outcomes.record_and_save(root, task_id="a", outcome=model.O_COMPLETED,
                             actual_minutes=40)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    records = store.read_outcomes(root)
    pva = outcomes.planned_vs_actual(portfolio, records)
    assert pva.completed_with_actual == 1
    assert pva.actual_minutes == 40
    assert pva.mean_absolute_error_minutes == 10.0


def test_record_unknown_task_fails(tmp_path: object) -> None:
    import pytest

    root = _tmp_root(tmp_path)
    store.save_portfolio(root, _portfolio())
    with pytest.raises(model.MvpError) as exc:
        outcomes.record_and_save(root, task_id="nope",
                                 outcome=model.O_COMPLETED)
    assert exc.value.code == model.E_UNKNOWN_ID
