"""M027 — web dashboard projections (read-only composition).

Every document returned here is composed from the existing canonical
projections: the M017-M019 goal snapshot, the M026 event projection, the
M020 portfolio projection and the M025 artifact projection. Nothing is
recomputed into a parallel store and nothing mutates authoritative state.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.artifacts import summary as artifact_summary
from trajectory_os.daemon import summary as daemon_summary
from trajectory_os.events import summary as event_summary
from trajectory_os.goals import snapshot as goal_snapshot
from trajectory_os.graph import store as graph_store
from trajectory_os.portfolio import summary as portfolio_summary
from trajectory_os.web import model


def _safe(payload: Mapping[str, Any]) -> dict[str, Any]:
    return dict(payload)


def snapshot(root: str | Path, goal_id: str) -> dict[str, Any]:
    return goal_snapshot.build_snapshot(str(root), goal_id)


def events(root: str | Path, goal_id: str, *,
           refresh: bool = False) -> dict[str, Any]:
    return event_summary.document(str(root), goal_id, refresh=refresh)


def dashboard(root: str | Path, goal_id: str) -> dict[str, Any]:
    """One combined operator dashboard document for a single goal."""
    root_str = str(root)
    snapshot_doc = snapshot(root_str, goal_id)
    return {
        "status": "OK",
        "schema_version": model.SCHEMA_VERSION,
        "web_version": model.WEB_VERSION,
        "goal_id": goal_id,
        "snapshot": snapshot_doc,
        "events": events(root_str, goal_id),
        "portfolio": _safe(portfolio_summary.goal_portfolio_document(
            root_str, goal_id)),
        "daemon": _safe(daemon_summary.goal_daemon_document(
            root_str, goal_id)),
        "artifacts": _safe(artifact_summary.status_document(
            root_str, goal_id, verify_content=False)),
    }


def overview(root: str | Path) -> dict[str, Any]:
    """Bounded list of every goal under the root plus a few key signals."""
    root_str = str(root)
    goals: list[dict[str, Any]] = []
    for goal_id in graph_store.list_goal_ids(root_str):
        try:
            snapshot_doc = snapshot(root_str, goal_id)
            goals.append({
                "goal_id": goal_id,
                "state": snapshot_doc["final"]["state"],
                "reason": snapshot_doc["final"]["reason"],
                "complete": snapshot_doc["final"]["complete"],
                "criteria_proven": snapshot_doc["counts"]["criteria_proven"],
                "criteria_total": snapshot_doc["counts"]["criteria_total"],
                "blockers": len(snapshot_doc.get("blockers", [])),
                "gate": snapshot_doc.get("gate", {}).get("state"),
                "stop_requested": snapshot_doc.get("stop") is not None,
                "status": "OK",
            })
        except Exception as exc:  # noqa: BLE001 - overview must not crash
            goals.append({
                "goal_id": goal_id,
                "status": "MALFORMED",
                "error": type(exc).__name__,
            })
    return {
        "status": "OK",
        "schema_version": model.SCHEMA_VERSION,
        "web_version": model.WEB_VERSION,
        "root": root_str,
        "count": len(goals),
        "goals": goals,
        "portfolio": _safe(portfolio_summary.status_document(root_str)),
    }
