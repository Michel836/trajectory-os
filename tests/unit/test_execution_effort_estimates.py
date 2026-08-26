"""Unit tests for the V1.10-A planned-effort estimate domain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
    ExecutionEffortEstimateEntityNotFoundError,
    ExecutionEffortEstimateError,
    create_execution_effort_estimate,
)
from trajectory_os.domain.portfolio import Portfolio

BASE_TIME = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
BACKFILL_TIME = BASE_TIME - timedelta(days=30)
LATER_TIME = BASE_TIME + timedelta(seconds=90)
NAIVE_TIME = datetime(2026, 8, 25, 12, 0, 0)


def make_entity(
    updated_at: datetime = BASE_TIME,
    status: EntityStatus = EntityStatus.ACTIVE,
) -> TrajectoryEntity:
    return TrajectoryEntity(
        entity_type=EntityType.PROJECT,
        title="Target",
        description="the entity under test",
        status=status,
        source=SourceKind.USER_CONFIRMED,
        confidence=0.9,
        created_at=BASE_TIME,
        updated_at=updated_at,
    )


def make_portfolio(entity: TrajectoryEntity) -> Portfolio:
    return Portfolio(id=uuid4(), name="Portfolio", entities=[entity])


def make_portfolio_with_two_entities(
) -> tuple[Portfolio, TrajectoryEntity, TrajectoryEntity]:
    first = make_entity()
    second = make_entity(status=EntityStatus.INCUBATOR)
    portfolio = Portfolio(id=uuid4(), name="Portfolio", entities=[first, second])
    return portfolio, first, second


def direct_model(
    *,
    duration_seconds: object = 60,
    estimated_at: object = LATER_TIME,
) -> ExecutionEffortEstimate:
    return ExecutionEffortEstimate(
        id=uuid4(),
        portfolio_id=uuid4(),
        entity_id=uuid4(),
        duration_seconds=cast(int, duration_seconds),
        estimated_at=cast(datetime, estimated_at),
        source=SourceKind.USER_CONFIRMED,
    )


def test_valid_positive_estimate() -> None:
    entity = make_entity()
    portfolio = make_portfolio(entity)
    estimate_id = uuid4()

    estimate = create_execution_effort_estimate(
        portfolio, estimate_id, entity.id, 240, LATER_TIME
    )

    assert isinstance(estimate, ExecutionEffortEstimate)


def test_estimate_id_preserved_exactly() -> None:
    portfolio = make_portfolio(make_entity())
    estimate_id = uuid4()

    estimate = create_execution_effort_estimate(
        portfolio, estimate_id, portfolio.entities[0].id, 60, LATER_TIME
    )

    assert estimate.id is estimate_id
    assert estimate.id == estimate_id


def test_portfolio_id_derived_from_current_portfolio() -> None:
    portfolio = make_portfolio(make_entity())

    estimate = create_execution_effort_estimate(
        portfolio, uuid4(), portfolio.entities[0].id, 60, LATER_TIME
    )

    assert estimate.portfolio_id == portfolio.id


def test_entity_id_duration_and_estimated_at_preserved() -> None:
    entity = make_entity()
    portfolio = make_portfolio(entity)

    estimate = create_execution_effort_estimate(
        portfolio, uuid4(), entity.id, 1234, LATER_TIME
    )

    assert estimate.entity_id is entity.id
    assert estimate.duration_seconds == 1234
    assert estimate.estimated_at is LATER_TIME
    assert estimate.estimated_at == LATER_TIME


def test_zero_direct_effort_is_valid_and_meaningful() -> None:
    portfolio = make_portfolio(make_entity())

    estimate = create_execution_effort_estimate(
        portfolio, uuid4(), portfolio.entities[0].id, 0, LATER_TIME
    )

    assert estimate.duration_seconds == 0


def test_negative_duration_rejected() -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortEstimateError, match=">= 0"):
        create_execution_effort_estimate(
            portfolio, uuid4(), portfolio.entities[0].id, -1, LATER_TIME
        )


@pytest.mark.parametrize("bad_duration", [True, False, 60.5, "60", None, object()])
def test_non_int_duration_rejected(bad_duration: object) -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortEstimateError):
        create_execution_effort_estimate(
            portfolio,
            uuid4(),
            portfolio.entities[0].id,
            cast(int, bad_duration),
            LATER_TIME,
        )


def test_model_rejects_negative_duration_directly() -> None:
    with pytest.raises(ValidationError, match="duration_seconds"):
        direct_model(duration_seconds=-1)


@pytest.mark.parametrize("bad_duration", [True, False, 60.5, "60", None])
def test_model_rejects_non_int_duration_directly(bad_duration: object) -> None:
    with pytest.raises(ValidationError):
        direct_model(duration_seconds=bad_duration)


def test_aware_estimated_at_required_by_factory() -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortEstimateError, match="timezone-aware"):
        create_execution_effort_estimate(
            portfolio, uuid4(), portfolio.entities[0].id, 60, NAIVE_TIME
        )


def test_model_rejects_naive_estimated_at_directly() -> None:
    with pytest.raises(ValidationError, match="estimated_at"):
        direct_model(estimated_at=NAIVE_TIME)


def test_model_rejects_non_datetime_estimated_at_directly() -> None:
    with pytest.raises(ValidationError):
        direct_model(estimated_at="2026-08-25T12:00:00+00:00")


def test_backfilled_estimate_is_allowed() -> None:
    portfolio = make_portfolio(make_entity())

    estimate = create_execution_effort_estimate(
        portfolio, uuid4(), portfolio.entities[0].id, 60, BACKFILL_TIME
    )

    assert estimate.estimated_at == BACKFILL_TIME


def test_portfolio_and_id_types_are_rejected() -> None:
    entity = make_entity()

    with pytest.raises(ExecutionEffortEstimateError):
        create_execution_effort_estimate(
            cast(Portfolio, "not a portfolio"),
            uuid4(),
            entity.id,
            60,
            LATER_TIME,
        )

    portfolio = make_portfolio(entity)

    with pytest.raises(ExecutionEffortEstimateError):
        create_execution_effort_estimate(
            portfolio,
            cast(UUID, "not-a-uuid"),
            entity.id,
            60,
            LATER_TIME,
        )

    with pytest.raises(ExecutionEffortEstimateError):
        create_execution_effort_estimate(
            portfolio, uuid4(), cast(UUID, "not-a-uuid"), 60, LATER_TIME
        )


def test_target_entity_must_exist_in_current_portfolio() -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortEstimateEntityNotFoundError):
        create_execution_effort_estimate(
            portfolio, uuid4(), uuid4(), 60, LATER_TIME
        )


def test_only_the_named_entity_is_accepted() -> None:
    portfolio, first, second = make_portfolio_with_two_entities()

    estimate = create_execution_effort_estimate(
        portfolio, uuid4(), first.id, 60, LATER_TIME
    )

    assert estimate.entity_id == first.id
    assert estimate.entity_id != second.id


def test_recording_source_is_user_confirmed() -> None:
    portfolio = make_portfolio(make_entity())

    estimate = create_execution_effort_estimate(
        portfolio, uuid4(), portfolio.entities[0].id, 60, LATER_TIME
    )

    assert estimate.source is SourceKind.USER_CONFIRMED


def test_model_is_frozen() -> None:
    portfolio = make_portfolio(make_entity())

    estimate = create_execution_effort_estimate(
        portfolio, uuid4(), portfolio.entities[0].id, 60, LATER_TIME
    )

    assert estimate.model_config.get("frozen") is True

    with pytest.raises(ValidationError):
        estimate.duration_seconds = 42  # type: ignore[misc]


def test_model_rejects_wrong_uuid_types_directly() -> None:
    with pytest.raises(ValidationError):
        ExecutionEffortEstimate(
            id=cast(UUID, "not-a-uuid"),
            portfolio_id=uuid4(),
            entity_id=uuid4(),
            duration_seconds=60,
            estimated_at=LATER_TIME,
            source=SourceKind.USER_CONFIRMED,
        )


def test_portfolio_remains_deeply_unchanged() -> None:
    entity = make_entity()
    portfolio = make_portfolio(entity)
    snapshot = portfolio.model_dump()

    create_execution_effort_estimate(
        portfolio, uuid4(), entity.id, 60, LATER_TIME
    )

    assert portfolio.model_dump() == snapshot
    assert entity.updated_at == BASE_TIME


def test_new_estimate_for_same_entity_is_not_an_update() -> None:
    portfolio = make_portfolio(make_entity())

    first = create_execution_effort_estimate(
        portfolio, uuid4(), portfolio.entities[0].id, 60, LATER_TIME
    )
    second = create_execution_effort_estimate(
        portfolio, uuid4(), portfolio.entities[0].id, 120, LATER_TIME
    )

    assert first.id != second.id
    assert first.duration_seconds == 60
    assert second.duration_seconds == 120


def test_errors_are_value_error_subclasses() -> None:
    assert issubclass(
        ExecutionEffortEstimateEntityNotFoundError, ExecutionEffortEstimateError
    )
    assert issubclass(ExecutionEffortEstimateError, ValueError)
