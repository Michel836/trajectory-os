"""Unit tests for V1.15 pure calibration-factor proposal derivation."""

from __future__ import annotations

import copy
import inspect
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import trajectory_os.domain.execution_effort_calibration_factor_proposals as v115
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration import (
    EffortCalibrationSample,
    EffortCalibrationSummary,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalError,
    EffortCalibrationFactorProposalReason,
    EffortCalibrationTypeFactorProposal,
    WorkBreakdownEffortCalibrationFactorProposalSet,
    build_effort_calibration_factor_proposals,
)
from trajectory_os.domain.execution_effort_calibration_profile import (
    EffortCalibrationTypeSegment,
    WorkBreakdownEffortCalibrationProfile,
)
from trajectory_os.domain.execution_effort_calibration_sufficiency import (
    assess_effort_calibration_sufficiency,
)

PORTFOLIO_ID = UUID("14141414-1414-4414-8414-141414141414")
OTHER_PORTFOLIO_ID = UUID("15151515-1515-4515-8515-151515151515")
PROJECT_ID = UUID("24242424-2424-4424-8424-242424242424")
TASK_1_ID = UUID("34343434-3434-4434-8434-343434343434")
TASK_2_ID = UUID("35353535-3535-4535-8535-353535353535")
DELIVERABLE_ID = UUID("36363636-3636-4636-8636-363636363636")
PROJECT_ENTITY_ID = UUID("37373737-3737-4737-8737-373737373737")

ESTIMATED_AT = datetime(2025, 4, 1, tzinfo=UTC)
OBSERVED_AT = datetime(2025, 4, 2, tzinfo=UTC)
LAST_OBSERVED_AT = datetime(2025, 4, 3, tzinfo=UTC)


def _sample(
    entity_id: UUID,
    _entity_type: EntityType,
    planned: int,
    actual: int,
) -> EffortCalibrationSample:
    variance = actual - planned
    return EffortCalibrationSample(
        entity_id=entity_id,
        estimate_id=uuid4(),
        estimated_at=ESTIMATED_AT,
        first_observed_at=OBSERVED_AT,
        last_observed_at=LAST_OBSERVED_AT,
        observation_count=1,
        planned_duration_seconds=planned,
        actual_duration_seconds=actual,
        variance_seconds=variance,
        absolute_error_seconds=abs(variance),
    )


