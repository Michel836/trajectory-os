"""Inspired patterns — guided review, split inbox, updates and activity."""

from __future__ import annotations

from datetime import date

from trajectory_os.mvp import (
    changes,
    cockpit_ui,
    model,
    project_review,
    store,
)


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="patterns",
        projects=(
            model.Project(project_id="p1", name="At risk", objective="o",
                          deadline="2026-01-12"),
            model.Project(project_id="p2", name="Stale", objective="o"),
            model.Project(project_id="p3", name="Empty", objective="o"),
        ),
        tasks=(
            model.Task(task_id="t1", project_id="p1", title="T1"),
            model.Task(task_id="t2", project_id="p2", title="T2",
                       updated_at="2025-01-01T00:00:00Z"),
        ),
    ))


def test_project_review_queue_is_ordered_by_attention(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    queue = project_review.build_queue(root, today=date(2026, 1, 10))
    assert queue["projects"][0]["project_id"] == "p1"
    assert queue["projects"][0]["health"] == "AT_RISK"
    assert queue["counts"]["AT_RISK"] == 1
    assert queue["counts"]["AT_RISK"] == 1


def test_project_review_decisions(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    result = project_review.decide(root, "p1", "DEFER", note="later")
    assert result["status"] == model.PS_DEFERRED
    result = project_review.decide(root, "p1", "KEEP")
    assert result["status"] == model.PS_ACTIVE
    result = project_review.decide(root, "p3", "PARK_IDEA")
    assert result["status"] == model.PS_IDEA
    result = project_review.decide(root, "p1", "CLOSE")
    assert result["status"] == model.PS_COMPLETED
    assert any("revue projet" in change.summary
               for change in changes.read_changes(root))


def test_project_review_update_and_timeline_endpoints(tmp_path: object) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, queue = _request(port, "GET", "/api/projects/review")
        assert status == 200
        assert queue["projects"]
        status, result = _request(port, "POST", "/api/projects/p1/review", {
            "decision": "DEFER", "note": "plus tard"})
        assert status == 200
        assert result["status"] == model.PS_DEFERRED
        status, update = _request(port, "POST", "/api/projects/p1/update", {
            "state": "AT_RISK", "note": "en attente du notaire",
            "risks": "échéance proche", "decision": "continuer",
            "next_step": "relancer"})
        assert status == 200
        assert update["update"]["summary"] == "mise à jour de projet"
        status, timeline = _request(port, "GET",
                                    "/api/projects/p1/timeline")
        assert status == 200
        assert timeline["entries"]


def test_patterns_ui_is_wired() -> None:
    js = cockpit_ui.PAGE_JS
    for marker in ("renderProjectReview", "reviewProjectDecision",
                   "renderIngestionQueue", "signalFilterChange",
                   "openProjectUpdate", "saveProjectUpdate",
                   "objectActivity", "shortcutAllowed", "openHelp",
                   "help-overlay", "update-overlay"):
        assert marker in js, marker
