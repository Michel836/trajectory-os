"""Unit tests for the durable execution-effort application boundary (V1.8-B)."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from trajectory_os.application import (
    DurableExecutionEffortError,
    ExecutionEffortObservationRepository,
    ExecutionEffortPortfolioNotFoundError,
    record_execution_effort_durably,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.execution_effort import (
    ExecutionEffortEntityNotFoundError,
    ExecutionEffortObservation,
    ExecutionEffortObservationError,
    create_execution_effort_observation,
)
from trajectory_os.domain.portfolio import Portfolio

BASE_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


class _SentinelError(Exception):
    """Repository-side failure with a non-colliding identity."""


class FakePortfolioRepository:
    """In-memory, behaviorally scriptable PortfolioRepository double (read-only use)."""

    def __init__(
        self,
        portfolios: dict[UUID, Portfolio] | None = None,
        *,
        load_error: Exception | None = None,
    ) -> None:
        self._portfolios = dict(portfolios or {})
        self._load_error = load_error
        self.loaded_ids: list[object] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.loaded_ids.append(portfolio_id)
        if self._load_error is not None:
            raise self._load_error
        return self._portfolios.get(portfolio_id)

    def save(self, portfolio: Portfolio) -> None:
        raise AssertionError("the execution-effort boundary must never save")


class FakeObservationRepository:
    """In-memory, behaviorally scriptable execution-effort observation double."""

    def __init__(self, *, add_error: Exception | None = None) -> None:
        self._add_error = add_error
        self.added: list[ExecutionEffortObservation] = []
        self.get_calls: list[object] = []

    def add(self, observation: ExecutionEffortObservation) -> None:
        if self._add_error is not None:
            raise self._add_error
        self.added.append(observation)

    def get(
        self,
        observation_id: UUID,
    ) -> ExecutionEffortObservation | None:
        self.get_calls.append(observation_id)
        return next((obs for obs in self.added if obs.id == observation_id), None)


def _entity_portfolio(name: str) -> Portfolio:
    entity = TrajectoryEntity(
        id=uuid4(),
        entity_type=EntityType.PROJECT,
        title="Platform",
        status=EntityStatus.ACTIVE,
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )
    return Portfolio(id=uuid4(), name=name, entities=[entity])


def test_valid_durable_recording_returns_domain_observation() -> None:
    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    observation_id = uuid4()
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()

    result = record_execution_effort_durably(
        portfolio.id,
        observation_id,
        entity.id,
        42,
        OBSERVED_AT,
        portfolio_repository,
        observation_repository,
    )

    assert isinstance(result, ExecutionEffortObservation)
    assert result.id == observation_id
    assert result.portfolio_id == portfolio.id
    assert result.entity_id == entity.id
    assert result.duration_seconds == 42
    assert result.observed_at == OBSERVED_AT


@pytest.mark.parametrize(
    "bad_portfolio_id",
    ["0148790b-ba4c-5f9e-9f6c-8c4e21d5b0c1", 42, b"0148790b", [uuid4()], None, True],
)
def test_invalid_portfolio_id_rejected_before_any_repository_interaction(
    bad_portfolio_id: object,
) -> None:
    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()

    with pytest.raises(DurableExecutionEffortError) as excinfo:
        record_execution_effort_durably(
            cast(UUID, bad_portfolio_id),
            uuid4(),
            entity.id,
            42,
            OBSERVED_AT,
            portfolio_repository,
            observation_repository,
        )

    assert type(excinfo.value) is DurableExecutionEffortError
    assert not isinstance(excinfo.value, ExecutionEffortPortfolioNotFoundError)
    assert portfolio_repository.loaded_ids == []
    assert observation_repository.added == []


def test_load_receives_exactly_requested_portfolio_id() -> None:
    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()

    record_execution_effort_durably(
        portfolio.id,
        uuid4(),
        entity.id,
        42,
        OBSERVED_AT,
        portfolio_repository,
        observation_repository,
    )

    assert portfolio_repository.loaded_ids == [portfolio.id]


def test_missing_portfolio_raises_and_adds_nothing() -> None:
    missing_id = uuid4()
    portfolio_repository = FakePortfolioRepository()
    observation_repository = FakeObservationRepository()

    with pytest.raises(ExecutionEffortPortfolioNotFoundError) as excinfo:
        record_execution_effort_durably(
            missing_id,
            uuid4(),
            uuid4(),
            42,
            OBSERVED_AT,
            portfolio_repository,
            observation_repository,
        )

    assert isinstance(excinfo.value, DurableExecutionEffortError)
    assert portfolio_repository.loaded_ids == [missing_id]
    assert observation_repository.added == []
    assert observation_repository.get_calls == []


def test_current_persisted_portfolio_is_authoritative() -> None:
    # A stale caller-side Portfolio snapshot shares the id but lacks the
    # entity; only the CURRENT persisted copy carries it. Success proves
    # the persisted state loaded via load() won.
    entity_id = uuid4()
    portfolio_id = uuid4()
    stale = Portfolio(id=portfolio_id, name="stale", entities=[])
    assert stale.get_entity(entity_id) is None
    stored = Portfolio(
        id=portfolio_id,
        name="persisted",
        entities=[
            TrajectoryEntity(
                id=entity_id,
                entity_type=EntityType.TASK,
                title="Persisted-only entity",
                status=EntityStatus.ACTIVE,
                created_at=BASE_TS,
                updated_at=BASE_TS,
            )
        ],
    )
    assert stored.get_entity(entity_id) is not None
    portfolio_repository = FakePortfolioRepository({portfolio_id: stored})
    observation_repository = FakeObservationRepository()
    observation_id = uuid4()

    result = record_execution_effort_durably(
        portfolio_id,
        observation_id,
        entity_id,
        120,
        OBSERVED_AT,
        portfolio_repository,
        observation_repository,
    )

    assert result.entity_id == entity_id
    assert result.portfolio_id == portfolio_id
    assert observation_repository.added == [result]


def test_entity_missing_from_current_portfolio_raises_real_domain_error() -> None:
    portfolio = _entity_portfolio("V1.8-B")
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()

    with pytest.raises(ExecutionEffortEntityNotFoundError) as excinfo:
        record_execution_effort_durably(
            portfolio.id,
            uuid4(),
            uuid4(),
            42,
            OBSERVED_AT,
            portfolio_repository,
            observation_repository,
        )

    assert isinstance(excinfo.value, ExecutionEffortObservationError)
    assert not isinstance(
        excinfo.value, ExecutionEffortPortfolioNotFoundError
    ), "an unknown entity must fail as a V1.8-A domain error, not a boundary error"
    assert observation_repository.added == []


def test_invalid_observation_id_propagates_domain_error_and_does_not_add() -> None:
    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()

    with pytest.raises(ExecutionEffortObservationError) as excinfo:
        record_execution_effort_durably(
            portfolio.id,
            cast(UUID, "not a uuid"),  # deliberate wrong runtime type
            entity.id,
            42,
            OBSERVED_AT,
            portfolio_repository,
            observation_repository,
        )

    assert type(excinfo.value) is ExecutionEffortObservationError
    assert observation_repository.added == []


def test_invalid_entity_id_propagates_domain_error_and_does_not_add() -> None:
    portfolio = _entity_portfolio("V1.8-B")
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()

    with pytest.raises(ExecutionEffortObservationError) as excinfo:
        record_execution_effort_durably(
            portfolio.id,
            uuid4(),
            cast(UUID, 42),  # deliberate wrong runtime type
            42,
            OBSERVED_AT,
            portfolio_repository,
            observation_repository,
        )

    assert type(excinfo.value) is ExecutionEffortObservationError
    assert observation_repository.added == []


@pytest.mark.parametrize(
    "bad_duration",
    [True, 0, -3, 1.5, "42"],
)
def test_invalid_duration_propagates_domain_error_and_does_not_add(
    bad_duration: object,
) -> None:
    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()

    with pytest.raises(ExecutionEffortObservationError):
        record_execution_effort_durably(
            portfolio.id,
            uuid4(),
            entity.id,
            cast(int, bad_duration),
            OBSERVED_AT,
            portfolio_repository,
            observation_repository,
        )

    assert observation_repository.added == []


def test_naive_observed_at_propagates_domain_error_and_does_not_add() -> None:
    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()
    naive = datetime(2026, 3, 1, 9, 0)  # naive on purpose

    with pytest.raises(ExecutionEffortObservationError) as excinfo:
        record_execution_effort_durably(
            portfolio.id,
            uuid4(),
            entity.id,
            42,
            naive,
            portfolio_repository,
            observation_repository,
        )

    assert observation_repository.added == []
    # the boundary still loaded; the failure is the V1.8-A domain's
    assert portfolio_repository.loaded_ids == [portfolio.id]
    assert not isinstance(excinfo.value, ExecutionEffortPortfolioNotFoundError)


def test_success_adds_exactly_once_by_identity() -> None:
    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()

    result = record_execution_effort_durably(
        portfolio.id,
        uuid4(),
        entity.id,
        42,
        OBSERVED_AT,
        portfolio_repository,
        observation_repository,
    )

    assert len(observation_repository.added) == 1
    assert observation_repository.added[0] is result


def test_application_returns_exact_domain_observation_object() -> None:
    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()
    observation_id = uuid4()

    result = record_execution_effort_durably(
        portfolio.id,
        observation_id,
        entity.id,
        42,
        OBSERVED_AT,
        portfolio_repository,
        observation_repository,
    )

    # recompute the domain result through the same V1.8-A factory
    expected = create_execution_effort_observation(
        portfolio,
        observation_id,
        entity.id,
        42,
        OBSERVED_AT,
    )
    assert result == expected
    # the EXACT object handed to add() is the EXACT object returned
    assert observation_repository.added[0] is result
    assert (result.id, result.portfolio_id, result.entity_id) == (
        observation_id,
        portfolio.id,
        entity.id,
    )
    assert result.source.name == "USER_CONFIRMED"


def test_portfolio_load_exception_propagates_unchanged_and_adds_nothing() -> None:
    sentinel = _SentinelError("load exploded")
    portfolio_id = uuid4()
    portfolio_repository = FakePortfolioRepository(load_error=sentinel)
    observation_repository = FakeObservationRepository()

    with pytest.raises(_SentinelError) as excinfo:
        record_execution_effort_durably(
            portfolio_id,
            uuid4(),
            uuid4(),
            42,
            OBSERVED_AT,
            portfolio_repository,
            observation_repository,
        )

    assert excinfo.value is sentinel
    assert portfolio_repository.loaded_ids == [portfolio_id]
    assert observation_repository.added == []


def test_observation_add_exception_propagates_unchanged() -> None:
    sentinel = _SentinelError("add exploded")
    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository(add_error=sentinel)

    with pytest.raises(_SentinelError) as excinfo:
        record_execution_effort_durably(
            portfolio.id,
            uuid4(),
            entity.id,
            42,
            OBSERVED_AT,
            portfolio_repository,
            observation_repository,
        )

    assert excinfo.value is sentinel
    assert observation_repository.added == []


def test_loaded_portfolio_remains_deeply_unchanged() -> None:
    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()
    before = portfolio.model_dump()
    before_entity = entity.model_dump()

    record_execution_effort_durably(
        portfolio.id,
        uuid4(),
        entity.id,
        42,
        OBSERVED_AT,
        portfolio_repository,
        observation_repository,
    )

    assert portfolio.model_dump() == before
    assert portfolio.entities[0].model_dump() == before_entity
    assert portfolio.get_entity(entity.id) is entity


def test_application_reuses_the_v1_6_portfolio_repository_protocol() -> None:
    # V1.8-B must reuse the EXISTING V1.6 Protocol object, not redefine it
    import typing

    from trajectory_os.application.work_breakdown_acceptance import (
        PortfolioRepository as V16Repository,
    )

    boundary_parameter = typing.get_type_hints(
        record_execution_effort_durably
    )["portfolio_repository"]

    assert boundary_parameter is V16Repository
    assert boundary_parameter.__name__ == "PortfolioRepository"
    assert "load" in vars(boundary_parameter)
    assert "save" in vars(boundary_parameter)


def test_get_is_in_protocol_but_never_called_by_the_use_case() -> None:
    from trajectory_os.application import PortfolioRepository

    portfolio = _entity_portfolio("V1.8-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    observation_repository = FakeObservationRepository()

    record_execution_effort_durably(
        portfolio.id,
        uuid4(),
        entity.id,
        42,
        OBSERVED_AT,
        portfolio_repository,
        observation_repository,
    )

    # get() is part of the structural Protocol ...
    assert "get" in vars(ExecutionEffortObservationRepository)
    assert "add" in vars(ExecutionEffortObservationRepository)
    assert "load" in vars(PortfolioRepository)
    assert "save" in vars(PortfolioRepository)
    # ... but the write use case never calls it
    assert observation_repository.get_calls == []
    # the fakes provide the structural members required by the Protocols
    assert callable(observation_repository.add)
    assert callable(observation_repository.get)
    assert callable(portfolio_repository.load)
    assert callable(portfolio_repository.save)
