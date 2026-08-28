"""SQLite persistence adapter for durable V1.16 calibration-factor decisions.

``SqliteExecutionEffortCalibrationFactorDecisionRepository`` persists the
V1.16 immutable ``EffortCalibrationFactorDecision`` domain values against
the same SQLite file used by ``SqlitePortfolioRepository``, using the
shared explicit row schema in ``models.py``. The decision table is a
separate table and is never merged with the estimate or observation
tables.

Representation mapping is explicit at this boundary:

- UUID values are stored as 36-character text (RFC 4122 string form);
- ``EntityType``, ``EffortCalibrationFactorProposalReason``, and
  ``EffortCalibrationDecision`` are stored by their enum ``.value``;
- ``decided_at`` is stored as ISO-8601 text preserving the timezone
  offset; reconstruction uses ``datetime.fromisoformat()``;
- ``proposal_available`` is stored as an exact 0/1 integer;
- the factor remains EXACT INTEGER numerator/denominator columns (the
  factor is never stored as a SQLite REAL and no float is introduced).

Load/read reconstructs REAL ``EffortCalibrationFactorDecision`` values
through normal strict Pydantic validation (never ``model_construct``), so
model-level invariants are revalidated on every read.

Append-only V1.16 contract:

- ``add()`` only INSERTs; it never updates or replaces existing rows;
- a duplicate decision id raises
  :class:`DuplicateEffortCalibrationFactorDecisionError` inside the add
  transaction, leaving the existing row untouched;
- multiple records for the same portfolio/project/entity_type are allowed
  (history, not supersession — no supersede/update/delete API exists);
- no concurrency or distributed-transaction guarantees are claimed; a
  database-level uniqueness violation may still propagate in a race.

Read-only history contract:

- ``list_history()`` never mutates rows and returns a tuple (``()`` when
  empty);
- results are ordered by actual aware ``decided_at`` instant ascending and
  then decision UUID int ascending, never by insertion order or lexical
  ISO text;
- the reader reconstructs the EXACT stored snapshot rows only; it never
  infers a "current" or "effective" decision and never rederives V1.15.

``portfolio_id`` is a foreign key into ``portfolios.id`` with
``ON DELETE CASCADE`` (same convention as the estimate/observation
tables); ``project_id``/``entity_type`` carry no entity snapshot foreign
keys by design: decision history must survive replacement or removal of
entity snapshot rows in later portfolio saves.
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
    ExecutionEffortCalibrationFactorDecisionRow,
)
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_factor_decisions import (
    EffortCalibrationDecision,
    EffortCalibrationFactorDecision,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
)


class DuplicateEffortCalibrationFactorDecisionError(ValueError):
    """Raised when a decision id is already durably stored."""


def _to_text(value: UUID) -> str:
    return str(value)


def _to_domain_datetime(value: str) -> datetime:
    """Reconstruct a datetime from stored ISO-8601 text."""
    return datetime.fromisoformat(value)


def _row_to_domain(
    row: ExecutionEffortCalibrationFactorDecisionRow,
) -> EffortCalibrationFactorDecision:
    """Reconstruct one strict immutable domain decision from a stored row."""
    return EffortCalibrationFactorDecision(
        decision_id=UUID(row.id),
        portfolio_id=UUID(row.portfolio_id),
        project_id=UUID(row.project_id),
        entity_type=EntityType(row.entity_type),
        sample_count=int(row.sample_count),
        minimum_required_sample_count=int(row.minimum_required_sample_count),
        total_planned_duration_seconds=int(row.total_planned_duration_seconds),
        total_actual_duration_seconds=int(row.total_actual_duration_seconds),
        proposal_available=bool(row.proposal_available),
        proposal_reason=EffortCalibrationFactorProposalReason(row.proposal_reason),
        factor_numerator=(
            None if row.factor_numerator is None else int(row.factor_numerator)
        ),
        factor_denominator=(
            None if row.factor_denominator is None else int(row.factor_denominator)
        ),
        decision=EffortCalibrationDecision(row.decision),
        decided_at=_to_domain_datetime(row.decided_at),
    )


def _decision_sort_key(
    decision: EffortCalibrationFactorDecision,
) -> tuple[datetime, int]:
    """Order by chronological instant, with UUID int as tie-breaker."""
    return decision.decided_at, decision.decision_id.int


class SqliteExecutionEffortCalibrationFactorDecisionRepository:
    """Append-only durable storage plus deterministic read-only decision history.

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

    def __enter__(self) -> SqliteExecutionEffortCalibrationFactorDecisionRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the database connections owned by this repository."""
        self._engine.dispose()

    def add(self, decision: EffortCalibrationFactorDecision) -> None:
        """Persist one durable decision record with INSERT-only semantics.

        Within the add transaction, an existing row with the same decision
        id is detected and
        :class:`DuplicateEffortCalibrationFactorDecisionError` is raised;
        the existing row is never updated or replaced.
        """
        stored_id = _to_text(decision.decision_id)

        with Session(self._engine) as session:
            existing = session.scalar(
                select(ExecutionEffortCalibrationFactorDecisionRow.id).where(
                    ExecutionEffortCalibrationFactorDecisionRow.id == stored_id
                )
            )

            if existing is not None:
                raise DuplicateEffortCalibrationFactorDecisionError(
                    f"effort calibration factor decision already exists: "
                    f"{stored_id}"
                )

            session.execute(
                insert(ExecutionEffortCalibrationFactorDecisionRow).values(
                    id=stored_id,
                    portfolio_id=_to_text(decision.portfolio_id),
                    project_id=_to_text(decision.project_id),
                    entity_type=decision.entity_type.value,
                    sample_count=decision.sample_count,
                    minimum_required_sample_count=(
                        decision.minimum_required_sample_count
                    ),
                    total_planned_duration_seconds=(
                        decision.total_planned_duration_seconds
                    ),
                    total_actual_duration_seconds=(
                        decision.total_actual_duration_seconds
                    ),
                    proposal_available=int(decision.proposal_available),
                    proposal_reason=decision.proposal_reason.value,
                    factor_numerator=decision.factor_numerator,
                    factor_denominator=decision.factor_denominator,
                    decision=decision.decision.value,
                    decided_at=decision.decided_at.isoformat(),
                )
            )

            session.commit()

    def list_history(
        self,
        portfolio_id: UUID,
        project_id: UUID,
        entity_type: EntityType,
    ) -> tuple[EffortCalibrationFactorDecision, ...]:
        """Return the exact durable decision history for one segment scope.

        Rows are reconstructed before sorting so aware datetimes with
        different UTC offsets are compared by their actual chronological
        instant rather than by the lexical ordering of their stored
        ISO-8601 text. Returns ``()`` when the history is empty. The
        reader performs no V1.15 derivation and infers no "current" or
        "effective" decision.
        """
        stored_portfolio_id = _to_text(portfolio_id)
        stored_project_id = _to_text(project_id)
        stored_entity_type = entity_type.value

        with Session(self._engine) as session:
            rows = tuple(
                session.scalars(
                    select(ExecutionEffortCalibrationFactorDecisionRow).where(
                        ExecutionEffortCalibrationFactorDecisionRow.portfolio_id
                        == stored_portfolio_id,
                        ExecutionEffortCalibrationFactorDecisionRow.project_id
                        == stored_project_id,
                        ExecutionEffortCalibrationFactorDecisionRow.entity_type
                        == stored_entity_type,
                    )
                ).all()
            )

        decisions = [_row_to_domain(row) for row in rows]
        decisions.sort(key=_decision_sort_key)
        return tuple(decisions)
