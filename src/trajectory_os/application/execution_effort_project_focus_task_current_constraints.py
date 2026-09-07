"""V1.39 — deterministic CURRENT constraint evaluation for focused TASK work units.

Evaluates ONE genuine, freshly strict-revalidated V1.38
``PortfolioProjectFocusTaskCurrentRelationProjection`` plus one matching
CURRENT canonical ``Portfolio`` into a deterministic CURRENT
constraint/readiness evaluation for every focused TASK work unit.

V1.39 is the FIRST milestone that interprets selected relation types as
execution constraints. It is a pure, deterministic, explainable,
policy-bounded, CURRENT-state-only application boundary:

- deterministic and pure: identical inputs yield value-identical
  evaluations; repeated calls are stable;
- explainable: every constraint row is an exact copy of a validated V1.38
  relation row plus the exact CURRENT counterpart entity id, type, and
  status — no inference anywhere;
- immutable: all output models are strict / frozen / extra-forbid;
- policy-bounded: the ONLY interpretation is the fixed policy below —
  nothing else is reinterpreted;
- CURRENT-state only: it never claims the constraint state existed when
  the focus decision was made, nor invents historical state.

V1.39 does NOT:

- rank, score, prioritize, or recommend tasks;
- choose a next task or a next action;
- infer urgency, due-date pressure, or business importance;
- reconstruct historical state;
- mutate the Portfolio or rebuild the work breakdown (``no call to
  build_work_breakdown``);
- persist, call any repository / provider / AI / runtime boundary;
- read the wall clock or generate UUIDs / timestamps.

Authority:

- the V1.38 projection is the SOLE authority for decision provenance,
  focused project identities and order, focused TASK identities and
  order, and the exact CURRENT relation rows attached to each focused
  TASK (including their exact per-direction tuples);
- the CURRENT canonical ``Portfolio`` is authoritative ONLY for
  resolving the CURRENT canonical counterpart entity (id, type, status)
  referenced by constraining relations.

The focused TASK's lifecycle status is the exact status copied from the
validated V1.38 row (it was itself copied from the CURRENT Portfolio by
V1.38).

FIXED POLICY

1. Lifecycle eligibility: a focused TASK is lifecycle-eligible EXACTLY
   when its copied status is ``EntityStatus.ACTIVE``. Every other status
   (``WAITING``, ``PAUSED``, ``INCUBATOR``, ``SOMEDAY``, ``COMPLETED``,
   ``CANCELLED``, ``ARCHIVED``) is lifecycle-ineligible. A
   lifecycle-ineligible task is NOT graph-blocked merely because of its
   own status: it is reported as ``INELIGIBLE_STATUS`` and no constraint
   is collected for it.

2. Constraining relations (the only relation types V1.39 interprets):

   - outgoing from the focused TASK: ``DEPENDS_ON``, ``REQUIRES``,
     ``WAITING_FOR`` — the counterpart is the relation ``target_id``;
   - incoming onto the focused TASK: ``BLOCKS``, ``PRECEDES`` — the
     counterpart is the relation ``source_id``.

   Every other relation type (``BELONGS_TO``, ``USES``,
   ``CONTRIBUTES_TO``, ``PRODUCES``, ``GENERATED_FROM``,
   ``CAN_BATCH_WITH``, ``RELATED_TO``) is NON-CONSTRAINING in V1.39 and
   must not affect readiness.

3. Constraint satisfaction: ``EntityStatus.COMPLETED`` is the ONLY
   counterpart status that satisfies a constraint. Every other status —
   including terminal lifecycle states (``CANCELLED``, ``ARCHIVED``) —
   is unsatisfied. Terminal states are NEVER reinterpreted as successful
   completion.

4. Readiness:

   - task status != ACTIVE                    -> INELIGIBLE_STATUS
   - ACTIVE + any unsatisfied constraint      -> CONSTRAINED
   - ACTIVE + zero unsatisfied (incl. no
     constraints at all)                      -> READY

COUNTERPART SCOPE

A counterpart may be ANY CURRENT Portfolio entity type: it need not be
focused, need not be a TASK, and need not belong to the same project.
It is resolved from the supplied CURRENT Portfolio. A constraining
relation whose counterpart cannot be resolved raises the V1.39 boundary
error — it is NEVER reinterpreted as unsatisfied.

CONSTRAINT ORDERING

For each focused TASK, V1.39:

1. traverses the validated V1.38 ``incoming_relations`` tuple in exact
   order and retains only ``BLOCKS`` and ``PRECEDES``;
2. then traverses the validated V1.38 ``outgoing_relations`` tuple in
   exact order and retains only ``DEPENDS_ON``, ``REQUIRES``, and
   ``WAITING_FOR``.

Constraint rows are NOT sorted by UUID, type, counterpart, status, or
satisfaction.

Repository inspection confirms V1.38 carries separate incoming/outgoing
tuples (each in exact original ``Portfolio.relations`` order) and does
NOT carry a single unified original relation order to preserve, so no
merged order is invented: the ordering above is defined directly on the
validated V1.38 tuples.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from trajectory_os.application.execution_effort_project_focus_task_current_relations import (  # noqa: E501
    PortfolioProjectFocusTaskCurrentRelationProjection,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType

__all__ = [
    "FocusedProjectTaskCurrentConstraintEvaluations",
    "FocusedTaskConstraintReadinessState",
    "FocusedTaskCurrentConstraint",
    "FocusedTaskCurrentConstraintEvaluation",
    "PortfolioProjectFocusTaskCurrentConstraintEvaluation",
    "PortfolioProjectFocusTaskCurrentConstraintEvaluationError",
    "evaluate_current_constraints_for_focused_task_work_units",
]

# Fixed policy: constraining relation types per direction.
_INCOMING_CONSTRAINING: frozenset[RelationType] = frozenset(
    {RelationType.BLOCKS, RelationType.PRECEDES}
)
_OUTGOING_CONSTRAINING: frozenset[RelationType] = frozenset(
    {RelationType.DEPENDS_ON, RelationType.REQUIRES, RelationType.WAITING_FOR}
)


class PortfolioProjectFocusTaskCurrentConstraintEvaluationError(ValueError):
    """Raised when a genuine V1.38 focus task CURRENT projection cannot
    be evaluated against the CURRENT Portfolio.

    Raised for: a V1.38 projection payload that is not a genuine
    ``PortfolioProjectFocusTaskCurrentRelationProjection``, a payload
    that fails fresh strict re-validation (hostile ``model_construct``
    payloads, bad scalars, malformed nested project/task/relation
    payloads), a ``portfolio`` that is not a genuine ``Portfolio``, an
    exact ``portfolio.id`` mismatch, or a constraining relation whose
    counterpart entity cannot be resolved in the CURRENT Portfolio.
    """


# ---------------------------------------------------------------------------
# Readiness state.
# ---------------------------------------------------------------------------


class FocusedTaskConstraintReadinessState(StrEnum):
    """The exact V1.39 CURRENT readiness state of ONE focused TASK work
    unit.

    - ``READY``: lifecycle-eligible (``ACTIVE``) and every collected
      constraint is satisfied (including zero constraints);
    - ``CONSTRAINED``: lifecycle-eligible (``ACTIVE``) with at least one
      unsatisfied constraint;
    - ``INELIGIBLE_STATUS``: the task's exact CURRENT status is not
      ``ACTIVE`` — the task is simply not lifecycle-eligible; this is
      not a statement that the task is graph-blocked.

    No other state exists. There is no priority, urgency, rank, score,
    or recommendation encoded here.
    """

    READY = "ready"
    CONSTRAINED = "constrained"
    INELIGIBLE_STATUS = "ineligible_status"


# ---------------------------------------------------------------------------
# Models (immutable, self-validating).
# ---------------------------------------------------------------------------
# ``FocusedTaskCurrentConstraint`` fields
# ---------------------------------------------------------------------------


class FocusedTaskCurrentConstraint(BaseModel):
    """ONE exact V1.39 constraint row for ONE focused TASK work unit.

    ``relation_id`` and ``relation_type`` are copied exactly from a
    validated V1.38 relation row; ``counterpart_entity_id``,
    ``counterpart_entity_type``, and ``counterpart_status`` are the
    exact CURRENT canonical counterpart entity identity, type, and
    status resolved from the CURRENT Portfolio. ``satisfied`` is
    EXACTLY ``counterpart_status is EntityStatus.COMPLETED`` per the
    fixed V1.39 policy — nothing else satisfies a constraint.

    No generated id, timestamp, score, rank, recommendation, or inferred
    confidence appears here.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    relation_id: UUID
    relation_type: RelationType
    counterpart_entity_id: UUID
    counterpart_entity_type: EntityType
    counterpart_status: EntityStatus
    satisfied: bool

    @model_validator(mode="after")
    def _validate_constraint_invariants(self) -> FocusedTaskCurrentConstraint:
        if self.satisfied is not (
            self.counterpart_status is EntityStatus.COMPLETED
        ):
            raise ValueError(
                "a constraint is satisfied exactly when its counterpart "
                "status is COMPLETED"
            )
        return self


