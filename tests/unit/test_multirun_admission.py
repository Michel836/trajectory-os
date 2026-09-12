"""Bounded admission control (V1.87).

Covers: capacity validation bounds (strict; fail closed); queue full /
duplicate queued job => rejected; unproven or stale ownership => rejected
(never admitted with stale evidence); capacity exhausted by proven active
runs/records => rejected; deterministic reason ordering and stable
machine-readable codes; CLI exit codes for rejected admission.
"""

from __future__ import annotations

import pytest

from trajectory_os.runs import admission, model, registry, store


def _run_view(
    run_id: str,
    *,
    lifecycle: str = model.LIFECYCLE_ENDED,
    started_at: str | None = "t0",
    ended_at: str | None = "t1",
    ownership_code: str | None = None,
    ownership_pid: int | None = None,
) -> registry.RunView:
    ownership = registry.OwnershipEvidence(
        recorded_pid=ownership_pid,
        recorded_workspace="/tmp/ws" if ownership_code else None,
        live=ownership_pid is not None,
        code=ownership_code,
    )
    return registry.RunView(
        run_id=run_id,
        run_dir=run_id,
        lifecycle=lifecycle,
        state="in_progress" if lifecycle == model.LIFECYCLE_ACTIVE else "incomplete",
        started_at=started_at,
        ended_at=ended_at,
        ownership=ownership,
    )


def _active_record(job_id: str = "job-1", pid: int = 99999) -> store.ActiveRecord:
    return store.ActiveRecord(
        job_id=job_id,
        slot="s1",
        pgid=pid,
        pid=pid,
        token="tok",
        workspace="/tmp/ws",
        command=["true"],
        seq=1,
        max_attempts=1,
        attempts=1,
        started_at="t0",
        log_stdout="/tmp/ws/stdout.log",
        log_stderr="/tmp/ws/stderr.log",
    )


class TestCapacity:
    def test_valid_capacity(self) -> None:
        assert admission.validate_capacity(1) == 1
        assert admission.validate_capacity(16) == 16
        assert admission.validate_capacity("4") == 4

    def test_invalid_capacity_rejected(self) -> None:
        for bad in (0, -1, 17, 1.5, "two", None, True, b"1"):
            try:
                admission.validate_capacity(bad)  # type: ignore[arg-type]
            except admission.InvalidCapacityError:
                pass
            else:  # pragma: no cover - defensive
                raise AssertionError(f"expected reject for {bad!r}")


