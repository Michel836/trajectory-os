"""M042 — full-lifecycle deterministic recovery.

Extends mission recovery (M033) beyond execution to the **whole** release
lifecycle: execution, validation, final review, commit-handoff creation, GO
COMMIT execution/persistence, push persistence, PR creation/discovery, CI
watch, merge handoff, GO MERGE/persistence and release closure.

:func:`decide_recovery` is a pure read over the durable artifacts. It never
repeats an irreversible action: an already-completed commit, push, PR, merge
or closure is *discovered*, not re-executed. A local/remote contradiction
fails closed with an explicit contradiction list.

:func:`record_recovery` persists the decision idempotently (an identical
decision never rewrites the file), so repeated recover/resume leaves the
mission root byte-identical.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.assembly import model as assembly_model
from trajectory_os.assembly import recovery as assembly_recovery
from trajectory_os.assembly import store as assembly_store
from trajectory_os.observability import model as obs_model
from trajectory_os.operator import model
from trajectory_os.operator._util import (
    read_optional_json,
    utc_now,
    write_json,
)
from trajectory_os.release import model as release_model
from trajectory_os.release import store as release_store

# --- lifecycle stages (closed set, ordered) ----------------------------------

LS_EXECUTION = "EXECUTION"
LS_VALIDATION = "VALIDATION"
LS_FINAL_REVIEW = "FINAL_REVIEW"
LS_COMMIT_HANDOFF = "COMMIT_HANDOFF"
LS_GO_COMMIT = "GO_COMMIT"
LS_PUSH = "PUSH"
LS_PR_BIND = "PR_BIND"
LS_CI_WATCH = "CI_WATCH"
LS_MERGE_HANDOFF = "MERGE_HANDOFF"
LS_GO_MERGE = "GO_MERGE"
LS_RELEASE_CLOSURE = "RELEASE_CLOSURE"

LIFECYCLE_STAGES = (
    LS_EXECUTION, LS_VALIDATION, LS_FINAL_REVIEW, LS_COMMIT_HANDOFF,
    LS_GO_COMMIT, LS_PUSH, LS_PR_BIND, LS_CI_WATCH, LS_MERGE_HANDOFF,
    LS_GO_MERGE, LS_RELEASE_CLOSURE,
)

# --- recovery actions (closed set) -------------------------------------------

RA_CONTINUE = "CONTINUE"
RA_RESUME_EXECUTION = "RESUME_EXECUTION"
RA_RESUME_REVIEW = "RESUME_REVIEW"
RA_BUILD_HANDOFF = "BUILD_COMMIT_HANDOFF"
RA_AWAIT_GO_COMMIT = "AWAIT_GO_COMMIT"
RA_BIND_PR = "BIND_OR_DISCOVER_PR"
RA_WATCH_CI = "WATCH_EXACT_HEAD_CI"
RA_BUILD_MERGE_HANDOFF = "BUILD_MERGE_HANDOFF"
RA_AWAIT_GO_MERGE = "AWAIT_GO_MERGE"
RA_RECORD_CLOSURE = "RECORD_RELEASE_CLOSURE"
RA_ALREADY_COMPLETE = "ALREADY_COMPLETE"
RA_BLOCKED = "BLOCKED"

RECOVERY_ACTIONS = frozenset({
    RA_CONTINUE, RA_RESUME_EXECUTION, RA_RESUME_REVIEW, RA_BUILD_HANDOFF,
    RA_AWAIT_GO_COMMIT, RA_BIND_PR, RA_WATCH_CI, RA_BUILD_MERGE_HANDOFF,
    RA_AWAIT_GO_MERGE, RA_RECORD_CLOSURE, RA_ALREADY_COMPLETE, RA_BLOCKED,
})

#: Irreversible actions that are discovered, never repeated.
IRREVERSIBLE_COMMIT = "COMMIT"
IRREVERSIBLE_PUSH = "PUSH"
IRREVERSIBLE_PR = "PULL_REQUEST"
IRREVERSIBLE_MERGE = "MERGE"
IRREVERSIBLE_CLOSURE = "RELEASE_CLOSURE"


@dataclass(frozen=True)
class RecoveryDecision:
    """One explicit, deterministic full-lifecycle recovery decision."""

    mission_id: str
    stage: str
    action: str
    reason: str
    resume_point: str
    already_done: tuple[str, ...]
    contradictions: tuple[str, ...]
    terminal: bool
    idempotent: bool
    artifacts: Mapping[str, str]
    next_step: str
    decided_at: str
    schema_version: int = model.SCHEMA_VERSION
    operator_version: str = model.OPERATOR_VERSION

    def validate(self) -> RecoveryDecision:
        if self.stage not in LIFECYCLE_STAGES:
            model.fail(model.E_MALFORMED, f"stage {self.stage!r}")
        if self.action not in RECOVERY_ACTIONS:
            model.fail(model.E_MALFORMED, f"action {self.action!r}")
        if self.action == RA_BLOCKED and not self.contradictions:
            model.fail(model.E_RECOVERY_CONTRADICTION,
                       "BLOCKED recovery requires explicit contradictions")
        if self.action != RA_BLOCKED and self.contradictions:
            model.fail(model.E_RECOVERY_CONTRADICTION,
                       "contradictions require a BLOCKED decision")
        return self

    @property
    def fail_closed(self) -> bool:
        return self.action == RA_BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operator_version": self.operator_version,
            "mission_id": self.mission_id,
            "stage": self.stage,
            "action": self.action,
            "reason": self.reason,
            "resume_point": self.resume_point,
            "already_done": list(self.already_done),
            "contradictions": list(self.contradictions),
            "terminal": self.terminal,
            "idempotent": self.idempotent,
            "artifacts": dict(sorted(self.artifacts.items())),
            "next_step": self.next_step,
            "decided_at": self.decided_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> RecoveryDecision:
        already = data.get("already_done")
        contradictions = data.get("contradictions")
        artifacts = data.get("artifacts")
        return RecoveryDecision(
            mission_id=str(data.get("mission_id", "")),
            stage=str(data.get("stage", "")),
            action=str(data.get("action", "")),
            reason=str(data.get("reason", "")),
            resume_point=str(data.get("resume_point", "")),
            already_done=tuple(str(x) for x in already)
            if isinstance(already, (list, tuple)) else (),
            contradictions=tuple(str(x) for x in contradictions)
            if isinstance(contradictions, (list, tuple)) else (),
            artifacts={str(k): str(v) for k, v in artifacts.items()}
            if isinstance(artifacts, Mapping) else {},
            next_step=str(data.get("next_step", "")),
            terminal=bool(data.get("terminal", False)),
            idempotent=bool(data.get("idempotent", False)),
            decided_at=str(data.get("decided_at", "")),
            schema_version=int(data.get("schema_version",
                                        model.SCHEMA_VERSION)),
            operator_version=str(data.get("operator_version",
                                          model.OPERATOR_VERSION)),
        ).validate()


#: The exact next idempotent operator command per recovery action.
_NEXT_STEP: Mapping[str, str] = {
    RA_CONTINUE: "trajectory status",
    RA_RESUME_EXECUTION: "trajectory resume",
    RA_RESUME_REVIEW: "trajectory resume",
    RA_BUILD_HANDOFF: "trajectory handoff",
    RA_AWAIT_GO_COMMIT: "trajectory go-commit --authorize-commit <token>",
    RA_BIND_PR: "trajectory bind-pr",
    RA_WATCH_CI: "trajectory watch-ci",
    RA_BUILD_MERGE_HANDOFF: "trajectory merge-handoff",
    RA_AWAIT_GO_MERGE: "trajectory go-merge --authorize-merge <token>",
    RA_RECORD_CLOSURE: "trajectory closure",
    RA_ALREADY_COMPLETE: "trajectory status",
    RA_BLOCKED: "trajectory recover --json",
}


def _artifact_paths(root: str, mission_id: str) -> dict[str, str]:
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    names = (
        assembly_store.MISSION_NAME, assembly_store.CLOSURE_NAME,
        release_store.RELEASE_STATE_NAME, release_store.COMMIT_HANDOFF_NAME,
        release_store.COMMIT_RESULT_NAME, release_store.PR_BINDING_NAME,
        release_store.CI_STATUS_NAME, release_store.MERGE_HANDOFF_NAME,
        release_store.MERGE_RESULT_NAME, release_store.RELEASE_CLOSURE_NAME,
        model.POLICY_NAME, model.ROUTING_NAME,
    )
    return {name: str(mission_root / name) for name in names
            if (mission_root / name).is_file()}


def _contradictions(release_state: Mapping[str, Any] | None,
                    commit_result: Mapping[str, Any] | None,
                    pr_binding: Mapping[str, Any] | None,
                    merge_handoff: Mapping[str, Any] | None,
                    merge_result: Mapping[str, Any] | None,
                    release_closure: Mapping[str, Any] | None,
                    commit_handoff: Mapping[str, Any] | None) -> list[str]:
    out: list[str] = []
    state = release_state or {}
    state_stage = state.get("stage")
    state_commit = state.get("commit_sha")

    if commit_result is None and state_commit:
        out.append("release-state records a commit but commit-result is "
                   "missing")
    if commit_result is not None:
        commit_sha = commit_result.get("commit_sha")
        if state_commit and state_commit != commit_sha:
            out.append("release-state commit does not match commit-result")
        if commit_handoff is not None and (
                commit_result.get("reviewed_patch_sha256")
                != commit_handoff.get("reviewed_patch_sha256")):
            out.append("commit-result reviewed patch does not match the "
                       "commit handoff")
    if pr_binding is not None:
        if commit_result is None:
            out.append("pull request bound without a recorded commit")
        elif pr_binding.get("bound_commit_sha") != commit_result.get(
                "commit_sha"):
            out.append("pull request commit does not match commit-result")
    if merge_handoff is not None and commit_result is not None and (
            merge_handoff.get("release_commit_sha")
            != commit_result.get("commit_sha")):
        out.append("merge handoff commit does not match commit-result")
    if merge_result is not None:
        if bool(merge_result.get("merged")) and not merge_result.get(
                "merge_sha"):
            out.append("merge-result claims merged without a merge SHA")
        if merge_handoff is not None and (
                merge_result.get("pr_number")
                != merge_handoff.get("pr_number")):
            out.append("merge-result PR does not match merge handoff")
    if release_closure is not None and commit_result is not None and (
            release_closure.get("commit_sha") != commit_result.get(
                "commit_sha")):
        out.append("release closure commit does not match commit-result")
    if state_stage in (release_model.RST_MERGED, release_model.RST_RELEASED) \
            and merge_result is None:
        out.append("release-state records a merge but merge-result is "
                   "missing")
    return out


def _mission_stage(root: str, mission_id: str,
                   status: Mapping[str, Any] | None) -> tuple[str, str, str]:
    """Return ``(stage, action, reason)`` for a not-yet-releasable mission."""
    try:
        decision = assembly_recovery.detect_resume(root, mission_id)
    except assembly_model.AssemblyError:
        decision = None
    if decision is not None:
        kind = decision.kind
        if kind == assembly_recovery.RC_TERMINAL_COMPLETE:
            return (LS_EXECUTION, RA_CONTINUE,
                    "mission closure exists; awaiting commit handoff")
        if kind == assembly_recovery.RC_EXECUTION_FINALIZED:
            return (LS_FINAL_REVIEW, RA_CONTINUE,
                    "execution finalized; resume at the human gate")
        if kind == assembly_recovery.RC_RESUME_EXECUTION:
            return (LS_EXECUTION, RA_RESUME_EXECUTION,
                    "durable plan present; resume at execution")
        if kind == assembly_recovery.RC_RESUME_PLAN:
            return (LS_EXECUTION, RA_RESUME_EXECUTION,
                    "resume at planning")
        if kind == assembly_recovery.RC_RESUME_PREFLIGHT:
            return (LS_EXECUTION, RA_RESUME_EXECUTION,
                    "resume at preflight")
        if kind == assembly_recovery.RC_RESUME_INTAKE:
            return (LS_EXECUTION, RA_RESUME_EXECUTION,
                    "resume at intake")
    lifecycle = (status or {}).get("state")
    if lifecycle == obs_model.LC_REVIEWING:
        return (LS_FINAL_REVIEW, RA_RESUME_REVIEW,
                "final review in progress")
    if lifecycle in (obs_model.LC_IMPLEMENTING, obs_model.LC_VALIDATING):
        return (LS_VALIDATION, RA_RESUME_EXECUTION,
                "execution/validation in progress")
    return (LS_EXECUTION, RA_CONTINUE, "no release evidence yet")


def decide_recovery(root: str, mission_id: str, *,
                    clock: Callable[[], str] = utc_now) -> RecoveryDecision:
    """Classify the safe, idempotent recovery decision for one mission."""
    if not assembly_store.mission_exists(root, mission_id):
        model.fail(model.E_RECOVERY_MISSING, mission_id)
    mission_root = assembly_store.mission_root(root, mission_id)
    status = assembly_recovery.load_identity_status(mission_root, mission_id)
    closure = read_optional_json(mission_root
                                 / assembly_store.CLOSURE_NAME)
    release_state = read_optional_json(
        mission_root / release_store.RELEASE_STATE_NAME)
    commit_handoff = read_optional_json(
        mission_root / release_store.COMMIT_HANDOFF_NAME)
    commit_result = read_optional_json(
        mission_root / release_store.COMMIT_RESULT_NAME)
    pr_binding = read_optional_json(
        mission_root / release_store.PR_BINDING_NAME)
    ci_status = read_optional_json(
        mission_root / release_store.CI_STATUS_NAME)
    merge_handoff = read_optional_json(
        mission_root / release_store.MERGE_HANDOFF_NAME)
    merge_result = read_optional_json(
        mission_root / release_store.MERGE_RESULT_NAME)
    release_closure = read_optional_json(
        mission_root / release_store.RELEASE_CLOSURE_NAME)

    contradictions = _contradictions(
        release_state, commit_result, pr_binding,
        merge_handoff, merge_result, release_closure, commit_handoff)

    already_done: list[str] = []
    if commit_result is not None:
        already_done.extend([IRREVERSIBLE_COMMIT, IRREVERSIBLE_PUSH])
    if pr_binding is not None:
        already_done.append(IRREVERSIBLE_PR)
    if merge_result is not None and bool(merge_result.get("merged")):
        already_done.append(IRREVERSIBLE_MERGE)
    if release_closure is not None:
        already_done.append(IRREVERSIBLE_CLOSURE)

    artifacts = _artifact_paths(root, mission_id)

    def decision(stage: str, action: str, reason: str, resume_point: str,
                 *, terminal: bool = False, idempotent: bool = False,
                 ) -> RecoveryDecision:
        return RecoveryDecision(
            mission_id=mission_id, stage=stage, action=action, reason=reason,
            resume_point=resume_point, already_done=tuple(already_done),
            contradictions=tuple(contradictions), terminal=terminal,
            idempotent=idempotent, artifacts=artifacts,
            next_step=_NEXT_STEP[action], decided_at=clock()).validate()

    if contradictions:
        return decision(
            LS_RELEASE_CLOSURE, RA_BLOCKED,
            "local/remote contradiction detected; recovery refuses to guess",
            assembly_model.MP_HUMAN_GATE)

    if release_closure is not None and str(
            release_closure.get("status")) == "CLOSED":
        return decision(
            LS_RELEASE_CLOSURE, RA_ALREADY_COMPLETE,
            "release closure already recorded (idempotent recovery)",
            assembly_model.MP_CLOSURE, terminal=True, idempotent=True)
    if merge_result is not None and bool(merge_result.get("merged")):
        return decision(
            LS_RELEASE_CLOSURE, RA_RECORD_CLOSURE,
            "merge confirmed; the only remaining step is the release closure",
            assembly_model.MP_CLOSURE)
    if merge_handoff is not None:
        return decision(
            LS_GO_MERGE, RA_AWAIT_GO_MERGE,
            "GO MERGE handoff present; awaiting the human gate",
            assembly_model.MP_HUMAN_GATE)
    if ci_status is not None and bool(ci_status.get("green")):
        return decision(
            LS_MERGE_HANDOFF, RA_BUILD_MERGE_HANDOFF,
            "exact-head CI is green; build the GO MERGE handoff",
            assembly_model.MP_HUMAN_GATE)
    if pr_binding is not None:
        return decision(
            LS_CI_WATCH, RA_WATCH_CI,
            "exactly one PR is bound; watch the exact-head CI",
            assembly_model.MP_HUMAN_GATE)
    if commit_result is not None:
        return decision(
            LS_PR_BIND, RA_BIND_PR,
            "commit and push are already durable; bind or discover the PR",
            assembly_model.MP_HUMAN_GATE, idempotent=True)
    if commit_handoff is not None:
        return decision(
            LS_GO_COMMIT, RA_AWAIT_GO_COMMIT,
            "commit handoff present; awaiting the human GO COMMIT gate",
            assembly_model.MP_HUMAN_GATE)
    if closure is not None:
        readiness = str(closure.get("readiness", ""))
        if readiness == obs_model.RD_READY_FOR_COMMIT:
            return decision(
                LS_COMMIT_HANDOFF, RA_BUILD_HANDOFF,
                "mission is READY_FOR_COMMIT; build the commit handoff",
                assembly_model.MP_HUMAN_GATE)
        return decision(
            LS_EXECUTION, RA_BLOCKED,
            f"mission terminal with readiness {readiness!r}; no release path",
            assembly_model.MP_CLOSURE, terminal=True,
            idempotent=False)
    stage, action, reason = _mission_stage(root, mission_id, status)
    return decision(stage, action, reason, assembly_model.MP_EXECUTION)


def record_recovery(root: str, decision: RecoveryDecision) -> Path:
    """Persist the recovery decision idempotently (never rewrites identical)."""
    mission_root = Path(assembly_store.mission_root(
        root, decision.mission_id))
    mission_root.mkdir(parents=True, exist_ok=True)
    path = mission_root / model.LIFECYCLE_RECOVERY_NAME
    existing = read_optional_json(path)
    if existing == decision.to_dict():
        return path
    write_json(path, decision.to_dict())
    return path


def read_recovery(root: str,
                  mission_id: str) -> RecoveryDecision | None:
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    document = read_optional_json(mission_root
                                  / model.LIFECYCLE_RECOVERY_NAME)
    if document is None:
        return None
    return RecoveryDecision.from_dict(document)


__all__ = [
    "IRREVERSIBLE_CLOSURE",
    "IRREVERSIBLE_COMMIT",
    "IRREVERSIBLE_MERGE",
    "IRREVERSIBLE_PR",
    "IRREVERSIBLE_PUSH",
    "LIFECYCLE_STAGES",
    "LS_CI_WATCH",
    "LS_COMMIT_HANDOFF",
    "LS_EXECUTION",
    "LS_FINAL_REVIEW",
    "LS_GO_COMMIT",
    "LS_GO_MERGE",
    "LS_MERGE_HANDOFF",
    "LS_PR_BIND",
    "LS_PUSH",
    "LS_RELEASE_CLOSURE",
    "LS_VALIDATION",
    "RA_ALREADY_COMPLETE",
    "RA_AWAIT_GO_COMMIT",
    "RA_AWAIT_GO_MERGE",
    "RA_BIND_PR",
    "RA_BLOCKED",
    "RA_BUILD_HANDOFF",
    "RA_BUILD_MERGE_HANDOFF",
    "RA_CONTINUE",
    "RA_RECORD_CLOSURE",
    "RA_RESUME_EXECUTION",
    "RA_RESUME_REVIEW",
    "RA_WATCH_CI",
    "RECOVERY_ACTIONS",
    "RecoveryDecision",
    "decide_recovery",
    "read_recovery",
    "record_recovery",
]
