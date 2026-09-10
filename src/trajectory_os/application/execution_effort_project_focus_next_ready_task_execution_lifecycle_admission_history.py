"""V1.58 — durable append-only history for V1.51 lifecycle admissions.

V1.58 is OBSERVATIONAL ONLY: it makes one already-produced genuine V1.51
``TaskExecutionLifecycleAdmission`` durable as immutable append-only history.
It never re-creates, re-admits, re-derives, or re-interprets V1.51
semantics (it DOES freshly and strictly re-validate the supplied V1.51
admission before any repository interaction), never calls
``admit_current_task_execution_lifecycle``, never touches CURRENT
Portfolio/WBS/status, never calls V1.52, V1.53, V1.54, V1.55, or V1.56,
never transitions any lifecycle state, and never claims
transactionality, exactly-once semantics, idempotency, or crash
consistency with V1.51.

Canonical authority chain:

    V1.49 durable execution result
    -> V1.50 explicit human lifecycle disposition
    -> V1.51 CURRENT-state admission
    -> V1.58 durable append-only admission history   <-- this module
    -> V1.52 durable application to COMPLETED
    -> V1.53/V1.54 application history

The durable record follows the established V1.35/V1.43/V1.49/V1.53/V1.55
pattern:

- caller-supplied UUID record identity;
- caller-supplied timezone-aware timestamp;
- exact upstream immutable admission as the sole semantic authority;
- fresh COMPLETE strict revalidation before repository interaction;
- every validation failure before any repository interaction;
- append-only structural repository boundary;
- repository failures propagate unchanged.

SOLE UPSTREAM AUTHORITY: one genuine ``TaskExecutionLifecycleAdmission``
already produced by V1.51. Nothing is reconstructed: no V1.50 decision,
no Portfolio load, no CURRENT WBS/relations/status inspection, no replay,
no inferred provenance. The fresh revalidated admission is embedded
verbatim and ALL thirteen V1.51 fields (``lifecycle_decision_id``,
``decided_at``, ``execution_record_id``, ``execution_recorded_at``,
``request_id``, ``intent_id``, ``execution_decision_id``,
``portfolio_id``, ``authorized_project_id``, ``authorized_task_id``,
``execution_succeeded``, ``disposition``, ``current_task_status``) are
preserved exactly, including exact datetime offsets. No field is
normalized, derived, or reinterpreted.

HISTORICAL STATUS SEMANTICS: the embedded ``current_task_status`` is
HISTORICAL admission evidence only. Persisting it does not make it
authoritative for any later mutation; no CURRENT task state of any kind is
ever inferred from this history.

IDENTITY: ``admission_record_id`` is the durable outer record identity and
is distinct from ``lifecycle_decision_id``, ``execution_record_id``,
``request_id``, and every task/project/portfolio id. Nothing is
deduplicated by any semantic identity: repeated explicit calls with
distinct ``admission_record_id`` values remain separate appends. There is
no idempotency, exactly-once, latest, effective, supersession, or
revocation semantics — plain append-only history. ``list_history`` means
exactly: the exact durable admission history scoped to one portfolio.

NO PERSISTENCE BACKEND HERE: the repository is a structural Python
protocol only. Concrete durable storage (for example SQLite) and any
deterministic ordering mechanics (for example by true ``recorded_at``
instant then ``admission_record_id.int``) are intentionally reserved for
a later milestone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_admission import (  # noqa: E501
    TaskExecutionLifecycleAdmission,
)

__all__ = [
    "DurableTaskExecutionLifecycleAdmissionError",
    "TaskExecutionLifecycleAdmissionRecord",
    "TaskExecutionLifecycleAdmissionRepository",
    "record_task_execution_lifecycle_admission_durably",
]


class DurableTaskExecutionLifecycleAdmissionError(ValueError):
    """Raised when a V1.58 durable lifecycle admission record input is
    invalid."""


class TaskExecutionLifecycleAdmissionRecord(BaseModel):
    """One immutable durable record of an exact V1.51 CURRENT-state
    lifecycle admission.

    The embedded admission must itself be a genuine V1.51 admission.
    Construction freshly strict-revalidates the embedded admission and
    re-enforces the V1.51 model's own invariants — so an invalid or
    hostile V1.51-semantic state (for example a ``model_construct``
    payload, a naive ``decided_at`` inside the admission, an admission
    over a failed execution, or an already-COMPLETED
    ``current_task_status``) can never exist inside this public record,
    whether it arrives via the durable boundary or direct construction.

    The embedded ``current_task_status`` is HISTORICAL admission evidence
    only. It authorizes nothing beyond proving the decision was applicable
    in the state at the moment of admission.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    admission_record_id: UUID
    recorded_at: datetime
    admission: TaskExecutionLifecycleAdmission

    @model_validator(mode="after")
    def _enforce_record_invariants(
        self,
    ) -> TaskExecutionLifecycleAdmissionRecord:
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError(
                "recorded_at must be a timezone-aware datetime with a non-None UTC offset"
            )
        fresh = _freshly_revalidate_v151_admission(self.admission)
        object.__setattr__(self, "admission", fresh)
        return self


