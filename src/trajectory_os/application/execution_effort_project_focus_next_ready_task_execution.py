"""V1.48 — provider-agnostic execution for one CURRENT-admitted exact TASK.

V1.48 is the first side-effectful boundary in the exact-task execution chain:

    V1.47 CURRENT execution admission
    -> V1.48 provider-agnostic execution port
    -> later durable receipt / lifecycle consequences

The sole semantic admission authority is one genuine, freshly strict-revalidated
V1.47 ``PortfolioProjectFocusNextReadyTaskExecutionAdmission`` whose state is
exactly ``ADMITTED``. No task selection, fallback, substitution, CURRENT
Portfolio read, lifecycle transition, persistence, clock read, UUID generation,
or provider-specific behavior occurs here.

The supplied ``TaskExecutionPort`` is invoked exactly once for a valid admitted
input. It receives an immutable command containing only exact upstream identity
and provenance UUIDs. Its result is treated as untrusted: a genuine
``TaskExecutionResult`` is required, the complete result is freshly strict-
revalidated, and every identity must exactly match the command. Executor
exceptions propagate unchanged and no retry is attempted.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_admission import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskExecutionAdmission,
    PortfolioProjectFocusNextReadyTaskExecutionAdmissionState,
)

__all__ = [
    "TaskExecutionBoundaryError",
    "TaskExecutionCommand",
    "TaskExecutionPort",
    "TaskExecutionResult",
    "execute_current_admitted_task",
]


class TaskExecutionBoundaryError(ValueError):
    """Raised when V1.48 cannot safely invoke or accept execution."""


class TaskExecutionCommand(BaseModel):
    """Exact immutable provider-agnostic command for one admitted TASK."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    request_id: UUID
    intent_id: UUID
    decision_id: UUID
    portfolio_id: UUID
    authorized_project_id: UUID
    authorized_task_id: UUID


class TaskExecutionResult(BaseModel):
    """Minimal immutable provider-agnostic outcome for one exact command.

    ``succeeded`` is the executor's explicit outcome value. It does not imply
    any TASK lifecycle transition and is not persisted by V1.48.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    request_id: UUID
    intent_id: UUID
    decision_id: UUID
    portfolio_id: UUID
    authorized_project_id: UUID
    authorized_task_id: UUID
    succeeded: StrictBool


class TaskExecutionPort(Protocol):
    """Provider-agnostic side-effect seam for one exact execution command."""

    def execute(self, command: TaskExecutionCommand) -> TaskExecutionResult:
        """Execute exactly ``command`` and return its explicit outcome."""
        ...


def _require_genuine_admission(
    admission: object,
) -> PortfolioProjectFocusNextReadyTaskExecutionAdmission:
    if not isinstance(
        admission, PortfolioProjectFocusNextReadyTaskExecutionAdmission
    ):
        raise TaskExecutionBoundaryError(
            "a genuine V1.47 execution admission is required, "
            f"got {type(admission).__name__}"
        )
    return admission


def _revalidate_admission(
    admission: PortfolioProjectFocusNextReadyTaskExecutionAdmission,
) -> PortfolioProjectFocusNextReadyTaskExecutionAdmission:
    try:
        payload: object = admission.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise TaskExecutionBoundaryError(
            "the supplied V1.47 admission is not the V1.47 shape"
        ) from exc

    try:
        return PortfolioProjectFocusNextReadyTaskExecutionAdmission.model_validate(
            payload, strict=True
        )
    except ValidationError as exc:
        raise TaskExecutionBoundaryError(
            "the supplied V1.47 admission failed fresh COMPLETE strict re-validation"
        ) from exc


def _revalidate_result(result: object) -> TaskExecutionResult:
    if not isinstance(result, TaskExecutionResult):
        raise TaskExecutionBoundaryError(
            "executor must return a genuine TaskExecutionResult, "
            f"got {type(result).__name__}"
        )

    try:
        payload: object = result.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise TaskExecutionBoundaryError(
            "the executor result is not the V1.48 result shape"
        ) from exc

    try:
        return TaskExecutionResult.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise TaskExecutionBoundaryError(
            "the executor result failed fresh COMPLETE strict re-validation"
        ) from exc


def execute_current_admitted_task(
    admission: PortfolioProjectFocusNextReadyTaskExecutionAdmission,
    executor: TaskExecutionPort,
) -> TaskExecutionResult:
    """Execute exactly one V1.47-admitted TASK through ``executor``.

    Validation occurs before the side effect. The executor is then called
    exactly once. Its exceptions propagate unchanged. Its returned value is
    freshly strict-revalidated and rejected if any identity differs from the
    exact command. No retry, persistence, lifecycle mutation, provider-specific
    action, generated identity/time, or fallback task exists in this boundary.
    """

    genuine = _require_genuine_admission(admission)
    validated = _revalidate_admission(genuine)

    if (
        validated.admission_state
        is not PortfolioProjectFocusNextReadyTaskExecutionAdmissionState.ADMITTED
    ):
        raise TaskExecutionBoundaryError(
            "V1.48 requires V1.47 admission_state == ADMITTED"
        )

    command = TaskExecutionCommand(
        request_id=validated.request_id,
        intent_id=validated.intent_id,
        decision_id=validated.decision_id,
        portfolio_id=validated.portfolio_id,
        authorized_project_id=validated.authorized_project_id,
        authorized_task_id=validated.authorized_task_id,
    )

    result = executor.execute(command)
    validated_result = _revalidate_result(result)

    if (
        validated_result.request_id != command.request_id
        or validated_result.intent_id != command.intent_id
        or validated_result.decision_id != command.decision_id
        or validated_result.portfolio_id != command.portfolio_id
        or validated_result.authorized_project_id != command.authorized_project_id
        or validated_result.authorized_task_id != command.authorized_task_id
    ):
        raise TaskExecutionBoundaryError(
            "executor result identities must exactly match the V1.48 command"
        )

    return validated_result
