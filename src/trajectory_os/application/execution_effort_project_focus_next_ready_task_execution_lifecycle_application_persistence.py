"""V1.53 — durable append-only history for completed lifecycle applications.

V1.53 is OBSERVATIONAL ONLY: it makes one already-produced V1.52
``EntityStatusTransitionResult`` (the exact result of one durable, admitted
COMPLETE_TASK lifecycle application) durable as immutable append-only
history. It never re-executes, replays, or re-validates the V1.52
application, never mutates lifecycle state, and never claims
transactionality, exactly-once semantics, idempotency, or crash
consistency with V1.52.

Canonical authority chain:

    V1.49 durable execution result
    -> V1.50 explicit human lifecycle disposition
    -> V1.51 CURRENT-state admission of COMPLETE_TASK
    -> V1.52 durable application to COMPLETED
    -> V1.53 durable append-only history of that exact application  <-- this module

The durable record follows the established V1.35/V1.43/V1.49 pattern:

- caller-supplied UUID record identity;
- caller-supplied timezone-aware timestamp;
- exact upstream immutable result as the sole semantic authority;
- fresh COMPLETE strict revalidation before repository interaction;
- validation of ONLY the invariants the V1.52 result itself proves;
- append-only structural repository boundary;
- repository failures propagate unchanged.

SOLE UPSTREAM AUTHORITY: one genuine ``EntityStatusTransitionResult``
already returned by V1.52. Nothing is reconstructed: no V1.50 decision,
no V1.51 admission, no execution record, no project relation, no
invented provenance. The fresh revalidated result is embedded verbatim.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from trajectory_os.domain.entities import EntityStatus, EntityType
from trajectory_os.domain.entity_status_transition import EntityStatusTransitionResult

__all__ = [
    "DurableTaskExecutionLifecycleApplicationRecordError",
    "TaskExecutionLifecycleApplicationRecord",
    "TaskExecutionLifecycleApplicationRepository",
    "record_task_execution_lifecycle_application_durably",
]


class DurableTaskExecutionLifecycleApplicationRecordError(ValueError):
    """Raised when a V1.53 durable lifecycle application record input is
    invalid."""


class TaskExecutionLifecycleApplicationRecord(BaseModel):
    """One immutable durable record of an exact V1.52 completed lifecycle
    application result.

    The embedded result must itself be a genuine V1.52 completed-application
    result. Construction freshly strict-revalidates the embedded result and
    enforces the same V1.52-compatible completion invariants as the public
    durable boundary — so a semantically invalid result (for example a
    genuine CANCELLED transition) can never exist inside this public record,
    whether it arrives via the durable boundary or direct construction.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    application_record_id: UUID
    recorded_at: datetime
    result: EntityStatusTransitionResult

    @model_validator(mode="after")
    def _enforce_record_invariants(self) -> TaskExecutionLifecycleApplicationRecord:
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError(
                "recorded_at must be a timezone-aware datetime with a non-None UTC offset"
            )
        fresh = _freshly_revalidate_v152_completion_result(self.result)
        object.__setattr__(self, "result", fresh)
        return self


class TaskExecutionLifecycleApplicationRepository(Protocol):
    """Technology-agnostic append-only durable lifecycle application
    history boundary."""

    def add(self, record: TaskExecutionLifecycleApplicationRecord) -> None:
        """Append exactly one immutable lifecycle application record."""
        ...

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleApplicationRecord, ...]:
        """Return exact durable history for one portfolio."""
        ...


def _freshly_revalidate_v152_completion_result(
    result: EntityStatusTransitionResult,
) -> EntityStatusTransitionResult:
    """One canonical entry point: fresh COMPLETE strict revalidation of the
    embedded result, followed by the V1.52-compatible completion invariants.

    Reused by the record model and the public durable boundary so the
    semantic rules are never duplicated inconsistently. Only the returned
    fresh copy may be used for semantic reads afterwards; the caller-owned
    result is never read semantically here.
    """

    try:
        fresh = EntityStatusTransitionResult.model_validate(
            result.model_dump(mode="python"),
            strict=True,
        )
    except ValidationError as exc:
        raise DurableTaskExecutionLifecycleApplicationRecordError(
            "result did not survive fresh COMPLETE strict re-validation "
            "as a genuine EntityStatusTransitionResult"
        ) from exc

    _validate_v152_completion_invariants(fresh)
    return fresh


