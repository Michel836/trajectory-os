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


def _ordering_uuid(tag: int) -> UUID:
    """Build a UUID whose lexical string order matches the ``tag`` order."""
    return UUID(f"6ba7b811-9dad-41d2-80b4-00000000000{tag:x}")


def _ordered_entity(tag: int) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=_ordering_uuid(tag),
        entity_type=EntityType.TASK,
        title=f"Order entity {tag:x}",
        description=None,
        status=EntityStatus.ACTIVE,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=datetime(2026, 8, 1, 9, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 2, 9, 0, 0, tzinfo=UTC),
    )


def _ordered_relation(tag: int, source_tag: int, target_tag: int) -> TrajectoryRelation:
    return TrajectoryRelation(
        id=_ordering_uuid(tag),
        source_id=_ordering_uuid(source_tag),
        target_id=_ordering_uuid(target_tag),
        relation_type=RelationType.BELONGS_TO,
        source=SourceKind.USER_CONFIRMED,
        confidence=0.5,
    )


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


def test_order_round_trip_is_preserved(tmp_path: Path) -> None:
    """Saved canonical list order must round-trip, never be re-derived from UUIDs."""
    # Canonical list order is deliberately the reverse of UUID lexical
    # order, for entities and relations alike.
    entities = [_ordered_entity(tag) for tag in (0xC, 0xB, 0xA)]
    assert [entity.id for entity in entities] != sorted(entity.id for entity in entities)

    relations = [
        _ordered_relation(0xE, 0xC, 0xA),
        _ordered_relation(0xD, 0xB, 0xC),
    ]
    assert [relation.id for relation in relations] != sorted(
        relation.id for relation in relations
    )

    original = Portfolio(
        id=_ordering_uuid(0x1),
        name="Order",
        entities=entities,
        relations=relations,
    )

    database = tmp_path / "portfolio.db"
    repository = SqlitePortfolioRepository(database)
    repository.save(original)
    repository.close()
    # Discard the repository instance; a new one must restore state from disk.
    del repository

    fresh = SqlitePortfolioRepository(database)
    loaded = fresh.load(original.id)
    assert loaded is not None
    assert [entity.id for entity in loaded.entities] == [
        entity.id for entity in original.entities
    ]
    assert [relation.id for relation in loaded.relations] == [
        relation.id for relation in original.relations
    ]
    # Order preservation must not hide a semantic regression.
    assert loaded.entities == original.entities
    assert loaded.relations == original.relations
    fresh.close()


def test_reordered_resave_replaces_persisted_order(tmp_path: Path) -> None:
    """Persisted positions are snapshot state, not stale insertion metadata."""
    entities = [_ordered_entity(tag) for tag in (0xC, 0xB, 0xA)]
    relation_da = _ordered_relation(0xD, 0xC, 0xA)
    relation_ec = _ordered_relation(0xE, 0xB, 0xC)
    portfolio_id = _ordering_uuid(0x1)

    original = Portfolio(
        id=portfolio_id,
        name="Order",
        entities=entities,
        relations=[relation_da, relation_ec],
    )
    # Same entities and relations, deliberately reordered lists.
    reordered = Portfolio(
        id=portfolio_id,
        name="Order",
        entities=list(reversed(original.entities)),
        relations=[relation_ec, relation_da],
    )
    assert [e.id for e in reordered.entities] != [e.id for e in original.entities]
    assert [r.id for r in reordered.relations] != [r.id for r in original.relations]

    database = tmp_path / "portfolio.db"

    first = SqlitePortfolioRepository(database)
    first.save(original)
    first.close()
    del first

    with SqlitePortfolioRepository(database) as second:
        loaded = second.load(portfolio_id)
        assert loaded is not None
        assert [e.id for e in loaded.entities] == [e.id for e in original.entities]
        assert [r.id for r in loaded.relations] == [r.id for r in original.relations]

    third = SqlitePortfolioRepository(database)
    third.save(reordered)
    third.close()
    del third

    with SqlitePortfolioRepository(database) as fourth:
        loaded = fourth.load(portfolio_id)

    assert loaded is not None
    assert [e.id for e in loaded.entities] == [e.id for e in reordered.entities]
    assert [r.id for r in loaded.relations] == [r.id for r in reordered.relations]
    assert loaded.entities == reordered.entities
    assert loaded.relations == reordered.relations


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
                        position=0,
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


def test_cross_portfolio_relation_is_rejected(tmp_path: Path) -> None:
    """The database itself must reject relations whose endpoints span portfolios.

    Both endpoint entities exist in the database; only the composite
    (portfolio_id, id) foreign key can reject this. The old standalone
    source_id/target_id foreign keys would have accepted the insert.
    """
    database = tmp_path / "portfolio.db"
    first = _build_portfolio()
    second = _build_portfolio()

    repository = SqlitePortfolioRepository(database)
    repository.save(first)
    repository.save(second)

    try:
        with repository.engine.begin() as connection:
            connection.execute(
                insert(RelationRow).values(
                    id=str(uuid4()),
                    portfolio_id=str(first.id),
                    source_id=str(first.entities[0].id),
                    target_id=str(second.entities[0].id),
                    position=0,
                    relation_type=RelationType.BELONGS_TO.value,
                    source=SourceKind.USER_CONFIRMED.value,
                    confidence=0.5,
                )
            )
    except IntegrityError:
        pass
    else:
        msg = "cross-portfolio relation was not rejected by the database"
        raise AssertionError(msg)
    finally:
        repository.close()


def test_portfolio_identity_is_stable(tmp_path: Path) -> None:
    portfolio = _build_portfolio()
    assert isinstance(portfolio.id, UUID)
    assert portfolio.model_dump()["id"] == portfolio.id
