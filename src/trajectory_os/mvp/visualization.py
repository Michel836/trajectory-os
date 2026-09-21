"""MVP — visual execution & decision layer view model.

This module is the single bridge between the validated portfolio and every
visual representation in the cockpit. It derives, deterministically, all of
the data each view needs:

* normalised object records (projects + tasks) carrying every attribute the
  interface may expose;
* a real hierarchical WBS (AREA -> PROJECT -> WORKSTREAM -> PACKAGE -> TASK);
* the confirmed dependency graph with upstream/downstream context levels;
* Gantt lanes that distinguish dated / scheduled / unscheduled / waiting /
  blocked work (never inventing a date);
* Eisenhower, impact/effort, portfolio-map, treemap, progress, heatmap and
  goal/outcome-flow projections.

Nothing here mutates the portfolio and nothing here invents missing values:
``UNKNOWN`` stays ``UNKNOWN`` and a missing schedule is reported as
``unscheduled`` rather than fabricated. Business rules (readiness, priority,
dependencies, aggregation) stay in Python; the browser only lays out and draws
what it receives.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import (
    graph as graph_module,
)
from trajectory_os.mvp import (
    model,
    scheduler,
    store,
    uistate,
)
from trajectory_os.mvp import (
    outcomes as outcomes_module,
)
from trajectory_os.mvp import (
    priority as priority_module,
)
from trajectory_os.mvp import (
    readiness as readiness_module,
)

#: Evidence labels (persisted entities are human-confirmed facts; the label
#: infrastructure exists so a future importer can surface SUGGESTED content).
EV_FACT = "FACT"
EV_INFERRED = "INFERRED"
EV_SUGGESTED = "SUGGESTED"
EV_UNKNOWN = "UNKNOWN"
EVIDENCES = (EV_FACT, EV_INFERRED, EV_SUGGESTED, EV_UNKNOWN)

#: Impact/effort quadrant thresholds (estimated effort only — never actual).
QUICK_WIN_MAX_MINUTES = 60
MAJOR_BET_MIN_MINUTES = 120

#: Only urgency CRITICAL/HIGH counts as "urgent"; only impact HIGH as
#: "important" (Eisenhower). MEDIUM/LOW are never silently promoted.
URGENT_URGENCIES = (model.U_CRITICAL, model.U_HIGH)
IMPORTANT_IMPACTS = (model.I_HIGH,)

#: Attribute catalogue driving density modes in the interface.
ATTRIBUTE_CATALOG: tuple[dict[str, str], ...] = (
    {"key": "title", "label": "Title", "density": "compact"},
    {"key": "kind", "label": "Type", "density": "compact"},
    {"key": "status", "label": "Status", "density": "compact"},
    {"key": "project", "label": "Project", "density": "normal"},
    {"key": "domain", "label": "Area / domain", "density": "normal"},
    {"key": "workstream", "label": "Workstream", "density": "normal"},
    {"key": "work_package", "label": "Work package / deliverable",
     "density": "normal"},
    {"key": "readiness", "label": "Readiness", "density": "normal"},
    {"key": "urgency", "label": "Urgency", "density": "normal"},
    {"key": "impact", "label": "Impact", "density": "normal"},
    {"key": "estimated_minutes", "label": "Estimated effort",
     "density": "normal"},
    {"key": "deadline", "label": "Deadline", "density": "normal"},
    {"key": "next_action", "label": "Next action", "density": "normal"},
    {"key": "unlocks", "label": "Unlocks", "density": "normal"},
    {"key": "blockers", "label": "Blockers", "density": "normal"},
    {"key": "waiting_for", "label": "Waiting for", "density": "normal"},
    {"key": "evidence", "label": "Evidence", "density": "normal"},
    {"key": "priority", "label": "Calculated priority", "density": "detailed"},
    {"key": "priority_reasons", "label": "Why this priority",
     "density": "detailed"},
    {"key": "actual_minutes", "label": "Actual time", "density": "detailed"},
    {"key": "planned_vs_actual", "label": "Planned vs actual",
     "density": "detailed"},
    {"key": "estimation_error", "label": "Estimation error",
     "density": "detailed"},
    {"key": "dependencies", "label": "Confirmed dependencies",
     "density": "detailed"},
    {"key": "suggested_dependencies", "label": "Suggested dependencies",
     "density": "detailed"},
    {"key": "prerequisites", "label": "Prerequisites", "density": "detailed"},
    {"key": "downstream", "label": "Downstream / unlocks",
     "density": "detailed"},
    {"key": "deliverables", "label": "Deliverables", "density": "detailed"},
    {"key": "notes", "label": "Notes", "density": "detailed"},
    {"key": "outcomes", "label": "Outcome history", "density": "detailed"},
    {"key": "provenance", "label": "Provenance", "density": "detailed"},
    {"key": "confidence", "label": "Confidence / source",
     "density": "detailed"},
)

WBS_LEVELS = ("AREA", "PROJECT", "WORKSTREAM", "WORK_PACKAGE", "TASK",
              "SUBTASK", "DELIVERABLE")


def build_views(root: str | Path, *, today: date | None = None,
                prefs: uistate.UIPreferences | None = None) -> dict[str, Any]:
    """Build the complete, single-source view payload for the cockpit."""
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    today = today or date.today()
    prefs = prefs or uistate.load_preferences(root)
    stamp = intel_model.utc_now()

    ranked = priority_module.rank_ready_tasks(portfolio, today=today)
    rank_map = {item.task_id: item for item in ranked}
    readiness_map = {item.task_id: item
                     for item in readiness_module.evaluate(portfolio)}
    dep_graph = graph_module.build_graph(portfolio)
    records = store.read_outcomes(root)
    latest_outcome = {record.task_id: record for record in records}
    outcomes_by_task: dict[str, list[model.OutcomeRecord]] = defaultdict(list)
    for record in records:
        outcomes_by_task[record.task_id].append(record)

    # --- task objects --------------------------------------------------------
    task_objects: dict[str, dict[str, Any]] = {}
    for task in sorted(portfolio.tasks, key=lambda t: t.task_id):
        task_objects[task.task_id] = _task_object(
            task, portfolio, rank_map, readiness_map, dep_graph,
            outcomes_by_task.get(task.task_id, []), latest_outcome,
            prefs,
        )

    # --- project objects -----------------------------------------------------
    project_tasks: dict[str, list[model.Task]] = defaultdict(list)
    for task in portfolio.tasks:
        project_tasks[task.project_id].append(task)
    project_objects: dict[str, dict[str, Any]] = {}
    for project in sorted(portfolio.projects, key=lambda p: p.project_id):
        project_objects[project.project_id] = _project_object(
            project, project_tasks.get(project.project_id, []),
            portfolio, rank_map, readiness_map, dep_graph, prefs,
        )

    objects: list[dict[str, Any]] = [
        project_objects[pid] for pid in sorted(project_objects)
    ] + [task_objects[tid] for tid in sorted(task_objects)]

    week = scheduler.plan_week(portfolio, ranked, today)
    day = scheduler.plan_day(portfolio, ranked, today)

    return {
        "schema_version": model.SCHEMA_VERSION,
        "kind": "mvp_views",
        "generated_at": stamp,
        "today": today.isoformat(),
        "name": portfolio.name,
        "capacity": portfolio.capacity.to_dict(),
        "portfolio": _portfolio_summary(portfolio, project_objects,
                                        task_objects),
        "objects": objects,
        "object_index": {obj["id"]: obj for obj in objects},
        "wbs": _build_wbs(project_objects, task_objects, portfolio),
        "graph": _build_graph(portfolio, task_objects),
        "gantt": _build_gantt(portfolio, task_objects, week, day),
        "eisenhower": _build_eisenhower(objects),
        "impact_effort": _build_impact_effort(objects),
        "portfolio_map": _build_portfolio_map(project_objects),
        "treemap": _build_treemap(portfolio),
        "progress": _build_progress(portfolio, project_objects, records),
        "heatmap": _build_heatmap(portfolio, records, week),
        "goal_flow": _build_goal_flow(portfolio, task_objects, records),
        "today_plan": day.to_dict(),
        "ready_workflow": _ready_workflow(portfolio, readiness_map),
        "attribute_catalog": list(ATTRIBUTE_CATALOG),
        "colour_modes": list(uistate.COLOR_MODES),
        "densities": list(uistate.DENSITIES),
        "views": list(uistate.VIEWS),
        "highlight_colours": list(uistate.HIGHLIGHT_COLORS),
        "semantic_colours": uistate.SEMANTIC_COLORS,
        "preferences": prefs.to_dict(),
        "wbs_levels": list(WBS_LEVELS),
    }


# --- normalised objects -------------------------------------------------------


def _object_id(kind: str, ref_id: str) -> str:
    return f"{kind}:{ref_id}"


def _task_object(
    task: model.Task,
    portfolio: model.Portfolio,
    rank_map: Mapping[str, priority_module.PrioritizedTask],
    readiness_map: Mapping[str, readiness_module.TaskReadiness],
    dep_graph: graph_module.DependencyGraph,
    records: Sequence[model.OutcomeRecord],
    latest_outcome: Mapping[str, model.OutcomeRecord],
    prefs: uistate.UIPreferences,
) -> dict[str, Any]:
    project = portfolio.project_map()[task.project_id]
    domain = project.domain
    state = readiness_map[task.task_id]
    ranked = rank_map.get(task.task_id)
    downstream = list(dep_graph.downstream(task.task_id))
    upstream = list(dep_graph.upstream(task.task_id))
    latest = latest_outcome.get(task.task_id)
    estimate_error: int | None = None
    if latest is not None and latest.actual_minutes is not None \
            and task.estimated_minutes:
        estimate_error = latest.actual_minutes - task.estimated_minutes
    colours = {
        mode: uistate.resolve_colour(
            mode, domain=domain, project_id=task.project_id,
            status=task.status, urgency=task.urgency, impact=task.impact,
            prefs=prefs)
        for mode in uistate.COLOR_MODES
    }
    return {
        "id": _object_id("task", task.task_id),
        "kind": "task",
        "ref_id": task.task_id,
        "title": task.title,
        "description": task.description,
        "project_id": task.project_id,
        "project": project.name,
        "domain": domain,
        "workstream": task.workstream,
        "work_package": task.deliverable,
        "deliverable": task.deliverable,
        "status": task.status,
        "readiness": state.state,
        "ready": state.ready,
        "urgency": task.urgency,
        "impact": task.impact,
        "urgency_rank": model.URGENCY_RANK[task.urgency],
        "impact_rank": model.IMPACT_RANK[task.impact],
        "priority_rank": ranked.rank if ranked is not None else None,
        "priority_score": ranked.score if ranked is not None else None,
        "priority_reasons": list(ranked.reasons) if ranked is not None else [],
        "estimated_minutes": task.estimated_minutes,
        "actual_minutes": task.actual_minutes,
        "effort_minutes": task.effort_minutes,
        "remaining_minutes": (task.estimated_minutes
                              if task.status not in model.TASK_TERMINAL_STATUSES
                              else 0),
        "estimate_error_minutes": estimate_error,
        "deadline": task.deadline,
        "next_action": task.next_action,
        "dependencies": list(task.dependencies),
        "dependency_titles": [portfolio.task_map()[dep].title
                              for dep in task.dependencies
                              if dep in portfolio.task_map()],
        "suggested_dependencies": [],
        "blocked_by": list(task.blocked_by),
        "blocked_by_titles": [portfolio.task_map()[dep].title
                              for dep in task.blocked_by
                              if dep in portfolio.task_map()],
        "waiting_for": list(task.waiting_for),
        "resources": list(task.resources),
        "upstream": upstream,
        "downstream": downstream,
        "upstream_levels": _levels(upstream, dep_graph.predecessors),
        "downstream_levels": _levels(downstream, dep_graph.successors),
        "unlocks_count": dep_graph.transitive_unblocks(task.task_id),
        "outcomes": [record.to_dict() for record in records],
        "latest_outcome": latest.to_dict() if latest is not None else None,
        "evidence": EV_FACT,
        "confidence": "human-confirmed",
        "provenance": ("imported" if task.created_at else "created in cockpit"),
        "notes": task.description,
        "colour": colours,
        "highlight": prefs.highlights.get(_object_id("task", task.task_id)),
        "reasons": list(state.reasons),
        "blockers": list(state.blockers),
    }


def _project_object(
    project: model.Project,
    tasks: Sequence[model.Task],
    portfolio: model.Portfolio,
    rank_map: Mapping[str, priority_module.PrioritizedTask],
    readiness_map: Mapping[str, readiness_module.TaskReadiness],
    dep_graph: graph_module.DependencyGraph,
    prefs: uistate.UIPreferences,
) -> dict[str, Any]:
    completed = sum(1 for task in tasks if task.status == model.TS_COMPLETED)
    total = len(tasks)
    estimated = sum(task.estimated_minutes or 0 for task in tasks)
    actual = sum(task.actual_minutes or 0 for task in tasks)
    remaining = sum(task.estimated_minutes or 0 for task in tasks
                    if task.status not in model.TASK_TERMINAL_STATUSES)
    project_task_ids = {task.task_id for task in tasks}
    downstream: set[str] = set()
    upstream: set[str] = set()
    for task in tasks:
        downstream.update(dep_graph.downstream(task.task_id))
        upstream.update(dep_graph.upstream(task.task_id))
    downstream -= project_task_ids
    upstream -= project_task_ids
    best: priority_module.PrioritizedTask | None = None
    for task in tasks:
        ranked = rank_map.get(task.task_id)
        if ranked is None:
            continue
        if best is None or ranked.score > best.score:
            best = ranked
    colours = {
        mode: uistate.resolve_colour(
            mode, domain=project.domain, project_id=project.project_id,
            status=project.status, urgency=project.urgency,
            impact=project.impact, prefs=prefs)
        for mode in uistate.COLOR_MODES
    }
    return {
        "id": _object_id("project", project.project_id),
        "kind": "project",
        "ref_id": project.project_id,
        "title": project.name,
        "description": project.description,
        "objective": project.objective,
        "project_id": project.project_id,
        "project": project.name,
        "domain": project.domain,
        "workstream": "",
        "work_package": "",
        "deliverable": "",
        "status": project.status,
        "readiness": None,
        "ready": project.open,
        "urgency": project.urgency,
        "impact": project.impact,
        "urgency_rank": model.URGENCY_RANK[project.urgency],
        "impact_rank": model.IMPACT_RANK[project.impact],
        "priority_rank": best.rank if best is not None else None,
        "priority_score": best.score if best is not None else None,
        "priority_reasons": list(best.reasons) if best is not None else [],
        "estimated_minutes": estimated or None,
        "actual_minutes": actual or None,
        "effort_minutes": estimated,
        "remaining_minutes": remaining,
        "estimate_error_minutes": None,
        "deadline": project.deadline,
        "next_action": (best.title if best is not None else ""),
        "dependencies": [],
        "dependency_titles": [],
        "suggested_dependencies": [],
        "blocked_by": [],
        "blocked_by_titles": [],
        "waiting_for": [],
        "resources": [],
        "upstream": sorted(upstream),
        "downstream": sorted(downstream),
        "upstream_levels": _levels(sorted(upstream),
                                   dep_graph.predecessors),
        "downstream_levels": _levels(sorted(downstream),
                                     dep_graph.successors),
        "unlocks_count": len(downstream),
        "outcomes": [],
        "latest_outcome": None,
        "evidence": EV_FACT,
        "confidence": "human-confirmed",
        "provenance": ("imported" if project.description
                       and project.description.startswith("Imported")
                       else "human"),
        "notes": project.description,
        "colour": colours,
        "highlight": prefs.highlights.get(
            _object_id("project", project.project_id)),
        "reasons": [],
        "blockers": [],
        "task_count": total,
        "completed": completed,
        "progress": (round(completed / total, 3) if total else None),
    }


def _levels(seeds: Sequence[str],
            adjacency: Mapping[str, tuple[str, ...]],
            *, max_levels: int = 8) -> list[list[str]]:
    """BFS levels of the transitive closure (display context, no inference).

    Level 0 is the direct neighbourhood; each following level is one hop
    further. No level is inferred from silence.
    """
    if not seeds:
        return []
    seen: set[str] = set(seeds)
    frontier = list(seeds)
    result: list[list[str]] = [sorted(seeds)]
    for _ in range(max_levels):
        nxt: list[str] = []
        for node in frontier:
            for child in adjacency.get(node, ()):
                if child not in seen:
                    seen.add(child)
                    nxt.append(child)
        if not nxt:
            break
        result.append(sorted(nxt))
        frontier = nxt
    return result


# --- decision workflow (Today / Ready) ---------------------------------------

#: Readiness state -> workflow group (display grouping only).
_WORKFLOW_GROUP = {
    readiness_module.RS_READY: "ready",
    readiness_module.RS_IN_PROGRESS: "in_progress",
    readiness_module.RS_BLOCKED: "blocked",
    readiness_module.RS_WAITING: "waiting",
    readiness_module.RS_DEFERRED: "deferred",
    readiness_module.RS_PROJECT_CLOSED: "project_closed",
    readiness_module.RS_COMPLETED: "terminal",
    readiness_module.RS_ABANDONED: "terminal",
}

_WORKFLOW_GROUP_ORDER = (
    "ready", "in_progress", "blocked", "waiting", "deferred",
    "project_closed", "terminal",
)


def _blocking_kind(state: str, reasons: Sequence[str],
                   blockers: Sequence[str],
                   missing_resources: Sequence[str]) -> str:
    """Classify *why* a task is not ready (never inventing a cause)."""
    if state in (readiness_module.RS_READY,
                 readiness_module.RS_IN_PROGRESS):
        return ""
    if blockers:
        return "blocker"
    if any("dependency not complete" in reason for reason in reasons):
        return "dependency"
    if missing_resources:
        return "resource"
    if state == readiness_module.RS_WAITING or any(
            reason.startswith("waiting for") for reason in reasons):
        return "waiting"
    if state == readiness_module.RS_PROJECT_CLOSED:
        return "project"
    return "status"


def _ready_workflow(
    portfolio: model.Portfolio,
    readiness_map: Mapping[str, readiness_module.TaskReadiness],
) -> dict[str, Any]:
    """Group every task by deterministic readiness with an explicit reason.

    This is the source of truth for the Today/Ready views: the browser only
    renders the groups and never re-derives readiness or priority.
    """
    tasks = portfolio.task_map()
    projects = portfolio.project_map()
    groups: dict[str, list[dict[str, Any]]] = {
        name: [] for name in _WORKFLOW_GROUP_ORDER}
    for task_id in sorted(readiness_map):
        item = readiness_map[task_id]
        task = tasks[task_id]
        project = projects[task.project_id]
        group = _WORKFLOW_GROUP.get(item.state, "terminal")
        groups[group].append({
            "task_id": item.task_id,
            "title": task.title,
            "project_id": task.project_id,
            "project": project.name,
            "state": item.state,
            "ready": item.ready,
            "reasons": list(item.reasons),
            "blockers": list(item.blockers),
            "missing_resources": list(item.missing_resources),
            "blocking_kind": _blocking_kind(
                item.state, item.reasons, item.blockers,
                item.missing_resources),
            "blocking_reason": (item.reasons[0] if item.reasons else ""),
            "urgency": task.urgency,
            "impact": task.impact,
            "effort_minutes": task.effort_minutes,
            "deadline": task.deadline,
            "waiting_for": list(task.waiting_for),
        })
    return {
        "groups": groups,
        "group_order": list(_WORKFLOW_GROUP_ORDER),
        "counts": {name: len(items) for name, items in groups.items()},
        "ready_total": (len(groups["ready"]) + len(groups["in_progress"])),
    }


# --- portfolio summary --------------------------------------------------------


def _portfolio_summary(
    portfolio: model.Portfolio,
    projects: Mapping[str, dict[str, Any]],
    tasks: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    project_status: dict[str, int] = {status: 0 for status in
                                      model.PROJECT_STATUSES}
    for project in portfolio.projects:
        project_status[project.status] = project_status.get(
            project.status, 0) + 1
    task_status: dict[str, int] = {status: 0 for status in model.TASK_STATUSES}
    for task in portfolio.tasks:
        task_status[task.status] = task_status.get(task.status, 0) + 1
    completed = task_status.get(model.TS_COMPLETED, 0)
    total = len(portfolio.tasks)
    return {
        "projects": len(portfolio.projects),
        "tasks": total,
        "completed_tasks": completed,
        "progress": (round(completed / total, 3) if total else None),
        "project_status": project_status,
        "task_status": task_status,
        "estimated_minutes": sum(t.estimated_minutes or 0
                                 for t in portfolio.tasks),
        "remaining_minutes": sum(t.estimated_minutes or 0
                                 for t in portfolio.tasks
                                 if t.status not in model.TASK_TERMINAL_STATUSES),
        "domains": sorted({p.domain for p in portfolio.projects}),
    }


# --- WBS ----------------------------------------------------------------------


def _wbs_node(kind: str, key: str, label: str, *, ref_id: str = "",
              status: str = "", evidence: str = EV_FACT,
              ) -> dict[str, Any]:
    return {
        "id": f"{kind.lower()}:{key}",
        "kind": kind,
        "ref_id": ref_id,
        "label": label,
        "status": status,
        "evidence": evidence,
        "progress": None,
        "count": 0,
        "completed": 0,
        "effort_minutes": 0,
        "children": [],
    }


def _build_wbs(
    projects: Mapping[str, dict[str, Any]],
    tasks: Mapping[str, dict[str, Any]],
    portfolio: model.Portfolio,
) -> dict[str, Any]:
    """Build the real hierarchy: AREA -> PROJECT -> WORKSTREAM -> PACKAGE ->
    TASK. Levels with no data are omitted rather than invented."""
    areas: dict[str, dict[str, Any]] = {}
    for project_id in sorted(projects):
        project = projects[project_id]
        area_key = str(project["domain"] or "personal")
        area = areas.setdefault(area_key, _wbs_node(
            "AREA", area_key, area_key))
        project_node = _wbs_node(
            "PROJECT", project_id, str(project["title"]),
            ref_id=project_id, status=str(project["status"]))
        area["children"].append(project_node)

    for task_id in sorted(tasks):
        task = tasks[task_id]
        domain = str(task["domain"] or "personal")
        project_id = str(task["project_id"])
        if domain not in areas or project_id not in projects:
            continue
        area = areas[domain]
        wbs_project_node: dict[str, Any] | None = None
        for candidate in area["children"]:
            if candidate["ref_id"] == project_id:
                wbs_project_node = candidate
                break
        if wbs_project_node is None:
            continue
        workstream_label = str(task["workstream"] or "(no workstream)")
        workstream = _find_or_add(
            wbs_project_node, "WORKSTREAM",
            f"{project_id}:{workstream_label}", workstream_label)
        package_label = str(task["work_package"] or "(no package)")
        package = _find_or_add(
            workstream, "WORK_PACKAGE",
            f"{project_id}:{workstream_label}:{package_label}", package_label)
        task_node = _wbs_node(
            "TASK", str(task_id), str(task["title"]),
            ref_id=str(task_id), status=str(task["status"]),
            evidence=str(task["evidence"]))
        task_node["effort_minutes"] = int(task["estimated_minutes"] or 0)
        task_node["readiness"] = task["readiness"]
        task_node["priority_rank"] = task["priority_rank"]
        task_node["deadline"] = task["deadline"]
        task_node["unlocks_count"] = task["unlocks_count"]
        package["children"].append(task_node)

    roots = sorted(areas.values(), key=lambda node: str(node["label"]))
    for root in roots:
        _aggregate_wbs(root)
    return {"levels": list(WBS_LEVELS), "roots": roots}


def _find_or_add(parent: dict[str, Any], kind: str, key: str,
                 label: str) -> dict[str, Any]:
    children: list[dict[str, Any]] = parent["children"]
    for child in children:
        if child["kind"] == kind and child["id"] == f"{kind.lower()}:{key}":
            return child
    node = _wbs_node(kind, key, label)
    children.append(node)
    return node


def _aggregate_wbs(node: dict[str, Any]) -> None:
    if node["kind"] == "TASK":
        node["count"] = 1
        node["completed"] = 1 if node["status"] == model.TS_COMPLETED else 0
        node["progress"] = (1.0 if node["completed"] else 0.0)
        return
    for child in node["children"]:
        _aggregate_wbs(child)
        node["count"] += child["count"]
        node["completed"] += child["completed"]
        node["effort_minutes"] += child["effort_minutes"]
    node["children"].sort(key=lambda child: (str(child["label"])))
    node["progress"] = (round(node["completed"] / node["count"], 3)
                        if node["count"] else None)


# --- graph --------------------------------------------------------------------


def _build_graph(portfolio: model.Portfolio,
                 tasks: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    for task_id in sorted(tasks):
        task = tasks[task_id]
        nodes.append({
            "id": task_id,
            "object_id": task["id"],
            "title": task["title"],
            "project_id": task["project_id"],
            "domain": task["domain"],
            "status": task["status"],
            "readiness": task["readiness"],
            "evidence": task["evidence"],
            "estimated_minutes": task["estimated_minutes"],
            "unlocks_count": task["unlocks_count"],
        })
    edges: list[dict[str, Any]] = []
    confirmed: set[tuple[str, str, str]] = set()
    for portfolio_task in portfolio.tasks:
        for dep in portfolio_task.dependencies:
            confirmed.add((dep, portfolio_task.task_id, "dependency"))
        for blocker in portfolio_task.blocked_by:
            confirmed.add((blocker, portfolio_task.task_id, "blocker"))
    for source, target, kind in sorted(confirmed):
        edges.append({"from": source, "to": target, "kind": kind,
                      "evidence": EV_FACT,
                      "label": ("depends on" if kind == "dependency"
                                else "blocked by")})
    return {"nodes": nodes, "edges": edges,
            "edge_kinds": ["dependency", "blocker", "suggested"]}


# --- Gantt --------------------------------------------------------------------


def _build_gantt(
    portfolio: model.Portfolio,
    tasks: Mapping[str, dict[str, Any]],
    week: scheduler.WeekPlan,
    day: scheduler.DayPlan,
) -> dict[str, Any]:
    scheduled: dict[str, str] = {}
    for day_plan in week.days:
        for item in day_plan.planned:
            scheduled[item.task_id] = day_plan.day
    for item in day.planned:
        scheduled.setdefault(item.task_id, day.day)
    dated: list[dict[str, Any]] = []
    scheduled_lane: list[dict[str, Any]] = []
    unscheduled: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for task_id in sorted(tasks):
        task = tasks[task_id]
        entry = {
            "id": task_id,
            "object_id": task["id"],
            "title": task["title"],
            "project_id": task["project_id"],
            "project": task["project"],
            "domain": task["domain"],
            "status": task["status"],
            "readiness": task["readiness"],
            "estimated_minutes": task["estimated_minutes"],
            "deadline": task["deadline"],
            "highlight": task["highlight"],
        }
        if task["readiness"] == readiness_module.RS_WAITING:
            waiting.append(entry)
        if task["readiness"] == readiness_module.RS_BLOCKED:
            blocked.append(entry)
        if task["deadline"]:
            dated.append(dict(entry))
        if scheduled.get(task_id):
            scheduled_lane.append({**entry,
                                   "day": scheduled[task_id]})
        elif not task["deadline"]:
            unscheduled.append(dict(entry))
    dates = [str(item["deadline"]) for item in dated if item["deadline"]]
    days = [str(item["day"]) for item in scheduled_lane]
    all_days = sorted(set(dates) | set(days))
    return {
        "dated": dated,
        "scheduled": scheduled_lane,
        "unscheduled": unscheduled,
        "waiting": waiting,
        "blocked": blocked,
        "range": {
            "start": all_days[0] if all_days else None,
            "end": all_days[-1] if all_days else None,
        },
        "today": day.day,
        "has_dates": bool(dates),
        "has_schedule": bool(days),
        "note": ("Tasks without a stored date are shown as unscheduled; "
                 "no date is invented."),
    }


# --- Eisenhower / impact-effort ----------------------------------------------


def _build_eisenhower(objects: Sequence[dict[str, Any]]) -> dict[str, Any]:
    quadrants: dict[str, list[str]] = {
        "DO": [], "SCHEDULE": [], "DELEGATE": [], "ELIMINATE": [],
    }
    assignments: dict[str, dict[str, Any]] = {}
    for obj in objects:
        urgent = obj["urgency"] in URGENT_URGENCIES
        important = obj["impact"] in IMPORTANT_IMPACTS
        if urgent and important:
            quadrant = "DO"
        elif important:
            quadrant = "SCHEDULE"
        elif urgent:
            quadrant = "DELEGATE"
        else:
            quadrant = "ELIMINATE"
        quadrants[quadrant].append(obj["id"])
        assignments[obj["id"]] = {
            "urgent": urgent,
            "important": important,
            "quadrant": quadrant,
            "urgency": obj["urgency"],
            "impact": obj["impact"],
        }
    return {"axis": {"x": "urgency", "y": "impact"},
            "quadrants": quadrants, "assignments": assignments,
            "labels": {"DO": "Do now", "SCHEDULE": "Schedule",
                       "DELEGATE": "Delegate", "ELIMINATE": "Eliminate"}}


def _build_impact_effort(objects: Sequence[dict[str, Any]]) -> dict[str, Any]:
    assignments: dict[str, dict[str, Any]] = {}
    quadrants: dict[str, list[str]] = {
        "quick_win": [], "major_bet": [], "filler": [], "low_return": [],
        "midfield": [], "unknown_effort": [],
    }
    for obj in objects:
        effort = obj.get("effort_minutes") or 0
        impact_high = obj["impact"] in IMPORTANT_IMPACTS
        if not effort:
            quadrant = "unknown_effort"
        elif impact_high and effort <= QUICK_WIN_MAX_MINUTES:
            quadrant = "quick_win"
        elif impact_high and effort >= MAJOR_BET_MIN_MINUTES:
            quadrant = "major_bet"
        elif not impact_high and effort <= QUICK_WIN_MAX_MINUTES:
            quadrant = "filler"
        elif not impact_high and effort >= MAJOR_BET_MIN_MINUTES:
            quadrant = "low_return"
        else:
            quadrant = "midfield"
        quadrants[quadrant].append(obj["id"])
        assignments[obj["id"]] = {
            "impact_rank": obj["impact_rank"],
            "effort_minutes": obj.get("effort_minutes"),
            "quadrant": quadrant,
        }
    return {
        "axis": {"x": "effort", "y": "impact"},
        "thresholds": {"quick_win_max": QUICK_WIN_MAX_MINUTES,
                       "major_bet_min": MAJOR_BET_MIN_MINUTES},
        "quadrants": quadrants,
        "assignments": assignments,
        "effort_basis": "estimated (never actual)",
    }


# --- portfolio map ------------------------------------------------------------


def _build_portfolio_map(projects: Mapping[str, dict[str, Any]]
                         ) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    for project_id in sorted(projects):
        project = projects[project_id]
        objects.append({
            "id": project["id"],
            "title": project["title"],
            "domain": project["domain"],
            "status": project["status"],
            "urgency_rank": project["urgency_rank"],
            "impact_rank": project["impact_rank"],
            "effort_minutes": project["effort_minutes"],
            "progress": project["progress"],
            "task_count": project["task_count"],
            "unlocks_count": project["unlocks_count"],
            "highlight": project["highlight"],
        })
    return {
        "axes": ["urgency", "impact", "effort", "progress"],
        "objects": objects,
        "sizes": ["task_count", "effort_minutes", "remaining_minutes",
                  "unlocks_count"],
    }


# --- treemap ------------------------------------------------------------------


def _build_treemap(portfolio: model.Portfolio) -> dict[str, Any]:
    metrics = ("task_count", "estimated_minutes", "remaining_minutes")
    groupings = {
        "domain": _treemap_grouping(portfolio, ("domain", "project",
                                                 "workstream")),
        "project": _treemap_grouping(portfolio, ("project", "workstream")),
        "workstream": _treemap_grouping(portfolio, ("workstream", "project")),
    }
    return {"metrics": list(metrics), "groupings": groupings}


def _treemap_grouping(portfolio: model.Portfolio,
                      levels: Sequence[str]) -> dict[str, Any]:
    project_map = portfolio.project_map()
    root = _tm_node("portfolio", "Portfolio")

    def keys_for(task: model.Task) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for level in levels:
            if level == "domain":
                result.append(("domain", project_map[task.project_id].domain))
            elif level == "project":
                result.append(("project", task.project_id))
            elif level == "workstream":
                result.append(("workstream", task.workstream or "(none)"))
        return result

    for task in portfolio.tasks:
        node = root
        for key in keys_for(task):
            node = node["_children"].setdefault(
                ":".join(key), _tm_node(key[0], key[1]))
        node["task_count"] += 1
        node["estimated_minutes"] += task.estimated_minutes or 0
        if task.status not in model.TASK_TERMINAL_STATUSES:
            node["remaining_minutes"] += task.estimated_minutes or 0

    def finalise(node: dict[str, Any]) -> None:
        for child in node["_children"].values():
            finalise(child)
            node["task_count"] += child["task_count"]
            node["estimated_minutes"] += child["estimated_minutes"]
            node["remaining_minutes"] += child["remaining_minutes"]
        node["children"] = sorted(
            (finalise_strip(child) for child in node["_children"].values()),
            key=lambda child: (-child["task_count"], str(child["label"])))

    def finalise_strip(node: dict[str, Any]) -> dict[str, Any]:
        stripped = {key: value for key, value in node.items()
                    if key != "_children"}
        return stripped

    finalise(root)
    return {key: value for key, value in root.items() if key != "_children"}


def _tm_node(key: str, label: str) -> dict[str, Any]:
    return {"key": key, "label": label, "task_count": 0,
            "estimated_minutes": 0, "remaining_minutes": 0,
            "children": [], "_children": {}}


# --- progress -----------------------------------------------------------------


def _build_progress(
    portfolio: model.Portfolio,
    projects: Mapping[str, dict[str, Any]],
    records: Sequence[model.OutcomeRecord],
) -> dict[str, Any]:
    completed = [task for task in portfolio.tasks
                 if task.status == model.TS_COMPLETED]
    total = len(portfolio.tasks)
    pva = outcomes_module.planned_vs_actual(portfolio, tuple(records))
    throughput: dict[str, dict[str, int]] = defaultdict(
        lambda: {"completed": 0, "actual_minutes": 0})
    for record in records:
        day = record.recorded_at[:10] if record.recorded_at else "unknown"
        if record.outcome == model.O_COMPLETED:
            throughput[day]["completed"] += 1
        if record.actual_minutes is not None:
            throughput[day]["actual_minutes"] += record.actual_minutes
    days = [{"day": day, **values}
            for day, values in sorted(throughput.items())]
    return {
        "overall": {
            "projects": len(portfolio.projects),
            "tasks": total,
            "completed": len(completed),
            "remaining": total - len(completed),
            "progress": (round(len(completed) / total, 3) if total else None),
            "estimated_minutes": sum(t.estimated_minutes or 0
                                     for t in portfolio.tasks),
            "actual_minutes": sum(t.actual_minutes or 0
                                  for t in portfolio.tasks),
        },
        "projects": [
            {
                "id": projects[pid]["id"],
                "project_id": pid,
                "title": projects[pid]["title"],
                "status": projects[pid]["status"],
                "completed": projects[pid]["completed"],
                "tasks": projects[pid]["task_count"],
                "progress": projects[pid]["progress"],
                "estimated_minutes": projects[pid]["estimated_minutes"],
                "actual_minutes": projects[pid]["actual_minutes"],
                "remaining_minutes": projects[pid]["remaining_minutes"],
            }
            for pid in sorted(projects)
        ],
        "planned_vs_actual": pva.to_dict(),
        "throughput": {
            "days": days,
            "insufficient_data": len([d for d in days
                                      if d["day"] != "unknown"]) < 2,
        },
    }


# --- heatmap ------------------------------------------------------------------


def _build_heatmap(
    portfolio: model.Portfolio,
    records: Sequence[model.OutcomeRecord],
    week: scheduler.WeekPlan,
) -> dict[str, Any]:
    activity: dict[str, dict[str, int]] = defaultdict(
        lambda: {"completed": 0, "actual_minutes": 0})
    for record in records:
        day = record.recorded_at[:10] if record.recorded_at else "unknown"
        if record.outcome == model.O_COMPLETED:
            activity[day]["completed"] += 1
        if record.actual_minutes is not None:
            activity[day]["actual_minutes"] += record.actual_minutes
    activity_days = [{"day": day, **values}
                     for day, values in sorted(activity.items())
                     if day != "unknown"]
    max_completed = 0
    for entry in activity_days:
        value = entry.get("completed", 0)
        if isinstance(value, int):
            max_completed = max(max_completed, value)
    planned_days: list[dict[str, Any]] = []
    for day_plan in week.days:
        planned_days.append({
            "day": day_plan.day,
            "planned_minutes": day_plan.planned_minutes,
            "plan_capacity": day_plan.plan_capacity,
            "planned": len(day_plan.planned),
        })
    projects = portfolio.project_map()
    domains: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tasks": 0, "completed": 0})
    for task in portfolio.tasks:
        domain = projects[task.project_id].domain
        domains[domain]["tasks"] += 1
        if task.status == model.TS_COMPLETED:
            domains[domain]["completed"] += 1
    return {
        "activity": {
            "days": activity_days,
            "max_completed": max_completed,
            "insufficient_data": len(activity_days) < 2,
        },
        "planned_load": {
            "days": planned_days,
            "insufficient_data": not any(d["planned"] for d in planned_days),
        },
        "domains": [
            {"domain": domain, **values,
             "progress": (round(values["completed"] / values["tasks"], 3)
                          if values["tasks"] else None)}
            for domain, values in sorted(domains.items())
        ],
        "note": ("Activity is derived only from recorded outcomes; missing "
                 "history is shown as insufficient data."),
    }


# --- goal / outcome flow ------------------------------------------------------


def _build_goal_flow(
    portfolio: model.Portfolio,
    tasks: Mapping[str, dict[str, Any]],
    records: Sequence[model.OutcomeRecord],
) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []

    def add(node_id: str, kind: str, label: str, **extra: Any) -> None:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "kind": kind, "label": label,
                              **extra}

    for project in portfolio.projects:
        area_id = f"area:{project.domain}"
        add(area_id, "AREA", project.domain)
        project_id = f"project:{project.project_id}"
        add(project_id, "PROJECT", project.name, status=project.status,
            domain=project.domain)
        edges.append({"from": area_id, "to": project_id, "kind": "contains"})

    for task in portfolio.tasks:
        project = portfolio.project_map()[task.project_id]
        workstream_id = (f"workstream:{task.project_id}:"
                         f"{task.workstream or '(none)'}")
        add(workstream_id, "WORKSTREAM",
            task.workstream or "(no workstream)",
            project_id=task.project_id)
        edges.append({"from": f"project:{task.project_id}",
                      "to": workstream_id, "kind": "contains"})
        task_node_id = f"task:{task.task_id}"
        task_obj = tasks[task.task_id]
        add(task_node_id, "TASK", task.title, status=task.status,
            project_id=task.project_id, readiness=task_obj["readiness"])
        edges.append({"from": workstream_id, "to": task_node_id,
                      "kind": "contains"})
        if task.deliverable:
            deliverable_id = (f"deliverable:{task.project_id}:"
                              f"{task.deliverable}")
            add(deliverable_id, "DELIVERABLE", task.deliverable,
                project_id=task.project_id)
            edges.append({"from": task_node_id, "to": deliverable_id,
                          "kind": "produces"})

    for record in records:
        task_id = f"task:{record.task_id}"
        if task_id not in nodes:
            continue
        outcome_id = (f"outcome:{record.task_id}:{record.recorded_at}")
        add(outcome_id, "OUTCOME",
            f"{record.outcome} · {record.recorded_at[:10]}",
            outcome=record.outcome, recorded_at=record.recorded_at,
            actual_minutes=record.actual_minutes)
        edges.append({"from": task_id, "to": outcome_id,
                      "kind": "recorded"})

    columns = ["AREA", "PROJECT", "WORKSTREAM", "TASK", "DELIVERABLE",
               "OUTCOME"]
    ordered = sorted(nodes.values(),
                     key=lambda node: (columns.index(str(node["kind"])),
                                       str(node["id"])))
    return {
        "columns": columns,
        "nodes": ordered,
        "edges": edges,
        "has_outcomes": any(node["kind"] == "OUTCOME"
                            for node in ordered),
    }


__all__ = [
    "ATTRIBUTE_CATALOG", "EVIDENCES", "EV_FACT", "EV_INFERRED",
    "EV_SUGGESTED", "EV_UNKNOWN", "IMPORTANT_IMPACTS", "URGENT_URGENCIES",
    "WBS_LEVELS", "build_views",
]
