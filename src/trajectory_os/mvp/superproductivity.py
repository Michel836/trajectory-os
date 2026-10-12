"""MVP — minimal Super Productivity integration.

The preferred direction for the MVP is one-way, human-triggered export:

    TrajectoryOS selected executable tasks -> Super Productivity import JSON

Completion status and actual time flow back by *re-importing* (the user
records outcomes via ``mvp record``), not by a bidirectional sync engine.

The export reuses the existing Super Productivity import document shape from
:mod:`trajectory_os.lifeos.adapters` so the two never drift.

The stable, deterministic id ``trajectory-mvp-<slug(task_id)>`` is the
de-duplication key: re-importing a document updates the same Super
Productivity task instead of creating a second copy. Every exported task also
carries the TrajectoryOS ``task_id`` / ``project_id`` so the handoff stays
traceable, and a dry-run preview can be produced without writing anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.lifeos import adapters as lifeos_adapters
from trajectory_os.mvp import model, priority, store


def build_sp_document(
    portfolio: model.Portfolio,
    tasks: tuple[priority.PrioritizedTask, ...],
    *,
    project_title: str = "TrajectoryOS",
) -> dict[str, Any]:
    """Build a Super Productivity import document for the selected tasks."""
    task_map = portfolio.task_map()
    exported: list[dict[str, Any]] = []
    for item in tasks:
        task = task_map[item.task_id]
        exported.append({
            "id": _sp_id(item.task_id),
            "title": task.title,
            "notes": "; ".join(item.reasons),
            "projectId": project_title,
            "isDone": task.status == model.TS_COMPLETED,
            "tagIds": ["trajectory-os", task.urgency.lower()],
            "timeEstimate": (task.estimated_minutes or 0) * 60 * 1000,
            # Traceability back to TrajectoryOS (additive, ignored by clients
            # that do not understand it).
            "trajectory_os": {
                "task_id": task.task_id,
                "project_id": task.project_id,
                "urgency": task.urgency,
                "impact": task.impact,
                "deadline": task.deadline,
                "planned_minutes": task.estimated_minutes,
            },
        })
    return {
        "project": {"title": project_title},
        "tasks": exported,
        "trajectory_os": {
            "schema_version": model.SCHEMA_VERSION,
            "mvp_version": model.MVP_VERSION,
            "task_count": len(exported),
            "direction": "trajectory-os -> super-productivity",
        },
    }


def _sp_id(task_id: str) -> str:
    """Stable Super Productivity id (the de-duplication key)."""
    return f"trajectory-mvp-{lifeos_adapters.slug(task_id, maximum=48)}"


def select_ready(
    portfolio: model.Portfolio,
    ranked: tuple[priority.PrioritizedTask, ...],
    *,
    task_ids: Sequence[str] | None = None,
    limit: int = 20,
) -> tuple[tuple[priority.PrioritizedTask, ...], tuple[str, ...]]:
    """Select ready tasks by explicit id (deliberate action set) or top-N.

    Returns ``(selected, skipped)``. ``skipped`` holds requested ids that are
    not currently ready, so the caller can report them instead of silently
    exporting something the plan does not consider executable.
    """
    by_id = {item.task_id: item for item in ranked}
    if task_ids:
        selected: list[priority.PrioritizedTask] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for task_id in task_ids:
            if task_id in seen:
                continue
            seen.add(task_id)
            item = by_id.get(task_id)
            if item is None:
                skipped.append(task_id)
                continue
            selected.append(item)
        return tuple(selected), tuple(skipped)
    if limit > 0:
        return tuple(ranked[:limit]), ()
    return tuple(ranked), ()


def preview_export(
    root: str | Path,
    *,
    task_ids: Sequence[str] | None = None,
    limit: int = 20,
    project_title: str = "TrajectoryOS",
) -> dict[str, Any]:
    """Build the export document *without writing anything* (dry run)."""
    portfolio = _load(root)
    ranked = priority.rank_ready_tasks(portfolio)
    selected, skipped = select_ready(portfolio, ranked, task_ids=task_ids,
                                     limit=limit)
    document = build_sp_document(portfolio, selected,
                                 project_title=project_title)
    return {
        "target": None,
        "written": False,
        "exported": len(selected),
        "task_ids": [item.task_id for item in selected],
        "skipped_task_ids": list(skipped),
        "document": document,
        "note": ("dry run — nothing written; import this document in Super "
                 "Productivity (human action)"),
    }


def export_selected(
    root: str | Path,
    task_ids: Sequence[str],
    *,
    target: str | Path,
    project_title: str = "TrajectoryOS",
) -> dict[str, Any]:
    """Export an explicit, deliberate set of ready tasks to a JSON file."""
    portfolio = _load(root)
    ranked = priority.rank_ready_tasks(portfolio)
    selected, skipped = select_ready(portfolio, ranked, task_ids=task_ids)
    document = build_sp_document(portfolio, selected,
                                 project_title=project_title)
    path = Path(target)
    intel_model.write_json(path, document)
    return {
        "target": str(path),
        "written": True,
        "exported": len(selected),
        "task_ids": [item.task_id for item in selected],
        "skipped_task_ids": list(skipped),
        "note": "import this file in Super Productivity (human action)",
    }


def export_ready(root: str | Path, *, target: str | Path,
                 limit: int = 20,
                 project_title: str = "TrajectoryOS",
                 dry_run: bool = False) -> dict[str, Any]:
    """Export the top ready tasks to a Super Productivity import JSON file.

    Returns a summary (never auto-imports; the human imports the file). With
    ``dry_run=True`` the document is built and returned but never written.
    """
    if dry_run:
        return preview_export(root, limit=limit, project_title=project_title)
    portfolio = _load(root)
    ranked = priority.rank_ready_tasks(portfolio)
    selected, _skipped = select_ready(portfolio, ranked, limit=limit)
    document = build_sp_document(portfolio, selected,
                                 project_title=project_title)
    path = Path(target)
    intel_model.write_json(path, document)
    return {
        "target": str(path),
        "written": True,
        "exported": len(selected),
        "task_ids": [item.task_id for item in selected],
        "note": "import this file in Super Productivity (human action)",
    }


def _load(root: str | Path) -> model.Portfolio:
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    return portfolio


__all__ = [
    "build_sp_document", "export_ready", "export_selected", "preview_export",
    "select_ready",
]
