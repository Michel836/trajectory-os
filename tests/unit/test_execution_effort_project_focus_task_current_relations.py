"""V1.38 — CURRENT status and exact relation projection for focused TASK work units.

Covers:
* the ``FocusedTaskCurrentRelation``, ``FocusedTaskCurrentRelations``,
  ``FocusedProjectTaskCurrentRelations``, and
  ``PortfolioProjectFocusTaskCurrentRelationProjection`` models (strict /
  frozen / extra-forbid, exact field set, bool rejection for
  ``selected_project_count``, count/tuple mismatch, duplicate project /
  task / relation-ID rejection, incoming/outgoing endpoint invariants,
  empty tuples valid);
* the ``project_current_relations_for_focused_task_work_units`` boundary:
  - genuine V1.37 projection and genuine Portfolio required;
  - hostile ``model_construct`` V1.37 payloads (bad scalars, malformed
    nested project/task payloads) rejected by fresh strict re-validation;
  - freshly validated copy authoritative; caller-owned V1.37 not
    semantically trusted after re-validation;
  - exact portfolio identity, missing / non-PROJECT focused project,
    missing / non-TASK focused task;
  - exact CURRENT status copy, exact relation field copy,
    every relation type preserved;
  - incoming/outgoing order follows ``Portfolio.relations`` (no sorting),
    project / task order follows the V1.37 projection exactly,
    same-type distinct relation rows both preserved;
  - empty states: zero selected projects, project with zero tasks,
    task with zero relations;
  - discipline: determinism, input immutability, no work-breakdown
    construction, no repository / persistence / provider surface,
    no clock / UUID generation, public exports.
"""