def _summary(
    samples: tuple[EffortCalibrationSample, ...],
) -> EffortCalibrationSummary:
    total_planned = sum(sample.planned_duration_seconds for sample in samples)
    total_actual = sum(sample.actual_duration_seconds for sample in samples)
    return EffortCalibrationSummary(
        sample_count=len(samples),
        total_planned_duration_seconds=total_planned,
        total_actual_duration_seconds=total_actual,
        signed_variance_seconds=total_actual - total_planned,
        absolute_error_seconds=sum(
            sample.absolute_error_seconds for sample in samples
        ),
        underplanned_entity_count=sum(
            1 for sample in samples if sample.variance_seconds > 0
        ),
        exact_entity_count=sum(
            1 for sample in samples if sample.variance_seconds == 0
        ),
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


def _profile(
    *segments: EffortCalibrationTypeSegment,
    portfolio: UUID = PORTFOLIO_ID,
    project: UUID = PROJECT_ID,
) -> WorkBreakdownEffortCalibrationProfile:
    def total(attribute: str) -> int:
        return sum(getattr(segment.summary, attribute) for segment in segments)

    overall = EffortCalibrationSummary(
        sample_count=total("sample_count"),
        total_planned_duration_seconds=total("total_planned_duration_seconds"),
        total_actual_duration_seconds=total("total_actual_duration_seconds"),
        signed_variance_seconds=total("signed_variance_seconds"),
        absolute_error_seconds=total("absolute_error_seconds"),
        underplanned_entity_count=total("underplanned_entity_count"),
        exact_entity_count=total("exact_entity_count"),
        overplanned_entity_count=total("overplanned_entity_count"),
    )
    return WorkBreakdownEffortCalibrationProfile(
        portfolio_id=portfolio,
        project_id=project,
        completed_entity_count=overall.sample_count,
        completed_without_observation_count=0,
        completed_without_prior_estimate_count=0,
        overall_summary=overall,
        segments=segments,
    )


def _canonical_segments() -> tuple[
    EffortCalibrationTypeSegment, EffortCalibrationTypeSegment, EffortCalibrationTypeSegment
]:
    """Three aligned segments covering every V1.15 outcome.

    * TASK: 2 samples, planned 200 / actual 180 → 9/10 when sufficient.
    * DELIVERABLE: 1 sample, planned 100 / actual 50 → 1/2 when sufficient.
    * PROJECT: 1 sample, planned 0 / actual 5 → ZERO_TOTAL_PLANNED_DURATION.
    """
    return (
        _segment(
            EntityType.TASK,
            (
                _sample(TASK_1_ID, EntityType.TASK, 100, 130),
                _sample(TASK_2_ID, EntityType.TASK, 100, 50),
            ),
        ),
        _segment(
            EntityType.DELIVERABLE,
            (_sample(DELIVERABLE_ID, EntityType.DELIVERABLE, 100, 50),),
        ),
        _segment(
            EntityType.PROJECT,
            (_sample(PROJECT_ENTITY_ID, EntityType.PROJECT, 0, 5),),
        ),
    )


def _factor(
    result: WorkBreakdownEffortCalibrationFactorProposalSet,
    entity_type: EntityType,
) -> EffortCalibrationTypeFactorProposal:
    matches = [segment for segment in result.segments if segment.entity_type == entity_type]
    assert len(matches) == 1
    return matches[0]


# --- Model invariants ---------------------------------------------------------


def test_proposal_models_are_frozen() -> None:
    profile = _profile(*_canonical_segments())
    result = build_effort_calibration_factor_proposals(
        profile,
        assess_effort_calibration_sufficiency(profile, 1),
    )
    set_copy = copy.copy(result)
    with pytest.raises(ValidationError):
        set_copy.portfolio_id = OTHER_PORTFOLIO_ID

    segment_copy = copy.copy(result.segments[0])
    with pytest.raises(ValidationError):
        segment_copy.factor_numerator = 123


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sample_count", "2"),
        ("total_planned_duration_seconds", "3"),
        ("available_proposal_count", "1"),
    ],
)
def test_proposal_models_require_exact_ints(
    field: str, value: str
) -> None:
    base = {
        "entity_type": EntityType.TASK,
        "sample_count": 1,
        "total_planned_duration_seconds": 100,
        "total_actual_duration_seconds": 50,
        "proposal_available": True,
        "reason": EffortCalibrationFactorProposalReason.AVAILABLE,
        "factor_numerator": 1,
        "factor_denominator": 2,
    }
    base[field] = value  # type: ignore[index]
    with pytest.raises(ValidationError):
        EffortCalibrationTypeFactorProposal(**base)

    set_base: dict[str, object] = {
        "portfolio_id": PORTFOLIO_ID,
        "project_id": PROJECT_ID,
        "minimum_required_sample_count": 1,
        "available_proposal_count": 0,
        "unavailable_proposal_count": 0,
        "segments": (),
    }
    set_base["available_proposal_count"] = value
    with pytest.raises(ValidationError):
        WorkBreakdownEffortCalibrationFactorProposalSet(**set_base)


