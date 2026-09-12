"""V1.90 — bounded multi-run orchestration composing V1.85-V1.89.

Operations (all fail closed, all bounded; no daemon, no remote state):

* ``rebuild_state`` — strict load of persisted queue / active / closed
  state; ownership of every active record is re-proven (multi-factor), so
  orchestration state survives wrapper restarts without trusting stale
  evidence;
* ``reap`` — observe finished jobs: non-live proven jobs are recorded
  (done/failed/crashed explicitly), removed from active, and — only while
  their attempts budget remains — re-queued as a NEW bounded attempt.
  Attempts only ever increase and are capped, so auto-requeue cannot loop;
* ``start_one`` — explicit authorized transition: admission decides first
  (capacity + ownership + queue health) and performs no mutation when it
  rejects; then the head entry is dequeued, launched in an isolated
  process group with per-run artifacts, and the active record persisted
  atomically;
* ``cancel`` — only after ownership proof; graceful group termination;
  unproven records are never signalled;
* ``orchestrate`` — deterministic ``cycles``-bounded loop of
  reap-then-start; it terminates because each pass either starts a job
  (queue strictly shrinks) or stops because admission is impossible.

Stale evidence is never authorized: malformed persisted evidence blocks
admission; runs whose patch identity contradicts their validation/review
evidence are never ``ready`` (registry gates).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.runs import admission, model, ownership, registry, store

# ---------------------------------------------------------------------------
# State bundle
# ---------------------------------------------------------------------------


@dataclass
class StateBundle:
    base: Path
    queue_path: Path
    active_path: Path
    closed_path: Path
    workspaces: Path
    queue: store.QueueDoc
    active: list[store.ActiveRecord]
    closed: list[store.ClosedRecord]
    active_proven: dict[str, ownership.OwnershipProof] = field(default_factory=dict)

    @property
    def active_ids(self) -> set[str]:
        return {record.job_id for record in self.active}


def rebuild_state(state_root: Path) -> StateBundle:
    """Strictly load durable orchestration state and prove live ownership."""
    paths = store.state_paths(state_root)
    queue = store.QueueDoc.load(paths["queue"])
    active = store.load_active_records(paths["active"])
    closed = store.load_closed_records(paths["closed"])
    proven: dict[str, ownership.OwnershipProof] = {}
    for record in active:
        proof = ownership.prove_active_record(record)
        proven[record.job_id] = proof
        record.live_proven = proof.proven
    return StateBundle(
        base=paths["base"],
        queue_path=paths["queue"],
        active_path=paths["active"],
        closed_path=paths["closed"],
        workspaces=paths["workspaces"],
        queue=queue,
        active=active,
        closed=closed,
        active_proven=proven,
    )


def registry_activity(runs_root: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Proven-active registry runs vs unproven ambiguity (fail closed)."""
    runs = registry.load_runs(runs_root)
    proven: list[str] = []
    unproven: list[dict[str, str]] = []
    for view in runs:
        if view.lifecycle == model.LIFECYCLE_ACTIVE:
            proven.append(view.run_id)
        elif (
            view.lifecycle == model.LIFECYCLE_UNKNOWN
            and view.started_at is not None
            and view.ended_at is None
            and view.ownership.code is not None
        ):
            unproven.append(
                {
                    "id": view.run_id,
                    "code": (
                        model.REASON_OWNERSHIP_STALE
                        if view.ownership.code == model.OWNERSHIP_STALE
                        else model.REASON_OWNERSHIP_UNPROVEN
                    ),
                }
            )
    return proven, unproven


def _queue_view(paths: dict[str, Path]) -> tuple[store.QueueDoc, bool, str | None]:
    """Load the queue document; report (doc, malformed, code) fail-closed."""
    try:
        return store.QueueDoc.load(paths["queue"]), False, None
    except store.MalformedStoreError as exc:
        return store.QueueDoc(seq=0, entries=[]), True, exc.code


# ---------------------------------------------------------------------------
# Reap (observe finished jobs; bounded re-queue)
# ---------------------------------------------------------------------------


