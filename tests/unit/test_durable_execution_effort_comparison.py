"""Unit tests for the V1.11 durable comparison orchestration boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from trajectory_os.application.execution_effort_comparison import (
    DurableExecutionEffortComparisonError,
    ExecutionEffortComparisonPortfolioNotFoundError,
    compare_work_breakdown_effort_durably,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
PROJECT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TASK_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

T0 = datetime(2024, 6, 1, tzinfo=UTC)


def _make_portfolio() -> Portfolio:
    project = TrajectoryEntity(
        id=PROJECT_ID,
        entity_type=EntityType.PROJECT,
        title="Test Project",
        description="",
        status=EntityStatus.ACTIVE,
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
        status=EntityStatus.ACTIVE,
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

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.load_calls.append(portfolio_id)
        return self._portfolio


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


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_non_uuid_portfolio_id_rejected(self) -> None:
        repo = FakePortfolioRepository(_make_portfolio())
        est_reader = FakeEstimateReader()
        obs_reader = FakeObservationReader()
        with pytest.raises(DurableExecutionEffortComparisonError, match="UUID"):
            compare_work_breakdown_effort_durably(
                "not-a-uuid",  # type: ignore[arg-type]
                PROJECT_ID,
                repo,
                est_reader,
                obs_reader,
            )
        # Repository should NOT have been called
        assert repo.load_calls == []

    def test_missing_portfolio_raises(self) -> None:
        repo = FakePortfolioRepository(None)
        est_reader = FakeEstimateReader()
        obs_reader = FakeObservationReader()
        with pytest.raises(
            ExecutionEffortComparisonPortfolioNotFoundError, match="portfolio not found"
        ):
            compare_work_breakdown_effort_durably(
                PORTFOLIO_ID, PROJECT_ID, repo, est_reader, obs_reader
            )
        # Readers should NOT have been called
        assert est_reader.calls == []
        assert obs_reader.calls == []


# ---------------------------------------------------------------------------
# Portfolio loaded exactly once
# ---------------------------------------------------------------------------


class TestSingleLoad:
    def test_portfolio_loaded_exactly_once(self) -> None:
        portfolio = _make_portfolio()
        repo = FakePortfolioRepository(portfolio)
        est_reader = FakeEstimateReader()
        obs_reader = FakeObservationReader()

        compare_work_breakdown_effort_durably(
            PORTFOLIO_ID, PROJECT_ID, repo, est_reader, obs_reader
        )

        assert len(repo.load_calls) == 1
        assert repo.load_calls[0] == PORTFOLIO_ID


# ---------------------------------------------------------------------------
# Readers receive correct portfolio id
# ---------------------------------------------------------------------------


class TestReaderCalls:
    def test_both_readers_receive_exact_portfolio_id(self) -> None:
        portfolio = _make_portfolio()
        repo = FakePortfolioRepository(portfolio)
        est_reader = FakeEstimateReader()
        obs_reader = FakeObservationReader()

        compare_work_breakdown_effort_durably(
            PORTFOLIO_ID, PROJECT_ID, repo, est_reader, obs_reader
        )

        assert est_reader.calls == [PORTFOLIO_ID]
        assert obs_reader.calls == [PORTFOLIO_ID]


# ---------------------------------------------------------------------------
# Failure propagation
# ---------------------------------------------------------------------------


class TestFailurePropagation:
    def test_repository_failure_propagates(self) -> None:
        est_reader = FakeEstimateReader()
        obs_reader = FakeObservationReader()
        with pytest.raises(RuntimeError, match="db down"):
            compare_work_breakdown_effort_durably(
                PORTFOLIO_ID, PROJECT_ID, FailingRepository(), est_reader, obs_reader
            )

    def test_estimate_reader_failure_propagates(self) -> None:
        repo = FakePortfolioRepository(_make_portfolio())
        obs_reader = FakeObservationReader()
        with pytest.raises(RuntimeError, match="estimate reader down"):
            compare_work_breakdown_effort_durably(
                PORTFOLIO_ID, PROJECT_ID, repo, FailingEstimateReader(), obs_reader
            )

    def test_observation_reader_failure_propagates(self) -> None:
        repo = FakePortfolioRepository(_make_portfolio())
        est_reader = FakeEstimateReader()
        with pytest.raises(RuntimeError, match="observation reader down"):
            compare_work_breakdown_effort_durably(
                PORTFOLIO_ID, PROJECT_ID, repo, est_reader, FailingObservationReader()
            )


# ---------------------------------------------------------------------------
# No writes
# ---------------------------------------------------------------------------


class TestNoWrites:
    def test_no_save_or_add_called(self) -> None:
        """The fakes have no save/add methods; if the code tried to call them,
        it would raise AttributeError. Successful execution proves no writes."""
        portfolio = _make_portfolio()
        repo = FakePortfolioRepository(portfolio)
        est_reader = FakeEstimateReader()
        obs_reader = FakeObservationReader()

        # Should not raise
        compare_work_breakdown_effort_durably(
            PORTFOLIO_ID, PROJECT_ID, repo, est_reader, obs_reader
        )
