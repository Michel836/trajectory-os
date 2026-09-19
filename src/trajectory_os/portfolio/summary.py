"""M020 — read-only portfolio operator projections.

Every projection is derived from the canonical persisted portfolio decision
plus each member goal's own canonical graph/scheduler/proof state. Nothing
here mutates state, invents evidence or merges two goals into one identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trajectory_os.portfolio import engine, model, store


def _entry_view(entry: model.PortfolioEntry) -> dict[str, Any]:
    return entry.to_dict()


def status_document(root: str) -> dict[str, Any]:
    """Canonical read-only portfolio status (never crashes on a missing store)."""
    state = store.load_state(root)
    latest: model.PortfolioDecision | None = None
    if state is not None and state.last_decision_id is not None:
        latest = store.load_decision(
            store.portfolio_paths(root), state.last_decision_id)
    stop = engine.read_stop_request(root)
    document: dict[str, Any] = {
        "status": "OK",
        "schema_version": model.SCHEMA_VERSION,
        "portfolio_version": model.PORTFOLIO_VERSION,
        "present": state is not None,
        "portfolio_id": state.portfolio_id if state is not None else None,
        "policy": (state.policy.identity_payload()
                   if state is not None else model.DEFAULT_POLICY.identity_payload()),
        "policy_id": (state.policy.policy_id if state is not None
                      else model.DEFAULT_POLICY.policy_id),
        "decision_id": (latest.decision_id if latest is not None else None),
        "cycle_count": state.cycle_count if state is not None else 0,
        "updated_at": state.updated_at if state is not None else None,
        "counts": (latest.counts() if latest is not None
                   else {"goals_total": 0, "selected": 0, "excluded": 0,
                         "dispatched": 0, "active_reservations": 0}),
        "entries": ([_entry_view(e) for e in latest.entries]
                    if latest is not None else []),
        "stop": stop,
    }
    return document


def goal_portfolio_document(root: str, goal_id: str) -> dict[str, Any]:
    """Portfolio membership of one goal plus portfolio-wide counts."""
    document = status_document(root)
    entry = None
    for item in document["entries"]:
        if item["goal_id"] == goal_id:
            entry = item
            break
    return {
        "status": "OK",
        "present": document["present"],
        "portfolio_id": document["portfolio_id"],
        "decision_id": document["decision_id"],
        "cycle_count": document["cycle_count"],
        "counts": document["counts"],
        "member": entry is not None,
        "entry": entry,
        "stop": document["stop"],
    }


def render_status(document: Mapping[str, Any]) -> str:
    policy = document["policy"]
    counts = document["counts"]
    lines = [
        f"portfolio : {document['portfolio_id']} "
        f"present={document['present']}",
        f"decision  : {document['decision_id']}",
        f"cycles    : {document['cycle_count']} "
        f"updated={document['updated_at']}",
        f"capacity  : goals<={policy['max_active_goals']} "
        f"cpu={policy['cpu_slots']} gpu={policy['gpu_slots']} "
        f"vram={policy['gpu_mem_bytes']} "
        f"slots={policy['global_concurrency']}",
        f"counts    : total={counts['goals_total']} "
        f"selected={counts['selected']} excluded={counts['excluded']} "
        f"active={counts['active_reservations']}",
    ]
    if document["stop"] is not None:
        lines.append(f"stop      : {document['stop']}")
    lines.append("goals:")
    if not document["entries"]:
        lines.append("  - (none)")
    for entry in document["entries"]:
        lines.append(
            f"  - {entry['goal_id']} [{entry['outcome']}] "
            f"{entry['reason']} graph={entry['graph_id']} "
            f"ready={len(entry['ready_nodes'])} "
            f"active={entry['active_reservations']}")
    return "\n".join(lines)
