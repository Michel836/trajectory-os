"""Unit tests for V1.40 — deterministic READY TASK candidate projection.

Covers:

  - genuine / hostile input discipline (None, dict, string, foreign
    model, hostile ``model_construct`` scalars at top-level and nested
    levels, hostile nested project / task / readiness states, and proof
    that fresh re-validation — not the caller-owned original — is
    authoritative);
  - the exact READY-only filter policy (READY retained, CONSTRAINED and
    INELIGIBLE_STATUS excluded, no re-evaluation of V1.39 semantics);
  - exact V1.39 project order and exact in-place V1.39 task order
    preservation (including zero-READY project rows in source position);
  - all empty / zero-READY states;
  - model invariants (strict / frozen / extra-forbid, unique project
    and task IDs, exact counts, hostile direct construction rejected);
  - behavior (repeated identical calls value-identical; input not
    mutated);
  - public surface and architecture discipline (exact exports, no
    Portfolio argument, no WBS, no repository / persistence / provider /
    runtime / clock / UUID surface, no ranking / scoring / selection
    policy).
"""

from __future__ import annotations

import inspect
import types
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import trajectory_os.application as app
import trajectory_os.application.execution_effort_project_focus_ready_task_candidates as v140_mod  # noqa: E501
from trajectory_os.application import (
    PortfolioProjectFocusReadyTaskCandidates,
    PortfolioProjectFocusReadyTaskCandidatesError,
    ReadyProjectTaskCandidates,
    ReadyTaskCandidate,
    project_ready_task_candidates_from_current_constraint_evaluation,
)
from trajectory_os.application.execution_effort_project_focus_task_current_constraints import (  # noqa: E501
    FocusedProjectTaskCurrentConstraintEvaluations,
    FocusedTaskConstraintReadinessState,
    FocusedTaskCurrentConstraint,
    FocusedTaskCurrentConstraintEvaluation,
    PortfolioProjectFocusTaskCurrentConstraintEvaluation,
)
from trajectory_os.domain.entities import EntityStatus, EntityType
from trajectory_os.domain.relations import RelationType

PORTFOLIO_ID = uuid.UUID("71616161-6161-4161-8161-616161616161")
DECISION_ID = uuid.UUID("72626262-6262-4262-8262-726262626262")
DECIDED_AT = datetime(2025, 7, 2, 9, 15, tzinfo=UTC)

# Project identities deliberately INVERTED against natural UUID order so
# any project-UUID-sorted output differs.
P_ALPHA = uuid.UUID("a0000000-0000-4000-8000-00000000000a")
P_BETA = uuid.UUID("b0000000-0000-4000-8000-00000000000b")
P_GAMMA = uuid.UUID("c0000000-0000-4000-8000-00000000000c")

# Task identities deliberately INVERTED against natural UUID order so
# any task-UUID-sorted output differs.
T_AAA = uuid.UUID("d1d1d1d1-0000-4000-8000-000000000011")
T_BBB = uuid.UUID("c2c2c2c2-0000-4000-8000-000000000022")
T_CCC = uuid.UUID("e3e3e3e3-0000-4000-8000-000000000033")
T_DDD = uuid.UUID("f4f4f4f4-0000-4000-8000-000000000044")
T_EEE = uuid.UUID("a5a5a5a5-0000-4000-8000-000000000055")

R1 = uuid.UUID("f0000000-0000-4000-8000-000000000091")
R2 = uuid.UUID("f0000000-0000-4000-8000-000000000092")
C1 = uuid.UUID("c1c1c1c1-0000-4000-8000-000000000001")
C2 = uuid.UUID("c2c2c2c2-0000-4000-8000-000000000002")


# ---------------------------------------------------------------------------
# Fixture helpers — genuine V1.39 evaluation construction.
# ---------------------------------------------------------------------------


def _constraint(
    relation_id: uuid.UUID,
    counterpart_id: uuid.UUID,
    status: EntityStatus | None = None,
) -> FocusedTaskCurrentConstraint:
    counterpart_status = (
        status if status is not None else EntityStatus.COMPLETED
    )
    return FocusedTaskCurrentConstraint(
        relation_id=relation_id,
        relation_type=RelationType.DEPENDS_ON,
        counterpart_entity_id=counterpart_id,
        counterpart_entity_type=EntityType.TASK,
        counterpart_status=counterpart_status,
        satisfied=counterpart_status is EntityStatus.COMPLETED,
    )


