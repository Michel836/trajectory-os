"""M037 — exact-head PR binding and CI watch (operator-authorized release).

After a successful authorized commit/push the release is bound to the exact
commit SHA:

* :func:`bind_pull_request` creates or discovers **exactly one** pull request
  and persists its identity, base branch and exact head SHA;
* :func:`watch_ci` looks up CI **only** for that exact head SHA and
  distinguishes ``queued`` / ``in_progress`` / ``success`` / ``failure`` /
  ``cancelled`` / ``missing``.

Both operations are read-only with respect to Git and never allow a merge
while the exact-head CI is not a completed success. Repeated watch/status
with the same inputs is byte-idempotent (it never appends events and never
rewrites the canonical mission state).
"""

from __future__ import annotations

from collections.abc import Callable

from trajectory_os.assembly import store as assembly_store
from trajectory_os.release import model, store
from trajectory_os.release.git_adapter import GitAdapter
from trajectory_os.release.github_adapter import GitHubAdapter

Clock = Callable[[], str]

_DEFAULT_PR_TITLE = "TrajectoryOS release"
_STATE_STAGE_FOR_CI: dict[str, str] = {
    model.CI_SUCCESS: model.RST_CI_SUCCESS,
    model.CI_QUEUED: model.RST_CI_PENDING,
    model.CI_IN_PROGRESS: model.RST_CI_PENDING,
    model.CI_FAILURE: model.RST_CI_FAILED,
    model.CI_CANCELLED: model.RST_CI_FAILED,
    model.CI_MISSING: model.RST_CI_FAILED,
    model.CI_UNKNOWN: model.RST_CI_FAILED,
}


def _mission_root(root: str, mission_id: str) -> object:
    return assembly_store.mission_root(root, mission_id)


def _require_committed(root: str, mission_id: str) -> model.ReleaseState:
    mission_root = assembly_store.mission_root(root, mission_id)
    if not store.exists(mission_root, store.RELEASE_STATE_NAME):
        raise model.ReleaseError(model.R_HANDOFF_MISSING,
                                 "no release state recorded")
    state = store.load_state(mission_root)
    if state.mission_id != mission_id:
        raise model.ReleaseError(model.R_IDENTITY_MISMATCH,
                                 "release state identity mismatch")
    if state.commit_sha is None:
        raise model.ReleaseError(model.R_NOT_READY,
                                 "release is not committed yet")
    return state


