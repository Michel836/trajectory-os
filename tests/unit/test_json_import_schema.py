"""Tests for JSON import schema validation."""

from datetime import UTC, datetime

import pytest

from trajectory_os.importers.json_portfolio import (
    EntityDTO,
    ImportEnvelope,
    PortfolioHeader,
    RelationDTO,
)


def test_valid_complete_envelope():
    """Test that a valid complete envelope validates."""
    envelope_data = {
        "schema_version": 1,
        "source_namespace": "example-source",
        "portfolio": {
            "external_id": "portfolio-1",
            "name": "My Portfolio"
        },
        "entities": [
            {
                "external_id": "project-1",
                "entity_type": "project",
                "title": "My Project"
            }
        ],
        "relations": [
            {
                "external_id": "relation-1",
                "source_external_id": "project-1",
                "target_external_id": "task-1",
                "relation_type": "depends_on"
            }
        ]
    }

    envelope = ImportEnvelope(**envelope_data)
    assert envelope.schema_version == 1
    assert envelope.source_namespace == "example-source"
    assert envelope.portfolio.external_id == "portfolio-1"
    assert envelope.portfolio.name == "My Portfolio"
    assert len(envelope.entities) == 1
    assert len(envelope.relations) == 1


def test_valid_minimal_envelope():
    """Test that a valid minimal envelope validates."""
    envelope_data = {
        "schema_version": 1,
        "source_namespace": "example-source",
        "portfolio": {
            "external_id": "portfolio-1",
            "name": "My Portfolio"
        },
        "entities": [],
        "relations": []
    }

    envelope = ImportEnvelope(**envelope_data)
    assert envelope.schema_version == 1
    assert envelope.source_namespace == "example-source"
    assert envelope.portfolio.external_id == "portfolio-1"


def _envelope(schema_version: object) -> ImportEnvelope:
    return ImportEnvelope(
        schema_version=schema_version,
        source_namespace="test",
        portfolio={"external_id": "test", "name": "test"},
        entities=[],
        relations=[]
    )


def test_strict_schema_version():
    """Test that schema_version accepts only strict integer 1."""
    envelope = _envelope(1)
    assert envelope.schema_version == 1

    # Invalid cases - should all fail
    invalid_versions = [True, False, 1.0, "1", 0, 2]

    for version in invalid_versions:
        with pytest.raises(ValueError):
            _envelope(version)


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 0, 2])
def test_schema_version_rejected_values(version):
    """Explicitly reject non-strict schema_version values."""
    with pytest.raises(ValueError, match="schema_version must be integer 1"):
        _envelope(version)


@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, 0.5])
def test_confidence_entity_accepted_values(value):
    """Entity confidence accepts raw int and float values."""
    entity = EntityDTO(
        external_id="test",
        entity_type="project",
        title="Test",
        confidence=value
    )
    assert entity.confidence == value


@pytest.mark.parametrize("value", [True, False, "0", "1", "0.5", -0.1, 1.1])
def test_confidence_entity_rejected_values(value):
    """Entity confidence rejects bool, str, and out-of-range numbers."""
    with pytest.raises(ValueError):
        EntityDTO(
            external_id="test",
            entity_type="project",
            title="Test",
            confidence=value
        )


@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, 0.5])
def test_confidence_relation_accepted_values(value):
    """Relation confidence accepts raw int and float values."""
    relation = RelationDTO(
        external_id="relation-1",
        source_external_id="source-1",
        target_external_id="target-1",
        relation_type="depends_on",
        confidence=value
    )
    assert relation.confidence == value


@pytest.mark.parametrize("value", [True, False, "0", "1", "0.5", -0.1, 1.1])
def test_confidence_relation_rejected_values(value):
    """Relation confidence rejects bool, str, and out-of-range numbers."""
    with pytest.raises(ValueError):
        RelationDTO(
            external_id="relation-1",
            source_external_id="source-1",
            target_external_id="target-1",
            relation_type="depends_on",
            confidence=value
        )


def test_invalid_entity_type():
    """Test that invalid entity_types are rejected."""
    with pytest.raises(ValueError):
        EntityDTO(
            external_id="test",
            entity_type="invalid-type",
            title="Test"
        )


def test_invalid_status():
    """Test that invalid statuses are rejected."""
    with pytest.raises(ValueError):
        EntityDTO(
            external_id="test",
            entity_type="project",
            title="Test",
            status="invalid-status"
        )


def test_invalid_relation_type():
    """Test that invalid relation_types are rejected."""
    with pytest.raises(ValueError):
        RelationDTO(
            external_id="test",
            source_external_id="source-1",
            target_external_id="target-1",
            relation_type="invalid-type"
        )


def test_valid_confidence_values():
    """Test that valid confidence values work."""
    # Valid values
    valid_values = [0, 1, 0.5]

    for value in valid_values:
        entity = EntityDTO(
            external_id="test",
            entity_type="project",
            title="Test",
            confidence=value
        )
        assert entity.confidence == value


def _entity(**overrides: object) -> EntityDTO:
    data: dict[str, object] = {
        "external_id": "test",
        "entity_type": "project",
        "title": "Test",
    }
    data.update(overrides)
    return EntityDTO(**data)


