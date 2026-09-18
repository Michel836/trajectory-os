"""Mission 012 — read-only goal graph output (inspect, explain, render).

Every human-readable or machine-readable projection here is derived only
from the canonical persisted graph document plus read-only resolved mission
evidence. Nothing in this module mutates state, writes files or invents
evidence, so operator and machine output share one source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trajectory_os.graph import model, readiness, store


def node_view(graph_node: model.GraphNode,
              status: readiness.NodeReadiness) -> dict[str, Any]:
    """One node: normalized structure plus its derived readiness (never merged)."""
    view = graph_node.to_dict()
    view.update({
        "state": status.state,
        "eligible": status.eligible,
        "state_reason": status.reason,
        "own_status": status.own_status,
        "dependencies": [dep.to_dict() for dep in status.dependencies],
        "mission": None if status.mission is None else status.mission.to_dict(),
    })
    return view


def inspect(root: str, goal_id: str) -> dict[str, Any]:
    """Canonical operator document for one goal graph (read-only)."""
    graph, _ = store.load_graph(root, goal_id)
    projection = readiness.project_with_store(root, graph)
    statuses = projection.by_id()
    return {
        "status": "OK",
        "schema_version": model.SCHEMA_VERSION,
        "goal_id": graph.goal_id,
        "objective": graph.objective,
        "graph_id": graph.graph_id,
        "spec_sha256": graph.spec_sha256,
        "provenance": graph.provenance.to_dict(),
        "creation_events": store.load_events(root, goal_id),
        "size": {"nodes": len(graph.nodes), "edges": len(graph.edges)},
        "topological_order": list(projection.topological_order),
        "counts": projection.counts(),
        "ready": list(projection.ready()),
        "blocked": list(projection.blocked()),
        "nodes": [
            node_view(graph.node_map()[node_id], statuses[node_id])
            for node_id in projection.topological_order
        ],
        "edges": [edge.to_dict() for edge in graph.edges],
        "scheduler": readiness.scheduler_projection(graph, projection),
    }


def explain(root: str, goal_id: str, node_id: str) -> dict[str, Any]:
    """Deterministic dependency-block explanation for one node."""
    document = inspect(root, goal_id)
    for node in document["nodes"]:
        if node["node_id"] == node_id:
            return {
                "status": "OK",
                "goal_id": goal_id,
                "graph_id": document["graph_id"],
                "node": node,
                "topological_order": document["topological_order"],
            }
    return {
        "status": "UNKNOWN_NODE",
        "goal_id": goal_id,
        "graph_id": document["graph_id"],
        "node": None,
        "topological_order": document["topological_order"],
    }


def _format_list(values: list[str]) -> str:
    return " ".join(values) if values else "-"


def render_inspect(document: Mapping[str, Any]) -> str:
    """Human-readable rendering derived from the canonical machine document."""
    provenance = document["provenance"]
    repository = provenance["repository"]
    size = document["size"]
    counts = document["counts"]
    lines = [
        f"goal      : {document['goal_id']}",
        f"objective : {document['objective']}",
        f"graph     : {document['graph_id']}",
        f"spec      : {document['spec_sha256']}",
        f"created   : {provenance['created_at']} by {provenance['created_by']}",
        f"repo      : {repository['repo_root']} "
        f"baseline={repository['baseline_revision']}",
        f"size      : nodes={size['nodes']} edges={size['edges']}",
        f"order     : {_format_list(list(document['topological_order']))}",
        f"ready     : {counts['READY']} "
        f"[{_format_list(list(document['ready']))}]",
        f"blocked   : {counts['BLOCKED']} "
        f"[{_format_list(list(document['blocked']))}]",
        f"complete  : {counts['COMPLETE']}",
        f"inflight  : {counts['IN_PROGRESS']}",
        f"unresolved: {counts['UNRESOLVED']} invalid={counts['INVALID']}",
        "nodes:",
    ]
    for node in document["nodes"]:
        mission = node["mission"]
        mission_text = "-"
        if mission is not None:
            mission_text = (
                f"{mission['mission_id']} "
                f"({mission['state']}/{mission['error'] or 'ok'})")
        lines.append(
            f"  - {node['node_id']} [{node['state']}] "
            f"priority={node['priority']} ac={len(node['acceptance_criteria'])} "
            f"deps={_format_list(list(node['depends_on']))} "
            f"mission={mission_text} reason={node['state_reason']}")
    return "\n".join(lines)


def render_explain(document: Mapping[str, Any]) -> str:
    """Human-readable dependency-block explanation for one node."""
    if document["node"] is None:
        return f"node not found: goal={document['goal_id']}"
    node = document["node"]
    lines = [
        f"node      : {node['node_id']}",
        f"title     : {node['title']}",
        f"state     : {node['state']} ({node['state_reason']})",
        f"eligible  : {node['eligible']}",
        f"priority  : {node['priority']}",
        f"own       : {node['own_status']}",
    ]
    mission = node["mission"]
    if mission is not None:
        lines.append(
            f"mission   : {mission['mission_id']} "
            f"state={mission['state']} error={mission['error']} "
            f"proven={mission['proven_complete']}")
    lines.append("depends:")
    if not node["dependencies"]:
        lines.append("  - (none)")
    for dep in node["dependencies"]:
        lines.append(
            f"  - {dep['node_id']} [{dep['state']}] "
            f"proven={dep['proven']} {dep['reason']}")
    criteria = node["acceptance_criteria"]
    lines.append("acceptance:")
    for criterion in criteria:
        lines.append(
            f"  - {criterion['criterion_id']}: {criterion['statement']}")
    return "\n".join(lines)


def render_nodes(document: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for node in document["nodes"]:
        lines.append(
            f"{node['node_id']}  {node['state']}  priority={node['priority']}  "
            f"deps={_format_list(list(node['depends_on']))}")
    return "\n".join(lines) if lines else "(no nodes)"


def render_edges(document: Mapping[str, Any]) -> str:
    lines = [
        f"{edge['from']} -> {edge['to']}"
        for edge in document["edges"]
    ]
    return "\n".join(lines) if lines else "(no edges)"


def render_order(document: Mapping[str, Any]) -> str:
    return _format_list(list(document["topological_order"]))


def render_ready(document: Mapping[str, Any]) -> str:
    ready = list(document["ready"])
    lines = [f"ready ({len(ready)}): {_format_list(ready)}"]
    for node in document["nodes"]:
        if node["state"] == readiness.RS_READY:
            lines.append(
                f"  {node['node_id']}  priority={node['priority']}  "
                f"{node['state_reason']}")
    return "\n".join(lines)


def render_blocked(document: Mapping[str, Any]) -> str:
    blocked = list(document["blocked"])
    lines = [f"blocked ({len(blocked)}): {_format_list(blocked)}"]
    for node in document["nodes"]:
        if node["state"] != readiness.RS_BLOCKED:
            continue
        unproven = [
            f"{dep['node_id']}={dep['state']}"
            for dep in node["dependencies"] if not dep["proven"]
        ]
        lines.append(
            f"  {node['node_id']}  {node['state_reason']}  "
            f"blocked_by={_format_list(unproven)}")
    return "\n".join(lines)
