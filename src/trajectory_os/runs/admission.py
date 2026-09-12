"""V1.87 — bounded admission control (fail-closed, deterministic).

Decides whether new work may start given:

* the current concurrency capacity (strict validation: integer in a fixed
  bounded range; invalid values reject, they never silently degrade),
* the V1.85 registry (live ownership of runs is proven, not assumed),
* the V1.88+ active-record file (orchestration-started jobs),
* the V1.89 queue file (must be well-formed to be considered at all).

Rules (all fail closed):

* a stale ownership state (started, not ended, pid dead/unverifiable)
  rejects the decision — the true state is ambiguous, so no new work;
* unproven live activity rejects the decision;
* capacity exhausted rejects the decision;
* malformed queue evidence rejects the decision;
* on success the decision states the capacity still available.

Admission is a pure decision: it performs no writes and no signals.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from trajectory_os.runs import model

from .registry import RunView


class InvalidCapacityError(ValueError):
    def __init__(self, raw: object) -> None:
        super().__init__(f"invalid capacity: {raw!r}")
        self.raw = raw


def validate_capacity(raw: object) -> int:
    """Strict capacity validation: integer within [MIN_CAPACITY, MAX_CAPACITY]."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        if isinstance(raw, str) and raw.strip().isdigit():
            candidate = int(raw.strip())
        else:
            raise InvalidCapacityError(raw)
    else:
        candidate = raw
    if candidate < model.MIN_CAPACITY or candidate > model.MAX_CAPACITY:
        raise InvalidCapacityError(raw)
    return candidate


@dataclass(frozen=True)
class UnprovenActivity:
    run_id: str
    code: str  # model.OWNERSHIP_* or model.REASON_STATE_AMBIGUOUS-related


@dataclass(frozen=True)
class AdmissionDecision:
    decision: str
    capacity: int
    active_proven: tuple[str, ...]
    active_unproven: tuple[dict[str, str], ...]
    queued: int
    reasons: tuple[str, ...]
    capacity_free: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "tool": model.CLI_NAME,
            "decision": self.decision,
            "capacity": self.capacity,
            "capacity_free": self.capacity_free,
            "active_proven": list(self.active_proven),
            "active_unproven": list(self.active_unproven),
            "queued": self.queued,
            "reasons": list(self.reasons),
        }


def evaluate_admission(
    *,
    capacity_raw: object,
    runs: Iterable[RunView],
    active_records: Iterable[Any] = (),
    queue_entries: Iterable[Any] | None = None,
    queue_malformed: bool = False,
) -> AdmissionDecision:
    """Deterministic admission decision (pure; no I/O, no mutation).

    ``active_records`` are orchestration active records (objects with
    ``job_id`` and ``live_proven`` attributes).  ``queue_entries``/
    ``queue_malformed`` reflect the persisted queue state (V1.89).
    """
    capacity = validate_capacity(capacity_raw)
    proven: list[str] = []
    unproven: list[dict[str, str]] = []

    for view in runs:
        if view.lifecycle == model.LIFECYCLE_ENDED:
            # Ended runs are finished; their (possibly dead) owner must not
            # affect whether new work may start.
            continue
        if view.lifecycle == model.LIFECYCLE_ACTIVE:
            # Registry contract: ACTIVE already implies ownership proven.
            proven.append(view.run_id)
            continue
        # UNKNOWN lifecycle: started, not ended, and not proven.
        # Fail closed — we cannot safely assume it is inactive.
        code = (
            model.REASON_OWNERSHIP_STALE
            if view.ownership.code == model.OWNERSHIP_STALE
            else model.REASON_OWNERSHIP_UNPROVEN
        )
        unproven.append({"id": view.run_id, "code": code})

    for record in active_records:
        if getattr(record, "live_proven", False):
            proven.append(str(record.job_id))
        else:
            unproven.append(
                {
                    "id": str(getattr(record, "job_id", "unknown")),
                    "code": model.REASON_STATE_AMBIGUOUS,
                }
            )

    queued = len(list(queue_entries)) if queue_entries is not None else 0
    # Deterministic, order-independent identity of unproven/ambiguous activity.
    unproven.sort(key=lambda entry: (entry["id"], entry["code"]))

    reasons: list[str] = []
    if queue_malformed:
        reasons.append(model.REASON_QUEUE_MALFORMED)
    for entry in unproven:
        reasons.append(entry["code"])
    if len(proven) >= capacity:
        reasons.append(model.REASON_CAPACITY_EXHAUSTED)
    if queued >= model.MAX_QUEUE_ENTRIES:
        reasons.append(model.REASON_QUEUE_FULL)
    active_ids = {entry["id"] for entry in unproven} | set(proven)
    queued_ids = {
        str(getattr(entry, "job_id", "")) for entry in (queue_entries or ())
    }
    if active_ids & queued_ids:
        reasons.append(model.REASON_DUPLICATE_IDENTITY)
    if reasons:
        reasons = sorted(dict.fromkeys(reasons))  # deterministic, order-independent

    if reasons:
        return AdmissionDecision(
            decision=model.DECISION_REJECTED,
            capacity=capacity,
            active_proven=tuple(sorted(proven)),
            active_unproven=tuple(unproven),
            queued=queued,
            reasons=tuple(reasons),
            capacity_free=0,
        )
    return AdmissionDecision(
        decision=model.DECISION_ALLOWED,
        capacity=capacity,
        active_proven=tuple(sorted(proven)),
        active_unproven=(),
        queued=queued,
        reasons=(model.REASON_ADMITTED,),
        capacity_free=capacity - len(proven),
    )
