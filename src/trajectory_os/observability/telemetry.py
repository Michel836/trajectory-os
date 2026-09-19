"""M030 — telemetry collection + aggregation (provider-grounded, never invented).

Collection, aggregation and presentation are separate layers:

* :func:`collect_trial_metrics` reads one trial's authoritative
  :class:`~trajectory_os.benchmark.model.TrialTelemetry` (collection);
* :func:`aggregate_telemetry` folds the observed trials into one canonical
  run telemetry document (aggregation);
* :func:`derive_metrics` computes deterministic derived metrics only where
  the source values exist (aggregation);
* the projection layer renders the document (presentation).

Three modes bound the overhead:

* ``off``       — no metric document is produced (identity/outcome only);
* ``standard``  — lightweight run/phase summaries, bounded overhead;
* ``benchmark`` — finer sampling, percentiles and local resource sampling.

Every metric carries explicit provenance; an unavailable metric is ``None``
with a stable reason and is never estimated or invented.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from trajectory_os.benchmark import model as bench_model
from trajectory_os.observability import model

#: Metric aggregation kinds.
SUM = "sum"
AVERAGE = "average"
MINIMUM = "minimum"
MAXIMUM = "maximum"
RATIO = "ratio"

#: Canonical run metric names and their aggregation kind.
METRIC_KINDS: Mapping[str, str] = {
    "request_count": SUM,
    "prompt_tokens": SUM,
    "completion_tokens": SUM,
    "total_tokens": SUM,
    "cache_read_tokens": SUM,
    "cache_hit_tokens": SUM,
    "cache_miss_tokens": SUM,
    "cost_usd": SUM,
    "total_duration_ms": SUM,
    "resume_duration_ms": SUM,
    "retries": SUM,
    "repairs": SUM,
    "review_rejects": SUM,
    "protocol_errors": SUM,
    "validation_failures": SUM,
    "request_latency_ms": AVERAGE,
    "ttft_ms": AVERAGE,
    "prompt_tps": AVERAGE,
    "generation_tps": AVERAGE,
    "cpu_percent": AVERAGE,
    "gpu_utilization_pct": MAXIMUM,
    "ram_bytes": MAXIMUM,
    "vram_bytes": MAXIMUM,
    "gpu_power_w": MAXIMUM,
    "gpu_temp_c": MAXIMUM,
    "first_useful_patch_ms": MINIMUM,
}

#: Metrics whose benchmark-mode percentile series is meaningful.
PERCENTILE_METRICS = (
    "request_latency_ms", "ttft_ms", "prompt_tps", "generation_tps",
    "total_duration_ms",
)

#: Reason used whenever the backend/provider does not expose a metric.
REASON_UNAVAILABLE = "backend/provider does not expose this metric"


@dataclass(frozen=True)
class ResourceSample:
    """One bounded local resource observation (never fabricated)."""

    cpu_percent: float | None = None
    ram_bytes: int | None = None
    gpu_utilization_pct: int | None = None
    vram_bytes: int | None = None
    gpu_power_w: float | None = None
    gpu_temp_c: float | None = None
    reasons: Mapping[str, str] = field(default_factory=dict)

    def metrics(self) -> dict[str, model.Metric]:
        out: dict[str, model.Metric] = {}
        for name in ("cpu_percent", "ram_bytes", "gpu_utilization_pct",
                     "vram_bytes", "gpu_power_w", "gpu_temp_c"):
            value = getattr(self, name)
            if value is None:
                out[name] = model.Metric.unavailable(
                    name, self.reasons.get(
                        name, "local resource not exposed"))
            else:
                out[name] = model.Metric(
                    name=name, value=value, source=model.SRC_LOCAL)
        return out


def empty_resources(reason: str) -> ResourceSample:
    return ResourceSample(reasons={
        name: reason for name in
        ("cpu_percent", "ram_bytes", "gpu_utilization_pct", "vram_bytes",
         "gpu_power_w", "gpu_temp_c")})


def local_resource_sampler(model_name: str,
                           *, locality: str | None = None,
                           ) -> Callable[[], ResourceSample]:
    """Read-only local resource sampler for benchmark mode (fail-soft)."""
    from trajectory_os.resources import probe as resource_probe

    def sample() -> ResourceSample:
        report = resource_probe.discover()
        reasons = {
            "cpu_percent": "read-only probe does not expose CPU utilization",
            "gpu_power_w": "not exposed by the bounded nvidia-smi query",
            "gpu_temp_c": "not exposed by the bounded nvidia-smi query",
        }
        if report.gpu_utilization_pct is None:
            reasons["gpu_utilization_pct"] = "no local GPU telemetry available"
        if report.gpu_mem_used_bytes is None:
            reasons["vram_bytes"] = "no local GPU telemetry available"
        if report.ram_bytes is None:
            reasons["ram_bytes"] = "read-only probe reports total RAM only"
        return ResourceSample(
            cpu_percent=None, ram_bytes=report.ram_bytes,
            gpu_utilization_pct=report.gpu_utilization_pct,
            vram_bytes=report.gpu_mem_used_bytes, gpu_power_w=None,
            gpu_temp_c=None, reasons=reasons)

    return sample


# --- collection ---------------------------------------------------------------


def collect_trial_metrics(
    telemetry: bench_model.TrialTelemetry,
) -> dict[str, model.Metric]:
    """Project one authoritative trial telemetry into canonical metrics."""
    out: dict[str, model.Metric] = {}
    for name in bench_model.TELEMETRY_FIELDS:
        value = getattr(telemetry, name)
        source = telemetry.sources.get(name, model.SRC_UNAVAILABLE)
        if value is None:
            out[name] = model.Metric.unavailable(
                name, telemetry.unavailable.get(name, REASON_UNAVAILABLE))
        else:
            canonical = {
                bench_model.SRC_PROVIDER: model.SRC_PROVIDER,
                bench_model.SRC_DERIVED: model.SRC_DERIVED,
                bench_model.SRC_LOCAL: model.SRC_LOCAL,
            }.get(source, model.SRC_LOCAL)
            out[name] = model.Metric(name=name, value=value, source=canonical)
    return out


# --- aggregation --------------------------------------------------------------


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile (deterministic, no interpolation)."""
    if not values:
        raise ValueError("percentile of empty sequence")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1,
                       int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _aggregate(values: Sequence[float], kind: str) -> float:
    if kind == SUM:
        return float(sum(values))
    if kind == AVERAGE:
        return float(sum(values) / len(values))
    if kind == MINIMUM:
        return float(min(values))
    if kind == MAXIMUM:
        return float(max(values))
    raise ValueError(f"unknown aggregation kind {kind!r}")


