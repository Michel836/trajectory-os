"""M036 — deterministic GO COMMIT handoff and operator-authorized commit/push.

Two strictly separated operations:

* :func:`build_commit_handoff` is **read-only** with respect to Git: it
  consumes the canonical mission evidence, captures the exact branch/HEAD,
  and emits ``commit-handoff.json`` plus a compact human summary;
* :func:`go_commit` is the release command that performs a stage/commit/push
  **only** after an explicit operator authorization bound to ``GO_COMMIT``.
  It rechecks every fact immediately before writing and fails closed if the
  branch moved, HEAD moved, the patch changed, the review became stale or the
  readiness changed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trajectory_os.release import evidence as release_evidence
from trajectory_os.release import model, store
from trajectory_os.release.authorization import ReleaseAuthorization
from trajectory_os.release.git_adapter import GitAdapter

Clock = Callable[[], str]

_GO_COMMIT_BODY = (
    "TrajectoryOS release commit authorized via the M036 GO COMMIT gate."
)


def commit_title(objective: str, *, limit: int = 72) -> str:
    """Deterministic, bounded one-line commit title."""
    first = objective.strip().splitlines()[0] if objective.strip() else ""
    title = " ".join(first.split())
    if len(title) > limit:
        title = title[: limit - 3].rstrip() + "..."
    return title or "TrajectoryOS release commit"


def proposed_commit_message(mission_id: str, objective: str, patch_sha256: str,
                            *, issue: str | None = None) -> str:
    """Deterministic proposed commit message (never guessed)."""
    lines = [
        commit_title(objective),
        "",
        f"Mission: {mission_id}",
        f"Reviewed-Patch: {patch_sha256}",
    ]
    if issue:
        lines.append(f"Closes #{issue.lstrip('#')}")
    return "\n".join(lines) + "\n"


def _scope_summary(mission_evidence: release_evidence.MissionEvidence,
                   gate: model.ReviewGateEvidence,
                   ) -> dict[str, Any]:
    closure = mission_evidence.closure
    return {
        "mission_id": closure.mission_id,
        "objective": closure.objective,
        "reviewed_patch_sha256": gate.reviewed_patch,
        "current_patch_sha256": gate.current_patch,
        "final_reviewer_model": gate.final_reviewer_model,
        "review_outcome": gate.review_outcome,
        "review_reason": gate.review_reason,
        "attempts": gate.attempts,
        "repairs": gate.repairs,
        "steps_executed": list(closure.steps_executed),
        "validation_results": [dict(item)
                               for item in closure.validation_results],
        "review_results": [dict(item) for item in closure.review_results],
        "definition_of_done": list(closure.definition_of_done),
        "constraints": list(closure.constraints),
    }


def build_commit_handoff(root: str, mission_id: str, *,
                         git: GitAdapter,
                         base_branch: str = "main",
                         issue: str | None = None,
                         clock: Clock = model.utc_now,
                         ) -> model.CommitHandoff:
    """Emit the deterministic GO COMMIT handoff (read-only Git)."""
    mission_evidence = release_evidence.load_mission_evidence(root, mission_id)
    gate = release_evidence.require_ready(mission_evidence)
    state = git.read_state()
    if not state.available or not state.branch or not state.head_sha:
        raise model.ReleaseError(
            model.R_ADAPTER_UNAVAILABLE,
            state.reason or "repository state unavailable")
    baseline_head = (mission_evidence.mission.baseline.revision
                     or state.head_sha)
    patch_identity = gate.semantic_patch_identity
    if not model.is_sha256(patch_identity):
        raise model.ReleaseError(model.R_PATCH_INVALID,
                                 "reviewed patch is not an exact SHA-256")
    assert patch_identity is not None  # narrowed by is_sha256 above
    handoff = model.CommitHandoff(
        mission_id=mission_id,
        run_id=gate.run_id,
        objective=mission_evidence.closure.objective,
        branch=state.branch,
        base_branch=base_branch,
        baseline_head=baseline_head,
        current_head=state.head_sha,
        patch_sha256=patch_identity,
        reviewed_patch_sha256=patch_identity,
        semantic_patch_identity=patch_identity,
        proposed_commit_message=proposed_commit_message(
            mission_id, mission_evidence.closure.objective,
            patch_identity, issue=issue),
        scope_summary=_scope_summary(mission_evidence, gate),
        review_evidence=gate.to_dict(),
        gate=model.GATE_GO_COMMIT,
        gate_reason=model.R_OK,
        created_at=clock(),
    ).validate()
    mission_root = mission_evidence.mission_root
    store.write_commit_handoff(mission_root, handoff)
    _write_state(mission_root, handoff, clock)
    store.append_release_event(mission_root, {
        "action": model.GATE_GO_COMMIT,
        "result": "HANDOFF_READY",
        "mission_id": mission_id,
        "branch": handoff.branch,
        "current_head": handoff.current_head,
        "patch_sha256": handoff.patch_sha256,
        "at": handoff.created_at,
    })
    return handoff


def _write_state(mission_root: Any, handoff: model.CommitHandoff,
                 clock: Clock) -> None:
    current = (store.load_state(mission_root)
               if store.exists(mission_root, store.RELEASE_STATE_NAME)
               else None)
    # Never regress a later release stage back to the handoff stage.
    if current is not None and current.stage not in (
            model.RST_READY_FOR_COMMIT, model.RST_COMMIT_HANDOFF):
        return
    store.write_state(mission_root, model.ReleaseState(
        mission_id=handoff.mission_id,
        stage=model.RST_COMMIT_HANDOFF,
        reason=model.R_OK,
        updated_at=clock(),
        branch=handoff.branch,
        base_branch=handoff.base_branch,
    ))


def _recheck(root: str, mission_id: str, handoff: model.CommitHandoff,
             git: GitAdapter) -> model.ReviewGateEvidence:
    """Recheck every GO COMMIT precondition immediately before writing."""
    mission_evidence = release_evidence.load_mission_evidence(root, mission_id)
    gate = release_evidence.derive_review_gate(mission_evidence)
    if not gate.ready:
        raise model.ReleaseError(model.R_READINESS_CHANGED, gate.reason)
    state = git.read_state()
    if not state.available or not state.branch or not state.head_sha:
        raise model.ReleaseError(model.R_ADAPTER_UNAVAILABLE,
                                 state.reason or "repository unavailable")
    if state.branch != handoff.branch:
        raise model.ReleaseError(
            model.R_BRANCH_MOVED,
            f"branch {state.branch!r} != handoff {handoff.branch!r}")
    if state.head_sha != handoff.current_head:
        raise model.ReleaseError(
            model.R_HEAD_MOVED,
            f"HEAD {state.head_sha} != handoff {handoff.current_head}")
    if gate.semantic_patch_identity != handoff.patch_sha256:
        raise model.ReleaseError(
            model.R_PATCH_CHANGED,
            f"reviewed patch {gate.semantic_patch_identity} != handoff "
            f"{handoff.patch_sha256}")
    if gate.semantic_patch_identity != handoff.reviewed_patch_sha256:
        raise model.ReleaseError(model.R_PATCH_MISMATCH,
                                 "reviewed patch identity changed")
    return gate


def go_commit(root: str, mission_id: str, *,
              git: GitAdapter,
              authorization: ReleaseAuthorization,
              remote: str = "origin",
              clock: Clock = model.utc_now,
              ) -> model.CommitResult:
    """Authorized GO COMMIT: recheck, stage, commit and push (fail closed)."""
    authorization.validate()
    if authorization.action != model.GATE_GO_COMMIT:
        raise model.ReleaseError(
            model.R_UNAUTHORIZED,
            f"GO COMMIT requires a {model.GATE_GO_COMMIT} authorization")
    if not store.exists(
            _mission_root(root, mission_id), store.COMMIT_HANDOFF_NAME):
        raise model.ReleaseError(model.R_HANDOFF_MISSING, mission_id)
    mission_root = _mission_root(root, mission_id)
    handoff = store.load_commit_handoff(mission_root)
    if handoff.mission_id != mission_id:
        raise model.ReleaseError(model.R_IDENTITY_MISMATCH,
                                 "commit handoff identity mismatch")
    existing_state = (store.load_state(mission_root)
                      if store.exists(mission_root, store.RELEASE_STATE_NAME)
                      else None)
    if existing_state is not None and existing_state.stage not in (
            model.RST_COMMIT_HANDOFF, model.RST_READY_FOR_COMMIT):
        raise model.ReleaseError(
            model.R_ALREADY_COMMITTED,
            f"release stage is {existing_state.stage}")

    _recheck(root, mission_id, handoff, git)
    commit = git.commit_all(handoff.proposed_commit_message,
                            authorization=authorization)
    if commit.commit_sha == handoff.current_head:
        raise model.ReleaseError(model.R_COMMIT_FAILED,
                                 "commit did not advance HEAD")
    push = git.push(handoff.branch, commit.commit_sha, remote=remote,
                    authorization=authorization)
    result = model.CommitResult(
        mission_id=mission_id,
        commit_sha=commit.commit_sha,
        branch=commit.branch or handoff.branch,
        remote=push.remote,
        pushed_sha=push.pushed_sha,
        parent_head=handoff.current_head,
        baseline_head=handoff.baseline_head,
        reviewed_patch_sha256=handoff.reviewed_patch_sha256,
        message=handoff.proposed_commit_message,
        authorization=authorization.to_dict(),
        created_at=clock(),
    ).validate()
    store.write_document(mission_root, store.COMMIT_RESULT_NAME,
                         result.to_dict())
    store.write_state(mission_root, model.ReleaseState(
        mission_id=mission_id,
        stage=model.RST_COMMITTED,
        reason=model.R_OK,
        updated_at=result.created_at,
        commit_sha=result.commit_sha,
        branch=result.branch,
        base_branch=handoff.base_branch,
    ))
    store.append_release_event(mission_root, {
        "action": model.GATE_GO_COMMIT,
        "result": "COMMITTED",
        "mission_id": mission_id,
        "commit_sha": result.commit_sha,
        "remote": result.remote,
        "authorization": result.authorization,
        "at": result.created_at,
    })
    return result


def _mission_root(root: str, mission_id: str) -> Any:
    from trajectory_os.assembly import store as assembly_store

    return assembly_store.mission_root(root, mission_id)


__all__ = [
    "build_commit_handoff",
    "commit_title",
    "go_commit",
    "proposed_commit_message",
]
