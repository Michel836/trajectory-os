"""Mission 015 — read-only adaptive-replanning output (inspect, explain).

Every human-readable or machine-readable projection here is derived only
from the canonical persisted graph plus the append-only replan history.
Nothing in this module mutates state, writes files or invents evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trajectory_os.graph import store as graph_store
from trajectory_os.graph.replan import engine, model, store


def status_document(root: str, goal_id: str) -> dict[str, Any]:
    """Current-generation status document (read-only, fail closed)."""
    graph, _ = graph_store.load_graph(root, goal_id)
    state = store.reconstruct(root, goal_id, graph)
    return {
        "status": "OK",
        "schema_version": model.SCHEMA_VERSION,
        "replan_version": model.REPLAN_VERSION,
        "goal_id": goal_id,
        "active_graph_id": graph.graph_id,
        "generation_id": state.generation.generation_id,
        "generation_number": state.generation.generation_number,
        "parent_generation_id": state.generation.parent_generation_id,
        "trigger_id": state.generation.trigger_id,
        "plan_id": state.generation.plan_id,
        "activated_at": state.generation.activated_at,
        "generation_count": len(state.generations),
        "event_count": len(state.events),
        "generations": [g.to_dict() for g in state.generations],
        "events": [e.to_dict() for e in state.events],
    }


def history_document(root: str, goal_id: str) -> dict[str, Any]:
    """Append-only replan history document (read-only, fail closed)."""
    graph, _ = graph_store.load_graph(root, goal_id)
    state = store.reconstruct(root, goal_id, graph)
    return {
        "status": "OK",
        "goal_id": goal_id,
        "generation_id": state.generation.generation_id,
        "count": len(state.events),
        "events": [e.to_dict() for e in state.events],
        "generations": [g.generation_id for g in state.generations],
    }


def explain_document(root: str, goal_id: str,
                     node_id: str) -> dict[str, Any]:
    """Explain one node's replan evolution with stable reason codes."""
    graph, _ = graph_store.load_graph(root, goal_id)
    state = store.reconstruct(root, goal_id, graph)
    node_map = graph.node_map()
    if node_id not in node_map:
        return {
            "status": "UNKNOWN_NODE",
            "goal_id": goal_id,
            "node_id": node_id,
            "node": None,
        }
    node = node_map[node_id]
    related = [
        event.to_dict() for event in state.events
        if _event_touches_node(event, node_id)
    ]
    return {
        "status": "OK",
        "goal_id": goal_id,
        "generation_id": state.generation.generation_id,
        "node_id": node_id,
        "node": node.to_dict(),
        "reuse_inputs": [r.to_dict() for r in node.reuse_inputs],
        "replan_events": related,
    }


def _event_touches_node(event: model.ReplanEvent, node_id: str) -> bool:
    for change in event.changes:
        if node_id in (change.get("node_id"),
                       change.get("dependency_from"),
                       change.get("dependency_to"),
                       change.get("consumer_node_id")):
            return True
        spec = change.get("node_spec")
        if isinstance(spec, Mapping) and spec.get("node_id") == node_id:
            return True
    return False


def projection_document(root: str, goal_id: str) -> dict[str, Any]:
    """M016-facing machine-readable replanning projection."""
    return engine.machine_projection(root, goal_id)


def render_status(document: Mapping[str, Any]) -> str:
    lines = [
        f"goal       : {document['goal_id']}",
        f"graph      : {document['active_graph_id']}",
        f"generation : {document['generation_id']} "
        f"(#{document['generation_number']})",
        f"parent     : {document['parent_generation_id'] or '-'}",
        f"trigger    : {document['trigger_id'] or '-'}",
        f"plan       : {document['plan_id'] or '-'}",
        f"activated  : {document['activated_at'] or '-'}",
        f"history    : generations={document['generation_count']} "
        f"events={document['event_count']}",
    ]
    return "\n".join(lines)


def render_history(document: Mapping[str, Any]) -> str:
    lines = [
        f"goal       : {document['goal_id']} events={document['count']}",
        f"generation : {document['generation_id']}",
    ]
    for event in document["events"]:
        lines.append(
            f"  {event['created_at']} {event['event']} "
            f"{event['status']} {event['reason']} "
            f"gen={event.get('generation_id') or '-'}")
    return "\n".join(lines)


def render_explain(document: Mapping[str, Any]) -> str:
    if document["node"] is None:
        return f"unknown node: {document['node_id']}"
    node = document["node"]
    lines = [
        f"node       : {node['node_id']} {node['title']}",
        f"generation : {document['generation_id']}",
        f"reuse      : {len(document['reuse_inputs'])} declared",
    ]
    for event in document["replan_events"]:
        lines.append(
            f"  {event['created_at']} {event['reason']} "
            f"gen={event.get('generation_id') or '-'}")
    if not document["replan_events"]:
        lines.append("  (no replan events reference this node)")
    return "\n".join(lines)
