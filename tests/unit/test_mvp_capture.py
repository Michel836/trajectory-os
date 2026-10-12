"""Focused tests for MVP Quick Capture (deterministic, preview then confirm).

Quick Capture is a fast, local, deterministic daily-input pipeline. These
tests pin its contract:

* a preview is a pure function of the text plus the current portfolio and
  never mutates ``portfolio.json``;
* classification is explainable (new task / possible project / existing
  match / duplicate) and never invents an LLM response;
* confirming applies *only* the explicitly decided items through the
  validated mutation layer;
* instrumentation is reported (engine, model, elapsed, counts, cost).
"""

from __future__ import annotations

import time

import pytest

from trajectory_os.mvp import capture, model, store


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(
            model.Project(project_id="admin-renewal",
                          name="Generic Administrative Renewal",
                          objective="complete the renewal formalities"),
            model.Project(project_id="garden-planner", name="Garden planning",
                          objective="plan the community garden"),
        ),
        tasks=(
            model.Task(task_id="update-cv", project_id="garden-planner",
                       title="Update the plans", estimated_minutes=60),
            model.Task(task_id="renewal-docs", project_id="admin-renewal",
                       title="Gather renewal documents", estimated_minutes=30),
        ),
    ))


def _portfolio_bytes(root: object) -> bytes:
    return store.default_portfolio_path(root).read_bytes()


def test_preview_does_not_mutate_portfolio(tmp_path: object) -> None:
    _seed(tmp_path)
    before = _portfolio_bytes(tmp_path)
    result = capture.preview(str(tmp_path), "Book the room\nCall the registrar\n")
    assert result.items
    assert _portfolio_bytes(tmp_path) == before
    assert result.instrumentation["reanalyzed_portfolio"] is False


def test_preview_classifies_new_task_and_possible_project(
        tmp_path: object) -> None:
    _seed(tmp_path)
    result = capture.preview(
        str(tmp_path), "Book the room for the meeting\n"
                       "Sample data project\n")
    by_title = {item.title: item for item in result.items}
    assert by_title["Book the room for the meeting"].category \
        == capture.CAT_NEW_TASK
    assert by_title["Sample data project"].category \
        == capture.CAT_POSSIBLE_PROJECT
    assert result.counts["new_tasks"] == 1
    assert result.counts["possible_projects"] == 1


def test_preview_detects_existing_project_attach(tmp_path: object) -> None:
    _seed(tmp_path)
    result = capture.preview(str(tmp_path), "Follow up on the renewal\n")
    item = result.items[0]
    assert item.category == capture.CAT_EXISTING_PROJECT
    assert item.suggested_action == capture.ACT_ATTACH
    assert item.matched_id == "admin-renewal"


def test_preview_detects_existing_task_and_duplicate(tmp_path: object) -> None:
    _seed(tmp_path)
    result = capture.preview(
        str(tmp_path), "Update the plans\ncheck the update plans file\n")
    exact, related = result.items
    assert exact.category == capture.CAT_DUPLICATE
    assert exact.suggested_action == capture.ACT_SKIP
    assert related.category == capture.CAT_EXISTING_TASK
    assert related.matched_id == "update-cv"
    assert result.counts["duplicates"] >= 1


def test_preview_detects_in_batch_duplicate(tmp_path: object) -> None:
    _seed(tmp_path)
    result = capture.preview(str(tmp_path), "Book the room\nBook the room\n")
    first, second = result.items
    assert first.category == capture.CAT_NEW_TASK
    assert second.category == capture.CAT_DUPLICATE
    assert second.duplicate_of == first.capture_id


def test_preview_reports_instrumentation(tmp_path: object) -> None:
    _seed(tmp_path)
    result = capture.preview(str(tmp_path), "Book the room\n")
    inst = result.instrumentation
    assert inst["engine"] == capture.ENGINE
    assert inst["model"] == capture.MODEL
    assert inst["items_processed"] == 1
    assert inst["cost_usd"] == 0.0
    assert isinstance(inst["elapsed_ms"], int)


