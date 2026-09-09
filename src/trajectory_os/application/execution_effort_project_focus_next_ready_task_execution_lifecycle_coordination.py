"""V1.57 — minimal application-layer coordinator for the V1.50–V1.56 chain.

V1.57 connects the EXISTING, already-merged V1.50–V1.56 lifecycle components
end-to-end. It introduces no new lifecycle states, no new transition rules,
no retries, no queues, no workers, no fallback policy, no hidden mutation,
and no provider-specific logic. Every semantic decision remains authoritative
in its ORIGINAL boundary:

    V1.50 explicit human lifecycle disposition
    -> V1.51 CURRENT-state admission of COMPLETE_TASK
    -> V1.55 durable append-only human-decision history
    -> V1.52 durable application to COMPLETED
    -> V1.53 durable append-only application history
    -> explicit deterministic outcome            <-- this module

The EXACT canonical sequence of this coordinator is:

1. require ONE genuine V1.50 ``TaskExecutionLifecycleDecision``;
2. ``portfolio_repository.load(decision.portfolio_id)``; a missing
   portfolio raises
   :class:`TaskExecutionLifecycleCoordinatorPortfolioNotFoundError`
   before anything durable is recorded or applied;
3. ``admit_current_task_execution_lifecycle`` (V1.51) against the EXACT
   freshly loaded CURRENT Portfolio — CURRENT admission is authoritative
   at this boundary; ANY admission failure means ZERO downstream side
   effects (no decision record, no application, no application record);
4. ``record_task_execution_lifecycle_decision_durably`` (V1.55) — the
   durable append-only human-decision record; a decision-persistence
   failure (including a failing ``decision_repository.add``) STOPS BEFORE
   the V1.52 application is ever called;
5. ``apply_admitted_task_execution_lifecycle_durably`` (V1.52) — the
   durable application; V1.52 itself reloads the Portfolio and RE-EVALUATES
   the V1.51 admission against that freshly loaded CURRENT state before it
   transitions anything and saves at most once; any application failure
   means NO application-history record is created;
6. ``record_task_execution_lifecycle_application_durably`` (V1.53) — the
   durable append-only application record for the EXACT V1.52 result. This
   step is ONLY reached after a real, durably saved V1.52 application.
   ANY failure here — although the application has already SUCCEEDED and
   been saved — is surfaced explicitly via
   :class:`TaskExecutionLifecycleApplicationHistoryPersistenceError` with
   the exact ``EntityStatusTransitionResult`` attached and the original
   cause chained; no application-history success record ever exists for a
   failed history append;
7. construct and return the exact immutable
   :class:`TaskExecutionLifecycleOutcome` and STOP.

IDENTITY PRESERVATION: every identity and timestamp —
``decision_record_id``, ``decision_recorded_at``, ``changed_at``,
``application_record_id``, ``application_recorded_at``, and every
V1.48/V1.49/V1.50 execution/result/decision identity — is supplied by the
caller or projected verbatim from the exact upstream values. V1.57 never
generates a UUID, never reads the wall clock, and never reinterprets,
normalizes, or substitutes any identity.

REPOSITORY DEPENDENCIES (protocols only, no concrete SQLite):

* ``portfolio_repository``: the existing structural
  ``PortfolioRepository`` (load/save) boundary;
* ``decision_repository``: the existing V1.55
  ``TaskExecutionLifecycleDecisionRepository`` protocol;
* ``application_repository``: the existing V1.53
  ``TaskExecutionLifecycleApplicationRepository`` protocol.

V1.57 NEVER bypasses an upstream boundary: V1.50/V1.51/V1.52/V1.53/V1.55
validation, revalidation, admission, transition, and persistence semantics
are invoked, not reimplemented. Repository failures and upstream boundary
errors propagate unchanged EXCEPT at step 6, where a failure is already
provably POST-application and is therefore explicitly tagged.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle import (  # noqa: E501
    TaskExecutionLifecycleDecision,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_admission import (  # noqa: E501
    admit_current_task_execution_lifecycle,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_admission_persistence import (  # noqa: E501
    apply_admitted_task_execution_lifecycle_durably,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_application_persistence import (  # noqa: E501
    TaskExecutionLifecycleApplicationRecord,
    TaskExecutionLifecycleApplicationRepository,
    record_task_execution_lifecycle_application_durably,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_decision_persistence import (  # noqa: E501
    TaskExecutionLifecycleDecisionRecord,
    TaskExecutionLifecycleDecisionRepository,
    record_task_execution_lifecycle_decision_durably,
)
from trajectory_os.application.work_breakdown_acceptance import (
    PortfolioRepository,
)
from trajectory_os.domain.entity_status_transition import (
    EntityStatusTransitionResult,
)

__all__ = [
    "TaskExecutionLifecycleApplicationHistoryPersistenceError",
    "TaskExecutionLifecycleCoordinatorError",
    "TaskExecutionLifecycleCoordinatorPortfolioNotFoundError",
    "TaskExecutionLifecycleOutcome",
    "coordinate_task_execution_lifecycle",
]


class TaskExecutionLifecycleCoordinatorError(ValueError):
    """Raised when the minimal V1.57 lifecycle coordination boundary
    rejects an input before any side effect can occur.

    Raised for: a non-genuine V1.50 decision; and a portfolio that is
    missing from the supplied ``portfolio_repository``. V1.51
    admission errors, V1.55 decision-persistence errors, and V1.52
    application errors propagate from their ORIGINAL boundaries
    unchanged.
    """


class TaskExecutionLifecycleCoordinatorPortfolioNotFoundError(
    TaskExecutionLifecycleCoordinatorError
):
    """Raised when the portfolio referenced by the exact V1.50 decision
    does not exist in the supplied ``portfolio_repository`` before any
    decision record, lifecycle application, or application record
    interaction occurs."""


class TaskExecutionLifecycleApplicationHistoryPersistenceError(
    TaskExecutionLifecycleCoordinatorError
):
    """Raised EXPLICITLY when the V1.52 lifecycle application has
    ALREADY been actually applied and durably saved, but the subsequent
    V1.53 application-history append failed.

    ``transition_result`` carries the exact genuine
    ``EntityStatusTransitionResult`` of the real application, and
    ``cause`` carries the original exception from the failed
    application-history interaction. No application-history success
    record exists for this application; nothing is hidden, retried, or
    recovered here.
    """

    def __init__(
        self,
        transition_result: EntityStatusTransitionResult,
        cause: BaseException,
    ) -> None:
        self.transition_result = transition_result
        self.cause = cause
        super().__init__(
            "the exact V1.52 lifecycle application was ACTUALLY applied "
            f"and durably saved (portfolio {transition_result.portfolio.id}, "
            f"task {transition_result.entity_id}), but the subsequent V1.53 "
            "application-history append failed; this failure is surfaced "
            f"explicitly (cause: {type(cause).__name__}: {cause}) and no "
            "application-history success record exists for this application"
        )
        self.__cause__ = cause


class TaskExecutionLifecycleOutcome(BaseModel):
    """One explicit deterministic outcome of a coordinated V1.57
    lifecycle run.

    ``decision_record`` is the EXACT durable V1.55 append-only
    human-decision record; ``transition_result`` is the EXACT V1.52
    ``EntityStatusTransitionResult`` of the real durable application;
    ``application_record`` is the EXACT durable V1.53 append-only
    application record for that same result. All identities and
    timestamps inside these three values are preserved exactly; this
    outcome value authorizes nothing further by itself and mutates
    nothing.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_record: TaskExecutionLifecycleDecisionRecord
    transition_result: EntityStatusTransitionResult
    application_record: TaskExecutionLifecycleApplicationRecord


