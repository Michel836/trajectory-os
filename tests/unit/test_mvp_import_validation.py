"""Focused tests for pre-confirm import validation and orphan resolution.

All fixtures are synthetic and generic: a small hierarchy with three structural
orphan gaps plus one contiguous projectless group. No real backlog text.
"""

from __future__ import annotations

import http.client
import inspect
import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import pytest

from trajectory_os.mvp import cockpit_ui, dashboard, importer, model, store


def _cand(
    cid: str,
    kind: str,
    title: str,
    *,
    parent: str | None = None,
    project_hint: str = "",
    merge: str | None = None,
    source_text: str = "",
    confidence: float = 0.5,
) -> importer.ImportCandidate:
    return importer.ImportCandidate(
        candidate_id=cid, kind=kind, title=title, description="",
        source_text=source_text or title, project_hint=project_hint,
        parent_id=parent, evidence=importer.FACT,
        merge_project_id=merge, suggested_status=None,
        suggested_urgency=None, suggested_impact=None,
        suggested_effort_minutes=None, confidence=confidence,
        needs_review=False, suggested_dependencies=(),
        suggested_children=(), suggested_next_action="", prerequisites=())


def _analysis(
    candidates: Sequence[importer.ImportCandidate],
) -> importer.ImportAnalysis:
    return importer.ImportAnalysis(
        source_name="synthetic.md", imported_at="2026-01-01T00:00:00Z",
        text_chars=0, truncated=False, engine="factual", warning="",
        candidates=tuple(candidates), matches=(),
        mode=importer.MODE_FACTUAL)


def _fixture() -> importer.ImportAnalysis:
    """3 hierarchy gaps (A/B/C) + 1 contiguous projectless group."""
    return _analysis([
        _cand("pa", importer.PROJECT, "Alpha Project"),
        _cand("a1", importer.TASK, "Alpha setup", parent="pa"),
        _cand("a2", importer.TASK, "Alpha orphan"),
        _cand("a3", importer.TASK, "Alpha verify", parent="pa"),
        _cand("pb", importer.AREA, "Beta Area"),
        _cand("b1", importer.TASK, "Beta draft", parent="pb"),
        _cand("b2", importer.TASK, "Beta orphan"),
        _cand("b3", importer.TASK, "Beta publish", parent="pb"),
        _cand("pc", importer.PROJECT, "Gamma Project"),
        _cand("c1", importer.TASK, "Gamma build", parent="pc"),
        _cand("c2", importer.TASK, "Gamma orphan"),
        _cand("r1", importer.RESOURCE, "Gamma notes"),
        _cand("q1", importer.QUESTION, "What next"),
        _cand("g1", importer.TASK, "Refactor search index"),
        _cand("g2", importer.TASK, "Refactor search pipeline"),
        _cand("g3", importer.TASK, "Refactor search tests"),
    ])


def _all_import(analysis: importer.ImportAnalysis) -> list[dict[str, object]]:
    return [{"candidate_id": c.candidate_id, "action": "import"}
            for c in analysis.candidates]


def _seed(root: str) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="base-project", name="Base Project",
                                objective="do things"),),
        tasks=()))


# --- A — hierarchy-gap task resolves to same-draft project candidate ----------


def test_hierarchy_gaps_resolve_to_same_draft_projects(tmp_path: object) -> None:
    _seed(str(tmp_path))
    analysis = _fixture()
    report = importer.validate_decisions(
        str(tmp_path), analysis, _all_import(analysis))
    repaired = {r["candidate_id"]: r["parent_candidate_id"]
                for r in report["repaired"]}
    assert repaired == {"a2": "pa", "b2": "pb", "c2": "pc"}
    unresolved_ids = {u["candidate_id"] for u in report["unresolved"]}
    assert {"a2", "b2", "c2"}.isdisjoint(unresolved_ids)


def test_infer_orphan_parents_is_structural_only() -> None:
    analysis = _fixture()
    decisions = [importer.ImportDecision(candidate_id=c.candidate_id,
                                         action=importer.ACTION_IMPORT)
                 for c in analysis.candidates]
    inferred = importer.infer_orphan_parents(analysis, decisions)
    assert inferred == {"a2": "pa", "b2": "pb", "c2": "pc"}
    # no existing parent is ever rewritten
    assert "a1" not in inferred and "a3" not in inferred


# --- B — unresolved root task detected before mutation ------------------------


def test_unresolved_root_task_detected(tmp_path: object) -> None:
    _seed(str(tmp_path))
    analysis = _fixture()
    report = importer.validate_decisions(
        str(tmp_path), analysis, _all_import(analysis))
    assert report["ok"] is False
    reasons = {u["candidate_id"]: u["reason"] for u in report["unresolved"]}
    assert reasons == {"g1": "missing_project", "g2": "missing_project",
                       "g3": "missing_project"}


# --- C — grouped projectless tasks become one suggested project ---------------


def test_projectless_group_is_one_suggestion(tmp_path: object) -> None:
    _seed(str(tmp_path))
    analysis = _fixture()
    report = importer.validate_decisions(
        str(tmp_path), analysis, _all_import(analysis))
    assert len(report["groups"]) == 1
    group = report["groups"][0]
    assert group["candidate_ids"] == ["g1", "g2", "g3"]
    assert group["proposed_title"] == "Refactor Search"
    assert group["confidence"] > 0
    assert all(u["group_id"] == group["group_id"]
               for u in report["unresolved"])


# --- D — validation never mutates the portfolio -------------------------------


def test_validation_does_not_mutate_portfolio(tmp_path: object) -> None:
    _seed(str(tmp_path))
    before = store.default_portfolio_path(str(tmp_path)).read_bytes()
    analysis = _fixture()
    importer.validate_decisions(str(tmp_path), analysis, _all_import(analysis))
    assert store.default_portfolio_path(str(tmp_path)).read_bytes() == before