from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import trajectory_os.application as app
import trajectory_os.application.execution_effort_project_focus_task_current_relations as v138_mod  # noqa: E501
from trajectory_os.application import (
    FocusedProjectTaskCurrentRelations,
    FocusedProjectTaskWorkUnits,
    FocusedTaskCurrentRelation,
    FocusedTaskCurrentRelations,
    PortfolioProjectFocusTaskCurrentRelationProjection,
    PortfolioProjectFocusTaskCurrentRelationProjectionError,
    PortfolioProjectFocusTaskWorkUnitProjection,
    project_current_relations_for_focused_task_work_units,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = uuid.UUID("71616161-6161-4161-8161-616161616161")
OTHER_PORTFOLIO_ID = uuid.UUID("71616161-6161-4161-8161-616161616162")
DECISION_ID = uuid.UUID("72626262-6262-4262-8262-726262626262")
DECIDED_AT = datetime(2025, 7, 2, 9, 15, tzinfo=UTC)

# Fixed identities: T2 > T1 lexicographically, and the V1.37 projection
# deliberately lists (T2, T1) so any task-UUID-sorted output differs.
P_ALPHA = uuid.UUID("a0000000-0000-4000-8000-00000000000a")
P_BETA = uuid.UUID("b0000000-0000-4000-8000-00000000000b")
T1 = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000011")
T2 = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000022")
T3 = uuid.UUID("c3c3c3c3-0000-4000-8000-000000000033")
X_DELIVERABLE = uuid.UUID("d4d4d4d4-0000-4000-8000-000000000044")
OTHER_TASK = uuid.UUID("e5e5e5e5-0000-4000-8000-000000000055")

# Relation identities deliberately INVERTED against portfolio order: the
# FIRST relation appearing in Portfolio.relations carries the HIGHEST id,
# so any relation-id-sorted output would visibly differ.
R_T1_T1_DEPENDS = uuid.UUID("f0000000-0000-4000-8000-00000000009a")
R_T1_ALPHA_BELONGS = uuid.UUID("f0000000-0000-4000-8000-00000000009b")
R_T1_T3_BLOCKS_A = uuid.UUID("f0000000-0000-4000-8000-00000000009c")
R_T1_T3_WAITING = uuid.UUID("f0000000-0000-4000-8000-00000000009d")
R_T1_T3_BLOCKS_B = uuid.UUID("f0000000-0000-4000-8000-00000000009e")
R_T3_T1_PRECEDES = uuid.UUID("f0000000-0000-4000-8000-00000000009f")


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


def _relation(
    relation_id: uuid.UUID,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    relation_type: RelationType,
    *,
    source: SourceKind = SourceKind.USER_CONFIRMED,
    confidence: float = 1.0,
) -> TrajectoryRelation:
    return TrajectoryRelation(
        id=relation_id,
        source_id=source_id,
        target_id=target_id,
        relation_type=relation_type,
        source=source,
        confidence=confidence,
    )


def _portfolio(
    entities: list[TrajectoryEntity],
    relations: list[TrajectoryRelation],
    portfolio_id: uuid.UUID = PORTFOLIO_ID,
) -> Portfolio:
    return Portfolio(
        id=portfolio_id,
        name="V1.38 test",
        entities=entities,
        relations=relations,
    )


def _main_portfolio() -> Portfolio:
    """Two projects and three tasks with a deliberately ordered relation set.

    Portfolio relation order (authoritative for incoming/outgoing order):
      1. T2 -> T1   DEPENDS_ON   (id ...9a, AI_INFERRED, 0.25)
      2. T1 -> P_A  BELONGS_TO   (id ...9b)
      3. T1 -> T3   BLOCKS       (id ...9c, 0.75)
      4. T1 -> T3   WAITING_FOR  (id ...9d)
      5. T1 -> T3   BLOCKS       (id ...9e, AI_INFERRED, 0.5)
      6. T3 -> T1   PRECEDES     (id ...9f)

    Note: relation #1 has the HIGHEST id, so relation-id sorting would
    move it to the end of the T1 incoming order.
    """
    alpha = _entity(P_ALPHA, EntityType.PROJECT, "Alpha")
    beta = _entity(P_BETA, EntityType.PROJECT, "Beta")
    t1 = _entity(T1, EntityType.TASK, "Ship", status=EntityStatus.WAITING)
    t2 = _entity(T2, EntityType.TASK, "Wire API", status=EntityStatus.ACTIVE)
    t3 = _entity(T3, EntityType.TASK, "Review", status=EntityStatus.PAUSED)
    deliverable = _entity(
        X_DELIVERABLE, EntityType.DELIVERABLE, "Gate deliverable"
    )

    return _portfolio(
        entities=[alpha, beta, t1, t2, t3, deliverable],
        relations=[
            _relation(
                R_T1_T1_DEPENDS, T2, T1, RelationType.DEPENDS_ON,
                source=SourceKind.AI_INFERRED, confidence=0.25,
            ),
            _relation(R_T1_ALPHA_BELONGS, T1, P_ALPHA, RelationType.BELONGS_TO),
            _relation(
                R_T1_T3_BLOCKS_A, T1, T3, RelationType.BLOCKS, confidence=0.75
            ),
            _relation(R_T1_T3_WAITING, T1, T3, RelationType.WAITING_FOR),
            _relation(
                R_T1_T3_BLOCKS_B, T1, T3, RelationType.BLOCKS,
                source=SourceKind.AI_INFERRED, confidence=0.5,
            ),
            _relation(R_T3_T1_PRECEDES, T3, T1, RelationType.PRECEDES),
        ],
    )


def _v137(
    projects: tuple[FocusedProjectTaskWorkUnits, ...],
    *,
    count: int | None = None,
    portfolio_id: uuid.UUID = PORTFOLIO_ID,
    decision_id: uuid.UUID = DECISION_ID,
    decided_at: datetime = DECIDED_AT,
) -> PortfolioProjectFocusTaskWorkUnitProjection:
    """A valid V1.37 projection shape for boundary tests."""
    if count is None:
        count = len(projects)
    return PortfolioProjectFocusTaskWorkUnitProjection(
        decision_id=decision_id,
        decided_at=decided_at,
        portfolio_id=portfolio_id,
        selected_project_count=count,
        projects=projects,
    )


def _v137_row(
    project_id: uuid.UUID, task_ids: tuple[uuid.UUID, ...] = ()
) -> FocusedProjectTaskWorkUnits:
    return FocusedProjectTaskWorkUnits(
        project_id=project_id, task_ids=tuple(task_ids)
    )


def _main_v137() -> PortfolioProjectFocusTaskWorkUnitProjection:
    """V1.37 focus: P_BETA first, P_ALPHA second; tasks (T3), (T2, T1)."""
    return _v137((_v137_row(P_BETA, (T3,)), _v137_row(P_ALPHA, (T2, T1))))


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


class _ForeignModel(BaseModel):
    """A different model; must never be accepted."""

    model_config = {"frozen": True}

    field: str = "foreign"


# ---------------------------------------------------------------------------
# Model strictness.
# ---------------------------------------------------------------------------


class TestFocusedTaskCurrentRelationModel:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        relation = _rel(R_T1_T3_BLOCKS_A, T1, T3, RelationType.BLOCKS)
        assert relation.model_config["strict"] is True
        assert relation.model_config["frozen"] is True
        assert relation.model_config["extra"] == "forbid"

        with pytest.raises(ValidationError):
            FocusedTaskCurrentRelation(
                relation_id=R_T1_T3_BLOCKS_A,
                relation_type=RelationType.BLOCKS,
                source_id=T1,
                target_id=T3,
                source=SourceKind.USER_CONFIRMED,
                confidence=1.0,
                unexpected_extra="not-allowed",
            )

        with pytest.raises(ValidationError):
            relation.relation_id = R_T3_T1_PRECEDES  # type: ignore[misc]

        # strict UUID: a string identity is rejected
        with pytest.raises(ValidationError):
            FocusedTaskCurrentRelation(
                relation_id="not-a-uuid",  # type: ignore[arg-type]
                relation_type=RelationType.BLOCKS,
                source_id=T1,
                target_id=T3,
                source=SourceKind.USER_CONFIRMED,
                confidence=1.0,
            )

        # strict enum: a bare string relation type / source is rejected
        with pytest.raises(ValidationError):
            FocusedTaskCurrentRelation(
                relation_id=R_T1_T3_BLOCKS_A,
                relation_type="blocks",  # type: ignore[arg-type]
                source_id=T1,
                target_id=T3,
                source=SourceKind.USER_CONFIRMED,
                confidence=1.0,
            )

        with pytest.raises(ValidationError):
            FocusedTaskCurrentRelation(
                relation_id=R_T1_T3_BLOCKS_A,
                relation_type=RelationType.BLOCKS,
                source_id=T1,
                target_id=T3,
                source="user_confirmed",  # type: ignore[arg-type]
                confidence=1.0,
            )


class TestFocusedTaskCurrentRelationsModel:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        row = _task_row(T1)
        assert row.model_config["strict"] is True
        assert row.model_config["frozen"] is True
        assert row.model_config["extra"] == "forbid"

        with pytest.raises(ValidationError):
            FocusedTaskCurrentRelations(
                task_id=T1,
                status=EntityStatus.ACTIVE,
                incoming_relations=(),
                outgoing_relations=(),
                unexpected_extra="not-allowed",
            )

        with pytest.raises(ValidationError):
            row.task_id = T2  # type: ignore[misc]

    def test_incoming_target_invariant_violation_rejected(self) -> None:
        with pytest.raises(ValidationError, match="target_id"):
            _task_row(
                T1,
                incoming=(
                    _rel(R_T1_T3_BLOCKS_A, T2, OTHER_TASK, RelationType.BLOCKS),
                ),
            )

    def test_outgoing_source_invariant_violation_rejected(self) -> None:
        with pytest.raises(ValidationError, match="source_id"):
            _task_row(
                T1,
                outgoing=(
                    _rel(R_T1_T3_BLOCKS_A, OTHER_TASK, T3, RelationType.BLOCKS),
                ),
            )

    def test_duplicate_relation_ids_within_incoming_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="unique within incoming_relations"
        ):
            _task_row(
                T1,
                incoming=(
                    _rel(R_T1_T3_BLOCKS_A, T2, T1, RelationType.DEPENDS_ON),
                    _rel(R_T1_T3_BLOCKS_A, T3, T1, RelationType.PRECEDES),
                ),
            )

    def test_duplicate_relation_ids_within_outgoing_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="unique within outgoing_relations"
        ):
            _task_row(
                T1,
                outgoing=(
                    _rel(R_T1_T3_BLOCKS_A, T1, T2, RelationType.BLOCKS),
                    _rel(R_T1_T3_BLOCKS_A, T1, T3, RelationType.WAITING_FOR),
                ),
            )

    def test_empty_relation_tuples_valid(self) -> None:
        assert _task_row(T1).incoming_relations == ()
        assert _task_row(T1).outgoing_relations == ()

    def test_same_relation_id_in_both_directions_is_coherent(self) -> None:
        # A row in each direction does not violate the per-direction
        # uniqueness invariants.
        row = _task_row(
            T1,
            incoming=(_rel(R_T3_T1_PRECEDES, T3, T1, RelationType.PRECEDES),),
            outgoing=(
                _rel(R_T1_T3_BLOCKS_A, T1, T3, RelationType.BLOCKS),
            ),
        )
        assert len(row.incoming_relations) == 1
        assert len(row.outgoing_relations) == 1


