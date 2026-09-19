"""M022 — deterministic agent-backend capability discovery.

Capability discovery never launches a model. It probes the backend's
deterministic availability and derives a closed, machine-readable capability
set, so an unavailable or unknown backend yields an explicit report instead of
an exception or an optimistic assumption.

Capabilities are statements about the provider-neutral contract (structured
lifecycle, events, timeout, cancellation, bounded retry, deterministic
fallback, subagents), not about a specific model route.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from trajectory_os.agents import identity as agent_identity
from trajectory_os.agents import model
from trajectory_os.agents.contract import AgentBackend

CAP_STRUCTURED_LIFECYCLE = "STRUCTURED_LIFECYCLE"
CAP_LIFECYCLE_EVENTS = "LIFECYCLE_EVENTS"
CAP_TIMEOUT = "TIMEOUT"
CAP_CANCELLATION = "CANCELLATION"
CAP_BOUNDED_RETRY = "BOUNDED_RETRY"
CAP_DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"
CAP_SUBAGENTS = "SUBAGENTS"

CAPABILITIES = frozenset({
    CAP_STRUCTURED_LIFECYCLE, CAP_LIFECYCLE_EVENTS, CAP_TIMEOUT,
    CAP_CANCELLATION, CAP_BOUNDED_RETRY, CAP_DETERMINISTIC_FALLBACK,
    CAP_SUBAGENTS,
})

#: Documented capability baseline per backend (never inferred from a run).
_BACKEND_CAPABILITIES: Mapping[str, tuple[str, ...]] = {
    model.BACKEND_PI: (
        CAP_LIFECYCLE_EVENTS, CAP_TIMEOUT, CAP_CANCELLATION,
        CAP_BOUNDED_RETRY, CAP_DETERMINISTIC_FALLBACK,
    ),
    model.BACKEND_DEEPSEEK_HARNESS: (
        CAP_STRUCTURED_LIFECYCLE, CAP_LIFECYCLE_EVENTS, CAP_TIMEOUT,
        CAP_CANCELLATION, CAP_BOUNDED_RETRY, CAP_DETERMINISTIC_FALLBACK,
        CAP_SUBAGENTS,
    ),
}


@dataclass(frozen=True)
class CapabilityReport:
    """One deterministic backend capability report (never launches a model)."""

    backend: str
    available: bool
    reason: str
    detail: str
    transport: str | None
    capabilities: tuple[str, ...]
    report_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "backend": self.backend,
            "available": self.available,
            "reason": self.reason,
            "transport": self.transport,
            "capabilities": list(self.capabilities),
        }

    def compute_report_id(self) -> str:
        return agent_identity.capability_id(self.identity_payload())

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "detail": self.detail,
                "report_id": self.report_id}

    @staticmethod
    def build(*, backend: str, available: bool, reason: str, detail: str = "",
              transport: str | None = None,
              capabilities: tuple[str, ...] = ()) -> CapabilityReport:
        if reason not in model.REASON_CODES:
            raise model.AgentBackendError(
                "MALFORMED_AGENT", "capabilities",
                f"unknown reason {reason!r}")
        for capability in capabilities:
            if capability not in CAPABILITIES:
                raise model.AgentBackendError(
                    "MALFORMED_AGENT", "capabilities",
                    f"unknown capability {capability!r}")
        report = CapabilityReport(
            backend=backend, available=available, reason=reason,
            detail=detail, transport=transport,
            capabilities=tuple(sorted(set(capabilities))))
        return replace(report, report_id=report.compute_report_id())


def discover(backend: AgentBackend) -> CapabilityReport:
    """Probe and classify one backend without launching a model."""
    probe = backend.probe()
    name = getattr(backend, "name", probe.backend)
    if not probe.available:
        return CapabilityReport.build(
            backend=name, available=False, reason=probe.reason,
            detail=probe.detail, transport=probe.transport, capabilities=())
    capabilities = _BACKEND_CAPABILITIES.get(name)
    if capabilities is None:
        return CapabilityReport.build(
            backend=name, available=True, reason=model.R_CAPABILITY_UNKNOWN,
            detail=(probe.detail or "no documented capability baseline"),
            transport=probe.transport, capabilities=())
    return CapabilityReport.build(
        backend=name, available=True, reason=model.R_OK, detail=probe.detail,
        transport=probe.transport, capabilities=capabilities)


def discover_all(factory: Callable[[str], AgentBackend] | None = None,
                 ) -> dict[str, Any]:
    """Deterministic capability discovery for every known backend."""
    if factory is None:
        from trajectory_os.agents import registry as agent_registry
        factory = lambda name: agent_registry.make_backend(name)  # noqa: E731
    return {name: discover(factory(name)).to_dict()
            for name in sorted(model.BACKENDS)}
