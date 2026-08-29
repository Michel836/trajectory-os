"""Unit tests for the V1.23 read-only enriched-work-breakdown plan.

Covers the required evidence surface for
**enrich_work_breakdown_effort_plan_with_calibration_provenance** and
**build_orchestrated_effective_plan**:

1.  strict/frozen result model (WorkBreakdownEffortPlan cannot carry extra fields),
2.  hostile rejections (non-portfolio/non-plan inputs rejected),
3.  exact calibration provenance matching (estimate_id is key),
4.  subtree and WBS order preservation,
5.  lookup-count verification (exactly once per calibrated item).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

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
    WorkBreakdownEffectivePlanError,
    WorkBreakdownEffectivePlanItem,
    build_effective_work_breakdown_effort_plan_durably,
    enrich_work_breakdown_effort_plan_with_calibration_provenance,
)
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactor,
)
from trajectory_os.domain.execution_effort_calibration_factor_application import (
    apply_effective_effort_calibration_factor,
)
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.execution_effort_planning import (
    PlannedEffortSummary,
    WorkBreakdownEffortPlan,
    WorkBreakdownEffortPlanItem,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("71111111-1111-4111-8111-111111111111")
PROJECT_ID = UUID("73333333-3333-4333-8333-333333333333")
TASK_ID = UUID("75555555-5555-4555-8555-555555555555")
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


class _ReadOnlyPortfolioRepository:
    def __init__(self, portfolio: Portfolio) -> None:
        self._portfolio = portfolio

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        if portfolio_id == self._portfolio.id:
            return self._portfolio
        return None

    def save(self, portfolio: Portfolio) -> None:
        raise AssertionError("V1.23 provenance fixture must never save")


def _factor() -> EffectiveEffortCalibrationFactor:
    return EffectiveEffortCalibrationFactor(
        entity_type=EntityType.TASK,
        decision_id=UUID("aaaa1111-1111-4111-8111-111111111111"),
        decided_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=150,
        factor_numerator=3,
        factor_denominator=2,
    )


def _ready_proposal(
    portfolio_id: UUID,
    project_id: UUID,
    entity_id: UUID,
) -> CalibratedEstimateRevisionProposal:
    project = TrajectoryEntity(
        id=project_id,
        entity_type=EntityType.PROJECT,
        title="project",
        description="",
    )
    task = TrajectoryEntity(
        id=entity_id,
        entity_type=EntityType.TASK,
        title="task",
        description="",
    )
    portfolio = Portfolio(
        id=portfolio_id,
        name="fixture",
        entities=[project, task],
        relations=[
            TrajectoryRelation(
                source_id=entity_id,
                target_id=project_id,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )

    result = EffectiveCalibrationApplicationResult(
        portfolio_id=portfolio_id,
        project_id=project_id,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=300,
        status=EffectiveCalibrationApplicationStatus.AVAILABLE,
        proposal=apply_effective_effort_calibration_factor(300, _factor()),
    )

    return bind_effort_calibration_to_current_entity(
        result,
        entity_id,
        _ReadOnlyPortfolioRepository(portfolio),
    )

def _record(
    portfolio_id: UUID = PORTFOLIO_ID,
    project_id: UUID = PROJECT_ID,
    entity_id: UUID = TASK_ID,
    estimate_id: UUID | None = None,
    **overrides: object,
) -> AcceptedCalibratedEstimateRevision:
    kwargs: dict[str, object] = dict(
        estimate_id=estimate_id or uuid4(),
        portfolio_id=portfolio_id,
        project_id=project_id,
        entity_id=entity_id,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=300,
        calibrated_duration_seconds=450,
        estimated_at=T0,
        source_proposal=_ready_proposal(portfolio_id, project_id, entity_id),
    )
    kwargs.update(overrides)
    return AcceptedCalibratedEstimateRevision(**kwargs)


def _estimate(
    portfolio_id: UUID = PORTFOLIO_ID,
    entity_id: UUID = TASK_ID,
    estimate_id: UUID | None = None,
    **overrides: object,
) -> ExecutionEffortEstimate:
    est_kwds = dict(
        id=estimate_id or uuid4(),
        portfolio_id=portfolio_id,
        entity_id=entity_id,
        duration_seconds=300,
        estimated_at=T0,
        source=SourceKind.USER_CONFIRMED,
    )
    est_kwds.update(overrides)
    return ExecutionEffortEstimate(**est_kwds)


def _plan_item(
    entity_id: UUID,
    parent_id: UUID | None,
    depth: int,
    direct_estimate: ExecutionEffortEstimate | None = None,
) -> WorkBreakdownEffortPlanItem:
    return WorkBreakdownEffortPlanItem(
        entity_id=entity_id,
        parent_id=parent_id,
        depth=depth,
        direct_estimate=direct_estimate,
        subtree=PlannedEffortSummary(
            known_duration_seconds=0,
            estimated_entity_count=0,
            unestimated_entity_count=0,
            total_duration_seconds=0,
        ),
    )


def _plan(
    items: list[WorkBreakdownEffortPlanItem],
    portfolio_id: UUID = PORTFOLIO_ID,
    project_id: UUID = PROJECT_ID,
) -> WorkBreakdownEffortPlan:
    root = WorkBreakdownEffortPlanItem(
        entity_id=project_id,
        parent_id=None,
        depth=0,
        direct_estimate=None,
        subtree=PlannedEffortSummary(
            known_duration_seconds=0,
            estimated_entity_count=0,
            unestimated_entity_count=0,
            total_duration_seconds=0,
        ),
    )

    normalized_children = tuple(
        WorkBreakdownEffortPlanItem(
            entity_id=item.entity_id,
            parent_id=project_id,
            depth=1,
            direct_estimate=item.direct_estimate,
            subtree=item.subtree,
        )
        for item in items
    )

    return WorkBreakdownEffortPlan(
        portfolio_id=portfolio_id,
        project_id=project_id,
        items=(root, *normalized_children),
    )


class _TrackingPortfolioRepository:
    def __init__(self, portfolio: Portfolio) -> None:
        self._portfolio = portfolio
        self.load_calls: list[UUID] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.load_calls.append(portfolio_id)
        if portfolio_id == self._portfolio.id:
            return self._portfolio
        return None

    def save(self, portfolio: Portfolio) -> None:
        raise AssertionError("V1.23 durable builder must never save")


class _TrackingEstimateReader:
    def __init__(
        self,
        estimates: tuple[ExecutionEffortEstimate, ...],
    ) -> None:
        self._estimates = estimates
        self.portfolio_calls: list[UUID] = []

    def list_for_portfolio(
        self,
        portfolio_id: UUID,
    ) -> tuple[ExecutionEffortEstimate, ...]:
        self.portfolio_calls.append(portfolio_id)
        return self._estimates

    def list_for_entity(
        self,
        portfolio_id: UUID,
        entity_id: UUID,
    ) -> tuple[ExecutionEffortEstimate, ...]:
        raise AssertionError(
            "V1.23 durable builder must not perform per-entity estimate reads"
        )


class _TrackingProvenanceReader:
    def __init__(
        self,
        provenances: dict[UUID, AcceptedCalibratedEstimateRevision] | None = None,
    ) -> None:
        self._provenances = provenances or {}
        self.calls: list[UUID] = []

    def get_provenance(
        self,
        estimate_id: UUID,
    ) -> AcceptedCalibratedEstimateRevision | None:
        self.calls.append(estimate_id)
        return self._provenances.get(estimate_id)


def _current_portfolio(
    task_id: UUID = TASK_ID,
) -> Portfolio:
    project = TrajectoryEntity(
        id=PROJECT_ID,
        entity_type=EntityType.PROJECT,
        title="project",
        description="",
    )
    task = TrajectoryEntity(
        id=task_id,
        entity_type=EntityType.TASK,
        title="task",
        description="",
    )
    return Portfolio(
        id=PORTFOLIO_ID,
        name="current",
        entities=[project, task],
        relations=[
            TrajectoryRelation(
                source_id=task_id,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )


def test_strict_frozen_model():
    """Result item is strict, frozen, and forbids extra fields."""
    subtree = PlannedEffortSummary(
        known_duration_seconds=0,
        estimated_entity_count=0,
        unestimated_entity_count=1,
        total_duration_seconds=None,
    )

    # extra="forbid"
    with pytest.raises(ValidationError):
        WorkBreakdownEffectivePlanItem(
            entity_id=TASK_ID,
            parent_id=PROJECT_ID,
            depth=1,
            direct_estimate=None,
            calibrated_provenance=None,
            subtree=subtree,
            extra_field="should_not_be_allowed",
        )

    # strict=True: no coercion from str -> int.
    with pytest.raises(ValidationError):
        WorkBreakdownEffectivePlanItem(
            entity_id=TASK_ID,
            parent_id=PROJECT_ID,
            depth="1",
            direct_estimate=None,
            calibrated_provenance=None,
            subtree=subtree,
        )

    item = WorkBreakdownEffectivePlanItem(
        entity_id=TASK_ID,
        parent_id=PROJECT_ID,
        depth=1,
        direct_estimate=None,
        calibrated_provenance=None,
        subtree=subtree,
    )

    # frozen=True
    with pytest.raises(ValidationError):
        item.depth = 2


def test_effective_item_rejects_provenance_without_direct_estimate():
    provenance = _record()

    with pytest.raises(ValidationError):
        WorkBreakdownEffectivePlanItem(
            entity_id=TASK_ID,
            parent_id=PROJECT_ID,
            depth=1,
            direct_estimate=None,
            calibrated_provenance=provenance,
            subtree=PlannedEffortSummary(
                known_duration_seconds=0,
                estimated_entity_count=0,
                unestimated_entity_count=1,
                total_duration_seconds=None,
            ),
        )


def test_effective_item_rejects_mismatching_provenance():
    estimate = _estimate(
        entity_id=TASK_ID,
        duration_seconds=450,
    )
    provenance = _record(
        entity_id=TASK_ID,
        estimate_id=uuid4(),
    )

    with pytest.raises(ValidationError):
        WorkBreakdownEffectivePlanItem(
            entity_id=TASK_ID,
            parent_id=PROJECT_ID,
            depth=1,
            direct_estimate=estimate,
            calibrated_provenance=provenance,
            subtree=PlannedEffortSummary(
                known_duration_seconds=450,
                estimated_entity_count=1,
                unestimated_entity_count=0,
                total_duration_seconds=450,
            ),
        )


def test_effective_plan_rejects_foreign_provenance_project():
    estimate = _estimate(
        entity_id=TASK_ID,
        duration_seconds=450,
    )
    foreign_project_id = uuid4()
    provenance = _record(
        project_id=foreign_project_id,
        entity_id=TASK_ID,
        estimate_id=estimate.id,
    )

    root = WorkBreakdownEffectivePlanItem(
        entity_id=PROJECT_ID,
        parent_id=None,
        depth=0,
        direct_estimate=None,
        calibrated_provenance=None,
        subtree=PlannedEffortSummary(
            known_duration_seconds=450,
            estimated_entity_count=1,
            unestimated_entity_count=1,
            total_duration_seconds=None,
        ),
    )
    child = WorkBreakdownEffectivePlanItem(
        entity_id=TASK_ID,
        parent_id=PROJECT_ID,
        depth=1,
        direct_estimate=estimate,
        calibrated_provenance=provenance,
        subtree=PlannedEffortSummary(
            known_duration_seconds=450,
            estimated_entity_count=1,
            unestimated_entity_count=0,
            total_duration_seconds=450,
        ),
    )

    with pytest.raises(ValidationError):
        WorkBreakdownEffectiveEffortPlan(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            items=(root, child),
        )


def test_hostile_plan_rejection():
    """Test that hostile plan inputs are rejected."""
    # Test with non-WorkBreakdownEffortPlan
    with pytest.raises(WorkBreakdownEffectivePlanError):
        enrich_work_breakdown_effort_plan_with_calibration_provenance(
            "not_a_plan", {}
        )


def test_hostile_constructed_plan_item_rejection():
    hostile_item = WorkBreakdownEffortPlanItem.model_construct(
        entity_id="not-a-uuid",
        parent_id=None,
        depth=0,
        direct_estimate=None,
        subtree=PlannedEffortSummary(
            known_duration_seconds=0,
            estimated_entity_count=0,
            unestimated_entity_count=0,
            total_duration_seconds=0,
        ),
    )
    hostile_plan = WorkBreakdownEffortPlan.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        items=(hostile_item,),
    )

    with pytest.raises(WorkBreakdownEffectivePlanError):
        enrich_work_breakdown_effort_plan_with_calibration_provenance(
            hostile_plan,
            {},
        )


def test_hostile_constructed_subtree_rejection():
    hostile_subtree = PlannedEffortSummary.model_construct(
        known_duration_seconds="not-an-int",
        estimated_entity_count=0,
        unestimated_entity_count=0,
        total_duration_seconds=0,
    )
    hostile_item = WorkBreakdownEffortPlanItem.model_construct(
        entity_id=PROJECT_ID,
        parent_id=None,
        depth=0,
        direct_estimate=None,
        subtree=hostile_subtree,
    )
    hostile_plan = WorkBreakdownEffortPlan.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        items=(hostile_item,),
    )

    with pytest.raises(WorkBreakdownEffectivePlanError):
        enrich_work_breakdown_effort_plan_with_calibration_provenance(
            hostile_plan,
            {},
        )


def test_hostile_estimate_rejection():
    """Test that hostile direct estimates are rejected."""
    hostile_estimate = ExecutionEffortEstimate.model_construct(
        id=uuid4(),
        portfolio_id=PORTFOLIO_ID,
        entity_id=PROJECT_ID,
        duration_seconds="not-an-int",
        estimated_at=T0,
        source=SourceKind.USER_CONFIRMED,
    )
    hostile_item = WorkBreakdownEffortPlanItem.model_construct(
        entity_id=PROJECT_ID,
        parent_id=None,
        depth=0,
        direct_estimate=hostile_estimate,
        subtree=PlannedEffortSummary(
            known_duration_seconds=0,
            estimated_entity_count=0,
            unestimated_entity_count=0,
            total_duration_seconds=0,
        ),
    )
    plan = WorkBreakdownEffortPlan.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        items=(hostile_item,),
    )

    with pytest.raises(WorkBreakdownEffectivePlanError):
        enrich_work_breakdown_effort_plan_with_calibration_provenance(
            plan, {}
        )


def test_hostile_provenance_rejection():
    """Test that hostile provenance is rejected."""
    # Create a valid plan
    entity_id = uuid4()
    estimate = _estimate(entity_id=entity_id)
    item = _plan_item(entity_id, None, 0, estimate)
    plan = _plan([item])

    # Test with hostile provenance
    with pytest.raises(WorkBreakdownEffectivePlanError):
        enrich_work_breakdown_effort_plan_with_calibration_provenance(
            plan, {"some_id": "not_a_provenance"}
        )


def test_duplicate_provenance_rejection():
    """Reject two supplied provenances carrying the same estimate_id."""
    entity_id = uuid4()
    estimate = _estimate(
        entity_id=entity_id,
        duration_seconds=450,
    )
    item = _plan_item(entity_id, None, 0, estimate)
    plan = _plan([item])

    provenance = _record(
        entity_id=entity_id,
        estimate_id=estimate.id,
    )

    # Distinct mapping keys preserve both entries at runtime, while both
    # provenance values claim the same estimate_id.
    provenances = {
        uuid4(): provenance,
        uuid4(): provenance,
    }

    assert len(provenances) == 2

    with pytest.raises(
        WorkBreakdownEffectivePlanError,
        match="duplicate provenance estimate_id",
    ):
        enrich_work_breakdown_effort_plan_with_calibration_provenance(
            plan,
            provenances,
        )


def test_foreign_provenance_rejection():
    """Test that provenance for non-selected estimates is rejected."""
    # Create a valid plan with one item
    entity_id = uuid4()
    estimate = _estimate(entity_id=entity_id)
    item = _plan_item(entity_id, None, 0, estimate)
    plan = _plan([item])

    # Test with provenance for a different estimate
    foreign_provenance = _record(estimate_id=uuid4())
    with pytest.raises(WorkBreakdownEffectivePlanError):
        enrich_work_breakdown_effort_plan_with_calibration_provenance(
            plan, {foreign_provenance.estimate_id: foreign_provenance}
        )


def test_provenance_matching():
    """Test that provenance matching works correctly."""
    # Create a valid plan with one item
    entity_id = uuid4()
    estimate_id = uuid4()
    estimate = _estimate(
        entity_id=entity_id,
        estimate_id=estimate_id,
        duration_seconds=450,
    )
    item = _plan_item(entity_id, None, 0, estimate)
    plan = _plan([item])

    # Test with matching provenance
    provenance = _record(entity_id=entity_id, estimate_id=estimate_id)
    enriched_plan = enrich_work_breakdown_effort_plan_with_calibration_provenance(
        plan, {provenance.estimate_id: provenance}
    )

    assert enriched_plan.items[1].calibrated_provenance is not None
    assert enriched_plan.items[1].calibrated_provenance.estimate_id == estimate_id


def test_unestimated_items():
    """Test that unestimated items have None provenance and no lookup."""
    # Create a plan with one estimated item and one unestimated item
    entity1_id = uuid4()
    entity2_id = uuid4()
    estimate_id = uuid4()
    estimate = _estimate(
        entity_id=entity1_id,
        estimate_id=estimate_id,
        duration_seconds=450,
    )

    item1 = _plan_item(entity1_id, None, 0, estimate)
    item2 = _plan_item(entity2_id, None, 0, None)
    plan = _plan([item1, item2])

    # Test with provenance
    provenance = _record(entity_id=entity1_id, estimate_id=estimate_id)
    enriched_plan = enrich_work_breakdown_effort_plan_with_calibration_provenance(
        plan, {provenance.estimate_id: provenance}
    )

    # Project root has no direct estimate or provenance.
    assert enriched_plan.items[0].calibrated_provenance is None

    # Estimated child has the exact calibrated provenance.
    assert enriched_plan.items[1].calibrated_provenance is not None

    # Unestimated child has no provenance.
    assert enriched_plan.items[2].calibrated_provenance is None


def test_preservation_of_order_and_structure():
    """Test that the plan preserves order and structure."""
    # Create a plan with multiple items
    entity1_id = uuid4()
    entity2_id = uuid4()
    estimate1_id = uuid4()
    estimate2_id = uuid4()

    estimate1 = _estimate(
        entity_id=entity1_id,
        estimate_id=estimate1_id,
        duration_seconds=450,
    )
    estimate2 = _estimate(entity_id=entity2_id, estimate_id=estimate2_id)

    item1 = _plan_item(entity1_id, None, 0, estimate1)
    item2 = _plan_item(entity2_id, None, 0, estimate2)
    plan = _plan([item1, item2])

    # Test with provenance
    provenance = _record(entity_id=entity1_id, estimate_id=estimate1_id)
    enriched_plan = enrich_work_breakdown_effort_plan_with_calibration_provenance(
        plan, {provenance.estimate_id: provenance}
    )

    # Check that order is preserved
    assert len(enriched_plan.items) == 3
    assert enriched_plan.items[0].entity_id == PROJECT_ID
    assert enriched_plan.items[1].entity_id == entity1_id
    assert enriched_plan.items[2].entity_id == entity2_id

    # Check that subtree summaries are preserved
    assert enriched_plan.items[1].subtree.known_duration_seconds == 0
    assert enriched_plan.items[2].subtree.known_duration_seconds == 0


def test_mismatching_provenance_rejection():
    """Test that mismatched provenance is rejected."""
    # Create a valid plan with one item
    entity_id = uuid4()
    estimate_id = uuid4()
    estimate = _estimate(entity_id=entity_id, estimate_id=estimate_id)
    item = _plan_item(entity_id, None, 0, estimate)
    plan = _plan([item])

    # Test with mismatched provenance
    provenance = _record(entity_id=uuid4(), estimate_id=estimate_id)
    with pytest.raises(WorkBreakdownEffectivePlanError):
        enrich_work_breakdown_effort_plan_with_calibration_provenance(
            plan, {provenance.estimate_id: provenance}
        )


def test_durable_newer_ordinary_estimate_wins_over_older_calibrated():
    older_id = UUID("10000000-0000-4000-8000-000000000001")
    newer_id = UUID("20000000-0000-4000-8000-000000000001")
    newer_time = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)

    older_calibrated = _estimate(
        entity_id=TASK_ID,
        estimate_id=older_id,
        duration_seconds=450,
        estimated_at=T0,
    )
    newer_ordinary = _estimate(
        entity_id=TASK_ID,
        estimate_id=newer_id,
        duration_seconds=600,
        estimated_at=newer_time,
    )
    old_provenance = _record(
        entity_id=TASK_ID,
        estimate_id=older_id,
    )

    portfolio_repo = _TrackingPortfolioRepository(_current_portfolio())
    estimate_reader = _TrackingEstimateReader(
        (older_calibrated, newer_ordinary)
    )
    provenance_reader = _TrackingProvenanceReader(
        {older_id: old_provenance}
    )

    result = build_effective_work_breakdown_effort_plan_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repo,
        estimate_reader,
        provenance_reader,
    )

    task_item = result.items[1]

    assert task_item.direct_estimate == newer_ordinary
    assert task_item.calibrated_provenance is None

    # V1.23 must not even ask about non-selected historical provenance.
    assert provenance_reader.calls == [newer_id]
    assert older_id not in provenance_reader.calls


def test_durable_selected_calibrated_estimate_gets_exact_provenance():
    estimate_id = UUID("30000000-0000-4000-8000-000000000001")
    estimate = _estimate(
        entity_id=TASK_ID,
        estimate_id=estimate_id,
        duration_seconds=450,
    )
    provenance = _record(
        entity_id=TASK_ID,
        estimate_id=estimate_id,
    )

    portfolio_repo = _TrackingPortfolioRepository(_current_portfolio())
    estimate_reader = _TrackingEstimateReader((estimate,))
    provenance_reader = _TrackingProvenanceReader(
        {estimate_id: provenance}
    )

    result = build_effective_work_breakdown_effort_plan_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repo,
        estimate_reader,
        provenance_reader,
    )

    task_item = result.items[1]

    assert task_item.direct_estimate == estimate
    assert task_item.calibrated_provenance == provenance
    assert provenance_reader.calls == [estimate_id]


def test_durable_equal_timestamp_preserves_v1_10_uuid_tiebreak():
    lower_id = UUID("10000000-0000-4000-8000-000000000001")
    higher_id = UUID("f0000000-0000-4000-8000-000000000001")

    lower = _estimate(
        entity_id=TASK_ID,
        estimate_id=lower_id,
        duration_seconds=111,
        estimated_at=T0,
    )
    higher = _estimate(
        entity_id=TASK_ID,
        estimate_id=higher_id,
        duration_seconds=222,
        estimated_at=T0,
    )

    portfolio_repo = _TrackingPortfolioRepository(_current_portfolio())
    estimate_reader = _TrackingEstimateReader((higher, lower))
    provenance_reader = _TrackingProvenanceReader()

    result = build_effective_work_breakdown_effort_plan_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repo,
        estimate_reader,
        provenance_reader,
    )

    assert result.items[1].direct_estimate == higher
    assert result.items[1].direct_estimate.duration_seconds == 222

    # Only the V1.10-selected UUID is eligible for provenance lookup.
    assert provenance_reader.calls == [higher_id]


def test_durable_performs_zero_lookup_for_out_of_wbs_estimate():
    outside_entity_id = uuid4()
    outside_estimate = _estimate(
        entity_id=outside_entity_id,
    )

    portfolio_repo = _TrackingPortfolioRepository(_current_portfolio())
    estimate_reader = _TrackingEstimateReader((outside_estimate,))
    provenance_reader = _TrackingProvenanceReader()

    result = build_effective_work_breakdown_effort_plan_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repo,
        estimate_reader,
        provenance_reader,
    )

    assert all(item.direct_estimate is None for item in result.items)
    assert provenance_reader.calls == []


def test_durable_performs_zero_lookup_when_wbs_is_unestimated():
    portfolio_repo = _TrackingPortfolioRepository(_current_portfolio())
    estimate_reader = _TrackingEstimateReader(())
    provenance_reader = _TrackingProvenanceReader()

    result = build_effective_work_breakdown_effort_plan_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repo,
        estimate_reader,
        provenance_reader,
    )

    assert all(item.direct_estimate is None for item in result.items)
    assert all(item.calibrated_provenance is None for item in result.items)
    assert provenance_reader.calls == []


def test_durable_reads_each_authoritative_boundary_once():
    estimate_id = uuid4()
    estimate = _estimate(
        entity_id=TASK_ID,
        estimate_id=estimate_id,
    )

    portfolio_repo = _TrackingPortfolioRepository(_current_portfolio())
    estimate_reader = _TrackingEstimateReader((estimate,))
    provenance_reader = _TrackingProvenanceReader()

    build_effective_work_breakdown_effort_plan_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repo,
        estimate_reader,
        provenance_reader,
    )

    assert portfolio_repo.load_calls == [PORTFOLIO_ID]
    assert estimate_reader.portfolio_calls == [PORTFOLIO_ID]
    assert provenance_reader.calls == [estimate_id]


def test_durable_repeated_reads_are_deterministic():
    estimate_id = UUID("40000000-0000-4000-8000-000000000001")
    estimate = _estimate(
        entity_id=TASK_ID,
        estimate_id=estimate_id,
        duration_seconds=450,
    )
    provenance = _record(
        entity_id=TASK_ID,
        estimate_id=estimate_id,
    )

    portfolio = _current_portfolio()

    first = build_effective_work_breakdown_effort_plan_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        _TrackingPortfolioRepository(portfolio),
        _TrackingEstimateReader((estimate,)),
        _TrackingProvenanceReader({estimate_id: provenance}),
    )
    second = build_effective_work_breakdown_effort_plan_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        _TrackingPortfolioRepository(portfolio),
        _TrackingEstimateReader((estimate,)),
        _TrackingProvenanceReader({estimate_id: provenance}),
    )

    assert first == second
