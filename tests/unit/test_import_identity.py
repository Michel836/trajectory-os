from uuid import UUID

import pytest

from trajectory_os.importers.identity import canonicalize_import_id

# Golden UUIDs according to the contract
EXAMPLE_SOURCE_PORTFOLIO_1 = UUID("99dd6b49-9858-5b5b-b101-79bd1dc5cf7b")
EXAMPLE_SOURCE_ENTITY_PROJECT_1 = UUID("f4de144d-9fe1-5fe1-8ca7-38d8f1a29f39")
EXAMPLE_SOURCE_ENTITY_TASK_1 = UUID("9386cf3e-ab10-52a9-b2be-bc0b5eb4e078")
EXAMPLE_SOURCE_RELATION_1 = UUID("00294af9-7633-551a-ba6a-133bec8fa5f9")
EXACT_SOURCE_PORTFOLIO = UUID("ab590803-c81f-5563-9252-9fb96bb91de0")
EXACT_SOURCE_ENTITY = UUID("8abf8e95-69af-52b5-845a-7bf5be9e8c99")
EXACT_SOURCE_RELATION = UUID("da983887-8a66-5b20-86da-9402b2fe4aeb")


def test_same_inputs_same_uuid():
    """Test that same inputs produce same UUID"""
    result1 = canonicalize_import_id("portfolio", "example-source", "portfolio-1")
    result2 = canonicalize_import_id("portfolio", "example-source", "portfolio-1")

    assert result1 == result2
    assert result1 == EXAMPLE_SOURCE_PORTFOLIO_1


def test_different_kind_different_uuid():
    """Test that different kinds produce different UUIDs"""
    portfolio_uuid = canonicalize_import_id("portfolio", "example-source", "portfolio-1")
    entity_uuid = canonicalize_import_id("entity", "example-source", "project-1")

    assert portfolio_uuid != entity_uuid
    assert entity_uuid == EXAMPLE_SOURCE_ENTITY_PROJECT_1


def test_different_source_namespace_different_uuid():
    """Test that different source namespaces produce different UUIDs"""
    uuid1 = canonicalize_import_id("portfolio", "example-source", "portfolio-1")
    uuid2 = canonicalize_import_id("portfolio", "different-source", "portfolio-1")

    assert uuid1 != uuid2


def test_different_external_id_different_uuid():
    """Test that different external IDs produce different UUIDs"""
    uuid1 = canonicalize_import_id("portfolio", "example-source", "portfolio-1")
    uuid2 = canonicalize_import_id("portfolio", "example-source", "portfolio-2")

    assert uuid1 != uuid2


def test_golden_uuids():
    """Test all the golden UUIDs mentioned in the contract"""
    # Test portfolio
    result = canonicalize_import_id("portfolio", "example-source", "portfolio-1")
    assert result == EXAMPLE_SOURCE_PORTFOLIO_1

    # Test entity project
    result = canonicalize_import_id("entity", "example-source", "project-1")
    assert result == EXAMPLE_SOURCE_ENTITY_PROJECT_1

    # Test entity task
    result = canonicalize_import_id("entity", "example-source", "task-1")
    assert result == EXAMPLE_SOURCE_ENTITY_TASK_1

    # Test relation
    result = canonicalize_import_id("relation", "example-source", "relation-1")
    assert result == EXAMPLE_SOURCE_RELATION_1


def test_invalid_kind_rejected():
    """Test that invalid kinds are rejected explicitly"""
    with pytest.raises(ValueError, match="Invalid kind"):
        canonicalize_import_id("invalid-kind", "example-source", "some-id")


def test_all_valid_kinds():
    """Test that all valid kinds work correctly"""
    # Test portfolio
    result = canonicalize_import_id("portfolio", "example-source", "some-id")
    assert isinstance(result, UUID)

    # Test entity
    result = canonicalize_import_id("entity", "example-source", "some-id")
    assert isinstance(result, UUID)

    # Test relation
    result = canonicalize_import_id("relation", "example-source", "some-id")
    assert isinstance(result, UUID)


def test_golden_same_id_portfolio():
    """Golden UUID contract: source_namespace example-source, external_id same-id"""
    assert (
        canonicalize_import_id("portfolio", "example-source", "same-id")
        == EXACT_SOURCE_PORTFOLIO
    )


def test_golden_same_id_entity():
    """Golden UUID contract: source_namespace example-source, external_id same-id"""
    assert canonicalize_import_id("entity", "example-source", "same-id") == EXACT_SOURCE_ENTITY


def test_golden_same_id_relation():
    """Golden UUID contract: source_namespace example-source, external_id same-id"""
    assert canonicalize_import_id("relation", "example-source", "same-id") == EXACT_SOURCE_RELATION


def test_external_id_is_case_sensitive():
    """external_id must be treated as an opaque string: case must not be folded"""
    uuid_lower = canonicalize_import_id("entity", "example-source", "abc")
    uuid_upper = canonicalize_import_id("entity", "example-source", "ABC")

    assert uuid_lower != uuid_upper


def test_external_id_is_not_trimmed():
    """external_id must be treated as an opaque string: whitespace must not be trimmed"""
    uuid_stripped = canonicalize_import_id("entity", "example-source", "abc")
    uuid_padded = canonicalize_import_id("entity", "example-source", " abc")

    assert uuid_stripped != uuid_padded
