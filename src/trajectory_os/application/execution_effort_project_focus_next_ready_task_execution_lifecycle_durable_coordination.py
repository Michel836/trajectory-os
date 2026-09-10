"""V1.63 — durable lifecycle coordination orchestration.

ONE narrow application-layer orchestration boundary that composes the
existing public application-layer boundaries, in this EXACT order:

1. V1.60 :func:`coordinate_task_execution_lifecycle` invoked EXACTLY
   ONCE with every caller-supplied V1.60 argument and repository
   passed through UNCHANGED; any V1.60 failure propagates the EXACT
   original error UNCHANGED and no V1.61 interaction — including any
   interaction with ``outcome_repository`` — ever occurs;
2. ONLY after the V1.60 boundary returned its exact genuine
   :class:`TaskExecutionLifecycleOutcome` successfully, V1.61
   :func:`record_task_execution_lifecycle_outcome_durably` is invoked
   EXACTLY ONCE with the exact caller-supplied
   ``outcome_record_id``, the exact caller-supplied
   ``outcome_recorded_at``, that EXACT V1.60 outcome, and the exact
   ``outcome_repository``.

On V1.61 success the EXACT
:class:`TaskExecutionLifecycleOutcomeRecord` returned by V1.61 is
returned unchanged and the orchestration STOPS.

V1.63 does NOT duplicate, recreate, or reinterpret any V1.60 or V1.61
semantics: V1.60 remains the SOLE authority for coordination, V1.61
remains the SOLE authority for outcome-record validation,
construction, and append. No component is manually constructed, no
nested admission/decision/application/transition state is manually
validated, and no CURRENT Portfolio/WBS/status is ever inspected here.

Because the V1.61 step happens ONLY after V1.60 has already completed
successfully, any V1.61 validation or outcome-history repository
failure is a clearly POST-V1.60 failure and is surfaced EXPLICITLY as
:class:`TaskExecutionLifecycleOutcomeHistoryPersistenceError` carrying
the EXACT genuine V1.60 outcome (``outcome``) and the EXACT original
failure (``cause``, chained via ``raise ... from``). V1.60 failures
are NEVER wrapped. The wrapping makes NO claim of rollback,
compensation, retry, recovery, or atomicity, and no such behavior
exists here.

No UUID is generated, no wall clock is read, no identity is
substituted, no timestamp is normalized. This layer depends only on
existing application/domain protocols (
:class:`PortfolioRepository`,
:class:`TaskExecutionLifecycleAdmissionRepository`,
:class:`TaskExecutionLifecycleDecisionRepository`,
:class:`TaskExecutionLifecycleApplicationRepository`,
:class:`TaskExecutionLifecycleOutcomeRepository`) and contains no
concrete persistence, provider, runtime, or storage dependency of any
kind.
"""

