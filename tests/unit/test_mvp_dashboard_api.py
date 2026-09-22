"""Unit tests for the MVP interactive dashboard backend endpoints."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from trajectory_os.mvp import dashboard, model, readiness, store


def _seed(root: str) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p1", name="P1",
                                objective="objective one"),),
        tasks=(
            model.Task(task_id="a", project_id="p1", title="A",
                       estimated_minutes=30),
            model.Task(task_id="b", project_id="p1", title="B",
                       dependencies=("a",), estimated_minutes=20),
            model.Task(task_id="c", project_id="p1", title="C",
                       estimated_minutes=10),
        ),
    ))


@contextmanager
def _server(root: str) -> Iterator[int]:
    app = dashboard.Dashboard(root)
    server = dashboard.create_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, method: str, path: str,
             body: dict[str, object] | None = None
             ) -> tuple[int, dict[str, object]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        data = {"_raw": raw.decode("utf-8", "replace")}
    return response.status, data


def test_get_html_and_health(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, data = _request(port, "GET", "/")
        assert status == 200
        assert "Today" in str(data["_raw"])
        assert "Projects" in str(data["_raw"])
        status, health = _request(port, "GET", "/healthz")
        assert status == 200
        assert health["status"] == "OK"


def test_get_cockpit_and_portfolio(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        _, cockpit = _request(port, "GET", "/api/cockpit")
        assert cockpit["kind"] == "mvp_cockpit"
        assert len(cockpit["projects"]) == 1
        _, portfolio = _request(port, "GET", "/api/portfolio")
        assert len(portfolio["tasks"]) == 3
        assert len(portfolio["projects"]) == 1


def test_create_and_update_project_via_api(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/projects",
                                  {"name": "New", "objective": "obj"})
        assert status == 200
        assert result["project_id"] == "new"
        status, result = _request(port, "POST", "/api/projects/new",
                                  {"status": model.PS_DEFERRED,
                                   "impact": model.I_HIGH})
        assert status == 200
        portfolio = store.load_portfolio(root)
        assert portfolio is not None
        project = portfolio.project_map()["new"]
        assert project.status == model.PS_DEFERRED
        assert project.impact == model.I_HIGH


def test_create_update_task_via_api(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/tasks",
                                  {"project_id": "p1", "title": "Task C",
                                   "estimated_minutes": 15})
        assert status == 200
        task_id = result["task_id"]
        status, result = _request(port, "POST", f"/api/tasks/{task_id}",
                                  {"title": "Task C renamed",
                                   "status": model.TS_IN_PROGRESS})
        assert status == 200
        portfolio = store.load_portfolio(root)
        assert portfolio is not None
        task = portfolio.task_map()[task_id]
        assert task.title == "Task C renamed"
        assert task.status == model.TS_IN_PROGRESS


def test_record_outcome_via_api_recomputes(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(
            port, "POST", "/api/tasks/a/outcome",
            {"outcome": model.O_COMPLETED, "actual_minutes": 40,
             "note": "done early"})
        assert status == 200
        assert result["task_status"] == model.TS_COMPLETED
        portfolio = store.load_portfolio(root)
        assert portfolio is not None
        states = {item.task_id: item for item in readiness.evaluate(portfolio)}
        assert states["b"].ready is True
        # outcome history preserved in the ledger
        records = store.read_outcomes(root)
        assert any(r.task_id == "a" and r.outcome == model.O_COMPLETED
                   and r.actual_minutes == 40 for r in records)


def test_legacy_record_endpoint_still_works(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(
            port, "POST", "/api/record",
            {"task_id": "a", "outcome": model.O_DEFERRED})
        assert status == 200
        assert result["task_status"] == model.TS_DEFERRED


def test_add_remove_dependency_via_api(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/tasks/c/dependency",
                                  {"dependency": "a"})
        assert status == 200
        assert result["added"] is True
        portfolio = store.load_portfolio(root)
        assert portfolio is not None
        assert "a" in portfolio.task_map()["c"].dependencies
        status, result = _request(
            port, "DELETE", "/api/tasks/c/dependency/a")
        assert status == 200
        assert result["removed"] is True
        portfolio = store.load_portfolio(root)
        assert portfolio is not None
        assert "a" not in portfolio.task_map()["c"].dependencies


def test_invalid_mutation_rejected_via_api(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        # unknown task
        status, result = _request(port, "POST", "/api/tasks/nope",
                                  {"title": "x"})
        assert status == 409
        assert result["error"] == model.E_UNKNOWN_ID
        # invalid outcome
        status, result = _request(port, "POST", "/api/tasks/a/outcome",
                                  {"outcome": "NOPE"})
        assert status == 409
        assert result["error"] == model.E_MALFORMED
        # unknown field
        status, result = _request(port, "POST", "/api/tasks/a",
                                  {"bogus": True})
        assert status == 409
        assert result["error"] == model.E_MALFORMED
        # self dependency
        status, result = _request(port, "POST", "/api/tasks/a/dependency",
                                  {"dependency": "a"})
        assert status == 409
        assert result["error"] == model.E_SELF_REFERENCE


def test_mutation_persists_after_server_restart(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        _request(port, "POST", "/api/projects",
                 {"name": "Persisted", "objective": "obj"})
        _request(port, "POST", "/api/tasks",
                 {"project_id": "persisted", "title": "Task X"})
    # restart server against the same root
    with _server(root) as port:
        _, portfolio = _request(port, "GET", "/api/portfolio")
        ids = {p["project_id"] for p in portfolio["projects"]}
        assert "persisted" in ids
        assert any(t["project_id"] == "persisted"
                   for t in portfolio["tasks"])


def test_views_endpoint_exposes_visual_payload(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, views = _request(port, "GET", "/api/views")
        assert status == 200
        assert views["kind"] == "mvp_views"
        assert len(views["objects"]) == 4  # 1 project + 3 tasks
        assert views["wbs"]["roots"]
        assert "preferences" in views
        assert views["graph"]["nodes"]
        # every view projection is present in one payload
        for key in ("wbs", "graph", "gantt", "eisenhower",
                    "impact_effort", "portfolio_map", "treemap", "progress",
                    "heatmap", "goal_flow", "attribute_catalog",
                    "colour_modes", "densities", "views"):
            assert key in views, key


def test_preferences_and_highlight_via_api(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/preferences",
                                  {"color_by": "status",
                                   "density": "detailed",
                                   "view": "kanban",
                                   "selected": "task:a"})
        assert status == 200
        assert result["preferences"]["color_by"] == "status"
        status, result = _request(port, "POST", "/api/highlight",
                                  {"object_id": "task:a",
                                   "colour": "yellow"})
        assert status == 200
        assert result["preferences"]["highlights"]["task:a"] == "yellow"
        # selection + highlight survive a restart through /api/views
    with _server(root) as port:
        _, views = _request(port, "GET", "/api/views")
        assert views["preferences"]["color_by"] == "status"
        assert views["preferences"]["selected"] == "task:a"
        assert views["preferences"]["view"] == "kanban"
        assert views["object_index"]["task:a"]["highlight"] == "yellow"


def test_table_state_roundtrip_via_api(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    tables = {
        "projects": {
            "sort": [{"column": "urgency", "dir": "desc"},
                     {"column": "impact", "dir": "desc"}],
            "filters": {"domain": "personal", "progress_min": "10"},
        },
        "tasks": {
            "sort": [{"column": "effort", "dir": "asc"}],
            "filters": {"ready": "yes"},
        },
    }
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/preferences",
                                  {"tables": tables})
        assert status == 200
        assert result["preferences"]["tables"] == tables
    # survives a restart through /api/views
    with _server(root) as port:
        _, views = _request(port, "GET", "/api/views")
        assert views["preferences"]["tables"] == tables
        # presentation-only: no business object changed
        assert views["object_index"]["task:a"]["status"] == model.TS_TODO


def test_table_ui_present_in_html(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, data = _request(port, "GET", "/")
        assert status == 200
        html = str(data["_raw"])
        for marker in ("grid-table", "grid-filter", "tableHeaderClick",
                       "tableClearFilters", "tableClearSort", "tableReset",
                       "tableNextSort", "tableFilterRows"):
            assert marker in html, marker


def test_colour_override_via_api(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/colour",
                                  {"scope": "domain", "key": "personal",
                                   "colour": "#123456"})
        assert status == 200
        assert result["preferences"]["domain_colors"]["personal"] \
            == "#123456"
        status, result = _request(port, "POST", "/api/colour",
                                  {"scope": "bogus", "key": "x",
                                   "colour": "#123456"})
        assert status == 409


def test_invalid_preferences_rejected_via_api(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/preferences",
                                  {"color_by": "nope"})
        assert status == 409
        assert result["error"] == model.E_MALFORMED
        status, result = _request(port, "POST", "/api/highlight",
                                  {"object_id": "task:a",
                                   "colour": "not-a-colour"})
        assert status == 409


def test_visual_endpoints_do_not_mutate_portfolio(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    path = store.default_portfolio_path(root)
    before = path.read_bytes()
    with _server(root) as port:
        _request(port, "GET", "/api/views")
        _request(port, "POST", "/api/preferences", {"view": "graph"})
        _request(port, "POST", "/api/highlight",
                 {"object_id": "task:a", "colour": "blue"})
    assert path.read_bytes() == before
