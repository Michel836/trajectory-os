"""SQLite persistence for durable V1.53 completed lifecycle application
history.

The adapter persists exact, already-produced V1.53
``TaskExecutionLifecycleApplicationRecord`` records, each of which embeds one
already-completed V1.52 ``EntityStatusTransitionResult``.

Storage is append-only:

- ``application_record_id`` is the sole durable-record primary-key authority;
- repeated entity/task/result values remain legal under distinct record ids;
- caller-supplied aware ``recorded_at`` offsets are preserved exactly;
- the exact embedded result is stored as explicit deterministic JSON;
- dedicated scalar columns (entity, statuses, changed_at) make corruption
  directly visible;
- no update, replace, upsert, save, delete, current, latest, or effective
  lifecycle-API exists.

Read-back reconstructs a genuine fresh V1.53
``TaskExecutionLifecycleApplicationRecord`` through normal Pydantic validation
(the record model itself revalidates the embedded V1.52 result and re-checks
the provable V1.52 completion invariants) and cross-checks every duplicated
scalar column against that snapshot. Corrupt rows are rejected rather than
normalized. There is no competing V1.53 lifecycle validator here.

History ordering is by true aware datetime instant and then by
``application_record_id.int``.
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
    TaskExecutionLifecycleApplicationRecordRow as Row,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_application_persistence import (  # noqa: E501
    TaskExecutionLifecycleApplicationRecord,
)
from trajectory_os.domain.entities import EntityStatus
from trajectory_os.domain.entity_status_transition import (
    EntityStatusTransitionResult,
)


class DuplicateTaskExecutionLifecycleApplicationRecordError(ValueError):
    """Raised when application_record_id already exists durably."""


_DUPLICATE_APPLICATION_RECORD_ID_MESSAGE = (
    "UNIQUE constraint failed: "
    "task_execution_lifecycle_application_records.application_record_id"
)


def _is_duplicate_application_record_id_violation(
    exc: IntegrityError,
) -> bool:
    """Return True only for this table's application_record_id PK
    violation."""

    orig = exc.orig
    if not isinstance(orig, sqlite3.IntegrityError):
        return False
    return _DUPLICATE_APPLICATION_RECORD_ID_MESSAGE in str(orig)


def _to_text(value: UUID) -> str:
    return str(value)


def _status_value(status: EntityStatus) -> str:
    return status.value


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
            "stored lifecycle application timestamp must be an aware "
            f"ISO-8601 datetime with a non-None UTC offset, got {value!r}"
        )
    return parsed


def _row_to_record(
    row: Any,
    expected_portfolio_id: UUID,
) -> TaskExecutionLifecycleApplicationRecord:
    """Strictly reconstruct and cross-check one durable V1.53 row."""

    result = EntityStatusTransitionResult.model_validate_json(
        row.result_snapshot
    )

    # Normal reconstruction through the V1.53 record model freshly
    # strict-revalidates the embedded result and re-checks the provable
    # V1.52 completion invariants. Never model-constructed.
    record = TaskExecutionLifecycleApplicationRecord(
        application_record_id=UUID(row.application_record_id),
        recorded_at=_from_iso(row.recorded_at),
        result=result,
    )

    mismatches: list[str] = []

    expected = {
        "portfolio_id": _to_text(result.portfolio.id),
        "entity_id": _to_text(result.entity_id),
        "previous_status": _status_value(result.previous_status),
        "new_status": _status_value(result.new_status),
        "changed_at": _to_iso(result.changed_at),
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
            "stored V1.53 lifecycle application row disagrees with its "
            f"exact embedded V1.52 snapshot: {details}"
        )

    return record


def _record_sort_key(
    record: TaskExecutionLifecycleApplicationRecord,
) -> tuple[datetime, int]:
    return record.recorded_at, record.application_record_id.int


class SqliteTaskExecutionLifecycleApplicationRepository:
    """Append-only SQLite repository for exact V1.53 lifecycle application
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

    def __enter__(self) -> (
        SqliteTaskExecutionLifecycleApplicationRepository
    ):
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._engine.dispose()

    def add(self, record: object) -> None:
        """Append exactly one validated V1.53 record."""

        if not isinstance(record, TaskExecutionLifecycleApplicationRecord):
            raise TypeError(
                "record must be a genuine V1.53 "
                "TaskExecutionLifecycleApplicationRecord instance, "
                f"got {type(record).__name__}"
            )

        # Fresh COMPLETE strict revalidation of the full V1.53 record before
        # any database interaction. Only the retained fresh copy is used
        # afterwards. A semantically invalid embedded result raises here,
        # before any row is written.
        validated = TaskExecutionLifecycleApplicationRecord.model_validate(
            record.model_dump(mode="python"),
            strict=True,
        )

        result = validated.result

        stored_application_record_id = _to_text(
            validated.application_record_id
        )

        session = Session(self._engine)
        try:
            session.execute(
                insert(Row).values(
                    application_record_id=stored_application_record_id,
                    portfolio_id=_to_text(result.portfolio.id),
                    entity_id=_to_text(result.entity_id),
                    previous_status=_status_value(result.previous_status),
                    new_status=_status_value(result.new_status),
                    changed_at=_to_iso(result.changed_at),
                    recorded_at=_to_iso(validated.recorded_at),
                    result_snapshot=result.model_dump_json(),
                )
            )
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if _is_duplicate_application_record_id_violation(exc):
                raise (
                    DuplicateTaskExecutionLifecycleApplicationRecordError(
                        "durable TASK execution lifecycle application "
                        "record already exists: "
                        f"{stored_application_record_id}"
                    )
                ) from exc
            raise
        finally:
            session.close()

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleApplicationRecord, ...]:
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
