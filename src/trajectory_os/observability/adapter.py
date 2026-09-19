"""M030 — backend-neutral adapter: benchmark artifacts -> canonical status.

The M029 benchmark runtime is one *runtime adapter* that feeds the canonical
observability contract. This module maps its authoritative durable state
(manifest, events, trials, summary) into a :class:`CanonicalStatus` document
without inventing any fact and without ever displaying a stale/default
reviewer as active.

The mapping is pure over the persisted artifacts: it never greps a log to
reconstruct current truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.benchmark import events as bench_events
from trajectory_os.benchmark import status as bench_status
from trajectory_os.benchmark import store as bench_store
from trajectory_os.observability import model

#: benchmark run state -> canonical lifecycle state.
_LIFECYCLE = {
    "RUNNING": model.LC_RUNNING,
    "READY_FOR_COMMIT": model.LC_COMPLETE,
    "BLOCKED": model.LC_COMPLETE,
    "FAILED": model.LC_COMPLETE,
    "CANCELLED": model.LC_COMPLETE,
    "COMPLETE": model.LC_COMPLETE,
}

_READINESS = {
    "RUNNING": model.RD_INDETERMINATE,
    "READY_FOR_COMMIT": model.RD_READY_FOR_COMMIT,
    "BLOCKED": model.RD_BLOCKED,
    "FAILED": model.RD_FAILED,
    "CANCELLED": model.RD_CANCELLED,
    "COMPLETE": model.RD_UNKNOWN,
}

_RESULT = {
    "PASS": model.RESULT_PASS,
    "REJECT": model.RESULT_REJECT,
    "FAIL": model.RESULT_FAIL,
    "BLOCKED": model.RESULT_BLOCKED,
}

_STAGE = {
    bench_events.PHASE_QUALIFY: model.STAGE_PREFLIGHT,
    bench_events.PHASE_EXECUTE: model.STAGE_EXECUTE,
    bench_events.PHASE_VALIDATE: model.STAGE_VALIDATE,
    bench_events.PHASE_REVIEW: model.STAGE_REVIEW,
    bench_events.PHASE_AGGREGATE: model.STAGE_AGGREGATE,
    bench_events.PHASE_DONE: model.STAGE_DONE,
}


def _reviewer_status(actor: Mapping[str, Any],
                     role: str) -> model.ReviewerStatus:
    if role == model.ROLE_INLINE_REVIEWER:
        return model.ReviewerStatus.disabled(
            role, reason="benchmark uses the final independent reviewer only")
    enabled = True
    active = bool(actor.get("active"))
    if not active:
        return model.ReviewerStatus.disabled(
            role, reason=str(actor.get("reason")
                             or "reviewer not active"))
    return model.ReviewerStatus(
        role=role, enabled=enabled, active=True,
        backend=(_opt(actor.get("backend"))),
        provider=_opt(actor.get("provider")),
        model=_opt(actor.get("model")),
        reason=str(actor.get("reason") or model.R_OK)).validate()


def _opt(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def canonical_from_document(
    document: Mapping[str, Any], *,
    telemetry_mode: str = model.TELEMETRY_STANDARD,
    final_reviewer_active: bool | None = None,
    final_reviewer_model: str | None = None,
) -> dict[str, Any]:
    """Map one benchmark status document into a canonical status document.

    ``final_reviewer_active``/``final_reviewer_model`` let the caller supply
    the run-wide observed reviewer fact (any trial in the run actually
    invoked the final independent reviewer) instead of relying on the single
    latest trial, which may be a fail-closed trial with no review.
    """
    run_state = str(document.get("state", "RUNNING"))
    actors = document.get("actors")
    actors = actors if isinstance(actors, Mapping) else {}
    final_actor = actors.get("final_independent_reviewer")
    final_actor = dict(final_actor) if isinstance(final_actor, Mapping) else {}
    if final_reviewer_active is not None:
        final_actor["active"] = final_reviewer_active
        if final_reviewer_active and final_reviewer_model:
            final_actor["model"] = final_reviewer_model
            final_actor.setdefault("backend", "ollama")
            final_actor.setdefault("provider", "ollama")
    review = document.get("review")
    review = review if isinstance(review, Mapping) else {}
    patch = document.get("current_patch")
    patch_sha = _opt(patch.get("sha256")) if isinstance(patch, Mapping) else None
    review_active = bool(final_actor.get("active"))
    previous_gate: str | None = None
    previous_result: str | None = None
    if review_active:
        previous_gate = model.GATE_REVIEW
        previous_result = {
            "READY_FOR_COMMIT": model.RESULT_PASS,
            "BLOCKED": model.RESULT_REJECT,
            "FAILED": model.RESULT_FAIL,
        }.get(run_state, model.RESULT_PASS)
    readiness = _READINESS.get(run_state, model.RD_UNKNOWN)
    lifecycle = _LIFECYCLE.get(run_state, model.LC_RUNNING)
    status = model.CanonicalStatus.build(
        run_id=str(document.get("benchmark_run_id", "unknown")),
        state=lifecycle,
        stage=_STAGE.get(str(document.get("phase", "QUALIFY")),
                         model.STAGE_EXECUTE),
        phase=str(document.get("phase", "QUALIFY")),
        attempt=int(document.get("repetition", 0) or 0),
        current_backend=_opt(document.get("backend")),
        current_provider=_opt(document.get("provider")),
        current_model=_opt(document.get("model")),
        inline_review_enabled=False,
        inline_reviewer=_reviewer_status(
            {}, model.ROLE_INLINE_REVIEWER),
        final_review_enabled=review_active or run_state == "RUNNING",
        final_reviewer=_reviewer_status(
            final_actor, model.ROLE_FINAL_INDEPENDENT_REVIEWER),
        previous_gate=previous_gate,
        previous_result=previous_result,
        reviewed_patch=patch_sha if review_active else None,
        current_patch=patch_sha,
        next_action=model.next_action_for(lifecycle, readiness),
        last_meaningful_event_at=_opt(document.get("last_event_at")),
        heartbeat_at=_opt(document.get("updated_at")),
        terminal_reason=None,
        terminal_reason_code=None,
        readiness=readiness,
        current=_opt(document.get("current")),
        next=_opt(document.get("next")),
        telemetry_mode=telemetry_mode,
        telemetry=None,
        updated_at=_opt(document.get("updated_at")))
    return status.to_dict()


def canonical_from_run(
    root: str | Path, benchmark_run_id: str, *,
    telemetry_mode: str = model.TELEMETRY_STANDARD,
) -> dict[str, Any]:
    """Read a benchmark run and return its canonical status document."""
    run_root = bench_store.run_root(root, benchmark_run_id)
    document = bench_status.status_document(root, benchmark_run_id)
    records = bench_store.load_trials(run_root)
    active_reviews = [record.review for record in records
                      if record.review.active]
    canonical = canonical_from_document(
        document, telemetry_mode=telemetry_mode,
        final_reviewer_active=bool(active_reviews),
        final_reviewer_model=(active_reviews[0].reviewer.model
                              if active_reviews else None))
    telemetry_path = run_root / "telemetry.json"
    if telemetry_path.is_file():
        import json

        try:
            canonical["telemetry"] = json.loads(
                telemetry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            canonical["telemetry"] = None
    return canonical


def blocked_status(
    *, run_id: str, backend: str | None, provider: str | None,
    model_name: str | None, reason: str, detail: str,
    telemetry_mode: str = model.TELEMETRY_STANDARD,
) -> dict[str, Any]:
    """Canonical BLOCKED status for a fail-fast preflight rejection."""
    status = model.CanonicalStatus.build(
        run_id=run_id, state=model.LC_COMPLETE, stage=model.STAGE_DONE,
        phase="PREFLIGHT", attempt=0,
        current_backend=backend, current_provider=provider,
        current_model=model_name,
        inline_review_enabled=False,
        inline_reviewer=model.ReviewerStatus.disabled(
            model.ROLE_INLINE_REVIEWER,
            reason="preflight rejection; no review attempted"),
        final_review_enabled=False,
        final_reviewer=model.ReviewerStatus.disabled(
            model.ROLE_FINAL_INDEPENDENT_REVIEWER,
            reason="preflight rejection; no review attempted"),
        previous_gate=model.GATE_PREFLIGHT,
        previous_result=model.RESULT_FAIL,
        reviewed_patch=None, current_patch=None,
        next_action="operator: fix the invalid configuration and re-run",
        last_meaningful_event_at=None, heartbeat_at=None,
        terminal_reason=detail, terminal_reason_code=reason,
        readiness=model.RD_BLOCKED, telemetry_mode=telemetry_mode)
    return status.to_dict()


def write_run_artifacts(
    root: str | Path, benchmark_run_id: str, *,
    telemetry_mode: str = model.TELEMETRY_STANDARD,
) -> dict[str, Any]:
    """Persist canonical status.json + telemetry.json for a benchmark run.

    This is the benchmark runtime adapter's output boundary: durable M029
    artifacts are mapped once into the canonical contract and written
    atomically. Failures are non-fatal to the benchmark itself (observability
    must never break the run it observes).
    """
    from trajectory_os.observability import store, telemetry

    run_root = bench_store.run_root(root, benchmark_run_id)
    canonical = canonical_from_run(
        root, benchmark_run_id, telemetry_mode=telemetry_mode)
    records = bench_store.load_trials(run_root)
    successful = sum(1 for record in records
                     if record.status == "PASS")
    identity = {
        "run_id": benchmark_run_id,
        "backend": canonical.get("current_backend"),
        "provider": canonical.get("current_provider"),
        "model": canonical.get("current_model"),
        "mode": telemetry_mode,
    }
    telemetry_document = telemetry.aggregate_telemetry(
        run_id=benchmark_run_id, mode=telemetry_mode, trials=records,
        identity=identity, successful_tasks=successful,
        reviewed_trials=sum(1 for r in records if r.review.active))
    telemetry_document["waste"] = telemetry.wasted_in_failed_cycles(records)
    canonical["telemetry"] = telemetry_document
    store.write_telemetry(run_root, telemetry_document)
    store.write_json(run_root / store.STATUS_NAME, canonical)
    return canonical
