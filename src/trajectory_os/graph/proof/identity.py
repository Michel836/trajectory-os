"""Mission 016 — canonical goal-proof identity domains (pure constants).

Three domain-separated digests are produced by the goal-level proof layer.
Each answers a different question and none is ever compared to a graph,
scheduler, reuse, replan, agent or Mission 010 patch identity:

* ``trajectory-os.goal-proof.v1`` — the deterministic, timestamp-free
  identity of one derived goal-proof projection (``proof_id``);
* ``trajectory-os.goal-proof-evidence-binding.v1`` — the identity of one
  acceptance-criterion -> exact proven-evidence binding (``binding_id``);
* ``trajectory-os.goal-proof-event.v1`` — the identity of one append-only
  proof/operator event (``event_id``).

Canonical bytes are ``domain_utf8 || 0x00 || canonical_json_utf8`` where
``canonical_json`` is ``json.dumps(..., sort_keys=True,
separators=(",", ":"), ensure_ascii=True)``. Identity therefore depends
only on normalized content: never on clocks, filesystem order, dictionary
insertion order or process identity.

A goal-proof identity is *derived evidence*: it is never a second source
of truth and is never compared for equality with any other domain.
"""

from __future__ import annotations

import hashlib
import json

#: Domain id for the derived goal-proof projection digest.
PROOF_DOMAIN = "trajectory-os.goal-proof.v1"

#: Domain id for one acceptance-criterion evidence binding digest.
BINDING_DOMAIN = "trajectory-os.goal-proof-evidence-binding.v1"

#: Domain id for one append-only goal-proof event digest.
EVENT_DOMAIN = "trajectory-os.goal-proof-event.v1"

#: Every known goal-proof identity domain id (closed set).
DOMAIN_IDS = frozenset({PROOF_DOMAIN, BINDING_DOMAIN, EVENT_DOMAIN})


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def canonical_bytes(domain: str, payload: object) -> bytes:
    """Domain-separated canonical bytes for one goal-proof identity payload."""
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown goal-proof identity domain: {domain!r}")
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


def proof_id(payload: object) -> str:
    return digest(PROOF_DOMAIN, payload)


def binding_id(payload: object) -> str:
    return digest(BINDING_DOMAIN, payload)


def event_id(payload: object) -> str:
    return digest(EVENT_DOMAIN, payload)


def identity_refs(*, proof: str, generation: str | None,
                  graph: str) -> dict[str, str | None]:
    """Labeled goal-proof identity block (never cross-compared by domain)."""
    return {
        "domain": PROOF_DOMAIN,
        "proof_id": proof,
        "graph_id": graph,
        "generation_id": generation,
    }
