"""Mission 016 — bounded goal-level proof domain model (pure, fail closed).

This module owns the deterministic, bounded representation of one *derived*
goal-level proof projection: it answers whether the original strategic goal
is actually proven satisfied from authoritative M008-M015 mission-level
evidence. It performs no I/O, no clock reads and no store access:
normalization, validation and identity are pure functions of their inputs.

Canonical invariants (ADR-015):

* the goal proof is **derived evidence** — it is never a second source of
  truth and never mutates graph, mission, scheduler, reuse or replan state;
* ``COMPLETE`` is permitted **only** when every configured goal acceptance
  criterion is bound to exact proven evidence and no unresolved risk
  remains; success is never inferred from a process exit code, model prose,
  a timestamp, scheduler admission, resource availability, reuse records or
  replanning alone;
* every acceptance-criterion binding names the exact contributing mission,
  phase and sub-run (plus attestation trust) it rests on;
* unresolved, blocked, stale, legacy, unproven, contradictory and invalid
  states are explicit and carry stable machine-readable reason codes;
* proof identity is a domain-separated digest over normalized content and
  never includes a timestamp — a timestamp is evidence only, never trust or
  ordering semantics;
* malformed, unsupported-version, identity-mismatched, contradictory or
  impossible state fails closed with an explicit error, never a guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NoReturn

from trajectory_os.graph.proof import identity as proof_identity

#: Schema version of the durable goal-proof projection (strict, fail closed).
SCHEMA_VERSION = 1

#: Human/machine proof version string (additive, never an input digest).
PROOF_VERSION = "m016.1"

# --- hard bounded limits (no configuration may exceed these) -----------------

MAX_NODES = 64
MAX_CRITERIA = 512
MAX_RISKS = 1024
MAX_EVIDENCE_REFS = 64
MAX_DETAIL_LEN = 512
MAX_TIMESTAMP_LEN = 64
MAX_ID_LEN = 64
MAX_STATEMENT_LEN = 1024

# --- final goal states (closed set) ------------------------------------------

GS_COMPLETE = "COMPLETE"
GS_INCOMPLETE = "INCOMPLETE"

GOAL_STATES = frozenset({GS_COMPLETE, GS_INCOMPLETE})

# --- acceptance-criterion binding statuses (closed set) ----------------------

CS_PROVEN = "PROVEN"
CS_UNPROVEN = "UNPROVEN"
CS_CONTRADICTORY = "CONTRADICTORY"
CS_INVALID = "INVALID"

CRITERION_STATUSES = frozenset({CS_PROVEN, CS_UNPROVEN, CS_CONTRADICTORY,
                                CS_INVALID})

# --- evidence trust levels (closed set) --------------------------------------

TRUST_PROVEN = "PROVEN"
TRUST_LEGACY = "LEGACY"
TRUST_NONE = "NONE"

TRUST_LEVELS = frozenset({TRUST_PROVEN, TRUST_LEGACY, TRUST_NONE})

# --- risk classes (closed set) -----------------------------------------------

RISK_UNRESOLVED = "UNRESOLVED"
RISK_BLOCKED = "BLOCKED"
RISK_STALE = "STALE"
RISK_LEGACY = "LEGACY"
RISK_UNPROVEN = "UNPROVEN"
RISK_CONTRADICTORY = "CONTRADICTORY"
RISK_INVALID = "INVALID"
RISK_IMPOSSIBLE = "IMPOSSIBLE"

RISK_CLASSES = frozenset({
    RISK_UNRESOLVED, RISK_BLOCKED, RISK_STALE, RISK_LEGACY, RISK_UNPROVEN,
    RISK_CONTRADICTORY, RISK_INVALID, RISK_IMPOSSIBLE,
})

#: Deterministic risk precedence (higher first) used for reason selection.
RISK_PRIORITY = {
    RISK_IMPOSSIBLE: 0,
    RISK_CONTRADICTORY: 1,
    RISK_INVALID: 2,
    RISK_STALE: 3,
    RISK_LEGACY: 4,
    RISK_UNRESOLVED: 5,
    RISK_BLOCKED: 6,
    RISK_UNPROVEN: 7,
}

# --- stable, machine-readable reason codes -----------------------------------

R_ALL_CRITERIA_PROVEN = "ALL_CRITERIA_PROVEN"
R_NO_ACCEPTANCE_CRITERIA = "NO_ACCEPTANCE_CRITERIA"
R_CRITERION_UNPROVEN = "CRITERION_UNPROVEN"
R_CRITERION_CONTRADICTORY = "CRITERION_CONTRADICTORY"
R_CRITERION_INVALID = "CRITERION_INVALID"
R_CRITERION_AMBIGUOUS = "CRITERION_AMBIGUOUS"
R_CRITERION_UNBOUND = "CRITERION_UNBOUND"
R_MISSION_UNRESOLVED = "MISSION_UNRESOLVED"
R_MISSION_INVALID = "MISSION_INVALID"
R_MISSION_FAILED = "MISSION_FAILED"
R_MISSION_NOT_PROVEN = "MISSION_NOT_PROVEN"
R_MISSION_LEGACY_EVIDENCE = "MISSION_LEGACY_EVIDENCE"
R_MISSION_EVIDENCE_MISSING = "MISSION_EVIDENCE_MISSING"
R_NODE_BLOCKED = "NODE_BLOCKED"
R_NODE_INVALID = "NODE_INVALID"
R_NODE_UNRESOLVED = "NODE_UNRESOLVED"
R_NODE_INCOMPLETE = "NODE_INCOMPLETE"
R_REUSE_UNRESOLVED = "REUSE_UNRESOLVED"
R_REUSE_REJECTED = "REUSE_REJECTED"
R_REUSE_STALE = "REUSE_STALE"
R_SCHEDULER_STALE = "SCHEDULER_STALE"
R_SCHEDULER_BLOCKER = "SCHEDULER_BLOCKER"
R_SCHEDULER_DEFERRED = "SCHEDULER_DEFERRED"
R_SCHEDULER_MISSING = "SCHEDULER_MISSING"
R_REPLAN_UNRESOLVED = "REPLAN_UNRESOLVED"
R_GENERATION_MISMATCH = "GENERATION_MISMATCH"
R_RECONSTRUCTION_MISMATCH = "RECONSTRUCTION_MISMATCH"
R_PROOF_EVIDENCE_MISSING = "PROOF_EVIDENCE_MISSING"
R_PROOF_EVIDENCE_STALE = "PROOF_EVIDENCE_STALE"
R_UNSUPPORTED_STATE = "UNSUPPORTED_STATE"
R_IMPOSSIBLE_STATE = "IMPOSSIBLE_STATE"
R_MALFORMED_STATE = "MALFORMED_STATE"

REASON_CODES = frozenset({
    R_ALL_CRITERIA_PROVEN, R_NO_ACCEPTANCE_CRITERIA, R_CRITERION_UNPROVEN,
    R_CRITERION_CONTRADICTORY, R_CRITERION_INVALID, R_CRITERION_AMBIGUOUS,
    R_CRITERION_UNBOUND,
    R_MISSION_UNRESOLVED, R_MISSION_INVALID, R_MISSION_FAILED,
    R_MISSION_NOT_PROVEN, R_MISSION_LEGACY_EVIDENCE,
    R_MISSION_EVIDENCE_MISSING, R_NODE_BLOCKED, R_NODE_INVALID,
    R_NODE_UNRESOLVED, R_NODE_INCOMPLETE, R_REUSE_UNRESOLVED,
    R_REUSE_REJECTED, R_REUSE_STALE, R_SCHEDULER_STALE, R_SCHEDULER_BLOCKER,
    R_SCHEDULER_DEFERRED, R_SCHEDULER_MISSING, R_REPLAN_UNRESOLVED,
    R_GENERATION_MISMATCH, R_RECONSTRUCTION_MISMATCH,
    R_PROOF_EVIDENCE_MISSING, R_PROOF_EVIDENCE_STALE, R_UNSUPPORTED_STATE,
    R_IMPOSSIBLE_STATE, R_MALFORMED_STATE,
})

# --- fail-closed error codes -------------------------------------------------

E_MALFORMED = "MALFORMED_GOAL_PROOF"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_GOAL_PROOF_SCHEMA"
E_IDENTITY_MISMATCH = "GOAL_PROOF_IDENTITY_MISMATCH"
E_GRAPH_MISMATCH = "GOAL_PROOF_GRAPH_MISMATCH"
E_GENERATION_MISMATCH = "GOAL_PROOF_GENERATION_MISMATCH"
E_RECONSTRUCTION_MISMATCH = "GOAL_PROOF_RECONSTRUCTION_MISMATCH"
E_CONTRADICTORY = "CONTRADICTORY_GOAL_PROOF"
E_IMPOSSIBLE = "IMPOSSIBLE_GOAL_PROOF_STATE"
E_MISSING_EVIDENCE = "GOAL_PROOF_EVIDENCE_MISSING"
E_OVERFLOW = "GOAL_PROOF_OVERFLOW"


class GoalProofError(Exception):
    """The goal-proof state violates the canonical bounded schema."""

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(f"{code}: {path}" + (f" ({detail})" if detail else ""))
        self.code = code
        self.path = path
        self.detail = detail


# --- strict primitive helpers -------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fail(code: str, path: str, detail: str = "") -> NoReturn:
    raise GoalProofError(code, path, detail)


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


def _mapping(value: object, path: str, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(E_MALFORMED, path, detail)
    return value


def _list(value: object, path: str, detail: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(E_MALFORMED, path, detail)
    return value


def _str(value: object, path: str, detail: str, *,
         maximum: int = MAX_DETAIL_LEN, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        _fail(E_MALFORMED, path, detail)
    if len(value) > maximum:
        _fail(E_MALFORMED, path, f"{detail} exceeds {maximum} chars")
    return value


def _opt_str(value: object, path: str, detail: str, *,
             maximum: int = MAX_DETAIL_LEN) -> str | None:
    return _str(value, path, detail, maximum=maximum, optional=True)


def _bool(value: object, path: str, detail: str) -> bool:
    if not isinstance(value, bool):
        _fail(E_MALFORMED, path, detail)
    return value


def _int(value: object, path: str, detail: str, *,
         minimum: int = 0, maximum: int | None = None) -> int:
    if not _is_int(value):
        _fail(E_MALFORMED, path, detail)
    assert isinstance(value, int)
    if value < minimum or (maximum is not None and value > maximum):
        _fail(E_MALFORMED, path, f"{detail} out of bounds: {value}")
    return value


def _digest(value: object, path: str, detail: str, *,
            optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not proof_identity.is_valid_digest(value):
        _fail(E_MALFORMED, path, detail)
    assert isinstance(value, str)
    return value


def _enum(value: object, path: str, detail: str,
          allowed: frozenset[str], *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or value not in allowed:
        _fail(E_MALFORMED, path, f"unknown {detail}: {value!r}")
    return value


def _reason(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in REASON_CODES:
        _fail(E_MALFORMED, path, f"unknown reason code: {value!r}")
    return value


# --- exact contributing evidence ---------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """One exact contributing execution/attestation/evidence identity.

    The reference carries only bounded canonical identities resolved
    strictly read-only from the mission store: the referenced mission, the
    contributing phase and its terminal sub-run, the persisted semantic
    classification/attestation outcome, and the trust level those identities
    support. It never carries timestamps, prose or payloads.
    """

    node_id: str
    mission_id: str
    phase_id: str | None
    subrun_id: str | None
    classification: str | None
    semantic_status: str | None
    attestation: str | None
    trust: str
    exact: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "mission_id": self.mission_id,
            "phase_id": self.phase_id,
            "subrun_id": self.subrun_id,
            "classification": self.classification,
            "semantic_status": self.semantic_status,
            "attestation": self.attestation,
            "trust": self.trust,
            "exact": self.exact,
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> EvidenceRef:
        mapping = _mapping(doc, path, "evidence ref object required")
        _known_keys(mapping, tuple(EvidenceRef.__dataclass_fields__), path)
        _require_keys(mapping, tuple(EvidenceRef.__dataclass_fields__), path)
        return EvidenceRef(
            node_id=str(_str(mapping["node_id"], path, "node_id",
                             maximum=MAX_ID_LEN)),
            mission_id=str(_str(mapping["mission_id"], path, "mission_id",
                                maximum=MAX_ID_LEN)),
            phase_id=_opt_str(mapping["phase_id"], path, "phase_id",
                              maximum=MAX_ID_LEN),
            subrun_id=_opt_str(mapping["subrun_id"], path, "subrun_id",
                               maximum=MAX_DETAIL_LEN),
            classification=_opt_str(mapping["classification"], path,
                                    "classification", maximum=MAX_ID_LEN),
            semantic_status=_opt_str(mapping["semantic_status"], path,
                                     "semantic_status", maximum=MAX_ID_LEN),
            attestation=_opt_str(mapping["attestation"], path, "attestation",
                                 maximum=MAX_ID_LEN),
            trust=str(_enum(mapping["trust"], path, "trust", TRUST_LEVELS)),
            exact=_bool(mapping["exact"], path, "exact"),
        )


# --- acceptance-criterion binding --------------------------------------------


@dataclass(frozen=True)
class CriterionBinding:
    """One acceptance criterion bound to its exact proven (or missing) evidence."""

    node_id: str
    criterion_id: str
    statement: str
    verification: str | None
    required: bool
    status: str
    reason: str
    trust: str
    mission_id: str | None
    mission_state: str | None
    mission_reason: str | None
    mission_proven: bool
    phase_id: str | None
    evidence: tuple[EvidenceRef, ...]
    evidence_sha256: str | None
    binding_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "criterion_id": self.criterion_id,
            "statement": self.statement,
            "verification": self.verification,
            "required": self.required,
            "status": self.status,
            "reason": self.reason,
            "trust": self.trust,
            "mission_id": self.mission_id,
            "mission_state": self.mission_state,
            "mission_reason": self.mission_reason,
            "mission_proven": self.mission_proven,
            "phase_id": self.phase_id,
            "evidence": [ref.to_dict() for ref in self.evidence],
            "evidence_sha256": self.evidence_sha256,
        }

    def compute_binding_id(self) -> str:
        return proof_identity.binding_id(self.identity_payload())

    @property
    def proven(self) -> bool:
        return self.status == CS_PROVEN

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "binding_id": self.binding_id}

    @staticmethod
    def build(*, node_id: str, criterion_id: str, statement: str,
              verification: str | None, required: bool, status: str,
              reason: str, trust: str, mission_id: str | None,
              mission_state: str | None, mission_reason: str | None,
              mission_proven: bool, phase_id: str | None,
              evidence: Sequence[EvidenceRef]) -> CriterionBinding:
        if status not in CRITERION_STATUSES:
            _fail(E_MALFORMED, "criterion", f"unknown status {status!r}")
        if len(evidence) > MAX_EVIDENCE_REFS:
            _fail(E_OVERFLOW, "criterion", "too many evidence references")
        ordered = tuple(evidence)
        evidence_payload = [ref.to_dict() for ref in ordered]
        evidence_sha = (proof_identity.binding_id(evidence_payload)
                        if ordered and status == CS_PROVEN else None)
        binding = CriterionBinding(
            node_id=node_id, criterion_id=criterion_id, statement=statement,
            verification=verification, required=required, status=status,
            reason=reason, trust=trust, mission_id=mission_id,
            mission_state=mission_state, mission_reason=mission_reason,
            mission_proven=mission_proven, phase_id=phase_id,
            evidence=ordered, evidence_sha256=evidence_sha)
        return replace(binding, binding_id=binding.compute_binding_id())

    @staticmethod
    def from_dict(doc: object, path: str) -> CriterionBinding:
        mapping = _mapping(doc, path, "criterion binding required")
        keys = tuple(CriterionBinding.__dataclass_fields__)
        _known_keys(mapping, keys, path)
        _require_keys(mapping, (*keys, "binding_id"), path)
        raw_evidence = _list(mapping["evidence"], path, "evidence")
        if len(raw_evidence) > MAX_EVIDENCE_REFS:
            _fail(E_OVERFLOW, path, "too many evidence references")
        evidence = tuple(
            EvidenceRef.from_dict(item, f"{path}[evidence/{index}]")
            for index, item in enumerate(raw_evidence)
        )
        binding = CriterionBinding(
            node_id=str(_str(mapping["node_id"], path, "node_id",
                             maximum=MAX_ID_LEN)),
            criterion_id=str(_str(mapping["criterion_id"], path,
                                  "criterion_id", maximum=MAX_ID_LEN)),
            statement=str(_str(mapping["statement"], path, "statement",
                               maximum=MAX_STATEMENT_LEN)),
            verification=_opt_str(mapping["verification"], path,
                                  "verification",
                                  maximum=MAX_STATEMENT_LEN),
            required=_bool(mapping["required"], path, "required"),
            status=str(_enum(mapping["status"], path, "status",
                             CRITERION_STATUSES)),
            reason=_reason(mapping["reason"], path),
            trust=str(_enum(mapping["trust"], path, "trust", TRUST_LEVELS)),
            mission_id=_opt_str(mapping["mission_id"], path, "mission_id",
                                maximum=MAX_ID_LEN),
            mission_state=_opt_str(mapping["mission_state"], path,
                                   "mission_state", maximum=MAX_ID_LEN),
            mission_reason=_opt_str(mapping["mission_reason"], path,
                                    "mission_reason", maximum=MAX_ID_LEN),
            mission_proven=_bool(mapping["mission_proven"], path,
                                 "mission_proven"),
            phase_id=_opt_str(mapping["phase_id"], path, "phase_id",
                              maximum=MAX_ID_LEN),
            evidence=evidence,
            evidence_sha256=_digest(mapping["evidence_sha256"], path,
                                    "evidence_sha256", optional=True),
            binding_id=str(_digest(mapping["binding_id"], path, "binding_id")),
        )
        if binding.status == CS_PROVEN:
            if binding.evidence_sha256 is None or not binding.evidence:
                _fail(E_CONTRADICTORY, path,
                      "proven criterion requires evidence")
        elif binding.evidence_sha256 is not None:
            _fail(E_CONTRADICTORY, path,
                  f"{binding.status} criterion cannot carry evidence digest")
        if binding.compute_binding_id() != binding.binding_id:
            _fail(E_IDENTITY_MISMATCH, path, binding.binding_id)
        return binding


# --- bounded summary value objects -------------------------------------------


@dataclass(frozen=True)
class NodeProof:
    """One node's deterministic goal-level status (derived, never authoritative)."""

    node_id: str
    title: str
    priority: int
    depends_on: tuple[str, ...]
    state: str
    reason: str
    own_status: str
    eligible: bool
    mission_id: str | None
    mission_state: str | None
    mission_reason: str | None
    mission_proven: bool
    criteria_total: int
    criteria_proven: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "priority": self.priority,
            "depends_on": list(self.depends_on),
            "state": self.state,
            "reason": self.reason,
            "own_status": self.own_status,
            "eligible": self.eligible,
            "mission_id": self.mission_id,
            "mission_state": self.mission_state,
            "mission_reason": self.mission_reason,
            "mission_proven": self.mission_proven,
            "criteria_total": self.criteria_total,
            "criteria_proven": self.criteria_proven,
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> NodeProof:
        mapping = _mapping(doc, path, "node proof required")
        keys = tuple(NodeProof.__dataclass_fields__)
        _known_keys(mapping, keys, path)
        _require_keys(mapping, keys, path)
        raw_deps = _list(mapping["depends_on"], path, "depends_on")
        return NodeProof(
            node_id=str(_str(mapping["node_id"], path, "node_id",
                             maximum=MAX_ID_LEN)),
            title=str(_str(mapping["title"], path, "title",
                           maximum=MAX_STATEMENT_LEN)),
            priority=_int(mapping["priority"], path, "priority", maximum=100),
            depends_on=tuple(
                str(_str(dep, path, "depends_on", maximum=MAX_ID_LEN))
                for dep in raw_deps
            ),
            state=str(_str(mapping["state"], path, "state",
                           maximum=MAX_ID_LEN)),
            reason=str(_str(mapping["reason"], path, "reason",
                            maximum=MAX_ID_LEN)),
            own_status=str(_str(mapping["own_status"], path, "own_status",
                                maximum=MAX_ID_LEN)),
            eligible=_bool(mapping["eligible"], path, "eligible"),
            mission_id=_opt_str(mapping["mission_id"], path, "mission_id",
                                maximum=MAX_ID_LEN),
            mission_state=_opt_str(mapping["mission_state"], path,
                                   "mission_state", maximum=MAX_ID_LEN),
            mission_reason=_opt_str(mapping["mission_reason"], path,
                                    "mission_reason", maximum=MAX_ID_LEN),
            mission_proven=_bool(mapping["mission_proven"], path,
                                 "mission_proven"),
            criteria_total=_int(mapping["criteria_total"], path,
                                "criteria_total", maximum=MAX_CRITERIA),
            criteria_proven=_int(mapping["criteria_proven"], path,
                                 "criteria_proven", maximum=MAX_CRITERIA),
        )


