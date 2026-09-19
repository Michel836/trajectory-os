"""M039 — release closure and reconstruction (read-only, idempotent).

After a confirmed merge :func:`build_release_closure` records the complete
chain without prose:

    objective -> mission -> execution -> validation -> review
    -> READY_FOR_COMMIT -> GO COMMIT -> commit/push -> PR
    -> exact-head CI -> GO MERGE -> merge -> release closure

:func:`reconstruct_release` rebuilds that chain from durable artifacts only
and never writes anything; repeated reconstruction is byte-idempotent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from trajectory_os.assembly import closure as assembly_closure
from trajectory_os.assembly import model as assembly_model
from trajectory_os.assembly import store as assembly_store
from trajectory_os.observability import store as obs_store
from trajectory_os.release import evidence as release_evidence
from trajectory_os.release import model, store
from trajectory_os.release.github_adapter import GitHubAdapter

Clock = Callable[[], str]

_ISSUE_RE = re.compile(r"(?im)^\s*Closes\s+#(\d+)\s*$")


def _issue_relation(issue: str | None,
                    pull_request_body: str | None) -> dict[str, Any]:
    if issue:
        return {"relation": "closes", "issue": issue.lstrip("#"),
                "source": "operator"}
    if pull_request_body:
        match = _ISSUE_RE.search(pull_request_body)
        if match:
            return {"relation": "closes", "issue": match.group(1),
                    "source": "pull_request_body"}
    return {"relation": "not-recorded", "issue": None, "source": "none"}


def build_release_closure(root: str, mission_id: str, *,
                          github: GitHubAdapter,
                          base_branch: str | None = None,
                          issue: str | None = None,
                          clock: Clock = model.utc_now,
                          ) -> model.ReleaseClosure:
    """Record the authoritative release closure after a confirmed merge."""
    mission_root = assembly_store.mission_root(root, mission_id)
    if not store.exists(mission_root, store.MERGE_RESULT_NAME):
        raise model.ReleaseError(model.R_NOT_MERGED,
                                 "no authoritative merge result recorded")
    merge_result = store.load_merge_result(mission_root)
    if not merge_result.merged:
        raise model.ReleaseError(model.R_NOT_MERGED,
                                 "merge was not confirmed")
    if not store.exists(mission_root, store.MERGE_HANDOFF_NAME):
        raise model.ReleaseError(model.R_MERGE_HANDOFF_MISSING, mission_id)
    if not store.exists(mission_root, store.PR_BINDING_NAME):
        raise model.ReleaseError(model.R_PR_MISSING, mission_id)
    if not store.exists(mission_root, store.COMMIT_RESULT_NAME):
        raise model.ReleaseError(model.R_HANDOFF_MISSING,
                                 "no commit result recorded")
    handoff = store.load_merge_handoff(mission_root)
    binding = store.load_pr_binding(mission_root)
    commit_result = store.load_commit_result(mission_root)
    commit_handoff = store.load_commit_handoff(mission_root)
    mission_evidence = release_evidence.load_mission_evidence(root, mission_id)
    target_base = base_branch or handoff.base_branch

    # Fail closed on any chain mismatch.
    if commit_result.commit_sha != binding.bound_commit_sha:
        raise model.ReleaseError(model.R_IDENTITY_MISMATCH,
                                 "commit/PR binding mismatch")
    if binding.head_sha != commit_result.commit_sha:
        raise model.ReleaseError(model.R_PR_HEAD_MOVED,
                                 "PR head does not equal the commit")
    if merge_result.target_branch != target_base:
        raise model.ReleaseError(model.R_TARGET_MISMATCH,
                                 "merge target does not match the base")

    ci_status = (store.load_ci_status(mission_root)
                 if store.exists(mission_root, store.CI_STATUS_NAME)
                 else None)
    ci_state = handoff.ci_state
    ci_head_sha = handoff.ci_head_sha
    if ci_status is not None and ci_status.head_sha == commit_result.commit_sha:
        ci_state = ci_status.state
    runs = ci_status.runs if ci_status is not None else ()
    ci_run = runs[0] if runs else None
    ci_conclusion = None
    ci_workflow = None
    ci_run_id = None
    if ci_run is not None:
        ci_workflow = ci_run.workflow
        ci_run_id = ci_run.run_id
        ci_conclusion = ci_run.conclusion

    target_head = github.branch_head(target_base)
    verified = target_head is not None and target_head == merge_result.merge_sha
    if not verified:
        verified = bool(merge_result.verified
                        and merge_result.target_head_sha
                        == merge_result.merge_sha)

    body: str | None = None
    if github.available():
        try:
            body = github.get_pull_request(binding.number).body
        except model.ReleaseError:
            body = None
    relation = _issue_relation(issue, body)

    artifacts = store.artifact_paths(mission_root)
    closure = model.ReleaseClosure(
        mission_id=mission_id,
        run_id=mission_evidence.mission_id,
        objective=mission_evidence.closure.objective,
        reviewed_patch_sha256=commit_handoff.reviewed_patch_sha256,
        final_patch_sha256=commit_handoff.patch_sha256,
        commit_sha=commit_result.commit_sha,
        remote_branch=commit_result.branch,
        pr_number=binding.number,
        pr_url=binding.url,
        base_branch=target_base,
        base_sha=binding.base_sha,
        pr_head_sha=binding.head_sha,
        ci_workflow=ci_workflow,
        ci_run_id=ci_run_id,
        ci_status=ci_state,
        ci_conclusion=ci_conclusion,
        ci_head_sha=ci_head_sha,
        go_commit_evidence=_go_commit_evidence(commit_handoff, commit_result),
        go_merge_evidence=_go_merge_evidence(handoff, merge_result),
        merge_sha=merge_result.merge_sha or "",
        target_branch=target_base,
        target_branch_verified=verified,
        issue_closure=relation,
        timestamps=_timestamps(commit_handoff, commit_result, binding,
                               handoff, merge_result),
        artifacts=artifacts,
        status="CLOSED",
        created_at=clock(),
    ).validate()
    store.write_release_closure(mission_root, closure)
    store.write_state(mission_root, model.ReleaseState(
        mission_id=mission_id,
        stage=model.RST_RELEASED,
        reason=model.R_OK,
        updated_at=closure.created_at,
        commit_sha=closure.commit_sha,
        branch=closure.remote_branch,
        base_branch=closure.base_branch,
        pr_number=closure.pr_number,
        pr_head_sha=closure.pr_head_sha,
        ci_state=closure.ci_status,
        merge_sha=closure.merge_sha,
    ))
    store.append_release_event(mission_root, {
        "action": "RELEASE_CLOSURE",
        "result": closure.status,
        "mission_id": mission_id,
        "merge_sha": closure.merge_sha,
        "target_branch_verified": closure.target_branch_verified,
        "at": closure.created_at,
    })
    return closure


def _go_commit_evidence(commit_handoff: model.CommitHandoff,
                        commit_result: model.CommitResult,
                        ) -> Mapping[str, Any]:
    return {
        "gate": commit_handoff.gate,
        "branch": commit_handoff.branch,
        "base_branch": commit_handoff.base_branch,
        "baseline_head": commit_handoff.baseline_head,
        "parent_head": commit_result.parent_head,
        "commit_sha": commit_result.commit_sha,
        "pushed_sha": commit_result.pushed_sha,
        "reviewed_patch_sha256": commit_handoff.reviewed_patch_sha256,
        "authorization": dict(commit_result.authorization),
        "handoff_at": commit_handoff.created_at,
        "committed_at": commit_result.created_at,
    }


def _go_merge_evidence(handoff: model.MergeHandoff,
                       merge_result: model.MergeResult,
                       ) -> Mapping[str, Any]:
    return {
        "gate": handoff.gate,
        "release_commit_sha": handoff.release_commit_sha,
        "pr_number": handoff.pr_number,
        "pr_head_sha": handoff.pr_head_sha,
        "ci_state": handoff.ci_state,
        "ci_head_sha": handoff.ci_head_sha,
        "merge_method": handoff.merge_method,
        "merge_sha": merge_result.merge_sha,
        "target_branch": merge_result.target_branch,
        "target_head_sha": merge_result.target_head_sha,
        "target_branch_verified": merge_result.verified,
        "handoff_at": handoff.created_at,
        "merged_at": merge_result.merged_at,
    }


def _timestamps(commit_handoff: model.CommitHandoff,
                commit_result: model.CommitResult,
                binding: model.PullRequestBinding,
                handoff: model.MergeHandoff,
                merge_result: model.MergeResult,
                ) -> Mapping[str, Any]:
    return {
        "commit_handoff_at": commit_handoff.created_at,
        "committed_at": commit_result.created_at,
        "pr_bound_at": binding.bound_at,
        "merge_handoff_at": handoff.created_at,
        "merged_at": merge_result.merged_at,
    }


def reconstruct_release(root: str, mission_id: str) -> dict[str, Any]:
    """Rebuild the release chain from durable artifacts only (read-only)."""
    mission_root = assembly_store.mission_root(root, mission_id)
    try:
        mission = assembly_closure.reconstruct_mission(root, mission_id)
    except assembly_model.AssemblyError as exc:
        raise model.ReleaseError(exc.code, exc.detail) from exc
    release: dict[str, Any] = {
        "state": _load_optional(mission_root, store.RELEASE_STATE_NAME),
        "commit_handoff": _load_optional(mission_root,
                                         store.COMMIT_HANDOFF_NAME),
        "commit_result": _load_optional(mission_root,
                                        store.COMMIT_RESULT_NAME),
        "pr_binding": _load_optional(mission_root, store.PR_BINDING_NAME),
        "ci_status": _load_optional(mission_root, store.CI_STATUS_NAME),
        "merge_handoff": _load_optional(mission_root, store.MERGE_HANDOFF_NAME),
        "merge_result": _load_optional(mission_root, store.MERGE_RESULT_NAME),
        "release_closure": _load_optional(mission_root,
                                          store.RELEASE_CLOSURE_NAME),
        "release_events": store.load_release_events(mission_root),
    }
    return {
        "schema_version": model.SCHEMA_VERSION,
        "release_version": model.RELEASE_VERSION,
        "mission_id": mission_id,
        "mission": mission,
        "release": release,
        "artifacts": store.artifact_paths(mission_root),
    }


def _load_optional(mission_root: Path, name: str) -> dict[str, Any] | None:
    if not store.exists(mission_root, name):
        return None
    try:
        return store.read_document(mission_root, name)
    except (model.ReleaseError, obs_store.CanonicalStoreError):
        return None


__all__ = [
    "build_release_closure",
    "reconstruct_release",
]
