"""M021 — bounded persistent daemon over the M020 multi-goal portfolio.

The daemon is a restart-safe, bounded composition loop. It owns no
authoritative work state and never forks a second source of truth: it
delegates every cycle to the canonical portfolio path and persists only its
own runtime accounting so a restarted process resumes exactly.

No module performs a Git trust-boundary write.
"""

from __future__ import annotations

__all__ = ["engine", "model", "store", "summary"]
