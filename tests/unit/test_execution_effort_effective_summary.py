"""Focused unit evidence for the V1.24 effective-effort summary boundary.

All tests exercise the pure :func:`summarize_effective_work_breakdown_effort_plan`
boundary (and its Pydantic strict contract) with in-memory V1.23 plans only.
No database, repository, migration, wall-clock, or provider interaction occurs.

The required coverage is:

1. summary models are strict, frozen, and reject unknown fields;
2. hostile ``model_construct`` plans are rejected;
3. hostile nested values are rejected;
4. zero estimates;
5. only ordinary selected estimates;
6. only calibrated selected estimates;
7. mixed selected estimates;
8. ordinary + calibrated counts equal the root estimated entity count;
9. root known duration is copied exactly;
10. complete root total is copied exactly;
11. partial root total remains ``None``;
12. root coverage counts are copied exactly;
13. child subtree values cannot change project arithmetic;
14. provenance changes classification only, never arithmetic;
15. repeated summaries are deterministically equivalent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from trajectory_os.application.execution_effort_calibration_acceptance import (
    AcceptedCalibratedEstimateRevision,
)
from trajectory_os.application.execution_effort_calibration_composition import (
    EffectiveCalibrationApplicationResult,
    EffectiveCalibrationApplicationStatus,
)
from trajectory_os.application.execution_effort_calibration_entity_binding import (
    CalibratedEstimateRevisionProposal,
    bind_effort_calibration_to_current_entity,
)
from trajectory_os.application.execution_effort_effective_plan import (
    WorkBreakdownEffectiveEffortPlan,
    WorkBreakdownEffectivePlanItem,
)
from trajectory_os.application.execution_effort_effective_summary import (
    WorkBreakdownEffectiveEffortSummary,
    WorkBreakdownEffectiveSummaryError,
    build_effective_work_breakdown_effort_summary_durably,
    summarize_effective_work_breakdown_effort_plan,
)
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactor,
)
from trajectory_os.domain.execution_effort_calibration_factor_application import (
    apply_effective_effort_calibration_factor,
)
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.execution_effort_planning import PlannedEffortSummary
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("aa000000-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PROJECT_ID = UUID("aa111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TASK_A = UUID("aa222222-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TASK_B = UUID("aa333333-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

EST_A = UUID("aa444444-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
EST_B = UUID("aa555555-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
EST_ROOT = UUID("aa666666-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory construction helpers (no persistence)
# ---------------------------------------------------------------------------


def _estimate(entity_id: UUID, duration_seconds: int, estimate_id: UUID) -> ExecutionEffortEstimate:
    """Build one valid ordinary V1.10 direct effort estimate."""
    return ExecutionEffortEstimate(
        id=estimate_id,
        portfolio_id=PORTFOLIO_ID,
        entity_id=entity_id,
        duration_seconds=duration_seconds,
        estimated_at=T0,
        source=SourceKind.USER_CONFIRMED,
    )


def _factor() -> EffectiveEffortCalibrationFactor:
    """One minimally valid V1.20 effective calibration factor (0.5 applied)."""
    return EffectiveEffortCalibrationFactor(
        entity_type=EntityType.TASK,
        decision_id=UUID("aaaa7777-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        decided_at=datetime(2026, 1, 1, tzinfo=UTC),
        sample_count=1,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=150,
        factor_numerator=3,
        factor_denominator=2,
    )


class _ReadOnlyPortfolioRepository:
    """A read-only repository wrapper satisfying the V1.20 binding boundary."""

    def __init__(self, portfolio: Portfolio) -> None:
        self._portfolio = portfolio

    def load(self, portfolio_id: UUID) -> Portfolio:
        if portfolio_id != self._portfolio.id:
            raise WorkBreakdownEffectiveSummaryError("unexpected portfolio_id")
        return self._portfolio

    def save(self, portfolio: object) -> None:
        raise WorkBreakdownEffectiveSummaryError("summary must never write")


def _provenance(entity_id: UUID, estimate_id: UUID) -> AcceptedCalibratedEstimateRevision:
    """Bind exact V1.21 provenance to one already-accepted calibrated estimate.

    The estimate must have ``duration_seconds == 450`` and ``estimated_at == T0``
    so that the provenance fields stay exactly consistent.
    """
    application = EffectiveCalibrationApplicationResult(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=300,
        status=EffectiveCalibrationApplicationStatus.AVAILABLE,
        proposal=apply_effective_effort_calibration_factor(300, _factor()),
    )

    proposal = bind_effort_calibration_to_current_entity(
        application,
        entity_id,
        _ReadOnlyPortfolioRepository(
            Portfolio(
                id=PORTFOLIO_ID,
                name="summary-test",
                entities=[
                    TrajectoryEntity(
                        id=PROJECT_ID,
                        entity_type=EntityType.PROJECT,
                        title="project",
                        description="",
                    ),
                    TrajectoryEntity(
                        id=entity_id,
                        entity_type=EntityType.TASK,
                        title="task",
                        description="",
                    ),
                ],
                relations=[
                    TrajectoryRelation(
                        source_id=entity_id,
                        target_id=PROJECT_ID,
                        relation_type=RelationType.BELONGS_TO,
                    ),
                ],
            )
        ),
    )
    assert isinstance(
        proposal, CalibratedEstimateRevisionProposal
    ), "binding must succeed for a current calibrated target"

    return AcceptedCalibratedEstimateRevision(
        estimate_id=estimate_id,
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_id=entity_id,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=300,
        calibrated_duration_seconds=450,
        estimated_at=T0,
        source_proposal=proposal,
    )


def _item(
    entity_id: UUID,
    parent_id: UUID | None,
    depth: int,
    *,
    estimate: ExecutionEffortEstimate | None = None,
    provenance: AcceptedCalibratedEstimateRevision | None = None,
    subtree: PlannedEffortSummary | None = None,
) -> WorkBreakdownEffectivePlanItem:
    """Build one valid V1.23 effective-plan item."""
    return WorkBreakdownEffectivePlanItem(
        entity_id=entity_id,
        parent_id=parent_id,
        depth=depth,
        direct_estimate=estimate,
        calibrated_provenance=provenance,
        subtree=subtree
        or PlannedEffortSummary(
            known_duration_seconds=0,
            estimated_entity_count=0,
            unestimated_entity_count=1,
        ),
    )


def _plan(
    root_subtree: PlannedEffortSummary,
    children: tuple[WorkBreakdownEffectivePlanItem, ...] = (),
) -> WorkBreakdownEffectiveEffortPlan:
    """Build a valid V1.23 effective plan for the root project plus children."""
    return WorkBreakdownEffectiveEffortPlan(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        items=(
            _item(PROJECT_ID, None, 0, subtree=root_subtree),
            *children,
        ),
    )


# ---------------------------------------------------------------------------
# 1. Strict/frozen summary model contract
# ---------------------------------------------------------------------------


def test_summary_model_is_strict_frozen_and_forbids_unknown_fields() -> None:
    """The summary exposes exact strict, immutable fields only."""
    root = PlannedEffortSummary(
        known_duration_seconds=900,
        estimated_entity_count=2,
        unestimated_entity_count=1,
    )
    plan = _plan(root)

    summary = WorkBreakdownEffectiveEffortSummary(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        ordinary_estimate_count=1,
        calibrated_estimate_count=1,
        effort=root,
    )

    # Unknown fields are rejected.
    with pytest.raises(ValidationError, match="extra"):
        WorkBreakdownEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            ordinary_estimate_count=1,
            calibrated_estimate_count=1,
            effort=root,
            invented="field",  # type: ignore[call-overload]
        )

    # Strict typing is enforced for the counter fields.
    with pytest.raises(ValidationError):
        WorkBreakdownEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            ordinary_estimate_count="1",  # type: ignore[arg-type]
            calibrated_estimate_count=1,
            effort=root,
        )

    # Negative counters are rejected.
    with pytest.raises(ValidationError):
        WorkBreakdownEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            ordinary_estimate_count=-1,
            calibrated_estimate_count=1,
            effort=root,
        )

    # The model is frozen.
    with pytest.raises(ValidationError):
        summary.project_id = TASK_A  # type: ignore[misc]

    # The summary stays equivalent to the same root values from the plan.
    assert summary.portfolio_id == plan.portfolio_id
    assert summary.project_id == plan.project_id
    assert summary.effort == root


def test_summary_rejects_counter_partition_inconsistent_with_effort() -> None:
    """ordinary + calibrated must be explained by the root estimated count."""
    root = PlannedEffortSummary(
        known_duration_seconds=450,
        estimated_entity_count=1,
        unestimated_entity_count=1,
    )

    with pytest.raises(ValidationError, match="estimated"):
        WorkBreakdownEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            ordinary_estimate_count=2,
            calibrated_estimate_count=1,
            effort=root,
        )


def test_summary_rejects_hostile_constructed_subtree() -> None:
    """A hostile model_construct effort subtree fails strict re-validation."""
    hostile_effort = PlannedEffortSummary.model_construct(
        known_duration_seconds="not-an-int",
        estimated_entity_count=1,
        unestimated_entity_count=1,
        total_duration_seconds=None,
    )
    with pytest.raises(ValidationError, match="strict re-validation"):
        WorkBreakdownEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            ordinary_estimate_count=1,
            calibrated_estimate_count=0,
            effort=hostile_effort,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 2. Hostile model_construct plans are rejected
# ---------------------------------------------------------------------------


def test_hostile_constructed_plan_is_rejected() -> None:
    """Validator bypass on the V1.23 plan boundary does not produce a summary."""
    hostile = WorkBreakdownEffectiveEffortPlan.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        items=(),
    )
    with pytest.raises(WorkBreakdownEffectiveSummaryError):
        summarize_effective_work_breakdown_effort_plan(hostile)


def test_hostile_constructed_plan_items_are_rejected() -> None:
    """Hostile root item values must fail strict re-validation again."""
    hostile = WorkBreakdownEffectiveEffortPlan.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        items=(
            WorkBreakdownEffectivePlanItem.model_construct(
                entity_id="not-a-uuid",  # type: ignore[arg-type]
                parent_id=None,
                depth=0,
                direct_estimate=None,
                calibrated_provenance=None,
                subtree=PlannedEffortSummary(
                    known_duration_seconds=0,
                    estimated_entity_count=0,
                    unestimated_entity_count=1,
                ),
            ),
        ),
    )
    with pytest.raises(WorkBreakdownEffectiveSummaryError):
        summarize_effective_work_breakdown_effort_plan(hostile)


def test_hostile_non_collection_plan_items_are_rejected() -> None:
    """Items that are not a collection of items must be rejected."""
    hostile = WorkBreakdownEffectiveEffortPlan.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        items="not-items",  # type: ignore[arg-type]
    )
    with pytest.raises(WorkBreakdownEffectiveSummaryError):
        summarize_effective_work_breakdown_effort_plan(hostile)


# ---------------------------------------------------------------------------
# 3. Hostile nested values are rejected
# ---------------------------------------------------------------------------


def test_hostile_nested_subtree_is_rejected() -> None:
    """A hostile nested subtree inside an otherwise-plausible item is rejected."""
    hostile_child = WorkBreakdownEffectivePlanItem.model_construct(
        entity_id=TASK_A,
        parent_id=PROJECT_ID,
        depth=1,
        direct_estimate=_estimate(TASK_A, 300, EST_A),
        calibrated_provenance=None,
        subtree=PlannedEffortSummary.model_construct(
            known_duration_seconds=300,
            estimated_entity_count="not-an-int",  # type: ignore[arg-type]
            unestimated_entity_count=0,
            total_duration_seconds=300,
        ),
    )
    hostile_plan = WorkBreakdownEffectiveEffortPlan.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        items=(
            _item(
                PROJECT_ID,
                None,
                0,
                subtree=PlannedEffortSummary(
                    known_duration_seconds=300,
                    estimated_entity_count=1,
                    unestimated_entity_count=1,
                ),
            ),
            hostile_child,
        ),
    )
    with pytest.raises(WorkBreakdownEffectiveSummaryError):
        summarize_effective_work_breakdown_effort_plan(hostile_plan)


def test_hostile_nested_estimate_is_rejected() -> None:
    """A hostile nested direct estimate is rejected during re-validation."""
    hostile_estimate = ExecutionEffortEstimate.model_construct(
        id=EST_A,
        portfolio_id=PORTFOLIO_ID,
        entity_id=TASK_A,
        duration_seconds="not-an-int",  # type: ignore[arg-type]
        estimated_at=T0,
        source=SourceKind.USER_CONFIRMED,
    )
    hostile_child = WorkBreakdownEffectivePlanItem.model_construct(
        entity_id=TASK_A,
        parent_id=PROJECT_ID,
        depth=1,
        direct_estimate=hostile_estimate,
        calibrated_provenance=None,
        subtree=PlannedEffortSummary(
            known_duration_seconds=0,
            estimated_entity_count=1,
            unestimated_entity_count=0,
            total_duration_seconds=0,
        ),
    )
    hostile_plan = WorkBreakdownEffectiveEffortPlan.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        items=(
            _item(
                PROJECT_ID,
                None,
                0,
                subtree=PlannedEffortSummary(
                    known_duration_seconds=0,
                    estimated_entity_count=1,
                    unestimated_entity_count=1,
                ),
            ),
            hostile_child,
        ),
    )
    with pytest.raises(WorkBreakdownEffectiveSummaryError):
        summarize_effective_work_breakdown_effort_plan(hostile_plan)


def test_hostile_nested_provenance_is_rejected() -> None:
    """A hostile nested provenance record is rejected during re-validation."""
    estimate = _estimate(TASK_A, 450, EST_A)
    hostile_provenance = AcceptedCalibratedEstimateRevision.model_construct(
        estimate_id=EST_A,
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_id=TASK_A,
        entity_type=EntityType.TASK,
        candidate_duration_seconds="not-an-int",  # type: ignore[arg-type]
        calibrated_duration_seconds=450,
        estimated_at=T0,
        source_proposal=None,
    )
    hostile_child = WorkBreakdownEffectivePlanItem.model_construct(
        entity_id=TASK_A,
        parent_id=PROJECT_ID,
        depth=1,
        direct_estimate=estimate,
        calibrated_provenance=hostile_provenance,
        subtree=PlannedEffortSummary(
            known_duration_seconds=450,
            estimated_entity_count=1,
            unestimated_entity_count=0,
            total_duration_seconds=450,
        ),
    )
    hostile_plan = WorkBreakdownEffectiveEffortPlan.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PORTFOLIO_ID,
        items=(hostile_child,),
    )
    with pytest.raises(WorkBreakdownEffectiveSummaryError):
        summarize_effective_work_breakdown_effort_plan(hostile_plan)


# ---------------------------------------------------------------------------
# 4-8. Estimation scenarios and the exact partition invariant
# ---------------------------------------------------------------------------


def test_zero_estimates() -> None:
    """A fully unestimated project still produces an exact zero-classification."""
    root = PlannedEffortSummary(
        known_duration_seconds=0,
        estimated_entity_count=0,
        unestimated_entity_count=2,
    )
    summary = summarize_effective_work_breakdown_effort_plan(
        _plan(root, (_item(TASK_A, PROJECT_ID, 1),))
    )

    assert summary.ordinary_estimate_count == 0
    assert summary.calibrated_estimate_count == 0
    assert summary.effort == root
    assert summary.effort.known_duration_seconds == 0
    assert summary.effort.estimated_entity_count == 0
    assert summary.effort.unestimated_entity_count == 2
    assert summary.effort.total_duration_seconds is None


def test_ordinary_only_estimates() -> None:
    """All selected estimates ordinary: classification reflects that exactly."""
    a = _estimate(TASK_A, 300, EST_A)
    b = _estimate(TASK_B, 600, EST_B)
    root = PlannedEffortSummary(
        known_duration_seconds=900,
        estimated_entity_count=2,
        unestimated_entity_count=1,
    )
    plan = _plan(
        root,
        (
            _item(TASK_A, PROJECT_ID, 1, estimate=a),
            _item(TASK_B, PROJECT_ID, 1, estimate=b),
        ),
    )
    summary = summarize_effective_work_breakdown_effort_plan(plan)

    assert summary.ordinary_estimate_count == 2
    assert summary.calibrated_estimate_count == 0


def test_calibrated_only_estimates() -> None:
    """All selected estimates calibrated: classification reflects that exactly."""
    root = PlannedEffortSummary(
        known_duration_seconds=900,
        estimated_entity_count=2,
        unestimated_entity_count=1,
    )
    plan = _plan(
        root,
        (
            _item(
                TASK_A,
                PROJECT_ID,
                1,
                estimate=_estimate(TASK_A, 450, EST_A),
                provenance=_provenance(TASK_A, EST_A),
            ),
            _item(
                TASK_B,
                PROJECT_ID,
                1,
                estimate=_estimate(TASK_B, 450, EST_B),
                provenance=_provenance(TASK_B, EST_B),
            ),
        ),
    )
    summary = summarize_effective_work_breakdown_effort_plan(plan)

    assert summary.ordinary_estimate_count == 0
    assert summary.calibrated_estimate_count == 2


def test_mixed_estimates_and_partition_invariant() -> None:
    """Mixed selection keeps the exact partition and preserves the invariant."""
    ordinary = _estimate(TASK_A, 300, EST_A)
    calibrated = _estimate(TASK_B, 450, EST_B)
    root = PlannedEffortSummary(
        known_duration_seconds=750,
        estimated_entity_count=2,
        unestimated_entity_count=1,
    )
    plan = _plan(
        root,
        (
            _item(TASK_A, PROJECT_ID, 1, estimate=ordinary),
            _item(
                TASK_B,
                PROJECT_ID,
                1,
                estimate=calibrated,
                provenance=_provenance(TASK_B, EST_B),
            ),
        ),
    )
    summary = summarize_effective_work_breakdown_effort_plan(plan)

    assert summary.ordinary_estimate_count == 1
    assert summary.calibrated_estimate_count == 1
    assert (
        summary.ordinary_estimate_count + summary.calibrated_estimate_count
        == summary.effort.estimated_entity_count
    )


# ---------------------------------------------------------------------------
# 9-12. Root effort values are copied exactly
# ---------------------------------------------------------------------------


def test_root_known_duration_is_copied_exactly() -> None:
    """The project-level known duration is the authoritative root value."""
    a = _estimate(TASK_A, 300, EST_A)
    b = _estimate(TASK_B, 600, EST_B)
    root = PlannedEffortSummary(
        known_duration_seconds=900,
        estimated_entity_count=2,
        unestimated_entity_count=1,
    )
    summary = summarize_effective_work_breakdown_effort_plan(
        _plan(
            root,
            (
                _item(TASK_A, PROJECT_ID, 1, estimate=a),
                _item(TASK_B, PROJECT_ID, 1, estimate=b),
            ),
        )
    )
    assert summary.effort.known_duration_seconds == 900
    assert summary.effort == root


def test_complete_root_total_is_copied_exactly() -> None:
    """A fully estimated WBS exposes the exact complete root total."""
    root = PlannedEffortSummary(
        known_duration_seconds=450,
        estimated_entity_count=2,
        unestimated_entity_count=0,
        total_duration_seconds=450,
    )
    # Every WBS node is estimated, so the root exposes an exact complete total.
    plan = WorkBreakdownEffectiveEffortPlan(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        items=(
            _item(
                PROJECT_ID,
                None,
                0,
                estimate=_estimate(PROJECT_ID, 150, EST_ROOT),
                subtree=root,
            ),
            _item(
                TASK_A,
                PROJECT_ID,
                1,
                estimate=_estimate(TASK_A, 300, EST_A),
                subtree=PlannedEffortSummary(
                    known_duration_seconds=300,
                    estimated_entity_count=1,
                    unestimated_entity_count=0,
                    total_duration_seconds=300,
                ),
            ),
        ),
    )
    summary = summarize_effective_work_breakdown_effort_plan(plan)

    assert summary.effort.known_duration_seconds == 450
    assert summary.effort.total_duration_seconds == 450
    assert summary.effort.estimated_entity_count == 2
    assert summary.effort.unestimated_entity_count == 0


def test_partial_root_total_remains_none() -> None:
    """A partially estimated WBS never exposes a complete root total."""
    root = PlannedEffortSummary(
        known_duration_seconds=750,
        estimated_entity_count=2,
        unestimated_entity_count=1,
    )
    summary = summarize_effective_work_breakdown_effort_plan(
        _plan(
            root,
            (
                _item(TASK_A, PROJECT_ID, 1, estimate=_estimate(TASK_A, 300, EST_A)),
                _item(TASK_B, PROJECT_ID, 1, estimate=_estimate(TASK_B, 450, EST_B)),
            ),
        )
    )
    assert summary.effort.total_duration_seconds is None


def test_root_coverage_counts_are_copied_exactly() -> None:
    """Estimated and unestimated coverage counts are verbatim root values."""
    root = PlannedEffortSummary(
        known_duration_seconds=450,
        estimated_entity_count=1,
        unestimated_entity_count=3,
    )
    summary = summarize_effective_work_breakdown_effort_plan(
        _plan(
            root,
            (
                _item(TASK_A, PROJECT_ID, 1, estimate=_estimate(TASK_A, 450, EST_A)),
                _item(TASK_B, PROJECT_ID, 1),
            ),
        )
    )
    assert summary.effort.estimated_entity_count == 1
    assert summary.effort.unestimated_entity_count == 3


# ---------------------------------------------------------------------------
# 13. Child subtree values cannot change project arithmetic
# ---------------------------------------------------------------------------


def test_child_subtree_values_cannot_change_project_arithmetic() -> None:
    """Inflated child summaries never alter the authoritative root summary."""
    root = PlannedEffortSummary(
        known_duration_seconds=900,
        estimated_entity_count=2,
        unestimated_entity_count=1,
    )

    benign = _plan(
        root,
        (
            _item(TASK_A, PROJECT_ID, 1, estimate=_estimate(TASK_A, 300, EST_A)),
            _item(TASK_B, PROJECT_ID, 1, estimate=_estimate(TASK_B, 600, EST_B)),
        ),
    )

    inflated = _plan(
        root,
        (
            _item(
                TASK_A,
                PROJECT_ID,
                1,
                estimate=_estimate(TASK_A, 300, EST_A),
                subtree=PlannedEffortSummary(
                    known_duration_seconds=10_000_000,
                    estimated_entity_count=999,
                    unestimated_entity_count=999,
                ),
            ),
            _item(TASK_B, PROJECT_ID, 1, estimate=_estimate(TASK_B, 600, EST_B)),
        ),
    )

    assert (
        summarize_effective_work_breakdown_effort_plan(benign)
        == summarize_effective_work_breakdown_effort_plan(inflated)
    )
    summary = summarize_effective_work_breakdown_effort_plan(inflated)
    assert summary.effort == root


# ---------------------------------------------------------------------------
# 14. Provenance changes classification only, never arithmetic
# ---------------------------------------------------------------------------


def test_provenance_changes_classification_only_never_arithmetic() -> None:
    """Adding/removing provenance reclassifies but keeps every effort value."""
    root = PlannedEffortSummary(
        known_duration_seconds=450,
        estimated_entity_count=1,
        unestimated_entity_count=1,
    )

    ordinary_plan = _plan(
        root,
        (_item(TASK_A, PROJECT_ID, 1, estimate=_estimate(TASK_A, 450, EST_A)),),
    )
    calibrated_plan = _plan(
        root,
        (
            _item(
                TASK_A,
                PROJECT_ID,
                1,
                estimate=_estimate(TASK_A, 450, EST_A),
                provenance=_provenance(TASK_A, EST_A),
            ),
        ),
    )

    ordinary_summary = summarize_effective_work_breakdown_effort_plan(ordinary_plan)
    calibrated_summary = summarize_effective_work_breakdown_effort_plan(
        calibrated_plan
    )

    # Classification moves exactly one estimate between the two classes.
    assert ordinary_summary.ordinary_estimate_count == 1
    assert ordinary_summary.calibrated_estimate_count == 0
    assert calibrated_summary.ordinary_estimate_count == 0
    assert calibrated_summary.calibrated_estimate_count == 1

    # Arithmetic is identical in both scenarios.
    assert ordinary_summary.effort == calibrated_summary.effort
    assert ordinary_summary.effort.known_duration_seconds == 450
    assert ordinary_summary.effort.estimated_entity_count == 1
    assert ordinary_summary.effort.unestimated_entity_count == 1
    assert ordinary_summary.effort.total_duration_seconds is None


# ---------------------------------------------------------------------------
# 15. Deterministic equivalence
# ---------------------------------------------------------------------------


def test_repeated_summaries_are_deterministically_equivalent() -> None:
    """Re-summarizing the same plan, and rebuilding the same plan, are stable."""
    root = PlannedEffortSummary(
        known_duration_seconds=750,
        estimated_entity_count=2,
        unestimated_entity_count=1,
    )
    plan = _plan(
        root,
        (
            _item(TASK_A, PROJECT_ID, 1, estimate=_estimate(TASK_A, 300, EST_A)),
            _item(
                TASK_B,
                PROJECT_ID,
                1,
                estimate=_estimate(TASK_B, 450, EST_B),
                provenance=_provenance(TASK_B, EST_B),
            ),
        ),
    )

    first = summarize_effective_work_breakdown_effort_plan(plan)
    second = summarize_effective_work_breakdown_effort_plan(plan)

    # Same input object, repeated boundary calls: bit-equivalent results.
    assert first == second
    assert first.ordinary_estimate_count == 1
    assert first.calibrated_estimate_count == 1
    assert first.effort == root

    # Fresh construction of an equivalent plan yields an equivalent summary.
    rebuilt = _plan(
        root,
        (
            _item(TASK_A, PROJECT_ID, 1, estimate=plan.items[1].direct_estimate),
            _item(
                TASK_B,
                PROJECT_ID,
                1,
                estimate=plan.items[2].direct_estimate,
                provenance=plan.items[2].calibrated_provenance,
            ),
        ),
    )
    assert summarize_effective_work_breakdown_effort_plan(rebuilt) == first


# ---------------------------------------------------------------------------
# Durable boundary: single authoritative read, exact equality
# ---------------------------------------------------------------------------


class _TrackingPortfolioRepository:
    """Minimal repository stand-in that counts authoritative reads."""

    def __init__(self, portfolio: Portfolio, reads: list[str]) -> None:
        self._portfolio = portfolio
        self._reads = reads

    def load(self, portfolio_id: UUID) -> object:
        self._reads.append("load")
        if portfolio_id != self._portfolio.id:
            raise WorkBreakdownEffectiveSummaryError("unexpected portfolio_id")
        return self._portfolio

    def save(self, portfolio: object) -> None:
        raise WorkBreakdownEffectiveSummaryError("summary must never write")


class _TrackingEstimateReader:
    """Minimal estimate reader that returns a fixed history and counts calls."""

    def __init__(
        self,
        estimates: tuple[object, ...],
        reads: list[str],
    ) -> None:
        self._estimates = estimates
        self._reads = reads

    def list_for_portfolio(self, portfolio_id: UUID) -> tuple[object, ...]:
        self._reads.append("list_for_portfolio")
        return self._estimates

    def list_for_entity(self, portfolio_id: UUID, entity_id: UUID) -> tuple[object, ...]:
        self._reads.append("list_for_entity")
        return self._estimates


class _TrackingProvenanceReader:
    """Minimal provenance reader that counts per-estimate lookups."""

    def __init__(
        self,
        by_estimate_id: dict[UUID, AcceptedCalibratedEstimateRevision],
        reads: list[str],
    ) -> None:
        self._by_estimate_id = by_estimate_id
        self._reads = reads

    def get_provenance(
        self, estimate_id: UUID
    ) -> AcceptedCalibratedEstimateRevision | None:
        self._reads.append("get_provenance")
        return self._by_estimate_id.get(estimate_id)


def _portfolio_for_summary_tests() -> Portfolio:
    """A minimal two-task portfolio with one project root."""
    return Portfolio(
        id=PORTFOLIO_ID,
        name="summary-test",
        entities=[
            TrajectoryEntity(
                id=PROJECT_ID,
                entity_type=EntityType.PROJECT,
                title="project",
                description="",
            ),
            TrajectoryEntity(
                id=TASK_A,
                entity_type=EntityType.TASK,
                title="task A",
                description="",
            ),
            TrajectoryEntity(
                id=TASK_B,
                entity_type=EntityType.TASK,
                title="task B",
                description="",
            ),
        ],
        relations=[
            TrajectoryRelation(
                source_id=TASK_A,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
            ),
            TrajectoryRelation(
                source_id=TASK_B,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
            ),
        ],
    )


def test_durable_boundary_reads_exactly_once_and_delegates_to_pure_summary() -> None:
    """The durable API performs one authoritative portfolio read and mirrors V1.23."""
    estimate_a = _estimate(TASK_A, 300, EST_A)
    estimate_b = _estimate(TASK_B, 450, EST_B)
    reads: list[str] = []

    summary = build_effective_work_breakdown_effort_summary_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        _TrackingPortfolioRepository(_portfolio_for_summary_tests(), reads),
        _TrackingEstimateReader((estimate_a, estimate_b), reads),
        _TrackingProvenanceReader({EST_B: _provenance(TASK_B, EST_B)}, reads),
    )

    # Exactly one authoritative portfolio load; the estimate history is read
    # exactly once per the V1.23 contract and provenance is looked up only for
    # the two selected direct estimates.
    assert reads.count("load") == 1
    assert reads.count("list_for_portfolio") == 1
    assert reads.count("get_provenance") == 2

    # The durable result carries the authoritative plan arithmetic verbatim,
    # with the ordinary/calibrated classification derived from provenance.
    assert summary.portfolio_id == PORTFOLIO_ID
    assert summary.project_id == PROJECT_ID
    assert summary.ordinary_estimate_count == 1
    assert summary.calibrated_estimate_count == 1
    assert summary.effort.known_duration_seconds == 750
    assert summary.effort.estimated_entity_count == 2
    assert summary.effort.unestimated_entity_count == 1
    assert summary.effort.total_duration_seconds is None
