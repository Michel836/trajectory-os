"""MVP — deterministic work-breakdown projection.

Projects a :class:`~trajectory_os.mvp.model.Portfolio` into the personal WBS
hierarchy::

    Objective
      -> Project
        -> Workstream
          -> Deliverable
            -> Task

Workstream and deliverable are supplied per task and are therefore *never*
invented: a task without a workstream sits directly under its project, and a
task without a deliverable sits directly under its workstream.
"""

from __future__ import annotations

from typing import Any

from trajectory_os.mvp import model

#: A single WBS row, from root (objective) to leaf (task).
WBS_COLUMNS = ("objective", "project", "workstream", "deliverable", "task")


def rows(portfolio: model.Portfolio) -> tuple[dict[str, str], ...]:
    """Return one row per task, ordered by project then task id."""
    project_map = portfolio.project_map()
    tasks = sorted(portfolio.tasks,
                   key=lambda task: (task.project_id, task.task_id))
    result: list[dict[str, str]] = []
    for task in tasks:
        project = project_map[task.project_id]
        result.append({
            "objective": project.objective,
            "project": project.name,
            "workstream": task.workstream,
            "deliverable": task.deliverable,
            "task": task.title,
            "task_id": task.task_id,
        })
    return tuple(result)


def tree(portfolio: model.Portfolio) -> dict[str, Any]:
    """Return a nested WBS tree (objective -> project -> workstream ->
    deliverable -> tasks), deterministically ordered.

    Keys ``workstream``/``deliverable`` fall back to ``"(none)"`` when absent
    so the tree is complete without inventing labels.
    """
    project_map = portfolio.project_map()
    objectives: dict[str, dict[str, Any]] = {}
    for project in sorted(portfolio.projects,
                          key=lambda p: p.project_id):
        objective = objectives.setdefault(project.objective, {
            "objective": project.objective,
            "projects": {},
        })
        objective["projects"][project.project_id] = {
            "project_id": project.project_id,
            "name": project.name,
            "workstreams": {},
        }

    for task in sorted(portfolio.tasks,
                       key=lambda t: (t.project_id, t.task_id)):
        project = project_map[task.project_id]
        node = objectives[project.objective]["projects"][project.project_id]
        workstream_key = task.workstream or "(none)"
        workstream = node["workstreams"].setdefault(workstream_key, {
            "workstream": workstream_key,
            "deliverables": {},
        })
        deliverable_key = task.deliverable or "(none)"
        deliverable = workstream["deliverables"].setdefault(deliverable_key, {
            "deliverable": deliverable_key,
            "tasks": [],
        })
        deliverable["tasks"].append({
            "task_id": task.task_id,
            "title": task.title,
            "status": task.status,
            "estimated_minutes": task.estimated_minutes,
        })

    return {
        "objectives": sorted(objectives.values(),
                             key=lambda o: str(o["objective"])),
    }


def workstreams(portfolio: model.Portfolio,
                project_id: str) -> tuple[str, ...]:
    """Return the distinct, ordered workstream labels of one project."""
    labels = sorted({task.workstream for task in portfolio.tasks
                     if task.project_id == project_id and task.workstream})
    return tuple(labels)


def deliverables(portfolio: model.Portfolio,
                 project_id: str) -> tuple[str, ...]:
    """Return the distinct, ordered deliverable labels of one project."""
    labels = sorted({task.deliverable for task in portfolio.tasks
                     if task.project_id == project_id and task.deliverable})
    return tuple(labels)


def render_text(portfolio: model.Portfolio) -> str:
    """Render the WBS as indented text."""
    tree_doc = tree(portfolio)
    lines: list[str] = ["# Work breakdown structure", ""]
    for objective in tree_doc["objectives"]:
        lines.append(f"## {objective['objective']}")
        for project in sorted(objective["projects"].values(),
                              key=lambda p: str(p["name"])):
            lines.append(f"- {project['name']}")
            for workstream in sorted(project["workstreams"].values(),
                                     key=lambda w: str(w["workstream"])):
                prefix = "" if workstream["workstream"] == "(none)" \
                    else f"  - {workstream['workstream']}"
                if prefix:
                    lines.append(prefix)
                for deliverable in sorted(
                        workstream["deliverables"].values(),
                        key=lambda d: str(d["deliverable"])):
                    if deliverable["deliverable"] != "(none)":
                        lines.append(f"    - {deliverable['deliverable']}")
                    for task in deliverable["tasks"]:
                        indent = "      " if deliverable["deliverable"] \
                            != "(none)" else "    "
                        lines.append(
                            f"{indent}* [{task['status']}] "
                            f"{task['title']} "
                            f"({task['task_id']})")
    return "\n".join(lines) + "\n"


__all__ = [
    "WBS_COLUMNS",
    "deliverables",
    "render_text",
    "rows",
    "tree",
    "workstreams",
]
