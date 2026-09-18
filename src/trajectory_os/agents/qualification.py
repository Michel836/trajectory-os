"""Mission 016 — DeepSeek Harness qualification over the M015 contract.

M016 exercises the M015 provider-neutral agent-backend contract with one
bounded, structured DeepSeek Harness canary and records deterministic
comparison evidence. It introduces **no second backend**: it uses the
existing :mod:`trajectory_os.agents` adapter/contract unchanged.

Trust rules:

* a bounded structured canary is attempted only when the official SDK and
  runtime probe as available;
* structured lifecycle evidence is authoritative — an errored turn (for
  example a missing credential) is never reinterpreted as success;
* when credentials/runtime are unavailable or incompatible the result is a
  deterministic ``CANARY_UNAVAILABLE`` / ``CANARY_INCOMPATIBLE`` with the
  exact reason, and the proven Pi path remains the fallback;
* no backend performs any Git trust-boundary write and no secret is ever
  logged or persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trajectory_os.agents import model, registry
from trajectory_os.agents.canary import run_canary
from trajectory_os.agents.registry import BackendFactory

#: The bounded, representative canary task (never a full mission, read-only).
DEFAULT_CANARY_TASK = (
    "Reply with exactly the token DSH_CANARY_OK and make no repository "
    "change. Do not run git, create, modify or delete any file."
)

#: Stable qualification statuses (additive over the M015 canary statuses).
QS_QUALIFIED = "QUALIFIED"
QS_UNAVAILABLE = "CANARY_UNAVAILABLE"
QS_INCOMPATIBLE = "CANARY_INCOMPATIBLE"
QS_FAILED = "CANARY_FAILED"


@dataclass(frozen=True)
class QualificationOutcome:
    """One bounded DeepSeek Harness qualification result (deterministic)."""

    status: str
    reason: str
    task: str
    primary_probe: dict[str, Any]
    fallback_probe: dict[str, Any]
    canary: dict[str, Any]
    comparison: dict[str, Any]
    fallback: dict[str, Any]
    git_writes: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "task": self.task,
            "primary_probe": self.primary_probe,
            "fallback_probe": self.fallback_probe,
            "canary": self.canary,
            "comparison": self.comparison,
            "fallback": self.fallback,
            "git_writes": self.git_writes,
        }


def _qualification_status(canary_status: str, reason: str) -> str:
    if canary_status == model.CS_CANARY_PASS:
        return QS_QUALIFIED
    if canary_status == model.CS_CANARY_INCOMPATIBLE:
        return QS_INCOMPATIBLE
    if canary_status == model.CS_CANARY_UNAVAILABLE:
        return QS_UNAVAILABLE
    if reason in (model.R_RPC_ERROR, model.R_INCOMPATIBLE_PROTOCOL,
                  model.R_LAUNCH_FAILED, model.R_TIMEOUT):
        # A protocol/transport-level failure of the developer-preview runtime
        # is an incompatibility, never a general canary failure.
        return QS_INCOMPATIBLE
    return QS_FAILED


def qualify(
    *,
    workspace: str,
    timeout_s: int = 120,
    task: str = DEFAULT_CANARY_TASK,
    factory: BackendFactory | None = None,
    allow_runtime: bool = False,
) -> QualificationOutcome:
    """Run one bounded DeepSeek Harness qualification (never a full mission)."""
    build = factory or (lambda name: registry.make_backend(name))
    probes = {
        name: build(name).probe().to_dict()
        for name in sorted(model.BACKENDS)
    }
    request = model.AgentRequest(
        task=task, workspace=workspace, timeout_s=timeout_s,
        provider=None, model=None).validate()
    require_sdk = not allow_runtime
    outcome = run_canary(
        request, require_sdk=require_sdk, factory=build)
    status = _qualification_status(outcome.status, outcome.reason)
    comparison: dict[str, Any] = dict(outcome.comparison or {})
    comparison["canary_status"] = outcome.status
    comparison["canary_reason"] = outcome.reason
    comparison["structured_lifecycle"] = _structured_lifecycle(comparison)
    turn_error = _turn_error(outcome)
    if turn_error is not None:
        comparison["turn_error"] = turn_error
    fallback = {
        "backend": model.BACKEND_PI,
        "authoritative": True,
        "executed": False,
        "reason": ("PROVEN_PI_FALLBACK"
                   if status != QS_QUALIFIED else "NOT_REQUIRED"),
        "note": ("the proven Pi + DeepSeek Flash path remains authoritative; "
                 "no live fallback model run is required for the "
                 "deterministic goal-proof program"),
    }
    return QualificationOutcome(
        status=status,
        reason=outcome.reason,
        task=task,
        primary_probe=probes[model.BACKEND_DEEPSEEK_HARNESS],
        fallback_probe=probes[model.BACKEND_PI],
        canary=_bounded_canary(outcome),
        comparison=comparison,
        fallback=fallback,
    )


def _bounded_canary(outcome: model.CanaryOutcome) -> dict[str, Any]:
    """A bounded, secret-free summary of one canary outcome.

    Raw event payloads and the model's free-form final response are
    deliberately excluded: only lifecycle kinds/methods, completion
    evidence and bounded identities are recorded, so no prompt, credential
    name or provider error text can leak into evidence.
    """
    primary = outcome.primary
    document: dict[str, Any] = {
        "status": outcome.status,
        "reason": outcome.reason,
        "primary_backend": outcome.primary_backend,
        "fallback_backend": outcome.fallback_backend,
        "canary_id": outcome.canary_id,
    }
    if primary is not None:
        document.update({
            "backend": primary.backend,
            "run_status": primary.status,
            "run_reason": primary.reason,
            "run_id": primary.run_id,
            "session_id": primary.session_id,
            "transport": primary.transport,
            "runtime_ms": primary.runtime_ms,
            "fallback_from": primary.fallback_from,
            "completion": (None if primary.completion is None
                           else primary.completion.to_dict()),
            "error": primary.error,
            "lifecycle": [
                {"sequence": event.sequence, "kind": event.kind,
                 "method": event.method}
                for event in primary.events
            ],
        })
    return document


def _turn_error(outcome: model.CanaryOutcome) -> str | None:
    primary = outcome.primary
    if primary is None:
        return None
    for event in reversed(primary.events):
        payload = event.payload or {}
        code = payload.get("turn_error")
        if isinstance(code, str) and code:
            return code
    return None


def _structured_lifecycle(comparison: dict[str, Any]) -> bool:
    source = comparison.get("completion_source")
    return bool(source) and source in (model.CS_STRUCTURED_RESULT,
                                       model.CS_LIFECYCLE_IDLE)
