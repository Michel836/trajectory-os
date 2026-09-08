"""V1.50 — explicit human lifecycle disposition for one durable execution result.

Canonical authority chain:

    V1.48 provider-agnostic side-effectful execution
    -> V1.49 durable append-only execution result record/history
    -> V1.50 explicit human lifecycle disposition        <-- this module
    -> later CURRENT-state admission / durable lifecycle change

V1.50 is PURE. It turns one caller-supplied, genuine V1.49
``TaskExecutionResultRecord`` plus exactly three caller/human supplied values
(``lifecycle_decision_id``, ``decided_at``, ``disposition``) into one
immutable ``TaskExecutionLifecycleDecision``. It performs no TASK lifecycle
change, transitions no entity status, and never reads or mutates any
Portfolio, WBS, relation, or CURRENT-state value. No persistence, repository
interaction, provider call, runtime, subprocess, shell, clock read, or UUID
generation occurs here.

DISPOSITION SEMANTICS (exactly two values, no default exists):

- ``NO_LIFECYCLE_CHANGE``: valid for ``succeeded == True`` and for
  ``succeeded == False``. It is only the explicit human choice that this
  durable result shall not be used to derive a TASK lifecycle change.
- ``COMPLETE_TASK``: valid only for ``succeeded == True``. It is the explicit
  human semantic authorization for a future boundary to attempt transitioning
  the exact authorized TASK toward COMPLETED. V1.50 itself performs no
  transition of any kind.

A successful execution never automatically implies ``COMPLETE_TASK``, and a
failed execution never implies any lifecycle status.

VALIDATION ORDER (boundary errors raised in exactly this order):

1. ``lifecycle_decision_id`` must already be a genuine ``UUID``;
2. ``decided_at`` must already be a genuine timezone-aware ``datetime``;
3. ``disposition`` must already be a genuine
   ``TaskExecutionLifecycleDisposition``;
4. ``execution_record`` must already be a genuine V1.49
   ``TaskExecutionResultRecord``;
5. complete fresh strict revalidation of the V1.49 outer record
   (``model_dump(mode="python")`` then ``model_validate(payload, strict=True)``)
   to defeat hostile ``model_construct`` payloads; thereafter EVERY semantic
   read uses ONLY the retained fresh validated copy, never the caller-owned
   record;
6. ``COMPLETE_TASK`` requires the nested ``result.succeeded`` to be exactly
   ``True``;
7. construct the exact immutable decision and return it.

There is NO temporal ordering rule between ``decided_at`` and the record's
``recorded_at``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    ValidationError,
    model_validator,
)

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_persistence import (  # noqa: E501
    TaskExecutionResultRecord,
)

__all__ = [
    "TaskExecutionLifecycleDecision",
    "TaskExecutionLifecycleDecisionError",
    "TaskExecutionLifecycleDisposition",
    "decide_task_execution_lifecycle",
]


class TaskExecutionLifecycleDecisionError(ValueError):
    """Raised when a V1.50 human lifecycle disposition input is invalid."""


class TaskExecutionLifecycleDisposition(StrEnum):
    """Exactly two explicit human dispositions for one durable result."""

    NO_LIFECYCLE_CHANGE = "NO_LIFECYCLE_CHANGE"
    COMPLETE_TASK = "COMPLETE_TASK"


_AWARE_TIMESTAMP_NAMES: Final[tuple[str, str]] = ("decided_at", "execution_recorded_at")


class TaskExecutionLifecycleDecision(BaseModel):
    """One immutable explicit human lifecycle disposition decision.

    All provenance fields are projected exactly from the retained freshly
    validated V1.49 record. The caller supplies only
    ``lifecycle_decision_id``, ``decided_at`` and ``disposition``; upstream
    execution provenance can never be overridden by the caller.

    This decision type is self-validating: it is impossible to construct a valid
    decision with ``disposition == COMPLETE_TASK`` while
    ``execution_succeeded is not True``. Direct construction with a failed
    execution and ``COMPLETE_TASK`` raises ``pydantic.ValidationError``.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    lifecycle_decision_id: UUID
    decided_at: datetime
    execution_record_id: UUID
    execution_recorded_at: datetime
    request_id: UUID
    intent_id: UUID
    execution_decision_id: UUID
    portfolio_id: UUID
    authorized_project_id: UUID
    authorized_task_id: UUID
    execution_succeeded: StrictBool
    disposition: TaskExecutionLifecycleDisposition

    @model_validator(mode="after")
    def _require_complete_task_only_when_succeeded(
        self,
    ) -> TaskExecutionLifecycleDecision:
        if (
            self.disposition is TaskExecutionLifecycleDisposition.COMPLETE_TASK
            and self.execution_succeeded is not True
        ):
            raise ValueError(
                "COMPLETE_TASK is allowed only when execution_succeeded is exactly True"
            )
        return self

    @model_validator(mode="after")
    def _require_genuine_aware_timestamps(self) -> TaskExecutionLifecycleDecision:
        for name in _AWARE_TIMESTAMP_NAMES:
            value: datetime = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(
                    f"{name} must be a timezone-aware datetime with a non-None UTC offset"
                )
        return self