class TestFocusedProjectTaskCurrentRelationsModel:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        row = _project_row(P_ALPHA, (_task_row(T1),))
        assert row.model_config["strict"] is True
        assert row.model_config["frozen"] is True
        assert row.model_config["extra"] == "forbid"

        with pytest.raises(ValidationError):
            FocusedProjectTaskCurrentRelations(
                project_id=P_ALPHA,
                tasks=(),
                unexpected_extra="not-allowed",
            )

        with pytest.raises(ValidationError):
            row.project_id = P_BETA  # type: ignore[misc]

    def test_duplicate_task_ids_within_row_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="unique within a project row"
        ):
            _project_row(
                P_ALPHA, (_task_row(T1), _task_row(T1))
            )

    def test_empty_tasks_valid(self) -> None:
        assert _project_row(P_ALPHA).tasks == ()


class TestProjectionModel:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        projection = _projection(())
        assert projection.model_config["strict"] is True
        assert projection.model_config["frozen"] is True
        assert projection.model_config["extra"] == "forbid"

        with pytest.raises(ValidationError):
            projection.selected_project_count = 1  # type: ignore[misc]

        with pytest.raises(ValidationError):
            PortfolioProjectFocusTaskCurrentRelationProjection.model_validate(
                {
                    "decision_id": DECISION_ID,
                    "decided_at": DECIDED_AT,
                    "portfolio_id": PORTFOLIO_ID,
                    "selected_project_count": 0,
                    "projects": [],
                    "unexpected_extra": "not-allowed",
                }
            )

    def test_bool_rejected_for_selected_project_count(self) -> None:
        with pytest.raises(ValidationError, match="selected_project_count"):
            _projection((), count=True)  # type: ignore[arg-type]

    def test_negative_count_rejected(self) -> None:
        with pytest.raises(ValidationError, match="selected_project_count"):
            _projection((), count=-1)

    def test_selected_count_projects_tuple_mismatch_rejected(self) -> None:
        with pytest.raises(ValidationError, match="selected_project_count"):
            _projection((_project_row(P_ALPHA),), count=2)
        with pytest.raises(ValidationError, match="selected_project_count"):
            _projection((_project_row(P_ALPHA), _project_row(P_BETA)), count=1)

    def test_duplicate_project_ids_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="unique across project rows"
        ):
            _projection((_project_row(P_ALPHA), _project_row(P_ALPHA)), count=2)

    def test_duplicate_task_ids_within_row_rejected_via_projection(self) -> None:
        with pytest.raises(ValidationError):
            _projection(
                (_project_row(P_ALPHA, (_task_row(T1), _task_row(T1))),),
                count=1,
            )

    def test_duplicate_task_ids_across_rows_rejected(self) -> None:
        with pytest.raises(ValidationError, match="globally unique"):
            _projection(
                (
                    _project_row(P_ALPHA, (_task_row(T1),)),
                    _project_row(P_BETA, (_task_row(T1),)),
                ),
                count=2,
            )

    def test_zero_count_only_valid_with_empty_projects(self) -> None:
        assert _projection((), count=0).projects == ()
        with pytest.raises(ValidationError, match="selected_project_count"):
            _projection((_project_row(P_ALPHA),), count=0)