def aggregate_telemetry(
    *,
    run_id: str,
    mode: str,
    trials: Sequence[bench_model.TrialRecord],
    identity: Mapping[str, Any],
    phase_durations: Sequence[tuple[str, int]] = (),
    resources: ResourceSample | None = None,
    successful_tasks: int | None = None,
    reviewed_trials: int | None = None,
) -> dict[str, Any]:
    """Fold observed trials into one canonical run telemetry document."""
    if mode not in model.TELEMETRY_MODES:
        raise model.ObservabilityError("MALFORMED_TELEMETRY", mode)
    if mode == model.TELEMETRY_OFF:
        return {
            "schema_version": model.CANONICAL_SCHEMA_VERSION,
            "observability_version": model.OBSERVABILITY_VERSION,
            "run_id": run_id,
            "mode": mode,
            "identity": dict(identity),
            "metrics": {},
            "derived": {},
            "phase_durations": [],
            "series": {},
            "reason": "telemetry disabled by operator (mode=off)",
        }

    per_trial = [collect_trial_metrics(record.telemetry) for record in trials]
    metrics: dict[str, model.Metric] = {}
    for name, kind in METRIC_KINDS.items():
        collected = [trial[name] for trial in per_trial if name in trial]
        present = [m for m in collected
                   if m.source != model.SRC_UNAVAILABLE and m.value is not None]
        if present:
            numeric = [float(m.value) for m in present]  # type: ignore[arg-type]
            value = _aggregate(numeric, kind)
            if kind in (SUM,) and name not in ("cost_usd",):
                value = int(value)
            metrics[name] = model.Metric(
                name=name, value=value, source=model.SRC_DERIVED)
        else:
            reason = next(
                (m.reason for m in collected
                 if m.source == model.SRC_UNAVAILABLE and m.reason),
                REASON_UNAVAILABLE)
            metrics[name] = model.Metric.unavailable(name, reason)

    # Quality counters are local deterministic facts, never unavailable.
    for name in ("retries", "repairs", "review_rejects", "protocol_errors",
                 "validation_failures"):
        count = sum(1 for record in trials
                    if name == "repairs" and record.resumed)
        explicit = sum(int(getattr(record.telemetry, name, 0)
                           or 0) for record in trials)
        metrics[name] = model.Metric(
            name=name, value=(explicit if name != "repairs"
                              else max(explicit, count)),
            source=model.SRC_LOCAL)
    metrics["trial_count"] = model.Metric(
        name="trial_count", value=len(trials), source=model.SRC_LOCAL)

    if resources is not None:
        metrics.update(resources.metrics())

    derived = derive_metrics(
        metrics, successful_tasks=successful_tasks,
        reviewed_trials=(reviewed_trials
                         if reviewed_trials is not None
                         else sum(1 for r in trials if r.review.active)),
        trial_count=len(trials))
    series: dict[str, Any] = {}
    if mode == model.TELEMETRY_BENCHMARK:
        for name in PERCENTILE_METRICS:
            values = [
                float(getattr(record.telemetry, name))
                for record in trials
                if getattr(record.telemetry, name) is not None
            ]
            if values:
                series[name] = {
                    "average": sum(values) / len(values),
                    "median": _percentile(values, 0.5),
                    "p95": _percentile(values, 0.95),
                    "count": len(values),
                }
            else:
                series[name] = {
                    "average": None, "median": None, "p95": None,
                    "count": 0, "reason": REASON_UNAVAILABLE,
                }
    phase_series = [
        {"phase": phase, "duration_ms": duration}
        for phase, duration in phase_durations]
    return {
        "schema_version": model.CANONICAL_SCHEMA_VERSION,
        "observability_version": model.OBSERVABILITY_VERSION,
        "run_id": run_id,
        "mode": mode,
        "identity": dict(identity),
        "metrics": {name: metric.to_dict()
                    for name, metric in sorted(metrics.items())},
        "derived": {name: metric.to_dict()
                    for name, metric in sorted(derived.items())},
        "phase_durations": phase_series,
        "series": series,
    }


