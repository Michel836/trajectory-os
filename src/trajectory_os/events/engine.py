"""M026 — derive authoritative events from the canonical persisted stores.

Every event in the projection is a deterministic function of persisted state
(graph, missions/phases/sub-runs, scheduler/resource decisions, replans,
artifacts, human gates, derived proof and the safe-stop marker). No event is
ever derived from model prose or from a transient in-memory value, and no
event store is a source of truth: the projection can be fully re-derived.

Read-only by construction: derivation never writes to a canonical store.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from trajectory_os import live_status
from trajectory_os.artifacts import store as artifact_store
from trajectory_os.events import model
from trajectory_os.events import store as event_store
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import store as proof_store
from trajectory_os.graph.replan import store as replan_store
from trajectory_os.graph.scheduler import engine as sched_engine
from trajectory_os.graph.scheduler import summary as sched_summary
from trajectory_os.missions import gate as mission_gate
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import store as mission_store


def _utc_now_iso() -> str:
    from trajectory_os.goals import snapshot as goal_snapshot

    return goal_snapshot.utc_now_iso()


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


def _mission_severity(state: str) -> str:
    if state in (mission_model.MS_BLOCKED, mission_model.MS_FAILED):
        return model.SEV_WARNING
    if state == mission_model.MS_COMPLETE:
        return model.SEV_NOTICE
    return model.SEV_INFO


_GOAL_PROVISIONED = model.K_GOAL_PROVISIONED
_PROOF_RECORDED = model.K_PROOF_RECORDED
_MISSION_STATE = model.K_MISSION_STATE
_MISSION_PHASE = model.K_MISSION_PHASE
_MISSION_SUBRUN = model.K_MISSION_SUBRUN
_NODE_BLOCKED = model.K_NODE_BLOCKED
_SCHEDULER_DECISION = model.K_SCHEDULER_DECISION
_RESOURCE_RESERVATION = model.K_RESOURCE_RESERVATION
_REPLAN = model.K_REPLAN
_AGENT_RUN = model.K_AGENT_RUN
_ARTIFACT_RECORDED = model.K_ARTIFACT_RECORDED
_HUMAN_GATE = model.K_HUMAN_GATE
_STOP_REQUESTED = model.K_STOP_REQUESTED


def _goal_events(root: str, goal_id: str) -> list[model.EventRecord]:
    graph, _ = graph_store.load_graph(root, goal_id)
    return [model.EventRecord.build(
        kind=_GOAL_PROVISIONED,
        severity=model.SEV_INFO,
        goal_id=goal_id,
        source="graph",
        subject=graph.graph_id,
        reason="GOAL_GRAPH_PROVISIONED",
        detail=f"{len(graph.nodes)} node(s)",
        occurred_at=graph.provenance.created_at,
        payload={
            "objective": graph.objective,
            "spec_sha256": graph.spec_sha256,
            "node_count": len(graph.nodes),
        },
    )]


def _proof_events(root: str, goal_id: str) -> list[model.EventRecord]:
    events: list[model.EventRecord] = []
    for event in proof_store.load_events(root, goal_id):
        events.append(model.EventRecord.build(
            kind=_PROOF_RECORDED,
            severity=(model.SEV_NOTICE if event.status == "COMPLETE"
                      else model.SEV_WARNING),
            goal_id=goal_id,
            source="goal_proof",
            subject=event.generation_id or event.graph_id,
            reason=event.reason,
            detail=event.status,
            occurred_at=event.computed_at,
            payload={
                "proof_id": event.proof_id,
                "graph_id": event.graph_id,
                "final_state": event.final_state,
                "complete": event.status == "COMPLETE",
            },
        ))
    return events


def _mission_events(root: str, goal_id: str,
                    mission_id: str) -> list[model.EventRecord]:
    events: list[model.EventRecord] = []
    mission, paths = mission_store.load_mission(root, mission_id)
    events.append(model.EventRecord.build(
        kind=_MISSION_STATE,
        severity=_mission_severity(mission.mission_state),
        goal_id=goal_id,
        source="mission",
        subject=mission_id,
        reason=mission.mission_reason,
        detail=mission.mission_state,
        occurred_at=mission.finished_at or mission.updated_at
        or mission.created_at,
        payload={
            "mission_id": mission_id,
            "mission_state": mission.mission_state,
            "phase_count": len(mission.phases),
        },
    ))
    for phase in mission.phases:
        events.append(model.EventRecord.build(
            kind=_MISSION_PHASE,
            severity=(model.SEV_WARNING if phase.state == mission_model.PS_FAILED
                      else model.SEV_INFO),
            goal_id=goal_id,
            source="mission",
            subject=f"{mission_id}/{phase.phase_id}",
            reason=phase.reason,
            detail=phase.state,
            occurred_at=phase.finished_at or phase.started_at,
            payload={
                "mission_id": mission_id,
                "phase_id": phase.phase_id,
                "kind": phase.kind,
                "round": phase.round,
                "attempt": phase.attempt,
                "state": phase.state,
            },
        ))
    for subrun_id in mission.subruns:
        try:
            record = mission_store.load_subrun(paths, subrun_id)
        except (mission_store.MissionNotFound,
                mission_store.MalformedMissionError):
            continue
        events.append(model.EventRecord.build(
            kind=_MISSION_SUBRUN,
            severity=(model.SEV_WARNING
                      if record.classification == mission_model.CR_FAILED
                      else model.SEV_INFO),
            goal_id=goal_id,
            source="mission",
            subject=f"{mission_id}/{subrun_id}",
            reason=record.classification,
            detail=record.phase_id,
            occurred_at=record.finished_at or record.started_at,
            payload={
                "mission_id": mission_id,
                "subrun_id": subrun_id,
                "phase_id": record.phase_id,
                "classification": record.classification,
                "exit_code": record.exit_code,
                "semantic_status": record.semantic_status,
            },
        ))
        events.extend(_agent_events(goal_id, mission_id, record))
    gate_state = mission_gate.derive_gate(mission)
    events.append(model.EventRecord.build(
        kind=_HUMAN_GATE,
        severity=(model.SEV_WARNING if gate_state == mission_gate.GATE_STOP
                  else model.SEV_NOTICE),
        goal_id=goal_id,
        source="human_gate",
        subject=mission_id,
        reason=mission_gate.gate_reason(mission, gate_state),
        detail=gate_state,
        occurred_at=mission.finished_at or mission.updated_at,
        payload={
            "mission_id": mission_id,
            "gate": gate_state,
            "human_action_required": (
                gate_state in mission_gate.HUMAN_GATES),
            "next_human_action": mission_gate.next_human_action(
                mission, gate_state),
        },
    ))
    return events


def _agent_events(goal_id: str, mission_id: str,
                  record: mission_store.SubrunDoc) -> list[model.EventRecord]:
    model_name = _command_value(record.command, "--model")
    agent_backend = _command_agent(record.command)
    provider: str | None = None
    locality: str | None = None
    normalized: str | None = None
    if model_name:
        provider, locality = live_status.classify_provider(model_name)
        normalized = live_status.normalize_model(provider, model_name)
    return [model.EventRecord.build(
        kind=_AGENT_RUN,
        severity=model.SEV_INFO,
        goal_id=goal_id,
        source="agent",
        subject=f"{mission_id}/{record.subrun_id}",
        reason=record.classification,
        detail=agent_backend,
        occurred_at=record.finished_at or record.started_at,
        payload={
            "mission_id": mission_id,
            "subrun_id": record.subrun_id,
            "agent_backend": agent_backend,
            "provider": provider,
            "model": normalized,
            "locality": locality,
        },
    )]


def _scheduler_events(root: str, goal_id: str) -> list[model.EventRecord]:
    events: list[model.EventRecord] = []
    decision = sched_engine.latest_decision(root, goal_id)
    if decision is not None:
        counts = decision.counts()
        events.append(model.EventRecord.build(
            kind=_SCHEDULER_DECISION,
            severity=model.SEV_INFO,
            goal_id=goal_id,
            source="scheduler",
            subject=decision.decision_id,
            reason=f"admitted={counts['admitted']}",
            detail=f"deferred={counts['deferred']} blocked={counts['blocked']}",
            occurred_at=decision.created_at,
            payload={
                "decision_id": decision.decision_id,
                "counts": counts,
                "admitted": [node.node_id for node in decision.admitted],
                "deferred": [node.node_id for node in decision.deferred],
                "blocked": [node.node_id for node in decision.blocked],
            },
        ))
        for node in decision.blocked:
            events.append(model.EventRecord.build(
                kind=_NODE_BLOCKED,
                severity=model.SEV_WARNING,
                goal_id=goal_id,
                source="scheduler",
                subject=node.node_id,
                reason=node.reason,
                detail=f"decision={decision.decision_id}",
                occurred_at=decision.created_at,
                payload={
                    "node_id": node.node_id,
                    "mission_id": node.mission_id,
                    "reason": node.reason,
                },
            ))
    try:
        status = sched_summary.status_document(root, goal_id)
    except Exception:  # noqa: BLE001 - read-only projection must not crash
        return events
    totals = status.get("reservation_totals")
    if isinstance(totals, dict) and _positive(totals):
        policy = status.get("policy", {})
        events.append(model.EventRecord.build(
            kind=_RESOURCE_RESERVATION,
            severity=model.SEV_INFO,
            goal_id=goal_id,
            source="resources",
            subject=status.get("decision_id") or goal_id,
            reason="RESERVATIONS_ACTIVE",
            detail=(f"cpu={totals.get('cpu_slots')} "
                    f"gpu={totals.get('gpu_slots')}"),
            occurred_at=(decision.created_at if decision is not None
                         else None),
            payload={
                "cpu_slots": totals.get("cpu_slots"),
                "gpu_slots": totals.get("gpu_slots"),
                "gpu_mem_bytes": totals.get("gpu_mem_bytes"),
                "count": totals.get("count"),
                "cpu_limit": (policy.get("cpu_slots")
                              if isinstance(policy, dict) else None),
                "gpu_limit": (policy.get("gpu_slots")
                              if isinstance(policy, dict) else None),
            },
        ))
    return events


def _positive(totals: dict[Any, Any]) -> bool:
    for key in ("cpu_slots", "gpu_slots", "gpu_mem_bytes", "count"):
        value = totals.get(key)
        if isinstance(value, int) and not isinstance(value, bool) \
                and value > 0:
            return True
    return False


def _replan_events(root: str, goal_id: str) -> list[model.EventRecord]:
    if not replan_store.replan_exists(root, goal_id):
        return []
    events: list[model.EventRecord] = []
    for event in replan_store.load_events(replan_store.replan_paths(
            root, goal_id)):
        events.append(model.EventRecord.build(
            kind=_REPLAN,
            severity=(model.SEV_NOTICE if event.status == "ACCEPTED"
                      else model.SEV_WARNING),
            goal_id=goal_id,
            source="replan",
            subject=event.generation_id or goal_id,
            reason=event.reason,
            detail=f"{event.event} {event.status}",
            occurred_at=event.created_at,
            payload={
                "event": event.event,
                "status": event.status,
                "generation_id": event.generation_id,
                "parent_generation_id": event.parent_generation_id,
                "plan_id": event.plan_id,
                "change_count": len(event.changes),
            },
        ))
    return events


def _artifact_events(root: str, goal_id: str) -> list[model.EventRecord]:
    events: list[model.EventRecord] = []
    try:
        artifacts = artifact_store.list_artifacts(root, goal_id)
    except Exception:  # noqa: BLE001 - read-only projection must not crash
        return events
    for artifact in artifacts:
        events.append(model.EventRecord.build(
            kind=_ARTIFACT_RECORDED,
            severity=model.SEV_INFO,
            goal_id=goal_id,
            source="artifact",
            subject=artifact.artifact_id,
            reason=artifact.kind,
            detail=artifact.name,
            occurred_at=artifact.created_at,
            payload={
                "artifact_id": artifact.artifact_id,
                "kind": artifact.kind,
                "name": artifact.name,
                "mission_id": artifact.mission_id,
                "content_sha256": artifact.content_sha256,
                "size_bytes": artifact.size_bytes,
                "parent_count": len(artifact.parent_ids),
            },
        ))
    return events


def _stop_events(root: str, goal_id: str) -> list[model.EventRecord]:
    from trajectory_os.goals import snapshot as goal_snapshot

    request = goal_snapshot.read_stop_request(root, goal_id)
    if request is None:
        return []
    return [model.EventRecord.build(
        kind=_STOP_REQUESTED,
        severity=model.SEV_WARNING,
        goal_id=goal_id,
        source="stop_request",
        subject=goal_id,
        reason=str(request.get("reason") or "SAFE_STOP_REQUESTED"),
        detail="safe stop is a request; in-flight work is never killed",
        occurred_at=(request.get("requested_at")
                     if isinstance(request.get("requested_at"), str)
                     else None),
        payload={"requested": True},
    )]


def _mission_ids(root: str, goal_id: str) -> list[str]:
    graph, _ = graph_store.load_graph(root, goal_id)
    ids: list[str] = []
    for node in graph.nodes:
        ref = node.mission_ref
        if ref is not None and ref.mission_id not in ids:
            ids.append(ref.mission_id)
    return ids


def project_events(root: str | Path, goal_id: str) -> list[model.EventRecord]:
    """Derive the complete (unbounded) event set for one goal (read-only).

    Deterministic: the same canonical state yields the same events and ids.
    """
    root_str = str(root)
    candidates: list[model.EventRecord] = []
    candidates.extend(_goal_events(root_str, goal_id))
    candidates.extend(_proof_events(root_str, goal_id))
    for mission_id in _mission_ids(root_str, goal_id):
        try:
            candidates.extend(_mission_events(root_str, goal_id, mission_id))
        except (mission_store.MissionNotFound,
                mission_store.MalformedMissionError):
            continue
    candidates.extend(_scheduler_events(root_str, goal_id))
    candidates.extend(_replan_events(root_str, goal_id))
    candidates.extend(_artifact_events(root_str, goal_id))
    candidates.extend(_stop_events(root_str, goal_id))
    return event_store.dedupe_sorted(candidates)


class RefreshResult:
    """Outcome of refreshing the persisted event projection."""

    def __init__(self, goal_id: str, document: dict[str, object],
                 records: Iterable[model.EventRecord],
                 added: Iterable[model.EventRecord],
                 removed: Iterable[model.EventRecord]) -> None:
        self.goal_id = goal_id
        self.document = document
        self.records = tuple(records)
        self.added = tuple(added)
        self.removed = tuple(removed)

    @property
    def count(self) -> int:
        return len(self.records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "OK",
            **event_store.projection_summary(self.document),
            "added": [record.event_id for record in self.added],
            "removed": [record.event_id for record in self.removed],
            "events": [record.to_dict() for record in self.records],
        }


def refresh(root: str | Path, goal_id: str, *,
            clock: Callable[[], str] | None = None) -> RefreshResult:
    """Re-derive and persist the bounded, deduplicated projection.

    Restart-safe: the persisted projection is always exactly the bounded,
    deduplicated derivation of the current canonical state, so re-running
    after a restart is an idempotent no-op when nothing changed.
    """
    root_str = str(root)
    try:
        previous = event_store.load_events(root_str, goal_id)
    except model.EventError:
        previous = []
    current = project_events(root_str, goal_id)
    added, removed = event_store.changed(previous, current)
    stamp = (clock or _utc_now_iso)()
    document = event_store.save_projection(
        root_str, goal_id, current, updated_at=stamp)
    return RefreshResult(goal_id, document, current, added, removed)


def persisted(root: str | Path,
              goal_id: str) -> list[model.EventRecord]:
    """Read the persisted projection (empty when never refreshed)."""
    try:
        return event_store.load_events(root, goal_id)
    except model.EventError:
        return []
