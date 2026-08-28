"""Deterministic explicit sample-sufficiency assessment (V1.14).

This module derives an immutable readiness assessment from ONE existing
trusted input:

- an authoritative V1.13 :class:`WorkBreakdownEffortCalibrationProfile`;
- an explicit caller-supplied ``minimum_sample_count`` policy (no default,
  no magic threshold).

The boundary is deliberately pure and is a **policy gate only**. It answers:

> Given an explicit minimum-sample policy supplied by the caller, which
> CURRENT entity-type calibration segments have enough leakage-safe V1.12
> samples to be considered ready for a later human-reviewable calibration
> proposal?

It makes no statistical claim (no confidence interval, significance test,
or power analysis), performs no correction, prediction, learning, ranking,
persistence, wall-clock read, and provider/AI call.

The comparison is exactly:

    has_sufficient_samples
    = segment.summary.sample_count >= minimum_sample_count

V1.14 must not recreate prior evidence semantics. It rereads no estimates,
rescans no observations, rebuilds no V1.9 measurement or V1.12 evidence,
regroups no samples, recomputes no V1.13 summaries, and infers no
historical entity types.

Determinism rules:

- V1.13 segments are iterated in their existing authoritative order;
- every V1.13 segment maps exactly once to one readiness segment, in the
  same order;
- no dict/hash/global enum ordering may influence output order.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_profile import (
    WorkBreakdownEffortCalibrationProfile,
)


class EffortCalibrationSufficiencyError(ValueError):
    """Raised when sample-sufficiency input is invalid."""


class EffortCalibrationTypeSufficiency(BaseModel):
    """Readiness of one CURRENT canonical :class:`EntityType` segment.

    ``minimum_required_sample_count`` records the explicit caller policy
    applied to this segment. No statistical-validity claim is attached:
    ``has_sufficient_samples`` is a deterministic policy gate only.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    entity_type: EntityType
    sample_count: Annotated[StrictInt, Field(ge=0)]
    minimum_required_sample_count: Annotated[StrictInt, Field(ge=1)]
    has_sufficient_samples: bool

    @model_validator(mode="after")
    def _validate_sufficiency_consistency(self) -> EffortCalibrationTypeSufficiency:
        if self.has_sufficient_samples != (
            self.sample_count >= self.minimum_required_sample_count
        ):
            raise ValueError(
                "has_sufficient_samples must be exactly "
                "sample_count >= minimum_required_sample_count"
            )
        return self


class WorkBreakdownEffortCalibrationSufficiencyAssessment(BaseModel):
    """Immutable explicit sample-sufficiency assessment over V1.13 segments.

    ``sufficient_segment_count`` and ``insufficient_segment_count`` conserve
    exactly against ``len(segments)``. This is a readiness policy result,
    not another calibration summary model; no V1.13 error totals are copied.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    portfolio_id: UUID
    project_id: UUID
    minimum_required_sample_count: Annotated[StrictInt, Field(ge=1)]
    sufficient_segment_count: Annotated[StrictInt, Field(ge=0)]
    insufficient_segment_count: Annotated[StrictInt, Field(ge=0)]
    segments: tuple[
        EffortCalibrationTypeSufficiency,
        ...,
    ]

    @model_validator(mode="after")
    def _validate_assessment_consistency(
        self,
    ) -> WorkBreakdownEffortCalibrationSufficiencyAssessment:
        if (
            self.sufficient_segment_count
            + self.insufficient_segment_count
            != len(self.segments)
        ):
            raise ValueError(
                "sufficient_segment_count + insufficient_segment_count "
                "must equal len(segments)"
            )
        if any(
            segment.minimum_required_sample_count
            != self.minimum_required_sample_count
            for segment in self.segments
        ):
            raise ValueError(
                "every segment must record the assessment's "
                "minimum_required_sample_count"
            )
        return self


def _require_strict_minimum_sample_count(
    minimum_sample_count: object,
) -> int:
    """Validate the explicit policy threshold without any coercion.

    ``minimum_sample_count`` must be a strict integer >= 1. ``bool`` is
    rejected even though it subclasses ``int`` in Python; floats, strings,
    and any other coercion are rejected. There is deliberately no default
    and no magic threshold.
    """
    if isinstance(minimum_sample_count, bool) or not isinstance(
        minimum_sample_count, int
    ):
        raise EffortCalibrationSufficiencyError(
            "minimum_sample_count must be a strict integer >= 1, got "
            f"{type(minimum_sample_count).__name__}"
        )
    if minimum_sample_count < 1:
        raise EffortCalibrationSufficiencyError(
            "minimum_sample_count must be >= 1, got "
            f"{minimum_sample_count}"
        )
    return minimum_sample_count


def _revalidate_profile(candidate: object) -> WorkBreakdownEffortCalibrationProfile:
    """Return a freshly strictly revalidated copy of the supplied profile.

    This defeats ``model_construct``/validation bypass on a hostile profile
    object by forcing every field back through normal strict validation,
    including every V1.13 consistency validator.
    """
    if not isinstance(candidate, WorkBreakdownEffortCalibrationProfile):
        raise EffortCalibrationSufficiencyError(
            "profile must be a WorkBreakdownEffortCalibrationProfile "
            f"instance, got {type(candidate).__name__}"
        )
    try:
        return WorkBreakdownEffortCalibrationProfile.model_validate(
            candidate.model_dump(), strict=True
        )
    except Exception as exc:
        raise EffortCalibrationSufficiencyError(
            "invalid WorkBreakdownEffortCalibrationProfile supplied to the "
            "sufficiency assessment"
        ) from exc


def assess_effort_calibration_sufficiency(
    profile: WorkBreakdownEffortCalibrationProfile,
    minimum_sample_count: int,
) -> WorkBreakdownEffortCalibrationSufficiencyAssessment:
    """Assess explicit sample sufficiency over an authoritative V1.13 profile.

    The caller supplies the policy; V1.14 hard-codes nothing.

    For every V1.13 segment, in the existing authoritative segment order:

    1. read ``segment.entity_type``;
    2. read ``segment.summary.sample_count`` exactly;
    3. compare the exact integer count with the explicit caller threshold;
    4. emit one immutable sufficiency result for that segment.

    Validation:

    - ``minimum_sample_count`` is validated before anything else and
      without coercion (strict integer >= 1; ``bool`` rejected;
      floats/strings rejected);
    - ``profile`` must be a real
      :class:`WorkBreakdownEffortCalibrationProfile` instance and is
      freshly and strictly revalidated to defeat ``model_construct``
      bypass;
    - the authoritative profile IDs are copied exactly;
    - the source profile is not mutated and repeated equivalent calls
      yield equivalent immutable output.

    The boundary performs no repository/reader access, no persistence,
    no wall-clock read, and no provider/AI call.
    """
    minimum_required = _require_strict_minimum_sample_count(minimum_sample_count)

    validated = _revalidate_profile(profile)

    segments = tuple(
        EffortCalibrationTypeSufficiency(
            entity_type=segment.entity_type,
            sample_count=segment.summary.sample_count,
            minimum_required_sample_count=minimum_required,
            has_sufficient_samples=(
                segment.summary.sample_count >= minimum_required
            ),
        )
        for segment in validated.segments
    )

    sufficient = sum(1 for segment in segments if segment.has_sufficient_samples)

    return WorkBreakdownEffortCalibrationSufficiencyAssessment(
        portfolio_id=validated.portfolio_id,
        project_id=validated.project_id,
        minimum_required_sample_count=minimum_required,
        sufficient_segment_count=sufficient,
        insufficient_segment_count=len(segments) - sufficient,
        segments=segments,
    )
