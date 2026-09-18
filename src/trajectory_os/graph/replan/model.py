"""Mission 015 — bounded adaptive-replanning domain model (pure, fail closed).

This module owns the deterministic, bounded representation of explicit
replan triggers, the deterministic replan policy, declarative graph changes,
one validated replan plan, immutable graph generations and the append-only
replan event history. It performs no I/O, no clock reads and no mission
access: normalization and validation are pure functions of their inputs.

Canonical invariants (ADR-013):

* replanning is *graph evolution only* — it never marks a node complete,
  never copies mission success and never promotes semantic evidence;
* every replan is driven by one explicit machine-readable trigger that
  carries evidence provenance; free-form operator prose is never parsed;
* history is append-only and every mutation creates a new explicit graph
  generation while preserving every prior generation;
* a plan is validated (schema, dependencies, cycles, resources, reuse)
  before it is activated; a rejected plan writes no new generation;
* generation, plan, trigger, policy and event identities are
  domain-separated digests over normalized content and never include a
  timestamp;
* persisted state is strictly reconstructed and fails closed on malformed,
  contradictory, oversized or identity-mismatched content.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NoReturn

from trajectory_os.graph import model as graph_model
from trajectory_os.graph.replan import identity as replan_identity

#: Schema version of every durable replan document.
SCHEMA_VERSION = 1

#: Human/machine replan version string (additive, never an input digest).
REPLAN_VERSION = "m015.1"

# --- hard bounded limits (no configuration may exceed these) -----------------

MAX_GENERATIONS = 64
MAX_EVENTS = 8192
MAX_CHANGES = 256
MAX_TRIGGER_DETAIL_LEN = 512
MAX_PROVENANCE_NOTE_LEN = 512
MAX_REASON_LEN = 256
MAX_TIMESTAMP_LEN = 64
MAX_SOURCE_LEN = 128
MAX_CHANGE_SUMMARY = 512

# --- stable fail-closed error codes ------------------------------------------

E_MALFORMED = "MALFORMED_REPLAN"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_REPLAN_SCHEMA"
E_IDENTITY_MISMATCH = "REPLAN_IDENTITY_MISMATCH"
E_GRAPH_MISMATCH = "REPLAN_GRAPH_IDENTITY_MISMATCH"
E_EVENT_LOG = "MALFORMED_REPLAN_EVENT_LOG"
E_OVERFLOW = "REPLAN_HISTORY_OVERFLOW"
E_CONTRADICTORY = "CONTRADICTORY_REPLAN_STATE"
E_CYCLE = "REPLAN_CYCLE_REJECTED"
E_STALE_TRIGGER = "REPLAN_TRIGGER_STALE"
E_VALIDATION = "REPLAN_VALIDATION_FAILED"

# --- trigger kinds (closed set) ----------------------------------------------

TK_MISSION_FAILURE = "MISSION_FAILURE"
TK_BLOCKER = "BLOCKER"
TK_INVALIDATED_ASSUMPTION = "INVALIDATED_ASSUMPTION"
TK_NEW_PROVEN_EVIDENCE = "NEW_PROVEN_EVIDENCE"
TK_OPERATOR_REQUEST = "OPERATOR_REQUEST"

TRIGGER_KINDS = frozenset({
    TK_MISSION_FAILURE, TK_BLOCKER, TK_INVALIDATED_ASSUMPTION,
    TK_NEW_PROVEN_EVIDENCE, TK_OPERATOR_REQUEST,
})

#: Kinds that an explicit bounded operator replan request may carry.
OPERATOR_KINDS = frozenset({TK_OPERATOR_REQUEST})

# --- declarative change operations (closed set) ------------------------------

OP_ADD_NODE = "ADD_NODE"
OP_REMOVE_NODE = "REMOVE_NODE"
OP_SUPERSEDE_NODE = "SUPERSEDE_NODE"
OP_ADD_DEPENDENCY = "ADD_DEPENDENCY"
OP_REMOVE_DEPENDENCY = "REMOVE_DEPENDENCY"
OP_INVALIDATE_REUSE = "INVALIDATE_REUSE"

CHANGE_OPS = frozenset({
    OP_ADD_NODE, OP_REMOVE_NODE, OP_SUPERSEDE_NODE, OP_ADD_DEPENDENCY,
    OP_REMOVE_DEPENDENCY, OP_INVALIDATE_REUSE,
})

# --- decision statuses (closed set) ------------------------------------------

DS_ACCEPTED = "ACCEPTED"
DS_REJECTED = "REJECTED"
DECISION_STATUSES = frozenset({DS_ACCEPTED, DS_REJECTED})

# --- event kinds (closed set) ------------------------------------------------

EV_GENERATION_INITIALIZED = "generation_initialized"
EV_GENERATION_ACTIVATED = "generation_activated"
EV_PLAN_REJECTED = "plan_rejected"
EV_SCHEDULER_ADOPTED = "scheduler_adopted"

EVENT_KINDS = frozenset({
    EV_GENERATION_INITIALIZED, EV_GENERATION_ACTIVATED, EV_PLAN_REJECTED,
    EV_SCHEDULER_ADOPTED,
})

# --- stable, machine-readable reason codes ------------------------------------

RC_GENERATION_INITIALIZED = "GENERATION_INITIALIZED"
RC_GENERATION_ACTIVATED = "GENERATION_ACTIVATED"
RC_TRIGGER_STALE = "REPLAN_TRIGGER_STALE"
RC_TRIGGER_ALREADY_APPLIED = "REPLAN_TRIGGER_ALREADY_APPLIED"
RC_TRIGGER_EVIDENCE_REQUIRED = "REPLAN_TRIGGER_EVIDENCE_REQUIRED"
RC_TRIGGER_UNKNOWN = "REPLAN_TRIGGER_UNKNOWN"
RC_POLICY_DENIED = "REPLAN_POLICY_DENIED"
RC_GENERATION_LIMIT = "REPLAN_GENERATION_LIMIT"
RC_CHANGE_LIMIT = "REPLAN_CHANGE_LIMIT"
RC_CYCLE = "REPLAN_CYCLE_REJECTED"
RC_INVALID_DEPENDENCY = "REPLAN_INVALID_DEPENDENCY"
RC_INVALID_RESOURCE = "REPLAN_INVALID_RESOURCE"
RC_INVALID_REUSE = "REPLAN_INVALID_REUSE"
RC_VALIDATION_FAILED = "REPLAN_VALIDATION_FAILED"
RC_NO_OP = "REPLAN_NO_OP"
RC_APPLIED = "REPLAN_APPLIED"
RC_SCHEDULER_ADOPTED = "REPLAN_SCHEDULER_ADOPTED"
RC_NOT_FOUND = "REPLAN_NOT_FOUND"

#: Every known replan reason code (closed set, fail closed).
REASON_CODES = frozenset({
    RC_GENERATION_INITIALIZED, RC_GENERATION_ACTIVATED, RC_TRIGGER_STALE,
    RC_TRIGGER_ALREADY_APPLIED, RC_TRIGGER_EVIDENCE_REQUIRED,
    RC_TRIGGER_UNKNOWN, RC_POLICY_DENIED, RC_GENERATION_LIMIT,
    RC_CHANGE_LIMIT, RC_CYCLE, RC_INVALID_DEPENDENCY, RC_INVALID_RESOURCE,
    RC_INVALID_REUSE, RC_VALIDATION_FAILED, RC_NO_OP, RC_APPLIED,
    RC_SCHEDULER_ADOPTED, RC_NOT_FOUND,
})

#: Reasons that mark a structurally rejected (never activated) plan.
REJECTION_REASONS = frozenset({
    RC_TRIGGER_STALE, RC_TRIGGER_ALREADY_APPLIED,
    RC_TRIGGER_EVIDENCE_REQUIRED, RC_TRIGGER_UNKNOWN, RC_POLICY_DENIED,
    RC_GENERATION_LIMIT, RC_CHANGE_LIMIT, RC_CYCLE, RC_INVALID_DEPENDENCY,
    RC_INVALID_RESOURCE, RC_INVALID_REUSE, RC_VALIDATION_FAILED, RC_NO_OP,
})


class ReplanValidationError(Exception):
    """Replan state violates the canonical bounded schema (fail closed)."""

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(f"{code}: {path}" + (f" ({detail})" if detail else ""))
        self.code = code
        self.path = path
        self.detail = detail


# --- strict primitive helpers -------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fail(code: str, path: str, detail: str = "") -> NoReturn:
    raise ReplanValidationError(code, path, detail)


def _known_keys(doc: Mapping[str, Any], allowed: Sequence[str],
                path: str) -> None:
    unknown = set(doc) - set(allowed)
    if unknown:
        _fail(E_MALFORMED, path, f"unknown field(s): {sorted(unknown)}")


def _require_keys(doc: Mapping[str, Any], required: Sequence[str],
                  path: str) -> None:
    missing = [key for key in required if key not in doc]
    if missing:
        _fail(E_MALFORMED, path, f"missing field(s): {missing}")


def _require_mapping(value: object, path: str, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(E_MALFORMED, path, detail)
    return value


def _require_list(value: object, path: str, detail: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(E_MALFORMED, path, detail)
    return value


def _require_str(value: object, path: str, detail: str, *,
                 maximum: int) -> str:
    if not isinstance(value, str) or not value:
        _fail(E_MALFORMED, path, detail)
    if len(value) > maximum:
        _fail(E_MALFORMED, path, f"{detail} exceeds {maximum} chars")
    return value


def _optional_str(value: object, path: str, detail: str, *,
                  maximum: int) -> str | None:
    if value is None:
        return None
    return _require_str(value, path, detail, maximum=maximum)


def _require_bool(value: object, path: str, detail: str) -> bool:
    if not isinstance(value, bool):
        _fail(E_MALFORMED, path, detail)
    return value


def _require_int(value: object, path: str, detail: str, *,
                 minimum: int, maximum: int) -> int:
    if not _is_int(value):
        _fail(E_MALFORMED, path, detail)
    assert isinstance(value, int)
    if value < minimum or value > maximum:
        _fail(E_MALFORMED, path, f"{detail} out of bounds: {value}")
    return value


def _require_digest(value: object, path: str, detail: str) -> str:
    if not replan_identity.is_valid_digest(value):
        _fail(E_MALFORMED, path, detail)
    assert isinstance(value, str)
    return value


def _require_optional_digest(value: object, path: str,
                             detail: str) -> str | None:
    if value is None:
        return None
    return _require_digest(value, path, detail)


def _require_trigger_kind(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in TRIGGER_KINDS:
        _fail(E_MALFORMED, path, f"unknown trigger kind: {value!r}")
    return value


def _require_change_op(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in CHANGE_OPS:
        _fail(E_MALFORMED, path, f"unknown change op: {value!r}")
    return value


def _require_reason(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in REASON_CODES:
        _fail(E_MALFORMED, path, f"unknown reason code: {value!r}")
    return value


def _require_event_kind(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in EVENT_KINDS:
        _fail(E_MALFORMED, path, f"unknown event kind: {value!r}")
    return value


def _require_status(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in DECISION_STATUSES:
        _fail(E_MALFORMED, path, f"unknown status: {value!r}")
    return value


# --- trigger evidence provenance ----------------------------------------------


@dataclass(frozen=True)
class EvidenceProvenance:
    """Explicit, bounded provenance for one replan trigger (never inferred)."""

    mission_id: str | None = None
    phase_id: str | None = None
    subrun_id: str | None = None
    attestation: str | None = None
    artifact_content_sha256: str | None = None
    blocker_code: str | None = None
    note: str | None = None

    @property
    def present(self) -> bool:
        """True when at least one concrete evidence field is supplied."""
        return any((
            self.mission_id, self.phase_id, self.subrun_id, self.attestation,
            self.artifact_content_sha256, self.blocker_code, self.note,
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "phase_id": self.phase_id,
            "subrun_id": self.subrun_id,
            "attestation": self.attestation,
            "artifact_content_sha256": self.artifact_content_sha256,
            "blocker_code": self.blocker_code,
            "note": self.note,
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any] | None,
                  path: str) -> EvidenceProvenance:
        if doc is None:
            return EvidenceProvenance()
        _known_keys(doc, tuple(EvidenceProvenance.__dataclass_fields__), path)
        sha = doc.get("artifact_content_sha256")
        if sha is not None:
            sha = _require_digest(sha, path, "artifact_content_sha256")
        return EvidenceProvenance(
            mission_id=_optional_str(doc.get("mission_id"), path, "mission_id",
                                     maximum=128),
            phase_id=_optional_str(doc.get("phase_id"), path, "phase_id",
                                   maximum=64),
            subrun_id=_optional_str(doc.get("subrun_id"), path, "subrun_id",
                                    maximum=128),
            attestation=_optional_str(doc.get("attestation"), path,
                                      "attestation", maximum=64),
            artifact_content_sha256=sha,
            blocker_code=_optional_str(doc.get("blocker_code"), path,
                                       "blocker_code", maximum=64),
            note=_optional_str(doc.get("note"), path, "note",
                               maximum=MAX_PROVENANCE_NOTE_LEN),
        )


# --- trigger ------------------------------------------------------------------


@dataclass(frozen=True)
class ReplanTrigger:
    """One explicit, machine-readable replan trigger.

    ``generation_id``/``graph_id`` pin the exact generation the trigger was
    observed against, so a trigger produced for a superseded generation is
    deterministically rejected as stale instead of being replayed.
    """

    kind: str
    source: str
    generation_id: str
    graph_id: str
    provenance: EvidenceProvenance = EvidenceProvenance()
    detail: str = ""
    trigger_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind,
            "source": self.source,
            "generation_id": self.generation_id,
            "graph_id": self.graph_id,
            "provenance": self.provenance.to_dict(),
            "detail": self.detail,
        }

    def compute_trigger_id(self) -> str:
        return replan_identity.trigger_id(self.identity_payload())

    def has_evidence(self) -> bool:
        """Kind-specific evidence requirement (explicit, never inferred)."""
        if self.kind == TK_INVALIDATED_ASSUMPTION:
            return bool(self.detail) or self.provenance.present
        if self.kind == TK_OPERATOR_REQUEST:
            return bool(self.detail) or self.provenance.present
        if self.kind == TK_MISSION_FAILURE:
            return self.provenance.mission_id is not None
        if self.kind == TK_BLOCKER:
            return (self.provenance.mission_id is not None
                    or self.provenance.blocker_code is not None
                    or bool(self.detail))
        if self.kind == TK_NEW_PROVEN_EVIDENCE:
            return (self.provenance.artifact_content_sha256 is not None
                    or self.provenance.mission_id is not None)
        return False

    @staticmethod
    def build(
        *,
        kind: str,
        source: str,
        generation_id: str,
        graph_id: str,
        provenance: EvidenceProvenance | None = None,
        detail: str = "",
    ) -> ReplanTrigger:
        if kind not in TRIGGER_KINDS:
            _fail(E_MALFORMED, "trigger", f"unknown kind: {kind!r}")
        if not source:
            _fail(E_MALFORMED, "trigger", "source is required")
        if len(source) > MAX_SOURCE_LEN:
            _fail(E_MALFORMED, "trigger", "source too long")
        if len(detail) > MAX_TRIGGER_DETAIL_LEN:
            _fail(E_MALFORMED, "trigger", "detail too long")
        if not replan_identity.is_valid_digest(generation_id):
            _fail(E_MALFORMED, "trigger", "generation_id")
        if not replan_identity.is_valid_digest(graph_id):
            _fail(E_MALFORMED, "trigger", "graph_id")
        trigger = ReplanTrigger(
            kind=kind, source=source, generation_id=generation_id,
            graph_id=graph_id, provenance=provenance or EvidenceProvenance(),
            detail=detail,
        )
        return replace(trigger,
                       trigger_id=trigger.compute_trigger_id())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "trigger_id": self.trigger_id}

    @staticmethod
    def from_dict(doc: object, path: str = "trigger") -> ReplanTrigger:
        mapping = _require_mapping(doc, path, "trigger object required")
        _known_keys(
            mapping,
            ("schema_version", "kind", "source", "generation_id",
             "graph_id", "provenance", "detail", "trigger_id"),
            path)
        _require_keys(
            mapping,
            ("kind", "source", "generation_id", "graph_id", "trigger_id"),
            path)
        provenance = EvidenceProvenance.from_dict(
            mapping.get("provenance"), f"{path}[provenance]")
        raw_detail = mapping.get("detail")
        detail = ("" if raw_detail in (None, "") else _require_str(
            raw_detail, path, "detail", maximum=MAX_TRIGGER_DETAIL_LEN))
        trigger = ReplanTrigger(
            kind=_require_trigger_kind(mapping["kind"], path),
            source=_require_str(mapping["source"], path, "source",
                                maximum=MAX_SOURCE_LEN),
            generation_id=_require_digest(
                mapping["generation_id"], path, "generation_id"),
            graph_id=_require_digest(mapping["graph_id"], path, "graph_id"),
            provenance=provenance,
            detail=detail,
            trigger_id=_require_digest(
                mapping["trigger_id"], path, "trigger_id"),
        )
        if trigger.compute_trigger_id() != trigger.trigger_id:
            _fail(E_IDENTITY_MISMATCH, path, trigger.trigger_id)
        return trigger


# --- deterministic replan policy ----------------------------------------------


@dataclass(frozen=True)
class ReplanPolicy:
    """Explicit bounded policy governing which replans may activate."""

    max_generations: int = MAX_GENERATIONS
    max_changes: int = 64
    allow_node_removal: bool = True
    allow_supersession: bool = True
    allow_reuse_invalidation: bool = True
    require_trigger_evidence: bool = True
    policy_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "max_generations": self.max_generations,
            "max_changes": self.max_changes,
            "allow_node_removal": self.allow_node_removal,
            "allow_supersession": self.allow_supersession,
            "allow_reuse_invalidation": self.allow_reuse_invalidation,
            "require_trigger_evidence": self.require_trigger_evidence,
        }

    def compute_policy_id(self) -> str:
        return replan_identity.policy_id(self.identity_payload())

    @staticmethod
    def build(
        *,
        max_generations: int = MAX_GENERATIONS,
        max_changes: int = 64,
        allow_node_removal: bool = True,
        allow_supersession: bool = True,
        allow_reuse_invalidation: bool = True,
        require_trigger_evidence: bool = True,
    ) -> ReplanPolicy:
        if not (1 <= max_generations <= MAX_GENERATIONS):
            _fail(E_MALFORMED, "policy", "max_generations out of bounds")
        if not (1 <= max_changes <= MAX_CHANGES):
            _fail(E_MALFORMED, "policy", "max_changes out of bounds")
        policy = ReplanPolicy(
            max_generations=max_generations,
            max_changes=max_changes,
            allow_node_removal=allow_node_removal,
            allow_supersession=allow_supersession,
            allow_reuse_invalidation=allow_reuse_invalidation,
            require_trigger_evidence=require_trigger_evidence,
        )
        return replace(policy, policy_id=policy.compute_policy_id())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "policy_id": self.policy_id}

    @staticmethod
    def from_dict(doc: object, path: str = "policy") -> ReplanPolicy:
        mapping = _require_mapping(doc, path, "policy object required")
        _known_keys(
            mapping,
            ("schema_version", "max_generations", "max_changes",
             "allow_node_removal", "allow_supersession",
             "allow_reuse_invalidation", "require_trigger_evidence",
             "policy_id"),
            path)
        _require_keys(
            mapping,
            ("max_generations", "max_changes", "allow_node_removal",
             "allow_supersession", "allow_reuse_invalidation",
             "require_trigger_evidence", "policy_id"),
            path)
        policy = ReplanPolicy(
            max_generations=_require_int(
                mapping["max_generations"], path, "max_generations",
                minimum=1, maximum=MAX_GENERATIONS),
            max_changes=_require_int(
                mapping["max_changes"], path, "max_changes", minimum=1,
                maximum=MAX_CHANGES),
            allow_node_removal=_require_bool(
                mapping["allow_node_removal"], path, "allow_node_removal"),
            allow_supersession=_require_bool(
                mapping["allow_supersession"], path, "allow_supersession"),
            allow_reuse_invalidation=_require_bool(
                mapping["allow_reuse_invalidation"], path,
                "allow_reuse_invalidation"),
            require_trigger_evidence=_require_bool(
                mapping["require_trigger_evidence"], path,
                "require_trigger_evidence"),
            policy_id=_require_digest(mapping["policy_id"], path, "policy_id"),
        )
        if policy.compute_policy_id() != policy.policy_id:
            _fail(E_IDENTITY_MISMATCH, path, policy.policy_id)
        return policy


#: Bounded, documented default replan policy.
DEFAULT_POLICY = ReplanPolicy.build()


# --- declarative change -------------------------------------------------------


@dataclass(frozen=True)
class ReplanChange:
    """One explicit, declarative, bounded replan operation."""

    op: str
    reason: str
    node_id: str | None = None
    dependency_from: str | None = None
    dependency_to: str | None = None
    node_spec: Mapping[str, Any] | None = None
    consumer_node_id: str | None = None
    input_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "reason": self.reason,
            "node_id": self.node_id,
            "dependency_from": self.dependency_from,
            "dependency_to": self.dependency_to,
            "node_spec": (None if self.node_spec is None
                          else dict(self.node_spec)),
            "consumer_node_id": self.consumer_node_id,
            "input_id": self.input_id,
        }

    @staticmethod
    def from_dict(doc: object, path: str = "change") -> ReplanChange:
        mapping = _require_mapping(doc, path, "change object required")
        _known_keys(mapping, tuple(ReplanChange.__dataclass_fields__), path)
        _require_keys(mapping, ("op", "reason"), path)
        op = _require_change_op(mapping["op"], path)
        reason = _require_str(mapping["reason"], path, "reason",
                              maximum=MAX_REASON_LEN)
        node_id = _optional_str(mapping.get("node_id"), path, "node_id",
                                maximum=64)
        dep_from = _optional_str(mapping.get("dependency_from"), path,
                                 "dependency_from", maximum=64)
        dep_to = _optional_str(mapping.get("dependency_to"), path,
                               "dependency_to", maximum=64)
        consumer = _optional_str(mapping.get("consumer_node_id"), path,
                                 "consumer_node_id", maximum=64)
        input_id = _optional_str(mapping.get("input_id"), path, "input_id",
                                 maximum=64)
        raw_spec = mapping.get("node_spec")
        node_spec: dict[str, Any] | None = None
        if raw_spec is not None:
            node_spec = dict(_require_mapping(raw_spec, path, "node_spec"))
        change = ReplanChange(
            op=op, reason=reason, node_id=node_id, dependency_from=dep_from,
            dependency_to=dep_to, node_spec=node_spec,
            consumer_node_id=consumer, input_id=input_id,
        )
        _validate_change_shape(change, path)
        return change

    @staticmethod
    def add_node(*, node_spec: Mapping[str, Any], reason: str) -> ReplanChange:
        mapping = dict(_require_mapping(node_spec, "change", "node_spec"))
        node_id = mapping.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            _fail(E_MALFORMED, "change", "node_spec.node_id required")
        change = ReplanChange(op=OP_ADD_NODE, reason=reason, node_id=node_id,
                              node_spec=mapping)
        _validate_change_shape(change, "change")
        return change

    @staticmethod
    def remove_node(*, node_id: str, reason: str) -> ReplanChange:
        change = ReplanChange(op=OP_REMOVE_NODE, reason=reason,
                              node_id=node_id)
        _validate_change_shape(change, "change")
        return change

    @staticmethod
    def supersede_node(*, old_node_id: str,
                       node_spec: Mapping[str, Any],
                       reason: str) -> ReplanChange:
        mapping = dict(_require_mapping(node_spec, "change", "node_spec"))
        new_id = mapping.get("node_id")
        if not isinstance(new_id, str) or not new_id:
            _fail(E_MALFORMED, "change", "node_spec.node_id required")
        if new_id == old_node_id:
            _fail(E_MALFORMED, "change", "supersede requires a new node id")
        change = ReplanChange(op=OP_SUPERSEDE_NODE, reason=reason,
                              node_id=old_node_id, node_spec=mapping)
        _validate_change_shape(change, "change")
        return change

    @staticmethod
    def add_dependency(*, dependency_from: str, dependency_to: str,
                       reason: str) -> ReplanChange:
        change = ReplanChange(
            op=OP_ADD_DEPENDENCY, reason=reason,
            dependency_from=dependency_from, dependency_to=dependency_to)
        _validate_change_shape(change, "change")
        return change

    @staticmethod
    def remove_dependency(*, dependency_from: str, dependency_to: str,
                          reason: str) -> ReplanChange:
        change = ReplanChange(
            op=OP_REMOVE_DEPENDENCY, reason=reason,
            dependency_from=dependency_from, dependency_to=dependency_to)
        _validate_change_shape(change, "change")
        return change

    @staticmethod
    def invalidate_reuse(*, consumer_node_id: str, input_id: str,
                         reason: str) -> ReplanChange:
        change = ReplanChange(
            op=OP_INVALIDATE_REUSE, reason=reason,
            consumer_node_id=consumer_node_id, input_id=input_id)
        _validate_change_shape(change, "change")
        return change


def _validate_change_shape(change: ReplanChange, path: str) -> None:
    if change.op == OP_ADD_NODE:
        if change.node_id is None or change.node_spec is None:
            _fail(E_MALFORMED, path, "ADD_NODE requires node_id + node_spec")
        spec_id = change.node_spec.get("node_id")
        if spec_id != change.node_id:
            _fail(E_MALFORMED, path, "ADD_NODE node_spec id mismatch")
    elif change.op == OP_REMOVE_NODE:
        if change.node_id is None:
            _fail(E_MALFORMED, path, "REMOVE_NODE requires node_id")
    elif change.op == OP_SUPERSEDE_NODE:
        if change.node_id is None or change.node_spec is None:
            _fail(E_MALFORMED, path,
                  "SUPERSEDE_NODE requires node_id + node_spec")
        spec_id = change.node_spec.get("node_id")
        if not isinstance(spec_id, str) or spec_id == change.node_id:
            _fail(E_MALFORMED, path, "SUPERSEDE_NODE requires a new node id")
    elif change.op in (OP_ADD_DEPENDENCY, OP_REMOVE_DEPENDENCY):
        if change.dependency_from is None or change.dependency_to is None:
            _fail(E_MALFORMED, path,
                  f"{change.op} requires dependency_from + dependency_to")
    elif change.op == OP_INVALIDATE_REUSE:
        if change.consumer_node_id is None or change.input_id is None:
            _fail(E_MALFORMED, path,
                  "INVALIDATE_REUSE requires consumer_node_id + input_id")


# --- graph generation ---------------------------------------------------------


@dataclass(frozen=True)
class Generation:
    """One immutable, exact graph generation."""

    generation_number: int
    goal_id: str
    graph_id: str
    spec_sha256: str
    parent_generation_id: str | None
    trigger_id: str | None
    plan_id: str | None
    generation_id: str = ""
    activated_at: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "replan_version": REPLAN_VERSION,
            "generation_number": self.generation_number,
            "goal_id": self.goal_id,
            "graph_id": self.graph_id,
            "spec_sha256": self.spec_sha256,
            "parent_generation_id": self.parent_generation_id,
            "trigger_id": self.trigger_id,
            "plan_id": self.plan_id,
        }

    def compute_generation_id(self) -> str:
        return replan_identity.generation_id(self.identity_payload())

    @staticmethod
    def base(*, graph: graph_model.GoalGraph,
             generation_number: int = 1) -> Generation:
        generation = Generation(
            generation_number=generation_number,
            goal_id=graph.goal_id,
            graph_id=graph.graph_id,
            spec_sha256=graph.spec_sha256,
            parent_generation_id=None,
            trigger_id=None,
            plan_id=None,
        )
        return replace(generation,
                       generation_id=generation.compute_generation_id())

    @staticmethod
    def activated(
        *,
        parent: Generation,
        graph: graph_model.GoalGraph,
        trigger_id: str,
        plan_id: str,
    ) -> Generation:
        generation = Generation(
            generation_number=parent.generation_number + 1,
            goal_id=graph.goal_id,
            graph_id=graph.graph_id,
            spec_sha256=graph.spec_sha256,
            parent_generation_id=parent.generation_id,
            trigger_id=trigger_id,
            plan_id=plan_id,
        )
        return replace(generation,
                       generation_id=generation.compute_generation_id())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "generation_id": self.generation_id,
            "activated_at": self.activated_at,
        }

    @staticmethod
    def from_dict(doc: object, path: str = "generation") -> Generation:
        mapping = _require_mapping(doc, path, "generation object required")
        _known_keys(
            mapping,
            ("schema_version", "replan_version", "generation_number",
             "goal_id", "graph_id", "spec_sha256",
             "parent_generation_id", "trigger_id", "plan_id",
             "generation_id", "activated_at"),
            path)
        _require_keys(
            mapping,
            ("generation_number", "goal_id", "graph_id", "spec_sha256",
             "parent_generation_id", "trigger_id", "plan_id",
             "generation_id", "activated_at"),
            path)
        version = mapping.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        generation = Generation(
            generation_number=_require_int(
                mapping["generation_number"], path, "generation_number",
                minimum=1, maximum=MAX_GENERATIONS),
            goal_id=_require_str(mapping["goal_id"], path, "goal_id",
                                 maximum=64),
            graph_id=_require_digest(mapping["graph_id"], path, "graph_id"),
            spec_sha256=_require_digest(mapping["spec_sha256"], path,
                                        "spec_sha256"),
            parent_generation_id=_require_optional_digest(
                mapping["parent_generation_id"], path, "parent_generation_id"),
            trigger_id=_require_optional_digest(
                mapping["trigger_id"], path, "trigger_id"),
            plan_id=_require_optional_digest(
                mapping["plan_id"], path, "plan_id"),
            generation_id=_require_digest(
                mapping["generation_id"], path, "generation_id"),
            activated_at=_require_str(
                mapping["activated_at"], path, "activated_at",
                maximum=MAX_TIMESTAMP_LEN),
        )
        if generation.compute_generation_id() != generation.generation_id:
            _fail(E_IDENTITY_MISMATCH, path, generation.generation_id)
        return generation


# --- replan event -------------------------------------------------------------


@dataclass(frozen=True)
class ReplanEvent:
    """One append-only replan event (accepted activation or rejected plan)."""

    event: str
    status: str
    reason: str
    goal_id: str
    generation_id: str | None
    parent_generation_id: str | None
    plan_id: str | None
    trigger_id: str | None
    changes: tuple[Mapping[str, Any], ...]
    created_at: str
    event_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "replan_version": REPLAN_VERSION,
            "event": self.event,
            "status": self.status,
            "reason": self.reason,
            "goal_id": self.goal_id,
            "generation_id": self.generation_id,
            "parent_generation_id": self.parent_generation_id,
            "plan_id": self.plan_id,
            "trigger_id": self.trigger_id,
            "changes": [dict(change) for change in self.changes],
        }

    def compute_event_id(self) -> str:
        return replan_identity.event_id(self.identity_payload())

    @staticmethod
    def build(
        *,
        event: str,
        status: str,
        reason: str,
        goal_id: str,
        generation_id: str | None,
        parent_generation_id: str | None,
        plan_id: str | None,
        trigger_id: str | None,
        changes: Sequence[Mapping[str, Any]],
        created_at: str,
    ) -> ReplanEvent:
        record = ReplanEvent(
            event=event, status=status, reason=reason, goal_id=goal_id,
            generation_id=generation_id,
            parent_generation_id=parent_generation_id, plan_id=plan_id,
            trigger_id=trigger_id,
            changes=tuple(dict(change) for change in changes),
            created_at=created_at,
        )
        return replace(record, event_id=record.compute_event_id())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "event_id": self.event_id,
                "created_at": self.created_at}

    @staticmethod
    def from_dict(doc: object, path: str = "event") -> ReplanEvent:
        mapping = _require_mapping(doc, path, "replan event required")
        _known_keys(
            mapping,
            ("schema_version", "replan_version", "event", "status",
             "reason", "goal_id", "generation_id", "parent_generation_id",
             "plan_id", "trigger_id", "changes", "created_at",
             "event_id"),
            path)
        _require_keys(
            mapping,
            ("event", "status", "reason", "goal_id", "generation_id",
             "parent_generation_id", "plan_id", "trigger_id", "changes",
             "created_at", "event_id"),
            path)
        version = mapping.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        changes_raw = _require_list(mapping["changes"], path, "changes")
        changes = tuple(
            dict(_require_mapping(change, f"{path}[changes/{index}]",
                                  "change object required"))
            for index, change in enumerate(changes_raw)
        )
        record = ReplanEvent(
            event=_require_event_kind(mapping["event"], path),
            status=_require_status(mapping["status"], path),
            reason=_require_reason(mapping["reason"], path),
            goal_id=_require_str(mapping["goal_id"], path, "goal_id",
                                 maximum=64),
            generation_id=_require_optional_digest(
                mapping["generation_id"], path, "generation_id"),
            parent_generation_id=_require_optional_digest(
                mapping["parent_generation_id"], path, "parent_generation_id"),
            plan_id=_require_optional_digest(
                mapping["plan_id"], path, "plan_id"),
            trigger_id=_require_optional_digest(
                mapping["trigger_id"], path, "trigger_id"),
            changes=changes,
            created_at=_require_str(mapping["created_at"], path, "created_at",
                                    maximum=MAX_TIMESTAMP_LEN),
            event_id=_require_digest(mapping["event_id"], path, "event_id"),
        )
        if record.compute_event_id() != record.event_id:
            _fail(E_IDENTITY_MISMATCH, path, record.event_id)
        return record


# --- plan ---------------------------------------------------------------------


@dataclass(frozen=True)
class ReplanPlan:
    """One validated, deterministic replan plan (not yet activated)."""

    goal_id: str
    parent_generation_id: str
    parent_graph_id: str
    trigger: ReplanTrigger
    policy: ReplanPolicy
    changes: tuple[ReplanChange, ...]
    resulting_nodes: tuple[graph_model.GraphNode, ...]
    resulting_edges: tuple[graph_model.GraphEdge, ...]
    new_graph_id: str
    new_spec_sha256: str
    plan_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "replan_version": REPLAN_VERSION,
            "goal_id": self.goal_id,
            "parent_generation_id": self.parent_generation_id,
            "parent_graph_id": self.parent_graph_id,
            "trigger": self.trigger.to_dict(),
            "policy": self.policy.to_dict(),
            "changes": [change.to_dict() for change in self.changes],
            "new_graph_id": self.new_graph_id,
            "new_spec_sha256": self.new_spec_sha256,
        }

    def compute_plan_id(self) -> str:
        return replan_identity.plan_id(self.identity_payload())

    def change_summary(self) -> tuple[dict[str, Any], ...]:
        return tuple(change.to_dict() for change in self.changes)

    @staticmethod
    def build(
        *,
        goal_id: str,
        parent_generation_id: str,
        parent_graph_id: str,
        trigger: ReplanTrigger,
        policy: ReplanPolicy,
        changes: Sequence[ReplanChange],
        resulting_nodes: Sequence[graph_model.GraphNode],
        resulting_edges: Sequence[graph_model.GraphEdge],
        new_graph_id: str,
        new_spec_sha256: str,
    ) -> ReplanPlan:
        plan = ReplanPlan(
            goal_id=goal_id,
            parent_generation_id=parent_generation_id,
            parent_graph_id=parent_graph_id,
            trigger=trigger,
            policy=policy,
            changes=tuple(changes),
            resulting_nodes=tuple(sorted(resulting_nodes,
                                         key=lambda n: n.node_id)),
            resulting_edges=tuple(sorted(resulting_edges,
                                         key=lambda e: (e.source, e.target))),
            new_graph_id=new_graph_id,
            new_spec_sha256=new_spec_sha256,
        )
        return replace(plan, plan_id=plan.compute_plan_id())


# --- decision -----------------------------------------------------------------


@dataclass(frozen=True)
class ReplanDecision:
    """One deterministic trigger evaluation outcome (accepted or rejected)."""

    status: str
    reason: str
    goal_id: str
    trigger: ReplanTrigger
    current_generation_id: str
    plan: ReplanPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "goal_id": self.goal_id,
            "trigger": self.trigger.to_dict(),
            "current_generation_id": self.current_generation_id,
            "plan": None if self.plan is None else {
                "plan_id": self.plan.plan_id,
                "new_graph_id": self.plan.new_graph_id,
                "change_summary": self.plan.change_summary(),
            },
        }
