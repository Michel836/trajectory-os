"""V1.45 — CURRENT applicability preflight for a human-authorized execution.

Pure current-state safety gate: determine whether the EXACT TASK already
authorized by ONE genuine V1.44
``PortfolioProjectFocusNextReadyTaskExecutionIntent`` is STILL applicable
for execution against the CURRENT canonical Portfolio.

V1.45 is a PURE CURRENT-STATE SAFETY GATE ONLY. Even APPLICABLE remains an
immutable value. V1.45 MUST NOT execute anything.

CORE RULE: a stale or inapplicable human authorization MUST NEVER be
transferred, rebound, redirected, substituted, or automatically applied to
another TASK. No fallback selection exists: no alternative task is ever
inspected, ranked, or substituted.

Authorities (exactly two):

1. ONE genuine V1.44
   ``PortfolioProjectFocusNextReadyTaskExecutionIntent`` — authoritative
   for intent provenance, the authorized project identity, and the
   authorized task identity;
2. ONE genuine matching CURRENT canonical Portfolio — authoritative
   ONLY for current entity existence/type/status, current canonical WBS
   membership (through the repository's ``build_work_breakdown``
   authority), current relations, and current counterpart type/status.

The exact fixed V1.39 constraint policy (same domain enums) is
IMPLEMENTED IN-MODULE directly against CURRENT canonical relations: the
V1.38/V1.39 boundaries are not called and no V1.38/V1.39 object is
fabricated. Incoming constraining relation types: BLOCKS, PRECEDES
(counterpart = relation.source_id). Outgoing constraining relation
types: DEPENDS_ON, REQUIRES, WAITING_FOR (counterpart = relation.target_id).
Every other relation type is NON-CONSTRAINING. A constraint is satisfied
EXACTLY when the counterpart status is EntityStatus.COMPLETED; every
other status (including CANCELLED and ARCHIVED) is unsatisfied. A
constraining relation whose counterpart cannot be resolved is a
BOUNDARY ERROR, never reinterpreted as unsatisfied or CONSTRAINED.

Constraint ORDER: one pass over CURRENT Portfolio.relations in canonical
tuple order collecting incoming BLOCKS/PRECEDES for the authorized task,
then a second pass in canonical tuple order collecting outgoing
DEPENDS_ON/REQUIRES/WAITING_FOR. No sorting, no UUID/type ordering, no
deduplication, no invented merged order.

Temporal semantics: NO current-time or temporal validity rule is
invented. authorized_at is provenance only; an earlier or later
authorized_at has zero applicability effect.

State derivation order (boundary errors raised before any state):
genuine V1.44 intent -> fresh COMPLETE strict revalidation -> retain the
fresh copy only -> genuine CURRENT Portfolio -> exact portfolio.id
match (mismatch is a boundary error) -> project existence/type ->
task existence/type -> canonical WBS membership via
build_work_breakdown (a structurally incoherent WBS is a boundary error
and is NEVER reinterpreted as TASK_NOT_IN_AUTHORIZED_PROJECT) ->
exact CURRENT task status (non-ACTIVE -> INELIGIBLE_STATUS with an
empty constraint tuple and zero unsatisfied count) -> fixed-policy
constraints (only for an ACTIVE task) -> CONSTRAINED or APPLICABLE.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    ValidationError,
    model_validator,
)

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_intent import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskExecutionIntent,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import (
    WorkBreakdownError,
    WorkBreakdownNode,
    WorkBreakdownStructure,
    build_work_breakdown,
)

__all__ = [
    "PortfolioProjectFocusNextReadyTaskExecutionApplicability",
    "PortfolioProjectFocusNextReadyTaskExecutionApplicabilityConstraint",
    "PortfolioProjectFocusNextReadyTaskExecutionApplicabilityError",
    "PortfolioProjectFocusNextReadyTaskExecutionApplicabilityState",
    "evaluate_current_next_ready_task_execution_applicability",
]

# Exact fixed V1.39 policy, implemented in-module: constraining relation
# types per direction against the EXACT authorized CURRENT task.
_INCOMING_CONSTRAINING: frozenset[RelationType] = frozenset(
    {RelationType.BLOCKS, RelationType.PRECEDES}
)
_OUTGOING_CONSTRAINING: frozenset[RelationType] = frozenset(
    {RelationType.DEPENDS_ON, RelationType.REQUIRES, RelationType.WAITING_FOR}
)

# States decided before the task could be resolved in the CURRENT
# Portfolio: no task status, no constraint tuple, no count.
_PRE_RESOLUTION_STATES = frozenset(
    {
        "PROJECT_MISSING",
        "PROJECT_TYPE_MISMATCH",
        "TASK_MISSING",
        "TASK_TYPE_MISMATCH",
    }
)

# States decided before constraint evaluation: constraints must be None.
_NO_CONSTRAINT_TUPLE_STATES = _PRE_RESOLUTION_STATES | frozenset(
    {"TASK_NOT_IN_AUTHORIZED_PROJECT"}
)


class PortfolioProjectFocusNextReadyTaskExecutionApplicabilityError(
    ValueError
):
    """Raised when the CURRENT applicability preflight CANNOT be
    performed because a boundary input or the canonical state is
    structurally invalid.

    Raised for: a non-genuine V1.44 intent; a hostile / invalid V1.44
    payload failing fresh COMPLETE strict re-validation; a non-genuine
    Portfolio; an exact portfolio.id mismatch; a structurally
    incoherent canonical WBS (a build_work_breakdown failure, causal
    exception preserved); or a constraining relation whose counterpart
    cannot be resolved in the CURRENT Portfolio.

    Normal CURRENT-state changes — missing project/task, type drift,
    membership drift, non-ACTIVE status, or newly unsatisfied
    constraints — are RESULT STATES, never exceptions. This error never
    carries a replacement task, fallback selection, or any inference of
    a current, latest, or effective authorization.
    """


class PortfolioProjectFocusNextReadyTaskExecutionApplicabilityState(
    StrEnum
):
    """The exact V1.45 CURRENT applicability state of the EXACT task
    authorized by the V1.44 intent. No other state exists, and nothing
    here selects, ranks, or substitutes any task."""

    APPLICABLE = "applicable"
    PROJECT_MISSING = "project_missing"
    PROJECT_TYPE_MISMATCH = "project_type_mismatch"
    TASK_MISSING = "task_missing"
    TASK_TYPE_MISMATCH = "task_type_mismatch"
    TASK_NOT_IN_AUTHORIZED_PROJECT = "task_not_in_authorized_project"
    INELIGIBLE_STATUS = "ineligible_status"
    CONSTRAINED = "constrained"

State = PortfolioProjectFocusNextReadyTaskExecutionApplicabilityState


class PortfolioProjectFocusNextReadyTaskExecutionApplicabilityConstraint(
    BaseModel
):
    """ONE exact V1.45 constraint row for the EXACT authorized task.

    ``relation_id`` and ``relation_type`` are the exact CURRENT
    canonical constraining relation identity and type. The counterpart
    fields are the EXACT CURRENT canonical counterpart identity, type,
    and status resolved from the CURRENT Portfolio; the counterpart may
    be any valid entity type.

    ``satisfied`` is EXACTLY ``counterpart_status is
    EntityStatus.COMPLETED``: nothing else satisfies a constraint;
    CANCELLED and ARCHIVED are never successful completion. No score,
    rank, title, description, priority, urgency, or confidence is
    attached; no generated identity or timestamp.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    relation_id: UUID
    relation_type: RelationType
    counterpart_entity_id: UUID
    counterpart_entity_type: EntityType
    counterpart_status: EntityStatus
    satisfied: bool

    @model_validator(mode="after")
    def _enforce_constraint_invariants(
        self,
    ) -> (
        PortfolioProjectFocusNextReadyTaskExecutionApplicabilityConstraint
    ):
        if self.satisfied is not (
            self.counterpart_status is EntityStatus.COMPLETED
        ):
            raise ValueError(
                "a constraint is satisfied exactly when its counterpart "
                "status is COMPLETED; CANCELLED and ARCHIVED are not "
                "successful completion"
            )
        return self