def _validate_v152_completion_invariants(
    fresh: EntityStatusTransitionResult,
) -> None:
    """Validate ONLY the invariants the fresh V1.52 result itself proves.

    Nothing beyond the freshly revalidated result is read: no V1.50
    decision, no V1.51 admission, no execution record, no project
    relation, no inferred lifecycle provenance.
    """

    if fresh.new_status is not EntityStatus.COMPLETED:
        raise DurableTaskExecutionLifecycleApplicationRecordError(
            "a completed V1.52 lifecycle application result requires "
            f"new_status to be exactly EntityStatus.COMPLETED, "
            f"got {fresh.new_status!r}"
        )

    entity = fresh.portfolio.get_entity(fresh.entity_id)
    if entity is None:
        raise DurableTaskExecutionLifecycleApplicationRecordError(
            "result.entity_id does not resolve to an entity inside "
            f"result.portfolio: {fresh.entity_id}"
        )

    if entity.entity_type is not EntityType.TASK:
        raise DurableTaskExecutionLifecycleApplicationRecordError(
            "the exact target entity must be a TASK, "
            f"got {entity.entity_type!r}"
        )

    if entity.status is not EntityStatus.COMPLETED:
        raise DurableTaskExecutionLifecycleApplicationRecordError(
            "the exact target TASK must itself be COMPLETED, "
            f"got {entity.status!r}"
        )

    if entity.updated_at != fresh.changed_at:
        raise DurableTaskExecutionLifecycleApplicationRecordError(
            "the exact target TASK updated_at must equal result.changed_at, "
            f"got {entity.updated_at!r} != {fresh.changed_at!r}"
        )


def record_task_execution_lifecycle_application_durably(
    application_record_id: object,
    recorded_at: object,
    result: object,
    *,
    repository: TaskExecutionLifecycleApplicationRepository,
) -> TaskExecutionLifecycleApplicationRecord:
    """Append one already-produced V1.52 completed lifecycle application
    result exactly once to ``repository``, and stop.

    The sequence is exact: genuine ``application_record_id`` UUID ->
    genuine timezone-aware ``recorded_at`` -> genuine
    ``EntityStatusTransitionResult`` -> fresh COMPLETE strict
    revalidation of the full result -> validation of ONLY the provable
    V1.52 completion invariants against the retained fresh copy ->
    immutable record construction -> ``repository.add(record)`` exactly
    once -> return the exact record -> stop.

    Every validation failure occurs before repository interaction. The
    supplied result is freshly strict-revalidated and only the retained
    fresh copy is used for every semantic read afterwards. The V1.52
    application itself is never called, never replayed, and no Portfolio
    is ever loaded or saved. No clock or UUID generation occurs here.
    Repeated valid calls append repeatedly; there is no dedupe,
    replacement, supersession, idempotency, or exactly-once semantics —
    plain append-only history.
    """

    if not isinstance(application_record_id, UUID):
        raise DurableTaskExecutionLifecycleApplicationRecordError(
            "application_record_id must already be a UUID instance, "
            f"got {type(application_record_id).__name__}"
        )
    if not isinstance(recorded_at, datetime):
        raise DurableTaskExecutionLifecycleApplicationRecordError(
            "recorded_at must already be a datetime instance, "
            f"got {type(recorded_at).__name__}"
        )
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise DurableTaskExecutionLifecycleApplicationRecordError(
            "recorded_at must be a timezone-aware datetime with a non-None UTC offset"
        )
    if not isinstance(result, EntityStatusTransitionResult):
        raise DurableTaskExecutionLifecycleApplicationRecordError(
            "result must be a genuine V1.52-compatible "
            "EntityStatusTransitionResult instance, "
            f"got {type(result).__name__}"
        )

    fresh = _freshly_revalidate_v152_completion_result(result)

    record = TaskExecutionLifecycleApplicationRecord(
        application_record_id=application_record_id,
        recorded_at=recorded_at,
        result=fresh,
    )

    repository.add(record)
    return record
