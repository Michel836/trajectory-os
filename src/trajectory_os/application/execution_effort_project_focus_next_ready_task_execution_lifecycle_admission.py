"""V1.51 — CURRENT-state admission of one COMPLETE_TASK lifecycle decision.

Canonical authority chain:

    V1.49 durable execution result
    -> V1.50 explicit human lifecycle disposition
    -> V1.51 CURRENT-state admission of COMPLETE_TASK   <-- this module
    -> later durable lifecycle mutation to COMPLETED

V1.51 is PURE and MUST STOP BEFORE MUTATION. It admits that ONE genuine,
freshly strict-revalidated V1.50 ``TaskExecutionLifecycleDecision`` with an
explicit ``COMPLETE_TASK`` disposition is applicable against the CURRENT
canonical Portfolio — it performs no lifecycle change, changes no entity
status, and executes nothing. Even an admitted value is an immutable
evidence value only: nothing it contains is itself authoritative for a
later durable mutation boundary.

CORE RULE: the decision carries exactly one authorized identity triple
(``portfolio_id``, ``authorized_project_id``, ``authorized_task_id``). V1.51
resolves exactly those identities in the CURRENT Portfolio. No fallback
selection exists: no alternative task, project, or portfolio is ever
inspected, ranked, or substituted. A missing, mismatched, or non-conforming
target is a boundary error, not a reselection.

AUTHORITATIVE BASIS FOR PROJECT->TASK MEMBERSHIP (existing repository
semantics):

The repository's canonical relation model defines ``BELONGS_TO``
containment edges whose direction is ``source_id`` = child and
``target_id`` = parent (the same orientation the existing work-breakdown
projection and work-breakdown materialization both reuse), and
``Portfolio.relations`` is the canonical CURRENT relation store. V1.51
therefore admits the exact authorized task ONLY when the CURRENT
Portfolio's canonical relations contain a ``BELONGS_TO`` row with
``source_id`` exactly the authorized task identity and ``target_id``
exactly the authorized project identity. A task that exists but belongs
to a DIFFERENT project, or for which no membership row exists, is a
boundary error: no other relation type, relation direction, task, or
project is ever inspected, inferred from, or substituted.

AUTHORITATIVE BASIS FOR STATUS ADMISSION (existing repository semantics):

The existing pure status-mutation authority (V1.7-A) permits any two
DISTINCT ``EntityStatus`` values and rejects only same-status changes.
It is the sole authoritative semantics V1.51 may rely on:

- an already-COMPLETED task is rejected, because a COMPLETED -> COMPLETED
  change is exactly the same-status change the existing authority rejects;
- every other current status (including CANCELLED and ARCHIVED) is
  admissible here, because the existing repository semantics make no
  claim forbidding a later change from those statuses to COMPLETED, and
  V1.51 invents no lifecycle graph and no FAILED status of any kind.

SOLE SEMANTIC INPUTS (exactly two):

1. ONE genuine V1.50 ``TaskExecutionLifecycleDecision`` — authoritative for
   the complete provenance chain (``lifecycle_decision_id``, ``decided_at``,
   ``execution_record_id``, ``execution_recorded_at``, ``request_id``,
   ``intent_id``, ``execution_decision_id``), the portfolio identity, the
   authorized project identity, the authorized task identity, the exact
   execution outcome, and the explicit disposition;
2. ONE CURRENT canonical Portfolio — authoritative ONLY for current entity
   existence, type, status, and the CURRENT ``BELONGS_TO`` membership
   relation of the exact authorized identities.

FRESH DECISION REVALIDATION: the V1.50 decision is freshly COMPLETE
strict-revalidated (``model_dump(mode="python")`` then
``model_validate(payload, strict=True)``) to defeat hostile
``model_construct`` payloads. After that, EVERY semantic read uses ONLY
the retained fresh validated copy; the caller-owned decision is never read
back for semantic values.

ADMISSION CHECKS (boundary errors raised in exactly this order):

1. genuine V1.50 ``TaskExecutionLifecycleDecision``;
2. fresh COMPLETE strict revalidation of the decision; retain the fresh
   validated copy only;
3. the retained disposition is ``COMPLETE_TASK`` (``NO_LIFECYCLE_CHANGE``
   is rejected with a boundary error);
4. the retained ``execution_succeeded`` is exactly ``True``;
5. genuine CURRENT canonical Portfolio;
6. ``portfolio.id`` exactly equals the retained decision ``portfolio_id``;
7. the exact authorized project identity exists in the CURRENT Portfolio
   and is a ``PROJECT``-typed entity;
8. the exact authorized task identity exists in the CURRENT Portfolio and
   is a ``TASK``-typed entity;
9. the exact authorized task CURRENTLY carries a ``BELONGS_TO``
   membership relation to the exact authorized project in the CURRENT
   Portfolio's canonical ``relations`` (``source_id`` = exact authorized
   task, ``target_id`` = exact authorized project — the existing
   repository containment orientation)
   — a missing, reversed, or differently-typed row is a boundary error;
10. the exact authorized task does NOT already carry ``EntityStatus.COMPLETED``;
11. construct the exact immutable admission value and return it; stop.

Temporal semantics: ``decided_at`` and ``execution_recorded_at`` are
provenance only, with their exact original UTC offsets preserved verbatim.
There is NO ordering rule between them and any current state, NO TTL, NO
expiry, NO comparison against the wall clock, and NO generated identity
or timestamp.

V1.51 does NOT: mutate the Portfolio or any entity status; call the pure
or the durable status-mutation use cases of any kind; persist anything;
touch SQLite or any repository; execute or re-execute the task; invoke
any provider / runtime / agent / subprocess / shell; generate a random
identity or read the wall clock; add recovery, reselection,
supersession, or temporal validity of any kind; invent a lifecycle graph,
a FAILED status, or a replacement target; or broaden into durable mutation.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    ValidationError,
    model_validator,
)

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle import (  # noqa: E501
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDisposition,
)
from trajectory_os.domain.entities import EntityStatus, EntityType
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType

__all__ = [
    "TaskExecutionLifecycleAdmission",
    "TaskExecutionLifecycleAdmissionError",
    "admit_current_task_execution_lifecycle",
]


class TaskExecutionLifecycleAdmissionError(ValueError):
    """Raised when the CURRENT-state admission of the exact COMPLETE_TASK
    decision CANNOT be performed because a boundary input or the canonical
    CURRENT state is invalid for admission.

    Raised for: a non-genuine V1.50 decision; a hostile / invalid V1.50
    payload failing fresh COMPLETE strict re-validation (hostile
    ``model_construct`` values included); a retained disposition that is not
    ``COMPLETE_TASK``; a retained ``execution_succeeded`` that is not
    exactly ``True``; a non-genuine CURRENT Portfolio; an exact
    portfolio.id mismatch; an authorized project or task that is missing
    from the CURRENT Portfolio or carries a different entity type; an
    authorized task that does not CURRENTLY carry a ``BELONGS_TO``
    membership relation to the exact authorized project (including a task
    that belongs to a different project); and an authorized task that
    already carries ``EntityStatus.COMPLETED``.

    This error never carries a replacement task, a replacement project, a
    fallback selection, or any inference of another authorization target.
    """


class TaskExecutionLifecycleAdmission(BaseModel):
    """One immutable V1.51 CURRENT-state admission value for the EXACT
    COMPLETE_TASK decision supplied at the boundary.

    ``lifecycle_decision_id``, ``decided_at`` (exact original offset
    preserved), ``execution_record_id``, ``execution_recorded_at`` (exact
    original offset preserved), ``request_id``, ``intent_id``,
    ``execution_decision_id``, ``portfolio_id``,
    ``authorized_project_id``, ``authorized_task_id``,
    ``execution_succeeded``, and ``disposition`` are EXACT provenance
    projections of the freshly revalidated V1.50 decision; both
    timestamps are provenance only and carry no temporal-validity
    semantics.

    ``current_task_status`` is the minimal CURRENT evidence: the exact
    status the authorized task carried in the supplied CURRENT Portfolio
    at the moment of admission. It is always a non-COMPLETED status,
    because an already-COMPLETED task is a boundary error, and it
    authorizes nothing beyond proving the decision was applicable in
    CURRENT state.

    An admitted value is an immutable evidence value only: it mutates
    nothing and executes nothing by itself.
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
    current_task_status: EntityStatus

    @model_validator(mode="after")
    def _enforce_admitted_invariant(
        self,
    ) -> TaskExecutionLifecycleAdmission:
        if self.disposition is not TaskExecutionLifecycleDisposition.COMPLETE_TASK:
            raise ValueError(
                "a V1.51 admission is admitted only for an explicit COMPLETE_TASK disposition"
            )
        if self.execution_succeeded is not True:
            raise ValueError("a V1.51 admission requires execution_succeeded to be exactly True")
        if self.current_task_status is EntityStatus.COMPLETED:
            raise ValueError(
                "a V1.51 admission can never carry an already-COMPLETED current task status"
            )
        for name in ("decided_at", "execution_recorded_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(
                    f"{name} must be a timezone-aware datetime with a non-None UTC offset"
                )
        return self


