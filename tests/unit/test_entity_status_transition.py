"""Unit tests for V1.7-A entity status transition."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.entity_status_transition import (
    EntityStatusTransitionError,
    SameStatusTransitionError,
    StaleChangedAtError,
    UnknownEntityError,
    transition_entity_status,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

BASE_TIME = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
LATER_TIME = BASE_TIME + timedelta(seconds=1)


def make_target_entity(status: EntityStatus = EntityStatus.ACTIVE) -> TrajectoryEntity:
    return TrajectoryEntity(
        entity_type=EntityType.PROJECT,
        title="Target",
        description="the entity under test",
        status=status,
        source=SourceKind.USER_CONFIRMED,
        confidence=0.9,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def make_unrelated_entity() -> TrajectoryEntity:
    return TrajectoryEntity(
        entity_type=EntityType.TASK,
        title="Other",
        description="an unrelated entity",
        status=EntityStatus.INCUBATOR,
        source=SourceKind.AI_INFERRED,
        confidence=0.5,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def make_portfolio() -> Portfolio:
    target = make_target_entity()
    other = make_unrelated_entity()
    relation = TrajectoryRelation(
        source_id=target.id,
        target_id=other.id,
        relation_type=RelationType.BELONGS_TO,
        source=SourceKind.USER_CONFIRMED,
        confidence=0.75,
    )
    return Portfolio(
        id=uuid4(),
        name="Portfolio",
        entities=[target, other],
        relations=[relation],
    )


def test_valid_active_to_completed_transition():
    portfolio = make_portfolio()
    target_id = portfolio.entities[0].id

    result = transition_entity_status(
        portfolio, target_id, EntityStatus.COMPLETED, LATER_TIME
    )

    assert result.entity_id == target_id
    assert result.previous_status == EntityStatus.ACTIVE
    assert result.new_status == EntityStatus.COMPLETED
    assert result.changed_at is LATER_TIME

    transitioned = result.portfolio
    assert transitioned is not portfolio
    new_target = transitioned.get_entity(target_id)
    assert new_target is not None
    assert new_target.status == EntityStatus.COMPLETED
    assert new_target.updated_at == LATER_TIME


def test_completed_to_active_transition_is_allowed():
    portfolio = make_portfolio()
    portfolio.entities[0].status = EntityStatus.COMPLETED
    target_id = portfolio.entities[0].id

    result = transition_entity_status(
        portfolio, target_id, EntityStatus.ACTIVE, LATER_TIME
    )

    assert result.previous_status == EntityStatus.COMPLETED
    assert result.new_status == EntityStatus.ACTIVE
    assert result.portfolio.get_entity(target_id).status == EntityStatus.ACTIVE


def test_changed_at_equal_to_updated_at_is_allowed():
    portfolio = make_portfolio()
    target = portfolio.entities[0]
    assert target.updated_at == BASE_TIME

    result = transition_entity_status(
        portfolio, target.id, EntityStatus.PAUSED, BASE_TIME
    )

    assert result.portfolio.get_entity(target.id).status == EntityStatus.PAUSED
    assert result.portfolio.get_entity(target.id).updated_at == BASE_TIME


def test_older_changed_at_rejected():
    portfolio = make_portfolio()
    stale = BASE_TIME - timedelta(seconds=1)

    with pytest.raises(StaleChangedAtError) as excinfo:
        transition_entity_status(
            portfolio, portfolio.entities[0].id, EntityStatus.COMPLETED, stale
        )

    assert isinstance(excinfo.value, EntityStatusTransitionError)


def test_naive_datetime_rejected():
    portfolio = make_portfolio()
    naive = datetime(2026, 8, 25, 12, 0, 0)

    with pytest.raises(EntityStatusTransitionError, match="timezone-aware"):
        transition_entity_status(
            portfolio, portfolio.entities[0].id, EntityStatus.COMPLETED, naive
        )


def test_invalid_entity_id_type_rejected():
    portfolio = make_portfolio()

    with pytest.raises(EntityStatusTransitionError, match="entity_id"):
        transition_entity_status(
            portfolio, str(uuid4()), EntityStatus.COMPLETED, LATER_TIME
        )


def test_invalid_target_status_type_rejected():
    portfolio = make_portfolio()

    with pytest.raises(EntityStatusTransitionError, match="target_status"):
        transition_entity_status(
            portfolio, portfolio.entities[0].id, "completed", LATER_TIME
        )


def test_unknown_entity_rejected():
    portfolio = make_portfolio()

    with pytest.raises(UnknownEntityError) as excinfo:
        transition_entity_status(
            portfolio, uuid4(), EntityStatus.COMPLETED, LATER_TIME
        )

    assert isinstance(excinfo.value, EntityStatusTransitionError)


def test_same_status_rejected():
    portfolio = make_portfolio()

    with pytest.raises(SameStatusTransitionError) as excinfo:
        transition_entity_status(
            portfolio, portfolio.entities[0].id, EntityStatus.ACTIVE, LATER_TIME
        )

    assert isinstance(excinfo.value, EntityStatusTransitionError)


def test_only_status_and_updated_at_change_on_target_entity():
    portfolio = make_portfolio()
    original = portfolio.entities[0]

    changed_keys: set[str] = set()
    invariant_keys: set[str] = set()

    result = transition_entity_status(
        portfolio, original.id, EntityStatus.COMPLETED, LATER_TIME
    )
    new_target = result.portfolio.get_entity(original.id)
    assert new_target is not None

    original_dump = original.model_dump()
    new_dump = new_target.model_dump()

    for key in original_dump:
        if original_dump[key] != new_dump[key]:
            changed_keys.add(key)
        else:
            invariant_keys.add(key)

    assert changed_keys == {"status", "updated_at"}
    assert "id" in invariant_keys
    assert new_target.entity_type is original.entity_type
    assert new_target.title == original.title
    assert new_target.description == original.description
    assert new_target.source is original.source
    assert new_target.confidence == original.confidence
    assert new_target.created_at == original.created_at


def test_source_portfolio_deeply_unchanged():
    portfolio = make_portfolio()
    snapshot = portfolio.model_dump()

    transition_entity_status(
        portfolio, portfolio.entities[0].id, EntityStatus.COMPLETED, LATER_TIME
    )

    assert portfolio.model_dump() == snapshot
    assert portfolio.entities[0].status == EntityStatus.ACTIVE
    assert portfolio.entities[0].updated_at == BASE_TIME


def test_returned_portfolio_is_a_different_object():
    portfolio = make_portfolio()

    result = transition_entity_status(
        portfolio, portfolio.entities[0].id, EntityStatus.COMPLETED, LATER_TIME
    )

    assert result.portfolio is not portfolio
    assert result.portfolio.model_dump() != portfolio.model_dump()


def test_returned_entities_are_independent_objects():
    portfolio = make_portfolio()
    source_target, source_other = portfolio.entities

    result = transition_entity_status(
        portfolio, source_target.id, EntityStatus.COMPLETED, LATER_TIME
    )
    new_target, new_other = result.portfolio.entities

    assert new_target is not source_target
    assert new_other is not source_other

    new_other.status = EntityStatus.CANCELLED
    assert source_other.status == EntityStatus.INCUBATOR


def test_returned_relations_are_independent_objects():
    portfolio = make_portfolio()
    source_relation = portfolio.relations[0]

    result = transition_entity_status(
        portfolio, portfolio.entities[0].id, EntityStatus.COMPLETED, LATER_TIME
    )
    new_relation = result.portfolio.relations[0]

    assert new_relation is not source_relation
    assert new_relation.model_dump() == source_relation.model_dump()

    new_relation.confidence = 0.0
    assert new_relation.id == source_relation.id
    assert source_relation.confidence == 0.75


def test_portfolio_id_and_name_preserved():
    portfolio = make_portfolio()

    result = transition_entity_status(
        portfolio, portfolio.entities[0].id, EntityStatus.COMPLETED, LATER_TIME
    )

    assert result.portfolio.id == portfolio.id
    assert result.portfolio.name == portfolio.name


def test_entity_ordering_preserved():
    portfolio = make_portfolio()

    result = transition_entity_status(
        portfolio, portfolio.entities[0].id, EntityStatus.COMPLETED, LATER_TIME
    )

    assert [e.id for e in result.portfolio.entities] == [
        e.id for e in portfolio.entities
    ]


def test_relation_ordering_and_values_preserved():
    portfolio = make_portfolio()

    result = transition_entity_status(
        portfolio, portfolio.entities[0].id, EntityStatus.COMPLETED, LATER_TIME
    )

    assert [r.id for r in result.portfolio.relations] == [
        r.id for r in portfolio.relations
    ]
    assert [r.model_dump() for r in result.portfolio.relations] == [
        r.model_dump() for r in portfolio.relations
    ]


def test_naive_entity_updated_at_raises_transition_error_not_typeerror():
    portfolio = make_portfolio()
    portfolio.entities[0].updated_at = BASE_TIME.replace(tzinfo=None)

    try:
        with pytest.raises(EntityStatusTransitionError):
            transition_entity_status(
                portfolio, portfolio.entities[0].id, EntityStatus.COMPLETED, LATER_TIME
            )
    except TypeError:
        pytest.fail(
            "naive updated_at leaked a TypeError instead of "
            "EntityStatusTransitionError"
        )


def test_deep_revalidation_rejects_invalid_unrelated_entity():
    portfolio = make_portfolio()
    target_id = portfolio.entities[0].id

    # Mutate an unrelated entity into an invalid field state (confidence > 1).
    portfolio.entities[1].confidence = 1.5

    with pytest.raises(EntityStatusTransitionError) as excinfo:
        transition_entity_status(
            portfolio, target_id, EntityStatus.COMPLETED, LATER_TIME
        )

    assert isinstance(excinfo.value, ValueError)
    assert not isinstance(excinfo.value, SameStatusTransitionError)