@dataclass(frozen=True)
class Risk:
    """One explicit unresolved/blocked/stale/legacy/unproven/invalid risk."""

    risk_class: str
    reason: str
    subject: str
    detail: str

    def sort_key(self) -> tuple[int, str, str]:
        return (RISK_PRIORITY[self.risk_class], self.reason, self.subject)

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_class": self.risk_class,
            "reason": self.reason,
            "subject": self.subject,
            "detail": self.detail,
        }

    @staticmethod
    def build(*, risk_class: str, reason: str, subject: str,
              detail: str) -> Risk:
        if risk_class not in RISK_CLASSES:
            _fail(E_MALFORMED, "risk", f"unknown class {risk_class!r}")
        if reason not in REASON_CODES:
            _fail(E_MALFORMED, "risk", f"unknown reason {reason!r}")
        if len(detail) > MAX_DETAIL_LEN:
            detail = detail[:MAX_DETAIL_LEN]
        return Risk(risk_class=risk_class, reason=reason, subject=subject,
                    detail=detail)

    @staticmethod
    def from_dict(doc: object, path: str) -> Risk:
        mapping = _mapping(doc, path, "risk required")
        _known_keys(mapping, tuple(Risk.__dataclass_fields__), path)
        _require_keys(mapping, tuple(Risk.__dataclass_fields__), path)
        return Risk(
            risk_class=str(_enum(mapping["risk_class"], path, "risk_class",
                                 RISK_CLASSES)),
            reason=_reason(mapping["reason"], path),
            subject=str(_str(mapping["subject"], path, "subject",
                             maximum=MAX_DETAIL_LEN)),
            detail=str(_str(mapping["detail"], path, "detail",
                            maximum=MAX_DETAIL_LEN)),
        )


