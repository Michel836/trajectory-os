"""Mission 015 — bounded DeepSeek Harness canary with Pi fallback.

The canary attempts at most one representative bounded task against the
primary backend only when the runtime/SDK and credentials are safely
available. When the official Python SDK is absent (the current official
package name is ``deepseek-harness-sdk`` / module ``deepseek_harness``) the
canary deterministically records ``CANARY_UNAVAILABLE`` with the reason and
preserves the fallback evidence; it never weakens validation or review gates
and never commits, pushes or merges.
"""

from __future__ import annotations

from typing import Any

from trajectory_os.agents import model, registry
from trajectory_os.agents.contract import AgentBackend
from trajectory_os.agents.registry import BackendFactory


def run_canary(
    request: model.AgentRequest,
    *,
    primary: str = model.BACKEND_DEEPSEEK_HARNESS,
    fallback: str = model.BACKEND_PI,
    require_sdk: bool = True,
    factory: BackendFactory | None = None,
    probe_fallback: bool = True,
) -> model.CanaryOutcome:
    """Run one bounded backend canary (never a full mission)."""
    build = factory or (lambda name: registry.make_backend(name))
    primary_backend: AgentBackend = build(primary)
    probe = primary_backend.probe()
    fallback_probe = (build(fallback).probe().identity_payload()
                      if probe_fallback else None)
    base_comparison: dict[str, Any] = {
        "primary_probe": probe.identity_payload(),
        "fallback_probe": fallback_probe,
        "primary_transport": probe.transport,
        "sdk_available": probe.sdk_version is not None,
        "runtime_available": probe.available,
    }

    if require_sdk and probe.sdk_version is None:
        primary_result = model.AgentResult.build(
            backend=primary, status=model.RS_UNAVAILABLE,
            reason=model.R_SDK_MISSING,
            error=("official DeepSeek Harness Python SDK not installed "
                   "(deepseek-harness-sdk / deepseek_harness)"),
            transport=probe.transport)
        return model.CanaryOutcome.build(
            status=model.CS_CANARY_UNAVAILABLE, reason=model.R_SDK_MISSING,
            primary_backend=primary, fallback_backend=fallback,
            primary=primary_result,
            comparison={**base_comparison, **registry.comparison_summary(
                primary_result)})

    if not probe.available:
        primary_result = model.AgentResult.build(
            backend=primary, status=model.RS_UNAVAILABLE, reason=probe.reason,
            error=probe.detail, transport=probe.transport)
        return model.CanaryOutcome.build(
            status=model.CS_CANARY_UNAVAILABLE, reason=probe.reason,
            primary_backend=primary, fallback_backend=fallback,
            primary=primary_result,
            comparison={**base_comparison, **registry.comparison_summary(
                primary_result)})

    result = primary_backend.run(request)
    comparison = {**base_comparison, **registry.comparison_summary(result)}
    if result.status == model.RS_INCOMPATIBLE:
        status = model.CS_CANARY_INCOMPATIBLE
    elif result.status in (model.RS_UNAVAILABLE, model.RS_TIMEOUT,
                           model.RS_CANCELLED):
        status = model.CS_CANARY_UNAVAILABLE
    elif result.completed and registry.completion_reliable(result):
        status = model.CS_CANARY_PASS
    else:
        status = model.CS_CANARY_FAIL
    reason = (model.R_CANARY_PASS if status == model.CS_CANARY_PASS
              else result.reason)
    return model.CanaryOutcome.build(
        status=status, reason=reason, primary_backend=primary,
        fallback_backend=fallback, primary=result,
        comparison=comparison)