def _require_genuine_decision(decision: object) -> TaskExecutionLifecycleDecision:
    """Require one genuine V1.50 decision before ANY repository or
    boundary interaction; no coercion of any kind."""

    if not isinstance(decision, TaskExecutionLifecycleDecision):
        raise TaskExecutionLifecycleCoordinatorError(
            "a genuine V1.50 TaskExecutionLifecycleDecision is required, "
            f"got {type(decision).__name__}"
        )
    return decision


def coordinate_task_execution_lifecycle(
    decision_record_id: object,
    decision_recorded_at: object,
    decision: object,
    changed_at: object,
    application_record_id: object,
    application_recorded_at: object,
    *,
    decision_repository: TaskExecutionLifecycleDecisionRepository,
    application_repository: TaskExecutionLifecycleApplicationRepository,
    portfolio_repository: PortfolioRepository,
) -> TaskExecutionLifecycleOutcome:
    """Coordinate ONE exact V1.50 human lifecycle decision end-to-end
    through the existing V1.51 -> V1.55 -> V1.52 -> V1.53 boundaries,
    and return one explicit deterministic outcome.

    The sequence follows the module docstring exactly and is invoked in
    this DETERMINISTIC order:

    1. genuine V1.50 ``decision`` (boundary error before any side
       effect);
    2. exactly one ``portfolio_repository.load(decision.portfolio_id)``;
       a missing portfolio raises
       :class:`TaskExecutionLifecycleCoordinatorPortfolioNotFoundError`;
    3. the V1.51
       ``admit_current_task_execution_lifecycle(decision, current)``;
       this is the authoritative CURRENT admission at this boundary; any
       failure raises the V1.51 boundary error with ZERO downstream side
       effects (no decision record, no application, no application
       record);
    4. the V1.55
       ``record_task_execution_lifecycle_decision_durably(decision_record_id,
       decision_recorded_at, decision, repository=decision_repository)``;
       any failure (validation or append) raises the ORIGINAL V1.55 /
       repository error and STOPS BEFORE the V1.52 application is ever
       called (no application, no application record);
    5. the V1.52
       ``apply_admitted_task_execution_lifecycle_durably(admission,
       changed_at, portfolio_repository)``; V1.52 itself reloads the
       Portfolio, RE-EVALUATES the V1.51 admission against that freshly
       loaded CURRENT state, transitions the exact authorized task to
       ``EntityStatus.COMPLETED`` at most once, and saves at most once;
       any failure raises the ORIGINAL V1.52 error and creates NO
       application-history record;
    6. the V1.53
       ``record_task_execution_lifecycle_application_durably(application_record_id,
       application_recorded_at, transition_result,
       repository=application_repository)``; any failure at this step is
       already provably POST-application and is surfaced EXPLICITLY as
       :class:`TaskExecutionLifecycleApplicationHistoryPersistenceError`
       carrying the exact ``transition_result`` and the original cause;
    7. construct and return the exact immutable
       :class:`TaskExecutionLifecycleOutcome` (the exact decision
       record, the exact transition result, the exact application
       record) and STOP.

    Every identity and timestamp is caller-supplied or projected
    verbatim from the exact upstream values: no UUID is generated, no
    wall-clock read, no normalization, no retry, no queue, no worker,
    no fallback, and no provider-specific logic occurs here. Upstream
    boundary semantics (V1.50–V1.56) are invoked, not reimplemented.
    """

    genuine_decision: TaskExecutionLifecycleDecision = (
        _require_genuine_decision(decision)
    )

    current = portfolio_repository.load(genuine_decision.portfolio_id)
    if current is None:
        raise TaskExecutionLifecycleCoordinatorPortfolioNotFoundError(
            f"portfolio not found: {genuine_decision.portfolio_id}"
        )

    admission = admit_current_task_execution_lifecycle(
        genuine_decision, current
    )

    decision_record: TaskExecutionLifecycleDecisionRecord = (
        record_task_execution_lifecycle_decision_durably(
            decision_record_id,
            decision_recorded_at,
            genuine_decision,
            repository=decision_repository,
        )
    )

    transition_result: EntityStatusTransitionResult = (
        apply_admitted_task_execution_lifecycle_durably(
            admission,
            changed_at,
            portfolio_repository,
        )
    )

    try:
        application_record: TaskExecutionLifecycleApplicationRecord = (
            record_task_execution_lifecycle_application_durably(
                application_record_id,
                application_recorded_at,
                transition_result,
                repository=application_repository,
            )
        )
    except Exception as exc:
        raise TaskExecutionLifecycleApplicationHistoryPersistenceError(
            transition_result,
            exc,
        ) from exc

    return TaskExecutionLifecycleOutcome(
        decision_record=decision_record,
        transition_result=transition_result,
        application_record=application_record,
    )
