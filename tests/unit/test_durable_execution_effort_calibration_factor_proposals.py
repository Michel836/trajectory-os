"""Unit tests for the V1.15 durable effort calibration factor proposals.

The durable boundary must validate the policy first, delegate the entire
V1.13 pipeline exactly once, and derive pure V1.14 → V1.15 over the SAME
authoritative V1.13 profile — without writes or extra repository passes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

import trajectory_os.application.execution_effort_calibration_factor_proposals as factor_app
from trajectory_os.application.execution_effort_calibration_factor_proposals import (
    DurableEffortCalibrationFactorProposalError,
    build_effort_calibration_factor_proposals_durably,
)
from trajectory_os.application.execution_effort_calibration_profile import (
    EffortCalibrationProfilePortfolioNotFoundError,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_calibration import (
    EffortCalibrationSample,
    EffortCalibrationSummary,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
    WorkBreakdownEffortCalibrationFactorProposalSet,
    build_effort_calibration_factor_proposals,
)
from trajectory_os.domain.execution_effort_calibration_profile import (
    EffortCalibrationTypeSegment,
    WorkBreakdownEffortCalibrationProfile,
)
from trajectory_os.domain.execution_effort_calibration_sufficiency import (
    WorkBreakdownEffortCalibrationSufficiencyAssessment,
    assess_effort_calibration_sufficiency,
)
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.portfolio import Portfolio

PORTFOLIO_ID = UUID("14141414-1414-4414-8414-141414141414")
PROJECT_ID = UUID("15151515-1515-4515-8515-151515151515")
TASK_ID = UUID("16161616-1616-4616-8616-161616161616")
DELIVERABLE_ID = UUID("17171717-1717-4717-8717-171717171717")
PROJECT_TYPE_ID = UUID("18181818-1818-4818-8818-181818181818")

ESTIMATED_AT = datetime(2025, 4, 1, tzinfo=UTC)
FIRST_OBSERVED_AT = datetime(2025, 4, 2, tzinfo=UTC)
LAST_OBSERVED_AT = datetime(2025, 4, 3, tzinfo=UTC)


def _entity(entity_id: UUID, entity_type: EntityType) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id,
        entity_type=entity_type,
        title=f"Entity {str(entity_id)[:8]}",
        status=EntityStatus.COMPLETED,
        created_at=ESTIMATED_AT,
        updated_at=ESTIMATED_AT,
    )


def _portfolio() -> Portfolio:
    return Portfolio(
        id=PORTFOLIO_ID,
        name="Durable V1.15 Portfolio",
        entities=[
            _entity(PROJECT_ID, EntityType.PROJECT),
            _entity(TASK_ID, EntityType.TASK),
        ],
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


def _authoritative_profile() -> WorkBreakdownEffortCalibrationProfile:
    task_a = _sample(TASK_ID, 100, 150)
    deliverable = _sample(DELIVERABLE_ID, 100, 100)
    project = _sample(PROJECT_TYPE_ID, 100, 50)
    overall = _summary((task_a, deliverable, project))
    segments = (
        EffortCalibrationTypeSegment(
            entity_type=EntityType.TASK,
            sample_entity_ids=(TASK_ID,),
            summary=_summary((task_a,)),
        ),
        EffortCalibrationTypeSegment(
            entity_type=EntityType.DELIVERABLE,
            sample_entity_ids=(DELIVERABLE_ID,),
            summary=_summary((deliverable,)),
        ),
        EffortCalibrationTypeSegment(
            entity_type=EntityType.PROJECT,
            sample_entity_ids=(PROJECT_TYPE_ID,),
            summary=_summary((project,)),
        ),
    )
    return WorkBreakdownEffortCalibrationProfile(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        completed_entity_count=3,
        completed_without_observation_count=0,
        completed_without_prior_estimate_count=0,
        overall_summary=overall,
        segments=segments,
    )


class FakePortfolioRepository:
    def __init__(self, portfolio: Portfolio | None) -> None:
        self.portfolio = portfolio
        self.load_calls: list[UUID] = []
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.load_calls.append(portfolio_id)
        return self.portfolio

    def save(self, portfolio: Portfolio) -> None:
        self.saved.append(portfolio)
        raise AssertionError("V1.15 durable factor proposals must not write")


class FakeEstimateReader:
    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.values: tuple[ExecutionEffortEstimate, ...] = ()

    def list_for_portfolio(
        self, portfolio_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        self.calls.append(portfolio_id)
        return self.values

    def list_for_entity(
        self, portfolio_id: UUID, entity_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        return ()


class FakeObservationReader:
    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.values: tuple[ExecutionEffortObservation, ...] = ()

    def list_for_portfolio(
        self, portfolio_id: UUID
    ) -> tuple[ExecutionEffortObservation, ...]:
        self.calls.append(portfolio_id)
        return self.values

    def list_for_entity(
        self, portfolio_id: UUID, entity_id: UUID
    ) -> tuple[ExecutionEffortObservation, ...]:
        return ()


@pytest.mark.parametrize(
    "threshold",
    [0, -1, True, False, 1.0, "2", None, [2], (2,)],
)
def test_invalid_threshold_is_rejected_before_any_repository_or_reader_access(
    threshold: object,
) -> None:
    repo = FakePortfolioRepository(_portfolio())
    estimates = FakeEstimateReader()
    observations = FakeObservationReader()

    with pytest.raises(DurableEffortCalibrationFactorProposalError):
        build_effort_calibration_factor_proposals_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            threshold,  # type: ignore[arg-type]
            repo,
            estimates,
            observations,
        )

    assert repo.load_calls == []
    assert estimates.calls == []
    assert observations.calls == []
    assert repo.saved == []


def test_valid_threshold_reaches_v113_pipeline_exactly_once_and_derives_empty() -> None:
    repo = FakePortfolioRepository(_portfolio())
    estimates = FakeEstimateReader()
    observations = FakeObservationReader()

    result = build_effort_calibration_factor_proposals_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        2,
        repo,
        estimates,
        observations,
    )

    assert repo.load_calls == [PORTFOLIO_ID]
    assert estimates.calls == [PORTFOLIO_ID]
    assert observations.calls == [PORTFOLIO_ID]
    assert repo.saved == []

    # No estimates/observations were persisted, so V1.13 yields zero
    # segments and V1.15 yields the empty proposal set.
    assert result.segments == ()
    assert result.minimum_required_sample_count == 2
    assert result.available_proposal_count == 0
    assert result.unavailable_proposal_count == 0


def test_v113_missing_portfolio_error_propagates() -> None:
    repo = FakePortfolioRepository(None)

    with pytest.raises(
        EffortCalibrationProfilePortfolioNotFoundError,
        match="portfolio not found",
    ):
        build_effort_calibration_factor_proposals_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            2,
            repo,
            FakeEstimateReader(),
            FakeObservationReader(),
        )


@dataclass
class DelegationCapture:
    v113_calls: list[dict[str, object]] = field(default_factory=list)
    v114_calls: list[tuple[object, object]] = field(default_factory=list)
    v115_calls: list[tuple[object, object]] = field(default_factory=list)


def test_delegates_to_v113_once_then_to_pure_v114_and_v115_over_same_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio = _portfolio()
    repo = FakePortfolioRepository(portfolio)
    estimates = FakeEstimateReader()
    observations = FakeObservationReader()

    expected_profile = _authoritative_profile()
    expected_sufficiency = assess_effort_calibration_sufficiency(
        expected_profile, 1
    )
    expected_result = build_effort_calibration_factor_proposals(
        expected_profile, expected_sufficiency
    )
    capture = DelegationCapture()
    events: list[str] = []

    def fake_v113(
        *,
        portfolio_id: UUID,
        project_id: UUID,
        portfolio_repository: object,
        estimate_reader: object,
        observation_reader: object,
    ) -> WorkBreakdownEffortCalibrationProfile:
        events.append("v113-durable")
        capture.v113_calls.append(
            {
                "portfolio_id": portfolio_id,
                "project_id": project_id,
                "portfolio_repository": portfolio_repository,
                "estimate_reader": estimate_reader,
                "observation_reader": observation_reader,
            }
        )
        return expected_profile

    def fake_v114(profile: object, minimum: object) -> (
        WorkBreakdownEffortCalibrationSufficiencyAssessment
    ):
        events.append("v114-pure")
        capture.v114_calls.append((profile, minimum))
        return expected_sufficiency

    def fake_v115(
        profile: object,
        sufficiency: object,
    ) -> WorkBreakdownEffortCalibrationFactorProposalSet:
        events.append("v115-pure")
        capture.v115_calls.append((profile, sufficiency))
        return expected_result

    monkeypatch.setattr(
        factor_app, "build_effort_calibration_profile_durably", fake_v113
    )
    monkeypatch.setattr(
        factor_app, "assess_effort_calibration_sufficiency", fake_v114
    )
    monkeypatch.setattr(
        factor_app, "build_effort_calibration_factor_proposals", fake_v115
    )

    result = build_effort_calibration_factor_proposals_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        1,
        repo,
        estimates,
        observations,
    )

    # V1.13 durable is called exactly once, then pure V1.14, then pure V1.15.
    assert events == ["v113-durable", "v114-pure", "v115-pure"]
    assert len(capture.v113_calls) == 1
    assert capture.v113_calls == [
        {
            "portfolio_id": PORTFOLIO_ID,
            "project_id": PROJECT_ID,
            "portfolio_repository": repo,
            "estimate_reader": estimates,
            "observation_reader": observations,
        }
    ]
    # V1.14 must receive exactly the SAME authoritative V1.13 profile
    # object the V1.13 boundary returned.
    assert capture.v114_calls == [(expected_profile, 1)]
    assert len(capture.v115_calls) == 1
    # V1.15 must receive the SAME profile object AND the SAME sufficiency
    # object V1.14 produced — no profile is ever re-derived.
    v115_profile, v115_sufficiency = capture.v115_calls[0]
    assert v115_profile is expected_profile
    assert v115_sufficiency is expected_sufficiency
    # And the returned set is exactly what V1.15 produced.
    assert result is expected_result
    assert [
        segment.entity_type for segment in result.segments
    ] == [EntityType.TASK, EntityType.DELIVERABLE, EntityType.PROJECT]
    assert result.available_proposal_count == 3
    assert result.unavailable_proposal_count == 0
    # Threshold 1 leaves every segment sufficient and every planned
    # total > 0, so all three proposals are AVAILABLE and no
    # project-wide factor is invented.
    assert all(
        segment.reason is EffortCalibrationFactorProposalReason.AVAILABLE
        for segment in result.segments
    )
    assert repo.saved == []


def test_real_v114_and_v115_receive_the_same_profile_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SAME V1.13 profile object feeds pure V1.14 then pure V1.15.

    Only the V1.13 boundary is stubbed; V1.14 and V1.15 run for real and
    are wrapped merely to record the objects they receive.
    """
    repo = FakePortfolioRepository(_portfolio())
    estimates = FakeEstimateReader()
    observations = FakeObservationReader()

    expected_profile = _authoritative_profile()
    seen_profiles: list[object] = []
    v114_returned: list[object] = []
    v115_sufficiencies: list[object] = []

    monkeypatch.setattr(
        factor_app,
        "build_effort_calibration_profile_durably",
        lambda **kwargs: expected_profile,
    )
    real_v114 = factor_app.assess_effort_calibration_sufficiency

    def wrapped_v114(profile: object, minimum: object) -> object:
        seen_profiles.append(profile)
        assessment = real_v114(profile, minimum)  # type: ignore[arg-type]
        v114_returned.append(assessment)
        return assessment

    monkeypatch.setattr(
        factor_app, "assess_effort_calibration_sufficiency", wrapped_v114
    )
    real_v115 = factor_app.build_effort_calibration_factor_proposals

    def wrapped_v115(profile: object, sufficiency: object) -> object:
        seen_profiles.append(profile)
        v115_sufficiencies.append(sufficiency)
        return real_v115(profile, sufficiency)  # type: ignore[arg-type]

    monkeypatch.setattr(
        factor_app, "build_effort_calibration_factor_proposals", wrapped_v115
    )

    result = build_effort_calibration_factor_proposals_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        2,
        repo,
        estimates,
        observations,
    )

    # Exactly one V1.13 derivation feeds both pure boundaries, and the
    # sufficiency object V1.14 returned is the one V1.15 consumed.
    assert seen_profiles[0] is expected_profile
    assert seen_profiles[1] is expected_profile
    assert len(seen_profiles) == 2
    assert v115_sufficiencies[0] is v114_returned[0]

    # Threshold 2 over single-sample segments: every segment is
    # insufficient, so no factor is proposed anywhere.
    assert result == build_effort_calibration_factor_proposals(
        expected_profile,
        assess_effort_calibration_sufficiency(expected_profile, 2),
    )
    assert result.available_proposal_count == 0
    assert result.unavailable_proposal_count == 3
    assert all(
        segment.reason is EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES
        and segment.proposal_available is False
        and segment.factor_numerator is None
        and segment.factor_denominator is None
        for segment in result.segments
    )
    assert repo.saved == []