# ---------------------------------------------------------------------------
# Boundary hostility.
# ---------------------------------------------------------------------------


class TestBoundaryTypes:
    def test_genuine_v137_required(self) -> None:
        wrong = [
            None,
            "a v1.37 projection",
            {"decision_id": str(DECISION_ID)},
            [_v137_row(P_ALPHA)],
            _ForeignModel(),
            types.SimpleNamespace(
                portfolio_id=PORTFOLIO_ID, projects=()
            ),
            # the V1.37 row model is NOT the V1.37 projection model
            _v137_row(P_ALPHA),
        ]
        portfolio = _main_portfolio()
        for item in wrong:
            with pytest.raises(
                PortfolioProjectFocusTaskCurrentRelationProjectionError
            ):
                project_current_relations_for_focused_task_work_units(  # type: ignore[arg-type]
                    item, portfolio
                )

    def test_hostile_model_construct_bad_scalar_rejected(self) -> None:
        hostile = (
            PortfolioProjectFocusTaskWorkUnitProjection.model_construct(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count="one",  # string where StrictInt required
                projects=(_v137_row(P_ALPHA),),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError
        ):
            project_current_relations_for_focused_task_work_units(
                hostile, _main_portfolio()
            )

    def test_hostile_model_construct_malformed_nested_payload_rejected(
        self,
    ) -> None:
        hostile = (
            PortfolioProjectFocusTaskWorkUnitProjection.model_construct(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=1,
                projects=[
                    {
                        # malformed nested project payload: bad task uuid
                        "project_id": str(P_ALPHA),
                        "task_ids": ["not-a-uuid"],
                    }
                ],  # type: ignore[list-item]
            )
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError
        ):
            project_current_relations_for_focused_task_work_units(
                hostile, _main_portfolio()
            )

    def test_hostile_model_construct_duplicate_project_rows_rejected(self) -> None:
        hostile = (
            PortfolioProjectFocusTaskWorkUnitProjection.model_construct(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=2,
                projects=(
                    _v137_row(P_ALPHA),
                    _v137_row(P_ALPHA),  # V1.37 invariant: unique projects
                ),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError
        ):
            project_current_relations_for_focused_task_work_units(
                hostile, _main_portfolio()
            )

    def test_fresh_validated_copy_is_authoritative(self) -> None:
        # A model_construct() object carrying fully coherent values still
        # only passes via fresh strict re-validation; its provenance
        # fields flow into the V1.38 projection EXACTLY.
        constructed = (
            PortfolioProjectFocusTaskWorkUnitProjection.model_construct(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=2,
                projects=(
                    _v137_row(P_BETA, (T3,)),
                    _v137_row(P_ALPHA, (T2, T1)),
                ),
            )
        )
        projection = project_current_relations_for_focused_task_work_units(
            constructed, _main_portfolio()
        )
        assert projection.decision_id == DECISION_ID
        assert projection.decided_at == DECIDED_AT
        assert projection.portfolio_id == PORTFOLIO_ID
        assert [row.project_id for row in projection.projects] == [
            P_BETA,
            P_ALPHA,
        ]

    def test_inconsistent_caller_payload_cannot_be_salvaged_by_reading_original(
        self,
    ) -> None:
        # A caller object whose raw fields FAIL the V1.37 invariants
        # (count/tuple mismatch) is rejected: the boundary cannot read the
        # caller-owned original after re-validation to build a "valid
        # enough" projection — fresh re-validation is authoritative.
        inconsistent = (
            PortfolioProjectFocusTaskWorkUnitProjection.model_construct(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=5,  # raw field inconsistent with projects
                projects=(_v137_row(P_ALPHA, (T1,)),),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError
        ):
            project_current_relations_for_focused_task_work_units(
                inconsistent, _main_portfolio()
            )

    def test_genuine_portfolio_required(self) -> None:
        wrong = [
            None,
            "a portfolio",
            {"id": str(PORTFOLIO_ID)},
            _ForeignModel(),
            types.SimpleNamespace(id=PORTFOLIO_ID, entities=(), relations=()),
        ]
        v137 = _main_v137()
        for item in wrong:
            with pytest.raises(
                PortfolioProjectFocusTaskCurrentRelationProjectionError
            ):
                project_current_relations_for_focused_task_work_units(
                    v137, item  # type: ignore[arg-type]
                )


# ---------------------------------------------------------------------------
# Portfolio compatibility.
# ---------------------------------------------------------------------------


class TestPortfolioCompatibility:
    def test_portfolio_id_mismatch_rejected(self) -> None:
        v137 = _main_v137()
        conflicting = Portfolio(
            id=OTHER_PORTFOLIO_ID,
            name="Other",
            entities=list(_main_portfolio().entities),
            relations=list(_main_portfolio().relations),
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError,
            match="does not exactly match",
        ):
            project_current_relations_for_focused_task_work_units(
                v137, conflicting
            )

    def test_v137_of_other_portfolio_rejected(self) -> None:
        v137 = _v137(
            (_v137_row(P_BETA, (T3,)), _v137_row(P_ALPHA, (T2, T1))),
            portfolio_id=OTHER_PORTFOLIO_ID,
            count=2,
        )
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError,
            match="does not exactly match",
        ):
            project_current_relations_for_focused_task_work_units(
                v137, _main_portfolio()
            )

    def test_focused_project_missing_from_current_portfolio_rejected(self) -> None:
        missing = uuid.UUID("f0000000-0000-4000-8000-000000000071")
        v137 = _v137((_v137_row(missing, (T1,)),), count=1)
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError,
            match="not present in the CURRENT portfolio",
        ):
            project_current_relations_for_focused_task_work_units(
                v137, _main_portfolio()
            )

    def test_focused_project_resolving_to_non_project_rejected(self) -> None:
        v137 = _v137((_v137_row(X_DELIVERABLE, (T1,)),), count=1)
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError,
            match="must resolve to a PROJECT entity",
        ):
            project_current_relations_for_focused_task_work_units(
                v137, _main_portfolio()
            )

    def test_focused_project_resolving_to_task_rejected(self) -> None:
        v137 = _v137((_v137_row(T1,),), count=1)
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError,
            match="must resolve to a PROJECT entity",
        ):
            project_current_relations_for_focused_task_work_units(
                v137, _main_portfolio()
            )

    def test_focused_task_missing_from_current_portfolio_rejected(self) -> None:
        missing = uuid.UUID("f0000000-0000-4000-8000-000000000072")
        v137 = _v137((_v137_row(P_ALPHA, (missing,)),), count=1)
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError,
            match="not present in the CURRENT portfolio",
        ):
            project_current_relations_for_focused_task_work_units(
                v137, _main_portfolio()
            )

    def test_focused_task_resolving_to_non_task_rejected(self) -> None:
        v137 = _v137((_v137_row(P_ALPHA, (X_DELIVERABLE,)),), count=1)
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError,
            match="must resolve to a TASK entity",
        ):
            project_current_relations_for_focused_task_work_units(
                v137, _main_portfolio()
            )

    def test_focused_task_resolving_to_project_rejected(self) -> None:
        v137 = _v137((_v137_row(P_BETA, (P_ALPHA,)),), count=1)
        with pytest.raises(
            PortfolioProjectFocusTaskCurrentRelationProjectionError,
            match="must resolve to a TASK entity",
        ):
            project_current_relations_for_focused_task_work_units(
                v137, _main_portfolio()
            )


