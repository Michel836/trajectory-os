"""Mission 013 — bounded portfolio-scheduler domain model (pure, fail closed).

This module owns the deterministic, bounded representation of a scheduling
capacity policy, per-node scheduled demand, resource reservations, one
scheduling decision and the append-only scheduler state. It performs no I/O,
no clock reads and no mission access: normalization and validation are pure
functions of their inputs.

Canonical invariants (ADR-011):

* capacity is explicit and configured — never inferred from mission prose,
  hardware probing or physical GPU telemetry;
* node demand is derived only from the explicit M012 ``resources`` /
  ``budgets`` declaration; ``model_heavy`` alone never reserves local GPU;
* every non-admission carries one bounded, stable, machine-readable reason
  code;
* decision identity is a domain-separated digest over normalized content and
  never includes a timestamp;
* persisted state is strictly reconstructed and fails closed on malformed,
  contradictory, oversized or identity-mismatched content.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn

from trajectory_os.graph import identity as graph_identity
from trajectory_os.graph import model as graph_model
from trajectory_os.graph.scheduler import identity as sched_identity
from trajectory_os.missions import model as mission_model
from trajectory_os.runs import resources as runs_resources

#: Schema version of the durable scheduler state / decision documents.
SCHEMA_VERSION = 1

#: Human/machine scheduler version string (additive, never an input digest).
SCHEDULER_VERSION = "m013.1"

# --- hard bounded limits (no configuration may exceed these) -----------------

MAX_CPU_SLOTS = runs_resources.MAX_CPU_SLOTS
MAX_GPU_SLOTS = 16
MAX_GPU_MEM_BYTES = 1 << 50
MAX_GLOBAL_CONCURRENCY = 32
MAX_DECISIONS = 1024
MAX_SCHEDULER_EVENTS = 8192
MAX_DISPATCH_RECORDS = 1024
MAX_TIMESTAMP_LEN = 64
MAX_DISPATCH_REF_LEN = 256
MAX_STOP_LEN = 64

#: Rationale strings are bounded; reasons themselves are a closed set.
MAX_DETAIL_LEN = 256

# --- stable fail-closed error codes ------------------------------------------

E_MALFORMED = "MALFORMED_SCHEDULER_STATE"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_SCHEDULER_SCHEMA"
E_INVALID_POLICY = "INVALID_CAPACITY_POLICY"
E_INVALID_RESOURCE = "INVALID_RESOURCE_SPEC"
E_INVALID_BUDGET = "INVALID_BUDGET"
E_IDENTITY_MISMATCH = "SCHEDULER_IDENTITY_MISMATCH"
E_GRAPH_MISMATCH = "GRAPH_IDENTITY_MISMATCH"
E_DUPLICATE_SELECTION = "DUPLICATE_SELECTION"
E_CONTRADICTORY_RESERVATION = "CONTRADICTORY_RESERVATION"
E_IMPOSSIBLE_ACCOUNTING = "IMPOSSIBLE_RESOURCE_ACCOUNTING"
E_UNTRACKED_DECISION = "UNTRACKED_DECISION"
E_MISSING_DECISION = "MISSING_DECISION"
E_DISPATCH_EVIDENCE = "AMBIGUOUS_DISPATCH_EVIDENCE"
E_EVENT_LOG = "MALFORMED_EVENT_LOG"
E_OVERFLOW = "SCHEDULER_HISTORY_OVERFLOW"
E_MALFORMED_DECISION = "MALFORMED_SCHEDULER_DECISION"

# --- scheduler reason codes (closed, stable, machine-readable) ----------------

R_ADMITTED = "ADMITTED"
R_DEPENDENCY_BLOCKED = "DEPENDENCY_BLOCKED"
R_UNRESOLVED_EVIDENCE = "UNRESOLVED_EVIDENCE"
R_INVALID_EVIDENCE = "INVALID_EVIDENCE"
R_MISSION_FAILED = "MISSION_FAILED"
R_ALREADY_COMPLETE = "ALREADY_COMPLETE"
R_ALREADY_ACTIVE = "ALREADY_ACTIVE"
R_ALREADY_DISPATCHED = "ALREADY_DISPATCHED"
R_MISSING_MISSION_REFERENCE = "MISSING_MISSION_REFERENCE"
R_INVALID_RESOURCE_SPEC = "INVALID_RESOURCE_SPEC"
R_CPU_CAPACITY = "CPU_CAPACITY"
R_GPU_CAPACITY = "GPU_CAPACITY"
R_GPU_MEMORY = "GPU_MEMORY"
R_EXCLUSIVE_CONFLICT = "EXCLUSIVE_CONFLICT"
R_CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
R_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
R_STALE_DECISION_INPUT = "STALE_DECISION_INPUT"

REASON_CODES = frozenset({
    R_ADMITTED, R_DEPENDENCY_BLOCKED, R_UNRESOLVED_EVIDENCE,
    R_INVALID_EVIDENCE, R_MISSION_FAILED, R_ALREADY_COMPLETE,
    R_ALREADY_ACTIVE, R_ALREADY_DISPATCHED, R_MISSING_MISSION_REFERENCE,
    R_INVALID_RESOURCE_SPEC, R_CPU_CAPACITY, R_GPU_CAPACITY, R_GPU_MEMORY,
    R_EXCLUSIVE_CONFLICT, R_CONCURRENCY_LIMIT, R_BUDGET_EXHAUSTED,
    R_STALE_DECISION_INPUT,
})

# --- node scheduling outcomes (closed set) -----------------------------------

O_ADMITTED = "ADMITTED"
O_DEFERRED = "DEFERRED"
O_BLOCKED = "BLOCKED"
O_ACTIVE = "ACTIVE"
O_COMPLETE = "COMPLETE"

OUTCOMES = frozenset({O_ADMITTED, O_DEFERRED, O_BLOCKED, O_ACTIVE, O_COMPLETE})

# --- explicit execution-path classification (never inferred) ------------------

X_LOCAL_CPU = "LOCAL_CPU"
X_LOCAL_GPU = "LOCAL_GPU"
X_REMOTE_MODEL = "REMOTE_MODEL"
X_UNSPECIFIED = "UNSPECIFIED"

EXECUTION_CLASSES = frozenset({
    X_LOCAL_CPU, X_LOCAL_GPU, X_REMOTE_MODEL, X_UNSPECIFIED,
})

#: Reason codes that release a reservation (authoritative terminal evidence).
RELEASE_REASONS = frozenset({
    "MISSION_COMPLETE", "MISSION_FAILED", "MISSION_BLOCKED",
    "MISSION_INVALID",
})


class SchedulerValidationError(Exception):
    """Scheduler state violates the canonical bounded schema (fail closed)."""

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(f"{code}: {path}" + (f" ({detail})" if detail else ""))
        self.code = code
        self.path = path
        self.detail = detail


# --- strict primitive helpers -------------------------------------------------


def _fail(code: str, path: str, detail: str = "") -> NoReturn:
    raise SchedulerValidationError(code, path, detail)


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


def _require_int(value: object, path: str, detail: str, *,
                 minimum: int, maximum: int | None = None,
                 code: str = E_MALFORMED) -> int:
    if not _is_int(value):
        _fail(code, path, detail)
    assert isinstance(value, int)
    if value < minimum or (maximum is not None and value > maximum):
        _fail(code, path, f"{detail} out of bounds: {value}")
    return value


def _require_reason(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in REASON_CODES:
        _fail(E_MALFORMED, path, f"unknown reason code: {value!r}")
    return value


def _require_outcome(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in OUTCOMES:
        _fail(E_MALFORMED, path, f"unknown outcome: {value!r}")
    return value


def _require_execution(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in EXECUTION_CLASSES:
        _fail(E_MALFORMED, path, f"unknown execution class: {value!r}")
    return value


# --- configured capacity policy ----------------------------------------------


@dataclass(frozen=True)
class SchedulerPolicy:
    """Explicit, configured scheduler capacity (never probed, never inferred)."""

    cpu_slots: int
    gpu_slots: int
    gpu_mem_bytes: int
    global_concurrency: int

    def validate(self) -> SchedulerPolicy:
        _require_int(self.cpu_slots, "policy", "cpu_slots", minimum=1,
                     maximum=MAX_CPU_SLOTS, code=E_INVALID_POLICY)
        _require_int(self.gpu_slots, "policy", "gpu_slots", minimum=0,
                     maximum=MAX_GPU_SLOTS, code=E_INVALID_POLICY)
        _require_int(self.gpu_mem_bytes, "policy", "gpu_mem_bytes", minimum=0,
                     maximum=MAX_GPU_MEM_BYTES, code=E_INVALID_POLICY)
        _require_int(self.global_concurrency, "policy", "global_concurrency",
                     minimum=1, maximum=MAX_GLOBAL_CONCURRENCY,
                     code=E_INVALID_POLICY)
        return self

    def to_dict(self) -> dict[str, int]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cpu_slots": self.cpu_slots,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
            "global_concurrency": self.global_concurrency,
        }

    @property
    def policy_id(self) -> str:
        return sched_identity.policy_id({
            "cpu_slots": self.cpu_slots,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
            "global_concurrency": self.global_concurrency,
        })

    @property
    def capacity_id(self) -> str:
        """Alias emphasizing that the digest identifies the capacity snapshot."""
        return self.policy_id

    @staticmethod
    def from_dict(doc: object, path: str = "policy") -> SchedulerPolicy:
        mapping = _require_mapping(doc, path, "policy object required")
        _known_keys(
            mapping,
            ("schema_version", "cpu_slots", "gpu_slots", "gpu_mem_bytes",
             "global_concurrency"),
            path)
        _require_keys(
            mapping,
            ("cpu_slots", "gpu_slots", "gpu_mem_bytes", "global_concurrency"),
            path)
        version = mapping.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        policy = SchedulerPolicy(
            cpu_slots=_require_int(
                mapping["cpu_slots"], path, "cpu_slots", minimum=1,
                maximum=MAX_CPU_SLOTS, code=E_INVALID_POLICY),
            gpu_slots=_require_int(
                mapping["gpu_slots"], path, "gpu_slots", minimum=0,
                maximum=MAX_GPU_SLOTS, code=E_INVALID_POLICY),
            gpu_mem_bytes=_require_int(
                mapping["gpu_mem_bytes"], path, "gpu_mem_bytes", minimum=0,
                maximum=MAX_GPU_MEM_BYTES, code=E_INVALID_POLICY),
            global_concurrency=_require_int(
                mapping["global_concurrency"], path, "global_concurrency",
                minimum=1, maximum=MAX_GLOBAL_CONCURRENCY,
                code=E_INVALID_POLICY),
        )
        return policy.validate()


#: Bounded, documented default used when the operator supplies no policy file.
DEFAULT_POLICY = SchedulerPolicy(
    cpu_slots=4, gpu_slots=1, gpu_mem_bytes=8 * (1 << 30),
    global_concurrency=2)


# --- per-node demand ----------------------------------------------------------


@dataclass(frozen=True)
class NodeDemand:
    """Deterministic resource/budget demand derived from explicit M012 fields."""

    node_id: str
    mission_id: str | None
    execution: str
    cpu_slots: int
    gpu_slots: int
    gpu_mem_bytes: int
    exclusive: bool
    model_heavy: bool
    max_attempts: int | None
    subrun_budget: int | None
    time_budget_s: int | None
    repair_budget: int | None

    @property
    def declared(self) -> bool:
        return (self.cpu_slots > 0 or self.gpu_slots > 0
                or self.gpu_mem_bytes > 0 or self.exclusive)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "mission_id": self.mission_id,
            "execution": self.execution,
            "cpu_slots": self.cpu_slots,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
            "exclusive": self.exclusive,
            "model_heavy": self.model_heavy,
            "max_attempts": self.max_attempts,
            "subrun_budget": self.subrun_budget,
            "time_budget_s": self.time_budget_s,
            "repair_budget": self.repair_budget,
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> NodeDemand:
        mapping = _require_mapping(doc, path, "demand object required")
        _known_keys(mapping, tuple(NodeDemand.__dataclass_fields__), path)
        _require_keys(mapping, tuple(NodeDemand.__dataclass_fields__), path)
        return NodeDemand(
            node_id=_require_str(mapping["node_id"], path, "node_id",
                                 maximum=64),
            mission_id=_optional_str(mapping["mission_id"], path, "mission_id",
                                     maximum=64),
            execution=_require_execution(mapping["execution"], path),
            cpu_slots=_require_int(mapping["cpu_slots"], path, "cpu_slots",
                                   minimum=0, maximum=MAX_CPU_SLOTS),
            gpu_slots=_require_int(mapping["gpu_slots"], path, "gpu_slots",
                                   minimum=0, maximum=1),
            gpu_mem_bytes=_require_int(mapping["gpu_mem_bytes"], path,
                                       "gpu_mem_bytes", minimum=0,
                                       maximum=MAX_GPU_MEM_BYTES),
            exclusive=_require_bool(mapping["exclusive"], path, "exclusive"),
            model_heavy=_require_bool(mapping["model_heavy"], path,
                                      "model_heavy"),
            max_attempts=_optional_int(mapping["max_attempts"], path,
                                       "max_attempts",
                                       maximum=mission_model.MAX_ATTEMPTS_PER_PHASE),
            subrun_budget=_optional_int(mapping["subrun_budget"], path,
                                        "subrun_budget",
                                        maximum=mission_model.MAX_SUBRUNS),
            time_budget_s=_optional_int(mapping["time_budget_s"], path,
                                        "time_budget_s",
                                        maximum=mission_model.MAX_TIME_BUDGET_S),
            repair_budget=_optional_int(mapping["repair_budget"], path,
                                        "repair_budget",
                                        maximum=mission_model.MAX_REPAIR_ROUNDS),
        )


def _optional_int(value: object, path: str, detail: str, *,
                  maximum: int) -> int | None:
    if value is None:
        return None
    return _require_int(value, path, detail, minimum=0, maximum=maximum)


def derive_demand(node: graph_model.GraphNode) -> NodeDemand:
    """Derive bounded demand from one M012 node (pure; fails closed).

    Local GPU is reserved **if and only if** the node explicitly declares
    ``resources.gpu = true``. A ``model_heavy`` node without that explicit
    declaration is classified ``REMOTE_MODEL`` and reserves no local GPU or
    VRAM — model-heaviness alone never claims local hardware.
    """
    resources = node.resources
    cpu = resources.cpu_slots or 0
    gpu = 1 if resources.gpu is True else 0
    vram = resources.gpu_mem_bytes or 0
    if resources.gpu is not True and resources.gpu_mem_bytes is not None:
        _fail(E_INVALID_RESOURCE, f"node {node.node_id}",
              "gpu_mem_bytes requires gpu=true")
    if resources.cpu_slots is not None and isinstance(resources.cpu_slots, bool):
        _fail(E_INVALID_RESOURCE, f"node {node.node_id}", "cpu_slots")
    model_heavy = resources.model_heavy is True
    exclusive = resources.exclusive is True
    if gpu:
        execution = X_LOCAL_GPU
    elif model_heavy:
        execution = X_REMOTE_MODEL
    elif cpu:
        execution = X_LOCAL_CPU
    else:
        execution = X_UNSPECIFIED
    budgets = node.budgets
    return NodeDemand(
        node_id=node.node_id,
        mission_id=(node.mission_ref.mission_id
                    if node.mission_ref is not None else None),
        execution=execution,
        cpu_slots=cpu,
        gpu_slots=gpu,
        gpu_mem_bytes=vram,
        exclusive=exclusive,
        model_heavy=model_heavy,
        max_attempts=budgets.max_attempts,
        subrun_budget=budgets.subruns,
        time_budget_s=budgets.time_budget_s,
        repair_budget=budgets.repair_budget,
    )


# --- reservations -------------------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    """One active resource reservation bound to a mission identity."""

    node_id: str
    mission_id: str
    execution: str
    cpu_slots: int
    gpu_slots: int
    gpu_mem_bytes: int
    exclusive: bool
    owned: bool
    decision_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "mission_id": self.mission_id,
            "execution": self.execution,
            "cpu_slots": self.cpu_slots,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
            "exclusive": self.exclusive,
            "owned": self.owned,
            "decision_id": self.decision_id,
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> Reservation:
        mapping = _require_mapping(doc, path, "reservation object required")
        _known_keys(mapping, tuple(Reservation.__dataclass_fields__), path)
        _require_keys(mapping, tuple(Reservation.__dataclass_fields__), path)
        decision = mapping["decision_id"]
        if decision is not None and not sched_identity.is_valid_digest(decision):
            _fail(E_MALFORMED, path, "decision_id")
        return Reservation(
            node_id=_require_str(mapping["node_id"], path, "node_id",
                                 maximum=64),
            mission_id=_require_str(mapping["mission_id"], path, "mission_id",
                                    maximum=64),
            execution=_require_execution(mapping["execution"], path),
            cpu_slots=_require_int(mapping["cpu_slots"], path, "cpu_slots",
                                   minimum=0, maximum=MAX_CPU_SLOTS),
            gpu_slots=_require_int(mapping["gpu_slots"], path, "gpu_slots",
                                   minimum=0, maximum=1),
            gpu_mem_bytes=_require_int(mapping["gpu_mem_bytes"], path,
                                       "gpu_mem_bytes", minimum=0,
                                       maximum=MAX_GPU_MEM_BYTES),
            exclusive=_require_bool(mapping["exclusive"], path, "exclusive"),
            owned=_require_bool(mapping["owned"], path, "owned"),
            decision_id=decision,
        )


@dataclass(frozen=True)
class ReservationTotals:
    cpu_slots: int = 0
    gpu_slots: int = 0
    gpu_mem_bytes: int = 0
    count: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "cpu_slots": self.cpu_slots,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
            "count": self.count,
        }

    @staticmethod
    def of(reservations: Sequence[Reservation]) -> ReservationTotals:
        return ReservationTotals(
            cpu_slots=sum(r.cpu_slots for r in reservations),
            gpu_slots=sum(r.gpu_slots for r in reservations),
            gpu_mem_bytes=sum(r.gpu_mem_bytes for r in reservations),
            count=len(reservations),
        )


@dataclass(frozen=True)
class ReleaseRecord:
    """One reservation released this cycle on authoritative terminal evidence."""

    node_id: str
    mission_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"node_id": self.node_id, "mission_id": self.mission_id,
                "reason": self.reason}

    @staticmethod
    def from_dict(doc: object, path: str) -> ReleaseRecord:
        mapping = _require_mapping(doc, path, "release object required")
        _known_keys(mapping, ("node_id", "mission_id", "reason"), path)
        _require_keys(mapping, ("node_id", "mission_id", "reason"), path)
        reason = mapping["reason"]
        if not isinstance(reason, str) or reason not in RELEASE_REASONS:
            _fail(E_MALFORMED, path, f"unknown release reason: {reason!r}")
        return ReleaseRecord(
            node_id=_require_str(mapping["node_id"], path, "node_id",
                                 maximum=64),
            mission_id=_require_str(mapping["mission_id"], path, "mission_id",
                                    maximum=64),
            reason=reason,
        )


@dataclass(frozen=True)
class ReservationSnapshot:
    """Separates configured capacity from current and scheduler-owned holds."""

    current: tuple[Reservation, ...]
    scheduler_owned: tuple[Reservation, ...]
    released: tuple[ReleaseRecord, ...] = ()
    newly_reserved: tuple[Reservation, ...] = ()

    @property
    def totals_current(self) -> ReservationTotals:
        return ReservationTotals.of(self.current)

    @property
    def totals_owned(self) -> ReservationTotals:
        return ReservationTotals.of(self.scheduler_owned)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": [r.to_dict() for r in self.current],
            "scheduler_owned": [r.to_dict() for r in self.scheduler_owned],
            "released": [r.to_dict() for r in self.released],
            "newly_reserved": [r.to_dict() for r in self.newly_reserved],
            "totals_current": self.totals_current.to_dict(),
            "totals_owned": self.totals_owned.to_dict(),
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> ReservationSnapshot:
        mapping = _require_mapping(doc, path, "reservation snapshot required")
        _known_keys(
            mapping,
            ("current", "scheduler_owned", "released", "newly_reserved",
             "totals_current", "totals_owned"),
            path)
        _require_keys(
            mapping,
            ("current", "scheduler_owned", "released", "newly_reserved"),
            path)
        current = tuple(
            Reservation.from_dict(item, f"{path}[current/{i}]")
            for i, item in enumerate(_require_list(mapping["current"], path,
                                                   "current"))
        )
        owned = tuple(
            Reservation.from_dict(item, f"{path}[scheduler_owned/{i}]")
            for i, item in enumerate(_require_list(mapping["scheduler_owned"],
                                                   path, "scheduler_owned"))
        )
        released = tuple(
            ReleaseRecord.from_dict(item, f"{path}[released/{i}]")
            for i, item in enumerate(_require_list(mapping["released"], path,
                                                   "released"))
        )
        newly = tuple(
            Reservation.from_dict(item, f"{path}[newly_reserved/{i}]")
            for i, item in enumerate(_require_list(mapping["newly_reserved"],
                                                   path, "newly_reserved"))
        )
        return ReservationSnapshot(
            current=current, scheduler_owned=owned, released=released,
            newly_reserved=newly)


# --- dispatch records ---------------------------------------------------------


@dataclass(frozen=True)
class DispatchRecord:
    """One durable dispatch attempt of one admitted node (append-only)."""

    node_id: str
    mission_id: str
    decision_id: str
    dispatch_id: str
    dispatch_ref: str | None
    stop: str
    mission_state: str
    mission_reason: str
    dispatched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "mission_id": self.mission_id,
            "decision_id": self.decision_id,
            "dispatch_id": self.dispatch_id,
            "dispatch_ref": self.dispatch_ref,
            "stop": self.stop,
            "mission_state": self.mission_state,
            "mission_reason": self.mission_reason,
            "dispatched_at": self.dispatched_at,
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> DispatchRecord:
        mapping = _require_mapping(doc, path, "dispatch record required")
        _known_keys(mapping, tuple(DispatchRecord.__dataclass_fields__), path)
        _require_keys(mapping, tuple(DispatchRecord.__dataclass_fields__), path)
        for field_name in ("decision_id", "dispatch_id"):
            if not sched_identity.is_valid_digest(mapping[field_name]):
                _fail(E_DISPATCH_EVIDENCE, path, field_name)
        state = mapping["mission_state"]
        if not isinstance(state, str) or state not in mission_model.MISSION_STATES:
            _fail(E_DISPATCH_EVIDENCE, path, f"mission_state={state!r}")
        reason = mapping["mission_reason"]
        if not isinstance(reason, str) or reason not in mission_model.REASON_CODES:
            _fail(E_DISPATCH_EVIDENCE, path, f"mission_reason={reason!r}")
        return DispatchRecord(
            node_id=_require_str(mapping["node_id"], path, "node_id",
                                 maximum=64),
            mission_id=_require_str(mapping["mission_id"], path, "mission_id",
                                    maximum=64),
            decision_id=mapping["decision_id"],
            dispatch_id=mapping["dispatch_id"],
            dispatch_ref=_optional_str(mapping["dispatch_ref"], path,
                                       "dispatch_ref",
                                       maximum=MAX_DISPATCH_REF_LEN),
            stop=_require_str(mapping["stop"], path, "stop",
                              maximum=MAX_STOP_LEN),
            mission_state=state,
            mission_reason=reason,
            dispatched_at=_require_str(mapping["dispatched_at"], path,
                                       "dispatched_at",
                                       maximum=MAX_TIMESTAMP_LEN),
        )


# --- per-node decision --------------------------------------------------------


@dataclass(frozen=True)
class NodeDecision:
    """One node's deterministic scheduling outcome and reason."""

    node_id: str
    priority: int
    m012_state: str
    outcome: str
    reason: str
    demand: NodeDemand

    @property
    def mission_id(self) -> str | None:
        return self.demand.mission_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "priority": self.priority,
            "m012_state": self.m012_state,
            "outcome": self.outcome,
            "reason": self.reason,
            "demand": self.demand.to_dict(),
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> NodeDecision:
        mapping = _require_mapping(doc, path, "node decision required")
        _known_keys(mapping, ("node_id", "priority", "m012_state", "outcome",
                              "reason", "demand"), path)
        _require_keys(mapping, ("node_id", "priority", "m012_state", "outcome",
                                "reason", "demand"), path)
        state = mapping["m012_state"]
        if not isinstance(state, str):
            _fail(E_MALFORMED, path, "m012_state")
        return NodeDecision(
            node_id=_require_str(mapping["node_id"], path, "node_id",
                                 maximum=64),
            priority=_require_int(mapping["priority"], path, "priority",
                                  minimum=graph_model.MIN_PRIORITY,
                                  maximum=graph_model.MAX_PRIORITY),
            m012_state=state,
            outcome=_require_outcome(mapping["outcome"], path),
            reason=_require_reason(mapping["reason"], path),
            demand=NodeDemand.from_dict(mapping["demand"],
                                        f"{path}[demand]"),
        )