@pytest.mark.parametrize(
    "segment_kwargs",
    [
        # Available requires an exact reduced factor and the exact
        # cross-multiplication identity.
        {
            "entity_type": EntityType.TASK,
            "sample_count": 2,
            "total_planned_duration_seconds": 200,
            "total_actual_duration_seconds": 180,
            "proposal_available": True,
            "reason": EffortCalibrationFactorProposalReason.AVAILABLE,
            # missing factor components
        },
        {
            "entity_type": EntityType.TASK,
            "sample_count": 2,
            "total_planned_duration_seconds": 200,
            "total_actual_duration_seconds": 180,
            "proposal_available": True,
            "reason": EffortCalibrationFactorProposalReason.AVAILABLE,
            "factor_numerator": 9,
            "factor_denominator": 7,  # cross-multiplication mismatch
        },
        {
            "entity_type": EntityType.TASK,
            "sample_count": 2,
            "total_planned_duration_seconds": 200,
            "total_actual_duration_seconds": 180,
            "proposal_available": True,
            "reason": EffortCalibrationFactorProposalReason.AVAILABLE,
            "factor_numerator": 18,
            "factor_denominator": 20,  # not reduced: gcd(18, 20) != 1
        },
        {
            "entity_type": EntityType.TASK,
            "sample_count": 0,
            "total_planned_duration_seconds": 200,
            "total_actual_duration_seconds": 180,
            "proposal_available": True,
            "reason": EffortCalibrationFactorProposalReason.AVAILABLE,
            "factor_numerator": 9,
            "factor_denominator": 10,
        },
        {
            "entity_type": EntityType.TASK,
            "sample_count": 1,
            "total_planned_duration_seconds": 0,
            "total_actual_duration_seconds": 5,
            "proposal_available": True,
            "reason": EffortCalibrationFactorProposalReason.AVAILABLE,
            "factor_numerator": 0,
            "factor_denominator": 1,
        },
        # Unavailable reasons must not carry factors.
        {
            "entity_type": EntityType.TASK,
            "sample_count": 1,
            "total_planned_duration_seconds": 100,
            "total_actual_duration_seconds": 50,
            "proposal_available": False,
            "reason": EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES,
            "factor_numerator": 1,
            "factor_denominator": 2,
        },
        {
            "entity_type": EntityType.PROJECT,
            "sample_count": 1,
            "total_planned_duration_seconds": 1,
            "total_actual_duration_seconds": 5,
            "proposal_available": False,
            "reason": EffortCalibrationFactorProposalReason.ZERO_TOTAL_PLANNED_DURATION,
        },
        # proposal_available and reason must agree.
        {
            "entity_type": EntityType.TASK,
            "sample_count": 1,
            "total_planned_duration_seconds": 100,
            "total_actual_duration_seconds": 50,
            "proposal_available": True,
            "reason": EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES,
        },
    ],
)
def test_proposal_segment_rejects_inconsistent_composition(
    segment_kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EffortCalibrationTypeFactorProposal(**segment_kwargs)


def _proposal_segments(available: int) -> tuple[EffortCalibrationTypeFactorProposal, ...]:
    base = {
        "entity_type": EntityType.TASK,
        "sample_count": 1,
        "total_planned_duration_seconds": 100,
        "total_actual_duration_seconds": 50,
    }
    segments = [
        EffortCalibrationTypeFactorProposal(
            **base,
            proposal_available=True,
            reason=EffortCalibrationFactorProposalReason.AVAILABLE,
            factor_numerator=1,
            factor_denominator=2,
        )
        for _ in range(available)
    ]
    segments.extend(
        EffortCalibrationTypeFactorProposal(
            entity_type=EntityType.DELIVERABLE if i == 0 else EntityType.PROJECT,
            sample_count=1,
            total_planned_duration_seconds=100,
            total_actual_duration_seconds=50,
            proposal_available=False,
            reason=EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES,
        )
        for i in range(3 - available)
    )
    return tuple(segments)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "portfolio_id": PORTFOLIO_ID,
            "project_id": PROJECT_ID,
            "minimum_required_sample_count": 2,
            "available_proposal_count": 1,  # count mismatch
            "unavailable_proposal_count": 1,
            "segments": _proposal_segments(1),
        },
        {
            "portfolio_id": PORTFOLIO_ID,
            "project_id": PROJECT_ID,
            "minimum_required_sample_count": 2,
            "available_proposal_count": 0,
            "unavailable_proposal_count": 0,
            "segments": _proposal_segments(2),  # count mismatch
        },
        {
            "portfolio_id": PORTFOLIO_ID,
            "project_id": PROJECT_ID,
            "minimum_required_sample_count": 2,
            "available_proposal_count": 2,  # available count mismatch
            "unavailable_proposal_count": 1,
            "segments": _proposal_segments(1),
        },
        {
            "portfolio_id": PORTFOLIO_ID,
            "project_id": PROJECT_ID,
            "minimum_required_sample_count": 2,
            "available_proposal_count": 1,
            "unavailable_proposal_count": 3,  # total mismatch
            "segments": _proposal_segments(1),
        },
    ],
)
def test_proposal_set_rejects_inconsistent_composition(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WorkBreakdownEffortCalibrationFactorProposalSet(**overrides)




# --- Input contract -------------------------------------------------------


@pytest.mark.parametrize(
    "bad_profile",
    [
        object,
        "not a profile",
        1,
        None,
        {"portfolio_id": PORTFOLIO_ID},
    ],
)
def test_rejects_invalid_profile_input(bad_profile: object) -> None:
    sufficiency = assess_effort_calibration_sufficiency(
        _profile(*_canonical_segments()), 1
    )
    with pytest.raises(EffortCalibrationFactorProposalError) as excinfo:
        build_effort_calibration_factor_proposals(bad_profile, sufficiency)
    assert "profile" in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_sufficiency",
    [
        object,
        "not a sufficiency",
        1,
        None,
        {"portfolio_id": PORTFOLIO_ID},
    ],
)
def test_rejects_invalid_sufficiency_input(bad_sufficiency: object) -> None:
    profile = _profile(*_canonical_segments())
    with pytest.raises(EffortCalibrationFactorProposalError) as excinfo:
        build_effort_calibration_factor_proposals(profile, bad_sufficiency)
    assert "sufficiency" in str(excinfo.value)


