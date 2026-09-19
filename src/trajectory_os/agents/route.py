"""M022 — exact provider/model route identity (provider-neutral).

A route is the exact, machine-readable ``(backend, provider, model,
transport)`` tuple a bounded run targets. It is resolved deterministically
from an explicit request override plus the backend's documented default, and
it carries a domain-separated ``route_id`` so two runs that differ only in
provider or model are never conflated.

Provider-specific model *naming* remains adapter-owned: this module never
rewrites a model name, it only records the exact resolved string.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from trajectory_os.agents import identity as agent_identity
from trajectory_os.agents import model

#: Documented default provider per backend (adapter-owned naming).
DEFAULT_PROVIDER_BY_BACKEND: Mapping[str, str] = {
    model.BACKEND_PI: "ollama",
    model.BACKEND_DEEPSEEK_HARNESS: "deepseek-official",
}

#: Documented default model per backend (adapter-owned naming).
DEFAULT_MODEL_BY_BACKEND: Mapping[str, str] = {
    model.BACKEND_PI: "qwen3.8-dev3090",
    model.BACKEND_DEEPSEEK_HARNESS: "deepseek-flash",
}

MAX_IDENTITY_LEN = 128


@dataclass(frozen=True)
class ProviderRoute:
    """One exact provider/model route with a deterministic identity."""

    backend: str
    provider: str
    model: str
    transport: str | None = None
    route_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "transport": self.transport,
        }

    def compute_route_id(self) -> str:
        return agent_identity.route_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "route_id": self.route_id}

    @staticmethod
    def build(*, backend: str, provider: str, model_name: str,
              transport: str | None = None) -> ProviderRoute:
        if backend not in model.BACKENDS:
            raise model.AgentBackendError(
                model.R_ROUTE_INVALID, "route", f"unknown backend {backend!r}")
        if not provider or len(provider) > MAX_IDENTITY_LEN:
            raise model.AgentBackendError(
                model.R_ROUTE_INVALID, "route", "invalid provider")
        if not model_name or len(model_name) > MAX_IDENTITY_LEN:
            raise model.AgentBackendError(
                model.R_ROUTE_INVALID, "route", "invalid model")
        if transport is not None and transport not in model.TRANSPORTS:
            raise model.AgentBackendError(
                model.R_ROUTE_INVALID, "route",
                f"unknown transport {transport!r}")
        route = ProviderRoute(
            backend=backend, provider=provider, model=model_name,
            transport=transport)
        return replace(route, route_id=route.compute_route_id())


def default_route(backend: str) -> ProviderRoute:
    """Resolve the documented default route for a known backend."""
    if backend not in model.BACKENDS:
        raise model.AgentBackendError(
            model.R_ROUTE_INVALID, "route", f"unknown backend {backend!r}")
    return ProviderRoute.build(
        backend=backend,
        provider=DEFAULT_PROVIDER_BY_BACKEND[backend],
        model_name=DEFAULT_MODEL_BY_BACKEND[backend],
    )


def resolve_route(backend: str, request: model.AgentRequest,
                  *, transport: str | None = None) -> ProviderRoute:
    """Resolve the exact route from an explicit request override + default."""
    if backend not in model.BACKENDS:
        raise model.AgentBackendError(
            model.R_ROUTE_INVALID, "route", f"unknown backend {backend!r}")
    provider = request.provider or DEFAULT_PROVIDER_BY_BACKEND[backend]
    model_name = request.model or DEFAULT_MODEL_BY_BACKEND[backend]
    return ProviderRoute.build(
        backend=backend, provider=provider, model_name=model_name,
        transport=transport)
