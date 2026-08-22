"""Core domain entities for TrajectoryOS."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EntityType(StrEnum):
    """Supported TrajectoryOS entity types."""

    GOAL = "goal"
    AREA = "area"
    PROGRAM = "program"
    PROJECT = "project"
    DELIVERABLE = "deliverable"
    WORK_PACKAGE = "work_package"
    TASK = "task"
    IDEA = "idea"
    DECISION = "decision"
    RESEARCH = "research"
    ROUTINE = "routine"
    WAITING = "waiting"
    RESOURCE = "resource"


class EntityStatus(StrEnum):
    """Generic lifecycle states."""

    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    INCUBATOR = "incubator"
    SOMEDAY = "someday"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class SourceKind(StrEnum):
    """Origin of an entity or assertion."""

    USER_CONFIRMED = "user_confirmed"
    IMPORTED = "imported"
    AI_INFERRED = "ai_inferred"
    AI_RECOMMENDED = "ai_recommended"


class TrajectoryEntity(BaseModel):
    """Base representation shared by TrajectoryOS entities."""

    id: UUID = Field(default_factory=uuid4)

    entity_type: EntityType
    title: str = Field(min_length=1)

    description: str | None = None
    status: EntityStatus = EntityStatus.INCUBATOR

    source: SourceKind = SourceKind.USER_CONFIRMED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
