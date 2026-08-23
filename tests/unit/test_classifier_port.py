"""Unit tests for the provider-agnostic EntityClassifier protocol."""

from trajectory_os.domain.classification import (
    EntityClassificationProposal,
    EntityClassifier,
)
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity


class FakeClassifier:
    """Deterministic fake satisfying the EntityClassifier protocol."""

    def __init__(self) -> None:
        self.calls: list[TrajectoryEntity] = []

    def classify(self, entity: TrajectoryEntity) -> EntityClassificationProposal:
        self.calls.append(entity)
        return EntityClassificationProposal(
            entity_id=entity.id,
            proposed_entity_type=EntityType.PROGRAM,
            source=SourceKind.AI_INFERRED,
            confidence=0.6,
            classifier_id="fake-classifier",
            model_id="fake-model",
            rationale="deterministic fake",
        )


def use_classifier(
    classifier: EntityClassifier,
    entity: TrajectoryEntity,
) -> EntityClassificationProposal:
    return classifier.classify(entity)


def test_fake_classifier_is_usable_through_protocol() -> None:
    fake = FakeClassifier()
    entity = TrajectoryEntity(entity_type=EntityType.TASK, title="Example task")

    proposal = use_classifier(fake, entity)

    assert proposal.entity_id == entity.id
    assert proposal.proposed_entity_type is EntityType.PROGRAM
    assert proposal.source is SourceKind.AI_INFERRED
    assert proposal.confidence == 0.6
    assert fake.calls == [entity]