# ---------------------------------------------------------------------------
# Projection: exact CURRENT facts.
# ---------------------------------------------------------------------------


class TestProjectionFacts:
    def test_exact_current_status_copied(self) -> None:
        projection = project_current_relations_for_focused_task_work_units(
            _main_v137(), _main_portfolio()
        )
        by_task = {
            task.task_id: task
            for row in projection.projects
            for task in row.tasks
        }
        assert by_task[T1].status is EntityStatus.WAITING
        assert by_task[T2].status is EntityStatus.ACTIVE
        assert by_task[T3].status is EntityStatus.PAUSED

    def test_status_copied_from_current_entity_not_from_v137(self) -> None:
        # V1.37 carries no status at all: the status must come from the
        # CURRENT portfolio entity.
        v137 = _v137((_v137_row(P_ALPHA, (T1,)),), count=1)
        portfolio = _main_portfolio()
        projection = project_current_relations_for_focused_task_work_units(
            v137, portfolio
        )
        assert (
            projection.projects[0].tasks[0].status
            is portfolio.get_entity(T1).status
        )

    def test_every_relation_type_preserved_unfiltered(self) -> None:
        projection = project_current_relations_for_focused_task_work_units(
            _main_v137(), _main_portfolio()
        )
        observed_types = {
            relation.relation_type
            for row in projection.projects
            for task in row.tasks
            for relation in (
                task.incoming_relations + task.outgoing_relations
            )
        }
        assert observed_types == {
            RelationType.DEPENDS_ON,
            RelationType.BELONGS_TO,
            RelationType.BLOCKS,
            RelationType.WAITING_FOR,
            RelationType.PRECEDES,
        }

    def test_relation_fields_copied_exactly(self) -> None:
        projection = project_current_relations_for_focused_task_work_units(
            _main_v137(), _main_portfolio()
        )
        t1_row = projection.projects[1].tasks[1]  # V1.37 order: (T2, T1)
        depends = t1_row.incoming_relations[0]
        assert depends.relation_id is R_T1_T1_DEPENDS
        assert depends.relation_type is RelationType.DEPENDS_ON
        assert depends.source_id is T2
        assert depends.target_id is T1
        assert depends.source is SourceKind.AI_INFERRED
        assert depends.confidence == 0.25

        blocks_b = t1_row.outgoing_relations[3]
        assert blocks_b.relation_id is R_T1_T3_BLOCKS_B
        assert blocks_b.relation_type is RelationType.BLOCKS
        assert blocks_b.source is SourceKind.AI_INFERRED
        assert blocks_b.confidence == 0.5

    def test_same_type_distinct_relation_rows_both_preserved(self) -> None:
        projection = project_current_relations_for_focused_task_work_units(
            _main_v137(), _main_portfolio()
        )
        t1_row = projection.projects[1].tasks[1]
        outgoing_ids = [r.relation_id for r in t1_row.outgoing_relations]
        assert outgoing_ids == [
            R_T1_ALPHA_BELONGS,
            R_T1_T3_BLOCKS_A,
            R_T1_T3_WAITING,
            R_T1_T3_BLOCKS_B,
        ]