# ---------------------------------------------------------------------------
# Task evaluation row
# ---------------------------------------------------------------------------


class FocusedTaskCurrentConstraintEvaluation(BaseModel):
    """The exact V1.39 constraint/readiness evaluation for ONE focused
    TASK work unit.

    ``task_id`` and ``task_status`` are copied exactly from the
    validated V1.38 row; ``readiness_state`` is the exact readiness
    state derived from the fixed V1.39 policy; ``constraints`` is the
    exact tuple of collected constraint rows (empty for a
    lifecycle-ineligible task); ``unsatisfied_constraint_count``
    exactly equals the number of unsatisfied constraints.

    Invariants (enforced):

    - relation IDs are unique within the task's constraints;
    - ``unsatisfied_constraint_count`` exactly equals
      ``sum(c.satisfied is False for c in constraints)``;
    - non-``ACTIVE`` status  -> ``INELIGIBLE_STATUS`` (and zero
      constraints — the fixed policy collects none);
    - ``ACTIVE`` + any unsatisfied constraint -> ``CONSTRAINED``;
    - ``ACTIVE`` + zero unsatisfied constraints -> ``READY``.

    No generated id, timestamp, score, rank, or recommendation.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    task_id: UUID
    task_status: EntityStatus
    readiness_state: FocusedTaskConstraintReadinessState
    constraints: tuple[FocusedTaskCurrentConstraint, ...]
    unsatisfied_constraint_count: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def _validate_evaluation_invariants(
        self,
    ) -> FocusedTaskCurrentConstraintEvaluation:
        relation_ids = [c.relation_id for c in self.constraints]
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError(
                "relation IDs must be unique within the task constraints"
            )

        unsatisfied = sum(1 for c in self.constraints if not c.satisfied)
        if unsatisfied != self.unsatisfied_constraint_count:
            raise ValueError(
                "unsatisfied_constraint_count must exactly equal the "
                "number of unsatisfied constraints"
            )

        for constraint in self.constraints:
            if constraint.satisfied is not (
                constraint.counterpart_status is EntityStatus.COMPLETED
            ):
                raise ValueError(
                    "a constraint is satisfied exactly when its "
                    "counterpart status is COMPLETED"
                )

        if self.task_status is not EntityStatus.ACTIVE:
            if (
                self.readiness_state
                is not FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS
            ):
                raise ValueError(
                    "a non-ACTIVE focused task must be evaluated as "
                    "INELIGIBLE_STATUS"
                )
            if self.constraints:
                raise ValueError(
                    "a non-ACTIVE focused task carries no collected "
                    "constraints"
                )
        elif self.unsatisfied_constraint_count > 0:
            if (
                self.readiness_state
                is not FocusedTaskConstraintReadinessState.CONSTRAINED
            ):
                raise ValueError(
                    "an ACTIVE focused task with an unsatisfied "
                    "constraint must be CONSTRAINED"
                )
        else:
            if (
                self.readiness_state
                is not FocusedTaskConstraintReadinessState.READY
            ):
                raise ValueError(
                    "an ACTIVE focused task without an unsatisfied "
                    "constraint must be READY"
                )

        return self


# ---------------------------------------------------------------------------
# Project evaluation row
# ---------------------------------------------------------------------------


class FocusedProjectTaskCurrentConstraintEvaluations(BaseModel):
    """One selected project's focused TASK work units, each carrying its
    exact V1.39 constraint/readiness evaluation.

    Task IDs are unique within the row and the ``tasks`` tuple order is
    the exact validated V1.38 task tuple order: no sorting, no
    deduplication, no filtering. An empty ``tasks`` tuple is coherent
    (a selected project with zero focused TASK work units).
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    project_id: UUID
    tasks: tuple[FocusedTaskCurrentConstraintEvaluation, ...]

    @model_validator(mode="after")
    def _validate_project_row_invariants(
        self,
    ) -> FocusedProjectTaskCurrentConstraintEvaluations:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique within a project row")
        return self


