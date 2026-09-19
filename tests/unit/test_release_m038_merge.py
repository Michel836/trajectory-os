"""M038 unit — explicit GO MERGE gate and authorized squash merge."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from trajectory_os.assembly import store as assembly_store
from trajectory_os.release import acceptance, merge_gate, model, store
from trajectory_os.release.authorization import authorize, unauthorized
from trajectory_os.release.git_adapter import FakeGitAdapter


def _pipeline(tmp_path: Path, mission_id: str) -> acceptance.Pipeline:
    return acceptance.Pipeline(
        root=str(tmp_path), mission_id=mission_id,
        git=FakeGitAdapter(branch="feature", head_sha="a" * 40),
        github=acceptance.FixtureGitHubAdapter(),
        clock=acceptance.ScriptedClock())


def _green(tmp_path: Path, mission_id: str) -> acceptance.Pipeline:
    pipe = _pipeline(tmp_path, mission_id)
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    pipe.bind()
    pipe.set_ci(model.CI_SUCCESS)
    pipe.watch()
    return pipe


def _replace_pr(pipe: acceptance.Pipeline, number: int,
                **changes: object) -> None:
    for index, pr in enumerate(pipe.github.prs):
        if pr.number == number:
            pipe.github.prs[index] = dataclasses.replace(pr, **changes)
            return
    raise AssertionError("pr not found")


def test_green_exact_head_ci_allows_merge(tmp_path: Path) -> None:
    pipe = _green(tmp_path, "m038-merge")
    handoff = pipe.merge_handoff()
    assert handoff.gate == model.GATE_GO_MERGE
    assert handoff.ci_state == model.CI_SUCCESS
    assert handoff.merge_method == model.MERGE_SQUASH
    result = pipe.merge()
    assert result.merged
    assert result.merge_method == model.MERGE_SQUASH
    assert result.target_branch == "main"
    assert result.target_head_sha == result.merge_sha
    assert result.verified is True
    assert pipe.github.branch_heads["main"] == result.merge_sha


def test_repeated_merge_is_idempotent(tmp_path: Path) -> None:
    pipe = _green(tmp_path, "m038-twice")
    pipe.merge_handoff()
    first = pipe.merge()
    second = pipe.merge()
    assert second.merge_sha == first.merge_sha
    assert len(pipe.github.merged) == 1


def test_missing_merge_handoff_fails_closed(tmp_path: Path) -> None:
    pipe = _green(tmp_path, "m038-nohandoff")
    with pytest.raises(model.ReleaseError) as exc:
        pipe.merge()
    assert exc.value.code == model.R_MERGE_HANDOFF_MISSING


def test_unauthorized_merge_is_refused(tmp_path: Path) -> None:
    pipe = _green(tmp_path, "m038-unauth")
    pipe.merge_handoff()
    with pytest.raises(model.ReleaseError) as exc:
        merge_gate.go_merge(str(tmp_path), "m038-unauth", github=pipe.github,
                            authorization=unauthorized(model.GATE_GO_MERGE))
    assert exc.value.code == model.R_UNAUTHORIZED
    with pytest.raises(model.ReleaseError) as exc:
        merge_gate.go_merge(
            str(tmp_path), "m038-unauth", github=pipe.github,
            authorization=authorize(model.GATE_GO_COMMIT, "t",
                                    actor="operator"))
    assert exc.value.code == model.R_UNAUTHORIZED


def test_moved_pr_head_blocks_merge(tmp_path: Path) -> None:
    pipe = _green(tmp_path, "m038-moved")
    binding = store.load_pr_binding(assembly_store.mission_root(
        str(tmp_path), "m038-moved"))
    pipe.merge_handoff()
    _replace_pr(pipe, binding.number, head_sha="d" * 40)
    with pytest.raises(model.ReleaseError) as exc:
        pipe.merge()
    assert exc.value.code in (model.R_PR_HEAD_MOVED, model.R_CI_NOT_GREEN)


def test_closed_pr_blocks_merge(tmp_path: Path) -> None:
    pipe = _green(tmp_path, "m038-closed")
    binding = store.load_pr_binding(assembly_store.mission_root(
        str(tmp_path), "m038-closed"))
    _replace_pr(pipe, binding.number, state=model.PR_CLOSED)
    with pytest.raises(model.ReleaseError) as exc:
        pipe.merge_handoff()
    assert exc.value.code == model.R_PR_NOT_OPEN


def test_not_mergeable_pr_blocks_merge(tmp_path: Path) -> None:
    pipe = _green(tmp_path, "m038-conflict")
    binding = store.load_pr_binding(assembly_store.mission_root(
        str(tmp_path), "m038-conflict"))
    _replace_pr(pipe, binding.number, mergeable=False,
                mergeable_state="CONFLICTING")
    with pytest.raises(model.ReleaseError) as exc:
        pipe.merge_handoff()
    assert exc.value.code == model.R_PR_NOT_MERGEABLE


def test_merge_method_change_is_stale(tmp_path: Path) -> None:
    pipe = _green(tmp_path, "m038-method")
    pipe.merge_handoff()
    with pytest.raises(model.ReleaseError) as exc:
        merge_gate.go_merge(
            str(tmp_path), "m038-method", github=pipe.github,
            authorization=authorize(model.GATE_GO_MERGE, "t",
                                    actor="operator"),
            merge_method=model.MERGE_REBASE)
    assert exc.value.code == model.R_MERGE_HANDOFF_STALE


def test_merge_handoff_validation_requires_green_ci() -> None:
    handoff_obj = model.MergeHandoff(
        mission_id="m", release_commit_sha="a" * 40, pr_number=1,
        pr_url=None, pr_head_sha="a" * 40, base_branch="main",
        base_sha=None, ci_state=model.CI_QUEUED, ci_head_sha="a" * 40,
        ci_checked_at="t", merge_method=model.MERGE_SQUASH,
        gate=model.GATE_GO_MERGE, gate_reason=model.R_OK,
        created_at="t")
    with pytest.raises(model.ReleaseError) as exc:
        handoff_obj.validate()
    assert exc.value.code == model.R_CI_NOT_GREEN


def test_invalid_merge_method_is_rejected(tmp_path: Path) -> None:
    pipe = _green(tmp_path, "m038-badmethod")
    with pytest.raises(model.ReleaseError) as exc:
        merge_gate.build_merge_handoff(
            str(tmp_path), "m038-badmethod", github=pipe.github,
            base_branch="main", merge_method="octopus")
    assert exc.value.code == model.R_MERGE_METHOD_INVALID