# ---------------------------------------------------------------------------
# Order / structure.
# ---------------------------------------------------------------------------


class TestOrderStructure:
    def test_project_order_exactly_preserved(self) -> None:
        projection = project_current_relations_for_focused_task_work_units(
            _main_v137(), _main_portfolio()
        )
        assert [row.project_id for row in projection.projects] == [
            P_BETA,
            P_ALPHA,
        ]

    def test_task_order_exactly_preserved(self) -> None:
        projection = project_current_relations_for_focused_task_work_units(
            _main_v137(), _main_portfolio()
        )
        by_project = {
            row.project_id: [t.task_id for t in row.tasks]
            for row in projection.projects
        }
        # V1.37 lists (T2, T1); a UUID-sorted output would be (T1, T2).
        assert by_project[P_ALPHA] == [T2, T1]
        assert by_project[P_BETA] == [T3]

    def test_incoming_order_follows_portfolio_relations(self) -> None:
        projection = project_current_relations_for_focused_task_work_units(
            _main_v137(), _main_portfolio()
        )
        t1_row = projection.projects[1].tasks[1]
        # DEPENDS_ON (...9a, highest id) MUST precede PRECEDES (...9f):
        # this would be inverted by any relation-id sort.
        assert [r.relation_id for r in t1_row.incoming_relations] == [
            R_T1_T1_DEPENDS,
            R_T3_T1_PRECEDES,
        ]

    def test_outgoing_order_follows_portfolio_relations(self) -> None:
        projection = project_current_relations_for_focused_task_work_units(
            _main_v137(), _main_portfolio()
        )
        t3_row = projection.projects[0].tasks[0]
        assert [r.relation_id for r in t3_row.incoming_relations] == [
            R_T1_T3_BLOCKS_A,
            R_T1_T3_WAITING,
            R_T1_T3_BLOCKS_B,
        ]
        assert [r.relation_id for r in t3_row.outgoing_relations] == [
            R_T3_T1_PRECEDES,
        ]

    def test_relation_ids_never_deduplicated_or_reordered(self) -> None:
        # T1 outgoing contains TWO distinct BLOCKS rows; both survive in
        # portfolio order, proving no type/direction deduplication policy.
        projection = project_current_relations_for_focused_task_work_units(
            _main_v137(), _main_portfolio()
        )
        t1_row = projection.projects[1].tasks[1]
        blocks = [
            r for r in t1_row.outgoing_relations
            if r.relation_type is RelationType.BLOCKS
        ]
        assert [r.relation_id for r in blocks] == [
            R_T1_T3_BLOCKS_A,
            R_T1_T3_BLOCKS_B,
        ]


