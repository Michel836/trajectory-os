"""SQLite append-only persistence for durable V1.55 human lifecycle decision
history.

The adapter persists exact, already-produced V1.55
``TaskExecutionLifecycleDecisionRecord`` records, each of which embeds one
genuine V1.50 ``TaskExecutionLifecycleDecision``.

Storage is append-only:

- ``decision_record_id`` is the sole durable-record primary-key authority;
- repeated ``lifecycle_decision_id`` / ``execution_record_id`` / task /
  disposition / timestamp values remain legal under distinct record ids;
- caller-supplied aware ``recorded_at`` and V1.50 ``decided_at`` offsets are
  preserved exactly;
- the exact embedded V1.50 decision is stored as explicit deterministic
  JSON;
- dedicated scalar columns (portfolio, ids, disposition, decided_at) make
  corruption directly visible;
- no update, replace, upsert, save, delete, current, latest, or effective
  decision-API exists.

Read-back reconstructs a genuine fresh V1.55
``TaskExecutionLifecycleDecisionRecord`` through normal Pydantic validation
(the record model itself freshly strict-revalidates the embedded V1.50
decision) and cross-checks every duplicated scalar column against that
snapshot. Corrupt rows are rejected rather than normalized. There is no
competing V1.50/V1.55 lifecycle validator here.

History ordering is by the true aware datetime instant of the outer
``recorded_at`` and then by ``decision_record_id.int``.
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

from trajectory_os.adapters.persistence.models import Base
from trajectory_os.adapters.persistence.models import (
    TaskExecutionLifecycleDecisionRecordRow as Row,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle import (  # noqa: E501
    TaskExecutionLifecycleDecision,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_decision_persistence import (  # noqa: E501
    TaskExecutionLifecycleDecisionRecord,
)


class DuplicateTaskExecutionLifecycleDecisionRecordError(ValueError):
    """Raised when decision_record_id already exists durably."""


_DUPLICATE_DECISION_RECORD_ID_MESSAGE = (
    "UNIQUE constraint failed: "
    "task_execution_lifecycle_decision_records.decision_record_id"
)


def _is_duplicate_decision_record_id_violation(exc: IntegrityError) -> bool:
    """Return True only for this table's decision_record_id PK violation."""

    orig = exc.orig
    if not isinstance(orig, sqlite3.IntegrityError):
        return False
    return _DUPLICATE_DECISION_RECORD_ID_MESSAGE in str(orig)


def _to_text(value: UUID) -> str:
    return str(value)


def _to_iso(value: datetime) -> str:
    return value.isoformat()


def _from_iso(value: str) -> datetime:
    """Strictly reconstruct an aware ISO-8601 timestamp.

    A naive (offset-less) stored timestamp is corruption and is rejected
    rather than silently normalized.
    """

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "stored lifecycle decision timestamp must be an aware "
            f"ISO-8601 datetime with a non-None UTC offset, got {value!r}"
        )
    return parsed


def _row_to_record(
    row: Any,
    expected_portfolio_id: UUID,
) -> TaskExecutionLifecycleDecisionRecord:
    """Strictly reconstruct and cross-check one durable V1.55 row."""

    decision = TaskExecutionLifecycleDecision.model_validate_json(
        row.decision_snapshot
    )

    # Normal reconstruction through the V1.55 record model freshly
    # strict-revalidates the embedded V1.50 decision — so the outer record's
    # own invariants rerun before the row is ever accepted. Never
    # model-constructed.
    record = TaskExecutionLifecycleDecisionRecord(
        decision_record_id=UUID(row.decision_record_id),
        recorded_at=_from_iso(row.recorded_at),
        decision=decision,
    )

    mismatches: list[str] = []

    expected = {
        "portfolio_id": _to_text(decision.portfolio_id),
        "lifecycle_decision_id": _to_text(decision.lifecycle_decision_id),
        "execution_record_id": _to_text(decision.execution_record_id),
        "authorized_task_id": _to_text(decision.authorized_task_id),
        "disposition": decision.disposition.value,
        "decided_at": _to_iso(decision.decided_at),
    }

    for column_name, expected_value in expected.items():
        stored_value = getattr(row, column_name)
        if stored_value != expected_value:
            mismatches.append(
                f"{column_name}: stored={stored_value!r}, "
                f"snapshot={expected_value!r}"
            )

    expected_portfolio_text = _to_text(expected_portfolio_id)
    if row.portfolio_id != expected_portfolio_text:
        mismatches.append(
            "portfolio_id query scope: "
            f"stored={row.portfolio_id!r}, "
            f"requested={expected_portfolio_text!r}"
        )

    if mismatches:
        details = "; ".join(mismatches)
        raise ValueError(
            "stored V1.55 lifecycle decision row disagrees with its "
            f"exact embedded V1.50 snapshot: {details}"
        )

    return record


