"""V1.52 — durable application of one admitted COMPLETE_TASK lifecycle transition.

Canonical authority chain:

    V1.49 durable execution result
    -> V1.50 explicit human lifecycle disposition
    -> V1.51 CURRENT-state admission of COMPLETE_TASK
    -> V1.52 durable application to COMPLETED      <-- this module

This is the V1.7-B-shaped durable boundary for the execution lifecycle
chain: exactly ONE repository load, exactly ONE save, and the exact
V1.7-A pure ``transition_entity_status`` semantics as the sole status
mutation authority. The canonical sequence is:

1. require ONE genuine V1.51 ``TaskExecutionLifecycleAdmission``;
2. freshly COMPLETE strict-revalidate the full admission
   (``model_dump(mode="python")`` then
   ``model_validate(payload, strict=True)``) to defeat hostile
   ``model_construct`` payloads; retain ONLY the fresh validated copy;
3. require a genuine timezone-aware ``changed_at``;
4. ``repository.load(validated.portfolio_id)`` exactly ONCE;
5. a missing portfolio raises
   :class:`TaskExecutionLifecyclePortfolioNotFoundError` before any
   transition or save;
6. reconstruct the EXACT genuine V1.50 ``TaskExecutionLifecycleDecision``
   SOLELY from the freshly revalidated V1.51 provenance, using the V1.50
   model's exact canonical constructor / validation semantics;
7. replay the canonical V1.51
   ``admit_current_task_execution_lifecycle`` against the EXACT freshly
   loaded CURRENT Portfolio;
8. transition that SAME loaded Portfolio with the V1.7-A
   ``transition_entity_status`` use case for exactly
   ``validated.authorized_task_id`` to exactly
   ``EntityStatus.COMPLETED`` with exactly the caller-supplied
   ``changed_at``;
9. ``repository.save(result.portfolio)`` exactly once;
10. return the exact V1.7-A ``EntityStatusTransitionResult`` and stop.

CURRENT STATE IS RE-DERIVED, NEVER TRUSTED: the V1.51
``admission.current_task_status`` is HISTORICAL evidence only. Its
applicability is re-derived through the V1.51 replay on the freshly
loaded Portfolio immediately before the transition. Nothing from the
loaded Portfolio is trusted without that replay.

RECONSTRUCTION OF V1.50: the V1.50 decision is reconstructed from
exactly the provenance the V1.51 admission carries (``lifecycle_decision_id``,
``decided_at``, ``execution_record_id``, ``execution_recorded_at``,
``request_id``, ``intent_id``, ``execution_decision_id``,
``portfolio_id``, ``authorized_project_id``, ``authorized_task_id``,
``execution_succeeded``, ``disposition``) — no generated fields, no
caller overrides, no inference. The V1.50 model's own strict
constructor/model-validation semantics are authoritative.

BOUNDARY RULES (mirroring the existing V1.7-B durable boundary):

* the V1.7-B ``transition_entity_status_durably`` helper is NOT used,
  because it would perform a second repository load; the single load
  above is the only load;
* EVERY semantic read uses ONLY the fresh validated admission copy; the
  caller-owned admission is never read back for semantic values;
* the target id, the target status, and the timestamp are authoritative
  from the validated admission, the fixed COMPLETED status, and the
  caller-supplied ``changed_at`` — a caller can never substitute
  another task, another status, or another timestamp;
* ``repository.save`` is called at most once, and only after a
  successful V1.51 replay and a successful V1.7-A transition, with
  exactly ``result.portfolio``;
* the loaded Portfolio is never mutated; the V1.7-A pure use case
  always returns a fresh, independent Portfolio;
* repository load/save exceptions propagate unchanged; V1.51 errors
  and V1.7-A transition errors propagate unchanged;
* no wall-clock read, no UUID generation, no provider / runtime / agent
  / subprocess / shell, no automatic re-attempt, no crash recovery, no
  delivery-guarantee, atomicity, or concurrency claims, no status change
  beyond the exact authorized TASK, no execution of the task, no DB
  schema of any kind;
* there is NO ordering rule between ``changed_at`` and any provenance
  timestamp; the only temporal rule is the one already authoritative in
  V1.7-A: ``changed_at`` must be timezone-aware and must not precede
  the CURRENT task's ``updated_at`` (equality is allowed).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle import (  # noqa: E501
    TaskExecutionLifecycleDecision,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_admission import (  # noqa: E501
    TaskExecutionLifecycleAdmission,
    admit_current_task_execution_lifecycle,
)
from trajectory_os.application.work_breakdown_acceptance import (
    PortfolioRepository,
)
from trajectory_os.domain.entities import EntityStatus
from trajectory_os.domain.entity_status_transition import (
    EntityStatusTransitionResult,
    transition_entity_status,
)

__all__ = [
    "DurableTaskExecutionLifecycleApplicationError",
    "TaskExecutionLifecyclePortfolioNotFoundError",
    "apply_admitted_task_execution_lifecycle_durably",
]


class DurableTaskExecutionLifecycleApplicationError(ValueError):
    """Raised when the durable application of one admitted COMPLETE_TASK
    lifecycle transition fails at this boundary.

    Raised for: a non-genuine V1.51 admission; a hostile / invalid V1.51
    payload failing fresh COMPLETE strict re-validation (hostile
    ``model_construct`` values included); a non-genuine, non-datetime, or
    naive ``changed_at``; and a V1.50 reconstruction failure.

    This error never carries a replacement task, a replacement project,
    a fallback selection, or any inference of another authorization
    target.
    """


class TaskExecutionLifecyclePortfolioNotFoundError(
    DurableTaskExecutionLifecycleApplicationError
):
    """Raised when the portfolio to apply the admitted lifecycle
    transition to does not exist in the repository."""


def _require_genuine_admission(
    admission: object,
) -> TaskExecutionLifecycleAdmission:
    """Require one genuine V1.51 ``TaskExecutionLifecycleAdmission``.

    None, dicts, strings, foreign models, and duck types are rejected
    with the V1.52 boundary error; no coercion of any kind.
    """

    if not isinstance(admission, TaskExecutionLifecycleAdmission):
        raise DurableTaskExecutionLifecycleApplicationError(
            "a genuine V1.51 TaskExecutionLifecycleAdmission is required, "
            f"got {type(admission).__name__}"
        )
    return admission


def _revalidate_v151_admission(
    admission: TaskExecutionLifecycleAdmission,
) -> TaskExecutionLifecycleAdmission:
    """Freshly strict-revalidate the COMPLETE V1.51 payload.

    Defeats hostile ``model_construct`` payloads. Every semantic V1.52
    read afterwards must use ONLY the fresh validated copy returned
    here; the caller-owned admission is never read back for semantic
    values.
    """

    try:
        payload: object = admission.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise DurableTaskExecutionLifecycleApplicationError(
            "the supplied V1.51 admission is not the V1.51 value shape"
        ) from exc

    try:
        return TaskExecutionLifecycleAdmission.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise DurableTaskExecutionLifecycleApplicationError(
            "the supplied V1.51 admission failed fresh COMPLETE "
            "strict re-validation"
        ) from exc


def _require_genuine_changed_at(changed_at: object) -> datetime:
    """Require one genuine timezone-aware ``changed_at``.

    Non-datetime values and naive datetimes are rejected with the V1.52
    boundary error; no conversion or normalization of any kind.
    """

    if not isinstance(changed_at, datetime):
        raise DurableTaskExecutionLifecycleApplicationError(
            "changed_at must already be a genuine datetime instance, "
            f"got {type(changed_at).__name__}"
        )
    if changed_at.tzinfo is None or changed_at.utcoffset() is None:
        raise DurableTaskExecutionLifecycleApplicationError(
            "changed_at must be a timezone-aware datetime with a "
            f"non-None UTC offset (got {changed_at!r})"
        )
    return changed_at


def _reconstruct_v150_decision(
    admission: TaskExecutionLifecycleAdmission,
) -> TaskExecutionLifecycleDecision:
    """Reconstruct the exact genuine V1.50 decision solely from the fresh
    validated V1.51 provenance.

    Only the provenance the V1.51 admission carries is used, via the
    V1.50 model's exact canonical constructor / validation semantics.
    No generated fields, no caller overrides, no inference, and no
    fallback of any kind.
    """

    try:
        return TaskExecutionLifecycleDecision(
            lifecycle_decision_id=admission.lifecycle_decision_id,
            decided_at=admission.decided_at,
            execution_record_id=admission.execution_record_id,
            execution_recorded_at=admission.execution_recorded_at,
            request_id=admission.request_id,
            intent_id=admission.intent_id,
            execution_decision_id=admission.execution_decision_id,
            portfolio_id=admission.portfolio_id,
            authorized_project_id=admission.authorized_project_id,
            authorized_task_id=admission.authorized_task_id,
            execution_succeeded=admission.execution_succeeded,
            disposition=admission.disposition,
        )
    except ValidationError as exc:
        raise DurableTaskExecutionLifecycleApplicationError(
            "the V1.51 admission provenance does not reconstruct a "
            "genuine V1.50 TaskExecutionLifecycleDecision"
        ) from exc


def apply_admitted_task_execution_lifecycle_durably(
    admission: object,
    changed_at: object,
    repository: PortfolioRepository,
) -> EntityStatusTransitionResult:
    """Apply ONE genuine V1.51 COMPLETE_TASK admission durably to
    ``EntityStatus.COMPLETED``, and STOP.

    The sequence follows the module docstring exactly: genuine admission
    -> fresh COMPLETE strict revalidation of the full admission ->
    genuine timezone-aware ``changed_at`` -> exactly ONE
    ``repository.load(validated.portfolio_id)`` -> missing-portfolio
    boundary error -> reconstruction of the exact genuine V1.50 decision
    solely from the V1.51 provenance -> canonical V1.51 replay against
    the EXACT freshly loaded CURRENT Portfolio -> V1.7-A
    ``transition_entity_status`` on that SAME loaded Portfolio for
    exactly ``validated.authorized_task_id`` to exactly
    ``EntityStatus.COMPLETED`` with exactly the caller-supplied
    ``changed_at`` -> ``repository.save(result.portfolio)`` exactly
    once -> return the exact V1.7-A
    ``EntityStatusTransitionResult`` -> stop.

    The V1.51 ``admission.current_task_status`` is historical evidence
    only; the CURRENT applicability is re-derived through the V1.51
    replay on the freshly loaded Portfolio immediately before the
    transition. The loaded Portfolio is never mutated; the V1.7-A pure
    use case returns a fresh, independent Portfolio. No second
    repository load, no second save, no provider / runtime / agent /
    subprocess / shell, no automatic re-attempt, no crash recovery, no
    delivery-guarantee or atomicity claims, no cross-entity status
    changes, no re-execution.
    """

    genuine: TaskExecutionLifecycleAdmission = _require_genuine_admission(admission)

    validated: TaskExecutionLifecycleAdmission = _revalidate_v151_admission(genuine)

    applied_changed_at: datetime = _require_genuine_changed_at(changed_at)

    current = repository.load(validated.portfolio_id)
    if current is None:
        raise TaskExecutionLifecyclePortfolioNotFoundError(
            f"portfolio not found: {validated.portfolio_id}"
        )

    decision: TaskExecutionLifecycleDecision = _reconstruct_v150_decision(validated)

    admit_current_task_execution_lifecycle(decision, current)

    result = transition_entity_status(
        current,
        validated.authorized_task_id,
        EntityStatus.COMPLETED,
        applied_changed_at,
    )

    repository.save(result.portfolio)

    return result
