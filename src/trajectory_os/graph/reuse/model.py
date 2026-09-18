"""Mission 014 — bounded cross-mission reuse domain model (pure, fail closed).

This module owns the deterministic, bounded representation of one resolved
cross-mission reuse projection and its append-only consumption history. It
performs no I/O, no clock reads and no mission access: normalization and
validation are pure functions of their inputs.

Canonical invariants (ADR-012):

* reuse is **input provenance only** — it never copies semantic success from
  one mission to another and never proves a consumer complete;
* every resolution preserves goal, graph, producer node/mission, consumer
  node/mission, execution/attestation, artifact content and patch identities;
* missing, stale, malformed, ambiguous, contradictory, duplicate, cyclic or
  identity-mismatched references fail closed with a stable reason code;
* historical/legacy evidence stays explicitly ``LEGACY`` and can never be
  silently promoted to ``PROVEN``;
* projection identity is a domain-separated digest over normalized content and
  never includes a timestamp.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NoReturn

from trajectory_os.graph import model as graph_model
from trajectory_os.graph.reuse import identity as reuse_identity

#: Schema version of the durable reuse projection / consumption documents.
SCHEMA_VERSION = 1

#: Human/machine reuse version string (additive, never an input digest).
REUSE_VERSION = "m014.1"

# --- hard bounded limits (no configuration may exceed these) -----------------

MAX_INPUTS = 1024
MAX_CONSUMPTIONS = 8192
MAX_TIMESTAMP_LEN = 64
MAX_DETAIL_LEN = 256
MAX_STOP_LEN = 64

# --- stable fail-closed error codes ------------------------------------------

E_MALFORMED = "MALFORMED_REUSE_STATE"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_REUSE_SCHEMA"
E_IDENTITY_MISMATCH = "REUSE_IDENTITY_MISMATCH"
E_GRAPH_MISMATCH = "REUSE_GRAPH_IDENTITY_MISMATCH"
E_STALE = "REUSE_EVIDENCE_STALE"
E_AMBIGUOUS = "REUSE_AMBIGUOUS_RESOLUTION"
E_OVERFLOW = "REUSE_HISTORY_OVERFLOW"
E_DUPLICATE_CONSUMPTION = "DUPLICATE_REUSE_CONSUMPTION"
E_UNTRACKED_CONSUMPTION = "UNTRACKED_REUSE_CONSUMPTION"
E_CONTRADICTORY = "CONTRADICTORY_REUSE_STATE"

# --- resolution statuses (closed set) ----------------------------------------

ST_RESOLVED = "RESOLVED"
ST_UNRESOLVED = "UNRESOLVED"
ST_REJECTED = "REJECTED"

STATUSES = frozenset({ST_RESOLVED, ST_UNRESOLVED, ST_REJECTED})

# --- stable, machine-readable resolution reason codes -------------------------

RR_RESOLVED = "REUSE_RESOLVED"
RR_NO_INPUTS = "REUSE_NO_INPUTS"
RR_PRODUCER_MISSING = "REUSE_PRODUCER_MISSING"
RR_DEPENDENCY_RELATION = "REUSE_IMPOSSIBLE_DEPENDENCY_RELATION"
RR_PRODUCER_UNRESOLVED = "REUSE_PRODUCER_UNRESOLVED"
RR_PRODUCER_NOT_PROVEN = "REUSE_PRODUCER_NOT_PROVEN"
RR_PRODUCER_FAILED = "REUSE_PRODUCER_FAILED"
RR_EVIDENCE_MISSING = "REUSE_EVIDENCE_MISSING"
RR_EVIDENCE_MALFORMED = "REUSE_EVIDENCE_MALFORMED"
RR_EVIDENCE_STALE = "REUSE_EVIDENCE_STALE"
RR_IDENTITY_MISMATCH = "REUSE_IDENTITY_MISMATCH"
RR_LEGACY_NOT_PROMOTABLE = "REUSE_LEGACY_NOT_PROMOTABLE"
RR_AMBIGUOUS = "REUSE_AMBIGUOUS"

#: Every known reuse resolution reason code (closed set, fail closed).
REASON_CODES = frozenset({
    RR_RESOLVED, RR_NO_INPUTS, RR_PRODUCER_MISSING, RR_DEPENDENCY_RELATION,
    RR_PRODUCER_UNRESOLVED, RR_PRODUCER_NOT_PROVEN, RR_PRODUCER_FAILED,
    RR_EVIDENCE_MISSING, RR_EVIDENCE_MALFORMED, RR_EVIDENCE_STALE,
    RR_IDENTITY_MISMATCH, RR_LEGACY_NOT_PROMOTABLE, RR_AMBIGUOUS,
})

#: Reasons that represent an *unresolved* (not yet provable) reference rather
#: than a *rejected* (structurally invalid / contradicted) one.
UNRESOLVED_REASONS = frozenset({
    RR_PRODUCER_MISSING, RR_PRODUCER_UNRESOLVED, RR_PRODUCER_NOT_PROVEN,
    RR_EVIDENCE_MISSING,
})

#: Re-exported trust vocabulary (declared on the graph node, resolved here).
TRUST_PROVEN = graph_model.TRUST_PROVEN
TRUST_LEGACY = graph_model.TRUST_LEGACY
TRUST_LEVELS = graph_model.TRUST_LEVELS

#: Trust ordering: a consumer may only require a level it can satisfy.
TRUST_ORDER = {TRUST_LEGACY: 0, TRUST_PROVEN: 1}


class ReuseValidationError(Exception):
    """Reuse state violates the canonical bounded schema (fail closed)."""

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(f"{code}: {path}" + (f" ({detail})" if detail else ""))
        self.code = code
        self.path = path
        self.detail = detail


# --- strict primitive helpers -------------------------------------------------


def _fail(code: str, path: str, detail: str = "") -> NoReturn:
    raise ReuseValidationError(code, path, detail)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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


def _require_digest(value: object, path: str, detail: str) -> str:
    if not reuse_identity.is_valid_digest(value):
        _fail(E_MALFORMED, path, detail)
    assert isinstance(value, str)
    return value


def _require_status(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in STATUSES:
        _fail(E_MALFORMED, path, f"unknown status: {value!r}")
    return value


def _require_reason(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in REASON_CODES:
        _fail(E_MALFORMED, path, f"unknown reason code: {value!r}")
    return value


def _require_trust(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in TRUST_LEVELS:
        _fail(E_MALFORMED, path, f"unknown trust level: {value!r}")
    return value


# --- per-input resolution -----------------------------------------------------


@dataclass(frozen=True)
class ReuseInputStatus:
    """Exact, bounded resolution of one consumer's declared reuse input."""

    consumer_node_id: str
    consumer_mission_id: str | None
    input_id: str
    producer_node_id: str
    producer_mission_id: str | None
    evidence_kind: str
    phase_id: str
    required: bool
    min_trust: str
    status: str
    reason: str
    trust: str | None = None
    artifact_content_sha256: str | None = None
    artifact_patch_sha256: str | None = None
    artifact_attestation: str | None = None
    artifact_subrun_id: str | None = None
    artifact_semantic_status: str | None = None
    expected_sha256: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == ST_RESOLVED

    @property
    def blocking(self) -> bool:
        return self.required and not self.resolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer_node_id": self.consumer_node_id,
            "consumer_mission_id": self.consumer_mission_id,
            "input_id": self.input_id,
            "producer_node_id": self.producer_node_id,
            "producer_mission_id": self.producer_mission_id,
            "evidence_kind": self.evidence_kind,
            "phase_id": self.phase_id,
            "required": self.required,
            "min_trust": self.min_trust,
            "status": self.status,
            "reason": self.reason,
            "trust": self.trust,
            "artifact_content_sha256": self.artifact_content_sha256,
            "artifact_patch_sha256": self.artifact_patch_sha256,
            "artifact_attestation": self.artifact_attestation,
            "artifact_subrun_id": self.artifact_subrun_id,
            "artifact_semantic_status": self.artifact_semantic_status,
            "expected_sha256": self.expected_sha256,
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> ReuseInputStatus:
        mapping = _require_mapping(doc, path, "reuse input status required")
        keys = tuple(ReuseInputStatus.__dataclass_fields__)
        _known_keys(mapping, keys, path)
        _require_keys(mapping, keys, path)
        evidence_kind = mapping["evidence_kind"]
        if evidence_kind not in graph_model.EVIDENCE_KINDS:
            _fail(E_MALFORMED, path, f"evidence_kind={evidence_kind!r}")
        min_trust = mapping["min_trust"]
        if min_trust not in TRUST_LEVELS:
            _fail(E_MALFORMED, path, f"min_trust={min_trust!r}")
        expected = mapping["expected_sha256"]
        if expected is not None:
            _require_digest(expected, path, "expected_sha256")
        for field_name in ("artifact_content_sha256",
                           "artifact_patch_sha256"):
            value = mapping[field_name]
            if value is not None:
                _require_digest(value, path, field_name)
        status = _require_status(mapping["status"], path)
        reason = _require_reason(mapping["reason"], path)
        trust = _require_trust(mapping["trust"], path)
        content_sha = mapping["artifact_content_sha256"]
        if status == ST_RESOLVED:
            if reason != RR_RESOLVED or content_sha is None or trust is None:
                _fail(E_CONTRADICTORY, path,
                      "resolved input requires REUSE_RESOLVED + trust + "
                      "artifact content")
        elif reason == RR_RESOLVED:
            _fail(E_CONTRADICTORY, path,
                  f"{status} input cannot carry {RR_RESOLVED}")
        elif status == ST_UNRESOLVED and reason not in UNRESOLVED_REASONS:
            _fail(E_CONTRADICTORY, path,
                  f"unresolved input cannot carry reason {reason!r}")
        return ReuseInputStatus(
            consumer_node_id=_require_str(
                mapping["consumer_node_id"], path, "consumer_node_id",
                maximum=64),
            consumer_mission_id=_optional_str(
                mapping["consumer_mission_id"], path, "consumer_mission_id",
                maximum=64),
            input_id=_require_str(mapping["input_id"], path, "input_id",
                                  maximum=64),
            producer_node_id=_require_str(
                mapping["producer_node_id"], path, "producer_node_id",
                maximum=64),
            producer_mission_id=_optional_str(
                mapping["producer_mission_id"], path, "producer_mission_id",
                maximum=64),
            evidence_kind=evidence_kind,
            phase_id=_require_str(mapping["phase_id"], path, "phase_id",
                                  maximum=64),
            required=_require_bool(mapping["required"], path, "required"),
            min_trust=min_trust,
            status=status,
            reason=reason,
            trust=trust,
            artifact_content_sha256=content_sha,
            artifact_patch_sha256=mapping["artifact_patch_sha256"],
            artifact_attestation=_optional_str(
                mapping["artifact_attestation"], path, "artifact_attestation",
                maximum=64),
            artifact_subrun_id=_optional_str(
                mapping["artifact_subrun_id"], path, "artifact_subrun_id",
                maximum=128),
            artifact_semantic_status=_optional_str(
                mapping["artifact_semantic_status"], path,
                "artifact_semantic_status", maximum=64),
            expected_sha256=expected,
        )


