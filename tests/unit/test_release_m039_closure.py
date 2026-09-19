"""M039 unit — release closure and read-only reconstruction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trajectory_os.assembly import store as assembly_store
from trajectory_os.release import acceptance, closure, evidence, model, store
from trajectory_os.release.git_adapter import FakeGitAdapter


def _pipeline(tmp_path: Path, mission_id: str) -> acceptance.Pipeline:
    return acceptance.Pipeline(
        root=str(tmp_path), mission_id=mission_id,
        git=FakeGitAdapter(branch="feature", head_sha="a" * 40),
        github=acceptance.FixtureGitHubAdapter(),
        clock=acceptance.ScriptedClock())


def _digest(mission_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(mission_root.rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_release_closure_links_the_whole_chain(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m039-link")
    pipe.full_release()
    mission_root = assembly_store.mission_root(str(tmp_path), "m039-link")
    result = store.load_release_closure(mission_root)
    gate = evidence.derive_review_gate(
        evidence.load_mission_evidence(str(tmp_path), "m039-link"))
    commit = store.load_commit_result(mission_root)
    binding = store.load_pr_binding(mission_root)
    merge = store.load_merge_result(mission_root)
    assert result.status == "CLOSED"
    assert result.reviewed_patch_sha256 == gate.semantic_patch_identity
    assert result.final_patch_sha256 == gate.semantic_patch_identity
    assert result.commit_sha == commit.commit_sha
    assert result.pr_number == binding.number
    assert result.pr_head_sha == binding.head_sha
    assert result.ci_status == model.CI_SUCCESS
    assert result.ci_head_sha == commit.commit_sha
    assert result.merge_sha == merge.merge_sha
    assert result.target_branch_verified is True
    assert result.go_commit_evidence["gate"] == model.GATE_GO_COMMIT
    assert result.go_merge_evidence["gate"] == model.GATE_GO_MERGE
    assert result.issue_closure["relation"] in ("closes", "not-recorded")


def test_issue_closure_from_operator(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m039-issue")
    pipe.full_release()
    mission_root = assembly_store.mission_root(str(tmp_path), "m039-issue")
    result = closure.build_release_closure(
        str(tmp_path), "m039-issue", github=pipe.github, base_branch="main",
        issue="238")
    assert result.issue_closure["relation"] == "closes"
    assert result.issue_closure["issue"] == "238"
    assert store.load_release_closure(mission_root).issue_closure["issue"] \
        == "238"


def test_closure_requires_authoritative_merge(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m039-nomerge")
    pipe.ready()
    with pytest.raises(model.ReleaseError) as exc:
        closure.build_release_closure(str(tmp_path), "m039-nomerge",
                                      github=pipe.github, base_branch="main")
    assert exc.value.code == model.R_NOT_MERGED


def test_reconstruct_is_read_only_and_idempotent(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m039-reconstruct")
    pipe.full_release()
    mission_root = assembly_store.mission_root(str(tmp_path),
                                               "m039-reconstruct")
    before = _digest(mission_root)
    first = closure.reconstruct_release(str(tmp_path), "m039-reconstruct")
    second = closure.reconstruct_release(str(tmp_path), "m039-reconstruct")
    assert _digest(mission_root) == before
    assert first["release"]["release_closure"]["status"] == "CLOSED"
    assert first["release"]["state"]["stage"] == model.RST_RELEASED
    assert json.dumps(first, sort_keys=True) == json.dumps(second,
                                                           sort_keys=True)


def test_reconstruct_unknown_mission_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(model.ReleaseError):
        closure.reconstruct_release(str(tmp_path), "missing-mission")