def test_confirm_applies_only_accepted_items(tmp_path: object) -> None:
    _seed(tmp_path)
    text = ("Book the room\nFollow up on the renewal\nUpdate the plans\n")
    preview = capture.preview(str(tmp_path), text)
    decisions = [
        {"capture_id": preview.items[0].capture_id, "action": "accept",
         "project_id": "garden-planner"},
        {"capture_id": preview.items[1].capture_id, "action": "attach"},
        {"capture_id": preview.items[2].capture_id, "action": "skip"},
    ]
    summary = capture.confirm(str(tmp_path), text, decisions)
    assert summary["created_tasks"] == 2
    assert summary["created_projects"] == 0
    assert summary["skipped"] == 1
    portfolio = store.load_portfolio(str(tmp_path))
    assert portfolio is not None
    titles = {t.title for t in portfolio.tasks}
    assert "Book the room" in titles
    assert "Follow up on the renewal" in titles
    # 'Update the plans' was skipped: no duplicate task is created.
    assert sum(1 for t in portfolio.tasks if t.title == "Update the plans") == 1


def test_confirm_can_create_a_project(tmp_path: object) -> None:
    _seed(tmp_path)
    text = "Sample data project\n"
    preview = capture.preview(str(tmp_path), text)
    summary = capture.confirm(str(tmp_path), text, [
        {"capture_id": preview.items[0].capture_id, "action": "accept"}])
    assert summary["created_projects"] == 1
    portfolio = store.load_portfolio(str(tmp_path))
    assert portfolio is not None
    assert any(p.name == "Sample data project" for p in portfolio.projects)


def test_confirm_requires_a_project_for_a_task(tmp_path: object) -> None:
    _seed(tmp_path)
    text = "Book the room\n"
    preview = capture.preview(str(tmp_path), text)
    with pytest.raises(model.MvpError):
        capture.confirm(str(tmp_path), text, [
            {"capture_id": preview.items[0].capture_id, "action": "accept"}])


def test_confirm_rejects_unknown_action_and_id(tmp_path: object) -> None:
    _seed(tmp_path)
    text = "Book the room\n"
    preview = capture.preview(str(tmp_path), text)
    with pytest.raises(model.MvpError):
        capture.confirm(str(tmp_path), text, [
            {"capture_id": preview.items[0].capture_id, "action": "explode"}])
    with pytest.raises(model.MvpError):
        capture.confirm(str(tmp_path), text, [
            {"capture_id": "c9999", "action": "accept"}])


def test_confirm_is_all_or_nothing(tmp_path: object) -> None:
    _seed(tmp_path)
    text = "Book the room\nCall the town office\n"
    preview = capture.preview(str(tmp_path), text)
    before = _portfolio_bytes(tmp_path)
    with pytest.raises(model.MvpError):
        capture.confirm(str(tmp_path), text, [
            {"capture_id": preview.items[0].capture_id, "action": "accept",
             "project_id": "garden-planner"},
            {"capture_id": preview.items[1].capture_id,
             "action": "accept"},
        ])
    assert _portfolio_bytes(tmp_path) == before


def test_empty_capture_is_rejected(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError):
        capture.preview(str(tmp_path), "   \n  \n")


def test_capture_is_lightweight_with_hundreds_of_tasks(tmp_path: object) -> None:
    projects = tuple(
        model.Project(project_id=f"p{i}", name=f"Project {i}",
                      objective="objective") for i in range(20))
    tasks = tuple(
        model.Task(task_id=f"t{i}", project_id=f"p{i % 20}",
                   title=f"Task number {i}") for i in range(400))
    store.save_portfolio(str(tmp_path), model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="big",
        projects=projects, tasks=tasks))
    text = "\n".join(f"New item {i}" for i in range(40))
    started = time.perf_counter()
    preview = capture.preview(str(tmp_path), text)
    elapsed = time.perf_counter() - started
    assert len(preview.items) == 40
    assert elapsed < 2.0, f"capture preview too slow: {elapsed:.3f}s"
