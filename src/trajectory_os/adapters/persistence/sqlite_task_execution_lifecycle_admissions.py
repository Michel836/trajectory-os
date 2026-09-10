"""SQLite append-only persistence for durable V1.51 lifecycle admission
history.

The adapter persists exact, already-produced V1.58
``TaskExecutionLifecycleAdmissionRecord`` records, each of which embeds one
genuine V1.51 CURRENT-state ``TaskExecutionLifecycleAdmission``.

Storage is append-only:

- ``admission_record_id`` is the sole durable-record primary-key authority;
- repeated ``lifecycle_decision_id`` / ``execution_record_id`` / task /
  disposition / status / timestamp values remain legal under distinct
  record ids;
- caller-supplied aware ``recorded_at`` and V1.51 ``decided_at`` offsets
  are preserved exactly;
- the exact embedded V1.51 admission is stored as explicit deterministic
  JSON;
- dedicated scalar columns (portfolio, ids, disposition,
  current_task_status, decided_at) make corruption directly visible;
- no update, replace, upsert, save, delete, current, latest, or effective
  admission-API exists.

Read-back reconstructs a genuine fresh V1.58
``TaskExecutionLifecycleAdmissionRecord`` through normal Pydantic
validation (the record model itself freshly strict-revalidates the embedded
V1.51 admission and re-checks the V1.51 admission invariants) and
cross-checks every duplicated scalar column against that snapshot. Corrupt
rows are rejected rather than normalized. There is no competing V1.51/V1.58
lifecycle validator here.

The embedded ``current_task_status`` is HISTORICAL admission evidence only.
It is persisted and read back exactly, but it is never interpreted as
current, effective, validity-carrying, or otherwise later-authoritative
task state of any kind.

History ordering is by the true aware datetime instant of the outer
``recorded_at`` and then by ``admission_record_id.int``.
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
    TaskExecutionLifecycleAdmissionRecordRow as Row,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_admission import (  # noqa: E501
    TaskExecutionLifecycleAdmission,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_admission_history import (  # noqa: E501
    TaskExecutionLifecycleAdmissionRecord,
)
from trajectory_os.domain.entities import EntityStatus


class DuplicateTaskExecutionLifecycleAdmissionRecordError(ValueError):
    """Raised when admission_record_id already exists durably."""


_DUPLICATE_ADMISSION_RECORD_ID_MESSAGE = (
    "UNIQUE constraint failed: "
    "task_execution_lifecycle_admission_records.admission_record_id"
)


def _is_duplicate_admission_record_id_violation(exc: IntegrityError) -> bool:
    """Return True only for this table's admission_record_id PK violation."""

    orig = exc.orig
    if not isinstance(orig, sqlite3.IntegrityError):
        return False
    return _DUPLICATE_ADMISSION_RECORD_ID_MESSAGE in str(orig)


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
            "stored lifecycle admission timestamp must be an aware "
            f"ISO-8601 datetime with a non-None UTC offset, got {value!r}"
        )
    return parsed


def _row_to_record(
    row: Any,
    expected_portfolio_id: UUID,
) -> TaskExecutionLifecycleAdmissionRecord:
    """Strictly reconstruct and cross-check one durable V1.51 admission
    row."""

    admission = TaskExecutionLifecycleAdmission.model_validate_json(
        row.admission_snapshot
    )

    # Normal reconstruction through the V1.58 record model freshly
    # strict-revalidates the embedded V1.51 admission — so the outer
    # record's own invariants rerun before the row is ever accepted.
    # Never model-constructed.
    record = TaskExecutionLifecycleAdmissionRecord(
        admission_record_id=UUID(row.admission_record_id),
        recorded_at=_from_iso(row.recorded_at),
        admission=admission,
    )

    mismatches: list[str] = []

    expected = {
        "portfolio_id": _to_text(admission.portfolio_id),
        "lifecycle_decision_id": _to_text(admission.lifecycle_decision_id),
        "execution_record_id": _to_text(admission.execution_record_id),
        "authorized_task_id": _to_text(admission.authorized_task_id),
        "disposition": admission.disposition.value,
        "current_task_status": admission.current_task_status.value,
        "decided_at": _to_iso(admission.decided_at),
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
            "stored V1.51 lifecycle admission row disagrees with its "
            f"exact embedded V1.51 snapshot: {details}"
        )

    return record


def _record_sort_key(
    record: TaskExecutionLifecycleAdmissionRecord,
) -> tuple[datetime, int]:
    # True aware datetime instant first — never lexicographic ISO text,
    # because different offsets may represent equal or different real
    # instants — then the record identity's integer form.
    return record.recorded_at, record.admission_record_id.int


class SqliteTaskExecutionLifecycleAdmissionRepository:
    """Append-only SQLite repository for exact V1.51 lifecycle admission
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
        SqliteTaskExecutionLifecycleAdmissionRepository
    ):
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._engine.dispose()

    def add(self, record: object) -> None:
        """Append exactly one validated V1.51 admission record."""

        if not isinstance(record, TaskExecutionLifecycleAdmissionRecord):
            raise TypeError(
                "record must be a genuine V1.58 "
                "TaskExecutionLifecycleAdmissionRecord instance, "
                f"got {type(record).__name__}"
            )

        # Fresh COMPLETE strict revalidation of the full V1.58 record before
        # any database interaction. Only the retained fresh copy is used
        # afterwards. A semantically invalid embedded V1.51 admission (for
        # example a bypassed-validator payload, an admission over a failed
        # execution, or an already-COMPLETED current_task_status) raises
        # here, before any row is written.
        validated = TaskExecutionLifecycleAdmissionRecord.model_validate(
            record.model_dump(mode="python"),
            strict=True,
        )

        admission = validated.admission

        stored_admission_record_id = _to_text(validated.admission_record_id)

        session = Session(self._engine)
        try:
            session.execute(
                insert(Row).values(
                    admission_record_id=stored_admission_record_id,
                    portfolio_id=_to_text(admission.portfolio_id),
                    lifecycle_decision_id=_to_text(
                        admission.lifecycle_decision_id
                    ),
                    execution_record_id=_to_text(admission.execution_record_id),
                    authorized_task_id=_to_text(admission.authorized_task_id),
                    disposition=admission.disposition.value,
                    current_task_status=_status_value(admission.current_task_status),  # noqa: E501
                    decided_at=_to_iso(admission.decided_at),
                    recorded_at=_to_iso(validated.recorded_at),
                    admission_snapshot=admission.model_dump_json(),
                )
            )
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if _is_duplicate_admission_record_id_violation(exc):
                raise (
                    DuplicateTaskExecutionLifecycleAdmissionRecordError(
                        "durable TASK execution lifecycle admission "
                        "record already exists: "
                        f"{stored_admission_record_id}"
                    )
                ) from exc
            raise
        finally:
            session.close()

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleAdmissionRecord, ...]:
        """Return exact history for one portfolio in deterministic order.

        Exact historical access only: no latest/effective/validity
        inference is ever derived from the history, and no task state of
        any kind is ever inferred from the embedded historical
        ``current_task_status`` evidence.
        """

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
