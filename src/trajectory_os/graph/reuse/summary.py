"""Mission 014 — read-only cross-mission reuse operator projections.

Every human-readable or machine-readable projection here is derived from the
canonical persisted graph plus read-only resolved mission evidence. Nothing
mutates state, writes files or invents evidence, so operator and machine
consumers share one source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trajectory_os.graph import store as graph_store
from trajectory_os.graph.reuse import model, resolver, store


def status_document(root: str, goal_id: str) -> dict[str, Any]:
    """Canonical read-only reuse status for one goal graph."""
    graph, _ = graph_store.load_graph(root, goal_id)
    projection = resolver.build_projection(root, graph)
    persisted = store.load_projection(root, goal_id)
    persisted_id = persisted.projection_id if persisted is not None else None
    return {
        "status": "OK",
        "schema_version": model.SCHEMA_VERSION,
        "reuse_version": model.REUSE_VERSION,
        "goal_id": graph.goal_id,
        "graph_id": graph.graph_id,
        "spec_sha256": graph.spec_sha256,
        "projection_id": projection.projection_id,
        "persisted_projection_id": persisted_id,
        "stale": persisted_id is not None
        and persisted_id != projection.projection_id,
        "declared_inputs": len(projection.inputs),
        "counts": projection.counts(),
        "blocked": projection.blocked,
        "blocked_nodes": list(projection.blocked_nodes()),
        "inputs": [item.to_dict() for item in projection.inputs],
    }


def provenance_document(root: str, goal_id: str) -> dict[str, Any]:
    """Explicit producer -> consumer provenance chains (read-only)."""
    graph, _ = graph_store.load_graph(root, goal_id)
    projection = resolver.build_projection(root, graph)
    chains = [
        {
            "consumer_node_id": item.consumer_node_id,
            "consumer_mission_id": item.consumer_mission_id,
            "input_id": item.input_id,
            "producer_node_id": item.producer_node_id,
            "producer_mission_id": item.producer_mission_id,
            "phase_id": item.phase_id,
            "trust": item.trust,
            "status": item.status,
            "reason": item.reason,
            "artifact_content_sha256": item.artifact_content_sha256,
            "artifact_patch_sha256": item.artifact_patch_sha256,
            "artifact_attestation": item.artifact_attestation,
            "artifact_subrun_id": item.artifact_subrun_id,
        }
        for item in projection.inputs
    ]
    return {
        "status": "OK",
        "goal_id": graph.goal_id,
        "graph_id": graph.graph_id,
        "projection_id": projection.projection_id,
        "chains": chains,
    }


def explain_document(root: str, goal_id: str,
                     node_id: str) -> dict[str, Any]:
    """Explain one consumer node's reuse inputs and stable reasons."""
    document = status_document(root, goal_id)
    items = [item for item in document["inputs"]
             if item["consumer_node_id"] == node_id]
    return {
        "status": "OK" if items else "UNKNOWN_NODE",
        "goal_id": goal_id,
        "graph_id": document["graph_id"],
        "projection_id": document["projection_id"],
        "node_id": node_id,
        "blocked": any(item["required"] and item["status"] != model.ST_RESOLVED
                       for item in items),
        "inputs": items,
    }


def history_document(root: str, goal_id: str) -> dict[str, Any]:
    """Append-only cross-mission consumption history (read-only)."""
    persisted = store.load_projection(root, goal_id)
    consumptions = store.load_consumptions(store.reuse_paths(root, goal_id))
    return {
        "status": "OK",
        "goal_id": goal_id,
        "graph_id": (persisted.graph_id if persisted is not None else None),
        "projection_id": (persisted.projection_id
                          if persisted is not None else None),
        "count": len(consumptions),
        "consumptions": [c.to_dict() for c in consumptions],
    }


# --- rendering -----------------------------------------------------------------


def _fmt(values: list[str]) -> str:
    return " ".join(values) if values else "-"


def render_status(document: Mapping[str, Any]) -> str:
    counts = document["counts"]
    lines = [
        f"goal      : {document['goal_id']}",
        f"graph     : {document['graph_id']}",
        f"projection: {document['projection_id']}",
        f"persisted : {document['persisted_projection_id']} "
        f"(stale={document['stale']})",
        f"inputs    : {document['declared_inputs']} "
        f"resolved={counts['resolved']} unresolved={counts['unresolved']} "
        f"rejected={counts['rejected']} blocking={counts['blocking']}",
        f"blocked   : {_fmt(list(document['blocked_nodes']))}",
    ]
    for item in document["inputs"]:
        lines.append(
            f"  - {item['consumer_node_id']} <- "
            f"{item['producer_node_id']}/{item['phase_id']} "
            f"[{item['status']}] {item['reason']} trust={item['trust']}")
    return "\n".join(lines)


def render_provenance(document: Mapping[str, Any]) -> str:
    lines = [f"provenance ({len(document['chains'])}):"]
    if not document["chains"]:
        lines.append("  - (none)")
    for chain in document["chains"]:
        lines.append(
            f"  - {chain['producer_node_id']}"
            f"({chain['producer_mission_id']}) -> "
            f"{chain['consumer_node_id']} "
            f"input={chain['input_id']} phase={chain['phase_id']} "
            f"trust={chain['trust']} "
            f"content={chain['artifact_content_sha256']}")
    return "\n".join(lines)


def render_explain(document: Mapping[str, Any]) -> str:
    if document["status"] == "UNKNOWN_NODE":
        return f"node not found: goal={document['goal_id']}"
    lines = [
        f"node      : {document['node_id']}",
        f"blocked   : {document['blocked']}",
        f"projection: {document['projection_id']}",
        "reuse:",
    ]
    if not document["inputs"]:
        lines.append("  - (none)")
    for item in document["inputs"]:
        lines.append(
            f"  - {item['input_id']} producer={item['producer_node_id']} "
            f"phase={item['phase_id']} required={item['required']} "
            f"min_trust={item['min_trust']} [{item['status']}] "
            f"{item['reason']} trust={item['trust']}")
    return "\n".join(lines)


def render_history(document: Mapping[str, Any]) -> str:
    lines = [f"reuse history ({document['count']}):"]
    if not document["consumptions"]:
        lines.append("  - (none)")
    for item in document["consumptions"]:
        lines.append(
            f"  - {item['consumer_node_id']} <- "
            f"{item['producer_node_id']} input={item['input_id']} "
            f"content={item['artifact_content_sha256']} "
            f"trust={item['trust']} at={item['consumed_at']}")
    return "\n".join(lines)