class FailingRepository:
    def __init__(self, message: str = "repository failure") -> None:
        self._message = message

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        raise RuntimeError(self._message)


class FailingReader(FakeEstimateReader):
    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def list_for_portfolio(
        self, portfolio_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        raise RuntimeError(self._message)


@pytest.mark.parametrize(
    ("component", "message"),
    [
        ("repository", "repository failure"),
        ("estimate_reader", "estimate failure"),
        ("observation_reader", "observation failure"),
        ("domain", "domain failure"),
    ],
)
def test_component_failures_propagate_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    message: str,
) -> None:
    if component == "domain":

        def failing_v113(*args: object, **kwargs: object) -> object:
            raise RuntimeError(message)

        monkeypatch.setattr(
            factor_app, "build_effort_calibration_profile_durably", failing_v113
        )

    repo: object = (
        FailingRepository(message)
        if component == "repository"
        else FakePortfolioRepository(_portfolio())
    )
    estimates: object = (
        FailingReader(message) if component == "estimate_reader" else FakeEstimateReader()
    )
    observations: object = (
        FailingReader(message)
        if component == "observation_reader"
        else FakeObservationReader()
    )

    with pytest.raises(RuntimeError, match=message):
        build_effort_calibration_factor_proposals_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            2,
            repo,  # type: ignore[arg-type]
            estimates,  # type: ignore[arg-type]
            observations,  # type: ignore[arg-type]
        )
