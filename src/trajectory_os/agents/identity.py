"""Mission 015 — canonical agent-backend identity domains (pure).

Four domain-separated digests describe one bounded agent run. None is ever
compared to a graph/scheduler/reuse digest or a Mission 010 patch identity:

* ``trajectory-os.agent-backend-probe.v1`` -> one backend capability probe;
* ``trajectory-os.agent-backend-run.v1`` -> one bounded agent run result;
* ``trajectory-os.agent-completion-evidence.v1`` -> one completion-evidence
  record;
* ``trajectory-os.agent-backend-canary.v1`` -> one canary comparison outcome
  (``canary_id``).

Canonical bytes are ``domain_utf8 || 0x00 || canonical_json_utf8`` where
``canonical_json`` is ``json.dumps(..., sort_keys=True,
separators=(",", ":"), ensure_ascii=True)``. Identity therefore depends only
on normalized content: never on clocks, filesystem order, dictionary
insertion order or process identity.
"""

from __future__ import annotations

import hashlib
import json

#: Domain id for one backend capability probe digest.
PROBE_DOMAIN = "trajectory-os.agent-backend-probe.v1"

#: Domain id for one bounded agent run result digest.
RUN_DOMAIN = "trajectory-os.agent-backend-run.v1"

#: Domain id for one completion-evidence digest.
EVIDENCE_DOMAIN = "trajectory-os.agent-completion-evidence.v1"

#: Domain id for one canary comparison outcome digest.
CANARY_DOMAIN = "trajectory-os.agent-backend-canary.v1"

#: Every agent-backend identity domain id (closed set).
DOMAIN_IDS = frozenset({
    PROBE_DOMAIN, RUN_DOMAIN, EVIDENCE_DOMAIN, CANARY_DOMAIN,
})


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def canonical_bytes(domain: str, payload: object) -> bytes:
    """Domain-separated canonical bytes for one agent identity payload."""
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown agent-backend identity domain: {domain!r}")
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


def probe_id(payload: object) -> str:
    return digest(PROBE_DOMAIN, payload)


def run_id(payload: object) -> str:
    return digest(RUN_DOMAIN, payload)


def evidence_id(payload: object) -> str:
    return digest(EVIDENCE_DOMAIN, payload)


def canary_id(payload: object) -> str:
    return digest(CANARY_DOMAIN, payload)