@pytest.mark.parametrize(
    "value", ["2023-01-01T12:00:00Z", "2023-01-02T12:00:00+00:00", "2023-01-01T12:00:00"]
)
def test_iso_datetime_strings_accepted(value):
    """Test that ISO-8601 string representations are accepted."""
    entity = _entity(created_at=value, updated_at=value)
    assert entity.created_at is not None
    assert entity.updated_at is not None
    assert entity.created_at.isoformat().startswith("2023-01-")


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
@pytest.mark.parametrize("value", [0, 1234567890, 1.5, True, False])
def test_non_string_datetime_rejected(field, value):
    """Test that non-string (numeric, bool) datetime representations are rejected."""
    with pytest.raises(ValueError, match="datetime must be an ISO-8601 string"):
        _entity(**{field: value})


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_invalid_datetime_string_rejected(field):
    """Test that syntactically invalid ISO-8601 strings are rejected by Pydantic."""
    with pytest.raises(ValueError):
        _entity(**{field: "not-a-datetime"})


def test_iso_datetime_accepted():
    """Test that ISO-8601 strings parse to datetime values."""
    entity = EntityDTO(
        external_id="test",
        entity_type="project",
        title="Test",
        created_at="2023-01-01T12:00:00Z",
        updated_at="2023-01-02T12:00:00+00:00"
    )
    assert entity.created_at == datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert entity.updated_at == datetime(2023, 1, 2, 12, 0, 0, tzinfo=UTC)


def test_numeric_datetime_rejected():
    """Test that numeric datetime values are rejected."""
    with pytest.raises(ValueError, match="datetime must be an ISO-8601 string"):
        EntityDTO(
            external_id="test",
            entity_type="project",
            title="Test",
            created_at=1234567890  # Unix timestamp numeric values should not be valid
        )


def test_invalid_confidence_bool():
    """Test that boolean confidence values are rejected."""
    with pytest.raises(ValueError):
        EntityDTO(
            external_id="test",
            entity_type="project",
            title="Test",
            confidence=True
        )

    with pytest.raises(ValueError):
        RelationDTO(
            external_id="test",
            source_external_id="source-1",
            target_external_id="target-1",
            relation_type="depends_on",
            confidence=False
        )


def test_invalid_confidence_strings():
    """Test that string confidence values are rejected."""
    with pytest.raises(ValueError):
        EntityDTO(
            external_id="test",
            entity_type="project",
            title="Test",
            confidence="0.5"
        )

    with pytest.raises(ValueError):
        RelationDTO(
            external_id="test",
            source_external_id="source-1",
            target_external_id="target-1",
            relation_type="depends_on",
            confidence="1"
        )


def test_invalid_confidence_out_of_range():
    """Test that out-of-range confidence values are rejected."""
    with pytest.raises(ValueError):
        EntityDTO(
            external_id="test",
            entity_type="project",
            title="Test",
            confidence=-0.1
        )

    with pytest.raises(ValueError):
        RelationDTO(
            external_id="test",
            source_external_id="source-1",
            target_external_id="target-1",
            relation_type="depends_on",
            confidence=1.1
        )


def test_extra_envelope_fields_rejected():
    """Test that extra envelope fields are rejected."""
    with pytest.raises(ValueError):
        ImportEnvelope(
            schema_version=1,
            source_namespace="test",
            portfolio={"external_id": "test", "name": "test"},
            entities=[],
            relations=[],
            extra_field="should_fail"  # This should be rejected
        )


def test_extra_nested_fields_rejected():
    """Test that extra nested fields are rejected."""

    # Test with extra field in entity
    with pytest.raises(ValueError):
        EntityDTO(
            external_id="test",
            entity_type="project",
            title="Test",
            extra_field="should_fail"  # This should be rejected
        )

    # Test with extra field in relation
    with pytest.raises(ValueError):
        RelationDTO(
            external_id="test",
            source_external_id="source-1",
            target_external_id="target-1",
            relation_type="depends_on",
            extra_field="should_fail"  # This should be rejected
        )


def test_source_injection_rejected():
    """Test that source injection is rejected in entity and relation."""

    # Test entity source rejection
    with pytest.raises(ValueError):
        EntityDTO(
            external_id="test",
            entity_type="project",
            title="Test",
            source="user_confirmed"  # This should be rejected as it's not allowed
        )

    # Test relation source rejection
    with pytest.raises(ValueError):
        RelationDTO(
            external_id="test",
            source_external_id="source-1",
            target_external_id="target-1",
            relation_type="depends_on",
            source="user_confirmed"  # This should be rejected as it's not allowed
        )


def test_empty_source_namespace_rejected():
    """Test that empty source_namespace is rejected."""
    with pytest.raises(ValueError):
        ImportEnvelope(
            schema_version=1,
            source_namespace="",  # Empty string should fail
            portfolio={"external_id": "test", "name": "test"},
            entities=[],
            relations=[]
        )


def test_empty_external_ids_rejected():
    """Test that empty external IDs are rejected."""

    # Test empty entity external ID
    with pytest.raises(ValueError):
        EntityDTO(
            external_id="",  # Empty string should fail
            entity_type="project",
            title="Test"
        )

    # Test empty portfolio external ID
    with pytest.raises(ValueError):
        PortfolioHeader(
            external_id="",  # Empty string should fail
            name="test"
        )


def test_empty_title_name_rejected():
    """Test that empty title/name are rejected."""

    # Test empty entity title
    with pytest.raises(ValueError):
        EntityDTO(
            external_id="test",
            entity_type="project",
            title=""  # Empty string should fail
        )

    # Test empty portfolio name
    with pytest.raises(ValueError):
        PortfolioHeader(
            external_id="test",
            name=""  # Empty string should fail
        )