class TestAdmission:
    def test_empty_admission_allowed(self) -> None:
        decision = admission.evaluate_admission(
            capacity_raw=2, runs=[], active_records=[], queue_entries=[], queue_malformed=False
        )
        assert decision.decision == model.DECISION_ALLOWED
        assert decision.capacity == 2
        assert decision.queued == 0

    def test_capacity_exhausted_by_proven_active_runs(self) -> None:
        # registry contract: ACTIVE lifecycle => ownership proven
        active_run = _run_view(
            "run-live",
            lifecycle=model.LIFECYCLE_ACTIVE,
            started_at="t0",
            ended_at=None,
            ownership_code=model.OWNERSHIP_PROVEN,
            ownership_pid=42,
        )
        decision = admission.evaluate_admission(
            capacity_raw=1, runs=[active_run], active_records=[], queue_entries=[],
            queue_malformed=False,
        )
        assert decision.decision == model.DECISION_REJECTED
        assert model.REASON_CAPACITY_EXHAUSTED in decision.reasons
        assert "run-live" in decision.active_proven

    def test_stale_or_unproven_runs_reject(self) -> None:
        # registry contract: stale/unproven => lifecycle UNKNOWN (never ACTIVE)
        stale = _run_view(
            "run-stale",
            lifecycle=model.LIFECYCLE_UNKNOWN,
            started_at="t0",
            ended_at=None,
            ownership_code=model.OWNERSHIP_STALE,
            ownership_pid=1,
        )
        unproven = _run_view(
            "run-unproven",
            lifecycle=model.LIFECYCLE_UNKNOWN,
            started_at="t0",
            ended_at=None,
            ownership_code=model.OWNERSHIP_UNPROVEN,
        )
        decision = admission.evaluate_admission(
            capacity_raw=4, runs=[stale, unproven], active_records=[],
            queue_entries=[], queue_malformed=False,
        )
        assert decision.decision == model.DECISION_REJECTED
        assert model.REASON_OWNERSHIP_STALE in decision.reasons
        assert model.REASON_OWNERSHIP_UNPROVEN in decision.reasons

    def test_unproven_active_record_rejects(self) -> None:
        decision = admission.evaluate_admission(
            capacity_raw=1, runs=[], active_records=[_active_record()],
            queue_entries=[], queue_malformed=False,
        )
        assert decision.decision == model.DECISION_REJECTED
        assert model.REASON_STATE_AMBIGUOUS in decision.reasons

    def test_queue_full_rejects(self) -> None:
        doc = store.QueueDoc(seq=0, entries=[])
        for i in range(model.MAX_QUEUE_ENTRIES):
            doc.enqueue(job_id=f"j{i}", command=["true"], max_attempts=1)
        decision = admission.evaluate_admission(
            capacity_raw=1, runs=[], active_records=[], queue_entries=doc.entries,
            queue_malformed=False,
        )
        assert decision.decision == model.DECISION_REJECTED
        assert decision.queued == model.MAX_QUEUE_ENTRIES
        assert model.REASON_QUEUE_FULL in decision.reasons

    def test_malformed_queue_rejects(self) -> None:
        decision = admission.evaluate_admission(
            capacity_raw=1, runs=[], active_records=[], queue_entries=[],
            queue_malformed=True,
        )
        assert decision.decision == model.DECISION_REJECTED
        assert model.REASON_QUEUE_MALFORMED in decision.reasons

    def test_duplicate_queued_identity_rejects(self) -> None:
        """A job already active cannot also sit in the queue under the same id."""
        queue_entry = store.QueueEntry(
            seq=1, job_id="dup", enqueued_at="t0", command=["true"],
            max_attempts=1, query_file=None, attempts=0,
        )
        decision = admission.evaluate_admission(
            capacity_raw=2, runs=[], active_records=[_active_record("dup")],
            queue_entries=[queue_entry], queue_malformed=False,
        )
        assert decision.decision == model.DECISION_REJECTED
        assert model.REASON_DUPLICATE_IDENTITY in decision.reasons

    def test_deterministic_reason_order(self) -> None:
        # Registry contract: stale/unproven => lifecycle UNKNOWN (never ACTIVE).
        stale = _run_view(
            "a-stale", started_at="t0", ended_at=None,
            lifecycle=model.LIFECYCLE_UNKNOWN, ownership_code=model.OWNERSHIP_STALE,
        )
        unproven = _run_view(
            "b-unproven", started_at="t0", ended_at=None,
            lifecycle=model.LIFECYCLE_UNKNOWN,
            ownership_code=model.OWNERSHIP_UNPROVEN,
        )
        d1 = admission.evaluate_admission(
            capacity_raw=9, runs=[stale, unproven], active_records=[],
            queue_entries=[], queue_malformed=True,
        )
        d2 = admission.evaluate_admission(
            capacity_raw=9, runs=[unproven, stale], active_records=[],
            queue_entries=[], queue_malformed=True,
        )
        assert d1.reasons == d2.reasons  # independent of input order
        assert d1.to_dict() == d2.to_dict()


class TestAdmissionCli:
    def test_admit_cli_rejects_when_capacity_invalid(
        self, tmp_path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from trajectory_os.runs import cli

        code = cli.main(
            [
                "admit",
                "--state-root", str(tmp_path / "state"),
                "--runs-root", str(tmp_path / "runs"),
                "--capacity", "0",
            ]
        )
        assert code == cli.EXIT_REJECTED
        err = capsys.readouterr().err
        assert model.REASON_CAPACITY_INVALID in err
