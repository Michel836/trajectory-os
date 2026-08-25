"""Unit tests for the durable entity status transition (V1.7-B)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from trajectory_os.application import (
    DurableEntityStatusTransitionError,
    StatusTransitionPortfolioNotFoundError,
    transition_entity_status_durably,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.entity_status_transition import (
    EntityStatusTransitionError,
    SameStatusTransitionError,
    StaleChangedAtError,
    UnknownEntityError,
)
from trajectory_os.domain.portfolio import Portfolio

BASE_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LATER_TS = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)


class _SentinelError(Exception):
    """Repository-side failure with a non-colliding identity."""


class FakePortfolioRepository:
    """In-memory, behaviorally scriptable PortfolioRepository double."""

    def __init__(
        self,
        portfolios: dict[UUID, Portfolio] | None = None,
        *,
        load_error: Exception | None = None,
        save_error: Exception | None = None,
    ) -> None:
        self._portfolios = dict(portfolios or {})
        self._load_error = load_error
        self._save_error = save_error
        self.loaded_ids: list[object] = []
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.loaded_ids.append(portfolio_id)
        if self._load_error is not None:
            raise self._load_error
        return self._portfolios.get(portfolio_id)

    def save(self, portfolio: Portfolio) -> None:
        if self._save_error is not None:
            raise self._save_error
        self.saved.append(portfolio)


def _entity_portfolio(name: str, status: EntityStatus) -> Portfolio:
    entity = TrajectoryEntity(
        id=uuid4(),
        entity_type=EntityType.PROJECT,
        title="Platform",
        status=status,
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )
    return Portfolio(id=uuid4(), name=name, entities=[entity])


def _entity(portfolio: Portfolio) -> TrajectoryEntity:
    return portfolio.entities[0]


def test_success_transitions_persists_and_returns_fresh_result() -> None:
    portfolio = _entity_portfolio("V1.7-B", EntityStatus.PAUSED)
    entity = _entity(portfolio)
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    loaded_snapshot = portfolio.model_dump()

    result = transition_entity_status_durably(
        portfolio.id,
        entity.id,
        EntityStatus.ACTIVE,
        LATER_TS,
        repository,
    )

    # load received exactly the requested portfolio_id
    assert repository.loaded_ids == [portfolio.id]
    # exactly one save, by identity, of exactly result.portfolio
    assert len(repository.saved) == 1
    assert repository.saved[0] is result.portfolio
    # the result reflects the CURRENT persisted state
    assert result.entity_id == entity.id
    assert result.previous_status == EntityStatus.PAUSED
    assert result.new_status == EntityStatus.ACTIVE
    assert result.changed_at == LATER_TS
    # fresh portfolio: different object, updated entity, same identity
    assert result.portfolio is not portfolio
    assert result.portfolio.id == portfolio.id
    assert result.portfolio.get_entity(entity.id).status == EntityStatus.ACTIVE
    assert result.portfolio.get_entity(entity.id).updated_at == LATER_TS
    # the loaded portfolio was not mutated
    assert portfolio.model_dump() == loaded_snapshot
    assert _entity(portfolio).status == EntityStatus.PAUSED


def test_previous_status_follows_current_persisted_state() -> None:
    persisted = _entity_portfolio("V1.7-B", EntityStatus.SOMEDAY)
    stored = _entity_portfolio("V1.7-B", EntityStatus.SOMEDAY)
    stored.id = persisted.id
    stored.entities[0].id = _entity(persisted).id
    repository = FakePortfolioRepository({persisted.id: stored})
    entity = _entity(persisted)

    result = transition_entity_status_durably(
        persisted.id,
        entity.id,
        EntityStatus.COMPLETED,
        LATER_TS,
        repository,
    )

    assert result.previous_status == _entity(stored).status == EntityStatus.SOMEDAY
    assert result.portfolio.get_entity(entity.id).status == EntityStatus.COMPLETED


def test_missing_portfolio_raises_and_does_not_save() -> None:
    missing_id = uuid4()
    repository = FakePortfolioRepository()

    with pytest.raises(StatusTransitionPortfolioNotFoundError) as excinfo:
        transition_entity_status_durably(
            missing_id,
            uuid4(),
            EntityStatus.ACTIVE,
            LATER_TS,
            repository,
        )

    assert isinstance(excinfo.value, DurableEntityStatusTransitionError)
    assert repository.loaded_ids == [missing_id]
    assert repository.saved == []


@pytest.mark.parametrize(
    "bad_id",
    ["0148790b-ba4c-5f9e-9f6c-8c4e21d5b0c1", 42, b"0148790b", [uuid4()], None, True],
)
def test_invalid_portfolio_id_fails_before_load(bad_id: object) -> None:
    repository = FakePortfolioRepository()

    with pytest.raises(DurableEntityStatusTransitionError) as excinfo:
        transition_entity_status_durably(  # type: ignore[arg-type]
            bad_id,
            uuid4(),
            EntityStatus.ACTIVE,
            LATER_TS,
            repository,
        )

    assert type(excinfo.value) is DurableEntityStatusTransitionError
    assert repository.loaded_ids == []
    assert repository.saved == []


def test_unknown_entity_domain_error_propagates_and_does_not_save() -> None:
    portfolio = _entity_portfolio("V1.7-B", EntityStatus.PAUSED)
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    loaded_snapshot = portfolio.model_dump()

    with pytest.raises(UnknownEntityError) as excinfo:
        transition_entity_status_durably(
            portfolio.id,
            uuid4(),
            EntityStatus.ACTIVE,
            LATER_TS,
            repository,
        )

    assert isinstance(excinfo.value, EntityStatusTransitionError)
    assert repository.saved == []
    assert portfolio.model_dump() == loaded_snapshot


def test_same_status_domain_error_propagates_and_does_not_save() -> None:
    portfolio = _entity_portfolio("V1.7-B", EntityStatus.ACTIVE)
    entity = _entity(portfolio)
    repository = FakePortfolioRepository({portfolio.id: portfolio})

    with pytest.raises(SameStatusTransitionError) as excinfo:
        transition_entity_status_durably(
            portfolio.id,
            entity.id,
            EntityStatus.ACTIVE,
            LATER_TS,
            repository,
        )

    assert isinstance(excinfo.value, EntityStatusTransitionError)
    assert repository.saved == []


def test_stale_changed_at_domain_error_propagates_and_does_not_save() -> None:
    portfolio = _entity_portfolio("V1.7-B", EntityStatus.PAUSED)
    entity = _entity(portfolio)
    repository = FakePortfolioRepository({portfolio.id: portfolio})

    stale = BASE_TS - timedelta(seconds=1)

    with pytest.raises(StaleChangedAtError) as excinfo:
        transition_entity_status_durably(
            portfolio.id,
            entity.id,
            EntityStatus.ACTIVE,
            stale,
            repository,
        )

    assert isinstance(excinfo.value, EntityStatusTransitionError)
    assert repository.saved == []


def test_naive_changed_at_domain_error_propagates_and_does_not_save() -> None:
    portfolio = _entity_portfolio("V1.7-B", EntityStatus.PAUSED)
    entity = _entity(portfolio)
    repository = FakePortfolioRepository({portfolio.id: portfolio})

    naive = datetime(2026, 2, 1, 12, 0)  # naive on purpose

    with pytest.raises(EntityStatusTransitionError) as excinfo:
        transition_entity_status_durably(
            portfolio.id,
            entity.id,
            EntityStatus.ACTIVE,
            naive,
            repository,
        )

    assert not isinstance(excinfo.value, StatusTransitionPortfolioNotFoundError)
    assert repository.saved == []
    # still loads the portfolio; the failure is the domain's, not the boundary's
    assert repository.loaded_ids == [portfolio.id]


def test_repository_load_exception_propagates_unchanged() -> None:
    sentinel = _SentinelError("load exploded")
    portfolio_id = uuid4()
    repository = FakePortfolioRepository(load_error=sentinel)

    with pytest.raises(_SentinelError) as excinfo:
        transition_entity_status_durably(
            portfolio_id,
            uuid4(),
            EntityStatus.ACTIVE,
            LATER_TS,
            repository,
        )

    assert excinfo.value is sentinel
    assert repository.loaded_ids == [portfolio_id]
    assert repository.saved == []


def test_repository_save_exception_propagates_unchanged() -> None:
    sentinel = _SentinelError("save exploded")
    portfolio = _entity_portfolio("V1.7-B", EntityStatus.PAUSED)
    entity = _entity(portfolio)
    repository = FakePortfolioRepository({portfolio.id: portfolio}, save_error=sentinel)

    with pytest.raises(_SentinelError) as excinfo:
        transition_entity_status_durably(
            portfolio.id,
            entity.id,
            EntityStatus.ACTIVE,
            LATER_TS,
            repository,
        )

    assert excinfo.value is sentinel
    assert repository.loaded_ids == [portfolio.id]
    assert repository.saved == []


def test_loaded_portfolio_remains_deeply_unchanged_on_success() -> None:
    portfolio = _entity_portfolio("V1.7-B", EntityStatus.WAITING)
    entity = _entity(portfolio)
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    before = portfolio.model_dump()
    before_entity = entity.model_dump()

    result = transition_entity_status_durably(
        portfolio.id,
        entity.id,
        EntityStatus.ACTIVE,
        LATER_TS,
        repository,
    )

    assert portfolio.model_dump() == before
    assert _entity(portfolio).model_dump() == before_entity
    assert result.portfolio is not portfolio
    assert result.portfolio.entities[0] is not portfolio.entities[0]


def test_only_one_save_occurs_on_success() -> None:
    portfolio = _entity_portfolio("V1.7-B", EntityStatus.INCUBATOR)
    entity = _entity(portfolio)
    repository = FakePortfolioRepository({portfolio.id: portfolio})

    transition_entity_status_durably(
        portfolio.id,
        entity.id,
        EntityStatus.ACTIVE,
        LATER_TS,
        repository,
    )

    assert len(repository.saved) == 1
    assert len(repository.loaded_ids) == 1


def test_repository_boundary_reuses_the_v1_6_protocol() -> None:
    # V1.7-B reuses the existing V1.6 Protocol object without redefining it
    from trajectory_os.application.entity_status_transition import (
        PortfolioRepository as BoundaryRepository,
    )
    from trajectory_os.application.work_breakdown_acceptance import (
        PortfolioRepository as V16Repository,
    )

    assert BoundaryRepository is V16Repository
    assert BoundaryRepository.__name__ == "PortfolioRepository"
