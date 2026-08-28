"""Pure, explicit, leakage-safe V1.15 effort-calibration factor proposals (V1.15).

Given the authoritative V1.13 durable calibration profile and the V1.14
explicit sample-sufficiency assessment derived from that SAME profile, this
boundary produces an IMMUTABLE, EXACT integer-ratio proposal set with one
per-segment multiplicative factor proposal:

>>> adjusted_estimate = planned_duration_seconds * numerator / denominator

This is the FIRST consumer of V1.13 + V1.14. Nothing in this module is
persisted, estimated, applied, or compared to future estimates.

Deterministic-only invariants:

* No floats, Decimals, or statistics.
* No wall-clock, randomness, or external input; identical inputs yield
  identical immutable results.
* No provider, model, ML, or AI involvement.
* No write, status transition, or side effect.
* No estimate/observation history inspection.
* No repository or reader is accepted.
* No implicit policy and no default fallback.
* A project-wide overall factor is never invented.

Consumes exactly:

* one AUTHORITATIVE V1.13 calibration profile object
* its EXACTLY ALIGNED V1.14 sufficiency assessment

V1.13 remains authoritative for Portfolio structure, sample construction,
and type segmentation; V1.14 remains the sole owner of the explicit
``minimum_sample_count`` policy and of the per-segment sufficiency flag.
V1.15 neither invents, regroups, nor recomputes any profile data: every
value is copied or exact-integer-derived from the authoritative inputs.

No mutation of the inputs. No persistence. No provider, model, or AI
involved.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_profile import (
    WorkBreakdownEffortCalibrationProfile,
)
from trajectory_os.domain.execution_effort_calibration_sufficiency import (
    WorkBreakdownEffortCalibrationSufficiencyAssessment,
)

__all__ = [
    "EffortCalibrationFactorProposalError",
    "EffortCalibrationFactorProposalReason",
    "EffortCalibrationTypeFactorProposal",
    "WorkBreakdownEffortCalibrationFactorProposalSet",
    "build_effort_calibration_factor_proposals",
]


class EffortCalibrationFactorProposalError(ValueError):
    """Raised when V1.15 factor-proposal derivation fails."""


class EffortCalibrationFactorProposalReason(StrEnum):
    """Exact, closed per-segment reason vocabulary (V1.15).

    AVAILABLE:
        The V1.14 assessment marks the aligned segment as having sufficient
        samples and the V1.13 segment's exact planned total is > 0. An exact
        reduced integer factor ``numerator / denominator`` is present.
    INSUFFICIENT_SAMPLES:
        The V1.14 assessment marks the aligned segment as NOT having
        sufficient samples under the explicit policy. No factor is proposed.
    ZERO_TOTAL_PLANNED_DURATION:
        The V1.13 segment's exact planned total is zero, so no multiplicative
        factor can be defined. No factor is proposed.
    """

    AVAILABLE = "available"
    INSUFFICIENT_SAMPLES = "insufficient_samples"
    ZERO_TOTAL_PLANNED_DURATION = "zero_total_planned_duration"


def _require_genuine_profile(
    profile: object,
) -> WorkBreakdownEffortCalibrationProfile:
    """Require the exact V1.13 profile type and freshly revalidate it.

    The caller may supply a hostile instance bypassing construction
    validation, so genuine revalidation is mandatory (V1.13 invariants are
    exact, including the conservation rules).

    Raises :class:`EffortCalibrationFactorProposalError` on any failure.
    """
    if not isinstance(profile, WorkBreakdownEffortCalibrationProfile):
        raise EffortCalibrationFactorProposalError(
            "profile must be an authoritative WorkBreakdown"
            f"EffortCalibrationProfile (V1.13); got {type(profile).__name__}"
        )
    try:
        return WorkBreakdownEffortCalibrationProfile.model_validate(
            profile.model_dump(mode="python"), strict=True
        )
    except ValueError as exc:
        raise EffortCalibrationFactorProposalError(
            "V1.15 calibration factor proposal is unavailable because the "
            f"V1.13 profile is invalid: {exc}"
        ) from exc


def _require_genuine_sufficiency(
    sufficiency: object,
) -> WorkBreakdownEffortCalibrationSufficiencyAssessment:
    """Require the exact V1.14 assessment type and freshly revalidate it.

    Raises :class:`EffortCalibrationFactorProposalError` on any failure.
    """
    if not isinstance(
        sufficiency, WorkBreakdownEffortCalibrationSufficiencyAssessment
    ):
        raise EffortCalibrationFactorProposalError(
            "sufficiency must be a WorkBreakdownEffortCalibration"
            "SufficiencyAssessment (V1.14); "
            f"got {type(sufficiency).__name__}"
        )
    try:
        return WorkBreakdownEffortCalibrationSufficiencyAssessment.model_validate(
            sufficiency.model_dump(mode="python"), strict=True
        )
    except ValueError as exc:
        raise EffortCalibrationFactorProposalError(
            "V1.15 calibration factor proposal is unavailable because the "
            f"V1.14 sufficiency assessment is invalid: {exc}"
        ) from exc


def _require_exact_segment_alignment(
    profile: WorkBreakdownEffortCalibrationProfile,
    sufficiency: WorkBreakdownEffortCalibrationSufficiencyAssessment,
) -> None:
    """Require exact identity and order alignment between V1.13 and V1.14.

    Checks exactly: portfolio_id, project_id, segment count, per-index
    entity_type, and per-index sample_count. Any mismatch fails closed with
    no partial result.
    """
    if profile.portfolio_id != sufficiency.portfolio_id:
        raise EffortCalibrationFactorProposalError(
            "V1.15 exact alignment failed: portfolio_id differs between the "
            f"V1.13 profile ({profile.portfolio_id}) and the V1.14 "
            f"sufficiency assessment ({sufficiency.portfolio_id})"
        )

    if profile.project_id != sufficiency.project_id:
        raise EffortCalibrationFactorProposalError(
            "V1.15 exact alignment failed: project_id differs between the "
            f"V1.13 profile ({profile.project_id}) and the V1.14 "
            f"sufficiency assessment ({sufficiency.project_id})"
        )

    profile_segments = profile.segments
    assessed_segments = sufficiency.segments
    if len(profile_segments) != len(assessed_segments):
        raise EffortCalibrationFactorProposalError(
            "V1.15 exact alignment failed: segment count differs between "
            f"the V1.13 profile ({len(profile_segments)}) and the V1.14 "
            f"sufficiency assessment ({len(assessed_segments)})"
        )

    for index, (profile_segment, assessed_segment) in enumerate(
        zip(profile_segments, assessed_segments, strict=True)
    ):
        if profile_segment.entity_type != assessed_segment.entity_type:
            raise EffortCalibrationFactorProposalError(
                "V1.15 exact alignment failed: entity_type differs at "
                f"segment index {index} (V1.13 "
                f"{profile_segment.entity_type!r} vs V1.14 "
                f"{assessed_segment.entity_type!r})"
            )
        if profile_segment.summary.sample_count != assessed_segment.sample_count:
            raise EffortCalibrationFactorProposalError(
                "V1.15 exact alignment failed: sample_count differs at "
                f"segment index {index} (V1.13 "
                f"{profile_segment.summary.sample_count} vs V1.14 "
                f"{assessed_segment.sample_count})"
            )


class EffortCalibrationTypeFactorProposal(BaseModel):
    """One exact immutable per-segment multiplicative factor proposal (V1.15).

    All fields are copied from the authoritative aligned V1.13 segment and
    V1.14 assessment for the SAME segment. ``factor_numerator`` and
    ``factor_denominator`` are exact positive integer values satisfying::

        0 <= factor_numerator
        factor_denominator >= 1
        gcd(factor_numerator, factor_denominator) == 1

    whenever both are present, and the exact cross-multiplication identity::

        factor_numerator * total_planned_duration_seconds
            == factor_denominator * total_actual_duration_seconds

    The factor is never stored, computed, or compared as a float.

    Exact per-reason invariants:

    * AVAILABLE:
        proposal_available is true, both factors are present, the factor is
        fully reduced, sample_count >= 1, total_planned_duration_seconds
        >= 1, and the cross-multiplication identity holds.
    * INSUFFICIENT_SAMPLES:
        proposal_available is false and both factors are absent.
    * ZERO_TOTAL_PLANNED_DURATION:
        proposal_available is false, both factors are absent, and
        total_planned_duration_seconds is exactly zero.

    Never: floats, Decimals, uncertainty, confidence, or statistical
    adjustment.
    """

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        str_strip_whitespace=False,
    )

    entity_type: EntityType
    sample_count: Annotated[StrictInt, Field(ge=0)]
    total_planned_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    total_actual_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    proposal_available: bool
    reason: EffortCalibrationFactorProposalReason
    factor_numerator: Annotated[StrictInt, Field(ge=0)] | None = None
    factor_denominator: Annotated[StrictInt, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def _validate_reason_consistency(self) -> EffortCalibrationTypeFactorProposal:
        if (self.proposal_available is True) != (
            self.reason is EffortCalibrationFactorProposalReason.AVAILABLE
        ):
            raise ValueError(
                "proposal_available and reason must be consistent: only an "
                "AVAILABLE proposal is available, no other reason is"
            )

        if self.reason is EffortCalibrationFactorProposalReason.AVAILABLE:
            if self.sample_count < 1:
                raise ValueError(
                    "an AVAILABLE factor proposal requires sample_count >= 1"
                )
            if self.total_planned_duration_seconds < 1:
                raise ValueError(
                    "an AVAILABLE factor proposal requires "
                    "total_planned_duration_seconds >= 1"
                )
            if self.factor_numerator is None or self.factor_denominator is None:
                raise ValueError(
                    "an AVAILABLE factor proposal requires both "
                    "factor_numerator and factor_denominator"
                )
            if math.gcd(self.factor_numerator, self.factor_denominator) != 1:
                raise ValueError(
                    "factor_numerator and factor_denominator must be exact "
                    "reduced integer values with gcd == 1"
                )
            if (
                self.factor_numerator * self.total_planned_duration_seconds
                != self.factor_denominator * self.total_actual_duration_seconds
            ):
                raise ValueError(
                    "the proposed exact factor must satisfy "
                    "factor_numerator * total_planned_duration_seconds == "
                    "factor_denominator * total_actual_duration_seconds"
                )
        elif self.factor_numerator is not None or self.factor_denominator is not None:
            raise ValueError(
                "an unavailable factor proposal must not carry any factor "
                "components"
            )

        if (
            self.reason
            is EffortCalibrationFactorProposalReason.ZERO_TOTAL_PLANNED_DURATION
            and self.total_planned_duration_seconds != 0
        ):
            raise ValueError(
                "a ZERO_TOTAL_PLANNED_DURATION proposal requires "
                "total_planned_duration_seconds == 0"
            )

        return self


class WorkBreakdownEffortCalibrationFactorProposalSet(BaseModel):
    """Immutable project-level V1.15 factor-proposal set (V1.15).

    ``segments`` is the aligned per-segment tuple in AUTHORITATIVE V1.13
    order — not a mutable dict. ``minimum_required_sample_count`` is copied
    exactly from the authoritative V1.14 assessment. Segment count is
    conserved from the aligned inputs. No project-wide overall factor is
    ever proposed.
    """

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        str_strip_whitespace=False,
    )

    portfolio_id: UUID
    project_id: UUID
    minimum_required_sample_count: Annotated[StrictInt, Field(ge=1)]
    available_proposal_count: Annotated[StrictInt, Field(ge=0)]
    unavailable_proposal_count: Annotated[StrictInt, Field(ge=0)]
    segments: tuple[EffortCalibrationTypeFactorProposal, ...]

    @model_validator(mode="after")
    def _validate_conservation(self) -> (
        WorkBreakdownEffortCalibrationFactorProposalSet
    ):
        available = sum(
            1
            for segment in self.segments
            if segment.reason
            is EffortCalibrationFactorProposalReason.AVAILABLE
        )
        if available != self.available_proposal_count:
            raise ValueError(
                "available_proposal_count must equal the number of "
                "AVAILABLE segments"
            )
        if self.available_proposal_count + self.unavailable_proposal_count != (
            len(self.segments)
        ):
            raise ValueError(
                "available_proposal_count + unavailable_proposal_count "
                "must equal the number of segments"
            )
        entity_types = (segment.entity_type for segment in self.segments)
        if len(set(entity_types)) != len(self.segments):
            raise ValueError("entity_type must be unique within one project")

        return self


def build_effort_calibration_factor_proposals(
    profile: WorkBreakdownEffortCalibrationProfile,
    sufficiency: WorkBreakdownEffortCalibrationSufficiencyAssessment,
) -> WorkBreakdownEffortCalibrationFactorProposalSet:
    """Build the exact immutable V1.15 factor-proposal set.

    Sequence is exactly:

    ``require genuine exactly-validated V1.13 profile → require genuine
    exactly-validated V1.14 sufficiency assessment → require EXACT
    alignment (portfolio_id, project_id, segment count, per-index
    entity_type, per-index sample_count)
    → for each aligned segment in V1.13 order:
        not sufficient → INSUFFICIENT_SAMPLES (no factor)
        sufficient and planned total == 0 → ZERO_TOTAL_PLANNED_DURATION
        (no factor)
        sufficient and planned total > 0 → AVAILABLE with the exact reduced
        integer factor (actual_total, planned_total) reduced by their exact
        integer gcd
    → return immutable proposal set
    → STOP``.

    The factor is derived with exact integer arithmetic only:
    ``numerator = actual_total // gcd`` and
    ``denominator = planned_total // gcd``. No float, Decimal, or
    intermediate statistic is introduced anywhere.

    The aligned inputs are never mutated, and no repository, reader,
    persistence, wall-clock, randomness, or provider is accepted or
    consulted.
    """
    genuine_profile = _require_genuine_profile(profile)
    genuine_sufficiency = _require_genuine_sufficiency(sufficiency)
    _require_exact_segment_alignment(genuine_profile, genuine_sufficiency)

    segments: list[EffortCalibrationTypeFactorProposal] = []
    for profile_segment, assessed_segment in zip(
        genuine_profile.segments, genuine_sufficiency.segments, strict=True
    ):
        planned_total = profile_segment.summary.total_planned_duration_seconds
        actual_total = profile_segment.summary.total_actual_duration_seconds

        if not assessed_segment.has_sufficient_samples:
            segments.append(
                EffortCalibrationTypeFactorProposal(
                    entity_type=profile_segment.entity_type,
                    sample_count=assessed_segment.sample_count,
                    total_planned_duration_seconds=planned_total,
                    total_actual_duration_seconds=actual_total,
                    proposal_available=False,
                    reason=(
                        EffortCalibrationFactorProposalReason
                        .INSUFFICIENT_SAMPLES
                    ),
                )
            )
            continue

        if planned_total == 0:
            segments.append(
                EffortCalibrationTypeFactorProposal(
                    entity_type=profile_segment.entity_type,
                    sample_count=assessed_segment.sample_count,
                    total_planned_duration_seconds=0,
                    total_actual_duration_seconds=actual_total,
                    proposal_available=False,
                    reason=(
                        EffortCalibrationFactorProposalReason
                        .ZERO_TOTAL_PLANNED_DURATION
                    ),
                )
            )
            continue

        gcd = math.gcd(actual_total, planned_total)
        numerator = actual_total // gcd
        denominator = planned_total // gcd
        segments.append(
            EffortCalibrationTypeFactorProposal(
                entity_type=profile_segment.entity_type,
                sample_count=assessed_segment.sample_count,
                total_planned_duration_seconds=planned_total,
                total_actual_duration_seconds=actual_total,
                proposal_available=True,
                reason=EffortCalibrationFactorProposalReason.AVAILABLE,
                factor_numerator=numerator,
                factor_denominator=denominator,
            )
        )

    available = sum(
        1
        for segment in segments
        if segment.reason
        is EffortCalibrationFactorProposalReason.AVAILABLE
    )

    return WorkBreakdownEffortCalibrationFactorProposalSet(
        portfolio_id=genuine_profile.portfolio_id,
        project_id=genuine_profile.project_id,
        minimum_required_sample_count=(
            genuine_sufficiency.minimum_required_sample_count
        ),
        available_proposal_count=available,
        unavailable_proposal_count=len(segments) - available,
        segments=tuple(segments),
    )
