"""JSON portfolio import schema validation."""

from __future__ import annotations

import json
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, field_validator

from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.importers.identity import canonicalize_import_id


class PortfolioHeader(BaseModel):
    """Portfolio header DTO."""

    external_id: str = Field(min_length=1)
    name: str = Field(min_length=1)

    model_config = {
        "extra": "forbid",
    }


class EntityDTO(BaseModel):
    """Entity DTO for import validation."""

    external_id: str = Field(min_length=1)
    entity_type: EntityType
    title: str = Field(min_length=1)
    description: str | None = None
    status: EntityStatus = EntityStatus.INCUBATOR
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {
        "extra": "forbid",
    }

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_confidence_type(cls, value: object) -> object:
        if type(value) not in (int, float):
            raise ValueError("confidence must be a JSON number")
        return value

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def validate_datetime_representation(cls, value: object) -> object:
        if value is not None and type(value) is not str:
            raise ValueError("datetime must be an ISO-8601 string")
        return value


class RelationDTO(BaseModel):
    """Relation DTO for import validation."""

    external_id: str = Field(min_length=1)
    source_external_id: str = Field(min_length=1)
    target_external_id: str = Field(min_length=1)
    relation_type: RelationType
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    model_config = {
        "extra": "forbid",
    }

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_confidence_type(cls, value: object) -> object:
        if type(value) not in (int, float):
            raise ValueError("confidence must be a JSON number")
        return value


class ImportEnvelope(BaseModel):
    """Import envelope DTO."""

    schema_version: int  # Will be validated by tests
    source_namespace: str = Field(min_length=1)
    portfolio: PortfolioHeader
    entities: list[EntityDTO] = Field(default_factory=list)
    relations: list[RelationDTO] = Field(default_factory=list)

    model_config = {
        "extra": "forbid",
    }

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value


# ---------------------------------------------------------------------------
# Deterministic mapping from a validated envelope to the canonical domain.
# ---------------------------------------------------------------------------


def _canonical_portfolio_id(envelope: ImportEnvelope) -> UUID:
    return canonicalize_import_id(
        kind="portfolio",
        source_namespace=envelope.source_namespace,
        external_id=envelope.portfolio.external_id,
    )


def _canonical_entity_id(envelope: ImportEnvelope, external_id: str) -> UUID:
    return canonicalize_import_id(
        kind="entity",
        source_namespace=envelope.source_namespace,
        external_id=external_id,
    )


def _canonical_relation_id(envelope: ImportEnvelope, external_id: str) -> UUID:
    return canonicalize_import_id(
        kind="relation",
        source_namespace=envelope.source_namespace,
        external_id=external_id,
    )


def _validate_batch(envelope: ImportEnvelope) -> dict[str, UUID]:
    """Validate intra-envelope invariants and return external_id -> canonical UUID."""
    seen_entity_ids: set[str] = set()
    for dto in envelope.entities:
        if dto.external_id in seen_entity_ids:
            raise ValueError(f"duplicate entity external_id: {dto.external_id}")
        seen_entity_ids.add(dto.external_id)

    seen_relation_ids: set[str] = set()
    for relation_dto in envelope.relations:
        if relation_dto.external_id in seen_relation_ids:
            raise ValueError(f"duplicate relation external_id: {relation_dto.external_id}")
        seen_relation_ids.add(relation_dto.external_id)

    entity_by_external_id: dict[str, UUID] = {
        dto.external_id: _canonical_entity_id(envelope, dto.external_id)
        for dto in envelope.entities
    }

    for relation_dto in envelope.relations:
        if relation_dto.source_external_id not in entity_by_external_id:
            raise ValueError(
                f"relation '{relation_dto.external_id}' references unknown source entity: "
                f"{relation_dto.source_external_id}"
            )
        if relation_dto.target_external_id not in entity_by_external_id:
            raise ValueError(
                f"relation '{relation_dto.external_id}' references unknown target entity: "
                f"{relation_dto.target_external_id}"
            )

    return entity_by_external_id


def _build_entity(envelope: ImportEnvelope, entity_dto: EntityDTO) -> TrajectoryEntity:
    """Construct a canonical entity, preserving explicit timestamps when present."""
    kwargs: dict[str, datetime] = {}
    if entity_dto.created_at is not None:
        kwargs["created_at"] = entity_dto.created_at
    if entity_dto.updated_at is not None:
        kwargs["updated_at"] = entity_dto.updated_at
    return TrajectoryEntity(
        id=_canonical_entity_id(envelope, entity_dto.external_id),
        entity_type=entity_dto.entity_type,
        title=entity_dto.title,
        description=entity_dto.description,
        status=entity_dto.status,
        source=SourceKind.IMPORTED,
        confidence=entity_dto.confidence,
        **kwargs,
    )


def map_envelope_to_portfolio(envelope: ImportEnvelope) -> Portfolio:
    """Map a validated ImportEnvelope to a canonical Portfolio.

    Deterministic, pure mapping layer: no file I/O, no persistence,
    no timestamp manufacture. Canonical domain validation (Portfolio
    model validator) remains the final authority.
    """
    entity_by_external_id = _validate_batch(envelope)

    entities = [_build_entity(envelope, entity_dto) for entity_dto in envelope.entities]

    relations: list[TrajectoryRelation] = []
    for relation_dto in envelope.relations:
        relations.append(
            TrajectoryRelation(
                id=_canonical_relation_id(envelope, relation_dto.external_id),
                source_id=entity_by_external_id[relation_dto.source_external_id],
                target_id=entity_by_external_id[relation_dto.target_external_id],
                relation_type=relation_dto.relation_type,
                source=SourceKind.IMPORTED,
                confidence=relation_dto.confidence,
            )
        )

    return Portfolio(
        id=_canonical_portfolio_id(envelope),
        name=envelope.portfolio.name,
        entities=entities,
        relations=relations,
    )


class PortfolioImportError(Exception):
    """Raised when importing a portfolio from a file fails at any boundary."""


def import_portfolio_file(path: str | Path) -> Portfolio:
    """Import a canonical Portfolio from a UTF-8 JSON file.

    Read errors, JSON parse errors, schema validation errors, batch/mapping
    validation errors, and canonical domain validation errors are all
    re-raised as PortfolioImportError with the original exception chained
    via __cause__. A failure never returns a partial Portfolio.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PortfolioImportError(f"unable to read portfolio import file: {exc!s}") from exc

    try:
        payload = json.loads(text)
    except JSONDecodeError as exc:
        raise PortfolioImportError(f"invalid JSON portfolio import: {exc}") from exc

    try:
        envelope = ImportEnvelope.model_validate(payload)
    except ValidationError as exc:
        raise PortfolioImportError(f"invalid portfolio import schema: {exc}") from exc

    try:
        return map_envelope_to_portfolio(envelope)
    except ValueError as exc:
        raise PortfolioImportError(f"invalid portfolio import mapping: {exc}") from exc
