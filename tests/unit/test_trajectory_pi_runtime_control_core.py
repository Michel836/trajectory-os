"""Unit tests for the deterministic runtime-control core (V1.73-V1.76).

All logic under test is either pure or confined to a tmp run directory;
tests never target a real long-lived process except short-lived ones that
are always reaped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from trajectory_os import runtime_control as ctl

REPO = Path(__file__).resolve().parents[2]
DEAD_PID = 4_294_967_295  # far above any Linux pid_max: deterministically dead


def _run_dir(tmp_path: Path, meta: str, status_log: str | None = None) -> Path:
    d = tmp_path / "20260101-093000"
    d.mkdir()
    (d / "meta.txt").write_text(meta, encoding="utf-8")
    if status_log is not None:
        (d / "status.log").write_text(status_log, encoding="utf-8")
    return d


# ------------------------------------------------------------- parse_meta


def test_parse_meta_reads_key_value_pairs(tmp_path: Path) -> None:
    meta = _run_dir(tmp_path, """\
# comment
run_id=20260101-093000
pid=123
pi_pid=999
workspace=/tmp/ws
garbage line without equals
=missing-key

pi_pid=1000
""")
    meta = ctl.parse_meta(meta / "meta.txt")
    assert meta["run_id"] == "20260101-093000"
    assert meta["pid"] == "123"
    assert meta["pi_pid"] == "1000"  # last wins
    assert meta["workspace"] == "/tmp/ws"


def test_parse_meta_missing_file_is_empty(tmp_path: Path) -> None:
    assert ctl.parse_meta(tmp_path / "nope" / "meta.txt") == {}


# --------------------------------------------------- target pid resolution


def test_target_pid_prefers_pi_pid() -> None:
    assert ctl.target_pid_for_stop({"pid": "11", "pi_pid": "22"}) == 22


def test_target_pid_falls_back_to_wrapper_pid() -> None:
    assert ctl.target_pid_for_stop({"pid": "11"}) == 11


def test_target_pid_absent_or_invalid_is_none() -> None:
    assert ctl.target_pid_for_stop({}) is None
    assert ctl.target_pid_for_stop({"pid": "", "pi_pid": "not-a-pid"}) is None


# ------------------------------------------------------- identity verdicts


def test_identity_verified_when_all_evidence_matches() -> None:
    ws = "/tmp/ws"
    snap = ctl.ProcSnapshot(pid=1234, alive=True, cwd=ws, start_epoch=1000.0)
    verdict = ctl.verify_pid_identity(
        pid=1234, workspace=ws, run_started_epoch=1000.0, snapshot=snap
    )
    assert verdict.verified
    assert verdict.state == "verified"


def test_identity_dead_process() -> None:
    snap = ctl.ProcSnapshot(pid=1234, alive=False, cwd=None, start_epoch=None)
    verdict = ctl.verify_pid_identity(
        pid=1234, workspace=None, run_started_epoch=None, snapshot=snap
    )
    assert not verdict.verified
    assert verdict.reasons == ("process-dead",)


def test_identity_cwd_mismatch_fails_closed() -> None:
    snap = ctl.ProcSnapshot(pid=1234, alive=True, cwd="/other", start_epoch=1000.0)
    verdict = ctl.verify_pid_identity(
        pid=1234, workspace="/tmp/ws", run_started_epoch=1000.0, snapshot=snap
    )
    assert not verdict.verified
    assert "cwd-mismatch" in verdict.reasons


def test_identity_pid_reuse_signature_fails_closed() -> None:
    # Live process started far after the run's recorded start: pid reuse.
    snap = ctl.ProcSnapshot(pid=1234, alive=True, cwd="/tmp/ws", start_epoch=5000.0)
    verdict = ctl.verify_pid_identity(
        pid=1234, workspace="/tmp/ws", run_started_epoch=1000.0, snapshot=snap,
        start_window_seconds=300.0,
    )
    assert not verdict.verified
    assert "start-time-window-exceeded" in verdict.reasons


def test_identity_own_process_excluded() -> None:
    snap = ctl.ProcSnapshot(pid=1234, alive=True, cwd="/tmp/ws", start_epoch=1000.0)
    verdict = ctl.verify_pid_identity(
        pid=1234, workspace="/tmp/ws", run_started_epoch=1000.0,
        snapshot=snap, own_pids=(1234,),
    )
    assert not verdict.verified
    assert verdict.reasons == ("own-process-excluded",)


def test_identity_invalid_pid() -> None:
    snap = ctl.ProcSnapshot(pid=0, alive=True, cwd="/tmp/ws", start_epoch=1000.0)
    verdict = ctl.verify_pid_identity(
        pid=0, workspace="/tmp/ws", run_started_epoch=1000.0, snapshot=snap
    )
    assert verdict.state == "unknown"
    assert "invalid-pid" in verdict.reasons


def test_identity_unreadable_live_cwd_fails_closed() -> None:
    snap = ctl.ProcSnapshot(pid=1234, alive=True, cwd=None, start_epoch=1000.0)
    verdict = ctl.verify_pid_identity(
        pid=1234, workspace="/tmp/ws", run_started_epoch=1000.0, snapshot=snap
    )
    assert not verdict.verified
    assert "live-cwd-unreadable" in verdict.reasons


def test_identity_missing_expected_workspace_fails_closed() -> None:
    snap = ctl.ProcSnapshot(pid=1234, alive=True, cwd="/tmp/ws", start_epoch=1000.0)
    verdict = ctl.verify_pid_identity(
        pid=1234, workspace=None, run_started_epoch=1000.0, snapshot=snap
    )
    assert not verdict.verified
    assert "expected-workspace-missing" in verdict.reasons


# ------------------------------------------------------------ run state


def test_state_ended_wins_over_heartbeat(tmp_path: Path) -> None:
    d = _run_dir(
        tmp_path,
        "started_at=2026-01-01T09:50:00+00:00\n"
        "ended_at=2026-01-01T09:58:00+00:00\n",
        "[09:50:01] elapsed=00:00:01 | x\n",
    )
    state, reasons = ctl.classify_run_state(d, now_epoch=time.time())
    assert state == "ended"
    assert "ended-at-recorded" in reasons


def test_state_unknown_when_start_missing(tmp_path: Path) -> None:
    d = _run_dir(tmp_path, "model=fake\n", "[09:50:01] elapsed=00:00:01 | x\n")
    state, reasons = ctl.classify_run_state(d)
    assert state == "unknown"
    assert "started-at-missing-or-unparseable" in reasons


def test_state_unknown_without_heartbeat_evidence(tmp_path: Path) -> None:
    d = _run_dir(tmp_path, "started_at=2026-01-01T09:50:00+00:00\n")
    state, reasons = ctl.classify_run_state(d)
    assert state == "unknown"
    assert "no-heartbeat-evidence" in reasons


def test_state_running_with_fresh_heartbeat(tmp_path: Path) -> None:
    now = time.time()
    d = _run_dir(
        tmp_path,
        f"started_at={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 10))}\n",
        "[00:00:02] elapsed=00:00:02 | x\n",
    )
    state, _ = ctl.classify_run_state(d, now_epoch=now)
    assert state == "running"


def test_state_stale_when_heartbeat_aged(tmp_path: Path) -> None:
    now = time.time()
    d = _run_dir(
        tmp_path,
        f"started_at={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 200))}\n",
        "[00:00:01] elapsed=00:00:01 | x\n",
    )
    state, reasons = ctl.classify_run_state(d, now_epoch=now)
    assert state == "stale"
    assert "heartbeat-aged-beyond-stale-window" in reasons


# ------------------------------------------------------------ recovery


def _facts(**kw: object) -> ctl.RecoveryFacts:
    base: dict[str, object] = {
        "transcript_present": True,
        "last_line_valid": True,
        "run_id_consistent": True,
        "pi_exit_code": "0",
        "evidence_source": "status.log",
    }
    base.update(kw)
    return ctl.RecoveryFacts(**base)  # type: ignore[arg-type]


def test_recovery_ready_for_clean_ended_run() -> None:
    ready, reasons = ctl.evaluate_recovery(state="ended", facts=_facts())
    assert ready
    assert "terminal-state" in reasons


def test_recovery_never_ready_for_live_states() -> None:
    for state in ("running", "stale", "unknown"):
        ready, reasons = ctl.evaluate_recovery(state=state, facts=_facts())
        assert not ready
        assert f"state-is-{state}" in reasons


def test_recovery_flags_run_id_inconsistency() -> None:
    ready, reasons = ctl.evaluate_recovery(
        state="ended", facts=_facts(run_id_consistent=False)
    )
    assert not ready
    assert "run-id-inconsistent" in reasons


def test_recovery_flags_missing_transcript() -> None:
    ready, reasons = ctl.evaluate_recovery(
        state="ended",
        facts=_facts(transcript_present=False, last_line_valid=False),
    )
    assert not ready
    assert "transcript-missing" in reasons


def test_recovery_flags_invalid_last_line() -> None:
    ready, reasons = ctl.evaluate_recovery(
        state="ended", facts=_facts(last_line_valid=False)
    )
    assert not ready
    assert "last-evidence-line-invalid" in reasons


def test_recovery_flags_missing_exit_evidence() -> None:
    ready, reasons = ctl.evaluate_recovery(
        state="ended", facts=_facts(pi_exit_code="unknown")
    )
    assert not ready
    assert "exit-evidence-missing" in reasons


def test_recovery_flags_abnormal_exit_with_code() -> None:
    ready, reasons = ctl.evaluate_recovery(
        state="ended", facts=_facts(pi_exit_code="127")
    )
    assert not ready
    assert "abnormal-exit:127" in reasons


def test_collect_recovery_facts_from_run_dir(tmp_path: Path) -> None:
    d = _run_dir(
        tmp_path,
        "run_id=20260101-093000\npi_exit_code=0\n",
        "[09:50:01] elapsed=00:00:01 | x\n[09:58:00] rc=0\n",
    )
    facts = ctl.collect_recovery_facts(d, "20260101-093000")
    assert facts.transcript_present
    assert facts.last_line_valid
    assert facts.run_id_consistent
    assert facts.pi_exit_code == "0"
    assert facts.evidence_source == "status.log"


def test_resume_hint_shape_is_deterministic_and_evidence_based() -> None:
    facts = _facts()
    hint = ctl.make_resume_hint(
        run_id="20260101-093000", workspace="/tmp/ws", facts=facts
    )
    assert hint["schema"] == "resume-hint/1"
    assert hint["run_id"] == "20260101-093000"
    assert hint["workspace"] == "/tmp/ws"
    assert hint["evidence"] == {"source": "status.log", "exit_code": "0"}
    assert hint["suggested_next_action"]
    assert hint["note"]


# ------------------------------------------------------------- build_view


def test_view_ended_clean_run_is_recoverable_but_not_stoppable() -> None:
    view = ctl.build_view(
        run_id="20260101-093000",
        state="ended",
        state_reasons=("ended-at-recorded",),
        meta={"run_id": "20260101-093000", "pid": str(DEAD_PID)},
        workspace="/tmp/ws",
        run_started_epoch=1000.0,
        snapshot=ctl.ProcSnapshot(pid=DEAD_PID, alive=False, cwd=None,
                                  start_epoch=None),
        facts=_facts(),
    )
    assert view.targetable is False
    assert view.stoppable is False
    assert "state-is-ended" in tuple(view.stoppable_reasons)
    assert view.recovery_ready is True
    assert view.resume_hint is not None


def test_view_running_verified_run_is_stoppable() -> None:
    view = ctl.build_view(
        run_id="20260101-093000",
        state="running",
        state_reasons=("heartbeat-fresh",),
        meta={"pid": "1234"},
        workspace="/tmp/ws",
        run_started_epoch=1000.0,
        snapshot=ctl.ProcSnapshot(pid=1234, alive=True, cwd="/tmp/ws",
                                  start_epoch=1000.0),
        facts=_facts(pi_exit_code="unknown"),
    )
    assert view.targetable is True
    assert view.stoppable is True
    assert view.targetable_reasons == ()
    assert view.stoppable_reasons == ()
    assert view.recovery_ready is False
    assert view.resume_hint is None


def test_view_without_recorded_pid_fails_closed() -> None:
    view = ctl.build_view(
        run_id="20260101-093000",
        state="running",
        state_reasons=("heartbeat-fresh",),
        meta={},
        workspace="/tmp/ws",
        run_started_epoch=1000.0,
        snapshot=None,
        facts=_facts(pi_exit_code="unknown"),
    )
    assert view.target_pid is None
    assert view.targetable is False
    assert view.stoppable is False
    assert "no-recorded-pid" in tuple(view.targetable_reasons)


def test_view_to_dict_has_stable_shape(tmp_path: Path) -> None:
    view = ctl.build_view(
        run_id="20260101-093000",
        state="ended",
        state_reasons=("ended-at-recorded",),
        meta={"run_id": "20260101-093000", "pid": str(DEAD_PID)},
        workspace="/tmp/ws",
        run_started_epoch=1000.0,
        snapshot=ctl.ProcSnapshot(pid=DEAD_PID, alive=False, cwd=None,
                                  start_epoch=None),
        facts=_facts(),
    )
    out = view.to_dict()
    assert out["schema"].startswith("trajectory-pi-control/")
    assert set(out) >= {
        "schema", "run_id", "state", "state_reasons", "target_pid", "identity",
        "targetable_safe", "targetable_reasons", "stoppable", "stoppable_reasons",
        "recovery", "resume_hint",
    }
    assert out["identity"]["state"] in ("dead", "unknown")
    assert out["recovery"]["ready"] is True


def test_evaluate_run_is_deterministic_for_ended_dir(tmp_path: Path) -> None:
    d = tmp_path / "20260101-093000"
    d.mkdir()
    (d / "meta.txt").write_text(
        f"run_id=20260101-093000\nstarted_at=2026-01-01T09:50:00+00:00\n"
        f"ended_at=2026-01-01T09:58:00+00:00\npi_exit_code=0\npid={DEAD_PID}\n",
        encoding="utf-8",
    )
    (d / "status.log").write_text(
        "[09:50:01] elapsed=00:00:01 | x\n[09:58:00] rc=0\n", encoding="utf-8"
    )
    first = json.dumps(ctl.evaluate_run(d).to_dict(), sort_keys=True)
    second = json.dumps(ctl.evaluate_run(d).to_dict(), sort_keys=True)
    assert first == second


# ------------------------------------------------------------------- lock


def test_lock_acquire_then_read(tmp_path: Path) -> None:
    outcome = ctl.acquire_control_lock(tmp_path, "stop", holder_pid=1111)
    assert outcome == "acquired"
    info = ctl.read_lock(tmp_path)
    assert info.holder == 1111
    assert info.action == "stop"
    assert not info.corrupt


def test_lock_missing_is_readable_absent(tmp_path: Path) -> None:
    info = ctl.read_lock(tmp_path)
    assert info.holder is None
    assert not info.corrupt


def test_lock_corrupt_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "control.lock").write_text("{not valid json", encoding="utf-8")
    assert ctl.read_lock(tmp_path).corrupt
    assert ctl.acquire_control_lock(tmp_path, "stop", holder_pid=1111) == "corrupt"
    # Corrupt content must never be overwritten or guessed past.
    assert (tmp_path / "control.lock").read_text(encoding="utf-8") == "{not valid json"


def test_lock_dead_holder_is_reclaimed(tmp_path: Path) -> None:
    (tmp_path / "control.lock").write_text(
        json.dumps({"action": "stop", "holder": str(DEAD_PID)}), encoding="utf-8"
    )
    assert ctl.acquire_control_lock(tmp_path, "stop", holder_pid=2222) == "reclaimed"
    assert ctl.read_lock(tmp_path).holder == 2222


def test_lock_live_foreign_holder_conflicts(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        (tmp_path / "control.lock").write_text(
            json.dumps({"action": "stop", "holder": str(proc.pid)}),
            encoding="utf-8",
        )
        assert ctl.acquire_control_lock(tmp_path, "stop", holder_pid=3333) == "conflict"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_lock_released_unlinks(tmp_path: Path) -> None:
    ctl.acquire_control_lock(tmp_path, "stop", holder_pid=4444)
    assert (tmp_path / "control.lock").exists()
    ctl.release_control_lock(tmp_path)
    assert not (tmp_path / "control.lock").exists()
    # Releasing a missing lock must not raise.
    ctl.release_control_lock(tmp_path)


# ------------------------------------------------------------------ events


def test_control_events_append_jsonl(tmp_path: Path) -> None:
    ctl.append_control_event(tmp_path, action="stop", target_pid=1234)
    ctl.append_control_event(tmp_path, event="stop-result", result="STOPPED")
    events = tmp_path / "control-events.jsonl"
    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["action"] == "stop"
    assert first["target_pid"] == 1234
    second = json.loads(lines[1])
    assert second["event"] == "stop-result"
    assert "ts" in first and "ts" in second


def test_send_sigterm_terminates_and_never_targets_dead(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert ctl.send_sigterm(proc.pid) is True
    proc.wait(timeout=10)
    assert ctl.send_sigterm(DEAD_PID) is False


# ------------------------------------------------- source-level safeguards


def test_core_source_has_no_sigkill_or_daemon_primitives() -> None:
    src = (REPO / "src/trajectory_os/runtime_control.py").read_text(
        encoding="utf-8")
    assert "signal.SIGKILL" not in src
    assert "os.kill(pid, 9)" not in src
    assert "subprocess" not in src
    assert "socket" not in src
    assert "urllib" not in src
    assert "http://" not in src and "https://" not in src


def test_lock_actions_are_stop_only() -> None:
    assert ctl.LOCK_ACTIONS == ("stop",)
    with pytest.raises(ValueError):
        ctl.acquire_control_lock(Path("/tmp"), "kill")


def test_constants_are_stable() -> None:
    assert ctl.LOCK_FILENAME == "control.lock"
    assert ctl.EVENTS_FILENAME == "control-events.jsonl"
    assert ctl.ACTIVE_OBSERVED_STATES == ("running", "stale")
    assert ctl.STALE_AFTER_SECONDS > 0


def test_ancestor_chain_is_loop_safe() -> None:
    chain = ctl.ancestor_chain(os.getpid())
    assert os.getppid() in chain


def test_is_pid_alive_for_dead_pid() -> None:
    assert ctl.is_pid_alive(0) is False
    assert ctl.is_pid_alive(-1) is False
    assert ctl.is_pid_alive(DEAD_PID) is False
