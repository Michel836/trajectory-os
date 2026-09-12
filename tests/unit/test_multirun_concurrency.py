"""V1.88 — controlled concurrency with per-run isolation (integration).

Covers: per-run isolation (distinct process groups, workspaces, final-query
files, log files, ownership tokens); PID-only ownership rejection (liveness
alone never proves ownership); capacity enforcement blocks a second start
without mutating state; graceful SIGTERM termination of a verified group;
cancel_pending is reported, never escalated to SIGKILL; cancel of an
unproven record is rejected and the group is never signalled.

All launched processes are cleaned up in a fixture to avoid leaks.
"""

from __future__ import annotations

import contextlib
import os
import signal as _sig
import time
from pathlib import Path

import pytest

from trajectory_os.runs import admission, model, orchestration, ownership, store

_SLEEP = "30"
_IGNORE_TERM = (
    "python3 -c "
    "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "time.sleep(60)'"
)


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(OSError):
        os.killpg(pgid, _sig.SIGKILL)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    state_root = tmp_path / "state"
    runs_root = tmp_path / "runs"
    state_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)
    return {"state": state_root, "runs": runs_root}


def _enqueue(state_root: Path, job_id: str, command: list[str], max_attempts: int = 1) -> None:
    paths = store.state_paths(state_root)
    doc = store.QueueDoc.load(paths["queue"])
    active_ids = frozenset(r.job_id for r in store.load_active_records(paths["active"]))
    doc.enqueue(job_id=job_id, command=command, max_attempts=max_attempts,
                reserved_ids=active_ids)
    doc.save(paths["queue"])


def _active(state_root: Path) -> list[store.ActiveRecord]:
    return store.load_active_records(store.state_paths(state_root)["active"])


def _cleanup_state(state_root: Path) -> None:
    for record in _active(state_root):
        _kill_group(record.pgid)


def _start(state_root: Path, runs_root: Path, capacity_raw: object = 2) -> dict:
    return orchestration.start_one(state_root, runs_root, capacity_raw=capacity_raw)


class TestPerRunIsolation:
    def test_two_runs_are_fully_isolated(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "iso-a", ["sleep", _SLEEP])
        _enqueue(state, "iso-b", ["sleep", _SLEEP])
        try:
            a = _start(state, runs)
            b = _start(state, runs)
            # Distinct process groups and pids
            assert a["pid"] != b["pid"]
            assert a["pgid"] != b["pgid"]
            # Distinct workspaces
            ws_a, ws_b = Path(a["workspace"]), Path(b["workspace"])
            assert ws_a != ws_b
            # Distinct per-run final-query files
            fq_a = ws_a / "final-query.txt"
            fq_b = ws_b / "final-query.txt"
            assert fq_a != fq_b
            # Distinct log files, both present
            recs = {r.job_id: r for r in _active(state)}
            ra, rb = recs["iso-a"], recs["iso-b"]
            assert ra.log_stdout != rb.log_stdout
            assert ra.log_stderr != rb.log_stderr
            assert Path(ra.log_stdout).exists() and Path(rb.log_stdout).exists()
            # Distinct ownership tokens (never shared)
            assert ra.token != rb.token
        finally:
            _cleanup_state(state)

    def test_pid_only_never_proves(self, env: dict[str, Path]) -> None:
        """Liveness + pid alone must not prove ownership (token/cwd/pgid required)."""
        state, runs = env["state"], env["runs"]
        _enqueue(state, "pidonly", ["sleep", _SLEEP])
        try:
            started = _start(state, runs)
            real = next(r for r in _active(state) if r.job_id == "pidonly")
            assert real.pid == started["pid"]
            proof_ok = ownership.prove_active_record(real)
            assert proof_ok.proven is True

            # Same live pid, WRONG token => unproven (PID-only is rejected)
            wrong_token = store.ActiveRecord(
                **{**real.__dict__, "token": "f" * len(real.token)}
            )
            proof_bad = ownership.prove_active_record(wrong_token)
            assert proof_bad.proven is False
            assert any("TOKEN" in r for r in proof_bad.reasons)

            # Same live pid, WRONG workspace => unproven
            wrong_ws = store.ActiveRecord(
                **{**real.__dict__, "workspace": str(env["state"] / "elsewhere")}
            )
            proof_ws = ownership.prove_active_record(wrong_ws)
            assert proof_ws.proven is False
        finally:
            _cleanup_state(state)


