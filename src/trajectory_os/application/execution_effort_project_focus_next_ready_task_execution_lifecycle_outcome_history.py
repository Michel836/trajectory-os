"""V1.61 — durable append-only history for V1.60 lifecycle outcomes.

V1.61 is OBSERVATIONAL ONLY: it makes one already-produced genuine V1.60
``TaskExecutionLifecycleOutcome`` durable as immutable append-only
history. It never re-creates, re-coordinates, re-admits, re-decides, or
re-interprets V1.60 semantics (it DOES freshly and strictly re-validate
the supplied V1.60 outcome before any repository interaction), never
calls ``coordinate_task_execution_lifecycle``, never calls
``admit_current_task_execution_lifecycle``, never calls
``apply_admitted_task_execution_lifecycle_durably``, never touches
CURRENT Portfolio/WBS/status, never applies any lifecycle state, and
never claims transactionality, exactly-once semantics, idempotency, or
crash consistency with V1.60.

Canonical authority chain:

    V1.49 durable execution result
    -> V1.50 explicit human lifecycle disposition
    -> V1.51 CURRENT-state admission
    -> V1.58 durable append-only admission history
    -> V1.55 durable append-only human-decision history
    -> V1.52 durable application to COMPLETED
    -> V1.53 durable append-only application history
    -> V1.60 explicit deterministic outcome
    -> V1.61 durable append-only outcome history   <-- this module

The durable record follows the established V1.35/V1.43/V1.49/V1.53/V1.55/
V1.58 pattern:

- caller-supplied UUID record identity;
- caller-supplied timezone-aware timestamp;
- exact upstream immutable outcome as the sole semantic authority;
- fresh COMPLETE strict revalidation before repository interaction;
- every validation failure before any repository interaction;
- append-only structural repository boundary;
- repository failures propagate unchanged.

SOLE UPSTREAM AUTHORITY: one genuine ``TaskExecutionLifecycleOutcome``
already produced by V1.60, containing the EXACT V1.58 admission record
(V1.51 admission), the EXACT V1.55 decision record (V1.50 decision), the
EXACT V1.52 ``EntityStatusTransitionResult``, and the EXACT V1.53
application record. Nothing is reconstructed: no V1.50 decision, no
Portfolio load, no CURRENT WBS/relations/status inspection, no replay,
no inferred provenance. The fresh revalidated outcome is embedded
verbatim and ALL four V1.60 components
(``admission_record``, ``decision_record``, ``transition_result``,
``application_record``) are preserved exactly, including every nested
identity and exact datetime offset. No field is normalized, derived, or
reinterpreted. The canonical nested public models (V1.58, V1.55, V1.52,
V1.53) remain the sole semantic authority for their nested states; this
module never duplicates their nested semantic rules.

HISTORICAL STATUS SEMANTICS: every embedded value — in particular the
V1.58 historical ``current_task_status`` admission evidence and the
V1.52 ``transition_result`` — is HISTORICAL EVIDENCE ONLY. Persisting it
does not make it authoritative for any later mutation, never proves any
CURRENT Portfolio/task state, and authorizes no later state transition.

IDENTITY: ``outcome_record_id`` is the durable OUTER record identity and
is distinct from ``admission_record_id``, ``decision_record_id``,
``application_record_id``, ``lifecycle_decision_id``,
``execution_record_id``, and every task/project/portfolio id. Two
value-equivalent V1.60 outcomes recorded under two different
``outcome_record_id`` values are legal distinct append-only records.
Nothing is deduplicated by any semantic identity: repeated explicit
calls with distinct ``outcome_record_id`` values remain separate
appends. There is no idempotency, exactly-once, latest, effective,
supersession, or revocation semantics — plain append-only history.
``list_history`` means exactly: the exact durable outcome history scoped
to one portfolio.

NO PERSISTENCE BACKEND HERE: the repository is a structural Python
protocol only. Concrete durable storage (for example SQLite) and any
deterministic ordering mechanics are intentionally reserved for a later
milestone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_coordination import (  # noqa: E501
    TaskExecutionLifecycleOutcome,
)

__all__ = [
    "DurableTaskExecutionLifecycleOutcomeError",
    "TaskExecutionLifecycleOutcomeRecord",
    "TaskExecutionLifecycleOutcomeRepository",
    "record_task_execution_lifecycle_outcome_durably",
]


class DurableTaskExecutionLifecycleOutcomeError(ValueError):
    """Raised when a V1.61 durable lifecycle outcome record input is
    invalid."""


class TaskExecutionLifecycleOutcomeRecord(BaseModel):
    """One immutable durable record of an exact V1.60 coordinated
    lifecycle outcome.

    The embedded outcome must itself be a genuine V1.60 outcome.
    Construction freshly strict-revalidates the embedded outcome —
    COMPLETELY, including every nested V1.58/V1.55/V1.52/V1.53 record —
    and re-enforces the canonical nested models' own invariants, so an
    invalid or hostile V1.60-semantic state (for example a
    ``model_construct`` payload, a hostile nested
    ``model_construct`` admission/decision/transition/application state,
    a naive nested timestamp, or a non-COMPLETED target inside the
    embedded transition result) can never exist inside this public
    record, whether it arrives via the durable boundary or direct
    construction.

    All embedded values are HISTORICAL EVIDENCE ONLY: the historical
    admission ``current_task_status`` and the V1.52
    ``transition_result`` author nothing beyond proving what was
    produced and applied at their exact historical moments.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    outcome_record_id: UUID
    recorded_at: datetime
    outcome: TaskExecutionLifecycleOutcome

    @model_validator(mode="after")
    def _enforce_record_invariants(
        self,
    ) -> TaskExecutionLifecycleOutcomeRecord:
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError(
                "recorded_at must be a timezone-aware datetime with a non-None UTC offset"
            )
        fresh = _freshly_revalidate_v160_outcome(self.outcome)
        object.__setattr__(self, "outcome", fresh)
        return self


