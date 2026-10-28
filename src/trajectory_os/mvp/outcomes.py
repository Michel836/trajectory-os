"""MVP — outcome recording and replanning.

Recording a real execution outcome:

* appends one immutable ledger revision;
* updates the task status (completed/deferred/blocked/abandoned);
* records actual duration when supplied;
* derives the project status from the new task states (never invents one);
* releases downstream tasks simply by re-evaluating readiness.

No ML is required for the MVP: planned-vs-actual statistics are computed as
plain deterministic aggregates over the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import changes, model, readiness, store

#: Outcome -> task status mapping (deterministic, explicit).
_OUTCOME_TO_STATUS = {
    model.O_COMPLETED: model.TS_COMPLETED,
    model.O_DEFERRED: model.TS_DEFERRED,
    model.O_BLOCKED: model.TS_BLOCKED,
    model.O_ABANDONED: model.TS_ABANDONED,
}


def apply_outcome(portfolio: model.Portfolio,
                  record: model.OutcomeRecord) -> tuple[model.Portfolio, model.Task]:
    """Apply one outcome to the portfolio, returning the new portfolio and
    the updated task. The input portfolio is never mutated."""
    record.validate()
    tasks = portfolio.task_map()
    task = tasks[record.task_id]
    updated = replace(
        task,
        status=_OUTCOME_TO_STATUS[record.outcome],
        actual_minutes=(record.actual_minutes
                        if record.actual_minutes is not None
                        else task.actual_minutes),
        updated_at=record.recorded_at,
    )
    new_tasks = tuple(updated if t.task_id == task.task_id else t
                      for t in portfolio.tasks)
    new_project = derive_project_status(portfolio, new_tasks,
                                        task.project_id)
    new_projects = tuple(
        new_project if p.project_id == task.project_id else p
        for p in portfolio.projects)
    new_portfolio = replace(portfolio, tasks=new_tasks,
                            projects=new_projects,
                            updated_at=record.recorded_at).validate()
    return new_portfolio, updated


def derive_project_status(
    portfolio: model.Portfolio,
    tasks: tuple[model.Task, ...],
    project_id: str,
) -> model.Project:
    """Derive one project's status from its tasks' readiness (never
    silently rewrites unrelated projects)."""
    project = portfolio.project_map()[project_id]
    if project.status in model.PROJECT_INBOX_STATUSES:
        # A parked (Inbox) project is an explicit human decision: editing one
        # of its tasks must not silently "activate" it.
        return project
    states = readiness.evaluate(replace(portfolio, tasks=tasks))
    project_states = [item for item in states
                      if item.project_id == project_id]
    if not project_states:
        return project
    if all(item.state in (readiness.RS_COMPLETED, readiness.RS_ABANDONED)
           for item in project_states):
        status = model.PS_COMPLETED
    elif any(item.state in (readiness.RS_READY, readiness.RS_IN_PROGRESS)
             for item in project_states):
        # A project is ACTIVE as soon as *something* is executable. A single
        # blocked or waiting task says nothing about the project's ability to
        # make progress; reporting BLOCKED in that case hid ready work.
        status = model.PS_ACTIVE
    elif any(item.state == readiness.RS_WAITING for item in project_states):
        status = model.PS_WAITING
    elif any(item.state == readiness.RS_BLOCKED for item in project_states):
        status = model.PS_BLOCKED
    elif all(item.state in (readiness.RS_COMPLETED, readiness.RS_ABANDONED,
                            readiness.RS_DEFERRED,
                            readiness.RS_PROJECT_CLOSED)
             for item in project_states):
        # Nothing is executable and nothing is waiting/blocked: the project is
        # deferred (completed tasks do not reactivate a deferred project).
        status = model.PS_DEFERRED
    else:
        status = model.PS_ACTIVE
    return replace(project, status=status)


def _rederive_project_statuses_locked(root: str | Path) -> dict[str, Any]:
    """Recompute every project status from its current task readiness.

    This is an explicit human-triggered repair for a portfolio whose stored
    project statuses predate the executability rule. It never touches task
    status, readiness, priorities or dependencies, and parked (Inbox)
    projects are preserved by :func:`derive_project_status`.
    """
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    stamp = intel_model.utc_now()
    projects: list[model.Project] = []
    changed: list[dict[str, str]] = []
    for project in portfolio.projects:
        derived = derive_project_status(portfolio, portfolio.tasks,
                                        project.project_id)
        if derived.status != project.status:
            changed.append({
                "project_id": project.project_id,
                "from": project.status,
                "to": derived.status,
            })
        projects.append(derived)
    if not changed:
        return {"changed": 0, "projects": []}
    new_portfolio = replace(portfolio, projects=tuple(projects),
                            updated_at=stamp).validate()
    store.backup_portfolio(root, reason="rederive-statuses")
    store.save_portfolio(root, new_portfolio)
    for entry in changed:
        try:
            changes.append_change(
                root, kind=changes.KIND_UPDATE,
                object_kind=changes.OBJECT_PROJECT,
                object_id=entry["project_id"],
                title=new_portfolio.project_map()[
                    entry["project_id"]].name,
                summary="statut recalculé",
                before={"status": entry["from"]},
                after={"status": entry["to"]}, source="system", at=stamp)
        except (OSError, changes.ChangeError):
            continue
    return {"changed": len(changed), "projects": changed}


def _record_and_save_locked(root: str | Path, *, task_id: str, outcome: str,
                           actual_minutes: int | None = None, note: str = "",
                           recorded_at: str = "") -> dict[str, Any]:
    """Record one outcome, persist portfolio + ledger, return a summary."""
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    tasks = portfolio.task_map()
    if task_id not in tasks:
        raise model.MvpError(model.E_UNKNOWN_ID, task_id)
    task = tasks[task_id]
    before = {"status": task.status,
              "actual_minutes": task.actual_minutes}
    stamp = recorded_at or intel_model.utc_now()
    record = model.OutcomeRecord(
        task_id=task_id, outcome=outcome, recorded_at=stamp,
        actual_minutes=actual_minutes, note=note).validate()
    new_portfolio, updated = apply_outcome(portfolio, record)
    store.save_portfolio(root, new_portfolio)
    store.append_outcome(root, record)
    after = {"status": updated.status,
             "actual_minutes": updated.actual_minutes}
    change_id = ""
    journal_warning = ""
    try:
        change = changes.append_change(
            root, kind=changes.KIND_OUTCOME,
            object_kind=changes.OBJECT_TASK, object_id=task_id,
            title=task.title,
            summary=(f"{outcome} · {actual_minutes}m"
                     if actual_minutes is not None else outcome),
            before=before, after=after, source="human", undoable=True,
            at=stamp)
        change_id = change.change_id
    except (OSError, changes.ChangeError) as exc:  # best-effort journal
        journal_warning = f"journal unavailable: {exc}"
    return {
        "task_id": task_id,
        "outcome": outcome,
        "recorded_at": stamp,
        "task_status": updated.status,
        "actual_minutes": updated.actual_minutes,
        "project_status": new_portfolio.project_map()[updated.project_id].status,
        "change_id": change_id,
        "journal_warning": journal_warning,
    }


def _undo_last_locked(root: str | Path, *, task_id: str,
                      recorded_at: str = "") -> dict[str, Any]:
    """Undo the most recent outcome for one task.

    Undoing is a *new* durable decision: the outcome ledger is never rewritten
    or deleted. The previous task status/actual minutes are restored only when
    the task still matches the outcome that is being undone; otherwise the
    operation fails closed instead of silently overwriting newer work.
    """
    change = changes.last_undoable(root, changes.OBJECT_TASK, task_id)
    if change is None:
        raise model.MvpError(model.E_MALFORMED, "nothing to undo")
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    tasks = portfolio.task_map()
    if task_id not in tasks:
        raise model.MvpError(model.E_UNKNOWN_ID, task_id)
    task = tasks[task_id]
    expected_status = change.after.get("status")
    expected_actual = change.after.get("actual_minutes")
    if task.status != expected_status or task.actual_minutes != expected_actual:
        raise model.MvpError(
            model.E_MALFORMED,
            "task changed since the recorded outcome; undo refused")
    restored_status = str(change.before.get("status", task.status))
    restored_actual = change.before.get("actual_minutes")
    if restored_status not in model.TASK_STATUSES:
        raise model.MvpError(model.E_MALFORMED,
                             "stored status is no longer valid")
    stamp = recorded_at or intel_model.utc_now()
    updated = replace(task, status=restored_status,
                      actual_minutes=restored_actual, updated_at=stamp)
    new_tasks = tuple(updated if t.task_id == task_id else t
                      for t in portfolio.tasks)
    new_project = derive_project_status(portfolio, new_tasks,
                                        task.project_id)
    new_projects = tuple(
        new_project if p.project_id == task.project_id else p
        for p in portfolio.projects)
    new_portfolio = replace(portfolio, tasks=new_tasks,
                            projects=new_projects,
                            updated_at=stamp).validate()
    store.save_portfolio(root, new_portfolio)
    before = {"status": task.status,
              "actual_minutes": task.actual_minutes}
    after = {"status": updated.status,
             "actual_minutes": updated.actual_minutes}
    undo_id = ""
    journal_warning = ""
    try:
        undo = changes.append_change(
            root, kind=changes.KIND_UNDO, object_kind=changes.OBJECT_TASK,
            object_id=task_id, title=task.title,
            summary="annulation du dernier résultat", before=before,
            after=after, source="human", undoable=False,
            references=change.change_id, at=stamp)
        undo_id = undo.change_id
    except (OSError, changes.ChangeError) as exc:  # best-effort journal
        journal_warning = f"journal unavailable: {exc}"
    return {
        "task_id": task_id,
        "restored_status": updated.status,
        "actual_minutes": updated.actual_minutes,
        "project_status": new_portfolio.project_map()[
            updated.project_id].status,
        "undone_change_id": change.change_id,
        "change_id": undo_id,
        "journal_warning": journal_warning,
    }


def rederive_project_statuses(root: str | Path) -> dict[str, Any]:
    """Recompute project statuses under the portfolio write lock."""
    with store.portfolio_lock(root):
        return _rederive_project_statuses_locked(root)


def record_and_save(root: str | Path, *, task_id: str, outcome: str,
                    actual_minutes: int | None = None, note: str = "",
                    recorded_at: str = "") -> dict[str, Any]:
    """Record an outcome under the portfolio write lock."""
    with store.portfolio_lock(root):
        return _record_and_save_locked(
            root, task_id=task_id, outcome=outcome,
            actual_minutes=actual_minutes, note=note,
            recorded_at=recorded_at)


def undo_last(root: str | Path, *, task_id: str,
              recorded_at: str = "") -> dict[str, Any]:
    """Undo the last outcome under the portfolio write lock."""
    with store.portfolio_lock(root):
        return _undo_last_locked(
            root, task_id=task_id, recorded_at=recorded_at)


@dataclass(frozen=True)
class PlannedVsActual:
    """Deterministic planned-vs-actual statistics (no ML)."""

    completed_with_estimate: int
    completed_with_actual: int
    planned_minutes: int
    actual_minutes: int
    mean_absolute_error_minutes: float | None
    mean_relative_error: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_with_estimate": self.completed_with_estimate,
            "completed_with_actual": self.completed_with_actual,
            "planned_minutes": self.planned_minutes,
            "actual_minutes": self.actual_minutes,
            "mean_absolute_error_minutes": self.mean_absolute_error_minutes,
            "mean_relative_error": self.mean_relative_error,
        }


def planned_vs_actual(portfolio: model.Portfolio,
                      outcomes: tuple[model.OutcomeRecord, ...]
                      ) -> PlannedVsActual:
    """Aggregate estimation error over completed tasks with actuals."""
    tasks = portfolio.task_map()
    completed_estimate = 0
    completed_actual = 0
    planned_total = 0
    actual_total = 0
    absolute_errors: list[float] = []
    relative_errors: list[float] = []

    for record in outcomes:
        if record.outcome != model.O_COMPLETED:
            continue
        task = tasks.get(record.task_id)
        if task is None:
            continue
        if task.estimated_minutes:
            completed_estimate += 1
            planned_total += task.estimated_minutes
        if record.actual_minutes is not None:
            completed_actual += 1
            actual_total += record.actual_minutes
            if task.estimated_minutes:
                absolute = abs(record.actual_minutes - task.estimated_minutes)
                absolute_errors.append(float(absolute))
                if task.estimated_minutes:
                    relative_errors.append(
                        absolute / float(task.estimated_minutes))

    return PlannedVsActual(
        completed_with_estimate=completed_estimate,
        completed_with_actual=completed_actual,
        planned_minutes=planned_total,
        actual_minutes=actual_total,
        mean_absolute_error_minutes=(
            sum(absolute_errors) / len(absolute_errors)
            if absolute_errors else None),
        mean_relative_error=(sum(relative_errors) / len(relative_errors)
                             if relative_errors else None),
    )


__all__ = [
    "PlannedVsActual",
    "apply_outcome",
    "derive_project_status",
    "planned_vs_actual",
    "record_and_save",
    "rederive_project_statuses",
    "undo_last",
]
