"""Unit tests for the pure V1.11 planned-vs-actual effort comparison."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import SourceKind
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_comparison import (
    EffortVariance,
    ExecutionEffortComparisonError,
    WorkBreakdownEffortComparison,
    compare_work_breakdown_effort,
)
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
)
from trajectory_os.domain.execution_effort_measurement import (
    ExecutionEffortSummary,
    WorkBreakdownEffortMeasurement,
    WorkBreakdownEffortMeasurementItem,
)
from trajectory_os.domain.execution_effort_planning import (
    PlannedEffortSummary,
    WorkBreakdownEffortPlan,
    WorkBreakdownEffortPlanItem,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PORTFOLIO_ID = UUID("11111111-1111-1111-1111-111111111111")
PROJECT_ID = UUID("22222222-2222-2222-2222-222222222222")
TASK_A_ID = UUID("33333333-3333-3333-3333-333333333333")
TASK_B_ID = UUID("44444444-4444-4444-4444-444444444444")

T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)


def _make_estimate(
    entity_id: UUID,
    duration: int,
    estimated_at: datetime = T0,
) -> ExecutionEffortEstimate:
    return ExecutionEffortEstimate(
        id=uuid4(),
        portfolio_id=PORTFOLIO_ID,
        entity_id=entity_id,
        duration_seconds=duration,
        estimated_at=estimated_at,
        source=SourceKind.USER_CONFIRMED,
    )


def _make_observation(
    entity_id: UUID,
    duration: int,
    observed_at: datetime = T1,
) -> ExecutionEffortObservation:
    return ExecutionEffortObservation(
        id=uuid4(),
        portfolio_id=PORTFOLIO_ID,
        entity_id=entity_id,
        duration_seconds=duration,
        observed_at=observed_at,
        source=SourceKind.USER_CONFIRMED,
    )


def _make_plan(
    project_id: UUID = PROJECT_ID,
    portfolio_id: UUID = PORTFOLIO_ID,
    task_a_estimate: ExecutionEffortEstimate | None = None,
    task_b_estimate: ExecutionEffortEstimate | None = None,
) -> WorkBreakdownEffortPlan:
    """Build a valid 3-node plan: project → task_a, task_b."""
    a_known = task_a_estimate.duration_seconds if task_a_estimate else 0
    b_known = task_b_estimate.duration_seconds if task_b_estimate else 0
    a_est = 1 if task_a_estimate else 0
    b_est = 1 if task_b_estimate else 0
    a_unest = 0 if task_a_estimate else 1
    b_unest = 0 if task_b_estimate else 1

    root_known = a_known + b_known
    root_est = a_est + b_est
    root_unest = a_unest + b_unest

    return WorkBreakdownEffortPlan(
        portfolio_id=portfolio_id,
        project_id=project_id,
        items=(
            WorkBreakdownEffortPlanItem(
                entity_id=project_id,
                parent_id=None,
                depth=0,
                direct_estimate=None,
                subtree=PlannedEffortSummary(
                    known_duration_seconds=root_known,
                    estimated_entity_count=root_est,
                    unestimated_entity_count=root_unest,
                    total_duration_seconds=(
                        root_known if root_unest == 0 else None
                    ),
                ),
            ),
            WorkBreakdownEffortPlanItem(
                entity_id=TASK_A_ID,
                parent_id=project_id,
                depth=1,
                direct_estimate=task_a_estimate,
                subtree=PlannedEffortSummary(
                    known_duration_seconds=a_known,
                    estimated_entity_count=a_est,
                    unestimated_entity_count=a_unest,
                    total_duration_seconds=(
                        a_known if a_unest == 0 else None
                    ),
                ),
            ),
            WorkBreakdownEffortPlanItem(
                entity_id=TASK_B_ID,
                parent_id=project_id,
                depth=1,
                direct_estimate=task_b_estimate,
                subtree=PlannedEffortSummary(
                    known_duration_seconds=b_known,
                    estimated_entity_count=b_est,
                    unestimated_entity_count=b_unest,
                    total_duration_seconds=(
                        b_known if b_unest == 0 else None
                    ),
                ),
            ),
        ),
    )


def _make_measurement(
    project_id: UUID = PROJECT_ID,
    portfolio_id: UUID = PORTFOLIO_ID,
    task_a_actual: int = 0,
    task_b_actual: int = 0,
) -> WorkBreakdownEffortMeasurement:
    """Build a valid 3-node measurement: project → task_a, task_b."""
    a_summary = (
        ExecutionEffortSummary(
            duration_seconds=task_a_actual,
            observation_count=1,
            first_observed_at=T1,
            last_observed_at=T1,
        )
        if task_a_actual > 0
        else ExecutionEffortSummary(duration_seconds=0, observation_count=0)
    )
    b_summary = (
        ExecutionEffortSummary(
            duration_seconds=task_b_actual,
            observation_count=1,
            first_observed_at=T1,
            last_observed_at=T1,
        )
        if task_b_actual > 0
        else ExecutionEffortSummary(duration_seconds=0, observation_count=0)
    )
    total_actual = task_a_actual + task_b_actual
    total_count = (1 if task_a_actual > 0 else 0) + (1 if task_b_actual > 0 else 0)

    root_summary = (
        ExecutionEffortSummary(
            duration_seconds=total_actual,
            observation_count=total_count,
            first_observed_at=T1,
            last_observed_at=T1,
        )
        if total_count > 0
        else ExecutionEffortSummary(duration_seconds=0, observation_count=0)
    )

    return WorkBreakdownEffortMeasurement(
        portfolio_id=portfolio_id,
        project_id=project_id,
        items=(
            WorkBreakdownEffortMeasurementItem(
                entity_id=project_id,
                parent_id=None,
                depth=0,
                direct=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                subtree=root_summary,
            ),
            WorkBreakdownEffortMeasurementItem(
                entity_id=TASK_A_ID,
                parent_id=project_id,
                depth=1,
                direct=a_summary,
                subtree=a_summary,
            ),
            WorkBreakdownEffortMeasurementItem(
                entity_id=TASK_B_ID,
                parent_id=project_id,
                depth=1,
                direct=b_summary,
                subtree=b_summary,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Model invariants
# ---------------------------------------------------------------------------


class TestEffortVarianceModel:
    def test_frozen(self) -> None:
        v = EffortVariance(
            planned_duration_seconds=100,
            actual_duration_seconds=120,
            variance_seconds=20,
        )
        with pytest.raises(ValidationError):
            v.planned_duration_seconds = 999  # type: ignore[misc]

    def test_strict_rejects_bool(self) -> None:
        with pytest.raises(ValidationError):
            EffortVariance(
                planned_duration_seconds=True,  # type: ignore[arg-type]
                actual_duration_seconds=10,
                variance_seconds=10,
            )

    def test_none_planned_requires_none_variance(self) -> None:
        with pytest.raises(ValidationError):
            EffortVariance(
                planned_duration_seconds=None,
                actual_duration_seconds=10,
                variance_seconds=10,
            )

    def test_variance_must_equal_actual_minus_planned(self) -> None:
        with pytest.raises(ValidationError):
            EffortVariance(
                planned_duration_seconds=100,
                actual_duration_seconds=120,
                variance_seconds=99,
            )

    def test_negative_variance_allowed(self) -> None:
        v = EffortVariance(
            planned_duration_seconds=120,
            actual_duration_seconds=100,
            variance_seconds=-20,
        )
        assert v.variance_seconds == -20


class TestWorkBreakdownEffortComparisonModel:
    def test_frozen(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 100))
        meas = _make_measurement(task_a_actual=120)
        result = compare_work_breakdown_effort(plan, meas)
        with pytest.raises(ValidationError):
            result.portfolio_id = uuid4()  # type: ignore[misc]

    def test_empty_items_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkBreakdownEffortComparison(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                items=(),
            )


# ---------------------------------------------------------------------------
# Structural alignment
# ---------------------------------------------------------------------------


class TestStructuralAlignment:
    def test_matching_structure_succeeds(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 100))
        meas = _make_measurement(task_a_actual=120)
        result = compare_work_breakdown_effort(plan, meas)
        assert len(result.items) == 3

    def test_portfolio_mismatch_rejected(self) -> None:
        other_pf = uuid4()
        plan = _make_plan(portfolio_id=PORTFOLIO_ID)
        meas = _make_measurement(portfolio_id=other_pf)
        with pytest.raises(ExecutionEffortComparisonError, match="portfolio_id"):
            compare_work_breakdown_effort(plan, meas)

    def test_project_mismatch_rejected(self) -> None:
        other_proj = uuid4()
        plan = _make_plan(project_id=PROJECT_ID)
        meas = _make_measurement(project_id=other_proj)
        with pytest.raises(ExecutionEffortComparisonError, match="project_id"):
            compare_work_breakdown_effort(plan, meas)

    def test_item_count_mismatch_rejected(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 100))
        # Build a measurement with only 2 items (missing task_b)
        meas = WorkBreakdownEffortMeasurement(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            items=(
                WorkBreakdownEffortMeasurementItem(
                    entity_id=PROJECT_ID,
                    parent_id=None,
                    depth=0,
                    direct=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                    subtree=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                ),
                WorkBreakdownEffortMeasurementItem(
                    entity_id=TASK_A_ID,
                    parent_id=PROJECT_ID,
                    depth=1,
                    direct=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                    subtree=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                ),
            ),
        )
        with pytest.raises(ExecutionEffortComparisonError, match="item count"):
            compare_work_breakdown_effort(plan, meas)

    def test_entity_id_mismatch_rejected(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 100))
        # Swap task_a and task_b in measurement
        meas = WorkBreakdownEffortMeasurement(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            items=(
                WorkBreakdownEffortMeasurementItem(
                    entity_id=PROJECT_ID,
                    parent_id=None,
                    depth=0,
                    direct=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                    subtree=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                ),
                WorkBreakdownEffortMeasurementItem(
                    entity_id=TASK_B_ID,  # swapped
                    parent_id=PROJECT_ID,
                    depth=1,
                    direct=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                    subtree=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                ),
                WorkBreakdownEffortMeasurementItem(
                    entity_id=TASK_A_ID,  # swapped
                    parent_id=PROJECT_ID,
                    depth=1,
                    direct=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                    subtree=ExecutionEffortSummary(duration_seconds=0, observation_count=0),
                ),
            ),
        )
        with pytest.raises(ExecutionEffortComparisonError, match="entity_id mismatch"):
            compare_work_breakdown_effort(plan, meas)

    def test_non_plan_instance_rejected(self) -> None:
        meas = _make_measurement()
        with pytest.raises(ExecutionEffortComparisonError, match="WorkBreakdownEffortPlan"):
            compare_work_breakdown_effort("not a plan", meas)  # type: ignore[arg-type]

    def test_non_measurement_instance_rejected(self) -> None:
        plan = _make_plan()
        with pytest.raises(
            ExecutionEffortComparisonError, match="WorkBreakdownEffortMeasurement"
        ):
            compare_work_breakdown_effort(plan, "not a measurement")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Direct variance semantics
# ---------------------------------------------------------------------------


class TestDirectVariance:
    def test_planned_100_actual_120_gives_plus_20(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 100))
        meas = _make_measurement(task_a_actual=120)
        result = compare_work_breakdown_effort(plan, meas)
        task_a_item = result.items[1]
        assert task_a_item.direct.planned_duration_seconds == 100
        assert task_a_item.direct.actual_duration_seconds == 120
        assert task_a_item.direct.variance_seconds == 20

    def test_planned_120_actual_100_gives_minus_20(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 120))
        meas = _make_measurement(task_a_actual=100)
        result = compare_work_breakdown_effort(plan, meas)
        task_a_item = result.items[1]
        assert task_a_item.direct.variance_seconds == -20

    def test_equal_values_give_zero(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 100))
        meas = _make_measurement(task_a_actual=100)
        result = compare_work_breakdown_effort(plan, meas)
        task_a_item = result.items[1]
        assert task_a_item.direct.variance_seconds == 0

    def test_explicit_zero_plan_produces_actual_variance(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 0))
        meas = _make_measurement(task_a_actual=50)
        result = compare_work_breakdown_effort(plan, meas)
        task_a_item = result.items[1]
        assert task_a_item.direct.planned_duration_seconds == 0
        assert task_a_item.direct.actual_duration_seconds == 50
        assert task_a_item.direct.variance_seconds == 50

    def test_missing_direct_estimate_yields_none_variance(self) -> None:
        plan = _make_plan(task_a_estimate=None)
        meas = _make_measurement(task_a_actual=100)
        result = compare_work_breakdown_effort(plan, meas)
        task_a_item = result.items[1]
        assert task_a_item.direct.planned_duration_seconds is None
        assert task_a_item.direct.actual_duration_seconds == 100
        assert task_a_item.direct.variance_seconds is None


# ---------------------------------------------------------------------------
# Subtree variance semantics
# ---------------------------------------------------------------------------


class TestSubtreeVariance:
    def test_fully_estimated_subtree_gives_signed_variance(self) -> None:
        plan = _make_plan(
            task_a_estimate=_make_estimate(TASK_A_ID, 100),
            task_b_estimate=_make_estimate(TASK_B_ID, 200),
        )
        meas = _make_measurement(task_a_actual=120, task_b_actual=180)
        result = compare_work_breakdown_effort(plan, meas)
        root = result.items[0]
        # planned total = 300, actual = 300 → variance 0
        assert root.subtree.planned_duration_seconds == 300
        assert root.subtree.actual_duration_seconds == 300
        assert root.subtree.variance_seconds == 0

    def test_incomplete_subtree_yields_none_variance(self) -> None:
        plan = _make_plan(
            task_a_estimate=_make_estimate(TASK_A_ID, 100),
            task_b_estimate=None,  # incomplete
        )
        meas = _make_measurement(task_a_actual=120, task_b_actual=50)
        result = compare_work_breakdown_effort(plan, meas)
        root = result.items[0]
        assert root.subtree.planned_duration_seconds is None
        assert root.subtree.actual_duration_seconds == 170
        assert root.subtree.variance_seconds is None
        # Coverage still visible
        assert root.planned_estimated_entity_count == 1
        assert root.planned_unestimated_entity_count == 1

    def test_actual_zero_with_complete_plan_is_valid(self) -> None:
        plan = _make_plan(
            task_a_estimate=_make_estimate(TASK_A_ID, 100),
            task_b_estimate=_make_estimate(TASK_B_ID, 200),
        )
        meas = _make_measurement(task_a_actual=0, task_b_actual=0)
        result = compare_work_breakdown_effort(plan, meas)
        root = result.items[0]
        assert root.subtree.planned_duration_seconds == 300
        assert root.subtree.actual_duration_seconds == 0
        assert root.subtree.variance_seconds == -300


# ---------------------------------------------------------------------------
# Order preservation and immutability
# ---------------------------------------------------------------------------


class TestOrderAndImmutability:
    def test_exact_preorder_preserved(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 100))
        meas = _make_measurement(task_a_actual=120)
        result = compare_work_breakdown_effort(plan, meas)
        assert [i.entity_id for i in result.items] == [PROJECT_ID, TASK_A_ID, TASK_B_ID]
        assert [i.depth for i in result.items] == [0, 1, 1]
        assert [i.parent_id for i in result.items] == [None, PROJECT_ID, PROJECT_ID]

    def test_source_models_unchanged(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 100))
        meas = _make_measurement(task_a_actual=120)
        plan_before = plan.model_dump()
        meas_before = meas.model_dump()
        compare_work_breakdown_effort(plan, meas)
        assert plan.model_dump() == plan_before
        assert meas.model_dump() == meas_before

    def test_repeated_comparison_yields_equivalent_output(self) -> None:
        plan = _make_plan(task_a_estimate=_make_estimate(TASK_A_ID, 100))
        meas = _make_measurement(task_a_actual=120)
        r1 = compare_work_breakdown_effort(plan, meas)
        r2 = compare_work_breakdown_effort(plan, meas)
        assert r1.model_dump() == r2.model_dump()
