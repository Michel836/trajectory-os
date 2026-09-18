"""Mission 015 — agent-backend registry and deterministic fallback.

Backend selection is explicit and bounded. A primary harness that is
unavailable, incompatible or does not produce reliable completion evidence is
deterministically replaced by the proven Pi backend, and the fallback
provenance is recorded in the result.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from trajectory_os.agents import model
from trajectory_os.agents.contract import AgentBackend
from trajectory_os.agents.deepseek_harness import DeepSeekHarnessBackend
from trajectory_os.agents.pi import PiBackend

BackendFactory = Callable[[str], AgentBackend]


def make_backend(name: str, **kwargs: Any) -> AgentBackend:
    """Construct one backend by stable name (fail closed on unknown names)."""
    if name == model.BACKEND_PI:
        return PiBackend(**kwargs)
    if name == model.BACKEND_DEEPSEEK_HARNESS:
        return DeepSeekHarnessBackend(**kwargs)
    raise model.AgentBackendError(
        "UNKNOWN_BACKEND", "backend", f"unknown backend: {name!r}")


def probe_all(factory: BackendFactory | None = None) -> dict[str, Any]:
    """Deterministic capability probes for every known backend."""
    build = factory or (lambda name: make_backend(name))
    return {name: build(name).probe().to_dict()
            for name in sorted(model.BACKENDS)}


def run_with_fallback(
    request: model.AgentRequest,
    *,
    primary: str = model.BACKEND_DEEPSEEK_HARNESS,
    fallback: str = model.BACKEND_PI,
    factory: BackendFactory | None = None,
    cancel: object | None = None,
) -> model.AgentResult:
    """Run ``primary`` and deterministically fall back to ``fallback``.

    A fallback result always records ``fallback_from=<primary>`` so provenance
    is never lost. The function never merges, promotes or reinterprets a
    failed result as success.
    """
    if primary == fallback:
        raise model.AgentBackendError(
            "INVALID_FALLBACK", "backend", "primary and fallback are identical")
    build = factory or (lambda name: make_backend(name))
    primary_backend = build(primary)
    probe = primary_backend.probe()
    if probe.available:
        result = primary_backend.run(request, cancel=cancel)
        if result.completed and completion_reliable(result):
            return result
    else:
        result = model.AgentResult.build(
            backend=primary, status=model.RS_UNAVAILABLE, reason=probe.reason,
            error=probe.detail, transport=probe.transport)
    return _fallback(request, result, primary, fallback, build, cancel)


def _fallback(
    request: model.AgentRequest,
    primary_result: model.AgentResult,
    primary: str,
    fallback: str,
    build: BackendFactory,
    cancel: object | None,
) -> model.AgentResult:
    fallback_backend = build(fallback)
    result = fallback_backend.run(request, cancel=cancel)
    return replace(result, fallback_from=primary,
                   error=(result.error
                          or f"primary {primary}: {primary_result.reason}"))


def completion_reliable(result: model.AgentResult) -> bool:
    return result.completion is not None and result.completion.reliable


def comparison_summary(result: model.AgentResult) -> Mapping[str, Any]:
    """Bounded, provider-agnostic comparison facts for one run."""
    return {
        "backend": result.backend,
        "status": result.status,
        "reason": result.reason,
        "transport": result.transport,
        "completion_source": (None if result.completion is None
                              else result.completion.source),
        "completion_reliable": completion_reliable(result),
        "lifecycle_evidence_count": len(result.events),
        "repair_count": 0,
        "runtime_ms": result.runtime_ms,
        "fallback_from": result.fallback_from,
        "error": result.error,
    }
