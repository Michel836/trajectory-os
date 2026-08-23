"""End-to-end integration: JSON file -> public importer -> SQLite -> fresh repository.

Composes existing production components only:
``import_portfolio_file`` (V0.3-D) and ``SqlitePortfolioRepository``
(V0.3-C). No SQL row manipulation, no duplicated mapper logic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from trajectory_os.adapters.persistence.sqlite import SqlitePortfolioRepository
from trajectory_os.domain.entities import SourceKind
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.importers import import_portfolio_file
from trajectory_os.importers.identity import canonicalize_import_id

NAMESPACE = "acme"


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_namespace": NAMESPACE,
        "portfolio": {"external_id": "portfolio-e2e", "name": "E2E Portfolio"},
        "entities": [
            {
                "external_id": "project-e2e",
                "entity_type": "project",
                "title": "TrajectoryOS V0",
                "description": "Adaptive decision-intelligence platform",
                "status": "active",
                "confidence": 0.8,
                "created_at": "2026-01-15T09:30:15.123456+05:30",
                "updated_at": "2026-02-20T18:00:00Z",
            },
            {
                "external_id": "task-e2e",
                "entity_type": "task",
                "title": "Build V0.3 evidence",
                "description": "Integration evidence only",
                "status": "waiting",
                "confidence": 0.4,
                "created_at": "2026-02-01T00:00:00-08:00",
                "updated_at": "2026-02-02T00:00:00Z",
            },
        ],
        "relations": [
            {
                "external_id": "rel-e2e",
                "source_external_id": "task-e2e",
                "target_external_id": "project-e2e",
                "relation_type": "belongs_to",
                "confidence": 0.6,
            }
        ],
    }


def _project_uuid() -> object:
    return canonicalize_import_id("entity", NAMESPACE, "project-e2e")


def _task_uuid() -> object:
    return canonicalize_import_id("entity", NAMESPACE, "task-e2e")


def _relation_uuid() -> object:
    return canonicalize_import_id("relation", NAMESPACE, "rel-e2e")


def _portfolio_uuid() -> object:
    return canonicalize_import_id("portfolio", NAMESPACE, "portfolio-e2e")


def _entities_by_id(portfolio: Portfolio) -> dict:
    return {entity.id: entity for entity in portfolio.entities}


def _relations_by_id(portfolio: Portfolio) -> dict:
    return {relation.id: relation for relation in portfolio.relations}


def _assert_entity_semantics(expected, actual) -> None:
    assert actual.id == expected.id
    assert actual.entity_type == expected.entity_type
    assert actual.title == expected.title
    assert actual.description == expected.description
    assert actual.status == expected.status
    assert actual.source == expected.source
    assert actual.confidence == expected.confidence
    assert actual.created_at == expected.created_at
    assert actual.updated_at == expected.updated_at
    assert actual.created_at.utcoffset() == expected.created_at.utcoffset()
    assert actual.updated_at.utcoffset() == expected.updated_at.utcoffset()


def _assert_relation_semantics(expected, actual) -> None:
    assert actual.id == expected.id
    assert actual.source_id == expected.source_id
    assert actual.target_id == expected.target_id
    assert actual.relation_type == expected.relation_type
    assert actual.source == expected.source
    assert actual.confidence == expected.confidence


def _write_json_file(directory: Path) -> Path:
    import_path = directory / "portfolio.json"
    import_path.write_text(json.dumps(_payload()), encoding="utf-8")
    return import_path


def test_json_import_produces_deterministic_canonical_portfolio(tmp_path: Path):
    """Steps 1-6: file -> public importer -> canonical, deterministic Portfolio."""
    import_path = _write_json_file(tmp_path)
    portfolio = import_portfolio_file(import_path)

    # Deterministic identifiers.
    assert portfolio.id == _portfolio_uuid()
    assert {e.id for e in portfolio.entities} == {_project_uuid(), _task_uuid()}
    assert {r.id for r in portfolio.relations} == {_relation_uuid()}

    # Provenance and preserved field values.
    for entity in portfolio.entities:
        assert entity.source == SourceKind.IMPORTED
    assert all(r.source == SourceKind.IMPORTED for r in portfolio.relations)

    project = next(e for e in portfolio.entities if e.id == _project_uuid())
    task = next(e for e in portfolio.entities if e.id == _task_uuid())
    assert project.status.value == "active"
    assert project.confidence == 0.8
    assert project.description == "Adaptive decision-intelligence platform"
    assert task.status.value == "waiting"
    assert task.confidence == 0.4
    assert task.description == "Integration evidence only"

    # Explicit timestamps, including a non-UTC offset.
    tz_offset = timezone(timedelta(hours=5, minutes=30))
    assert project.created_at == datetime(2026, 1, 15, 9, 30, 15, 123456, tzinfo=tz_offset)
    assert project.created_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert project.updated_at == datetime(2026, 2, 20, 18, 0, 0, tzinfo=UTC)
    assert task.created_at == datetime(2026, 2, 1, tzinfo=timezone(timedelta(hours=-8)))

    # Relation endpoints resolve to the mapped entities.
    relation = next(r for r in portfolio.relations if r.id == _relation_uuid())
    assert relation.source_id == _task_uuid()
    assert relation.target_id == _project_uuid()
    assert relation.confidence == 0.6
    assert relation.relation_type.value == "belongs_to"
    known_ids = {e.id for e in portfolio.entities}
    assert relation.source_id in known_ids
    assert relation.target_id in known_ids


def test_reimport_is_deterministic(tmp_path: Path):
    """Step 5-6: re-importing the same file preserves IDs and topology."""
    import_path = _write_json_file(tmp_path)
    first = import_portfolio_file(import_path)
    second = import_portfolio_file(str(import_path))

    assert first.id == second.id
    assert second.id == _portfolio_uuid()
    assert {e.id for e in first.entities} == {e.id for e in second.entities}
    assert {r.id for r in first.relations} == {r.id for r in second.relations}
    for relation in second.relations:
        assert relation.source_id == _task_uuid()
        assert relation.target_id == _project_uuid()
    assert (
        {(r.source_id, r.target_id, r.relation_type) for r in first.relations}
        == {(r.source_id, r.target_id, r.relation_type) for r in second.relations}
    )


def test_import_persist_reload_roundtrip(tmp_path: Path):
    """Steps 7-14: persist, close, fresh repository, load, compare, re-save."""
    import_path = _write_json_file(tmp_path)
    database_path = tmp_path / "trajectories.sqlite3"

    imported = import_portfolio_file(import_path)
    reimported = import_portfolio_file(import_path)

    # Steps 7-9: persist into a real SQLite file, then drop the instance.
    with SqlitePortfolioRepository(database_path) as repository:
        repository.save(imported)

    # Steps 10-12: fresh repository against the same file.
    with SqlitePortfolioRepository(database_path) as fresh:
        loaded = fresh.load(imported.id)

        assert loaded is not None
        assert loaded.id == imported.id
        assert loaded.id == _portfolio_uuid()
        assert loaded.name == imported.name

        loaded_entities = _entities_by_id(loaded)
        imported_entities = _entities_by_id(imported)
        assert set(loaded_entities) == set(imported_entities)
        assert len(loaded_entities) == 2  # no duplicated semantic entities

        for entity_id, expected in imported_entities.items():
            _assert_entity_semantics(expected, loaded_entities[entity_id])

        loaded_relations = _relations_by_id(loaded)
        imported_relations = _relations_by_id(imported)
        assert set(loaded_relations) == set(imported_relations)
        assert len(loaded_relations) == 1  # no duplicated semantic relations

        for relation_id, expected in imported_relations.items():
            _assert_relation_semantics(expected, loaded_relations[relation_id])

    # Steps 13-14: re-save the re-imported portfolio, reload via a fresh
    # repository, verify no duplicates and unchanged identifiers/topology.
    with SqlitePortfolioRepository(database_path) as saver:
        saver.save(reimported)

    with SqlitePortfolioRepository(database_path) as final:
        final_loaded = final.load(imported.id)

        assert final_loaded is not None
        assert final_loaded.id == imported.id
        assert final_loaded.name == imported.name

        final_entities = _entities_by_id(final_loaded)
        assert set(final_entities) == set(imported_entities)
        assert len(final_entities) == 2
        assert len({e.id for e in final_loaded.entities}) == len(final_loaded.entities)
        for entity_id, expected in imported_entities.items():
            _assert_entity_semantics(expected, final_entities[entity_id])

        final_relations = _relations_by_id(final_loaded)
        assert set(final_relations) == set(imported_relations)
        assert len(final_relations) == 1
        assert len({r.id for r in final_loaded.relations}) == len(final_loaded.relations)
        for relation_id, expected in imported_relations.items():
            _assert_relation_semantics(expected, final_relations[relation_id])
