"""Phase 2.2/2.3 — trash, generic undo, backups and the security surface."""

from __future__ import annotations

import pytest

from trajectory_os.mvp import (
    capture,
    changes,
    cockpit_ui,
    model,
    mutations,
    outcomes,
    store,
    trash,
    uistate,
)


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="phase2",
        projects=(model.Project(project_id="p1", name="P1", objective="o"),),
        tasks=(
            model.Task(task_id="t1", project_id="p1", title="T1"),
            model.Task(task_id="t2", project_id="p1", title="T2",
                       dependencies=("t1",)),
        ),
    ))


def test_trash_task_cleans_and_restores_dependencies(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    entry = trash.trash_task(root, "t1")
    assert entry["kind"] == "task"
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert "t1" not in portfolio.task_map()
    assert portfolio.task_map()["t2"].dependencies == ()
    assert any(change.kind == changes.KIND_DELETE
               for change in changes.read_changes(root))

    restored = trash.restore_trash(root, str(entry["trash_id"]))
    assert restored["restored"] == "t1"
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert "t1" in portfolio.task_map()
    assert portfolio.task_map()["t2"].dependencies == ("t1",)
    assert trash.list_trash(root) == ()


def test_trash_project_requires_explicit_cascade(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(trash.TrashError):
        trash.trash_project(root, "p1")
    entry = trash.trash_project(root, "p1", cascade=True)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert "p1" not in portfolio.project_map()
    restored = trash.restore_trash(root, str(entry["trash_id"]))
    assert restored["kind"] == "project"
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert "p1" in portfolio.project_map()
    assert set(portfolio.task_map()) == {"t1", "t2"}
    assert portfolio.task_map()["t2"].dependencies == ("t1",)


def test_generic_undo_restores_an_update(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.update_task(root, "t1", {"estimated_minutes": 30})
    update = next(change for change in changes.read_changes(root)
                  if change.kind == changes.KIND_UPDATE)
    result = changes.undo_change(root, update.change_id)
    assert result["restored"] == "t1"
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.task_map()["t1"].estimated_minutes is None


def test_generic_undo_restores_an_outcome(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    result = outcomes.record_and_save(
        root, task_id="t1", outcome=model.O_COMPLETED, actual_minutes=12)
    undone = changes.undo_change(root, str(result["change_id"]))
    assert undone["restored_status"] == model.TS_TODO
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.task_map()["t1"].status == model.TS_TODO


def test_generic_undo_trashes_a_created_task(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    created = mutations.create_task(root, {
        "project_id": "p1", "title": "Created task"})
    create = next(change for change in changes.read_changes(root)
                  if change.kind == changes.KIND_CREATE
                  and change.object_id == created["task_id"])
    changes.undo_change(root, create.change_id)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert created["task_id"] not in portfolio.task_map()
    assert any(entry.object_id == created["task_id"]
               for entry in trash.list_trash(root))


def test_generic_undo_reverts_a_capture_batch(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    text = "preparer le dossier alpha"
    preview = capture.preview(root, text)
    result = capture.confirm(root, text, [{
        "capture_id": preview.items[0].capture_id,
        "action": capture.ACT_ACCEPT,
        "kind": "task",
        "project_id": "p1",
    }])
    assert result["created_tasks"] == 1
    created_id = str(result["task_ids"][0])
    batch = next(change for change in changes.read_changes(root)
                 if change.kind == changes.KIND_CAPTURE)
    changes.undo_change(root, batch.change_id)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert created_id not in portfolio.task_map()
    assert any(entry.object_id == created_id for entry in trash.list_trash(root))


def test_trash_view_is_wired() -> None:
    assert "trash" in uistate.VIEWS
    js = cockpit_ui.PAGE_JS
    for marker in ("trash:renderTrash", "function renderTrash",
                   "function restoreTrash", "function backupNow",
                   "function undoChange", "Corbeille"):
        assert marker in js, marker


def test_backup_and_trash_endpoints(tmp_path: object) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, created = _request(port, "POST", "/api/trash", {
            "kind": "task", "ref_id": "t1",
        })
        assert status == 200
        status, listing = _request(port, "GET", "/api/trash")
        assert status == 200
        entry = listing["trash"][0]
        assert entry["object_id"] == "t1"
        status, restored = _request(
            port, "POST", f"/api/trash/{entry['trash_id']}/restore", {})
        assert status == 200
        assert restored["restored"] == "t1"

        status, backup = _request(port, "POST", "/api/backups", {})
        assert status == 200
        assert backup["name"].startswith("portfolio-")
        status, backups = _request(port, "GET", "/api/backups")
        assert status == 200
        assert backups["backups"]
        status, restored_backup = _request(
            port, "POST", "/api/backups/restore",
            {"name": backups["backups"][0]["name"]})
        assert status == 200
        assert restored_backup["restored"] == backups["backups"][0]["name"]


def test_global_search_is_filter_independent(tmp_path: object) -> None:
    from trajectory_os.mvp import search

    root = str(tmp_path)
    _seed(root)
    result = search.search(root, "T1")
    assert result["count"] >= 1
    assert any(item["kind"] == "task" and item["ref_id"] == "t1"
               for item in result["results"])
    assert search.search(root, "")["count"] == 0


def test_search_and_command_palette_are_wired(tmp_path: object) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "GET", "/api/search?q=T1")
        assert status == 200
        assert result["count"] >= 1
    js = cockpit_ui.PAGE_JS
    for marker in ("openCommandPalette", "commandInput", "commandKey",
                   "command-overlay", "runCommandIndex"):
        assert marker in js, marker