# --- scheduling decision ------------------------------------------------------


def _node_list_key(item: NodeDecision) -> tuple[str, str]:
    return (item.node_id, item.outcome)


@dataclass(frozen=True)
class ScheduleDecision:
    """One immutable, deterministic, bounded scheduling decision."""

    schema_version: int
    scheduler_version: str
    goal_id: str
    graph_id: str
    spec_sha256: str
    input_projection_id: str
    policy: SchedulerPolicy
    concurrency_limit: int
    candidate_order: tuple[str, ...]
    admitted: tuple[NodeDecision, ...]
    deferred: tuple[NodeDecision, ...]
    blocked: tuple[NodeDecision, ...]
    active: tuple[NodeDecision, ...]
    completed: tuple[NodeDecision, ...]
    reservations: ReservationSnapshot
    dispatch: tuple[DispatchRecord, ...]
    budget_counters: tuple[tuple[str, int], ...]
    created_at: str
    decision_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        """Deterministic decision payload (excludes clock-only evidence)."""
        return {
            "schema_version": self.schema_version,
            "scheduler_version": self.scheduler_version,
            "goal_id": self.goal_id,
            "graph_id": self.graph_id,
            "spec_sha256": self.spec_sha256,
            "input_projection_id": self.input_projection_id,
            "policy": self.policy.to_dict(),
            "concurrency_limit": self.concurrency_limit,
            "candidate_order": list(self.candidate_order),
            "admitted": [n.to_dict() for n in self.admitted],
            "deferred": [n.to_dict() for n in self.deferred],
            "blocked": [n.to_dict() for n in self.blocked],
            "active": [n.to_dict() for n in self.active],
            "completed": [n.to_dict() for n in self.completed],
            "reservations": self.reservations.to_dict(),
            "dispatch": [d.to_dict() for d in self.dispatch],
            "budget_counters": dict(self.budget_counters),
        }

    def compute_decision_id(self) -> str:
        return sched_identity.decision_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "decision_id": self.decision_id,
            "created_at": self.created_at,
            "counts": self.counts(),
        }

    def counts(self) -> dict[str, int]:
        return {
            "admitted": len(self.admitted),
            "deferred": len(self.deferred),
            "blocked": len(self.blocked),
            "active": len(self.active),
            "completed": len(self.completed),
        }

    @staticmethod
    def build(
        *,
        goal_id: str,
        graph_id: str,
        spec_sha256: str,
        input_projection_id: str,
        policy: SchedulerPolicy,
        concurrency_limit: int,
        candidate_order: Sequence[str],
        admitted: Sequence[NodeDecision],
        deferred: Sequence[NodeDecision],
        blocked: Sequence[NodeDecision],
        active: Sequence[NodeDecision],
        completed: Sequence[NodeDecision],
        reservations: ReservationSnapshot,
        dispatch: Sequence[DispatchRecord],
        budget_counters: Mapping[str, int],
        created_at: str,
    ) -> ScheduleDecision:
        decision = ScheduleDecision(
            schema_version=SCHEMA_VERSION,
            scheduler_version=SCHEDULER_VERSION,
            goal_id=goal_id,
            graph_id=graph_id,
            spec_sha256=spec_sha256,
            input_projection_id=input_projection_id,
            policy=policy,
            concurrency_limit=concurrency_limit,
            candidate_order=tuple(candidate_order),
            admitted=tuple(sorted(admitted, key=_node_list_key)),
            deferred=tuple(sorted(deferred, key=_node_list_key)),
            blocked=tuple(sorted(blocked, key=_node_list_key)),
            active=tuple(sorted(active, key=_node_list_key)),
            completed=tuple(sorted(completed, key=_node_list_key)),
            reservations=reservations,
            dispatch=tuple(sorted(dispatch, key=lambda d: (d.node_id,
                                                           d.dispatch_id))),
            budget_counters=tuple(sorted(budget_counters.items())),
            created_at=created_at,
        )
        return replace(decision, decision_id=decision.compute_decision_id())

    @staticmethod
    def from_dict(doc: object, path: str) -> ScheduleDecision:
        mapping = _require_mapping(doc, path, "decision object required")
        keys = tuple(ScheduleDecision.__dataclass_fields__)
        _known_keys(mapping, (*keys, "created_at", "counts"), path)
        _require_keys(mapping, (*keys, "created_at"), path)
        version = mapping["schema_version"]
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        policy = SchedulerPolicy.from_dict(mapping["policy"],
                                           f"{path}[policy]")
        admitted = tuple(
            NodeDecision.from_dict(item, f"{path}[admitted/{i}]")
            for i, item in enumerate(_require_list(mapping["admitted"], path,
                                                   "admitted"))
        )
        deferred = tuple(
            NodeDecision.from_dict(item, f"{path}[deferred/{i}]")
            for i, item in enumerate(_require_list(mapping["deferred"], path,
                                                   "deferred"))
        )
        blocked = tuple(
            NodeDecision.from_dict(item, f"{path}[blocked/{i}]")
            for i, item in enumerate(_require_list(mapping["blocked"], path,
                                                   "blocked"))
        )
        active = tuple(
            NodeDecision.from_dict(item, f"{path}[active/{i}]")
            for i, item in enumerate(_require_list(mapping["active"], path,
                                                   "active"))
        )
        completed = tuple(
            NodeDecision.from_dict(item, f"{path}[completed/{i}]")
            for i, item in enumerate(_require_list(mapping["completed"], path,
                                                   "completed"))
        )
        _check_unique_identities(
            [*admitted, *deferred, *blocked, *active, *completed], path)
        dispatch = tuple(
            DispatchRecord.from_dict(item, f"{path}[dispatch/{i}]")
            for i, item in enumerate(_require_list(mapping["dispatch"], path,
                                                   "dispatch"))
        )
        reservations = ReservationSnapshot.from_dict(
            mapping["reservations"], f"{path}[reservations]")
        budget_raw = _require_mapping(mapping["budget_counters"], path,
                                      "budget_counters")
        budget_counters = tuple(sorted(
            (str(key), _require_int(value, path, f"budget[{key}]", minimum=0))
            for key, value in budget_raw.items()
        ))
        decision = ScheduleDecision(
            schema_version=SCHEMA_VERSION,
            scheduler_version=_require_str(mapping["scheduler_version"], path,
                                           "scheduler_version", maximum=64),
            goal_id=_require_str(mapping["goal_id"], path, "goal_id",
                                 maximum=64),
            graph_id=_require_str(mapping["graph_id"], path, "graph_id",
                                  maximum=64),
            spec_sha256=_require_str(mapping["spec_sha256"], path,
                                     "spec_sha256", maximum=64),
            input_projection_id=mapping["input_projection_id"],
            policy=policy,
            concurrency_limit=_require_int(
                mapping["concurrency_limit"], path, "concurrency_limit",
                minimum=1, maximum=MAX_GLOBAL_CONCURRENCY),
            candidate_order=tuple(
                _require_str(item, path, "candidate_order", maximum=64)
                for item in _require_list(mapping["candidate_order"], path,
                                          "candidate_order")),
            admitted=admitted,
            deferred=deferred,
            blocked=blocked,
            active=active,
            completed=completed,
            reservations=reservations,
            dispatch=dispatch,
            budget_counters=budget_counters,
            created_at=_require_str(mapping["created_at"], path, "created_at",
                                    maximum=MAX_TIMESTAMP_LEN),
            decision_id=mapping["decision_id"],
        )
        if not sched_identity.is_valid_digest(decision.input_projection_id):
            _fail(E_MALFORMED, path, "input_projection_id")
        if decision.compute_decision_id() != decision.decision_id:
            _fail(E_IDENTITY_MISMATCH, path, decision.decision_id)
        _validate_reservation_accounting(decision, path)
        return decision


