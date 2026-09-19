"""M033 — mission recovery, stale-state detection and resume-point selection.

Process death must never corrupt canonical evidence. This module owns the
deterministic *recovery decision*: given the durable mission root, it decides
whether a resume is idempotent (the mission already reached a terminal trust
decision), whether a finalized execution can be closed directly, or which
explicit safe phase a resume must restart from.

Design invariants:

* the mission identity is never re-derived; it is read from ``mission.json``
  and cross-checked against the canonical ``status.json`` ``run_id`` — a
  mismatch fails closed and is never silently repaired;
* prior evidence is append-only and preserved: recovery only *reads*
  ``events.jsonl`` / ``status.json`` / ``closure.json``;
* a finalized trust decision is never silently repeated: an existing
  ``closure.json`` always yields an idempotent terminal decision;
* an incomplete/stale execution (an active lifecycle/readiness without a
  durable ``RUN_COMPLETED``) is detected and mapped to an explicit resume
  point instead of being treated as progress.

This module introduces no second status model; it consumes the canonical M030
document and the M031 mission documents only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.assembly import model, store
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import store as obs_store

#: Recovery decision kinds (closed set).
RC_TERMINAL_COMPLETE = "TERMINAL_COMPLETE"
RC_EXECUTION_FINALIZED = "EXECUTION_FINALIZED"
RC_RESUME_EXECUTION = "RESUME_EXECUTION"
RC_RESUME_PLAN = "RESUME_PLAN"
RC_RESUME_PREFLIGHT = "RESUME_PREFLIGHT"
RC_RESUME_INTAKE = "RESUME_INTAKE"
RC_MALFORMED = "MALFORMED"

RECOVERY_KINDS = frozenset({
    RC_TERMINAL_COMPLETE, RC_EXECUTION_FINALIZED, RC_RESUME_EXECUTION,
    RC_RESUME_PLAN, RC_RESUME_PREFLIGHT, RC_RESUME_INTAKE, RC_MALFORMED,
})

#: Kinds that require no further work (already terminal / idempotent).
TERMINAL_KINDS = frozenset({RC_TERMINAL_COMPLETE})

#: The canonical checkpoint document name.
CHECKPOINT_NAME = "recovery.json"


@dataclass(frozen=True)
class ResumeDecision:
    """The explicit, deterministic recovery decision for one mission."""

    mission_id: str
    kind: str
    resume_point: str
    reason: str
    terminal: bool
    idempotent: bool
    execution_finalized: bool
    events: int
    readiness: str | None
    lifecycle: str | None

    def validate(self) -> ResumeDecision:
        if self.kind not in RECOVERY_KINDS:
            raise model.AssemblyError(model.R_MALFORMED, self.kind)
        if not self.mission_id:
            raise model.AssemblyError(model.R_MALFORMED, "mission_id")
        if self.kind in TERMINAL_KINDS and not self.terminal:
            raise model.AssemblyError(
                model.R_MALFORMED, "terminal kind without terminal flag")
        if self.kind not in TERMINAL_KINDS and self.terminal:
            raise model.AssemblyError(
                model.R_MALFORMED, "non-terminal kind marked terminal")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "kind": self.kind,
            "resume_point": self.resume_point,
            "reason": self.reason,
            "terminal": self.terminal,
            "idempotent": self.idempotent,
            "execution_finalized": self.execution_finalized,
            "events": self.events,
            "readiness": self.readiness,
            "lifecycle": self.lifecycle,
        }


def load_identity_status(
    mission_root: Path, mission_id: str,
) -> dict[str, Any] | None:
    """Load ``status.json`` and fail closed on an identity mismatch.

    Returns ``None`` when no status has been written yet. A status document
    whose ``run_id`` differs from the requested mission id is a stale/corrupt
    target and is rejected deterministically.
    """
    try:
        document = obs_store.load_status(mission_root)
    except obs_store.CanonicalStoreError:
        return None
    run_id = document.get("run_id")
    if run_id != mission_id:
        raise model.AssemblyError(
            model.R_IDENTITY_MISMATCH,
            f"status run_id {run_id!r} != mission_id {mission_id!r}")
    return document


def detect_resume(root: str | Path, mission_id: str) -> ResumeDecision:
    """Classify the safe resume point for one mission (fail closed)."""
    if not store.mission_exists(root, mission_id):
        raise model.AssemblyError(model.R_MISSION_MISSING, mission_id)
    mission = store.load_mission(root, mission_id)
    if mission.mission_id != mission_id:
        raise model.AssemblyError(
            model.R_IDENTITY_MISMATCH,
            f"mission document id {mission.mission_id!r} != {mission_id!r}")
    mission_root = store.mission_root(root, mission_id)
    events = obs_store.load_events(mission_root)
    status = load_identity_status(mission_root, mission_id)

    # 1. An existing closure is the finalized trust decision: idempotent.
    if store.closure_exists(root, mission_id):
        return ResumeDecision(
            mission_id=mission_id, kind=RC_TERMINAL_COMPLETE,
            resume_point=model.MP_CLOSURE,
            reason="durable closure already recorded (idempotent resume)",
            terminal=True, idempotent=True, execution_finalized=True,
            events=len(events),
            readiness=_opt_str(status, "readiness"),
            lifecycle=_opt_str(status, "state")).validate()

    # 2. A durable RUN_COMPLETED means execution is finalized; the safe resume
    #    point is the human gate/closure (never a second execution cycle).
    if _execution_finalized(events):
        return ResumeDecision(
            mission_id=mission_id, kind=RC_EXECUTION_FINALIZED,
            resume_point=model.MP_HUMAN_GATE,
            reason="execution finalized; resume at the human gate",
            terminal=False, idempotent=False, execution_finalized=True,
            events=len(events),
            readiness=_opt_str(status, "readiness"),
            lifecycle=_opt_str(status, "state")).validate()

    # 3. A status that claims a terminal lifecycle without a durable
    #    RUN_COMPLETED and without closure is an incomplete/stale write. It is
    #    detected explicitly and mapped to a safe re-execution point.
    if status is not None and str(status.get("state")) == obs_model.LC_COMPLETE:
        return ResumeDecision(
            mission_id=mission_id, kind=RC_RESUME_EXECUTION,
            resume_point=model.MP_EXECUTION,
            reason=("status claims COMPLETE without durable RUN_COMPLETED "
                    "evidence (incomplete/stale execution); re-execute"),
            terminal=False, idempotent=False, execution_finalized=False,
            events=len(events),
            readiness=_opt_str(status, "readiness"),
            lifecycle=_opt_str(status, "state")).validate()

    # 4. No execution evidence: resume from the furthest durable document.
    if store.plan_exists(root, mission_id):
        return ResumeDecision(
            mission_id=mission_id, kind=RC_RESUME_EXECUTION,
            resume_point=model.MP_EXECUTION,
            reason="durable plan present; resume at execution",
            terminal=False, idempotent=False, execution_finalized=False,
            events=len(events),
            readiness=_opt_str(status, "readiness"),
            lifecycle=_opt_str(status, "state")).validate()
    if _preflight_completed(events):
        return ResumeDecision(
            mission_id=mission_id, kind=RC_RESUME_PLAN,
            resume_point=model.MP_PLAN,
            reason="preflight completed; resume at planning",
            terminal=False, idempotent=False, execution_finalized=False,
            events=len(events),
            readiness=_opt_str(status, "readiness"),
            lifecycle=_opt_str(status, "state")).validate()
    return ResumeDecision(
        mission_id=mission_id, kind=RC_RESUME_INTAKE,
        resume_point=model.MP_PREFLIGHT,
        reason="no durable phase evidence; resume at preflight",
        terminal=False, idempotent=False, execution_finalized=False,
        events=len(events),
        readiness=_opt_str(status, "readiness"),
        lifecycle=_opt_str(status, "state")).validate()


def write_recovery_record(root: str | Path,
                          decision: ResumeDecision) -> Path:
    """Persist the explicit recovery decision next to the mission root.

    The write is idempotent: an identical decision never rewrites the file, so
    repeated resumes leave the mission root byte-identical.
    """
    mission_root = store.mission_root(root, decision.mission_id)
    mission_root.mkdir(parents=True, exist_ok=True)
    path = mission_root / CHECKPOINT_NAME
    try:
        existing = obs_store.read_json(path)
    except obs_store.CanonicalStoreError:
        existing = None
    if existing == decision.to_dict():
        return path
    obs_store.write_json(path, decision.to_dict())
    return path


def read_recovery_record(root: str | Path,
                         mission_id: str) -> ResumeDecision | None:
    path = store.mission_root(root, mission_id) / CHECKPOINT_NAME
    try:
        document = obs_store.read_json(path)
    except obs_store.CanonicalStoreError:
        return None
    try:
        return ResumeDecision(
            mission_id=str(document["mission_id"]),
            kind=str(document["kind"]),
            resume_point=str(document["resume_point"]),
            reason=str(document.get("reason", "")),
            terminal=bool(document.get("terminal", False)),
            idempotent=bool(document.get("idempotent", False)),
            execution_finalized=bool(document.get("execution_finalized",
                                                  False)),
            events=int(document.get("events", 0)),
            readiness=_none_or_str(document.get("readiness")),
            lifecycle=_none_or_str(document.get("lifecycle")),
        ).validate()
    except (KeyError, TypeError, ValueError) as exc:
        raise model.AssemblyError(model.R_MALFORMED,
                                  f"recovery record: {exc}") from exc


# --- helpers ------------------------------------------------------------------


def _execution_finalized(events: Sequence[Mapping[str, Any]]) -> bool:
    return any(event.get("kind") == "RUN_COMPLETED" for event in events)


def _preflight_completed(events: Sequence[Mapping[str, Any]]) -> bool:
    return any(event.get("kind") == "PREFLIGHT_COMPLETED"
               for event in events)


def _opt_str(document: Mapping[str, Any] | None, key: str) -> str | None:
    if document is None:
        return None
    value = document.get(key)
    return value if isinstance(value, str) and value else None


def _none_or_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "CHECKPOINT_NAME",
    "RECOVERY_KINDS",
    "RC_EXECUTION_FINALIZED",
    "RC_MALFORMED",
    "RC_RESUME_EXECUTION",
    "RC_RESUME_INTAKE",
    "RC_RESUME_PLAN",
    "RC_RESUME_PREFLIGHT",
    "RC_TERMINAL_COMPLETE",
    "ResumeDecision",
    "detect_resume",
    "load_identity_status",
    "read_recovery_record",
    "write_recovery_record",
]
