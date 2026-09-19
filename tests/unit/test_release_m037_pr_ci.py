"""M037 unit — exactly-one PR binding and exact-head CI watch."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from trajectory_os.assembly import store as assembly_store
from trajectory_os.release import acceptance, merge_gate, model, pull_request
from trajectory_os.release.git_adapter import FakeGitAdapter


def _pipeline(tmp_path: Path, mission_id: str) -> acceptance.Pipeline:
    return acceptance.Pipeline(
        root=str(tmp_path), mission_id=mission_id,
        git=FakeGitAdapter(branch="feature", head_sha="a" * 40),
        github=acceptance.FixtureGitHubAdapter(),
        clock=acceptance.ScriptedClock())


def _committed(tmp_path: Path, mission_id: str) -> acceptance.Pipeline:
    pipe = _pipeline(tmp_path, mission_id)
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    return pipe


def _digest(mission_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(mission_root.rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_bind_creates_exactly_one_pr_for_exact_head(tmp_path: Path) -> None:
    pipe = _committed(tmp_path, "m037-bind")
    binding = pipe.bind()
    commit = acceptance.store.load_commit_result(
        assembly_store.mission_root(str(tmp_path), "m037-bind"))
    assert binding.bound_commit_sha == commit.commit_sha
    assert binding.head_sha == commit.commit_sha
    assert binding.created is True
    assert len(pipe.github.prs) == 1
    again = pipe.bind()
    assert again.number == binding.number
    assert len(pipe.github.prs) == 1


def test_ambiguous_existing_prs_fail_closed(tmp_path: Path) -> None:
    pipe = _committed(tmp_path, "m037-ambig")
    commit = acceptance.store.load_commit_result(
        assembly_store.mission_root(str(tmp_path), "m037-ambig"))
    pipe.github.add_pull_request(head_branch="feature", base_branch="main",
                                 head_sha=commit.commit_sha, number=10)
    pipe.github.add_pull_request(head_branch="feature", base_branch="main",
                                 head_sha=commit.commit_sha, number=11)
    with pytest.raises(model.ReleaseError) as exc:
        pipe.bind()
    assert exc.value.code == model.R_PR_AMBIGUOUS


def test_moved_pr_head_fails_closed(tmp_path: Path) -> None:
    pipe = _committed(tmp_path, "m037-head")
    pipe.github.add_pull_request(head_branch="feature", base_branch="main",
                                 head_sha="d" * 40, number=20)
    with pytest.raises(model.ReleaseError) as exc:
        pipe.bind()
    assert exc.value.code == model.R_PR_HEAD_MOVED


def test_watch_before_bind_fails_closed(tmp_path: Path) -> None:
    pipe = _committed(tmp_path, "m037-nobind")
    with pytest.raises(model.ReleaseError) as exc:
        pipe.watch()
    assert exc.value.code == model.R_PR_MISSING


def test_watch_ci_distinguishes_every_state(tmp_path: Path) -> None:
    pipe = _committed(tmp_path, "m037-states")
    pipe.bind()
    commit_sha = pipe.set_ci(model.CI_SUCCESS)
    assert pipe.watch().state == model.CI_SUCCESS
    pipe.github.set_checks(commit_sha, ())
    assert pipe.watch().state == model.CI_MISSING
    assert pipe.watch().green is False


def test_watch_ci_persist_false_is_read_only(tmp_path: Path) -> None:
    pipe = _committed(tmp_path, "m037-readonly")
    pipe.bind()
    pipe.set_ci(model.CI_SUCCESS)
    mission_root = assembly_store.mission_root(str(tmp_path), "m037-readonly")
    before = _digest(mission_root)
    status = pipe.watch(persist=False)
    assert status.state == model.CI_SUCCESS
    assert _digest(mission_root) == before


def test_every_non_green_ci_blocks_merge(tmp_path: Path) -> None:
    for state in (model.CI_QUEUED, model.CI_IN_PROGRESS, model.CI_FAILURE,
                  model.CI_CANCELLED, model.CI_MISSING):
        mission_id = f"m037-ci-{state}"
        pipe = _committed(tmp_path, mission_id)
        pipe.bind()
        if state != model.CI_MISSING:
            pipe.set_ci(state)
        pipe.watch()
        with pytest.raises(model.ReleaseError) as exc:
            merge_gate.build_merge_handoff(
                str(tmp_path), mission_id, github=pipe.github,
                base_branch="main")
        assert exc.value.code == model.R_CI_NOT_GREEN, state


def test_ci_missing_reason_is_explicit(tmp_path: Path) -> None:
    pipe = _committed(tmp_path, "m037-missing")
    pipe.bind()
    status = pipe.watch()
    assert status.state == model.CI_MISSING
    assert status.missing_reason
    assert status.failed_jobs == ()


def test_watch_ci_uses_exact_head_only(tmp_path: Path) -> None:
    pipe = _committed(tmp_path, "m037-exact")
    commit = acceptance.store.load_commit_result(
        assembly_store.mission_root(str(tmp_path), "m037-exact"))
    pipe.bind()
    # CI recorded for a *different* head must never be picked up by a
    # branch-name-only lookup.
    pipe.github.set_checks("e" * 40, (acceptance.ci_runs("e" * 40,
                                                          model.CI_SUCCESS),))
    status = pull_request.watch_ci(str(tmp_path), "m037-exact",
                                   github=pipe.github, persist=False)
    assert status.head_sha == commit.commit_sha
    assert status.state == model.CI_MISSING
