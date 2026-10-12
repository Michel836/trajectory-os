"""MVP — deterministic triage suggestions (B1).

Read-only suggestions that help the human reclassify work:

* a task that has outgrown a task (project-like wording and a large/absent
  estimate) should become its own project;
* a project with a single open task and no distinct objective is probably a
  task;
* a project with no open task is probably Inbox content (an idea / someday).

Nothing is applied automatically. The module only *suggests*; the human
applies it through the existing ``promote_task`` / ``demote_project`` /
``park_project`` operations. The heuristics are conservative and every
suggestion carries a human-readable reason.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from trajectory_os.mvp import classification, importer, model

PROMOTE = "promote"
DEMOTE = "demote"
PARK = "park"

#: A task at/above this estimate is large enough to deserve its own project.
PROMOTE_EFFORT_MINUTES = 240


@dataclass(frozen=True)
class Suggestion:
    """One human-actionable reclassification suggestion."""

    action: str
    ref_id: str
    title: str
    reason: str
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ref_id": self.ref_id,
            "title": self.title,
            "reason": self.reason,
            "target": self.target,
        }


def _has_project_name(portfolio: model.Portfolio, title: str) -> bool:
    needle = importer.normalize_text(title)
    return any(importer.normalize_text(p.name) == needle
               for p in portfolio.projects)


def suggest(portfolio: model.Portfolio,
            *, today: str | None = None) -> tuple[Suggestion, ...]:
    """Return conservative, deterministic reclassification suggestions."""
    project_map = portfolio.project_map()
    open_tasks: dict[str, list[model.Task]] = defaultdict(list)
    for task in portfolio.tasks:
        if task.status not in model.TASK_TERMINAL_STATUSES:
            open_tasks[task.project_id].append(task)

    suggestions: list[Suggestion] = []

    # Task -> project: project-like wording, large enough, not already a task
    # of a same-named project.
    for task in sorted(portfolio.tasks, key=lambda t: t.task_id):
        if task.project_id not in project_map:
            continue
        if task.status in model.TASK_TERMINAL_STATUSES:
            continue
        verdict = classification.classify_text(task.title)
        if verdict.kind != classification.PROJECT or verdict.ambiguous:
            continue
        if task.effort_minutes < PROMOTE_EFFORT_MINUTES:
            continue
        if _has_project_name(portfolio, task.title):
            continue
        suggestions.append(Suggestion(
            action=PROMOTE, ref_id=task.task_id, title=task.title,
            reason=(f"phrased as a project and estimated "
                    f"{task.effort_minutes} min; it may deserve its own "
                    f"project")))

    # Project -> task / Inbox.
    for project in sorted(portfolio.projects, key=lambda p: p.project_id):
        if project.parked:
            continue
        project_tasks = open_tasks.get(project.project_id, [])
        if not project_tasks:
            suggestions.append(Suggestion(
                action=PARK, ref_id=project.project_id, title=project.name,
                reason="no open task; park it in the Inbox or add a next "
                       "action"))
            continue
        has_objective = project.objective.strip() not in ("", project.name)
        if len(project_tasks) == 1 and not has_objective \
                and project.deadline is None:
            suggestions.append(Suggestion(
                action=DEMOTE, ref_id=project.project_id, title=project.name,
                reason="a single open task and no distinct objective; it may "
                       "be a task"))

    return tuple(suggestions)


__all__ = ["DEMOTE", "PARK", "PROMOTE", "PROMOTE_EFFORT_MINUTES", "Suggestion",
           "suggest"]