def _check_unique_identities(nodes: Sequence[NodeDecision], path: str) -> None:
    seen: set[str] = set()
    for node in nodes:
        if node.node_id in seen:
            _fail(E_DUPLICATE_SELECTION, path, node.node_id)
        seen.add(node.node_id)


def _validate_reservation_accounting(decision: ScheduleDecision,
                                     path: str) -> None:
    current = decision.reservations.current
    seen_nodes: set[str] = set()
    seen_missions: set[str] = set()
    for reservation in current:
        if reservation.node_id in seen_nodes:
            _fail(E_CONTRADICTORY_RESERVATION, path, reservation.node_id)
        if reservation.mission_id in seen_missions:
            _fail(E_CONTRADICTORY_RESERVATION, path, reservation.mission_id)
        seen_nodes.add(reservation.node_id)
        seen_missions.add(reservation.mission_id)
    totals = ReservationTotals.of(current)
    if totals.gpu_slots > decision.policy.gpu_slots:
        _fail(E_IMPOSSIBLE_ACCOUNTING, path, "gpu_slots")
    if totals.gpu_mem_bytes > decision.policy.gpu_mem_bytes:
        _fail(E_IMPOSSIBLE_ACCOUNTING, path, "gpu_mem_bytes")
    if totals.count > decision.policy.global_concurrency:
        _fail(E_IMPOSSIBLE_ACCOUNTING, path, "global_concurrency")
    admitted_totals = ReservationTotals.of(decision.reservations.newly_reserved)
    if admitted_totals.gpu_slots + totals.gpu_slots > decision.policy.gpu_slots:
        _fail(E_IMPOSSIBLE_ACCOUNTING, path,
              "admitted_and_current_gpu_slots")
    if admitted_totals.gpu_mem_bytes + totals.gpu_mem_bytes > \
            decision.policy.gpu_mem_bytes:
        _fail(E_IMPOSSIBLE_ACCOUNTING, path,
              "admitted_and_current_gpu_mem_bytes")
    if admitted_totals.count + totals.count > decision.policy.global_concurrency:
        _fail(E_IMPOSSIBLE_ACCOUNTING, path,
              "admitted_and_current_global_concurrency")