# --- projection ---------------------------------------------------------------


def _input_key(item: ReuseInputStatus) -> tuple[str, str]:
    return (item.consumer_node_id, item.input_id)


@dataclass(frozen=True)
class ReuseProjection:
    """The exact, immutable resolved reuse projection for one goal graph."""

    schema_version: int
    reuse_version: str
    goal_id: str
    graph_id: str
    spec_sha256: str
    inputs: tuple[ReuseInputStatus, ...]
    projection_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        """Deterministic projection payload (excludes every timestamp)."""
        return {
            "schema_version": self.schema_version,
            "reuse_version": self.reuse_version,
            "goal_id": self.goal_id,
            "graph_id": self.graph_id,
            "spec_sha256": self.spec_sha256,
            "inputs": [item.to_dict() for item in self.inputs],
        }

    def compute_projection_id(self) -> str:
        return reuse_identity.projection_id(self.identity_payload())

    def counts(self) -> dict[str, int]:
        counts = {"resolved": 0, "unresolved": 0, "rejected": 0,
                  "blocking": 0}
        for item in self.inputs:
            if item.status == ST_RESOLVED:
                counts["resolved"] += 1
            elif item.status == ST_UNRESOLVED:
                counts["unresolved"] += 1
            else:
                counts["rejected"] += 1
            if item.blocking:
                counts["blocking"] += 1
        return counts

    def by_consumer(self) -> dict[str, tuple[ReuseInputStatus, ...]]:
        grouped: dict[str, list[ReuseInputStatus]] = {}
        for item in self.inputs:
            grouped.setdefault(item.consumer_node_id, []).append(item)
        return {node_id: tuple(items) for node_id, items in grouped.items()}

    def blocking_reason(self, consumer_node_id: str) -> str | None:
        """First stable reason blocking a consumer, or ``None`` when clear."""
        for item in self.inputs:
            if item.consumer_node_id != consumer_node_id:
                continue
            if item.blocking:
                return item.reason
        return None

    def blocked_nodes(self) -> tuple[str, ...]:
        return tuple(sorted({
            item.consumer_node_id for item in self.inputs if item.blocking
        }))

    @property
    def blocked(self) -> bool:
        return any(item.blocking for item in self.inputs)

    @staticmethod
    def build(
        *,
        goal_id: str,
        graph_id: str,
        spec_sha256: str,
        inputs: Sequence[ReuseInputStatus],
    ) -> ReuseProjection:
        unique: dict[tuple[str, str], ReuseInputStatus] = {}
        for item in inputs:
            key = _input_key(item)
            if key in unique:
                _fail(E_AMBIGUOUS, f"reuse[{key[0]}/{key[1]}]",
                      "duplicate consumer/input resolution")
            unique[key] = item
        ordered = tuple(sorted(unique.values(), key=_input_key))
        projection = ReuseProjection(
            schema_version=SCHEMA_VERSION,
            reuse_version=REUSE_VERSION,
            goal_id=goal_id,
            graph_id=graph_id,
            spec_sha256=spec_sha256,
            inputs=ordered,
        )
        return replace(projection,
                       projection_id=projection.compute_projection_id())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "projection_id": self.projection_id,
            "status": "BLOCKED" if self.blocked else "RESOLVED",
            "counts": self.counts(),
            "blocked_nodes": list(self.blocked_nodes()),
        }

    @staticmethod
    def from_dict(doc: object, path: str = "reuse") -> ReuseProjection:
        mapping = _require_mapping(doc, path, "reuse projection required")
        keys = tuple(ReuseProjection.__dataclass_fields__)
        _known_keys(mapping, (*keys, "status", "counts", "blocked_nodes"),
                    path)
        _require_keys(mapping, (*keys, "status"), path)
        version = mapping["schema_version"]
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        raw_inputs = _require_list(mapping["inputs"], path, "inputs")
        if len(raw_inputs) > MAX_INPUTS:
            _fail(E_OVERFLOW, path, "too many reuse inputs")
        inputs = tuple(
            ReuseInputStatus.from_dict(item, f"{path}[inputs/{i}]")
            for i, item in enumerate(raw_inputs)
        )
        projection = ReuseProjection(
            schema_version=SCHEMA_VERSION,
            reuse_version=_require_str(mapping["reuse_version"], path,
                                       "reuse_version", maximum=64),
            goal_id=_require_str(mapping["goal_id"], path, "goal_id",
                                 maximum=64),
            graph_id=_require_str(mapping["graph_id"], path, "graph_id",
                                  maximum=64),
            spec_sha256=_require_str(mapping["spec_sha256"], path,
                                     "spec_sha256", maximum=64),
            inputs=inputs,
            projection_id=mapping["projection_id"],
        )
        if projection.compute_projection_id() != projection.projection_id:
            _fail(E_IDENTITY_MISMATCH, path, projection.projection_id)
        expected_status = "BLOCKED" if projection.blocked else "RESOLVED"
        if mapping["status"] != expected_status:
            _fail(E_CONTRADICTORY, path,
                  f"status={mapping['status']!r} != {expected_status!r}")
        return projection