# ---------------------------------------------------------------------------
# Final projection
# ---------------------------------------------------------------------------


class PortfolioProjectFocusTaskCurrentConstraintEvaluation(BaseModel):
    """The complete immutable V1.39 CURRENT constraint evaluation.

    ``decision_id`` and ``decided_at`` carry the freshly strict-
    revalidated V1.38 (accepted focus decision) provenance EXACTLY;
    ``portfolio_id``, ``selected_project_count``, and the ``projects``
    tuple align with the revalidated V1.38 projection. The ``projects``
    tuple order is the exact V1.38 project order and is preserved
    exactly, as is every task tuple order and every constraint tuple
    order within it. Task IDs are globally unique. No generated
    identity or timestamp appears here.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_id: UUID
    decided_at: datetime
    portfolio_id: UUID
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    projects: tuple[FocusedProjectTaskCurrentConstraintEvaluations, ...]

    @model_validator(mode="after")
    def _validate_evaluation_invariants(
        self,
    ) -> PortfolioProjectFocusTaskCurrentConstraintEvaluation:
        if len(self.projects) != self.selected_project_count:
            raise ValueError(
                "selected_project_count must equal the length of "
                f"projects (got {len(self.projects)})"
            )

        project_ids = [row.project_id for row in self.projects]
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("project IDs must be unique across project rows")

        task_ids = [
            task.task_id for row in self.projects for task in row.tasks
        ]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError(
                "task IDs must be globally unique across project rows"
            )

        return self


# ---------------------------------------------------------------------------
# Pure constraint-evaluation boundary.
# ---------------------------------------------------------------------------


def evaluate_current_constraints_for_focused_task_work_units(
    focused_task_relations: PortfolioProjectFocusTaskCurrentRelationProjection,
    portfolio: Portfolio,
) -> PortfolioProjectFocusTaskCurrentConstraintEvaluation:
    """Evaluate ONE genuine V1.38 focus task CURRENT status/relation
    projection plus the matching CURRENT canonical Portfolio into a
    deterministic CURRENT constraint/readiness evaluation for every
    focused TASK work unit.

    Pure and deterministic: no repository argument, no persistence, no
    provider / AI / runtime boundary, no work-breakdown construction,
    no portfolio mutation, no generated identity or timestamp, no clock
    read.

    Steps (deliberately ordered; every failure is raised BEFORE the
    output is built):

      1. require a genuine V1.38
         ``PortfolioProjectFocusTaskCurrentRelationProjection``
         instance (``None``, dicts, strings, foreign models, and duck
         types are rejected);
      2. freshly strict-revalidate the COMPLETE V1.38 payload and
         retain ONLY the validated copy — never trust an
         already-created object (hostile ``model_construct`` values or
         attribute tampering are rejected);
      3. from this point on every semantic V1.38 read uses ONLY the
         retained validated projection, never the caller-owned
         original;
      4. require ``portfolio`` to be a genuine ``Portfolio`` instance;
      5. require ``portfolio.id == validated.portfolio_id`` EXACTLY;
      6. if ``selected_project_count == 0`` (which implies
         ``projects == ()`` by V1.38 invariants): return an empty
         evaluation WITHOUT evaluating any task, entity, or relation
         and WITHOUT fabricating anything;
      7. otherwise: build ONE local CURRENT entity lookup index from
         ``portfolio.entities`` with first-occurrence semantics
         (defensive under a hostile constructed Portfolio that may
         carry duplicate IDs);
      8. iterate the validated V1.38 ``projects`` rows in exact tuple
         order;
      9. iterate the validated V1.38 task rows in exact tuple order;
     10. classify lifecycle eligibility from the exact copied V1.38
         task status — ``ACTIVE`` exactly; a lifecycle-ineligible
         task is reported as ``INELIGIBLE_STATUS`` with no collected
         constraints and is NOT interpreted as graph-blocked;
     11. for a lifecycle-eligible task, traverse the validated V1.38
         ``incoming_relations`` in exact tuple order retaining only
         ``BLOCKS`` and ``PRECEDES`` (counterpart = ``source_id``),
         then traverse the validated V1.38 ``outgoing_relations`` in
         exact tuple order retaining only ``DEPENDS_ON``, ``REQUIRES``,
         and ``WAITING_FOR`` (counterpart = ``target_id``); every other
         relation type is ignored and is never reinterpreted;
     12. resolve EVERY counterpart entity in the CURRENT Portfolio via
         the local single index; an unresolvable counterpart raises the
         V1.39 boundary error and is NEVER reinterpreted as
         unsatisfied;
     13. copy the counterpart's exact id, ``entity_type``, and status;
     14. mark the constraint satisfied EXACTLY when the counterpart
         status is ``EntityStatus.COMPLETED`` — terminal states such as
         ``CANCELLED`` or ``ARCHIVED`` are unsatisfied and never
         reinterpreted as successful completion;
     15. derive the exact readiness state from the fixed policy
         (``INELIGIBLE_STATUS`` / ``CONSTRAINED`` / ``READY``);
     16. preserve zero project / task / constraint states exactly;
     17. construct the exact provenance-carrying immutable evaluation
         and return.

    Repeated identical calls are value-identical.
    """

    # -- 1. genuine V1.38 focus task CURRENT projection -------------------
    if not isinstance(
        focused_task_relations,
        PortfolioProjectFocusTaskCurrentRelationProjection,
    ):
        raise (
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError(
                "a genuine V1.38 "
                "PortfolioProjectFocusTaskCurrentRelationProjection is "
                f"required, got {type(focused_task_relations).__name__}"
            )
        )

    # -- 2. freshly strict-revalidate the COMPLETE V1.38 payload -----------
    try:
        focused_payload: object = focused_task_relations.model_dump(
            mode="python"
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise (
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError(
                "the supplied focus task CURRENT projection is not the "
                "V1.38 shape"
            )
        ) from exc

    try:
        validated = (
            PortfolioProjectFocusTaskCurrentRelationProjection.model_validate(
                focused_payload, strict=True
            )
        )
    except ValidationError as exc:
        raise (
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError(
                "the supplied focus task CURRENT projection failed "
                "strict re-validation"
            )
        ) from exc

    # -- 3. from here on: ONLY the retained validated V1.38 projection ----

    # -- 4. genuine Portfolio ---------------------------------------------
    if not isinstance(portfolio, Portfolio):
        raise (
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError(
                "a genuine Portfolio is required, "
                f"got {type(portfolio).__name__}"
            )
        )

    # -- 5. exact portfolio identity ---------------------------------------
    if portfolio.id != validated.portfolio_id:
        raise (
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError(
                "the supplied portfolio does not exactly match the "
                "focus task CURRENT projection portfolio_id"
            )
        )

    # -- 6. empty focus: no evaluation, nothing fabricated ------------------
    if validated.selected_project_count == 0:
        return (
            PortfolioProjectFocusTaskCurrentConstraintEvaluation(
                decision_id=validated.decision_id,
                decided_at=validated.decided_at,
                portfolio_id=validated.portfolio_id,
                selected_project_count=0,
                projects=(),
            )
        )

    # -- 7. ONE local CURRENT entity lookup index ---------------------------
    # First-occurrence semantics preserved defensively under a hostile
    # constructed Portfolio that might carry duplicate entity IDs.
    entity_lookup: dict[UUID, TrajectoryEntity] = {}
    for portfolio_entity in portfolio.entities:
        entity_lookup.setdefault(portfolio_entity.id, portfolio_entity)

    def _resolve_counterpart(
        counterpart_id: UUID,
        constraint_relation_type: RelationType,
    ) -> TrajectoryEntity:
        counterpart: TrajectoryEntity | None = entity_lookup.get(
            counterpart_id
        )
        if counterpart is None:
            raise (
                PortfolioProjectFocusTaskCurrentConstraintEvaluationError(
                    "a constraining relation references a counterpart "
                    "entity that cannot be resolved in the CURRENT "
                    f"portfolio: relation type "
                    f"{constraint_relation_type.value}, counterpart "
                    f"entity {counterpart_id}"
                )
            )
        return counterpart

    # -- 8-15. exact V1.38 project/task order, fixed policy ----------------
    rows: list[FocusedProjectTaskCurrentConstraintEvaluations] = []

    for project_row in validated.projects:
        task_rows: list[FocusedTaskCurrentConstraintEvaluation] = []

        for task_row in project_row.tasks:
            if task_row.status is not EntityStatus.ACTIVE:
                # 10. lifecycle-ineligible: reported as such; NOT
                # graph-blocked by its own status; no constraint is
                # collected for it.
                task_rows.append(
                    FocusedTaskCurrentConstraintEvaluation(
                        task_id=task_row.task_id,
                        task_status=task_row.status,
                        readiness_state=(
                            FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS
                        ),
                        constraints=(),
                        unsatisfied_constraint_count=0,
                    )
                )
                continue

            # 11-14. collect exactly the fixed-policy constraining
            # relations, in the exact validated V1.38 order: incoming
            # BLOCKS / PRECEDES first, then outgoing DEPENDS_ON /
            # REQUIRES / WAITING_FOR. No sorting, no deduplication.
            constraints: list[FocusedTaskCurrentConstraint] = []

            for relation in task_row.incoming_relations:
                if relation.relation_type not in _INCOMING_CONSTRAINING:
                    continue
                counterpart = _resolve_counterpart(
                    relation.source_id, relation.relation_type
                )
                constraints.append(
                    FocusedTaskCurrentConstraint(
                        relation_id=relation.relation_id,
                        relation_type=relation.relation_type,
                        counterpart_entity_id=counterpart.id,
                        counterpart_entity_type=counterpart.entity_type,
                        counterpart_status=counterpart.status,
                        satisfied=counterpart.status
                        is EntityStatus.COMPLETED,
                    )
                )

            for relation in task_row.outgoing_relations:
                if relation.relation_type not in _OUTGOING_CONSTRAINING:
                    continue
                counterpart = _resolve_counterpart(
                    relation.target_id, relation.relation_type
                )
                constraints.append(
                    FocusedTaskCurrentConstraint(
                        relation_id=relation.relation_id,
                        relation_type=relation.relation_type,
                        counterpart_entity_id=counterpart.id,
                        counterpart_entity_type=counterpart.entity_type,
                        counterpart_status=counterpart.status,
                        satisfied=counterpart.status
                        is EntityStatus.COMPLETED,
                    )
                )

            unsatisfied = sum(1 for c in constraints if not c.satisfied)

            # 15. exact readiness state from the fixed policy.
            if unsatisfied > 0:
                readiness = FocusedTaskConstraintReadinessState.CONSTRAINED
            else:
                readiness = FocusedTaskConstraintReadinessState.READY

            task_rows.append(
                FocusedTaskCurrentConstraintEvaluation(
                    task_id=task_row.task_id,
                    task_status=task_row.status,
                    readiness_state=readiness,
                    constraints=tuple(constraints),
                    unsatisfied_constraint_count=unsatisfied,
                )
            )

        rows.append(
            FocusedProjectTaskCurrentConstraintEvaluations(
                project_id=project_row.project_id,
                tasks=tuple(task_rows),
            )
        )

    # -- 17. immutable evaluation from the validated V1.38 projection ------
    return (
        PortfolioProjectFocusTaskCurrentConstraintEvaluation(
            decision_id=validated.decision_id,
            decided_at=validated.decided_at,
            portfolio_id=validated.portfolio_id,
            selected_project_count=validated.selected_project_count,
            projects=tuple(rows),
        )
    )
