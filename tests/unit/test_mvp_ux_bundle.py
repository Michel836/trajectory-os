"""UX consolidation, decision workflow, handoff and dogfood tests.

Covers the next-phase bundle added on top of the V0 cockpit:

* global search is a pure, testable, presentation-only filter;
* saved views capture and restore presentation state only (never data);
* Today / Ready expose the deterministic decision workflow;
* the Super Productivity handoff has a dry-run preview and stable ids;
* Quick Capture endpoints preview then confirm;
* a large synthetic portfolio stays responsive (dogfood).
"""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from trajectory_os.mvp import (
    cockpit_ui,
    model,
    store,
    superproductivity,
    uistate,
)


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(
            model.Project(project_id="p1", name="P1", objective="objective one",
                          domain="career", urgency=model.U_HIGH,
                          impact=model.I_HIGH),
            model.Project(project_id="p2", name="P2", objective="objective two",
                          domain="admin"),
        ),
        tasks=(
            model.Task(task_id="a", project_id="p1", title="Alpha task",
                       estimated_minutes=30, urgency=model.U_HIGH,
                       impact=model.I_HIGH),
            model.Task(task_id="b", project_id="p1", title="Beta task",
                       dependencies=("a",), estimated_minutes=20),
            model.Task(task_id="c", project_id="p2", title="Gamma task",
                       estimated_minutes=10,
                       waiting_for=("external party",)),
            model.Task(task_id="d", project_id="p2", title="Delta task",
                       estimated_minutes=15, status=model.TS_DEFERRED),
        ),
    ))


@contextmanager
def _server(root: str) -> Iterator[int]:
    from trajectory_os.mvp import dashboard

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
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
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


# --- global search (pure JS helper) ------------------------------------------


