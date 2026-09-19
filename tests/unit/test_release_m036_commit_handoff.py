"""M036 unit — deterministic GO COMMIT handoff and authorized commit/push."""

from __future__ import annotations

from pathlib import Path

import pytest

from trajectory_os.assembly import store as assembly_store
from trajectory_os.observability import store as obs_store
from trajectory_os.release import acceptance, handoff, model, store
from trajectory_os.release.authorization import authorize, unauthorized
from trajectory_os.release.git_adapter import FakeGitAdapter


def _pipeline(tmp_path: Path, mission_id: str) -> acceptance.Pipeline:
    return acceptance.Pipeline(
        root=str(tmp_path), mission_id=mission_id,
        git=FakeGitAdapter(branch="feature", head_sha="a" * 40),
        github=acceptance.FixtureGitHubAdapter(),
        clock=acceptance.ScriptedClock())


def test_ready_mission_emits_deterministic_handoff(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m036-ready")
    pipe.ready()
    first = pipe.handoff()
    second = pipe.handoff()
    assert first.gate == model.GATE_GO_COMMIT
    assert first.patch_sha256 == first.reviewed_patch_sha256
    assert model.is_sha256(first.patch_sha256)
    assert first.branch == "feature"
    assert first.current_head == "a" * 40
    assert first.baseline_head == "a" * 40
    assert first.scope_summary["objective"]
    assert first.proposed_commit_message.startswith("release acceptance")
    assert second.patch_sha256 == first.patch_sha256
    assert second.scope_summary == first.scope_summary
    assert store.exists(assembly_store.mission_root(str(tmp_path), "m036-ready"),
                        store.COMMIT_HANDOFF_NAME)


def test_not_ready_mission_fails_closed(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m036-blocked")
    pipe.ready()
    mission_root = assembly_store.mission_root(str(tmp_path), "m036-blocked")
    document = store.read_document(mission_root, "closure.json")
    document["readiness"] = "BLOCKED"
    obs_store.write_json(mission_root / "closure.json", document)
    with pytest.raises(model.ReleaseError) as exc:
        pipe.handoff()
    assert exc.value.code == model.R_READINESS_NOT_READY


def test_authorized_commit_and_push_records_exact_sha(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m036-commit")
    pipe.ready()
    handoff_obj = pipe.handoff()
    result = pipe.commit()
    assert result.commit_sha != handoff_obj.current_head
    assert result.pushed_sha == result.commit_sha
    assert pipe.git.remote_heads["feature"] == result.commit_sha
    assert result.reviewed_patch_sha256 == handoff_obj.reviewed_patch_sha256
    state = store.load_state(assembly_store.mission_root(
        str(tmp_path), "m036-commit"))
    assert state.stage == model.RST_COMMITTED
    assert state.commit_sha == result.commit_sha


def test_unauthorized_commit_is_refused(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m036-unauth")
    pipe.ready()
    pipe.handoff()
    with pytest.raises(model.ReleaseError) as exc:
        handoff.go_commit(str(tmp_path), "m036-unauth", git=pipe.git,
                          authorization=unauthorized(model.GATE_GO_COMMIT))
    assert exc.value.code == model.R_UNAUTHORIZED
    with pytest.raises(model.ReleaseError) as exc:
        handoff.go_commit(
            str(tmp_path), "m036-unauth", git=pipe.git,
            authorization=authorize(model.GATE_GO_MERGE, "t",
                                    actor="operator"))
    assert exc.value.code == model.R_UNAUTHORIZED


def test_missing_handoff_fails_closed(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m036-nohandoff")
    pipe.ready()
    with pytest.raises(model.ReleaseError) as exc:
        pipe.commit()
    assert exc.value.code == model.R_HANDOFF_MISSING


def test_stale_patch_blocks_commit(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m036-stale")
    pipe.ready()
    pipe.handoff()
    mission_root = assembly_store.mission_root(str(tmp_path), "m036-stale")
    document = store.read_document(mission_root, "closure.json")
    document["current_patch"] = "f" * 64
    obs_store.write_json(mission_root / "closure.json", document)
    with pytest.raises(model.ReleaseError) as exc:
        pipe.commit()
    assert exc.value.code in (model.R_READINESS_CHANGED,
                              model.R_PATCH_MISMATCH)


def test_moved_head_blocks_commit(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m036-head")
    pipe.ready()
    pipe.handoff()
    pipe.git.advance_head(head_sha="c" * 40)
    with pytest.raises(model.ReleaseError) as exc:
        pipe.commit()
    assert exc.value.code == model.R_HEAD_MOVED


def test_moved_branch_blocks_commit(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m036-branch")
    pipe.ready()
    pipe.handoff()
    pipe.git.advance_head(branch="other")
    with pytest.raises(model.ReleaseError) as exc:
        pipe.commit()
    assert exc.value.code == model.R_BRANCH_MOVED


def test_repeated_commit_fails_closed(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path, "m036-twice")
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    with pytest.raises(model.ReleaseError) as exc:
        pipe.commit()
    assert exc.value.code == model.R_ALREADY_COMMITTED
