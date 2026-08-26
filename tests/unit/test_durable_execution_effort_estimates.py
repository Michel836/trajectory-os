"""Unit tests for the durable planned-effort estimate boundary (V1.10-B)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from trajectory_os.application import (
    DurableExecutionEffortEstimateError,
    ExecutionEffortEstimatePortfolioNotFoundError,
    ExecutionEffortEstimateRepository,
    record_execution_effort_estimate_durably,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
    ExecutionEffortEstimateEntityNotFoundError,
    ExecutionEffortEstimateError,
)
from trajectory_os.domain.portfolio import Portfolio

BASE_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ESTIMATED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
BACKFILLED_AT = datetime(2025, 12, 1, 9, 0, tzinfo=UTC)


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
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.loaded_ids.append(portfolio_id)
        if self._load_error is not None:
            raise self._load_error
        return self._portfolios.get(portfolio_id)

    def save(self, portfolio: Portfolio) -> None:
        self.saved.append(portfolio)
        raise AssertionError("the V1.10-B boundary must never save")


class FakeEstimateRepository:
    """In-memory, behaviorally scriptable execution-effort estimate double."""

    def __init__(self, *, add_error: Exception | None = None) -> None:
        self._add_error = add_error
        self.added: list[ExecutionEffortEstimate] = []
        self.added_order: list[UUID] = []

    def add(self, estimate: ExecutionEffortEstimate) -> None:
        if self._add_error is not None:
            raise self._add_error
        if estimate.id in self.added_order:
            raise AssertionError("the V1.10-B boundary must not append duplicates")
        self.added_order.append(estimate.id)
        self.added.append(estimate)

    def get(
        self, estimate_id: UUID
    ) -> ExecutionEffortEstimate | None:
        return next(
            (estimate for estimate in self.added if estimate.id == estimate_id),
            None,
        )


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


def test_valid_durable_recording_returns_domain_estimate() -> None:
    portfolio = _entity_portfolio("V1.10-B")
    entity = portfolio.entities[0]
    estimate_id = uuid4()
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    estimate_repository = FakeEstimateRepository()

    result = record_execution_effort_estimate_durably(
        portfolio.id,
        estimate_id,
        entity.id,
        42,
        ESTIMATED_AT,
        portfolio_repository,
        estimate_repository,
    )

    assert isinstance(result, ExecutionEffortEstimate)
    assert result.id == estimate_id
    assert result.portfolio_id == portfolio.id
    assert result.entity_id == entity.id
    assert result.duration_seconds == 42
    assert result.estimated_at == ESTIMATED_AT

    # The CURRENT persisted Portfolio is load authority for entity identity.
    assert portfolio_repository.loaded_ids == [portfolio.id]
    # Exactly one append after successful domain construction.
    assert estimate_repository.added_order == [estimate_id]
    assert estimate_repository.get(estimate_id) is result
    # No Portfolio save, and the returned object is the domain value.
    assert portfolio_repository.saved == []


def test_zero_duration_records_durably() -> None:
    portfolio = _entity_portfolio("V1.10-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    estimate_repository = FakeEstimateRepository()

    result = record_execution_effort_estimate_durably(
        portfolio.id,
        uuid4(),
        entity.id,
        0,
        ESTIMATED_AT,
        portfolio_repository,
        estimate_repository,
    )

    assert result.duration_seconds == 0
    assert estimate_repository.added_order == [result.id]


def test_backfilled_estimate_records_durably() -> None:
    portfolio = _entity_portfolio("V1.10-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    estimate_repository = FakeEstimateRepository()

    result = record_execution_effort_estimate_durably(
        portfolio.id,
        uuid4(),
        entity.id,
        30,
        BACKFILLED_AT,
        portfolio_repository,
        estimate_repository,
    )

    assert result.estimated_at == BACKFILLED_AT


@pytest.mark.parametrize(
    "bad_portfolio_id",
    ["0148790b-ba4c-5f9e-9f6c-8c4e21d5b0c1", 42, b"0148790b", [uuid4()], None, True],
)
def test_invalid_portfolio_id_rejected_before_any_repository_interaction(
    bad_portfolio_id: object,
) -> None:
    portfolio = _entity_portfolio("V1.10-B")
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    estimate_repository = FakeEstimateRepository()

    with pytest.raises(DurableExecutionEffortEstimateError) as excinfo:
        record_execution_effort_estimate_durably(
            cast(UUID, bad_portfolio_id),
            uuid4(),
            portfolio.entities[0].id,
            42,
            ESTIMATED_AT,
            portfolio_repository,
            estimate_repository,
        )

    assert type(excinfo.value) is DurableExecutionEffortEstimateError
    assert not isinstance(excinfo.value, ExecutionEffortEstimatePortfolioNotFoundError)
    assert portfolio_repository.loaded_ids == []
    assert estimate_repository.added == []


def test_missing_portfolio_fails_explicitly_before_append() -> None:
    missing_id = uuid4()
    portfolio_repository = FakePortfolioRepository()
    estimate_repository = FakeEstimateRepository()

    with pytest.raises(ExecutionEffortEstimatePortfolioNotFoundError) as excinfo:
        record_execution_effort_estimate_durably(
            missing_id,
            uuid4(),
            uuid4(),
            42,
            ESTIMATED_AT,
            portfolio_repository,
            estimate_repository,
        )

    assert type(excinfo.value) is ExecutionEffortEstimatePortfolioNotFoundError
    assert portfolio_repository.loaded_ids == [missing_id]
    assert estimate_repository.added == []


def test_entity_not_in_current_portfolio_fails_before_append() -> None:
    portfolio = _entity_portfolio("V1.10-B")
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    estimate_repository = FakeEstimateRepository()

    with pytest.raises(ExecutionEffortEstimateEntityNotFoundError):
        record_execution_effort_estimate_durably(
            portfolio.id,
            uuid4(),
            uuid4(),
            42,
            ESTIMATED_AT,
            portfolio_repository,
            estimate_repository,
        )

    assert estimate_repository.added == []


@pytest.mark.parametrize("bad_duration", [True, False, -1, 1.5, "90"])
def test_domain_duration_validation_failure_appends_nothing(
    bad_duration: object,
) -> None:
    portfolio = _entity_portfolio("V1.10-B")
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    estimate_repository = FakeEstimateRepository()

    with pytest.raises(ExecutionEffortEstimateError):
        record_execution_effort_estimate_durably(
            portfolio.id,
            uuid4(),
            portfolio.entities[0].id,
            cast(int, bad_duration),
            ESTIMATED_AT,
            portfolio_repository,
            estimate_repository,
        )

    assert estimate_repository.added == []
    assert portfolio_repository.saved == []


def test_naive_estimated_at_failure_appends_nothing() -> None:
    portfolio = _entity_portfolio("V1.10-B")
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    estimate_repository = FakeEstimateRepository()

    with pytest.raises(ExecutionEffortEstimateError, match="timezone-aware"):
        record_execution_effort_estimate_durably(
            portfolio.id,
            uuid4(),
            portfolio.entities[0].id,
            42,
            datetime(2026, 3, 1, 9, 0),
            portfolio_repository,
            estimate_repository,
        )

    assert estimate_repository.added == []


def test_portfolio_load_error_propagates_unchanged() -> None:
    portfolio_repository = FakePortfolioRepository(
        load_error=_SentinelError("load blew up")
    )
    estimate_repository = FakeEstimateRepository()

    with pytest.raises(_SentinelError, match="load blew up"):
        record_execution_effort_estimate_durably(
            uuid4(),
            uuid4(),
            uuid4(),
            42,
            ESTIMATED_AT,
            portfolio_repository,
            estimate_repository,
        )

    assert estimate_repository.added == []


def test_add_error_propagates_unchanged() -> None:
    portfolio = _entity_portfolio("V1.10-B")
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    estimate_repository = FakeEstimateRepository(add_error=_SentinelError("add blew up"))

    with pytest.raises(_SentinelError, match="add blew up"):
        record_execution_effort_estimate_durably(
            portfolio.id,
            uuid4(),
            portfolio.entities[0].id,
            42,
            ESTIMATED_AT,
            portfolio_repository,
            estimate_repository,
        )

    assert estimate_repository.added == []


def test_repository_side_effects_are_limited_to_one_add() -> None:
    portfolio = _entity_portfolio("V1.10-B")
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    estimate_repository: ExecutionEffortEstimateRepository = FakeEstimateRepository()

    result = record_execution_effort_estimate_durably(
        portfolio.id,
        uuid4(),
        portfolio.entities[0].id,
        42,
        ESTIMATED_AT,
        portfolio_repository,
        estimate_repository,
    )

    assert portfolio_repository.loaded_ids == [portfolio.id]
    assert portfolio_repository.saved == []
    assert estimate_repository.added_order == [result.id]


def test_new_revision_appends_without_touching_previous() -> None:
    portfolio = _entity_portfolio("V1.10-B")
    entity = portfolio.entities[0]
    portfolio_repository = FakePortfolioRepository({portfolio.id: portfolio})
    estimate_repository = FakeEstimateRepository()

    first = record_execution_effort_estimate_durably(
        portfolio.id,
        uuid4(),
        entity.id,
        42,
        ESTIMATED_AT,
        portfolio_repository,
        estimate_repository,
    )
    second = record_execution_effort_estimate_durably(
        portfolio.id,
        uuid4(),
        entity.id,
        90,
        ESTIMATED_AT,
        portfolio_repository,
        estimate_repository,
    )

    assert estimate_repository.added_order == [first.id, second.id]
    assert estimate_repository.get(first.id) is first
    assert estimate_repository.get(second.id) is second
    assert first.duration_seconds == 42
    assert second.duration_seconds == 90
