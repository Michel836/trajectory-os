"""M045 — consolidated backend/reviewer routing and provenance.

One deterministic :class:`RoutingDecision` persists the exact implementation,
inline-review and final-review identities **before** any work runs:

    implementation : backend / provider / model
    inline review  : enabled / backend / provider / model
    final review   : enabled / backend / provider / model

Invariants (fail closed):

* no phantom reviewer — a disabled role exposes no model at all;
* an unavailable backend fails closed unless the policy explicitly allows a
  fallback, and the fallback order is deterministic and persisted;
* the final independent reviewer stays exactly ``qwen3.8:27b-q4_K_M`` unless
  the operator recorded an explicit override — it is never silently swapped
  to the legacy ``qwen3.6`` alias;
* Pi + DeepSeek Flash and local Ollama both remain valid implementation
  routes; the Harness remains developer-preview (no benchmark claim here);
* the routing decision is persisted before use and every fallback records its
  reason.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from trajectory_os.agents import model as agent_model
from trajectory_os.agents import route as agent_route
from trajectory_os.observability import model as obs_model
from trajectory_os.operator import model
from trajectory_os.operator._util import (
    append_jsonl,
    optional_str,
    read_jsonl,
    utc_now,
)
from trajectory_os.operator.policy import (
    CANONICAL_FINAL_REVIEWER,
    LEGACY_PHANTOM_REVIEWER,
    ResolvedPolicy,
)

ROLE_IMPLEMENTATION = "IMPLEMENTATION"
ROLE_INLINE_REVIEW = "INLINE_REVIEW"
ROLE_FINAL_REVIEW = "FINAL_REVIEW"

ROUTING_ROLES = frozenset({
    ROLE_IMPLEMENTATION, ROLE_INLINE_REVIEW, ROLE_FINAL_REVIEW,
})

#: Reviewer backends use their own local namespace (Ollama by default).
REVIEWER_BACKENDS = frozenset({"ollama", *agent_model.BACKENDS})

#: Routing identity domain.
ROUTING_DOMAIN = "trajectory-os.operator-routing.v1"

#: Harness capability label (developer preview; never a performance claim).
HARNESS_PREVIEW = "developer-preview"


@dataclass(frozen=True)
class BackendCapability:
    """One explicit backend/provider/model capability declaration."""

    backend: str
    provider: str | None
    model: str | None
    available: bool
    supports_implementation: bool
    supports_review: bool
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "available": self.available,
            "supports_implementation": self.supports_implementation,
            "supports_review": self.supports_review,
            "reason": self.reason,
            "detail": self.detail,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> BackendCapability:
        return BackendCapability(
            backend=str(data.get("backend", "")),
            provider=optional_str(data.get("provider")),
            model=optional_str(data.get("model")),
            available=bool(data.get("available", False)),
            supports_implementation=bool(
                data.get("supports_implementation", False)),
            supports_review=bool(data.get("supports_review", False)),
            reason=str(data.get("reason", "")),
            detail=str(data.get("detail", "")),
        )


@dataclass(frozen=True)
class RouteSpec:
    """One role-explicit route (a disabled role never carries a model)."""

    role: str
    enabled: bool
    backend: str | None
    provider: str | None
    model: str | None
    route_id: str

    def validate(self) -> RouteSpec:
        if self.role not in ROUTING_ROLES:
            model.fail(model.E_ROUTING_INVALID, f"role {self.role!r}")
        if self.enabled and not (self.backend and self.model):
            model.fail(model.E_ROUTING_INVALID,
                       f"{self.role} enabled without backend/model")
        if not self.enabled and any(
                (self.backend, self.provider, self.model)):
            model.fail(model.E_ROUTING_PHANTOM_REVIEWER,
                       f"disabled {self.role} carries an identity")
        return self

    @property
    def display_model(self) -> str | None:
        """Only an enabled role exposes a model (never a phantom)."""
        return self.model if self.enabled else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "enabled": self.enabled,
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "display_model": self.display_model,
            "route_id": self.route_id,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> RouteSpec:
        return RouteSpec(
            role=str(data.get("role", "")),
            enabled=bool(data.get("enabled", False)),
            backend=optional_str(data.get("backend")),
            provider=optional_str(data.get("provider")),
            model=optional_str(data.get("model")),
            route_id=str(data.get("route_id", "")),
        ).validate()

    @staticmethod
    def disabled(role: str, *, reason_ignored: str = "") -> RouteSpec:
        return RouteSpec(role=role, enabled=False, backend=None,
                         provider=None, model=None, route_id="").validate()

    @staticmethod
    def enabled_for(role: str, *, backend: str, provider: str | None,
                    model_name: str) -> RouteSpec:
        route_id = model_digest(role, backend, provider, model_name)
        return RouteSpec(role=role, enabled=True, backend=backend,
                         provider=provider, model=model_name,
                         route_id=route_id).validate()


def model_digest(role: str, backend: str | None, provider: str | None,
                 model_name: str | None) -> str:
    from trajectory_os.operator._util import digest

    return digest(
        {"role": role, "backend": backend, "provider": provider,
         "model": model_name}, domain=ROUTING_DOMAIN)


@dataclass(frozen=True)
class RoutingDecision:
    """The persisted routing decision for one mission."""

    mission_id: str
    policy_id: str
    profile: str
    implementation: RouteSpec
    inline_review: RouteSpec
    final_review: RouteSpec
    fallback_used: bool
    fallback_reason: str | None
    fallback_from: str | None
    fallback_to: str | None
    capabilities: Mapping[str, Any]
    decided_at: str
    schema_version: int = model.SCHEMA_VERSION
    operator_version: str = model.OPERATOR_VERSION

    def validate(self) -> RoutingDecision:
        if not self.mission_id:
            model.fail(model.E_ROUTING_INVALID, "mission_id required")
        self.implementation.validate()
        self.inline_review.validate()
        self.final_review.validate()
        if self.fallback_used and not self.fallback_reason:
            model.fail(model.E_ROUTING_INVALID,
                       "fallback used without a persisted reason")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operator_version": self.operator_version,
            "mission_id": self.mission_id,
            "policy_id": self.policy_id,
            "profile": self.profile,
            "implementation": self.implementation.to_dict(),
            "inline_review": self.inline_review.to_dict(),
            "final_review": self.final_review.to_dict(),
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "fallback_from": self.fallback_from,
            "fallback_to": self.fallback_to,
            "capabilities": dict(sorted(self.capabilities.items())),
            "decided_at": self.decided_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> RoutingDecision:
        capabilities = data.get("capabilities")
        return RoutingDecision(
            mission_id=str(data.get("mission_id", "")),
            policy_id=str(data.get("policy_id", "")),
            profile=str(data.get("profile", "")),
            implementation=RouteSpec.from_dict(
                _mapping(data.get("implementation"))),
            inline_review=RouteSpec.from_dict(
                _mapping(data.get("inline_review"))),
            final_review=RouteSpec.from_dict(
                _mapping(data.get("final_review"))),
            fallback_used=bool(data.get("fallback_used", False)),
            fallback_reason=optional_str(data.get("fallback_reason")),
            fallback_from=optional_str(data.get("fallback_from")),
            fallback_to=optional_str(data.get("fallback_to")),
            capabilities=(dict(capabilities)
                          if isinstance(capabilities, Mapping) else {}),
            decided_at=str(data.get("decided_at", "")),
            schema_version=int(data.get("schema_version",
                                        model.SCHEMA_VERSION)),
            operator_version=str(data.get("operator_version",
                                          model.OPERATOR_VERSION)),
        ).validate()


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _capability_for(
    backend: str, *,
    capabilities: Mapping[str, BackendCapability],
    default: BackendCapability,
) -> BackendCapability:
    declared = capabilities.get(backend)
    return declared if declared is not None else default


def _implementation_default(policy: ResolvedPolicy) -> BackendCapability:
    return BackendCapability(
        backend=policy.policy.implementation_backend,
        provider=policy.policy.implementation_provider,
        model=policy.policy.implementation_model,
        available=True, supports_implementation=True, supports_review=False,
        reason="policy-declared implementation route")


def _fallback_default(backend: str) -> BackendCapability:
    try:
        route = agent_route.default_route(backend)
    except agent_model.AgentBackendError:
        return BackendCapability(
            backend=backend, provider=None, model=None, available=False,
            supports_implementation=False, supports_review=False,
            reason=model.E_ROUTING_INVALID)
    detail = HARNESS_PREVIEW if backend == (
        agent_model.BACKEND_DEEPSEEK_HARNESS) else ""
    return BackendCapability(
        backend=backend, provider=route.provider, model=route.model,
        available=True, supports_implementation=True, supports_review=False,
        reason="deterministic fallback route", detail=detail)


def _resolve_implementation(
    policy: ResolvedPolicy,
    capabilities: Mapping[str, BackendCapability],
) -> tuple[BackendCapability, bool, str | None, str | None, str | None]:
    primary = _capability_for(
        policy.policy.implementation_backend, capabilities=capabilities,
        default=_implementation_default(policy))
    if primary.available and primary.supports_implementation:
        return primary, False, None, None, None
    if not policy.policy.fallback_allowed:
        model.fail(
            model.E_ROUTING_UNAVAILABLE,
            f"backend {policy.policy.implementation_backend!r} unavailable: "
            f"{primary.reason}")
    for backend in policy.policy.fallback_order:
        if backend == policy.policy.implementation_backend:
            continue
        candidate = _capability_for(backend, capabilities=capabilities,
                                    default=_fallback_default(backend))
        if candidate.available and candidate.supports_implementation:
            reason = (f"primary {policy.policy.implementation_backend!r} "
                      f"unavailable ({primary.reason}); fell back to "
                      f"{backend!r}")
            return (candidate, True, reason,
                    policy.policy.implementation_backend, backend)
    model.fail(
        model.E_ROUTING_FALLBACK_DENIED,
        "no available fallback backend in the deterministic order")
    raise AssertionError("unreachable")  # pragma: no cover


def _resolve_reviewer(
    role: str, *,
    enabled: bool,
    backend: str,
    provider: str | None,
    model_name: str,
    capabilities: Mapping[str, BackendCapability],
) -> RouteSpec:
    if not enabled:
        return RouteSpec.disabled(role)
    declared = capabilities.get(backend)
    if declared is not None and not declared.available:
        model.fail(
            model.E_ROUTING_UNAVAILABLE,
            f"reviewer backend {backend!r} unavailable: {declared.reason}")
    return RouteSpec.enabled_for(role, backend=backend, provider=provider,
                                 model_name=model_name)


def resolve_routing(
    mission_id: str,
    resolved: ResolvedPolicy,
    *,
    capabilities: Mapping[str, BackendCapability] | None = None,
    clock: Callable[[], str] = utc_now,
) -> RoutingDecision:
    """Resolve and validate the deterministic routing decision (pure + clock)."""
    policy = resolved.policy
    declared = dict(capabilities or {})

    implementation, fallback_used, fallback_reason, fb_from, fb_to = (
        _resolve_implementation(resolved, declared))

    inline = _resolve_reviewer(
        ROLE_INLINE_REVIEW,
        enabled=policy.inline_review_enabled,
        backend="ollama",
        provider="ollama",
        model_name=obs_model.INLINE_REVIEWER_MODEL,
        capabilities=declared)

    final_enabled = policy.final_review_required
    final_model = policy.final_reviewer_model
    if final_enabled:
        if final_model == LEGACY_PHANTOM_REVIEWER:
            model.fail(model.E_ROUTING_PHANTOM_REVIEWER, final_model)
        if (final_model != CANONICAL_FINAL_REVIEWER
                and "final_reviewer_model" not in resolved.overrides):
            model.fail(
                model.E_ROUTING_FINAL_REVIEWER_SWAP,
                f"{final_model!r} != {CANONICAL_FINAL_REVIEWER!r} without an "
                "explicit recorded override")
    final = _resolve_reviewer(
        ROLE_FINAL_REVIEW,
        enabled=final_enabled,
        backend="ollama",
        provider="ollama",
        model_name=final_model,
        capabilities=declared)

    capability_document = {
        backend: capability.to_dict()
        for backend, capability in sorted(declared.items())
    }
    capability_document.setdefault(implementation.backend or "", {
        **implementation.to_dict(), "declared": "resolved"})
    return RoutingDecision(
        mission_id=mission_id,
        policy_id=resolved.policy_id,
        profile=policy.profile,
        implementation=RouteSpec.enabled_for(
            ROLE_IMPLEMENTATION, backend=implementation.backend or "",
            provider=implementation.provider,
            model_name=implementation.model or ""),
        inline_review=inline,
        final_review=final,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        fallback_from=fb_from,
        fallback_to=fb_to,
        capabilities=capability_document,
        decided_at=clock(),
    ).validate()


def persist_routing(root: str, mission_id: str,
                    decision: RoutingDecision) -> None:
    """Persist the routing decision before use (+ append-only history)."""
    from pathlib import Path

    from trajectory_os.assembly import store as assembly_store
    from trajectory_os.operator import model as operator_model

    mission_root = assembly_store.mission_root(root, mission_id)
    document = decision.to_dict()
    from trajectory_os.operator._util import write_json

    write_json(Path(mission_root) / operator_model.ROUTING_NAME, document)
    append_jsonl(Path(mission_root) / operator_model.ROUTING_HISTORY_NAME,
                 document)


def load_routing(root: str, mission_id: str) -> RoutingDecision | None:
    from pathlib import Path

    from trajectory_os.assembly import store as assembly_store
    from trajectory_os.operator import model as operator_model
    from trajectory_os.operator._util import read_optional_json

    mission_root = Path(assembly_store.mission_root(root, mission_id))
    document = read_optional_json(mission_root / operator_model.ROUTING_NAME)
    if document is None:
        return None
    return RoutingDecision.from_dict(document)


def routing_history(root: str, mission_id: str) -> list[dict[str, Any]]:
    from pathlib import Path

    from trajectory_os.assembly import store as assembly_store
    from trajectory_os.operator import model as operator_model

    mission_root = Path(assembly_store.mission_root(root, mission_id))
    return read_jsonl(mission_root / operator_model.ROUTING_HISTORY_NAME)


__all__ = [
    "BackendCapability",
    "HARNESS_PREVIEW",
    "REVIEWER_BACKENDS",
    "ROLE_FINAL_REVIEW",
    "ROLE_IMPLEMENTATION",
    "ROLE_INLINE_REVIEW",
    "ROUTING_DOMAIN",
    "ROUTING_ROLES",
    "RouteSpec",
    "RoutingDecision",
    "load_routing",
    "model_digest",
    "persist_routing",
    "resolve_routing",
    "routing_history",
]
