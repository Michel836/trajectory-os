"""Mission 003 — pure, deterministic mission state machine (no I/O, no clocks).

The single entry point is :func:`decide`.  Given a fully-loaded
:class:`~trajectory_os.missions.store.MissionDoc` it returns exactly one
deterministic :class:`Decision`:

* ``run_phase``  — launch the next sub-run for one concrete phase;
* ``repair``     — the bounded repair loop applies (repair round ``r``);
* ``complete``   — every phase is proven PASSED;
* ``blocked``    — fail closed with a stable reason (budget/stale/unproven);
* ``failed``     — deterministic hard failure with a stable reason;
* ``noop``       — mission already terminal (absorbing).

Rules (ADR-005 — bounded, fail closed, evidence-based):

* phases execute in listed order; a phase is runnable only when every
  dependency is PASSED (a missing/skipped dependency is BLOCKED, never
  silently ignored);
* a failed phase (any kind) consumes at most one repair round before it
  may be re-evidenced; total repairs stay within ``repair_budget``; repair
  exhaustion is BLOCKED (not FAILED) — repair outcomes are exactly what
  bounded human review is for;
* a REPAIR phase that fails is a hard stop (BLOCKED ``REPAIR_FAILED``):
  the repair loop itself is bounded and never self-heals unboundedly;
* a phase left UNPROVEN (stale/ambiguous evidence) is re-evidenced within
  ``max_attempts`` and otherwise BLOCKED with ``UNPROVEN_SUBRUN`` —
  never guessed;
* mission-state transitions are validated against the canonical legal map
  (:func:`assert_transition`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trajectory_os.missions import model
from trajectory_os.missions.store import MissionDoc, PhaseDoc


class IllegalTransitionError(Exception):
    """An attempted mission-state transition is not in the legal map."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def assert_transition(current: str, target: str) -> None:
    if current not in model.MISSION_TRANSITIONS:
        raise IllegalTransitionError(model.R_ILLEGAL_TRANSITION,
                                     f"unknown state {current!r}")
    if target not in model.MISSION_TRANSITIONS:
        raise IllegalTransitionError(model.R_ILLEGAL_TRANSITION,
                                     f"unknown state {target!r}")
    if current == target:
        return  # idempotent no-op is legal
    if target not in model.MISSION_TRANSITIONS[current]:
        raise IllegalTransitionError(
            model.R_ILLEGAL_TRANSITION,
            f"{current!r} -> {target!r} is not a legal mission transition",
        )


@dataclass(frozen=True)
class Decision:
    action: str                  # run_phase | repair | complete | blocked | failed | noop
    phase_id: str | None = None  # phase to act on (run_phase / blocked/failed target)
    phase_round: int = 0         # repair round for action == "repair"
    mission_state: str | None = None  # target mission-level state (if any)
    reason: str = model.R_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "phase_id": self.phase_id,
            "phase_round": self.phase_round,
            "mission_state": self.mission_state,
            "reason": self.reason,
        }


def _repairs_available(mission: MissionDoc) -> bool:
    return mission.repairs_used < mission.repair_budget


