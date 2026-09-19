"""M058 — advisory backend/model recommendation from measured evidence.

The recommendation layer is deliberately **advisory**. It never resolves the
routing policy, never mutates it, and never weakens a release gate. It reads
the M056 learning dataset, groups runs by their observed route
(backend/provider/model) and, only when two or more routes have *comparable*
evidence, emits a single recommendation with an explicit uncertainty and a
plain-language explanation.

When evidence is insufficient (too few runs, a single comparable route, or a
statistical tie) the honest answer is ``NO_RECOMMENDATION``. No superiority is
claimed beyond what the observed success rate and its Wilson interval support.
The final independent reviewer identity is always reported explicitly and the
developer-preview Harness is never presented as a benchmarked route.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from trajectory_os.intelligence import dataset as dataset_module
from trajectory_os.intelligence import model

#: Decision labels (closed set).
RECOMMEND = "RECOMMEND"
NO_RECOMMENDATION = "NO_RECOMMENDATION"

#: Minimum comparable runs for a route to enter the comparison.
MIN_ROUTE_EVIDENCE = 5

#: Fixed, explicit canonical final reviewer (never silently swapped).
CANONICAL_FINAL_REVIEWER = "qwen3.8:27b-q4_K_M"

#: The Harness is developer-preview until a handshake is proven.
HARNESS_BACKEND = "deepseek-harness"
HARNESS_STATUS = "developer-preview"

#: A route must beat the runner-up by this success-rate margin to be called.
MIN_MARGIN = 0.02

#: Stable reason codes.
R_RECOMMENDED = "HIGHEST_OBSERVED_SUCCESS_RATE"
R_INSUFFICIENT = "INSUFFICIENT_COMPARABLE_EVIDENCE"
R_TIE = "STATISTICAL_TIE"


@dataclass(frozen=True)
class RouteIdentity:
    """One candidate route identity (an observed backend/provider/model)."""

    backend: str | None
    provider: str | None
    model_name: str | None

    @property
    def key(self) -> str:
        return "|".join(value or "-" for value in (
            self.backend, self.provider, self.model_name))

    def to_dict(self) -> dict[str, Any]:
        return {"backend": self.backend, "provider": self.provider,
                "model": self.model_name, "route_key": self.key}

    @property
    def label(self) -> str:
        """Human-readable route label that stays unique across providers."""
        parts = [value for value in (self.backend, self.provider)
                 if value]
        prefix = "/".join(parts)
        model_name = self.model_name or "-"
        return f"{prefix}/{model_name}" if prefix else model_name


@dataclass(frozen=True)
class RouteEvidence:
    """Measured evidence for one candidate route."""

    route: RouteIdentity
    run_count: int
    labelled_count: int
    success_count: int
    blocked_count: int
    success_rate: float | None
    success_interval: tuple[float, float] | None
    blocked_rate: float | None
    median_duration_s: float | None
    mean_repairs: float | None
    source_refs: tuple[str, ...]

    @property
    def comparable(self) -> bool:
        return self.run_count >= MIN_ROUTE_EVIDENCE

    @property
    def utility_score(self) -> float | None:
        """Transparent advisory score: success rate minus blocked rate."""
        if self.success_rate is None or self.blocked_rate is None:
            return None
        return self.success_rate - self.blocked_rate

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.route.to_dict(),
            "run_count": self.run_count,
            "labelled_count": self.labelled_count,
            "success_count": self.success_count,
            "blocked_count": self.blocked_count,
            "success_rate": self.success_rate,
            "utility_score": self.utility_score,
            "success_interval_95": (list(self.success_interval)
                                    if self.success_interval is not None
                                    else None),
            "blocked_rate": self.blocked_rate,
            "median_duration_s": self.median_duration_s,
            "mean_repairs": self.mean_repairs,
            "comparable": self.comparable,
            "source_refs": list(self.source_refs[:10]),
        }


@dataclass(frozen=True)
class RouteRecommendation:
    """One persisted advisory recommendation (never a policy mutation)."""

    decision: str
    recommended_route: RouteIdentity | None
    reason_code: str
    explanation: str
    candidates: tuple[RouteEvidence, ...]
    min_evidence: int
    advisory_only: bool
    mutates_policy: bool
    release_gates_unchanged: bool
    final_reviewer: str
    harness_status: str
    dataset_id: str
    generated_at: str
    recommendation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "intelligence_version": model.INTELLIGENCE_VERSION,
            "kind": "route_recommendation",
            "decision": self.decision,
            "recommended_route": (self.recommended_route.to_dict()
                                  if self.recommended_route is not None
                                  else None),
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "candidates": [c.to_dict() for c in self.candidates],
            "min_evidence": self.min_evidence,
            "advisory_only": self.advisory_only,
            "mutates_policy": self.mutates_policy,
            "release_gates_unchanged": self.release_gates_unchanged,
            "final_reviewer": self.final_reviewer,
            "harness_status": self.harness_status,
            "dataset_id": self.dataset_id,
            "generated_at": self.generated_at,
            "recommendation_id": self.recommendation_id,
        }

    @staticmethod
    def from_dict(document: object) -> RouteRecommendation:
        mapping = model.require_mapping(document, "recommendation")
        model.check_version(mapping, "recommendation")
        recommended = mapping.get("recommended_route")
        candidates = tuple(
            _evidence_from_dict(model.require_mapping(item, "candidate"))
            for item in model.require_list(
                mapping.get("candidates", []), "candidates"))
        recommendation = RouteRecommendation(
            decision=str(mapping.get("decision", "")),
            recommended_route=(
                _route_from_dict(model.require_mapping(recommended,
                                                       "recommended_route"))
                if recommended is not None else None),
            reason_code=str(mapping.get("reason_code", "")),
            explanation=str(mapping.get("explanation", "")),
            candidates=candidates,
            min_evidence=int(mapping.get("min_evidence", MIN_ROUTE_EVIDENCE)),
            advisory_only=bool(mapping.get("advisory_only", True)),
            mutates_policy=bool(mapping.get("mutates_policy", False)),
            release_gates_unchanged=bool(
                mapping.get("release_gates_unchanged", True)),
            final_reviewer=str(mapping.get("final_reviewer", "")),
            harness_status=str(mapping.get("harness_status", "")),
            dataset_id=str(mapping.get("dataset_id", "")),
            generated_at=str(mapping.get("generated_at", "")),
            recommendation_id=str(mapping.get("recommendation_id", "")),
        )
        expected = recommendation.compute_id()
        stored = mapping.get("recommendation_id")
        if stored is not None and stored != expected:
            model.fail(model.E_IDENTITY_MISMATCH, str(stored))
        return recommendation

    def compute_id(self) -> str:
        return model.digest(
            {"decision": self.decision,
             "recommended": (self.recommended_route.key
                             if self.recommended_route is not None else None),
             "reason_code": self.reason_code,
             "dataset_id": self.dataset_id,
             "candidates": [_evidence_identity(c)
                            for c in self.candidates]},
            domain=model.MATERIAL_DOMAIN)


def _evidence_identity(evidence: RouteEvidence) -> dict[str, Any]:
    """Stable candidate identity (excludes display-only truncation)."""
    return {
        "route_key": evidence.route.key,
        "run_count": evidence.run_count,
        "labelled_count": evidence.labelled_count,
        "success_count": evidence.success_count,
        "blocked_count": evidence.blocked_count,
        "success_rate": evidence.success_rate,
        "blocked_rate": evidence.blocked_rate,
        "median_duration_s": evidence.median_duration_s,
        "mean_repairs": evidence.mean_repairs,
    }


def _route_from_dict(mapping: Mapping[str, Any]) -> RouteIdentity:
    return RouteIdentity(
        backend=_opt(mapping.get("backend")),
        provider=_opt(mapping.get("provider")),
        model_name=_opt(mapping.get("model", mapping.get("model_name"))),
    )


def _evidence_from_dict(mapping: Mapping[str, Any]) -> RouteEvidence:
    interval = mapping.get("success_interval_95")
    return RouteEvidence(
        route=_route_from_dict(mapping),
        run_count=int(mapping.get("run_count", 0)),
        labelled_count=int(mapping.get("labelled_count", 0)),
        success_count=int(mapping.get("success_count", 0)),
        blocked_count=int(mapping.get("blocked_count", 0)),
        success_rate=_opt_float(mapping.get("success_rate")),
        success_interval=(
            (float(model.require_list(interval, "interval")[0]),
             float(model.require_list(interval, "interval")[1]))
            if isinstance(interval, list) and len(interval) == 2 else None),
        blocked_rate=_opt_float(mapping.get("blocked_rate")),
        median_duration_s=_opt_float(mapping.get("median_duration_s")),
        mean_repairs=_opt_float(mapping.get("mean_repairs")),
        source_refs=tuple(str(v) for v in model.require_list(
            mapping.get("source_refs", []), "source_refs")),
    )


def _opt(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def collect_route_evidence(
    learning: dataset_module.LearningDataset,
) -> tuple[RouteEvidence, ...]:
    """Group observations by route and measure success/blocked evidence."""
    groups: dict[str, list[dataset_module.Observation]] = {}
    for observation in learning.observations:
        identity = RouteIdentity(
            backend=observation.backend, provider=observation.provider,
            model_name=observation.model)
        groups.setdefault(identity.key, []).append(observation)
    evidence: list[RouteEvidence] = []
    for key in sorted(groups):
        members = groups[key]
        first = members[0]
        route = RouteIdentity(backend=first.backend, provider=first.provider,
                              model_name=first.model)
        labelled = [o for o in members if o.readiness is not None]
        successes = sum(1 for o in labelled
                        if o.readiness == dataset_module.READY_FOR_COMMIT)
        blocked = sum(1 for o in labelled if o.readiness == "BLOCKED")
        durations = sorted(o.duration_s for o in members
                           if o.duration_s is not None)
        repairs = [float(o.repairs) for o in members
                   if o.repairs is not None]
        rate = (successes / len(labelled)) if labelled else None
        interval = (_wilson(successes, len(labelled))
                    if labelled else None)
        evidence.append(RouteEvidence(
            route=route, run_count=len(members),
            labelled_count=len(labelled), success_count=successes,
            blocked_count=blocked, success_rate=rate,
            success_interval=interval,
            blocked_rate=(blocked / len(labelled)) if labelled else None,
            median_duration_s=_median(durations),
            mean_repairs=(sum(repairs) / len(repairs)) if repairs else None,
            source_refs=tuple(o.source_ref for o in members)))
    return tuple(evidence)


def recommend_route(
    learning: dataset_module.LearningDataset, *,
    min_evidence: int = MIN_ROUTE_EVIDENCE,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> RouteRecommendation:
    """Produce one advisory recommendation (or NO_RECOMMENDATION)."""
    candidates = collect_route_evidence(learning)
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    comparable = [c for c in candidates
                  if c.comparable and c.success_rate is not None]
    if len(comparable) < 2:
        return _finalize(replace(
            _base(learning, candidates, min_evidence, stamp),
            decision=NO_RECOMMENDATION, recommended_route=None,
            reason_code=R_INSUFFICIENT,
            explanation=(
                f"{len(comparable)} route(s) have >= "
                f"{min_evidence} comparable runs; at least 2 are "
                "required before any recommendation is made")))
    ranked = sorted(
        comparable,
        key=lambda c: (-(c.utility_score or 0.0), c.blocked_rate or 1.0,
                       c.median_duration_s or math.inf,
                       c.route.key))
    best = ranked[0]
    runner_up = ranked[1]
    best_score = best.utility_score or 0.0
    runner_up_score = runner_up.utility_score or 0.0
    margin = best_score - runner_up_score
    if margin < MIN_MARGIN:
        return _finalize(replace(
            _base(learning, candidates, min_evidence, stamp),
            decision=NO_RECOMMENDATION, recommended_route=None,
            reason_code=R_TIE,
            explanation=(
                f"top two routes are within {MIN_MARGIN:.2f} advisory score "
                f"({best.route.label} {best_score:.3f} vs "
                f"{runner_up.route.label} {runner_up_score:.3f}); "
                "a distinction is not supportable")))
    interval = best.success_interval or (0.0, 0.0)
    explanation = (
        f"{best.route.label} has the highest transparent advisory score "
        f"(success rate minus blocked rate = {best_score:.3f}; "
        f"success {best.success_rate:.1%} of {best.labelled_count} labelled "
        f"runs, blocked {best.blocked_rate:.1%}, 95% Wilson interval "
        f"{interval[0]:.1%}-{interval[1]:.1%}) versus runner-up "
        f"{runner_up.route.label} ({runner_up_score:.3f}). "
        "This is observational evidence, not a controlled benchmark; the "
        "routing policy and human GO gates remain authoritative.")
    return _finalize(replace(
        _base(learning, candidates, min_evidence, stamp),
        decision=RECOMMEND, recommended_route=best.route,
        reason_code=R_RECOMMENDED, explanation=explanation))


def _finalize(recommendation: RouteRecommendation) -> RouteRecommendation:
    """Recompute the identity after the decision fields are finalized."""
    return replace(recommendation,
                   recommendation_id=recommendation.compute_id())


def _base(learning: dataset_module.LearningDataset,
          candidates: Sequence[RouteEvidence], min_evidence: int,
          stamp: str) -> RouteRecommendation:
    recommendation = RouteRecommendation(
        decision=NO_RECOMMENDATION, recommended_route=None,
        reason_code=R_INSUFFICIENT, explanation="",
        candidates=tuple(candidates), min_evidence=min_evidence,
        advisory_only=True,
        mutates_policy=False, release_gates_unchanged=True,
        final_reviewer=CANONICAL_FINAL_REVIEWER, harness_status=HARNESS_STATUS,
        dataset_id=learning.dataset_id, generated_at=stamp)
    return recommendation


def _wilson(successes: int, total: int) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    z = 1.96
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = (z * math.sqrt(phat * (1 - phat) / total
                            + z * z / (4 * total * total)) / denominator)
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def recommendation_path(root: str) -> str:
    return f"{root}/routing/recommendation.json"


def persist_recommendation(root: str,
                           recommendation: RouteRecommendation) -> str:
    model.write_json(recommendation_path(root), recommendation.to_dict())
    return recommendation_path(root)


def load_recommendation(root: str) -> RouteRecommendation | None:
    document = model.read_json(recommendation_path(root))
    if document is None:
        return None
    return RouteRecommendation.from_dict(document)


__all__ = [
    "CANONICAL_FINAL_REVIEWER",
    "HARNESS_STATUS",
    "MIN_ROUTE_EVIDENCE",
    "NO_RECOMMENDATION",
    "R_INSUFFICIENT",
    "R_RECOMMENDED",
    "R_TIE",
    "RECOMMEND",
    "RouteEvidence",
    "RouteIdentity",
    "RouteRecommendation",
    "collect_route_evidence",
    "load_recommendation",
    "persist_recommendation",
    "recommend_route",
    "recommendation_path",
]
