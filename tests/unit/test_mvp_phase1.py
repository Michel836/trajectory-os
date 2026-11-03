"""Phase 1 — portfolio planifiability: bulk estimates, suggestions, debt view."""

from __future__ import annotations

from datetime import date

import pytest

from trajectory_os.mvp import (
    calibration,
    changes,
    cockpit_ui,
    model,
    mutations,
    store,
    uistate,
    visualization,
)


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="phase1",
        projects=(
            model.Project(project_id="p1", name="P1", objective="o"),
            model.Project(project_id="p2", name="P2", objective="o"),
        ),
        tasks=(
            model.Task(task_id="t1", project_id="p1", title="T1"),
            model.Task(task_id="t2", project_id="p1", title="T2"),
            model.Task(task_id="t3", project_id="p1", title="T3"),
        ),
    ))


def _seed_calibration(root: object) -> None:
    completed = tuple(
        model.Task(task_id=f"done-{index}", project_id="p1",
                   title=f"Done {index}", status=model.TS_COMPLETED,
                   actual_minutes=minutes)
        for index, minutes in enumerate((30, 30, 40, 50, 60)))
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="cal",
        projects=(model.Project(project_id="p1", name="P1", objective="o"),),
        tasks=completed + (
            model.Task(task_id="todo", project_id="p1", title="To estimate"),
        ),
    ))


# --- bulk estimation ---------------------------------------------------------


def test_bulk_estimate_updates_and_journals(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    result = mutations.estimate_tasks(root, [
        {"task_id": "t1", "estimated_minutes": 30},
        {"task_id": "t2", "estimated_minutes": 60},
    ])
    assert result["updated"] == 2
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    tasks = portfolio.task_map()
    assert tasks["t1"].estimated_minutes == 30
    assert tasks["t2"].estimated_minutes == 60
    assert tasks["t3"].estimated_minutes is None
    journal = changes.read_changes(root)
    assert len([c for c in journal if c.kind == changes.KIND_UPDATE]) == 2
    # A second identical batch is a no-op and appends nothing.
    again = mutations.estimate_tasks(root, [
        {"task_id": "t1", "estimated_minutes": 30},
    ])
    assert again["updated"] == 0
    assert len(changes.read_changes(root)) == len(journal)


def test_bulk_estimate_can_clear_and_is_atomic(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.estimate_tasks(root, [{"task_id": "t1",
                                     "estimated_minutes": 45}])
    cleared = mutations.estimate_tasks(root, [{"task_id": "t1",
                                               "estimated_minutes": None}])
    assert cleared["updated"] == 1
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.task_map()["t1"].estimated_minutes is None

    before = store.default_portfolio_path(root).read_bytes()
    with pytest.raises(model.MvpError):
        mutations.estimate_tasks(root, [
            {"task_id": "t2", "estimated_minutes": 30},
            {"task_id": "missing", "estimated_minutes": 30},
        ])
    assert store.default_portfolio_path(root).read_bytes() == before


@pytest.mark.parametrize("updates", [
    [{"task_id": "t1", "estimated_minutes": 0}],
    [{"task_id": "t1", "estimated_minutes": 1441}],
    [{"task_id": "t1", "estimated_minutes": True}],
    [{"task_id": "t1", "estimated_minutes": 30},
     {"task_id": "t1", "estimated_minutes": 60}],
    [{"task_id": "missing", "estimated_minutes": 30}],
])
def test_bulk_estimate_rejects_invalid_input(tmp_path: object,
                                             updates: list[dict[str, object]]
                                             ) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(model.MvpError):
        mutations.estimate_tasks(root, updates)


# --- calibrated suggestions --------------------------------------------------


def test_suggestion_uses_project_median(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed_calibration(root)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    suggestions = calibration.suggestions(portfolio, ())
    assert suggestions["todo"]["scope"] == "project:p1"
    assert suggestions["todo"]["samples"] == 5
    assert suggestions["todo"]["minutes"] == 40


def test_suggestion_requires_minimum_samples(tmp_path: object) -> None:
    root = str(tmp_path)
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="cal",
        projects=(model.Project(project_id="p1", name="P1", objective="o"),),
        tasks=(
            model.Task(task_id="done", project_id="p1", title="Done",
                       status=model.TS_COMPLETED, actual_minutes=30),
            model.Task(task_id="todo", project_id="p1", title="To estimate"),
        ),
    ))
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert calibration.suggestions(portfolio, ()) == {}


def test_views_expose_suggestions_and_debt_view(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed_calibration(root)
    views = visualization.build_views(root, today=date(2026, 1, 10))
    assert views["estimate_suggestions"]["todo"]["minutes"] == 40
    assert "debt" in uistate.VIEWS
    assert "debt:renderDebt" in cockpit_ui.PAGE_JS
    assert "function renderDebt" in cockpit_ui.PAGE_JS
    assert "function applyEstimates" in cockpit_ui.PAGE_JS
    assert "estimate-overlay" in cockpit_ui.PAGE_JS


# --- HTTP endpoint -----------------------------------------------------------


def test_estimate_endpoint(tmp_path: object) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/tasks/estimate", {
            "updates": [
                {"task_id": "t1", "estimated_minutes": 30},
                {"task_id": "t2", "estimated_minutes": 45},
            ],
        })
        assert status == 200
        assert result["updated"] == 2
        portfolio = store.load_portfolio(root)
        assert portfolio is not None
        assert portfolio.task_map()["t2"].estimated_minutes == 45


def test_rename_domain_is_explicit_and_journaled(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    result = mutations.rename_domain(root, old="personal", new="perso")
    assert result["updated"] == 2
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert {p.domain for p in portfolio.projects} == {"perso"}
    assert any(change.summary == "domaine renommé"
               for change in changes.read_changes(root))
    assert mutations.rename_domain(root, old="missing", new="x")["updated"] == 0


def test_rename_domain_endpoint(tmp_path: object) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/domains/rename", {
            "from": "personal", "to": "perso",
        })
        assert status == 200
        assert result["updated"] == 2
        portfolio = store.load_portfolio(root)
        assert portfolio is not None
        assert {p.domain for p in portfolio.projects} == {"perso"}
