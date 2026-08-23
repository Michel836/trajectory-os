"""Unit tests for the classify_entity use case."""

import inspect
import uuid

import pytest
from pydantic import ValidationError

from trajectory_os.domain.classification import (
    EntityClassificationProposal,
    classify_entity,
)
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity


class RecordingClassifier:
    """Deterministic fake classifier that records calls and proposals."""

    def __init__(self, proposed_entity_type: EntityType, entity_id: object) -> None:
        self.proposed_entity_type = proposed_entity_type
        self.entity_id = entity_id
        self.calls: list[TrajectoryEntity] = []

    def classify(self, entity: TrajectoryEntity) -> EntityClassificationProposal:
        self.calls.append(entity)
        return EntityClassificationProposal(
            entity_id=self.entity_id,
            proposed_entity_type=self.proposed_entity_type,
            source=SourceKind.AI_RECOMMENDED,
            confidence=0.4,
            classifier_id="fake-classifier",
            model_id="fake-model",
        )


def make_entity() -> TrajectoryEntity:
    return TrajectoryEntity(
        entity_type=EntityType.TASK,
        title="Example task",
        source=SourceKind.IMPORTED,
        confidence=0.9,
    )


def test_valid_proposal_is_returned_unchanged() -> None:
    entity = make_entity()
    classifier = RecordingClassifier(EntityType.PROGRAM, entity.id)

    proposal = classify_entity(entity, classifier)

    assert proposal.entity_id == entity.id
    assert proposal.proposed_entity_type is EntityType.PROGRAM
    assert proposal.source is SourceKind.AI_RECOMMENDED
    assert proposal.confidence == 0.4
    assert proposal.rationale is None


def test_classifier_is_called_exactly_once() -> None:
    entity = make_entity()
    classifier = RecordingClassifier(EntityType.PROGRAM, entity.id)

    classify_entity(entity, classifier)

    assert len(classifier.calls) == 1
    assert classifier.calls[0] == entity
    assert classifier.calls[0].id == entity.id


def test_different_proposed_entity_type_is_allowed() -> None:
    entity = make_entity()

    proposal = classify_entity(entity, RecordingClassifier(EntityType.DECISION, entity.id))

    assert proposal.proposed_entity_type is EntityType.DECISION
    assert entity.entity_type is EntityType.TASK


def test_original_entity_remains_semantically_unchanged() -> None:
    entity = make_entity()
    before = entity.model_dump()

    classify_entity(entity, RecordingClassifier(EntityType.PROGRAM, entity.id))

    assert entity.model_dump() == before
    assert entity.source is SourceKind.IMPORTED
    assert entity.confidence == 0.9


def test_imported_source_and_confidence_remain_unchanged() -> None:
    entity = make_entity()

    proposal = classify_entity(entity, RecordingClassifier(EntityType.AREA, entity.id))

    assert entity.source is SourceKind.IMPORTED
    assert entity.confidence == 0.9
    assert proposal.source is SourceKind.AI_RECOMMENDED
    assert proposal.confidence == 0.4


def test_mismatched_entity_id_is_rejected() -> None:
    entity = make_entity()
    classifier = RecordingClassifier(EntityType.PROGRAM, uuid.uuid4())

    with pytest.raises(ValueError, match="targets a different entity"):
        classify_entity(entity, classifier)


class MutatingClassifier:
    """Deliberately misbehaving fake: mutates the entity instance it receives."""

    def __init__(self) -> None:
        self.received: TrajectoryEntity | None = None
        self.received_initial: dict[str, object] | None = None

    def classify(self, entity: TrajectoryEntity) -> EntityClassificationProposal:
        self.received = entity
        self.received_initial = entity.model_dump()
        entity.title = "MUTATED"
        entity.source = SourceKind.AI_INFERRED
        entity.confidence = 0.0
        entity.description = "overwritten"
        return EntityClassificationProposal(
            entity_id=entity.id,
            proposed_entity_type=EntityType.PROGRAM,
            source=SourceKind.AI_INFERRED,
            confidence=0.5,
            classifier_id="fake-classifier",
            model_id="fake-model",
        )


def test_mutating_classifier_cannot_change_canonical_entity() -> None:
    entity = make_entity()
    before = entity.model_dump()
    classifier = MutatingClassifier()

    proposal = classify_entity(entity, classifier)

    assert entity.model_dump() == before
    assert entity.source is SourceKind.IMPORTED
    assert entity.confidence == 0.9
    assert entity.description is None
    assert entity.title == "Example task"
    assert proposal.entity_id == entity.id
    received = classifier.received
    initial = classifier.received_initial
    assert received is not None
    assert received is not entity
    assert initial == before


class CorruptingClassifier:
    """Deliberately untrusted fake: builds a valid proposal, then mutates it."""

    def __init__(self, entity_id: object) -> None:
        self.entity_id = entity_id

    def classify(self, entity: TrajectoryEntity) -> object:
        proposal = EntityClassificationProposal(
            entity_id=self.entity_id,
            proposed_entity_type=EntityType.PROGRAM,
            source=SourceKind.AI_INFERRED,
            confidence=0.5,
            classifier_id="fake-classifier",
            model_id="fake-model",
        )
        # Corrupt after construction, after its own validation already ran.
        proposal.confidence = 1.7
        proposal.source = SourceKind.IMPORTED
        return proposal


def test_corrupted_returned_proposal_is_revalidated_and_rejected() -> None:
    entity = make_entity()
    classifier = CorruptingClassifier(entity.id)

    with pytest.raises(ValidationError):
        classify_entity(entity, classifier)


def test_corrupted_confidence_alone_is_still_rejected() -> None:
    entity = make_entity()

    class CorruptingConfidenceOnlyClassifier:
        def classify(self, candidate: TrajectoryEntity) -> object:
            proposal = EntityClassificationProposal(
                entity_id=candidate.id,
                proposed_entity_type=EntityType.PROGRAM,
                source=SourceKind.AI_INFERRED,
                confidence=0.5,
                classifier_id="fake-classifier",
                model_id="fake-model",
            )
            proposal.confidence = 2.0
            return proposal

    with pytest.raises(ValidationError):
        classify_entity(entity, CorruptingConfidenceOnlyClassifier())


class MalformedClassifier:
    """Intentionally contract-breaking fake: returns a non-proposal object."""

    def classify(self, entity: TrajectoryEntity) -> object:
        return {"proposed_entity_type": "program"}


def test_malformed_classifier_output_is_rejected() -> None:
    entity = make_entity()

    with pytest.raises(TypeError, match="EntityClassificationProposal"):
        classify_entity(entity, MalformedClassifier())


def test_use_case_has_no_portfolio_or_persistence_surface() -> None:
    params = inspect.signature(classify_entity).parameters

    assert list(params) == ["entity", "classifier"]


def test_proposal_model_still_rejects_invalid_values() -> None:
    with pytest.raises(ValidationError):
        EntityClassificationProposal(
            entity_id="not-a-uuid",
            proposed_entity_type="program",
            source=SourceKind.AI_INFERRED,
            confidence=0.5,
            classifier_id="c",
            model_id="m",
        )