@dataclass(frozen=True)
class GoalCounts:
    """Derived goal-level counts (deterministic, bounded)."""

    nodes_total: int
    nodes_complete: int
    nodes_ready: int
    nodes_in_progress: int
    nodes_blocked: int
    nodes_unresolved: int
    nodes_invalid: int
    criteria_total: int
    criteria_proven: int
    criteria_unproven: int
    criteria_contradictory: int
    criteria_invalid: int
    missions_total: int
    missions_proven: int
    missions_incomplete: int
    missions_missing: int
    risks_total: int
    risks_unresolved: int
    risks_blocked: int
    risks_stale: int
    risks_legacy: int
    risks_unproven: int
    risks_contradictory: int
    risks_invalid: int
    risks_impossible: int
    replans: int
    supersessions: int

    def to_dict(self) -> dict[str, int]:
        return {key: getattr(self, key)
                for key in GoalCounts.__dataclass_fields__}

    @staticmethod
    def from_dict(doc: object, path: str) -> GoalCounts:
        mapping = _mapping(doc, path, "counts required")
        _known_keys(mapping, tuple(GoalCounts.__dataclass_fields__), path)
        _require_keys(mapping, tuple(GoalCounts.__dataclass_fields__), path)
        return GoalCounts(
            **{key: _int(mapping[key], path, key)
               for key in GoalCounts.__dataclass_fields__}
        )