# --- derived metrics ----------------------------------------------------------


def _value(metrics: Mapping[str, model.Metric], name: str) -> float | None:
    metric = metrics.get(name)
    if metric is None or metric.source == model.SRC_UNAVAILABLE:
        return None
    if metric.value is None:
        return None
    return float(metric.value)


def _derived(name: str, *, numerator: float | None,
             denominator: float | None,
             reason: str) -> model.Metric:
    if numerator is None or denominator is None or denominator == 0:
        return model.Metric.unavailable(name, reason)
    return model.Metric(name=name, value=numerator / denominator,
                        source=model.SRC_DERIVED)


def derive_metrics(
    metrics: Mapping[str, model.Metric], *,
    successful_tasks: int | None,
    reviewed_trials: int,
    trial_count: int,
) -> dict[str, model.Metric]:
    """Deterministic derived metrics, computed only from available inputs."""
    tasks = (float(successful_tasks) if successful_tasks is not None
             else None)
    total_tokens = _value(metrics, "total_tokens")
    completion = _value(metrics, "completion_tokens")
    cost = _value(metrics, "cost_usd")
    duration_ms = _value(metrics, "total_duration_ms")
    hit = _value(metrics, "cache_hit_tokens")
    prompt = _value(metrics, "prompt_tokens")
    repairs = _value(metrics, "repairs")
    rejects = _value(metrics, "review_rejects")
    protocol_errors = _value(metrics, "protocol_errors")
    seconds = (duration_ms / 1000.0) if duration_ms is not None else None

    out: dict[str, model.Metric] = {}
    out["tokens_per_successful_task"] = _derived(
        "tokens_per_successful_task", numerator=total_tokens,
        denominator=tasks, reason="no successful trust-gated task/validated patch")
    out["cost_per_successful_task"] = _derived(
        "cost_per_successful_task", numerator=cost, denominator=tasks,
        reason="no successful trust-gated task/validated patch or no cost")
    out["seconds_per_successful_task"] = _derived(
        "seconds_per_successful_task", numerator=seconds, denominator=tasks,
        reason="no successful trust-gated task/validated patch")
    out["output_tokens_per_second"] = _derived(
        "output_tokens_per_second", numerator=completion, denominator=seconds,
        reason="completion tokens or duration unavailable")
    out["cache_hit_ratio"] = _derived(
        "cache_hit_ratio", numerator=hit, denominator=prompt,
        reason="cache hit tokens or prompt tokens unavailable")
    out["repair_rate"] = _derived(
        "repair_rate", numerator=repairs,
        denominator=(float(trial_count) if trial_count else None),
        reason="no trials recorded")
    out["review_reject_rate"] = _derived(
        "review_reject_rate", numerator=rejects,
        denominator=(float(reviewed_trials) if reviewed_trials else None),
        reason="no active independent review recorded")
    out["protocol_error_rate"] = _derived(
        "protocol_error_rate", numerator=protocol_errors,
        denominator=(float(reviewed_trials) if reviewed_trials else None),
        reason="no active independent review recorded")
    first_patch = metrics.get("first_useful_patch_ms")
    if first_patch is not None and first_patch.value is not None:
        out["time_to_first_useful_patch_ms"] = model.Metric(
            name="time_to_useful_patch_ms", value=first_patch.value,
            source=first_patch.source)
    else:
        out["time_to_first_useful_patch_ms"] = model.Metric.unavailable(
            "time_to_first_useful_patch_ms", "no useful patch observed")
    return out


