from uuid import uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import SourceKind
from trajectory_os.domain.relations import (
    RelationType,
    TrajectoryRelation,
)


def test_create_dependency_relation() -> None:
    source_id = uuid4()
    target_id = uuid4()

    relation = TrajectoryRelation(
        source_id=source_id,
        target_id=target_id,
        relation_type=RelationType.DEPENDS_ON,
    )

    assert relation.source_id == source_id
    assert relation.target_id == target_id
    assert relation.relation_type is RelationType.DEPENDS_ON
    assert relation.source is SourceKind.USER_CONFIRMED
    assert relation.confidence == 1.0


def test_relation_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        TrajectoryRelation(
            source_id=uuid4(),
            target_id=uuid4(),
            relation_type=RelationType.RELATED_TO,
            confidence=-0.1,
        )
