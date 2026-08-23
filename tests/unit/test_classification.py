"""Unit tests for the entity classification proposal model."""

import uuid

import pytest
from pydantic import ValidationError

from trajectory_os.domain.classification import EntityClassificationProposal
from trajectory_os.domain.entities import EntityType, SourceKind

ENTITY_ID = uuid.uuid4()


def make(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "entity_id": ENTITY_ID,
        "proposed_entity_type": EntityType.PROGRAM,
        "source": SourceKind.AI_INFERRED,
        "confidence": 0.75,
        "classifier_id": "classifier-1",
        "model_id": "model-1",
    }
    base.update(overrides)
    return base


def test_valid_proposal_roundtrips() -> None:
    proposal = EntityClassificationProposal(**make())

    assert proposal.entity_id == ENTITY_ID
    assert proposal.proposed_entity_type is EntityType.PROGRAM
    assert proposal.source is SourceKind.AI_INFERRED
    assert proposal.confidence == 0.75
    assert proposal.classifier_id == "classifier-1"
    assert proposal.model_id == "model-1"
    assert proposal.rationale is None


def test_valid_ai_recommended_source() -> None:
    proposal = EntityClassificationProposal(**make(source=SourceKind.AI_RECOMMENDED))

    assert proposal.source is SourceKind.AI_RECOMMENDED


def test_rationale_is_optional_and_retained() -> None:
    proposal = EntityClassificationProposal(**make(rationale="matched goal keywords"))

    assert proposal.rationale == "matched goal keywords"


def test_confidence_accepts_boundaries_and_ints() -> None:
    for value in (0, 1, 0.0, 1.0, 0.5):
        proposal = EntityClassificationProposal(**make(confidence=value))
        assert proposal.confidence == float(value)


def test_confidence_rejects_out_of_range() -> None:
    for value in (-0.1, 1.01):
        with pytest.raises(ValidationError):
            EntityClassificationProposal(**make(confidence=value))


def test_confidence_rejects_bools() -> None:
    for value in (True, False):
        with pytest.raises(ValidationError):
            EntityClassificationProposal(**make(confidence=value))


def test_confidence_rejects_strings() -> None:
    for value in ("0.5", "1"):
        with pytest.raises(ValidationError):
            EntityClassificationProposal(**make(confidence=value))


def test_source_rejects_user_confirmed_and_imported() -> None:
    for value in (SourceKind.USER_CONFIRMED, SourceKind.IMPORTED):
        with pytest.raises(ValidationError):
            EntityClassificationProposal(**make(source=value))


def test_proposed_entity_type_uses_canonical_validation() -> None:
    with pytest.raises(ValidationError):
        EntityClassificationProposal(**make(proposed_entity_type="not_a_type"))

    # Canonical string values are accepted by the canonical enum.
    proposal = EntityClassificationProposal(**make(proposed_entity_type="decision"))
    assert proposal.proposed_entity_type is EntityType.DECISION


def test_invalid_entity_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EntityClassificationProposal(**make(entity_id="not-a-uuid"))


def test_classifier_id_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        EntityClassificationProposal(**make(classifier_id=""))


def test_model_id_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        EntityClassificationProposal(**make(model_id=""))
