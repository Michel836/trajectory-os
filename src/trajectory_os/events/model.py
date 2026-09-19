"""M026 — authoritative event/notification domain model (pure, fail closed).

One :class:`EventRecord` is a bounded, content-addressed, machine-readable
statement about authoritative persisted state. Its identity is derived from
``(category, kind, severity, goal_id, subject, reason, detail, source,
payload)`` and deliberately *excludes* the observation clock: re-deriving the
same canonical state after a restart yields exactly the same ``event_id``, so
the projection is deduplicated and restart-safe.

Categories cover the authoritative surfaces the program exposes: goals,
missions, blockers, scheduler/resource state, replans, agents/models,
artifacts, human gates and the final derived proof. Event ``kind`` values are
a closed set mapped 1:1 to a category, so a caller can never invent a category
or silently re-label an event.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, NoReturn, cast

from trajectory_os.events import identity as event_identity

#: Schema version of every durable event document.
SCHEMA_VERSION = 1

#: Human/machine events version string (additive, never a digest input).
EVENTS_VERSION = "m026.1"

# --- categories (closed set) --------------------------------------------------

CAT_GOAL = "GOAL"
CAT_MISSION = "MISSION"
CAT_BLOCKER = "BLOCKER"
CAT_SCHEDULER = "SCHEDULER"
CAT_RESOURCE = "RESOURCE"
CAT_REPLAN = "REPLAN"
CAT_AGENT = "AGENT"
CAT_ARTIFACT = "ARTIFACT"
CAT_GATE = "GATE"
CAT_PROOF = "PROOF"

CATEGORIES = frozenset({
    CAT_GOAL, CAT_MISSION, CAT_BLOCKER, CAT_SCHEDULER, CAT_RESOURCE,
    CAT_REPLAN, CAT_AGENT, CAT_ARTIFACT, CAT_GATE, CAT_PROOF,
})

# --- severities (closed set) --------------------------------------------------

SEV_INFO = "INFO"
SEV_NOTICE = "NOTICE"
SEV_WARNING = "WARNING"
SEV_CRITICAL = "CRITICAL"

SEVERITIES = frozenset({SEV_INFO, SEV_NOTICE, SEV_WARNING, SEV_CRITICAL})

# --- event kinds (closed set, each mapped to exactly one category) ------------

K_GOAL_PROVISIONED = "goal_provisioned"
K_PROOF_RECORDED = "proof_recorded"
K_MISSION_STATE = "mission_state"
K_MISSION_PHASE = "mission_phase"
K_MISSION_SUBRUN = "mission_subrun"
K_NODE_BLOCKED = "node_blocked"
K_SCHEDULER_DECISION = "scheduler_decision"
K_RESOURCE_RESERVATION = "resource_reservation"
K_REPLAN = "replan"
K_AGENT_RUN = "agent_run"
K_ARTIFACT_RECORDED = "artifact_recorded"
K_HUMAN_GATE = "human_gate"
K_STOP_REQUESTED = "stop_requested"

KIND_CATEGORY: dict[str, str] = {
    K_GOAL_PROVISIONED: CAT_GOAL,
    K_PROOF_RECORDED: CAT_PROOF,
    K_MISSION_STATE: CAT_MISSION,
    K_MISSION_PHASE: CAT_MISSION,
    K_MISSION_SUBRUN: CAT_MISSION,
    K_NODE_BLOCKED: CAT_BLOCKER,
    K_SCHEDULER_DECISION: CAT_SCHEDULER,
    K_RESOURCE_RESERVATION: CAT_RESOURCE,
    K_REPLAN: CAT_REPLAN,
    K_AGENT_RUN: CAT_AGENT,
    K_ARTIFACT_RECORDED: CAT_ARTIFACT,
    K_HUMAN_GATE: CAT_GATE,
    K_STOP_REQUESTED: CAT_GATE,
}

KINDS = frozenset(KIND_CATEGORY)

# --- bounded limits (hard caps; no configuration may exceed them) ------------

MAX_EVENTS = 1024
MAX_EVENT_IDS = MAX_EVENTS
MAX_TIMESTAMP_LEN = 64
MAX_SUBJECT_LEN = 256
MAX_REASON_LEN = 128
MAX_DETAIL_LEN = 512
MAX_SOURCE_LEN = 64
MAX_PAYLOAD_BYTES = 8192

# --- stable fail-closed error codes ------------------------------------------

E_MALFORMED = "MALFORMED_EVENT"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_EVENT_SCHEMA"
E_IDENTITY_MISMATCH = "EVENT_IDENTITY_MISMATCH"
E_OVERFLOW = "EVENT_OVERFLOW"


class EventError(Exception):
    """Malformed or untrusted event/projection state (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise EventError(code, detail)


def _require_str(value: object, field: str, *, maximum: int,
                 optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        _fail(E_MALFORMED, f"{field} must be a non-empty string")
    if len(value) > maximum:
        _fail(E_MALFORMED, f"{field} exceeds {maximum} chars")
    return value


def _payload(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _fail(E_MALFORMED, "payload must be an object")
    materialized = {str(key): item for key, item in value.items()}
    text = event_identity.canonical_json(materialized)
    if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        _fail(E_MALFORMED, f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    try:
        # Round-trip through canonical JSON to guarantee a plain, JSON-safe
        # value graph (no tuples, no exotic Mapping subtypes).
        decoded: object = json.loads(text)
    except (ValueError, TypeError):
        _fail(E_MALFORMED, "payload is not JSON-serializable")
    if not isinstance(decoded, dict):
        _fail(E_MALFORMED, "payload must be an object")
    plain = cast("dict[str, Any]", decoded)
    for key in plain:
        if not isinstance(key, str):
            _fail(E_MALFORMED, "payload keys must be strings")
    return plain


@dataclass(frozen=True)
class EventRecord:
    """One authoritative, content-addressed event projection record."""

    event_id: str
    category: str
    kind: str
    severity: str
    goal_id: str
    source: str
    subject: str | None = None
    reason: str | None = None
    detail: str | None = None
    occurred_at: str | None = None
    payload: Mapping[str, Any] | None = None
    schema_version: int = SCHEMA_VERSION

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "category": self.category,
            "kind": self.kind,
            "severity": self.severity,
            "goal_id": self.goal_id,
            "subject": self.subject,
            "reason": self.reason,
            "detail": self.detail,
            "source": self.source,
            "payload": (None if self.payload is None
                        else dict(self.payload)),
        }

    def compute_event_id(self) -> str:
        return event_identity.event_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
        }

    @staticmethod
    def build(
        *,
        kind: str,
        severity: str,
        goal_id: str,
        source: str,
        subject: str | None = None,
        reason: str | None = None,
        detail: str | None = None,
        occurred_at: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        if kind not in KIND_CATEGORY:
            _fail(E_MALFORMED, f"unknown event kind {kind!r}")
        if severity not in SEVERITIES:
            _fail(E_MALFORMED, f"unknown severity {severity!r}")
        _require_str(goal_id, "goal_id", maximum=MAX_SUBJECT_LEN,
                     optional=False)
        _require_str(source, "source", maximum=MAX_SOURCE_LEN, optional=False)
        _require_str(subject, "subject", maximum=MAX_SUBJECT_LEN)
        _require_str(reason, "reason", maximum=MAX_REASON_LEN)
        _require_str(detail, "detail", maximum=MAX_DETAIL_LEN)
        _require_str(occurred_at, "occurred_at", maximum=MAX_TIMESTAMP_LEN)
        base = EventRecord(
            event_id="", category=KIND_CATEGORY[kind], kind=kind,
            severity=severity, goal_id=goal_id, source=source,
            subject=subject, reason=reason, detail=detail,
            occurred_at=occurred_at, payload=_payload(payload))
        return replace(base, event_id=base.compute_event_id())

    @classmethod
    def from_dict(cls, data: object) -> EventRecord:
        if not isinstance(data, dict):
            _fail(E_MALFORMED, f"event: {type(data).__name__}")
        if data.get("schema_version") != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, "event schema")
        record = cls.build(
            kind=data.get("kind"),  # type: ignore[arg-type]
            severity=data.get("severity"),  # type: ignore[arg-type]
            goal_id=data.get("goal_id"),  # type: ignore[arg-type]
            source=data.get("source"),  # type: ignore[arg-type]
            subject=data.get("subject"),
            reason=data.get("reason"),
            detail=data.get("detail"),
            occurred_at=data.get("occurred_at"),
            payload=data.get("payload"),
        )
        stored = data.get("event_id")
        if stored is not None and stored != record.event_id:
            _fail(E_IDENTITY_MISMATCH, "event")
        return record


__all__ = [
    "CATEGORIES", "CAT_AGENT", "CAT_ARTIFACT", "CAT_BLOCKER", "CAT_GATE",
    "CAT_GOAL", "CAT_MISSION", "CAT_PROOF", "CAT_REPLAN", "CAT_RESOURCE",
    "CAT_SCHEDULER", "E_IDENTITY_MISMATCH", "E_MALFORMED", "E_OVERFLOW",
    "E_UNSUPPORTED_VERSION", "EVENTS_VERSION", "EventError", "EventRecord",
    "KINDS", "KIND_CATEGORY", "K_AGENT_RUN", "K_ARTIFACT_RECORDED",
    "K_GOAL_PROVISIONED", "K_HUMAN_GATE", "K_MISSION_PHASE",
    "K_MISSION_STATE", "K_MISSION_SUBRUN", "K_NODE_BLOCKED", "K_PROOF_RECORDED",
    "K_REPLAN", "K_RESOURCE_RESERVATION", "K_SCHEDULER_DECISION",
    "K_STOP_REQUESTED", "MAX_EVENTS", "MAX_EVENT_IDS", "SCHEMA_VERSION",
    "SEVERITIES", "SEV_CRITICAL", "SEV_INFO", "SEV_NOTICE", "SEV_WARNING",
]