def _task(
    task_id: uuid.UUID,
    readiness: FocusedTaskConstraintReadinessState,
    *,
    status: EntityStatus = EntityStatus.ACTIVE,
    constraints: tuple[FocusedTaskCurrentConstraint, ...] = (),
) -> FocusedTaskCurrentConstraintEvaluation:
    return FocusedTaskCurrentConstraintEvaluation(
        task_id=task_id,
        task_status=status,
        readiness_state=readiness,
        constraints=constraints,
        unsatisfied_constraint_count=sum(
            1 for c in constraints if not c.satisfied
        ),
    )


def _project(
    project_id: uuid.UUID,
    tasks: tuple[FocusedTaskCurrentConstraintEvaluation, ...] = (),
) -> FocusedProjectTaskCurrentConstraintEvaluations:
    return FocusedProjectTaskCurrentConstraintEvaluations(
        project_id=project_id,
        tasks=tasks,
    )


def _evaluation(
    projects: tuple[FocusedProjectTaskCurrentConstraintEvaluations, ...],
    *,
    count: int | None = None,
    portfolio_id: uuid.UUID = PORTFOLIO_ID,
    decision_id: uuid.UUID = DECISION_ID,
    decided_at: datetime = DECIDED_AT,
) -> PortfolioProjectFocusTaskCurrentConstraintEvaluation:
    if count is None:
        count = len(projects)
    return PortfolioProjectFocusTaskCurrentConstraintEvaluation(
        decision_id=decision_id,
        decided_at=decided_at,
        portfolio_id=portfolio_id,
        selected_project_count=count,
        projects=projects,
    )


class _ForeignModel(BaseModel):
    """A different model; must never be accepted as a V1.39 payload."""

    model_config = {"frozen": True}

    field: str = "foreign"


def _ready(task_id: uuid.UUID) -> ReadyTaskCandidate:
    return ReadyTaskCandidate(task_id=task_id)


def _project_ids(
    result: PortfolioProjectFocusReadyTaskCandidates,
) -> list[uuid.UUID]:
    return [row.project_id for row in result.projects]


def _task_ids(
    result: PortfolioProjectFocusReadyTaskCandidates,
    project_id: uuid.UUID,
) -> list[uuid.UUID]:
    for row in result.projects:
        if row.project_id == project_id:
            return [task.task_id for task in row.tasks]
    raise AssertionError(f"project {project_id} not found in result")


# ---------------------------------------------------------------------------
# Output model invariants.
# ---------------------------------------------------------------------------


class TestReadyTaskCandidateModel:
    def test_strict_frozen_extra_forbid(self) -> None:
        candidate = _ready(T_AAA)

        with pytest.raises(ValidationError, match="task_id"):
            ReadyTaskCandidate(task_id="d1d1d1d1")  # type: ignore[arg-type]

        with pytest.raises(ValidationError):
            ReadyTaskCandidate(task_id=1)  # type: ignore[arg-type]

        with pytest.raises(ValidationError, match="Extra inputs"):
            ReadyTaskCandidate(task_id=T_AAA, status="active")  # type: ignore[call-arg]

        with pytest.raises(ValidationError, match="Extra inputs"):
            ReadyTaskCandidate(task_id=T_AAA, extra="nope")  # type: ignore[call-arg]

        with pytest.raises((ValidationError, AttributeError)):
            candidate.task_id = T_BBB  # type: ignore[misc]

    def test_exactly_one_field(self) -> None:
        assert set(ReadyTaskCandidate.model_fields) == {"task_id"}


