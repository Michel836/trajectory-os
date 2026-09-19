"""M057 — deterministic feature encoding with explicit missingness.

Features are grouped into two honest feature sets:

``PLANNING``
    Only information that exists *before* a run executes (task class,
    complexity, backend/provider/model/reviewer, prompt size). This is the
    default because a planning-time prediction must not peek at the outcome.
``TASK_SIZE``
    Adds measured task-size/resource features (changed files, patch size,
    GPU utilization, retries). Useful for retrospective analysis, clearly
    labelled, and never silently mixed into planning predictions.

Numeric missingness is never hidden: every numeric feature is encoded as
``(imputed_value, missing_indicator)``. The imputation median and categorical
vocabulary are fit **on the training split only**, so no validation/test
information leaks into training.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from trajectory_os.intelligence import dataset, model

#: Feature-set labels (closed set).
FEATURE_PLANNING = "PLANNING"
FEATURE_TASK_SIZE = "TASK_SIZE"

FEATURE_SETS = frozenset({FEATURE_PLANNING, FEATURE_TASK_SIZE})

#: Feature candidates measured before/at planning time.
PLANNING_NUMERIC: tuple[str, ...] = ("prompt_chars",)
PLANNING_CATEGORICAL: tuple[str, ...] = (
    "task_type", "complexity", "backend", "provider", "model", "reviewer",
)

#: Additional retrospective task-size/resource features.
TASK_SIZE_NUMERIC: tuple[str, ...] = (
    "changed_files", "patch_bytes", "patch_lines_changed",
    "gpu_utilization_pct", "retries",
)

#: Fields that must never be used as a feature for a given target.
TARGET_EXCLUSIONS: Mapping[str, frozenset[str]] = {
    "duration_s": frozenset({"duration_s", "review_wait_s", "ttft_ms"}),
    "success": frozenset({"readiness", "final_outcome", "block_reason",
                          "validation_failures", "review_failures"}),
    "blocked": frozenset({"readiness", "final_outcome", "block_reason",
                          "validation_failures", "review_failures"}),
    "repairs": frozenset({"repairs", "validation_failures",
                          "review_failures"}),
    "cost_usd": frozenset({"cost_usd"}),
}


@dataclass(frozen=True)
class FeatureSchema:
    """The explicit feature contract for one target/feature-set pair."""

    target: str
    feature_set: str
    numeric: tuple[str, ...]
    categorical: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "feature_set": self.feature_set,
            "numeric": list(self.numeric),
            "categorical": list(self.categorical),
        }

    @property
    def schema_id(self) -> str:
        return model.digest(self.to_dict(), domain=model.MATERIAL_DOMAIN)


def build_feature_schema(
    target: str, *, feature_set: str = FEATURE_PLANNING,
) -> FeatureSchema:
    """Build the leakage-aware schema for one target and feature set."""
    if target not in dataset.TARGET_FIELDS:
        model.fail(model.E_MALFORMED, f"unknown target {target!r}")
    if feature_set not in FEATURE_SETS:
        model.fail(model.E_MALFORMED, f"unknown feature set {feature_set!r}")
    excluded = TARGET_EXCLUSIONS.get(target, frozenset())
    numeric = tuple(
        name for name in (
            *PLANNING_NUMERIC,
            *(TASK_SIZE_NUMERIC if feature_set == FEATURE_TASK_SIZE else ()))
        if name not in excluded)
    categorical = tuple(
        name for name in PLANNING_CATEGORICAL if name not in excluded)
    return FeatureSchema(target=target, feature_set=feature_set,
                         numeric=numeric, categorical=categorical)


@dataclass
class FittedEncoder:
    """Training-fitted numeric imputation and categorical one-hot vocabulary."""

    schema: FeatureSchema
    numeric_medians: dict[str, float]
    categorical_vocabulary: dict[str, tuple[str, ...]]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def feature_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for name in self.schema.numeric:
            names.extend((f"num:{name}", f"missing:{name}"))
        for name in self.schema.categorical:
            for value in self.categorical_vocabulary.get(name, ()):
                names.append(f"cat:{name}={value}")
        return tuple(names)

    def dimension(self) -> int:
        return len(self.feature_names)

    def transform_row(self, observation: dataset.Observation) -> list[float]:
        row: list[float] = []
        for name in self.schema.numeric:
            raw = observation.value(name)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                row.append(self.numeric_medians.get(name, 0.0))
                row.append(1.0)
            else:
                row.append(float(raw))
                row.append(0.0)
        for name in self.schema.categorical:
            raw = observation.value(name)
            vocabulary = self.categorical_vocabulary.get(name, ())
            if isinstance(raw, str) and raw in vocabulary:
                for value in vocabulary:
                    row.append(1.0 if value == raw else 0.0)
            else:
                row.extend(0.0 for _ in vocabulary)
        return row

    def transform(
        self, observations: Sequence[dataset.Observation],
    ) -> list[list[float]]:
        return [self.transform_row(o) for o in observations]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema.to_dict(),
            "schema_id": self.schema.schema_id,
            "feature_names": list(self.feature_names),
            "dimension": self.dimension(),
            "numeric_medians": dict(sorted(self.numeric_medians.items())),
            "categorical_vocabulary": {
                key: list(value)
                for key, value in sorted(
                    self.categorical_vocabulary.items())},
        }


def fit_encoder(
    observations: Sequence[dataset.Observation],
    schema: FeatureSchema,
) -> FittedEncoder:
    """Fit medians/vocabulary on the training observations only."""
    medians: dict[str, float] = {}
    warnings: list[str] = []
    for name in schema.numeric:
        values = [
            float(raw) for o in observations
            if isinstance((raw := o.value(name)), (int, float))
            and not isinstance(raw, bool)]
        if values:
            medians[name] = _median(values)
        else:
            medians[name] = 0.0
            warnings.append(f"numeric feature {name!r} fully missing in train")
    vocabulary: dict[str, tuple[str, ...]] = {}
    for name in schema.categorical:
        seen = sorted({
            raw for o in observations
            if isinstance((raw := o.value(name)), str)})
        if not seen:
            warnings.append(
                f"categorical feature {name!r} fully missing in train")
        vocabulary[name] = tuple(seen)
    return FittedEncoder(schema=schema, numeric_medians=medians,
                         categorical_vocabulary=vocabulary,
                         warnings=tuple(warnings))


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


__all__ = [
    "FEATURE_PLANNING",
    "FEATURE_SETS",
    "FEATURE_TASK_SIZE",
    "FeatureSchema",
    "FittedEncoder",
    "PLANNING_CATEGORICAL",
    "PLANNING_NUMERIC",
    "TARGET_EXCLUSIONS",
    "TASK_SIZE_NUMERIC",
    "build_feature_schema",
    "fit_encoder",
]
