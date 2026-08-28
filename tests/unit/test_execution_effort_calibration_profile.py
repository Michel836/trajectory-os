"""Unit tests for the pure V1.13 effort calibration profile boundary."""

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
    EffortCalibrationProfileError,
    EffortCalibrationTypeSegment,
    WorkBreakdownEffortCalibrationProfile,
    build_effort_calibration_profile,
)
from trajectory_os.domain.portfolio import Portfolio

PORTFOLIO_ID = UUID("13131313-1313-4313-8313-131313131313")
OTHER_PORTFOLIO_ID = UUID("14141414-1414-4414-8414-141414141414")
PROJECT_ID = UUID("15151515-1515-4515-8515-151515151515")
TASK_A_ID = UUID("16161616-1616-4616-8616-161616161616")
TASK_B_ID = UUID("17171717-1717-4717-8717-171717171717")
DELIVERABLE_ID = UUID("18181818-1818-4818-8818-181818181818")
MISSING_ID = UUID("19191919-1919-4919-8919-191919191919")

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
        name="V1.13 Profile Portfolio",
        entities=list(entities),
        relations=[],
    )


def _sample(
    entity_id: UUID,
    planned: int,
    actual: int,
    *,
    estimate_id: UUID | None = None,
) -> EffortCalibrationSample:
    variance = actual - planned
    return EffortCalibrationSample(
        entity_id=entity_id,
        estimate_id=estimate_id or uuid4(),
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
            sample.variance_seconds > 0 for sample in samples
        ),
        exact_entity_count=sum(sample.variance_seconds == 0 for sample in samples),
        overplanned_entity_count=sum(
            sample.variance_seconds < 0 for sample in samples
        ),
    )


def _evidence(
    samples: tuple[EffortCalibrationSample, ...],
    *,
    portfolio_id: UUID = PORTFOLIO_ID,
    without_observation: int = 0,
    without_prior_estimate: int = 0,
) -> WorkBreakdownEffortCalibrationEvidence:
    summary = _summary(samples)
    return WorkBreakdownEffortCalibrationEvidence(
        portfolio_id=portfolio_id,
        project_id=PROJECT_ID,
        completed_entity_count=(
            summary.sample_count + without_observation + without_prior_estimate
        ),
        completed_without_observation_count=without_observation,
        completed_without_prior_estimate_count=without_prior_estimate,
        samples=samples,
        summary=summary,
    )


def _canonical_case() -> tuple[
    Portfolio,
    WorkBreakdownEffortCalibrationEvidence,
]:
    portfolio = _portfolio(
        _entity(TASK_A_ID, EntityType.TASK),
        _entity(DELIVERABLE_ID, EntityType.DELIVERABLE),
        _entity(TASK_B_ID, EntityType.TASK),
        _entity(PROJECT_ID, EntityType.PROJECT),
    )
    samples = (
        _sample(TASK_A_ID, 100, 130),
        _sample(DELIVERABLE_ID, 60, 60),
        _sample(TASK_B_ID, 90, 80),
        _sample(PROJECT_ID, 0, 5),
    )
    evidence = _evidence(
        samples,
        without_observation=1,
        without_prior_estimate=1,
    )
    return portfolio, evidence


def test_result_models_are_frozen_and_strict() -> None:
    portfolio, evidence = _canonical_case()
    profile = build_effort_calibration_profile(portfolio, evidence)

    with pytest.raises(ValidationError):
        profile.completed_entity_count = 999  # type: ignore[misc]

    zero_summary = _summary(())
    with pytest.raises(ValidationError):
        WorkBreakdownEffortCalibrationProfile(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            completed_entity_count="0",  # type: ignore[arg-type]
            completed_without_observation_count=0,
            completed_without_prior_estimate_count=0,
            overall_summary=zero_summary,
            segments=(),
        )


def test_foreign_evidence_portfolio_is_rejected() -> None:
    portfolio = _portfolio(_entity(PROJECT_ID, EntityType.PROJECT))
    evidence = _evidence((), portfolio_id=OTHER_PORTFOLIO_ID)

    with pytest.raises(EffortCalibrationProfileError, match="different portfolio"):
        build_effort_calibration_profile(portfolio, evidence)


def test_hostile_constructed_evidence_is_freshly_revalidated() -> None:
    portfolio = _portfolio(_entity(PROJECT_ID, EntityType.PROJECT))
    zero_summary = _summary(())
    hostile = WorkBreakdownEffortCalibrationEvidence.model_construct(
        portfolio_id=str(PORTFOLIO_ID),
        project_id=PROJECT_ID,
        completed_entity_count=0,
        completed_without_observation_count=0,
        completed_without_prior_estimate_count=0,
        samples=(),
        summary=zero_summary,
    )

    with pytest.raises(EffortCalibrationProfileError, match="invalid"):
        build_effort_calibration_profile(portfolio, hostile)


def test_missing_current_sample_entity_is_rejected() -> None:
    portfolio = _portfolio(_entity(PROJECT_ID, EntityType.PROJECT))
    evidence = _evidence((_sample(MISSING_ID, 10, 11),))

    with pytest.raises(EffortCalibrationProfileError, match="missing"):
        build_effort_calibration_profile(portfolio, evidence)


