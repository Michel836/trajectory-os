"""V1.90 — bounded multi-run orchestration (integration).

Covers the composition of V1.85-V1.89:

* a fully bounded cycle (start -> finish -> observe -> re-queue only within
  attempts budget -> terminal) with no infinite loop;
* bounded retry with strictly increasing attempts and a hard stop at
  max_attempts (never re-queued past budget);
* wrapper-restart recovery (rebuild_state re-proves live ownership, keeps
  queue/closed state intact, and a subsequent cancel succeeds);
* fail-closed: malformed persisted evidence blocks startup with no mutation;
* duplicate identity (active + queued) is rejected deterministically;
* cycle bounds are validated (no unbounded loops via the API);
* the CLI shim works end-to-end (version / list / admit).
"""

from __future__ import annotations

import contextlib
import os
import signal as _sig
import subprocess
import time
from pathlib import Path

import pytest

from trajectory_os.runs import model, orchestration, ownership, store

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM = REPO_ROOT / "scripts" / "trajectory-pi-runs"


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(OSError):
        os.killpg(pgid, _sig.SIGKILL)


def _enqueue(state_root: Path, job_id: str, command: list[str], max_attempts: int = 1) -> None:
    paths = store.state_paths(state_root)
    doc = store.QueueDoc.load(paths["queue"])
    active_ids = frozenset(r.job_id for r in store.load_active_records(paths["active"]))
    doc.enqueue(job_id=job_id, command=command, max_attempts=max_attempts, reserved_ids=active_ids)
    doc.save(paths["queue"])


def _active(state_root: Path) -> list[store.ActiveRecord]:
    return store.load_active_records(store.state_paths(state_root)["active"])


def _closed(state_root: Path) -> list[store.ClosedRecord]:
    return store.load_closed_records(store.state_paths(state_root)["closed"])


