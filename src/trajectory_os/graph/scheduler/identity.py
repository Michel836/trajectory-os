"""Mission 013 — canonical portfolio-scheduler identity domains (pure).

Four domain-separated digests are produced by the scheduler. Each answers a
different question and none is ever compared to a graph/spec digest or to a
Mission 010 patch identity:

* ``trajectory-os.portfolio-scheduler-policy.v1`` -> the configured
  capacity policy digest (``policy_id``);
* ``trajectory-os.portfolio-scheduler-projection.v1`` -> the identity of the
  exact scheduling input projection (graph + readiness + mission runtime
  evidence + policy + reservation snapshot) that a decision was computed
  from (``input_projection_id``);
* ``trajectory-os.portfolio-scheduler-decision.v1`` -> the deterministic
  identity of one scheduling decision (``decision_id``);
* ``trajectory-os.portfolio-scheduler-dispatch.v1`` -> the identity of one
  dispatch attempt of one admitted node (``dispatch_id``).

Canonical bytes are ``domain_utf8 || 0x00 || canonical_json_utf8`` where
``canonical_json`` is ``json.dumps(..., sort_keys=True,
separators=(",", ":"), ensure_ascii=True)``. Digest identity therefore
depends only on normalized content: never on clocks, filesystem order,
dictionary insertion order or process identity.
"""

from __future__ import annotations

import hashlib
import json

#: Domain id for the configured capacity policy digest.
POLICY_DOMAIN = "trajectory-os.portfolio-scheduler-policy.v1"

#: Domain id for the exact scheduling input projection digest.
PROJECTION_DOMAIN = "trajectory-os.portfolio-scheduler-projection.v1"

#: Domain id for one deterministic scheduling decision digest.
DECISION_DOMAIN = "trajectory-os.portfolio-scheduler-decision.v1"

#: Domain id for one dispatch attempt digest.
DISPATCH_DOMAIN = "trajectory-os.portfolio-scheduler-dispatch.v1"

#: Every scheduler identity domain id (closed set).
DOMAIN_IDS = frozenset({
    POLICY_DOMAIN, PROJECTION_DOMAIN, DECISION_DOMAIN, DISPATCH_DOMAIN,
})


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def canonical_bytes(domain: str, payload: object) -> bytes:
    """Domain-separated canonical bytes for one scheduler identity payload."""
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown scheduler identity domain: {domain!r}")
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


def policy_id(payload: object) -> str:
    return digest(POLICY_DOMAIN, payload)


def projection_id(payload: object) -> str:
    return digest(PROJECTION_DOMAIN, payload)


def decision_id(payload: object) -> str:
    return digest(DECISION_DOMAIN, payload)


def dispatch_id(payload: object) -> str:
    return digest(DISPATCH_DOMAIN, payload)


def identity_refs(*, policy: str, projection: str, decision: str) -> dict[str, str]:
    """Labeled scheduler identity block (never cross-compared by domain)."""
    return {
        "policy_id": policy,
        "input_projection_id": projection,
        "decision_id": decision,
        "domain": DECISION_DOMAIN,
    }
