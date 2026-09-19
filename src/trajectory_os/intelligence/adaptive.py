"""M059 — optional predictive overlay for the canonical M050 scheduler.

The canonical graph scheduler remains authoritative. This module never
schedules, dispatches, reserves resources or writes to a Git trust surface.
It produces a **transparent, advisory ordering** of already-eligible
candidates and, when predictions exist, a second ML-assisted ordering, then
reports how the two compare.

Every score is a sum of explicitly-named components. The rule-based ordering
is always available as a deterministic fallback: when no prediction is
available the ML-assisted ordering is byte-identical to the rule ordering and
the fallback reason is recorded. No improvement is ever claimed without
measured outcome evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from trajectory_os.intelligence import ml, model

#: Overlay modes (closed set).
MODE_RULE = "RULE_BASED"
MODE_ML = "ML_ASSISTED"

#: Explicit, documented rule weights.
W_PRIORITY = 10.0
W_URGENCY = 8.0
W_WAIT = 4.0
W_DURATION = 3.0
W_SUCCESS = 6.0
W_RISK = 8.0
W_COST = 3.0
BLOCK_NOT_READY = -100.0
BLOCK_UNAVAILABLE = -100.0

#: Normalisation caps (bounded, explicit).
WAIT_CAP_S = 86_400.0
DURATION_CAP_S = 3_600.0
COST_CAP_USD = 5.0


@dataclass(frozen=True)
class ScheduleCandidate:
    """One already-eligible candidate with optional predictive signals."""

    candidate_id: str
    project: str
    mission_id: str | None
    project_priority: int
    deadline_s: float | None
    wait_age_s: float
    dependencies_ready: bool
    backend_available: bool
    predicted_duration_s: float | None = None
    predicted_success: float | None = None
    predicted_block_risk: float | None = None
    expected_cost_usd: float | None = None
    human_priority: int | None = None

    def validate(self) -> ScheduleCandidate:
        if not self.candidate_id:
            model.fail(model.E_MALFORMED, "candidate_id required")
        if not 1 <= self.project_priority <= 5:
            model.fail(model.E_MALFORMED, "project_priority out of range")
        if self.wait_age_s < 0:
            model.fail(model.E_MALFORMED, "wait_age_s must be >= 0")
        for name in ("predicted_success", "predicted_block_risk"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= value <= 1.0:
                model.fail(model.E_MALFORMED, f"{name} must be in [0,1]")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "project": self.project,
            "mission_id": self.mission_id,
            "project_priority": self.project_priority,
            "human_priority": self.human_priority,
            "deadline_s": self.deadline_s,
            "wait_age_s": self.wait_age_s,
            "dependencies_ready": self.dependencies_ready,
            "backend_available": self.backend_available,
            "predicted_duration_s": self.predicted_duration_s,
            "predicted_success": self.predicted_success,
            "predicted_block_risk": self.predicted_block_risk,
            "expected_cost_usd": self.expected_cost_usd,
        }


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    value: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": round(self.value, 6),
                "reason": self.reason}


@dataclass(frozen=True)
class CandidateScore:
    candidate_id: str
    mode: str
    components: tuple[ScoreComponent, ...]
    total: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "mode": self.mode,
            "components": [c.to_dict() for c in self.components],
            "total": round(self.total, 6),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ScheduleOverlay:
    mode: str
    ordering: tuple[CandidateScore, ...]
    fallback_used: bool
    fallback_reason: str | None
    predictions_used: bool
    mutates_canonical_scheduler: bool
    commands_release_git: bool
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "ordering": [c.to_dict() for c in self.ordering],
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "predictions_used": self.predictions_used,
            "mutates_canonical_scheduler": self.mutates_canonical_scheduler,
            "commands_release_git": self.commands_release_git,
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True)
class ScheduleComparison:
    rule: ScheduleOverlay
    ml_assisted: ScheduleOverlay
    rank_agreement: float
    top1_same: bool
    claimed_improvement: bool
    improvement_reason: str
    predictions_available: bool
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "intelligence_version": model.INTELLIGENCE_VERSION,
            "kind": "scheduler_comparison",
            "rule": self.rule.to_dict(),
            "ml_assisted": self.ml_assisted.to_dict(),
            "rank_agreement": self.rank_agreement,
            "top1_same": self.top1_same,
            "claimed_improvement": self.claimed_improvement,
            "improvement_reason": self.improvement_reason,
            "predictions_available": self.predictions_available,
            "generated_at": self.generated_at,
        }

    def render_markdown(self) -> str:
        lines = [
            "# M059 — Adaptive scheduler comparison", "",
            f"- rank agreement (Kendall tau): {self.rank_agreement:.3f}",
            f"- top-1 identical: {self.top1_same}",
            f"- predictions available: {self.predictions_available}",
            f"- improvement claimed: {self.claimed_improvement} "
            f"({self.improvement_reason})", "",
            "| rank | rule-based | ML-assisted |", "| --- | --- | --- |",
        ]
        for index in range(max(len(self.rule.ordering),
                               len(self.ml_assisted.ordering))):
            rule_id = (self.rule.ordering[index].candidate_id
                       if index < len(self.rule.ordering) else "-")
            ml_id = (self.ml_assisted.ordering[index].candidate_id
                     if index < len(self.ml_assisted.ordering) else "-")
            lines.append(f"| {index + 1} | {rule_id} | {ml_id} |")
        return "\n".join(lines) + "\n"


def score_candidate(candidate: ScheduleCandidate, *,
                    mode: str) -> CandidateScore:
    """Compute the transparent component score for one candidate."""
    candidate.validate()
    if mode not in (MODE_RULE, MODE_ML):
        model.fail(model.E_MALFORMED, f"unknown mode {mode!r}")
    components: list[ScoreComponent] = []
    components.append(ScoreComponent(
        "project_priority", (candidate.project_priority - 1) * W_PRIORITY,
        f"project priority {candidate.project_priority}/5"))
    if candidate.human_priority is not None:
        components.append(ScoreComponent(
            "human_priority", (candidate.human_priority - 1) * W_PRIORITY,
            f"explicit human priority {candidate.human_priority}/5"))
    if candidate.deadline_s is not None:
        if candidate.deadline_s <= 0:
            urgency = W_URGENCY * 1.5
        elif candidate.deadline_s <= 3_600:
            urgency = W_URGENCY
        elif candidate.deadline_s <= 86_400:
            urgency = W_URGENCY * 0.5
        else:
            urgency = 0.0
        components.append(ScoreComponent(
            "urgency", urgency,
            f"deadline in {candidate.deadline_s:.0f}s"))
    components.append(ScoreComponent(
        "wait_age", min(candidate.wait_age_s / WAIT_CAP_S, 1.0) * W_WAIT,
        f"waited {candidate.wait_age_s:.0f}s"))
    if not candidate.dependencies_ready:
        components.append(ScoreComponent(
            "dependency_block", BLOCK_NOT_READY, "dependencies not ready"))
    if not candidate.backend_available:
        components.append(ScoreComponent(
            "backend_block", BLOCK_UNAVAILABLE, "backend unavailable"))
    if mode == MODE_ML:
        if candidate.predicted_success is not None:
            components.append(ScoreComponent(
                "predicted_success",
                candidate.predicted_success * W_SUCCESS,
                f"predicted success {candidate.predicted_success:.2f}"))
        if candidate.predicted_block_risk is not None:
            components.append(ScoreComponent(
                "predicted_block_risk",
                -candidate.predicted_block_risk * W_RISK,
                f"predicted block risk {candidate.predicted_block_risk:.2f}"))
        if candidate.predicted_duration_s is not None:
            components.append(ScoreComponent(
                "predicted_duration",
                -min(candidate.predicted_duration_s / DURATION_CAP_S, 1.0)
                * W_DURATION,
                f"predicted duration {candidate.predicted_duration_s:.0f}s"))
        if candidate.expected_cost_usd is not None:
            components.append(ScoreComponent(
                "expected_cost",
                -min(candidate.expected_cost_usd / COST_CAP_USD, 1.0) * W_COST,
                f"expected cost ${candidate.expected_cost_usd:.4f}"))
    total = sum(component.value for component in components)
    top = sorted(components, key=lambda c: (-abs(c.value), c.name))[:3]
    reason = "; ".join(f"{c.name}={c.value:+.2f}" for c in top) or "no signals"
    return CandidateScore(candidate_id=candidate.candidate_id, mode=mode,
                          components=tuple(components), total=total,
                          reason=reason)


def build_overlay(
    candidates: Sequence[ScheduleCandidate], *, mode: str = MODE_RULE,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> ScheduleOverlay:
    """Order candidates by the transparent component score (advisory)."""
    if mode == MODE_ML:
        predictions_used = any(
            c.predicted_success is not None
            or c.predicted_block_risk is not None
            or c.predicted_duration_s is not None for c in candidates)
        fallback_used = not predictions_used
        fallback_reason = (
            "no candidate carried a prediction; deterministic rule-based "
            "ordering used" if fallback_used else None)
        effective_mode = MODE_RULE if fallback_used else MODE_ML
    else:
        predictions_used = False
        fallback_used = False
        fallback_reason = None
        effective_mode = MODE_RULE
    scored = [score_candidate(candidate, mode=effective_mode)
              for candidate in candidates]
    ordered = tuple(sorted(
        scored,
        key=lambda item: (-item.total, item.candidate_id)))
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    return ScheduleOverlay(
        mode=effective_mode, ordering=ordered, fallback_used=fallback_used,
        fallback_reason=fallback_reason, predictions_used=predictions_used,
        mutates_canonical_scheduler=False, commands_release_git=False,
        generated_at=stamp)


def compare_schedulers(
    candidates: Sequence[ScheduleCandidate], *,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> ScheduleComparison:
    """Compare rule-based and ML-assisted orderings (advisory only)."""
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    rule = build_overlay(candidates, mode=MODE_RULE, generated_at=stamp)
    ml_assisted = build_overlay(candidates, mode=MODE_ML, generated_at=stamp)
    rule_ids = [c.candidate_id for c in rule.ordering]
    ml_ids = [c.candidate_id for c in ml_assisted.ordering]
    predictions_available = ml_assisted.predictions_used
    agreement = _kendall_tau(rule_ids, ml_ids)
    top1_same = bool(rule_ids) and rule_ids[0] == ml_ids[0]
    return ScheduleComparison(
        rule=rule, ml_assisted=ml_assisted, rank_agreement=agreement,
        top1_same=top1_same, claimed_improvement=False,
        improvement_reason=(
            "no executed outcome is attached to this snapshot; an ordering "
            "difference alone is not evidence of improvement"),
        predictions_available=predictions_available, generated_at=stamp)


def _kendall_tau(left: Sequence[str], right: Sequence[str]) -> float:
    if len(left) < 2 or len(left) != len(right):
        return 0.0
    position_right = {value: index for index, value in enumerate(right)}
    concordant = 0
    discordant = 0
    for i in range(len(left)):
        for j in range(i + 1, len(left)):
            a = position_right.get(left[i])
            b = position_right.get(left[j])
            if a is None or b is None:
                continue
            if a < b:
                concordant += 1
            elif a > b:
                discordant += 1
    total = concordant + discordant
    if total == 0:
        return 1.0 if left == right else 0.0
    return (concordant - discordant) / total


@dataclass(frozen=True)
class PredictionSnapshot:
    """Persisted feature/prediction snapshot used by the overlay."""

    generated_at: str
    feature_set: str
    predictions: Mapping[str, Mapping[str, float | None]]
    model_ids: Mapping[str, str]
    dataset_id: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "kind": "prediction_snapshot",
            "generated_at": self.generated_at,
            "feature_set": self.feature_set,
            "predictions": {key: dict(sorted(value.items()))
                            for key, value in sorted(
                                self.predictions.items())},
            "model_ids": dict(sorted(self.model_ids.items())),
            "dataset_id": self.dataset_id,
            "warnings": list(self.warnings),
        }


def prediction_snapshot(
    report: ml.TrainingReport, *,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> PredictionSnapshot:
    """Capture the model metadata behind the overlay for auditability."""
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    model_ids: dict[str, str] = {}
    warnings: list[str] = []
    for evaluation in report.evaluations:
        if evaluation.status == ml.ST_TRAINED and evaluation.model_id:
            model_ids[evaluation.target] = evaluation.model_id
        else:
            warnings.append(
                f"target {evaluation.target}: {evaluation.reason or 'not trained'}")
    return PredictionSnapshot(
        generated_at=stamp, feature_set=report.feature_set,
        predictions={}, model_ids=model_ids, dataset_id=report.dataset_id,
        warnings=tuple(warnings))


def comparison_path(root: str) -> str:
    return f"{root}/scheduler/comparison.json"


def persist_comparison(root: str,
                       comparison: ScheduleComparison) -> str:
    model.write_json(comparison_path(root), comparison.to_dict())
    return comparison_path(root)


__all__ = [
    "CandidateScore",
    "MODE_ML",
    "MODE_RULE",
    "PredictionSnapshot",
    "ScheduleCandidate",
    "ScheduleComparison",
    "ScheduleOverlay",
    "ScoreComponent",
    "build_overlay",
    "compare_schedulers",
    "comparison_path",
    "persist_comparison",
    "prediction_snapshot",
    "score_candidate",
]
