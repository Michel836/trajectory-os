"""M022 — bounded, deterministic agent retry composition (fail closed).

Retry is an explicit, bounded policy over the provider-neutral backend
contract. It never reinterprets a failed attempt as success, never retries a
cancellation or an unavailable/incompatible backend, and always returns the
last authoritative result plus the exact attempt ledger.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trajectory_os.agents import model
from trajectory_os.agents.contract import AgentBackend

MAX_ATTEMPTS = 8
DEFAULT_ATTEMPTS = 3

#: Statuses that a bounded retry may re-attempt (never CANCELLED/UNAVAILABLE/
#: INCOMPATIBLE, which are terminal fail-closed states).
RETRYABLE_STATUSES = frozenset({model.RS_TIMEOUT, model.RS_FAILED})

Noop = Callable[[float], None]


def _noop(_seconds: float) -> None:
    return None


@dataclass(frozen=True)
class RetryPolicy:
    """One explicit bounded retry policy (never inferred, never unbounded)."""

    max_attempts: int = DEFAULT_ATTEMPTS
    retry_statuses: tuple[str, ...] = (
        model.RS_TIMEOUT, model.RS_FAILED,
    )
    base_delay_s: float = 0.0

    def validate(self) -> RetryPolicy:
        if isinstance(self.max_attempts, bool) \
                or not isinstance(self.max_attempts, int) \
                or not (1 <= self.max_attempts <= MAX_ATTEMPTS):
            raise model.AgentBackendError(
                "INVALID_RETRY_POLICY", "retry",
                f"max_attempts must be 1..{MAX_ATTEMPTS}")
        if self.base_delay_s < 0 or self.base_delay_s > 3600:
            raise model.AgentBackendError(
                "INVALID_RETRY_POLICY", "retry", "base_delay_s out of bounds")
        for status in self.retry_statuses:
            if status not in RETRYABLE_STATUSES:
                raise model.AgentBackendError(
                    "INVALID_RETRY_POLICY", "retry",
                    f"non-retryable status {status!r}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "retry_statuses": list(self.retry_statuses),
            "base_delay_s": self.base_delay_s,
        }


DEFAULT_POLICY = RetryPolicy()


@dataclass(frozen=True)
class Attempt:
    """One bounded attempt record (evidence, never a proof)."""

    attempt: int
    status: str
    reason: str
    run_id: str | None
    retryable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "status": self.status,
            "reason": self.reason,
            "run_id": self.run_id,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class RetryOutcome:
    """The final authoritative result plus the complete attempt ledger."""

    result: model.AgentResult
    attempts: tuple[Attempt, ...]
    exhausted: bool

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "exhausted": self.exhausted,
            "attempt_count": self.attempt_count,
        }


def _completion_reliable(result: model.AgentResult) -> bool:
    return result.completion is not None and result.completion.reliable


def run_with_retries(
    backend: AgentBackend,
    request: model.AgentRequest,
    policy: RetryPolicy | None = None,
    *,
    cancel: object | None = None,
    sleep: Noop = _noop,
) -> RetryOutcome:
    """Run one bounded retrying session; never invents success."""
    policy = (policy or DEFAULT_POLICY).validate()
    request.validate()
    backend_name = getattr(backend, "name", "unknown")
    attempts: list[Attempt] = []
    if _cancel_requested(cancel):
        result = model.AgentResult.build(
            backend=backend_name, status=model.RS_CANCELLED,
            reason=model.R_CANCELLED, transport=_transport(backend))
        return RetryOutcome(result=result, attempts=(), exhausted=False)

    last: model.AgentResult | None = None
    for index in range(1, policy.max_attempts + 1):
        result = backend.run(request, cancel=cancel)
        retryable = result.status in policy.retry_statuses
        attempts.append(Attempt(
            attempt=index, status=result.status, reason=result.reason,
            run_id=result.run_id or None, retryable=retryable))
        last = result
        if result.completed and _completion_reliable(result):
            return RetryOutcome(result=result, attempts=tuple(attempts),
                                exhausted=False)
        if not retryable:
            return RetryOutcome(result=result, attempts=tuple(attempts),
                                exhausted=False)
        if index == policy.max_attempts:
            break
        if _cancel_requested(cancel):
            cancelled = model.AgentResult.build(
                backend=backend_name, status=model.RS_CANCELLED,
                reason=model.R_CANCELLED, transport=_transport(backend))
            return RetryOutcome(result=cancelled, attempts=tuple(attempts),
                                exhausted=False)
        sleep(policy.base_delay_s * index)
    assert last is not None  # loop always runs at least once
    return RetryOutcome(result=last, attempts=tuple(attempts), exhausted=True)


def _cancel_requested(cancel: object | None) -> bool:
    if cancel is None:
        return False
    is_set = getattr(cancel, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    return False


def _transport(backend: AgentBackend) -> str | None:
    probe = backend.probe()
    return probe.transport
