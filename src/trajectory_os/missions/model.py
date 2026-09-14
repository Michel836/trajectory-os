"""Mission 003 — canonical vocabulary for bounded multi-phase mission orchestration.

Constants only: mission-level states, phase kinds/states, sub-run
classifications, deterministic reason codes, and hard bounded limits.

Design invariants (ADR-005):

* deterministic and pure — no I/O, no clocks, no randomness here;
* fail closed — stale/ambiguous/malformed evidence never advances a mission,
  it blocks it with a stable reason code;
* bounded — every loop is bounded by small hard limits; no unbounded retry,
  requeue, or wait;
* no unproven ownership, no speculative state, no silent scope expansion.
"""

from __future__ import annotations

import re

#: Schema version of the durable mission model (forward-compatible checks).
SCHEMA_VERSION = 1

# --- mission-level canonical states -----------------------------------------

MS_PLANNING = "PLANNING"
MS_RUNNING = "RUNNING"
MS_VALIDATING = "VALIDATING"
MS_REVIEWING = "REVIEWING"
MS_REPAIRING = "REPAIRING"
MS_CONSOLIDATING = "CONSOLIDATING"
MS_BLOCKED = "BLOCKED"
MS_COMPLETE = "COMPLETE"
MS_FAILED = "FAILED"

MISSION_STATES = frozenset({
    MS_PLANNING, MS_RUNNING, MS_VALIDATING, MS_REVIEWING, MS_REPAIRING,
    MS_CONSOLIDATING, MS_BLOCKED, MS_COMPLETE, MS_FAILED,
})

INITIAL_MISSION_STATE = MS_PLANNING

#: Absorbing states: never transition out (recovery starts a new mission).
TERMINAL_MISSION_STATES = frozenset({MS_BLOCKED, MS_COMPLETE, MS_FAILED})

# --- phase kinds -------------------------------------------------------------

PH_PLAN = "PLAN"
PH_IMPLEMENT = "IMPLEMENT"
PH_VALIDATE = "VALIDATE"
PH_REVIEW = "REVIEW"
PH_REPAIR = "REPAIR"
PH_CONSOLIDATE = "CONSOLIDATE"

PHASE_KINDS = frozenset({
    PH_PLAN, PH_IMPLEMENT, PH_VALIDATE, PH_REVIEW, PH_REPAIR, PH_CONSOLIDATE,
})

#: Canonical mission core sequence (the default phase plan).
CANONICAL_SEQUENCE = (PH_PLAN, PH_IMPLEMENT, PH_VALIDATE, PH_REVIEW, PH_CONSOLIDATE)

#: Phases whose failure may trigger the bounded repair loop.
REPAIRABLE_KINDS = frozenset({PH_VALIDATE, PH_REVIEW})

#: Kind -> canonical Mission 002 execution mode the sub-run uses.
KIND_TO_MODE = {
    PH_PLAN: "SMOKE",
    PH_IMPLEMENT: "IMPLEMENT",
    PH_VALIDATE: "VERIFY",
    PH_REVIEW: "REVIEW",
    PH_REPAIR: "REPAIR",
    PH_CONSOLIDATE: "VERIFY",
}

#: Kind -> mission-level state while that phase is active.
KIND_TO_MISSION_STATE = {
    PH_PLAN: MS_PLANNING,
    PH_IMPLEMENT: MS_RUNNING,
    PH_VALIDATE: MS_VALIDATING,
    PH_REVIEW: MS_REVIEWING,
    PH_REPAIR: MS_REPAIRING,
    PH_CONSOLIDATE: MS_CONSOLIDATING,
}

#: Default phase id per core kind (REPAIR rounds are numbered: ``repair.1`` ...).
KIND_TO_PHASE_ID = {
    PH_PLAN: "plan",
    PH_IMPLEMENT: "implement",
    PH_VALIDATE: "validate",
    PH_REVIEW: "review",
    PH_CONSOLIDATE: "consolidate",
}

REPAIR_PHASE_ID_PREFIX = "repair."

# --- phase states --------------------------------------------------------------

PS_PENDING = "PENDING"
PS_RUNNING = "RUNNING"
PS_PASSED = "PASSED"
PS_FAILED = "FAILED"
PS_UNPROVEN = "UNPROVEN"

PHASE_STATES = frozenset({
    PS_PENDING, PS_RUNNING, PS_PASSED, PS_FAILED, PS_UNPROVEN,
})

TERMINAL_PHASE_STATES = frozenset({PS_PASSED, PS_FAILED, PS_UNPROVEN})

# --- sub-run classifications --------------------------------------------------

#: Non-terminal: sub-run persisted before launch; crash between these writes
#: leaves an explicit, fail-closed record (never guessed).
CR_RUNNING = "RUNNING"

CR_COMPLETED = "COMPLETED"
CR_FAILED = "FAILED"
CR_CRASHED = "CRASHED"
CR_UNPROVEN = "UNPROVEN"

SUBRUN_CLASSIFICATIONS = frozenset({
    CR_RUNNING, CR_COMPLETED, CR_FAILED, CR_CRASHED, CR_UNPROVEN,
})

TERMINAL_SUBRUN_CLASSIFICATIONS = frozenset({
    CR_COMPLETED, CR_FAILED, CR_CRASHED, CR_UNPROVEN,
})

#: Classifications that count as a provider/infrastructure failure.
PROVIDER_FAILURE_CLASSIFICATIONS = frozenset({CR_CRASHED, CR_UNPROVEN})

