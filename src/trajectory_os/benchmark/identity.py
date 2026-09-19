"""M029 — canonical benchmark identity domains (pure, domain separated).

Each durable benchmark object gets one deterministic SHA-256 identity derived
from normalized content only (never clocks, filesystem order, dictionary
insertion order or process identity). The domains are deliberately separate
from every agent/resource/telemetry identity domain so a benchmark digest can
never be accidentally compared with an unrelated digest.
"""

from __future__ import annotations

import hashlib
import json

#: Domain id for one benchmark run manifest.
RUN_DOMAIN = "trajectory-os.benchmark-run.v1"

#: Domain id for one trial record.
TRIAL_DOMAIN = "trajectory-os.benchmark-trial.v1"

#: Domain id for one benchmark event.
EVENT_DOMAIN = "trajectory-os.benchmark-event.v1"

#: Domain id for one aggregate benchmark summary.
SUMMARY_DOMAIN = "trajectory-os.benchmark-summary.v1"

#: Domain id for one reviewer observation.
REVIEW_DOMAIN = "trajectory-os.benchmark-review.v1"

#: Domain id for one exact semantic patch identity.
PATCH_DOMAIN = "trajectory-os.benchmark-patch.v1"

#: Every benchmark identity domain id (closed set).
DOMAIN_IDS = frozenset({
    RUN_DOMAIN, TRIAL_DOMAIN, EVENT_DOMAIN, SUMMARY_DOMAIN, REVIEW_DOMAIN,
    PATCH_DOMAIN,
})


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_bytes(domain: str, payload: object) -> bytes:
    """Domain-separated canonical bytes for one benchmark identity payload."""
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown benchmark identity domain: {domain!r}")
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


def run_id(payload: object) -> str:
    return digest(RUN_DOMAIN, payload)


def trial_id(payload: object) -> str:
    return digest(TRIAL_DOMAIN, payload)


def event_id(payload: object) -> str:
    return digest(EVENT_DOMAIN, payload)


def summary_id(payload: object) -> str:
    return digest(SUMMARY_DOMAIN, payload)


def review_id(payload: object) -> str:
    return digest(REVIEW_DOMAIN, payload)


def patch_id(payload: object) -> str:
    return digest(PATCH_DOMAIN, payload)
