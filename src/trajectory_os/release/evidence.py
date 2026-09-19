"""M036 — canonical mission evidence consumption for the release layer.

The release layer never invents mission state. It consumes the canonical
M030 ``status.json`` and the M031 ``closure.json`` (plus the append-only
``events.jsonl``) and derives one explicit, fail-closed
:class:`~trajectory_os.release.model.ReviewGateEvidence`:

* lifecycle ``COMPLETE``;
* readiness ``READY_FOR_COMMIT``;
* an active final independent reviewer;
* a *fresh* review on the exact current patch;
* ``reviewed_patch == current_patch`` with an exact 64-hex semantic identity.

A missing/mismatched/stale fact yields ``ready=False`` plus a stable reason
code — never a silent GO COMMIT.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from trajectory_os.assembly import model as assembly_model
from trajectory_os.assembly import store as assembly_store
from trajectory_os.missions import review_protocol
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import store as obs_store
from trajectory_os.release import model

#: Event kinds that carry a patch identity (in chronological order).
_PATCH_EVIDENCE_KINDS = (
    "PATCH_CAPTURED", "VALIDATION_COMPLETED", "REVIEW_COMPLETED",
)


class MissionEvidence:
    """The validated canonical mission documents consumed by the release."""

    def __init__(self, *, root: str | Path, mission_id: str,
                 mission: assembly_model.MissionDefinition,
                 closure: assembly_model.MissionClosure,
                 status: Mapping[str, Any],
                 events: Sequence[Mapping[str, Any]]) -> None:
        self.root = Path(root)
        self.mission_id = mission_id
        self.mission = mission
        self.closure = closure
        self.status = status
        self.events = events
        self.mission_root = assembly_store.mission_root(self.root, mission_id)


def load_mission_evidence(root: str | Path, mission_id: str) -> MissionEvidence:
    """Load and identity-check the canonical mission documents (fail closed)."""
    if not isinstance(mission_id, str) or not mission_id:
        raise model.ReleaseError(model.R_MALFORMED, "mission_id required")
    if not assembly_store.mission_exists(root, mission_id):
        raise model.ReleaseError(model.R_IDENTITY_MISMATCH,
                                 f"mission {mission_id!r} not found")
    if not assembly_store.closure_exists(root, mission_id):
        raise model.ReleaseError(
            model.R_NOT_READY,
            f"mission {mission_id!r} has no closure evidence yet")
    mission = assembly_store.load_mission(root, mission_id)
    closure = assembly_store.load_closure(root, mission_id)
    mission_root = assembly_store.mission_root(root, mission_id)
    status = obs_store.load_status(mission_root)
    run_id = status.get("run_id")
    if run_id != mission_id:
        raise model.ReleaseError(
            model.R_IDENTITY_MISMATCH,
            f"status run_id {run_id!r} != mission_id {mission_id!r}")
    if closure.mission_id != mission_id or mission.mission_id != mission_id:
        raise model.ReleaseError(model.R_IDENTITY_MISMATCH,
                                 "mission/closure identity mismatch")
    events = obs_store.load_events(mission_root)
    return MissionEvidence(root=root, mission_id=mission_id, mission=mission,
                           closure=closure, status=status, events=events)


def _final_reviewer(status: Mapping[str, Any]) -> Mapping[str, Any]:
    reviewer = status.get("final_reviewer")
    return reviewer if isinstance(reviewer, Mapping) else {}


def _fresh_review(events: Sequence[Mapping[str, Any]],
                  current_patch: str | None,
                  ) -> tuple[bool, str | None, str | None, str | None]:
    """Determine whether the latest patch evidence is a fresh valid PASS.

    Returns ``(fresh, outcome, reason, at)``. The *latest* patch-bearing
    event across capture/validation/review must be a ``REVIEW_COMPLETED``
    for the exact current patch with ``VALID_PASS`` and an active reviewer.
    """
    latest: Mapping[str, Any] | None = None
    for event in events:
        if event.get("kind") in _PATCH_EVIDENCE_KINDS and isinstance(
                event.get("patch"), str):
            latest = event
    if latest is None:
        return (False, None, None, None)
    detail = latest.get("detail")
    detail = detail if isinstance(detail, Mapping) else {}
    outcome = detail.get("outcome")
    outcome_text = outcome if isinstance(outcome, str) else None
    reason = detail.get("reason")
    reason_text = reason if isinstance(reason, str) else None
    at = latest.get("at")
    at_text = at if isinstance(at, str) else None
    if latest.get("kind") != "REVIEW_COMPLETED":
        return (False, outcome_text, reason_text, at_text)
    if current_patch is None or latest.get("patch") != current_patch:
        return (False, outcome_text, reason_text, at_text)
    if (latest.get("result") != obs_model.RESULT_PASS
            or outcome_text != review_protocol.OUTCOME_VALID_PASS):
        return (False, outcome_text, reason_text, at_text)
    if detail.get("active") is not True:
        return (False, outcome_text, reason_text, at_text)
    return (True, outcome_text, reason_text, at_text)


def derive_review_gate(evidence: MissionEvidence) -> model.ReviewGateEvidence:
    """Derive the M036 review/readiness gate (pure over loaded evidence)."""
    status = evidence.status
    closure = evidence.closure
    reviewer = _final_reviewer(status)
    reviewed_patch = closure.reviewed_patch
    current_patch = closure.current_patch
    if reviewed_patch is None:
        reviewed_patch = _opt_str(status.get("reviewed_patch"))
    if current_patch is None:
        current_patch = _opt_str(status.get("current_patch"))

    fresh, outcome, review_reason, review_at = _fresh_review(
        evidence.events, current_patch)

    lifecycle = closure.lifecycle
    readiness = closure.readiness
    require_review = bool(status.get("final_review_enabled", True))
    final_enabled = bool(reviewer.get("enabled", False))
    final_active = bool(reviewer.get("active", False))
    reviewer_model = _opt_str(reviewer.get("model"))
    semantic_identity = (reviewed_patch
                         if model.is_sha256(reviewed_patch) else None)

    reason = model.R_OK
    ready = True
    if lifecycle != obs_model.LC_COMPLETE:
        reason = model.R_LIFECYCLE_NOT_COMPLETE
    elif readiness != obs_model.RD_READY_FOR_COMMIT:
        reason = model.R_READINESS_NOT_READY
    elif not (final_enabled and final_active):
        reason = model.R_NO_ACTIVE_REVIEWER
    elif not (model.is_sha256(reviewed_patch)
              and model.is_sha256(current_patch)):
        reason = model.R_PATCH_INVALID
    elif reviewed_patch != current_patch:
        reason = model.R_PATCH_MISMATCH
    elif not fresh:
        reason = model.R_STALE_REVIEW
    elif outcome != review_protocol.OUTCOME_VALID_PASS:
        reason = model.R_REVIEW_NOT_PASS
    if reason != model.R_OK:
        ready = False

    return model.ReviewGateEvidence(
        mission_id=evidence.mission_id,
        run_id=str(status.get("run_id", evidence.mission_id)),
        lifecycle=lifecycle,
        readiness=readiness,
        require_review=require_review,
        final_review_enabled=final_enabled,
        final_reviewer_active=final_active,
        final_reviewer_model=reviewer_model,
        reviewed_patch=reviewed_patch,
        current_patch=current_patch,
        semantic_patch_identity=semantic_identity,
        fresh_review=fresh,
        review_outcome=outcome,
        review_reason=review_reason,
        review_at=review_at,
        attempts=closure.attempts,
        repairs=closure.repairs,
        ready=ready,
        reason=reason,
    ).validate()


def require_ready(evidence: MissionEvidence) -> model.ReviewGateEvidence:
    """Derive the gate and fail closed unless the mission is ready."""
    gate = derive_review_gate(evidence)
    if not gate.ready:
        raise model.ReleaseError(gate.reason,
                                 _readiness_detail(evidence, gate))
    return gate


def _readiness_detail(evidence: MissionEvidence,
                      gate: model.ReviewGateEvidence) -> str:
    return (f"mission={evidence.mission_id} lifecycle={gate.lifecycle} "
            f"readiness={gate.readiness} fresh={gate.fresh_review} "
            f"reviewed={gate.reviewed_patch} current={gate.current_patch}")


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "MissionEvidence",
    "derive_review_gate",
    "load_mission_evidence",
    "require_ready",
]
