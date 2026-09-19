"""M021 — read-only daemon operator projections.

Derived only from the durable daemon state/cycle log. A projection never
mutates state and never invents work or completion evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trajectory_os.daemon import engine, model, store


def status_document(root: str) -> dict[str, Any]:
    """Canonical read-only daemon status (safe when no daemon has run)."""
    state = store.load_state(root)
    cycles = store.load_cycles(store.daemon_paths(root))
    stop = engine.read_stop_request(root)
    return {
        "status": "OK",
        "schema_version": model.SCHEMA_VERSION,
        "daemon_version": model.DAEMON_VERSION,
        "present": state is not None,
        "daemon_status": state.status if state is not None else None,
        "cycles_executed": state.cycles_executed if state is not None else 0,
        "portfolio_id": state.portfolio_id if state is not None else None,
        "decision_ids": list(state.decision_ids) if state is not None else [],
        "last_cycle": (None if state is None or state.last_cycle is None
                       else state.last_cycle.to_dict()),
        "started_at": state.started_at if state is not None else None,
        "updated_at": state.updated_at if state is not None else None,
        "cycle_count": len(cycles),
        "stop": stop,
    }


def goal_daemon_document(root: str, goal_id: str) -> dict[str, Any]:
    """Daemon membership/observability for one goal."""
    document = status_document(root)
    selected = 0
    completed = 0
    for cycle in store.load_cycles(store.daemon_paths(root)):
        if goal_id in cycle.selected:
            selected += 1
        if goal_id in cycle.completed_goals:
            completed += 1
    return {
        **document,
        "goal_id": goal_id,
        "selected_in_cycles": selected,
        "completed_in_cycles": completed,
    }


def render_status(document: Mapping[str, Any]) -> str:
    lines = [
        f"daemon    : present={document['present']} "
        f"status={document['daemon_status']}",
        f"cycles    : {document['cycles_executed']} "
        f"(logged={document['cycle_count']})",
        f"portfolio : {document['portfolio_id']}",
        f"updated   : {document['updated_at']}",
    ]
    if document["stop"] is not None:
        lines.append(f"stop      : {document['stop']}")
    last = document.get("last_cycle")
    if isinstance(last, Mapping):
        lines.append(
            f"last      : cycle={last.get('cycle')} "
            f"status={last.get('status')} "
            f"selected={list(last.get('selected', []))}")
    return "\n".join(lines)
