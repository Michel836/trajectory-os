"""``trajectory_os.resources`` — M024 operational local resource orchestration.

Read-only discovery of local CPU/RAM/NVIDIA resources, explicit VRAM/CPU
reservations and hard admission control with bounded concurrency. Remote
inference is distinguished from local GPU use, and the local reviewer's
reserved pool is isolated from agent/job workloads.

The pure per-dimension policy remains owned by
:mod:`trajectory_os.runs.resources`; this package makes it operational.
"""

from trajectory_os.resources.arbiter import ResourceArbiter
from trajectory_os.resources.model import (
    AR_CONCURRENCY,
    AR_DUPLICATE,
    AR_EXHAUSTED,
    AR_LOCAL_CONCURRENCY,
    AR_MALFORMED,
    AR_OK,
    AR_UNKNOWN,
    LOCAL,
    REMOTE,
    ROLE_AGENT,
    ROLE_JOB,
    ROLE_REVIEWER,
    AdmissionDecision,
    LocalResourceReport,
    Reservation,
    ResourcePolicy,
)

__all__ = [
    "AR_CONCURRENCY", "AR_DUPLICATE", "AR_EXHAUSTED", "AR_LOCAL_CONCURRENCY",
    "AR_MALFORMED", "AR_OK", "AR_UNKNOWN", "LOCAL", "REMOTE",
    "ROLE_AGENT", "ROLE_JOB", "ROLE_REVIEWER", "AdmissionDecision",
    "LocalResourceReport", "ResourcePolicy", "Reservation", "ResourceArbiter",
]
