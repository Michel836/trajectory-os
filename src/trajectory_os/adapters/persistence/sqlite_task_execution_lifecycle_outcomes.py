"""SQLite append-only persistence for durable V1.61 lifecycle coordination
outcome history.

The adapter persists exact, already-produced V1.61
``TaskExecutionLifecycleOutcomeRecord`` records, each of which embeds one
genuine V1.60 ``TaskExecutionLifecycleOutcome``.

V1.61 remains the sole application-layer semantic authority. This adapter
is STORAGE ONLY: it never re-coordinates, re-admits, re-decides, or
re-applies any lifecycle step, never touches CURRENT Portfolio state, and
never introduces latest/effective/idempotency/retry semantics.

Storage is append-only:

- ``outcome_record_id`` is the sole durable-record primary-key authority;
- two value-equivalent V1.61 records under distinct ``outcome_record_id``
  values are legal separate rows — nothing is deduplicated by portfolio,
  task, decision, execution, admission, application, timestamps, outcome
  equality, or transition result equality;
- caller-supplied aware ``recorded_at`` and every nested datetime offset
  are preserved exactly (never normalized to UTC);
- the exact V1.60 outcome is stored as deterministic explicit JSON;
- dedicated scalar columns (portfolio, the four record ids, admission
  identity ids, transition entity/changed_at) make corruption directly
  visible;
- no update, replace, upsert, save, delete, current, latest, or
  effective outcome-API exists.

Every embedded value is HISTORICAL EVIDENCE ONLY: the historical admission
``current_task_status`` and the embedded transition result prove only what
was produced and applied at their exact historical moments and authorize
nothing later.

Read-back parses the snapshot through canonical
``TaskExecutionLifecycleOutcome``, constructs a normal V1.61 record (which
freshly strict-revalidates the outcome), and cross-checks every duplicated
scalar column against that canonical snapshot. Corrupt rows are rejected
rather than normalized or silently repaired.

History ordering is by the true aware datetime instant of the outer
``recorded_at`` and then by ``outcome_record_id.int``.
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
    TaskExecutionLifecycleOutcomeRecordRow as Row,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_coordination import (  # noqa: E501
    TaskExecutionLifecycleOutcome,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_outcome_history import (  # noqa: E501
    TaskExecutionLifecycleOutcomeRecord,
)


class DuplicateTaskExecutionLifecycleOutcomeRecordError(ValueError):
    """Raised when outcome_record_id already exists durably.

    This error represents ONLY duplication of the exact table primary key
    ``outcome_record_id``. Unrelated integrity failures never raise this
    error: they propagate unchanged."""


_DUPLICATE_OUTCOME_RECORD_ID_MESSAGE = (
    "UNIQUE constraint failed: "
    "task_execution_lifecycle_outcome_records.outcome_record_id"
)


def _is_duplicate_outcome_record_id_violation(exc: IntegrityError) -> bool:
    """Return True only for this table's outcome_record_id PK violation."""

    orig = exc.orig
    if not isinstance(orig, sqlite3.IntegrityError):
        return False
    return _DUPLICATE_OUTCOME_RECORD_ID_MESSAGE in str(orig)


def _to_text(value: UUID) -> str:
    return str(value)


def _to_iso(value: datetime) -> str:
    return value.isoformat()


def _from_iso(value: str) -> datetime:
    """Strictly reconstruct an aware ISO-8601 timestamp.

    A naive (offset-less) stored timestamp is corruption and is rejected
    rather than silently normalized to an aware value.
    """

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "stored lifecycle outcome timestamp must be an aware "
            f"ISO-8601 datetime with a non-None UTC offset, got {value!r}"
        )
    return parsed


