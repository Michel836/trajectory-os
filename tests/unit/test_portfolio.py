from uuid import uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation


def test_create_valid_portfolio() -> None:
    project = TrajectoryEntity(
        entity_type=EntityType.PROJECT,
        title="TrajectoryOS V0",
    )

    task = TrajectoryEntity(
        entity_type=EntityType.TASK,
        title="Build canonical domain model",
    )

    relation = TrajectoryRelation(
        source_id=task.id,
        target_id=project.id,
        relation_type=RelationType.BELONGS_TO,
    )

    portfolio = Portfolio(
        name="TrajectoryOS",
        entities=[project, task],
        relations=[relation],
    )

    assert len(portfolio.entities) == 2
    assert len(portfolio.relations) == 1
    assert portfolio.get_entity(project.id) == project


def test_reject_unknown_relation_target() -> None:
    task = TrajectoryEntity(
        entity_type=EntityType.TASK,
        title="Example task",
    )

    relation = TrajectoryRelation(
        source_id=task.id,
        target_id=uuid4(),
        relation_type=RelationType.BELONGS_TO,
    )

    with pytest.raises(ValidationError, match="unknown target entity"):
        Portfolio(
            name="Invalid portfolio",
            entities=[task],
            relations=[relation],
        )


def test_reject_duplicate_entity_ids() -> None:
    entity = TrajectoryEntity(
        entity_type=EntityType.PROJECT,
        title="Project",
    )

    with pytest.raises(ValidationError, match="duplicate entity IDs"):
        Portfolio(
            name="Invalid portfolio",
            entities=[entity, entity],
        )


def test_reject_self_relation() -> None:
    entity = TrajectoryEntity(
        entity_type=EntityType.TASK,
        title="Example task",
    )

    relation = TrajectoryRelation(
        source_id=entity.id,
        target_id=entity.id,
        relation_type=RelationType.RELATED_TO,
    )

    with pytest.raises(ValidationError, match="self-relations"):
        Portfolio(
            name="Invalid portfolio",
            entities=[entity],
            relations=[relation],
        )


def _mixed_entities() -> list[TrajectoryEntity]:
    return [
        TrajectoryEntity(
            entity_type=EntityType.PROJECT,
            title="Build platform",
            status=EntityStatus.ACTIVE,
            source=SourceKind.USER_CONFIRMED,
        ),
        TrajectoryEntity(
            entity_type=EntityType.TASK,
            title="Write tests",
            status=EntityStatus.WAITING,
            source=SourceKind.IMPORTED,
        ),
        TrajectoryEntity(
            entity_type=EntityType.PROJECT,
            title="Draft strategy",
            status=EntityStatus.SOMEDAY,
            source=SourceKind.AI_RECOMMENDED,
        ),
        TrajectoryEntity(
            entity_type=EntityType.TASK,
            title="Review design",
            status=EntityStatus.ACTIVE,
            source=SourceKind.AI_INFERRED,
        ),
    ]


def test_filter_entities_no_filters_returns_all_in_order() -> None:
    entities = _mixed_entities()
    portfolio = Portfolio(name="Portfolio", entities=entities)

    result = portfolio.filter_entities()

    assert result == entities
    assert [entity.id for entity in result] == [entity.id for entity in entities]


def test_filter_entities_returns_new_list() -> None:
    entities = _mixed_entities()
    portfolio = Portfolio(name="Portfolio", entities=entities)

    result = portfolio.filter_entities()

    assert result is not portfolio.entities


def test_filter_entities_by_type() -> None:
    portfolio = Portfolio(name="Portfolio", entities=_mixed_entities())

    tasks = portfolio.filter_entities(entity_type=EntityType.TASK)

    assert [entity.title for entity in tasks] == ["Write tests", "Review design"]


def test_filter_entities_by_status() -> None:
    portfolio = Portfolio(name="Portfolio", entities=_mixed_entities())

    active = portfolio.filter_entities(status=EntityStatus.ACTIVE)

    assert [entity.title for entity in active] == ["Build platform", "Review design"]