def test_duplicate_sample_entity_ids_are_rejected() -> None:
    portfolio = _portfolio(_entity(TASK_A_ID, EntityType.TASK))
    evidence = _evidence(
        (
            _sample(TASK_A_ID, 10, 12, estimate_id=uuid4()),
            _sample(TASK_A_ID, 20, 18, estimate_id=uuid4()),
        )
    )

    with pytest.raises(EffortCalibrationProfileError, match="duplicate"):
        build_effort_calibration_profile(portfolio, evidence)


def test_current_canonical_entity_type_controls_grouping() -> None:
    portfolio = _portfolio(_entity(TASK_A_ID, EntityType.RESEARCH))
    evidence = _evidence((_sample(TASK_A_ID, 10, 12),))

    profile = build_effort_calibration_profile(portfolio, evidence)

    assert len(profile.segments) == 1
    assert profile.segments[0].entity_type is EntityType.RESEARCH
    assert profile.segments[0].sample_entity_ids == (TASK_A_ID,)


def test_segments_preserve_order_and_exact_integer_arithmetic() -> None:
    portfolio, evidence = _canonical_case()

    profile = build_effort_calibration_profile(portfolio, evidence)

    assert [segment.entity_type for segment in profile.segments] == [
        EntityType.TASK,
        EntityType.DELIVERABLE,
        EntityType.PROJECT,
    ]
    assert profile.segments[0].sample_entity_ids == (TASK_A_ID, TASK_B_ID)
    assert profile.segments[1].sample_entity_ids == (DELIVERABLE_ID,)
    assert profile.segments[2].sample_entity_ids == (PROJECT_ID,)

    task = profile.segments[0].summary
    assert task.sample_count == 2
    assert task.total_planned_duration_seconds == 190
    assert task.total_actual_duration_seconds == 210
    assert task.signed_variance_seconds == 20
    assert task.absolute_error_seconds == 40
    assert task.underplanned_entity_count == 1
    assert task.exact_entity_count == 0
    assert task.overplanned_entity_count == 1

    deliverable = profile.segments[1].summary
    assert deliverable.total_planned_duration_seconds == 60
    assert deliverable.total_actual_duration_seconds == 60
    assert deliverable.signed_variance_seconds == 0
    assert deliverable.absolute_error_seconds == 0
    assert deliverable.exact_entity_count == 1

    project = profile.segments[2].summary
    assert project.total_planned_duration_seconds == 0
    assert project.total_actual_duration_seconds == 5
    assert project.signed_variance_seconds == 5
    assert project.absolute_error_seconds == 5
    assert project.underplanned_entity_count == 1

    assert profile.overall_summary == evidence.summary
    assert profile.overall_summary.sample_count == 4
    assert profile.overall_summary.total_planned_duration_seconds == 250
    assert profile.overall_summary.total_actual_duration_seconds == 275
    assert profile.overall_summary.signed_variance_seconds == 25
    assert profile.overall_summary.absolute_error_seconds == 45
    assert profile.overall_summary.underplanned_entity_count == 2
    assert profile.overall_summary.exact_entity_count == 1
    assert profile.overall_summary.overplanned_entity_count == 1

    assert sum(
        segment.summary.sample_count for segment in profile.segments
    ) == profile.overall_summary.sample_count
    assert sum(
        segment.summary.total_planned_duration_seconds
        for segment in profile.segments
    ) == profile.overall_summary.total_planned_duration_seconds
    assert sum(
        segment.summary.total_actual_duration_seconds
        for segment in profile.segments
    ) == profile.overall_summary.total_actual_duration_seconds
    assert sum(
        segment.summary.signed_variance_seconds for segment in profile.segments
    ) == profile.overall_summary.signed_variance_seconds
    assert sum(
        segment.summary.absolute_error_seconds for segment in profile.segments
    ) == profile.overall_summary.absolute_error_seconds


def test_global_coverage_is_preserved_without_per_type_exclusions() -> None:
    portfolio, evidence = _canonical_case()

    profile = build_effort_calibration_profile(portfolio, evidence)

    assert profile.completed_entity_count == 6
    assert profile.completed_without_observation_count == 1
    assert profile.completed_without_prior_estimate_count == 1
    assert (
        profile.completed_entity_count
        == profile.overall_summary.sample_count
        + profile.completed_without_observation_count
        + profile.completed_without_prior_estimate_count
    )
    assert "completed_without_observation_count" not in (
        EffortCalibrationTypeSegment.model_fields
    )
    assert "completed_without_prior_estimate_count" not in (
        EffortCalibrationTypeSegment.model_fields
    )


def test_empty_samples_produce_zero_segments_and_preserve_coverage() -> None:
    portfolio = _portfolio(_entity(PROJECT_ID, EntityType.PROJECT))
    evidence = _evidence((), without_observation=1, without_prior_estimate=1)

    profile = build_effort_calibration_profile(portfolio, evidence)

    assert profile.segments == ()
    assert profile.overall_summary == _summary(())
    assert profile.completed_entity_count == 2
    assert profile.completed_without_observation_count == 1
    assert profile.completed_without_prior_estimate_count == 1


def test_inputs_are_unchanged_and_repeated_derivation_is_deterministic() -> None:
    portfolio, evidence = _canonical_case()
    portfolio_before = copy.deepcopy(portfolio)
    evidence_before = copy.deepcopy(evidence)

    first = build_effort_calibration_profile(portfolio, evidence)
    second = build_effort_calibration_profile(portfolio, evidence)

    assert first == second
    assert portfolio == portfolio_before
    assert evidence == evidence_before
