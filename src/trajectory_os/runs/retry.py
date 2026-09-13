"""V1.95 — Bounded, fair re-attempt queue selection (deterministic).

Rules (all fail closed; no unbounded waiting; FIFO preserved within one
queue; no starvation under bounded retry budgets):

* a job requeued with a backoff counter (``retry_wait``) is **blocked**
  from auto-selection while ``retry_wait > 0`` — it stays FIFO-ordered in
  the queue (its position is preserved), but the selector skips it;

* the **first eligible** queued job (smallest ``seq`` with
  ``retry_wait == 0``; a single queued job is always eligible regardless of
  counter — a lone job with an exhausted backoff must not deadlock the
  queue) is the auto-selected job;

* if no queued job is eligible, auto-selection fails closed with
  ``RETRY_WAIT`` — the caller (orchestrator) deterministically discharges
  the queue (decrements all positive ``retry_wait`` counters by one, no
  below zero) **only when it started no job on that pass**, and retries on
  a later pass; the loop is bounded by ``cycles``;

* explicit ``--job-id`` selection bypasses the backoff block (operator
  intent), but still respects every other bound (capacity, attempts,
  identity, malformed state).

The discharge is idempotent, bounded by the queue length, and never
touches attempts/seq (position = FIFO fairness is preserved).
"""

from __future__ import annotations

from dataclasses import dataclass

from trajectory_os.runs import model

MAX_RETRY_WAIT = 8  # hard backoff cap (bounded; no unbounded delays)


class AutoStartBlockedError(Exception):
    """No queued job is eligible for auto-start on this pass (deterministic)."""

    def __init__(self, code: str = model.ERR_RETRY_WAIT,
                 message: str = "queue has entries but none eligible (RETRY_WAIT)") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RetryFacts:
    eligible: bool
    reason: str  # "eligible" | "RETRY_WAIT"
    blocked_count: int  # entries skipped due to positive counter (bounded info)
    eligible_seq: int | None  # seq of the selected entry, if any


def backoff_for_attempt(attempts_consumed: int) -> int:
    """Deterministic bounded backoff for a job that just used an attempt.

    Contract (explicit about ``attempts_consumed == 0``):

    * ``0`` — **deliberate zero-backoff case**: no attempt has been
      consumed yet (fresh / first-pass job), so the job carries no delay
      and is immediately eligible.  This is a defined, tested contract,
      not an error and not a silently-normalized input;
    * ``>= 1`` — grows geometrically (``1 << min(attempts-1, 6)``) and is
      hard-capped at ``MAX_RETRY_WAIT``; never negative, never unbounded;
    * boolean or non-integer inputs normalize to the same zero-backoff
      case (deterministic; strict type validation is a caller concern).

    The mapping is total and deterministic: the same input always yields
    the same bounded delay, so retry scheduling is reproducible.
    """
    if isinstance(attempts_consumed, bool) or not isinstance(attempts_consumed, int) \
            or attempts_consumed < 1:
        return 0  # first (and only, for DEFAULT_MAX_ATTEMPTS=1) -> no backoff
    delay = 1 << min(attempts_consumed - 1, 6)
    return min(delay, MAX_RETRY_WAIT)


def is_eligible(entry: object, entries_count: int) -> bool:
    """Eligibility predicate for one entry (pure, deterministic)."""
    wait = getattr(entry, "retry_wait", 0)
    if wait is None:
        wait = 0
    if not isinstance(wait, int) or isinstance(wait, bool):
        wait = 0
    wait = max(wait, 0)
    if entries_count <= 1:
        return True  # lone entry: never self-blocked (no deadlock)
    return wait == 0


def select_auto(entries: list[object]) -> tuple[object | None, RetryFacts]:
    """Deterministically select the auto-start candidate (FIFO-fair).

    Returns ``(entry_or_None, facts)``; the entry is never mutated.
    """
    ordered = sorted(entries, key=lambda e: int(getattr(e, "seq", 0)))
    if not ordered:
        return None, RetryFacts(eligible=False, reason="QUEUE_EMPTY",
                                blocked_count=0, eligible_seq=None)
    if len(ordered) == 1:
        seq = int(getattr(ordered[0], "seq", 0))
        return ordered[0], RetryFacts(eligible=True, reason="eligible",
                                      blocked_count=0,
                                      eligible_seq=seq)
    skipped = 0
    for entry in ordered:
        if is_eligible(entry, len(ordered)):
            seq = int(getattr(entry, "seq", 0))
            return entry, RetryFacts(eligible=True, reason="eligible",
                                     blocked_count=skipped, eligible_seq=seq)
        skipped += 1
    return None, RetryFacts(eligible=False, reason=model.ERR_RETRY_WAIT,
                            blocked_count=skipped, eligible_seq=None)


def pick_explicit(entries: list[object], job_id: str) -> object:
    """Locate an explicitly requested entry (bypasses backoff; respects nothing
    else here — the caller performs capacity/attempt/identity checks)."""
    for entry in entries:
        if getattr(entry, "job_id", None) == job_id:
            return entry
    raise LookupError(job_id)


def discharge_counts(entries: list[object]) -> int:
    """How many entries have a positive backoff counter (bounded, pure)."""
    count = 0
    for entry in entries:
        wait = getattr(entry, "retry_wait", 0)
        if isinstance(wait, int) and not isinstance(wait, bool) and wait > 0:
            count += 1
    return count
