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
from trajectory_os.mvp import model, readiness, store

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
    states = readiness.evaluate(replace(portfolio, tasks=tasks))
    project_states = [item for item in states
                      if item.project_id == project_id]
    if not project_states:
        return project
    if all(item.state in (readiness.RS_COMPLETED, readiness.RS_ABANDONED)
           for item in project_states):
        status = model.PS_COMPLETED
    elif any(item.state == readiness.RS_BLOCKED for item in project_states):
        status = model.PS_BLOCKED
    elif any(item.state == readiness.RS_WAITING for item in project_states):
        status = model.PS_WAITING
    elif (project.status == model.PS_DEFERRED
          and any(item.state == readiness.RS_PROJECT_CLOSED
                  for item in project_states)):
        status = model.PS_DEFERRED
    else:
        status = model.PS_ACTIVE
    return replace(project, status=status)


def record_and_save(root: str | Path, *, task_id: str, outcome: str,
                    actual_minutes: int | None = None, note: str = "",
                    recorded_at: str = "") -> dict[str, Any]:
    """Record one outcome, persist portfolio + ledger, return a summary."""
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    tasks = portfolio.task_map()
    if task_id not in tasks:
        raise model.MvpError(model.E_UNKNOWN_ID, task_id)
    stamp = recorded_at or intel_model.utc_now()
    record = model.OutcomeRecord(
        task_id=task_id, outcome=outcome, recorded_at=stamp,
        actual_minutes=actual_minutes, note=note).validate()
    new_portfolio, updated = apply_outcome(portfolio, record)
    store.save_portfolio(root, new_portfolio)
    store.append_outcome(root, record)
    return {
        "task_id": task_id,
        "outcome": outcome,
        "recorded_at": stamp,
        "task_status": updated.status,
        "actual_minutes": updated.actual_minutes,
        "project_status": new_portfolio.project_map()[updated.project_id].status,
    }


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
]
