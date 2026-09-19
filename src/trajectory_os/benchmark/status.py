"""M029 — benchmark observability: canonical status + follow mode.

The status document is derived only from persisted benchmark state (event
stream, manifest and trial records). It exposes the operator fields required
by the mission and, crucially, distinguishes the three actor roles:

* the **implementation agent** (the backend under test);
* the **inline reviewer** (inactive unless explicitly enabled);
* the **final independent reviewer** (the M029 model, active only when it was
  actually invoked for the latest trial).

An inactive reviewer is rendered as inactive with its own identity and reason;
a stale/default reviewer can never be displayed as the active one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trajectory_os.benchmark import engine, model, store
from trajectory_os.benchmark import events as bench_events
from trajectory_os.benchmark import review as bench_review
from trajectory_os.benchmark import workloads as bench_workloads


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _now(clock: Callable[[], str]) -> datetime:
    parsed = _parse(clock())
    return parsed if parsed is not None else datetime.now(UTC)


def status_document(root: str | Path, benchmark_run_id: str, *,
                    clock: Callable[[], str] = engine.utc_now) -> dict[str, Any]:
    run_root = store.run_root(root, benchmark_run_id)
    manifest = store.load_manifest(run_root)
    records = store.load_trials(run_root)
    event_list = store.load_events(run_root)
    state: Mapping[str, Any] = {}
    if (run_root / store.STATE_NAME).is_file():
        state = store.load_state(run_root)

    config = _config_from_manifest(root, manifest)
    planned = engine.plan_trials(config)
    completed = {event.trial_id for event in event_list
                 if event.kind == bench_events.KIND_TRIAL_COMPLETED
                 and event.trial_id}
    started_events = [event for event in event_list
                      if event.kind == bench_events.KIND_TRIAL_STARTED]
    current_trial_id: str | None = None
    if started_events:
        candidate = started_events[-1].trial_id
        if candidate not in completed:
            current_trial_id = candidate
    current_plan = next(
        (trial for trial in planned if trial.trial_id == current_trial_id),
        None)
    next_plan = next(
        (trial for trial in planned if trial.trial_id not in completed),
        None)
    latest = _latest_record(records, current_trial_id)
    phase = event_list[-1].phase if event_list else bench_events.PHASE_QUALIFY
    first_at = _parse(event_list[0].at) if event_list else None
    elapsed_s = None
    if first_at is not None:
        elapsed_s = max(0.0, (_now(clock) - first_at).total_seconds())

    backend = current_plan.backend if current_plan else (
        latest.backend if latest else _first_backend(manifest))
    telemetry = latest.telemetry if latest else None
    actors = _actors(latest)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "benchmark_run_id": benchmark_run_id,
        "state": str(state.get("state", model.RUN_RUNNING)),
        "phase": phase,
        "elapsed_s": elapsed_s,
        "current": (current_plan.trial_id if current_plan else None),
        "next": (next_plan.trial_id if next_plan else None),
        "trial": (current_plan.trial_id if current_plan
                  else (latest.trial_id if latest else None)),
        "workload_id": (current_plan.workload.workload_id if current_plan
                        else (latest.workload_id if latest else None)),
        "repetition": (current_plan.repetition if current_plan
                       else (latest.repetition if latest else None)),
        "backend": backend,
        "provider": manifest.target_provider,
        "model": manifest.target_model,
        "final_reviewer_model": manifest.final_reviewer_model,
        "actors": actors,
        "requests": telemetry.request_count if telemetry else None,
        "tokens": {
            "prompt": telemetry.prompt_tokens if telemetry else None,
            "completion": telemetry.completion_tokens if telemetry else None,
            "total": telemetry.total_tokens if telemetry else None,
        },
        "cache": {
            "hit": telemetry.cache_hit_tokens if telemetry else None,
            "miss": telemetry.cache_miss_tokens if telemetry else None,
            "ratio": telemetry.cache_hit_ratio if telemetry else None,
        },
        "tps": {
            "prompt": telemetry.prompt_tps if telemetry else None,
            "generation": telemetry.generation_tps if telemetry else None,
        },
        "cost_usd": telemetry.cost_usd if telemetry else None,
        "validation": (latest.validation.to_dict() if latest else None),
        "review": (latest.review.to_dict() if latest else None),
        "current_patch": (latest.patch.to_dict() if latest else None),
        "counts": {
            "planned": len(planned),
            "completed": len(records),
            "pass": sum(1 for r in records
                        if r.status == model.TS_PASS),
            "blocked": sum(1 for r in records
                           if r.status == model.TS_BLOCKED),
            "failed": sum(1 for r in records
                          if r.status == model.TS_FAILED),
            "cancelled": sum(1 for r in records
                             if r.status == model.TS_CANCELLED),
            "unavailable": sum(1 for r in records
                               if r.status == model.TS_UNAVAILABLE),
        },
        "terminal": str(state.get("state", model.RUN_RUNNING))
        in model.TERMINAL_RUN_STATES,
    }


def _config_from_manifest(root: str | Path,
                          manifest: model.RunManifest) -> engine.RunConfig:
    workload_specs = tuple(
        bench_workloads.by_id(workload_id)
        for workload_id in manifest.workload_ids)
    return engine.RunConfig(
        root=str(root), benchmark_run_id=manifest.benchmark_run_id,
        mode=manifest.mode, workloads=workload_specs,
        backends=manifest.backends, repetitions=manifest.repetitions,
        target_provider=manifest.target_provider,
        target_model=manifest.target_model,
        final_reviewer_model=manifest.final_reviewer_model,
        baseline_revision=manifest.baseline_revision,
        baseline_patch=manifest.baseline_patch,
        environment=dict(manifest.environment))


def _latest_record(records: Sequence[model.TrialRecord],
                   current_trial_id: str | None) -> model.TrialRecord | None:
    if current_trial_id is not None:
        for record in records:
            if record.trial_id == current_trial_id:
                return record
    return records[-1] if records else None


def _first_backend(manifest: model.RunManifest) -> str | None:
    return manifest.backends[0] if manifest.backends else None


def _actors(latest: model.TrialRecord | None) -> dict[str, Any]:
    if latest is None:
        return {
            "implementation_agent": None,
            "inline_reviewer": bench_review.inline_reviewer_observation(
            ).to_dict(),
            "final_independent_reviewer": None,
        }
    review = latest.review
    return {
        "implementation_agent": {
            "backend": latest.backend, "provider": latest.provider,
            "model": latest.model, "active": True,
        },
        "inline_reviewer": bench_review.inline_reviewer_observation(
        ).to_dict(),
        "final_independent_reviewer": review.reviewer.to_dict(),
    }


def render_status(document: Mapping[str, Any]) -> str:
    def fmt(value: object) -> str:
        return "unavailable" if value is None else str(value)

    tokens = document.get("tokens")
    tokens = tokens if isinstance(tokens, Mapping) else {}
    cache = document.get("cache")
    cache = cache if isinstance(cache, Mapping) else {}
    tps = document.get("tps")
    tps = tps if isinstance(tps, Mapping) else {}
    actors = document.get("actors")
    actors = actors if isinstance(actors, Mapping) else {}
    final = actors.get("final_independent_reviewer")
    final = final if isinstance(final, Mapping) else {}
    inline = actors.get("inline_reviewer")
    inline = inline if isinstance(inline, Mapping) else {}
    implementation = actors.get("implementation_agent")
    implementation = (implementation if isinstance(implementation, Mapping)
                      else {})
    counts = document.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    lines = [
        "=" * 64,
        f"RUN ID   : {document.get('benchmark_run_id')}",
        f"STATE    : {document.get('state')}   PHASE: {document.get('phase')}"
        f"   ELAPSED: {fmt(document.get('elapsed_s'))}s",
        f"CURRENT  : {fmt(document.get('current'))}",
        f"NEXT     : {fmt(document.get('next'))}",
        f"BACKEND  : {fmt(document.get('backend'))}",
        f"PROVIDER : {fmt(document.get('provider'))}   "
        f"MODEL: {fmt(document.get('model'))}",
        f"TRIAL    : {fmt(document.get('trial'))}   "
        f"REPETITION: {fmt(document.get('repetition'))}",
        f"REQUESTS : {fmt(document.get('requests'))}",
        f"TOKENS   : prompt={fmt(tokens.get('prompt'))} "
        f"completion={fmt(tokens.get('completion'))} "
        f"total={fmt(tokens.get('total'))}",
        f"CACHE    : hit={fmt(cache.get('hit'))} miss={fmt(cache.get('miss'))} "
        f"ratio={fmt(cache.get('ratio'))}",
        f"TPS      : prompt={fmt(tps.get('prompt'))} "
        f"generation={fmt(tps.get('generation'))}",
        f"COST     : {fmt(document.get('cost_usd'))} usd",
        f"VALIDATION: {fmt(document.get('validation'))}",
        f"REVIEW   : {fmt(document.get('review'))}",
        f"PATCH    : {fmt(document.get('current_patch'))}",
        f"COUNTS   : pass={counts.get('pass', 0)} "
        f"blocked={counts.get('blocked', 0)} failed={counts.get('failed', 0)} "
        f"cancelled={counts.get('cancelled', 0)} "
        f"unavailable={counts.get('unavailable', 0)}",
        "-" * 64,
        f"implementation agent : {fmt(implementation.get('backend'))} / "
        f"{fmt(implementation.get('provider'))}:"
        f"{fmt(implementation.get('model'))} "
        f"active={implementation.get('active', False)}",
        f"inline reviewer      : {fmt(inline.get('backend'))} / "
        f"{fmt(inline.get('provider'))}:{fmt(inline.get('model'))} "
        f"active={inline.get('active', False)} ({fmt(inline.get('reason'))})",
        f"final reviewer       : {fmt(final.get('backend'))} / "
        f"{fmt(final.get('provider'))}:{fmt(final.get('model'))} "
        f"active={final.get('active', False)} ({fmt(final.get('reason'))})",
        "=" * 64,
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class FollowResult:
    document: dict[str, Any]
    polls: int
    exited: bool


def follow(
    root: str | Path,
    benchmark_run_id: str,
    *,
    reader: Callable[[str | Path, str], dict[str, Any]] | None = None,
    sleep: Callable[[float], None] | None = None,
    interval_s: float = 2.0,
    max_polls: int = 600,
    clock: Callable[[], str] = engine.utc_now,
) -> FollowResult:
    """Poll the canonical status until a terminal state (bounded).

    Exits on ``READY_FOR_COMMIT`` / ``BLOCKED`` / ``FAILED`` / ``CANCELLED`` /
    ``COMPLETE``. A bounded number of polls prevents an unbounded wait.
    """
    from time import sleep as default_sleep
    read = reader or (lambda directory, run_id: status_document(
        directory, run_id, clock=clock))
    wait = sleep or default_sleep
    polls = 0
    document = read(root, benchmark_run_id)
    while not document.get("terminal") and polls < max_polls:
        polls += 1
        wait(interval_s)
        document = read(root, benchmark_run_id)
    return FollowResult(document=document, polls=polls,
                        exited=bool(document.get("terminal")))
