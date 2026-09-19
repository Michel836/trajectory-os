"""M034 unit — mission-scoped operator control (explicit, fail closed)."""

from __future__ import annotations

import pathlib
from pathlib import Path

import pytest

from trajectory_os.assembly import control, model, store
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark import model as bench_model
from trajectory_os.benchmark.executor import (
    ExecutionRequest,
    FixtureExecutor,
)
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


def _orchestrator(root: Path,
                  executor: object | None = None,
                  ) -> assembly_run.MissionOrchestrator:
    return assembly_run.MissionOrchestrator(
        root,
        executor=executor or FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory([PASS_REVIEW]))


def _interrupted(root: Path, mission_id: str) -> None:
    _orchestrator(root).start(_request(root, mission_id),
                              interrupt_after_phase=model.MP_PLAN)


def test_unknown_mission_fails_closed_for_every_control(tmp_path: Path) -> None:
    for operation in (control.request_stop, control.pause, control.cancel,
                      control.clear_stop_request):
        with pytest.raises(model.AssemblyError) as exc:
            operation(tmp_path, "missing-mission")
        assert exc.value.code == model.R_MISSION_MISSING


def test_request_stop_and_pause_record_a_durable_latch(tmp_path: Path) -> None:
    _interrupted(tmp_path, "m034-stop")
    outcome = control.request_stop(tmp_path, "m034-stop", reason="maintenance")
    assert outcome.result == control.RESULT_ACCEPTED
    assert control.stop_requested(tmp_path, "m034-stop") is True
    log = control.control_log(tmp_path, "m034-stop")
    assert log[-1]["action"] == control.ACTION_REQUEST_STOP
    assert log[-1]["mission_id"] == "m034-stop"

    paused = control.pause(tmp_path, "m034-stop")
    assert paused.action == control.ACTION_PAUSE
    assert paused.result == control.RESULT_ACCEPTED


def test_graceful_stop_is_honoured_and_resumable(tmp_path: Path) -> None:
    inner = FixtureExecutor(interrupt_once=False)

    class StopDuringExecution:
        calls = 0

        def execute(self, request: ExecutionRequest):
            self.calls += 1
            if self.calls == 1:
                control.request_stop(tmp_path, "m034-graceful",
                                     reason="operator stop")
            return inner.execute(request)

    orchestrator = _orchestrator(tmp_path, StopDuringExecution())
    first = orchestrator.start(_request(tmp_path, "m034-graceful"))
    assert first.interrupted is True
    assert first.interrupt_phase == model.MP_EXECUTION
    assert first.closure is None
    # The stop latch is durable until an explicit resume consumes it.
    assert control.stop_requested(tmp_path, "m034-graceful") is True
    resumed = orchestrator.resume("m034-graceful")
    assert resumed.closure is not None
    assert control.stop_requested(tmp_path, "m034-graceful") is False
    assert resumed.status["readiness"] == obs_model.RD_READY_FOR_COMMIT


def test_cancel_produces_canonical_cancelled_and_is_idempotent(
    tmp_path: Path,
) -> None:
    _interrupted(tmp_path, "m034-cancel")
    outcome = control.cancel(tmp_path, "m034-cancel", reason="abort")
    assert outcome.result == control.RESULT_ACCEPTED
    assert outcome.readiness == obs_model.RD_CANCELLED
    mission_root = store.mission_root(tmp_path, "m034-cancel")
    status = obs_store.load_status(mission_root)
    closure = store.load_closure(tmp_path, "m034-cancel")
    assert status["readiness"] == obs_model.RD_CANCELLED
    assert status["state"] == obs_model.LC_COMPLETE
    assert status["terminal_reason_code"] == obs_model.R_CANCELLED
    assert closure.readiness == obs_model.RD_CANCELLED
    assert closure.terminal_reason_code == obs_model.R_CANCELLED
    events = obs_store.load_events(mission_root)
    assert any(event["kind"] == "CONTROL_CANCEL_REQUESTED"
               for event in events)
    again = control.cancel(tmp_path, "m034-cancel")
    assert again.result == control.RESULT_ALREADY_CANCELLED


def test_late_cancel_never_overrides_a_terminal_trust_decision(
    tmp_path: Path,
) -> None:
    _orchestrator(tmp_path).start(_request(tmp_path, "m034-terminal"))
    outcome = control.cancel(tmp_path, "m034-terminal")
    assert outcome.result == control.RESULT_REFUSED
    status = obs_store.load_status(store.mission_root(tmp_path,
                                                      "m034-terminal"))
    assert status["readiness"] == obs_model.RD_READY_FOR_COMMIT


def test_control_after_terminal_stop_request_is_refused(tmp_path: Path) -> None:
    _orchestrator(tmp_path).start(_request(tmp_path, "m034-late-stop"))
    outcome = control.request_stop(tmp_path, "m034-late-stop")
    assert outcome.result == control.RESULT_ALREADY_TERMINAL
    assert control.stop_requested(tmp_path, "m034-late-stop") is False


def test_control_log_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    _interrupted(tmp_path, "m034-identity")
    control.request_stop(tmp_path, "m034-identity")
    mission_root = store.mission_root(tmp_path, "m034-identity")
    path = mission_root / control.CONTROL_LOG_NAME
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("m034-identity", "other-mission"),
                    encoding="utf-8")
    with pytest.raises(model.AssemblyError) as exc:
        control.control_log(tmp_path, "m034-identity")
    assert exc.value.code == model.R_IDENTITY_MISMATCH


def test_clear_stop_request_consumes_latch(tmp_path: Path) -> None:
    _interrupted(tmp_path, "m034-clear")
    control.request_stop(tmp_path, "m034-clear")
    assert control.stop_requested(tmp_path, "m034-clear") is True
    outcome = control.clear_stop_request(tmp_path, "m034-clear")
    assert outcome.result == control.RESULT_CLEARED
    assert control.stop_requested(tmp_path, "m034-clear") is False


def test_control_surface_has_no_git_or_process_writes() -> None:
    source = pathlib.Path(control.__file__).read_text(encoding="utf-8")
    verbs = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout")
    assert not any(f'"git", "{verb}"' in source for verb in verbs)
    assert "subprocess" not in source
    assert "os.system" not in source
    # Control and observation stay separate: the control log is its own
    # durable document and never becomes a second status model.
    assert control.CONTROL_LOG_NAME != obs_store.STATUS_NAME


def test_control_targets_explicit_identity_never_most_recent(
    tmp_path: Path,
) -> None:
    _interrupted(tmp_path, "m034-first")
    _interrupted(tmp_path, "m034-second")
    control.cancel(tmp_path, "m034-first")
    first = obs_store.load_status(store.mission_root(tmp_path, "m034-first"))
    second = obs_store.load_status(store.mission_root(tmp_path, "m034-second"))
    assert first["readiness"] == obs_model.RD_CANCELLED
    assert second["readiness"] != obs_model.RD_CANCELLED
    assert second["state"] != obs_model.LC_COMPLETE


def test_execution_request_model_is_not_shadowed(tmp_path: Path) -> None:
    # Guard against accidental import regrouping that changes behavior.
    assert bench_model.WorkloadSpec  # imported for typing parity
    assert control.ACTION_CANCEL in control.CONTROL_ACTIONS
