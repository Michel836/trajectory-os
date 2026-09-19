"""Mission 015 — bounded agent-backend domain model (pure, fail closed).

This module owns the Trajectory_OS-owned contract that every agent backend
(``pi`` and ``deepseek-harness``) must satisfy. It is deliberately provider
agnostic and performs no I/O: launch, session identity, lifecycle/status,
structured events, final result, cancellation/timeout, completion evidence,
and error/fallback provenance are all represented as bounded value objects.

Design invariants (ADR-014):

* the core depends on this contract, never on a specific harness/SDK;
* structured lifecycle/result evidence is the primary completion proof for
  the DeepSeek Harness backend; free-form stdout grep is never the primary
  proof when structured evidence exists;
* a backend is represented by an explicit capability probe, so an unavailable
  or incompatible harness yields a deterministic result instead of a crash;
* provider-specific model naming is adapter-owned and never globally
  rewritten.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NoReturn

from trajectory_os.agents import identity as agent_identity

#: Schema version of every agent-backend document.
SCHEMA_VERSION = 1

#: Human/machine agent-backend version string (additive, never a digest).
AGENT_VERSION = "m015.1"

# --- backends (closed set) ----------------------------------------------------

BACKEND_PI = "pi"
BACKEND_DEEPSEEK_HARNESS = "deepseek-harness"

BACKENDS = frozenset({BACKEND_PI, BACKEND_DEEPSEEK_HARNESS})

# --- transports (closed set) --------------------------------------------------

TRANSPORT_SDK = "sdk"
TRANSPORT_RUNTIME = "runtime"
TRANSPORT_SUBPROCESS = "subprocess"

TRANSPORTS = frozenset({TRANSPORT_SDK, TRANSPORT_RUNTIME, TRANSPORT_SUBPROCESS})

# --- bounded limits -----------------------------------------------------------

MAX_TASK_LEN = 131072
MAX_DETAIL_LEN = 1024
MAX_EVENTS = 8192
MAX_PAYLOAD_BYTES = 65536

# --- run statuses (closed set) ------------------------------------------------

RS_COMPLETED = "COMPLETED"
RS_FAILED = "FAILED"
RS_TIMEOUT = "TIMEOUT"
RS_CANCELLED = "CANCELLED"
RS_UNAVAILABLE = "UNAVAILABLE"
RS_INCOMPATIBLE = "INCOMPATIBLE"

RUN_STATUSES = frozenset({
    RS_COMPLETED, RS_FAILED, RS_TIMEOUT, RS_CANCELLED, RS_UNAVAILABLE,
    RS_INCOMPATIBLE,
})

# --- lifecycle event kinds (closed set) ---------------------------------------

LK_STARTED = "STARTED"
LK_INITIALIZED = "INITIALIZED"
LK_SESSION_OPENED = "SESSION_OPENED"
LK_RUNNING = "RUNNING"
LK_IDLE = "IDLE"
LK_MESSAGE = "MESSAGE"
LK_RESULT = "RESULT"
LK_SUBAGENT_STARTED = "SUBAGENT_STARTED"
LK_SUBAGENT_FINISHED = "SUBAGENT_FINISHED"
LK_ERROR = "ERROR"
LK_CANCELLED = "CANCELLED"
LK_TIMEOUT = "TIMEOUT"
LK_COMPLETED = "COMPLETED"
LK_SHUTDOWN = "SHUTDOWN"

EVENT_KINDS = frozenset({
    LK_STARTED, LK_INITIALIZED, LK_SESSION_OPENED, LK_RUNNING, LK_IDLE,
    LK_MESSAGE, LK_RESULT, LK_SUBAGENT_STARTED, LK_SUBAGENT_FINISHED,
    LK_ERROR, LK_CANCELLED, LK_TIMEOUT, LK_COMPLETED, LK_SHUTDOWN,
})

# --- completion-evidence sources (closed set) ---------------------------------

CS_STRUCTURED_RESULT = "STRUCTURED_RESULT"
CS_LIFECYCLE_IDLE = "LIFECYCLE_IDLE"
CS_EXIT_CODE_MARKER = "EXIT_CODE_MARKER"
CS_NONE = "NONE"

EVIDENCE_SOURCES = frozenset({
    CS_STRUCTURED_RESULT, CS_LIFECYCLE_IDLE, CS_EXIT_CODE_MARKER, CS_NONE,
})

# --- stable, machine-readable reason codes ------------------------------------

R_OK = "BACKEND_OK"
R_SDK_MISSING = "SDK_MISSING"
R_RUNTIME_MISSING = "RUNTIME_MISSING"
R_CREDENTIALS_MISSING = "CREDENTIALS_MISSING"
R_PROFILE_MISSING = "PROFILE_MISSING"
R_INCOMPATIBLE_PROTOCOL = "INCOMPATIBLE_PROTOCOL"
R_LAUNCH_FAILED = "LAUNCH_FAILED"
R_RPC_ERROR = "RPC_ERROR"
R_TIMEOUT = "TIMEOUT"
R_CANCELLED = "CANCELLED"
R_BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
R_CANARY_UNAVAILABLE = "CANARY_UNAVAILABLE"
R_CANARY_INCOMPATIBLE = "CANARY_INCOMPATIBLE"
R_CANARY_PASS = "CANARY_PASS"
R_CANARY_FAIL = "CANARY_FAIL"
R_FALLBACK_USED = "FALLBACK_USED"
R_REVIEW_PENDING = "REVIEW_PENDING"
R_CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
R_ROUTE_INVALID = "ROUTE_INVALID"
R_RETRY_EXHAUSTED = "RETRY_EXHAUSTED"

REASON_CODES = frozenset({
    R_OK, R_SDK_MISSING, R_RUNTIME_MISSING, R_CREDENTIALS_MISSING,
    R_PROFILE_MISSING, R_INCOMPATIBLE_PROTOCOL, R_LAUNCH_FAILED, R_RPC_ERROR,
    R_TIMEOUT, R_CANCELLED, R_BACKEND_UNAVAILABLE, R_CANARY_UNAVAILABLE,
    R_CANARY_INCOMPATIBLE, R_CANARY_PASS, R_CANARY_FAIL, R_FALLBACK_USED,
    R_REVIEW_PENDING, R_CAPABILITY_UNKNOWN, R_ROUTE_INVALID,
    R_RETRY_EXHAUSTED,
})

# --- canary statuses (closed set) ---------------------------------------------

CS_CANARY_PASS = "CANARY_PASS"
CS_CANARY_PASS_WITH_FALLBACK = "CANARY_PASS_WITH_FALLBACK"
CS_CANARY_UNAVAILABLE = "CANARY_UNAVAILABLE"
CS_CANARY_INCOMPATIBLE = "CANARY_INCOMPATIBLE"
CS_CANARY_FAIL = "CANARY_FAIL"

CANARY_STATUSES = frozenset({
    CS_CANARY_PASS, CS_CANARY_PASS_WITH_FALLBACK, CS_CANARY_UNAVAILABLE,
    CS_CANARY_INCOMPATIBLE, CS_CANARY_FAIL,
})


class AgentBackendError(Exception):
    """An agent-backend contract violation (fail closed)."""

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(f"{code}: {path}" + (f" ({detail})" if detail else ""))
        self.code = code
        self.path = path
        self.detail = detail


def _fail(code: str, path: str, detail: str = "") -> NoReturn:
    raise AgentBackendError(code, path, detail)


def _require_str(value: object, path: str, *, maximum: int,
                 optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        _fail("MALFORMED_AGENT", path, "non-empty string required")
    if len(value) > maximum:
        _fail("MALFORMED_AGENT", path, f"exceeds {maximum} chars")
    return value


# --- request ------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRequest:
    """One bounded, provider-agnostic agent run request."""

    task: str
    workspace: str
    prompt_file: str | None = None
    mode: str = "IMPLEMENT"
    timeout_s: int = 900
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None

    def validate(self) -> AgentRequest:
        if not self.task.strip():
            _fail("MALFORMED_AGENT", "request", "task required")
        if len(self.task) > MAX_TASK_LEN:
            _fail("MALFORMED_AGENT", "request", "task too large")
        if self.timeout_s < 1 or self.timeout_s > 86400:
            _fail("MALFORMED_AGENT", "request", "timeout out of bounds")
        if self.max_tokens is not None and not (
                1 <= self.max_tokens <= 10_000_000):
            _fail("MALFORMED_AGENT", "request", "max_tokens out of bounds")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "workspace": self.workspace,
            "prompt_file": self.prompt_file,
            "mode": self.mode,
            "timeout_s": self.timeout_s,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
        }


# --- structured event ---------------------------------------------------------


@dataclass(frozen=True)
class AgentEvent:
    """One bounded, structured lifecycle/protocol event from a backend."""

    sequence: int
    kind: str
    method: str
    session_id: str | None = None
    payload: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "method": self.method,
            "session_id": self.session_id,
            "payload": None if self.payload is None else dict(self.payload),
        }

    @staticmethod
    def build(*, sequence: int, kind: str, method: str,
              session_id: str | None = None,
              payload: Mapping[str, Any] | None = None) -> AgentEvent:
        if kind not in EVENT_KINDS:
            _fail("MALFORMED_AGENT", "event", f"unknown kind: {kind!r}")
        return AgentEvent(sequence=sequence, kind=kind, method=method,
                          session_id=session_id, payload=payload)


# --- completion evidence ------------------------------------------------------


@dataclass(frozen=True)
class CompletionEvidence:
    """Explicit completion proof classification (never inferred from prose)."""

    source: str
    reliable: bool
    detail: str
    message_id: str | None = None
    stop_reason: str | None = None
    evidence_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "reliable": self.reliable,
            "detail": self.detail,
            "message_id": self.message_id,
            "stop_reason": self.stop_reason,
        }

    def compute_evidence_id(self) -> str:
        return agent_identity.evidence_id(self.identity_payload())

    @staticmethod
    def build(*, source: str, reliable: bool, detail: str,
              message_id: str | None = None,
              stop_reason: str | None = None) -> CompletionEvidence:
        if source not in EVIDENCE_SOURCES:
            _fail("MALFORMED_AGENT", "completion", f"unknown source {source!r}")
        evidence = CompletionEvidence(
            source=source, reliable=reliable, detail=detail,
            message_id=message_id, stop_reason=stop_reason)
        return replace(evidence,
                       evidence_id=evidence.compute_evidence_id())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "evidence_id": self.evidence_id}


# --- probe --------------------------------------------------------------------


@dataclass(frozen=True)
class BackendProbe:
    """One deterministic backend capability probe."""

    backend: str
    available: bool
    reason: str
    detail: str = ""
    transport: str | None = None
    sdk_version: str | None = None
    runtime_version: str | None = None

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "backend": self.backend,
            "available": self.available,
            "reason": self.reason,
            "transport": self.transport,
            "sdk_version": self.sdk_version,
            "runtime_version": self.runtime_version,
        }

    def compute_probe_id(self) -> str:
        return agent_identity.probe_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "detail": self.detail,
                "probe_id": self.compute_probe_id()}


# --- result -------------------------------------------------------------------


@dataclass(frozen=True)
class AgentResult:
    """One bounded agent run result with explicit completion evidence."""

    backend: str
    status: str
    reason: str
    events: tuple[AgentEvent, ...] = ()
    session_id: str | None = None
    exit_code: int | None = None
    final_response: str | None = None
    completion: CompletionEvidence | None = None
    error: str | None = None
    fallback_from: str | None = None
    transport: str | None = None
    runtime_ms: int | None = None
    run_id: str = ""

    @property
    def completed(self) -> bool:
        return self.status == RS_COMPLETED

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "agent_version": AGENT_VERSION,
            "backend": self.backend,
            "status": self.status,
            "reason": self.reason,
            "session_id": self.session_id,
            "exit_code": self.exit_code,
            "final_response": self.final_response,
            "completion": (None if self.completion is None
                           else self.completion.identity_payload()),
            "error": self.error,
            "fallback_from": self.fallback_from,
            "transport": self.transport,
            "events": [e.to_dict() for e in self.events],
        }

    def compute_run_id(self) -> str:
        return agent_identity.run_id(self.identity_payload())

    @staticmethod
    def build(*, backend: str, status: str, reason: str,
              events: Sequence[AgentEvent] = (),
              session_id: str | None = None,
              exit_code: int | None = None,
              final_response: str | None = None,
              completion: CompletionEvidence | None = None,
              error: str | None = None,
              fallback_from: str | None = None,
              transport: str | None = None,
              runtime_ms: int | None = None) -> AgentResult:
        if status not in RUN_STATUSES:
            _fail("MALFORMED_AGENT", "result", f"unknown status {status!r}")
        if reason not in REASON_CODES:
            _fail("MALFORMED_AGENT", "result", f"unknown reason {reason!r}")
        result = AgentResult(
            backend=backend, status=status, reason=reason,
            events=tuple(events), session_id=session_id, exit_code=exit_code,
            final_response=final_response, completion=completion,
            error=error, fallback_from=fallback_from, transport=transport,
            runtime_ms=runtime_ms)
        return replace(result, run_id=result.compute_run_id())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "run_id": self.run_id,
            "runtime_ms": self.runtime_ms,
        }


# --- canary -------------------------------------------------------------------


@dataclass(frozen=True)
class CanaryOutcome:
    """One bounded canary comparison outcome with deterministic fallback."""

    status: str
    reason: str
    primary_backend: str
    fallback_backend: str | None = None
    primary: AgentResult | None = None
    fallback: AgentResult | None = None
    comparison: Mapping[str, Any] | None = None
    canary_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "agent_version": AGENT_VERSION,
            "status": self.status,
            "reason": self.reason,
            "primary_backend": self.primary_backend,
            "fallback_backend": self.fallback_backend,
            "primary_run_id": (None if self.primary is None
                               else self.primary.run_id),
            "fallback_run_id": (None if self.fallback is None
                                else self.fallback.run_id),
            "comparison": (None if self.comparison is None
                           else dict(self.comparison)),
        }

    def compute_canary_id(self) -> str:
        return agent_identity.canary_id(self.identity_payload())

    @staticmethod
    def build(*, status: str, reason: str, primary_backend: str,
              fallback_backend: str | None = None,
              primary: AgentResult | None = None,
              fallback: AgentResult | None = None,
              comparison: Mapping[str, Any] | None = None) -> CanaryOutcome:
        if status not in CANARY_STATUSES:
            _fail("MALFORMED_AGENT", "canary", f"unknown status {status!r}")
        outcome = CanaryOutcome(
            status=status, reason=reason, primary_backend=primary_backend,
            fallback_backend=fallback_backend, primary=primary,
            fallback=fallback, comparison=comparison)
        return replace(outcome, canary_id=outcome.compute_canary_id())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "canary_id": self.canary_id,
            "primary": None if self.primary is None else self.primary.to_dict(),
            "fallback": (None if self.fallback is None
                         else self.fallback.to_dict()),
        }