def test_filter_entities_by_source() -> None:
    entities = _mixed_entities()
    portfolio = Portfolio(name="Portfolio", entities=entities)

    imported = portfolio.filter_entities(source=SourceKind.IMPORTED)

    assert [entity.title for entity in imported] == ["Write tests"]
    assert imported[0] is entities[1]


def test_filter_entities_combines_filters_with_and() -> None:
    portfolio = Portfolio(name="Portfolio", entities=_mixed_entities())

    result = portfolio.filter_entities(
        entity_type=EntityType.PROJECT,
        status=EntityStatus.SOMEDAY,
    )

    assert [entity.title for entity in result] == ["Draft strategy"]

    all_three = portfolio.filter_entities(
        entity_type=EntityType.TASK,
        status=EntityStatus.ACTIVE,
        source=SourceKind.AI_INFERRED,
    )

    assert [entity.title for entity in all_three] == ["Review design"]


def test_filter_entities_no_match_returns_empty_list() -> None:
    portfolio = Portfolio(name="Portfolio", entities=_mixed_entities())

    result = portfolio.filter_entities(entity_type=EntityType.ROUTINE)

    assert result == []
    assert result is not portfolio.entities


def test_filter_entities_does_not_mutate_portfolio() -> None:
    entities = _mixed_entities()
    portfolio = Portfolio(name="Portfolio", entities=entities)
    before = portfolio.model_dump()

    portfolio.filter_entities(entity_type=EntityType.PROJECT)
    portfolio.filter_entities(status=EntityStatus.ACTIVE)
    portfolio.filter_entities(source=SourceKind.IMPORTED)
    portfolio.filter_entities(
        entity_type=EntityType.TASK,
        status=EntityStatus.WAITING,
        source=SourceKind.AI_INFERRED,
    )

    assert portfolio.model_dump() == before
    assert [entity.title for entity in portfolio.entities] == [
        entity.title for entity in entities
    ]


def test_filter_entities_leaves_get_entity_unchanged() -> None:
    entities = _mixed_entities()
    portfolio = Portfolio(name="Portfolio", entities=entities)

    portfolio.filter_entities(entity_type=EntityType.PROJECT)

    assert portfolio.get_entity(entities[0].id) == entities[0]
    assert portfolio.get_entity(uuid4()) is None


def _relation_graph() -> tuple[
    list[TrajectoryEntity],
    list[TrajectoryRelation],
]:
    """Small directed graph: task b belongs to project a, a depends on b,
    a is related to idea c, b requires task d, c contributes to b.
    Task d has no outgoing relations."""

    a = TrajectoryEntity(entity_type=EntityType.PROJECT, title="Platform")
    b = TrajectoryEntity(entity_type=EntityType.TASK, title="Build feature")
    c = TrajectoryEntity(entity_type=EntityType.IDEA, title="Idea X")
    d = TrajectoryEntity(entity_type=EntityType.TASK, title="Write tests")

    r1 = TrajectoryRelation(
        source_id=b.id,
        target_id=a.id,
        relation_type=RelationType.BELONGS_TO,
    )
    r2 = TrajectoryRelation(
        source_id=a.id,
        target_id=b.id,
        relation_type=RelationType.DEPENDS_ON,
    )
    r3 = TrajectoryRelation(
        source_id=a.id,
        target_id=c.id,
        relation_type=RelationType.RELATED_TO,
    )
    r4 = TrajectoryRelation(
        source_id=b.id,
        target_id=d.id,
        relation_type=RelationType.REQUIRES,
    )
    r5 = TrajectoryRelation(
        source_id=c.id,
        target_id=b.id,
        relation_type=RelationType.CONTRIBUTES_TO,
    )

    relations = [r1, r2, r3, r4, r5]

    return [a, b, c, d], relations


def test_outgoing_relations_for_known_entity() -> None:
    entities, relations = _relation_graph()
    a = entities[0]
    portfolio = Portfolio(name="Graph", entities=entities, relations=relations)

    assert portfolio.outgoing_relations(a.id) == [relations[1], relations[2]]