def _record_sort_key(
    record: TaskExecutionLifecycleDecisionRecord,
) -> tuple[datetime, int]:
    return record.recorded_at, record.decision_record_id.int


class SqliteTaskExecutionLifecycleDecisionRepository:
    """Append-only SQLite repository for exact V1.55 lifecycle decision
    history."""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._engine = create_engine(f"sqlite:///{self._path.as_posix()}")

        @event.listens_for(self._engine, "connect")
        def _enable_foreign_keys(
            dbapi_connection: Any,
            connection_record: Any,
        ) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        Base.metadata.create_all(self._engine)

    @property
    def engine(self) -> Engine:
        """Expose the engine for persistence/integration inspection."""

        return self._engine

    def __enter__(self) -> SqliteTaskExecutionLifecycleDecisionRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._engine.dispose()

    def add(self, record: object) -> None:
        """Append exactly one validated V1.55 record."""

        if not isinstance(record, TaskExecutionLifecycleDecisionRecord):
            raise TypeError(
                "record must be a genuine V1.55 "
                "TaskExecutionLifecycleDecisionRecord instance, "
                f"got {type(record).__name__}"
            )

        # Fresh COMPLETE strict revalidation of the full V1.55 record before
        # any database interaction. Only the retained fresh copy is used
        # afterwards. A semantically invalid embedded V1.50 decision (for
        # example a bypassed-validator payload, or COMPLETE_TASK over a
        # failed execution) raises here, before any row is written.
        validated = TaskExecutionLifecycleDecisionRecord.model_validate(
            record.model_dump(mode="python"),
            strict=True,
        )

        decision = validated.decision

        stored_decision_record_id = _to_text(validated.decision_record_id)

        session = Session(self._engine)
        try:
            session.execute(
                insert(Row).values(
                    decision_record_id=stored_decision_record_id,
                    portfolio_id=_to_text(decision.portfolio_id),
                    lifecycle_decision_id=_to_text(
                        decision.lifecycle_decision_id
                    ),
                    execution_record_id=_to_text(decision.execution_record_id),
                    authorized_task_id=_to_text(decision.authorized_task_id),
                    disposition=decision.disposition.value,
                    decided_at=_to_iso(decision.decided_at),
                    recorded_at=_to_iso(validated.recorded_at),
                    decision_snapshot=decision.model_dump_json(),
                )
            )
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if _is_duplicate_decision_record_id_violation(exc):
                raise (
                    DuplicateTaskExecutionLifecycleDecisionRecordError(
                        "durable TASK execution lifecycle decision "
                        "record already exists: "
                        f"{stored_decision_record_id}"
                    )
                ) from exc
            raise
        finally:
            session.close()

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleDecisionRecord, ...]:
        """Return exact history for one portfolio in deterministic order."""

        # Strict portfolio scope identity gate: the scope MUST already be a
        # genuine UUID instance. Stringified UUIDs or foreign scalars are
        # rejected before ANY session / SQL interaction.
        if not isinstance(portfolio_id, UUID):
            raise TypeError(
                "portfolio_id must already be a genuine UUID instance, "
                f"got {type(portfolio_id).__name__}"
            )

        stored_portfolio_id = _to_text(portfolio_id)

        with Session(self._engine) as session:
            rows = tuple(
                session.scalars(
                    select(Row).where(Row.portfolio_id == stored_portfolio_id)
                ).all()
            )

        records = [
            _row_to_record(row, portfolio_id)
            for row in rows
        ]
        records.sort(key=_record_sort_key)
        return tuple(records)