# --- consumption history ------------------------------------------------------


@dataclass(frozen=True)
class ConsumptionRecord:
    """One append-only cross-mission consumption event (provenance only)."""

    schema_version: int
    goal_id: str
    graph_id: str
    projection_id: str
    consumer_node_id: str
    consumer_mission_id: str | None
    input_id: str
    producer_node_id: str
    producer_mission_id: str | None
    artifact_content_sha256: str
    artifact_patch_sha256: str | None
    trust: str
    consumption_id: str = ""
    consumed_at: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal_id": self.goal_id,
            "graph_id": self.graph_id,
            "projection_id": self.projection_id,
            "consumer_node_id": self.consumer_node_id,
            "consumer_mission_id": self.consumer_mission_id,
            "input_id": self.input_id,
            "producer_node_id": self.producer_node_id,
            "producer_mission_id": self.producer_mission_id,
            "artifact_content_sha256": self.artifact_content_sha256,
            "artifact_patch_sha256": self.artifact_patch_sha256,
            "trust": self.trust,
        }

    def compute_consumption_id(self) -> str:
        return reuse_identity.consumption_id(self.identity_payload())

    @staticmethod
    def build(
        *,
        goal_id: str,
        graph_id: str,
        projection_id: str,
        input_status: ReuseInputStatus,
        consumed_at: str,
    ) -> ConsumptionRecord:
        if (input_status.status != ST_RESOLVED
                or input_status.artifact_content_sha256 is None
                or input_status.trust is None):
            _fail(E_CONTRADICTORY, "consumption",
                  "only resolved inputs may be recorded as consumed")
        record = ConsumptionRecord(
            schema_version=SCHEMA_VERSION,
            goal_id=goal_id,
            graph_id=graph_id,
            projection_id=projection_id,
            consumer_node_id=input_status.consumer_node_id,
            consumer_mission_id=input_status.consumer_mission_id,
            input_id=input_status.input_id,
            producer_node_id=input_status.producer_node_id,
            producer_mission_id=input_status.producer_mission_id,
            artifact_content_sha256=input_status.artifact_content_sha256,
            artifact_patch_sha256=input_status.artifact_patch_sha256,
            trust=input_status.trust,
            consumed_at=consumed_at,
        )
        return replace(record,
                       consumption_id=record.compute_consumption_id())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "consumption_id": self.consumption_id,
            "consumed_at": self.consumed_at,
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> ConsumptionRecord:
        mapping = _require_mapping(doc, path, "consumption record required")
        keys = tuple(ConsumptionRecord.__dataclass_fields__)
        _known_keys(mapping, keys, path)
        _require_keys(mapping, keys, path)
        version = mapping["schema_version"]
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        trust = _require_trust(mapping["trust"], path)
        if trust is None:
            _fail(E_MALFORMED, path, "trust is required")
        patch = mapping["artifact_patch_sha256"]
        if patch is not None:
            _require_digest(patch, path, "artifact_patch_sha256")
        record = ConsumptionRecord(
            schema_version=SCHEMA_VERSION,
            goal_id=_require_str(mapping["goal_id"], path, "goal_id",
                                 maximum=64),
            graph_id=_require_str(mapping["graph_id"], path, "graph_id",
                                  maximum=64),
            projection_id=_require_digest(
                mapping["projection_id"], path, "projection_id"),
            consumer_node_id=_require_str(
                mapping["consumer_node_id"], path, "consumer_node_id",
                maximum=64),
            consumer_mission_id=_optional_str(
                mapping["consumer_mission_id"], path, "consumer_mission_id",
                maximum=64),
            input_id=_require_str(mapping["input_id"], path, "input_id",
                                  maximum=64),
            producer_node_id=_require_str(
                mapping["producer_node_id"], path, "producer_node_id",
                maximum=64),
            producer_mission_id=_optional_str(
                mapping["producer_mission_id"], path, "producer_mission_id",
                maximum=64),
            artifact_content_sha256=_require_digest(
                mapping["artifact_content_sha256"], path,
                "artifact_content_sha256"),
            artifact_patch_sha256=patch,
            trust=trust,
            consumption_id=mapping["consumption_id"],
            consumed_at=_require_str(mapping["consumed_at"], path,
                                     "consumed_at",
                                     maximum=MAX_TIMESTAMP_LEN),
        )
        if record.compute_consumption_id() != record.consumption_id:
            _fail(E_IDENTITY_MISMATCH, path, record.consumption_id)
        return record