def decide(mission: MissionDoc) -> Decision:
    """Deterministic single-step decision from canonical state (pure)."""
    if mission.mission_state in model.TERMINAL_MISSION_STATES:
        return Decision(action="noop", reason=mission.mission_reason)

    states = mission.phase_states()

    # 0) a repair that failed (exhausted) hard-stops the mission: the repair
    #    loop is bounded and never self-heals unboundedly (fail closed).
    for p in mission.phases:
        if (p.kind == model.PH_REPAIR
                and p.state in (model.PS_FAILED, model.PS_UNPROVEN)
                and p.attempt >= p.max_attempts):
            return Decision(action="blocked", phase_id=p.phase_id,
                            mission_state=model.MS_BLOCKED,
                            reason=model.R_REPAIR_FAILED)

    # 1) a pending repair round always precedes any re-attempt — it was
    #    created before we got here (repair rounds are PENDING until run).
    pending_repairs = sorted(
        (p for p in mission.phases
         if p.kind == model.PH_REPAIR and p.state == model.PS_PENDING),
        key=lambda p: (p.round, p.phase_id),
    )
    if pending_repairs:
        pr = pending_repairs[0]
        if _repairs_available(mission):
            return Decision(action="run_phase", phase_id=pr.phase_id,
                            mission_state=model.MS_REPAIRING, reason=model.R_OK)
        return Decision(action="blocked", phase_id=pr.phase_id,
                        mission_state=model.MS_BLOCKED,
                        reason=model.R_REPAIR_BUDGET_EXHAUSTED)

    # 2) first unsatisfied phase in canonical order.
    for phase in mission.phases:
        if phase.state == model.PS_PASSED:
            continue
        if not phase.depends_satisfied(states):
            return Decision(action="blocked", phase_id=phase.phase_id,
                            mission_state=model.MS_BLOCKED,
                            reason=model.R_DEPENDENCY_MISSING)
        if phase.state in (model.PS_PENDING, model.PS_RUNNING):
            # RUNNING with no terminal evidence is ambiguous: fail closed.
            if phase.state == model.PS_RUNNING:
                return Decision(action="blocked", phase_id=phase.phase_id,
                                mission_state=model.MS_BLOCKED,
                                reason=model.R_STALE_EVIDENCE)
            if not phase.command:
                return Decision(action="blocked", phase_id=phase.phase_id,
                                mission_state=model.MS_BLOCKED,
                                reason=model.R_MALFORMED_STATE)
            return Decision(action="run_phase", phase_id=phase.phase_id,
                            mission_state=model.KIND_TO_MISSION_STATE[phase.kind],
                            reason=model.R_OK)
        if phase.state == model.PS_FAILED:
            return _failure_decision(mission, phase)
        if phase.state == model.PS_UNPROVEN:
            return _unproven_decision(phase)
        raise AssertionError("unreachable phase state")  # coverage guard

    # 3) every phase is proven PASSED.
    return Decision(action="complete", mission_state=model.MS_COMPLETE,
                    reason=model.R_COMPLETE)


def _failure_decision(mission: MissionDoc, phase: PhaseDoc) -> Decision:
    """Decision for a phase whose last attempt FAILED (bounded repair loop).

    The failed phase consumes exactly one repair round before it may be
    re-evidenced (repair = bounded fresh-context run of the phase that most
    directly can fix). Each round costs one repair-budget unit; a failed
    repair hard-stops (finalize in the orchestrator). Exhaustion is
    BLOCKED, never FAILED — the bounded evidence is what a human reviews.

    Repair-since-attempt comparison invariant: ``repairs_used`` is
    monotonic (never decremented anywhere), and ``repairs_at_attempt`` is
    refreshed unconditionally to ``repairs_used`` in the SAME atomic persist
    that takes a phase out of RUNNING (see
    ``orchestrator.finalize_subrun``).  The decision function only reads
    phases that are in a terminal (decidable) state, so it never sees a
    half-updated pair; a crash mid-finalize leaves the phase RUNNING, which
    this function explicitly fails closed on (``STALE_EVIDENCE``).
    """
    if mission.repairs_used > phase.repairs_at_attempt:
        # Invariant above: a repair round completed AFTER this phase's last
        # attempt -> re-evidence the phase itself (bounded by its attempts).
        if phase.attempt < phase.max_attempts:
            return Decision(action="run_phase", phase_id=phase.phase_id,
                            mission_state=model.KIND_TO_MISSION_STATE[phase.kind],
                            reason=model.R_OK)
        return Decision(action="blocked", phase_id=phase.phase_id,
                        mission_state=model.MS_BLOCKED, reason=model.R_REPAIR_FAILED)
    if _repairs_available(mission):
        round_no = mission.repairs_used + 1
        if round_no > model.MAX_REPAIR_ROUNDS:
            return Decision(action="blocked", phase_id=phase.phase_id,
                            mission_state=model.MS_BLOCKED,
                            reason=model.R_REPAIR_BUDGET_EXHAUSTED)
        return Decision(action="repair", phase_id=phase.phase_id,
                        phase_round=round_no, reason=model.R_OK)
    return Decision(action="blocked", phase_id=phase.phase_id,
                    mission_state=model.MS_BLOCKED,
                    reason=model.R_REPAIR_BUDGET_EXHAUSTED)


def _unproven_decision(phase: PhaseDoc) -> Decision:
    """Decision for a phase whose last attempt is UNPROVEN (ambiguous).

    Re-evidence within bounded attempts (fresh context), otherwise close
    with ``UNPROVEN_SUBRUN`` — never guess a pass.
    """
    if phase.attempt < phase.max_attempts:
        return Decision(action="run_phase", phase_id=phase.phase_id,
                        mission_state=model.KIND_TO_MISSION_STATE[phase.kind],
                        reason=model.R_OK)
    return Decision(action="blocked", phase_id=phase.phase_id,
                    mission_state=model.MS_BLOCKED, reason=model.R_UNPROVEN_SUBRUN)
