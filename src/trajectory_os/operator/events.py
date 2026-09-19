"""M043 — unified durable event model spanning mission and release.

An additive, append-only *projection/replay substrate*. It never replaces the
canonical ``status.json`` / ``closure.json`` / release artifacts: those stay
authoritative, and every event here is either a durably recorded operator
action or a deterministic projection of those artifacts.

Envelope fields: ``schema_version``, ``event_id``, ``sequence``, ``ts``,
``mission_id``, ``run_id``, ``release_id``, ``type``, ``source``, ``actor``,
``phase``, ``causation_id``, ``parent_id``, ``payload``, ``provenance``.

Design invariants:

* append-only — recorded events are never rewritten;
* deterministic ordering — by ``(sequence, ts, event_id)``;
* replayable — :func:`replay` is a pure read; it mutates nothing;
* duplicate handling is deterministic — the same semantic event (identity
  excludes ``sequence`` and ``ts``) is deduplicated by ``event_id``;
* schema evolution is explicit through ``schema_version``;
* migration compatibility with M030–M039 evidence is provided by
  :func:`derive_events`, which reads the canonical artifacts read-only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from trajectory_os.assembly import model as assembly_model
from trajectory_os.assembly import store as assembly_store
from trajectory_os.observability import store as obs_store
from trajectory_os.operator import model
from trajectory_os.operator._util import (
    append_jsonl,
    digest,
    optional_str,
    read_jsonl,
    read_optional_json,
    utc_now,
    write_json,
)
from trajectory_os.release import store as release_store

#: Domain id for one operator event identity.
EVENT_DOMAIN = "trajectory-os.operator-event.v1"

#: Domain id for one unified projection.
PROJECTION_DOMAIN = "trajectory-os.operator-projection.v1"

# --- event types (closed set) -------------------------------------------------

T_MISSION_CREATED = "MISSION_CREATED"
T_MISSION_STATUS = "MISSION_STATUS"
T_MISSION_CLOSED = "MISSION_CLOSED"
T_POLICY_RESOLVED = "POLICY_RESOLVED"
T_ROUTING_DECIDED = "ROUTING_DECIDED"
T_PATCH_CAPTURED = "PATCH_CAPTURED"
T_VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
T_REVIEW_COMPLETED = "REVIEW_COMPLETED"
T_READY_FOR_COMMIT = "READY_FOR_COMMIT"
T_COMMIT_HANDOFF = "COMMIT_HANDOFF"
T_GO_COMMIT = "GO_COMMIT"
T_COMMIT_RESULT = "COMMIT_RESULT"
T_PR_BOUND = "PR_BOUND"
T_CI_OBSERVED = "CI_OBSERVED"
T_MERGE_HANDOFF = "MERGE_HANDOFF"
T_GO_MERGE = "GO_MERGE"
T_MERGE_RESULT = "MERGE_RESULT"
T_RELEASE_CLOSURE = "RELEASE_CLOSURE"
T_RELEASE_ACTION = "RELEASE_ACTION"
T_RECOVERY_DECIDED = "RECOVERY_DECIDED"
T_CONTROL = "CONTROL"
T_LEGACY_CANONICAL = "LEGACY_CANONICAL"

EVENT_TYPES = frozenset({
    T_MISSION_CREATED, T_MISSION_STATUS, T_MISSION_CLOSED, T_POLICY_RESOLVED,
    T_ROUTING_DECIDED, T_PATCH_CAPTURED, T_VALIDATION_COMPLETED,
    T_REVIEW_COMPLETED, T_READY_FOR_COMMIT, T_COMMIT_HANDOFF, T_GO_COMMIT,
    T_COMMIT_RESULT, T_PR_BOUND, T_CI_OBSERVED, T_MERGE_HANDOFF, T_GO_MERGE,
    T_MERGE_RESULT, T_RELEASE_CLOSURE, T_RELEASE_ACTION, T_RECOVERY_DECIDED,
    T_CONTROL, T_LEGACY_CANONICAL,
})

# --- event sources (closed set) ----------------------------------------------

S_MISSION = "mission"
S_RUNTIME = "runtime"
S_RELEASE = "release"
S_POLICY = "policy"
S_ROUTING = "routing"
S_RECOVERY = "recovery"
S_CONTROL = "control"
S_LEGACY = "legacy"

EVENT_SOURCES = frozenset({
    S_MISSION, S_RUNTIME, S_RELEASE, S_POLICY, S_ROUTING, S_RECOVERY,
    S_CONTROL, S_LEGACY,
})


@dataclass(frozen=True)
class OperatorEvent:
    """One unified durable operator event."""

    event_id: str
    sequence: int
    ts: str
    mission_id: str
    run_id: str
    release_id: str | None
    type: str
    source: str
    actor: str
    phase: str
    causation_id: str | None
    parent_id: str | None
    payload: Mapping[str, Any]
    provenance: Mapping[str, Any]
    schema_version: int = model.SCHEMA_VERSION

    def identity_payload(self) -> dict[str, Any]:
        """Identity excludes ``sequence``/``ts`` so duplicates dedupe."""
        return {
            "schema_version": model.SCHEMA_VERSION,
            "mission_id": self.mission_id,
            "run_id": self.run_id,
            "release_id": self.release_id,
            "type": self.type,
            "source": self.source,
            "actor": self.actor,
            "phase": self.phase,
            "causation_id": self.causation_id,
            "parent_id": self.parent_id,
            "payload": dict(sorted(self.payload.items())),
            "provenance": dict(sorted(self.provenance.items())),
        }

    def compute_event_id(self) -> str:
        return digest(self.identity_payload(), domain=EVENT_DOMAIN)

    def validate(self) -> OperatorEvent:
        if self.schema_version != model.SCHEMA_VERSION:
            model.fail(model.E_EVENT_VERSION, str(self.schema_version))
        if self.type not in EVENT_TYPES:
            model.fail(model.E_EVENT_MALFORMED, f"type {self.type!r}")
        if self.source not in EVENT_SOURCES:
            model.fail(model.E_EVENT_MALFORMED, f"source {self.source!r}")
        if not self.mission_id or not self.run_id:
            model.fail(model.E_EVENT_MALFORMED, "mission/run identity required")
        if self.sequence < 0:
            model.fail(model.E_EVENT_MALFORMED, "negative sequence")
        if self.event_id != self.compute_event_id():
            model.fail(model.E_EVENT_IDENTITY, self.event_id)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "ts": self.ts,
            "mission_id": self.mission_id,
            "run_id": self.run_id,
            "release_id": self.release_id,
            "type": self.type,
            "source": self.source,
            "actor": self.actor,
            "phase": self.phase,
            "causation_id": self.causation_id,
            "parent_id": self.parent_id,
            "payload": dict(self.payload),
            "provenance": dict(self.provenance),
        }

    @staticmethod
    def build(*, mission_id: str, run_id: str | None = None, type: str,
              source: str, actor: str = "system", phase: str = "",
              payload: Mapping[str, Any] | None = None,
              provenance: Mapping[str, Any] | None = None,
              release_id: str | None = None,
              causation_id: str | None = None, parent_id: str | None = None,
              ts: str = "", sequence: int = 0) -> OperatorEvent:
        event = OperatorEvent(
            event_id="", sequence=sequence, ts=ts, mission_id=mission_id,
            run_id=run_id or mission_id, release_id=release_id, type=type,
            source=source, actor=actor, phase=phase,
            causation_id=causation_id, parent_id=parent_id,
            payload=dict(payload or {}), provenance=dict(provenance or {}))
        return replace(event, event_id=event.compute_event_id()).validate()

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> OperatorEvent:
        if data.get("schema_version") != model.SCHEMA_VERSION:
            model.fail(model.E_EVENT_VERSION,
                       str(data.get("schema_version")))
        payload = data.get("payload")
        provenance = data.get("provenance")
        event = OperatorEvent(
            event_id=str(data.get("event_id", "")),
            sequence=int(data.get("sequence", 0)),
            ts=str(data.get("ts", "")),
            mission_id=str(data.get("mission_id", "")),
            run_id=str(data.get("run_id", "")),
            release_id=optional_str(data.get("release_id")),
            type=str(data.get("type", "")),
            source=str(data.get("source", "")),
            actor=str(data.get("actor", "")),
            phase=str(data.get("phase", "")),
            causation_id=optional_str(data.get("causation_id")),
            parent_id=optional_str(data.get("parent_id")),
            payload=(dict(payload) if isinstance(payload, Mapping) else {}),
            provenance=(dict(provenance)
                        if isinstance(provenance, Mapping) else {}),
            schema_version=int(data.get("schema_version",
                                        model.SCHEMA_VERSION)),
        )
        return event.validate()


# --- durable recorded events --------------------------------------------------


def _events_path(root: str, mission_id: str) -> Path:
    return (Path(assembly_store.mission_root(root, mission_id))
            / model.OPERATOR_EVENTS_NAME)


def load_events(root: str, mission_id: str) -> list[OperatorEvent]:
    """Read the recorded operator events (bounded, deterministic order)."""
    raw = read_jsonl(_events_path(root, mission_id))
    events = [OperatorEvent.from_dict(document) for document in raw]
    events.sort(key=lambda event: (event.sequence, event.ts, event.event_id))
    return events[-model.MAX_OPERATOR_EVENTS:]


def record_event(root: str, *, mission_id: str, type: str, source: str,
                 payload: Mapping[str, Any] | None = None,
                 actor: str = "system", phase: str = "",
                 release_id: str | None = None,
                 causation_id: str | None = None,
                 parent_id: str | None = None,
                 provenance: Mapping[str, Any] | None = None,
                 clock: Callable[[], str] = utc_now) -> OperatorEvent:
    """Append one operator event; an identical semantic event is idempotent."""
    existing = load_events(root, mission_id)
    candidate = OperatorEvent.build(
        mission_id=mission_id, type=type, source=source, actor=actor,
        phase=phase, payload=payload, provenance=provenance,
        release_id=release_id, causation_id=causation_id,
        parent_id=parent_id, ts=clock(),
        sequence=len(existing) + 1)
    for event in existing:
        if event.event_id == candidate.event_id:
            return event
    append_jsonl(_events_path(root, mission_id), candidate.to_dict())
    return candidate


# --- derived projection (migration compatibility with M030–M039) -------------


def _artifact(root: str, mission_id: str, name: str,
              ) -> dict[str, Any] | None:
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    return read_optional_json(mission_root / name)


def _derived(*, mission_id: str, type: str, source: str, phase: str,
             payload: Mapping[str, Any], artifact: str, actor: str = "system",
             release_id: str | None = None,
             causation_id: str | None = None,
             parent_id: str | None = None, ts: str = "") -> OperatorEvent:
    return OperatorEvent.build(
        mission_id=mission_id, type=type, source=source, actor=actor,
        phase=phase, payload=payload, release_id=release_id,
        causation_id=causation_id, parent_id=parent_id, ts=ts,
        provenance={"artifact": artifact, "layer": "derived"})


def _status_event(root: str, mission_id: str) -> list[OperatorEvent]:
    document = _artifact(root, mission_id, obs_store.STATUS_NAME)
    if document is None:
        return []
    return [_derived(
        mission_id=mission_id, type=T_MISSION_STATUS, source=S_RUNTIME,
        phase=str(document.get("phase", "")),
        payload={
            "lifecycle": document.get("state"),
            "readiness": document.get("readiness"),
            "stage": document.get("stage"),
            "phase": document.get("phase"),
            "attempt": document.get("attempt"),
            "reviewed_patch": document.get("reviewed_patch"),
            "current_patch": document.get("current_patch"),
            "updated_at": document.get("updated_at"),
        },
        artifact=obs_store.STATUS_NAME,
        ts=str(document.get("updated_at") or ""))]


def _closure_event(root: str, mission_id: str) -> list[OperatorEvent]:
    document = _artifact(root, mission_id, assembly_store.CLOSURE_NAME)
    if document is None:
        return []
    return [_derived(
        mission_id=mission_id, type=T_MISSION_CLOSED, source=S_MISSION,
        phase=assembly_model.MP_CLOSURE,
        payload={
            "lifecycle": document.get("lifecycle"),
            "readiness": document.get("readiness"),
            "reviewed_patch": document.get("reviewed_patch"),
            "current_patch": document.get("current_patch"),
            "attempts": document.get("attempts"),
            "repairs": document.get("repairs"),
            "created_at": document.get("created_at"),
        },
        artifact=assembly_store.CLOSURE_NAME,
        ts=str(document.get("created_at") or ""))]


def _mission_event(root: str, mission_id: str) -> list[OperatorEvent]:
    document = _artifact(root, mission_id, assembly_store.MISSION_NAME)
    if document is None:
        return []
    return [_derived(
        mission_id=mission_id, type=T_MISSION_CREATED, source=S_MISSION,
        phase=assembly_model.MP_INTAKE,
        payload={
            "objective": document.get("objective"),
            "backend": document.get("backend"),
            "provider": document.get("provider"),
            "model": document.get("model"),
            "workload_id": document.get("workload_id"),
            "created_at": document.get("created_at"),
        },
        artifact=assembly_store.MISSION_NAME,
        ts=str(document.get("created_at") or ""))]


def _policy_event(root: str, mission_id: str) -> list[OperatorEvent]:
    document = _artifact(root, mission_id, model.POLICY_NAME)
    if document is None:
        return []
    return [_derived(
        mission_id=mission_id, type=T_POLICY_RESOLVED, source=S_POLICY,
        phase="POLICY",
        payload={
            "policy_id": document.get("policy_id"),
            "profile": document.get("profile"),
            "source": document.get("source"),
            "overrides": document.get("explicit_overrides"),
            "resolved_at": document.get("resolved_at"),
        },
        artifact=model.POLICY_NAME,
        ts=str(document.get("resolved_at") or ""))]


def _routing_event(root: str, mission_id: str) -> list[OperatorEvent]:
    document = _artifact(root, mission_id, model.ROUTING_NAME)
    if document is None:
        return []
    return [_derived(
        mission_id=mission_id, type=T_ROUTING_DECIDED, source=S_ROUTING,
        phase="ROUTING",
        payload={
            "policy_id": document.get("policy_id"),
            "implementation": document.get("implementation"),
            "inline_review": document.get("inline_review"),
            "final_review": document.get("final_review"),
            "fallback_used": document.get("fallback_used"),
            "fallback_reason": document.get("fallback_reason"),
            "decided_at": document.get("decided_at"),
        },
        artifact=model.ROUTING_NAME,
        ts=str(document.get("decided_at") or ""))]


def _release_events(root: str, mission_id: str) -> list[OperatorEvent]:
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    events: list[OperatorEvent] = []
    mapping = (
        (release_store.COMMIT_HANDOFF_NAME, T_COMMIT_HANDOFF, "created_at"),
        (release_store.COMMIT_RESULT_NAME, T_COMMIT_RESULT, "created_at"),
        (release_store.PR_BINDING_NAME, T_PR_BOUND, "bound_at"),
        (release_store.CI_STATUS_NAME, T_CI_OBSERVED, "checked_at"),
        (release_store.MERGE_HANDOFF_NAME, T_MERGE_HANDOFF, "created_at"),
        (release_store.MERGE_RESULT_NAME, T_MERGE_RESULT, "merged_at"),
        (release_store.RELEASE_CLOSURE_NAME, T_RELEASE_CLOSURE, "created_at"),
    )
    for name, type_text, ts_key in mapping:
        document = read_optional_json(mission_root / name)
        if document is None:
            continue
        events.append(_derived(
            mission_id=mission_id, type=type_text, source=S_RELEASE,
            phase="RELEASE", payload=document, artifact=name,
            release_id=mission_id, ts=str(document.get(ts_key) or "")))
    for record in release_store.load_release_events(mission_root):
        events.append(_derived(
            mission_id=mission_id,
            type=T_RELEASE_ACTION,
            source=S_RELEASE, phase="RELEASE", payload=record,
            artifact=release_store.RELEASE_EVENTS_NAME,
            release_id=mission_id, ts=str(record.get("at") or "")))
    return events


def _legacy_canonical_events(root: str, mission_id: str) -> list[OperatorEvent]:
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    events: list[OperatorEvent] = []
    for record in obs_store.load_events(mission_root):
        events.append(_derived(
            mission_id=mission_id, type=T_LEGACY_CANONICAL, source=S_LEGACY,
            phase=str(record.get("phase", "")),
            payload={
                "kind": record.get("kind"),
                "result": record.get("result"),
                "gate": record.get("gate"),
                "patch": record.get("patch"),
                "stage": record.get("stage"),
                "attempt": record.get("attempt"),
                "detail": record.get("detail"),
            },
            artifact=obs_store.EVENTS_NAME,
            actor=str(record.get("actor") or "system"),
            ts=str(record.get("at") or "")))
    return events


def derive_events(root: str, mission_id: str) -> list[OperatorEvent]:
    """Derive unified events from canonical M030–M039 artifacts (read-only)."""
    events: list[OperatorEvent] = []
    events.extend(_mission_event(root, mission_id))
    events.extend(_policy_event(root, mission_id))
    events.extend(_routing_event(root, mission_id))
    events.extend(_legacy_canonical_events(root, mission_id))
    events.extend(_status_event(root, mission_id))
    events.extend(_closure_event(root, mission_id))
    events.extend(_release_events(root, mission_id))
    return _dedupe_sort(events)


def _dedupe_sort(events: list[OperatorEvent]) -> list[OperatorEvent]:
    seen: set[str] = set()
    unique: list[OperatorEvent] = []
    for event in events:
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        unique.append(event)
    unique.sort(key=lambda event: (event.sequence, event.ts, event.event_id))
    return unique


def replay(root: str, mission_id: str) -> dict[str, Any]:
    """Replay the unified event stream (pure, read-only, idempotent)."""
    recorded = load_events(root, mission_id)
    derived = derive_events(root, mission_id)
    combined = _dedupe_sort([*derived, *recorded])
    projection = derive_projection(root, mission_id)
    replay_id = digest(
        {"mission_id": mission_id,
         "event_ids": [event.event_id for event in combined],
         "projection_id": projection.get("projection_id")},
        domain=PROJECTION_DOMAIN)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "operator_version": model.OPERATOR_VERSION,
        "mission_id": mission_id,
        "replay_id": replay_id,
        "event_count": len(combined),
        "recorded_count": len(recorded),
        "derived_count": len(derived),
        "events": [event.to_dict() for event in combined],
        "projection": projection,
    }


def derive_projection(root: str, mission_id: str) -> dict[str, Any]:
    """Derive the unified operator projection from canonical artifacts."""
    mission = _artifact(root, mission_id, assembly_store.MISSION_NAME)
    status = _artifact(root, mission_id, obs_store.STATUS_NAME)
    closure = _artifact(root, mission_id, assembly_store.CLOSURE_NAME)
    policy = _artifact(root, mission_id, model.POLICY_NAME)
    routing = _artifact(root, mission_id, model.ROUTING_NAME)
    release_state = _artifact(root, mission_id,
                              release_store.RELEASE_STATE_NAME)
    commit_result = _artifact(root, mission_id,
                              release_store.COMMIT_RESULT_NAME)
    pr_binding = _artifact(root, mission_id, release_store.PR_BINDING_NAME)
    ci_status = _artifact(root, mission_id, release_store.CI_STATUS_NAME)
    merge_result = _artifact(root, mission_id, release_store.MERGE_RESULT_NAME)
    release_closure = _artifact(root, mission_id,
                                release_store.RELEASE_CLOSURE_NAME)
    body: dict[str, Any] = {
        "mission_id": mission_id,
        "run_id": mission_id,
        "objective": (mission or {}).get("objective"),
        "policy": (None if policy is None else {
            "profile": policy.get("profile"),
            "policy_id": policy.get("policy_id")}),
        "routing": (None if routing is None else {
            "implementation": routing.get("implementation"),
            "inline_review": routing.get("inline_review"),
            "final_review": routing.get("final_review")}),
        "lifecycle": (status or {}).get("state"),
        "readiness": (status or {}).get("readiness"),
        "stage": (status or {}).get("stage"),
        "phase": (status or {}).get("phase"),
        "attempt": (status or {}).get("attempt"),
        "current_patch": (status or {}).get("current_patch"),
        "reviewed_patch": (status or {}).get("reviewed_patch"),
        "closure_readiness": (closure or {}).get("readiness"),
        "release_stage": (release_state or {}).get("stage"),
        "commit_sha": (commit_result or {}).get("commit_sha"),
        "pr_number": (pr_binding or {}).get("number"),
        "pr_head_sha": (pr_binding or {}).get("head_sha"),
        "ci_state": (ci_status or {}).get("state"),
        "merge_sha": (merge_result or {}).get("merge_sha"),
        "release_status": (release_closure or {}).get("status"),
    }
    projection_id = digest(body, domain=PROJECTION_DOMAIN)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "operator_version": model.OPERATOR_VERSION,
        "projection_id": projection_id,
        **body,
    }


def persist_events_document(root: str, mission_id: str) -> None:
    """Persist the recorded event stream + derived projection (explicit only)."""
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    write_json(mission_root / "operator-projection.json",
               derive_projection(root, mission_id))


__all__ = [
    "EVENT_DOMAIN",
    "EVENT_SOURCES",
    "EVENT_TYPES",
    "PROJECTION_DOMAIN",
    "S_CONTROL",
    "S_LEGACY",
    "S_MISSION",
    "S_POLICY",
    "S_RECOVERY",
    "S_RELEASE",
    "S_ROUTING",
    "S_RUNTIME",
    "T_CI_OBSERVED",
    "T_COMMIT_HANDOFF",
    "T_COMMIT_RESULT",
    "T_CONTROL",
    "T_GO_COMMIT",
    "T_GO_MERGE",
    "T_LEGACY_CANONICAL",
    "T_MERGE_HANDOFF",
    "T_MERGE_RESULT",
    "T_MISSION_CLOSED",
    "T_MISSION_CREATED",
    "T_MISSION_STATUS",
    "T_PATCH_CAPTURED",
    "T_POLICY_RESOLVED",
    "T_PR_BOUND",
    "T_READY_FOR_COMMIT",
    "T_RECOVERY_DECIDED",
    "T_RELEASE_ACTION",
    "T_RELEASE_CLOSURE",
    "T_REVIEW_COMPLETED",
    "T_ROUTING_DECIDED",
    "T_VALIDATION_COMPLETED",
    "OperatorEvent",
    "derive_events",
    "derive_projection",
    "load_events",
    "persist_events_document",
    "record_event",
    "replay",
]
