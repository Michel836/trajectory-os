"""Mission 010 — canonical patch identity domains (pure constants).

Two *different* patch identities flow through the mission pipeline. They
answer different questions, are produced by different components, and
have different byte domains. They must therefore **never** be compared
for equality:

* **wrapper snapshot** (``trajectory-pi.worktree.snapshot.v1``) — the
  SHA-256 of the exact temporary-index snapshot patch the canonical
  ``trajectory-pi`` wrapper built for one sub-run. Its authoritative
  field is ``attestation.patch_sha256`` (Mission 008). The wrapper never
  changes the real git index; the snapshot is taken through a private
  temporary index.

* **mission worktree diff** (``mission.worktree.diff.v1``) — the SHA-256
  of the read-only ``git diff HEAD`` bytes the mission orchestrator
  captured as fresh-context / worktree-identity evidence for a proven
  phase. Its authoritative field is ``worktree.worktree_patch_sha256``.

The two digests can legitimately differ for the same logical change
(different diff invocations, index state, and byte framing), so an
equality check across domains is meaningless and is never performed by
this codebase.

This module centralizes the domain ids and their authoritative field
names so every operator-visible projection labels them identically. It
performs no I/O and introduces no dependency.
"""

from __future__ import annotations

#: Domain id for the canonical wrapper's temporary-index snapshot patch.
WRAPPER_SNAPSHOT_DOMAIN = "trajectory-pi.worktree.snapshot.v1"

#: Authoritative field carrying the wrapper snapshot digest (Mission 008).
WRAPPER_SNAPSHOT_FIELD = "attestation.patch_sha256"

#: Domain id for the orchestrator's read-only worktree diff digest.
MISSION_WORKTREE_DOMAIN = "mission.worktree.diff.v1"

#: Authoritative field carrying the mission worktree diff digest.
MISSION_WORKTREE_FIELD = "worktree.worktree_patch_sha256"

#: Every known patch identity domain id (closed set; fail closed on
#: anything else in projections that need to validate a label).
DOMAIN_IDS = frozenset({WRAPPER_SNAPSHOT_DOMAIN, MISSION_WORKTREE_DOMAIN})

#: Canonical domain descriptors, ordered wrapper -> mission. Read-only
#: operator documentation; never a comparison input.
DOMAIN_DESCRIPTORS: tuple[dict[str, str], ...] = (
    {
        "domain": WRAPPER_SNAPSHOT_DOMAIN,
        "field": WRAPPER_SNAPSHOT_FIELD,
        "producer": "trajectory-pi wrapper (temporary-index snapshot)",
    },
    {
        "domain": MISSION_WORKTREE_DOMAIN,
        "field": MISSION_WORKTREE_FIELD,
        "producer": "mission orchestrator (read-only git diff HEAD)",
    },
)


def wrapper_snapshot_ref(patch_sha256: str | None) -> dict[str, str | None]:
    """Labeled wrapper-snapshot identity (never compared to any other domain)."""
    return {
        "domain": WRAPPER_SNAPSHOT_DOMAIN,
        "field": WRAPPER_SNAPSHOT_FIELD,
        "patch_sha256": patch_sha256,
    }


def mission_worktree_ref(patch_sha256: str | None) -> dict[str, str | None]:
    """Labeled mission-worktree identity (never compared to any other domain)."""
    return {
        "domain": MISSION_WORKTREE_DOMAIN,
        "field": MISSION_WORKTREE_FIELD,
        "patch_sha256": patch_sha256,
    }