def _node_assert(tmp_path: Path, body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "search_helpers.js"
    script.write_text(cockpit_ui.TABLE_JS_HELPERS + "\n" + body,
                      encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_search_is_case_insensitive_and_token_based(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const o={title:'Update the plans',project:'Garden planning',domain:'career',
  workstream:'positioning',deliverable:'cv',description:'refresh roles',
  objective:'find a role',next_action:'rewrite',status:'TODO',
  evidence:'FACT',ref_id:'update-cv'};
assert.strictEqual(searchMatches(o,searchTermsOf('update')),true);
assert.strictEqual(searchMatches(o,searchTermsOf('GARDEN')),true);
assert.strictEqual(searchMatches(o,searchTermsOf('career rewrite')),true);
assert.strictEqual(searchMatches(o,searchTermsOf('renewal')),false);
assert.strictEqual(searchMatches(o,searchTermsOf('')),true);
assert.strictEqual(searchMatches(null,searchTermsOf('x')),false);
""")


def test_search_ui_present_in_html(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, data = _request(port, "GET", "/")
        assert status == 200
        html = str(data["_raw"])
        for marker in ("f-search", "searchInput", "searchMatches",
                       "savedViewOptions", "savedViewChange",
                       "saveCurrentView", "renameCurrentView",
                       "deleteCurrentView"):
            assert marker in html, marker


# --- saved views --------------------------------------------------------------


def test_saved_views_roundtrip_and_rename_delete(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    portfolio_path = store.default_portfolio_path(root)
    before = portfolio_path.read_bytes()
    tables = {"projects": {"sort": [{"column": "urgency", "dir": "desc"}],
                           "filters": {"domain": "career"}},
              "tasks": {"sort": [], "filters": {}}}
    with _server(root) as port:
        status, _ = _request(port, "POST", "/api/preferences", {
            "view": "graph", "color_by": "status", "density": "detailed",
            "filters": {"scope": "domain", "scope_id": "career",
                        "level": "tasks"},
            "tables": tables})
        assert status == 200
        status, result = _request(port, "POST", "/api/saved-views",
                                  {"name": "Career"})
        assert status == 200
        assert "Career" in result["preferences"]["saved_views"]
        assert result["preferences"]["saved_views"]["Career"]["view"] \
            == "graph"
        # mutate the live preferences, then restore the saved view
        _request(port, "POST", "/api/preferences",
                 {"view": "kanban", "density": "compact"})
        status, result = _request(port, "POST", "/api/saved-views/load",
                                  {"name": "Career"})
        assert status == 200
        prefs = result["preferences"]
        assert prefs["view"] == "graph"
        assert prefs["density"] == "detailed"
        assert prefs["color_by"] == "status"
        assert prefs["tables"] == tables
        assert prefs["filters"]["scope"] == "domain"
        status, result = _request(port, "POST", "/api/saved-views/rename",
                                  {"name": "Career", "new_name": "Focus"})
        assert "Focus" in result["preferences"]["saved_views"]
        assert "Career" not in result["preferences"]["saved_views"]
        _, listing = _request(port, "GET", "/api/saved-views")
        assert "Focus" in listing["saved_views"]
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/saved-views/delete",
                                  {"name": "Focus"})
        assert status == 200
        assert result["preferences"]["saved_views"] == {}
    # presentation-only: the portfolio was never touched
    assert portfolio_path.read_bytes() == before


def test_saved_views_validation(tmp_path: object) -> None:
    prefs = uistate.default_preferences()
    with pytest.raises(uistate.UIStateError):
        uistate.save_view(prefs, "")
    with pytest.raises(uistate.UIStateError):
        uistate.save_view(prefs, "x" * 200)
    with pytest.raises(uistate.UIStateError):
        uistate.load_view(prefs, "missing")
    saved = uistate.save_view(prefs, "A")
    renamed = uistate.rename_view(saved, "A", "B")
    assert "B" in renamed.saved_views and "A" not in renamed.saved_views
    with pytest.raises(uistate.UIStateError):
        uistate.delete_view(renamed, "A")


def test_saved_views_bounded_count() -> None:
    prefs = uistate.default_preferences()
    for index in range(32):
        prefs = uistate.save_view(prefs, f"v{index}")
    with pytest.raises(uistate.UIStateError):
        uistate.save_view(prefs, "overflow")
    # overwriting an existing one is always allowed
    assert uistate.save_view(prefs, "v0").saved_views["v0"]


def test_saved_views_are_fail_soft_on_corrupt_state(tmp_path: object) -> None:
    root = str(tmp_path)
    Path(root).mkdir(parents=True, exist_ok=True)
    uistate.ui_state_path(root).write_text(json.dumps({
        "saved_views": {"ok": {"view": "list"}, "bad": "not-an-object"},
        "schema_version": 1,
    }), encoding="utf-8")
    prefs = uistate.load_preferences(root)
    assert "ok" in prefs.saved_views
    assert "bad" not in prefs.saved_views


# --- Today / Ready decision workflow -----------------------------------------


def test_views_payload_has_today_and_ready_workflow(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, views = _request(port, "GET", "/api/views")
        assert status == 200
        plan = views["today_plan"]
        assert plan["day"]
        assert isinstance(plan["planned"], list)
        wf = views["ready_workflow"]
        assert wf["groups"]["ready"]
        assert wf["groups"]["blocked"]
        assert wf["groups"]["waiting"]
        assert wf["groups"]["deferred"]
        assert wf["counts"]["ready"] >= 1
        # each blocked entry explains *why* it is blocked
        blocked = wf["groups"]["blocked"][0]
        assert blocked["blocking_kind"] in ("blocker", "dependency",
                                            "resource", "waiting", "project",
                                            "status")
        assert blocked["blocking_reason"]
        assert wf["group_order"][0] == "ready"


def test_today_and_ready_ui_present_in_html(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        _, data = _request(port, "GET", "/")
        html = str(data["_raw"])
        for marker in ("renderToday", "renderReady", "today_plan",
                       "ready_workflow", "openExportPreview",
                       "openOutcomeForm", "openCapture", "confirmCapture"):
            assert marker in html, marker


# --- Super Productivity handoff ----------------------------------------------


def test_export_preview_is_dry_run_and_traceable(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(
            port, "GET",
            "/api/export/superproductivity?ids=task:a,task:b,task:d")
        assert status == 200
        assert result["written"] is False
        assert result["exported"] == 1  # only 'a' is ready
        assert result["skipped_task_ids"] == ["b", "d"]
        doc = result["document"]
        ids = [task["id"] for task in doc["tasks"]]
        assert all(i.startswith("trajectory-mvp-") for i in ids)
        assert len(set(ids)) == len(ids)  # stable ids are unique
        for task in doc["tasks"]:
            assert task["trajectory_os"]["task_id"]
            assert task["trajectory_os"]["project_id"]
    # dry run never writes an export file into the data root
    assert not (Path(root) / "super-productivity.json").exists()


def test_export_selected_writes_explicit_target(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    target = str(Path(root) / "out" / "sp.json")
    result = superproductivity.export_selected(
        root, ["a"], target=target)
    assert result["written"] is True
    assert result["exported"] == 1
    assert Path(target).is_file()


def test_export_ready_dry_run_does_not_write(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    target = str(Path(root) / "sp.json")
    result = superproductivity.export_ready(root, target=target, limit=2,
                                            dry_run=True)
    assert result["written"] is False
    assert not Path(target).exists()


# --- Quick Capture endpoints --------------------------------------------------


def test_capture_endpoints_preview_then_confirm(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    portfolio_path = store.default_portfolio_path(root)
    before = portfolio_path.read_bytes()
    with _server(root) as port:
        status, preview = _request(port, "POST", "/api/capture",
                                   {"text": "Write the release notes\n"})
        assert status == 200
        assert preview["counts"]["items"] == 1
        assert portfolio_path.read_bytes() == before
        item = preview["items"][0]
        status, summary = _request(port, "POST", "/api/capture/confirm", {
            "text": "Write the release notes\n",
            "decisions": [{"capture_id": item["capture_id"],
                           "action": "accept", "project_id": "p1"}]})
        assert status == 200
        assert summary["created_tasks"] == 1
        portfolio = store.load_portfolio(root)
        assert portfolio is not None
        assert any(t.title == "Write the release notes"
                   for t in portfolio.tasks)


def test_capture_rejects_empty_text(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/capture", {"text": "  "})
        assert status == 409
        assert result["error"] == model.E_MALFORMED


# --- on-demand enrichment: edit / reject traceability -------------------------


def test_enrichment_edit_and_reject_are_traceable(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    from trajectory_os.mvp import enrichment

    def llm(system: str, user: str) -> dict[str, object]:
        del system, user
        return {"suggestions": [
            {"kind": "NEXT_ACTION", "title": "Audit repos",
             "detail": "start with the public ones"},
            {"kind": "NEXT_ACTION", "title": "Draft outline",
             "detail": "one page"}]}

    gen = enrichment.generate(root, "p1", enrichment.ENRICH_NEXT_ACTIONS,
                              llm=llm, engine="local")  # type: ignore[arg-type]
    accepted_id = gen.suggestions[0].suggestion_id
    rejected_id = gen.suggestions[1].suggestion_id
    before = store.load_portfolio(root)
    assert before is not None
    enrichment.reject(root, "p1", rejected_id)
    after_reject = store.load_portfolio(root)
    assert after_reject is not None
    assert len(after_reject.tasks) == len(before.tasks)
    enrichment.accept(root, "p1", accepted_id,
                      title="Audit the public repos first")
    after_accept = store.load_portfolio(root)
    assert after_accept is not None
    assert any(t.title == "Audit the public repos first"
               for t in after_accept.tasks)
    # the original suggestion records remain traceable in the sidecar
    stored = {s.suggestion_id: s
              for s in enrichment.load_suggestions(root, "p1")}
    assert stored[accepted_id].state == enrichment.ACCEPTED
    assert stored[accepted_id].title == "Audit the public repos first"
    assert stored[rejected_id].state == enrichment.REJECTED


# --- dogfood: large portfolio stays responsive --------------------------------


def _build_large_portfolio(root: object, *, projects: int = 30,
                           tasks: int = 450) -> None:
    project_rows = tuple(
        model.Project(project_id=f"px{i}", name=f"Project {i:02d}",
                      objective=f"objective {i}",
                      domain=f"domain{i % 6}",
                      urgency=[model.U_CRITICAL, model.U_HIGH,
                               model.U_MEDIUM, model.U_LOW][i % 4],
                      impact=[model.I_HIGH, model.I_MEDIUM,
                              model.I_LOW][i % 3])
        for i in range(projects))
    task_rows = tuple(
        model.Task(
            task_id=f"tx{i}",
            project_id=f"px{i % projects}",
            title=f"Task {i:03d} do something useful",
            estimated_minutes=15 + (i % 8) * 15,
            urgency=[model.U_CRITICAL, model.U_HIGH, model.U_MEDIUM,
                     model.U_LOW][i % 4],
            impact=[model.I_HIGH, model.I_MEDIUM, model.I_LOW][i % 3],
            dependencies=(f"tx{i - 1}",) if i % 5 == 0 and i > 0 else (),
            status=model.TS_DEFERRED if i % 17 == 0 else model.TS_TODO,
        )
        for i in range(tasks))
    store.save_portfolio(str(root), model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="large",
        projects=project_rows, tasks=task_rows,
        capacity=model.Capacity(minutes_per_day=360, buffer_ratio=0.3)))


def test_dogfood_large_portfolio_views_and_capture_are_responsive(
        tmp_path: object) -> None:
    root = str(tmp_path)
    _build_large_portfolio(root)
    with _server(root) as port:
        started = time.perf_counter()
        status, views = _request(port, "GET", "/api/views")
        elapsed = time.perf_counter() - started
        assert status == 200
        assert len(views["objects"]) == 480  # 30 projects + 450 tasks
        assert elapsed < 5.0, f"/api/views too slow: {elapsed:.2f}s"
        wf = views["ready_workflow"]
        assert wf["counts"]["ready"] > 0
        assert wf["counts"]["deferred"] == len(
            [t for t in views["objects"]
             if t["kind"] == "task" and t["status"] == model.TS_DEFERRED])
        # sorting/filtering is client-side; the payload must round-trip fast
        started = time.perf_counter()
        status, _ = _request(port, "POST", "/api/preferences", {
            "tables": {"tasks": {"sort": [
                {"column": "urgency", "dir": "desc"},
                {"column": "effort", "dir": "asc"}],
                "filters": {"ready": "yes"}}}})
        assert status == 200
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"preferences write too slow: {elapsed:.2f}s"
    # global capture over a large portfolio stays lightweight
    from trajectory_os.mvp import capture

    text = "\n".join(f"Call the contact {i}" for i in range(50))
    started = time.perf_counter()
    preview = capture.preview(root, text)
    elapsed = time.perf_counter() - started
    assert len(preview.items) == 50
    assert elapsed < 3.0, f"capture too slow: {elapsed:.2f}s"
