"""SQLite persistence adapter for durable planned-effort estimates.

``SqliteExecutionEffortEstimateRepository`` persists the V1.10-A immutable
``ExecutionEffortEstimate`` domain values against the same SQLite file used
by ``SqlitePortfolioRepository``, using the shared explicit row schema in
``models.py``. The estimate table is a separate table from the
V1.8 actual-observation table and is never merged with it.

Representation mapping is explicit at this boundary:

- UUID values are stored as 36-character text (RFC 4122 string form);
- ``SourceKind`` is stored by its enum ``.value``;
- ``estimated_at`` is stored as ISO-8601 text preserving the timezone
  offset; reconstruction uses ``datetime.fromisoformat()``;
- ``duration_seconds`` is stored as an integer.

Load/read reconstructs REAL ``ExecutionEffortEstimate`` values through
normal strict Pydantic validation (never ``model_construct``), so model-level
invariants are revalidated on every read.

Append-only V1.10 contract:

- ``add()`` only INSERTs; it never updates or replaces existing rows;
- a duplicate estimate id raises
  :class:`DuplicateExecutionEffortEstimateError` inside the add
  transaction, leaving the existing row untouched;
- no update/delete/correction API exists;
- no concurrency or distributed-transaction guarantees are claimed; a
  database-level uniqueness violation may still propagate in a race.

Read-only contract:

- ``list_for_portfolio()`` and ``list_for_entity()`` never mutate rows;
- results are ordered by actual aware ``estimated_at`` instant ascending and
  then estimate UUID ascending, never by insertion order or lexical ISO text;
- no SQL aggregation, recursive WBS query, analytics table, or cache is used.

``entity_id`` carries no foreign key to ``entities.id`` by design:
historical estimates must survive the replacement or removal of
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
    ExecutionEffortEstimateRow,
)
from trajectory_os.domain.entities import SourceKind
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
)


class DuplicateExecutionEffortEstimateError(ValueError):
    """Raised when an estimate id is already durably stored."""


def _to_text(value: UUID) -> str:
    return str(value)


def _to_domain_datetime(value: str) -> datetime:
    """Reconstruct a datetime from stored ISO-8601 text."""
    return datetime.fromisoformat(value)


def _row_to_domain(row: ExecutionEffortEstimateRow) -> ExecutionEffortEstimate:
    """Reconstruct one strict immutable domain estimate from a stored row."""
    return ExecutionEffortEstimate(
        id=UUID(row.id),
        portfolio_id=UUID(row.portfolio_id),
        entity_id=UUID(row.entity_id),
        duration_seconds=int(row.duration_seconds),
        estimated_at=_to_domain_datetime(row.estimated_at),
        source=SourceKind(row.source),
    )


def _estimate_sort_key(
    estimate: ExecutionEffortEstimate,
) -> tuple[datetime, int]:
    """Order by chronological instant, with UUID as deterministic tie-breaker."""
    return estimate.estimated_at, estimate.id.int


class SqliteExecutionEffortEstimateRepository:
    """Append-only durable storage plus deterministic read-only estimate queries.

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

    def __enter__(self) -> SqliteExecutionEffortEstimateRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the database connections owned by this repository."""
        self._engine.dispose()

    def add(self, estimate: ExecutionEffortEstimate) -> None:
        """Persist one durable estimate with INSERT-only semantics.

        Within the add transaction, an existing row with the same estimate id
        is detected and :class:`DuplicateExecutionEffortEstimateError` is
        raised; the existing row is never updated or replaced.
        """
        stored_id = _to_text(estimate.id)

        with Session(self._engine) as session:
            existing = session.scalar(
                select(ExecutionEffortEstimateRow.id).where(
                    ExecutionEffortEstimateRow.id == stored_id
                )
            )

            if existing is not None:
                raise DuplicateExecutionEffortEstimateError(
                    f"execution-effort estimate already exists: {stored_id}"
                )

            session.execute(
                insert(ExecutionEffortEstimateRow).values(
                    id=stored_id,
                    portfolio_id=_to_text(estimate.portfolio_id),
                    entity_id=_to_text(estimate.entity_id),
                    duration_seconds=estimate.duration_seconds,
                    estimated_at=estimate.estimated_at.isoformat(),
                    source=estimate.source.value,
                )
            )

            session.commit()

    def get(self, estimate_id: UUID) -> ExecutionEffortEstimate | None:
        """Load the stored estimate, or return ``None`` if absent.

        The lookup does not require the entity to still exist in the
        current portfolio; ``entity_id`` is deliberately not an FK.
        """
        stored_id = _to_text(estimate_id)

        with Session(self._engine) as session:
            row = session.scalar(
                select(ExecutionEffortEstimateRow).where(
                    ExecutionEffortEstimateRow.id == stored_id
                )
            )

        if row is None:
            return None

        return _row_to_domain(row)

    def list_for_portfolio(
        self,
        portfolio_id: UUID,
    ) -> tuple[ExecutionEffortEstimate, ...]:
        """Return all estimates for one portfolio in deterministic time order.

        Rows are reconstructed before sorting so aware datetimes with
        different UTC offsets are compared by their actual chronological
        instant rather than by the lexical ordering of their stored
        ISO-8601 text.
        """
        stored_portfolio_id = _to_text(portfolio_id)

        with Session(self._engine) as session:
            rows = tuple(
                session.scalars(
                    select(ExecutionEffortEstimateRow).where(
                        ExecutionEffortEstimateRow.portfolio_id
                        == stored_portfolio_id
                    )
                ).all()
            )

        estimates = [_row_to_domain(row) for row in rows]
        estimates.sort(key=_estimate_sort_key)
        return tuple(estimates)

    def list_for_entity(
        self,
        portfolio_id: UUID,
        entity_id: UUID,
    ) -> tuple[ExecutionEffortEstimate, ...]:
        """Return exact portfolio/entity history in deterministic time order.

        Entity membership in the CURRENT Portfolio is intentionally not
        checked: historical estimates remain queryable after snapshot
        replacement or entity removal.
        """
        stored_portfolio_id = _to_text(portfolio_id)
        stored_entity_id = _to_text(entity_id)

        with Session(self._engine) as session:
            rows = tuple(
                session.scalars(
                    select(ExecutionEffortEstimateRow).where(
                        ExecutionEffortEstimateRow.portfolio_id
                        == stored_portfolio_id,
                        ExecutionEffortEstimateRow.entity_id == stored_entity_id,
                    )
                ).all()
            )

        estimates = [_row_to_domain(row) for row in rows]
        estimates.sort(key=_estimate_sort_key)
        return tuple(estimates)
