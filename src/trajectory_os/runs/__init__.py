"""``trajectory_os.runs`` — deterministic multi-run foundation (V1.85-V1.90).

Modules (imported lazily by consumers to keep the package light):

* ``registry``       — V1.85 deterministic read-only run registry
* ``query``          — V1.86 unified state model + stable queries
* ``admission``      — V1.87 bounded admission control
* ``ownership``      — V1.88 ownership proof + graceful group control
* ``store``          — V1.89 durable FIFO queue + atomic state files
* ``orchestration``  — V1.90 bounded multi-run orchestration
* ``cli``            — ``trajectory-pi-runs`` command line entry point

Design invariants (see ADR-005): no DB, no remote API, no daemon,
fail-closed semantics, no PID-only ownership, no unbounded retry or
requeue, and strictly read-only inspection of run evidence.
"""

from trajectory_os.runs.model import SCHEMA_VERSION as RUNS_SCHEMA_VERSION  # noqa: F401

__all__ = ["RUNS_SCHEMA_VERSION"]
