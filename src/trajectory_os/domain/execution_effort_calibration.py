"""Leakage-safe completed-work effort calibration evidence (V1.12).

This module derives an immutable calibration-evidence view from three
existing trusted inputs:

- the CURRENT canonical :class:`Portfolio` (authoritative for entity status);
- a V1.9 :class:`WorkBreakdownEffortMeasurement` (authoritative actual direct
  effort: ``duration_seconds``, ``observation_count``,
  ``first_observed_at``, ``last_observed_at``);
- durable V1.10 :class:`ExecutionEffortEstimate` values.

The boundary is deliberately pure. It performs no persistence writes, no
wall-clock reads, no provider/AI calls, no learning, no correction factors,
no forecasting, no scoring, and no historical WBS reconstruction.

Calibration eligibility (leakage safety):

> only estimates recorded **strictly before** the entity's first observed
> effort may be used; ``estimated_at < first_observed_at`` is a STRICT
> inequality. An estimate at or after the first observation is ignored. The
> latest eligible estimate is selected with the deterministic
> ``max(estimated_at, estimate_id.int)`` tie-break convention of V1.10, so
> post-observation revisions can never leak into calibration.

Signed variance remains exactly the V1.11 convention:
``variance_seconds = actual_duration_seconds - planned_duration_seconds``,
using exact integer seconds. Positive means actual exceeded plan
(underplanned); negative means actual is below plan (overplanned).

Only CURRENT-WBS entities whose canonical status is
:attr:`EntityStatus.COMPLETED` may yield samples. Non-completed entities are
never calibration samples. Completed entities with no direct observations are
counted as missing actual evidence (never interpreted as ``actual = 0``);
completed entities with observations but no strictly prior estimate are
counted as missing prior planning evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from trajectory_os.domain.entities import EntityStatus, TrajectoryEntity
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.execution_effort_measurement import (
    WorkBreakdownEffortMeasurement,
)
from trajectory_os.domain.portfolio import Portfolio


class ExecutionEffortCalibrationError(ValueError):
    """Raised when calibration-evidence input is invalid."""


def _require_aware_datetime(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class EffortCalibrationSample(BaseModel):
    """One leakage-safe completed-work calibration sample (direct effort only).

    The selected estimate is guaranteed to satisfy the strict eligibility
    rule ``estimated_at < first_observed_at``; variance uses the exact V1.11
    signed convention and exact integer seconds.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    entity_id: UUID
    estimate_id: UUID
    estimated_at: datetime
    first_observed_at: datetime
    last_observed_at: datetime
    observation_count: Annotated[StrictInt, Field(ge=1)]
    planned_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    actual_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    variance_seconds: StrictInt
    absolute_error_seconds: Annotated[StrictInt, Field(ge=0)]

    @field_validator("estimated_at", "first_observed_at", "last_observed_at")
    @classmethod
    def _validate_aware_datetime(
        cls, value: datetime | None, info: object
    ) -> datetime | None:
        field_name = getattr(info, "field_name", "estimated_at")
        return _require_aware_datetime(value, field_name)

    @model_validator(mode="after")
    def _validate_sample_consistency(self) -> EffortCalibrationSample:
        if self.estimated_at >= self.first_observed_at:
            raise ValueError(
                "calibration estimate must be strictly before the first "
                "observation (estimated_at < first_observed_at)"
            )
        if self.first_observed_at > self.last_observed_at:
            raise ValueError("first_observed_at must not be after last_observed_at")
        if self.observation_count <= 0:
            raise ValueError("calibration sample requires at least one observation")
        expected_variance = self.actual_duration_seconds - self.planned_duration_seconds
        if self.variance_seconds != expected_variance:
            raise ValueError(
                "variance_seconds must equal actual - planned "
                "(V1.11 signed convention)"
            )
        if self.absolute_error_seconds != abs(expected_variance):
            raise ValueError("absolute_error_seconds must equal abs(variance_seconds)")
        return self


