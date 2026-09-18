"""``trajectory_os.graph`` — bounded goal decomposition graph (Mission 012).

One strategic goal is represented as a deterministic directed acyclic graph
of explicit, dependent mission nodes. The graph is an *orchestration /
projection* structure only:

* it never becomes a second source of truth for mission execution,
  completion or proof — the canonical mission evidence
  (``missions/mission.json`` + sub-run records) stays authoritative;
* it references missions by stable identity and derives readiness from the
  live mission state at projection time, never by copying trust evidence
  into graph state;
* every persisted document is deterministic, bounded, atomic and strictly
  reconstructed (fail closed on malformed, contradictory, oversized or
  identity-mismatched state).

Modules (imported lazily by consumers to keep the package light):

* ``identity``    — canonical bytes + hash domains (graph vs spec);
* ``model``       — schema, bounded validators, normalization, topological
  order (pure);
* ``evidence``    — read-only mission reference resolution;
* ``readiness``   — deterministic dependency readiness projection (M013
  interface);
* ``store``       — atomic durable graph store (strict, fail-closed);
* ``summary``     — human/machine operator projections;
* ``cli``        — operator CLI (``trajectory-pi-goals``);
* ``scheduler``  — M013 deterministic portfolio scheduler + resource
  arbiter (capacity policy, admission, dispatch composition, durable
  decisions).
* ``reuse``      — M014 explicit fail-closed cross-mission evidence reuse
  (declarations, read-only resolution, projection + consumption store).
* ``replan``     — M015 bounded adaptive replanning over immutable graph
  generations (explicit triggers, validated plans, append-only history,
  scheduler generation binding).

Design invariants (ADR-010, ADR-011, ADR-012, ADR-013): no second graph or
execution engine, reuse is input provenance only (never completion proof),
replanning is graph evolution only (never semantic success promotion), no
operator micro-gates, no autonomous Git trust-boundary write.
"""

from trajectory_os.graph.model import SCHEMA_VERSION as GRAPH_SCHEMA_VERSION  # noqa: F401

__all__ = ["GRAPH_SCHEMA_VERSION"]