def _require_genuine_decision(
    decision: object,
) -> TaskExecutionLifecycleDecision:
    """Require one genuine V1.50 decision.

    None, dicts, strings, foreign models, and duck types are rejected with
    the V1.51 boundary error; no coercion of any kind.
    """

    if not isinstance(decision, TaskExecutionLifecycleDecision):
        raise TaskExecutionLifecycleAdmissionError(
            "a genuine V1.50 TaskExecutionLifecycleDecision is required, "
            f"got {type(decision).__name__}"
        )
    return decision


def _revalidate_v150_decision(
    decision: TaskExecutionLifecycleDecision,
) -> TaskExecutionLifecycleDecision:
    """Freshly strict-revalidate the COMPLETE V1.50 payload.

    Defeats hostile ``model_construct`` payloads. Every semantic V1.50 read
    afterwards must use ONLY the fresh validated copy returned here; the
    caller-owned decision is never read back for semantic values.
    """

    try:
        payload: object = decision.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise TaskExecutionLifecycleAdmissionError(
            "the supplied V1.50 decision is not the V1.50 value shape"
        ) from exc

    try:
        return TaskExecutionLifecycleDecision.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise TaskExecutionLifecycleAdmissionError(
            "the supplied V1.50 decision failed fresh COMPLETE strict re-validation"
        ) from exc