class EffortCalibrationSummary(BaseModel):
    """Immutable exact integer aggregate of calibration samples.

    No ratios, means, normalized scores, or correction factors are derived;
    aggregate arithmetic uses exact integer seconds only.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    sample_count: Annotated[StrictInt, Field(ge=0)]
    total_planned_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    total_actual_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    signed_variance_seconds: StrictInt
    absolute_error_seconds: Annotated[StrictInt, Field(ge=0)]
    underplanned_entity_count: Annotated[StrictInt, Field(ge=0)]
    exact_entity_count: Annotated[StrictInt, Field(ge=0)]
    overplanned_entity_count: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def _validate_summary_consistency(self) -> EffortCalibrationSummary:
        if (
            self.underplanned_entity_count
            + self.exact_entity_count
            + self.overplanned_entity_count
            != self.sample_count
        ):
            raise ValueError(
                "classification counts must sum exactly to sample_count"
            )
        if self.sample_count == 0:
            if (
                self.total_planned_duration_seconds != 0
                or self.total_actual_duration_seconds != 0
                or self.signed_variance_seconds != 0
                or self.absolute_error_seconds != 0
            ):
                raise ValueError("zero samples require all-zero aggregates")
            return self

        if (
            self.signed_variance_seconds
            != self.total_actual_duration_seconds - self.total_planned_duration_seconds
        ):
            raise ValueError(
                "signed_variance_seconds must equal total_actual - total_planned"
            )
        return self


class WorkBreakdownEffortCalibrationEvidence(BaseModel):
    """Immutable leakage-safe calibration evidence for one CURRENT project WBS.

    Coverage accounting is internally consistent:

    ``completed_entity_count
    = sample_count
      + completed_without_observation_count
      + completed_without_prior_estimate_count``.

    No calibration state is persisted and nothing is learned; the value is a
    pure deterministic derivation of the supplied trusted inputs.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    portfolio_id: UUID
    project_id: UUID
    completed_entity_count: Annotated[StrictInt, Field(ge=0)]
    completed_without_observation_count: Annotated[StrictInt, Field(ge=0)]
    completed_without_prior_estimate_count: Annotated[StrictInt, Field(ge=0)]
    samples: tuple[EffortCalibrationSample, ...]
    summary: EffortCalibrationSummary

    @model_validator(mode="after")
    def _validate_evidence_consistency(self) -> WorkBreakdownEffortCalibrationEvidence:
        if (
            self.completed_entity_count
            != self.summary.sample_count
            + self.completed_without_observation_count
            + self.completed_without_prior_estimate_count
        ):
            raise ValueError(
                "completed_entity_count must equal sample_count + "
                "completed_without_observation_count + "
                "completed_without_prior_estimate_count"
            )
        if self.summary.sample_count != len(self.samples):
            raise ValueError("summary.sample_count must equal len(samples)")
        expected_planned = sum(sample.planned_duration_seconds for sample in self.samples)
        expected_actual = sum(sample.actual_duration_seconds for sample in self.samples)
        if self.summary.total_planned_duration_seconds != expected_planned:
            raise ValueError(
                "total_planned_duration_seconds must equal the sum over samples"
            )
        if self.summary.total_actual_duration_seconds != expected_actual:
            raise ValueError(
                "total_actual_duration_seconds must equal the sum over samples"
            )
        return self


def _revalidate_measurement(
    candidate: object,
) -> WorkBreakdownEffortMeasurement:
    if not isinstance(candidate, WorkBreakdownEffortMeasurement):
        raise ExecutionEffortCalibrationError(
            "measurement must be a WorkBreakdownEffortMeasurement instance, "
            f"got {type(candidate).__name__}"
        )
    try:
        return WorkBreakdownEffortMeasurement.model_validate(
            candidate.model_dump(), strict=True
        )
    except Exception as exc:
        raise ExecutionEffortCalibrationError(
            "invalid WorkBreakdownEffortMeasurement supplied to calibration"
        ) from exc


def _revalidate_estimate(candidate: object) -> ExecutionEffortEstimate:
    if not isinstance(candidate, ExecutionEffortEstimate):
        raise ExecutionEffortCalibrationError(
            "every estimate must be an ExecutionEffortEstimate instance"
        )

    # Access fields directly instead of serializing the caller-owned instance.
    # This avoids serializer warnings for deliberately hostile
    # ``model_construct`` values while still forcing every field back through
    # normal strict validation.
    payload = {
        "id": getattr(candidate, "id", None),
        "portfolio_id": getattr(candidate, "portfolio_id", None),
        "entity_id": getattr(candidate, "entity_id", None),
        "duration_seconds": getattr(candidate, "duration_seconds", None),
        "estimated_at": getattr(candidate, "estimated_at", None),
        "source": getattr(candidate, "source", None),
    }

    try:
        return ExecutionEffortEstimate.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise ExecutionEffortCalibrationError(
            "invalid execution-effort estimate supplied to calibration"
        ) from exc


