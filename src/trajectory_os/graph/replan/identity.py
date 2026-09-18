"""Mission 015 — canonical adaptive-replanning identity domains (pure).

Adaptive replanning introduces new, domain-separated digests. Each answers a
different question and none is ever compared to a graph/spec digest, a
scheduler digest, a reuse digest or a Mission 010 patch identity:

* ``trajectory-os.graph-replan-trigger.v1`` -> the exact content identity of
  one explicit machine-readable replan trigger (``trigger_id``);
* ``trajectory-os.graph-replan-policy.v1`` -> the identity of the
  deterministic replan policy that gated a plan (``policy_id``);
* ``trajectory-os.graph-replan-plan.v1`` -> the deterministic identity of one
  proposed/validated replan plan (``plan_id``);
* ``trajectory-os.graph-generation.v1`` -> the identity of one immutable
  graph generation (``generation_id``);
* ``trajectory-os.graph-replan-event.v1`` -> the identity of one append-only
  replan event (``event_id``);
* ``trajectory-os.graph-replan-projection.v1`` -> the identity of the
  machine-readable M016-facing replanning projection (``projection_id``).

Canonical bytes are ``domain_utf8 || 0x00 || canonical_json_utf8`` where
``canonical_json`` is ``json.dumps(..., sort_keys=True,
separators=(",", ":"), ensure_ascii=True)``. Identity therefore depends only
on normalized content: never on clocks, filesystem order, dictionary
insertion order or process identity.
"""

from __future__ import annotations

import hashlib
import json

#: Domain id for one explicit replan trigger digest.
TRIGGER_DOMAIN = "trajectory-os.graph-replan-trigger.v1"

#: Domain id for the deterministic replan policy digest.
POLICY_DOMAIN = "trajectory-os.graph-replan-policy.v1"

#: Domain id for one deterministic replan plan digest.
PLAN_DOMAIN = "trajectory-os.graph-replan-plan.v1"

#: Domain id for one immutable graph generation digest.
GENERATION_DOMAIN = "trajectory-os.graph-generation.v1"

#: Domain id for one append-only replan event digest.
EVENT_DOMAIN = "trajectory-os.graph-replan-event.v1"

#: Domain id for the M016-facing replanning projection digest.
PROJECTION_DOMAIN = "trajectory-os.graph-replan-projection.v1"

#: Every replan identity domain id (closed set).
DOMAIN_IDS = frozenset({
    TRIGGER_DOMAIN, POLICY_DOMAIN, PLAN_DOMAIN, GENERATION_DOMAIN,
    EVENT_DOMAIN, PROJECTION_DOMAIN,
})


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def canonical_bytes(domain: str, payload: object) -> bytes:
    """Domain-separated canonical bytes for one replan identity payload."""
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown replan identity domain: {domain!r}")
    return (
        domain.encode("utf-8")
        + b"\x00"
        + canonical_json(payload).encode("utf-8")
    )


def digest(domain: str, payload: object) -> str:
    """SHA-256 hex digest of the domain-separated canonical bytes."""
    return hashlib.sha256(canonical_bytes(domain, payload)).hexdigest()


def is_valid_digest(value: object) -> bool:
    """Strict 64-char lowercase hex digest check (fail closed)."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


def trigger_id(payload: object) -> str:
    return digest(TRIGGER_DOMAIN, payload)


def policy_id(payload: object) -> str:
    return digest(POLICY_DOMAIN, payload)


def plan_id(payload: object) -> str:
    return digest(PLAN_DOMAIN, payload)


def generation_id(payload: object) -> str:
    return digest(GENERATION_DOMAIN, payload)


def event_id(payload: object) -> str:
    return digest(EVENT_DOMAIN, payload)


def projection_id(payload: object) -> str:
    return digest(PROJECTION_DOMAIN, payload)


def identity_refs(*, generation: str, plan: str | None,
                  trigger: str | None) -> dict[str, str | None]:
    """Labeled replan identity block (never cross-compared by domain)."""
    return {
        "domain": GENERATION_DOMAIN,
        "generation_id": generation,
        "plan_id": plan,
        "trigger_id": trigger,
    }
