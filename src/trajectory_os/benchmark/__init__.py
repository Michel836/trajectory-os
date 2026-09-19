"""``trajectory_os.benchmark`` — M029 Pi vs DeepSeek Harness benchmark.

A decision-grade, repeatable benchmark that runs the same canonical workloads
through the proven Pi runtime and the qualified DeepSeek Harness runtime,
records authoritative provider-grounded telemetry (never invented), applies
the same validation and review trust gates, and persists per-trial and
aggregate artifacts sufficient for an explicit operator runtime decision.

Design invariants:

* no Git trust-boundary write is ever performed;
* every trial runs in a fresh isolated workspace;
* a metric the backend/provider does not expose is recorded ``UNAVAILABLE``
  with an explicit reason — never guessed;
* failed/blocked/unavailable trials are evidence, never discarded;
* an inactive or stale reviewer is never displayed as the active reviewer.
"""

from trajectory_os.benchmark.model import (  # noqa: F401
    BENCHMARK_VERSION,
    FINAL_REVIEWER_MODEL,
    RUN_BLOCKED,
    RUN_CANCELLED,
    RUN_COMPLETE,
    RUN_FAILED,
    RUN_READY_FOR_COMMIT,
    RUN_RUNNING,
    SCHEMA_VERSION,
    TERMINAL_RUN_STATES,
    TrialRecord,
    WorkloadSpec,
)

__all__ = [
    "BENCHMARK_VERSION", "FINAL_REVIEWER_MODEL", "RUN_BLOCKED",
    "RUN_CANCELLED", "RUN_COMPLETE", "RUN_FAILED", "RUN_READY_FOR_COMMIT",
    "RUN_RUNNING", "SCHEMA_VERSION", "TERMINAL_RUN_STATES", "TrialRecord",
    "WorkloadSpec",
]