def build_effort_calibration_evidence(
    portfolio: Portfolio,
    measurement: WorkBreakdownEffortMeasurement,
    estimates: Iterable[ExecutionEffortEstimate],
) -> WorkBreakdownEffortCalibrationEvidence:
    """Derive leakage-safe calibration evidence over the CURRENT project WBS.

    ``portfolio`` is authoritative for CURRENT canonical entity status.
    ``measurement`` must be a valid V1.9 measurement of the same portfolio;
    every measurement item must resolve to a canonical entity in
    ``portfolio``. All supplied estimates are strictly revalidated, must
    belong to ``portfolio.id``, and must carry globally unique estimate IDs.
    Estimates attached to entities outside the measured CURRENT WBS remain
    legitimate history but contribute nothing.

    Deterministic classification, in preorder of the measurement:

    1. non-completed entity → ignored entirely;
    2. completed, zero direct observations → missing actual evidence;
    3. completed, with observations but no estimate strictly before
       ``first_observed_at`` → missing prior planning evidence;
    4. otherwise → one calibration sample whose estimate is the latest one
       with ``estimated_at < first_observed_at`` selected by the V1.10
       ``max(estimated_at, estimate_id.int)`` convention.

    The boundary is pure: it does not mutate any input and derives no
    learning, correction, forecasting, or scoring.
    """

    if not isinstance(portfolio, Portfolio):
        raise ExecutionEffortCalibrationError(
            "portfolio must be a Portfolio instance, "
            f"got {type(portfolio).__name__}"
        )

    validated_measurement = _revalidate_measurement(measurement)

    if validated_measurement.portfolio_id != portfolio.id:
        raise ExecutionEffortCalibrationError(
            "measurement belongs to a different portfolio: "
            f"{validated_measurement.portfolio_id} != {portfolio.id}"
        )

    # Every measurement item must resolve to the same canonical entity in the
    # CURRENT portfolio; this defeats measurements pointing at fabricated
    # or removed entities.
    resolved: list[TrajectoryEntity] = []
    for item in validated_measurement.items:
        entity = portfolio.get_entity(item.entity_id)
        if entity is None:
            raise ExecutionEffortCalibrationError(
                f"measurement item references an entity missing from the "
                f"current portfolio: {item.entity_id}"
            )
        resolved.append(entity)

    # Group all strictly revalidated estimates by target entity. Entities
    # outside the measured WBS are legitimate history but contribute nothing.
    estimates_by_entity: dict[UUID, list[ExecutionEffortEstimate]] = {}
    seen_estimate_ids: set[UUID] = set()

    for candidate in estimates:
        estimate = _revalidate_estimate(candidate)

        if estimate.portfolio_id != portfolio.id:
            raise ExecutionEffortCalibrationError(
                "estimate belongs to a different portfolio: "
                f"{estimate.id} -> {estimate.portfolio_id}"
            )
        if estimate.id in seen_estimate_ids:
            raise ExecutionEffortCalibrationError(
                f"duplicate estimate id in calibration input: {estimate.id}"
            )
        seen_estimate_ids.add(estimate.id)
        estimates_by_entity.setdefault(estimate.entity_id, []).append(estimate)

    samples: list[EffortCalibrationSample] = []
    completed_without_observation = 0
    completed_without_prior_estimate = 0
    completed_entity_count = 0

    for item, entity in zip(validated_measurement.items, resolved, strict=True):
        if entity.status is not EntityStatus.COMPLETED:
            # Non-completed work is never calibration evidence.
            continue

        completed_entity_count += 1
        direct = item.direct

        if direct.observation_count == 0:
            # Conservatively: missing actual evidence, never ``actual = 0``.
            completed_without_observation += 1
            continue

        first_observed_at = direct.first_observed_at
        if first_observed_at is None or direct.last_observed_at is None:
            # Unreachable for a valid V1.9 summary with observations, but
            # defended explicitly rather than trusted.
            raise ExecutionEffortCalibrationError(
                f"observed entity has missing observation timestamps: "
                f"{item.entity_id}"
            )

        # Strict leakage safety: only estimates strictly before the first
        # observation qualify, with the V1.10 deterministic tie-break.
        eligible: ExecutionEffortEstimate | None = None
        eligible_key: tuple[datetime, int] | None = None
        for estimate in estimates_by_entity.get(item.entity_id, ()):
            if not estimate.estimated_at < first_observed_at:
                continue
            key = (estimate.estimated_at, estimate.id.int)
            if eligible_key is None or key > eligible_key:
                eligible = estimate
                eligible_key = key

        if eligible is None:
            completed_without_prior_estimate += 1
            continue

        actual = direct.duration_seconds
        planned = eligible.duration_seconds
        variance = actual - planned

        samples.append(
            EffortCalibrationSample(
                entity_id=item.entity_id,
                estimate_id=eligible.id,
                estimated_at=eligible.estimated_at,
                first_observed_at=first_observed_at,
                last_observed_at=direct.last_observed_at,
                observation_count=direct.observation_count,
                planned_duration_seconds=planned,
                actual_duration_seconds=actual,
                variance_seconds=variance,
                absolute_error_seconds=abs(variance),
            )
        )

    total_planned = sum(sample.planned_duration_seconds for sample in samples)
    total_actual = sum(sample.actual_duration_seconds for sample in samples)

    summary = EffortCalibrationSummary(
        sample_count=len(samples),
        total_planned_duration_seconds=total_planned,
        total_actual_duration_seconds=total_actual,
        signed_variance_seconds=total_actual - total_planned,
        absolute_error_seconds=sum(sample.absolute_error_seconds for sample in samples),
        underplanned_entity_count=sum(1 for sample in samples if sample.variance_seconds > 0),
        exact_entity_count=sum(1 for sample in samples if sample.variance_seconds == 0),
        overplanned_entity_count=sum(1 for sample in samples if sample.variance_seconds < 0),
    )

    return WorkBreakdownEffortCalibrationEvidence(
        portfolio_id=validated_measurement.portfolio_id,
        project_id=validated_measurement.project_id,
        completed_entity_count=completed_entity_count,
        completed_without_observation_count=completed_without_observation,
        completed_without_prior_estimate_count=completed_without_prior_estimate,
        samples=tuple(samples),
        summary=summary,
    )
