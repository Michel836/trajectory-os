"""``trajectory_os.graph.replan`` — M015 adaptive replanning (ADR-013).

A bounded, deterministic, fail-closed layer that evolves the canonical M012
goal graph through explicit machine-readable triggers. Replanning never
destructively rewrites history: every mutation archives the prior generation
and appends a new explicit generation plus an append-only replan event.

Modules (imported by consumers):

* ``identity``  — canonical replan identity domains + digests;
* ``model``     — triggers, policy, changes, plans, generations, events;
* ``store``     — atomic generation archive + append-only replan events;
* ``engine``    — plan build/validate/apply composition;
* ``summary``   — read-only operator projections.

Design invariants (ADR-013): replanning is graph evolution only (never
semantic success promotion), stale triggers fail closed, plans are validated
before activation, and the M013 scheduler is re-bound to the newest validated
generation so stale-generation work can never be dispatched.
"""

from trajectory_os.graph.replan.model import (  # noqa: F401
    REASON_CODES,
    REPLAN_VERSION,
    SCHEMA_VERSION,
    Generation,
    ReplanChange,
    ReplanEvent,
    ReplanPlan,
    ReplanPolicy,
    ReplanTrigger,
)

__all__ = ["REASON_CODES", "REPLAN_VERSION", "SCHEMA_VERSION", "Generation",
           "ReplanChange", "ReplanEvent", "ReplanPlan", "ReplanPolicy",
           "ReplanTrigger"]
