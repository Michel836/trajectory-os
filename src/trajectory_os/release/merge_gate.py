"""M038 — explicit GO MERGE gate and operator-authorized merge.

There is no background merge and no auto-merge. The sequence is strictly:

1. :func:`build_merge_handoff` verifies PR-open + mergeable + exact PR head ==
   release commit + fresh exact-head CI success, and persists
   ``merge-handoff.json`` **before** the gate;
2. :func:`go_merge` rechecks every fact and performs the merge **only** after
   an explicit operator authorization bound to ``GO_MERGE``, using
   expected-head protection and the configured default method (``squash``);
3. the authoritative result is persisted in ``merge-result.json``.

Implementation/review/runtime-control components can never reach the write:
they cannot mint a ``GO_MERGE`` authorization.
"""

from __future__ import annotations

from collections.abc import Callable

from trajectory_os.assembly import store as assembly_store
from trajectory_os.release import model, store
from trajectory_os.release import pull_request as release_pr
from trajectory_os.release.authorization import ReleaseAuthorization
from trajectory_os.release.github_adapter import GitHubAdapter

Clock = Callable[[], str]


def _mission_root(root: str, mission_id: str) -> object:
    return assembly_store.mission_root(root, mission_id)


def _recheck(root: str, mission_id: str, *,
             github: GitHubAdapter, commit_sha: str,
             base_branch: str,
             ) -> tuple[model.PullRequestBinding, model.CiStatus]:
    mission_root = assembly_store.mission_root(root, mission_id)
    if not store.exists(mission_root, store.PR_BINDING_NAME):
        raise model.ReleaseError(model.R_PR_MISSING,
                                 "no PR binding recorded yet")
    binding = store.load_pr_binding(mission_root)
    if binding.bound_commit_sha != commit_sha:
        raise model.ReleaseError(model.R_PR_HEAD_MOVED,
                                 "PR binding commit mismatch")
    if not github.available():
        raise model.ReleaseError(model.R_ADAPTER_UNAVAILABLE,
                                 "GitHub adapter unavailable")
    pr = github.get_pull_request(binding.number)
    if pr.state != model.PR_OPEN:
        raise model.ReleaseError(model.R_PR_NOT_OPEN,
                                 f"PR state is {pr.state}")
    if pr.head_sha != commit_sha:
        raise model.ReleaseError(
            model.R_PR_HEAD_MOVED,
            f"PR head {pr.head_sha} != release commit {commit_sha}")
    if pr.base_branch != base_branch:
        raise model.ReleaseError(
            model.R_TARGET_MISMATCH,
            f"PR base {pr.base_branch!r} != {base_branch!r}")
    if pr.mergeable is not True:
        raise model.ReleaseError(
            model.R_PR_NOT_MERGEABLE,
            f"PR mergeable={pr.mergeable!r} state={pr.mergeable_state!r}")
    ci = release_pr.watch_ci(root, mission_id, github=github, persist=False)
    if ci.head_sha != commit_sha:
        raise model.ReleaseError(model.R_HANDOFF_MISSING,
                                 "CI was not looked up for the exact commit")
    if not ci.green:
        raise model.ReleaseError(
            model.R_CI_NOT_GREEN,
            f"exact-head CI state is {ci.state}")
    return binding, ci


def build_merge_handoff(root: str, mission_id: str, *,
                        github: GitHubAdapter,
                        base_branch: str | None = None,
                        merge_method: str = model.DEFAULT_MERGE_METHOD,
                        clock: Clock = model.utc_now,
                        ) -> model.MergeHandoff:
    """Verify every merge precondition and persist the GO MERGE handoff."""
    if merge_method not in model.MERGE_METHODS:
        raise model.ReleaseError(model.R_MERGE_METHOD_INVALID,
                                 str(merge_method))
    mission_root = assembly_store.mission_root(root, mission_id)
    state = release_pr._require_committed(root, mission_id)
    commit_sha = state.commit_sha
    assert commit_sha is not None
    target_base = base_branch or state.base_branch or "main"
    binding, ci = _recheck(root, mission_id, github=github,
                           commit_sha=commit_sha, base_branch=target_base)
    handoff = model.MergeHandoff(
        mission_id=mission_id,
        release_commit_sha=commit_sha,
        pr_number=binding.number,
        pr_url=binding.url,
        pr_head_sha=binding.head_sha,
        base_branch=target_base,
        base_sha=binding.base_sha,
        ci_state=ci.state,
        ci_head_sha=ci.head_sha,
        ci_checked_at=ci.checked_at,
        merge_method=merge_method,
        gate=model.GATE_GO_MERGE,
        gate_reason=model.R_OK,
        created_at=clock(),
    ).validate()
    store.write_merge_handoff(mission_root, handoff)
    _write_state(mission_root, handoff, state)
    store.append_release_event(mission_root, {
        "action": model.GATE_GO_MERGE,
        "result": "HANDOFF_READY",
        "mission_id": mission_id,
        "pr_number": handoff.pr_number,
        "release_commit_sha": handoff.release_commit_sha,
        "ci_state": handoff.ci_state,
        "at": handoff.created_at,
    })
    return handoff


