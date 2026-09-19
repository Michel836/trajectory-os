"""M026 — domain-separated event identity (pure).

Canonical bytes are ``domain_utf8 || 0x00 || canonical_json_utf8`` where
``canonical_json`` is ``json.dumps(..., sort_keys=True, separators=(",", ":"),
ensure_ascii=True)``. Identity therefore depends only on normalized content:
never on clocks, filesystem order or process identity. That is what makes the
projection deduplicated and restart-safe — re-deriving the same canonical
state always yields the same event id.
"""

from __future__ import annotations

import hashlib
import json

#: Domain id for one authoritative event record.
EVENT_DOMAIN = "trajectory-os.event-record.v1"

#: Domain id for one complete event projection document.
PROJECTION_DOMAIN = "trajectory-os.event-projection.v1"

#: Every event identity domain id (closed set).
DOMAIN_IDS = frozenset({EVENT_DOMAIN, PROJECTION_DOMAIN})


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def canonical_bytes(domain: str, payload: object) -> bytes:
    """Domain-separated canonical bytes for one event identity payload."""
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown event identity domain: {domain!r}")
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


def event_id(payload: object) -> str:
    return digest(EVENT_DOMAIN, payload)


def projection_id(payload: object) -> str:
    return digest(PROJECTION_DOMAIN, payload)
