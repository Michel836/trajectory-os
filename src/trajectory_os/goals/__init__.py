"""M017-M019 — operator-visible product layer over the M008-M016 foundation.

This package is a *projection and control* layer only. It never introduces a
second source of truth and never rewrites canonical state:

* :mod:`trajectory_os.goals.launch` provisions the canonical missions a goal
  graph references (idempotent);
* :mod:`trajectory_os.goals.snapshot` composes one deterministic read-only
  operator snapshot from the canonical graph/mission/scheduler/proof stores;
* :mod:`trajectory_os.goals.runner` drives the existing production mission
  path end to end (scheduler dispatch) and records the derived goal proof;
* :mod:`trajectory_os.goals.tui` renders that snapshot as a live one-screen
  terminal dashboard;
* :mod:`trajectory_os.goals.cli` is the unified operator CLI.

No module performs a Git trust-boundary write.
"""

from __future__ import annotations

__all__ = ["cli", "launch", "runner", "snapshot", "tui"]
