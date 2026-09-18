"""Mission 013 — read-only portfolio-scheduler operator projections.

Every human-readable or machine-readable projection here is derived from the
canonical persisted scheduler decision/state plus read-only resolved mission
evidence. Nothing mutates state, writes files or invents evidence, so the
operator and the M014/M015 machine consumers share one source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trajectory_os.graph import readiness
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.scheduler import arbiter, engine, model
from trajectory_os.graph.scheduler import evidence as sched_evidence
from trajectory_os.graph.scheduler import store as sched_store


def _node_view(node: model.NodeDecision) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "priority": node.priority,
        "m012_state": node.m012_state,
        "outcome": node.outcome,
        "reason": node.reason,
        "mission_id": node.mission_id,
        "execution": node.demand.execution,
        "cpu_slots": node.demand.cpu_slots,
        "gpu_slots": node.demand.gpu_slots,
        "gpu_mem_bytes": node.demand.gpu_mem_bytes,
        "exclusive": node.demand.exclusive,
        "model_heavy": node.demand.model_heavy,
        "declared": node.demand.declared,
    }


def status_document(root: str, goal_id: str) -> dict[str, Any]:
    """Canonical read-only scheduler status for one goal graph."""
    graph, _ = graph_store.load_graph(root, goal_id)
    projection = readiness.project_with_store(root, graph)
    mission_ids = [
        node.mission_ref.mission_id
        for node in graph.nodes if node.mission_ref is not None
    ]
    runtime = sched_evidence.collect_runtime(root, mission_ids)
    state = sched_store.load_state(root, goal_id)
    latest = engine.latest_decision(root, goal_id)
    dispatch_records = state.dispatch_records if state is not None else ()
    reservations = arbiter.derive_reservations(graph, runtime, dispatch_records)
    document: dict[str, Any] = {
        "status": "OK",
        "schema_version": model.SCHEMA_VERSION,
        "scheduler_version": model.SCHEDULER_VERSION,
        "goal_id": graph.goal_id,
        "graph_id": graph.graph_id,
        "spec_sha256": graph.spec_sha256,
        "objective": graph.objective,
        "policy": (state.policy.to_dict() if state is not None
                   else model.DEFAULT_POLICY.to_dict()),
        "policy_id": (state.policy.policy_id if state is not None
                      else model.DEFAULT_POLICY.policy_id),
        "decision_id": (latest.decision_id if latest is not None else None),
        "input_projection_id": (latest.input_projection_id
                                if latest is not None else None),
        "counts": (latest.counts() if latest is not None
                   else {"admitted": 0, "deferred": 0, "blocked": 0,
                         "active": 0, "completed": 0}),
        "candidate_order": (list(latest.candidate_order)
                            if latest is not None else []),
        "admitted": ([_node_view(n) for n in latest.admitted]
                     if latest is not None else []),
        "deferred": ([_node_view(n) for n in latest.deferred]
                     if latest is not None else []),
        "blocked": ([_node_view(n) for n in latest.blocked]
                    if latest is not None else []),
        "active": ([_node_view(n) for n in latest.active]
                   if latest is not None else []),
        "completed": ([_node_view(n) for n in latest.completed]
                      if latest is not None else []),
        "reservations": [r.to_dict() for r in reservations],
        "reservation_totals": model.ReservationTotals.of(reservations).to_dict(),
        "dispatch": ([r.to_dict() for r in dispatch_records]
                     if state is not None else []),
        "history": (list(state.decision_ids) if state is not None else []),
        "m012_ready": list(projection.ready()),
        "m012_blocked": list(projection.blocked()),
    }
    return document


def explain_document(root: str, goal_id: str, node_id: str) -> dict[str, Any]:
    document = status_document(root, goal_id)
    for group in ("admitted", "deferred", "blocked", "active", "completed"):
        for node in document[group]:
            if node["node_id"] == node_id:
                return {"status": "OK", "goal_id": goal_id,
                        "graph_id": document["graph_id"], "group": group,
                        "node": node}
    return {"status": "UNKNOWN_NODE", "goal_id": goal_id,
            "graph_id": document["graph_id"], "group": None, "node": None}


def history_document(root: str, goal_id: str) -> dict[str, Any]:
    state = sched_store.load_state(root, goal_id)
    decisions = engine.decision_history(root, goal_id)
    return {
        "status": "OK",
        "goal_id": goal_id,
        "graph_id": (state.graph_id if state is not None else None),
        "count": len(decisions),
        "decisions": [
            {
                "decision_id": d.decision_id,
                "input_projection_id": d.input_projection_id,
                "created_at": d.created_at,
                "counts": d.counts(),
                "admitted": [n.node_id for n in d.admitted],
                "deferred": [{"node_id": n.node_id, "reason": n.reason}
                             for n in d.deferred],
                "blocked": [{"node_id": n.node_id, "reason": n.reason}
                            for n in d.blocked],
            }
            for d in decisions
        ],
    }


# --- rendering -----------------------------------------------------------------


def _fmt(values: list[str]) -> str:
    return " ".join(values) if values else "-"


def render_decision(decision: model.ScheduleDecision) -> str:
    """Human-readable rendering of one (unpersisted) scheduling preview."""
    counts = decision.counts()
    totals = decision.reservations.totals_current
    lines = [
        f"goal      : {decision.goal_id}",
        f"graph     : {decision.graph_id}",
        f"decision  : {decision.decision_id}",
        f"input     : {decision.input_projection_id}",
        f"capacity  : cpu={decision.policy.cpu_slots} "
        f"gpu={decision.policy.gpu_slots} "
        f"vram={decision.policy.gpu_mem_bytes} "
        f"concurrency={decision.concurrency_limit}",
        f"reserved  : cpu={totals.cpu_slots} gpu={totals.gpu_slots} "
        f"vram={totals.gpu_mem_bytes} count={totals.count}",
        f"order     : {_fmt(list(decision.candidate_order))}",
        f"admitted  : {counts['admitted']} "
        f"[{_fmt([n.node_id for n in decision.admitted])}]",
        f"deferred  : {counts['deferred']} "
        f"[{_fmt([n.node_id for n in decision.deferred])}]",
        f"blocked   : {counts['blocked']} "
        f"[{_fmt([n.node_id for n in decision.blocked])}]",
        "nodes:",
    ]
    for group in ("admitted", "deferred", "blocked", "active",
                  "completed"):
        for node in getattr(decision, group):
            lines.append(
                f"  - {node.node_id} [{group.upper()}] "
                f"priority={node.priority} exec={node.demand.execution} "
                f"mission={node.mission_id} reason={node.reason}")
    return "\n".join(lines)


def render_status(document: Mapping[str, Any]) -> str:
    policy = document["policy"]
    totals = document["reservation_totals"]
    counts = document["counts"]
    lines = [
        f"goal      : {document['goal_id']}",
        f"graph     : {document['graph_id']}",
        f"decision  : {document['decision_id']}",
        f"input     : {document['input_projection_id']}",
        f"capacity  : cpu={policy['cpu_slots']} gpu={policy['gpu_slots']} "
        f"vram={policy['gpu_mem_bytes']} "
        f"concurrency={policy['global_concurrency']}",
        f"policy    : {document['policy_id']}",
        f"admitted  : {counts['admitted']} "
        f"[{_fmt([n['node_id'] for n in document['admitted']])}]",
        f"deferred  : {counts['deferred']} "
        f"[{_fmt([n['node_id'] for n in document['deferred']])}]",
        f"blocked   : {counts['blocked']} "
        f"[{_fmt([n['node_id'] for n in document['blocked']])}]",
        f"active    : {counts['active']} "
        f"[{_fmt([n['node_id'] for n in document['active']])}]",
        f"reserved  : cpu={totals['cpu_slots']} gpu={totals['gpu_slots']} "
        f"vram={totals['gpu_mem_bytes']} count={totals['count']}",
        "nodes:",
    ]
    for group in ("admitted", "deferred", "blocked", "active", "completed"):
        for node in document[group]:
            lines.append(
                f"  - {node['node_id']} [{group.upper()}] "
                f"priority={node['priority']} mission={node['mission_id']} "
                f"exec={node['execution']} reason={node['reason']}")
    return "\n".join(lines)


def render_resources(document: Mapping[str, Any]) -> str:
    policy = document["policy"]
    totals = document["reservation_totals"]
    lines = [
        f"configured : cpu={policy['cpu_slots']} gpu={policy['gpu_slots']} "
        f"vram={policy['gpu_mem_bytes']} "
        f"concurrency={policy['global_concurrency']}",
        f"reserved   : cpu={totals['cpu_slots']} gpu={totals['gpu_slots']} "
        f"vram={totals['gpu_mem_bytes']} count={totals['count']}",
        "active reservations:",
    ]
    if not document["reservations"]:
        lines.append("  - (none)")
    for reservation in document["reservations"]:
        lines.append(
            f"  - {reservation['node_id']} mission={reservation['mission_id']} "
            f"exec={reservation['execution']} cpu={reservation['cpu_slots']} "
            f"gpu={reservation['gpu_slots']} "
            f"vram={reservation['gpu_mem_bytes']} "
            f"exclusive={reservation['exclusive']} "
            f"owned={reservation['owned']}")
    return "\n".join(lines)


def render_history(document: Mapping[str, Any]) -> str:
    lines = [f"history ({document['count']}):"]
    if not document["decisions"]:
        lines.append("  - (none)")
    for entry in document["decisions"]:
        counts = entry["counts"]
        lines.append(
            f"  - {entry['decision_id'][:12]} {entry['created_at']} "
            f"admitted={counts['admitted']} deferred={counts['deferred']} "
            f"blocked={counts['blocked']}")
    return "\n".join(lines)


def render_explain(document: Mapping[str, Any]) -> str:
    if document["node"] is None:
        return f"node not found: goal={document['goal_id']}"
    node = document["node"]
    lines = [
        f"node      : {node['node_id']}",
        f"group     : {document['group']}",
        f"outcome   : {node['outcome']} ({node['reason']})",
        f"m012      : {node['m012_state']}",
        f"priority  : {node['priority']}",
        f"mission   : {node['mission_id']}",
        f"execution : {node['execution']}",
        f"demand    : cpu={node['cpu_slots']} gpu={node['gpu_slots']} "
        f"vram={node['gpu_mem_bytes']} exclusive={node['exclusive']} "
        f"model_heavy={node['model_heavy']}",
    ]
    return "\n".join(lines)


def render_portfolio(document: Mapping[str, Any]) -> str:
    """Compact human-readable portfolio summary (M011 reduced intervention)."""
    counts = document["counts"]
    policy = document["policy"]
    totals = document["reservation_totals"]
    return (
        f"portfolio : {document['goal_id']} "
        f"(nodes ready={len(document['m012_ready'])} "
        f"blocked={len(document['m012_blocked'])}) "
        f"admitted={counts['admitted']} deferred={counts['deferred']} "
        f"blocked={counts['blocked']} active={counts['active']} "
        f"completed={counts['completed']} "
        f"gpu={totals['gpu_slots']}/{policy['gpu_slots']} "
        f"vram={totals['gpu_mem_bytes']}/{policy['gpu_mem_bytes']} "
        f"cpu={totals['cpu_slots']}/{policy['cpu_slots']} "
        f"slots={totals['count']}/{policy['global_concurrency']}"
    )