def test_incoming_relations_for_known_entity() -> None:
    entities, relations = _relation_graph()
    b = entities[1]
    portfolio = Portfolio(name="Graph", entities=entities, relations=relations)

    assert portfolio.incoming_relations(b.id) == [relations[1], relations[4]]


def test_outgoing_relations_filter_by_relation_type() -> None:
    entities, relations = _relation_graph()
    a = entities[0]
    portfolio = Portfolio(name="Graph", entities=entities, relations=relations)

    assert (
        portfolio.outgoing_relations(a.id, relation_type=RelationType.DEPENDS_ON)
        == [relations[1]]
    )

    assert portfolio.outgoing_relations(a.id, relation_type=RelationType.BLOCKS) == []


def test_incoming_relations_filter_by_relation_type() -> None:
    entities, relations = _relation_graph()
    b = entities[1]
    portfolio = Portfolio(name="Graph", entities=entities, relations=relations)

    assert (
        portfolio.incoming_relations(
            b.id, relation_type=RelationType.CONTRIBUTES_TO
        )
        == [relations[4]]
    )


def test_relation_queries_preserve_relations_order() -> None:
    entities, relations = _relation_graph()
    b = entities[1]
    shuffled = list(reversed(relations))
    portfolio = Portfolio(name="Graph", entities=entities, relations=shuffled)

    # In reversed storage order b's incoming are stored as [r5, r2].
    assert portfolio.incoming_relations(b.id) == [relations[4], relations[1]]

    # Outgoing of a in reversed storage: [r3, r2].
    assert portfolio.outgoing_relations(entities[0].id) == [
        relations[2],
        relations[1],
    ]


def test_known_entity_without_matching_side_returns_empty() -> None:
    entities, relations = _relation_graph()
    d = entities[3]  # task d has only an incoming relation
    portfolio = Portfolio(name="Graph", entities=entities, relations=relations)

    assert portfolio.outgoing_relations(d.id) == []


def test_known_entity_with_relations_but_no_matching_type() -> None:
    entities, relations = _relation_graph()
    d = entities[3]  # d has incoming REQUIRES only
    portfolio = Portfolio(name="Graph", entities=entities, relations=relations)

    assert (
        portfolio.incoming_relations(d.id, relation_type=RelationType.BELONGS_TO)
        == []
    )


def test_outgoing_relations_raise_for_unknown_entity() -> None:
    entities, relations = _relation_graph()
    portfolio = Portfolio(name="Graph", entities=entities, relations=relations)

    with pytest.raises(ValueError, match="unknown entity"):
        portfolio.outgoing_relations(uuid4())


def test_incoming_relations_raise_for_unknown_entity() -> None:
    entities, relations = _relation_graph()
    portfolio = Portfolio(name="Graph", entities=entities, relations=relations)

    with pytest.raises(ValueError, match="unknown entity"):
        portfolio.incoming_relations(uuid4())


def test_relation_queries_do_not_mutate_portfolio() -> None:
    entities, relations = _relation_graph()
    portfolio = Portfolio(name="Graph", entities=entities, relations=relations)
    before = portfolio.model_dump()

    assert portfolio.outgoing_relations(entities[0].id) is not portfolio.relations
    assert portfolio.outgoing_relations(
        entities[0].id, relation_type=RelationType.RELATED_TO
    ) is not portfolio.relations
    assert (
        portfolio.incoming_relations(
            entities[1].id, relation_type=RelationType.BELONGS_TO
        )
        is not portfolio.relations
    )

    with pytest.raises(ValueError):
        portfolio.outgoing_relations(uuid4())

    assert portfolio.model_dump() == before


def test_relation_queries_leave_existing_api_unchanged() -> None:
    entities, relations = _relation_graph()
    portfolio = Portfolio(name="Graph", entities=entities, relations=relations)

    portfolio.outgoing_relations(entities[1].id)
    portfolio.incoming_relations(entities[1].id, relation_type=RelationType.DEPENDS_ON)

    assert portfolio.get_entity(entities[0].id) == entities[0]
    assert portfolio.get_entity(uuid4()) is None
    assert portfolio.filter_entities(entity_type=EntityType.TASK) == [
        entities[1],
        entities[3],
    ]
