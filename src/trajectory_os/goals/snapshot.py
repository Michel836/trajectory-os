"""M017-M019 — one canonical read-only operator snapshot.

Everything the operator product layer shows (unified CLI, live TUI, final
proof view) is derived from this single projection, which in turn is derived
only from the canonical M012-M016 stores plus read-only mission evidence.
Nothing here mutates state, invents evidence or forks a second source of
truth: status/inspect/dashboard/TUI are different renderings of the same
document.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os import live_status
from trajectory_os.artifacts import summary as artifact_summary
from trajectory_os.daemon import summary as daemon_summary
from trajectory_os.graph import readiness
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import summary as proof_summary
from trajectory_os.graph.replan import store as replan_store
from trajectory_os.graph.replan import summary as replan_summary
from trajectory_os.graph.scheduler import summary as sched_summary
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import store as mission_store
from trajectory_os.portfolio import summary as portfolio_summary

#: Stable snapshot schema version (forward-compatible checks).
SCHEMA_VERSION = 1

#: Safe-stop marker file name (under the goal root). It is a request only:
#: the runner never kills an in-flight sub-run, it simply declines to launch
#: new work once the current bounded step returns.
STOP_MARKER_NAME = "stop.json"

_MAX_STOP_BYTES = 4096


def utc_now_iso() -> str:
    """Canonical UTC timestamp (same grammar as the mission store)."""
    return (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "Z"
    )


def stop_marker_path(root: str, goal_id: str) -> Path:
    return graph_store.graph_paths(root, goal_id)["root"] / STOP_MARKER_NAME


def read_stop_request(root: str, goal_id: str) -> dict[str, Any] | None:
    """Read the safe-stop request (``None`` when no request exists)."""
    path = stop_marker_path(root, goal_id)
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()[:_MAX_STOP_BYTES + 1]
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"requested": True, "reason": "UNREADABLE", "requested_at": None}
    if not isinstance(doc, dict):
        return {"requested": True, "reason": "MALFORMED", "requested_at": None}
    return {
        "requested": True,
        "reason": doc.get("reason"),
        "requested_at": doc.get("requested_at"),
    }


def _parse_iso(ts: object) -> datetime.datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.strptime(
            ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    except ValueError:
        return None


def _elapsed_seconds(start: object, now_iso: str) -> int | None:
    start_dt = _parse_iso(start)
    now_dt = _parse_iso(now_iso)
    if start_dt is None or now_dt is None:
        return None
    return max(0, int((now_dt - start_dt).total_seconds()))


def _command_value(command: object, flag: str) -> str | None:
    if not isinstance(command, list):
        return None
    if command.count(flag) != 1:
        return None
    index = command.index(flag)
    if index + 1 >= len(command):
        return None
    value = command[index + 1]
    return value if isinstance(value, str) else None


def _command_agent(command: object) -> str | None:
    if not isinstance(command, list) or not command:
        return None
    first = command[0]
    if not isinstance(first, str):
        return None
    return Path(first).name or None


def _attribution(model_name: str | None,
                 agent_backend: str | None) -> dict[str, Any]:
    if not model_name:
        return {
            "agent_backend": agent_backend,
            "provider": None,
            "model": None,
            "locality": None,
        }
    provider, locality = live_status.classify_provider(model_name)
    return {
        "agent_backend": agent_backend,
        "provider": provider,
        "model": live_status.normalize_model(provider, model_name),
        "locality": locality,
    }


def _scheduler_demand(scheduler: Mapping[str, Any],
                      node_id: str) -> dict[str, Any]:
    for group in ("active", "admitted", "deferred", "blocked", "completed"):
        entries = scheduler.get(group)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and entry.get("node_id") == node_id:
                return dict(entry)
    return {}


def _mission_activity(root: str, node: Mapping[str, Any],
                      scheduler: Mapping[str, Any],
                      now_iso: str) -> dict[str, Any] | None:
    mission_id = node.get("mission_id")
    if not isinstance(mission_id, str):
        return None
    try:
        mission, paths = mission_store.load_mission(root, mission_id)
    except (mission_store.MissionNotFound, mission_store.MalformedMissionError):
        return None

    phase = next((p for p in mission.phases
                  if p.state == mission_model.PS_RUNNING), None)
    command: list[str] = []
    started_at: str | None = None
    running_subrun_id: str | None = None
    if phase is not None and phase.subrun_ids:
        last_id = phase.subrun_ids[-1]
        try:
            record = mission_store.load_subrun(paths, last_id)
        except (mission_store.MissionNotFound,
                mission_store.MalformedMissionError):
            record = None
        if record is not None:
            command = list(record.command)
            started_at = record.started_at
            running_subrun_id = (
                record.subrun_id
                if record.classification == mission_model.CR_RUNNING
                else None)
    if not command:
        # No active sub-run: fall back to the latest persisted command so the
        # operator still sees the exact agent/model that last ran.
        for subrun_id in reversed(mission.subruns):
            try:
                record = mission_store.load_subrun(paths, subrun_id)
            except (mission_store.MissionNotFound,
                    mission_store.MalformedMissionError):
                continue
            command = list(record.command)
            started_at = record.started_at
            break
    if not command:
        # Fresh mission: the plan itself carries the command/model.
        candidate = phase or (mission.phases[0] if mission.phases else None)
        if candidate is not None:
            command = list(candidate.command)

    model_name = _command_value(command, "--model")
    agent_backend = _command_agent(command)
    demand = _scheduler_demand(scheduler, str(node.get("node_id")))
    attribution = _attribution(model_name, agent_backend)
    phase_states = {p.phase_id: p.state for p in mission.phases}
    return {
        "node_id": node.get("node_id"),
        "title": node.get("title"),
        "mission_id": mission_id,
        "mission_state": mission.mission_state,
        "mission_reason": mission.mission_reason,
        "phase_id": (phase.phase_id if phase is not None
                     else (mission.phases[0].phase_id
                           if mission.phases else None)),
        "phase_kind": (phase.kind if phase is not None
                       else (mission.phases[0].kind
                             if mission.phases else None)),
        "phase_states": phase_states,
        "validate_state": phase_states.get("validate"),
        "review_state": phase_states.get("review"),
        "running": running_subrun_id is not None,
        "subrun_id": running_subrun_id,
        "started_at": started_at,
        "elapsed_seconds": _elapsed_seconds(started_at, now_iso),
        "execution": demand.get("execution"),
        "cpu_slots": demand.get("cpu_slots"),
        "gpu_slots": demand.get("gpu_slots"),
        "gpu_mem_bytes": demand.get("gpu_mem_bytes"),
        **attribution,
    }


def _build_activity(root: str, nodes: list[dict[str, Any]],
                    scheduler: Mapping[str, Any],
                    now_iso: str,
                    ) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for node in nodes:
        entry = _mission_activity(root, node, scheduler, now_iso)
        if entry is not None:
            entries.append(entry)
    running = [e for e in entries if e["running"]]
    active = [e for e in entries
              if e["mission_state"] not in mission_model.TERMINAL_MISSION_STATES]
    primary: dict[str, Any] | None = None
    if running:
        primary = running[0]
    elif active:
        primary = active[0]
    last: dict[str, Any] | None = entries[-1] if entries else None
    return {
        "missions": entries,
        "active": active,
        "running": running,
        "agent": primary,
        "last": last,
    }


def _build_blockers(dashboard: Mapping[str, Any],
                    scheduler: Mapping[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for node in dashboard.get("nodes", []):
        if not isinstance(node, Mapping):
            continue
        if node.get("state") == readiness.RS_BLOCKED:
            blockers.append({
                "kind": "NODE",
                "subject": node.get("node_id"),
                "reason": node.get("reason"),
                "detail": (f"mission={node.get('mission_id')} "
                           f"state={node.get('mission_state')}"),
            })
    for entry in scheduler.get("blocked", []):
        if isinstance(entry, Mapping):
            blockers.append({
                "kind": "SCHEDULER",
                "subject": entry.get("node_id"),
                "reason": entry.get("reason"),
                "detail": (f"execution={entry.get('execution')} "
                           f"mission={entry.get('mission_id')}"),
            })
    for risk in dashboard.get("risks", []):
        if isinstance(risk, Mapping):
            blockers.append({
                "kind": f"RISK:{risk.get('risk_class')}",
                "subject": risk.get("subject"),
                "reason": risk.get("reason"),
                "detail": risk.get("detail"),
            })
    reuse = dashboard.get("reuse")
    if isinstance(reuse, Mapping):
        for node_id in reuse.get("blocked_nodes", []):
            blockers.append({
                "kind": "REUSE",
                "subject": node_id,
                "reason": "REUSE_INPUT_UNRESOLVED",
                "detail": "declared cross-mission evidence is not resolved",
            })
    return blockers


def _safe_replan_history(root: str, goal_id: str) -> dict[str, Any] | None:
    try:
        if not replan_store.replan_exists(root, goal_id):
            return None
        return replan_summary.history_document(root, goal_id)
    except Exception:  # noqa: BLE001 - read-only projection must not crash
        return None


def _safe_portfolio(root: str, goal_id: str) -> dict[str, Any]:
    try:
        return portfolio_summary.goal_portfolio_document(root, goal_id)
    except Exception:  # noqa: BLE001 - read-only projection must not crash
        return {"status": "UNAVAILABLE", "present": False, "member": False}


def _safe_daemon(root: str, goal_id: str) -> dict[str, Any]:
    try:
        return daemon_summary.goal_daemon_document(root, goal_id)
    except Exception:  # noqa: BLE001 - read-only projection must not crash
        return {"status": "UNAVAILABLE", "present": False, "goal_id": goal_id}


def _safe_artifacts(root: str, goal_id: str) -> dict[str, Any]:
    """Read-only artifact/workspace projection (never hashes content)."""
    try:
        return artifact_summary.status_document(
            root, goal_id, verify_content=False)
    except Exception:  # noqa: BLE001 - read-only projection must not crash
        return {"status": "UNAVAILABLE", "present": False, "goal_id": goal_id}


def build_snapshot(root: str, goal_id: str, *,
                   now_iso: str | None = None,
                   local_resources: Mapping[str, Any] | None = None,
                   ) -> dict[str, Any]:
    """Canonical, deterministic, read-only operator snapshot for one goal.

    ``local_resources`` is an optional, already-computed M024 live resource
    document. It is never probed here (so read-only snapshot construction
    stays deterministic and hardware-independent); callers that own a live
    probe pass the document in explicitly.
    """
    now_iso = now_iso or utc_now_iso()
    dashboard = proof_summary.dashboard_document(root, goal_id)
    scheduler = sched_summary.status_document(root, goal_id)
    nodes = [node for node in dashboard.get("nodes", [])
             if isinstance(node, dict)]
    counts = dashboard["counts"]
    readiness_view = {
        "ready": [n["node_id"] for n in nodes
                  if n.get("state") == readiness.RS_READY],
        "in_progress": [n["node_id"] for n in nodes
                        if n.get("state") == readiness.RS_IN_PROGRESS],
        "blocked": [n["node_id"] for n in nodes
                    if n.get("state") == readiness.RS_BLOCKED],
        "complete": [n["node_id"] for n in nodes
                     if n.get("state") == readiness.RS_COMPLETE],
        "unresolved": [n["node_id"] for n in nodes
                       if n.get("state") == readiness.RS_UNRESOLVED],
        "invalid": [n["node_id"] for n in nodes
                    if n.get("state") == readiness.RS_INVALID],
    }
    resources = dashboard.get("resources", {})
    reservation = scheduler.get("reservation_totals", {})
    policy = scheduler.get("policy", {})
    return {
        "status": "OK",
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso,
        "goal": dashboard["goal"],
        "generation": dashboard["generation"],
        "final": dashboard["final"],
        "proof": {
            "proof_id": dashboard["identity"].get("proof_id"),
            "persisted_proof_id": dashboard["persisted_proof_id"],
            "stale": dashboard["stale"],
            "complete": dashboard["final"]["complete"],
            "state": dashboard["final"]["state"],
            "reason": dashboard["final"]["reason"],
        },
        "counts": counts,
        "critical_path": dashboard["critical_path"],
        "decomposition": nodes,
        "readiness": readiness_view,
        "scheduler": {
            "present": scheduler.get("decision_id") is not None,
            "decision_id": scheduler.get("decision_id"),
            "input_projection_id": scheduler.get("input_projection_id"),
            "policy_id": scheduler.get("policy_id"),
            "counts": scheduler.get("counts"),
            "admitted": scheduler.get("admitted"),
            "deferred": scheduler.get("deferred"),
            "blocked": scheduler.get("blocked"),
            "active": scheduler.get("active"),
            "completed": scheduler.get("completed"),
            "candidate_order": scheduler.get("candidate_order"),
        },
        "resources": {
            "cpu_limit": policy.get("cpu_slots"),
            "gpu_limit": policy.get("gpu_slots"),
            "vram_limit": policy.get("gpu_mem_bytes"),
            "concurrency_limit": policy.get("global_concurrency"),
            "cpu_reserved": reservation.get("cpu_slots"),
            "gpu_reserved": reservation.get("gpu_slots"),
            "vram_reserved": reservation.get("gpu_mem_bytes"),
            "slots_reserved": reservation.get("count"),
            "present": resources.get("present", False),
        },
        "activity": _build_activity(root, nodes, scheduler, now_iso),
        "blockers": _build_blockers(dashboard, scheduler),
        "replans": {
            "projection": dashboard.get("replan"),
            "history": _safe_replan_history(root, goal_id),
        },
        "evidence": {
            "criteria": dashboard["criteria"],
            "counts": {
                "criteria_total": counts["criteria_total"],
                "criteria_proven": counts["criteria_proven"],
                "criteria_unproven": counts["criteria_unproven"],
            },
        },
        "gate": dashboard["gate"],
        "risks": dashboard["risks"],
        "reuse": dashboard["reuse"],
        "stop": read_stop_request(root, goal_id),
        "portfolio": _safe_portfolio(root, goal_id),
        "daemon": _safe_daemon(root, goal_id),
        "artifacts": _safe_artifacts(root, goal_id),
        "local_resources": (None if local_resources is None
                            else dict(local_resources)),
        "why": dashboard.get("why", []),
    }


def render_status(snapshot: Mapping[str, Any]) -> str:
    """Compact, deterministic human status derived from the same snapshot."""
    goal = snapshot["goal"]
    final = snapshot["final"]
    proof = snapshot["proof"]
    generation = snapshot["generation"] or {}
    counts = snapshot["counts"]
    gate = snapshot["gate"]
    readiness_view = snapshot["readiness"]
    agent = snapshot["activity"]["agent"]
    lines = [
        f"goal      : {goal['goal_id']}  [{final['state']}/{final['reason']}]",
        f"objective : {goal['objective']}",
        f"graph     : {goal['graph_id']}",
        f"generation: {generation.get('generation_number', '-')} "
        f"{generation.get('generation_id', '-')}",
        f"proof     : {proof['proof_id']} persisted="
        f"{proof['persisted_proof_id']} stale={proof['stale']}",
        f"gate      : {gate['state']} human={gate['human_action_required']} "
        f"({gate['reason']})",
        f"missions  : total={counts['missions_total']} "
        f"proven={counts['missions_proven']} "
        f"incomplete={counts['missions_incomplete']} "
        f"missing={counts['missions_missing']}",
        f"nodes     : complete={len(readiness_view['complete'])} "
        f"ready={len(readiness_view['ready'])} "
        f"inflight={len(readiness_view['in_progress'])} "
        f"blocked={len(readiness_view['blocked'])}",
        f"criteria  : proven={counts['criteria_proven']}/"
        f"{counts['criteria_total']}",
        f"path      : {' '.join(snapshot['critical_path']) or '-'}",
    ]
    if agent is not None:
        elapsed = agent.get("elapsed_seconds")
        lines.append(
            f"active    : {agent.get('node_id')} "
            f"{agent.get('mission_id')} state={agent.get('mission_state')} "
            f"phase={agent.get('phase_id')} "
            f"agent={agent.get('agent_backend')} "
            f"model={agent.get('model')} "
            f"locality={agent.get('locality')} "
            f"elapsed={elapsed if elapsed is not None else '-'}s")
    else:
        lines.append("active    : -")
    blockers = snapshot["blockers"]
    lines.append(f"blockers  : {len(blockers)}")
    for blocker in blockers[:8]:
        lines.append(f"  ! {blocker['kind']}:{blocker['subject']} "
                     f"{blocker['reason']}")
    replans = snapshot["replans"]["projection"] or {}
    lines.append(
        f"replans   : accepted={replans.get('accepted', 0)} "
        f"supersessions={replans.get('supersessions', 0)} "
        f"rejected={replans.get('rejected', 0)}")
    if snapshot.get("stop") is not None:
        lines.append(f"stop      : request={snapshot['stop']}")
    return "\n".join(lines)


def render_inspect(snapshot: Mapping[str, Any]) -> str:
    """Detailed (multi-line) human rendering of the canonical snapshot."""
    lines = [render_status(snapshot), "decomposition:"]
    for node in snapshot["decomposition"]:
        lines.append(
            f"  - {node['node_id']} [{node['state']}] "
            f"mission={node.get('mission_id')} "
            f"mission_state={node.get('mission_state')} "
            f"criteria={node.get('criteria_proven')}/"
            f"{node.get('criteria_total')} reason={node.get('reason')}")
    lines.append("criteria:")
    for criterion in snapshot["evidence"]["criteria"]:
        lines.append(
            f"  - {criterion['node_id']}/{criterion['criterion_id']} "
            f"{criterion['status']} trust={criterion.get('trust')}")
    lines.append("replan history:")
    history = snapshot["replans"]["history"]
    if history is None or not history.get("events"):
        lines.append("  - (none)")
    else:
        for event in history["events"]:
            lines.append(
                f"  - {event['created_at']} {event['event']} "
                f"{event['status']} {event['reason']}")
    return "\n".join(lines)
