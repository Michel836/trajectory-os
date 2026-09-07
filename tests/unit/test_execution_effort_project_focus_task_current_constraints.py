"""V1.39 — deterministic CURRENT constraint evaluation for focused TASK work units.

Covers:

* the ``FocusedTaskConstraintReadinessState``,
  ``FocusedTaskCurrentConstraint``,
  ``FocusedTaskCurrentConstraintEvaluation``,
  ``FocusedProjectTaskCurrentConstraintEvaluations``, and
  ``PortfolioProjectFocusTaskCurrentConstraintEvaluation`` models (strict /
  frozen / extra-forbid, exact field set, policy invariants:
  satisfaction exactly ``COMPLETED``, exact unsatisfied count, unique
  relation IDs, readiness mapping, count/tuple alignment, unique
  project/task IDs);
* the ``evaluate_current_constraints_for_focused_task_work_units``
  boundary:

  - genuine V1.38 projection and genuine Portfolio required;
  - hostile ``model_construct`` V1.38 payloads (bad scalars, malformed
    nested payloads) rejected by fresh strict re-validation;
  - freshly validated copy authoritative; caller-owned V1.38 not
    semantically trusted after re-validation;
  - exact portfolio identity;
  - first-occurrence semantics of the local entity index under a hostile
    duplicate-ID Portfolio;
  - all 8 focused TASK ``EntityStatus`` values ->
    ``INELIGIBLE_STATUS``;
  - all 8 counterpart ``EntityStatus`` values proving ONLY
    ``COMPLETED`` satisfies;
  - each constraining relation type independently (``DEPENDS_ON``,
    ``REQUIRES``, ``WAITING_FOR`` outgoing; ``BLOCKS``, ``PRECEDES``
    incoming);
  - every non-constraining relation type independently ignored;
  - counterpart outside the focused set, counterpart non-TASK entity
    type;
  - unresolved counterpart -> boundary error (never reinterpreted as
    unsatisfied);
  - mixed satisfied/unsatisfied constraints;
  - ``ACTIVE`` + zero constraints -> ``READY``;
  - non-``ACTIVE`` + zero constraints -> ``INELIGIBLE_STATUS``;
  - exact project order, exact task order, exact constraint ordering
    rule (incoming ``BLOCKS``/``PRECEDES`` first, then outgoing
    ``DEPENDS_ON``/``REQUIRES``/``WAITING_FOR``, no sorting);
  - zero selected projects, selected project with zero tasks;
  - discipline: deterministic repeated calls, no input mutation,
    no repository / persistence / provider surface, no clock / UUID
    generation, public exports.
"""

from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import trajectory_os.application as app
import trajectory_os.application.execution_effort_project_focus_task_current_constraints as v139_mod  # noqa: E501
from trajectory_os.application import (
    FocusedProjectTaskCurrentConstraintEvaluations,
    FocusedProjectTaskCurrentRelations,
    FocusedTaskConstraintReadinessState,
    FocusedTaskCurrentConstraint,
    FocusedTaskCurrentConstraintEvaluation,
    FocusedTaskCurrentRelation,
    FocusedTaskCurrentRelations,
    PortfolioProjectFocusTaskCurrentConstraintEvaluation,
    PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
    PortfolioProjectFocusTaskCurrentRelationProjection,
    evaluate_current_constraints_for_focused_task_work_units,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType

PORTFOLIO_ID = uuid.UUID("71616161-6161-4161-8161-616161616161")
OTHER_PORTFOLIO_ID = uuid.UUID("71616161-6161-4161-8161-616161616162")
DECISION_ID = uuid.UUID("72626262-6262-4262-8262-726262626262")
DECIDED_AT = datetime(2025, 7, 2, 9, 15, tzinfo=UTC)

P_ALPHA = uuid.UUID("a0000000-0000-4000-8000-00000000000a")
P_BETA = uuid.UUID("b0000000-0000-4000-8000-00000000000b")

# Task identities deliberately INVERTED against V1.38 projection order so
# any task-UUID-sorted output differs.
T1 = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000011")
T2 = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000022")
T3 = uuid.UUID("c3c3c3c3-0000-4000-8000-000000000033")

# Counterpart entities (may be non-focused, non-TASK, other project).
C1 = uuid.UUID("c1c1c1c1-0000-4000-8000-000000000001")
C2 = uuid.UUID("c2c2c2c2-0000-4000-8000-000000000002")
C3 = uuid.UUID("c3c3c3c3-0000-4000-8000-00000000000a")
C4 = uuid.UUID("c4c4c4c4-0000-4000-8000-00000000000c")
X_DELIVERABLE = uuid.UUID("d4d4d4d4-0000-4000-8000-000000000044")

# Relation identities deliberately INVERTED against V1.38 tuple order so
# any relation-id-sorted constraint output differs.
R_IN_RELATED = uuid.UUID("f0000000-0000-4000-8000-000000000099")
R_IN_BLOCKS = uuid.UUID("f0000000-0000-4000-8000-000000000097")
R_IN_PRECEDES = uuid.UUID("f0000000-0000-4000-8000-000000000098")
R_IN_USES = uuid.UUID("f0000000-0000-4000-8000-000000000096")
R_OUT_DEPENDS = uuid.UUID("f0000000-0000-4000-8000-000000000095")
R_OUT_RELATED = uuid.UUID("f0000000-0000-4000-8000-000000000094")
R_OUT_WAITING = uuid.UUID("f0000000-0000-4000-8000-000000000093")
R_OUT_REQUIRES = uuid.UUID("f0000000-0000-4000-8000-000000000092")
R_SINGLE = uuid.UUID("f0000000-0000-4000-8000-000000000080")


# ---------------------------------------------------------------------------
# Fixtures — deterministic identities and model construction helpers.
# ---------------------------------------------------------------------------


def _entity(
    entity_uuid: uuid.UUID,
    entity_type: EntityType,
    title: str,
    status: EntityStatus = EntityStatus.ACTIVE,
) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_uuid,
        entity_type=entity_type,
        title=title,
        status=status,
    )


def _rel(
    relation_id: uuid.UUID,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    relation_type: RelationType,
    *,
    source: SourceKind = SourceKind.USER_CONFIRMED,
    confidence: float = 1.0,
) -> FocusedTaskCurrentRelation:
    return FocusedTaskCurrentRelation(
        relation_id=relation_id,
        relation_type=relation_type,
        source_id=source_id,
        target_id=target_id,
        source=source,
        confidence=confidence,
    )


def _task_row(
    task_id: uuid.UUID,
    *,
    status: EntityStatus = EntityStatus.ACTIVE,
    incoming: tuple[FocusedTaskCurrentRelation, ...] = (),
    outgoing: tuple[FocusedTaskCurrentRelation, ...] = (),
) -> FocusedTaskCurrentRelations:
    return FocusedTaskCurrentRelations(
        task_id=task_id,
        status=status,
        incoming_relations=incoming,
        outgoing_relations=outgoing,
    )


