"""SQLite persistence for durable V1.49 TASK execution result history.

The adapter persists exact, already-produced V1.48 ``TaskExecutionResult``
values inside immutable V1.49 ``TaskExecutionResultRecord`` records.

Storage is append-only:

- ``execution_record_id`` is the sole durable-record primary-key authority;
- repeated request/task/result values remain legal under distinct record ids;
- caller-supplied aware ``recorded_at`` offsets are preserved exactly;
- the exact V1.48 result is stored as explicit deterministic JSON;
- dedicated scalar columns make corruption directly visible;
- no update, replace, upsert, save, delete, current, latest, or effective
  execution API exists.

Read-back reconstructs a genuine fresh V1.48 ``TaskExecutionResult`` through
normal Pydantic validation and cross-checks every duplicated scalar column
against that snapshot. Corrupt rows are rejected rather than normalized.

History ordering is by true aware datetime instant and then by
``execution_record_id.int``.
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
    TaskExecutionResultRecordRow as Row,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution import (  # noqa: E501
    TaskExecutionResult,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_persistence import (  # noqa: E501
    TaskExecutionResultRecord,
)


class DuplicateTaskExecutionResultRecordError(ValueError):
    """Raised when execution_record_id already exists durably."""


_DUPLICATE_EXECUTION_RECORD_ID_MESSAGE = (
    "UNIQUE constraint failed: "
    "task_execution_result_records.execution_record_id"
)


def _is_duplicate_execution_record_id_violation(
    exc: IntegrityError,
) -> bool:
    """Return True only for this table's execution_record_id PK violation."""

    orig = exc.orig
    if not isinstance(orig, sqlite3.IntegrityError):
        return False
    return _DUPLICATE_EXECUTION_RECORD_ID_MESSAGE in str(orig)


def _to_text(value: UUID) -> str:
    return str(value)


def _to_domain_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _row_to_record(
    row: Any,
    expected_portfolio_id: UUID,
) -> TaskExecutionResultRecord:
    """Strictly reconstruct and cross-check one durable V1.49 row."""

    result = TaskExecutionResult.model_validate_json(row.result_snapshot)

    mismatches: list[str] = []

    expected = {
        "portfolio_id": _to_text(result.portfolio_id),
        "request_id": _to_text(result.request_id),
        "intent_id": _to_text(result.intent_id),
        "decision_id": _to_text(result.decision_id),
        "authorized_project_id": _to_text(result.authorized_project_id),
        "authorized_task_id": _to_text(result.authorized_task_id),
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

    if row.succeeded not in (0, 1):
        raise ValueError(
            "stored V1.49 execution result row has invalid succeeded "
            f"representation {row.succeeded!r}; expected exactly 0 or 1"
        )

    if bool(row.succeeded) is not result.succeeded:
        mismatches.append(
            f"succeeded: stored={row.succeeded!r}, "
            f"snapshot={result.succeeded!r}"
        )

    if mismatches:
        details = "; ".join(mismatches)
        raise ValueError(
            "stored V1.49 execution result row disagrees with its exact "
            f"V1.48 snapshot: {details}"
        )

    return TaskExecutionResultRecord(
        execution_record_id=UUID(row.execution_record_id),
        recorded_at=_to_domain_datetime(row.recorded_at),
        result=result,
    )


def _record_sort_key(
    record: TaskExecutionResultRecord,
) -> tuple[datetime, int]:
    return record.recorded_at, record.execution_record_id.int


class SqliteTaskExecutionResultRepository:
    """Append-only SQLite repository for exact V1.49 execution history."""

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

    def __enter__(self) -> SqliteTaskExecutionResultRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._engine.dispose()

    def add(self, record: object) -> None:
        """Append exactly one validated V1.49 record."""

        if not isinstance(record, TaskExecutionResultRecord):
            raise TypeError(
                "record must be a genuine V1.49 "
                "TaskExecutionResultRecord instance, "
                f"got {type(record).__name__}"
            )

        validated = TaskExecutionResultRecord.model_validate(
            record.model_dump(mode="python"),
            strict=True,
        )

        stored_execution_record_id = _to_text(
            validated.execution_record_id
        )
        result = validated.result

        session = Session(self._engine)
        try:
            session.execute(
                insert(Row).values(
                    execution_record_id=stored_execution_record_id,
                    portfolio_id=_to_text(result.portfolio_id),
                    request_id=_to_text(result.request_id),
                    intent_id=_to_text(result.intent_id),
                    decision_id=_to_text(result.decision_id),
                    authorized_project_id=_to_text(
                        result.authorized_project_id
                    ),
                    authorized_task_id=_to_text(
                        result.authorized_task_id
                    ),
                    succeeded=int(result.succeeded),
                    recorded_at=validated.recorded_at.isoformat(),
                    result_snapshot=result.model_dump_json(),
                )
            )
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if _is_duplicate_execution_record_id_violation(exc):
                raise DuplicateTaskExecutionResultRecordError(
                    "durable TASK execution result record already exists: "
                    f"{stored_execution_record_id}"
                ) from exc
            raise
        finally:
            session.close()

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionResultRecord, ...]:
        """Return exact history for one portfolio in deterministic order."""

        stored_portfolio_id = _to_text(portfolio_id)

        with Session(self._engine) as session:
            rows = tuple(
                session.scalars(
                    select(Row).where(
                        Row.portfolio_id == stored_portfolio_id
                    )
                ).all()
            )

        records = [
            _row_to_record(row, portfolio_id)
            for row in rows
        ]
        records.sort(key=_record_sort_key)
        return tuple(records)
