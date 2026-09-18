"""Mission 013 — read-only authoritative mission runtime evidence.

M012's :class:`~trajectory_os.graph.evidence.MissionEvidenceRecord` answers
the *trust* question ("is this mission proven complete?"). Scheduling also
needs bounded *runtime* facts (has the mission started? is it terminal? which
declared execution budget is exhausted?), which this module resolves
read-only from the canonical mission store.

Nothing here mutates, terminalizes or repairs mission state. Missing or
malformed mission documents are surfaced as explicit, fail-closed fields and
never guessed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from trajectory_os.graph import evidence as graph_evidence
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import store as mission_store

#: Mission-level reason codes that prove a declared budget is exhausted.
BUDGET_REASONS = frozenset({
    mission_model.R_TIME_BUDGET_EXHAUSTED,
    mission_model.R_SUBRUN_BUDGET_EXHAUSTED,
    mission_model.R_REPAIR_BUDGET_EXHAUSTED,
})


@dataclass(frozen=True)
class MissionRuntimeEvidence:
    """Bounded, derived runtime view of one referenced mission (never authoritative)."""

    mission_id: str
    resolved: bool
    error: str | None
    state: str | None
    reason: str | None
    proven_complete: bool
    started: bool
    terminal: bool
    failed_or_blocked: bool
    budget_exhausted: bool
    subrun_started: int
    subrun_completed: int
    subrun_budget: int
    repairs_used: int
    repair_budget: int

    @property
    def active(self) -> bool:
        """True while the mission genuinely owns the local resources."""
        return (self.resolved and self.error is None and self.started
                and not self.terminal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "resolved": self.resolved,
            "error": self.error,
            "state": self.state,
            "reason": self.reason,
            "proven_complete": self.proven_complete,
            "started": self.started,
            "terminal": self.terminal,
            "failed_or_blocked": self.failed_or_blocked,
            "budget_exhausted": self.budget_exhausted,
            "subrun_started": self.subrun_started,
            "subrun_completed": self.subrun_completed,
            "subrun_budget": self.subrun_budget,
            "repairs_used": self.repairs_used,
            "repair_budget": self.repair_budget,
        }

    @staticmethod
    def from_dict(doc: object, path: str = "runtime") -> MissionRuntimeEvidence:
        if not isinstance(doc, dict):  # pragma: no cover - defensive
            raise ValueError(f"{path}: runtime evidence object required")
        return MissionRuntimeEvidence(
            mission_id=str(doc["mission_id"]),
            resolved=bool(doc["resolved"]),
            error=doc.get("error"),
            state=doc.get("state"),
            reason=doc.get("reason"),
            proven_complete=bool(doc.get("proven_complete", False)),
            started=bool(doc.get("started", False)),
            terminal=bool(doc.get("terminal", False)),
            failed_or_blocked=bool(doc.get("failed_or_blocked", False)),
            budget_exhausted=bool(doc.get("budget_exhausted", False)),
            subrun_started=int(doc.get("subrun_started", 0)),
            subrun_completed=int(doc.get("subrun_completed", 0)),
            subrun_budget=int(doc.get("subrun_budget", 0)),
            repairs_used=int(doc.get("repairs_used", 0)),
            repair_budget=int(doc.get("repair_budget", 0)),
        )


def resolve_runtime(root: str, mission_id: str) -> MissionRuntimeEvidence:
    """Resolve one mission's trust + runtime evidence strictly read-only."""
    trust = graph_evidence.resolve_mission_evidence(root, mission_id)
    started = False
    terminal = False
    failed_or_blocked = False
    subrun_started = 0
    subrun_completed = 0
    subrun_budget = 0
    repairs_used = 0
    repair_budget = 0
    reason = trust.reason
    if trust.resolved and trust.error is None:
        try:
            mission, _ = mission_store.load_mission(root, mission_id)
        except (mission_store.MissionNotFound,
                mission_store.MalformedMissionError):
            mission = None
        if mission is not None:
            state = mission.mission_state
            started = mission.started_at is not None
            terminal = state in mission_model.TERMINAL_MISSION_STATES
            failed_or_blocked = state in (mission_model.MS_FAILED,
                                          mission_model.MS_BLOCKED)
            subrun_started = mission.subrun_started
            subrun_completed = mission.subrun_completed
            subrun_budget = mission.subrun_budget
            repairs_used = mission.repairs_used
            repair_budget = mission.repair_budget
            reason = mission.mission_reason
    budget_exhausted = reason in BUDGET_REASONS
    return MissionRuntimeEvidence(
        mission_id=mission_id,
        resolved=trust.resolved,
        error=trust.error,
        state=trust.state,
        reason=reason,
        proven_complete=trust.proven_complete,
        started=started,
        terminal=terminal,
        failed_or_blocked=failed_or_blocked,
        budget_exhausted=budget_exhausted,
        subrun_started=subrun_started,
        subrun_completed=subrun_completed,
        subrun_budget=subrun_budget,
        repairs_used=repairs_used,
        repair_budget=repair_budget,
    )


def collect_runtime(root: str, mission_ids: Iterable[str]
                    ) -> dict[str, MissionRuntimeEvidence]:
    """Resolve each unique mission once in deterministic (sorted) order."""
    return {
        mission_id: resolve_runtime(root, mission_id)
        for mission_id in sorted(set(mission_ids))
    }