def _project_row(
    project_id: uuid.UUID,
    tasks: tuple[FocusedTaskCurrentRelations, ...] = (),
) -> FocusedProjectTaskCurrentRelations:
    return FocusedProjectTaskCurrentRelations(
        project_id=project_id, tasks=tasks
    )


def _projection(
    projects: tuple[FocusedProjectTaskCurrentRelations, ...],
    *,
    count: int | None = None,
    portfolio_id: uuid.UUID = PORTFOLIO_ID,
    decision_id: uuid.UUID = DECISION_ID,
    decided_at: datetime = DECIDED_AT,
) -> PortfolioProjectFocusTaskCurrentRelationProjection:
    if count is None:
        count = len(projects)
    return PortfolioProjectFocusTaskCurrentRelationProjection(
        decision_id=decision_id,
        decided_at=decided_at,
        portfolio_id=portfolio_id,
        selected_project_count=count,
        projects=projects,
    )


def _portfolio(
    entities: list[TrajectoryEntity],
    portfolio_id: uuid.UUID = PORTFOLIO_ID,
) -> Portfolio:
    return Portfolio(
        id=portfolio_id,
        name="V1.39 test",
        entities=tuple(entities),
        relations=(),
    )


def _order_portfolio() -> Portfolio:
    """Portfolio for the ordering / mixed-satisfaction scenarios.

    Counterpart statuses:
      C1 COMPLETED (satisfies), C2 ACTIVE (unsatisfied),
      C3 COMPLETED (satisfies), C4 WAITING (unsatisfied),
      X delivery entity DELIVERABLE COMPLETED (non-TASK satisfied).
    """
    return _portfolio(
        entities=[
            _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
            _entity(T1, EntityType.TASK, "Focus task"),
            _entity(C1, EntityType.TASK, "C1", EntityStatus.COMPLETED),
            _entity(C2, EntityType.TASK, "C2", EntityStatus.ACTIVE),
            _entity(C3, EntityType.TASK, "C3", EntityStatus.COMPLETED),
            _entity(C4, EntityType.TASK, "C4", EntityStatus.WAITING),
            _entity(
                X_DELIVERABLE,
                EntityType.DELIVERABLE,
                "Gate deliverable",
                EntityStatus.COMPLETED,
            ),
        ]
    )


def _order_v138() -> PortfolioProjectFocusTaskCurrentRelationProjection:
    """V1.38 projection with interleaved constraining / non-constraining
    relations in both directions (deliberately NOT type or id sorted).

    V1.38 incoming tuple order (authoritative):
      1. R_IN_RELATED  (X -> T1)          non-constraining
      2. R_IN_BLOCKS   (C1 -> T1)         constraining
      3. R_IN_PRECEDES (C3 -> T1)         constraining
      4. R_IN_USES     (X -> T1)          non-constraining

    V1.38 outgoing tuple order (authoritative):
      5. R_OUT_DEPENDS  (T1 -> C2)        constraining
      6. R_OUT_RELATED  (T1 -> X)         non-constraining
      7. R_OUT_WAITING  (T1 -> C3)        constraining
      8. R_OUT_REQUIRES (T1 -> C4)        constraining

    Expected V1.39 constraint order:
      R_IN_BLOCKS, R_IN_PRECEDES, R_OUT_DEPENDS, R_OUT_WAITING,
      R_OUT_REQUIRES
    """
    return _projection(
        (
            _project_row(
                P_ALPHA,
                (
                    _task_row(
                        T1,
                        incoming=(
                            _rel(
                                R_IN_RELATED, X_DELIVERABLE, T1,
                                RelationType.RELATED_TO,
                            ),
                            _rel(
                                R_IN_BLOCKS, C1, T1, RelationType.BLOCKS
                            ),
                            _rel(
                                R_IN_PRECEDES, C3, T1,
                                RelationType.PRECEDES,
                            ),
                            _rel(
                                R_IN_USES, X_DELIVERABLE, T1,
                                RelationType.USES,
                            ),
                        ),
                        outgoing=(
                            _rel(
                                R_OUT_DEPENDS, T1, C2,
                                RelationType.DEPENDS_ON,
                            ),
                            _rel(
                                R_OUT_RELATED, T1, X_DELIVERABLE,
                                RelationType.RELATED_TO,
                            ),
                            _rel(
                                R_OUT_WAITING, T1, C3,
                                RelationType.WAITING_FOR,
                            ),
                            _rel(
                                R_OUT_REQUIRES, T1, C4,
                                RelationType.REQUIRES,
                            ),
                        ),
                    ),
                ),
            ),
        )
    )


def _task_result(
    result: PortfolioProjectFocusTaskCurrentConstraintEvaluation,
    task_id: uuid.UUID,
) -> FocusedTaskCurrentConstraintEvaluation:
    for row in result.projects:
        for task in row.tasks:
            if task.task_id == task_id:
                return task
    raise AssertionError(f"task {task_id} not found in evaluation result")


class _ForeignModel(BaseModel):
    """A different model; must never be accepted."""

    model_config = {"frozen": True}

    field: str = "foreign"


# ---------------------------------------------------------------------------
# Constraint model strictness and invariants.
# ---------------------------------------------------------------------------


def _constraint(
    relation_id: uuid.UUID,
    relation_type: RelationType,
    counterpart: uuid.UUID,
    counterpart_type: EntityType,
    counterpart_status: EntityStatus,
    *,
    satisfied: bool | None = None,
) -> FocusedTaskCurrentConstraint:
    if satisfied is None:
        satisfied = counterpart_status is EntityStatus.COMPLETED
    return FocusedTaskCurrentConstraint(
        relation_id=relation_id,
        relation_type=relation_type,
        counterpart_entity_id=counterpart,
        counterpart_entity_type=counterpart_type,
        counterpart_status=counterpart_status,
        satisfied=satisfied,
    )


