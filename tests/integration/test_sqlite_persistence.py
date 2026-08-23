"""Integration tests for SQLite persistence of canonical portfolios."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from trajectory_os.adapters.persistence.models import EntityRow, PortfolioRow, RelationRow
from trajectory_os.adapters.persistence.sqlite import SqlitePortfolioRepository
from trajectory_os.domain.entities import EntityStatus, EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

if TYPE_CHECKING:
    from sqlalchemy.sql.schema import Table


def _build_portfolio() -> Portfolio:
    """Build a non-trivial portfolio with explicit identity and values."""
    project = TrajectoryEntity(
        entity_type=EntityType.PROJECT,
        title="TrajectoryOS V0",
        description="Adaptive execution and decision-intelligence platform",
        status=EntityStatus.ACTIVE,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=datetime(2026, 8, 1, 9, 30, 15, 123456, tzinfo=UTC),
        updated_at=datetime(2026, 8, 15, 16, 0, 0, tzinfo=timezone(timedelta(hours=2, minutes=30))),
    )

    task = TrajectoryEntity(
        entity_type=EntityType.TASK,
        title="Build SQLite persistence",
        description=None,
        status=EntityStatus.WAITING,
        source=SourceKind.AI_INFERRED,
        confidence=0.75,
        created_at=datetime(2026, 7, 20, 8, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 20, 8, 0, 0, tzinfo=UTC),
    )

    relation = TrajectoryRelation(
        source_id=task.id,
        target_id=project.id,
        relation_type=RelationType.BELONGS_TO,
        source=SourceKind.AI_RECOMMENDED,
        confidence=0.42,
    )

    return Portfolio(
        id=uuid4(),
        name="TrajectoryOS",
        entities=[project, task],
        relations=[relation],
    )


def _count(engine: Engine, table: Table) -> int:
    """Count rows of a table on a fresh connection."""
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def test_round_trip_preserves_full_semantics(tmp_path: Path) -> None:
    database = tmp_path / "portfolio.db"
    portfolio = _build_portfolio()

    repository = SqlitePortfolioRepository(database)
    repository.save(portfolio)
    repository.close()
    # Discard the repository instance; a new one must restore state from disk.
    del repository

    fresh = SqlitePortfolioRepository(database)
    loaded = fresh.load(portfolio.id)
    assert loaded is not None
    assert loaded.id == portfolio.id
    assert loaded.name == portfolio.name

    # Compare entities and relations semantically by UUID, never by row order.
    loaded_entities = {entity.id: entity for entity in loaded.entities}
    original_entities = {entity.id: entity for entity in portfolio.entities}
    assert set(loaded_entities) == set(original_entities)
    for entity_id, expected in original_entities.items():
        assert loaded_entities[entity_id] == expected

    loaded_relations = {relation.id: relation for relation in loaded.relations}
    original_relations = {relation.id: relation for relation in portfolio.relations}
    assert set(loaded_relations) == set(original_relations)
    for relation_id, expected in original_relations.items():
        assert loaded_relations[relation_id] == expected

    # Relation endpoints remain valid and validated by the domain model.
    for relation in loaded.relations:
        assert loaded.get_entity(relation.source_id) is not None
        assert loaded.get_entity(relation.target_id) is not None

    fresh.close()


def test_load_unknown_portfolio_returns_none(tmp_path: Path) -> None:
    repository = SqlitePortfolioRepository(tmp_path / "portfolio.db")
    repository.save(_build_portfolio())

    assert repository.load(uuid4()) is None
    repository.close()


def test_repeated_save_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "portfolio.db"
    portfolio = _build_portfolio()

    first = SqlitePortfolioRepository(database)
    first.save(portfolio)
    first.save(portfolio)
    first.close()

    with SqlitePortfolioRepository(database) as second:
        assert _count(second.engine, PortfolioRow.__table__) == 1
        assert _count(second.engine, EntityRow.__table__) == len(portfolio.entities)
        assert _count(second.engine, RelationRow.__table__) == len(portfolio.relations)


def test_snapshot_replacement_removes_stale_rows(tmp_path: Path) -> None:
    database = tmp_path / "portfolio.db"
    portfolio = _build_portfolio()

    with SqlitePortfolioRepository(database) as repository:
        repository.save(portfolio)

        task = next(
            entity for entity in portfolio.entities if entity.entity_type == EntityType.TASK
        )
        shrunken = Portfolio(
            id=portfolio.id,
            name=portfolio.name,
            entities=[entity for entity in portfolio.entities if entity.id != task.id],
            relations=[],
        )
        repository.save(shrunken)

        loaded = repository.load(portfolio.id)
        assert loaded is not None
        assert not any(entity.id == task.id for entity in loaded.entities)
        assert loaded.relations == []

        assert _count(repository.engine, EntityRow.__table__) == 1
        assert _count(repository.engine, RelationRow.__table__) == 0


def test_unsaved_portfolio_is_not_loaded(tmp_path: Path) -> None:
    with SqlitePortfolioRepository(tmp_path / "portfolio.db") as repository:
        assert repository.load(uuid4()) is None


def test_foreign_keys_are_enabled(tmp_path: Path) -> None:
    with SqlitePortfolioRepository(tmp_path / "portfolio.db") as repository:
        assert repository.foreign_keys_enabled() is True

        # Enforcement must actually be active, not merely reported: an
        # entity row referencing a missing portfolio must be rejected.
        try:
            with repository.engine.begin() as connection:
                connection.execute(
                    insert(EntityRow).values(
                        id=str(uuid4()),
                        portfolio_id=str(uuid4()),
                        entity_type=EntityType.TASK.value,
                        title="Orphan",
                        status=EntityStatus.ACTIVE.value,
                        source=SourceKind.USER_CONFIRMED.value,
                        confidence=1.0,
                        created_at="2026-01-01T00:00:00+00:00",
                        updated_at="2026-01-01T00:00:00+00:00",
                    )
                )
        except IntegrityError:
            pass
        else:
            msg = "foreign key constraint was not enforced"
            raise AssertionError(msg)


def test_portfolio_name_update_is_persisted(tmp_path: Path) -> None:
    database = tmp_path / "portfolio.db"
    portfolio = _build_portfolio()

    with SqlitePortfolioRepository(database) as repository:
        repository.save(portfolio)

        renamed = Portfolio(
            id=portfolio.id,
            name="TrajectoryOS (renamed)",
            entities=portfolio.entities,
            relations=portfolio.relations,
        )
        repository.save(renamed)

        loaded = repository.load(portfolio.id)
        assert loaded is not None
        assert loaded.id == portfolio.id
        assert loaded.name == renamed.name
        assert loaded.name != portfolio.name

        assert _count(repository.engine, PortfolioRow.__table__) == 1
        assert _count(repository.engine, EntityRow.__table__) == len(renamed.entities)
        assert _count(repository.engine, RelationRow.__table__) == len(renamed.relations)


def test_empty_portfolio_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "portfolio.db"
    portfolio = Portfolio(id=uuid4(), name="Empty")
    assert portfolio.entities == []
    assert portfolio.relations == []

    repository = SqlitePortfolioRepository(database)
    repository.save(portfolio)
    repository.close()
    # Discard the repository instance; a new one must restore state from disk.
    del repository

    fresh = SqlitePortfolioRepository(database)
    loaded = fresh.load(portfolio.id)
    assert loaded is not None
    assert loaded.id == portfolio.id
    assert loaded.name == portfolio.name
    assert loaded.entities == []
    assert loaded.relations == []
    fresh.close()


def test_portfolio_identity_is_stable(tmp_path: Path) -> None:
    portfolio = _build_portfolio()
    assert isinstance(portfolio.id, UUID)
    assert portfolio.model_dump()["id"] == portfolio.id
