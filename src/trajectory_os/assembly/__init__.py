"""``trajectory_os.assembly`` — M031 end-to-end mission assembly.

Assembles the capabilities delivered through M023–M030 into one coherent,
trust-gated mission flow from objective intake to ``READY_FOR_COMMIT`` /
``BLOCKED`` with durable closure evidence::

    Mission -> Preflight -> Plan -> Execution -> Validation
            -> Review -> Repair -> Human Gate -> Closure

This package owns mission-level composition only. It reuses:

* the M030 canonical observability status/events/telemetry/summary contract
  (``status.json`` remains the single source of truth);
* the M030 canonical live-run orchestrator (execution → validation → review →
  repair);
* the M029 deterministic validation, exact patch identity and independent
  review protocol;
* the M029/M030 preflight and legacy-free reviewer identity model.

It introduces no competing architecture, no second status model, and no Git
trust-boundary write.
"""

from trajectory_os.assembly.model import (  # noqa: F401
    ASSEMBLY_VERSION,
    SCHEMA_VERSION,
    AssemblyError,
    MissionBaseline,
    MissionClosure,
    MissionDefinition,
    MissionPlan,
    PlanStep,
    TrustPolicy,
)

__all__ = [
    "ASSEMBLY_VERSION",
    "SCHEMA_VERSION",
    "AssemblyError",
    "MissionBaseline",
    "MissionClosure",
    "MissionDefinition",
    "MissionPlan",
    "PlanStep",
    "TrustPolicy",
]