def reap(state: StateBundle, *, observe: dict[str, str] | None = None) -> dict[str, Any]:
    """Observe finished jobs and advance state (bounded, explicit).

    ``observe`` optionally carries authoritative per-job finishing evidence
    (``"exit:<code>"`` / ``"killed:<sig>"``) for jobs whose exit status the
    caller has captured; without it, non-live jobs record
    ``exit_code=None`` and ``terminal=crashed`` (no invented statuses).
    """
    results: list[dict[str, Any]] = []
    still_active: list[store.ActiveRecord] = []
    for record in state.active:
        proof = state.active_proven.get(record.job_id) or ownership.prove_active_record(record)
        if proof.proven:
            record.live_proven = True
            still_active.append(record)
            results.append(
                {"job_id": record.job_id, "outcome": "still_running", "proof": list(proof.reasons)}
            )
            continue

        evidence = (observe or {}).get(record.job_id, "")
        terminal = model.TERMINAL_CRASHED
        exit_code: int | None = None
        sig: int | None = None
        if evidence.startswith("exit:"):
            try:
                exit_code = int(evidence.split(":", 1)[1])
            except ValueError:
                exit_code = None
            if exit_code is not None:
                terminal = model.TERMINAL_DONE if exit_code == 0 else model.TERMINAL_FAILED
        elif evidence.startswith("killed:"):
            try:
                sig = int(evidence.split(":", 1)[1])
            except ValueError:
                sig = None

        closed_record = store.ClosedRecord(
            job_id=record.job_id,
            seq=record.seq,
            observed_at=store.utc_now_iso(),
            terminal=terminal,
            attempts=record.attempts,
            exit_code=exit_code,
            signal=sig,
        )
        store.append_closed_record(state.closed_path, closed_record)
        state.closed = store.load_closed_records(state.closed_path)

        # Bounded auto-requeue: attempts only ever increase and are capped.
        will_requeue = (
            terminal != model.TERMINAL_DONE and record.attempts < record.max_attempts
        )
        if will_requeue:
            state.queue.enqueue(
                job_id=record.job_id,
                command=list(record.command),
                max_attempts=record.max_attempts,
                attempts=record.attempts,
            )
            state.queue.save(state.queue_path)

        results.append(
            {
                "job_id": record.job_id,
                "outcome": terminal,
                "exit_code": exit_code,
                "signal": sig,
                "requeued": will_requeue,
                "proof_failure": list(proof.reasons),
            }
        )
    state.active = still_active
    store.save_active_records(state.active_path, still_active)
    return {"results": results, "active_remaining": len(still_active)}


# ---------------------------------------------------------------------------
# Start (explicit authorized transition)
# ---------------------------------------------------------------------------


class AdmissionRejectedError(RuntimeError):
    def __init__(self, decision: admission.AdmissionDecision) -> None:
        super().__init__("; ".join(decision.reasons))
        self.decision = decision


def start_one(
    state_root: Path,
    runs_root: Path,
    *,
    job_id: str | None = None,
    capacity_raw: object = model.DEFAULT_CAPACITY,
) -> dict[str, Any]:
    """One explicit start transition (admission first; no mutation on reject)."""
    paths = store.state_paths(state_root)
    queue, queue_malformed, queue_code = _queue_view(paths)
    active = store.load_active_records(paths["active"])
    # Admission needs live ownership facts: prove every active record first
    # (fail closed: unproven activity blocks capacity as ambiguous).
    for record in active:
        proof = ownership.prove_active_record(record)
        record.live_proven = proof.proven

    try:
        decision = admission.evaluate_admission(
            capacity_raw=capacity_raw,
            runs=registry.load_runs(runs_root),
            active_records=active,
            queue_entries=queue.entries,
            queue_malformed=queue_malformed,
        )
    except admission.InvalidCapacityError:
        # Strict validation: invalid capacity rejects without side effects.
        raise
    if decision.decision != model.DECISION_ALLOWED:
        raise AdmissionRejectedError(decision)

    if not queue.entries:
        raise store.QueueEmptyError()

    entry = None
    if job_id is not None:
        for candidate in queue.entries:
            if candidate.job_id == job_id:
                entry = candidate
                break
        if entry is None:
            raise store.JobNotFoundError()
    else:
        entry = queue.head()
        assert entry is not None

    queue.remove(entry.job_id, entry.seq)

    slot = store.slot_name(entry.seq)
    workspace = paths["workspaces"] / slot
    final_query = workspace / "final-query.txt"
    query_content: str | None = None
    if entry.query_file is not None:
        try:
            query_content = Path(entry.query_file).read_text(encoding="utf-8")
        except OSError:
            query_content = None

    pid, pgid, token = ownership.launch_job(
        job_id=entry.job_id,
        slot=slot,
        command=list(entry.command),
        workspace=workspace,
        log_stdout=workspace / "job.stdout.log",
        log_stderr=workspace / "job.stderr.log",
        final_query_file=final_query,
        query_content=query_content,
    )
    record = store.ActiveRecord(
        job_id=entry.job_id,
        slot=slot,
        pgid=pgid,
        pid=pid,
        token=token,
        workspace=str(workspace),
        command=list(entry.command),
        seq=entry.seq,
        max_attempts=entry.max_attempts,
        attempts=entry.attempts + 1,
        started_at=store.utc_now_iso(),
        log_stdout=str(workspace / "job.stdout.log"),
        log_stderr=str(workspace / "job.stderr.log"),
        query_file=str(final_query),
    )
    remaining = [rec for rec in active if rec.job_id != record.job_id]
    remaining.append(record)
    store.save_active_records(paths["active"], remaining)
    queue.save(paths["queue"])
    return {
        "started": True,
        "job_id": record.job_id,
        "pid": pid,
        "pgid": pgid,
        "slot": slot,
        "workspace": str(workspace),
        "started_at": record.started_at,
        "admission": decision.to_dict(),
    }