class TestReadyProjectRowModel:
    def test_exactly_two_fields(self) -> None:
        assert set(ReadyProjectTaskCandidates.model_fields) == {
            "project_id",
            "tasks",
        }

    def test_strict_scalars_and_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            ReadyProjectTaskCandidates(
                project_id="alpha",  # type: ignore[arg-type]
                tasks=(),
            )
        with pytest.raises(ValidationError, match="Extra inputs"):
            ReadyProjectTaskCandidates(
                project_id=P_ALPHA, tasks=(), note="x"  # type: ignore[call-arg]
            )

        with pytest.raises((ValidationError, AttributeError)):
            row = ReadyProjectTaskCandidates(
                project_id=P_ALPHA, tasks=(_ready(T_AAA),)
            )
            row.project_id = P_BETA  # type: ignore[misc]

    def test_empty_tasks_tuple_valid(self) -> None:
        row = ReadyProjectTaskCandidates(project_id=P_ALPHA, tasks=())
        assert row.tasks == ()

    def test_unique_task_ids_within_row(self) -> None:
        with pytest.raises(ValidationError, match="unique within"):
            ReadyProjectTaskCandidates(
                project_id=P_ALPHA,
                tasks=(_ready(T_AAA), _ready(T_AAA)),
            )

    def test_frozen(self) -> None:
        row = ReadyProjectTaskCandidates(
            project_id=P_ALPHA, tasks=(_ready(T_AAA),)
        )
        with pytest.raises((ValidationError, AttributeError)):
            row.project_id = P_BETA  # type: ignore[misc]


class TestFinalCandidateModel:
    def test_exactly_six_fields(self) -> None:
        assert set(PortfolioProjectFocusReadyTaskCandidates.model_fields) == {
            "decision_id",
            "decided_at",
            "portfolio_id",
            "selected_project_count",
            "ready_task_count",
            "projects",
        }

    def test_strict_frozen_extra_forbid(self) -> None:
        final = (
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=0,
                ready_task_count=0,
                projects=(),
            )
        )
        assert final.selected_project_count == 0

        with pytest.raises(ValidationError, match="decision_id"):
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id="nope",  # type: ignore[call-arg]
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=0,
                ready_task_count=0,
                projects=(),
            )

        with pytest.raises(ValidationError, match="Extra inputs"):
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=0,
                ready_task_count=0,
                projects=(),
                next_task=T_AAA,  # type: ignore[call-arg]
            )

        with pytest.raises(ValidationError):
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id=DECISION_ID,
                decided_at=str(DECIDED_AT),  # type: ignore[call-arg]
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=0,
                ready_task_count=0,
                projects=(),
            )

        with pytest.raises((ValidationError, AttributeError)):
            final.decision_id = DECISION_ID  # type: ignore[misc]

    def test_count_must_equal_project_lengths(self) -> None:
        row = ReadyProjectTaskCandidates(project_id=P_ALPHA, tasks=())
        with pytest.raises(ValidationError, match="selected_project_count"):
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=5,
                ready_task_count=0,
                projects=(row,),
            )

    def test_duplicate_project_ids_rejected(self) -> None:
        row_a = ReadyProjectTaskCandidates(
            project_id=P_ALPHA, tasks=(_ready(T_AAA),)
        )
        row_b = ReadyProjectTaskCandidates(
            project_id=P_ALPHA, tasks=(_ready(T_BBB),)
        )
        with pytest.raises(ValidationError, match="project IDs"):
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=2,
                ready_task_count=2,
                projects=(row_a, row_b),
            )

    def test_duplicate_task_ids_globally_rejected(self) -> None:
        row_a = ReadyProjectTaskCandidates(
            project_id=P_ALPHA, tasks=(_ready(T_AAA),)
        )
        row_b = ReadyProjectTaskCandidates(
            project_id=P_BETA, tasks=(_ready(T_AAA),)
        )
        with pytest.raises(ValidationError, match="task IDs"):
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=2,
                ready_task_count=2,
                projects=(row_a, row_b),
            )

    def test_ready_task_count_must_be_exact(self) -> None:
        row = ReadyProjectTaskCandidates(
            project_id=P_ALPHA, tasks=(_ready(T_AAA),)
        )
        with pytest.raises(ValidationError, match="ready_task_count"):
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=1,
                ready_task_count=3,
                projects=(row,),
            )

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=-1,
                ready_task_count=0,
                projects=(),
            )
        with pytest.raises(ValidationError):
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=0,
                ready_task_count=-1,
                projects=(),
            )

    def test_zero_selected_projects_implies_empty_projects(self) -> None:
        row = ReadyProjectTaskCandidates(
            project_id=P_ALPHA, tasks=(_ready(T_AAA),)
        )
        with pytest.raises(ValidationError):
            PortfolioProjectFocusReadyTaskCandidates(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=0,
                ready_task_count=1,
                projects=(row,),
            )