#: Deterministic exit-code classification (fail closed on ambiguity):
#:    0                -> COMPLETED
#:    1..120           -> FAILED (deterministic failure; repair-eligible)
#:    any other nonzero-> CRASHED (signal/infrastructure; provider failure)
#:    None             -> UNPROVEN (no exit evidence; fail closed)
MAX_DETERMINISTIC_FAILURE_EXIT = 120

# --- deterministic reason codes ------------------------------------------------

R_OK = "OK"
R_COMPLETE = "COMPLETE"
R_SUBRUN_FAILED = "SUBRUN_FAILED"
R_PROVIDER_FAILURE = "PROVIDER_FAILURE"
R_REPAIR_FAILED = "REPAIR_FAILED"
R_REPAIR_BUDGET_EXHAUSTED = "REPAIR_BUDGET_EXHAUSTED"
R_TIME_BUDGET_EXHAUSTED = "TIME_BUDGET_EXHAUSTED"
R_SUBRUN_BUDGET_EXHAUSTED = "SUBRUN_BUDGET_EXHAUSTED"
R_UNPROVEN_SUBRUN = "UNPROVEN_SUBRUN"
R_HEAD_DRIFT = "HEAD_DRIFT"
R_STALE_EVIDENCE = "STALE_EVIDENCE"
R_CONTRADICTION = "CONTRADICTION"
R_MALFORMED_STATE = "MALFORMED_STATE"
R_RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
R_WORKSPACE_CONFLICT = "WORKSPACE_CONFLICT"
R_LEGAL = "LEGAL"
R_ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
R_DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
R_DUPLICATE_IDENTITY = "DUPLICATE_IDENTITY"
R_SESSION_BOUND = "SESSION_BOUND"

REASON_CODES = frozenset({
    R_OK, R_COMPLETE, R_SUBRUN_FAILED, R_PROVIDER_FAILURE, R_REPAIR_FAILED,
    R_REPAIR_BUDGET_EXHAUSTED, R_TIME_BUDGET_EXHAUSTED,
    R_SUBRUN_BUDGET_EXHAUSTED, R_UNPROVEN_SUBRUN, R_HEAD_DRIFT, R_STALE_EVIDENCE,
    R_CONTRADICTION, R_MALFORMED_STATE, R_RESOURCE_UNAVAILABLE,
    R_WORKSPACE_CONFLICT, R_LEGAL, R_ILLEGAL_TRANSITION, R_DEPENDENCY_MISSING,
    R_DUPLICATE_IDENTITY, R_SESSION_BOUND,
})

# --- hard bounded limits (no configuration may exceed these) -------------------

MAX_PHASES = 16
MAX_DEPENDENCIES = 8
MAX_PHASES_PER_ID = 4
MAX_REPAIR_ROUNDS = 3
MAX_ATTEMPTS_PER_PHASE = 4
MAX_SUBRUNS = 64
MAX_SESSION_SUBRUNS = 32
MAX_HUMAN_INTERVENTIONS = 8
MAX_OBJECTIVE_LEN = 4096
MAX_NOTE_LEN = 2048
MAX_REVISION_LEN = 256
MAX_COMMAND_PARTS = 64
MAX_COMMAND_PART_LEN = 4096
MAX_EVENT_LOG = 512

MIN_TIME_BUDGET_S = 60
MAX_TIME_BUDGET_S = 86400
DEFAULT_TIME_BUDGET_S = 7200

MIN_SUBRUN_TIMEOUT_S = 5
MAX_SUBRUN_TIMEOUT_S = 43200
DEFAULT_SUBRUN_TIMEOUT_S = 1800

#: Mission ids: lowercase slug (reuse the V1.89 id grammar for compatibility).
#: A digit-leading first character (``[a-z0-9]``) is deliberate, not an
#: oversight: it mirrors the V1.89 run/job id grammar (see
#: ``trajectory_os.runs.JOB_ID_RE``) so canonical ids such as
#: ``20260914-215136`` stay valid — mission ids may legitimately mirror
#: operator-derived run ids (one mission per run). Restricting the first
#: character to letters would break that compatibility.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

#: Mission-level legal transition map (absorbing terminals; deterministic).
MISSION_TRANSITIONS: dict[str, frozenset[str]] = {
    # REPAIRING is reachable from PLANNING/RUNNING as well: any failed phase
    # (plan or implement included) may trigger the bounded repair round.
    MS_PLANNING: frozenset({MS_RUNNING, MS_REPAIRING, MS_BLOCKED, MS_FAILED}),
    MS_RUNNING: frozenset({MS_VALIDATING, MS_REPAIRING, MS_BLOCKED, MS_FAILED}),
    MS_VALIDATING: frozenset({MS_REVIEWING, MS_REPAIRING, MS_BLOCKED, MS_FAILED}),
    MS_REVIEWING: frozenset({MS_CONSOLIDATING, MS_REPAIRING, MS_BLOCKED, MS_FAILED}),
    MS_REPAIRING: frozenset({MS_VALIDATING, MS_REVIEWING, MS_BLOCKED, MS_FAILED}),
    MS_CONSOLIDATING: frozenset({MS_COMPLETE, MS_BLOCKED, MS_FAILED}),
    MS_BLOCKED: frozenset(),
    MS_COMPLETE: frozenset(),
    MS_FAILED: frozenset(),
}
