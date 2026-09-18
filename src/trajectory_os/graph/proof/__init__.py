"""``trajectory_os.graph.proof`` — M016 goal-level proof and operator control.

A bounded, deterministic, fail-closed layer that answers whether one
strategic goal is actually proven satisfied from authoritative M008-M015
mission-level evidence, and exposes a compact operator dashboard plus a
complete machine-readable projection.

Design invariants (ADR-015):

* the goal proof is **derived evidence**, never a second source of truth;
* ``COMPLETE`` is permitted only when every configured acceptance criterion
  is bound to exact proven evidence and zero unresolved risk remains;
* fail closed on malformed/unsupported state, missing evidence, graph or
  generation mismatch, stale scheduler/reuse state, contradictory
  provenance, ambiguous criterion mapping, impossible state, reconstruction
  mismatch or unknown future semantics;
* proof identity is timestamp-free and reconstructible after interruption;
* no Git trust-boundary write is ever performed.

Modules (imported by consumers):

* ``identity`` — canonical goal-proof identity domains + digests;
* ``model``    — bounded proof projection, criteria bindings and risks (pure);
* ``evidence`` — read-only exact mission/phase/sub-run proof-chain resolution;
* ``engine``   — read-only composition + reconstruction proof;
* ``store``    — atomic projection + append-only proof/operator events;
* ``summary``  — compact operator dashboard and why/explain projections.
"""

from trajectory_os.graph.proof.model import (  # noqa: F401
    PROOF_VERSION,
    REASON_CODES,
    SCHEMA_VERSION,
    GoalProof,
    GoalProofError,
)

__all__ = [
    "PROOF_VERSION", "REASON_CODES", "SCHEMA_VERSION", "GoalProof",
    "GoalProofError",
]
