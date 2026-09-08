"""V1.49 durable append-only history for exact V1.48 execution results.

V1.49 makes one already-produced V1.48 ``TaskExecutionResult`` durable. It
never re-executes a TASK, recomputes CURRENT state, mutates lifecycle state,
or introduces execution idempotency.

The durable record follows the established V1.35/V1.43 pattern:

- caller-supplied UUID record identity;
- caller-supplied timezone-aware timestamp;
- exact upstream immutable result as the sole semantic authority;
- fresh COMPLETE strict revalidation before repository interaction;
- append-only structural repository boundary;
- repository failures propagate unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution import (  # noqa: E501
    TaskExecutionResult,
)

__all__ = [
    "DurableTaskExecutionResultError",
    "TaskExecutionResultRecord",
    "TaskExecutionResultRepository",
    "record_task_execution_result_durably",
]


class DurableTaskExecutionResultError(ValueError):
    """Raised when a V1.49 durable execution record input is invalid."""


class TaskExecutionResultRecord(BaseModel):
    """One immutable durable record of an exact V1.48 execution result."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    execution_record_id: UUID
    recorded_at: datetime
    result: TaskExecutionResult

    @model_validator(mode="after")
    def _enforce_record_invariants(self) -> TaskExecutionResultRecord:
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError(
                "recorded_at must be a timezone-aware datetime with a non-None UTC offset"
            )
        TaskExecutionResult.model_validate(
            self.result.model_dump(mode="python"),
            strict=True,
        )
        return self


class TaskExecutionResultRepository(Protocol):
    """Technology-agnostic append-only durable execution history boundary."""

    def add(self, record: TaskExecutionResultRecord) -> None:
        """Append exactly one immutable execution result record."""
        ...

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionResultRecord, ...]:
        """Return exact durable history for one portfolio."""
        ...


def record_task_execution_result_durably(
    execution_record_id: object,
    recorded_at: object,
    result: object,
    *,
    repository: TaskExecutionResultRepository,
) -> TaskExecutionResultRecord:
    """Append one already-produced V1.48 result exactly once to ``repository``.

    Every validation failure occurs before repository interaction. The supplied
    result is freshly strict-revalidated and only the retained fresh copy is
    used after that point. No clock or UUID generation occurs here.
    """

    if not isinstance(execution_record_id, UUID):
        raise DurableTaskExecutionResultError(
            "execution_record_id must already be a UUID instance, "
            f"got {type(execution_record_id).__name__}"
        )
    if not isinstance(recorded_at, datetime):
        raise DurableTaskExecutionResultError(
            "recorded_at must already be a datetime instance, "
            f"got {type(recorded_at).__name__}"
        )
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise DurableTaskExecutionResultError(
            "recorded_at must be a timezone-aware datetime with a non-None UTC offset"
        )
    if not isinstance(result, TaskExecutionResult):
        raise DurableTaskExecutionResultError(
            "result must be a genuine V1.48 TaskExecutionResult instance, "
            f"got {type(result).__name__}"
        )

    try:
        fresh = TaskExecutionResult.model_validate(
            result.model_dump(mode="python"),
            strict=True,
        )
    except ValidationError as exc:
        raise DurableTaskExecutionResultError(
            "result did not survive fresh COMPLETE strict re-validation as a genuine V1.48 TaskExecutionResult"
        ) from exc

    record = TaskExecutionResultRecord(
        execution_record_id=execution_record_id,
        recorded_at=recorded_at,
        result=fresh,
    )

    repository.add(record)
    return record
