"""Unit tests for V1.8-A execution-effort observation domain."""

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
from trajectory_os.domain.execution_effort import (
    ExecutionEffortEntityNotFoundError,
    ExecutionEffortObservation,
    ExecutionEffortObservationError,
    create_execution_effort_observation,
)
from trajectory_os.domain.portfolio import Portfolio

BASE_TIME = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
EARLIER_TIME = BASE_TIME - timedelta(hours=6)
LATER_TIME = BASE_TIME + timedelta(seconds=90)


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


def make_portfolio_with_two_entities() -> tuple[Portfolio, TrajectoryEntity, TrajectoryEntity]:
    first = make_entity()
    second = make_entity(status=EntityStatus.INCUBATOR)
    portfolio = Portfolio(id=uuid4(), name="Portfolio", entities=[first, second])
    return portfolio, first, second


def direct_model(
    *,
    duration_seconds: object,
    observed_at: object,
) -> ExecutionEffortObservation:
    return ExecutionEffortObservation(
        id=uuid4(),
        portfolio_id=uuid4(),
        entity_id=uuid4(),
        duration_seconds=cast(int, duration_seconds),
        observed_at=cast(datetime, observed_at),
        source=SourceKind.USER_CONFIRMED,
    )


def test_valid_observation() -> None:
    entity = make_entity()
    portfolio = make_portfolio(entity)
    observation_id = uuid4()

    observation = create_execution_effort_observation(
        portfolio, observation_id, entity.id, 240, LATER_TIME
    )

    assert isinstance(observation, ExecutionEffortObservation)


def test_observation_id_preserved_exactly() -> None:
    portfolio = make_portfolio(make_entity())
    observation_id = uuid4()

    observation = create_execution_effort_observation(
        portfolio, observation_id, portfolio.entities[0].id, 60, LATER_TIME
    )

    assert observation.id is observation_id
    assert observation.id == observation_id


def test_portfolio_id_derived_from_portfolio() -> None:
    portfolio = make_portfolio(make_entity())

    observation = create_execution_effort_observation(
        portfolio, uuid4(), portfolio.entities[0].id, 60, LATER_TIME
    )

    assert observation.portfolio_id == portfolio.id


def test_entity_id_and_duration_and_observed_at_preserved() -> None:
    entity = make_entity()
    portfolio = make_portfolio(entity)

    observation = create_execution_effort_observation(
        portfolio, uuid4(), entity.id, 1234, LATER_TIME
    )

    assert observation.entity_id is entity.id
    assert observation.duration_seconds == 1234
    assert observation.observed_at is LATER_TIME
    assert observation.observed_at == LATER_TIME


def test_source_is_user_confirmed() -> None:
    portfolio = make_portfolio(make_entity())

    observation = create_execution_effort_observation(
        portfolio, uuid4(), portfolio.entities[0].id, 60, LATER_TIME
    )

    assert observation.source is SourceKind.USER_CONFIRMED


def test_observation_model_is_frozen() -> None:
    portfolio = make_portfolio(make_entity())

    observation = create_execution_effort_observation(
        portfolio, uuid4(), portfolio.entities[0].id, 60, LATER_TIME
    )

    assert observation.model_config.get("frozen") is True

    with pytest.raises(ValidationError):
        observation.duration_seconds = 61


def test_frozen_model_rejects_reassignment_of_every_field() -> None:
    portfolio = make_portfolio(make_entity())

    observation = create_execution_effort_observation(
        portfolio, uuid4(), portfolio.entities[0].id, 60, LATER_TIME
    )

    for field_name in ("id", "portfolio_id", "entity_id", "source"):
        with pytest.raises(ValidationError):
            setattr(observation, field_name, None)


def test_unknown_entity_rejected_with_entity_not_found_error() -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortEntityNotFoundError) as excinfo:
        create_execution_effort_observation(
            portfolio, uuid4(), uuid4(), 60, LATER_TIME
        )

    assert isinstance(excinfo.value, ExecutionEffortObservationError)


def test_invalid_observation_id_type_rejected() -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortObservationError, match="observation_id"):
        create_execution_effort_observation(
            portfolio, cast(UUID, str(uuid4())), portfolio.entities[0].id, 60, LATER_TIME
        )

    with pytest.raises(ExecutionEffortObservationError, match="observation_id"):
        create_execution_effort_observation(
            portfolio, cast(UUID, 42), portfolio.entities[0].id, 60, LATER_TIME
        )


def test_invalid_entity_id_type_rejected() -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortObservationError, match="entity_id"):
        create_execution_effort_observation(
            portfolio, uuid4(), cast(UUID, str(uuid4())), 60, LATER_TIME
        )


def test_non_portfolio_rejected() -> None:
    with pytest.raises(ExecutionEffortObservationError, match="portfolio"):
        create_execution_effort_observation(
            cast(Portfolio, object()), uuid4(), uuid4(), 60, LATER_TIME
        )


@pytest.mark.parametrize(
    "duration",
    [True, False],
    ids=["bool-true", "bool-false"],
)
def test_factory_rejects_bool_duration(duration: bool) -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortObservationError, match="duration_seconds"):
        create_execution_effort_observation(
            portfolio,
            uuid4(),
            portfolio.entities[0].id,
            duration,
            LATER_TIME,
        )


def test_factory_rejects_zero_duration() -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortObservationError, match="> 0"):
        create_execution_effort_observation(
            portfolio, uuid4(), portfolio.entities[0].id, 0, LATER_TIME
        )


