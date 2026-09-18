"""Mission 012 — canonical goal-graph identity domains (pure constants).

Two *different* identities are produced for one goal graph and they answer
different questions:

* **spec identity** (``trajectory-os.goal-decomposition-spec.v1``) — the
  SHA-256 of the normalized declarative spec (goal id, objective and
  normalized nodes). It answers "which normalized input spec created this
  graph?".

* **graph identity** (``trajectory-os.goal-decomposition-graph.v1``) — the
  SHA-256 of the normalized graph projection (goal id, objective,
  normalized nodes *and* the explicit dependency edge set). It answers
  "which normalized graph projection is this?".

Both digests are domain-separated: the canonical bytes are
``domain_utf8 || 0x00 || canonical_json_utf8`` so a digest produced in one
domain can never equal a digest produced in another for the same payload.

The domains here are **deliberately distinct** from the Mission 010 patch
identity domains (``trajectory-pi.worktree.snapshot.v1`` and
``mission.worktree.diff.v1``). A graph identity is never compared to a
patch identity; no equality relation is implied. This module performs no
I/O and introduces no dependency.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

#: Domain id for the normalized goal-graph spec digest.
SPEC_DOMAIN = "trajectory-os.goal-decomposition-spec.v1"

#: Domain id for the normalized goal-graph projection digest.
GRAPH_DOMAIN = "trajectory-os.goal-decomposition-graph.v1"

#: Every known goal-graph identity domain id (closed set).
DOMAIN_IDS = frozenset({SPEC_DOMAIN, GRAPH_DOMAIN})

#: Canonical domain descriptors, ordered spec -> graph. Read-only operator
#: documentation; never a comparison input.
DOMAIN_DESCRIPTORS: tuple[dict[str, str], ...] = (
    {
        "domain": SPEC_DOMAIN,
        "field": "spec_sha256",
        "producer": "normalized declarative goal spec",
    },
    {
        "domain": GRAPH_DOMAIN,
        "field": "graph_id",
        "producer": "normalized goal graph projection (nodes + edges)",
    },
)


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII.

    Lists keep their order: callers must pass already-normalized (sorted)
    sequences, which is exactly what the graph normalizer guarantees.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def canonical_bytes(domain: str, payload: object) -> bytes:
    """Domain-separated canonical bytes for one identity payload."""
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown goal-graph identity domain: {domain!r}")
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


def spec_identity_ref(spec_sha256: str) -> dict[str, str]:
    """Labeled spec identity (never compared to any other domain)."""
    return {"domain": SPEC_DOMAIN, "field": "spec_sha256",
            "spec_sha256": spec_sha256}


def graph_identity_ref(graph_id: str) -> dict[str, str]:
    """Labeled graph identity (never compared to any other domain)."""
    return {"domain": GRAPH_DOMAIN, "field": "graph_id",
            "graph_id": graph_id}


def domain_separation_holds(payload: Mapping[str, object] | Sequence[object]) -> bool:
    """True when the two domains produce different digests for one payload.

    Used by tests/operators to demonstrate that the graph and spec domains
    are distinct; it is never a promotion or trust decision.
    """
    return digest(SPEC_DOMAIN, payload) != digest(GRAPH_DOMAIN, payload)
