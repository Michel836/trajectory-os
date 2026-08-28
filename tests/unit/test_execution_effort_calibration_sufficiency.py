"""Unit tests for the pure V1.14 effort calibration sufficiency boundary."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort_calibration import (
    EffortCalibrationSample,
    EffortCalibrationSummary,
    WorkBreakdownEffortCalibrationEvidence,
)
from trajectory_os.domain.execution_effort_calibration_profile import (
    EffortCalibrationTypeSegment,
    WorkBreakdownEffortCalibrationProfile,
    build_effort_calibration_profile,
)
from trajectory_os.domain.execution_effort_calibration_sufficiency import (
    EffortCalibrationSufficiencyError,
    EffortCalibrationTypeSufficiency,
    WorkBreakdownEffortCalibrationSufficiencyAssessment,
    assess_effort_calibration_sufficiency,
)
from trajectory_os.domain.portfolio import Portfolio

PORTFOLIO_ID = UUID("23131313-1313-4313-8313-131313131313")
PROJECT_ID = UUID("24131313-1515-4515-8515-151515151515")
TASK_A_ID = UUID("25131616-1616-4616-8616-161616161616")
TASK_B_ID = UUID("26171717-1717-4717-8717-171717171717")
DELIVERABLE_ID = UUID("27181818-1818-4818-8818-181818181818")
PROJECT_TYPE_ID = UUID("28191919-1919-4919-8919-191919191919")

ESTIMATED_AT = datetime(2025, 1, 1, tzinfo=UTC)
FIRST_OBSERVED_AT = datetime(2025, 1, 2, tzinfo=UTC)
LAST_OBSERVED_AT = datetime(2025, 1, 3, tzinfo=UTC)


def _entity(entity_id: UUID, entity_type: EntityType) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id,
        entity_type=entity_type,
        title=f"Entity {str(entity_id)[:8]}",
        status=EntityStatus.COMPLETED,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=ESTIMATED_AT,
        updated_at=ESTIMATED_AT,
    )


def _portfolio(*entities: TrajectoryEntity) -> Portfolio:
    return Portfolio(
        id=PORTFOLIO_ID,
        name="V1.14 Sufficiency Portfolio",
        entities=list(entities),
        relations=[],
    )


def _sample(entity_id: UUID, planned: int, actual: int) -> EffortCalibrationSample:
    variance = actual - planned
    return EffortCalibrationSample(
        entity_id=entity_id,
        estimate_id=uuid4(),
        estimated_at=ESTIMATED_AT,
        first_observed_at=FIRST_OBSERVED_AT,
        last_observed_at=LAST_OBSERVED_AT,
        observation_count=1,
        planned_duration_seconds=planned,
        actual_duration_seconds=actual,
        variance_seconds=variance,
        absolute_error_seconds=abs(variance),
    )


def _summary(samples: tuple[EffortCalibrationSample, ...]) -> EffortCalibrationSummary:
    total_planned = sum(sample.planned_duration_seconds for sample in samples)
    total_actual = sum(sample.actual_duration_seconds for sample in samples)
    return EffortCalibrationSummary(
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


def _segment(
    entity_type: EntityType, samples: tuple[EffortCalibrationSample, ...]
) -> EffortCalibrationTypeSegment:
    return EffortCalibrationTypeSegment(
        entity_type=entity_type,
        sample_entity_ids=tuple(sample.entity_id for sample in samples),
        summary=_summary(samples),
    )


def _canonical_profile() -> WorkBreakdownEffortCalibrationProfile:
    """Authoritative V1.13 profile with three ordered segments.

    Segment order (authoritative V1.13 first-appearance order): TASK (2
    samples) → DELIVERABLE (1 sample) → PROJECT (1 sample).
    """
    task_a = _sample(TASK_A_ID, 100, 130)
    deliverable = _sample(DELIVERABLE_ID, 60, 60)
    task_b = _sample(TASK_B_ID, 90, 80)
    project = _sample(PROJECT_TYPE_ID, 0, 5)

    segments = (
        _segment(EntityType.TASK, (task_a, task_b)),
        _segment(EntityType.DELIVERABLE, (deliverable,)),
        _segment(EntityType.PROJECT, (project,)),
    )
    overall = _summary((task_a, deliverable, task_b, project))
    return WorkBreakdownEffortCalibrationProfile(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        completed_entity_count=4,
        completed_without_observation_count=0,
        completed_without_prior_estimate_count=0,
        overall_summary=overall,
        segments=segments,
    )


def _empty_profile() -> WorkBreakdownEffortCalibrationProfile:
    return WorkBreakdownEffortCalibrationProfile(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        completed_entity_count=0,
        completed_without_observation_count=0,
        completed_without_prior_estimate_count=0,
        overall_summary=_summary(()),
        segments=(),
    )


def test_result_models_are_frozen_and_strict() -> None:
    assessment = assess_effort_calibration_sufficiency(
        _canonical_profile(), 2
    )

    with pytest.raises(ValidationError):
        assessment.minimum_required_sample_count = 999  # type: ignore[misc]

    with pytest.raises(ValidationError):
        WorkBreakdownEffortCalibrationSufficiencyAssessment(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            minimum_required_sample_count="1",  # type: ignore[arg-type]
            sufficient_segment_count=0,
            insufficient_segment_count=0,
            segments=(),
        )

    with pytest.raises(ValidationError):
        EffortCalibrationTypeSufficiency(
            entity_type=EntityType.TASK,
            sample_count="2",  # type: ignore[arg-type]
            minimum_required_sample_count=1,
            has_sufficient_samples=True,
        )

    # The policy gate is exact: inconsistent flags are rejected.
    with pytest.raises(ValidationError):
        EffortCalibrationTypeSufficiency(
            entity_type=EntityType.TASK,
            sample_count=1,
            minimum_required_sample_count=2,
            has_sufficient_samples=True,
        )


@pytest.mark.parametrize("threshold", [0, -1, -100])
def test_nonpositive_minimum_sample_count_is_rejected(threshold: int) -> None:
    profile = _empty_profile()

    with pytest.raises(EffortCalibrationSufficiencyError, match="minimum_sample_count"):
        assess_effort_calibration_sufficiency(profile, threshold)


def test_bool_threshold_is_rejected() -> None:
    profile = _empty_profile()

    with pytest.raises(EffortCalibrationSufficiencyError):
        assess_effort_calibration_sufficiency(profile, True)  # type: ignore[arg-type]
    with pytest.raises(EffortCalibrationSufficiencyError):
        assess_effort_calibration_sufficiency(profile, False)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "threshold",
    [2.0, "2", "1", "2.0", None, [2], (2,)],
)
def test_non_integer_or_string_thresholds_are_rejected(threshold: object) -> None:
    profile = _empty_profile()

    with pytest.raises(EffortCalibrationSufficiencyError):
        assess_effort_calibration_sufficiency(profile, threshold)


def test_threshold_one_is_accepted() -> None:
    assessment = assess_effort_calibration_sufficiency(_canonical_profile(), 1)

    assert assessment.minimum_required_sample_count == 1
    assert all(
        segment.has_sufficient_samples for segment in assessment.segments
    )


def test_non_v113_profile_input_is_rejected() -> None:
    with pytest.raises(EffortCalibrationSufficiencyError, match="WorkBreakdown"):
        assess_effort_calibration_sufficiency(object(), 1)  # type: ignore[arg-type]

    with pytest.raises(EffortCalibrationSufficiencyError, match="WorkBreakdown"):
        assess_effort_calibration_sufficiency(
            "not-a-profile", 1  # type: ignore[arg-type]
        )


def test_hostile_constructed_profile_is_freshly_revalidated() -> None:
    zero_summary = _summary(())
    hostile = WorkBreakdownEffortCalibrationProfile.model_construct(
        portfolio_id=str(PORTFOLIO_ID),  # type: ignore[arg-type]
        project_id=PROJECT_ID,
        completed_entity_count=0,
        completed_without_observation_count=0,
        completed_without_prior_estimate_count=0,
        overall_summary=zero_summary,
        segments=(),
    )

    with pytest.raises(EffortCalibrationSufficiencyError, match="invalid"):
        assess_effort_calibration_sufficiency(hostile, 1)


def test_internally_inconsistent_profile_is_rejected() -> None:
    inconsistent = WorkBreakdownEffortCalibrationProfile.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        # Violates the V1.12 conservation invariant.
        completed_entity_count=99,
        completed_without_observation_count=0,
        completed_without_prior_estimate_count=0,
        overall_summary=_summary(()),
        segments=(),
    )

    with pytest.raises(EffortCalibrationSufficiencyError, match="invalid"):
        assess_effort_calibration_sufficiency(inconsistent, 1)


def test_segment_exactly_at_threshold_is_sufficient() -> None:
    profile = _canonical_profile()  # TASK segment has exactly 2 samples

    assessment = assess_effort_calibration_sufficiency(profile, 2)

    assert assessment.segments[0].sample_count == 2
    assert assessment.segments[0].minimum_required_sample_count == 2
    assert assessment.segments[0].has_sufficient_samples is True


def test_segment_above_threshold_is_sufficient() -> None:
    assessment = assess_effort_calibration_sufficiency(_canonical_profile(), 1)

    assert assessment.segments[0].sample_count == 2
    assert assessment.segments[0].has_sufficient_samples is True


def test_segment_below_threshold_is_insufficient() -> None:
    assessment = assess_effort_calibration_sufficiency(_canonical_profile(), 3)

    assert assessment.segments[0].sample_count == 2
    assert assessment.segments[0].has_sufficient_samples is False


def test_multiple_segments_preserve_v113_order_and_conservation() -> None:
    profile = _canonical_profile()

    assessment = assess_effort_calibration_sufficiency(profile, 2)

    # Order, one-to-one mapping, and exact counts copied from V1.13.
    assert [segment.entity_type for segment in assessment.segments] == [
        segment.entity_type for segment in profile.segments
    ]
    assert [segment.sample_count for segment in assessment.segments] == [
        segment.summary.sample_count for segment in profile.segments
    ]
    assert len(assessment.segments) == len(profile.segments)
    entity_types = [segment.entity_type for segment in assessment.segments]
    assert entity_types == [
        EntityType.TASK,
        EntityType.DELIVERABLE,
        EntityType.PROJECT,
    ]
    assert len(set(entity_types)) == len(entity_types)

    assert assessment.segments[0] == EffortCalibrationTypeSufficiency(
        entity_type=EntityType.TASK,
        sample_count=2,
        minimum_required_sample_count=2,
        has_sufficient_samples=True,
    )
    assert assessment.segments[1] == EffortCalibrationTypeSufficiency(
        entity_type=EntityType.DELIVERABLE,
        sample_count=1,
        minimum_required_sample_count=2,
        has_sufficient_samples=False,
    )
    assert assessment.segments[2] == EffortCalibrationTypeSufficiency(
        entity_type=EntityType.PROJECT,
        sample_count=1,
        minimum_required_sample_count=2,
        has_sufficient_samples=False,
    )

    # Exact segment-count conservation.
    assert assessment.sufficient_segment_count == 1
    assert assessment.insufficient_segment_count == 2
    assert (
        assessment.sufficient_segment_count + assessment.insufficient_segment_count
        == len(assessment.segments)
    )


def test_inconsistently_counted_assessment_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkBreakdownEffortCalibrationSufficiencyAssessment(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            minimum_required_sample_count=1,
            sufficient_segment_count=1,
            insufficient_segment_count=1,
            segments=(),
        )


def test_empty_profile_yields_empty_assessment() -> None:
    assessment = assess_effort_calibration_sufficiency(_empty_profile(), 5)

    assert assessment.portfolio_id == PORTFOLIO_ID
    assert assessment.project_id == PROJECT_ID
    assert assessment.minimum_required_sample_count == 5
    assert assessment.segments == ()
    assert assessment.sufficient_segment_count == 0
    assert assessment.insufficient_segment_count == 0


def test_no_v113_error_totals_are_copied() -> None:
    assert "total_planned_duration_seconds" not in (
        EffortCalibrationTypeSufficiency.model_fields
    )
    assert "total_actual_duration_seconds" not in (
        EffortCalibrationTypeSufficiency.model_fields
    )
    assert "absolute_error_seconds" not in (
        EffortCalibrationTypeSufficiency.model_fields
    )
    assert "overall_summary" not in (
        WorkBreakdownEffortCalibrationSufficiencyAssessment.model_fields
    )


def test_pure_boundary_consumes_only_the_v113_profile_and_policy() -> None:
    # The pure boundary is structurally incapable of inspecting estimate or
    # observation history: it takes exactly the authoritative V1.13 profile
    # and the explicit policy, and no repository or reader is accepted.
    parameters = list(assess_effort_calibration_sufficiency.__code__.co_varnames)[:2]
    assert parameters == ["profile", "minimum_sample_count"]

    profile = _empty_profile()
    assert assess_effort_calibration_sufficiency(profile, 1).segments == ()

def test_input_profile_is_unchanged_and_repeated_assessment_is_deterministic() -> None:
    profile = _canonical_profile()
    profile_before = copy.deepcopy(profile)

    first = assess_effort_calibration_sufficiency(profile, 2)
    second = assess_effort_calibration_sufficiency(
        copy.deepcopy(profile), 2
    )

    assert first == second
    assert profile == profile_before


def test_assessment_is_an_immutable_derivation_not_a_summary_model() -> None:
    # V1.14 must not regroup, recompute, or rebuild anything: the result
    # depends exactly on profile IDs, threshold, entity types, and counts.
    profile = _canonical_profile()

    low = assess_effort_calibration_sufficiency(profile, 1)
    high = assess_effort_calibration_sufficiency(profile, 2)

    assert (low.segments[0], low.segments[1], low.segments[2]) == (
        EffortCalibrationTypeSufficiency(
            entity_type=EntityType.TASK,
            sample_count=2,
            minimum_required_sample_count=1,
            has_sufficient_samples=True,
        ),
        EffortCalibrationTypeSufficiency(
            entity_type=EntityType.DELIVERABLE,
            sample_count=1,
            minimum_required_sample_count=1,
            has_sufficient_samples=True,
        ),
        EffortCalibrationTypeSufficiency(
            entity_type=EntityType.PROJECT,
            sample_count=1,
            minimum_required_sample_count=1,
            has_sufficient_samples=True,
        ),
    )
    assert high.sufficient_segment_count == 1
    assert low.sufficient_segment_count == 3
    assert low.insufficient_segment_count == 0


def test_pure_assessment_matches_domain_chain_from_v113_builder() -> None:
    # The pure V1.14 boundary consumes whatever authoritative V1.13 profile
    # is supplied; here we feed one produced by the real V1.13 builder.
    portfolio = _portfolio(
        _entity(TASK_A_ID, EntityType.TASK),
        _entity(DELIVERABLE_ID, EntityType.DELIVERABLE),
        _entity(TASK_B_ID, EntityType.TASK),
        _entity(PROJECT_TYPE_ID, EntityType.PROJECT),
    )
    evidence = WorkBreakdownEffortCalibrationEvidence(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        completed_entity_count=4,
        completed_without_observation_count=0,
        completed_without_prior_estimate_count=0,
        samples=(
            _sample(TASK_A_ID, 100, 130),
            _sample(DELIVERABLE_ID, 60, 60),
            _sample(TASK_B_ID, 90, 80),
            _sample(PROJECT_TYPE_ID, 0, 5),
        ),
        summary=_summary(
            (
                _sample(TASK_A_ID, 100, 130),
                _sample(DELIVERABLE_ID, 60, 60),
                _sample(TASK_B_ID, 90, 80),
                _sample(PROJECT_TYPE_ID, 0, 5),
            )
        ),
    )
    profile = build_effort_calibration_profile(portfolio, evidence)

    assessment = assess_effort_calibration_sufficiency(profile, 2)

    assert [segment.entity_type for segment in assessment.segments] == [
        segment.entity_type for segment in profile.segments
    ]
    assert assessment.segments[0].sample_count == 2
    assert assessment.segments[0].has_sufficient_samples is True
    assert assessment.sufficient_segment_count == 1
    assert assessment.insufficient_segment_count == 2
