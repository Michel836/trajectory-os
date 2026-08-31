"""SQLite persistence adapter for durable V1.34 focus decisions (V1.35).

``SqlitePortfolioProjectEffortFocusDecisionRepository`` persists durable,
append-only V1.35 records: ONE SQLite transaction appends EXACTLY the
passed V1.35 ``PortfolioProjectEffortFocusDecisionRecord`` into the
immutable ``portfolio_project_effort_focus_decision_records`` table.
There is no update, replace, upsert, save, or delete path: the same
``decision_id`` may be stored EXACTLY ONCE, and
:class:`DuplicatePortfolioProjectEffortFocusDecisionError` is raised for
a second attempt — the existing row is never touched.

Representation mapping is explicit at this boundary:

- UUID values are stored as 36-character text (RFC 4122 string form);
- ``decided_at`` is stored as EXACT ISO-8601 aware text — the caller's
  original UTC offset is preserved (never re-normalized to UTC);
- the EXACT accepted V1.34 decision is stored as deterministic explicit
  JSON via the Pydantic JSON serialization of the genuine nested domain
  object (``model_dump(mode="json")`` -> JSON text). This is NOT a
  pickle and NOT an opaque binary blob: every V1.34 scalar also remains
  independently queryable through the dedicated ``portfolio_id`` column
  (which always agrees with the nested decision's ``portfolio_id``,
  enforced on read) and visible in the JSON for corruption checks.

Read-back reconstructs REAL domain values through normal strict Pydantic
validation (never ``model_construct``): the stored JSON is re-validated
back into a genuine, fresh V1.34 ``PortfolioProjectEffortFocusDecision``
(``model_validate_json`` — model-level invariants revalidated on every
read), and the explicit columns are re-checked against that genuine
decision. The stored ``portfolio_id`` column must agree with the
decision's own ``portfolio_id`` (a disagreeing row is corrupt storage and
raises a precise error; no silent adoption).

History is ordered by TRUE chronological instant (aware ``decided_at``,
so two datetimes with different UTC offsets are compared by their actual
instant, not by the lexical ordering of their stored ISO text) and then
by ``decision_id.int`` (numeric UUID order, not lexical string order).
``list_history`` returns ``()`` for an empty history. The reader
performs no derivation and infers no "current"/"effective"/"latest"
decision: it returns exactly the stored records.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import event, insert, select
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence.models import (
    Base,
)
from trajectory_os.adapters.persistence.models import (
    PortfolioProjectEffortFocusDecisionRecordRow as Row,
)
from trajectory_os.application.execution_effort_project_focus_decision import (
    PortfolioProjectEffortFocusDecision,
)
from trajectory_os.application.execution_effort_project_focus_decision_persistence import (  # noqa: E501
    PortfolioProjectEffortFocusDecisionRecord,
)


class DuplicatePortfolioProjectEffortFocusDecisionError(ValueError):
    """Raised when a durable focus decision with the same decision_id exists."""


# The exact SQLite integrity error message for a PRIMARY KEY / UNIQUE
# violation on the decision_id column of our table.
_DUPLICATE_DECISION_ID_MESSAGE = (
    "UNIQUE constraint failed: portfolio_project_effort_focus_decision_records.decision_id"
)


def _is_duplicate_focus_decision_id_violation(exc: IntegrityError) -> bool:
    """Return True ONLY for a SQLite UNIQUE/PK violation on
    ``portfolio_project_effort_focus_decision_records.decision_id``.

    Inspects the underlying ``sqlite3.IntegrityError`` and its actual
    constraint message — not a blanket ``IntegrityError`` classification.
    """
    orig = exc.orig
    if not isinstance(orig, sqlite3.IntegrityError):
        return False
    return _DUPLICATE_DECISION_ID_MESSAGE in str(orig)


def _to_text(value: UUID) -> str:
    return str(value)


def _to_domain_datetime(value: str) -> datetime:
    """Reconstruct an aware datetime from stored ISO-8601 text."""
    return datetime.fromisoformat(value)


def _row_to_record(
    row: Any, expected_portfolio_id: UUID
) -> PortfolioProjectEffortFocusDecisionRecord:
    """Reconstruct one strict immutable V1.35 durable record from a row.

    The stored explicit JSON snapshot is FIRST re-validated into a
    genuine, fresh V1.34 ``PortfolioProjectEffortFocusDecision`` through
    normal strict Pydantic validation (never ``model_construct``), and
    only then is the outer V1.35 record built around it. The dedicated
    ``portfolio_id`` column is then re-checked against that genuine
    decision's own ``portfolio_id``; a disagreeing row is corrupt
    storage and is rejected with a precise error.
    """
    decision = PortfolioProjectEffortFocusDecision.model_validate_json(
        row.decision_snapshot,
    )
    if decision.portfolio_id != expected_portfolio_id:
        raise ValueError(
            "stored focus decision row for decision "
            f"{row.decision_id} carries portfolio_id "
            f"{row.portfolio_id} but its exact V1.34 snapshot declares "
            f"{decision.portfolio_id}; the explicit column and the "
            "nested decision must always agree"
        )
    return PortfolioProjectEffortFocusDecisionRecord(
        decision_id=UUID(row.decision_id),
        decided_at=_to_domain_datetime(row.decided_at),
        decision=decision,
    )


def _record_sort_key(
    record: PortfolioProjectEffortFocusDecisionRecord,
) -> tuple[datetime, int]:
    """Order by true chronological instant, UUID int as tie-breaker."""
    return record.decided_at, record.decision_id.int


class SqlitePortfolioProjectEffortFocusDecisionRepository:
    """Append-only durable V1.35 storage plus deterministic read-only history.

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

    def __enter__(self) -> SqlitePortfolioProjectEffortFocusDecisionRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the database connections owned by this repository."""
        self._engine.dispose()

    def add(self, record: object) -> None:
        """Persist one durable V1.35 record with INSERT-only semantics.

        The SQLite PRIMARY KEY constraint on ``decision_id`` is the
        atomic duplicate authority: a second INSERT with the same
        ``decision_id`` raises a DB-level IntegrityError that this
        adapter translates into
        :class:`DuplicatePortfolioProjectEffortFocusDecisionError`.
        The existing row is never updated or replaced.

        The input is strictly validated before any field access to
        prevent bypass of domain validation via ``model_construct()``
        (a hostile nested V1.34 decision that genuine construction could
        never have produced fails the strict re-validation round-trip
        before any write).
        """
        if not isinstance(record, PortfolioProjectEffortFocusDecisionRecord):
            raise TypeError(
                "record must be a genuine V1.35 "
                "PortfolioProjectEffortFocusDecisionRecord instance, "
                f"got {type(record).__name__}"
            )

        validated = PortfolioProjectEffortFocusDecisionRecord.model_validate(
            record.model_dump(mode="python"),
            strict=True,
        )

        stored_decision_id = _to_text(validated.decision_id)

        session = Session(self._engine)
        try:
            session.execute(
                insert(Row).values(
                    decision_id=stored_decision_id,
                    portfolio_id=_to_text(validated.decision.portfolio_id),
                    decided_at=validated.decided_at.isoformat(),
                    decision_snapshot=validated.decision.model_dump_json(),
                )
            )
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if _is_duplicate_focus_decision_id_violation(exc):
                raise DuplicatePortfolioProjectEffortFocusDecisionError(
                    f"durable focus decision already exists: {stored_decision_id}"
                ) from exc
            raise
        finally:
            session.close()

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[PortfolioProjectEffortFocusDecisionRecord, ...]:
        """Return the exact durable focus-decision history for one portfolio.

        Rows are reconstructed before sorting so aware datetimes with
        different UTC offsets are compared by their actual chronological
        instant rather than by the lexical ordering of their stored
        ISO-8601 text; equal instants are ordered by ``decision_id.int``
        (numeric UUID order, not lexical string order). Returns ``()``
        when the history is empty. The reader performs no derivation and
        infers no "current"/"effective"/"latest" decision.
        """
        stored_portfolio_id = _to_text(portfolio_id)

        with Session(self._engine) as session:
            rows = tuple(
                session.scalars(
                    select(Row).where(
                        Row.portfolio_id == stored_portfolio_id
                    )
                ).all()
            )

        records = [_row_to_record(row, portfolio_id) for row in rows]
        records.sort(key=_record_sort_key)
        return tuple(records)