class TestFocusedTaskCurrentConstraintModel:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        constraint = _constraint(
            R_IN_BLOCKS, RelationType.BLOCKS, C1, EntityType.TASK,
            EntityStatus.COMPLETED,
        )
        assert constraint.model_config["strict"] is True
        assert constraint.model_config["frozen"] is True
        assert constraint.model_config["extra"] == "forbid"

        with pytest.raises(ValidationError):
            FocusedTaskCurrentConstraint(
                relation_id=R_IN_BLOCKS,
                relation_type=RelationType.BLOCKS,
                counterpart_entity_id=C1,
                counterpart_entity_type=EntityType.TASK,
                counterpart_status=EntityStatus.COMPLETED,
                satisfied=True,
                unexpected_extra="not-allowed",
            )

        with pytest.raises(ValidationError):
            constraint.relation_id = R_SINGLE  # type: ignore[misc]

        with pytest.raises(ValidationError):
            FocusedTaskCurrentConstraint(
                relation_id="not-a-uuid",  # type: ignore[arg-type]
                relation_type=RelationType.BLOCKS,
                counterpart_entity_id=C1,
                counterpart_entity_type=EntityType.TASK,
                counterpart_status=EntityStatus.COMPLETED,
                satisfied=True,
            )

        with pytest.raises(ValidationError):
            FocusedTaskCurrentConstraint(
                relation_id=R_IN_BLOCKS,
                relation_type=RelationType.BLOCKS,
                counterpart_entity_id="not-a-uuid",  # type: ignore[arg-type]
                counterpart_entity_type=EntityType.TASK,
                counterpart_status=EntityStatus.COMPLETED,
                satisfied=True,
            )

        # strict enum: plain strings rejected.
        with pytest.raises(ValidationError):
            FocusedTaskCurrentConstraint(
                relation_id=R_IN_BLOCKS,
                relation_type="blocks",  # type: ignore[arg-type]
                counterpart_entity_id=C1,
                counterpart_entity_type=EntityType.TASK,
                counterpart_status=EntityStatus.COMPLETED,
                satisfied=True,
            )

        # strict bool: integer rejected.
        with pytest.raises(ValidationError):
            FocusedTaskCurrentConstraint(
                relation_id=R_IN_BLOCKS,
                relation_type=RelationType.BLOCKS,
                counterpart_entity_id=C1,
                counterpart_entity_type=EntityType.TASK,
                counterpart_status=EntityStatus.COMPLETED,
                satisfied=1,  # type: ignore[arg-type]
            )

    def test_satisfaction_consistent_with_counterpart_status(self) -> None:
        with pytest.raises(ValidationError):
            _constraint(
                R_IN_BLOCKS, RelationType.BLOCKS, C1, EntityType.TASK,
                EntityStatus.ACTIVE, satisfied=True,
            )
        with pytest.raises(ValidationError):
            _constraint(
                R_SINGLE, RelationType.DEPENDS_ON, C1, EntityType.TASK,
                EntityStatus.COMPLETED, satisfied=False,
            )


# ---------------------------------------------------------------------------
# Evaluation row invariants.
# ---------------------------------------------------------------------------


def _evaluation(
    task_id: uuid.UUID,
    task_status: EntityStatus,
    *,
    readiness: FocusedTaskConstraintReadinessState,
    constraints: tuple[FocusedTaskCurrentConstraint, ...] = (),
    unsatisfied: int = 0,
) -> FocusedTaskCurrentConstraintEvaluation:
    return FocusedTaskCurrentConstraintEvaluation(
        task_id=task_id,
        task_status=task_status,
        readiness_state=readiness,
        constraints=constraints,
        unsatisfied_constraint_count=unsatisfied,
    )


class TestFocusedTaskCurrentConstraintEvaluationModel:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        evaluation = _evaluation(
            T1, EntityStatus.ACTIVE,
            readiness=FocusedTaskConstraintReadinessState.READY,
        )
        assert evaluation.model_config["strict"] is True
        assert evaluation.model_config["frozen"] is True
        assert evaluation.model_config["extra"] == "forbid"

        with pytest.raises(ValidationError):
            FocusedTaskCurrentConstraintEvaluation(
                task_id=T1,
                task_status=EntityStatus.ACTIVE,
                readiness_state=FocusedTaskConstraintReadinessState.READY,
                constraints=(),
                unsatisfied_constraint_count=0,
                unexpected_extra="not-allowed",
            )

        with pytest.raises(ValidationError):
            evaluation.task_id = T2  # type: ignore[misc]

        with pytest.raises(ValidationError):
            FocusedTaskCurrentConstraintEvaluation(
                task_id="not-a-uuid",  # type: ignore[arg-type]
                task_status=EntityStatus.ACTIVE,
                readiness_state=FocusedTaskConstraintReadinessState.READY,
                constraints=(),
                unsatisfied_constraint_count=0,
            )

        # StrictInt: bool and negative rejected.
        for bad_count in (True, -1):  # type: ignore[list-item]
            with pytest.raises(ValidationError):
                FocusedTaskCurrentConstraintEvaluation(
                    task_id=T1,
                    task_status=EntityStatus.ACTIVE,
                    readiness_state=FocusedTaskConstraintReadinessState.READY,
                    constraints=(),
                    unsatisfied_constraint_count=bad_count,
                )

    def test_unsatisfied_count_mismatch_rejected(self) -> None:
        with pytest.raises(
            ValidationError,
            match="unsatisfied_constraint_count",
        ):
            _evaluation(
                T1,
                EntityStatus.ACTIVE,
                readiness=FocusedTaskConstraintReadinessState.CONSTRAINED,
                constraints=(
                    _constraint(
                        R_IN_BLOCKS, RelationType.BLOCKS, C1,
                        EntityType.TASK, EntityStatus.ACTIVE,
                    ),
                ),
                unsatisfied=2,
            )

    def test_duplicate_relation_ids_rejected(self) -> None:
        same_relation = _constraint(
            R_IN_BLOCKS, RelationType.BLOCKS, C1, EntityType.TASK,
            EntityStatus.ACTIVE,
        )
        with pytest.raises(
            ValidationError,
            match="relation IDs must be unique",
        ):
            _evaluation(
                T1,
                EntityStatus.ACTIVE,
                readiness=FocusedTaskConstraintReadinessState.CONSTRAINED,
                constraints=(same_relation, same_relation),
                unsatisfied=2,
            )

    def test_non_active_must_be_ineligible_status(self) -> None:
        satisfied = _constraint(
            R_IN_BLOCKS, RelationType.BLOCKS, C1, EntityType.TASK,
            EntityStatus.COMPLETED,
        )
        with pytest.raises(
            ValidationError,
            match="INELIGIBLE_STATUS",
        ):
            _evaluation(
                T1,
                EntityStatus.WAITING,
                readiness=FocusedTaskConstraintReadinessState.READY,
                constraints=(satisfied,),
                unsatisfied=0,
            )

        # non-ACTIVE rows carry no collected constraints at all.
        with pytest.raises(
            ValidationError,
            match="no collected constraints",
        ):
            _evaluation(
                T1,
                EntityStatus.CANCELLED,
                readiness=FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                constraints=(satisfied,),
                unsatisfied=0,
            )

    def test_readiness_mapping_enforced(self) -> None:
        unsatisfied = _constraint(
            R_IN_BLOCKS, RelationType.BLOCKS, C1, EntityType.TASK,
            EntityStatus.ACTIVE,
        )
        satisfied = _constraint(
            R_IN_PRECEDES, RelationType.PRECEDES, C1, EntityType.TASK,
            EntityStatus.COMPLETED,
        )
        mixed = (unsatisfied, satisfied)

        # ACTIVE + unsatisfied must be CONSTRAINED.
        with pytest.raises(
            ValidationError,
            match="must be CONSTRAINED",
        ):
            _evaluation(
                T1,
                EntityStatus.ACTIVE,
                readiness=FocusedTaskConstraintReadinessState.READY,
                constraints=mixed,
                unsatisfied=1,
            )

        # ACTIVE + zero unsatisfied must be READY.
        with pytest.raises(
            ValidationError,
            match="must be READY",
        ):
            _evaluation(
                T1,
                EntityStatus.ACTIVE,
                readiness=FocusedTaskConstraintReadinessState.CONSTRAINED,
                constraints=(satisfied,),
                unsatisfied=0,
            )