def _wait_unproven(state_root: Path, timeout: float = 5.0) -> None:
    """Bounded wait until no active record can be proven live."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        bundle = orchestration.rebuild_state(state_root)
        if not bundle.active or not any(
            bundle.active_proven.get(r.job_id, ownership.OwnershipProof(False, (), 0)).proven
            for r in bundle.active
        ):
            return
        time.sleep(0.05)
    raise AssertionError("job still proven live after bounded timeout")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    state_root = tmp_path / "state"
    runs_root = tmp_path / "runs"
    state_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)
    return {"state": state_root, "runs": runs_root}


class TestBoundedCycle:
    def test_full_cycle_completes_without_loop(self, env: dict[str, Path]) -> None:
        """Three full start->finish->observed->terminal cycles; explicit outcomes."""
        state, runs = env["state"], env["runs"]
        for i in range(3):
            _enqueue(state, f"ok-{i}", ["true"])

        for i in range(3):
            # Bounded cycle: start, wait (bounded), observe, terminalize.
            orchestration.start_one(state, runs, capacity_raw=3, job_id=f"ok-{i}")
            _wait_unproven(state)
            bundle = orchestration.rebuild_state(state)
            reaped = orchestration.reap(bundle, observe={f"ok-{i}": "exit:0"})
            result = next(r for r in reaped["results"] if r["job_id"] == f"ok-{i}")
            assert result["outcome"] == model.TERMINAL_DONE
            assert result["requeued"] is False  # done never requeues
            assert not bundle.active

        # Terminal aggregate state: nothing queued, nothing active, all closed
        closed_by_job = {r.job_id: r for r in _closed(state)}
        assert set(closed_by_job) == {"ok-0", "ok-1", "ok-2"}
        assert all(r.terminal == model.TERMINAL_DONE for r in closed_by_job.values())
        queue = store.QueueDoc.load(store.state_paths(state)["queue"])
        assert queue.entries == []

    def test_orchestrate_terminates_and_drains(self, env: dict[str, Path]) -> None:
        """orchestrate() always terminates and drains a finite queue."""
        state, runs = env["state"], env["runs"]
        for i in range(3):
            _enqueue(state, f"drain-{i}", ["true"])

        # cycles=8 is more than enough; termination is guaranteed by design
        report = orchestration.orchestrate(state, runs, cycles=8, capacity=3)
        assert report["total_started"] == 3

        _wait_unproven(state)
        bundle = orchestration.rebuild_state(state)
        if bundle.active:
            orchestration.reap(bundle)
            bundle = orchestration.rebuild_state(state)
        assert not bundle.active
        queue = store.QueueDoc.load(store.state_paths(state)["queue"])
        assert queue.entries == []
        closed_by_job = {r.job_id: r for r in _closed(state)}
        assert set(closed_by_job) == {"drain-0", "drain-1", "drain-2"}
        # Terminal outcomes are explicit, never invented (no exit evidence
        # captured => crashed, which is an explicit terminal state)
        assert all(
            r.terminal in (model.TERMINAL_DONE, model.TERMINAL_FAILED, model.TERMINAL_CRASHED)
            for r in closed_by_job.values()
        )

    def test_cycle_bound_validation(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        with pytest.raises(ValueError):
            orchestration.orchestrate(state, runs, cycles=0)
        with pytest.raises(ValueError):
            orchestration.orchestrate(state, runs, cycles=2000)
        with pytest.raises(ValueError):
            orchestration.orchestrate(state, runs, cycles=True)


class TestBoundedRetry:
    def test_retries_exhaust_exactly_max_attempts(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "flaky", ["false"], max_attempts=2)

        # Attempt 1
        orchestration.start_one(state, runs, capacity_raw=2)
        _wait_unproven(state)
        reaped = orchestration.reap(orchestration.rebuild_state(state), observe={"flaky": "exit:1"})
        assert reaped["results"][0]["requeued"] is True  # 1 < 2
        assert reaped["results"][0]["outcome"] == model.TERMINAL_FAILED

        # Attempt 2
        orchestration.start_one(state, runs, capacity_raw=2)
        _wait_unproven(state)
        reaped = orchestration.reap(orchestration.rebuild_state(state), observe={"flaky": "exit:1"})
        assert reaped["results"][0]["requeued"] is False  # 2 == 2: budget exhausted

        # Terminal state: nothing queued, nothing active, both attempts closed
        queue = store.QueueDoc.load(store.state_paths(state)["queue"])
        assert queue.entries == []
        assert not _active(state)
        closed = [r for r in _closed(state) if r.job_id == "flaky"]
        assert len(closed) == 2
        assert [r.attempts for r in closed] == [1, 2]  # strictly increasing, bounded
        assert all(r.terminal == model.TERMINAL_FAILED for r in closed)

    def test_done_never_requeues(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "good", ["true"], max_attempts=3)
        orchestration.start_one(state, runs, capacity_raw=2)
        # let it finish (bounded wait), then reap with authoritative evidence
        _wait_unproven(state)
        reaped = orchestration.reap(orchestration.rebuild_state(state), observe={"good": "exit:0"})
        entry = reaped["results"][0]
        assert entry["outcome"] == model.TERMINAL_DONE
        assert entry["requeued"] is False
        queue = store.QueueDoc.load(store.state_paths(state)["queue"])
        assert queue.entries == []


class TestWrapperRestartRecovery:
    def test_rebuild_after_restart_keeps_state_and_can_cancel(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "survivor", ["sleep", "30"])
        started = orchestration.start_one(state, runs, capacity_raw=2)
        pgid = started["pgid"]
        try:
            # Simulate wrapper restart: rebuild state from disk only.
            bundle = orchestration.rebuild_state(state)
            assert len(bundle.active) == 1
            rec = bundle.active[0]
            assert rec.job_id == "survivor"
            # Ownership re-proven from evidence alone
            proof = bundle.active_proven["survivor"]
            assert proof.proven is True

            # A queued job must survive the restart
            _enqueue(state, "second", ["sleep", "30"])
            bundle2 = orchestration.rebuild_state(state)
            assert [e.job_id for e in bundle2.queue.entries] == ["second"]

            # Cancel still works against the rebuilt state
            report = orchestration.cancel_job(bundle2, job_id="survivor", grace_seconds=2.0)
            assert report["cancelled"] is True
            assert not ownership.group_alive(pgid)
        finally:
            _kill_group(pgid)
            for rec in _active(state):
                _kill_group(rec.pgid)


class TestFailClosed:
    def test_malformed_queue_blocks_start_without_mutation(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        queue_path = store.state_paths(state)["queue"]
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(Exception) as exc:
            orchestration.start_one(state, runs, capacity_raw=2)
        # No job was started
        assert not _active(state)
        # The reason surfaces deterministically
        assert "MALFORMED" in str(exc.value).upper()

    def test_duplicate_active_and_queued_rejected(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "dup", ["sleep", "30"])
        orchestration.start_one(state, runs, capacity_raw=2)
        active = _active(state)
        pgid = active[0].pgid
        try:
            # Store-level guard: the normal re-enqueue path rejects the duplicate
            paths = store.state_paths(state)
            with pytest.raises(store.DuplicateIdentityError):
                self._enqueue_raw(state, "dup")

            # Admission-level guard: force a duplicate identity into the
            # persisted queue (bypassing the store guard) and require the
            # deterministic DUPLICATE_IDENTITY rejection at startup, with no
            # mutation of active state.
            queue = store.QueueDoc.load(paths["queue"])
            queue.entries.append(
                store.QueueEntry(
                    seq=queue.seq + 1,
                    job_id="dup",
                    enqueued_at=store.utc_now_iso(),
                    command=["sleep", "30"],
                    max_attempts=1,
                )
            )
            queue.seq += 1
            queue.save(paths["queue"])
            before = [r.job_id for r in _active(state)]
            with pytest.raises(Exception) as exc:
                orchestration.start_one(state, runs, capacity_raw=3, job_id="dup")
            assert "DUPLICATE_IDENTITY" in str(exc.value).upper()
            # No mutation: same active records, and the injected entry stays
            assert [r.job_id for r in _active(state)] == before
        finally:
            _kill_group(pgid)

    @staticmethod
    def _enqueue_raw(state_root: Path, job_id: str) -> None:
        paths = store.state_paths(state_root)
        doc = store.QueueDoc.load(paths["queue"])
        active_ids = frozenset(r.job_id for r in store.load_active_records(paths["active"]))
        doc.enqueue(job_id=job_id, command=["sleep", "30"], max_attempts=1, reserved_ids=active_ids)
        doc.save(paths["queue"])

    def test_stale_active_blocks_admission(self, env: dict[str, Path]) -> None:
        state, runs = env["state"], env["runs"]
        _enqueue(state, "stale1", ["sleep", "30"])
        started = orchestration.start_one(state, runs, capacity_raw=3)
        pgid = started["pgid"]
        try:
            # Kill the job directly so it is dead but still recorded active:
            # its record is now unproven => admission must fail closed.
            _kill_group(pgid)
            time.sleep(0.1)
            _enqueue(state, "stale2", ["sleep", "30"])
            with pytest.raises(Exception) as exc:
                orchestration.start_one(state, runs, capacity_raw=3)
            assert "STATE_AMBIGUOUS" in str(exc.value).upper()
        finally:
            _kill_group(pgid)


class TestCliShim:
    def test_shim_executes_version(self) -> None:
        """The bash wrapper resolves the project env and runs the CLI."""
        if not SHIM.exists():
            pytest.skip("shim not present")
        os.chmod(SHIM, 0o755)
        proc = subprocess.run(
            [str(SHIM), "version"],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0
        assert "trajectory-pi-runs" in (proc.stdout + proc.stderr).lower()
        assert "v1.85" in (proc.stdout + proc.stderr).lower()

    def test_cli_admit_invalid_capacity(
        self, env: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        from trajectory_os.runs import cli

        state, runs = env["state"], env["runs"]
        code = cli.main(
            [
                "admit",
                "--state-root", str(state),
                "--runs-root", str(runs),
                "--capacity", "0",
            ]
        )
        assert code == 3  # EXIT_REJECTED (fail-closed, deterministic code)
        captured = capsys.readouterr()
        assert "CAPACITY_INVALID" in (captured.err + captured.out)

    def test_cli_queue_lifecycle_enlist_start_cancel(
        self, env: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """End-to-end CLI: enqueue -> start -> cancel (real process)."""
        from trajectory_os.runs import cli

        state, runs = env["state"], env["runs"]
        code = cli.main(
            ["enqueue", "--state-root", str(state), "--runs-root", str(runs),
             "clijob", "--", "sleep", "30"],
        )
        assert code == 0
        capsys.readouterr()  # clear the enqueue message before the JSON check
        code = cli.main(
            ["start", "--state-root", str(state), "--runs-root", str(runs),
             "--capacity", "1", "--json"],
        )
        assert code == 0
        import json as _json

        doc = _json.loads(capsys.readouterr().out)
        assert doc["started"] is True

        code = cli.main(
            ["cancel", "--state-root", str(state), "--runs-root", str(runs),
             "--job-id", "clijob", "--grace", "2"],
        )
        assert code == 0
        assert not _active(state)


if __name__ == "__main__":
    raise SystemExit(pytest.main([(__file__), "-v"]))
