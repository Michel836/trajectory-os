"""M069 — guarded active learning / model refresh (champion vs challenger).

This is a *guarded* refresh workflow. It never promotes a model, never mutates
policy and never changes route/scheduler authority. It produces an explicit,
deterministic evaluation with a promotion recommendation that a human must
act on:

* dataset snapshot identity;
* champion model metadata and challenger evaluation;
* deterministic evaluation on the same leakage-free split;
* minimum sample thresholds;
* calibration comparison (classification);
* data-quality checks;
* drift indicators where supportable (otherwise explicitly ``UNSUPPORTED``);
* model-registry metadata and a rollback path;
* the terminal states ``PROMOTE``, ``NO_PROMOTION`` and ``INSUFFICIENT_DATA``.

There is no automatic promotion without an explicit evidence threshold, and
the output document records ``policy_mutation=false``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trajectory_os.intelligence import dataset as dataset_module
from trajectory_os.intelligence import ml
from trajectory_os.realworld import model

FAMILY = "ACTIVE_LEARNING"

#: Refresh states (closed set).
STATE_PROMOTE = "PROMOTE"
STATE_NO_PROMOTION = "NO_PROMOTION"
STATE_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

REFRESH_STATES = (STATE_PROMOTE, STATE_NO_PROMOTION, STATE_INSUFFICIENT_DATA)

#: Default minimum sample threshold for a refresh decision.
DEFAULT_MIN_SAMPLES = 20

#: Minimum relative improvement required to recommend promotion.
DEFAULT_MIN_RELATIVE_IMPROVEMENT = 0.05

#: Maximum calibration degradation tolerated (absolute ECE increase).
DEFAULT_MAX_CALIBRATION_DEGRADATION = 0.02

#: Maximum supported drift indicator (normalised mean shift).
DEFAULT_MAX_DRIFT = 0.25

_REGISTRY_DOMAIN = "trajectory-os.realworld.model-registry.v1"


@dataclass(frozen=True)
class DatasetSnapshot:
    """Identity and quality of one learning-dataset snapshot."""

    snapshot_id: str
    dataset_id: str
    row_count: int
    real_rows: int
    fixture_rows: int
    field_availability: Mapping[str, int]
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "dataset_id": self.dataset_id,
            "row_count": self.row_count,
            "real_rows": self.real_rows,
            "fixture_rows": self.fixture_rows,
            "field_availability": dict(sorted(self.field_availability.items())),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class ModelRecord:
    """Model-registry metadata for one (champion or challenger) model."""

    model_id: str
    target: str
    algorithm: str
    dataset_id: str
    metrics: Mapping[str, float | None]
    calibration_ece: float | None
    registered_at: str
    schema_version: int = model.SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "target": self.target,
            "algorithm": self.algorithm,
            "dataset_id": self.dataset_id,
            "metrics": dict(sorted(self.metrics.items())),
            "calibration_ece": self.calibration_ece,
            "registered_at": self.registered_at,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class RefreshReport:
    """The complete, human-reviewable refresh evidence document."""

    target: str
    snapshot: DatasetSnapshot
    champion: ModelRecord | None
    challenger: ModelRecord | None
    challenger_evaluation: Mapping[str, Any] | None
    state: str
    rationale: tuple[str, ...]
    minimum_samples: int
    sample_count: int
    min_relative_improvement: float
    calibration_comparison: Mapping[str, Any]
    quality_checks: Mapping[str, Any]
    drift: Mapping[str, Any]
    rollback: Mapping[str, Any]
    generated_at: str
    policy_mutation: bool = False
    route_authority_change: bool = False
    scheduler_authority_change: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "realworld_version": model.REALWORLD_VERSION,
            "kind": "model_refresh_report",
            "family": FAMILY,
            "target": self.target,
            "snapshot": self.snapshot.to_dict(),
            "champion": (self.champion.to_dict()
                         if self.champion is not None else None),
            "challenger": (self.challenger.to_dict()
                           if self.challenger is not None else None),
            "challenger_evaluation": (dict(self.challenger_evaluation)
                                      if self.challenger_evaluation is not None
                                      else None),
            "state": self.state,
            "rationale": list(self.rationale),
            "minimum_samples": self.minimum_samples,
            "sample_count": self.sample_count,
            "min_relative_improvement": self.min_relative_improvement,
            "calibration_comparison": dict(self.calibration_comparison),
            "quality_checks": dict(self.quality_checks),
            "drift": dict(self.drift),
            "rollback": dict(self.rollback),
            "generated_at": self.generated_at,
            "policy_mutation": self.policy_mutation,
            "route_authority_change": self.route_authority_change,
            "scheduler_authority_change": self.scheduler_authority_change,
        }

    def render_markdown(self) -> str:
        lines = [
            f"# Model refresh — {self.target}", "",
            f"- state: **{self.state}**",
            f"- snapshot: `{self.snapshot.snapshot_id}` "
            f"({self.snapshot.row_count} rows; "
            f"real={self.snapshot.real_rows}, "
            f"fixture={self.snapshot.fixture_rows})",
            f"- sample threshold: {self.minimum_samples} "
            f"(observed {self.sample_count})",
            f"- policy mutation: {self.policy_mutation}",
            "", "## Rationale", "",
        ]
        lines.extend(f"- {item}" for item in self.rationale)
        lines += ["", "## Calibration", "",
                  f"```json\n{_json(self.calibration_comparison)}\n```",
                  "", "## Data quality", "",
                  f"```json\n{_json(self.quality_checks)}\n```",
                  "", "## Drift", "",
                  f"```json\n{_json(self.drift)}\n```",
                  "", "## Rollback path", "",
                  f"```json\n{_json(self.rollback)}\n```"]
        return "\n".join(lines) + "\n"


def _json(payload: Mapping[str, Any]) -> str:
    return str(model.intel_model.canonical_json(dict(payload)))


def snapshot_from_dataset(
    learning: dataset_module.LearningDataset, *,
    observed_at: str = "",
) -> DatasetSnapshot:
    quality = learning.quality
    snapshot_id = model.digest(
        {"dataset_id": learning.dataset_id,
         "row_count": learning.row_count,
         "field_availability": dict(quality.field_availability)},
        domain=_REGISTRY_DOMAIN)[:32]
    return DatasetSnapshot(
        snapshot_id=snapshot_id, dataset_id=learning.dataset_id,
        row_count=learning.row_count, real_rows=quality.real_rows,
        fixture_rows=quality.fixture_rows,
        field_availability=dict(quality.field_availability),
        observed_at=observed_at or learning.built_at)


def _selected(evaluation: ml.TargetEvaluation) -> ml.AlgorithmReport | None:
    if evaluation.selected_algorithm is None:
        return None
    for algorithm in evaluation.algorithms:
        if algorithm.algorithm == evaluation.selected_algorithm:
            return algorithm
    return None


def _primary_metric(target: str) -> tuple[str, bool]:
    """Return ``(metric, higher_is_better)``."""
    if target in ("success", "blocked"):
        return "auc", True
    return "mae", False


def _value(metrics: Mapping[str, Any], metric: str) -> float | None:
    value = metrics.get(metric)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _calibration_ece(evaluation: ml.TargetEvaluation) -> float | None:
    calibration = evaluation.calibration
    if not isinstance(calibration, Mapping):
        return None
    after = calibration.get("after")
    if not isinstance(after, Mapping):
        return None
    value = after.get("expected_calibration_error")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def model_record_from_evaluation(
    evaluation: ml.TargetEvaluation, *, registered_at: str,
) -> ModelRecord | None:
    selected = _selected(evaluation)
    if selected is None:
        return None
    metric, _ = _primary_metric(evaluation.target)
    metrics = {metric: _value(selected.metrics, metric)}
    metrics["baseline_" + metric] = _value(evaluation.baseline, metric)
    return ModelRecord(
        model_id=evaluation.model_id, target=evaluation.target,
        algorithm=selected.algorithm, dataset_id=evaluation.dataset_id,
        metrics=metrics, calibration_ece=_calibration_ece(evaluation),
        registered_at=registered_at)


def _quality_checks(
    learning: dataset_module.LearningDataset, *,
    minimum_missingness: float = 0.9,
) -> dict[str, Any]:
    quality = learning.quality
    fields = quality.field_availability
    total = learning.row_count or 1
    sparse = sorted(
        field for field, count in fields.items()
        if count / total < minimum_missingness)
    return {
        "row_count": learning.row_count,
        "real_rows": quality.real_rows,
        "fixture_rows": quality.fixture_rows,
        "leakage_free": bool(learning.split_metadata.get("leakage_free")),
        "sparse_fields": sparse,
        "passed": learning.row_count > 0 and bool(
            learning.split_metadata.get("leakage_free")),
    }


def _drift_indicator(
    challenger: dataset_module.LearningDataset,
    champion: dataset_module.LearningDataset | None,
) -> dict[str, Any]:
    if champion is None:
        return {
            "status": "UNSUPPORTED",
            "reason": "no champion dataset snapshot is available to compare",
            "max_normalised_shift": None,
            "metrics": {},
        }
    fields = (
        "duration_s", "repairs", "blocked", "success", "cost_usd",
    )
    shifts: dict[str, float | None] = {}
    maximum: float | None = None
    for field_name in fields:
        left = _mean_target(champion, field_name)
        right = _mean_target(challenger, field_name)
        if left is None or right is None:
            shifts[field_name] = None
            continue
        denominator = max(abs(left), 1e-9)
        shift = min(1.0, abs(right - left) / denominator)
        shifts[field_name] = shift
        maximum = shift if maximum is None else max(maximum, shift)
    return {
        "status": "COMPUTED",
        "reason": None,
        "max_normalised_shift": maximum,
        "metrics": {key: value for key, value in sorted(shifts.items())},
    }


def _mean_target(learning: dataset_module.LearningDataset,
                 target: str) -> float | None:
    if target not in dataset_module.TARGET_FIELDS:
        return None
    values = [value for observation in learning.observations
              if (value := dataset_module.target_value(observation, target))
              is not None]
    if not values:
        return None
    return sum(values) / len(values)


def refresh_model(
    learning: dataset_module.LearningDataset, target: str, *,
    champion: ModelRecord | None = None,
    champion_dataset: dataset_module.LearningDataset | None = None,
    generated_at: str = "",
    minimum_samples: int = DEFAULT_MIN_SAMPLES,
    min_relative_improvement: float = DEFAULT_MIN_RELATIVE_IMPROVEMENT,
    max_calibration_degradation: float = DEFAULT_MAX_CALIBRATION_DEGRADATION,
    max_drift: float = DEFAULT_MAX_DRIFT,
) -> RefreshReport:
    """Evaluate a challenger against a champion; never promotes automatically."""
    stamp = generated_at or model.utc_now()
    snapshot = snapshot_from_dataset(learning, observed_at=stamp)
    quality = _quality_checks(learning)
    drift = _drift_indicator(learning, champion_dataset)
    evaluation = ml.evaluate_target(learning, target)
    rationale: list[str] = []

    if (evaluation.status != ml.ST_TRAINED
            or learning.row_count < minimum_samples):
        rationale.append(
            f"challenger evaluation is {evaluation.status}"
            + (f" ({evaluation.reason})" if evaluation.reason else ""))
        if learning.row_count < minimum_samples:
            rationale.append(
                f"only {learning.row_count} rows < minimum {minimum_samples}")
        return _report(
            target=target, snapshot=snapshot, champion=champion,
            challenger=None, evaluation=evaluation.to_dict(),
            state=STATE_INSUFFICIENT_DATA, rationale=tuple(rationale),
            minimum_samples=minimum_samples, sample_count=learning.row_count,
            min_relative_improvement=min_relative_improvement,
            calibration_comparison={"status": "NOT_EVALUATED",
                                    "reason": "insufficient data"},
            quality=quality, drift=drift, generated_at=stamp)

    challenger = model_record_from_evaluation(evaluation,
                                              registered_at=stamp)
    if challenger is None:
        rationale.append("no challenger algorithm was selected")
        return _report(
            target=target, snapshot=snapshot, champion=champion,
            challenger=None, evaluation=evaluation.to_dict(),
            state=STATE_NO_PROMOTION, rationale=tuple(rationale),
            minimum_samples=minimum_samples, sample_count=learning.row_count,
            min_relative_improvement=min_relative_improvement,
            calibration_comparison={"status": "NOT_EVALUATED",
                                    "reason": "no selected challenger"},
            quality=quality, drift=drift, generated_at=stamp)

    metric, higher_is_better = _primary_metric(target)
    challenger_value = challenger.metrics.get(metric)
    baseline_value = challenger.metrics.get("baseline_" + metric)
    calibration = {
        "status": "COMPUTED" if challenger.calibration_ece is not None
        else "UNAVAILABLE",
        "challenger_ece": challenger.calibration_ece,
        "champion_ece": (champion.calibration_ece
                         if champion is not None else None),
        "max_degradation": max_calibration_degradation,
    }

    if not quality["passed"]:
        rationale.append("data-quality checks failed; promotion withheld")
        return _report(
            target=target, snapshot=snapshot, champion=champion,
            challenger=challenger, evaluation=evaluation.to_dict(),
            state=STATE_NO_PROMOTION, rationale=tuple(rationale),
            minimum_samples=minimum_samples, sample_count=learning.row_count,
            min_relative_improvement=min_relative_improvement,
            calibration_comparison=calibration, quality=quality, drift=drift,
            generated_at=stamp)

    if champion is None:
        promotion = _beats_baseline(
            challenger_value, baseline_value, metric, higher_is_better,
            min_relative_improvement)
        rationale.append(
            "no registered champion: challenger compared to the naive "
            f"baseline ({metric})")
        state = STATE_PROMOTE if promotion else STATE_NO_PROMOTION
        if promotion:
            rationale.append(
                "challenger beats the naive baseline by the required margin")
        else:
            rationale.append(
                "challenger does not beat the naive baseline by the required "
                "margin; no superiority is claimed")
        return _report(
            target=target, snapshot=snapshot, champion=champion,
            challenger=challenger, evaluation=evaluation.to_dict(),
            state=state, rationale=tuple(rationale),
            minimum_samples=minimum_samples, sample_count=learning.row_count,
            min_relative_improvement=min_relative_improvement,
            calibration_comparison=calibration, quality=quality, drift=drift,
            generated_at=stamp)

    champion_value = champion.metrics.get(metric)
    beats_champion = _relative_improvement(
        challenger_value, champion_value, higher_is_better)
    calibration_ok = _calibration_ok(champion.calibration_ece,
                                     challenger.calibration_ece,
                                     max_calibration_degradation)
    drift_value = drift.get("max_normalised_shift")
    drift_ok = (drift.get("status") != "COMPUTED"
                or (isinstance(drift_value, (int, float))
                    and float(drift_value) <= max_drift))

    rationale.append(f"primary metric: {metric}")
    rationale.append(
        f"champion={champion_value} challenger={challenger_value}")
    rationale.append(f"beats champion by required margin: {beats_champion}")
    rationale.append(f"calibration acceptable: {calibration_ok}")
    rationale.append(f"drift acceptable: {drift_ok}")
    state = (STATE_PROMOTE
             if beats_champion and calibration_ok and drift_ok
             else STATE_NO_PROMOTION)
    if state == STATE_NO_PROMOTION:
        rationale.append("promotion withheld because an evidence threshold "
                         "was not met")
    return _report(
        target=target, snapshot=snapshot, champion=champion,
        challenger=challenger, evaluation=evaluation.to_dict(), state=state,
        rationale=tuple(rationale), minimum_samples=minimum_samples,
        sample_count=learning.row_count,
        min_relative_improvement=min_relative_improvement,
        calibration_comparison=calibration, quality=quality, drift=drift,
        generated_at=stamp)


def _beats_baseline(challenger: float | None, baseline: float | None,
                    metric: str, higher_is_better: bool,
                    min_relative: float) -> bool:
    if challenger is None or baseline is None:
        return False
    return _relative_improvement(challenger, baseline,
                                 higher_is_better) >= min_relative


def _relative_improvement(challenger: float | None, champion: float | None,
                          higher_is_better: bool) -> float:
    if challenger is None or champion is None:
        return 0.0
    if higher_is_better:
        if champion == 0:
            return 1.0 if challenger > champion else 0.0
        return (challenger - champion) / abs(champion)
    if champion == 0:
        return 1.0 if challenger < champion else 0.0
    return (champion - challenger) / abs(champion)


def _calibration_ok(champion_ece: float | None, challenger_ece: float | None,
                    maximum_degradation: float) -> bool:
    if champion_ece is None or challenger_ece is None:
        return True
    return (challenger_ece - champion_ece) <= maximum_degradation


def _report(
    *, target: str, snapshot: DatasetSnapshot, champion: ModelRecord | None,
    challenger: ModelRecord | None, evaluation: Mapping[str, Any] | None,
    state: str, rationale: tuple[str, ...], minimum_samples: int,
    sample_count: int, min_relative_improvement: float,
    calibration_comparison: Mapping[str, Any], quality: Mapping[str, Any],
    drift: Mapping[str, Any], generated_at: str,
) -> RefreshReport:
    if state not in REFRESH_STATES:
        model.intel_model.fail(model.intel_model.E_MALFORMED,
                               "unknown refresh state")
    rollback: dict[str, Any] = {
        "available": champion is not None,
        "champion_model_id": (champion.model_id
                              if champion is not None else None),
        "champion_algorithm": (champion.algorithm
                               if champion is not None else None),
        "champion_dataset_id": (champion.dataset_id
                                if champion is not None else None),
        "action": ("keep the current champion; a promoted challenger must be "
                   "reverted to this model id on regression"),
    }
    return RefreshReport(
        target=target, snapshot=snapshot, champion=champion,
        challenger=challenger, challenger_evaluation=evaluation, state=state,
        rationale=rationale, minimum_samples=minimum_samples,
        sample_count=sample_count,
        min_relative_improvement=min_relative_improvement,
        calibration_comparison=calibration_comparison, quality_checks=quality,
        drift=drift, rollback=rollback, generated_at=generated_at)


def registry_entry(report: RefreshReport) -> dict[str, Any]:
    """Return the registry metadata a *human* may persist after a PROMOTE."""
    if report.state != STATE_PROMOTE or report.challenger is None:
        return {
            "state": report.state,
            "registered": False,
            "reason": "only a PROMOTE recommendation may be registered by a "
                      "human; this function never registers automatically",
        }
    return {
        "state": report.state,
        "registered": False,
        "model": report.challenger.to_dict(),
        "promotion_rationale": list(report.rationale),
        "rollback": dict(report.rollback),
        "requires_human_action": True,
    }


def persist_report(root: str, report: RefreshReport) -> str:
    from pathlib import Path

    base = Path(root) / "realworld" / "model-refresh"
    base.mkdir(parents=True, exist_ok=True)
    name = f"{report.target}.json"
    model.write_json(str(base / name), report.to_dict())
    (base / f"{report.target}.md").write_text(report.render_markdown(),
                                              encoding="utf-8")
    return str(base / name)


__all__ = [
    "DEFAULT_MAX_CALIBRATION_DEGRADATION",
    "DEFAULT_MAX_DRIFT",
    "DEFAULT_MIN_RELATIVE_IMPROVEMENT",
    "DEFAULT_MIN_SAMPLES",
    "FAMILY",
    "REFRESH_STATES",
    "STATE_INSUFFICIENT_DATA",
    "STATE_NO_PROMOTION",
    "STATE_PROMOTE",
    "DatasetSnapshot",
    "ModelRecord",
    "RefreshReport",
    "model_record_from_evaluation",
    "persist_report",
    "refresh_model",
    "registry_entry",
    "snapshot_from_dataset",
]
