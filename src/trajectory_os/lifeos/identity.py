"""M028 — domain-separated LifeOS exchange identity (pure)."""

from __future__ import annotations

import hashlib
import json

#: Domain id for one scoped LifeOS exchange payload.
EXCHANGE_DOMAIN = "trajectory-os.lifeos-exchange.v1"

DOMAIN_IDS = frozenset({EXCHANGE_DOMAIN})


def canonical_json(payload: object) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def digest(domain: str, payload: object) -> str:
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown lifeos identity domain: {domain!r}")
    data = (
        domain.encode("utf-8")
        + b"\x00"
        + canonical_json(payload).encode("utf-8")
    )
    return hashlib.sha256(data).hexdigest()


def exchange_id(payload: object) -> str:
    return digest(EXCHANGE_DOMAIN, payload)