# ---------------------------------------------------------------------------
# Exact READY-only filtering.
# ---------------------------------------------------------------------------


class TestFiltering:
    def test_ready_retained(self) -> None:
        evaluation = _evaluation(
            (_project(P_ALPHA, (_task(T_AAA, FocusedTaskConstraintReadinessState.READY),)),)
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _task_ids(result, P_ALPHA) == [T_AAA]

    def test_constrained_excluded(self) -> None:
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    (
                        _task(
                            T_AAA,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(
                                _constraint(R1, C1, EntityStatus.ACTIVE),
                            ),
                        ),
                    ),
                ),
            )
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _task_ids(result, P_ALPHA) == []

    def test_ineligible_status_excluded(self) -> None:
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    (
                        _task(
                            T_AAA,
                            FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                            status=EntityStatus.WAITING,
                        ),
                    ),
                ),
            )
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _task_ids(result, P_ALPHA) == []

    def test_mixed_states_filtered_exactly(self) -> None:
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    (
                        _task(
                            T_DDD,
                            FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                            status=EntityStatus.PAUSED,
                        ),
                        _task(
                            T_AAA,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(_constraint(R1, C1, EntityStatus.ACTIVE),),
                        ),
                        _task(T_BBB, FocusedTaskConstraintReadinessState.READY),
                        _task(
                            T_CCC,
                            FocusedTaskConstraintReadinessState.READY,
                            constraints=(_constraint(R2, C2),),
                        ),
                        _task(
                            T_EEE,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(_constraint(R1, C2, EntityStatus.CANCELLED),),
                        ),
                    ),
                ),
            )
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _task_ids(result, P_ALPHA) == [T_BBB, T_CCC]
        assert ready_total(result) == 2

    def test_retention_decided_by_readiness_state_only(self) -> None:
        # The boundary's ONLY decision input is the V1.39 readiness
        # state. A READY row carrying satisfied constraints must still be
        # retained; a CONSTRAINED row must still be excluded even though
        # V1.40 never re-inspects the constraints; an INELIGIBLE_STATUS
        # row is excluded even though it carries zero constraints.
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    (
                        _task(
                            T_AAA,
                            FocusedTaskConstraintReadinessState.READY,
                            constraints=(_constraint(R1, C1),),
                        ),
                        _task(
                            T_BBB,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(_constraint(R2, C2, EntityStatus.ACTIVE),),
                        ),
                        _task(
                            T_CCC,
                            FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                            status=EntityStatus.ARCHIVED,
                        ),
                    ),
                ),
            )
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _task_ids(result, P_ALPHA) == [T_AAA]

    def test_output_carries_no_semantic_details(self) -> None:
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    (
                        _task(
                            T_AAA,
                            FocusedTaskConstraintReadinessState.READY,
                            constraints=(_constraint(R1, C1),),
                        ),
                    ),
                ),
            )
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        candidate = result.projects[0].tasks[0]
        # Only the exact task identity; no status, readiness, constraints,
        # score, rank, priority, urgency, or timestamp.
        assert set(candidate.model_dump(mode="python")) == {"task_id"}
        assert candidate.task_id == T_AAA


def ready_total(
    result: PortfolioProjectFocusReadyTaskCandidates,
) -> int:
    return result.ready_task_count


