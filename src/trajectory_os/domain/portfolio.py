"""Canonical portfolio model for TrajectoryOS."""

from __future__ import annotations

from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.relations import RelationType, TrajectoryRelation


class Portfolio(BaseModel):
    """Collection of entities and relations forming a coherent trajectory graph."""

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1)
    entities: list[TrajectoryEntity] = Field(default_factory=list)
    relations: list[TrajectoryRelation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        """Ensure identifiers and graph references are internally consistent."""

        entity_ids = [entity.id for entity in self.entities]

        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("portfolio contains duplicate entity IDs")

        relation_ids = [relation.id for relation in self.relations]

        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("portfolio contains duplicate relation IDs")

        known_entities = set(entity_ids)

        for relation in self.relations:
            if relation.source_id == relation.target_id:
                raise ValueError("self-relations are not allowed")

            if relation.source_id not in known_entities:
                raise ValueError(
                    f"relation references unknown source entity: {relation.source_id}"
                )

            if relation.target_id not in known_entities:
                raise ValueError(
                    f"relation references unknown target entity: {relation.target_id}"
                )

        return self

    def get_entity(self, entity_id: UUID) -> TrajectoryEntity | None:
        """Return an entity by identifier."""

        return next(
            (entity for entity in self.entities if entity.id == entity_id),
            None,
        )

    def filter_entities(
        self,
        *,
        entity_type: EntityType | None = None,
        status: EntityStatus | None = None,
        source: SourceKind | None = None,
    ) -> list[TrajectoryEntity]:
        """Return entities matching all supplied equality filters.

        Filters combine with logical AND and are evaluated against the
        canonical enum values. Order follows ``Portfolio.entities``. The
        portfolio is not mutated and a new list is always returned.
        """

        return [
            entity
            for entity in self.entities
            if (entity_type is None or entity.entity_type == entity_type)
            and (status is None or entity.status == status)
            and (source is None or entity.source == source)
        ]

    def _require_member(self, entity_id: UUID) -> None:
        """Raise ``ValueError`` when ``entity_id`` is not part of this portfolio."""

        if self.get_entity(entity_id) is None:
            raise ValueError(f"unknown entity in portfolio: {entity_id}")

    def outgoing_relations(
        self,
        entity_id: UUID,
        *,
        relation_type: RelationType | None = None,
    ) -> list[TrajectoryRelation]:
        """Return relations for which ``entity_id`` is the source.

        Relations are returned in ``Portfolio.relations`` order. The
        portfolio is not mutated and a new list is always returned.
        """

        self._require_member(entity_id)

        return [
            relation
            for relation in self.relations
            if relation.source_id == entity_id
            and (relation_type is None or relation.relation_type == relation_type)
        ]

    def incoming_relations(
        self,
        entity_id: UUID,
        *,
        relation_type: RelationType | None = None,
    ) -> list[TrajectoryRelation]:
        """Return relations for which ``entity_id`` is the target.

        Relations are returned in ``Portfolio.relations`` order. The
        portfolio is not mutated and a new list is always returned.
        """

        self._require_member(entity_id)

        return [
            relation
            for relation in self.relations
            if relation.target_id == entity_id
            and (relation_type is None or relation.relation_type == relation_type)
        ]