class TestProjectAndFinalEvaluationModels:
    def test_project_row_strict_frozen_and_unique_tasks(self) -> None:
        ready = _evaluation(
            T1, EntityStatus.ACTIVE,
            readiness=FocusedTaskConstraintReadinessState.READY,
        )
        row = FocusedProjectTaskCurrentConstraintEvaluations(
            project_id=P_ALPHA, tasks=(ready,)
        )
        assert row.model_config["strict"] is True
        assert row.model_config["frozen"] is True
        assert row.model_config["extra"] == "forbid"

        with pytest.raises(ValidationError):
            FocusedProjectTaskCurrentConstraintEvaluations(
                project_id=P_ALPHA, tasks=(ready, ready)
            )

        with pytest.raises(ValidationError):
            FocusedProjectTaskCurrentConstraintEvaluations(
                project_id="not-a-uuid",  # type: ignore[arg-type]
                tasks=(),
            )

    def test_final_projection_strict_frozen_and_invariants(self) -> None:
        ready = _evaluation(
            T1, EntityStatus.ACTIVE,
            readiness=FocusedTaskConstraintReadinessState.READY,
        )
        final = PortfolioProjectFocusTaskCurrentConstraintEvaluation(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            portfolio_id=PORTFOLIO_ID,
            selected_project_count=1,
            projects=(
                FocusedProjectTaskCurrentConstraintEvaluations(
                    project_id=P_ALPHA, tasks=(ready,)
                ),
            ),
        )
        assert final.model_config["strict"] is True
        assert final.model_config["frozen"] is True
        assert final.model_config["extra"] == "forbid"

        with pytest.raises(ValidationError):
            final.decision_id = DECISION_ID  # type: ignore[misc]

        with pytest.raises(ValidationError):
            PortfolioProjectFocusTaskCurrentConstraintEvaluation(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=1,
                projects=(),
            )

        with pytest.raises(ValidationError):
            PortfolioProjectFocusTaskCurrentConstraintEvaluation(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count="one",  # type: ignore[arg-type]
                projects=(),
            )

        row_alpha = FocusedProjectTaskCurrentConstraintEvaluations(
            project_id=P_ALPHA, tasks=()
        )
        # duplicate project IDs.
        with pytest.raises(ValidationError, match="unique across project"):
            PortfolioProjectFocusTaskCurrentConstraintEvaluation(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=2,
                projects=(row_alpha, row_alpha),
            )

        # global task-ID uniqueness across project rows.
        duplicate_task = _evaluation(
            T1, EntityStatus.ACTIVE,
            readiness=FocusedTaskConstraintReadinessState.READY,
        )
        with pytest.raises(
            ValidationError,
            match="globally unique",
        ):
            PortfolioProjectFocusTaskCurrentConstraintEvaluation(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=2,
                projects=(
                    FocusedProjectTaskCurrentConstraintEvaluations(
                        project_id=P_ALPHA, tasks=(duplicate_task,)
                    ),
                    FocusedProjectTaskCurrentConstraintEvaluations(
                        project_id=P_BETA, tasks=(duplicate_task,)
                    ),
                ),
            )

    def test_readiness_enum_has_exactly_three_values(self) -> None:
        assert {
            state.value
            for state in FocusedTaskConstraintReadinessState
        } == {"ready", "constrained", "ineligible_status"}


# ---------------------------------------------------------------------------
# Boundary input discipline.
# ---------------------------------------------------------------------------


