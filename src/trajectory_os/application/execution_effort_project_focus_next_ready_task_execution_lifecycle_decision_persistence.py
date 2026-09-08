"""V1.55 — durable append-only history for explicit human lifecycle decisions.

V1.55 is OBSERVATIONAL ONLY: it makes one already-produced genuine V1.50
``TaskExecutionLifecycleDecision`` durable as immutable append-only history.
It never re-creates, re-decides, re-derives, or re-interprets V1.50
semantics (it DOES freshly and strictly re-validate the supplied V1.50
decision before any repository interaction), never calls
``decide_task_execution_lifecycle``, never touches CURRENT
Portfolio/WBS/status, never calls V1.51, V1.52, V1.53, or V1.54,
never transitions any lifecycle state, and never claims
transactionality, exactly-once semantics, idempotency, or crash
consistency with V1.50.

Canonical authority chain:

    V1.49 durable execution result
    -> V1.50 explicit human lifecycle disposition        <- sole semantic authority
    -> V1.55 durable append-only decision history       <-- this module
    -> existing V1.51 CURRENT-state admission
    -> existing V1.52 durable application to COMPLETED
    -> existing V1.53/V1.54 application history

The durable record follows the established V1.35/V1.43/V1.49/V1.53 pattern:

- caller-supplied UUID record identity;
- caller-supplied timezone-aware timestamp;
- exact upstream immutable decision as the sole semantic authority;
- fresh COMPLETE strict revalidation before repository interaction;
- every validation failure before any repository interaction;
- append-only structural repository boundary;
- repository failures propagate unchanged.

SOLE UPSTREAM AUTHORITY: one genuine ``TaskExecutionLifecycleDecision``
already produced by V1.50. Nothing is reconstructed: no V1.48 execution,
no V1.49 record reload, no V1.51 admission, no V1.52 result, no inferred
provenance. The fresh revalidated decision is embedded verbatim and ALL
twelve V1.50 provenance/semantic fields
(``lifecycle_decision_id``, ``decided_at``, ``execution_record_id``,
``execution_recorded_at``, ``request_id``, ``intent_id``,
``execution_decision_id``, ``portfolio_id``, ``authorized_project_id``,
``authorized_task_id``, ``execution_succeeded``, ``disposition``) are
preserved exactly, including exact datetime offsets. No field is
normalized, derived, or reinterpreted.

IDENTITY: ``decision_record_id`` is the durable outer record identity and
is distinct from ``lifecycle_decision_id``, ``execution_record_id``,
``request_id``, and every task/project/portfolio id. Nothing is
deduplicated by any semantic identity: repeated explicit calls with
distinct ``decision_record_id`` values remain separate appends. There is
no idempotency, exactly-once, latest, current, effective, supersession,
or revocation semantics — plain append-only history.

NO PERSISTENCE BACKEND HERE: the repository is a structural Python
protocol only. Concrete durable storage (for example SQLite) is
intentionally reserved for a later milestone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle import (  # noqa: E501
    TaskExecutionLifecycleDecision,
)

__all__ = [
    "DurableTaskExecutionLifecycleDecisionError",
    "TaskExecutionLifecycleDecisionRecord",
    "TaskExecutionLifecycleDecisionRepository",
    "record_task_execution_lifecycle_decision_durably",
]


class DurableTaskExecutionLifecycleDecisionError(ValueError):
    """Raised when a V1.55 durable lifecycle decision record input is
    invalid."""


class TaskExecutionLifecycleDecisionRecord(BaseModel):
    """One immutable durable record of an exact V1.50 explicit human
    lifecycle disposition decision.

    The embedded decision must itself be a genuine V1.50 decision.
    Construction freshly strict-revalidates the embedded decision and
    re-enforces the V1.50 model's own invariants — so an invalid or
    hostile V1.50-semantic state (for example a ``model_construct``
    payload, a naive ``decided_at`` inside the decision, or a genuine
    ``COMPLETE_TASK`` disposition over a failed execution) can never
    exist inside this public record, whether it arrives via the durable
    boundary or direct construction.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_record_id: UUID
    recorded_at: datetime
    decision: TaskExecutionLifecycleDecision

    @model_validator(mode="after")
    def _enforce_record_invariants(self) -> TaskExecutionLifecycleDecisionRecord:
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError(
                "recorded_at must be a timezone-aware datetime with a non-None UTC offset"
            )
        fresh = _freshly_revalidate_v150_decision(self.decision)
        object.__setattr__(self, "decision", fresh)
        return self


