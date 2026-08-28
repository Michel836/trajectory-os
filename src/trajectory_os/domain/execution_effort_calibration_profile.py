"""Deterministic calibration profile by CURRENT canonical entity type (V1.13).

This module derives an immutable, descriptive profile from one existing
trusted input:

- a V1.12 :class:`WorkBreakdownEffortCalibrationEvidence` (authoritative
  leakage-safe completed-work calibration samples, exact integer arithmetic);
- the CURRENT canonical :class:`Portfolio` (authoritative for each sample
  entity's CURRENT :class:`EntityType`).

The boundary is deliberately pure and purely descriptive. It performs no
persistence writes, no wall-clock reads, no provider/AI calls, no learning,
no correction factors, no forecasting, no scoring, no statistics beyond the
exact integer aggregates already fixed by V1.12, and no historical WBS or
entity-type reconstruction.

Semantic boundary (V1.13 question):

> Among the leakage-safe completed-work samples produced by V1.12, how is
> planned-vs-actual error distributed across the CURRENT canonical entity
> types represented in those samples?

The grouping key is deliberately each sample entity's **CURRENT canonical
``EntityType``** at derivation time. This is a CURRENT-type profile, not a
historical-type profile: V1.13 does not reconstruct the entity type at
estimation or execution time.

V1.13 must not recreate V1.12 calibration eligibility. It rereads no estimate
history, rescans no observations, rebuilds no V1.9 measurement, and
reinterprets no completion or leakage semantics.

Determinism rules:

- V1.12 ``samples`` are iterated in their existing authoritative order;
- a segment first appears when its ``EntityType`` is first encountered in
  that sample sequence, and segments preserve that first-appearance order;
- within a segment, ``sample_entity_ids`` preserves the exact relative V1.12
  sample order;
- each V1.12 sample entity ID appears exactly once across all segments
  (no sample may be duplicated or dropped).

Coverage semantics: the three global V1.12 coverage counts
(``completed_entity_count``, ``completed_without_observation_count``,
``completed_without_prior_estimate_count``) are preserved exactly. Per
EntityType exclusion coverage is deliberately NOT derived: V1.12 exposes
excluded entities only as global counts, not as identified entities, so
per-type exclusion coverage is a deliberate non-goal.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from trajectory_os.domain.entities import EntityType, TrajectoryEntity
from trajectory_os.domain.execution_effort_calibration import (
    EffortCalibrationSample,
    EffortCalibrationSummary,
    WorkBreakdownEffortCalibrationEvidence,
)
from trajectory_os.domain.portfolio import Portfolio


class EffortCalibrationProfileError(ValueError):
    """Raised when calibration-profile input is invalid."""


class EffortCalibrationTypeSegment(BaseModel):
    """One CURRENT canonical :class:`EntityType` segment of the profile.

    ``summary`` reuses the exact V1.12 :class:`EffortCalibrationSummary`
    integer vocabulary over only this segment's samples; no ratios, means,
    or correction factors are derived.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    entity_type: EntityType
    sample_entity_ids: tuple[UUID, ...]
    summary: EffortCalibrationSummary

    @model_validator(mode="after")
    def _validate_segment_consistency(self) -> EffortCalibrationTypeSegment:
        if self.summary.sample_count != len(self.sample_entity_ids):
            raise ValueError(
                "summary.sample_count must equal len(sample_entity_ids) "
                "within one segment"
            )
        if len(set(self.sample_entity_ids)) != len(self.sample_entity_ids):
            raise ValueError("duplicate sample entity ids within one segment")
        return self