class TestBoundaryInputs:
    def test_genuine_v138_projection_required(self) -> None:
        wrong = [
            None,
            "a projection",
            {"decision_id": str(DECISION_ID)},
            _ForeignModel(),
            types.SimpleNamespace(
                decision_id=DECISION_ID, projects=()
            ),
            _portfolio([_entity(T1, EntityType.TASK, "t")]),
        ]
        for item in wrong:
            with pytest.raises(
                PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
                match="genuine V1.38",
            ):
                evaluate_current_constraints_for_focused_task_work_units(
                    item,  # type: ignore[arg-type]
                    _order_portfolio(),
                )

    def test_hostile_model_construct_scalar_rejection(self) -> None:
        hostile = (
            PortfolioProjectFocusTaskCurrentRelationProjection.model_construct(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count="two",  # hostile scalar
                projects=(
                    _project_row(
                        P_ALPHA,
                        (_task_row(T1),),
                    ),
                ),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
            match="strict re-validation",
        ):
            evaluate_current_constraints_for_focused_task_work_units(
                hostile, _order_portfolio()
            )

    def test_hostile_model_construct_nested_payload_rejection(self) -> None:
        # A nested relation row with a plain-string relation type defeats
        # strict re-validation.
        bad_relation = FocusedTaskCurrentRelation.model_construct(
            relation_id=R_SINGLE,
            relation_type="depends_on",  # hostile nested scalar
            source_id=T1,
            target_id=C1,
            source=SourceKind.USER_CONFIRMED,
            confidence=1.0,
        )
        hostile = _projection(
            (
                _project_row(
                    P_ALPHA,
                    (_task_row(T1, outgoing=(bad_relation,)),),
                ),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
            match="strict re-validation",
        ):
            evaluate_current_constraints_for_focused_task_work_units(
                hostile, _order_portfolio()
            )

        # A nested task row with an unknown status defeats re-validation.
        bad_task = FocusedTaskCurrentRelations.model_construct(
            task_id=T1,
            status="brogue",  # hostile nested scalar
            incoming_relations=(),
            outgoing_relations=(),
        )
        hostile_status = _projection((_project_row(P_ALPHA, (bad_task,)),))
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
            match="strict re-validation",
        ):
            evaluate_current_constraints_for_focused_task_work_units(
                hostile_status, _order_portfolio()
            )

    def test_fresh_validated_copy_is_authoritative(self) -> None:
        constructed = (
            PortfolioProjectFocusTaskCurrentRelationProjection.model_construct(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=1,
                projects=(
                    _project_row(P_ALPHA, (_task_row(T1),)),
                ),
            )
        )
        result = evaluate_current_constraints_for_focused_task_work_units(
            constructed, _order_portfolio()
        )
        assert result.decision_id == DECISION_ID
        assert result.decided_at == DECIDED_AT
        assert result.portfolio_id == PORTFOLIO_ID
        assert result.selected_project_count == 1
        assert [row.project_id for row in result.projects] == [P_ALPHA]

    def test_inconsistent_caller_payload_cannot_be_salvaged_by_reading_original(
        self,
    ) -> None:
        # A caller object whose raw fields FAIL the V1.38 invariants
        # (count/tuple mismatch) is rejected: the boundary cannot read
        # the caller-owned original after re-validation to build a "valid
        # enough" evaluation — fresh re-validation is authoritative.
        inconsistent = (
            PortfolioProjectFocusTaskCurrentRelationProjection.model_construct(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=5,  # raw field inconsistent
                projects=(_project_row(P_ALPHA, (_task_row(T1),)),),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
            match="strict re-validation",
        ):
            evaluate_current_constraints_for_focused_task_work_units(
                inconsistent, _order_portfolio()
            )

    def test_genuine_portfolio_required(self) -> None:
        v138 = _projection((_project_row(P_ALPHA, (_task_row(T1),)),))
        wrong = [
            None,
            "a portfolio",
            {"id": str(PORTFOLIO_ID)},
            _ForeignModel(),
            types.SimpleNamespace(id=PORTFOLIO_ID, entities=(), relations=()),
        ]
        for item in wrong:
            with pytest.raises(
                PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
                match="genuine Portfolio",
            ):
                evaluate_current_constraints_for_focused_task_work_units(
                    v138, item  # type: ignore[arg-type]
                )

    def test_portfolio_id_mismatch_rejected(self) -> None:
        v138 = _projection((_project_row(P_ALPHA, (_task_row(T1),)),))
        conflicting = _portfolio(
            [_entity(T1, EntityType.TASK, "t")],
            portfolio_id=OTHER_PORTFOLIO_ID,
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
            match="does not exactly match",
        ):
            evaluate_current_constraints_for_focused_task_work_units(
                v138, conflicting
            )

    def test_v138_of_other_portfolio_rejected(self) -> None:
        v138 = _projection(
            (_project_row(P_ALPHA, (_task_row(T1),)),),
            portfolio_id=OTHER_PORTFOLIO_ID,
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
            match="does not exactly match",
        ):
            evaluate_current_constraints_for_focused_task_work_units(
                v138, _order_portfolio()
            )


# ---------------------------------------------------------------------------
# Counterpart resolution.
# ---------------------------------------------------------------------------


class TestCounterpartResolution:
    def test_first_occurrence_semantics_of_local_entity_index(self) -> None:
        # Hostile Portfolio whose entity tuple carries the SAME counterpart
        # identity TWICE with different statuses. The local index MUST use
        # first-occurrence semantics (mirroring Portfolio.get_entity).
        v138 = _projection(
            (
                _project_row(
                    P_ALPHA,
                    (_task_row(
                        T1,
                        outgoing=(
                            _rel(
                                R_SINGLE, T1, C1,
                                RelationType.DEPENDS_ON,
                            ),
                        ),
                    ),),
                ),
            )
        )
        hostile = Portfolio.model_construct(
            id=PORTFOLIO_ID,
            name="duplicate-identity-hostile",
            entities=(
                _entity(
                    C1, EntityType.TASK, "first is ACTIVE",
                    EntityStatus.ACTIVE,
                ),
                _entity(
                    C1, EntityType.TASK, "later COMPLETED",
                    EntityStatus.COMPLETED,
                ),
            ),
            relations=(),
        )
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, hostile
        )
        task = _task_result(result, T1)
        assert task.constraints[0].counterpart_status is EntityStatus.ACTIVE
        assert task.constraints[0].satisfied is False
        assert (
            task.readiness_state
            is FocusedTaskConstraintReadinessState.CONSTRAINED
        )

    def test_counterpart_outside_focused_set(self) -> None:
        # C1 is NOT part of the focused task set of the V1.38 projection;
        # it must still resolve from the CURRENT Portfolio.
        v138 = _projection(
            (
                _project_row(
                    P_ALPHA,
                    (_task_row(
                        T2,
                        outgoing=(
                            _rel(
                                R_SINGLE, T2, C1,
                                RelationType.DEPENDS_ON,
                            ),
                        ),
                    ),),
                ),
            )
        )
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, _order_portfolio()
        )
        task = _task_result(result, T2)
        assert task.constraints[0].counterpart_entity_id == C1
        assert task.constraints[0].counterpart_status is EntityStatus.COMPLETED
        assert task.constraints[0].satisfied is True
        assert (
            task.readiness_state
            is FocusedTaskConstraintReadinessState.READY
        )

    def test_counterpart_non_task_entity_type(self) -> None:
        # The counterpart may be ANY entity type: here a DELIVERABLE.
        portfolio = _portfolio(
            [
                _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
                _entity(T1, EntityType.TASK, "Focus task"),
                _entity(
                    X_DELIVERABLE, EntityType.DELIVERABLE, "Gate",
                    EntityStatus.COMPLETED,
                ),
            ]
        )
        v138 = _projection(
            (
                _project_row(
                    P_ALPHA,
                    (_task_row(
                        T1,
                        outgoing=(
                            _rel(
                                R_SINGLE, T1, X_DELIVERABLE,
                                RelationType.REQUIRES,
                            ),
                        ),
                    ),),
                ),
            )
        )
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, portfolio
        )
        task = _task_result(result, T1)
        constraint = task.constraints[0]
        assert constraint.counterpart_entity_id == X_DELIVERABLE
        assert (
            constraint.counterpart_entity_type
            is EntityType.DELIVERABLE
        )
        assert constraint.counterpart_status is EntityStatus.COMPLETED
        assert constraint.satisfied is True
        assert (
            task.readiness_state
            is FocusedTaskConstraintReadinessState.READY
        )

    def test_counterpart_in_another_project(self) -> None:
        portfolio = _portfolio(
            [
                _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
                _entity(P_BETA, EntityType.PROJECT, "Beta"),
                _entity(T1, EntityType.TASK, "Focus task"),
                _entity(
                    C1, EntityType.TASK, "Other-project task",
                    EntityStatus.COMPLETED,
                ),
            ]
        )
        v138 = _projection(
            (
                _project_row(
                    P_ALPHA,
                    (_task_row(
                        T1,
                        incoming=(
                            _rel(R_SINGLE, C1, T1, RelationType.BLOCKS),
                        ),
                    ),),
                ),
            )
        )
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, portfolio
        )
        task = _task_result(result, T1)
        assert task.constraints[0].satisfied is True
        assert (
            task.readiness_state
            is FocusedTaskConstraintReadinessState.READY
        )

    def test_unresolved_counterpart_raises_boundary_error(self) -> None:
        portfolio = _portfolio(
            [
                _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
                _entity(T1, EntityType.TASK, "Focus task"),
                # C1 deliberately ABSENT from the CURRENT portfolio.
            ]
        )
        v138 = _projection(
            (
                _project_row(
                    P_ALPHA,
                    (_task_row(
                        T1,
                        outgoing=(
                            _rel(
                                R_SINGLE, T1, C1,
                                RelationType.DEPENDS_ON,
                            ),
                        ),
                    ),),
                ),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
            match="cannot be resolved",
        ):
            evaluate_current_constraints_for_focused_task_work_units(
                v138, portfolio
            )

    def test_unresolved_incoming_counterpart_raises_boundary_error(self) -> None:
        portfolio = _portfolio(
            [
                _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
                _entity(T1, EntityType.TASK, "Focus task"),
            ]
        )
        v138 = _projection(
            (
                _project_row(
                    P_ALPHA,
                    (_task_row(
                        T1,
                        incoming=(
                            _rel(R_SINGLE, C2, T1, RelationType.PRECEDES),
                        ),
                    ),),
                ),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
            match="cannot be resolved",
        ):
            evaluate_current_constraints_for_focused_task_work_units(
                v138, portfolio
            )


# ---------------------------------------------------------------------------
# Lifecycle eligibility and constraint satisfaction policy.
# ---------------------------------------------------------------------------


class TestLifecycleEligibility:
    def test_all_eight_task_statuses_are_ineligible(self) -> None:
        statuses = [
            EntityStatus.WAITING,
            EntityStatus.PAUSED,
            EntityStatus.INCUBATOR,
            EntityStatus.SOMEDAY,
            EntityStatus.COMPLETED,
            EntityStatus.CANCELLED,
            EntityStatus.ARCHIVED,
        ]
        for status in statuses:
            v138 = _projection(
                (
                    _project_row(
                        P_ALPHA,
                        (_task_row(
                            T1,
                            status=status,
                            outgoing=(
                                _rel(
                                    R_SINGLE, T1, C1,
                                    RelationType.DEPENDS_ON,
                                ),
                            ),
                        ),),
                    ),
                )
            )
            result = (
                evaluate_current_constraints_for_focused_task_work_units(
                    v138, _order_portfolio()
                )
            )
            task = _task_result(result, T1)
            assert task.task_status is status
            assert (
                task.readiness_state
                is FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS
            )
            # A lifecycle-ineligible task is NOT graph-blocked; no
            # constraint is collected for it.
            assert task.constraints == ()
            assert task.unsatisfied_constraint_count == 0

    def test_active_is_the_only_lifecycle_eligible_status(self) -> None:
        v138 = _projection(
            (
                _project_row(
                    P_ALPHA,
                    (_task_row(T1, status=EntityStatus.ACTIVE),),
                ),
            )
        )
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, _order_portfolio()
        )
        task = _task_result(result, T1)
        assert task.task_status is EntityStatus.ACTIVE
        assert (
            task.readiness_state is FocusedTaskConstraintReadinessState.READY
        )

    def test_active_zero_constraints_ready(self) -> None:
        v138 = _projection(
            (
                _project_row(P_ALPHA, (_task_row(T1),)),
            )
        )
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, _order_portfolio()
        )
        task = _task_result(result, T1)
        assert (
            task.readiness_state is FocusedTaskConstraintReadinessState.READY
        )
        assert task.constraints == ()
        assert task.unsatisfied_constraint_count == 0

    def test_all_eight_counterpart_statuses_only_completed_satisfies(self) -> None:
        for status in EntityStatus:
            v138 = _projection(
                (
                    _project_row(
                        P_ALPHA,
                        (_task_row(
                            T1,
                            outgoing=(
                                _rel(
                                    R_SINGLE, T1, C1,
                                    RelationType.DEPENDS_ON,
                                ),
                            ),
                        ),),
                    ),
                )
            )
            portfolio = _portfolio(
                [
                    _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
                    _entity(T1, EntityType.TASK, "Focus task"),
                    _entity(C1, EntityType.TASK, "Counterpart", status),
                ]
            )
            result = (
                evaluate_current_constraints_for_focused_task_work_units(
                    v138, portfolio
                )
            )
            task = _task_result(result, T1)
            constraint = task.constraints[0]
            assert constraint.counterpart_status is status
            assert constraint.satisfied is (
                status is EntityStatus.COMPLETED
            )
            expected = (
                FocusedTaskConstraintReadinessState.READY
                if status is EntityStatus.COMPLETED
                else FocusedTaskConstraintReadinessState.CONSTRAINED
            )
            assert task.readiness_state is expected

    def test_terminal_states_are_never_reinterpreted_as_completion(self) -> None:
        for status in (EntityStatus.CANCELLED, EntityStatus.ARCHIVED):
            portfolio = _portfolio(
                [
                    _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
                    _entity(T1, EntityType.TASK, "Focus task"),
                    _entity(C1, EntityType.TASK, "Counterpart", status),
                ]
            )
            v138 = _projection(
                (
                    _project_row(
                        P_ALPHA,
                        (_task_row(
                            T1,
                            incoming=(
                                _rel(R_SINGLE, C1, T1, RelationType.BLOCKS),
                            ),
                        ),),
                    ),
                )
            )
            result = (
                evaluate_current_constraints_for_focused_task_work_units(
                    v138, portfolio
                )
            )
            task = _task_result(result, T1)
            assert task.constraints[0].satisfied is False
            assert (
                task.readiness_state
                is FocusedTaskConstraintReadinessState.CONSTRAINED
            )


