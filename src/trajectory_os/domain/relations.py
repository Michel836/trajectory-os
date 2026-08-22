"""Relations between TrajectoryOS domain entities."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from trajectory_os.domain.entities import SourceKind


class RelationType(StrEnum):
    """Supported relationships between TrajectoryOS entities."""

    BELONGS_TO = "belongs_to"
    DEPENDS_ON = "depends_on"
    BLOCKS = "blocks"
    REQUIRES = "requires"
    USES = "uses"
    WAITING_FOR = "waiting_for"
    CONTRIBUTES_TO = "contributes_to"
    PRODUCES = "produces"
    GENERATED_FROM = "generated_from"
    CAN_BATCH_WITH = "can_batch_with"
    PRECEDES = "precedes"
    RELATED_TO = "related_to"


class TrajectoryRelation(BaseModel):
    """Directed relation between two TrajectoryOS entities."""

    id: UUID = Field(default_factory=uuid4)

    source_id: UUID
    target_id: UUID
    relation_type: RelationType

    source: SourceKind = SourceKind.USER_CONFIRMED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
