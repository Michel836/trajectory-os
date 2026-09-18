"""Mission 014 — canonical cross-mission reuse identity domains (pure).

Three domain-separated digests are produced by the reuse layer. Each answers
a different question and none is ever compared to a graph/spec digest, a
scheduler digest or a Mission 010 patch identity:

* ``trajectory-os.cross-mission-reuse-artifact.v1`` -> the exact content
  identity of one resolved upstream evidence artifact
  (``artifact_content_sha256``);
* ``trajectory-os.cross-mission-reuse-projection.v1`` -> the identity of the
  exact resolved reuse projection (graph + every input resolution)
  (``projection_id``);
* ``trajectory-os.cross-mission-reuse-consumption.v1`` -> the identity of one
  append-only (producer -> consumer, input, artifact) consumption event
  (``consumption_id``).

Canonical bytes are ``domain_utf8 || 0x00 || canonical_json_utf8`` where
``canonical_json`` is ``json.dumps(..., sort_keys=True,
separators=(",", ":"), ensure_ascii=True)``. Identity therefore depends only
on normalized content: never on clocks, filesystem order, dictionary
insertion order or process identity.
"""

from __future__ import annotations

import hashlib
import json

#: Domain id for one resolved reusable evidence artifact digest.
ARTIFACT_DOMAIN = "trajectory-os.cross-mission-reuse-artifact.v1"

#: Domain id for the exact resolved reuse projection digest.
PROJECTION_DOMAIN = "trajectory-os.cross-mission-reuse-projection.v1"

#: Domain id for one append-only reuse consumption event digest.
CONSUMPTION_DOMAIN = "trajectory-os.cross-mission-reuse-consumption.v1"

#: Every reuse identity domain id (closed set).
DOMAIN_IDS = frozenset({ARTIFACT_DOMAIN, PROJECTION_DOMAIN, CONSUMPTION_DOMAIN})


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


def canonical_bytes(domain: str, payload: object) -> bytes:
    """Domain-separated canonical bytes for one reuse identity payload."""
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown reuse identity domain: {domain!r}")
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


def artifact_id(payload: object) -> str:
    return digest(ARTIFACT_DOMAIN, payload)


def projection_id(payload: object) -> str:
    return digest(PROJECTION_DOMAIN, payload)


def consumption_id(payload: object) -> str:
    return digest(CONSUMPTION_DOMAIN, payload)