from __future__ import annotations

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_admission_history import (  # noqa: E501
    TaskExecutionLifecycleAdmissionRepository,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_application_persistence import (  # noqa: E501
    TaskExecutionLifecycleApplicationRepository,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_coordination import (  # noqa: E501
    TaskExecutionLifecycleOutcome,
    coordinate_task_execution_lifecycle,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_decision_persistence import (  # noqa: E501
    TaskExecutionLifecycleDecisionRepository,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_outcome_history import (  # noqa: E501
    TaskExecutionLifecycleOutcomeRecord,
    TaskExecutionLifecycleOutcomeRepository,
    record_task_execution_lifecycle_outcome_durably,
)
from trajectory_os.application.work_breakdown_acceptance import (
    PortfolioRepository,
)

__all__ = [
    "TaskExecutionLifecycleOutcomeHistoryPersistenceError",
    "coordinate_task_execution_lifecycle_durably",
]


class TaskExecutionLifecycleOutcomeHistoryPersistenceError(ValueError):
    """Raised ONLY for a failure of the V1.61 durable outcome-history
    step that occurs AFTER the V1.60 coordination boundary has already
    returned its exact outcome successfully.

    The error carries the EXACT genuine V1.60
    :class:`TaskExecutionLifecycleOutcome` that was already produced
    successfully (``outcome``) and the EXACT original V1.61 /
    outcome-history repository failure (``cause``); the original
    exception is chained as ``__cause__``. A V1.60 failure is NEVER
    represented by this error type. This error makes no claim of
    rollback, compensation, retry, or recovery, and none exists here:
    the prior durable V1.60 appends remain durable, and no
    outcome-history success record exists for this outcome.
    """

    def __init__(
        self,
        outcome: TaskExecutionLifecycleOutcome,
        cause: BaseException,
    ) -> None:
        self.outcome = outcome
        self.cause = cause
        super().__init__(
            "the exact V1.60 lifecycle outcome was already produced and "
            "returned successfully, but the subsequent V1.61 durable "
            "outcome-history append failed; this failure is surfaced "
            f"explicitly (cause: {type(cause).__name__}: {cause}) and no "
            "outcome-history success record exists for this outcome"
        )
        self.__cause__ = cause


def coordinate_task_execution_lifecycle_durably(
    admission_record_id: object,
    admission_recorded_at: object,
    decision_record_id: object,
    decision_recorded_at: object,
    decision: object,
    changed_at: object,
    application_record_id: object,
    application_recorded_at: object,
    *,
    admission_repository: TaskExecutionLifecycleAdmissionRepository,
    decision_repository: TaskExecutionLifecycleDecisionRepository,
    application_repository: TaskExecutionLifecycleApplicationRepository,
    portfolio_repository: PortfolioRepository,
    outcome_record_id: object,
    outcome_recorded_at: object,
    outcome_repository: TaskExecutionLifecycleOutcomeRepository,
) -> TaskExecutionLifecycleOutcomeRecord:
    """Compose the existing V1.60 and V1.61 public boundaries into ONE
    durable lifecycle coordination orchestration and stop.

    The sequence is EXACT:

    1. invoke the existing public V1.60
       :func:`coordinate_task_execution_lifecycle` EXACTLY ONCE, with
       every supplied V1.60 value and repository passed through
       UNCHANGED (no coercion, no substitution, no clock or UUID
       handling); any V1.60 failure propagates the EXACT original
       error UNCHANGED — the V1.61 boundary is never called,
       ``outcome_repository`` is never touched, and the orchestration
       stops;
    2. retain the EXACT ``TaskExecutionLifecycleOutcome`` returned by
       that V1.60 call;
    3. invoke the existing public V1.61
       :func:`record_task_execution_lifecycle_outcome_durably` EXACTLY
       ONCE with the exact caller ``outcome_record_id``, the exact
       caller ``outcome_recorded_at``, that EXACT V1.60 outcome, and
       the exact ``outcome_repository``;
    4. on success return the EXACT
       ``TaskExecutionLifecycleOutcomeRecord`` returned by V1.61,
       unchanged (it is never reconstructed or copied here), and stop.

    V1.61 happens ONLY after V1.60 has already completed successfully,
    so a V1.61 validation or outcome-history repository failure is a
    clearly POST-V1.60 failure: it is surfaced EXPLICITLY as a
    :class:`TaskExecutionLifecycleOutcomeHistoryPersistenceError`
    carrying the exact successful ``outcome`` and the exact original
    ``cause`` (chained), with no claim of rollback, compensation,
    retry, recovery, or atomicity. V1.60 failures are never wrapped.
    This boundary adds no validation of its own, no state transition,
    no additional Portfolio load or save, and no dependency on any
    concrete persistence.
    """

    outcome: TaskExecutionLifecycleOutcome = (
        coordinate_task_execution_lifecycle(
            admission_record_id,
            admission_recorded_at,
            decision_record_id,
            decision_recorded_at,
            decision,
            changed_at,
            application_record_id,
            application_recorded_at,
            admission_repository=admission_repository,
            decision_repository=decision_repository,
            application_repository=application_repository,
            portfolio_repository=portfolio_repository,
        )
    )

    try:
        return record_task_execution_lifecycle_outcome_durably(
            outcome_record_id,
            outcome_recorded_at,
            outcome,
            repository=outcome_repository,
        )
    except Exception as exc:
        raise TaskExecutionLifecycleOutcomeHistoryPersistenceError(
            outcome,
            exc,
        ) from exc