class TaskExecutionLifecycleAdmissionRepository(Protocol):
    """Technology-agnostic append-only durable lifecycle admission history
    boundary."""

    def add(self, record: TaskExecutionLifecycleAdmissionRecord) -> None:
        """Append exactly one immutable lifecycle admission record."""
        ...

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleAdmissionRecord, ...]:
        """Return exact durable history for one portfolio.

        Exact historical access only: no latest/effective/validity
        inference is ever derived from the history, and no task state of
        any kind is ever inferred from the embedded historical
        ``current_task_status`` evidence.
        """
        ...


def _freshly_revalidate_v151_admission(
    admission: TaskExecutionLifecycleAdmission,
) -> TaskExecutionLifecycleAdmission:
    """One canonical entry point: fresh COMPLETE strict revalidation of the
    embedded V1.51 admission.

    Reused by the record model and the public durable boundary so the
    semantic rules are never duplicated inconsistently. Only the returned
    fresh copy may be used for semantic reads afterwards; the caller-owned
    admission is never read semantically here. The V1.51 model's own
    invariants remain the sole semantic authority — nothing beyond the
    freshly revalidated admission is read.
    """

    try:
        payload: object = admission.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise DurableTaskExecutionLifecycleAdmissionError(
            "the supplied V1.51 admission is not the V1.51 admission shape"
        ) from exc

    try:
        return TaskExecutionLifecycleAdmission.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise DurableTaskExecutionLifecycleAdmissionError(
            "the supplied V1.51 admission failed fresh COMPLETE strict "
            "re-validation"
        ) from exc


def record_task_execution_lifecycle_admission_durably(
    admission_record_id: object,
    recorded_at: object,
    admission: object,
    *,
    repository: TaskExecutionLifecycleAdmissionRepository,
) -> TaskExecutionLifecycleAdmissionRecord:
    """Append one already-produced V1.51 lifecycle admission exactly once
    to ``repository``, and stop.

    The sequence is exact: genuine ``admission_record_id`` UUID -> genuine
    timezone-aware ``recorded_at`` -> genuine
    ``TaskExecutionLifecycleAdmission`` -> fresh COMPLETE strict
    revalidation of the full admission -> immutable record construction ->
    ``repository.add(record)`` exactly once -> return the exact record ->
    stop.

    Every validation failure occurs before repository interaction. The
    supplied admission is freshly strict-revalidated and only the retained
    fresh copy is used for every semantic read afterwards. The V1.51
    admission itself is the sole semantic authority: it is never called,
    recreated, or reinterpreted; no Portfolio is ever loaded or saved; no
    CURRENT WBS/relations/status is ever inspected; no V1.52/V1.53/V1.54/
    V1.55/V1.56 boundary is touched; and no lifecycle state transition of
    any kind occurs. No clock or UUID generation occurs here. Repeated
    valid calls append repeatedly; there is no dedupe, replacement,
    supersession, idempotency, or exactly-once semantics — plain
    append-only history.
    """

    if not isinstance(admission_record_id, UUID):
        raise DurableTaskExecutionLifecycleAdmissionError(
            "admission_record_id must already be a UUID instance, "
            f"got {type(admission_record_id).__name__}"
        )
    if not isinstance(recorded_at, datetime):
        raise DurableTaskExecutionLifecycleAdmissionError(
            "recorded_at must already be a datetime instance, "
            f"got {type(recorded_at).__name__}"
        )
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise DurableTaskExecutionLifecycleAdmissionError(
            "recorded_at must be a timezone-aware datetime with a non-None UTC offset"
        )
    if not isinstance(admission, TaskExecutionLifecycleAdmission):
        raise DurableTaskExecutionLifecycleAdmissionError(
            "admission must be a genuine V1.51 TaskExecutionLifecycleAdmission, "
            f"got {type(admission).__name__}"
        )

    fresh = _freshly_revalidate_v151_admission(admission)

    record = TaskExecutionLifecycleAdmissionRecord(
        admission_record_id=admission_record_id,
        recorded_at=recorded_at,
        admission=fresh,
    )

    repository.add(record)
    return record