def _revalidate_record(
    execution_record: TaskExecutionResultRecord,
) -> TaskExecutionResultRecord:
    """Freshly COMPLETE strict-revalidate one genuine V1.49 outer record."""

    try:
        payload: object = execution_record.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise TaskExecutionLifecycleDecisionError(
            "the supplied V1.49 record is not the V1.49 record shape"
        ) from exc

    try:
        return TaskExecutionResultRecord.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise TaskExecutionLifecycleDecisionError(
            "the supplied V1.49 record failed fresh COMPLETE strict re-validation"
        ) from exc


def decide_task_execution_lifecycle(
    lifecycle_decision_id: object,
    decided_at: object,
    execution_record: object,
    disposition: object,
) -> TaskExecutionLifecycleDecision:
    """Build one immutable explicit human lifecycle disposition decision.

    Every validation failure is raised before the decision exists. The
    supplied V1.49 record is freshly strict-revalidated and only the retained
    fresh copy is used for every semantic read thereafter. ``COMPLETE_TASK``
    is accepted only when the nested V1.48 result explicitly succeeded. No
    lifecycle transition, persistence, clock read, or UUID generation occurs.
    """

    if not isinstance(lifecycle_decision_id, UUID):
        raise TaskExecutionLifecycleDecisionError(
            "lifecycle_decision_id must already be a genuine UUID instance, "
            f"got {type(lifecycle_decision_id).__name__}"
        )
    if not isinstance(decided_at, datetime):
        raise TaskExecutionLifecycleDecisionError(
            "decided_at must already be a genuine datetime instance, "
            f"got {type(decided_at).__name__}"
        )
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise TaskExecutionLifecycleDecisionError(
            "decided_at must be a timezone-aware datetime with a non-None UTC offset"
        )
    if not isinstance(disposition, TaskExecutionLifecycleDisposition):
        raise TaskExecutionLifecycleDecisionError(
            "disposition must be a genuine TaskExecutionLifecycleDisposition, "
            f"got {type(disposition).__name__}"
        )
    if not isinstance(execution_record, TaskExecutionResultRecord):
        raise TaskExecutionLifecycleDecisionError(
            "execution_record must be a genuine V1.49 TaskExecutionResultRecord, "
            f"got {type(execution_record).__name__}"
        )

    validated_record = _revalidate_record(execution_record)

    if (
        disposition is TaskExecutionLifecycleDisposition.COMPLETE_TASK
        and validated_record.result.succeeded is not True
    ):
        raise TaskExecutionLifecycleDecisionError(
            "COMPLETE_TASK is allowed only when the retained V1.49 record's "
            "nested result.succeeded is exactly True; no lifecycle status is "
            "inferred from a failed execution"
        )

    return TaskExecutionLifecycleDecision(
        lifecycle_decision_id=lifecycle_decision_id,
        decided_at=decided_at,
        execution_record_id=validated_record.execution_record_id,
        execution_recorded_at=validated_record.recorded_at,
        request_id=validated_record.result.request_id,
        intent_id=validated_record.result.intent_id,
        execution_decision_id=validated_record.result.decision_id,
        portfolio_id=validated_record.result.portfolio_id,
        authorized_project_id=validated_record.result.authorized_project_id,
        authorized_task_id=validated_record.result.authorized_task_id,
        execution_succeeded=validated_record.result.succeeded,
        disposition=disposition,
    )