# ---------------------------------------------------------------------------
# Constraining vs non-constraining relation types.
# ---------------------------------------------------------------------------


def _single_relation_v138(
    relation: FocusedTaskCurrentRelation,
    outgoing: bool,
) -> PortfolioProjectFocusTaskCurrentRelationProjection:
    task = (
        _task_row(T1, outgoing=(relation,))
        if outgoing
        else _task_row(T1, incoming=(relation,))
    )
    return _projection((_project_row(P_ALPHA, (task,)),))


def _portfolio_with_counterpart(
    counterpart_type: EntityType = EntityType.TASK,
    counterpart_status: EntityStatus = EntityStatus.COMPLETED,
) -> Portfolio:
    return _portfolio(
        [
            _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
            _entity(T1, EntityType.TASK, "Focus task"),
            _entity(
                C1, counterpart_type, "Counterpart", counterpart_status
            ),
        ]
    )


class TestConstrainingRelationTypes:
    def test_each_constraining_type_independently_satisfied(self) -> None:
        cases = [
            (RelationType.DEPENDS_ON, T1, C1, True),
            (RelationType.REQUIRES, T1, C1, True),
            (RelationType.WAITING_FOR, T1, C1, True),
            (RelationType.BLOCKS, C1, T1, False),
            (RelationType.PRECEDES, C1, T1, False),
        ]
        for relation_type, source, target, outgoing in cases:
            v138 = _single_relation_v138(
                _rel(R_SINGLE, source, target, relation_type), outgoing
            )
            result = (
                evaluate_current_constraints_for_focused_task_work_units(
                    v138, _portfolio_with_counterpart()
                )
            )
            task = _task_result(result, T1)
            assert len(task.constraints) == 1
            constraint = task.constraints[0]
            assert constraint.relation_type is relation_type
            assert constraint.satisfied is True
            assert task.unsatisfied_constraint_count == 0
            assert (
                task.readiness_state
                is FocusedTaskConstraintReadinessState.READY
            )

    def test_each_constraining_type_independently_unsatisfied(self) -> None:
        cases = [
            (RelationType.DEPENDS_ON, T1, C1, True),
            (RelationType.REQUIRES, T1, C1, True),
            (RelationType.WAITING_FOR, T1, C1, True),
            (RelationType.BLOCKS, C1, T1, False),
            (RelationType.PRECEDES, C1, T1, False),
        ]
        for relation_type, source, target, outgoing in cases:
            v138 = _single_relation_v138(
                _rel(R_SINGLE, source, target, relation_type), outgoing
            )
            result = (
                evaluate_current_constraints_for_focused_task_work_units(
                    v138,
                    _portfolio_with_counterpart(
                        counterpart_status=EntityStatus.ACTIVE
                    ),
                )
            )
            task = _task_result(result, T1)
            assert len(task.constraints) == 1
            assert task.constraints[0].satisfied is False
            assert task.unsatisfied_constraint_count == 1
            assert (
                task.readiness_state
                is FocusedTaskConstraintReadinessState.CONSTRAINED
            )