@dataclass(frozen=True)
class GenerationRef:
    """The active graph generation identity (M015)."""

    generation_id: str
    generation_number: int
    parent_generation_id: str | None
    graph_id: str
    plan_id: str | None
    trigger_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "generation_number": self.generation_number,
            "parent_generation_id": self.parent_generation_id,
            "graph_id": self.graph_id,
            "plan_id": self.plan_id,
            "trigger_id": self.trigger_id,
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> GenerationRef:
        mapping = _mapping(doc, path, "generation required")
        _known_keys(mapping, tuple(GenerationRef.__dataclass_fields__), path)
        _require_keys(mapping, tuple(GenerationRef.__dataclass_fields__), path)
        return GenerationRef(
            generation_id=str(_digest(mapping["generation_id"], path,
                                      "generation_id")),
            generation_number=_int(mapping["generation_number"], path,
                                   "generation_number", minimum=1),
            parent_generation_id=_digest(mapping["parent_generation_id"], path,
                                         "parent_generation_id",
                                         optional=True),
            graph_id=str(_digest(mapping["graph_id"], path, "graph_id")),
            plan_id=_digest(mapping["plan_id"], path, "plan_id", optional=True),
            trigger_id=_digest(mapping["trigger_id"], path, "trigger_id",
                               optional=True),
        )