# ---------------------------------------------------------------------------
# Exact ordering preservation.
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_exact_project_order_preserved(self) -> None:
        # Deliberately NOT project-UUID-sorted order (BETA < ALPHA by
        # UUID lexical order in the second pair).
        order = [P_BETA, P_GAMMA, P_ALPHA]
        evaluation = _evaluation(
            (
                _project(P_BETA, (_task(T_AAA, FocusedTaskConstraintReadinessState.READY),)),
                _project(P_GAMMA, (_task(T_BBB, FocusedTaskConstraintReadinessState.READY),)),
                _project(P_ALPHA, (_task(T_CCC, FocusedTaskConstraintReadinessState.READY),)),
            ),
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _project_ids(result) == order
        assert order != sorted(order)  # sanity: the order is not UUID-sorted

    def test_exact_task_order_preserved_after_filtering(self) -> None:
        # Source order (E, D, C) is NOT task-UUID sorted order
        # (AAA < B... the retained ready rows appear exactly where they
        # sat in the source).
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    (
                        _task(
                            T_EEE,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(_constraint(R1, C1, EntityStatus.ACTIVE),),
                        ),
                        _task(T_DDD, FocusedTaskConstraintReadinessState.READY),
                        _task(
                            T_CCC,
                            FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                            status=EntityStatus.SOMEDAY,
                        ),
                        _task(T_BBB, FocusedTaskConstraintReadinessState.READY),
                        _task(T_AAA, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
            )
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _task_ids(result, P_ALPHA) == [T_DDD, T_BBB, T_AAA]

    def test_zero_ready_project_row_remains_in_source_position(self) -> None:
        middle = _project(
            P_GAMMA,
            (
                _task(
                    T_AAA,
                    FocusedTaskConstraintReadinessState.CONSTRAINED,
                    constraints=(_constraint(R1, C1, EntityStatus.ACTIVE),),
                ),
                _task(
                    T_BBB,
                    FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                    status=EntityStatus.CANCELLED,
                ),
            ),
        )
        evaluation = _evaluation(
            (
                _project(P_ALPHA, (_task(T_CCC, FocusedTaskConstraintReadinessState.READY),)),
                middle,
                _project(P_BETA, (_task(T_DDD, FocusedTaskConstraintReadinessState.READY),)),
            ),
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _project_ids(result) == [P_ALPHA, P_GAMMA, P_BETA]
        middle_row = result.projects[1]
        assert middle_row.project_id == P_GAMMA
        assert middle_row.tasks == ()
        assert result.selected_project_count == 3
        assert result.ready_task_count == 2

    def test_no_ordering_side_effects_across_projects(self) -> None:
        evaluation = _evaluation(
            (
                _project(
                    P_BETA,
                    (
                        _task(
                            T_EEE,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(_constraint(R1, C2, EntityStatus.WAITING),),
                        ),
                        _task(T_CCC, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
                _project(
                    P_ALPHA,
                    (
                        _task(T_AAA, FocusedTaskConstraintReadinessState.READY),
                        _task(T_BBB, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
            ),
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _project_ids(result) == [P_BETA, P_ALPHA]
        assert _task_ids(result, P_BETA) == [T_CCC]
        assert _task_ids(result, P_ALPHA) == [T_AAA, T_BBB]


# ---------------------------------------------------------------------------
# Empty and zero-READY states.
# ---------------------------------------------------------------------------


class TestEmptyStates:
    def test_zero_selected_projects(self) -> None:
        evaluation = _evaluation(())
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert result.decision_id == DECISION_ID
        assert result.decided_at == DECIDED_AT
        assert result.portfolio_id == PORTFOLIO_ID
        assert result.selected_project_count == 0
        assert result.ready_task_count == 0
        assert result.projects == ()

    def test_selected_project_with_zero_tasks(self) -> None:
        evaluation = _evaluation((_project(P_ALPHA),))
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _project_ids(result) == [P_ALPHA]
        assert result.projects[0].tasks == ()
        assert result.selected_project_count == 1
        assert result.ready_task_count == 0

    def test_source_tasks_but_zero_ready(self) -> None:
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    (
                        _task(
                            T_AAA,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(_constraint(R1, C1, EntityStatus.ACTIVE),),
                        ),
                        _task(
                            T_BBB,
                            FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                            status=EntityStatus.WAITING,
                        ),
                    ),
                ),
            )
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _project_ids(result) == [P_ALPHA]
        assert result.projects[0].tasks == ()
        assert result.selected_project_count == 1
        assert result.ready_task_count == 0

    def test_all_source_tasks_ready(self) -> None:
        source = (T_EEE, T_DDD, T_CCC, T_BBB, T_AAA)
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    tuple(
                        _task(t, FocusedTaskConstraintReadinessState.READY)
                        for t in source
                    ),
                ),
            )
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _task_ids(result, P_ALPHA) == list(source)
        assert result.ready_task_count == 5

    def test_mixed_readiness_across_multiple_projects(self) -> None:
        evaluation = _evaluation(
            (
                _project(
                    P_GAMMA,
                    (
                        _task(T_CCC, FocusedTaskConstraintReadinessState.READY),
                        _task(
                            T_DDD,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(_constraint(R1, C2, EntityStatus.ARCHIVED),),
                        ),
                    ),
                ),
                _project(P_ALPHA, (_task(T_AAA, FocusedTaskConstraintReadinessState.READY),)),
                _project(
                    P_BETA,
                    (
                        _task(
                            T_EEE,
                            FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                            status=EntityStatus.INCUBATOR,
                        ),
                    ),
                ),
            ),
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _project_ids(result) == [P_GAMMA, P_ALPHA, P_BETA]
        assert _task_ids(result, P_GAMMA) == [T_CCC]
        assert _task_ids(result, P_ALPHA) == [T_AAA]
        assert _task_ids(result, P_BETA) == []
        assert result.selected_project_count == 3
        assert result.ready_task_count == 2

    def test_globally_zero_ready_across_non_empty_projects(self) -> None:
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    (
                        _task(
                            T_AAA,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(_constraint(R1, C1, EntityStatus.ACTIVE),),
                        ),
                    ),
                ),
                _project(
                    P_BETA,
                    (
                        _task(
                            T_BBB,
                            FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                            status=EntityStatus.COMPLETED,
                        ),
                    ),
                ),
            ),
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _project_ids(result) == [P_ALPHA, P_BETA]
        assert all(row.tasks == () for row in result.projects)
        assert result.selected_project_count == 2
        assert result.ready_task_count == 0


# ---------------------------------------------------------------------------
# Genuine / hostile input discipline.
# ---------------------------------------------------------------------------


class TestBoundaryInputs:
    def test_genuine_v139_evaluation_accepted(self) -> None:
        evaluation = _evaluation(
            (_project(P_ALPHA, (_task(T_AAA, FocusedTaskConstraintReadinessState.READY),)),)
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert _task_ids(result, P_ALPHA) == [T_AAA]

    def test_wrong_payload_types_rejected(self) -> None:
        wrong = [
            None,
            "an evaluation",
            {"decision_id": str(DECISION_ID)},
            [1, 2, 3],
            _ForeignModel(),
            types.SimpleNamespace(decision_id=DECISION_ID, projects=()),
        ]
        for item in wrong:
            with pytest.raises(
                PortfolioProjectFocusReadyTaskCandidatesError,
                match="genuine V1.39",
            ):
                project_ready_task_candidates_from_current_constraint_evaluation(  # type: ignore[arg-type]
                    item
                )

    def test_hostile_model_construct_top_level_scalar_rejected(self) -> None:
        hostile = (
            PortfolioProjectFocusTaskCurrentConstraintEvaluation.model_construct(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count="one",  # hostile top-level scalar
                projects=(
                    _project(P_ALPHA, (_task(T_AAA, FocusedTaskConstraintReadinessState.READY),)),
                ),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusReadyTaskCandidatesError,
            match="strict re-validation",
        ):
            project_ready_task_candidates_from_current_constraint_evaluation(  # type: ignore[arg-type]
                hostile
            )

    def test_hostile_nested_project_row_rejected(self) -> None:
        bad_row = FocusedProjectTaskCurrentConstraintEvaluations.model_construct(
            project_id="alpha",  # hostile nested scalar
            tasks=(),
        )
        hostile = _wrap_hostile((bad_row,))
        with pytest.raises(
            PortfolioProjectFocusReadyTaskCandidatesError,
            match="strict re-validation",
        ):
            project_ready_task_candidates_from_current_constraint_evaluation(  # type: ignore[arg-type]
                hostile
            )

    def test_hostile_nested_task_evaluation_rejected(self) -> None:
        bad_task = FocusedTaskCurrentConstraintEvaluation.model_construct(
            task_id=T_AAA,
            task_status="brogue",  # hostile nested scalar
            readiness_state=FocusedTaskConstraintReadinessState.READY,
            constraints=(),
            unsatisfied_constraint_count=0,
        )
        hostile = _wrap_hostile((_hostile_project((bad_task,)),))
        with pytest.raises(
            PortfolioProjectFocusReadyTaskCandidatesError,
            match="strict re-validation",
        ):
            project_ready_task_candidates_from_current_constraint_evaluation(  # type: ignore[arg-type]
                hostile
            )

    def test_hostile_nested_readiness_state_rejected(self) -> None:
        # ACTIVE task with an unsatisfied constraint is semantically
        # CONSTRAINED; a "READY" readiness claim on such a row defeats
        # V1.39 invariants and must be rejected on fresh re-validation.
        unsatisfied = _constraint(R1, C1, EntityStatus.ACTIVE)
        bad_task = FocusedTaskCurrentConstraintEvaluation.model_construct(
            task_id=T_AAA,
            task_status=EntityStatus.ACTIVE,
            readiness_state=FocusedTaskConstraintReadinessState.READY,
            constraints=(unsatisfied,),
            unsatisfied_constraint_count=1,
        )
        hostile = _wrap_hostile((_hostile_project((bad_task,)),))
        with pytest.raises(
            PortfolioProjectFocusReadyTaskCandidatesError,
            match="strict re-validation",
        ):
            project_ready_task_candidates_from_current_constraint_evaluation(  # type: ignore[arg-type]
                hostile
            )

    def test_hostile_nested_readiness_state_string_rejected(self) -> None:
        bad_task = FocusedTaskCurrentConstraintEvaluation.model_construct(
            task_id=T_AAA,
            task_status=EntityStatus.ACTIVE,
            readiness_state="bogus",  # hostile nested scalar
            constraints=(),
            unsatisfied_constraint_count=0,
        )
        hostile = _wrap_hostile((_hostile_project((bad_task,)),))
        with pytest.raises(
            PortfolioProjectFocusReadyTaskCandidatesError,
            match="strict re-validation",
        ):
            project_ready_task_candidates_from_current_constraint_evaluation(  # type: ignore[arg-type]
                hostile
            )

    def test_fresh_validated_copy_is_authoritative(self) -> None:
        constructed = (
            PortfolioProjectFocusTaskCurrentConstraintEvaluation.model_construct(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=1,
                projects=(
                    _project(
                        P_ALPHA,
                        (
                            _task(T_AAA, FocusedTaskConstraintReadinessState.READY),
                            _task(
                                T_BBB,
                                FocusedTaskConstraintReadinessState.CONSTRAINED,
                                constraints=(_constraint(R1, C1, EntityStatus.ACTIVE),),
                            ),
                        ),
                    ),
                ),
            )
        )
        result = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                constructed
            )
        )
        assert result.decision_id == DECISION_ID
        assert result.decided_at == DECIDED_AT
        assert result.portfolio_id == PORTFOLIO_ID
        assert result.selected_project_count == 1
        assert _task_ids(result, P_ALPHA) == [T_AAA]

    def test_inconsistent_caller_payload_cannot_be_salvaged(self) -> None:
        # A caller object whose raw fields FAIL the V1.39 invariants
        # (count/tuple mismatch) is rejected: the boundary must not read
        # the caller-owned original after re-validation to build a "valid
        # enough" projection.
        inconsistent = (
            PortfolioProjectFocusTaskCurrentConstraintEvaluation.model_construct(  # noqa: E501
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=5,  # raw field inconsistent
                projects=(
                    _project(P_ALPHA, (_task(T_AAA, FocusedTaskConstraintReadinessState.READY),)),
                ),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusReadyTaskCandidatesError,
            match="strict re-validation",
        ):
            project_ready_task_candidates_from_current_constraint_evaluation(  # type: ignore[arg-type]
                inconsistent
            )

    def test_caller_row_with_duplicate_project_ids_rejected(self) -> None:
        duplicate_rows = (
            _project(
                P_ALPHA,
                (_task(T_AAA, FocusedTaskConstraintReadinessState.READY),),
            ),
            _project(
                P_ALPHA,
                (_task(T_BBB, FocusedTaskConstraintReadinessState.READY),),
            ),
        )
        hostile = _wrap_hostile(duplicate_rows)
        with pytest.raises(
            PortfolioProjectFocusReadyTaskCandidatesError,
            match="strict re-validation",
        ):
            project_ready_task_candidates_from_current_constraint_evaluation(  # type: ignore[arg-type]
                hostile
            )


def _hostile_project(
    tasks: tuple[object, ...],
) -> FocusedProjectTaskCurrentConstraintEvaluations:
    """A validation-bypassed project row so the V1.40 fresh strict
    re-validation is the only thing that can reject its contents. """
    return (
        FocusedProjectTaskCurrentConstraintEvaluations.model_construct(
            project_id=P_ALPHA,
            tasks=tasks,  # type: ignore[arg-type]
        )
    )


def _wrap_hostile(
    projects: tuple[object, ...],
) -> PortfolioProjectFocusTaskCurrentConstraintEvaluation:
    """Wrap hostile (validator-bypassed) project rows into a genuine V1.39
    instance WITHOUT running V1.39 validation, so that the V1.40 fresh
    strict re-validation is the only thing that can reject them."""
    return (
        PortfolioProjectFocusTaskCurrentConstraintEvaluation.model_construct(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            portfolio_id=PORTFOLIO_ID,
            selected_project_count=len(projects),
            projects=projects,  # type: ignore[arg-type]
        )
    )


# ---------------------------------------------------------------------------
# Behavior.
# ---------------------------------------------------------------------------


class TestBehavior:
    def test_repeated_identical_calls_value_identical(self) -> None:
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    (
                        _task(T_AAA, FocusedTaskConstraintReadinessState.READY),
                        _task(
                            T_BBB,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(_constraint(R1, C1, EntityStatus.ACTIVE),),
                        ),
                    ),
                ),
                _project(
                    P_BETA,
                    (
                        _task(T_CCC, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
            ),
        )
        first = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        second = (
            project_ready_task_candidates_from_current_constraint_evaluation(
                evaluation
            )
        )
        assert first == second
        assert first.model_dump(mode="json") == second.model_dump(mode="json")

    def test_input_not_mutated(self) -> None:
        evaluation = _evaluation(
            (
                _project(
                    P_ALPHA,
                    (
                        _task(T_AAA, FocusedTaskConstraintReadinessState.READY),
                        _task(
                            T_BBB,
                            FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                            status=EntityStatus.WAITING,
                        ),
                    ),
                ),
            )
        )
        before = evaluation.model_dump(mode="json")
        project_ready_task_candidates_from_current_constraint_evaluation(evaluation)
        assert evaluation.model_dump(mode="json") == before


# ---------------------------------------------------------------------------
# Public surface and architecture discipline.
# ---------------------------------------------------------------------------


class TestDiscipline:
    def test_signature_takes_exactly_one_non_portfolio_argument(self) -> None:
        signature = inspect.signature(
            project_ready_task_candidates_from_current_constraint_evaluation
        )
        parameters = list(signature.parameters)
        assert parameters == ["evaluation"]
        assert not any("portfolio" in name.lower() for name in parameters)

    def test_no_work_breakdown_construction_for_semantics(self) -> None:
        module_vars = vars(v140_mod)
        assert "build_work_breakdown" not in module_vars
        assert "WorkBreakdownError" not in module_vars
        assert not any(
            "workbreakdown" in name.lower() or "build_work" in name.lower()
            for name in module_vars
            if not name.startswith("__")
        )

    def test_no_repository_persistence_or_provider_surface(self) -> None:
        public_names = [
            name for name in vars(v140_mod) if not name.startswith("_")
        ]
        assert not any(
            "repository" in name.lower()
            or "persist" in name.lower()
            or "provider" in name.lower()
            or "runtime" in name.lower()
            for name in public_names
        )

        source = Path(v140_mod.__file__).read_text()
        assert "datetime.now" not in source
        assert "datetime.utcnow" not in source
        assert "uuid4" not in source
        assert "uuid5" not in source
        assert "random()" not in source
        assert "import os" not in source
        assert "requests" not in source

    def test_no_ranking_scoring_or_selection_policy(self) -> None:
        public_names = [
            name for name in vars(v140_mod) if not name.startswith("_")
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
                "nexttask",
            ):
                assert forbidden not in lowered

    def test_public_exports_correct(self) -> None:
        required = [
            "PortfolioProjectFocusReadyTaskCandidates",
            "PortfolioProjectFocusReadyTaskCandidatesError",
            "ReadyProjectTaskCandidates",
            "ReadyTaskCandidate",
            "project_ready_task_candidates_from_current_constraint_evaluation",
        ]
        for name in required:
            assert name in app.__all__
            assert getattr(app, name) is not None

        module_all = set(v140_mod.__all__)
        for name in required:
            assert name in module_all

    def test_error_is_a_value_error(self) -> None:
        assert issubclass(PortfolioProjectFocusReadyTaskCandidatesError, ValueError)