# --- G/H/I — existing valid paths ---------------------------------------------


def test_valid_confirm_path_still_works(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    analysis = _analysis([
        _cand("p1", importer.PROJECT, "Valid Project"),
        _cand("t1", importer.TASK, "Valid task", parent="p1"),
    ])
    decisions = _all_import(analysis)
    draft_id = importer.save_draft(root, analysis)
    app = dashboard.Dashboard(root)
    report = app.validate_import({"draft_id": draft_id,
                                  "decisions": decisions})
    assert report["ok"] is True
    result = app.confirm_import({"draft_id": draft_id, "decisions": decisions})
    assert result["imported"] >= 1
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert any(t.title == "Valid task" for t in portfolio.tasks)


def test_merge_candidates_still_resolve(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    analysis = _analysis([
        _cand("m1", importer.PROJECT, "Merge Me",
              merge="base-project"),
    ])
    report = importer.validate_decisions(root, analysis, _all_import(analysis))
    assert report["ok"] is True
    assert report["summary"]["merges"] == 1
    assert not report["unresolved"]


def test_skip_candidates_do_not_require_project(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    analysis = _analysis([_cand("s1", importer.TASK, "Loose task")])
    decisions = [{"candidate_id": "s1", "action": "skip"}]
    report = importer.validate_decisions(root, analysis, decisions)
    assert report["ok"] is True
    assert report["unresolved"] == []


# --- E — confirm cannot proceed with unresolved blockers ----------------------


def test_confirm_fails_closed_with_unresolved(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    analysis = _fixture()
    before = store.default_portfolio_path(root).read_bytes()
    draft_id = importer.save_draft(root, analysis)
    app = dashboard.Dashboard(root)
    with pytest.raises(importer.ImportValidationError) as excinfo:
        app.confirm_import({"draft_id": draft_id,
                            "decisions": _all_import(analysis)})
    payload = excinfo.value.payload
    assert payload["ok"] is False
    assert len(payload["unresolved"]) == 3
    assert store.default_portfolio_path(root).read_bytes() == before


# --- Grouped new-project / existing-project resolutions -----------------------


def test_group_new_project_resolution_creates_project(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    analysis = _fixture()
    decisions = _all_import(analysis)
    for decision in decisions:
        if decision["candidate_id"] in {"g1", "g2", "g3"}:
            decision["project_id"] = "new:g1"
            decision["new_project_title"] = "Refactor Search"
    report = importer.validate_decisions(root, analysis, decisions)
    assert report["ok"] is True
    new_portfolio, entity_map = importer.apply_decisions(
        store.load_portfolio(root), analysis,
        importer.parse_decisions(decisions))
    titles = {p.name for p in new_portfolio.projects}
    assert "Refactor Search" in titles
    assert sum(1 for t in new_portfolio.tasks
               if t.project_id.startswith("refactor-search")) == 3


def test_group_existing_project_resolution(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    analysis = _fixture()
    decisions = _all_import(analysis)
    for decision in decisions:
        if decision["candidate_id"] in {"g1", "g2", "g3"}:
            decision["project_id"] = "base-project"
    report = importer.validate_decisions(root, analysis, decisions)
    assert report["ok"] is True
    assert not report["unresolved"]


# --- F — structured error payload + in-modal UI rendering ---------------------


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
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
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


def test_validate_endpoint_returns_structured_payload(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    analysis = _fixture()
    draft_id = importer.save_draft(root, analysis)
    with _server(root) as port:
        status, body = _request(port, "POST", "/api/import/validate", {
            "draft_id": draft_id, "decisions": _all_import(analysis)})
    assert status == 200
    assert body["ok"] is False
    assert body["draft_id"] == draft_id
    assert isinstance(body["unresolved"], list)
    assert body["groups"] and body["groups"][0]["proposed_title"]


def test_confirm_endpoint_conflict_carries_unresolved(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    analysis = _fixture()
    draft_id = importer.save_draft(root, analysis)
    with _server(root) as port:
        status, body = _request(port, "POST", "/api/import/confirm", {
            "draft_id": draft_id, "decisions": _all_import(analysis)})
    assert status == 409
    assert body["error"] == "IMPORT_UNRESOLVED"
    assert len(body["unresolved"]) == 3


def test_draft_list_and_reopen_endpoints(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    analysis = _fixture()
    draft_id = importer.save_draft(root, analysis)
    with _server(root) as port:
        status, body = _request(port, "GET", "/api/import/drafts")
        assert status == 200
        ids = [d["draft_id"] for d in body["drafts"]]
        assert draft_id in ids
        status, detail = _request(
            port, "GET", f"/api/import/drafts/{draft_id}")
    assert status == 200
    assert detail["draft_id"] == draft_id
    assert len(detail["analysis"]["candidates"]) == len(
        analysis.candidates)


def test_import_ui_shows_blockers_inside_modal() -> None:
    js = cockpit_ui.PAGE_JS
    assert "renderImportBlockers" in js
    assert "renderImportReady" in js
    assert "importError" in js
    assert "import-validation" in js
    assert "collectImportDecisions" in js
    assert "assignGroupNew" in js
    assert "skipGroup" in js
    # errors render into the modal container, not only #notice
    assert "$('import-validation').innerHTML" in js
    # the modal markup carries the inline blocker/validation container
    source = inspect.getsource(cockpit_ui.render_page)
    assert "id='import-validation'" in source


def test_embedded_js_still_valid(tmp_path: object) -> None:
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = Path(str(tmp_path)) / "cockpit.js"
    script.write_text(cockpit_ui.PAGE_JS, encoding="utf-8")
    result = subprocess.run([node, "--check", str(script)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