# ---------------------------------------------------------------------------
# Cancel (explicit, ownership-proven, graceful)
# ---------------------------------------------------------------------------


def cancel_job(
    state: StateBundle, *, job_id: str | None = None, grace_seconds: float = 3.0
) -> dict[str, Any]:
    if not state.active:
        raise store.QueueEmptyError()
    record = None
    if job_id is not None:
        for candidate in state.active:
            if candidate.job_id == job_id:
                record = candidate
                break
        if record is None:
            raise store.QueueEmptyError()
    else:
        record = state.active[0]
    proof = state.active_proven.get(record.job_id) or ownership.prove_active_record(record)
    if not proof.proven:
        # Fail closed: never signal an unproven process group.
        return {
            "cancelled": False,
            "job_id": record.job_id,
            "outcome": "REJECTED_OWNERSHIP",
            "reasons": list(proof.reasons),
        }
    report = ownership.graceful_terminate_group(record.pgid, grace_seconds)
    outcome = str(report.get("outcome"))
    if outcome in (model.TERMINAL_CANCELLED, "group_gone"):
        store.append_closed_record(
            state.closed_path,
            store.ClosedRecord(
                job_id=record.job_id,
                seq=record.seq,
                observed_at=store.utc_now_iso(),
                terminal=model.TERMINAL_CANCELLED,
                attempts=record.attempts,
            ),
        )
        state.closed = store.load_closed_records(state.closed_path)
        state.active = [rec for rec in state.active if rec.job_id != record.job_id]
        store.save_active_records(state.active_path, state.active)
        return {
            "cancelled": True,
            "job_id": record.job_id,
            "outcome": model.TERMINAL_CANCELLED,
            "proof": list(proof.reasons),
            **report,
        }
    return {
        "cancelled": False,
        "job_id": record.job_id,
        "outcome": outcome,
        "proof": list(proof.reasons),
        **report,
    }


# ---------------------------------------------------------------------------
# Orchestrate (deterministic bounded loop)
# ---------------------------------------------------------------------------


def orchestrate(
    state_root: Path,
    runs_root: Path,
    *,
    cycles: int = 1,
    capacity: int = model.DEFAULT_CAPACITY,
) -> dict[str, Any]:
    """Bounded orchestration: ``cycles`` passes of reap -> fill capacity.

    Always terminates: each pass either starts jobs (the queue strictly
    shrinks) or stops because admission is impossible.
    """
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 1:
        raise ValueError("INVALID_CYCLES: cycles must be an integer >= 1")
    if cycles > 1024:
        raise ValueError("INVALID_CYCLES: cycles exceed bounded limit 1024")
    passes: list[dict[str, Any]] = []
    for _ in range(cycles):
        state = rebuild_state(state_root)
        pass_report: dict[str, Any] = {"reap": reap(state, observe=None)}
        started: list[dict[str, Any]] = []
        while True:
            state = rebuild_state(state_root)
            proven = [
                rec
                for rec in state.active
                if state.active_proven.get(
                    rec.job_id, ownership.OwnershipProof(False, (), 0)
                ).proven
            ]
            if not state.queue.entries or len(proven) >= capacity:
                break
            try:
                started.append(start_one(state_root, runs_root, capacity_raw=capacity))
            except (store.QueueEmptyError, store.MalformedStoreError, AdmissionRejectedError):
                break
        pass_report["started"] = started
        passes.append(pass_report)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "passes": passes,
        "total_started": sum(len(p.get("started", [])) for p in passes),
    }
