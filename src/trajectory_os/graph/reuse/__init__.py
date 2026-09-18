"""``trajectory_os.graph.reuse`` — M014 explicit cross-mission evidence reuse.

A bounded, deterministic, fail-closed layer that lets a downstream graph node
consume an explicitly referenced, proven upstream artifact/evidence item
without copying or promoting upstream success. It extends the canonical M012
graph (reuse declarations live on graph nodes) and composes with the M013
scheduler (unresolved mandatory reuse blocks admission/dispatch) without
introducing a second graph, scheduler or mission engine.

Modules (imported by consumers):

* ``identity``  — canonical reuse identity domains + digests;
* ``model``     — resolution statuses, reasons, projection, consumption;
* ``resolver``  — read-only canonical mission evidence resolution;
* ``engine``    — resolution cycle + consumption recording composition;
* ``store``     — atomic durable projection + append-only consumption store;
* ``summary``   — human/machine operator projections.

Design invariants (ADR-012): reuse is input provenance only, never completion
proof; no implicit filesystem/process/environment/model-context leakage is
ever trusted; every unresolved/stale/mismatched reference fails closed with a
stable machine-readable reason code.
"""

from trajectory_os.graph.reuse.model import (  # noqa: F401
    REASON_CODES,
    REUSE_VERSION,
    SCHEMA_VERSION,
    ReuseInputStatus,
    ReuseProjection,
)

__all__ = ["REASON_CODES", "REUSE_VERSION", "SCHEMA_VERSION",
           "ReuseInputStatus", "ReuseProjection"]