# short module alias used by helpers (defined after the class)
Constraint = PortfolioProjectFocusNextReadyTaskExecutionApplicabilityConstraint
ConstraintRows = tuple[Constraint, ...]


class PortfolioProjectFocusNextReadyTaskExecutionApplicability(BaseModel):
    """The complete immutable V1.45 CURRENT applicability preflight
    result for the EXACT task authorized by the V1.44 intent.

    ``intent_id``, ``authorized_at`` (exact original offset preserved),
    ``decision_id``, ``portfolio_id``, ``authorized_project_id``, and
    ``authorized_task_id`` are EXACT projections of the freshly
    revalidated V1.44 intent (provenance only; ``authorized_at``
    carries no temporal validity semantics).

    Per-state invariants are enforced so that no state silently
    represents another semantic condition:

    - PROJECT_MISSING / PROJECT_TYPE_MISMATCH / TASK_MISSING /
      TASK_TYPE_MISMATCH: task_status is None; constraints is None;
      unsatisfied_constraint_count is None;
    - TASK_NOT_IN_AUTHORIZED_PROJECT: task_status is the exact CURRENT
      task status (any value); constraints is None; count is None;
    - INELIGIBLE_STATUS: task_status is the exact CURRENT non-ACTIVE
      task status; constraints == (); count == 0;
    - CONSTRAINED: task_status is EntityStatus.ACTIVE; constraints is a
      tuple with at least one unsatisfied row; count >= 1 and exactly
      equals the number of rows with satisfied == False;
    - APPLICABLE: task_status is EntityStatus.ACTIVE; constraints is a
      tuple (possibly empty) in which every row is satisfied; count is
      0.

    An APPLICABLE result is an immutable evidence value only: it
    authorizes and executes nothing by itself.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    intent_id: UUID
    authorized_at: datetime
    decision_id: UUID
    portfolio_id: UUID
    authorized_project_id: UUID
    authorized_task_id: UUID
    task_status: EntityStatus | None
    applicability_state: (
        PortfolioProjectFocusNextReadyTaskExecutionApplicabilityState
    )
    constraints: (
        tuple[
            PortfolioProjectFocusNextReadyTaskExecutionApplicabilityConstraint,
            ...
        ]
        | None
    )
    unsatisfied_constraint_count: StrictInt | None

    @model_validator(mode="after")
    def _enforce_applicability_invariants(
        self,
    ) -> PortfolioProjectFocusNextReadyTaskExecutionApplicability:
        state = self.applicability_state

        if state.name in _PRE_RESOLUTION_STATES:
            if (
                self.task_status is not None
                or self.constraints is not None
                or self.unsatisfied_constraint_count is not None
            ):
                raise ValueError(
                    f"{state.value} is decided before the task could be "
                    "resolved in the CURRENT portfolio: task_status, "
                    "constraints, and unsatisfied_constraint_count "
                    "must all be None"
                )
            return self

        if state.name in _NO_CONSTRAINT_TUPLE_STATES:
            if self.task_status is None:
                raise ValueError(
                    f"{state.value} must carry the exact CURRENT task "
                    "status"
                )
            if (
                self.constraints is not None
                or self.unsatisfied_constraint_count is not None
            ):
                raise ValueError(
                    f"{state.value} is decided before constraint "
                    "evaluation: constraints and "
                    "unsatisfied_constraint_count must both be None"
                )
            return self

        if state is State.INELIGIBLE_STATUS:
            if (
                self.task_status is None
                or self.task_status is EntityStatus.ACTIVE
            ):
                raise ValueError(
                    "INELIGIBLE_STATUS must carry the exact CURRENT "
                    "non-ACTIVE task status"
                )
            if self.constraints != ():
                raise ValueError(
                    "INELIGIBLE_STATUS carries exactly an empty "
                    "constraint tuple: a non-ACTIVE task is not "
                    "lifecycle-eligible and no constraint is collected"
                )
            if self.unsatisfied_constraint_count != 0:
                raise ValueError(
                    "INELIGIBLE_STATUS must carry zero unsatisfied "
                    "constraints"
                )
            return self

        # From here on only APPLICABLE and CONSTRAINED remain.
        if state not in (State.APPLICABLE, State.CONSTRAINED):
            raise ValueError(
                "applicability_state must be one of exactly the eight "
                "defined V1.45 states"
            )
        if self.task_status is not EntityStatus.ACTIVE:
            raise ValueError(
                f"{state.value} requires the EXACT authorized task to "
                "carry the exact CURRENT status EntityStatus.ACTIVE"
            )
        if self.constraints is None:
            raise ValueError(
                f"{state.value} requires an exact (possibly empty) "
                "constraint tuple"
            )
        if self.unsatisfied_constraint_count is None:
            raise ValueError(
                f"{state.value} requires an exact "
                "unsatisfied_constraint_count"
            )

        unsatisfied = sum(
            1 for row in self.constraints if not row.satisfied
        )
        if self.unsatisfied_constraint_count != unsatisfied:
            raise ValueError(
                "unsatisfied_constraint_count must exactly equal the "
                "number of constraints with satisfied == False"
            )
        if state is State.APPLICABLE:
            if unsatisfied != 0:
                raise ValueError(
                    "APPLICABLE requires every collected constraint to "
                    "be satisfied and a zero unsatisfied count"
                )
        elif unsatisfied < 1:
            raise ValueError(
                "CONSTRAINED requires at least one unsatisfied "
                "constraint"
            )
        return self



def _require_genuine_intent(
    intent: object,
) -> PortfolioProjectFocusNextReadyTaskExecutionIntent:
    """Require a genuine V1.44 intent.

    None, dicts, strings, foreign models, and duck types are rejected
    with the V1.45 boundary error; no coercion of any kind.
    """

    if not isinstance(
        intent, PortfolioProjectFocusNextReadyTaskExecutionIntent
    ):
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionApplicabilityError(
                "a genuine V1.44 "
                "PortfolioProjectFocusNextReadyTaskExecutionIntent is "
                f"required, got {type(intent).__name__}"
            )
        )
    return intent


def _revalidate_v144_intent(
    intent: PortfolioProjectFocusNextReadyTaskExecutionIntent,
) -> PortfolioProjectFocusNextReadyTaskExecutionIntent:
    """Freshly strict-revalidate the COMPLETE V1.44 payload.

    Defeats hostile model_construct payloads and any state that
    ordinary construction could never produce. Every semantic V1.44
    read afterwards must use ONLY the fresh validated copy returned
    here.
    """

    try:
        payload: object = intent.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionApplicabilityError(
                "the supplied V1.44 intent is not the V1.44 shape"
            )
        ) from exc

    try:
        return (
            PortfolioProjectFocusNextReadyTaskExecutionIntent.model_validate(payload, strict=True)
        )
    except ValidationError as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionApplicabilityError(
                "the supplied V1.44 intent failed fresh COMPLETE "
                "strict re-validation"
            )
        ) from exc


def _require_genuine_portfolio(portfolio: object) -> Portfolio:
    """Require a genuine CURRENT canonical Portfolio instance."""

    if not isinstance(portfolio, Portfolio):
        raise PortfolioProjectFocusNextReadyTaskExecutionApplicabilityError(
            "a genuine CURRENT Portfolio is required, "
            f"got {type(portfolio).__name__}"
        )
    return portfolio


def _wbs_included_ids(root: WorkBreakdownNode) -> frozenset[UUID]:
    """Collect every entity id contained in the projected WBS,
    including the root. Pure structural traversal."""

    included: set[UUID] = {root.entity_id}
    stack: list[WorkBreakdownNode] = [root]
    while stack:
        node = stack.pop()
        for child in node.children:
            included.add(child.entity_id)
            stack.append(child)
    return frozenset(included)



def _resolve_counterpart(
    lookup: dict[UUID, TrajectoryEntity],
    counterpart_id: UUID,
    relation: TrajectoryRelation,
) -> TrajectoryEntity:
    """Resolve a constraining relation's counterpart in the CURRENT
    Portfolio.

    An unresolvable counterpart is a V1.45 boundary error; it is never
    reinterpreted as unsatisfied or as CONSTRAINED.
    """

    counterpart = lookup.get(counterpart_id)
    if counterpart is None:
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionApplicabilityError(
                "a constraining relation references a counterpart "
                f"entity that cannot be resolved in the CURRENT "
                f"portfolio: relation {relation.id} of type "
                f"{relation.relation_type.value}, counterpart "
                f"{counterpart_id}"
            )
        )
    return counterpart


def _collect_constraints(
    portfolio: Portfolio,
    lookup: dict[UUID, TrajectoryEntity],
    authorized_task_id: UUID,
) -> ConstraintRows:
    """Collect EXACTLY the fixed-policy constraint rows against the
    EXACT authorized task from CURRENT canonical relations.

    Deterministic two-pass order matching the V1.39 policy: first pass
    over portfolio.relations in canonical tuple order collecting
    incoming BLOCKS / PRECEDES (counterpart = source_id); second pass
    in canonical tuple order collecting outgoing DEPENDS_ON /
    REQUIRES / WAITING_FOR (counterpart = target_id). Every other
    relation type is NON-CONSTRAINING and ignored. No sorting, no
    deduplication, no invented merged order. No mutation.
    """

    rows: list[Constraint] = []

    def _append(
        relation: TrajectoryRelation,
        counterpart: TrajectoryEntity,
    ) -> None:
        rows.append(
            PortfolioProjectFocusNextReadyTaskExecutionApplicabilityConstraint(
                relation_id=relation.id,
                relation_type=relation.relation_type,
                counterpart_entity_id=counterpart.id,
                counterpart_entity_type=counterpart.entity_type,
                counterpart_status=counterpart.status,
                satisfied=counterpart.status is EntityStatus.COMPLETED,
            )
        )

    for relation in portfolio.relations:
        if relation.target_id != authorized_task_id:
            continue
        if relation.relation_type not in _INCOMING_CONSTRAINING:
            continue
        _append(
            relation,
            _resolve_counterpart(lookup, relation.source_id, relation),
        )

    for relation in portfolio.relations:
        if relation.source_id != authorized_task_id:
            continue
        if relation.relation_type not in _OUTGOING_CONSTRAINING:
            continue
        _append(
            relation,
            _resolve_counterpart(lookup, relation.target_id, relation),
        )

    return tuple(rows)



def evaluate_current_next_ready_task_execution_applicability(
    intent: PortfolioProjectFocusNextReadyTaskExecutionIntent,
    portfolio: Portfolio,
) -> PortfolioProjectFocusNextReadyTaskExecutionApplicability:
    """Evaluate whether the EXACT task authorized by ONE genuine V1.44
    intent is still applicable for execution against the CURRENT
    canonical Portfolio.

    Pure and deterministic (same inputs -> identical value results);
    repeated identical calls yield identical values. No repository, no
    persistence, no execution / dispatch / queue / scheduler / agent /
    provider / runtime surface, no shell, no status transition of any
    kind, no portfolio / entity / relation mutation, no fallback task
    selection (no alternative task is ever inspected, ranked, or
    substituted), no recompute of any earlier milestone state, no
    temporal / expiration / TTL rule (authorized_at is provenance
    only), no wall-clock read, and no generated identity.

    Boundary inputs (non-genuine intent, hostile V1.44 payload,
    non-genuine Portfolio, exact portfolio.id mismatch, incoherent
    WBS, unresolvable counterpart) raise the V1.45 boundary error per
    the module docstring; every other CURRENT-state drift is a result
    state, never an exception.

    The APPLICABLE result is an immutable evidence value only: it
    executes nothing.
    """

    genuine: (
        PortfolioProjectFocusNextReadyTaskExecutionIntent
    ) = _require_genuine_intent(intent)

    validated: (
        PortfolioProjectFocusNextReadyTaskExecutionIntent
    ) = _revalidate_v144_intent(genuine)

    current: Portfolio = _require_genuine_portfolio(portfolio)

    if current.id != validated.portfolio_id:
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionApplicabilityError(
                "the supplied portfolio does not exactly match the "
                "V1.44 intent portfolio_id"
            )
        )

    project_id: UUID = validated.authorized_project_id
    task_id: UUID = validated.authorized_task_id

    lookup: dict[UUID, TrajectoryEntity] = {
        entity.id: entity for entity in current.entities
    }

    def _result(
        state: State,
        task_status: EntityStatus | None,
        constraint_rows: ConstraintRows | None,
        unsatisfied: int | None,
    ) -> PortfolioProjectFocusNextReadyTaskExecutionApplicability:
        return (
            PortfolioProjectFocusNextReadyTaskExecutionApplicability(
                intent_id=validated.intent_id,
                authorized_at=validated.authorized_at,
                decision_id=validated.decision_id,
                portfolio_id=validated.portfolio_id,
                authorized_project_id=project_id,
                authorized_task_id=task_id,
                task_status=task_status,
                applicability_state=state,
                constraints=constraint_rows,
                unsatisfied_constraint_count=unsatisfied,
            )
        )

    # -- authorized project in the CURRENT Portfolio ---------------------
    project_entity: TrajectoryEntity | None = lookup.get(project_id)
    if project_entity is None:
        return _result(State.PROJECT_MISSING, None, None, None)
    if project_entity.entity_type is not EntityType.PROJECT:
        return _result(State.PROJECT_TYPE_MISMATCH, None, None, None)

    # -- authorized task in the CURRENT Portfolio ------------------------
    task_entity: TrajectoryEntity | None = lookup.get(task_id)
    if task_entity is None:
        return _result(State.TASK_MISSING, None, None, None)
    if task_entity.entity_type is not EntityType.TASK:
        return _result(State.TASK_TYPE_MISMATCH, None, None, None)

    # -- current canonical WBS membership (build_work_breakdown) ---------
    try:
        wbs: WorkBreakdownStructure = build_work_breakdown(
            current, project_id
        )
    except WorkBreakdownError as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionApplicabilityError(
                "the canonical WBS for the authorized project is "
                "structurally invalid; refusing to reinterpret the "
                "failure as TASK_NOT_IN_AUTHORIZED_PROJECT"
            )
        ) from exc

    if task_id not in _wbs_included_ids(wbs.root):
        return _result(
            State.TASK_NOT_IN_AUTHORIZED_PROJECT,
            task_entity.status,
            None,
            None,
        )

    # -- exact CURRENT task status ---------------------------------------
    if task_entity.status is not EntityStatus.ACTIVE:
        return _result(State.INELIGIBLE_STATUS, task_entity.status, (), 0)

    # -- fixed-policy constraints (ACTIVE task only) ---------------------
    constraint_rows = _collect_constraints(current, lookup, task_id)
    unsatisfied = sum(1 for row in constraint_rows if not row.satisfied)
    if unsatisfied >= 1:
        return _result(
            State.CONSTRAINED, task_entity.status, constraint_rows, unsatisfied
        )
    return _result(
        State.APPLICABLE, task_entity.status, constraint_rows, 0
    )