def bind_pull_request(root: str, mission_id: str, *,
                      git: GitAdapter, github: GitHubAdapter,
                      base_branch: str | None = None,
                      title: str | None = None,
                      body: str | None = None,
                      clock: Clock = model.utc_now,
                      ) -> model.PullRequestBinding:
    """Create or discover exactly one PR bound to the exact commit SHA."""
    state = _require_committed(root, mission_id)
    mission_root = assembly_store.mission_root(root, mission_id)
    target_base = base_branch or state.base_branch or "main"
    commit_sha = state.commit_sha
    assert commit_sha is not None  # narrowed by _require_committed

    # Idempotent re-bind: an existing binding for the same commit is reused.
    if store.exists(mission_root, store.PR_BINDING_NAME):
        existing = store.load_pr_binding(mission_root)
        if existing.bound_commit_sha == commit_sha:
            return existing

    git_state = git.read_state()
    if not git_state.available or not git_state.branch:
        raise model.ReleaseError(model.R_ADAPTER_UNAVAILABLE,
                                 git_state.reason or "repository unavailable")
    if git_state.head_sha != commit_sha:
        raise model.ReleaseError(
            model.R_HEAD_MOVED,
            f"local HEAD {git_state.head_sha} != release commit {commit_sha}")

    if not github.available():
        raise model.ReleaseError(model.R_ADAPTER_UNAVAILABLE,
                                 "GitHub adapter unavailable")
    found = github.find_pull_requests(head_branch=git_state.branch,
                                      base_branch=target_base)
    if len(found) > 1:
        raise model.ReleaseError(
            model.R_PR_AMBIGUOUS,
            f"{len(found)} open pull requests for {git_state.branch!r}")
    created = False
    if found:
        pr = found[0]
    else:
        pr = github.create_pull_request(
            head_branch=git_state.branch, base_branch=target_base,
            title=title or _DEFAULT_PR_TITLE,
            body=body or "TrajectoryOS human-gated release.")
        created = True

    if pr.head_sha != commit_sha:
        raise model.ReleaseError(
            model.R_PR_HEAD_MOVED,
            f"PR head {pr.head_sha} != release commit {commit_sha}")
    if pr.base_branch != target_base:
        raise model.ReleaseError(
            model.R_TARGET_MISMATCH,
            f"PR base {pr.base_branch!r} != {target_base!r}")
    binding = model.PullRequestBinding(
        mission_id=mission_id,
        number=pr.number,
        url=pr.url,
        base_branch=pr.base_branch,
        base_sha=pr.base_sha,
        head_branch=pr.head_branch,
        head_sha=pr.head_sha,
        state=pr.state,
        mergeable=pr.mergeable,
        mergeable_state=pr.mergeable_state,
        created=created,
        bound_commit_sha=commit_sha,
        bound_at=clock(),
    ).validate()
    store.write_pr_binding(mission_root, binding)
    store.write_state(mission_root, model.ReleaseState(
        mission_id=mission_id,
        stage=model.RST_PR_BOUND,
        reason=model.R_OK,
        updated_at=binding.bound_at,
        commit_sha=commit_sha,
        branch=binding.head_branch,
        base_branch=binding.base_branch,
        pr_number=binding.number,
        pr_head_sha=binding.head_sha,
    ))
    store.append_release_event(mission_root, {
        "action": "BIND_PR",
        "result": "PR_BOUND",
        "mission_id": mission_id,
        "pr_number": binding.number,
        "pr_head_sha": binding.head_sha,
        "created": created,
        "at": binding.bound_at,
    })
    return binding


def bind_pull_request_explicit(root: str, mission_id: str, *,
                               git: GitAdapter, github: GitHubAdapter,
                               base_branch: str | None = None,
                               title: str | None = None,
                               body: str | None = None,
                               clock: Clock = model.utc_now,
                               ) -> model.PullRequestBinding:
    """Alias kept for symmetry with the CLI (same behaviour)."""
    return bind_pull_request(root, mission_id, git=git, github=github,
                             base_branch=base_branch, title=title, body=body,
                             clock=clock)


def watch_ci(root: str, mission_id: str, *,
             github: GitHubAdapter,
             persist: bool = False,
             clock: Clock = model.utc_now,
             ) -> model.CiStatus:
    """Read the exact-head CI state (never a branch-name-only view)."""
    state = _require_committed(root, mission_id)
    mission_root = assembly_store.mission_root(root, mission_id)
    if not store.exists(mission_root, store.PR_BINDING_NAME):
        raise model.ReleaseError(model.R_PR_MISSING,
                                 "no PR binding recorded yet")
    binding = store.load_pr_binding(mission_root)
    commit_sha = state.commit_sha
    assert commit_sha is not None
    if binding.bound_commit_sha != commit_sha:
        raise model.ReleaseError(
            model.R_PR_HEAD_MOVED,
            f"PR binding {binding.bound_commit_sha} != release commit "
            f"{commit_sha}")
    if not github.available():
        raise model.ReleaseError(model.R_ADAPTER_UNAVAILABLE,
                                 "GitHub adapter unavailable")
    query = github.checks_for_head(commit_sha)
    status = query.to_status(checked_at=clock())
    if persist:
        store.write_ci_status(mission_root, status)
        stage = _STATE_STAGE_FOR_CI.get(status.state, model.RST_CI_FAILED)
        store.write_state(mission_root, model.ReleaseState(
            mission_id=mission_id,
            stage=stage,
            reason=(model.R_OK if status.green
                    else model.R_CI_NOT_GREEN),
            updated_at=status.checked_at,
            commit_sha=commit_sha,
            branch=state.branch,
            base_branch=state.base_branch,
            pr_number=binding.number,
            pr_head_sha=binding.head_sha,
            ci_state=status.state,
        ))
    return status


__all__ = [
    "bind_pull_request",
    "bind_pull_request_explicit",
    "watch_ci",
]
