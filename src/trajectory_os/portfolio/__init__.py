"""M020 — multi-goal portfolio scheduling over the canonical M012-M016 stores.

The package is a deterministic *coordination* layer: it selects which member
goals may advance under one explicit portfolio capacity, then advances each
selected goal through the existing production goal path. It introduces no
second graph, scheduler, mission engine, evidence store or completion proof.

Isolation is structural: per-goal state remains under its own canonical goal
root, and every portfolio entry records the exact goal/graph/generation it was
computed from. No module performs a Git trust-boundary write.
"""

from __future__ import annotations

__all__ = ["engine", "identity", "model", "store", "summary"]
