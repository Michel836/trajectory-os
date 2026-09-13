"""V1.99 — state reconstruction (reap) into canonical buckets.

One bounded, deterministic, idempotent operation:

* state is loaded STRICTLY — malformed state fails closed with an explicit
  code and performs NO mutation (no partial repairs, no silent rewriting);
* ownership is proven for every active record (bounded, non-blocking
  evidence checks);
* unambiguous terminal records are moved to ``terminal`` (done/failed/
  crashed) with honest exit/signal evidence — no invented codes;
* records whose live state cannot be proven are kept in ``uncertain`` with
  their unproven reason codes — NEVER silently killed, dropped, or assumed;
* retry waits are preserved and, when a requeued job keeps its backoff, the
  requeued entry stays honest (``retry_wait`` semantics unchanged);
* the outcome is a stable report (buckets + mutation counts) and, because
  the operation is idempotent, running it again is a no-op.

Read-only by default; the one mutation path (terminalizing provably-dead
records) runs inside the bounded state lock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trajectory_os.runs import model, observability, store
from trajectory_os.runs import orchestration as orch
from trajectory_os.runs.locking import StateLock

LOCK_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ReconstructionOutcome:
    """Stable reconstruction report (read-only or single bounded mutation)."""

    reconstructed: bool
    code: str | None                 # stable failure code when not reconstructed
    buckets: Mapping[str, int]
    jobs: Mapping[str, Any]
    mutations: Mapping[str, int]
    stop_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.OPS_SCHEMA_VERSION,
            "tool": model.CLI_NAME,
            "kind": "reconstruction",
            "reconstructed": self.reconstructed,
            "code": self.code,
            "buckets": dict(self.buckets),
            "jobs": dict(self.jobs),
            "mutations": dict(self.mutations),
            "stop_reason": self.stop_reason,
        }


def _empty_mutations() -> dict[str, int]:
    return {
        "closed_appended": 0,
        "requeued": 0,
        "active_removed": 0,
        "retry_discharged": 0,
    }


def reconstruct(
    state_root: Any,
    runs_root: object,
    *,
    observe: Mapping[str, str] | None = None,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> ReconstructionOutcome:
    """Reconcile current state into canonical buckets (bounded, idempotent).

    ``runs_root`` is accepted for interface compatibility; the current
    persistence layout derives state strictly from ``state_root`` (the runs
    registry is consulted only at launch time).

    ``observe`` is an optional mapping job_id -> authoritative evidence
    string (e.g. ``"exit:0"`` / ``"killed:SIGKILL"``) supplied by an
    authoritative observer; it is never invented.
    """
    lock = StateLock(store.state_paths(state_root)["base"], timeout_seconds=lock_timeout)
    with lock as acquired:
        if not acquired:
            return ReconstructionOutcome(
                reconstructed=False,
                code=model.STOP_LOCK_CONTENTION,
                buckets=_zero_buckets(),
                jobs={},
                mutations=_empty_mutations(),
                stop_reason=model.STOP_LOCK_CONTENTION,
            )
        try:
            state = orch.rebuild_state(state_root)
        except store.MalformedStateError as exc:
            # Fail closed on malformed state: no partial repair, no mutation.
            return ReconstructionOutcome(
                reconstructed=False,
                code=model.ERR_RECONSTRUCTION_MALFORMED,
                buckets=_zero_buckets(),
                jobs={},
                mutations=_empty_mutations(),
                stop_reason=str(exc),
            )

        evidence = dict(observe) if observe is not None else None
        reap_report = orch.reap(state, observe=evidence)

        # Re-derive the strict snapshot after the (possible) bounded mutation.
        state_after = orch.rebuild_state(state_root)
        snap = observability.snapshot(state_after)

        results = reap_report.get("results")
        results = results if isinstance(results, (list, tuple)) else []
        closed_appended = 0
        requeued = 0
        for item in results:
            if not isinstance(item, Mapping):
                continue
            if item.get("outcome") == "still_running":
                continue
            closed_appended += 1
            if item.get("requeued") is True:
                requeued += 1

        return ReconstructionOutcome(
            reconstructed=True,
            code=None,
            buckets=_canonical_buckets(snap),
            jobs=_jobs_map(snap),
            mutations={
                "closed_appended": closed_appended,
                "requeued": requeued,
                "active_removed": closed_appended,
                "retry_discharged": 0,
            },
            stop_reason=model.STOP_WORK_SETTLED,
        )


def _zero_buckets() -> dict[str, int]:
    return {
        "queued": 0,
        "admitted_active": 0,
        "uncertain": 0,
        "retrying": 0,
        "dependency_blocked": 0,
        "terminal": 0,
    }


def _canonical_buckets(snap: Mapping[str, object]) -> dict[str, int]:
    """Map the (newer) snapshot shape onto the stable canonical bucket names."""
    counts = _counts_map(snap.get("counts"))
    raw = snap.get("queue")
    queue_docs: list[Mapping[str, object]] = []
    if isinstance(raw, (list, tuple)):
        queue_docs = [d for d in raw if isinstance(d, Mapping)]
    active_n = counts.get("active", 0)

    def backoff_pending(d: Mapping[str, object]) -> bool:
        wait = d.get("retry_wait")
        return isinstance(wait, int) and wait > 0

    return {
        "queued": counts.get("queued", 0),
        "admitted_active": active_n,
        "uncertain": max(active_n - counts.get("active_proven", 0), 0),
        "retrying": sum(1 for d in queue_docs if backoff_pending(d)),
        "dependency_blocked": sum(
            1
            for d in queue_docs
            if d.get("dependency_status") not in (model.DEP_STATUS_SATISFIED, None)
        ),
        "terminal": counts.get("closed", 0),
    }


def _counts_map(raw: object) -> dict[str, int]:
    """Narrow the (loosely typed) snapshot counts mapping to int counts."""
    out: dict[str, int] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, int):
                out[str(key)] = value
    return out


def _jobs_map(snap: Mapping[str, object]) -> dict[str, Any]:
    """Per-job document map from the snapshot sections (active/queue/closed)."""
    jobs: dict[str, Any] = {}
    for section in ("active", "queue", "closed"):
        docs = snap.get(section)
        if not isinstance(docs, (list, tuple)):
            continue
        for doc in docs:
            if isinstance(doc, Mapping) and doc.get("job_id") is not None:
                jobs[str(doc["job_id"])] = dict(doc)
    return jobs


def render_reconstruction_human(outcome: ReconstructionOutcome) -> str:
    lines = [
        f"reconstructed: {str(outcome.reconstructed).lower()}"
        + (f" code={outcome.code}" if outcome.code else ""),
    ]
    if outcome.reconstructed:
        counts = outcome.buckets
        lines.append(
            "buckets: "
            + ", ".join(
                f"{name}={counts.get(name, 0)}"
                for name in (
                    "queued", "admitted_active", "uncertain",
                    "retrying", "dependency_blocked", "terminal",
                )
            )
        )
        mutations = outcome.mutations
        lines.append(
            "mutations: "
            + ", ".join(f"{name}={value}" for name, value in mutations.items())
            + " (idempotent: a second run is a no-op)"
        )
    else:
        lines.append(f"stop_reason: {outcome.stop_reason} (no mutation performed)")
    return "\n".join(lines)
