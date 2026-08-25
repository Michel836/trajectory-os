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

Load reconstructs a REAL ``ExecutionEffortObservation`` through normal,
strict Pydantic validation (never ``model_construct``), so model-level
invariants are revalidated on every ``get()``.

Append-only contract:

- ``add()`` only INSERTs; it never updates or replaces existing rows;
- a duplicate observation id raises
  :class:`DuplicateExecutionEffortObservationError` inside the add
  transaction, leaving the existing row untouched;
- no concurrency or distributed-transaction guarantees are claimed; a
  database-level uniqueness violation may still propagate in a race.

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


class SqliteExecutionEffortObservationRepository:
    """Append-only, durable storage of execution-effort observations.

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

        Reconstructs a fresh ``ExecutionEffortObservation`` through
        strict domain validation and does not mutate or delete anything.
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

        return ExecutionEffortObservation(
            id=UUID(row.id),
            portfolio_id=UUID(row.portfolio_id),
            entity_id=UUID(row.entity_id),
            duration_seconds=int(row.duration_seconds),
            observed_at=_to_domain_datetime(row.observed_at),
            source=SourceKind(row.source),
        )
