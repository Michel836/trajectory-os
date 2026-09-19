"""M044 — small deterministic policy engine and trust profiles (pure).

Four profiles are defined: ``safe``, ``fast-local``, ``benchmark`` and
``release``. Policy resolution is a *pure function* of ``(profile, explicit
overrides)``: the same inputs always resolve to the same
:class:`ResolvedPolicy` and the same :attr:`ResolvedPolicy.policy_id`.

Trust invariants (fail closed):

* the ``release`` profile always retains an explicit human ``GO COMMIT`` and
  ``GO MERGE`` and an exact-head CI requirement — an override that disables
  any of them is refused;
* exact-head CI can never be silently disabled for a release;
* the environment is never consulted: any attempt to resolve policy from the
  environment is refused, so ambient state cannot mutate policy;
* overrides are explicit and recorded in the persisted document;
* an invalid combination (unknown profile, unknown field, bad value) is
  refused with a stable reason code.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from trajectory_os.agents import model as agent_model
from trajectory_os.observability import model as obs_model
from trajectory_os.operator import model
from trajectory_os.operator._util import digest, optional_str, utc_now
from trajectory_os.release import model as release_model

# --- profile names (closed set) ----------------------------------------------

PROFILE_SAFE = "safe"
PROFILE_FAST_LOCAL = "fast-local"
PROFILE_BENCHMARK = "benchmark"
PROFILE_RELEASE = "release"

PROFILES = frozenset({
    PROFILE_SAFE, PROFILE_FAST_LOCAL, PROFILE_BENCHMARK, PROFILE_RELEASE,
})

#: The only policy profile that may authorize release Git writes.
RELEASE_PROFILE = PROFILE_RELEASE

#: Reviewer identity that must never be silently swapped.
CANONICAL_FINAL_REVIEWER = obs_model.FINAL_REVIEWER_MODEL
LEGACY_PHANTOM_REVIEWER = obs_model.LEGACY_PHANTOM_REVIEWER

#: Domain id for one resolved policy identity.
POLICY_DOMAIN = "trajectory-os.operator-policy.v1"

#: Explicit override source; the environment is never an accepted source.
SOURCE_OPERATOR = "operator"
SOURCE_ENVIRONMENT = "environment"
FORBIDDEN_ENVIRONMENT_KEYS = (
    "TRAJECTORY_POLICY", "TRAJECTORY_POLICY_PROFILE",
    "TRAJECTORY_OPERATOR_POLICY",
)

#: Fields an explicit override may target (closed set).
OVERRIDABLE_FIELDS = frozenset({
    "implementation_backend", "implementation_provider",
    "implementation_model", "inline_review_enabled", "final_review_required",
    "final_reviewer_model", "repair_attempt_limit", "telemetry_level",
    "fallback_allowed", "validation_requirements", "merge_method",
    "go_commit_required", "go_merge_required", "exact_head_ci_required",
})

MAX_VALIDATION_REQUIREMENTS = 32
MAX_VALIDATION_REQUIREMENT_LEN = 512


@dataclass(frozen=True)
class Policy:
    """One resolved trust policy (never mutated in place)."""

    profile: str
    implementation_backend: str
    implementation_provider: str | None
    implementation_model: str | None
    inline_review_enabled: bool
    final_review_required: bool
    final_reviewer_model: str
    validation_requirements: tuple[str, ...]
    repair_attempt_limit: int
    telemetry_level: str
    fallback_allowed: bool
    fallback_order: tuple[str, ...]
    go_commit_required: bool
    go_merge_required: bool
    exact_head_ci_required: bool
    merge_method: str

    def validate(self) -> Policy:
        if self.profile not in PROFILES:
            model.fail(model.E_POLICY_PROFILE_UNKNOWN, self.profile)
        if self.implementation_backend not in agent_model.BACKENDS:
            model.fail(model.E_POLICY_INVALID,
                       f"backend {self.implementation_backend!r}")
        if self.telemetry_level not in obs_model.TELEMETRY_MODES:
            model.fail(model.E_POLICY_INVALID,
                       f"telemetry {self.telemetry_level!r}")
        if self.repair_attempt_limit < 0:
            model.fail(model.E_POLICY_INVALID, "repair_attempt_limit < 0")
        if self.merge_method not in release_model.MERGE_METHODS:
            model.fail(model.E_POLICY_INVALID,
                       f"merge_method {self.merge_method!r}")
        if not self.validation_requirements:
            model.fail(model.E_POLICY_INVALID,
                       "validation requirements required")
        if len(self.validation_requirements) > MAX_VALIDATION_REQUIREMENTS:
            model.fail(model.E_POLICY_INVALID, "too many validation runs")
        for requirement in self.validation_requirements:
            if (not requirement.strip()
                    or len(requirement) > MAX_VALIDATION_REQUIREMENT_LEN):
                model.fail(model.E_POLICY_INVALID,
                           f"invalid validation requirement {requirement!r}")
        if self.final_review_required:
            if not self.final_reviewer_model:
                model.fail(model.E_POLICY_INVALID,
                           "final review requires a reviewer model")
            if self.final_reviewer_model == LEGACY_PHANTOM_REVIEWER:
                model.fail(model.E_ROUTING_PHANTOM_REVIEWER,
                           self.final_reviewer_model)
        for backend in self.fallback_order:
            if backend not in agent_model.BACKENDS:
                model.fail(model.E_POLICY_INVALID,
                           f"fallback backend {backend!r}")
        if self.profile == PROFILE_RELEASE:
            # Release trust gates can never be disabled by any resolution.
            if not (self.go_commit_required and self.go_merge_required):
                model.fail(
                    model.E_POLICY_INVALID,
                    "release profile must retain GO COMMIT and GO MERGE")
            if not self.exact_head_ci_required:
                model.fail(
                    model.E_POLICY_INVALID,
                    "release profile exact-head CI cannot be disabled")
            if not self.final_review_required:
                model.fail(model.E_POLICY_INVALID,
                           "release profile requires independent review")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "operator_version": model.OPERATOR_VERSION,
            "profile": self.profile,
            "implementation": {
                "backend": self.implementation_backend,
                "provider": self.implementation_provider,
                "model": self.implementation_model,
            },
            "inline_review_enabled": self.inline_review_enabled,
            "final_review_required": self.final_review_required,
            "final_reviewer_model": self.final_reviewer_model,
            "validation_requirements": list(self.validation_requirements),
            "repair_attempt_limit": self.repair_attempt_limit,
            "telemetry_level": self.telemetry_level,
            "fallback_allowed": self.fallback_allowed,
            "fallback_order": list(self.fallback_order),
            "go_commit_required": self.go_commit_required,
            "go_merge_required": self.go_merge_required,
            "exact_head_ci_required": self.exact_head_ci_required,
            "merge_method": self.merge_method,
        }

    @property
    def policy_id(self) -> str:
        return digest(self.to_dict(), domain=POLICY_DOMAIN)


@dataclass(frozen=True)
class ResolvedPolicy:
    """The persisted result of resolving one profile with explicit overrides."""

    policy: Policy
    policy_id: str
    overrides: Mapping[str, Any]
    source: str
    resolved_at: str
    schema_version: int = model.SCHEMA_VERSION
    operator_version: str = model.OPERATOR_VERSION

    def validate(self) -> ResolvedPolicy:
        self.policy.validate()
        if self.source == SOURCE_ENVIRONMENT:
            model.fail(model.E_POLICY_ENVIRONMENT,
                       "policy cannot be resolved from the environment")
        if self.policy_id != self.policy.policy_id:
            model.fail(model.E_POLICY_INVALID, "policy_id mismatch")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operator_version": self.operator_version,
            "policy_id": self.policy_id,
            "profile": self.policy.profile,
            "source": self.source,
            "explicit_overrides": dict(sorted(self.overrides.items())),
            "overrides_recorded": bool(self.overrides),
            "resolved_at": self.resolved_at,
            "policy": self.policy.to_dict(),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ResolvedPolicy:
        if not isinstance(data.get("policy"), Mapping):
            model.fail(model.E_MALFORMED, "resolved policy document")
        policy = _policy_from_dict(data["policy"])
        overrides = data.get("explicit_overrides")
        return ResolvedPolicy(
            policy=policy,
            policy_id=str(data.get("policy_id", "")),
            overrides=(dict(overrides) if isinstance(overrides, Mapping)
                       else {}),
            source=str(data.get("source", SOURCE_OPERATOR)),
            resolved_at=str(data.get("resolved_at", "")),
            schema_version=int(data.get("schema_version",
                                        model.SCHEMA_VERSION)),
            operator_version=str(data.get("operator_version",
                                          model.OPERATOR_VERSION)),
        ).validate()


def _policy_from_dict(data: Mapping[str, Any]) -> Policy:
    implementation = data.get("implementation")
    implementation = (implementation
                      if isinstance(implementation, Mapping) else {})
    validation = data.get("validation_requirements")
    return Policy(
        profile=str(data.get("profile", "")),
        implementation_backend=str(implementation.get("backend", "")),
        implementation_provider=optional_str(
            implementation.get("provider")),
        implementation_model=optional_str(implementation.get("model")),
        inline_review_enabled=bool(data.get("inline_review_enabled", False)),
        final_review_required=bool(
            data.get("final_review_required", True)),
        final_reviewer_model=str(data.get("final_reviewer_model", "")),
        validation_requirements=tuple(
            str(x) for x in validation) if isinstance(validation, (list, tuple))
        else (),
        repair_attempt_limit=int(data.get("repair_attempt_limit", 2)),
        telemetry_level=str(data.get("telemetry_level",
                                     obs_model.TELEMETRY_STANDARD)),
        fallback_allowed=bool(data.get("fallback_allowed", False)),
        fallback_order=tuple(str(x) for x in data.get("fallback_order", ())),
        go_commit_required=bool(data.get("go_commit_required", True)),
        go_merge_required=bool(data.get("go_merge_required", True)),
        exact_head_ci_required=bool(
            data.get("exact_head_ci_required", True)),
        merge_method=str(data.get("merge_method",
                                  release_model.DEFAULT_MERGE_METHOD)),
    ).validate()


# --- canonical profile definitions --------------------------------------------

_DEFAULT_VALIDATION = (
    "the canonical deterministic validation gate passes on the exact patch",
    "the exact-head CI check succeeds before any merge",
)

_PROFILE_DEFINITIONS: Mapping[str, Policy] = {
    PROFILE_SAFE: Policy(
        profile=PROFILE_SAFE,
        implementation_backend=agent_model.BACKEND_PI,
        implementation_provider="deepseek",
        implementation_model="deepseek-flash",
        inline_review_enabled=False,
        final_review_required=True,
        final_reviewer_model=CANONICAL_FINAL_REVIEWER,
        validation_requirements=_DEFAULT_VALIDATION,
        repair_attempt_limit=2,
        telemetry_level=obs_model.TELEMETRY_STANDARD,
        fallback_allowed=False,
        fallback_order=(),
        go_commit_required=True,
        go_merge_required=True,
        exact_head_ci_required=True,
        merge_method=release_model.MERGE_SQUASH,
    ),
    PROFILE_FAST_LOCAL: Policy(
        profile=PROFILE_FAST_LOCAL,
        implementation_backend=agent_model.BACKEND_PI,
        implementation_provider="ollama",
        implementation_model="qwen3.8-dev3090",
        inline_review_enabled=False,
        final_review_required=True,
        final_reviewer_model=CANONICAL_FINAL_REVIEWER,
        validation_requirements=_DEFAULT_VALIDATION,
        repair_attempt_limit=1,
        telemetry_level=obs_model.TELEMETRY_OFF,
        fallback_allowed=True,
        fallback_order=(agent_model.BACKEND_PI,
                        agent_model.BACKEND_DEEPSEEK_HARNESS),
        go_commit_required=True,
        go_merge_required=True,
        exact_head_ci_required=True,
        merge_method=release_model.MERGE_SQUASH,
    ),
    PROFILE_BENCHMARK: Policy(
        profile=PROFILE_BENCHMARK,
        implementation_backend=agent_model.BACKEND_PI,
        implementation_provider="deepseek",
        implementation_model="deepseek-flash",
        inline_review_enabled=True,
        final_review_required=True,
        final_reviewer_model=CANONICAL_FINAL_REVIEWER,
        validation_requirements=_DEFAULT_VALIDATION,
        repair_attempt_limit=2,
        telemetry_level=obs_model.TELEMETRY_BENCHMARK,
        fallback_allowed=True,
        fallback_order=(agent_model.BACKEND_DEEPSEEK_HARNESS,
                        agent_model.BACKEND_PI),
        go_commit_required=True,
        go_merge_required=True,
        exact_head_ci_required=True,
        merge_method=release_model.MERGE_SQUASH,
    ),
    PROFILE_RELEASE: Policy(
        profile=PROFILE_RELEASE,
        implementation_backend=agent_model.BACKEND_PI,
        implementation_provider="deepseek",
        implementation_model="deepseek-flash",
        inline_review_enabled=False,
        final_review_required=True,
        final_reviewer_model=CANONICAL_FINAL_REVIEWER,
        validation_requirements=_DEFAULT_VALIDATION,
        repair_attempt_limit=2,
        telemetry_level=obs_model.TELEMETRY_STANDARD,
        fallback_allowed=False,
        fallback_order=(),
        go_commit_required=True,
        go_merge_required=True,
        exact_head_ci_required=True,
        merge_method=release_model.MERGE_SQUASH,
    ),
}


def profile_policy(profile: str) -> Policy:
    """Return the canonical, immutable base policy for one profile."""
    base = _PROFILE_DEFINITIONS.get(profile)
    if base is None:
        model.fail(model.E_POLICY_PROFILE_UNKNOWN, profile)
    return base


def _coerce_override(field: str, value: object) -> object:
    if field in ("inline_review_enabled", "final_review_required",
                 "fallback_allowed", "go_commit_required",
                 "go_merge_required", "exact_head_ci_required"):
        if not isinstance(value, bool):
            model.fail(model.E_POLICY_OVERRIDE_INVALID,
                       f"{field} must be a boolean")
        return value
    if field == "repair_attempt_limit":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            model.fail(model.E_POLICY_OVERRIDE_INVALID,
                       "repair_attempt_limit must be a non-negative int")
        return value
    if field == "validation_requirements":
        if (not isinstance(value, (list, tuple))
                or any(not isinstance(item, str) for item in value)):
            model.fail(model.E_POLICY_OVERRIDE_INVALID,
                       "validation_requirements must be a list of strings")
        return tuple(value)
    if not isinstance(value, str) or not value:
        model.fail(model.E_POLICY_OVERRIDE_INVALID,
                   f"{field} must be a non-empty string")
    return value


def resolve_policy(profile: str, *,
                   overrides: Mapping[str, object] | None = None,
                   source: str = SOURCE_OPERATOR,
                   clock: Callable[[], str] = utc_now) -> ResolvedPolicy:
    """Resolve one profile with explicit, recorded overrides (pure + clock).

    ``source`` exists only to *reject* environment-derived policy: the
    environment is never consulted and any environment source fails closed.
    """
    if source == SOURCE_ENVIRONMENT:
        model.fail(model.E_POLICY_ENVIRONMENT,
                   "policy must be resolved from explicit operator input")
    base = profile_policy(profile)
    applied: dict[str, object] = {}
    for field, raw in sorted((overrides or {}).items()):
        if field not in OVERRIDABLE_FIELDS:
            model.fail(model.E_POLICY_OVERRIDE_INVALID,
                       f"field {field!r} cannot be overridden")
        applied[field] = _coerce_override(field, raw)
    policy = replace(base, **applied).validate()  # type: ignore[arg-type]
    return ResolvedPolicy(
        policy=policy,
        policy_id=policy.policy_id,
        overrides=dict(applied),
        source=source,
        resolved_at=clock(),
    ).validate()


def release_policy(*, overrides: Mapping[str, object] | None = None,
                   clock: Callable[[], str] = utc_now) -> ResolvedPolicy:
    """Resolve the release profile (both human gates always retained)."""
    return resolve_policy(PROFILE_RELEASE, overrides=overrides,
                          clock=clock)


__all__ = [
    "CANONICAL_FINAL_REVIEWER",
    "FORBIDDEN_ENVIRONMENT_KEYS",
    "LEGACY_PHANTOM_REVIEWER",
    "OVERRIDABLE_FIELDS",
    "POLICY_DOMAIN",
    "PROFILES",
    "PROFILE_BENCHMARK",
    "PROFILE_FAST_LOCAL",
    "PROFILE_RELEASE",
    "PROFILE_SAFE",
    "RELEASE_PROFILE",
    "SOURCE_ENVIRONMENT",
    "SOURCE_OPERATOR",
    "Policy",
    "ResolvedPolicy",
    "profile_policy",
    "release_policy",
    "resolve_policy",
]
