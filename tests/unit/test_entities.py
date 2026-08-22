import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)


def test_create_project_entity() -> None:
    entity = TrajectoryEntity(
        entity_type=EntityType.PROJECT,
        title="Build TrajectoryOS V0",
        status=EntityStatus.ACTIVE,
    )

    assert entity.title == "Build TrajectoryOS V0"
    assert entity.entity_type is EntityType.PROJECT
    assert entity.status is EntityStatus.ACTIVE
    assert entity.source is SourceKind.USER
    assert entity.confidence == 1.0


def test_confidence_accepts_valid_value() -> None:
    entity = TrajectoryEntity(
        entity_type=EntityType.TASK,
        title="Example task",
        confidence=0.75,
    )

    assert entity.confidence == 0.75


def test_confidence_rejects_invalid_value() -> None:
    with pytest.raises(ValidationError):
        TrajectoryEntity(
            entity_type=EntityType.TASK,
            title="Invalid example",
            confidence=1.5,
        )
