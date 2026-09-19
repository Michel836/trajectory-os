"""M029 — deterministic metric aggregation (unavailable-aware).

Aggregation never invents a value. Only metrics whose per-trial provenance is
not ``UNAVAILABLE`` contribute to a statistic; the number of contributing
samples is always reported next to every statistic. A metric with zero
contributing samples yields ``null`` plus an explicit ``reason`` — never a
fabricated zero.

Efficiency ratios are computed from trust-gated outcomes only (``PASS``);
failed/redundant consumption is reported separately so a backend cannot win by
failing cheaply.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

from trajectory_os.benchmark import model

#: Metrics aggregated as continuous quantities.
CONTINUOUS_METRICS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_read_tokens", "cache_hit_tokens", "cache_miss_tokens",
    "cache_hit_ratio", "cost_usd", "total_duration_ms", "request_latency_ms",
    "ttft_ms", "first_useful_patch_ms", "resume_duration_ms", "prompt_tps",
    "generation_tps", "cpu_percent", "ram_bytes", "gpu_utilization_pct",
    "vram_bytes", "gpu_power_w", "gpu_temp_c",
)

#: Metrics aggregated as integer counters (summed).
COUNTER_METRICS = (
    "request_count", "retries", "repairs", "review_rejects",
    "protocol_errors", "validation_failures",
)

_UNAVAILABLE_REASON = "no trial exposed this metric"


def _values(records: Iterable[model.TrialRecord],
            metric: str) -> list[float]:
    out: list[float] = []
    for record in records:
        if record.telemetry.sources.get(metric) == model.SRC_UNAVAILABLE:
            continue
        value = getattr(record.telemetry, metric)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out.append(float(value))
    return out


def _stat(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "total": None, "mean": None, "median": None,
                "p95": None, "min": None, "max": None,
                "reason": _UNAVAILABLE_REASON}
    ordered = sorted(values)
    count = len(ordered)
    index = max(0, math.ceil(0.95 * count) - 1)
    return {
        "count": count,
        "total": sum(ordered),
        "mean": sum(ordered) / count,
        "median": _median(ordered),
        "p95": ordered[index],
        "min": ordered[0],
        "max": ordered[-1],
        "reason": None,
    }


def _median(ordered: Sequence[float]) -> float:
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _counter(records: Iterable[model.TrialRecord], metric: str) -> int:
    return sum(int(getattr(record.telemetry, metric) or 0)
               for record in records)


def _outcomes(records: Sequence[model.TrialRecord]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(model.TRIAL_STATUSES)}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    return counts


def _efficiency(records: Sequence[model.TrialRecord]) -> dict[str, Any]:
    passes = [r for r in records if r.status in model.SUCCESS_STATUSES]
    non_pass = [r for r in records if r.status not in model.SUCCESS_STATUSES]
    success_count = len(passes)
    result: dict[str, Any] = {
        "successful_trials": success_count,
        "failed_or_blocked_trials": len(non_pass),
    }
    result["tokens_per_success"] = _ratio(
        _available_total(passes, "total_tokens"), success_count,
        "no successful trial exposed total_tokens")
    result["cost_per_success"] = _ratio(
        _available_total(passes, "cost_usd"), success_count,
        "no successful trial exposed cost")
    result["seconds_per_success"] = _ratio(
        _available_total_ms(passes, "total_duration_ms"), success_count,
        "no successful trial exposed duration")
    result["failed_tokens"] = _available_total(non_pass, "total_tokens")
    result["failed_cost"] = _available_total(non_pass, "cost_usd")
    result["failed_seconds"] = _available_total_ms(non_pass,
                                                   "total_duration_ms")
    return result


def _available_total(records: Sequence[model.TrialRecord],
                     metric: str) -> float | None:
    values = _values(records, metric)
    if not values or len(values) < len(records):
        # Only report a success-efficiency ratio when *every* contributing
        # trial exposed the metric; otherwise the ratio would be biased.
        return None
    return float(sum(values))


def _available_total_ms(records: Sequence[model.TrialRecord],
                        metric: str) -> float | None:
    total = _available_total(records, metric)
    return None if total is None else total / 1000


def _ratio(numerator: float | None, denominator: int,
           reason: str) -> dict[str, Any]:
    if numerator is None or denominator <= 0:
        return {"value": None, "reason": reason}
    return {"value": numerator / denominator, "reason": None}


def summarize(records: Sequence[model.TrialRecord]) -> dict[str, Any]:
    """Aggregate a sequence of trial records (deterministic, unavailable-aware)."""
    continuous: dict[str, Any] = {
        metric: _stat(_values(records, metric))
        for metric in CONTINUOUS_METRICS
    }
    counters: dict[str, Any] = {
        metric: _counter(records, metric) for metric in COUNTER_METRICS
    }
    cache_hit = _values(records, "cache_hit_tokens")
    cache_prompt = _values(records, "prompt_tokens")
    if cache_hit and cache_prompt and len(cache_hit) == len(cache_prompt):
        cache_ratio: dict[str, Any] = {
            "value": sum(cache_hit) / sum(cache_prompt)
            if sum(cache_prompt) > 0 else None,
            "reason": None,
        }
    else:
        cache_ratio = {"value": None,
                       "reason": "cache/prompt samples incomplete"}
    return {
        "trials": len(records),
        "outcomes": _outcomes(records),
        "metrics": continuous,
        "counters": counters,
        "cache": {"hit_tokens": cache_hit, "prompt_tokens": cache_prompt,
                  "aggregate_hit_ratio": cache_ratio},
        "efficiency": _efficiency(records),
    }


def group_by_backend(
    records: Sequence[model.TrialRecord],
) -> dict[str, list[model.TrialRecord]]:
    grouped: dict[str, list[model.TrialRecord]] = {}
    for record in records:
        grouped.setdefault(record.backend, []).append(record)
    return grouped


def group_by_backend_workload(
    records: Sequence[model.TrialRecord],
) -> dict[str, dict[str, list[model.TrialRecord]]]:
    grouped: dict[str, dict[str, list[model.TrialRecord]]] = {}
    for record in records:
        grouped.setdefault(record.backend, {}).setdefault(
            record.workload_id, []).append(record)
    return grouped


def build_summary(
    records: Sequence[model.TrialRecord],
    *,
    benchmark_run_id: str,
    mode: str,
) -> dict[str, Any]:
    """Build the canonical aggregate summary document."""
    by_backend = {
        backend: summarize(items)
        for backend, items in sorted(group_by_backend(records).items())
    }
    by_workload: dict[str, Any] = {}
    for backend, workloads in sorted(group_by_backend_workload(records).items()):
        by_workload[backend] = {
            workload_id: summarize(items)
            for workload_id, items in sorted(workloads.items())
        }
    return {
        "schema_version": model.SCHEMA_VERSION,
        "benchmark_version": model.BENCHMARK_VERSION,
        "benchmark_run_id": benchmark_run_id,
        "mode": mode,
        "overall": summarize(records),
        "by_backend": by_backend,
        "by_backend_workload": by_workload,
    }


def cache_behavior(records: Sequence[model.TrialRecord]) -> dict[str, Any]:
    """Context-efficiency comparison (stable-prefix vs full-context reuse)."""
    prompt = _values(records, "prompt_tokens")
    cache_hit = _values(records, "cache_hit_tokens")
    cache_miss = _values(records, "cache_miss_tokens")
    requests = _values(records, "request_count")
    return {
        "prompt_samples": len(prompt),
        "cache_hit_samples": len(cache_hit),
        "cache_miss_samples": len(cache_miss),
        "repeated_full_context_requests": (
            sum(requests) if requests else None),
        "cache_hit_tokens_total": (sum(cache_hit) if cache_hit else None),
        "cache_miss_tokens_total": (sum(cache_miss) if cache_miss else None),
    }