def test_rejects_hostile_profile_bypassing_validation() -> None:
    profile = WorkBreakdownEffortCalibrationProfile.model_construct(
        portfolio_id="not-a-uuid",
        project_id=PROJECT_ID,
        segments=(),
    )
    assert not isinstance(profile.portfolio_id, UUID)
    sufficiency = assess_effort_calibration_sufficiency(
        _profile(*_canonical_segments()), 1
    )
    with pytest.raises(EffortCalibrationFactorProposalError):
        build_effort_calibration_factor_proposals(profile, sufficiency)


def test_rejects_hostile_sufficiency_bypassing_validation() -> None:
    from trajectory_os.domain.execution_effort_calibration_sufficiency import (
        WorkBreakdownEffortCalibrationSufficiencyAssessment,
    )

    sufficiency = WorkBreakdownEffortCalibrationSufficiencyAssessment.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        minimum_required_sample_count=True,
        segments=(),
    )
    profile = _profile(*_canonical_segments())
    with pytest.raises(EffortCalibrationFactorProposalError):
        build_effort_calibration_factor_proposals(profile, sufficiency)


# --- Exact alignment -------------------------------------------------------


def test_rejects_portfolio_mismatch() -> None:
    profile = _profile(*_canonical_segments())
    sufficiency = assess_effort_calibration_sufficiency(
        _profile(*_canonical_segments(), portfolio=OTHER_PORTFOLIO_ID), 2
    )
    with pytest.raises(EffortCalibrationFactorProposalError) as excinfo:
        build_effort_calibration_factor_proposals(profile, sufficiency)
    assert "portfolio_id" in str(excinfo.value)


def test_rejects_project_mismatch() -> None:
    profile = _profile(*_canonical_segments())
    sufficiency = assess_effort_calibration_sufficiency(
        _profile(*_canonical_segments(), project=OTHER_PORTFOLIO_ID), 2
    )
    with pytest.raises(EffortCalibrationFactorProposalError) as excinfo:
        build_effort_calibration_factor_proposals(profile, sufficiency)
    assert "project_id" in str(excinfo.value)


