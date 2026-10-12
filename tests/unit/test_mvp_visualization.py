"""Unit tests for the MVP visual execution & decision layer (view model).

Covers the WBS hierarchy, dependency-graph payload, focus context derivation,
Gantt handling of unscheduled work, Eisenhower/impact-effort quadrants,
attribute exposure, colour derivation/persistence, evidence labels, the
read-only guarantee of visual projections, and embedded-JS syntax.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.mvp import (
    cockpit_ui,
    dataset,
    model,
    store,
    uistate,
    visualization,
)


def _views(root: str) -> dict[str, Any]:
    dataset.write_seed(root)
    return visualization.build_views(root)


def _find_node(node: dict[str, Any], kind: str,
               label: str) -> dict[str, Any] | None:
    if node["kind"] == kind and node["label"] == label:
        return node
    for child in node["children"]:
        found = _find_node(child, kind, label)
        if found is not None:
            return found
    return None


# --- WBS ----------------------------------------------------------------------


def test_wbs_hierarchy_payload(tmp_path: object) -> None:
    root = str(tmp_path)
    views = _views(root)
    assert views["kind"] == "mvp_views"
    assert views["wbs"]["roots"], "WBS must have area roots"
    areas = {node["label"] for node in views["wbs"]["roots"]}
    assert "community" in areas
    community = next(n for n in views["wbs"]["roots"]
                     if n["label"] == "community")
    project = _find_node(community, "PROJECT", "Community Garden Planner")
    assert project is not None
    workstream = _find_node(project, "WORKSTREAM", "volunteering")
    assert workstream is not None
    package = _find_node(workstream, "WORK_PACKAGE", "planting board")
    assert package is not None
    task = _find_node(package, "TASK", "Publish the planting board")
    assert task is not None
    assert task["ref_id"] == "garden.build-board"
    # Aggregated effort/progress are real values, never invented placeholders.
    assert project["count"] > 0
    assert project["effort_minutes"] > 0
    assert project["progress"] is not None


def test_wbs_preserves_deep_hierarchy(tmp_path: object) -> None:
    root = str(tmp_path)
    views = _views(root)
    community = next(n for n in views["wbs"]["roots"]
                     if n["label"] == "community")
    project = _find_node(community, "PROJECT", "Community Garden Planner")
    assert project is not None
    assert project["children"], "project must contain workstreams"
    assert all(child["kind"] == "WORKSTREAM" for child in project["children"])
    workstream = project["children"][0]
    assert workstream["children"], "workstream must contain packages"
    assert all(child["kind"] == "WORK_PACKAGE"
               for child in workstream["children"])


# --- dependency graph ---------------------------------------------------------


def test_dependency_graph_payload(tmp_path: object) -> None:
    root = str(tmp_path)
    views = _views(root)
    graph = views["graph"]
    ids = {node["id"] for node in graph["nodes"]}
    assert "garden.build-board" in ids
    edge_set = {(e["from"], e["to"], e["kind"]) for e in graph["edges"]}
    assert ("garden.plot-model", "garden.build-board",
            "dependency") in edge_set
    assert ("garden.plot-model", "roster.messages", "blocker") in edge_set
    for edge in graph["edges"]:
        assert edge["from"] in ids and edge["to"] in ids
        assert edge["evidence"] == "FACT"
    assert "suggested" in graph["edge_kinds"]


def test_graph_confirmed_edges_only(tmp_path: object) -> None:
    root = str(tmp_path)
    views = _views(root)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    confirmed = set()
    for task in portfolio.tasks:
        for dep in task.dependencies:
            confirmed.add((dep, task.task_id))
        for blocker in task.blocked_by:
            confirmed.add((blocker, task.task_id))
    actual = {(e["from"], e["to"]) for e in views["graph"]["edges"]}
    assert actual == confirmed


# --- focus context ------------------------------------------------------------


def test_focus_upstream_downstream_levels(tmp_path: object) -> None:
    root = str(tmp_path)
    views = _views(root)
    index = views["object_index"]
    apply_task = index["task:garden.build-board"]
    assert set(apply_task["upstream"]) >= {"garden.plot-model",
                                           "garden.volunteer-list"}
    assert set(apply_task["upstream_levels"][0]) >= {
        "garden.plot-model", "garden.volunteer-list"}
    update_task = index["task:garden.plot-model"]
    assert update_task["downstream"], "expected direct downstream unlocks"
    assert update_task["downstream_levels"], "expected downstream levels"
    # Transitive unlock count is derived deterministically.
    assert update_task["unlocks_count"] >= 1


# --- Gantt --------------------------------------------------------------------


def test_gantt_unscheduled_lane(tmp_path: object) -> None:
    root = str(tmp_path)
    views = _views(root)
    gantt = views["gantt"]
    assert gantt["note"]
    # A task with neither deadline nor plan day is honestly unscheduled.
    assert any(t["id"] == "talk.slides" for t in gantt["unscheduled"])
    scheduled_ids = {t["id"] for t in gantt["scheduled"]}
    unscheduled_ids = {t["id"] for t in gantt["unscheduled"]}
    assert scheduled_ids.isdisjoint(unscheduled_ids)


def test_gantt_does_not_invent_dates(tmp_path: object) -> None:
    root = str(tmp_path)
    # Portfolio without any deadlines: dated must be empty, not fabricated.
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="plain",
        projects=(model.Project(project_id="p", name="P",
                                objective="obj"),),
        tasks=(model.Task(task_id="t", project_id="p", title="T"),),
    ))
    views = visualization.build_views(root)
    assert views["gantt"]["dated"] == []
    assert any(t["id"] == "t" for t in views["gantt"]["unscheduled"])


# --- Eisenhower / impact-effort ----------------------------------------------


def test_eisenhower_quadrant_mapping(tmp_path: object) -> None:
    root = str(tmp_path)
    views = _views(root)
    assignments = views["eisenhower"]["assignments"]
    apply_task = assignments["task:garden.build-board"]
    assert apply_task["urgent"] is True
    assert apply_task["important"] is True
    assert apply_task["quadrant"] == "DO"
    low = assignments["task:talk.slides"]
    assert low["urgent"] is False and low["important"] is False
    assert low["quadrant"] == "ELIMINATE"


def test_impact_effort_quadrants(tmp_path: object) -> None:
    root = str(tmp_path)
    views = _views(root)
    assignments = views["impact_effort"]["assignments"]
    quick = assignments["task:renewal.appt"]
    assert quick["quadrant"] == "quick_win"
    assert views["impact_effort"]["effort_basis"].startswith("estimated")


# --- attributes / evidence ----------------------------------------------------


def test_task_attributes_exposed(tmp_path: object) -> None:
    root = str(tmp_path)
    views = _views(root)
    task = views["object_index"]["task:garden.build-board"]
    required = {
        "title", "kind", "status", "project", "domain", "workstream",
        "work_package", "readiness", "urgency", "impact", "priority_rank",
        "priority_reasons", "estimated_minutes", "actual_minutes",
        "deadline", "next_action", "dependencies", "suggested_dependencies",
        "blocked_by", "waiting_for", "downstream", "upstream_levels",
        "downstream_levels", "deliverable", "outcomes", "provenance",
        "evidence", "confidence", "colour", "highlight",
    }
    assert required <= set(task)
    assert task["evidence"] in visualization.EVIDENCES
    assert set(task["colour"]) == set(uistate.COLOR_MODES)


def test_attribute_catalog_covers_density_modes() -> None:
    densities = {entry["density"] for entry in visualization.ATTRIBUTE_CATALOG}
    assert densities == {"compact", "normal", "detailed"}
    keys = {entry["key"] for entry in visualization.ATTRIBUTE_CATALOG}
    for key in ("title", "status", "urgency", "impact", "priority",
                "estimated_minutes", "outcomes", "provenance", "evidence"):
        assert key in keys


# --- colour system ------------------------------------------------------------


def test_colour_modes_are_semantic_and_stable() -> None:
    prefs = uistate.default_preferences()
    assert uistate.resolve_colour("status", status="BLOCKED",
                                  prefs=prefs) == "#e45756"
    assert uistate.resolve_colour("urgency", urgency="CRITICAL",
                                  prefs=prefs) == "#b3261e"
    assert uistate.resolve_colour("impact", impact="HIGH",
                                  prefs=prefs) == "#1f77b4"
    first = uistate.resolve_colour("domain", domain="career", prefs=prefs)
    second = uistate.resolve_colour("domain", domain="career", prefs=prefs)
    assert first == second
    assert first != uistate.resolve_colour("domain", domain="finance",
                                           prefs=prefs)


def test_identity_colour_override() -> None:
    prefs = uistate.set_identity_colour(uistate.default_preferences(),
                                        "domain", "career", "#123456")
    assert uistate.resolve_colour("domain", domain="career",
                                  prefs=prefs) == "#123456"


def test_preferences_persistence_after_restart(tmp_path: object) -> None:
    root = str(tmp_path)
    prefs = uistate.default_preferences()
    prefs = uistate.apply_update(prefs, {"color_by": "status",
                                         "density": "detailed",
                                         "view": "kanban"})
    prefs = uistate.set_highlight(prefs, "task:x", "yellow")
    uistate.save_preferences(root, prefs)
    # Simulate a restart: re-read from disk.
    reloaded = uistate.load_preferences(root)
    assert reloaded.color_by == "status"
    assert reloaded.density == "detailed"
    assert reloaded.view == "kanban"
    assert reloaded.highlights["task:x"] == "yellow"


def test_preferences_fail_closed_on_write(tmp_path: object) -> None:
    prefs = uistate.default_preferences()
    with pytest.raises(model.MvpError):
        uistate.apply_update(prefs, {"unknown": 1})
    with pytest.raises(model.MvpError):
        uistate.apply_update(prefs, {"color_by": "not-a-mode"})
    with pytest.raises(model.MvpError):
        uistate.set_highlight(prefs, "task:x", "not-a-colour")


def test_preferences_fail_soft_on_corrupt_file(tmp_path: object) -> None:
    root = str(tmp_path)
    Path(root, "ui_state.json").write_text("{not json", encoding="utf-8")
    prefs = uistate.load_preferences(root)
    assert prefs.color_by == "domain"


def test_personal_highlight_does_not_change_business_status(tmp_path: object) -> None:
    root = str(tmp_path)
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="p",
        projects=(model.Project(project_id="p", name="P",
                                objective="obj"),),
        tasks=(model.Task(task_id="t", project_id="p", title="T",
                          status=model.TS_TODO),),
    ))
    prefs = uistate.set_highlight(uistate.default_preferences(),
                                  "task:t", "purple")
    uistate.save_preferences(root, prefs)
    views = visualization.build_views(root)
    task = views["object_index"]["task:t"]
    assert task["highlight"] == "purple"
    assert task["status"] == model.TS_TODO


# --- read-only guarantee ------------------------------------------------------


def test_visual_projection_never_mutates_portfolio(tmp_path: object) -> None:
    root = str(tmp_path)
    dataset.write_seed(root)
    path = store.default_portfolio_path(root)
    before = path.read_bytes()
    visualization.build_views(root)
    visualization.build_views(root)
    assert path.read_bytes() == before


def test_visual_only_operations_do_not_write_portfolio(tmp_path: object) -> None:
    root = str(tmp_path)
    dataset.write_seed(root)
    path = store.default_portfolio_path(root)
    before = path.read_bytes()
    prefs = uistate.default_preferences()
    uistate.save_preferences(root, uistate.apply_update(
        prefs, {"view": "graph"}))
    uistate.save_preferences(root, uistate.set_highlight(
        prefs, "project:garden-planner", "blue"))
    assert path.read_bytes() == before


# --- embedded JS --------------------------------------------------------------


def test_embedded_js_is_valid(tmp_path: object) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = Path(str(tmp_path)) / "cockpit.js"
    script.write_text(cockpit_ui.PAGE_JS, encoding="utf-8")
    result = subprocess.run([node, "--check", str(script)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_view_bar_lists_all_views() -> None:
    for view in ("kanban", "wbs", "graph", "gantt", "eisenhower",
                 "impact_effort", "portfolio_map", "treemap", "progress",
                 "heatmap", "goal_flow"):
        assert "'" + view + "'" in cockpit_ui.PAGE_JS


def test_suggested_visual_distinction_present() -> None:
    # SUGGESTED content must be visually distinguished everywhere it appears.
    assert "badge-SUGGESTED" in cockpit_ui.PAGE_CSS
    assert "badge-INFERRED" in cockpit_ui.PAGE_CSS
    assert "stroke-dasharray" in cockpit_ui.PAGE_JS


def test_views_payload_is_json_serialisable(tmp_path: object) -> None:
    root = str(tmp_path)
    views = _views(root)
    encoded = json.dumps(views)
    assert "mvp_views" in encoded
