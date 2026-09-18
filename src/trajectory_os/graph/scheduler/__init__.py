"""``trajectory_os.graph.scheduler`` — M013 portfolio scheduler (ADR-011).

A persistent, deterministic portfolio scheduler and resource arbiter that
consumes the M012 goal decomposition graph. It selects only authoritative
dependency-ready graph nodes, admits them against an explicit bounded
capacity policy (CPU slots, local GPU slots, local GPU VRAM, exclusivity,
global concurrency), persists the complete admission/deferral decision with
stable reason codes, optionally dispatches admitted work through the
existing production mission path, and reconstructs its state exactly after a
restart.

Design invariants (ADR-011):

* the M012 goal graph stays the canonical decomposition/dependency source —
  no second graph or mission engine is introduced;
* dependency readiness is authoritative; the scheduler never silently
  promotes graph state;
* local GPU is reserved **only** by nodes that explicitly declare
  ``resources.gpu = true``; remote model-heavy work never reserves local GPU
  merely because it is model-heavy;
* unknown or malformed resource metadata fails closed;
* decision identity is a domain-separated digest over normalized content and
  never includes a timestamp;
* no human micro-gate is introduced for ordinary scheduling/admission/
  dispatch, and no Git trust-boundary write is ever performed.

Modules (imported by consumers):

* ``identity``  — canonical scheduler identity domains + digests;
* ``model``     — policy, demand, reservations, decisions, state (pure);
* ``evidence``  — read-only authoritative mission runtime evidence;
* ``arbiter``   — pure deterministic candidate ordering + admission;
* ``engine``    — cycle orchestration + production-path dispatch composition;
* ``store``     — atomic durable scheduler store (strict, fail closed);
* ``summary``   — human/machine operator projections.
"""

from trajectory_os.graph.scheduler.model import (  # noqa: F401
    REASON_CODES,
    SCHEDULER_VERSION,
    SCHEMA_VERSION,
    SchedulerPolicy,
)

__all__ = ["REASON_CODES", "SCHEDULER_VERSION", "SCHEMA_VERSION",
           "SchedulerPolicy"]
