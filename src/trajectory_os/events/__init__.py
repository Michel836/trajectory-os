"""M026 — authoritative event and notification projections.

The package derives a durable, restart-safe, bounded and deduplicated event
stream **only** from canonical persisted state (goal graph, missions,
scheduler/resource decisions, replans, artifacts, human gates and the derived
goal proof). It never derives an event from model prose and never becomes a
second source of truth: the event stream is a projection that can always be
re-derived from the authoritative stores.

No module performs a Git trust-boundary write.
"""

from __future__ import annotations

__all__ = ["engine", "identity", "model", "store", "summary"]
