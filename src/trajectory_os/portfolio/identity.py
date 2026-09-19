"""M020 — canonical multi-goal portfolio identity domains (pure).

Two domain-separated digests describe one portfolio decision and one
portfolio state. Neither is ever compared to a graph/scheduler/reuse/proof
digest: isolation between a portfolio and its member goals is structural.

Canonical bytes are ``domain_utf8 || 0x00 || canonical_json_utf8`` where
``canonical_json`` is ``json.dumps(..., sort_keys=True,
separators=(",", ":"), ensure_ascii=True)``. Identity therefore depends only
on normalized content: never on clocks, filesystem order or process identity.
"""

from __future__ import annotations

import hashlib
import json

#: Domain id for one portfolio scheduling decision.
PORTFOLIO_DECISION_DOMAIN = "trajectory-os.portfolio-decision.v1"

#: Domain id for one portfolio capacity policy.
PORTFOLIO_POLICY_DOMAIN = "trajectory-os.portfolio-policy.v1"

#: Domain id for one portfolio membership reference.
PORTFOLIO_ID_DOMAIN = "trajectory-os.portfolio-id.v1"

#: Domain id for one durable portfolio state document.
PORTFOLIO_STATE_DOMAIN = "trajectory-os.portfolio-state.v1"

#: Domain id for one daemon cycle document (M021).
DAEMON_CYCLE_DOMAIN = "trajectory-os.daemon-cycle.v1"

#: Domain id for one durable daemon state document (M021).
DAEMON_STATE_DOMAIN = "trajectory-os.daemon-state.v1"

#: Every portfolio/daemon identity domain id (closed set).
DOMAIN_IDS = frozenset({
    PORTFOLIO_DECISION_DOMAIN,
    PORTFOLIO_POLICY_DOMAIN,
    PORTFOLIO_ID_DOMAIN,
    PORTFOLIO_STATE_DOMAIN,
    DAEMON_CYCLE_DOMAIN,
    DAEMON_STATE_DOMAIN,
})


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def canonical_bytes(domain: str, payload: object) -> bytes:
    """Domain-separated canonical bytes for one portfolio identity payload."""
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown portfolio identity domain: {domain!r}")
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


def decision_id(payload: object) -> str:
    return digest(PORTFOLIO_DECISION_DOMAIN, payload)


def policy_id(payload: object) -> str:
    return digest(PORTFOLIO_POLICY_DOMAIN, payload)


def portfolio_id(payload: object) -> str:
    return digest(PORTFOLIO_ID_DOMAIN, payload)


def state_id(payload: object) -> str:
    return digest(PORTFOLIO_STATE_DOMAIN, payload)


def daemon_cycle_id(payload: object) -> str:
    return digest(DAEMON_CYCLE_DOMAIN, payload)


def daemon_state_id(payload: object) -> str:
    return digest(DAEMON_STATE_DOMAIN, payload)