class TestNonConstrainingRelationTypes:
    def test_every_non_constraining_type_ignored_outgoing(self) -> None:
        for relation_type in (
            RelationType.BELONGS_TO,
            RelationType.USES,
            RelationType.CONTRIBUTES_TO,
            RelationType.PRODUCES,
            RelationType.GENERATED_FROM,
            RelationType.CAN_BATCH_WITH,
            RelationType.RELATED_TO,
        ):
            v138 = _single_relation_v138(
                _rel(R_SINGLE, T1, C1, relation_type),
                outgoing=True,
            )
            result = (
                evaluate_current_constraints_for_focused_task_work_units(
                    v138,
                    _portfolio_with_counterpart(
                        counterpart_status=EntityStatus.ACTIVE
                    ),
                )
            )
            task = _task_result(result, T1)
            assert len(task.constraints) == 0
            assert task.unsatisfied_constraint_count == 0
            assert (
                task.readiness_state
                is FocusedTaskConstraintReadinessState.READY
            )

    def test_every_non_constraining_type_ignored_incoming(self) -> None:
        for relation_type in (
            RelationType.BELONGS_TO,
            RelationType.USES,
            RelationType.CONTRIBUTES_TO,
            RelationType.PRODUCES,
            RelationType.GENERATED_FROM,
            RelationType.CAN_BATCH_WITH,
            RelationType.RELATED_TO,
        ):
            v138 = _single_relation_v138(
                _rel(R_SINGLE, C1, T1, relation_type),
                outgoing=False,
            )
            result = (
                evaluate_current_constraints_for_focused_task_work_units(
                    v138,
                    _portfolio_with_counterpart(
                        counterpart_status=EntityStatus.ACTIVE
                    ),
                )
            )
            task = _task_result(result, T1)
            assert len(task.constraints) == 0
            assert task.unsatisfied_constraint_count == 0
            assert (
                task.readiness_state
                is FocusedTaskConstraintReadinessState.READY
            )


# ---------------------------------------------------------------------------
# Readiness composition.
# ---------------------------------------------------------------------------


class TestReadinessComposition:
    def test_mixed_satisfied_and_unsatisfied_constraints(self) -> None:
        result = evaluate_current_constraints_for_focused_task_work_units(
            _order_v138(), _order_portfolio()
        )
        task = _task_result(result, T1)
        assert task.unsatisfied_constraint_count == 2
        assert task.readiness_state is (
            FocusedTaskConstraintReadinessState.CONSTRAINED
        )
        satisfied_flags = [c.satisfied for c in task.constraints]
        assert satisfied_flags == [True, True, False, True, False]

    def test_all_satisfied_constraints_ready(self) -> None:
        portfolio = _portfolio(
            [
                _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
                _entity(T1, EntityType.TASK, "Focus task"),
                _entity(C1, EntityType.TASK, "C1", EntityStatus.COMPLETED),
                _entity(C2, EntityType.TASK, "C2", EntityStatus.COMPLETED),
                _entity(C3, EntityType.TASK, "C3", EntityStatus.COMPLETED),
                _entity(C4, EntityType.TASK, "C4", EntityStatus.COMPLETED),
            ]
        )
        result = evaluate_current_constraints_for_focused_task_work_units(
            _order_v138(), portfolio
        )
        task = _task_result(result, T1)
        assert task.unsatisfied_constraint_count == 0
        assert (
            task.readiness_state is FocusedTaskConstraintReadinessState.READY
        )

    def test_provenance_preserved_exactly(self) -> None:
        result = evaluate_current_constraints_for_focused_task_work_units(
            _order_v138(), _order_portfolio()
        )
        assert result.decision_id == DECISION_ID
        assert result.decided_at == DECIDED_AT
        assert result.portfolio_id == PORTFOLIO_ID
        assert result.selected_project_count == 1
        assert len(result.projects) == 1


