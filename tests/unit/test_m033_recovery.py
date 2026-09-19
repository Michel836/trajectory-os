"""M033 unit — recovery/stale-state detection and resume-point selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from trajectory_os.assembly import model, recovery, store
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store

PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")


def _workspace(root: Path, mission_id: str) -> str:
    workspace = root / mission_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


def _request(root: Path, mission_id: str, **overrides: object,
             ) -> assembly_run.MissionRequest:
    data: dict[str, object] = {
        "objective": "fix add(a, b) so it adds",
        "workspace": _workspace(root, mission_id),
        "mission_id": mission_id,
        "workload_id": "small-targeted-repair",
    }
    data.update(overrides)
    return assembly_run.MissionRequest(**data)


def _orchestrator(root: Path) -> assembly_run.MissionOrchestrator:
    return assembly_run.MissionOrchestrator(
        root, executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory([PASS_REVIEW]))


def test_unknown_mission_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(model.AssemblyError) as exc:
        recovery.detect_resume(tmp_path, "does-not-exist")
    assert exc.value.code == model.R_MISSION_MISSING


def test_intake_only_resumes_at_preflight(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.start(_request(tmp_path, "m033-intake"),
                       interrupt_after_phase=model.MP_PLAN)
    decision = recovery.detect_resume(tmp_path, "m033-intake")
    # The plan is durable, so the explicit safe resume point is execution.
    assert decision.kind == recovery.RC_RESUME_EXECUTION
    assert decision.resume_point == model.MP_EXECUTION
    assert decision.terminal is False


def test_finalized_execution_resumes_at_human_gate(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.start(_request(tmp_path, "m033-final"),
                       interrupt_after_phase=model.MP_EXECUTION)
    decision = recovery.detect_resume(tmp_path, "m033-final")
    assert decision.kind == recovery.RC_EXECUTION_FINALIZED
    assert decision.resume_point == model.MP_HUMAN_GATE
    assert decision.execution_finalized is True


def test_closure_is_idempotent_terminal_resume(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.start(_request(tmp_path, "m033-closed"))
    decision = recovery.detect_resume(tmp_path, "m033-closed")
    assert decision.kind == recovery.RC_TERMINAL_COMPLETE
    assert decision.terminal is True and decision.idempotent is True
    assert decision.resume_point == model.MP_CLOSURE


def test_stale_complete_without_evidence_re_executes(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.start(_request(tmp_path, "m033-stale"),
                       interrupt_after_phase=model.MP_EXECUTION)
    mission_root = store.mission_root(tmp_path, "m033-stale")
    # Remove the durable execution marker and the closure, then claim COMPLETE.
    store.load_mission(tmp_path, "m033-stale")
    status = obs_store.load_status(mission_root)
    status["state"] = obs_model.LC_COMPLETE
    status["readiness"] = obs_model.RD_READY_FOR_COMMIT
    obs_store.write_json(mission_root / obs_store.STATUS_NAME, status)
    events = obs_store.load_events(mission_root)
    kept = [event for event in events if event["kind"] != "RUN_COMPLETED"]
    _rewrite_events(mission_root, kept)
    (mission_root / store.CLOSURE_NAME).unlink(missing_ok=True)

    decision = recovery.detect_resume(tmp_path, "m033-stale")
    assert decision.kind == recovery.RC_RESUME_EXECUTION
    assert "incomplete/stale" in decision.reason


def test_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.start(_request(tmp_path, "m033-identity"))
    mission_root = store.mission_root(tmp_path, "m033-identity")
    status = obs_store.load_status(mission_root)
    status["run_id"] = "some-other-mission"
    obs_store.write_json(mission_root / obs_store.STATUS_NAME, status)
    with pytest.raises(model.AssemblyError) as exc:
        recovery.detect_resume(tmp_path, "m033-identity")
    assert exc.value.code == model.R_IDENTITY_MISMATCH


def test_resume_preserves_identity_and_evidence(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    first = orchestrator.start(
        _request(tmp_path, "m033-resume"),
        interrupt_after_phase=model.MP_EXECUTION)
    mission_root = store.mission_root(tmp_path, "m033-resume")
    events_before = obs_store.load_events(mission_root)
    resumed = orchestrator.resume("m033-resume")
    events_after = obs_store.load_events(mission_root)
    assert resumed.mission.mission_id == first.mission.mission_id
    assert events_after[:len(events_before)] == events_before
    assert resumed.closure is not None
    # The recovery record now reflects the terminal, idempotent decision.
    record = recovery.read_recovery_record(tmp_path, "m033-resume")
    assert record is not None and record.kind == recovery.RC_TERMINAL_COMPLETE


def test_repeated_resume_is_byte_idempotent(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.start(_request(tmp_path, "m033-repeat"))
    mission_root = store.mission_root(tmp_path, "m033-repeat")
    events = obs_store.load_events(mission_root)
    digest = _digest(mission_root)
    again = orchestrator.resume("m033-repeat")
    assert again.closure is not None
    assert obs_store.load_events(mission_root) == events
    assert _digest(mission_root) == digest


def test_write_recovery_record_is_idempotent(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.start(_request(tmp_path, "m033-record"))
    decision = recovery.detect_resume(tmp_path, "m033-record")
    path = recovery.write_recovery_record(tmp_path, decision)
    first = path.read_bytes()
    recovery.write_recovery_record(tmp_path, decision)
    assert path.read_bytes() == first


def _rewrite_events(mission_root: Path, events: list[dict[str, object]]) -> None:
    import json

    path = mission_root / obs_store.EVENTS_NAME
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True,
                                    separators=(",", ":")) + "\n")


def _digest(mission_root: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(mission_root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(mission_root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()