@dataclass(frozen=True)
class GateProof:
    """The current human trust gate for the goal (never an autonomous action)."""

    state: str
    reason: str
    human_action_required: bool
    next_human_action: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "human_action_required": self.human_action_required,
            "next_human_action": self.next_human_action,
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> GateProof:
        mapping = _mapping(doc, path, "gate required")
        _known_keys(mapping, tuple(GateProof.__dataclass_fields__), path)
        _require_keys(mapping, tuple(GateProof.__dataclass_fields__), path)
        return GateProof(
            state=str(_str(mapping["state"], path, "state",
                           maximum=MAX_ID_LEN)),
            reason=str(_str(mapping["reason"], path, "reason",
                            maximum=MAX_ID_LEN)),
            human_action_required=_bool(mapping["human_action_required"], path,
                                        "human_action_required"),
            next_human_action=_opt_str(mapping["next_human_action"], path,
                                       "next_human_action",
                                       maximum=MAX_DETAIL_LEN),
        )


# --- goal proof ---------------------------------------------------------------


@dataclass(frozen=True)
class GoalProof:
    """One bounded, versioned, deterministic, derived goal-level proof."""

    schema_version: int
    proof_version: str
    goal_id: str
    objective: str
    graph_id: str
    spec_sha256: str
    generation: GenerationRef | None
    replan: Mapping[str, Any]
    nodes: tuple[NodeProof, ...]
    critical_path: tuple[str, ...]
    criteria: tuple[CriterionBinding, ...]
    reuse: Mapping[str, Any]
    scheduler: Mapping[str, Any]
    resources: Mapping[str, Any]
    risks: tuple[Risk, ...]
    final_state: str
    final_reason: str
    complete: bool
    gate: GateProof
    counts: GoalCounts
    proof_id: str = ""
    #: Evidence-only timestamp. It is deliberately excluded from the
    #: identity payload: a timestamp is evidence, never trust/order semantics.
    computed_at: str | None = None

    def identity_payload(self) -> dict[str, Any]:
        """Deterministic proof payload (excludes every timestamp)."""
        return {
            "schema_version": self.schema_version,
            "proof_version": self.proof_version,
            "goal_id": self.goal_id,
            "objective": self.objective,
            "graph_id": self.graph_id,
            "spec_sha256": self.spec_sha256,
            "generation": (None if self.generation is None
                           else self.generation.to_dict()),
            "replan": dict(self.replan),
            "nodes": [node.to_dict() for node in self.nodes],
            "critical_path": list(self.critical_path),
            "criteria": [c.to_dict() for c in self.criteria],
            "reuse": dict(self.reuse),
            "scheduler": dict(self.scheduler),
            "resources": dict(self.resources),
            "risks": [risk.to_dict() for risk in self.risks],
            "final_state": self.final_state,
            "final_reason": self.final_reason,
            "complete": self.complete,
            "gate": self.gate.to_dict(),
            "counts": self.counts.to_dict(),
        }

    def compute_proof_id(self) -> str:
        return proof_identity.proof_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "proof_id": self.proof_id,
            "computed_at": self.computed_at,
        }

    @staticmethod
    def build(*, goal_id: str, objective: str, graph_id: str,
              spec_sha256: str, generation: GenerationRef | None,
              replan: Mapping[str, Any], nodes: Sequence[NodeProof],
              critical_path: Sequence[str],
              criteria: Sequence[CriterionBinding], reuse: Mapping[str, Any],
              scheduler: Mapping[str, Any], resources: Mapping[str, Any],
              risks: Sequence[Risk], gate: GateProof, counts: GoalCounts,
              computed_at: str | None = None) -> GoalProof:
        ordered_risks = tuple(sorted(risks, key=lambda r: r.sort_key()))
        if len(ordered_risks) > MAX_RISKS:
            _fail(E_OVERFLOW, "proof", "too many risks")
        if len(nodes) > MAX_NODES:
            _fail(E_OVERFLOW, "proof", "too many nodes")
        if len(criteria) > MAX_CRITERIA:
            _fail(E_OVERFLOW, "proof", "too many criteria")
        # COMPLETE requires: at least one configured acceptance criterion,
        # every criterion proven, and zero unresolved risk. The explicit
        # ``bool(criteria)`` guard means a goal with no acceptance criteria
        # can never be COMPLETE (it fails closed with
        # ``NO_ACCEPTANCE_CRITERIA``).
        complete = not ordered_risks and all(c.proven for c in criteria) \
            and bool(criteria)
        final_state = GS_COMPLETE if complete else GS_INCOMPLETE
        final_reason = (R_ALL_CRITERIA_PROVEN if complete
                        else _select_reason(ordered_risks, criteria))
        proof = GoalProof(
            schema_version=SCHEMA_VERSION, proof_version=PROOF_VERSION,
            goal_id=goal_id, objective=objective, graph_id=graph_id,
            spec_sha256=spec_sha256, generation=generation,
            replan=dict(replan), nodes=tuple(nodes),
            critical_path=tuple(critical_path), criteria=tuple(criteria),
            reuse=dict(reuse), scheduler=dict(scheduler),
            resources=dict(resources), risks=ordered_risks,
            final_state=final_state, final_reason=final_reason,
            complete=complete, gate=gate, counts=counts,
            computed_at=computed_at)
        return replace(proof, proof_id=proof.compute_proof_id())

    @staticmethod
    def from_dict(doc: object, path: str = "proof") -> GoalProof:
        mapping = _mapping(doc, path, "goal proof required")
        keys = tuple(GoalProof.__dataclass_fields__)
        _known_keys(mapping, keys, path)
        _require_keys(mapping, keys, path)
        version = mapping["schema_version"]
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        raw_nodes = _list(mapping["nodes"], path, "nodes")
        raw_criteria = _list(mapping["criteria"], path, "criteria")
        raw_risks = _list(mapping["risks"], path, "risks")
        if len(raw_nodes) > MAX_NODES or len(raw_criteria) > MAX_CRITERIA \
                or len(raw_risks) > MAX_RISKS:
            _fail(E_OVERFLOW, path, "proof projection oversized")
        generation = (None if mapping["generation"] is None
                      else GenerationRef.from_dict(mapping["generation"],
                                                   f"{path}[generation]"))
        proof = GoalProof(
            schema_version=SCHEMA_VERSION,
            proof_version=str(_str(mapping["proof_version"], path,
                                   "proof_version", maximum=64)),
            goal_id=str(_str(mapping["goal_id"], path, "goal_id",
                             maximum=MAX_ID_LEN)),
            objective=str(_str(mapping["objective"], path, "objective",
                               maximum=4096)),
            graph_id=str(_digest(mapping["graph_id"], path, "graph_id")),
            spec_sha256=str(_digest(mapping["spec_sha256"], path,
                                    "spec_sha256")),
            generation=generation,
            replan=dict(_mapping(mapping["replan"], path, "replan")),
            nodes=tuple(NodeProof.from_dict(item, f"{path}[nodes/{i}]")
                        for i, item in enumerate(raw_nodes)),
            critical_path=tuple(
                str(_str(item, path, "critical_path", maximum=MAX_ID_LEN))
                for item in _list(mapping["critical_path"], path,
                                  "critical_path")),
            criteria=tuple(CriterionBinding.from_dict(
                item, f"{path}[criteria/{i}]")
                for i, item in enumerate(raw_criteria)),
            reuse=dict(_mapping(mapping["reuse"], path, "reuse")),
            scheduler=dict(_mapping(mapping["scheduler"], path, "scheduler")),
            resources=dict(_mapping(mapping["resources"], path, "resources")),
            risks=tuple(Risk.from_dict(item, f"{path}[risks/{i}]")
                        for i, item in enumerate(raw_risks)),
            final_state=str(_enum(mapping["final_state"], path,
                                  "final_state", GOAL_STATES)),
            final_reason=_reason(mapping["final_reason"], path),
            complete=_bool(mapping["complete"], path, "complete"),
            gate=GateProof.from_dict(mapping["gate"], f"{path}[gate]"),
            counts=GoalCounts.from_dict(mapping["counts"],
                                        f"{path}[counts]"),
            proof_id=str(_digest(mapping["proof_id"], path, "proof_id")),
            computed_at=_opt_str(mapping["computed_at"], path,
                                 "computed_at",
                                 maximum=MAX_TIMESTAMP_LEN),
        )
        if proof.compute_proof_id() != proof.proof_id:
            _fail(E_IDENTITY_MISMATCH, path, proof.proof_id)
        expected_state = GS_COMPLETE if proof.complete else GS_INCOMPLETE
        if proof.final_state != expected_state:
            _fail(E_CONTRADICTORY, path, "final_state/complete mismatch")
        if proof.complete and (proof.risks
                               or not all(c.proven for c in proof.criteria)
                               or not proof.criteria):
            _fail(E_CONTRADICTORY, path,
                  "COMPLETE requires zero risks and all criteria proven")
        return proof


def _select_reason(risks: Sequence[Risk],
                   criteria: Sequence[CriterionBinding]) -> str:
    """Deterministic single stable reason for a non-complete goal."""
    if risks:
        return risks[0].reason
    if not criteria:
        return R_NO_ACCEPTANCE_CRITERIA
    for criterion in criteria:
        if not criterion.proven:
            return criterion.reason
    return R_NO_ACCEPTANCE_CRITERIA