def test_rejects_segment_length_mismatch() -> None:
    profile = _profile(*_canonical_segments())
    sufficiency = assess_effort_calibration_sufficiency(
        _profile(_segment(EntityType.TASK, (_sample(TASK_1_ID, EntityType.TASK, 100, 50),))),
        2,
    )
    with pytest.raises(EffortCalibrationFactorProposalError) as excinfo:
        build_effort_calibration_factor_proposals(profile, sufficiency)
    assert "segment count" in str(excinfo.value)


def test_rejects_segment_order_mismatch() -> None:
    profile = _profile(
        _segment(
            EntityType.TASK,
            (_sample(TASK_1_ID, EntityType.TASK, 100, 50),),
        ),
        _segment(
            EntityType.DELIVERABLE,
            (_sample(DELIVERABLE_ID, EntityType.DELIVERABLE, 50, 25),),
        ),
    )
    sufficiency = assess_effort_calibration_sufficiency(
        _profile(
            _segment(
                EntityType.DELIVERABLE,
                (_sample(DELIVERABLE_ID, EntityType.DELIVERABLE, 50, 25),),
            ),
            _segment(
                EntityType.TASK,
                (_sample(TASK_1_ID, EntityType.TASK, 100, 50),),
            ),
        ),
        2,
    )
    with pytest.raises(EffortCalibrationFactorProposalError) as excinfo:
        build_effort_calibration_factor_proposals(profile, sufficiency)
    assert "entity_type" in str(excinfo.value)


def test_rejects_sample_count_mismatch() -> None:
    profile = _profile(
        _segment(
            EntityType.TASK,
            (
                _sample(TASK_1_ID, EntityType.TASK, 100, 50),
                _sample(TASK_2_ID, EntityType.TASK, 100, 50),
            ),
        ),
    )
    sufficiency = assess_effort_calibration_sufficiency(
        _profile(
            _segment(
                EntityType.TASK,
                (_sample(TASK_1_ID, EntityType.TASK, 100, 50),),
            ),
        ),
        1,
    )
    with pytest.raises(EffortCalibrationFactorProposalError) as excinfo:
        build_effort_calibration_factor_proposals(profile, sufficiency)
    assert "sample_count" in str(excinfo.value)


# --- Derivation semantics --------------------------------------------------


def test_unsufficient_segment_becomes_insufficient_samples_without_factor() -> None:
    profile = _profile(*_canonical_segments())
    result = build_effort_calibration_factor_proposals(
        profile,
        assess_effort_calibration_sufficiency(profile, 2),
    )

    deliverable = _factor(result, EntityType.DELIVERABLE)
    assert deliverable.proposal_available is False
    assert (
        deliverable.reason
        is EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES
    )
    assert deliverable.factor_numerator is None
    assert deliverable.factor_denominator is None

    project = _factor(result, EntityType.PROJECT)
    assert project.proposal_available is False
    assert (
        project.reason
        is EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES
    )


def test_zero_planned_sufficient_segment_is_zero_total_planned() -> None:
    profile = _profile(*_canonical_segments())
    result = build_effort_calibration_factor_proposals(
        profile,
        assess_effort_calibration_sufficiency(profile, 1),
    )

    project = _factor(result, EntityType.PROJECT)
    assert project.proposal_available is False
    assert (
        project.reason
        is EffortCalibrationFactorProposalReason.ZERO_TOTAL_PLANNED_DURATION
    )
    assert project.total_planned_duration_seconds == 0
    assert project.total_actual_duration_seconds == 5
    assert project.factor_numerator is None
    assert project.factor_denominator is None


def test_available_factor_is_exact_reduced_and_cross_multiplication_consistent() -> None:
    profile = _profile(*_canonical_segments())
    result = build_effort_calibration_factor_proposals(
        profile,
        assess_effort_calibration_sufficiency(profile, 1),
    )

    task = _factor(result, EntityType.TASK)
    assert task.proposal_available is True
    assert (
        task.reason is EffortCalibrationFactorProposalReason.AVAILABLE
    )
    assert task.factor_numerator == 9
    assert task.factor_denominator == 10
    assert task.total_planned_duration_seconds == 200
    assert task.total_actual_duration_seconds == 180
    # Exact cross-multiplication identity (integer arithmetic only).
    assert task.factor_numerator * task.total_planned_duration_seconds == (
        task.factor_denominator * task.total_actual_duration_seconds
    )

    deliverable = _factor(result, EntityType.DELIVERABLE)
    assert deliverable.factor_numerator == 1
    assert deliverable.factor_denominator == 2
    assert deliverable.factor_numerator * (
        deliverable.total_planned_duration_seconds
    ) == deliverable.factor_denominator * (
        deliverable.total_actual_duration_seconds
    )


