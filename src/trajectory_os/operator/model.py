"""M040–M047 — self-hosting operator platform: canonical vocabulary (pure).

This package consolidates M017–M039 into one operator product. It introduces
**no** competing lifecycle, readiness, mission identity, release identity,
observability or trust model: ``mission_id == run_id`` remains authoritative
and every fact is derived from the canonical durable artifacts
(``status.json`` / ``closure.json`` / ``release-*.json`` / ``events.jsonl``).

Modules:

* :mod:`trajectory_os.operator.policy`     — M044 deterministic policy layer;
* :mod:`trajectory_os.operator.routing`    — M045 backend/reviewer routing;
* :mod:`trajectory_os.operator.events`     — M043 unified durable events;
* :mod:`trajectory_os.operator.recovery`   — M042 full-lifecycle recovery;
* :mod:`trajectory_os.operator.state`      — M046 one-screen operator state;
* :mod:`trajectory_os.operator.control_plane` — M041 unified control plane;
* :mod:`trajectory_os.operator.dogfood`    — M040 self-hosting dogfood;
* :mod:`trajectory_os.operator.acceptance` — M047 product acceptance matrix.
"""

from __future__ import annotations

from typing import NoReturn

#: Schema version of every operator platform document.
SCHEMA_VERSION = 1

#: Human/machine operator platform version string (additive).
OPERATOR_VERSION = "m047.1"

# --- operator artifact names (additive to the canonical mission root) ---------

POLICY_NAME = "policy.json"
ROUTING_NAME = "routing.json"
ROUTING_HISTORY_NAME = "routing-history.jsonl"
OPERATOR_EVENTS_NAME = "operator-events.jsonl"
LIFECYCLE_RECOVERY_NAME = "lifecycle-recovery.json"
SELF_HOSTING_EVIDENCE_NAME = "self-hosting-evidence.json"

#: Bound on the operator event timeline (newest records retained on read).
MAX_OPERATOR_EVENTS = 4096

# --- stable operator error codes (closed set) ---------------------------------

E_MALFORMED = "MALFORMED_OPERATOR_DOCUMENT"
E_POLICY_PROFILE_UNKNOWN = "POLICY_PROFILE_UNKNOWN"
E_POLICY_INVALID = "INVALID_POLICY_COMBINATION"
E_POLICY_OVERRIDE_INVALID = "POLICY_OVERRIDE_INVALID"
E_POLICY_ENVIRONMENT = "POLICY_ENVIRONMENT_MUTATION_FORBIDDEN"
E_ROUTING_INVALID = "ROUTING_DECISION_INVALID"
E_ROUTING_UNAVAILABLE = "BACKEND_UNAVAILABLE"
E_ROUTING_FALLBACK_DENIED = "FALLBACK_NOT_ALLOWED"
E_ROUTING_PHANTOM_REVIEWER = "PHANTOM_REVIEWER"
E_ROUTING_FINAL_REVIEWER_SWAP = "FINAL_REVIEWER_IDENTITY_CHANGED"
E_RECOVERY_CONTRADICTION = "LOCAL_REMOTE_CONTRADICTION"
E_RECOVERY_MISSING = "RECOVERY_TARGET_MISSING"
E_EVENT_MALFORMED = "MALFORMED_OPERATOR_EVENT"
E_EVENT_VERSION = "UNSUPPORTED_OPERATOR_EVENT_SCHEMA"
E_EVENT_IDENTITY = "OPERATOR_EVENT_IDENTITY_MISMATCH"
E_STATE_MALFORMED = "MALFORMED_OPERATOR_STATE"
E_CONTROL_REFUSED = "CONTROL_REFUSED"

OPERATOR_ERROR_CODES = frozenset({
    E_MALFORMED, E_POLICY_PROFILE_UNKNOWN, E_POLICY_INVALID,
    E_POLICY_OVERRIDE_INVALID, E_POLICY_ENVIRONMENT, E_ROUTING_INVALID,
    E_ROUTING_UNAVAILABLE, E_ROUTING_FALLBACK_DENIED,
    E_ROUTING_PHANTOM_REVIEWER, E_ROUTING_FINAL_REVIEWER_SWAP,
    E_RECOVERY_CONTRADICTION, E_RECOVERY_MISSING, E_EVENT_MALFORMED,
    E_EVENT_VERSION, E_EVENT_IDENTITY, E_STATE_MALFORMED, E_CONTROL_REFUSED,
})


class OperatorError(Exception):
    """A fail-closed operator-platform refusal (stable ``code``)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def fail(code: str, detail: str = "") -> NoReturn:
    raise OperatorError(code, detail)


__all__ = [
    "E_CONTROL_REFUSED",
    "E_EVENT_IDENTITY",
    "E_EVENT_MALFORMED",
    "E_EVENT_VERSION",
    "E_MALFORMED",
    "E_POLICY_ENVIRONMENT",
    "E_POLICY_INVALID",
    "E_POLICY_OVERRIDE_INVALID",
    "E_POLICY_PROFILE_UNKNOWN",
    "E_RECOVERY_CONTRADICTION",
    "E_RECOVERY_MISSING",
    "E_ROUTING_FALLBACK_DENIED",
    "E_ROUTING_FINAL_REVIEWER_SWAP",
    "E_ROUTING_INVALID",
    "E_ROUTING_PHANTOM_REVIEWER",
    "E_ROUTING_UNAVAILABLE",
    "E_STATE_MALFORMED",
    "LIFECYCLE_RECOVERY_NAME",
    "MAX_OPERATOR_EVENTS",
    "OPERATOR_ERROR_CODES",
    "OPERATOR_EVENTS_NAME",
    "OPERATOR_VERSION",
    "POLICY_NAME",
    "ROUTING_HISTORY_NAME",
    "ROUTING_NAME",
    "SCHEMA_VERSION",
    "SELF_HOSTING_EVIDENCE_NAME",
    "OperatorError",
    "fail",
]