def _row_to_record(
    row: Any,
    expected_portfolio_id: UUID,
) -> TaskExecutionLifecycleOutcomeRecord:
    """Strictly reconstruct and cross-check one durable V1.61 row."""

    outcome = TaskExecutionLifecycleOutcome.model_validate_json(
        row.outcome_snapshot
    )

    # Normal reconstruction through the V1.61 record model freshly
    # strict-revalidates the embedded V1.60 outcome (and thereby every
    # nested V1.58/V1.55/V1.52/V1.53 record) — so the record's own
    # invariants rerun before the row is ever accepted. Never
    # model-constructed.
    record = TaskExecutionLifecycleOutcomeRecord(
        outcome_record_id=UUID(row.outcome_record_id),
        recorded_at=_from_iso(row.recorded_at),
        outcome=outcome,
    )

    admission = outcome.admission_record.admission

    expected = {
        "portfolio_id": _to_text(admission.portfolio_id),
        "admission_record_id": _to_text(
            outcome.admission_record.admission_record_id
        ),
        "decision_record_id": _to_text(
            outcome.decision_record.decision_record_id
        ),
        "application_record_id": _to_text(
            outcome.application_record.application_record_id
        ),
        "lifecycle_decision_id": _to_text(admission.lifecycle_decision_id),
        "execution_record_id": _to_text(admission.execution_record_id),
        "authorized_task_id": _to_text(admission.authorized_task_id),
        "transition_entity_id": _to_text(
            outcome.transition_result.entity_id
        ),
        "transition_changed_at": _to_iso(
            outcome.transition_result.changed_at
        ),
    }

    mismatches: list[str] = []

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
            "stored V1.61 lifecycle outcome row disagrees with its "
            f"exact embedded V1.60 snapshot: {details}"
        )

    return record


def _record_sort_key(
    record: TaskExecutionLifecycleOutcomeRecord,
) -> tuple[datetime, int]:
    # True aware instant first (timezone-aware datetimes compare by
    # instant, never by stored ISO text), then outcome_record_id.int.
    return record.recorded_at, record.outcome_record_id.int


class SqliteTaskExecutionLifecycleOutcomeRepository:
    """Append-only SQLite repository for exact V1.61 lifecycle outcome
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

    def __enter__(self) -> SqliteTaskExecutionLifecycleOutcomeRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._engine.dispose()

    def add(self, record: object) -> None:
        """Append exactly one validated V1.61 record, and stop.

        No portfolio or CURRENT state is ever queried: the storage scope
        is derived from the canonical freshly validated record itself.
        """

        if not isinstance(record, TaskExecutionLifecycleOutcomeRecord):
            raise TypeError(
                "record must be a genuine V1.61 "
                "TaskExecutionLifecycleOutcomeRecord instance, "
                f"got {type(record).__name__}"
            )

        # Fresh COMPLETE strict revalidation of the full V1.61 record
        # (and thereby of the embedded V1.60 outcome and every nested
        # V1.58/V1.55/V1.52/V1.53 record) BEFORE any database interaction.
        # Only the retained fresh canonical copy is used afterwards. A
        # semantically invalid bypassed-validator payload is rejected
        # here, before any row is written.
        validated = TaskExecutionLifecycleOutcomeRecord.model_validate(
            record.model_dump(mode="python"),
            strict=True,
        )

        outcome = validated.outcome
        admission = outcome.admission_record.admission

        stored_outcome_record_id = _to_text(validated.outcome_record_id)

        session = Session(self._engine)
        try:
            session.execute(
                insert(Row).values(
                    outcome_record_id=stored_outcome_record_id,
                    portfolio_id=_to_text(admission.portfolio_id),
                    admission_record_id=_to_text(
                        outcome.admission_record.admission_record_id
                    ),
                    decision_record_id=_to_text(
                        outcome.decision_record.decision_record_id
                    ),
                    application_record_id=_to_text(
                        outcome.application_record.application_record_id
                    ),
                    lifecycle_decision_id=_to_text(
                        admission.lifecycle_decision_id
                    ),
                    execution_record_id=_to_text(admission.execution_record_id),
                    authorized_task_id=_to_text(admission.authorized_task_id),
                    transition_entity_id=_to_text(
                        outcome.transition_result.entity_id
                    ),
                    transition_changed_at=_to_iso(
                        outcome.transition_result.changed_at
                    ),
                    recorded_at=_to_iso(validated.recorded_at),
                    outcome_snapshot=outcome.model_dump_json(),
                )
            )
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if _is_duplicate_outcome_record_id_violation(exc):
                raise (
                    DuplicateTaskExecutionLifecycleOutcomeRecordError(
                        "durable TASK execution lifecycle outcome "
                        "record already exists: "
                        f"{stored_outcome_record_id}"
                    )
                ) from exc
            raise
        finally:
            session.close()

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleOutcomeRecord, ...]:
        """Return exact history for one portfolio in deterministic order.

        Exact historical access only: nothing here infers CURRENT
        task/portfolio state from the history.
        """

        # Strict portfolio scope identity gate: the scope MUST already be
        # a genuine UUID instance. Stringified UUIDs or foreign scalars
        # are rejected before ANY session / SQL interaction.
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

        records = [_row_to_record(row, portfolio_id) for row in rows]
        records.sort(key=_record_sort_key)
        return tuple(records)
