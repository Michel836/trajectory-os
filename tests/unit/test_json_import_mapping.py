"""Tests for deterministic mapping from validated DTOs to the canonical domain."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from trajectory_os.domain.entities import SourceKind
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.importers.identity import canonicalize_import_id
from trajectory_os.importers.json_portfolio import (
    ImportEnvelope,
    map_envelope_to_portfolio,
)


def _envelope(
    *, entities: list[dict] | None = None, relations: list[dict] | None = None
) -> ImportEnvelope:
    return ImportEnvelope(
        schema_version=1,
        source_namespace="acme",
        portfolio={"external_id": "portfolio-1", "name": "Acme Portfolio"},
        entities=entities or [],
        relations=relations or [],
    )


def _project_task_envelope() -> ImportEnvelope:
    return _envelope(
        entities=[
            {
                "external_id": "project-1",
                "entity_type": "project",
                "title": "Project One",
                "description": "A project",
                "status": "active",
                "confidence": 0.9,
            },
            {
                "external_id": "task-1",
                "entity_type": "task",
                "title": "Task One",
                "confidence": 1,
            },
        ],
        relations=[
            {
                "external_id": "rel-1",
                "source_external_id": "task-1",
                "target_external_id": "project-1",
                "relation_type": "belongs_to",
                "confidence": 0.5,
            }
        ],
    )


def test_valid_project_task_relation_maps_to_portfolio():
    envelope = _project_task_envelope()
    portfolio = map_envelope_to_portfolio(envelope)

    assert isinstance(portfolio, Portfolio)
    assert portfolio.name == "Acme Portfolio"
    assert len(portfolio.entities) == 2
    assert len(portfolio.relations) == 1

    project = portfolio.get_entity(canonicalize_import_id("entity", "acme", "project-1"))
    task = portfolio.get_entity(canonicalize_import_id("entity", "acme", "task-1"))
    assert project is not None and task is not None

    relation = portfolio.relations[0]
    assert relation.source_id == task.id
    assert relation.target_id == project.id


def test_portfolio_uuid_is_deterministic():
    portfolio = map_envelope_to_portfolio(_project_task_envelope())
    expected = canonicalize_import_id("portfolio", "acme", "portfolio-1")
    assert portfolio.id == expected


def test_entity_uuids_are_deterministic():
    portfolio = map_envelope_to_portfolio(_project_task_envelope())
    for external_id, entity in [
        ("project-1", portfolio.entities[0]),
        ("task-1", portfolio.entities[1]),
    ]:
        assert entity.id == canonicalize_import_id("entity", "acme", external_id)


def test_relation_uuid_is_deterministic():
    portfolio = map_envelope_to_portfolio(_project_task_envelope())
    assert portfolio.relations[0].id == canonicalize_import_id("relation", "acme", "rel-1")


def test_remapping_preserves_ids_and_topology():
    first = map_envelope_to_portfolio(_project_task_envelope())
    second = map_envelope_to_portfolio(_project_task_envelope())

    assert first.id == second.id
    assert [e.id for e in first.entities] == [e.id for e in second.entities]
    assert [r.id for r in first.relations] == [r.id for r in second.relations]
    assert first.relations[0].source_id == second.relations[0].source_id
    assert first.relations[0].target_id == second.relations[0].target_id


def test_entities_are_imported():
    portfolio = map_envelope_to_portfolio(_project_task_envelope())
    assert all(entity.source == SourceKind.IMPORTED for entity in portfolio.entities)


def test_relations_are_imported():
    portfolio = map_envelope_to_portfolio(_project_task_envelope())
    assert all(relation.source == SourceKind.IMPORTED for relation in portfolio.relations)


def test_all_mapped_fields_are_preserved():
    envelope = _project_task_envelope()
    project_dto = next(e for e in envelope.entities if e.external_id == "project-1")
    relation_dto = envelope.relations[0]

    portfolio = map_envelope_to_portfolio(envelope)
    project = next(e for e in portfolio.entities if e.title == "Project One")

    assert project.entity_type == project_dto.entity_type
    assert project.title == project_dto.title
    assert project.description == project_dto.description
    assert project.status == project_dto.status
    assert project.confidence == project_dto.confidence
    assert project.source == SourceKind.IMPORTED

    relation = portfolio.relations[0]
    assert relation.relation_type == relation_dto.relation_type
    assert relation.confidence == relation_dto.confidence
    assert relation.source == SourceKind.IMPORTED


def test_explicit_timestamp_timezone_offsets_preserved():
    offset = timezone(timedelta(hours=5))
    stamp = datetime(2023, 1, 1, 12, 0, 0, tzinfo=offset)
    envelope = _envelope(
        entities=[
            {
                "external_id": "p1",
                "entity_type": "project",
                "title": "P1",
                "created_at": "2023-01-01T12:00:00+05:00",
                "updated_at": "2023-01-02T00:00:00Z",
            }
        ]
    )
    portfolio = map_envelope_to_portfolio(envelope)
    entity = portfolio.entities[0]
    assert entity.created_at == stamp
    assert entity.created_at.utcoffset() == timedelta(hours=5)
    assert entity.updated_at == datetime(2023, 1, 2, tzinfo=UTC)


def test_omitted_timestamps_use_canonical_defaults():
    portfolio = map_envelope_to_portfolio(
        _envelope(entities=[{"external_id": "p1", "entity_type": "project", "title": "P1"}])
    )
    entity = portfolio.entities[0]
    assert entity.created_at.tzinfo is not None
    assert entity.updated_at.tzinfo is not None


def test_duplicate_entity_external_id_rejected():
    envelope = _envelope(
        entities=[
            {"external_id": "dup", "entity_type": "project", "title": "A"},
            {"external_id": "dup", "entity_type": "task", "title": "B"},
        ]
    )
    with pytest.raises(ValueError, match="duplicate entity external_id: dup"):
        map_envelope_to_portfolio(envelope)


def test_duplicate_relation_external_id_rejected():
    envelope = _envelope(
        entities=[
            {"external_id": "a", "entity_type": "project", "title": "A"},
            {"external_id": "b", "entity_type": "task", "title": "B"},
        ],
        relations=[
            {
                "external_id": "dup",
                "source_external_id": "a",
                "target_external_id": "b",
                "relation_type": "depends_on",
            },
            {
                "external_id": "dup",
                "source_external_id": "b",
                "target_external_id": "a",
                "relation_type": "blocks",
            },
        ],
    )
    with pytest.raises(ValueError, match="duplicate relation external_id: dup"):
        map_envelope_to_portfolio(envelope)


def test_unknown_source_endpoint_rejected():
    envelope = _envelope(
        entities=[
            {"external_id": "a", "entity_type": "project", "title": "A"},
            {"external_id": "b", "entity_type": "task", "title": "B"},
        ],
        relations=[
            {
                "external_id": "rel-1",
                "source_external_id": "ghost",
                "target_external_id": "b",
                "relation_type": "depends_on",
            }
        ],
    )
    with pytest.raises(ValueError, match="unknown source entity: ghost"):
        map_envelope_to_portfolio(envelope)


def test_unknown_target_endpoint_rejected():
    envelope = _envelope(
        entities=[
            {"external_id": "a", "entity_type": "project", "title": "A"},
            {"external_id": "b", "entity_type": "task", "title": "B"},
        ],
        relations=[
            {
                "external_id": "rel-1",
                "source_external_id": "a",
                "target_external_id": "ghost",
                "relation_type": "depends_on",
            }
        ],
    )
    with pytest.raises(ValueError, match="unknown target entity: ghost"):
        map_envelope_to_portfolio(envelope)


def test_identical_external_id_text_entity_vs_relation_different_uuids():
    envelope = _envelope(
        entities=[
            {"external_id": "same", "entity_type": "project", "title": "A"},
            {"external_id": "other", "entity_type": "task", "title": "B"},
        ],
        relations=[
            {
                "external_id": "same",
                "source_external_id": "other",
                "target_external_id": "same",
                "relation_type": "blocks",
            }
        ],
    )
    entity_uuid = canonicalize_import_id("entity", "acme", "same")
    relation_uuid = canonicalize_import_id("relation", "acme", "same")
    assert entity_uuid != relation_uuid

    portfolio = map_envelope_to_portfolio(envelope)
    entity_ids = {e.id for e in portfolio.entities}
    assert entity_uuid in entity_ids
    assert relation_uuid in {r.id for r in portfolio.relations}
    assert entity_uuid not in {r.id for r in portfolio.relations}
    assert relation_uuid not in entity_ids


def test_self_relation_reached_canonical_validation():
    envelope = _envelope(
        entities=[
            {"external_id": "solo", "entity_type": "project", "title": "Solo"},
        ],
        relations=[
            {
                "external_id": "rel-1",
                "source_external_id": "solo",
                "target_external_id": "solo",
                "relation_type": "depends_on",
            }
        ],
    )
    with pytest.raises(ValueError, match="self-relations are not allowed"):
        map_envelope_to_portfolio(envelope)
