"""Unit tests for the V1.14 durable effort calibration sufficiency boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

import trajectory_os.application.execution_effort_calibration_sufficiency as sufficiency_app
from trajectory_os.application.execution_effort_calibration_profile import (
    EffortCalibrationProfilePortfolioNotFoundError,
)
from trajectory_os.application.execution_effort_calibration_sufficiency import (
    DurableEffortCalibrationSufficiencyError,
    assess_effort_calibration_sufficiency_durably,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_calibration import (
    EffortCalibrationSample,
    EffortCalibrationSummary,
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
PROJECT_TYPE_ID = UUID("17171717-1717-4717-8717-171717171717")

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
        name="Durable V1.14 Portfolio",
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
    task_a = _sample(TASK_ID, 100, 130)
    project = _sample(PROJECT_TYPE_ID, 50, 40)
    overall = _summary((task_a, project))
    return WorkBreakdownEffortCalibrationProfile(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        completed_entity_count=2,
        completed_without_observation_count=0,
        completed_without_prior_estimate_count=0,
        overall_summary=overall,
        segments=(
            EffortCalibrationTypeSegment(
                entity_type=EntityType.TASK,
                sample_entity_ids=(TASK_ID,),
                summary=_summary((task_a,)),
            ),
            EffortCalibrationTypeSegment(
                entity_type=EntityType.PROJECT,
                sample_entity_ids=(PROJECT_TYPE_ID,),
                summary=_summary((project,)),
            ),
        ),
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
        raise AssertionError("V1.14 durable sufficiency must not write")


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

    with pytest.raises(DurableEffortCalibrationSufficiencyError):
        assess_effort_calibration_sufficiency_durably(
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


def test_valid_threshold_reaches_v113_pipeline_in_order() -> None:
    repo = FakePortfolioRepository(_portfolio())
    estimates = FakeEstimateReader()
    observations = FakeObservationReader()

    result = assess_effort_calibration_sufficiency_durably(
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
    # segments and V1.14 yields the conservative empty assessment.
    assert result.segments == ()
    assert result.minimum_required_sample_count == 2
    assert result.sufficient_segment_count == 0
    assert result.insufficient_segment_count == 0


def test_v113_missing_portfolio_error_propagates() -> None:
    repo = FakePortfolioRepository(None)

    with pytest.raises(
        EffortCalibrationProfilePortfolioNotFoundError,
        match="portfolio not found",
    ):
        assess_effort_calibration_sufficiency_durably(
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


def test_delegates_to_v113_durably_then_to_pure_v114(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio = _portfolio()
    repo = FakePortfolioRepository(portfolio)
    estimates = FakeEstimateReader()
    observations = FakeObservationReader()

    expected_profile = _authoritative_profile()
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
        return assess_effort_calibration_sufficiency(
            expected_profile, int(minimum)  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        sufficiency_app, "build_effort_calibration_profile_durably", fake_v113
    )
    monkeypatch.setattr(
        sufficiency_app, "assess_effort_calibration_sufficiency", fake_v114
    )

    result = assess_effort_calibration_sufficiency_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        3,
        repo,
        estimates,
        observations,
    )

    assert events == ["v113-durable", "v114-pure"]
    assert capture.v113_calls == [
        {
            "portfolio_id": PORTFOLIO_ID,
            "project_id": PROJECT_ID,
            "portfolio_repository": repo,
            "estimate_reader": estimates,
            "observation_reader": observations,
        }
    ]
    # V1.14 must receive exactly the SAME authoritative V1.13 profile object.
    assert capture.v114_calls == [(expected_profile, 3)]
    # And the returned assessment is exactly what V1.14 produced.
    assert result == fake_v114(expected_profile, 3)
    assert result.segments[0].entity_type == EntityType.TASK
    assert result.segments[0].has_sufficient_samples is False
    assert result.segments[1].entity_type == EntityType.PROJECT
    assert result.segments[1].has_sufficient_samples is False
    assert result.sufficient_segment_count == 0
    assert result.insufficient_segment_count == 2
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
            sufficiency_app, "build_effort_calibration_profile_durably", failing_v113
        )

    repo: object = (
        FailingRepository(message) if component == "repository" else _portfolio_fakes()
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
        assess_effort_calibration_sufficiency_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            2,
            repo,  # type: ignore[arg-type]
            estimates,  # type: ignore[arg-type]
            observations,  # type: ignore[arg-type]
        )


def _portfolio_fakes() -> FakePortfolioRepository:
    return FakePortfolioRepository(_portfolio())
