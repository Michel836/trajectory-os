"""M030 — canonical live-run observability model (pure, fail closed).

This module owns the single canonical status contract consumed by every
operator surface. It is deliberately independent of any specific backend or
frontend: the benchmark runtime (and any future adapter) maps into it.

Two orthogonal state axes are modelled explicitly and must never be
conflated:

* **lifecycle** (``state``/``stage``/``phase``/``attempt``) — where the
  execution process is;
* **readiness** (``readiness``) — whether the produced evidence is safe to
  commit.

A lifecycle ``COMPLETE`` state never implies ``READY_FOR_COMMIT``. The
projection layer renders both, and a success rendering is authorised only by
``readiness == READY_FOR_COMMIT``.

Reviewer identity is role-explicit. Three separate roles are modelled
(implementation agent, inline reviewer, final independent reviewer), each
with its own enabled/active flag. A reviewer that was not actually invoked is
never displayed with a phantom/default model: ``display_model`` is ``None``
for an inactive role.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn

#: Schema version of every canonical observability document.
CANONICAL_SCHEMA_VERSION = 1

#: Human/machine observability version string (additive).
OBSERVABILITY_VERSION = "m030.1"

# --- telemetry modes (closed set) --------------------------------------------

TELEMETRY_OFF = "off"
TELEMETRY_STANDARD = "standard"
TELEMETRY_BENCHMARK = "benchmark"

TELEMETRY_MODES = frozenset({
    TELEMETRY_OFF, TELEMETRY_STANDARD, TELEMETRY_BENCHMARK,
})

#: Modes that sample local resources at all.
RESOURCE_MODES = frozenset({TELEMETRY_BENCHMARK})

# --- lifecycle states (closed set) -------------------------------------------

LC_PENDING = "PENDING"
LC_PREFLIGHT = "PREFLIGHT"
LC_RUNNING = "RUNNING"
LC_IMPLEMENTING = "IMPLEMENTING"
LC_VALIDATING = "VALIDATING"
LC_REVIEWING = "REVIEWING"
LC_REPAIRING = "REPAIRING"
LC_COMPLETE = "COMPLETE"

LIFECYCLE_STATES = frozenset({
    LC_PENDING, LC_PREFLIGHT, LC_RUNNING, LC_IMPLEMENTING, LC_VALIDATING,
    LC_REVIEWING, LC_REPAIRING, LC_COMPLETE,
})

#: Lifecycle states that end the execution process.
TERMINAL_LIFECYCLE_STATES = frozenset({LC_COMPLETE})

# --- readiness (closed set) ---------------------------------------------------

RD_INDETERMINATE = "INDETERMINATE"
RD_READY_FOR_COMMIT = "READY_FOR_COMMIT"
RD_BLOCKED = "BLOCKED"
RD_FAILED = "FAILED"
RD_CANCELLED = "CANCELLED"
RD_UNKNOWN = "UNKNOWN"

READINESS_STATES = frozenset({
    RD_INDETERMINATE, RD_READY_FOR_COMMIT, RD_BLOCKED, RD_FAILED,
    RD_CANCELLED, RD_UNKNOWN,
})

#: Readiness outcomes that end follow mode.
TERMINAL_READINESS_STATES = frozenset({
    RD_READY_FOR_COMMIT, RD_BLOCKED, RD_FAILED, RD_CANCELLED,
})

# --- stages / phases (closed set) --------------------------------------------

STAGE_PREFLIGHT = "PREFLIGHT"
STAGE_EXECUTE = "EXECUTE"
STAGE_VALIDATE = "VALIDATE"
STAGE_REVIEW = "REVIEW"
STAGE_REPAIR = "REPAIR"
STAGE_AGGREGATE = "AGGREGATE"
STAGE_DONE = "DONE"

STAGES = frozenset({
    STAGE_PREFLIGHT, STAGE_EXECUTE, STAGE_VALIDATE, STAGE_REVIEW,
    STAGE_REPAIR, STAGE_AGGREGATE, STAGE_DONE,
})

# --- reviewer roles (closed set) ---------------------------------------------

ROLE_IMPLEMENTATION_AGENT = "IMPLEMENTATION_AGENT"
ROLE_INLINE_REVIEWER = "INLINE_REVIEWER"
ROLE_FINAL_INDEPENDENT_REVIEWER = "FINAL_INDEPENDENT_REVIEWER"

ACTOR_ROLES = frozenset({
    ROLE_IMPLEMENTATION_AGENT, ROLE_INLINE_REVIEWER,
    ROLE_FINAL_INDEPENDENT_REVIEWER,
})

#: The active final independent reviewer model for M030 (never a default).
FINAL_REVIEWER_MODEL = "qwen3.8:27b-q4_K_M"

#: The inline reviewer model (implementation-adjacent, often disabled).
INLINE_REVIEWER_MODEL = "qwen3.8:27b-q4_K_M"

#: The legacy reviewer that must never appear unless genuinely active.
LEGACY_PHANTOM_REVIEWER = "qwen3.6"

# --- gates / results (closed set) --------------------------------------------

GATE_PREFLIGHT = "PREFLIGHT"
GATE_VALIDATION = "VALIDATION"
GATE_REVIEW = "REVIEW"
GATE_NONE = "NONE"

GATES = frozenset({GATE_PREFLIGHT, GATE_VALIDATION, GATE_REVIEW, GATE_NONE})

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_REJECT = "REJECT"
RESULT_BLOCKED = "BLOCKED"
RESULT_INACTIVE = "INACTIVE"
RESULT_NA = "N/A"

RESULTS = frozenset({
    RESULT_PASS, RESULT_FAIL, RESULT_REJECT, RESULT_BLOCKED,
    RESULT_INACTIVE, RESULT_NA,
})

# --- stable reason codes ------------------------------------------------------

R_OK = "OK"
R_PREFLIGHT_REJECTED = "PREFLIGHT_REJECTED"
R_INVALID_MODEL_PROVIDER = "INVALID_MODEL_PROVIDER"
R_VALIDATION_FAILED = "VALIDATION_FAILED"
R_REVIEW_REJECTED = "REVIEW_REJECTED"
R_REVIEW_INACTIVE = "REVIEW_INACTIVE"
R_BLOCKED = "BLOCKED"
R_FAILED = "FAILED"
R_CANCELLED = "CANCELLED"
R_COMPLETE = "COMPLETE"
R_UNKNOWN = "UNKNOWN"
R_NOT_STARTED = "NOT_STARTED"

# --- metric provenance (closed set) ------------------------------------------

SRC_PROVIDER = "PROVIDER"
SRC_DERIVED = "DERIVED"
SRC_LOCAL = "LOCAL"
SRC_UNAVAILABLE = "UNAVAILABLE"

SOURCES = frozenset({SRC_PROVIDER, SRC_DERIVED, SRC_LOCAL, SRC_UNAVAILABLE})


class ObservabilityError(Exception):
    """A malformed or untrusted observability document (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise ObservabilityError(code, detail)


# --- metric -------------------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    """One telemetry value with explicit provenance.

    ``source == UNAVAILABLE`` requires ``value is None`` and a non-empty
    ``reason``. Any other source requires a non-null value. That invariant is
    enforced here so no downstream surface can display a fabricated number.
    """

    name: str
    value: int | float | str | None
    source: str
    reason: str = ""

    def validate(self) -> Metric:
        if self.source not in SOURCES:
            _fail("MALFORMED_METRIC", f"{self.name} source {self.source!r}")
        if self.source == SRC_UNAVAILABLE:
            if self.value is not None:
                _fail("MALFORMED_METRIC",
                      f"{self.name} is UNAVAILABLE but has a value")
            if not self.reason:
                _fail("MALFORMED_METRIC",
                      f"{self.name} is UNAVAILABLE without a reason")
        elif self.value is None:
            _fail("MALFORMED_METRIC",
                  f"{self.name} has source {self.source} but no value")
        return self

    @staticmethod
    def unavailable(name: str, reason: str) -> Metric:
        return Metric(name=name, value=None, source=SRC_UNAVAILABLE,
                      reason=reason).validate()

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "source": self.source,
                "reason": (self.reason or None)}


# --- reviewer identity --------------------------------------------------------


@dataclass(frozen=True)
class ReviewerStatus:
    """One reviewer role's configured/observed identity (role-explicit).

    ``enabled`` is the operator configuration; ``active`` is the observed
    fact that this role actually produced the outcome. Only an enabled and
    active role may expose a model via :attr:`display_model`; a configured
    but inactive role renders ``None`` so no phantom/default reviewer leaks
    into any surface.
    """

    role: str
    enabled: bool
    active: bool
    backend: str | None
    provider: str | None
    model: str | None
    reason: str

    def validate(self) -> ReviewerStatus:
        if self.role not in ACTOR_ROLES:
            _fail("MALFORMED_REVIEWER", f"role {self.role!r}")
        if self.active and not self.enabled:
            _fail("MALFORMED_REVIEWER",
                  f"{self.role} is active but not enabled")
        if self.active and not self.model:
            _fail("MALFORMED_REVIEWER",
                  f"{self.role} is active without a model")
        return self

    @property
    def display_model(self) -> str | None:
        """The model safe to display: only for an enabled, active role."""
        return self.model if (self.enabled and self.active) else None

    @property
    def display(self) -> bool:
        return self.enabled and self.active

    @staticmethod
    def disabled(role: str, *,
                 reason: str = "reviewer disabled by operator") -> ReviewerStatus:
        return ReviewerStatus(
            role=role, enabled=False, active=False, backend=None,
            provider=None, model=None, reason=reason).validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "enabled": self.enabled,
            "active": self.active,
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "display_model": self.display_model,
            "display": self.display,
            "reason": self.reason,
        }


# --- canonical event ----------------------------------------------------------


def _stable_id(payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CanonicalEvent:
    """One append-only canonical event (a projection of durable state)."""

    run_id: str
    sequence: int
    kind: str
    at: str
    stage: str
    phase: str
    attempt: int
    gate: str = GATE_NONE
    result: str = RESULT_NA
    patch: str | None = None
    actor: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "at": self.at,
            "stage": self.stage,
            "phase": self.phase,
            "attempt": self.attempt,
            "gate": self.gate,
            "result": self.result,
            "patch": self.patch,
            "actor": self.actor,
            "detail": dict(sorted(self.detail.items())),
        }

    def compute_event_id(self) -> str:
        return _stable_id(self.identity_payload())

    @staticmethod
    def build(**kwargs: Any) -> CanonicalEvent:
        base = CanonicalEvent(event_id="", **kwargs)
        if base.stage not in STAGES:
            _fail("MALFORMED_EVENT", f"stage {base.stage!r}")
        if base.gate not in GATES:
            _fail("MALFORMED_EVENT", f"gate {base.gate!r}")
        if base.result not in RESULTS:
            _fail("MALFORMED_EVENT", f"result {base.result!r}")
        if base.sequence < 0:
            _fail("MALFORMED_EVENT", "negative sequence")
        return replace(base, event_id=base.compute_event_id())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "event_id": self.event_id}


# --- preflight ----------------------------------------------------------------


@dataclass(frozen=True)
class PreflightResult:
    """Fail-fast preflight outcome (knowable errors before expensive phases).

    ``ok is False`` carries an explicit ``reason`` and the explicit
    ``state``/``readiness`` the run must adopt; the orchestrator must then
    stop before validation/review/repair.
    """

    ok: bool
    reason: str
    detail: str
    state: str
    readiness: str
    backend: str | None = None
    provider: str | None = None
    model: str | None = None
    checks: tuple[Mapping[str, Any], ...] = ()

    def validate(self) -> PreflightResult:
        if self.state not in LIFECYCLE_STATES:
            _fail("MALFORMED_PREFLIGHT", f"state {self.state!r}")
        if self.readiness not in READINESS_STATES:
            _fail("MALFORMED_PREFLIGHT", f"readiness {self.readiness!r}")
        if not self.ok and self.readiness == RD_READY_FOR_COMMIT:
            _fail("MALFORMED_PREFLIGHT",
                  "a rejected preflight cannot be READY_FOR_COMMIT")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "state": self.state,
            "readiness": self.readiness,
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "checks": [dict(check) for check in self.checks],
        }


# --- canonical status ---------------------------------------------------------


@dataclass(frozen=True)
class CanonicalStatus:
    """The authoritative run status consumed by every operator surface."""

    run_id: str
    state: str
    stage: str
    phase: str
    attempt: int
    current_backend: str | None
    current_provider: str | None
    current_model: str | None
    inline_review_enabled: bool
    inline_reviewer: ReviewerStatus
    final_review_enabled: bool
    final_reviewer: ReviewerStatus
    previous_gate: str | None
    previous_result: str | None
    reviewed_patch: str | None
    current_patch: str | None
    next_action: str
    last_meaningful_event_at: str | None
    heartbeat_at: str | None
    terminal_reason: str | None
    readiness: str
    current: str | None = None
    next: str | None = None
    terminal_reason_code: str | None = None
    telemetry_mode: str = TELEMETRY_STANDARD
    telemetry: Mapping[str, Any] | None = None
    updated_at: str | None = None
    schema_version: int = CANONICAL_SCHEMA_VERSION
    observability_version: str = OBSERVABILITY_VERSION

    def validate(self) -> CanonicalStatus:
        if not self.run_id:
            _fail("MALFORMED_STATUS", "run_id required")
        if self.state not in LIFECYCLE_STATES:
            _fail("MALFORMED_STATUS", f"state {self.state!r}")
        if self.stage not in STAGES:
            _fail("MALFORMED_STATUS", f"stage {self.stage!r}")
        if self.readiness not in READINESS_STATES:
            _fail("MALFORMED_STATUS", f"readiness {self.readiness!r}")
        if self.attempt < 0:
            _fail("MALFORMED_STATUS", "negative attempt")
        if self.telemetry_mode not in TELEMETRY_MODES:
            _fail("MALFORMED_STATUS",
                  f"telemetry_mode {self.telemetry_mode!r}")
        self.inline_reviewer.validate()
        self.final_reviewer.validate()
        # Lifecycle completion never implies readiness.
        if (self.readiness == RD_READY_FOR_COMMIT
                and self.state != LC_COMPLETE):
            _fail("MALFORMED_STATUS",
                  "READY_FOR_COMMIT requires lifecycle COMPLETE")
        return self

    @property
    def terminal(self) -> bool:
        """True once the run can no longer progress without operator action."""
        return (self.state in TERMINAL_LIFECYCLE_STATES
                or self.readiness in TERMINAL_READINESS_STATES)

    @property
    def success(self) -> bool:
        """Success is authorised by readiness alone, never by lifecycle."""
        return self.readiness == RD_READY_FOR_COMMIT

    @property
    def ready_for_commit(self) -> bool:
        return self.readiness == RD_READY_FOR_COMMIT

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observability_version": self.observability_version,
            "run_id": self.run_id,
            "state": self.state,
            "stage": self.stage,
            "phase": self.phase,
            "attempt": self.attempt,
            "current": self.current,
            "next": self.next,
            "current_backend": self.current_backend,
            "current_provider": self.current_provider,
            "current_model": self.current_model,
            "inline_review_enabled": self.inline_review_enabled,
            "inline_reviewer": self.inline_reviewer.to_dict(),
            "final_review_enabled": self.final_review_enabled,
            "final_reviewer": self.final_reviewer.to_dict(),
            "previous_gate": self.previous_gate,
            "previous_result": self.previous_result,
            "reviewed_patch": self.reviewed_patch,
            "current_patch": self.current_patch,
            "next_action": self.next_action,
            "last_meaningful_event_at": self.last_meaningful_event_at,
            "heartbeat_at": self.heartbeat_at,
            "terminal_reason": self.terminal_reason,
            "terminal_reason_code": self.terminal_reason_code,
            "readiness": self.readiness,
            "ready_for_commit": self.ready_for_commit,
            "terminal": self.terminal,
            "success": self.success,
            "telemetry_mode": self.telemetry_mode,
            "telemetry": (None if self.telemetry is None
                          else dict(self.telemetry)),
            "updated_at": self.updated_at,
        }

    @staticmethod
    def build(**kwargs: Any) -> CanonicalStatus:
        return CanonicalStatus(**kwargs).validate()


# --- derivation helpers -------------------------------------------------------


_NEXT_ACTIONS: Mapping[str, str] = {
    LC_PENDING: "run preflight",
    LC_PREFLIGHT: "run preflight",
    LC_RUNNING: "await runtime progress",
    LC_IMPLEMENTING: "await implementation result",
    LC_VALIDATING: "run deterministic validation",
    LC_REVIEWING: "run final independent review",
    LC_REPAIRING: "apply review findings and re-implement",
}


def next_action_for(state: str, readiness: str) -> str:
    """Deterministic CURRENT->NEXT guidance for every canonical state."""
    if state == LC_COMPLETE or readiness in TERMINAL_READINESS_STATES:
        return {
            RD_READY_FOR_COMMIT: "operator: commit the reviewed patch",
            RD_BLOCKED: "operator: resolve blocking findings",
            RD_FAILED: "operator: inspect failure evidence",
            RD_CANCELLED: "operator: resume or abandon the run",
        }.get(readiness, "operator: inspect terminal evidence")
    return _NEXT_ACTIONS.get(state, "inspect run state")


def derive_readiness(
    *,
    lifecycle_state: str,
    trial_results: Sequence[str] = (),
    cancelled: bool = False,
    blocked: bool = False,
    failed: bool = False,
) -> str:
    """Deterministic readiness from recorded evidence (fail closed).

    Lifecycle completion is necessary but never sufficient for
    ``READY_FOR_COMMIT``: a completed run with a blocked/failed/cancelled
    result stays blocked/failed/cancelled.
    """
    if cancelled:
        return RD_CANCELLED
    if failed or RESULT_FAIL in trial_results:
        return RD_FAILED
    if blocked or RESULT_BLOCKED in trial_results:
        return RD_BLOCKED
    if lifecycle_state in TERMINAL_LIFECYCLE_STATES:
        if trial_results and all(
                result in (RESULT_PASS, RESULT_NA, RESULT_INACTIVE)
                for result in trial_results):
            return RD_READY_FOR_COMMIT
        if not trial_results:
            return RD_UNKNOWN
        return RD_BLOCKED
    return RD_INDETERMINATE
