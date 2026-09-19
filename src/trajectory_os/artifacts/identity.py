"""M025 — canonical workspace/artifact identity domains (pure).

Three domain-separated digests describe persistent workspaces and generated
artifacts. None is ever compared to a mission patch identity, a graph/
scheduler/reuse digest, an agent run digest or a goal-proof digest:

* ``trajectory-os.workspace.v1`` -> one per-goal or per-mission workspace;
* ``trajectory-os.artifact.v1`` -> one artifact record (semantic provenance +
  content digest);
* ``trajectory-os.artifact-lineage.v1`` -> one reconstructed lineage.

Canonical bytes are ``domain_utf8 || 0x00 || canonical_json_utf8`` with
sorted keys and compact separators, so identity depends only on normalized
content — never on clocks, filesystem order or dictionary insertion order.
"""

from __future__ import annotations

import hashlib
import json

#: Domain id for one persistent workspace digest.
WORKSPACE_DOMAIN = "trajectory-os.workspace.v1"

#: Domain id for one artifact record digest.
ARTIFACT_DOMAIN = "trajectory-os.artifact.v1"

#: Domain id for one reconstructed artifact lineage digest.
LINEAGE_DOMAIN = "trajectory-os.artifact-lineage.v1"

#: Every workspace/artifact identity domain id (closed set).
DOMAIN_IDS = frozenset({WORKSPACE_DOMAIN, ARTIFACT_DOMAIN, LINEAGE_DOMAIN})


def canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def canonical_bytes(domain: str, payload: object) -> bytes:
    if domain not in DOMAIN_IDS:
        raise ValueError(f"unknown workspace/artifact identity domain: {domain!r}")
    return (domain.encode("utf-8") + b"\x00"
            + canonical_json(payload).encode("utf-8"))


def digest(domain: str, payload: object) -> str:
    return hashlib.sha256(canonical_bytes(domain, payload)).hexdigest()


def workspace_id(payload: object) -> str:
    return digest(WORKSPACE_DOMAIN, payload)


def artifact_id(payload: object) -> str:
    return digest(ARTIFACT_DOMAIN, payload)


def lineage_id(payload: object) -> str:
    return digest(LINEAGE_DOMAIN, payload)