def test_factory_rejects_negative_duration() -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortObservationError, match="> 0"):
        create_execution_effort_observation(
            portfolio, uuid4(), portfolio.entities[0].id, -5, LATER_TIME
        )


@pytest.mark.parametrize(
    "duration",
    [30.5, "90", None, [60]],
    ids=["float", "string", "none", "list"],
)
def test_factory_rejects_float_or_string_duration(duration: object) -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortObservationError, match="duration_seconds"):
        create_execution_effort_observation(
            portfolio, uuid4(), portfolio.entities[0].id, cast(int, duration), LATER_TIME
        )


def test_factory_rejects_non_datetime_observed_at() -> None:
    portfolio = make_portfolio(make_entity())

    with pytest.raises(ExecutionEffortObservationError, match="observed_at"):
        create_execution_effort_observation(
            portfolio,
            uuid4(),
            portfolio.entities[0].id,
            60,
            cast(datetime, "2026-08-25T12:00:00Z"),
        )


def test_factory_rejects_naive_observed_at() -> None:
    portfolio = make_portfolio(make_entity())
    naive = datetime(2026, 8, 25, 12, 0, 0)

    with pytest.raises(ExecutionEffortObservationError, match="timezone-aware"):
        create_execution_effort_observation(
            portfolio, uuid4(), portfolio.entities[0].id, 60, naive
        )


def test_backfilled_observed_at_earlier_than_updated_at_is_allowed() -> None:
    entity = make_entity(updated_at=LATER_TIME)
    portfolio = make_portfolio(entity)

    observation = create_execution_effort_observation(
        portfolio, uuid4(), entity.id, 60, EARLIER_TIME
    )

    assert observation.observed_at == EARLIER_TIME
    assert observation.observed_at < entity.updated_at


def test_portfolio_remains_deeply_unchanged() -> None:
    portfolio, entity, other = make_portfolio_with_two_entities()
    snapshot = portfolio.model_dump()
    entity_status = entity.status
    entity_updated_at = entity.updated_at
    other_status = other.status
    other_updated_at = other.updated_at

    create_execution_effort_observation(
        portfolio, uuid4(), entity.id, 60, LATER_TIME
    )

    assert portfolio.model_dump() == snapshot
    assert entity.status is entity_status
    assert entity.updated_at == entity_updated_at
    assert other.status is other_status
    assert other.updated_at == other_updated_at


def test_no_entity_status_or_updated_at_changes_across_entities() -> None:
    portfolio, entity, other = make_portfolio_with_two_entities()
    before = [(e.id, e.status, e.updated_at) for e in portfolio.entities]

    create_execution_effort_observation(
        portfolio, uuid4(), other.id, 45, LATER_TIME
    )

    after = [(e.id, e.status, e.updated_at) for e in portfolio.entities]
    assert after == before


# --- direct construction invariants (model-level, not factory-only) ---


def test_direct_construction_rejects_bool_duration() -> None:
    with pytest.raises(ValidationError):
        direct_model(duration_seconds=True, observed_at=LATER_TIME)

    with pytest.raises(ValidationError):
        direct_model(duration_seconds=False, observed_at=LATER_TIME)


def test_direct_construction_rejects_zero_or_negative_duration() -> None:
    with pytest.raises(ValidationError):
        direct_model(duration_seconds=0, observed_at=LATER_TIME)

    with pytest.raises(ValidationError):
        direct_model(duration_seconds=-1, observed_at=LATER_TIME)


@pytest.mark.parametrize(
    "duration",
    ["45", 45.0, None],
    ids=["string", "float", "none"],
)
def test_direct_construction_rejects_incompatible_duration_coercion(
    duration: object,
) -> None:
    with pytest.raises(ValidationError):
        direct_model(duration_seconds=duration, observed_at=LATER_TIME)


def test_direct_construction_rejects_naive_observed_at() -> None:
    naive = datetime(2026, 8, 25, 12, 0, 0)

    with pytest.raises(ValidationError, match="timezone-aware"):
        direct_model(duration_seconds=60, observed_at=naive)


def test_direct_construction_rejects_string_observed_at() -> None:
    with pytest.raises(ValidationError):
        direct_model(
            duration_seconds=60, observed_at="2026-08-25T12:00:00Z"
        )


def test_direct_construction_rejects_non_uuid_ids() -> None:
    with pytest.raises(ValidationError):
        ExecutionEffortObservation(
            id=cast(UUID, "not-a-uuid"),
            portfolio_id=uuid4(),
            entity_id=uuid4(),
            duration_seconds=60,
            observed_at=LATER_TIME,
            source=SourceKind.USER_CONFIRMED,
        )


def test_direct_construction_rejects_string_source_coercion() -> None:
    with pytest.raises(ValidationError):
        ExecutionEffortObservation(
            id=uuid4(),
            portfolio_id=uuid4(),
            entity_id=uuid4(),
            duration_seconds=60,
            observed_at=LATER_TIME,
            source=cast(SourceKind, "user_confirmed"),
        )


def test_direct_construction_requires_source() -> None:
    with pytest.raises(ValidationError):
        ExecutionEffortObservation.model_validate(
            {
                "id": uuid4(),
                "portfolio_id": uuid4(),
                "entity_id": uuid4(),
                "duration_seconds": 60,
                "observed_at": LATER_TIME,
            }
        )


def test_valid_direct_construction_is_frozen_and_valid() -> None:
    observation = direct_model(duration_seconds=75, observed_at=EARLIER_TIME)

    assert observation.duration_seconds == 75
    assert observation.observed_at == EARLIER_TIME

    with pytest.raises(ValidationError):
        observation.source = SourceKind.AI_INFERRED
