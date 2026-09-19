"""M029 — per-trial telemetry assembly (provider-grounded, never invented).

This module composes the M026 provider-grounded token/context metrics
(:mod:`trajectory_os.agents.telemetry`) with benchmark-local timing and
resource attribution. Every metric carries an explicit provenance; a metric
the backend/provider does not expose stays ``None`` with an explicit reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from trajectory_os.agents import model as agent_model
from trajectory_os.agents import telemetry as agent_telemetry
from trajectory_os.benchmark import model

#: Resource scopes (closed set).
SCOPE_REMOTE = "remote_inference"
SCOPE_LOCAL_REVIEWER = "local_reviewer"
SCOPE_LOCAL_AGENT = "local_agent"
SCOPE_MIXED = "mixed"
SCOPE_UNAVAILABLE = "unavailable"

SCOPES = frozenset({
    SCOPE_REMOTE, SCOPE_LOCAL_REVIEWER, SCOPE_LOCAL_AGENT, SCOPE_MIXED,
    SCOPE_UNAVAILABLE,
})


@dataclass(frozen=True)
class ResourceSample:
    """One bounded local resource sample (never a fabricated number)."""

    scope: str = SCOPE_UNAVAILABLE
    cpu_percent: float | None = None
    ram_bytes: int | None = None
    gpu_utilization_pct: int | None = None
    vram_bytes: int | None = None
    gpu_power_w: float | None = None
    gpu_temp_c: float | None = None
    reasons: Mapping[str, str] = field(default_factory=dict)

    @staticmethod
    def unavailable(reason: str) -> ResourceSample:
        fields = ("cpu_percent", "ram_bytes", "gpu_utilization_pct",
                  "vram_bytes", "gpu_power_w", "gpu_temp_c")
        return ResourceSample(
            scope=SCOPE_UNAVAILABLE,
            reasons={name: reason for name in fields})


def empty_resources() -> ResourceSample:
    return ResourceSample.unavailable(
        "no local resource sample captured for this trial")


def local_resource_sampler(
    model_name: str,
) -> Any:
    """Build a bounded read-only local resource sampler (fail-soft).

    Remote inference is explicitly distinguished from local reviewer GPU
    consumption: a remote backend's sample is scoped ``remote_inference``
    even though the GPU values describe the local machine.
    """
    from trajectory_os.resources import probe as resource_probe

    def sample(backend: str) -> ResourceSample:
        report = resource_probe.discover()
        remote = model.backend_locality(backend, model_name) == "remote"
        scope = (SCOPE_REMOTE if remote else SCOPE_LOCAL_AGENT)
        reasons = {
            "cpu_percent": "read-only probe does not expose CPU utilization",
            "gpu_power_w": "not exposed by the bounded nvidia-smi query",
            "gpu_temp_c": "not exposed by the bounded nvidia-smi query",
        }
        if report.ram_bytes is None:
            reasons["ram_bytes"] = "read-only probe reports total RAM only"
        if report.gpu_utilization_pct is None:
            reasons["gpu_utilization_pct"] = "no local GPU telemetry available"
        if report.gpu_mem_used_bytes is None:
            reasons["vram_bytes"] = "no local GPU telemetry available"
        return ResourceSample(
            scope=scope, cpu_percent=None, ram_bytes=report.ram_bytes,
            gpu_utilization_pct=report.gpu_utilization_pct,
            vram_bytes=report.gpu_mem_used_bytes,
            gpu_power_w=None, gpu_temp_c=None, reasons=reasons)

    return sample


def _override_resources(
    telemetry: model.TrialTelemetry, resources: ResourceSample,
) -> model.TrialTelemetry:
    result = replace(
        telemetry,
        cpu_percent=resources.cpu_percent,
        ram_bytes=resources.ram_bytes,
        gpu_utilization_pct=resources.gpu_utilization_pct,
        vram_bytes=resources.vram_bytes,
        gpu_power_w=resources.gpu_power_w,
        gpu_temp_c=resources.gpu_temp_c,
        resource_scope=resources.scope)
    overrides: dict[str, str] = {}
    for name in ("cpu_percent", "ram_bytes", "gpu_utilization_pct",
                 "vram_bytes", "gpu_power_w", "gpu_temp_c"):
        value = getattr(resources, name)
        if value is not None:
            overrides[name] = model.SRC_LOCAL
    result = model.with_sources(result, overrides)
    unavailable = dict(result.unavailable)
    for name in ("cpu_percent", "ram_bytes", "gpu_utilization_pct",
                 "vram_bytes", "gpu_power_w", "gpu_temp_c"):
        if getattr(resources, name) is None:
            unavailable[name] = resources.reasons.get(
                name, "local resource not exposed")
        else:
            unavailable.pop(name, None)
    result = replace(result, unavailable=unavailable)
    model.validate_telemetry(result)
    return result


def from_agent_result(
    result: agent_model.AgentResult,
    *,
    provider: str | None,
    model_name: str | None,
    wall_ms: int | None = None,
    first_useful_patch_ms: int | None = None,
    resume_duration_ms: int | None = None,
    ttft_ms: int | None = None,
    phase_durations: Sequence[tuple[str, int]] = (),
    retries: int = 0,
    repairs: int = 0,
    review_rejects: int = 0,
    protocol_errors: int = 0,
    validation_failures: int = 0,
    resources: ResourceSample | None = None,
) -> model.TrialTelemetry:
    """Assemble one trial's authoritative telemetry (pure).

    Raw provider token/context values come from the M026 extractor; timing and
    resource values are benchmark-local. Unavailable metrics are explicit.
    """
    usage = agent_telemetry.from_result(
        result, provider=provider, model=model_name, retries=retries,
        repair_rounds=repairs)
    prompt = usage.prompt_tokens
    completion = usage.completion_tokens
    total = usage.total_tokens
    cache_read = usage.cache_read_tokens
    cost = usage.cost_usd
    total_duration_ms = wall_ms if wall_ms is not None else usage.runtime_ms
    request_count = usage.request_count

    cache_hit_tokens = cache_read
    if prompt is not None and cache_read is not None:
        cache_miss_tokens: int | None = max(0, prompt - cache_read)
        cache_hit_ratio: float | None = (
            (cache_read / prompt) if prompt > 0 else None)
    else:
        cache_miss_tokens = None
        cache_hit_ratio = None

    if total_duration_ms is not None and request_count == 1:
        request_latency_ms: int | None = total_duration_ms
    else:
        request_latency_ms = None

    seconds = (total_duration_ms / 1000) if total_duration_ms else None
    if prompt is not None and seconds:
        prompt_tps: float | None = prompt / seconds
    else:
        prompt_tps = None
    if completion is not None and seconds:
        generation_tps: float | None = completion / seconds
    else:
        generation_tps = None

    telemetry = model.TrialTelemetry(
        request_count=request_count, prompt_tokens=prompt,
        completion_tokens=completion, total_tokens=total,
        cache_read_tokens=cache_read, cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens, cache_hit_ratio=cache_hit_ratio,
        cost_usd=cost, total_duration_ms=total_duration_ms,
        request_latency_ms=request_latency_ms, ttft_ms=ttft_ms,
        first_useful_patch_ms=first_useful_patch_ms,
        resume_duration_ms=resume_duration_ms, prompt_tps=prompt_tps,
        generation_tps=generation_tps, retries=retries, repairs=repairs,
        review_rejects=review_rejects, protocol_errors=protocol_errors,
        validation_failures=validation_failures,
        phase_durations=tuple(
            model.PhaseDuration(phase=phase, duration_ms=duration)
            for phase, duration in phase_durations),
        cpu_percent=None, ram_bytes=None, gpu_utilization_pct=None,
        vram_bytes=None, gpu_power_w=None, gpu_temp_c=None,
        resource_scope=(resources.scope if resources is not None
                        else SCOPE_UNAVAILABLE))

    overrides: dict[str, str] = {}
    if prompt is not None:
        overrides["prompt_tokens"] = usage.sources.get(
            "prompt_tokens", model.SRC_PROVIDER)
    if completion is not None:
        overrides["completion_tokens"] = usage.sources.get(
            "completion_tokens", model.SRC_PROVIDER)
    if total is not None:
        overrides["total_tokens"] = usage.sources.get(
            "total_tokens", model.SRC_DERIVED)
    if cache_read is not None:
        overrides["cache_read_tokens"] = model.SRC_PROVIDER
        overrides["cache_hit_tokens"] = model.SRC_PROVIDER
    if cache_miss_tokens is not None:
        overrides["cache_miss_tokens"] = model.SRC_DERIVED
    if cache_hit_ratio is not None:
        overrides["cache_hit_ratio"] = model.SRC_DERIVED
    if cost is not None:
        overrides["cost_usd"] = usage.sources.get(
            "cost_usd", model.SRC_PROVIDER)
    if total_duration_ms is not None:
        overrides["total_duration_ms"] = model.SRC_LOCAL
    if request_latency_ms is not None:
        overrides["request_latency_ms"] = model.SRC_DERIVED
    if ttft_ms is not None:
        overrides["ttft_ms"] = model.SRC_LOCAL
    if first_useful_patch_ms is not None:
        overrides["first_useful_patch_ms"] = model.SRC_LOCAL
    if resume_duration_ms is not None:
        overrides["resume_duration_ms"] = model.SRC_LOCAL
    if prompt_tps is not None:
        overrides["prompt_tps"] = model.SRC_DERIVED
    if generation_tps is not None:
        overrides["generation_tps"] = model.SRC_DERIVED

    telemetry = model.with_sources(telemetry, overrides)
    unavailable = dict(telemetry.unavailable)
    for name in model.TELEMETRY_FIELDS:
        if telemetry.sources.get(name) == model.SRC_UNAVAILABLE:
            unavailable.setdefault(
                name, "backend/provider does not expose this metric")
    telemetry = replace(telemetry, unavailable=unavailable)
    if resources is not None:
        telemetry = _override_resources(telemetry, resources)
    model.validate_telemetry(telemetry)
    return telemetry