# ---------------------------------------------------------------------------
# No-status non-policy.
# ---------------------------------------------------------------------------


class TestStatusNonPolicy:
    @pytest.mark.parametrize(
        "status",
        [
            EntityStatus.ACTIVE,
            EntityStatus.WAITING,
            EntityStatus.PAUSED,
            EntityStatus.INCUBATOR,
            EntityStatus.COMPLETED,
            EntityStatus.CANCELLED,
            EntityStatus.ARCHIVED,
        ],
    )
    def test_task_projected_in_every_lifecycle_state(self, status) -> None:
        v137 = _v137((_v137_row(P_ALPHA, (T1,)),), count=1)
        project = _entity(P_ALPHA, EntityType.PROJECT, "Status project")
        task = _entity(T1, EntityType.TASK, "Any status task", status=status)
        status_portfolio = _portfolio(
            entities=[project, task], relations=[]
        )
        projection = project_current_relations_for_focused_task_work_units(
            v137, status_portfolio
        )
        assert projection.projects[0].tasks[0].status is status
        assert projection.projects[0].tasks[0].task_id is T1


# ---------------------------------------------------------------------------
# Empty states.
# ---------------------------------------------------------------------------


class TestEmptyStates:
    def test_zero_selected_projects_returns_empty(self) -> None:
        v137 = _v137((), count=0)
        projection = project_current_relations_for_focused_task_work_units(
            v137, _main_portfolio()
        )
        assert projection.projects == ()
        assert projection.selected_project_count == 0

    def test_zero_selection_fabricates_nothing(self) -> None:
        own_decision = uuid.UUID("73636363-6363-4363-8363-736363636363")
        own_time = datetime(2025, 9, 1, 12, 0, tzinfo=UTC)
        v137 = _v137(
            (),
            count=0,
            decision_id=own_decision,
            decided_at=own_time,
        )
        projection = project_current_relations_for_focused_task_work_units(
            v137, _main_portfolio()
        )
        assert projection.decision_id == own_decision
        assert projection.decided_at == own_time
        assert projection.portfolio_id == PORTFOLIO_ID
        assert projection.projects == ()

    def test_project_with_zero_tasks_retained(self) -> None:
        v137 = _v137((_v137_row(P_ALPHA,),), count=1)
        projection = project_current_relations_for_focused_task_work_units(
            v137, _main_portfolio()
        )
        assert len(projection.projects) == 1
        assert projection.projects[0].project_id is P_ALPHA
        assert projection.projects[0].tasks == ()

    def test_task_with_zero_relations_retained(self) -> None:
        v137 = _v137((_v137_row(P_ALPHA, (T1,)),), count=1)
        bare_portfolio = _portfolio(
            entities=[
                _entity(P_ALPHA, EntityType.PROJECT, "Bare project"),
                _entity(T1, EntityType.TASK, "Bare task"),
            ],
            relations=[],
        )
        projection = project_current_relations_for_focused_task_work_units(
            v137, bare_portfolio
        )
        task_row = projection.projects[0].tasks[0]
        assert task_row.task_id is T1
        assert task_row.incoming_relations == ()
        assert task_row.outgoing_relations == ()


