"""V2.00 — bounded autonomous supervisor (local, deterministic, fail closed).

A session of at most ``max_cycles`` *bounded* supervisor cycles.  Each cycle
performs one atomic unit of work under the canonical state lock:

    1. strict state rebuild — malformed durable state fails closed
       (``store.MalformedStateError``) without any mutation;
    2. *reap* — terminate only provably-dead work using supplied liveness
       evidence, or terminalize with UNKNOWN when evidence is missing
       (bounded mutation window);
    3. eligibility composition and, if exactly one work item is eligible,
       its admission check + launch (``orchestration.start_one`` re-validates
       the authoritative persisted state), evaluated against the same
       in-memory bundle that was read under the lock;
    4. retry-backoff advance — decrement queued entries' backoff counters
       within the same bounded mutation window.

The whole cycle runs inside one state lock acquisition, so the eligibility
decision is never based on stale state and the eligibility→admission→launch
sequence is atomic with respect to any other supervisor or CLI actor.  Any
lock contention, malformed state, ambiguous record, unproven live work, or
resource uncertainty stops the supervisor — it never guesses, never
overcommits, and never force-closes work.

Deterministic session termination:
* ``WORK_SETTLED`` — no active and no queued work (quiescence);
* ``CYCLE_BOUND_EXHAUSTED`` — reached ``max_cycles`` while work remains;
* ``STATE_MALFORMED`` — malformed durable state (reused typed errors);
* ``ADMISSION_BLOCKED`` — admission rejected the launch or state was
  ambiguous / resource usage unproven (fail closed);
* ``NO_ELIGIBLE_WORK`` — work exists but none is eligible (deps/backoff);
* ``LOCK_CONTENTION`` — could not acquire the state lock within budget.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from trajectory_os.runs import admission, execution, model, observability, resources, store
from trajectory_os.runs import orchestration as orch
from trajectory_os.runs.locking import StateLock

DEFAULT_CYCLES = 4

# Hard, non-negotiable bound on launches per supervisor session.  A session
# is operator-bounded; this ceiling is a global safety cap (not a guess of
# normal scale) that no configuration may exceed.
MAX_LAUNCHES_PER_SESSION = 512

# Per-cycle budget for the state lock (fail closed on contention).
LOCK_TIMEOUT_SECONDS = 5.0

# Read-only liveness evidence callback, active records -> {job_id: "state"}.
ObserveFn = Callable[[list[store.ActiveRecord]], Mapping[str, str] | None]


class SupervisorConfigError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SupervisorConfig:
    cycles: int = DEFAULT_CYCLES
    capacity: int = 1
    observe: bool = False
    resource_evidence: object | None = None
    lock_timeout: float = LOCK_TIMEOUT_SECONDS
    launches_bound: int = MAX_LAUNCHES_PER_SESSION  # per-session ceiling default

    def validate(self) -> SupervisorConfig:
        if (
            isinstance(self.cycles, bool)
            or not isinstance(self.cycles, int)
            or self.cycles < 1
            or self.cycles > model.MAX_SUPERVISOR_CYCLES
        ):
            raise SupervisorConfigError(
                "CYCLES_INVALID",
                f"must be 1..{model.MAX_SUPERVISOR_CYCLES} (integer)",
            )
        if (
            isinstance(self.launches_bound, bool)
            or not isinstance(self.launches_bound, int)
            or self.launches_bound < 1
            or self.launches_bound > MAX_LAUNCHES_PER_SESSION
        ):
            raise SupervisorConfigError(
                "LAUNCHES_BOUND_INVALID",
                f"must be 1..{MAX_LAUNCHES_PER_SESSION} (integer); "
                f"the per-session hard cap is {MAX_LAUNCHES_PER_SESSION}",
            )
        admission.validate_capacity(self.capacity)
        if self.resource_evidence is not None:
            resources.policy_from_evidence(self.resource_evidence)
        return self


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "Z"
    )


def _atomic_write_json(path: Any, doc: Mapping[str, Any]) -> None:
    from pathlib import Path

    path = Path(path)
    data = json.dumps(dict(doc), sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _snapshot_counts(snap: Mapping[str, object]) -> dict[str, int]:
    """Narrow the (loosely typed) snapshot counts mapping to int counts."""
    raw = snap.get("counts")
    out: dict[str, int] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, int):
                out[str(key)] = value
    return out


def _resource_gate(
    capacity: resources.ResourceCapacity | None, active: list[store.ActiveRecord]
) -> str | None:
    """Fail-closed resource gate for new launches.

    Active records may not persist their own declared requirement, so their
    resource usage is *unproven*; with a capacity policy in force we cannot
    rule out overcommit, so the launch is withheld (fail closed).
    """
    if capacity is None or not active:
        return None
    return model.REASON_RESOURCE_UNKNOWN + ":active_usage_unproven"


def run_session(
    state_root: Any,
    runs_root: Any,
    *,
    config: SupervisorConfig | None = None,
    observe_fn: ObserveFn | None = None,
) -> dict[str, Any]:
    cfg = (config or SupervisorConfig()).validate()
    started: list[dict[str, Any]] = []
    reaped_total = 0
    discharged_total = 0
    codes: list[str] = []
    stop_reason: str | None = None
    cycles_executed = 0
    session_started_at = _now_iso()
    final_counts: Mapping[str, int] = {}
    final_started_by: dict[str, tuple[str, ...]] = {}
    any_unproven_final = False

    paths = store.state_paths(state_root)

    for _ in range(cfg.cycles):
        cycles_executed += 1
        lock = StateLock(paths["base"], timeout_seconds=cfg.lock_timeout)
        with lock as acquired:
            if not acquired:
                codes.append("STATE_LOCK_TIMEOUT")
                stop_reason = model.STOP_LOCK_CONTENTION
                break

            # 1) Strict load: fail closed on malformed durable state.
            try:
                state = orch.rebuild_state(state_root)
            except store.MalformedStateError as exc:
                codes.append(str(exc).upper())
                stop_reason = model.STOP_STATE_MALFORMED
                break

            # 2) Bounded mutation window: evidence-based reap (no guessing).
            evidence = observe_fn(list(state.active)) if observe_fn is not None else None
            observe = dict(evidence) if evidence is not None else None
            try:
                reap_report = orch.reap(state, observe=observe)
            except orch.StateConflictError as exc:
                code = str(getattr(exc, "code", "STATE_CONFLICT"))
                codes.append(code.upper())
                stop_reason = model.STOP_ADMISSION_BLOCKED
                break
            except store.MalformedStateError as exc:
                codes.append(str(exc).upper())
                stop_reason = model.STOP_STATE_MALFORMED
                break
            reaped_this = sum(
                1
                for item in reap_report.get("results", [])
                if item.get("outcome") != "still_running"
            )
            reaped_total += reaped_this

            # 3) Eligibility composition against the lock-authoritative bundle.
            snap = observability.snapshot(state)
            final_counts = _snapshot_counts(snap)
            final_started_by = orch.started_by_jobs(state)
            active = list(state.active)
            proven_live = [record for record in active if record.live_proven]
            queue = sorted(state.queue.entries, key=lambda entry: entry.seq)
            capacity_left = cfg.capacity - len(proven_live)
            any_unproven = len(proven_live) != len(active)
            # Authoritative identity set for the duplicate-identity rule:
            # jobs already active or already closed.  Explicit membership (not
            # derived from started-by keys), so the rule holds regardless of
            # the canonical closure's key shape.
            known_ids = state.active_ids | {record.job_id for record in state.closed}

            started_this_cycle = False
            if capacity_left > 0 and queue and not any_unproven:
                capacity = (
                    resources.policy_from_evidence(cfg.resource_evidence)
                    if cfg.resource_evidence is not None
                    else None
                )
                gate = _resource_gate(capacity, active)

                # Exactly ONE stop may be chosen per cycle.  The selection is
                # made once, against explicit ordered conditions (resource
                # gate -> session launch bound -> duplicate identity ->
                # eligibility); each branch assigns a single (code, reason)
                # pair, and the session halts immediately after that
                # assignment — no later branch can overwrite or conflict with
                # the chosen stop.
                halt: tuple[str, str] | None = None
                eligible: store.QueueEntry | None = None
                if gate is not None:
                    halt = (gate, model.STOP_ADMISSION_BLOCKED)
                elif len(started) >= cfg.launches_bound:
                    halt = (
                        "SESSION_LAUNCH_BOUND_EXHAUSTED",
                        model.STOP_CYCLE_BOUND_EXHAUSTED,
                    )
                else:
                    graph: dict[str, tuple[str, ...]] = {}
                    for entry in queue:
                        if entry.spec is not None and entry.spec.depends_on:
                            graph[entry.job_id] = tuple(entry.spec.depends_on)
                    context = execution.CandidateContext(
                        terminal_map=dict(state.terminal_map),
                        dependency_graph=graph,
                        capacity=capacity,
                        usage=None,
                        active_facts=tuple(
                            execution.ActiveFacts(
                                job_id=record.job_id,
                                execution_class=record.execution_class,
                                source_checkout=None,
                            )
                            for record in active
                        ),
                    )
                    for entry in queue:
                        if entry.retry_wait > 0:
                            continue  # backoff pending (advances each cycle)
                        if entry.job_id in known_ids:
                            halt = (
                                f"DUPLICATE_IDENTITY:{entry.job_id}",
                                model.STOP_ADMISSION_BLOCKED,
                            )
                            break
                        if entry.spec is None:
                            continue  # legacy entry: no canonical spec to evaluate
                        if execution.evaluate_candidate(entry.spec, context).eligible:
                            eligible = entry
                            break

                if halt is not None:
                    codes.append(halt[0])
                    state.queue.save(state.queue_path)  # persist window before break
                    stop_reason = halt[1]
                    break

                if eligible is not None:
                    launch_code: str | None = None
                    launch_result: dict[str, Any] | None = None
                    try:
                        launch_result = orch.start_one(
                            state_root,
                            runs_root,
                            capacity_raw=cfg.capacity,
                            job_id=eligible.job_id,
                        )
                    except orch.AdmissionRejectedError as exc:
                        reasons = exc.decision.reasons if exc.decision else ()
                        launch_code = ":".join(reasons)[:80] or "ADMISSION_REJECTED"
                    except Exception as exc:  # noqa: BLE001  # fail closed
                        launch_code = type(exc).__name__.upper()
                    if launch_code is not None:
                        codes.append(launch_code)
                        state.queue.save(state.queue_path)  # persist window before break
                        stop_reason = model.STOP_ADMISSION_BLOCKED
                        break
                    assert launch_result is not None  # set exactly when no exception
                    started.append(
                        {
                            "job_id": launch_result["job_id"],
                            "pid": launch_result["pid"],
                            "pgid": launch_result["pgid"],
                            "slot": launch_result["slot"],
                            "started_at": launch_result["started_at"],
                        }
                    )
                    started_this_cycle = True

            # 4) Retry-backoff advance within the same bounded mutation window.
            if stop_reason is None:
                reserved = {record.job_id for record in active}
                changed = 0
                for entry in list(state.queue.entries):
                    if entry.retry_wait > 0 and entry.job_id not in reserved:
                        state.queue.update_retry_wait(entry.job_id, entry.retry_wait - 1)
                        changed += 1
                if changed:
                    state.queue.save(state.queue_path)
                    discharged_total += changed

            # Session termination logic (deterministic, conservative ordering).
            if stop_reason is None:
                if started_this_cycle:
                    continue
                if not active and not queue:
                    stop_reason = model.STOP_WORK_SETTLED
                    break
                if not active and queue and not any_unproven:
                    # Queued but not eligible this cycle (deps/backoff).
                    stop_reason = model.STOP_NO_ELIGIBLE_WORK
                    break
                if not active and queue and any_unproven:
                    # Defensively: unproven live work should have been reaped.
                    codes.append("STATE_AMBIGUOUS")
                    stop_reason = model.STOP_ADMISSION_BLOCKED
                    break
                # else: keep advancing (retry waits discharge per cycle).

    # Post-session accounting (evidence only; strict load, no state changes).
    try:
        state_after = orch.rebuild_state(state_root)
        final_counts = _snapshot_counts(observability.snapshot(state_after))
        final_started_by = orch.started_by_jobs(state_after)
        any_unproven_final = any(not record.live_proven for record in state_after.active)
    except store.MalformedStateError:
        any_unproven_final = True  # fail closed: treat residual as unproven

    summary = {
        "schema_version": model.OPS_SCHEMA_VERSION,
        "tool": model.CLI_NAME,
        "started_at": session_started_at,
        "ended_at": _now_iso(),
        "cycles": {
            "configured": cfg.cycles,
            "executed": cycles_executed,
            "bounded_by": model.MAX_SUPERVISOR_CYCLES,
        },
        "stop": {
            "reason": stop_reason or model.STOP_CYCLE_BOUND_EXHAUSTED,
            "codes": list(dict.fromkeys(codes))[:64],
        },
        "totals": {
            "reaped": reaped_total,
            "launched": len(started),
            "retry_discharged": discharged_total,
        },
        "started_by_job": {job: list(deps) for job, deps in sorted(final_started_by.items())},
        "started": started[: cfg.launches_bound],
        "final_counts": dict(final_counts),
        "any_unproven_residual": bool(any_unproven_final),
    }
    _atomic_write_json(paths["base"] / model.SUPERVISOR_SUMMARY_FILE, summary)
    return summary


def run(
    state_root: Any,
    runs_root: Any,
    *,
    cycles: int = DEFAULT_CYCLES,
    capacity: int = 1,
    observe: bool = False,
    resource_evidence: object | None = None,
    launch_bound: int | None = None,
) -> dict[str, Any]:
    """CLI/operator-level bounded supervisor run (typed config, strict)."""
    cfg = SupervisorConfig(
        cycles=cycles,
        capacity=capacity,
        observe=observe,
        resource_evidence=resource_evidence,
        launches_bound=(launch_bound if launch_bound is not None else MAX_LAUNCHES_PER_SESSION),
    )
    return run_session(state_root, runs_root, config=cfg.validate())
