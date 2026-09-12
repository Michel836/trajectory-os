"""Shared constants and state model for the multi-run foundation (V1.85-V1.90).

Every schema surface exposed by ``trajectory_os.runs`` carries the same
explicit schema version so consumers can fail closed on unknown versions.
"""

from __future__ import annotations

# Canonical schema version for all machine-readable run/queue/admission
# payloads produced by this package (V1.85-V1.90).
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Lifecycle (V1.85): an evidence-based, fail-closed classification.
#   active  — started, not ended, and live process ownership PROVEN
#   ended   — the producer recorded a terminal lifecycle marker
#   unknown — started/not-ended split cannot be proven either way
# ---------------------------------------------------------------------------
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_ENDED = "ended"
LIFECYCLE_UNKNOWN = "unknown"

# Lifecycle ownership codes (no PID-only decisions, V1.87-V1.88):
OWNERSHIP_PROVEN = "proven"
OWNERSHIP_STALE = "stale"  # a pid is recorded but cannot be verified live
OWNERSHIP_UNPROVEN = "unproven"  # not enough recorded evidence to verify

# ---------------------------------------------------------------------------
# Outcome state (V1.85/V1.86): derived ONLY from persisted evidence.
#   in_progress — lifecycle active (ownership proven live)
#   ready       — every gate passes on complete, consistent evidence
#   failed      — complete evidence with at least one definitive gate FAIL
#   incomplete  — one or more required gates lack evidence entirely
#   invalid     — evidence is present but malformed or internally
#                 contradictory (e.g. patch identity mismatch)
# Incomplete and invalid evidence can NEVER be classified READY.
# ---------------------------------------------------------------------------
STATE_IN_PROGRESS = "in_progress"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_INCOMPLETE = "incomplete"
STATE_INVALID = "invalid"

ALL_STATES = (
    STATE_IN_PROGRESS,
    STATE_READY,
    STATE_FAILED,
    STATE_INCOMPLETE,
    STATE_INVALID,
)

# ---------------------------------------------------------------------------
# Deterministic query filters (V1.86).  An unknown filter must fail closed;
# never default silently to another filter.
# ---------------------------------------------------------------------------
FILTER_ALL = "all"
FILTER_RECENT = "recent"
FILTER_ACTIVE = "active"
FILTER_READY = "ready"
FILTER_FAILED = "failed"
FILTER_INCOMPLETE = "incomplete"
FILTER_REPAIRED = "repaired"

ALL_FILTERS = (
    FILTER_ALL,
    FILTER_RECENT,
    FILTER_ACTIVE,
    FILTER_READY,
    FILTER_FAILED,
    FILTER_INCOMPLETE,
    FILTER_REPAIRED,
)

DEFAULT_RECENT_LIMIT = 10

# ---------------------------------------------------------------------------
# Gate vocabulary (recognized deterministic statuses).  Anything outside a
# gate's recognized set is MALFORMED evidence, not a gate failure.
# ---------------------------------------------------------------------------
GATE_PASS = "PASS"
GATE_FAIL = "FAIL"
RECOGNIZED_GATE_VALUES = frozenset(
    {
        "PASS",
        "FAIL",
        "SKIPPED",
        "DISABLED",
        "STALE",
        "TIMEOUT",
        "ERROR",
        "REJECTED",
    }
)
RECOGNIZED_SNAPSHOT_VALUES = frozenset({"COMPLETE", "FAILED"})

# Ordered gate list: stable reason codes, stable ordering everywhere.
GATE_VALIDATION = "validation"
GATE_REVIEW = "review"
GATE_FINAL_VERIFY = "final_verify"
GATE_PATCH_IDENTITY = "patch_identity"
GATE_SNAPSHOT = "snapshot"
GATE_DIFF_CHECK = "diff_check"

ALL_GATES = (
    GATE_VALIDATION,
    GATE_REVIEW,
    GATE_FINAL_VERIFY,
    GATE_PATCH_IDENTITY,
    GATE_SNAPSHOT,
    GATE_DIFF_CHECK,
)

# ---------------------------------------------------------------------------
# Admission reason codes (V1.87) — stable, machine-readable, deterministic.
# ---------------------------------------------------------------------------
REASON_ADMITTED = "ADMITTED"
REASON_CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
REASON_CAPACITY_INVALID = "CAPACITY_INVALID"
REASON_OWNERSHIP_STALE = "OWNERSHIP_STALE"
REASON_OWNERSHIP_UNPROVEN = "OWNERSHIP_UNPROVEN"
REASON_STATE_AMBIGUOUS = "STATE_AMBIGUOUS"
REASON_QUEUE_MALFORMED = "QUEUE_MALFORMED"
REASON_QUEUE_FULL = "QUEUE_FULL"
REASON_DUPLICATE_IDENTITY = "DUPLICATE_IDENTITY"

DECISION_ALLOWED = "allowed"
DECISION_REJECTED = "rejected"

# ---------------------------------------------------------------------------
# Queue / orchestration error and terminal codes (V1.89-V1.90).
# ---------------------------------------------------------------------------
ERR_QUEUE_MALFORMED = "QUEUE_MALFORMED"
ERR_DUPLICATE_IDENTITY = "DUPLICATE_IDENTITY"
ERR_QUEUE_FULL = "QUEUE_FULL"
ERR_QUEUE_EMPTY = "QUEUE_EMPTY"
ERR_INVALID_JOB = "INVALID_JOB"

TERMINAL_DONE = "done"
TERMINAL_FAILED = "failed"
TERMINAL_CRASHED = "crashed"
TERMINAL_CANCELLED = "cancelled"
TERMINAL_CANCEL_PENDING = "cancel_pending"

# ---------------------------------------------------------------------------
# Bounded limits (no unbounded growth / retry / requeue).
# ---------------------------------------------------------------------------
MAX_QUEUE_ENTRIES = 64
MAX_CLOSED_RECORDS = 256
MAX_ATTEMPTS_LIMIT = 3
DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_CAPACITY = 1
MIN_CAPACITY = 1
MAX_CAPACITY = 16

CLI_NAME = "trajectory-pi-runs"
CLI_TOOL_VERSION = "1.85"