# ---------------------------------------------------------------------------
# Discipline.
# ---------------------------------------------------------------------------


class TestDiscipline:
    def test_repeated_identical_calls_value_identical(self) -> None:
        v137 = _main_v137()
        portfolio = _main_portfolio()
        first = project_current_relations_for_focused_task_work_units(
            v137, portfolio
        )
        second = project_current_relations_for_focused_task_work_units(
            v137, portfolio
        )
        assert first == second

    def test_v137_input_not_mutated(self) -> None:
        v137 = _main_v137()
        before = v137.model_dump(mode="json")
        project_current_relations_for_focused_task_work_units(
            v137, _main_portfolio()
        )
        assert v137.model_dump(mode="json") == before

    def test_portfolio_not_mutated(self) -> None:
        portfolio = _main_portfolio()
        before = portfolio.model_dump(mode="json")
        project_current_relations_for_focused_task_work_units(
            _main_v137(), portfolio
        )
        assert portfolio.model_dump(mode="json") == before

    def test_no_work_breakdown_construction_for_semantics(self) -> None:
        module_vars = vars(v138_mod)
        assert "build_work_breakdown" not in module_vars
        assert "WorkBreakdownError" not in module_vars

        source = Path(v138_mod.__file__).read_text()
        assert "build_work_breakdown" not in source
        assert "work_breakdown" not in source
        assert "BELONGS_TO" not in source

    def test_no_repository_persistence_or_provider_surface(self) -> None:
        module_vars = vars(v138_mod)
        assert not any(
            "repository" in name.lower()
            or "persist" in name.lower()
            or "provider" in name.lower()
            for name in module_vars
        )

        source = Path(v138_mod.__file__).read_text()
        assert "datetime.now" not in source
        assert "uuid4" not in source
        assert "uuid5" not in source
        assert "random" not in source
        assert "import os" not in source
        assert "requests" not in source

    def test_inference_free_vocabulary(self) -> None:
        # The boundary must never implement inference concepts under any
        # public name (they are disclaimed in the module docstring).
        public_names = [
            name for name in vars(v138_mod) if not name.startswith("_")
        ]
        for name in public_names:
            lowered = name.lower()
            for forbidden in (
                "readiness",
                "ready",
                "blockage",
                "satisfaction",
                "nextaction",
                "nexttask",
                "priority",
                "recommend",
                "executionorder",
            ):
                assert forbidden not in lowered
        assert "project_current_relations_for_focused_task_work_units" in public_names

    def test_public_exports_correct(self) -> None:
        required = [
            "FocusedTaskCurrentRelation",
            "FocusedTaskCurrentRelations",
            "FocusedProjectTaskCurrentRelations",
            "PortfolioProjectFocusTaskCurrentRelationProjection",
            "PortfolioProjectFocusTaskCurrentRelationProjectionError",
            "project_current_relations_for_focused_task_work_units",
        ]
        for name in required:
            assert name in app.__all__
            assert getattr(app, name) is not None

    def test_error_is_a_value_error(self) -> None:
        assert issubclass(
            PortfolioProjectFocusTaskCurrentRelationProjectionError,
            ValueError,
        )
