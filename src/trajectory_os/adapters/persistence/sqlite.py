"""SQLite persistence adapter for canonical portfolios.

``SqlitePortfolioRepository`` saves and loads complete ``Portfolio``
snapshots against a SQLite file using explicit row-to-domain mappings.

``save()`` has atomic snapshot semantics: within a single transaction it
upserts the portfolio row, deletes the portfolio's existing relations and
entities, then inserts the current entities and relations. Re-saving the
same portfolio is idempotent, and portfolio entities or relations that were
removed from the domain object are removed from the database on the next
save.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence.models import (
    Base,
    EntityRow,
    PortfolioRow,
    RelationRow,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation


def _to_text(value: UUID) -> str:
    return str(value)


def _to_datetime_text(value: datetime) -> str:
    """Store a datetime as ISO-8601 text, preserving its UTC offset when present."""
    return value.isoformat()


def _to_domain_datetime(value: str) -> datetime:
    """Reconstruct a datetime from stored ISO-8601 text."""
    return datetime.fromisoformat(value)


def _entity_to_row(portfolio_id: UUID, entity: TrajectoryEntity) -> dict[str, Any]:
    return {
        "id": _to_text(entity.id),
        "portfolio_id": _to_text(portfolio_id),
        "entity_type": entity.entity_type.value,
        "title": entity.title,
        "description": entity.description,
        "status": entity.status.value,
        "source": entity.source.value,
        "confidence": entity.confidence,
        "created_at": _to_datetime_text(entity.created_at),
        "updated_at": _to_datetime_text(entity.updated_at),
    }


def _entity_from_row(row: EntityRow) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=UUID(row.id),
        entity_type=EntityType(row.entity_type),
        title=row.title,
        description=row.description,
        status=EntityStatus(row.status),
        source=SourceKind(row.source),
        confidence=row.confidence,
        created_at=_to_domain_datetime(row.created_at),
        updated_at=_to_domain_datetime(row.updated_at),
    )


def _relation_to_row(portfolio_id: UUID, relation: TrajectoryRelation) -> dict[str, Any]:
    return {
        "id": _to_text(relation.id),
        "portfolio_id": _to_text(portfolio_id),
        "source_id": _to_text(relation.source_id),
        "target_id": _to_text(relation.target_id),
        "relation_type": relation.relation_type.value,
        "source": relation.source.value,
        "confidence": relation.confidence,
    }


def _relation_from_row(row: RelationRow) -> TrajectoryRelation:
    return TrajectoryRelation(
        id=UUID(row.id),
        source_id=UUID(row.source_id),
        target_id=UUID(row.target_id),
        relation_type=RelationType(row.relation_type),
        source=SourceKind(row.source),
        confidence=row.confidence,
    )


class SqlitePortfolioRepository:
    """Save and load complete portfolio snapshots in a SQLite file.

    The engine is owned by the repository; close it with ``close()`` when
    done. One connection is used per operation, so the repository can be
    shared freely from a single thread without ``check_same_thread=False``.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._engine = create_engine(f"sqlite:///{self._path.as_posix()}")

        @event.listens_for(self._engine, "connect")
        def _enable_foreign_keys(
            dbapi_connection: Any, connection_record: Any
        ) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        # The pragma is registered before the first connection is opened,
        # so it is active for metadata.create_all() as well.
        Base.metadata.create_all(self._engine)

    @property
    def engine(self) -> Engine:
        """Expose the underlying engine for inspection (e.g. pragma checks)."""
        return self._engine

    def __enter__(self) -> SqlitePortfolioRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def foreign_keys_enabled(self) -> bool:
        """Report whether ``PRAGMA foreign_keys`` is ON for new connections."""
        with self._engine.connect() as connection:
            result = connection.execute(text("PRAGMA foreign_keys")).scalar_one()

        return bool(result)

    def close(self) -> None:
        """Release the database connections owned by this repository."""
        self._engine.dispose()

    def save(self, portfolio: Portfolio) -> None:
        """Persist a complete portfolio snapshot atomically.

        The ``Session`` is the single ORM transaction boundary; the
        explicit ``commit()`` makes the atomic boundary visible at the
        end of the block.
        """
        portfolio_id = _to_text(portfolio.id)

        with Session(self._engine) as session:
            existing = session.scalar(
                select(PortfolioRow.id).where(PortfolioRow.id == portfolio_id)
            )

            if existing is None:
                session.execute(
                    insert(PortfolioRow).values(id=portfolio_id, name=portfolio.name)
                )
            else:
                session.execute(
                    update(PortfolioRow)
                    .where(PortfolioRow.id == portfolio_id)
                    .values(name=portfolio.name)
                )

            # Full snapshot replacement: drop all previously persisted
            # relations and entities before inserting the current state.
            session.execute(delete(RelationRow).where(RelationRow.portfolio_id == portfolio_id))
            session.execute(delete(EntityRow).where(EntityRow.portfolio_id == portfolio_id))

            entity_rows = [
                _entity_to_row(portfolio.id, entity)
                for entity in portfolio.entities
            ]
            if entity_rows:
                session.execute(insert(EntityRow), entity_rows)

            relation_rows = [
                _relation_to_row(portfolio.id, relation)
                for relation in portfolio.relations
            ]
            if relation_rows:
                session.execute(insert(RelationRow), relation_rows)

            session.commit()

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        """Load a portfolio by UUID, or return ``None`` if it does not exist."""
        stored_id = _to_text(portfolio_id)

        with Session(self._engine) as session:
            header = session.scalar(
                select(PortfolioRow).where(PortfolioRow.id == stored_id)
            )

            if header is None:
                return None

            entity_rows: list[EntityRow] = list(
                session.scalars(
                    select(EntityRow).where(EntityRow.portfolio_id == stored_id)
                ).all()
            )

            relation_rows: list[RelationRow] = list(
                session.scalars(
                    select(RelationRow).where(RelationRow.portfolio_id == stored_id)
                ).all()
            )

            # Reconstructing the Portfolio re-runs the canonical integrity
            # validation, so a restored snapshot must be domain-valid.
            return Portfolio(
                id=UUID(header.id),
                name=header.name,
                entities=[_entity_from_row(row) for row in entity_rows],
                relations=[_relation_from_row(row) for row in relation_rows],
            )
