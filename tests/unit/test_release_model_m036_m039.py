"""M036–M039 unit — pure release vocabulary, authorization and Git guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from trajectory_os.release import model
from trajectory_os.release.authorization import authorize, unauthorized
from trajectory_os.release.git_adapter import FakeGitAdapter


def test_ci_classification_is_fail_closed() -> None:
    assert model.classify_check_run("queued", None) == model.CI_QUEUED
    assert model.classify_check_run("in_progress", None) == model.CI_IN_PROGRESS
    assert model.classify_check_run("completed", "success") == model.CI_SUCCESS
    assert model.classify_check_run("completed", "neutral") == model.CI_SUCCESS
    assert model.classify_check_run("completed", "skipped") == model.CI_SUCCESS
    assert model.classify_check_run("completed", "failure") == model.CI_FAILURE
    assert model.classify_check_run("completed", "timed_out") == model.CI_FAILURE
    assert model.classify_check_run(
        "completed", "cancelled") == model.CI_CANCELLED
    assert model.classify_check_run("completed", None) == model.CI_UNKNOWN
    assert model.classify_check_run("weird", "success") == model.CI_UNKNOWN


def test_ci_combination_requires_every_run_green() -> None:
    assert model.combine_ci_states([]) == model.CI_MISSING
    assert model.combine_ci_states([model.CI_SUCCESS]) == model.CI_SUCCESS
    assert model.combine_ci_states(
        [model.CI_QUEUED, model.CI_QUEUED]) == model.CI_QUEUED
    assert model.combine_ci_states(
        [model.CI_QUEUED, model.CI_IN_PROGRESS]) == model.CI_IN_PROGRESS
    assert model.combine_ci_states(
        [model.CI_SUCCESS, model.CI_FAILURE]) == model.CI_FAILURE
    assert model.combine_ci_states(
        [model.CI_SUCCESS, model.CI_CANCELLED]) == model.CI_CANCELLED
    assert model.combine_ci_states(
        [model.CI_SUCCESS, model.CI_UNKNOWN]) == model.CI_UNKNOWN
    assert not model.ci_allows_merge(model.CI_QUEUED)
    assert model.ci_allows_merge(model.CI_SUCCESS)


def test_default_merge_method_is_squash() -> None:
    assert model.DEFAULT_MERGE_METHOD == model.MERGE_SQUASH


def test_sha_helpers_are_strict() -> None:
    assert model.is_sha256("a" * 64)
    assert not model.is_sha256("A" * 64)
    assert not model.is_sha256("a" * 63)
    assert not model.is_sha256(None)
    assert model.is_git_sha("a" * 40)
    assert model.is_git_sha("a" * 64)
    assert not model.is_git_sha("a" * 41)


def test_authorization_requires_explicit_operator_token() -> None:
    with pytest.raises(model.ReleaseError) as exc:
        authorize(model.GATE_GO_COMMIT, None)
    assert exc.value.code == model.R_UNAUTHORIZED
    with pytest.raises(model.ReleaseError):
        authorize(model.GATE_GO_COMMIT, "   ")
    with pytest.raises(model.ReleaseError) as exc:
        authorize(model.GATE_GO_MERGE, "token", actor="agent")
    assert exc.value.code == model.R_UNAUTHORIZED
    auth = authorize(model.GATE_GO_COMMIT, "explicit-token",
                     actor="operator")
    assert auth.action == model.GATE_GO_COMMIT
    assert "explicit-token" not in str(auth.to_dict())
    assert auth.to_dict()["token_digest"] != "explicit-token"


def test_unauthorized_sentinel_never_validates() -> None:
    sentinel = unauthorized(model.GATE_GO_COMMIT)
    with pytest.raises(model.ReleaseError) as exc:
        sentinel.validate()
    assert exc.value.code == model.R_UNAUTHORIZED


def test_git_adapter_write_requires_go_commit_authorization() -> None:
    git = FakeGitAdapter(head_sha="a" * 40)
    with pytest.raises(model.ReleaseError) as exc:
        git.commit_all("message",
                       authorization=unauthorized(model.GATE_GO_COMMIT))
    assert exc.value.code == model.R_UNAUTHORIZED
    merge_auth = authorize(model.GATE_GO_MERGE, "token", actor="operator")
    with pytest.raises(model.ReleaseError) as exc:
        git.commit_all("message", authorization=merge_auth)
    assert exc.value.code == model.R_UNAUTHORIZED
    with pytest.raises(model.ReleaseError) as exc:
        git.push("main", "a" * 40, authorization=merge_auth)
    assert exc.value.code == model.R_UNAUTHORIZED


def test_git_adapter_authorized_write_is_deterministic() -> None:
    auth = authorize(model.GATE_GO_COMMIT, "token", actor="operator")
    git = FakeGitAdapter(branch="feature", head_sha="a" * 40)
    result = git.commit_all("deterministic", authorization=auth)
    assert model.is_git_sha(result.commit_sha)
    assert result.commit_sha != "a" * 40
    pushed = git.push("feature", result.commit_sha, authorization=auth)
    assert pushed.pushed_sha == result.commit_sha
    assert git.remote_heads["feature"] == result.commit_sha
    with pytest.raises(model.ReleaseError) as exc:
        git.push("feature", "b" * 40, authorization=auth)
    assert exc.value.code == model.R_HEAD_MOVED


def test_review_gate_evidence_validation(tmp_path: Path) -> None:
    evidence = model.ReviewGateEvidence(
        mission_id="m", run_id="m", lifecycle="COMPLETE",
        readiness="READY_FOR_COMMIT", require_review=True,
        final_review_enabled=True, final_reviewer_active=True,
        final_reviewer_model="qwen3.8:27b-q4_K_M",
        reviewed_patch="a" * 64, current_patch="a" * 64,
        semantic_patch_identity="a" * 64, fresh_review=True,
        review_outcome="VALID_PASS", review_reason="OK", review_at="t",
        attempts=1, repairs=0, ready=True, reason=model.R_OK)
    assert evidence.ready
    assert evidence.to_dict()["semantic_patch_identity"] == "a" * 64
