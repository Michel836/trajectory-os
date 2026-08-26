"""SQLite persistence adapter for durable execution-effort observations.

``SqliteExecutionEffortObservationRepository`` persists the V1.8-A
immutable ``ExecutionEffortObservation`` domain values against the same
SQLite file used by ``SqlitePortfolioRepository``, using the shared
explicit row schema in ``models.py``.

Representation mapping is explicit at this boundary:

- UUID values are stored as 36-character text (RFC 4122 string form);
- ``SourceKind`` is stored by its enum ``.value``;
- ``observed_at`` is stored as ISO-8601 text preserving the timezone
  offset; reconstruction uses ``datetime.fromisoformat()``;
- ``duration_seconds`` is stored as an integer.

Load/read reconstructs REAL ``ExecutionEffortObservation`` values through
normal strict Pydantic validation (never ``model_construct``), so model-level
invariants are revalidated on every read.

Append-only V1.8 contract:

- ``add()`` only INSERTs; it never updates or replaces existing rows;
- a duplicate observation id raises
  :class:`DuplicateExecutionEffortObservationError` inside the add
  transaction, leaving the existing row untouched;
- no concurrency or distributed-transaction guarantees are claimed; a
  database-level uniqueness violation may still propagate in a race.

Read-only V1.9 contract:

- ``list_for_portfolio()`` and ``list_for_entity()`` never mutate rows;
- results are ordered by actual aware ``observed_at`` instant ascending and
  then observation UUID ascending, never by insertion order or lexical ISO text;
- no SQL aggregation, recursive WBS query, analytics table, or cache is used.

``entity_id`` carries no foreign key to ``entities.id`` by design:
historical observations must survive the replacement or removal of
entity snapshot rows in later portfolio saves. ``portfolio_id`` IS a
foreign key into ``portfolios.id`` with ``ON DELETE CASCADE``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import event, insert, select
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence.models import (
    Base,
    ExecutionEffortObservationRow,
)
from trajectory_os.domain.entities import SourceKind
from trajectory_os.domain.execution_effort import ExecutionEffortObservation


class DuplicateExecutionEffortObservationError(ValueError):
    """Raised when an observation id is already durably stored."""


def _to_text(value: UUID) -> str:
    return str(value)


def _to_domain_datetime(value: str) -> datetime:
    """Reconstruct a datetime from stored ISO-8601 text."""
    return datetime.fromisoformat(value)


def _row_to_domain(row: ExecutionEffortObservationRow) -> ExecutionEffortObservation:
    """Reconstruct one strict immutable domain observation from a stored row."""
    return ExecutionEffortObservation(
        id=UUID(row.id),
        portfolio_id=UUID(row.portfolio_id),
        entity_id=UUID(row.entity_id),
        duration_seconds=int(row.duration_seconds),
        observed_at=_to_domain_datetime(row.observed_at),
        source=SourceKind(row.source),
    )


def _observation_sort_key(
    observation: ExecutionEffortObservation,
) -> tuple[datetime, int]:
    """Order by chronological instant, with UUID as deterministic tie-breaker."""
    return observation.observed_at, observation.id.int


class SqliteExecutionEffortObservationRepository:
    """Append-only durable storage plus deterministic read-only V1.9 queries.

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

    def __enter__(self) -> SqliteExecutionEffortObservationRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the database connections owned by this repository."""
        self._engine.dispose()

    def add(self, observation: ExecutionEffortObservation) -> None:
        """Persist one durable observation with INSERT-only semantics.

        Within the add transaction, an existing row with the same
        observation id is detected and
        :class:`DuplicateExecutionEffortObservationError` is raised; the
        existing row is never updated or replaced.
        """
        stored_id = _to_text(observation.id)

        with Session(self._engine) as session:
            existing = session.scalar(
                select(ExecutionEffortObservationRow.id).where(
                    ExecutionEffortObservationRow.id == stored_id
                )
            )

            if existing is not None:
                raise DuplicateExecutionEffortObservationError(
                    f"execution-effort observation already exists: {stored_id}"
                )

            session.execute(
                insert(ExecutionEffortObservationRow).values(
                    id=stored_id,
                    portfolio_id=_to_text(observation.portfolio_id),
                    entity_id=_to_text(observation.entity_id),
                    duration_seconds=observation.duration_seconds,
                    observed_at=observation.observed_at.isoformat(),
                    source=observation.source.value,
                )
            )

            session.commit()

    def get(
        self,
        observation_id: UUID,
    ) -> ExecutionEffortObservation | None:
        """Load the stored observation, or return ``None`` if absent.

        The lookup does not require the entity to still exist in the
        current portfolio; ``entity_id`` is deliberately not an FK.
        """
        stored_id = _to_text(observation_id)

        with Session(self._engine) as session:
            row = session.scalar(
                select(ExecutionEffortObservationRow).where(
                    ExecutionEffortObservationRow.id == stored_id
                )
            )

        if row is None:
            return None

        return _row_to_domain(row)

    def list_for_portfolio(
        self,
        portfolio_id: UUID,
    ) -> tuple[ExecutionEffortObservation, ...]:
        """Return all observations for one portfolio in deterministic time order.

        Rows are reconstructed before sorting so aware datetimes with different
        UTC offsets are compared by their actual chronological instant rather
        than by the lexical ordering of their stored ISO-8601 text.
        """
        stored_portfolio_id = _to_text(portfolio_id)

        with Session(self._engine) as session:
            rows = tuple(
                session.scalars(
                    select(ExecutionEffortObservationRow).where(
                        ExecutionEffortObservationRow.portfolio_id
                        == stored_portfolio_id
                    )
                ).all()
            )

        observations = [_row_to_domain(row) for row in rows]
        observations.sort(key=_observation_sort_key)
        return tuple(observations)

    def list_for_entity(
        self,
        portfolio_id: UUID,
        entity_id: UUID,
    ) -> tuple[ExecutionEffortObservation, ...]:
        """Return exact portfolio/entity history in deterministic time order.

        Entity membership in the CURRENT Portfolio is intentionally not checked:
        historical observations remain queryable after snapshot replacement or
        entity removal.
        """
        stored_portfolio_id = _to_text(portfolio_id)
        stored_entity_id = _to_text(entity_id)

        with Session(self._engine) as session:
            rows = tuple(
                session.scalars(
                    select(ExecutionEffortObservationRow).where(
                        ExecutionEffortObservationRow.portfolio_id
                        == stored_portfolio_id,
                        ExecutionEffortObservationRow.entity_id == stored_entity_id,
                    )
                ).all()
            )

        observations = [_row_to_domain(row) for row in rows]
        observations.sort(key=_observation_sort_key)
        return tuple(observations)