class TaskExecutionLifecycleOutcomeRepository(Protocol):
    """Technology-agnostic append-only durable lifecycle outcome history
    boundary."""

    def add(self, record: TaskExecutionLifecycleOutcomeRecord) -> None:
        """Append exactly one immutable lifecycle outcome record."""
        ...

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleOutcomeRecord, ...]:
        """Return exact durable history for one portfolio.

        Exact historical access only: no latest/effective/validity
        inference is ever derived from the history, and no CURRENT
        task state of any kind is ever inferred from the embedded
        historical admission evidence or transition results.
        """
        ...


def _freshly_revalidate_v160_outcome(
    outcome: TaskExecutionLifecycleOutcome,
) -> TaskExecutionLifecycleOutcome:
    """One canonical entry point: fresh COMPLETE strict revalidation of the
    embedded V1.60 outcome.

    Reused by the record model and the public durable boundary so the
    semantic rules are never duplicated inconsistently. Only the returned
    fresh copy may be used for semantic reads afterwards; the caller-owned
    outcome is never read semantically here. The canonical nested public
    models (V1.58 admission record, V1.55 decision record, V1.52
    transition result, V1.53 application record) remain the sole semantic
    authority for their nested states — nothing beyond the freshly
    revalidated outcome is read.
    """

    try:
        payload: object = outcome.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise DurableTaskExecutionLifecycleOutcomeError(
            "the supplied V1.60 outcome is not the V1.60 outcome shape"
        ) from exc

    try:
        return TaskExecutionLifecycleOutcome.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise DurableTaskExecutionLifecycleOutcomeError(
            "the supplied V1.60 outcome failed fresh COMPLETE strict "
            "re-validation"
        ) from exc


def record_task_execution_lifecycle_outcome_durably(
    outcome_record_id: object,
    recorded_at: object,
    outcome: object,
    *,
    repository: TaskExecutionLifecycleOutcomeRepository,
) -> TaskExecutionLifecycleOutcomeRecord:
    """Append one already-produced V1.60 lifecycle outcome exactly once
    to ``repository``, and stop.

    The sequence is exact: genuine ``outcome_record_id`` UUID -> genuine
    timezone-aware ``recorded_at`` -> genuine
    ``TaskExecutionLifecycleOutcome`` -> fresh COMPLETE strict
    revalidation of the full outcome -> immutable record construction ->
    ``repository.add(record)`` exactly once -> return the exact record ->
    stop.

    Every validation failure occurs before repository interaction. The
    supplied outcome is freshly strict-revalidated and only the retained
    fresh copy is used for every semantic read afterwards. The V1.60
    outcome itself is the sole semantic authority: it is never called,
    recreated, or reinterpreted; no Portfolio is ever loaded or saved; no
    CURRENT WBS/relations/status is ever inspected; no V1.51/V1.52
    boundary is touched; and no lifecycle state transition of any kind
    occurs. No clock or UUID generation occurs here. Repeated valid calls
    append repeatedly; there is no dedupe, replacement, supersession,
    idempotency, or exactly-once semantics — plain append-only history.
    Repository failures propagate unchanged (they are never wrapped).
    """

    if not isinstance(outcome_record_id, UUID):
        raise DurableTaskExecutionLifecycleOutcomeError(
            "outcome_record_id must already be a UUID instance, "
            f"got {type(outcome_record_id).__name__}"
        )
    if not isinstance(recorded_at, datetime):
        raise DurableTaskExecutionLifecycleOutcomeError(
            "recorded_at must already be a datetime instance, "
            f"got {type(recorded_at).__name__}"
        )
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise DurableTaskExecutionLifecycleOutcomeError(
            "recorded_at must be a timezone-aware datetime with a non-None UTC offset"
        )
    if not isinstance(outcome, TaskExecutionLifecycleOutcome):
        raise DurableTaskExecutionLifecycleOutcomeError(
            "outcome must be a genuine V1.60 TaskExecutionLifecycleOutcome, "
            f"got {type(outcome).__name__}"
        )

    fresh = _freshly_revalidate_v160_outcome(outcome)

    record = TaskExecutionLifecycleOutcomeRecord(
        outcome_record_id=outcome_record_id,
        recorded_at=recorded_at,
        outcome=fresh,
    )

    repository.add(record)
    return record
