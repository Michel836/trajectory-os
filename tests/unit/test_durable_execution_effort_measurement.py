"""Unit tests for read-only durable execution-effort measurement (V1.9-A/C)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

import trajectory_os.application.execution_effort_measurement as measurement_module
from trajectory_os.application.execution_effort_measurement import (
    DurableExecutionEffortMeasurementError,
    ExecutionEffortMeasurementPortfolioNotFoundError,
    ExecutionEffortObservationReader,
    measure_work_breakdown_effort_durably,
)
from trajectory_os.application.work_breakdown_acceptance import PortfolioRepository
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_measurement import (
    WorkBreakdownEffortMeasurement,
    measure_work_breakdown_effort,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

BASE = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class _SentinelError(Exception):
    """Repository/reader failure with a unique identity."""


class FakePortfolioRepository:
    def __init__(
        self,
        portfolios: dict[UUID, Portfolio] | None = None,
        *,
        load_error: Exception | None = None,
    ) -> None:
        self._portfolios = dict(portfolios or {})
        self._load_error = load_error
        self.loaded_ids: list[UUID] = []
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.loaded_ids.append(portfolio_id)
        if self._load_error is not None:
            raise self._load_error
        return self._portfolios.get(portfolio_id)

    def save(self, portfolio: Portfolio) -> None:
        self.saved.append(portfolio)
        raise AssertionError("V1.9 measurement must never save the Portfolio")


class FakeObservationReader:
    def __init__(
        self,
        observations: tuple[ExecutionEffortObservation, ...] = (),
        *,
        list_error: Exception | None = None,
    ) -> None:
        self._observations = observations
        self._list_error = list_error
        self.portfolio_calls: list[UUID] = []
        self.entity_calls: list[tuple[UUID, UUID]] = []

    def list_for_portfolio(
        self,
        portfolio_id: UUID,
    ) -> tuple[ExecutionEffortObservation, ...]:
        self.portfolio_calls.append(portfolio_id)
        if self._list_error is not None:
            raise self._list_error
        return self._observations

    def list_for_entity(
        self,
        portfolio_id: UUID,
        entity_id: UUID,
    ) -> tuple[ExecutionEffortObservation, ...]:
        self.entity_calls.append((portfolio_id, entity_id))
        return tuple(
            observation
            for observation in self._observations
            if observation.portfolio_id == portfolio_id
            and observation.entity_id == entity_id
        )


def _portfolio() -> tuple[Portfolio, TrajectoryEntity, TrajectoryEntity]:
    project = TrajectoryEntity(
        id=uuid4(),
        entity_type=EntityType.PROJECT,
        title="Project",
        created_at=BASE,
        updated_at=BASE,
    )
    task = TrajectoryEntity(
        id=uuid4(),
        entity_type=EntityType.TASK,
        title="Task",
        created_at=BASE,
        updated_at=BASE,
    )
    portfolio = Portfolio(
        id=uuid4(),
        name="V1.9",
        entities=[project, task],
        relations=[
            TrajectoryRelation(
                source_id=task.id,
                target_id=project.id,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )
    return portfolio, project, task


def _observation(
    portfolio: Portfolio,
    entity: TrajectoryEntity,
) -> ExecutionEffortObservation:
    return ExecutionEffortObservation(
        id=uuid4(),
        portfolio_id=portfolio.id,
        entity_id=entity.id,
        duration_seconds=90,
        observed_at=BASE,
        source=SourceKind.USER_CONFIRMED,
    )


def test_reader_protocol_is_structurally_satisfied_by_fake() -> None:
    reader: ExecutionEffortObservationReader = FakeObservationReader()
    assert reader.list_for_portfolio(uuid4()) == ()


def test_success_loads_current_then_reads_then_measures_without_writes() -> None:
    portfolio, project, task = _portfolio()
    observation = _observation(portfolio, task)
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    reader = FakeObservationReader((observation,))

    result = measure_work_breakdown_effort_durably(
        portfolio.id,
        project.id,
        repository,
        reader,
    )

    assert repository.loaded_ids == [portfolio.id]
    assert reader.portfolio_calls == [portfolio.id]
    assert reader.entity_calls == []
    assert repository.saved == []
    assert result.items[0].subtree.duration_seconds == 90
    assert result.items[1].direct.duration_seconds == 90


@pytest.mark.parametrize(
    "bad_portfolio_id",
    ["not-a-uuid", 42, b"uuid", None, True],
)
def test_invalid_portfolio_id_fails_before_any_repository_interaction(
    bad_portfolio_id: object,
) -> None:
    repository = FakePortfolioRepository()
    reader = FakeObservationReader()

    with pytest.raises(DurableExecutionEffortMeasurementError):
        measure_work_breakdown_effort_durably(
            cast(UUID, bad_portfolio_id),
            uuid4(),
            repository,
            reader,
        )

    assert repository.loaded_ids == []
    assert reader.portfolio_calls == []
    assert repository.saved == []


def test_missing_portfolio_fails_before_observation_read() -> None:
    missing_id = uuid4()
    repository = FakePortfolioRepository()
    reader = FakeObservationReader()

    with pytest.raises(ExecutionEffortMeasurementPortfolioNotFoundError) as excinfo:
        measure_work_breakdown_effort_durably(
            missing_id,
            uuid4(),
            repository,
            reader,
        )

    assert isinstance(excinfo.value, DurableExecutionEffortMeasurementError)
    assert repository.loaded_ids == [missing_id]
    assert reader.portfolio_calls == []
    assert repository.saved == []


def test_portfolio_load_error_propagates_unchanged() -> None:
    failure = _SentinelError("load failed")
    repository = FakePortfolioRepository(load_error=failure)
    reader = FakeObservationReader()

    with pytest.raises(_SentinelError) as excinfo:
        measure_work_breakdown_effort_durably(
            uuid4(),
            uuid4(),
            repository,
            reader,
        )

    assert excinfo.value is failure
    assert reader.portfolio_calls == []


def test_observation_read_error_propagates_unchanged() -> None:
    portfolio, project, _ = _portfolio()
    failure = _SentinelError("read failed")
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    reader = FakeObservationReader(list_error=failure)

    with pytest.raises(_SentinelError) as excinfo:
        measure_work_breakdown_effort_durably(
            portfolio.id,
            project.id,
            repository,
            reader,
        )

    assert excinfo.value is failure
    assert repository.loaded_ids == [portfolio.id]
    assert reader.portfolio_calls == [portfolio.id]
    assert repository.saved == []


def test_invalid_project_id_is_delegated_to_domain_after_observation_read() -> None:
    portfolio, _, _ = _portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    reader = FakeObservationReader()

    with pytest.raises(ValueError, match="project_id"):
        measure_work_breakdown_effort_durably(
            portfolio.id,
            cast(UUID, "not-a-uuid"),
            repository,
            reader,
        )

    assert repository.loaded_ids == [portfolio.id]
    assert reader.portfolio_calls == [portfolio.id]
    assert repository.saved == []


def test_returns_exact_object_produced_by_domain_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    portfolio, project, task = _portfolio()
    observation = _observation(portfolio, task)
    repository: PortfolioRepository = FakePortfolioRepository({portfolio.id: portfolio})
    reader: ExecutionEffortObservationReader = FakeObservationReader((observation,))
    sentinel: WorkBreakdownEffortMeasurement = measure_work_breakdown_effort(
        portfolio,
        project.id,
        [observation],
    )
    captured: list[tuple[Portfolio, UUID, tuple[ExecutionEffortObservation, ...]]] = []

    def fake_measure(
        current: Portfolio,
        project_id: UUID,
        observations: tuple[ExecutionEffortObservation, ...],
    ) -> WorkBreakdownEffortMeasurement:
        captured.append((current, project_id, observations))
        return sentinel

    monkeypatch.setattr(
        measurement_module,
        "measure_work_breakdown_effort",
        fake_measure,
    )

    result = measure_work_breakdown_effort_durably(
        portfolio.id,
        project.id,
        repository,
        reader,
    )

    assert result is sentinel
    assert captured == [(portfolio, project.id, (observation,))]