class WorkBreakdownEffortCalibrationProfile(BaseModel):
    """Immutable descriptive calibration profile by CURRENT entity type.

    ``overall_summary`` is exactly the authoritative V1.12
    ``evidence.summary`` (never recomputed under different semantics).

    Global V1.12 coverage is preserved exactly, with the V1.12 invariant:

    ``completed_entity_count
    = overall_summary.sample_count
      + completed_without_observation_count
      + completed_without_prior_estimate_count``.

    Every segment aggregate and classification count conserves exactly
    against ``overall_summary``, and every sample entity ID appears in
    exactly one segment. No per-type exclusion coverage exists on this model
    on purpose: V1.12 identifies excluded entities only globally.

    The value is a pure deterministic derivation of the supplied trusted
    inputs; nothing is learned, corrected, or predicted.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    portfolio_id: UUID
    project_id: UUID
    completed_entity_count: Annotated[StrictInt, Field(ge=0)]
    completed_without_observation_count: Annotated[StrictInt, Field(ge=0)]
    completed_without_prior_estimate_count: Annotated[StrictInt, Field(ge=0)]
    overall_summary: EffortCalibrationSummary
    segments: tuple[EffortCalibrationTypeSegment, ...]

    @model_validator(mode="after")
    def _validate_profile_consistency(self) -> WorkBreakdownEffortCalibrationProfile:
        if (
            self.completed_entity_count
            != self.overall_summary.sample_count
            + self.completed_without_observation_count
            + self.completed_without_prior_estimate_count
        ):
            raise ValueError(
                "completed_entity_count must equal overall_summary.sample_count + "
                "completed_without_observation_count + "
                "completed_without_prior_estimate_count (V1.12 invariant)"
            )

        overall = self.overall_summary
        if (
            sum(segment.summary.sample_count for segment in self.segments)
            != overall.sample_count
        ):
            raise ValueError(
                "segment sample_count totals must conserve exactly against "
                "overall_summary.sample_count"
            )
        if (
            sum(
                segment.summary.total_planned_duration_seconds
                for segment in self.segments
            )
            != overall.total_planned_duration_seconds
        ):
            raise ValueError(
                "segment planned totals must conserve exactly against "
                "overall_summary.total_planned_duration_seconds"
            )
        if (
            sum(
                segment.summary.total_actual_duration_seconds
                for segment in self.segments
            )
            != overall.total_actual_duration_seconds
        ):
            raise ValueError(
                "segment actual totals must conserve exactly against "
                "overall_summary.total_actual_duration_seconds"
            )
        if (
            sum(segment.summary.signed_variance_seconds for segment in self.segments)
            != overall.signed_variance_seconds
        ):
            raise ValueError(
                "segment signed variance must conserve exactly against "
                "overall_summary.signed_variance_seconds"
            )
        if (
            sum(segment.summary.absolute_error_seconds for segment in self.segments)
            != overall.absolute_error_seconds
        ):
            raise ValueError(
                "segment absolute error must conserve exactly against "
                "overall_summary.absolute_error_seconds"
            )

        for field_name in (
            "underplanned_entity_count",
            "exact_entity_count",
            "overplanned_entity_count",
        ):
            if (
                sum(getattr(segment.summary, field_name) for segment in self.segments)
                != getattr(overall, field_name)
            ):
                raise ValueError(
                    f"segment {field_name} must conserve exactly against "
                    "overall_summary"
                )

        seen_types: set[EntityType] = set()
        seen_ids: set[UUID] = set()
        for segment in self.segments:
            if segment.entity_type in seen_types:
                raise ValueError(
                    f"duplicate entity type segment: {segment.entity_type}"
                )
            seen_types.add(segment.entity_type)
            for entity_id in segment.sample_entity_ids:
                if entity_id in seen_ids:
                    raise ValueError(
                        f"sample entity id appears in more than one segment: "
                        f"{entity_id}"
                    )
                seen_ids.add(entity_id)
        return self


def _revalidate_evidence(
    candidate: object,
) -> WorkBreakdownEffortCalibrationEvidence:
    """Return a freshly strictly revalidated copy of the supplied evidence.

    This defeats ``model_construct``/validation bypass on a hostile evidence
    object by forcing every field back through normal strict validation.
    """
    if not isinstance(candidate, WorkBreakdownEffortCalibrationEvidence):
        raise EffortCalibrationProfileError(
            "evidence must be a WorkBreakdownEffortCalibrationEvidence "
            f"instance, got {type(candidate).__name__}"
        )
    try:
        return WorkBreakdownEffortCalibrationEvidence.model_validate(
            candidate.model_dump(), strict=True
        )
    except Exception as exc:
        raise EffortCalibrationProfileError(
            "invalid WorkBreakdownEffortCalibrationEvidence supplied to the "
            "calibration profile"
        ) from exc


def _build_segment(
    entity_type: EntityType, samples: tuple[EffortCalibrationSample, ...]
) -> EffortCalibrationTypeSegment:
    total_planned = sum(sample.planned_duration_seconds for sample in samples)
    total_actual = sum(sample.actual_duration_seconds for sample in samples)

    summary = EffortCalibrationSummary(
        sample_count=len(samples),
        total_planned_duration_seconds=total_planned,
        total_actual_duration_seconds=total_actual,
        signed_variance_seconds=total_actual - total_planned,
        absolute_error_seconds=sum(sample.absolute_error_seconds for sample in samples),
        underplanned_entity_count=sum(
            1 for sample in samples if sample.variance_seconds > 0
        ),
        exact_entity_count=sum(1 for sample in samples if sample.variance_seconds == 0),
        overplanned_entity_count=sum(
            1 for sample in samples if sample.variance_seconds < 0
        ),
    )

    return EffortCalibrationTypeSegment(
        entity_type=entity_type,
        sample_entity_ids=tuple(sample.entity_id for sample in samples),
        summary=summary,
    )


def build_effort_calibration_profile(
    portfolio: Portfolio,
    evidence: WorkBreakdownEffortCalibrationEvidence,
) -> WorkBreakdownEffortCalibrationProfile:
    """Derive a deterministic descriptive profile over V1.12 samples.

    ``portfolio`` is authoritative for each sample entity's CURRENT
    canonical :class:`EntityType`. ``evidence`` is authoritative for sample
    membership, sample order, integer fields, and global coverage; V1.13
    neither recreates V1.12 eligibility nor recomputes
    ``evidence.summary`` under different semantics.

    For every V1.12 sample, in authoritative order:

    1. resolve ``sample.entity_id`` in the CURRENT Portfolio;
    2. read that entity's CURRENT ``EntityType``;
    3. assign the sample to exactly one type segment (segments appear in
       first-appearance order of their ``EntityType``);
    4. aggregate the exact integer V1.12 sample fields within the segment.

    Validation:

    - ``portfolio`` must be a real :class:`Portfolio` instance;
    - ``evidence`` must be a real
      :class:`WorkBreakdownEffortCalibrationEvidence` instance and is
      freshly and strictly revalidated to defeat ``model_construct``
      bypass;
    - ``evidence.portfolio_id`` must equal ``portfolio.id``;
    - every sample entity must still exist in the CURRENT Portfolio
      (fail explicitly rather than guess or drop);
    - hostile duplicate sample entity IDs are rejected.

    The boundary is pure and deterministic: inputs are not mutated and
    repeated equivalent calls yield equivalent immutable output.
    """

    if not isinstance(portfolio, Portfolio):
        raise EffortCalibrationProfileError(
            "portfolio must be a Portfolio instance, "
            f"got {type(portfolio).__name__}"
        )

    validated = _revalidate_evidence(evidence)

    if validated.portfolio_id != portfolio.id:
        raise EffortCalibrationProfileError(
            "evidence belongs to a different portfolio: "
            f"{validated.portfolio_id} != {portfolio.id}"
        )

    # Resolve every sample entity in the CURRENT portfolio; reject hostile
    # duplicates and unknown entities explicitly.
    resolved: list[TrajectoryEntity] = []
    seen: set[UUID] = set()
    for sample in validated.samples:
        if sample.entity_id in seen:
            raise EffortCalibrationProfileError(
                f"duplicate sample entity id in calibration evidence: "
                f"{sample.entity_id}"
            )
        seen.add(sample.entity_id)
        entity = portfolio.get_entity(sample.entity_id)
        if entity is None:
            raise EffortCalibrationProfileError(
                f"sample entity missing from the current portfolio: "
                f"{sample.entity_id}"
            )
        resolved.append(entity)

    # Group by CURRENT canonical EntityType, preserving the authoritative
    # V1.12 sample order for both first-appearance segment ordering and
    # within-segment id ordering.
    ordered: list[tuple[EntityType, list[EffortCalibrationSample]]] = []
    index_by_type: dict[EntityType, int] = {}
    for sample, entity in zip(validated.samples, resolved, strict=True):
        entity_type = entity.entity_type
        position = index_by_type.get(entity_type)
        if position is None:
            position = len(ordered)
            index_by_type[entity_type] = position
            ordered.append((entity_type, []))
        ordered[position][1].append(sample)

    segments = tuple(
        _build_segment(entity_type, tuple(items)) for entity_type, items in ordered
    )

    return WorkBreakdownEffortCalibrationProfile(
        portfolio_id=validated.portfolio_id,
        project_id=validated.project_id,
        completed_entity_count=validated.completed_entity_count,
        completed_without_observation_count=validated.completed_without_observation_count,
        completed_without_prior_estimate_count=(
            validated.completed_without_prior_estimate_count
        ),
        overall_summary=validated.summary,
        segments=segments,
    )
