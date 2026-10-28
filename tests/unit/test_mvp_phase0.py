"""Phase 0 — daily usability foundations.

Covers the first increment of the usability roadmap:

* project status is derived from *executability*, so a project with one
  blocked task and one ready task is ACTIVE, not BLOCKED;
* the read-only portfolio-health projection surfaces deferred projects,
  missing estimates and stale waiting items without mutating the portfolio.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from trajectory_os.mvp import (
    capture,
    changes,
    cockpit_ui,
    model,
    outcomes,
    store,
    uistate,
    visualization,
)


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="health",
        projects=(
            model.Project(project_id="mixed", name="Mixed",
                          objective="mixed", status=model.PS_BLOCKED),
            model.Project(project_id="blocked", name="Blocked only",
                          objective="blocked"),
            model.Project(project_id="waiting", name="Waiting",
                          objective="waiting"),
            model.Project(project_id="done", name="Done",
                          objective="done", status=model.PS_COMPLETED),
            model.Project(project_id="deferred", name="Deferred",
                          objective="deferred", status=model.PS_DEFERRED),
            model.Project(project_id="empty", name="Empty",
                          objective="empty", status=model.PS_ACTIVE),
        ),
        tasks=(
            model.Task(task_id="mixed-blocked", project_id="mixed",
                       title="Blocked task", status=model.TS_BLOCKED),
            model.Task(task_id="mixed-ready", project_id="mixed",
                       title="Ready task"),
            model.Task(task_id="only-blocked", project_id="blocked",
                       title="Only blocked", status=model.TS_BLOCKED),
            model.Task(task_id="only-waiting", project_id="waiting",
                       title="Only waiting", status=model.TS_WAITING,
                       waiting_for=("external party",)),
            model.Task(task_id="done-task", project_id="done",
                       title="Completed task", status=model.TS_COMPLETED),
            model.Task(task_id="deferred-task", project_id="deferred",
                       title="Deferred task"),
            model.Task(task_id="deferred-done", project_id="deferred",
                       title="Deferred done", status=model.TS_COMPLETED),
            model.Task(task_id="no-estimate", project_id="empty",
                       title="No estimate"),
            model.Task(task_id="old-waiting", project_id="waiting",
                       title="Old waiting", status=model.TS_WAITING,
                       waiting_for=("external party",),
                       updated_at="2025-12-20T10:00:00Z"),
        ),
    ))


def test_project_with_ready_and_blocked_tasks_is_active(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    derived = outcomes.derive_project_status(
        portfolio, portfolio.tasks, "mixed")
    assert derived.status == model.PS_ACTIVE


def test_project_blocked_only_when_nothing_is_executable(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    blocked = outcomes.derive_project_status(
        portfolio, portfolio.tasks, "blocked")
    waiting = outcomes.derive_project_status(
        portfolio, portfolio.tasks, "waiting")
    done = outcomes.derive_project_status(
        portfolio, portfolio.tasks, "done")
    deferred = outcomes.derive_project_status(
        portfolio, portfolio.tasks, "deferred")
    assert blocked.status == model.PS_BLOCKED
    assert waiting.status == model.PS_WAITING
    assert done.status == model.PS_COMPLETED
    # A completed task inside a deferred project does not reactivate it.
    assert deferred.status == model.PS_DEFERRED


def test_portfolio_health_counts_and_bounded_items(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    portfolio_path = store.default_portfolio_path(root)
    before = portfolio_path.read_bytes()

    views = visualization.build_views(root, today=date(2026, 1, 10))
    health = views["portfolio_health"]

    assert health["counts"]["deferred_projects"] == 1
    assert health["counts"]["projects_without_open_task"] == 1
    assert health["counts"]["open_tasks_without_estimate"] >= 5
    assert health["counts"]["ready_tasks_without_estimate"] >= 2
    # The persisted status is inconsistent with readiness: this is exactly the
    # signal the cockpit must surface until the human reacts.
    assert health["counts"]["blocked_projects_with_ready_task"] == 1
    assert health["counts"]["waiting_over_attention_days"] == 1
    assert health["deferred_projects"][0]["ref_id"] == "deferred"
    assert health["blocked_projects_with_ready_task"][0]["ref_id"] == "mixed"
    assert health["waiting_over_attention_days"][0]["ref_id"] == "old-waiting"

    # Reading the health projection never writes the portfolio.
    assert portfolio_path.read_bytes() == before


def test_changes_append_and_read_newest_first(tmp_path: object) -> None:
    root = str(tmp_path)
    first = changes.append_change(
        root, kind=changes.KIND_CREATE, object_kind=changes.OBJECT_TASK,
        object_id="t1", title="First", summary="created",
        at="2026-01-01T10:00:00Z")
    second = changes.append_change(
        root, kind=changes.KIND_UPDATE, object_kind=changes.OBJECT_TASK,
        object_id="t1", title="First", summary="updated",
        before={"status": "TODO"}, after={"status": "IN_PROGRESS"},
        at="2026-01-01T11:00:00Z")
    records = changes.read_changes(root, limit=10)
    assert [record.change_id for record in records] == [
        second.change_id, first.change_id]
    assert len(changes.read_changes(root, limit=1)) == 1
    assert changes.read_changes(root, since="2026-01-01T10:30:00Z") == (
        second,)


def test_outcome_undo_restores_status_and_keeps_ledger(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    before_count = len(store.read_outcomes(root))

    result = outcomes.record_and_save(
        root, task_id="no-estimate", outcome=model.O_COMPLETED,
        actual_minutes=12)
    assert result["change_id"]
    assert len(store.read_outcomes(root)) == before_count + 1
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.task_map()["no-estimate"].status == model.TS_COMPLETED

    undo = outcomes.undo_last(root, task_id="no-estimate")
    assert undo["restored_status"] == model.TS_TODO
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    task = portfolio.task_map()["no-estimate"]
    assert task.status == model.TS_TODO
    assert task.actual_minutes is None
    # The immutable outcome ledger is never rewritten or truncated.
    assert len(store.read_outcomes(root)) == before_count + 1
    records = changes.read_changes(root)
    assert any(record.kind == changes.KIND_UNDO
               and record.references == result["change_id"]
               for record in records)


def test_outcome_undo_refuses_after_a_later_change(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    outcomes.record_and_save(root, task_id="no-estimate",
                             outcome=model.O_COMPLETED, actual_minutes=12)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    task = portfolio.task_map()["no-estimate"]
    updated = replace(task, status=model.TS_TODO)
    store.save_portfolio(root, replace(
        portfolio,
        tasks=tuple(updated if t.task_id == "no-estimate" else t
                    for t in portfolio.tasks)))
    with pytest.raises(model.MvpError):
        outcomes.undo_last(root, task_id="no-estimate")


def test_single_capture_decision_creates_only_one_object(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    before = store.load_portfolio(root)
    assert before is not None
    text = "preparer le dossier alpha\npreparer le dossier beta"
    preview = capture.preview(root, text)
    first = preview.items[0]
    result = capture.confirm(root, text, [{
        "capture_id": first.capture_id,
        "action": capture.ACT_ACCEPT,
        "kind": "task",
        "project_id": "empty",
    }])
    assert result["created_tasks"] == 1
    after = store.load_portfolio(root)
    assert after is not None
    assert len(after.tasks) == len(before.tasks) + 1
    titles = {task.title for task in after.tasks}
    assert first.title in titles
    assert preview.items[1].title not in titles


def test_capture_line_by_line_is_wired() -> None:
    js = cockpit_ui.PAGE_JS
    for marker in ("S.captureApplied", "confirmCaptureItem",
                   "confirmCaptureItemById", "captureDecisionFor"):
        assert marker in js, marker


def test_changes_view_and_done_action_are_wired() -> None:
    assert "changes" in uistate.VIEWS
    assert uistate.default_preferences().view == "today"
    js = cockpit_ui.PAGE_JS
    for marker in ("'changes','Changes'", "changes:renderChanges",
                   "function renderChanges", "function renderHealthCard",
                   "function doneButton", "function undoOutcome",
                   "keepNotice", "Santé du portefeuille", "Terminé"):
        assert marker in js, marker


def test_workspaces_cover_every_view() -> None:
    js = cockpit_ui.PAGE_JS
    start = js.index("const WORKSPACES =")
    end = js.index("const ADVANCED_VIEWS", start)
    block = js[start:end]
    for view in uistate.VIEWS:
        assert "'" + view + "'" in block, view


def test_changes_feed_includes_legacy_outcomes(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    store.append_outcome(root, model.OutcomeRecord(
        task_id="no-estimate", outcome=model.O_COMPLETED,
        recorded_at="2026-01-01T10:00:00Z", actual_minutes=12))
    views = visualization.build_views(root, today=date(2026, 1, 10))
    legacy = [change for change in views["changes"]
              if change["source"] == "legacy"]
    assert legacy
    assert legacy[0]["object_id"] == "no-estimate"
    assert legacy[0]["summary"] == "COMPLETED · 12m"


def test_rederive_project_statuses_is_explicit_and_preserves_inbox(
        tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    result = outcomes.rederive_project_statuses(root)
    assert result["changed"] >= 1
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    projects = portfolio.project_map()
    assert projects["mixed"].status == model.PS_ACTIVE
    assert projects["blocked"].status == model.PS_BLOCKED
    assert projects["waiting"].status == model.PS_WAITING
    assert projects["deferred"].status == model.PS_DEFERRED
    # A second run is idempotent.
    assert outcomes.rederive_project_statuses(root)["changed"] == 0