def wasted_in_failed_cycles(
    trials: Sequence[bench_model.TrialRecord],
) -> dict[str, Any]:
    """Tokens/cost/time wasted in failed/blocked/cancelled cycles (honest)."""
    failed = [record for record in trials
              if record.status != bench_model.TS_PASS]
    tokens = [record.telemetry.total_tokens for record in failed
              if record.telemetry.total_tokens is not None]
    cost = [record.telemetry.cost_usd for record in failed
            if record.telemetry.cost_usd is not None]
    duration = [record.telemetry.total_duration_ms for record in failed
                if record.telemetry.total_duration_ms is not None]
    return {
        "failed_cycle_count": len(failed),
        "tokens_wasted_in_failed_cycles": (
            sum(tokens) if tokens else None),
        "cost_wasted_in_failed_cycles": (sum(cost) if cost else None),
        "time_wasted_in_failed_cycles_ms": (
            sum(duration) if duration else None),
    }


# --- overhead measurement -----------------------------------------------------


@dataclass(frozen=True)
class OverheadMeasurement:
    """Measured telemetry collection overhead for one mode."""

    mode: str
    collection_ms: float
    sample_count: int

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "collection_ms": self.collection_ms,
                "sample_count": self.sample_count}


def measure_overhead(
    mode: str, collector: Callable[[], object], *,
    clock: Callable[[], float] = time.perf_counter,
) -> OverheadMeasurement:
    """Measure a collector's wall time for the standard/benchmark report."""
    started = clock()
    result = collector()
    elapsed_ms = (clock() - started) * 1000.0
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        samples = len(result)
    else:
        samples = 1
    return OverheadMeasurement(mode=mode, collection_ms=elapsed_ms,
                               sample_count=samples)