def _write_state(mission_root: object, handoff: model.MergeHandoff,
                 state: model.ReleaseState) -> None:
    # Never regress an already-merged/released release.
    if state.stage in (model.RST_MERGED, model.RST_RELEASED):
        return
    store.write_state(mission_root, model.ReleaseState(  # type: ignore[arg-type]
        mission_id=handoff.mission_id,
        stage=model.RST_MERGE_HANDOFF,
        reason=model.R_OK,
        updated_at=handoff.created_at,
        commit_sha=handoff.release_commit_sha,
        branch=state.branch,
        base_branch=handoff.base_branch,
        pr_number=handoff.pr_number,
        pr_head_sha=handoff.pr_head_sha,
        ci_state=handoff.ci_state,
    ))


def go_merge(root: str, mission_id: str, *,
             github: GitHubAdapter,
             authorization: ReleaseAuthorization,
             merge_method: str | None = None,
             clock: Clock = model.utc_now,
             ) -> model.MergeResult:
    """Authorized GO MERGE: recheck and merge with expected-head protection."""
    authorization.validate()
    if authorization.action != model.GATE_GO_MERGE:
        raise model.ReleaseError(
            model.R_UNAUTHORIZED,
            f"GO MERGE requires a {model.GATE_GO_MERGE} authorization")
    mission_root = assembly_store.mission_root(root, mission_id)
    # Idempotent repeated merge: an authoritative merged result is returned.
    if store.exists(mission_root, store.MERGE_RESULT_NAME):
        existing = store.load_merge_result(mission_root)
        if existing.merged:
            return existing
    if not store.exists(mission_root, store.MERGE_HANDOFF_NAME):
        raise model.ReleaseError(model.R_MERGE_HANDOFF_MISSING, mission_id)
    handoff = store.load_merge_handoff(mission_root)
    if handoff.mission_id != mission_id:
        raise model.ReleaseError(model.R_IDENTITY_MISMATCH,
                                 "merge handoff identity mismatch")
    method = merge_method or handoff.merge_method
    if method not in model.MERGE_METHODS:
        raise model.ReleaseError(model.R_MERGE_METHOD_INVALID, str(method))
    if method != handoff.merge_method:
        raise model.ReleaseError(
            model.R_MERGE_HANDOFF_STALE,
            f"merge method {method!r} != handoff {handoff.merge_method!r}")
    state = release_pr._require_committed(root, mission_id)
    commit_sha = state.commit_sha
    assert commit_sha is not None
    if commit_sha != handoff.release_commit_sha:
        raise model.ReleaseError(model.R_MERGE_HANDOFF_STALE,
                                 "release commit changed since handoff")
    _recheck(root, mission_id, github=github, commit_sha=commit_sha,
             base_branch=handoff.base_branch)

    outcome = github.merge_pull_request(
        handoff.pr_number, expected_head_sha=commit_sha, method=method)
    if not outcome.merged or not outcome.merge_sha:
        raise model.ReleaseError(
            model.R_MERGE_FAILED,
            outcome.message[:256] or "merge was not confirmed")
    if not model.is_git_sha(outcome.merge_sha):
        raise model.ReleaseError(model.R_MERGE_FAILED,
                                 "merge SHA is not a Git object id")
    target_head = github.branch_head(handoff.base_branch)
    verified = target_head == outcome.merge_sha
    result = model.MergeResult(
        mission_id=mission_id,
        merged=True,
        merge_method=method,
        merge_sha=outcome.merge_sha,
        target_branch=handoff.base_branch,
        target_head_sha=target_head,
        pr_number=handoff.pr_number,
        merged_at=clock(),
        verified=verified,
        reason=(model.R_OK if verified else model.R_TARGET_UNVERIFIED),
    ).validate()
    store.write_merge_result(mission_root, result)
    store.write_state(mission_root, model.ReleaseState(
        mission_id=mission_id,
        stage=model.RST_MERGED,
        reason=result.reason,
        updated_at=result.merged_at,
        commit_sha=commit_sha,
        branch=state.branch,
        base_branch=handoff.base_branch,
        pr_number=handoff.pr_number,
        pr_head_sha=handoff.pr_head_sha,
        ci_state=handoff.ci_state,
        merge_sha=outcome.merge_sha,
    ))
    store.append_release_event(mission_root, {
        "action": model.GATE_GO_MERGE,
        "result": "MERGED",
        "mission_id": mission_id,
        "merge_sha": outcome.merge_sha,
        "target_branch": handoff.base_branch,
        "target_verified": verified,
        "authorization": authorization.to_dict(),
        "at": result.merged_at,
    })
    return result


__all__ = [
    "build_merge_handoff",
    "go_merge",
]
