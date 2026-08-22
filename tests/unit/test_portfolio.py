from uuid import uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import EntityType, TrajectoryEntity
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
