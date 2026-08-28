"""Unit tests for the V1.12 durable calibration-evidence orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from trajectory_os.application.execution_effort_calibration import (
    DurableExecutionEffortCalibrationError,
    ExecutionEffortCalibrationPortfolioNotFoundError,
    build_effort_calibration_evidence_durably,
)
from trajectory_os.application.execution_effort_measurement import (
    measure_work_breakdown_effort,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_calibration import (
    build_effort_calibration_evidence,
)
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("77777777-7777-7777-7777-777777777777")
PROJECT_ID = UUID("88888888-8888-8888-8888-888888888888")
TASK_ID = UUID("99999999-9999-9999-9999-999999999999")

T0 = datetime(2024, 9, 1, tzinfo=UTC)
T1 = datetime(2024, 9, 2, tzinfo=UTC)
T2 = datetime(2024, 9, 3, tzinfo=UTC)


def _make_portfolio() -> Portfolio:
    project = TrajectoryEntity(
        id=PROJECT_ID,
        entity_type=EntityType.PROJECT,
        title="Test Project",
        description="",
        status=EntityStatus.COMPLETED,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=T0,
        updated_at=T0,
    )
    task = TrajectoryEntity(
        id=TASK_ID,
        entity_type=EntityType.TASK,
        title="Test Task",
        description="",
        status=EntityStatus.COMPLETED,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=T0,
        updated_at=T0,
    )
    relation = TrajectoryRelation(
        id=uuid4(),
        source_id=TASK_ID,
        target_id=PROJECT_ID,
        relation_type=RelationType.BELONGS_TO,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
    )
    return Portfolio(
        id=PORTFOLIO_ID,
        name="Test Portfolio",
        entities=[project, task],
        relations=[relation],
    )


class FakePortfolioRepository:
    def __init__(self, portfolio: Portfolio | None) -> None:
        self._portfolio = portfolio
        self.load_calls: list[UUID] = []
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.load_calls.append(portfolio_id)
        return self._portfolio

    def save(self, portfolio: Portfolio) -> None:
        self.saved.append(portfolio)
        raise AssertionError("no writes allowed at this boundary")


class FakeEstimateReader:
    def __init__(self, estimates: tuple[ExecutionEffortEstimate, ...] = ()) -> None:
        self._estimates = estimates
        self.calls: list[UUID] = []

    def list_for_portfolio(self, portfolio_id: UUID) -> tuple[ExecutionEffortEstimate, ...]:
        self.calls.append(portfolio_id)
        return self._estimates

    def list_for_entity(
        self, portfolio_id: UUID, entity_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        return tuple(e for e in self._estimates if e.entity_id == entity_id)


class FakeObservationReader:
    def __init__(
        self, observations: tuple[ExecutionEffortObservation, ...] = ()
    ) -> None:
        self._observations = observations
        self.calls: list[UUID] = []

    def list_for_portfolio(
        self, portfolio_id: UUID
    ) -> tuple[ExecutionEffortObservation, ...]:
        self.calls.append(portfolio_id)
        return self._observations

    def list_for_entity(
        self, portfolio_id: UUID, entity_id: UUID
    ) -> tuple[ExecutionEffortObservation, ...]:
        return tuple(o for o in self._observations if o.entity_id == entity_id)


class FailingRepository:
    def load(self, portfolio_id: UUID) -> Portfolio | None:
        raise RuntimeError("db down")


class FailingEstimateReader:
    def list_for_portfolio(self, portfolio_id: UUID) -> tuple[ExecutionEffortEstimate, ...]:
        raise RuntimeError("estimate reader down")

    def list_for_entity(
        self, portfolio_id: UUID, entity_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        raise RuntimeError("estimate reader down")


class FailingObservationReader:
    def list_for_portfolio(
        self, portfolio_id: UUID
    ) -> tuple[ExecutionEffortObservation, ...]:
        raise RuntimeError("observation reader down")

    def list_for_entity(
        self, portfolio_id: UUID, entity_id: UUID
    ) -> tuple[ExecutionEffortObservation, ...]:
        raise RuntimeError("observation reader down")

def _prior_scenario() -> tuple[
    tuple[ExecutionEffortEstimate, ...],
    tuple[ExecutionEffortObservation, ...],
]:
    estimates = (
        ExecutionEffortEstimate(
            id=uuid4(),
            portfolio_id=PORTFOLIO_ID,
            entity_id=TASK_ID,
            duration_seconds=100,
            estimated_at=T0,
            source=SourceKind.USER_CONFIRMED,
        ),
    )
    observations = (
        ExecutionEffortObservation(
            id=uuid4(),
            portfolio_id=PORTFOLIO_ID,
            entity_id=TASK_ID,
            duration_seconds=120,
            observed_at=T1,
            source=SourceKind.USER_CONFIRMED,
        ),
    )
    return estimates, observations


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_non_uuid_portfolio_id_rejected_before_repository(self) -> None:
        repo = FakePortfolioRepository(_make_portfolio())
        est_reader = FakeEstimateReader()
        obs_reader = FakeObservationReader()
        with pytest.raises(DurableExecutionEffortCalibrationError, match="UUID"):
            build_effort_calibration_evidence_durably(
                "not-a-uuid",  # type: ignore[arg-type]
                PROJECT_ID,
                repo,
                est_reader,
                obs_reader,
            )
        assert repo.load_calls == []
        assert est_reader.calls == []
        assert obs_reader.calls == []

    def test_missing_portfolio_prevents_reader_calls(self) -> None:
        repo = FakePortfolioRepository(None)
        est_reader = FakeEstimateReader()
        obs_reader = FakeObservationReader()
        with pytest.raises(
            ExecutionEffortCalibrationPortfolioNotFoundError, match="portfolio not found"
        ):
            build_effort_calibration_evidence_durably(
                PORTFOLIO_ID, PROJECT_ID, repo, est_reader, obs_reader
            )
        assert est_reader.calls == []
        assert obs_reader.calls == []


# ---------------------------------------------------------------------------
# Single load / reader identity
# ---------------------------------------------------------------------------


class TestSingleLoad:
    def test_portfolio_loaded_exactly_once(self) -> None:
        portfolio = _make_portfolio()
        repo = FakePortfolioRepository(portfolio)
        estimates, observations = _prior_scenario()
        est_reader = FakeEstimateReader(estimates)
        obs_reader = FakeObservationReader(observations)

        result = build_effort_calibration_evidence_durably(
            PORTFOLIO_ID, PROJECT_ID, repo, est_reader, obs_reader
        )

        assert repo.load_calls == [PORTFOLIO_ID]
        assert result.portfolio_id == PORTFOLIO_ID
        assert result.project_id == PROJECT_ID

    def test_both_readers_receive_exact_portfolio_id(self) -> None:
        portfolio = _make_portfolio()
        repo = FakePortfolioRepository(portfolio)
        estimates, observations = _prior_scenario()
        est_reader = FakeEstimateReader(estimates)
        obs_reader = FakeObservationReader(observations)

        build_effort_calibration_evidence_durably(
            PORTFOLIO_ID, PROJECT_ID, repo, est_reader, obs_reader
        )

        assert est_reader.calls == [PORTFOLIO_ID]
        assert obs_reader.calls == [PORTFOLIO_ID]


# ---------------------------------------------------------------------------
# Delegation semantics
# ---------------------------------------------------------------------------


class TestDelegation:
    def test_delegates_measurement_to_pure_v9(self) -> None:
        """The durable result must exactly equal the pure V1.12 derivation
        over the pure V1.9 measurement of the same inputs."""
        portfolio = _make_portfolio()
        repo = FakePortfolioRepository(portfolio)
        estimates, observations = _prior_scenario()
        est_reader = FakeEstimateReader(estimates)
        obs_reader = FakeObservationReader(observations)

        result = build_effort_calibration_evidence_durably(
            PORTFOLIO_ID, PROJECT_ID, repo, est_reader, obs_reader
        )

        expected = build_effort_calibration_evidence(
            portfolio,
            measure_work_breakdown_effort(portfolio, PROJECT_ID, observations),
            estimates,
        )
        assert result == expected

    def test_sample_is_exact(self) -> None:
        portfolio = _make_portfolio()
        repo = FakePortfolioRepository(portfolio)
        estimates, observations = _prior_scenario()

        result = build_effort_calibration_evidence_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            repo,
            FakeEstimateReader(estimates),
            FakeObservationReader(observations),
        )

        assert result.completed_without_observation_count == 1  # project
        assert result.completed_without_prior_estimate_count == 0
        assert result.completed_entity_count == 2
        assert len(result.samples) == 1
        sample = result.samples[0]
        assert sample.entity_id == TASK_ID
        assert sample.planned_duration_seconds == 100
        assert sample.actual_duration_seconds == 120
        assert sample.variance_seconds == 20
        assert sample.first_observed_at == T1
        assert sample.estimated_at < sample.first_observed_at


# ---------------------------------------------------------------------------
# Failure propagation / no writes
# ---------------------------------------------------------------------------


class TestFailurePropagation:
    def test_repository_failure_propagates(self) -> None:
        with pytest.raises(RuntimeError, match="db down"):
            build_effort_calibration_evidence_durably(
                PORTFOLIO_ID,
                PROJECT_ID,
                FailingRepository(),
                FakeEstimateReader(),
                FakeObservationReader(),
            )

    def test_estimate_reader_failure_propagates(self) -> None:
        repo = FakePortfolioRepository(_make_portfolio())
        with pytest.raises(RuntimeError, match="estimate reader down"):
            build_effort_calibration_evidence_durably(
                PORTFOLIO_ID,
                PROJECT_ID,
                repo,
                FailingEstimateReader(),
                FakeObservationReader(),
            )

    def test_observation_reader_failure_propagates(self) -> None:
        repo = FakePortfolioRepository(_make_portfolio())
        with pytest.raises(RuntimeError, match="observation reader down"):
            build_effort_calibration_evidence_durably(
                PORTFOLIO_ID,
                PROJECT_ID,
                repo,
                FakeEstimateReader(),
                FailingObservationReader(),
            )

    def test_domain_failure_propagates(self) -> None:
        """A post-observation estimate alone must never produce a sample."""
        portfolio = _make_portfolio()
        repo = FakePortfolioRepository(portfolio)
        estimates = (
            ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_ID,
                duration_seconds=1,
                estimated_at=T2,
                source=SourceKind.USER_CONFIRMED,
            ),
        )
        observations = (
            ExecutionEffortObservation(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_ID,
                duration_seconds=5,
                observed_at=T1,
                source=SourceKind.USER_CONFIRMED,
            ),
        )
        result = build_effort_calibration_evidence_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            repo,
            FakeEstimateReader(estimates),
            FakeObservationReader(observations),
        )
        assert result.samples == ()
        assert result.completed_without_prior_estimate_count == 1


class TestNoWrites:
    def test_no_save_called(self) -> None:
        portfolio = _make_portfolio()
        repo = FakePortfolioRepository(portfolio)
        estimates, observations = _prior_scenario()

        build_effort_calibration_evidence_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            repo,
            FakeEstimateReader(estimates),
            FakeObservationReader(observations),
        )

        assert repo.saved == []
