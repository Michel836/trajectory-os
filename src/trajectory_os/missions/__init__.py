"""``trajectory_os.missions`` — bounded multi-phase mission orchestration (Mission 003).

Modules (imported lazily by consumers to keep the package light):

* ``model``          — canonical vocabulary: states, kinds, reason codes,
  hard bounds (pure constants);
* ``flow``           — pure deterministic mission state machine (``decide``);
* ``store``          — durable atomic mission/sub-run evidence store
  (strict, fail-closed);
* ``runner``         — bounded sub-run runners (fresh context per sub-run);
* ``orchestrator``   — bounded, resumable mission execution loop + guards;
* ``summary``        — read-only human/machine output derived from the
  canonical persisted state (status, benchmark, final summary).

Design invariants (ADR-005): no DB, no daemon, no distributed worker,
fail-closed semantics, bounded loops and budgets, strict read-only
inspection of Git worktrees, and no autonomous commit/push/PR/merge.
"""

from trajectory_os.missions.model import SCHEMA_VERSION as MISSIONS_SCHEMA_VERSION  # noqa: F401

__all__ = ["MISSIONS_SCHEMA_VERSION"]
