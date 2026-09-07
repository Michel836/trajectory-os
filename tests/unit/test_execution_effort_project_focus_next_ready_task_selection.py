"""Unit tests for V1.41 — deterministic first READY TASK selection.

Covers:

  - genuine / hostile input discipline (genuine V1.40 accepted; None,
    dict, string, foreign model, hostile ``model_construct`` scalars at
    top-level and nested levels — including a hostile
    ``ready_task_count`` — rejected; and proof that no semantic reads
    are served from the caller-owned V1.40 object after fresh
    re-validation);
  - the EXACT first-candidate-in-canonical-V1.40-order policy (zero
    READY -> no selection; first project's first task; first only;
    first project empty / later non-empty; globally first candidate in
    project-major / task-major order; no UUID sorting; exactly one
    READY candidate; no secondary policy anywhere);
  - exact provenance and count preservation (decision_id, decided_at,
    portfolio_id, selected_project_count, ready_task_count — selection
    does not alter any of them);
  - model invariants (strict / frozen / extra-forbid; selected IDs
    both-None-or-both-set; zero ready count requires no selection;
    positive ready count requires a selection; invalid direct
    construction rejected);
  - behavior (repeated identical calls value-identical; input not
    mutated);
  - public surface and architecture discipline (exact exports, no
    Portfolio argument, no WBS, no repository / persistence / provider /
    runtime / clock / UUID-generation surface, no ranking / scoring /
    priority / recommendation semantics).
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
import trajectory_os.application.execution_effort_project_focus_next_ready_task_selection as v141_mod  # noqa: E501
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskSelection,
    PortfolioProjectFocusNextReadyTaskSelectionError,
    PortfolioProjectFocusReadyTaskCandidates,
    ReadyProjectTaskCandidates,
    ReadyTaskCandidate,
    project_ready_task_candidates_from_current_constraint_evaluation,
    select_first_ready_task_candidate,
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

PORTFOLIO_ID = uuid.UUID("81616161-6161-4161-8161-616161616161")
DECISION_ID = uuid.UUID("82626262-6262-4262-8262-826262626262")
DECIDED_AT = datetime(2025, 7, 3, 10, 0, 30, tzinfo=UTC)

# Project identities deliberately INVERTED against natural UUID order so
# any project-UUID-sorted output differs from canonical tuple order.
P_HIGH = uuid.UUID("c0000000-0000-4000-8000-00000000000c")
P_MID = uuid.UUID("b0000000-0000-4000-8000-00000000000b")
P_LOW = uuid.UUID("a0000000-0000-4000-8000-00000000000a")

# Task identities deliberately INVERTED against natural UUID order so any
# task-UUID-sorted selection differs from canonical tuple order.
T_HIGH = uuid.UUID("f4f4f4f4-0000-4000-8000-000000000044")
T_MID = uuid.UUID("d1d1d1d1-0000-4000-8000-000000000011")
T_LOW = uuid.UUID("c2c2c2c2-0000-4000-8000-000000000022")
T_EEE = uuid.UUID("e3e3e3e3-0000-4000-8000-000000000033")

R1 = uuid.UUID("f0000000-0000-4000-8000-000000000091")
C1 = uuid.UUID("c1c1c1c1-0000-4000-8000-000000000001")


# ---------------------------------------------------------------------------
# Fixture helpers — genuine V1.40 candidates via the V1.40 boundary.
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


def _v139_project(
    project_id: uuid.UUID,
    tasks: tuple[FocusedTaskCurrentConstraintEvaluation, ...] = (),
) -> FocusedProjectTaskCurrentConstraintEvaluations:
    return FocusedProjectTaskCurrentConstraintEvaluations(
        project_id=project_id,
        tasks=tasks,
    )


def _v140(
    projects: tuple[FocusedProjectTaskCurrentConstraintEvaluations, ...],
) -> PortfolioProjectFocusReadyTaskCandidates:
    """Project ONE genuine V1.39 evaluation into a genuine V1.40."""
    evaluation = (
        PortfolioProjectFocusTaskCurrentConstraintEvaluation(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            portfolio_id=PORTFOLIO_ID,
            selected_project_count=len(projects),
            projects=projects,
        )
    )
    return project_ready_task_candidates_from_current_constraint_evaluation(
        evaluation
    )


def _candidate(task_id: uuid.UUID) -> ReadyTaskCandidate:
    return ReadyTaskCandidate(task_id=task_id)


class _ForeignModel(BaseModel):
    """A different model; must never be accepted as a V1.40 payload."""

    model_config = {"frozen": True}

    field: str = "foreign"


def _hostile_v140_row(
    project_id: uuid.UUID,
    tasks: tuple[object, ...],
) -> ReadyProjectTaskCandidates:
    """A validation-bypassed V1.40 project row; only the V1.41 fresh
    strict re-validation can reject its contents."""
    return (
        ReadyProjectTaskCandidates.model_construct(
            project_id=project_id,
            tasks=tasks,  # type: ignore[arg-type]
        )
    )


def _wrap_hostile_v140(
    rows: tuple[object, ...],
    **overrides: object,
) -> PortfolioProjectFocusReadyTaskCandidates:
    """Wrap hostile (validator-bypassed) V1.40 rows into a genuine V1.40
    instance WITHOUT running V1.40 validation, so that the V1.41 fresh
    strict re-validation is the only thing that can reject them."""
    base: dict[str, object] = {
        "decision_id": DECISION_ID,
        "decided_at": DECIDED_AT,
        "portfolio_id": PORTFOLIO_ID,
        "selected_project_count": len(rows),
        "ready_task_count": 1,
        "projects": rows,
    }
    base.update(overrides)
    return (
        PortfolioProjectFocusReadyTaskCandidates.model_construct(
            **base  # type: ignore[arg-type]
        )
    )


# ---------------------------------------------------------------------------
# Output model invariants.
# ---------------------------------------------------------------------------


class TestSelectionModel:
    def _base_kwargs(self) -> dict[str, object]:
        return {
            "decision_id": DECISION_ID,
            "decided_at": DECIDED_AT,
            "portfolio_id": PORTFOLIO_ID,
            "selected_project_count": 1,
            "ready_task_count": 1,
            "selected_project_id": P_HIGH,
            "selected_task_id": T_HIGH,
        }

    def test_exactly_seven_fields(self) -> None:
        expected = [
            "decision_id",
            "decided_at",
            "portfolio_id",
            "selected_project_count",
            "ready_task_count",
            "selected_project_id",
            "selected_task_id",
        ]
        assert list(PortfolioProjectFocusNextReadyTaskSelection.model_fields) == (  # noqa: E501
            expected
        )

    def test_strict_frozen_extra_forbid(self) -> None:
        selection = PortfolioProjectFocusNextReadyTaskSelection(
            **self._base_kwargs()
        )

        with pytest.raises(ValidationError, match="decision_id"):
            PortfolioProjectFocusNextReadyTaskSelection(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "decision_id": str(DECISION_ID),
                }
            )

        with pytest.raises(ValidationError, match="Extra inputs"):
            PortfolioProjectFocusNextReadyTaskSelection(
                **{  # type: ignore[call-arg]
                    **self._base_kwargs(),
                    "reached_at": DECIDED_AT,
                }
            )

        with pytest.raises((ValidationError, AttributeError, TypeError)):
            selection.decision_id = DECISION_ID  # type: ignore[misc]

    def test_selected_ids_both_none_or_both_set(self) -> None:
        with pytest.raises(
            ValidationError,
            match="both be None or both be non-None",
        ):
            PortfolioProjectFocusNextReadyTaskSelection(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "selected_task_id": None,
                }
            )
        with pytest.raises(
            ValidationError,
            match="both be None or both be non-None",
        ):
            PortfolioProjectFocusNextReadyTaskSelection(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "selected_project_id": None,
                }
            )

    def test_zero_ready_count_requires_no_selection(self) -> None:
        with pytest.raises(
            ValidationError, match="ready_task_count == 0 requires no selection"  # noqa: E501
        ):
            PortfolioProjectFocusNextReadyTaskSelection(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "ready_task_count": 0,
                }
            )

    def test_positive_ready_count_requires_selection(self) -> None:
        with pytest.raises(
            ValidationError, match="ready_task_count > 0 requires a selection"  # noqa: E501
        ):
            PortfolioProjectFocusNextReadyTaskSelection(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "selected_project_id": None,
                    "selected_task_id": None,
                }
            )

    def test_zero_ready_and_no_selection_is_valid(self) -> None:
        selection = PortfolioProjectFocusNextReadyTaskSelection(
            **{  # type: ignore[arg-type]
                **self._base_kwargs(),
                "ready_task_count": 0,
                "selected_project_count": 0,
                "selected_project_id": None,
                "selected_task_id": None,
            }
        )
        assert selection.selected_project_id is None
        assert selection.selected_task_id is None

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskSelection(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "selected_project_count": -1,
                }
            )
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskSelection(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "ready_task_count": -1,
                }
            )

    def test_non_integer_counts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskSelection(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "selected_project_count": "one",
                }
            )
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskSelection(
                **{  # type: ignore[arg-type]
                    **self._base_kwargs(),
                    "ready_task_count": 1.0,
                }
            )


# ---------------------------------------------------------------------------
# Exact first-candidate policy.
# ---------------------------------------------------------------------------


class TestFirstCandidatePolicy:
    def test_zero_ready_no_selection(self) -> None:
        candidates = _v140(
            (
                _v139_project(
                    P_HIGH,
                    (
                        _task(
                            T_HIGH,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(
                                _constraint(R1, C1, EntityStatus.ACTIVE),
                            ),
                        ),
                    ),
                ),
            )
        )
        assert candidates.ready_task_count == 0
        result = select_first_ready_task_candidate(candidates)

        assert result.selected_project_id is None
        assert result.selected_task_id is None

    def test_zero_selected_projects_no_selection(self) -> None:
        candidates = _v140(())
        assert candidates.selected_project_count == 0
        result = select_first_ready_task_candidate(candidates)

        assert result.selected_project_id is None
        assert result.selected_task_id is None

    def test_first_project_one_ready_selected(self) -> None:
        candidates = _v140(
            (
                _v139_project(
                    P_HIGH,
                    (_task(T_HIGH, FocusedTaskConstraintReadinessState.READY),),
                ),
                _v139_project(
                    P_LOW,
                    (_task(T_LOW, FocusedTaskConstraintReadinessState.READY),),
                ),
            )
        )
        result = select_first_ready_task_candidate(candidates)
        assert result.selected_project_id == P_HIGH
        assert result.selected_task_id == T_HIGH

    def test_first_project_multiple_ready_selects_first_only(self) -> None:
        # Tasks are deliberately in DESCENDING UUID order; a UUID-sorted
        # policy would have selected T_LOW instead of T_HIGH.
        candidates = _v140(
            (
                _v139_project(
                    P_HIGH,
                    (
                        _task(T_HIGH, FocusedTaskConstraintReadinessState.READY),
                        _task(T_MID, FocusedTaskConstraintReadinessState.READY),
                        _task(T_LOW, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
            )
        )
        assert candidates.ready_task_count == 3
        result = select_first_ready_task_candidate(candidates)
        assert result.selected_project_id == P_HIGH
        assert result.selected_task_id == T_HIGH

    def test_first_project_empty_second_non_empty(self) -> None:
        candidates = _v140(
            (
                _v139_project(
                    P_HIGH,
                    (
                        _task(
                            T_HIGH,
                            FocusedTaskConstraintReadinessState.INELIGIBLE_STATUS,
                            status=EntityStatus.CANCELLED,
                        ),
                    ),
                ),
                _v139_project(
                    P_MID,
                    (
                        _task(T_MID, FocusedTaskConstraintReadinessState.READY),
                        _task(T_LOW, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
            )
        )
        result = select_first_ready_task_candidate(candidates)
        assert result.selected_project_id == P_MID
        assert result.selected_task_id == T_MID

    def test_globally_first_candidate_project_major_task_major(self) -> None:
        # Projects are in descending UUID order; the FIRST tuple element
        # (P_HIGH / T_HIGH) is the highest UUID overall. A best-by-UUID,
        # oldest-first, or any other secondary policy produces a
        # different answer on one of the other projects.
        candidates = _v140(
            (
                _v139_project(
                    P_HIGH,
                    (_task(T_HIGH, FocusedTaskConstraintReadinessState.READY),),
                ),
                _v139_project(
                    P_MID,
                    (
                        _task(T_MID, FocusedTaskConstraintReadinessState.READY),
                        _task(T_EEE, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
                _v139_project(
                    P_LOW,
                    (_task(T_LOW, FocusedTaskConstraintReadinessState.READY),),
                ),
            )
        )
        result = select_first_ready_task_candidate(candidates)
        assert result.selected_project_id == P_HIGH
        assert result.selected_task_id == T_HIGH

    def test_no_uuid_sorting_selects_descending_order_first(self) -> None:
        # Single project, tasks in descending UUID order: the selected
        # task is the LARGEST UUID — the opposite of any ascending
        # UUID-sorted policy.
        candidates = _v140(
            (
                _v139_project(
                    P_LOW,
                    (
                        _task(T_HIGH, FocusedTaskConstraintReadinessState.READY),
                        _task(T_LOW, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
            )
        )
        result = select_first_ready_task_candidate(candidates)
        assert result.selected_project_id == P_LOW
        assert result.selected_task_id == T_HIGH

    def test_exactly_one_ready_candidate_selected(self) -> None:
        candidates = _v140(
            (
                _v139_project(P_MID, ()),
                _v139_project(
                    P_LOW,
                    (_task(T_MID, FocusedTaskConstraintReadinessState.READY),),
                ),
            )
        )
        assert candidates.ready_task_count == 1
        result = select_first_ready_task_candidate(candidates)
        assert result.selected_project_id == P_LOW
        assert result.selected_task_id == T_MID

    def test_stop_immediately_after_first_candidate(self) -> None:
        # The later projects carry READY candidates too; the selection
        # reflects ONLY the globally first one.
        candidates = _v140(
            (
                _v139_project(
                    P_HIGH,
                    (_task(T_HIGH, FocusedTaskConstraintReadinessState.READY),),
                ),
                _v139_project(
                    P_LOW,
                    (
                        _task(T_LOW, FocusedTaskConstraintReadinessState.READY),
                        _task(T_EEE, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
            )
        )
        result = select_first_ready_task_candidate(candidates)
        assert result.selected_project_id == P_HIGH
        assert result.selected_task_id == T_HIGH
        # No trace whatsoever of the later candidates in the output.
        assert result.selected_task_id != T_LOW
        assert result.selected_task_id != T_EEE
        assert result.selected_project_id != P_LOW


# ---------------------------------------------------------------------------
# Provenance / count preservation.
# ---------------------------------------------------------------------------


class TestProvenanceAndCounts:
    def _candidates(self) -> PortfolioProjectFocusReadyTaskCandidates:
        return _v140(
            (
                _v139_project(
                    P_HIGH,
                    (
                        _task(T_HIGH, FocusedTaskConstraintReadinessState.READY),
                        _task(T_MID, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
                _v139_project(
                    P_LOW,
                    (
                        _task(T_LOW, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
            )
        )

    def test_provenance_and_counts_preserved_exactly(self) -> None:
        candidates = self._candidates()
        result = select_first_ready_task_candidate(candidates)

        assert result.decision_id == DECISION_ID
        assert result.decided_at == DECIDED_AT
        assert result.portfolio_id == PORTFOLIO_ID
        assert result.selected_project_count == 2
        assert result.ready_task_count == candidates.ready_task_count == 3
        # and the selection itself is orthogonal to the counts
        assert result.selected_project_id == P_HIGH
        assert result.selected_task_id == T_HIGH

    def test_selection_does_not_alter_counts(self) -> None:
        candidates = self._candidates()
        result = select_first_ready_task_candidate(candidates)

        assert tuple(result.model_fields) == tuple(
            PortfolioProjectFocusNextReadyTaskSelection.model_fields
        )
        assert (
            result.selected_project_count,
            result.ready_task_count,
        ) == (
            candidates.selected_project_count,
            candidates.ready_task_count,
        )

    def test_zero_state_counts_preserved(self) -> None:
        candidates = _v140(
            (
                _v139_project(
                    P_HIGH,
                    (
                        _task(
                            T_HIGH,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(
                                _constraint(R1, C1, EntityStatus.ACTIVE),
                            ),
                        ),
                    ),
                ),
                _v139_project(P_LOW, ()),
            )
        )
        result = select_first_ready_task_candidate(candidates)
        assert result.selected_project_count == 2
        assert result.ready_task_count == 0
        assert result.selected_project_id is None
        assert result.selected_task_id is None

    def test_output_carries_only_identities(self) -> None:
        result = select_first_ready_task_candidate(self._candidates())
        fields = set(result.model_dump().keys())
        assert not fields & {
            "score",
            "rank",
            "priority",
            "effort",
            "confidence",
            "recommendation",
            "title",
            "description",
        }


# ---------------------------------------------------------------------------
# Genuine / hostile input discipline.
# ---------------------------------------------------------------------------


class TestBoundaryInputs:
    def test_genuine_v140_accepted(self) -> None:
        candidates = _v140(
            (
                _v139_project(
                    P_HIGH,
                    (_task(T_HIGH, FocusedTaskConstraintReadinessState.READY),),
                ),
            )
        )
        result = select_first_ready_task_candidate(candidates)
        assert result.selected_project_id == P_HIGH
        assert result.selected_task_id == T_HIGH

    def test_none_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskSelectionError, match="genuine V1.40"
        ):
            select_first_ready_task_candidate(None)  # type: ignore[arg-type]

    def test_dict_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskSelectionError, match="genuine V1.40"
        ):
            select_first_ready_task_candidate(
                {"decision_id": str(DECISION_ID)}
            )  # type: ignore[arg-type]

    def test_string_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskSelectionError, match="genuine V1.40"
        ):
            select_first_ready_task_candidate("an evaluation")  # type: ignore[arg-type]

    def test_foreign_model_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskSelectionError, match="genuine V1.40"
        ):
            select_first_ready_task_candidate(
                _ForeignModel()
            )  # type: ignore[arg-type]

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
                PortfolioProjectFocusNextReadyTaskSelectionError,
                match="genuine V1.40",
            ):
                select_first_ready_task_candidate(item)  # type: ignore[arg-type]

    def test_hostile_top_level_scalar_rejected(self) -> None:
        hostile = _wrap_hostile_v140(
            (_v140_project_row(P_HIGH, (_candidate(T_HIGH),)),),
            selected_project_count="one",  # hostile top-level scalar
        )
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskSelectionError,
            match="strict re-validation",
        ):
            select_first_ready_task_candidate(hostile)

    def test_hostile_nested_project_row_rejected(self) -> None:
        bad_row = _hostile_v140_row("alpha", ())  # hostile nested scalar
        hostile = _wrap_hostile_v140((bad_row,))
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskSelectionError,
            match="strict re-validation",
        ):
            select_first_ready_task_candidate(hostile)

    def test_hostile_nested_task_candidate_rejected(self) -> None:
        bad_task = ReadyTaskCandidate.model_construct(task_id="zzz")
        hostile = _wrap_hostile_v140((_hostile_v140_row(P_HIGH, (bad_task,)),))
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskSelectionError,
            match="strict re-validation",
        ):
            select_first_ready_task_candidate(hostile)

    def test_hostile_ready_task_count_rejected(self) -> None:
        hostile = _wrap_hostile_v140(
            (_v140_project_row(P_HIGH, (_candidate(T_HIGH),)),),
            ready_task_count="three",  # hostile raw count
        )
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskSelectionError,
            match="strict re-validation",
        ):
            select_first_ready_task_candidate(hostile)

    def test_hostile_nested_readiness_semantics_cannot_be_read(self) -> None:
        # V1.40 itself carries no readiness/status semantics; a raw row
        # that would be semantically usable for a shortcut selection but
        # FAILS the V1.40 invariants (duplicate project IDs — the first
        # row is trivially select-able from the raw payload alone) must
        # still be rejected by fresh re-validation.
        bad_row = _v140_project_row(P_HIGH, (_candidate(T_HIGH),))
        hostile = _wrap_hostile_v140((bad_row, bad_row))
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskSelectionError,
            match="strict re-validation",
        ):
            select_first_ready_task_candidate(hostile)

    def test_caller_owned_payload_is_not_semantic_source(self) -> None:
        # A raw payload whose first tuple element is trivially "first",
        # but whose V1.40 invariants fail (duplicate task IDs across
        # rows) is rejected: the boundary serves no semantic read from
        # the caller-owned instance — only the freshly validated copy
        # would be authoritative, and it does not exist here.
        bad_second = _hostile_v140_row(
            P_LOW,
            (_candidate(T_HIGH),),  # duplicate task ID across rows
        )
        first = _v140_project_row(P_HIGH, (_candidate(T_HIGH),))
        hostile = _wrap_hostile_v140((first, bad_second), ready_task_count=2)
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskSelectionError,
            match="strict re-validation",
        ):
            select_first_ready_task_candidate(hostile)


def _v140_project_row(
    project_id: uuid.UUID,
    tasks: tuple[ReadyTaskCandidate, ...],
) -> ReadyProjectTaskCandidates:
    """One genuine V1.40 project row."""
    return ReadyProjectTaskCandidates(project_id=project_id, tasks=tasks)


# ---------------------------------------------------------------------------
# Behavior.
# ---------------------------------------------------------------------------


class TestBehavior:
    def test_repeated_identical_calls_value_identical(self) -> None:
        candidates = _v140(
            (
                _v139_project(
                    P_HIGH,
                    (
                        _task(T_HIGH, FocusedTaskConstraintReadinessState.READY),
                        _task(T_MID, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
                _v139_project(
                    P_LOW,
                    (_task(
                        T_LOW,
                        FocusedTaskConstraintReadinessState.CONSTRAINED,
                        constraints=(
                            _constraint(R1, C1, EntityStatus.ACTIVE),
                        ),
                    ),),
                ),
            )
        )
        first = select_first_ready_task_candidate(candidates)
        second = select_first_ready_task_candidate(candidates)
        assert first == second
        assert first.model_dump(mode="json") == second.model_dump(mode="json")

    def test_input_not_mutated(self) -> None:
        candidates = _v140(
            (
                _v139_project(
                    P_HIGH,
                    (
                        _task(T_HIGH, FocusedTaskConstraintReadinessState.READY),
                        _task(T_MID, FocusedTaskConstraintReadinessState.READY),
                    ),
                ),
                _v139_project(
                    P_LOW,
                    (
                        _task(
                            T_LOW,
                            FocusedTaskConstraintReadinessState.CONSTRAINED,
                            constraints=(
                                _constraint(R1, C1, EntityStatus.ACTIVE),
                            ),
                        ),
                    ),
                ),
            )
        )
        before = candidates.model_dump(mode="json")
        select_first_ready_task_candidate(candidates)
        assert candidates.model_dump(mode="json") == before

    def test_no_hidden_secondary_selection_logic(self) -> None:
        source = Path(v141_mod.__file__).read_text()
        lowered = source.lower()
        for marker in (
            "sorted(",
            "sort(",
            ".sort",
            "heapq",
            "max(",
            "min(",
            "argmax",
            "argmin",
        ):
            assert marker not in lowered


# ---------------------------------------------------------------------------
# Public surface and architecture discipline.
# ---------------------------------------------------------------------------


class TestDiscipline:
    def test_signature_takes_exactly_one_non_portfolio_argument(self) -> None:
        signature = inspect.signature(select_first_ready_task_candidate)
        parameters = list(signature.parameters)
        assert parameters == ["candidates"]
        assert not any("portfolio" in name.lower() for name in parameters)

    def test_no_work_breakdown_construction_for_semantics(self) -> None:
        module_vars = vars(v141_mod)
        assert "build_work_breakdown" not in module_vars
        assert "WorkBreakdownError" not in module_vars
        assert not any(
            "workbreakdown" in name.lower() or "build_work" in name.lower()
            for name in module_vars
            if not name.startswith("__")
        )

    def test_no_repository_persistence_or_provider_surface(self) -> None:
        public_names = [name for name in vars(v141_mod) if not name.startswith("_")]
        assert not any(
            "repository" in name.lower()
            or "persist" in name.lower()
            or "provider" in name.lower()
            or "runtime" in name.lower()
            for name in public_names
        )

        source = Path(v141_mod.__file__).read_text()
        assert "datetime.now" not in source
        assert "datetime.utcnow" not in source
        assert "uuid4" not in source
        assert "uuid5" not in source
        assert "random()" not in source
        assert "import os" not in source
        assert "requests" not in source

    def test_no_ranking_scoring_priority_or_recommendation_semantics(self) -> None:
        public_names = [
            name for name in vars(v141_mod) if not name.startswith("_")
        ]
        for name in public_names:
            lowered = name.lower()
            for forbidden in (
                "rank",
                "score",
                "priorit",
                "recommend",
                "urgent",
                "best",
                "nexttask",
            ):
                assert forbidden not in lowered

    def test_public_exports_correct(self) -> None:
        required = [
            "PortfolioProjectFocusNextReadyTaskSelection",
            "PortfolioProjectFocusNextReadyTaskSelectionError",
            "select_first_ready_task_candidate",
        ]
        for name in required:
            assert name in app.__all__
            assert getattr(app, name) is not None

        module_all = set(v141_mod.__all__)
        for name in required:
            assert name in module_all

    def test_error_is_a_value_error(self) -> None:
        assert issubclass(
            PortfolioProjectFocusNextReadyTaskSelectionError, ValueError
        )