# ---------------------------------------------------------------------------
# Ordering.
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_exact_project_order_preserved(self) -> None:
        v138 = _projection(
            (
                _project_row(P_BETA, (_task_row(T3),)),
                _project_row(P_ALPHA, (_task_row(T1), _task_row(T2))),
            )
        )
        portfolio = _portfolio(
            [
                _entity(P_BETA, EntityType.PROJECT, "Beta"),
                _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
                _entity(T3, EntityType.TASK, "3"),
                _entity(T1, EntityType.TASK, "1"),
                _entity(T2, EntityType.TASK, "2"),
            ]
        )
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, portfolio
        )
        # Projection lists BETA before ALPHA; any project-id sort would
        # put ALPHA first.
        assert [row.project_id for row in result.projects] == [
            P_BETA,
            P_ALPHA,
        ]
        assert [task.task_id for task in result.projects[1].tasks] == [
            T1,
            T2,
        ]

    def test_exact_task_order_preserved(self) -> None:
        v138 = _projection(
            (
                _project_row(
                    P_ALPHA,
                    (_task_row(T2), _task_row(T1)),  # T2 first, T1 second
                ),
            )
        )
        portfolio = _portfolio(
            [
                _entity(P_ALPHA, EntityType.PROJECT, "Alpha"),
                _entity(T1, EntityType.TASK, "1"),
                _entity(T2, EntityType.TASK, "2"),
            ]
        )
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, portfolio
        )
        # T1 < T2 lexicographically; V1.38 order is (T2, T1).
        assert [task.task_id for task in result.projects[0].tasks] == [
            T2,
            T1,
        ]

    def test_exact_constraint_ordering_rule(self) -> None:
        result = evaluate_current_constraints_for_focused_task_work_units(
            _order_v138(), _order_portfolio()
        )
        task = _task_result(result, T1)
        # The expected order is fixed: incoming BLOCKS/PRECEDES in exact
        # V1.38 tuple order FIRST, then outgoing DEPENDS_ON / REQUIRES /
        # WAITING_FOR in exact V1.38 tuple order. Non-constraining
        # relations never appear. The relation IDs are deliberately
        # INVERTED against this order, so any UUID / type sort would be
        # visibly different.
        assert [c.relation_id for c in task.constraints] == [
            R_IN_BLOCKS,
            R_IN_PRECEDES,
            R_OUT_DEPENDS,
            R_OUT_WAITING,
            R_OUT_REQUIRES,
        ]
        assert [c.relation_type for c in task.constraints] == [
            RelationType.BLOCKS,
            RelationType.PRECEDES,
            RelationType.DEPENDS_ON,
            RelationType.WAITING_FOR,
            RelationType.REQUIRES,
        ]
        assert [c.counterpart_entity_id for c in task.constraints] == [
            C1,
            C3,
            C2,
            C3,
            C4,
        ]

    def test_constraint_order_not_by_satisfaction_or_status(self) -> None:
        # Satisfied / unsatisfied are interleaved in the preserved order;
        # satisfied rows appear both before and after unsatisfied rows.
        result = evaluate_current_constraints_for_focused_task_work_units(
            _order_v138(), _order_portfolio()
        )
        task = _task_result(result, T1)
        flags = [c.satisfied for c in task.constraints]
        assert flags != sorted(flags)


# ---------------------------------------------------------------------------
# Empty states.
# ---------------------------------------------------------------------------


class TestEmptyStates:
    def test_zero_selected_projects(self) -> None:
        v138 = _projection((), count=0)
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, _order_portfolio()
        )
        assert result.selected_project_count == 0
        assert result.projects == ()
        assert result.decision_id == DECISION_ID
        assert result.decided_at == DECIDED_AT
        assert result.portfolio_id == PORTFOLIO_ID

    def test_selected_project_with_zero_tasks(self) -> None:
        v138 = _projection((_project_row(P_ALPHA),))
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, _order_portfolio()
        )
        assert result.selected_project_count == 1
        assert [row.project_id for row in result.projects] == [P_ALPHA]
        assert result.projects[0].tasks == ()

    def test_task_with_zero_relations(self) -> None:
        v138 = _projection((_project_row(P_ALPHA, (_task_row(T1),)),))
        result = evaluate_current_constraints_for_focused_task_work_units(
            v138, _order_portfolio()
        )
        task = _task_result(result, T1)
        assert task.constraints == ()
        assert task.unsatisfied_constraint_count == 0
        assert (
            task.readiness_state is FocusedTaskConstraintReadinessState.READY
        )


# ---------------------------------------------------------------------------
# Discipline.
# ---------------------------------------------------------------------------


class TestDiscipline:
    def test_repeated_identical_calls_value_identical(self) -> None:
        first = evaluate_current_constraints_for_focused_task_work_units(
            _order_v138(), _order_portfolio()
        )
        second = evaluate_current_constraints_for_focused_task_work_units(
            _order_v138(), _order_portfolio()
        )
        assert first == second

    def test_deterministic_under_fresh_reconstruction(self) -> None:
        from_result_a = (
            evaluate_current_constraints_for_focused_task_work_units(
                _order_v138(), _order_portfolio()
            )
        )
        from_result_b = (
            evaluate_current_constraints_for_focused_task_work_units(
                _order_v138(), _order_portfolio()
            )
        )
        assert from_result_a.model_dump(mode="json") == (
            from_result_b.model_dump(mode="json")
        )

    def test_v138_input_not_mutated(self) -> None:
        v138 = _order_v138()
        before = v138.model_dump(mode="json")
        evaluate_current_constraints_for_focused_task_work_units(
            v138, _order_portfolio()
        )
        assert v138.model_dump(mode="json") == before

    def test_portfolio_not_mutated(self) -> None:
        portfolio = _order_portfolio()
        before = portfolio.model_dump(mode="json")
        evaluate_current_constraints_for_focused_task_work_units(
            _order_v138(), portfolio
        )
        assert portfolio.model_dump(mode="json") == before

    def test_no_work_breakdown_construction_for_semantics(self) -> None:
        module_vars = vars(v139_mod)
        assert "build_work_breakdown" not in module_vars
        assert "WorkBreakdownError" not in module_vars

    def test_no_repository_persistence_or_provider_surface(self) -> None:
        public_names = [
            name for name in vars(v139_mod) if not name.startswith("_")
        ]
        assert not any(
            "repository" in name.lower()
            or "persist" in name.lower()
            or "provider" in name.lower()
            or "runtime" in name.lower()
            for name in public_names
        )

        source = Path(v139_mod.__file__).read_text()
        assert "datetime.now" not in source
        assert "uuid4" not in source
        assert "uuid5" not in source
        assert "random()" not in source
        assert "import os" not in source
        assert "requests" not in source

    def test_v139_does_not_rank_or_recommend(self) -> None:
        # The boundary must never implement ranking / recommendation
        # concepts under any public name.
        public_names = [
            name for name in vars(v139_mod) if not name.startswith("_")
        ]
        for name in public_names:
            lowered = name.lower()
            for forbidden in (
                "rank",
                "score",
                "priorit",
                "recommend",
                "urgent",
                "nextaction",
            ):
                assert forbidden not in lowered

    def test_public_exports_correct(self) -> None:
        required = [
            "FocusedTaskConstraintReadinessState",
            "FocusedTaskCurrentConstraint",
            "FocusedTaskCurrentConstraintEvaluation",
            "FocusedProjectTaskCurrentConstraintEvaluations",
            "PortfolioProjectFocusTaskCurrentConstraintEvaluation",
            "PortfolioProjectFocusTaskCurrentConstraintEvaluationError",
            "evaluate_current_constraints_for_focused_task_work_units",
        ]
        for name in required:
            assert name in app.__all__
            assert getattr(app, name) is not None

        module_all = set(v139_mod.__all__)
        for name in required:
            assert name in module_all

    def test_error_is_a_value_error(self) -> None:
        assert issubclass(
            PortfolioProjectFocusTaskCurrentConstraintEvaluationError,
            ValueError,
        )