# --- scheduler state ----------------------------------------------------------


@dataclass(frozen=True)
class SchedulerState:
    """Durable, append-only scheduler state for one goal graph."""

    schema_version: int
    goal_id: str
    graph_id: str
    policy: SchedulerPolicy
    dispatch_records: tuple[DispatchRecord, ...]
    dispatch_counters: tuple[tuple[str, int], ...]
    active_node_ids: tuple[str, ...]
    last_decision_id: str | None
    decision_ids: tuple[str, ...]
    updated_at: str

    @property
    def counter_map(self) -> dict[str, int]:
        return dict(self.dispatch_counters)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scheduler_version": SCHEDULER_VERSION,
            "goal_id": self.goal_id,
            "graph_id": self.graph_id,
            "policy": self.policy.to_dict(),
            "dispatch_records": [r.to_dict() for r in self.dispatch_records],
            "dispatch_counters": dict(self.dispatch_counters),
            "active_node_ids": list(self.active_node_ids),
            "last_decision_id": self.last_decision_id,
            "decision_ids": list(self.decision_ids),
            "updated_at": self.updated_at,
        }

    @staticmethod
    def initial(
        *,
        goal_id: str,
        graph_id: str,
        policy: SchedulerPolicy,
        updated_at: str,
    ) -> SchedulerState:
        return SchedulerState(
            schema_version=SCHEMA_VERSION,
            goal_id=goal_id,
            graph_id=graph_id,
            policy=policy,
            dispatch_records=(),
            dispatch_counters=(),
            active_node_ids=(),
            last_decision_id=None,
            decision_ids=(),
            updated_at=updated_at,
        )

    @staticmethod
    def from_dict(doc: object, path: str) -> SchedulerState:
        mapping = _require_mapping(doc, path, "scheduler state required")
        _known_keys(
            mapping,
            ("schema_version", "scheduler_version", "goal_id", "graph_id",
             "policy", "dispatch_records", "dispatch_counters",
             "active_node_ids", "last_decision_id", "decision_ids",
             "updated_at"),
            path)
        _require_keys(
            mapping,
            ("schema_version", "goal_id", "graph_id", "policy",
             "dispatch_records", "dispatch_counters", "active_node_ids",
             "last_decision_id", "decision_ids", "updated_at"),
            path)
        version = mapping["schema_version"]
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        records = tuple(
            DispatchRecord.from_dict(item, f"{path}[dispatch_records/{i}]")
            for i, item in enumerate(_require_list(mapping["dispatch_records"],
                                                   path, "dispatch_records"))
        )
        if len(records) > MAX_DISPATCH_RECORDS:
            _fail(E_OVERFLOW, path, "too many dispatch records")
        seen_dispatch: set[str] = set()
        for record in records:
            if record.dispatch_id in seen_dispatch:
                _fail(E_DISPATCH_EVIDENCE, path,
                      f"duplicate dispatch_id: {record.dispatch_id}")
            seen_dispatch.add(record.dispatch_id)
        counters_raw = _require_mapping(mapping["dispatch_counters"], path,
                                        "dispatch_counters")
        counters = tuple(sorted(
            (str(key), _require_int(value, path, f"counter[{key}]", minimum=0))
            for key, value in counters_raw.items()
        ))
        decision_ids = tuple(
            _require_str(item, path, "decision_ids", maximum=64)
            for item in _require_list(mapping["decision_ids"], path,
                                      "decision_ids"))
        active_node_ids = tuple(
            _require_str(item, path, "active_node_ids", maximum=64)
            for item in _require_list(mapping["active_node_ids"], path,
                                      "active_node_ids"))
        if len(decision_ids) > MAX_DECISIONS:
            _fail(E_OVERFLOW, path, "too many decisions")
        for decision_id in decision_ids:
            if not sched_identity.is_valid_digest(decision_id):
                _fail(E_MALFORMED, path, f"decision_ids:{decision_id}")
        last = mapping["last_decision_id"]
        if last is not None and not sched_identity.is_valid_digest(last):
            _fail(E_MALFORMED, path, "last_decision_id")
        if last is not None and (not decision_ids or decision_ids[-1] != last):
            _fail(E_CONTRADICTORY_RESERVATION, path,
                  "last_decision_id not the newest history entry")
        return SchedulerState(
            schema_version=SCHEMA_VERSION,
            goal_id=_require_str(mapping["goal_id"], path, "goal_id",
                                 maximum=64),
            graph_id=_require_str(mapping["graph_id"], path, "graph_id",
                                  maximum=64),
            policy=SchedulerPolicy.from_dict(mapping["policy"],
                                             f"{path}[policy]"),
            dispatch_records=records,
            dispatch_counters=counters,
            active_node_ids=active_node_ids,
            last_decision_id=last,
            decision_ids=decision_ids,
            updated_at=_require_str(mapping["updated_at"], path, "updated_at",
                                    maximum=MAX_TIMESTAMP_LEN),
        )


@dataclass
class ReconstructedScheduler:
    """Read-only reconstruction of one scheduler's exact persisted state."""

    state: SchedulerState
    latest_decision: ScheduleDecision | None
    decisions: tuple[ScheduleDecision, ...] = field(default_factory=tuple)
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    graph_identity: str | None = None
    spec_identity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "VALID",
            "goal_id": self.state.goal_id,
            "graph_id": self.state.graph_id,
            "policy": self.state.policy.to_dict(),
            "policy_id": self.state.policy.policy_id,
            "last_decision_id": self.state.last_decision_id,
            "decisions": [d.decision_id for d in self.decisions],
            "dispatch_records": [r.to_dict()
                                 for r in self.state.dispatch_records],
            "latest": (None if self.latest_decision is None
                       else self.latest_decision.to_dict()),
            "events": len(self.events),
            "graph_identity": self.graph_identity,
            "spec_identity": self.spec_identity,
        }


# Graph identity helper reused for provenance labels (read-only).
GRAPH_DOMAIN = graph_identity.GRAPH_DOMAIN
SPEC_DOMAIN = graph_identity.SPEC_DOMAIN
