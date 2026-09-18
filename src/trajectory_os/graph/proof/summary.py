"""Mission 016 — compact operator dashboard and why/explain projections.

Every human-readable or machine-readable projection here is derived only
from the canonical M008-M015 stores plus the derived goal-proof engine. The
machine document is complete enough for automation; the human rendering is a
compact, deterministic view that fits approximately one terminal screen.
Nothing in this module mutates state or invents evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trajectory_os.graph.proof import engine, model, store

#: Maximum criteria / risk lines rendered in the compact human view.
MAX_HUMAN_CRITERIA = 12
MAX_HUMAN_RISKS = 8


def _fmt(values: list[str]) -> str:
    return " ".join(values) if values else "-"


def _node_view(node: model.NodeProof) -> dict[str, Any]:
    return node.to_dict()


def dashboard_document(root: str, goal_id: str) -> dict[str, Any]:
    """Complete machine-readable goal dashboard (derived, read-only)."""
    proof = engine.build_proof(root, goal_id)
    persisted = store.load_projection(root, goal_id)
    persisted_id = persisted.proof_id if persisted is not None else None
    stale = persisted_id is not None and persisted_id != proof.proof_id
    return {
        "status": "OK",
        "schema_version": proof.schema_version,
        "proof_version": proof.proof_version,
        "goal": {
            "goal_id": proof.goal_id,
            "objective": proof.objective,
            "graph_id": proof.graph_id,
            "spec_sha256": proof.spec_sha256,
        },
        "generation": (None if proof.generation is None
                       else proof.generation.to_dict()),
        "identity": engine.identity_refs(proof),
        "persisted_proof_id": persisted_id,
        "stale": stale,
        "counts": proof.counts.to_dict(),
        "critical_path": list(proof.critical_path),
        "nodes": [_node_view(node) for node in proof.nodes],
        "criteria": [c.to_dict() for c in proof.criteria],
        "reuse": dict(proof.reuse),
        "scheduler": dict(proof.scheduler),
        "resources": dict(proof.resources),
        "replan": dict(proof.replan),
        "risks": [risk.to_dict() for risk in proof.risks],
        "final": {
            "state": proof.final_state,
            "reason": proof.final_reason,
            "complete": proof.complete,
        },
        "gate": proof.gate.to_dict(),
        "why": _why(proof),
    }


def _why(proof: model.GoalProof) -> list[str]:
    """Bounded deterministic explanation lines for the current proof."""
    if proof.complete:
        return ["all configured acceptance criteria are proven by exact "
                "evidence"]
    lines: list[str] = []
    for risk in proof.risks[:MAX_HUMAN_RISKS]:
        lines.append(
            f"{risk.risk_class}:{risk.reason}:{risk.subject} "
            f"({risk.detail})")
    for criterion in proof.criteria:
        if not criterion.proven:
            lines.append(
                f"criterion {criterion.node_id}/{criterion.criterion_id} is "
                f"{criterion.status} ({criterion.reason})")
    if not proof.criteria:
        lines.append("no configured acceptance criteria (fail closed)")
    return lines


def explain_document(root: str, goal_id: str, subject: str,
                     ) -> dict[str, Any]:
    """Deterministic why/explain for the goal or one node/criterion."""
    proof = engine.build_proof(root, goal_id)
    if subject == "goal" or subject == "":
        return {
            "status": "OK" if proof.complete else "INCOMPLETE",
            "goal_id": proof.goal_id,
            "graph_id": proof.graph_id,
            "final": {"state": proof.final_state,
                      "reason": proof.final_reason,
                      "complete": proof.complete},
            "identity": engine.identity_refs(proof),
            "gate": proof.gate.to_dict(),
            "risks": [risk.to_dict() for risk in proof.risks],
            "unproven_criteria": [
                criterion.to_dict() for criterion in proof.criteria
                if not criterion.proven
            ],
            "why": _why(proof),
        }
    node = next((n for n in proof.nodes if n.node_id == subject), None)
    if node is None:
        return {"status": "UNKNOWN_NODE", "goal_id": proof.goal_id,
                "subject": subject, "node": None, "criteria": []}
    criteria = [c.to_dict() for c in proof.criteria
                if c.node_id == subject]
    risks = [risk.to_dict() for risk in proof.risks
             if risk.subject == subject]
    return {
        "status": "OK",
        "goal_id": proof.goal_id,
        "graph_id": proof.graph_id,
        "subject": subject,
        "node": node.to_dict(),
        "criteria": criteria,
        "risks": risks,
    }


def history_document(root: str, goal_id: str) -> dict[str, Any]:
    events = store.load_events(root, goal_id)
    return {
        "status": "OK",
        "goal_id": goal_id,
        "count": len(events),
        "events": [event.to_dict() for event in events],
    }


# --- compact human rendering --------------------------------------------------


def criterion_line(criterion: Mapping[str, Any]) -> str:
    label = f"{criterion['node_id']}/{criterion['criterion_id']}"
    if criterion["status"] == model.CS_PROVEN:
        evidence = criterion["evidence"]
        first = evidence[0] if evidence else {}
        trust = criterion["trust"]
        return (f"  + {label} PROVEN trust={trust} "
                f"mission={first.get('mission_id')} "
                f"phase={first.get('phase_id') or '*'} "
                f"subrun={first.get('subrun_id') or '-'} "
                f"attestation={first.get('attestation') or '-'}")
    return (f"  ! {label} {criterion['status']} "
            f"reason={criterion['reason']}")


def render_dashboard(document: Mapping[str, Any]) -> str:
    """Compact one-screen operator dashboard (derived from the JSON document)."""
    goal = document["goal"]
    counts = document["counts"]
    final = document["final"]
    gate = document["gate"]
    identity = document["identity"]
    generation = document["generation"]
    replan = document["replan"]
    scheduler = document["scheduler"]
    resources = document["resources"]
    reuse = document["reuse"]
    lines = [
        f"goal      : {goal['goal_id']}  [{final['state']}/{final['reason']}]",
        f"objective : {goal['objective']}",
        f"graph     : {goal['graph_id']}",
        f"generation: "
        f"{(generation or {}).get('generation_number', '-')} "
        f"{(generation or {}).get('generation_id', '-')}",
        f"proof     : {identity['proof_id']} "
        f"persisted={document['persisted_proof_id']} "
        f"stale={document['stale']}",
        f"gate      : {gate['state']} human={gate['human_action_required']} "
        f"({gate['reason']})",
        f"missions  : total={counts['missions_total']} "
        f"proven={counts['missions_proven']} "
        f"incomplete={counts['missions_incomplete']} "
        f"missing={counts['missions_missing']}",
        f"nodes     : total={counts['nodes_total']} "
        f"complete={counts['nodes_complete']} ready={counts['nodes_ready']} "
        f"inflight={counts['nodes_in_progress']} "
        f"blocked={counts['nodes_blocked']} "
        f"unresolved={counts['nodes_unresolved']} "
        f"invalid={counts['nodes_invalid']}",
        f"criteria  : total={counts['criteria_total']} "
        f"proven={counts['criteria_proven']} "
        f"unproven={counts['criteria_unproven']} "
        f"contradictory={counts['criteria_contradictory']} "
        f"invalid={counts['criteria_invalid']}",
        f"path      : {_fmt(list(document['critical_path']))}",
        f"scheduler : present={scheduler['present']} "
        f"decision={scheduler['decision_id']} "
        f"admitted={scheduler['counts']['admitted']} "
        f"deferred={scheduler['counts']['deferred']} "
        f"blocked={scheduler['counts']['blocked']} "
        f"stale={scheduler['stale']}",
        f"resources : "
        f"cpu={resources.get('reserved_cpu_slots', '-')}"
        f"/{resources.get('cpu_slots', '-')} "
        f"gpu={resources.get('reserved_gpu_slots', '-')}"
        f"/{resources.get('gpu_slots', '-')} "
        f"slots={resources.get('reserved_count', '-')}"
        f"/{resources.get('global_concurrency', '-')}",
        f"reuse     : declared={reuse['declared']} "
        f"blocked={_fmt(list(reuse['blocked_nodes']))} stale={reuse['stale']}",
        f"replans   : accepted={replan.get('accepted', 0)} "
        f"supersessions={replan.get('supersessions', 0)} "
        f"rejected={replan.get('rejected', 0)}",
        f"risks     : total={counts['risks_total']} "
        f"unresolved={counts['risks_unresolved']} "
        f"blocked={counts['risks_blocked']} stale={counts['risks_stale']} "
        f"legacy={counts['risks_legacy']} "
        f"unproven={counts['risks_unproven']} "
        f"contradictory={counts['risks_contradictory']} "
        f"invalid={counts['risks_invalid']}",
        "criteria:",
    ]
    criteria = list(document["criteria"])
    for criterion in criteria[:MAX_HUMAN_CRITERIA]:
        lines.append(criterion_line(criterion))
    if len(criteria) > MAX_HUMAN_CRITERIA:
        lines.append(f"  ... {len(criteria) - MAX_HUMAN_CRITERIA} more")
    if not criteria:
        lines.append("  (no configured acceptance criteria)")
    if document["why"]:
        lines.append("why:")
        for item in document["why"][:MAX_HUMAN_RISKS]:
            lines.append(f"  - {item}")
    if gate["next_human_action"]:
        lines.append(f"next      : {gate['next_human_action']}")
    return "\n".join(lines)


def render_explain(document: Mapping[str, Any]) -> str:
    if document.get("status") == "UNKNOWN_NODE":
        return f"node not found: {document['subject']}"
    if "final" in document:
        lines = [
            f"goal     : {document['goal_id']}",
            f"final    : {document['final']['state']} "
            f"({document['final']['reason']})",
            f"proof    : {document['identity']['proof_id']}",
            f"gate     : {document['gate']['state']}",
            "why:",
        ]
        for item in document["why"]:
            lines.append(f"  - {item}")
        return "\n".join(lines)
    node = document["node"]
    lines = [
        f"node     : {node['node_id']} {node['title']}",
        f"state    : {node['state']} ({node['reason']})",
        f"mission  : {node['mission_id']} state={node['mission_state']} "
        f"proven={node['mission_proven']}",
        f"criteria : {node['criteria_proven']}/{node['criteria_total']} proven",
    ]
    for criterion in document["criteria"]:
        lines.append(criterion_line(criterion))
    for risk in document["risks"]:
        lines.append(f"  ! {risk['risk_class']}:{risk['reason']} "
                     f"({risk['detail']})")
    return "\n".join(lines)
