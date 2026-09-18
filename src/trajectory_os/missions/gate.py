"""Mission 011 — reduced-intervention operator gate projection (pure).

The autonomous mission loop already performs every internal transition
(PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> bounded REPAIR -> REVALIDATE ->
REVIEW -> CONSOLIDATE) without human approval.  This module makes the
*remaining* human trust boundaries explicit and derivable from persisted
evidence alone:

    LAUNCH -> (autonomous execution) -> GO COMMIT -> GO MERGE

Design invariants:

* **pure and deterministic** — the gate is derived from the canonical
  persisted mission document (plus the two approval markers persisted on
  it); no I/O, no clocks, no randomness here;
* **fail closed** — an unknown/terminal-but-non-green state maps to a
  consolidated ``STOP`` gate, never to a silent GO COMMIT;
* **additive** — no new mission state, no schema-version move; legacy
  mission documents (no approval fields) stay readable and derive exactly
  the same gates they always implicitly had;
* **human authority preserved** — this module only *describes* the gate
  and the exact next human action.  It never performs a Git write and never
  advances a gate by itself.

Gate vocabulary:

* ``LAUNCH``      the mission is created and not yet launched (human action);
* ``AUTONOMOUS``  launched and progressing inside the bounded machine loop
                  (no human action required);
* ``GO_COMMIT``   the autonomous lifecycle is green; a human must review and
                  commit (human action);
* ``GO_MERGE``    the human commit was recorded; a human must integrate/merge
                  (human action);
* ``STOP``        the autonomous bounds were exhausted or a fail-closed guard
                  fired; one consolidated human decision is required
                  (human action);
* ``DONE``        the human merge was recorded; the mission pipeline is closed
                  (no human action).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trajectory_os.missions import model

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module pure
    from trajectory_os.missions.store import MissionDoc

# --- canonical gate states -----------------------------------------------------

GATE_LAUNCH = "LAUNCH"
GATE_AUTONOMOUS = "AUTONOMOUS"
GATE_GO_COMMIT = "GO_COMMIT"
GATE_GO_MERGE = "GO_MERGE"
GATE_STOP = "STOP"
GATE_DONE = "DONE"

GATE_STATES = frozenset({
    GATE_LAUNCH, GATE_AUTONOMOUS, GATE_GO_COMMIT, GATE_GO_MERGE, GATE_STOP,
    GATE_DONE,
})

#: Gates that genuinely require a human trust-boundary decision.
HUMAN_GATES = frozenset({GATE_LAUNCH, GATE_GO_COMMIT, GATE_GO_MERGE, GATE_STOP})

#: Gates at which no human action is required (autonomous or closed).
AUTONOMOUS_GATES = frozenset({GATE_AUTONOMOUS, GATE_DONE})

# --- stable gate reason codes --------------------------------------------------

REASON_LAUNCH_PENDING = "LAUNCH_PENDING"
REASON_AUTONOMOUS = "AUTONOMOUS_IN_PROGRESS"
REASON_READY_FOR_COMMIT = "READY_FOR_COMMIT"
REASON_COMMIT_APPROVED = "COMMIT_APPROVED_AWAITING_MERGE"
REASON_MERGE_APPROVED = "MERGE_APPROVED_PIPELINE_DONE"
REASON_CONSOLIDATED = "CONSOLIDATED_HUMAN_DECISION"

GATE_REASONS = frozenset({
    REASON_LAUNCH_PENDING, REASON_AUTONOMOUS, REASON_READY_FOR_COMMIT,
    REASON_COMMIT_APPROVED, REASON_MERGE_APPROVED, REASON_CONSOLIDATED,
})


def derive_gate(mission: MissionDoc) -> str:
    """Deterministic current gate from canonical persisted state (pure).

    The two approval markers are checked first so a recorded human decision
    can never be silently re-offered, even if a later state derivation would
    otherwise suggest an earlier gate.  ``COMPLETE`` is the internal
    lifecycle's green terminal and therefore maps to GO COMMIT, never to an
    implicit success: a human still has to commit.
    """
    if mission.merge_approved_at is not None:
        return GATE_DONE
    if mission.commit_approved_at is not None:
        return GATE_GO_MERGE
    if mission.mission_state == model.MS_COMPLETE:
        return GATE_GO_COMMIT
    if mission.mission_state in (model.MS_BLOCKED, model.MS_FAILED):
        return GATE_STOP
    if mission.started_at is None:
        return GATE_LAUNCH
    return GATE_AUTONOMOUS


def gate_reason(mission: MissionDoc, gate_state: str) -> str:
    """Stable machine-readable reason for the current gate (pure)."""
    if gate_state == GATE_LAUNCH:
        return REASON_LAUNCH_PENDING
    if gate_state == GATE_AUTONOMOUS:
        return REASON_AUTONOMOUS
    if gate_state == GATE_GO_COMMIT:
        return REASON_READY_FOR_COMMIT
    if gate_state == GATE_GO_MERGE:
        return REASON_COMMIT_APPROVED
    if gate_state == GATE_DONE:
        return REASON_MERGE_APPROVED
    return REASON_CONSOLIDATED


def next_human_action(mission: MissionDoc, gate_state: str) -> str | None:
    """Exact next human action for the current gate (or ``None``).

    The strings are operator instructions only — they never cause a Git
    write by themselves.  The autonomous gates (``AUTONOMOUS``/``DONE``)
    return ``None``: no human action is required.
    """
    mission_id = mission.mission_id
    if gate_state == GATE_LAUNCH:
        return (f"launch: trajectory-pi-missions run {mission_id} "
                f"(or start)")
    if gate_state == GATE_GO_COMMIT:
        return (f"review GO COMMIT evidence, commit the verified worktree "
                f"externally, then record: trajectory-pi-missions "
                f"approve-commit {mission_id} --revision <sha>")
    if gate_state == GATE_GO_MERGE:
        return (f"push / open pull request / pass CI / merge externally, "
                f"then record: trajectory-pi-missions approve-merge "
                f"{mission_id}")
    if gate_state == GATE_STOP:
        return (f"review consolidated evidence ({mission.mission_reason}); "
                f"repair externally or resume the mission; no automatic "
                f"continuation")
    return None


def gate_view(mission: MissionDoc) -> dict[str, Any]:
    """Canonical operator gate block (pure, derived only from state).

    Exposes, at minimum, the current gate, whether a human action is
    required, the stable reason, the exact next human action, and the
    remaining autonomous repair/sub-run budget.  The caller (summary)
    augments this with the supporting evidence and repository identity it
    already derives, keeping the gate projection itself pure.
    """
    gate_state = derive_gate(mission)
    repairs_remaining = max(0, mission.repair_budget - mission.repairs_used)
    subruns_remaining = max(0, mission.subrun_budget - mission.subrun_started)
    return {
        "gate": gate_state,
        "human_action_required": gate_state in HUMAN_GATES,
        "reason": gate_reason(mission, gate_state),
        "mission_reason": mission.mission_reason,
        "next_human_action": next_human_action(mission, gate_state),
        "autonomous_budget": {
            "repairs_used": mission.repairs_used,
            "repair_budget": mission.repair_budget,
            "repairs_remaining": repairs_remaining,
            "subruns_started": mission.subrun_started,
            "subrun_budget": mission.subrun_budget,
            "subruns_remaining": subruns_remaining,
            "time_budget_s": mission.time_budget_s,
            "time_budget_deadline": mission.time_budget_deadline,
            "waiting_for_human": gate_state in HUMAN_GATES,
        },
    }
