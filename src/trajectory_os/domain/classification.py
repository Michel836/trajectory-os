"""Provider-agnostic audit model for AI classification proposals."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity

# Classifier output may only carry AI-derived provenance.
ProposalSource = Literal[SourceKind.AI_INFERRED, SourceKind.AI_RECOMMENDED]


class EntityClassificationProposal(BaseModel):
    """Auditable, proposal-only classification of a canonical entity.

    A proposal never mutates the canonical entity and never becomes
    ``USER_CONFIRMED`` provenance on its own.
    """

    entity_id: UUID
    proposed_entity_type: EntityType
    source: ProposalSource
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    classifier_id: Annotated[str, Field(min_length=1)]
    model_id: Annotated[str, Field(min_length=1)]
    rationale: str | None = None

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_must_be_numeric(cls, value: object) -> object:
        # Explicitly reject bool and non-numeric values (e.g. strings)
        # rather than relying on silent Pydantic coercion.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be an int or float in [0, 1]")
        return value


class EntityClassifier(Protocol):
    """Provider-agnostic, proposal-only entity classification boundary."""

    def classify(self, entity: TrajectoryEntity) -> EntityClassificationProposal:
        """Return a classification proposal without mutating the entity."""
        ...


def classify_entity(
    entity: TrajectoryEntity,
    classifier: EntityClassifier,
) -> EntityClassificationProposal:
    """Classify one canonical entity and return a validated proposal.

    Proposal-only: does not mutate the entity, touch a Portfolio, or
    perform persistence or provider-specific work. Classifier output is
    untrusted: even a valid-looking proposal instance is revalidated
    from its current field state before being returned.
    """
    # Defend against misbehaving classifiers mutating canonical state.
    proposal = classifier.classify(entity.model_copy(deep=True))
    if not isinstance(proposal, EntityClassificationProposal):
        raise TypeError("classifier must return an EntityClassificationProposal")
    # Revalidate the current state from serialized data; the returned
    # instance must not be trusted as-is (pydantic models are mutable).
    revalidated = EntityClassificationProposal.model_validate(proposal.model_dump())
    if revalidated.entity_id != entity.id:
        raise ValueError("proposal targets a different entity than the classified one")
    return revalidated