@pytest.mark.parametrize(
    ("planned", "actual", "numerator", "denominator"),
    [
        (100, 100, 1, 1),
        (200, 300, 3, 2),
        (60, 90, 3, 2),
        (100, 150, 3, 2),
        (84, 14, 1, 6),
        (50, 0, 0, 1),
    ],
)
def test_available_factor_is_exact_reduced_integer_ratio(
    planned: int,
    actual: int,
    numerator: int,
    denominator: int,
) -> None:
    profile = _profile(
        _segment(
            EntityType.TASK,
            (_sample(TASK_1_ID, EntityType.TASK, planned, actual),),
        )
    )
    result = build_effort_calibration_factor_proposals(
        profile,
        assess_effort_calibration_sufficiency(profile, 1),
    )

    task = _factor(result, EntityType.TASK)
    assert task.proposal_available is True
    assert task.factor_numerator == numerator
    assert task.factor_denominator == denominator
    assert task.total_planned_duration_seconds == planned
    assert task.total_actual_duration_seconds == actual


def test_empty_input_yields_empty_proposal_set() -> None:
    profile = _profile()
    sufficiency = assess_effort_calibration_sufficiency(profile, 5)
    result = build_effort_calibration_factor_proposals(profile, sufficiency)

    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_id == PROJECT_ID
    assert result.minimum_required_sample_count == 5
    assert result.available_proposal_count == 0
    assert result.unavailable_proposal_count == 0
    assert result.segments == ()


def test_result_count_and_order_invariants() -> None:
    profile = _profile(*_canonical_segments())
    result = build_effort_calibration_factor_proposals(
        profile,
        assess_effort_calibration_sufficiency(profile, 1),
    )

    assert [
        segment.entity_type for segment in result.segments
    ] == [EntityType.TASK, EntityType.DELIVERABLE, EntityType.PROJECT]
    assert result.available_proposal_count == 2
    assert result.unavailable_proposal_count == 1
    assert (
        result.available_proposal_count + result.unavailable_proposal_count
        == len(result.segments)
    )
    assert result.minimum_required_sample_count == 1


def test_inputs_are_not_mutated_and_result_is_deterministic() -> None:
    profile = _profile(*_canonical_segments())
    sufficiency = assess_effort_calibration_sufficiency(profile, 1)

    profile_before = copy.deepcopy(profile)
    sufficiency_before = copy.deepcopy(sufficiency)

    first = build_effort_calibration_factor_proposals(profile, sufficiency)
    second = build_effort_calibration_factor_proposals(profile, sufficiency)

    assert profile == profile_before
    assert sufficiency == sufficiency_before
    assert first == second
    assert first.model_dump(mode="python") == second.model_dump(mode="python")


def test_result_carries_no_float_or_decimal_fields() -> None:
    for model in (
        EffortCalibrationTypeFactorProposal,
        WorkBreakdownEffortCalibrationFactorProposalSet,
    ):
        for _field_name, field in model.model_fields.items():
            annotation = str(field.annotation)
            assert "float" not in annotation.lower(), annotation
            assert "Decimal" not in annotation, annotation


def test_pure_boundary_accepts_no_repository_or_reader() -> None:
    source = inspect.getsource(v115)
    assert "portfolio_repository" not in source
    assert "estimate_reader" not in source
    assert "observation_reader" not in source
    assert "datetime.now" not in source

    parameters = set(inspect.signature(build_effort_calibration_factor_proposals).parameters)
    assert parameters == {"profile", "sufficiency"}
