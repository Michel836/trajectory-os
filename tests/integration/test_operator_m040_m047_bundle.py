"""M040–M047 integration — the operator platform on the production path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from trajectory_os.assembly import model as assembly_model
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.assembly import store as assembly_store
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store
from trajectory_os.operator import acceptance as operator_acceptance
from trajectory_os.operator import cli as operator_cli
from trajectory_os.operator import dogfood as operator_dogfood
from trajectory_os.operator import events as operator_events
from trajectory_os.operator import model, recovery
from trajectory_os.operator import state as operator_state
from trajectory_os.release import acceptance as release_acceptance
from trajectory_os.release import closure as release_closure
from trajectory_os.release import evidence as release_evidence
from trajectory_os.release import model as release_model
from trajectory_os.release import store as release_store


def _digest(path: Path) -> str:
    material = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            material.update(str(item.relative_to(path)).encode("utf-8"))
            material.update(item.read_bytes())
    return material.hexdigest()


def test_full_operator_path_through_control_plane(tmp_path: Path) -> None:
    pipe = operator_acceptance.OperatorPipeline(
        str(tmp_path), "m040-m047-integration")
    pipe.full_release()
    closure = release_store.load_release_closure(pipe.mission_root)
    state = release_store.load_state(pipe.mission_root)
    assert closure.status == "CLOSED"
    assert state.stage == release_model.RST_RELEASED
    assert closure.target_branch_verified
    gate = pipe.review_gate()
    assert gate.ready
    assert closure.reviewed_patch_sha256 == gate.semantic_patch_identity


def test_operator_acceptance_matrix_passes(tmp_path: Path) -> None:
    report = operator_acceptance.run_acceptance(tmp_path)
    assert report.status == "PASS", report.render()
    assert len(report.cases) == 35
    assert all(case.ok for case in report.cases)


def test_self_hosting_dogfood_evidence(tmp_path: Path) -> None:
    evidence = operator_dogfood.run_self_hosting_dogfood(
        tmp_path, repo=None, write_evidence=True)
    assert evidence["status"] == "PASS", json.dumps(evidence, indent=2)
    assert evidence["fixture_proof"]["ok"]
    assert evidence["live_dogfood"]["ready_for_commit"]
    assert evidence["guardrails"]["crossed_human_gate"] is False
    stored = operator_dogfood.load_self_hosting_evidence(str(tmp_path))
    assert stored is not None
    assert stored["status"] == "PASS"


def test_execution_interrupt_then_resume(tmp_path: Path) -> None:
    pipe = operator_acceptance.OperatorPipeline(
        str(tmp_path), "m040-exec-resume")
    pipe.start(interrupt_after_phase=assembly_model.MP_PLAN)
    decision = recovery.decide_recovery(str(tmp_path), pipe.mission_id,
                                        clock=pipe.clock)
    assert decision.action == recovery.RA_RESUME_EXECUTION
    pipe.plane.resume(
        pipe.mission_id, executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory(
            [release_acceptance.PASS_REVIEW]))
    status = obs_store.load_status(pipe.mission_root)
    assert status["readiness"] == obs_model.RD_READY_FOR_COMMIT
    assert status["run_id"] == pipe.mission_id


def test_commit_and_pr_and_merge_are_never_duplicated(tmp_path: Path) -> None:
    pipe = operator_acceptance.OperatorPipeline(
        str(tmp_path), "m040-idempotence")
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    from trajectory_os.operator import model as operator_model

    try:
        pipe.commit()
        raise AssertionError("second commit must fail closed")
    except release_model.ReleaseError as exc:
        assert exc.code == release_model.R_ALREADY_COMMITTED
    except operator_model.OperatorError as exc:  # pragma: no cover
        assert exc.code == release_model.R_ALREADY_COMMITTED
    assert len(pipe.git.commits) == 1
    first = pipe.bind()
    second = pipe.bind()
    assert first["number"] == second["number"]
    assert len(pipe.github.prs) == 1
    pipe.set_ci(release_model.CI_SUCCESS)
    pipe.watch()
    pipe.merge_handoff()
    pipe.merge()
    pipe.merge()
    assert len(pipe.github.merged) == 1


def test_observation_is_read_only(tmp_path: Path) -> None:
    pipe = operator_acceptance.OperatorPipeline(
        str(tmp_path), "m040-readonly")
    pipe.full_release()
    before = _digest(pipe.mission_root)
    pipe.plane.status(pipe.mission_id)
    pipe.plane.dashboard(pipe.mission_id)
    pipe.plane.follow(pipe.mission_id, iterations=2)
    pipe.plane.pr_status(pipe.mission_id)
    pipe.plane.reconstruct(pipe.mission_id)
    operator_state.build_operator_state(str(tmp_path), pipe.mission_id)
    operator_events.replay(str(tmp_path), pipe.mission_id)
    after = _digest(pipe.mission_root)
    assert before == after


def test_legacy_release_artifacts_remain_readable(tmp_path: Path) -> None:
    pipe = operator_acceptance.OperatorPipeline(
        str(tmp_path), "m040-legacy")
    pipe.full_release()
    evidence = release_evidence.load_mission_evidence(
        str(tmp_path), pipe.mission_id)
    gate = release_evidence.derive_review_gate(evidence)
    assert gate.ready
    reconstructed = release_closure.reconstruct_release(
        str(tmp_path), pipe.mission_id)
    assert reconstructed["release"]["release_closure"]["commit_sha"]
    types = {event.type for event in operator_events.derive_events(
        str(tmp_path), pipe.mission_id)}
    assert operator_events.T_RELEASE_CLOSURE in types
    assert operator_events.T_LEGACY_CANONICAL in types


def test_operator_cli_status_and_version(tmp_path: Path, capsys) -> None:
    pipe = operator_acceptance.OperatorPipeline(
        str(tmp_path), "m040-cli")
    pipe.full_release()
    assert operator_cli.main(["version"]) == 0
    out = capsys.readouterr().out
    assert "operator" in out
    code = operator_cli.main([
        "status", "--root", str(tmp_path),
        "--mission-id", pipe.mission_id, "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mission_id"] == pipe.mission_id
    assert payload["release_closure"] == "CLOSED"


def test_policy_and_routing_are_persisted_before_use(tmp_path: Path) -> None:
    pipe = operator_acceptance.OperatorPipeline(
        str(tmp_path), "m040-persisted")
    pipe.ready()
    mission_root = pipe.mission_root
    policy_doc = json.loads(
        (mission_root / model.POLICY_NAME).read_text(encoding="utf-8"))
    routing_doc = json.loads(
        (mission_root / model.ROUTING_NAME).read_text(encoding="utf-8"))
    assert policy_doc["profile"] == "release"
    assert routing_doc["final_review"]["model"] == "qwen3.8:27b-q4_K_M"
    assert routing_doc["implementation"]["backend"] == "pi"
    assert assembly_store.mission_exists(str(tmp_path), pipe.mission_id)


def test_control_plane_start_defaults_stay_fixture_safe(
    tmp_path: Path,
) -> None:
    from trajectory_os.operator.control_plane import ControlPlane

    plane = ControlPlane(str(tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outcome = plane.start(assembly_run.MissionRequest(
        objective="fixture-safe", workspace=str(workspace),
        mission_id="m040-fixture-safe"))
    assert outcome.result["status"]["readiness"] == (
        obs_model.RD_READY_FOR_COMMIT)
