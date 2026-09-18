"""Mission 012 — read-only mission reference resolution for the goal graph.

The goal graph never stores mission trust evidence. It stores only explicit,
bounded mission *references*; this module resolves those references against
the canonical mission store at projection time and returns a small,
bounded record. The canonical mission evidence remains authoritative:

* resolution is strictly read-only — no mission document is written,
  terminalized or repaired here;
* a referenced mission that cannot be read, is malformed, or whose
  ``COMPLETE`` claim is contradicted by its sub-run evidence is surfaced
  as an explicit fail-closed error, never silently interpreted;
* the returned record carries only bounded summary facts (state, reason,
  phase counts, proven flag), never sub-run payloads or attestation data.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from trajectory_os.missions import model, orchestrator, store

#: Stable reference-resolution error codes (fail closed).
ERR_NOT_FOUND = "MISSION_NOT_FOUND"
ERR_MALFORMED = "MISSION_MALFORMED"
ERR_CONTRADICTION = "MISSION_CONTRADICTION"

ERROR_CODES = frozenset({ERR_NOT_FOUND, ERR_MALFORMED, ERR_CONTRADICTION})

#: Errors that make a reference structurally invalid (fail closed) rather
#: than merely unresolved. A missing **required** reference is unresolved;
#: a malformed/contradictory mission is invalid regardless of ``required``.
INVALID_ERRORS = frozenset({ERR_MALFORMED, ERR_CONTRADICTION})


@dataclass(frozen=True)
class MissionEvidenceRecord:
    """Bounded, derived view of one referenced mission (never authoritative)."""

    mission_id: str
    resolved: bool
    error: str | None
    state: str | None
    reason: str | None
    phases_passed: int
    phases_total: int
    proven_complete: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "mission_id": self.mission_id,
            "resolved": self.resolved,
            "error": self.error,
            "state": self.state,
            "reason": self.reason,
            "phases_passed": self.phases_passed,
            "phases_total": self.phases_total,
            "proven_complete": self.proven_complete,
        }


def _record(mission_id: str, *, resolved: bool, error: str | None,
            state: str | None, reason: str | None, phases_passed: int,
            phases_total: int) -> MissionEvidenceRecord:
    proven = (
        resolved
        and error is None
        and state == model.MS_COMPLETE
        and reason == model.R_COMPLETE
        and phases_total > 0
        and phases_passed == phases_total
    )
    return MissionEvidenceRecord(
        mission_id=mission_id,
        resolved=resolved,
        error=error,
        state=state,
        reason=reason,
        phases_passed=phases_passed,
        phases_total=phases_total,
        proven_complete=proven,
    )


def resolve_mission_evidence(root: str, mission_id: str) -> MissionEvidenceRecord:
    """Resolve one reference strictly read-only (fail closed, never mutates).

    ``COMPLETE`` is only accepted as *proven* after the canonical
    orchestrator re-validates that every persisted ``PASSED`` phase still
    has ``COMPLETED`` sub-run evidence. That validation is a read-only
    branch for terminal missions; non-terminal missions are never passed
    through reconstruction (so no in-flight evidence is ever terminalized
    by a graph projection).
    """
    try:
        mission, _ = store.load_mission(root, mission_id)
    except store.MissionNotFound:
        return _record(mission_id, resolved=False, error=ERR_NOT_FOUND,
                       state=None, reason=None, phases_passed=0,
                       phases_total=0)
    except store.MalformedMissionError as exc:
        return _record(mission_id, resolved=False, error=ERR_MALFORMED,
                       state=None, reason=str(exc.code), phases_passed=0,
                       phases_total=0)
    phases_total = len(mission.phases)
    phases_passed = sum(
        1 for phase in mission.phases if phase.state == model.PS_PASSED)
    if mission.mission_state != model.MS_COMPLETE:
        return _record(
            mission_id, resolved=True, error=None,
            state=mission.mission_state, reason=mission.mission_reason,
            phases_passed=phases_passed, phases_total=phases_total)
    # Terminal COMPLETE: canonical read-only re-validation of the proof.
    try:
        report = orchestrator.reconstruct(root, mission_id)
    except store.MalformedMissionError as exc:
        return _record(mission_id, resolved=True, error=ERR_CONTRADICTION,
                       state=mission.mission_state, reason=str(exc.code),
                       phases_passed=phases_passed, phases_total=phases_total)
    return _record(
        mission_id, resolved=True, error=None,
        state=report.mission_state, reason=report.mission_reason,
        phases_passed=phases_passed, phases_total=phases_total)


def collect_evidence(root: str, mission_ids: Iterable[str]
                     ) -> dict[str, MissionEvidenceRecord]:
    """Resolve each unique mission reference once (deterministic order)."""
    return {
        mission_id: resolve_mission_evidence(root, mission_id)
        for mission_id in sorted(mission_ids)
    }