class TestCapacity:
    def test_second_start_blocked_without_mutation(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "cap-a", ["sleep", _SLEEP])
        _enqueue(state, "cap-b", ["sleep", _SLEEP])
        try:
            # capacity 1: first start admitted (consumes head), second rejected
            _start(state, runs, capacity_raw=1)
            seqs_after_first = [
                e.seq for e in store.QueueDoc.load(store.state_paths(state)["queue"]).entries
            ]
            with pytest.raises(orchestration.AdmissionRejectedError) as exc:
                _start(state, runs, capacity_raw=1)
            assert model.REASON_CAPACITY_EXHAUSTED in exc.value.decision.reasons
            # The rejected second start must not mutate queue or active state
            doc_after = store.QueueDoc.load(store.state_paths(state)["queue"])
            assert [e.seq for e in doc_after.entries] == seqs_after_first
            assert active_count(state) == 1
        finally:
            _cleanup_state(state)

    def test_invalid_capacity_rejected(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "inv", ["sleep", _SLEEP])
        with pytest.raises(admission.InvalidCapacityError):
            _start(state, runs, capacity_raw=0)
        # rejected, and nothing started
        assert active_count(state) == 0


def active_count(state: Path) -> int:
    return len(_active(state))


class TestGracefulTermination:
    def test_cancel_sigterm_of_verified_group(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "cancel1", ["sleep", _SLEEP])
        started = _start(state, runs)
        pgid = started["pgid"]
        assert ownership.group_alive(pgid)
        try:
            bundle = orchestration.rebuild_state(state)
            report = orchestration.cancel_job(bundle, job_id="cancel1", grace_seconds=2.0)
            assert report["cancelled"] is True
            assert report["outcome"] == model.TERMINAL_CANCELLED
            # Group is gone and was removed from active
            assert not ownership.group_alive(pgid)
            assert active_count(state) == 0
            # A closed record with a bounded terminal outcome was written
            closed = store.load_closed_records(store.state_paths(state)["closed"])
            assert any(r.job_id == "cancel1" for r in closed)
        finally:
            _kill_group(pgid)

    def test_cancel_pending_reported_not_escalated(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "pending1", ["bash", "-c", _IGNORE_TERM])
        started = _start(state, runs)
        pgid = started["pgid"]
        try:
            # Let the process install its SIGTERM trap.
            time.sleep(0.5)
            bundle = orchestration.rebuild_state(state)
            report = orchestration.cancel_job(bundle, job_id="pending1", grace_seconds=0.5)
            # Product must NOT escalate to SIGKILL: it reports cancel_pending
            assert model.TERMINAL_CANCEL_PENDING in str(report.get("outcome"))
            assert report["cancelled"] is False
            # Still alive: the product never force-killed it
            assert ownership.group_alive(pgid)
        finally:
            # Test-only cleanup (production code never does this).
            _kill_group(pgid)

    def test_cancel_unproven_rejected_never_signalled(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "unprov", ["sleep", _SLEEP])
        started = _start(state, runs)
        pgid = started["pgid"]
        try:
            # Corrupt the persisted token so ownership can no longer be proven.
            active_path = store.state_paths(state)["active"]
            records = _active(state)
            bad = store.ActiveRecord(**{**records[0].__dict__, "token": "0" * 32})
            store.save_active_records(active_path, [bad])

            bundle = orchestration.rebuild_state(state)
            report = orchestration.cancel_job(bundle, job_id="unprov", grace_seconds=1.0)
            assert report["cancelled"] is False
            assert "REJECTED_OWNERSHIP" in str(report.get("outcome"))
            # The group was never signalled — still fully alive
            assert ownership.group_alive(pgid)
        finally:
            _kill_group(pgid)