def _require_genuine_portfolio(portfolio: object) -> Portfolio:
    """Require a genuine CURRENT canonical Portfolio instance."""

    if not isinstance(portfolio, Portfolio):
        raise TaskExecutionLifecycleAdmissionError(
            f"a genuine CURRENT Portfolio is required, got {type(portfolio).__name__}"
        )
    return portfolio


def admit_current_task_execution_lifecycle(
    decision: TaskExecutionLifecycleDecision,
    portfolio: Portfolio,
) -> TaskExecutionLifecycleAdmission:
    """Admit ONE genuine COMPLETE_TASK lifecycle decision against the
    CURRENT canonical Portfolio, and STOP.

    Pure and deterministic (same inputs -> identical value results);
    repeated identical calls yield identical values. No repository, no
    persistence, no execution / dispatch / queue / scheduler / agent /
    provider surface, no shell, no status change of any kind, no portfolio
    or entity mutation, no fallback selection (no alternative identity is
    ever inspected, ranked, or substituted), no temporal / expiration / TTL
    rule (both timestamps are provenance only), no wall-clock read, and no
    generated identity or timestamp.

    The checks and their order follow the module docstring exactly. The
    exact authorized task must CURRENTLY carry the canonical
    ``BELONGS_TO`` membership relation to the exact authorized project
    (existing repository relation semantics); a missing or mismatched
    row is rejected with no fallback to any other task, project, or
    relation.

    The only status rejection is an already-COMPLETED task, which the
    existing repository status-mutation authority already makes
    authoritative by rejecting same-status changes from COMPLETED. Every
    other current status is admissible; no lifecycle graph is applied
    here.

    The returned value is an immutable evidence value only: it mutates
    nothing and executes nothing.
    """

    genuine: TaskExecutionLifecycleDecision = _require_genuine_decision(decision)

    validated: TaskExecutionLifecycleDecision = _revalidate_v150_decision(genuine)

    if validated.disposition is not TaskExecutionLifecycleDisposition.COMPLETE_TASK:
        raise TaskExecutionLifecycleAdmissionError(
            "only an explicit COMPLETE_TASK disposition is admissible "
            f"at the V1.51 boundary, got {validated.disposition.value}"
        )

    if validated.execution_succeeded is not True:
        raise TaskExecutionLifecycleAdmissionError(
            "COMPLETE_TASK admission requires execution_succeeded to be exactly True"
        )

    current: Portfolio = _require_genuine_portfolio(portfolio)

    if current.id != validated.portfolio_id:
        raise TaskExecutionLifecycleAdmissionError(
            "the supplied portfolio does not exactly match the V1.50 decision portfolio_id"
        )

    project_entity = current.get_entity(validated.authorized_project_id)
    if project_entity is None:
        raise TaskExecutionLifecycleAdmissionError(
            "the authorized project is missing from the CURRENT portfolio; "
            "no alternative identity is inspected or substituted"
        )
    if project_entity.entity_type is not EntityType.PROJECT:
        raise TaskExecutionLifecycleAdmissionError(
            "the entity at the authorized project identity does not carry "
            "the PROJECT entity type in the CURRENT portfolio"
        )

    task_entity = current.get_entity(validated.authorized_task_id)
    if task_entity is None:
        raise TaskExecutionLifecycleAdmissionError(
            "the authorized task is missing from the CURRENT portfolio; "
            "no alternative identity is inspected or substituted"
        )
    if task_entity.entity_type is not EntityType.TASK:
        raise TaskExecutionLifecycleAdmissionError(
            "the entity at the authorized task identity does not carry "
            "the TASK entity type in the CURRENT portfolio"
        )

    # Existing repository membership semantics: BELONGS_TO containment
    # edges with source_id = child (the task) and target_id = parent
    # (the project), read from the CURRENT Portfolio's canonical
    # relations. Exact identity match only; nothing is inferred.
    membership_present = any(
        relation.source_id == validated.authorized_task_id
        and relation.target_id == validated.authorized_project_id
        and relation.relation_type is RelationType.BELONGS_TO
        for relation in current.relations
    )
    if not membership_present:
        raise TaskExecutionLifecycleAdmissionError(
            "the authorized task does not CURRENTLY carry a BELONGS_TO "
            "membership relation to the authorized project in the "
            "CURRENT portfolio; no alternative task, project, or relation "
            "is inspected or substituted"
        )

    if task_entity.status is EntityStatus.COMPLETED:
        raise TaskExecutionLifecycleAdmissionError(
            "the authorized task already carries EntityStatus.COMPLETED "
            "in the CURRENT portfolio; an identical status change is what "
            "the existing status-mutation authority rejects"
        )

    return TaskExecutionLifecycleAdmission(
        lifecycle_decision_id=validated.lifecycle_decision_id,
        decided_at=validated.decided_at,
        execution_record_id=validated.execution_record_id,
        execution_recorded_at=validated.execution_recorded_at,
        request_id=validated.request_id,
        intent_id=validated.intent_id,
        execution_decision_id=validated.execution_decision_id,
        portfolio_id=validated.portfolio_id,
        authorized_project_id=validated.authorized_project_id,
        authorized_task_id=validated.authorized_task_id,
        execution_succeeded=validated.execution_succeeded,
        disposition=validated.disposition,
        current_task_status=task_entity.status,
    )