class TaskExecutionLifecycleDecisionRepository(Protocol):
    """Technology-agnostic append-only durable lifecycle decision history
    boundary."""

    def add(self, record: TaskExecutionLifecycleDecisionRecord) -> None:
        """Append exactly one immutable lifecycle decision record."""
        ...

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleDecisionRecord, ...]:
        """Return exact durable history for one portfolio.

        Exact historical access only: no latest/current/effective/current
        task-state inference is ever derived from the history.
        """
        ...


def _freshly_revalidate_v150_decision(
    decision: TaskExecutionLifecycleDecision,
) -> TaskExecutionLifecycleDecision:
    """One canonical entry point: fresh COMPLETE strict revalidation of the
    embedded V1.50 decision.

    Reused by the record model and the public durable boundary so the
    semantic rules are never duplicated inconsistently. Only the returned
    fresh copy may be used for semantic reads afterwards; the caller-owned
    decision is never read semantically here. The V1.50 model's own
    invariants remain the sole semantic authority — nothing beyond the
    freshly revalidated decision is read.
    """

    try:
        payload: object = decision.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise DurableTaskExecutionLifecycleDecisionError(
            "the supplied V1.50 decision is not the V1.50 decision shape"
        ) from exc

    try:
        return TaskExecutionLifecycleDecision.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise DurableTaskExecutionLifecycleDecisionError(
            "the supplied V1.50 decision failed fresh COMPLETE strict "
            "re-validation"
        ) from exc


def record_task_execution_lifecycle_decision_durably(
    decision_record_id: object,
    recorded_at: object,
    decision: object,
    *,
    repository: TaskExecutionLifecycleDecisionRepository,
) -> TaskExecutionLifecycleDecisionRecord:
    """Append one already-produced V1.50 explicit human lifecycle decision
    exactly once to ``repository``, and stop.

    The sequence is exact: genuine ``decision_record_id`` UUID -> genuine
    timezone-aware ``recorded_at`` -> genuine
    ``TaskExecutionLifecycleDecision`` -> fresh COMPLETE strict
    revalidation of the full decision -> immutable record construction ->
    ``repository.add(record)`` exactly once -> return the exact record ->
    stop.

    Every validation failure occurs before repository interaction. The
    supplied decision is freshly strict-revalidated and only the retained
    fresh copy is used for every semantic read afterwards. The V1.50
    decision itself is the sole semantic authority: it is never called,
    recreated, or reinterpreted; no Portfolio is ever loaded or saved; no
    V1.51/V1.52/V1.53/V1.54 boundary is touched; and no lifecycle state
    transition of any kind occurs. No clock or UUID generation occurs
    here. Repeated valid calls append repeatedly; there is no dedupe,
    replacement, supersession, idempotency, or exactly-once semantics —
    plain append-only history.
    """

    if not isinstance(decision_record_id, UUID):
        raise DurableTaskExecutionLifecycleDecisionError(
            "decision_record_id must already be a UUID instance, "
            f"got {type(decision_record_id).__name__}"
        )
    if not isinstance(recorded_at, datetime):
        raise DurableTaskExecutionLifecycleDecisionError(
            "recorded_at must already be a datetime instance, "
            f"got {type(recorded_at).__name__}"
        )
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise DurableTaskExecutionLifecycleDecisionError(
            "recorded_at must be a timezone-aware datetime with a non-None UTC offset"
        )
    if not isinstance(decision, TaskExecutionLifecycleDecision):
        raise DurableTaskExecutionLifecycleDecisionError(
            "decision must be a genuine V1.50 TaskExecutionLifecycleDecision, "
            f"got {type(decision).__name__}"
        )

    fresh = _freshly_revalidate_v150_decision(decision)

    record = TaskExecutionLifecycleDecisionRecord(
        decision_record_id=decision_record_id,
        recorded_at=recorded_at,
        decision=fresh,
    )

    repository.add(record)
    return record
