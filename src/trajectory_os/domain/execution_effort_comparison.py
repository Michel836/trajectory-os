"""Deterministic planned-vs-actual effort comparison (V1.11).

This module derives an immutable comparison from two existing trusted inputs:

- a V1.10 :class:`WorkBreakdownEffortPlan` (planned direct effort with explicit coverage);
- a V1.9 :class:`WorkBreakdownEffortMeasurement` (actual observed effort).

The boundary is deliberately pure. It performs no persistence writes, no wall-clock
reads, no provider/AI calls, no WBS reconstruction, and no re-interpretation of
estimate or observation semantics. It only aligns already-derived results and
computes exact signed integer variances where semantically valid.

Signed variance convention: ``variance_seconds = actual - planned``.
Positive means actual exceeded plan; negative means actual is below plan.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    model_validator,
)

from trajectory_os.domain.execution_effort_measurement import (
    WorkBreakdownEffortMeasurement,
    WorkBreakdownEffortMeasurementItem,
)
from trajectory_os.domain.execution_effort_planning import (
    WorkBreakdownEffortPlan,
    WorkBreakdownEffortPlanItem,
)


class ExecutionEffortComparisonError(ValueError):
    """Raised when planned-vs-actual comparison input is invalid."""


class EffortVariance(BaseModel):
    """Immutable signed variance between planned and actual effort in seconds."""

    model_config = ConfigDict(frozen=True, strict=True)

    planned_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    actual_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    variance_seconds: StrictInt | None = None

    @model_validator(mode="after")
    def _validate_variance_consistency(self) -> EffortVariance:
        if self.planned_duration_seconds is None:
            if self.variance_seconds is not None:
                raise ValueError(
                    "unknown planned duration requires None variance"
                )
        else:
            expected = self.actual_duration_seconds - self.planned_duration_seconds
            if self.variance_seconds != expected:
                raise ValueError(
                    "variance_seconds must equal actual - planned"
                )
        return self


class WorkBreakdownEffortComparisonItem(BaseModel):
    """One CURRENT WBS node with direct and subtree planned-vs-actual variance."""

    model_config = ConfigDict(frozen=True, strict=True)

    entity_id: UUID
    parent_id: UUID | None
    depth: Annotated[StrictInt, Field(ge=0)]
    direct: EffortVariance
    subtree: EffortVariance
    planned_estimated_entity_count: Annotated[StrictInt, Field(ge=0)]
    planned_unestimated_entity_count: Annotated[StrictInt, Field(ge=0)]


class WorkBreakdownEffortComparison(BaseModel):
    """Immutable flat pre-order planned-vs-actual comparison of one CURRENT project WBS."""

    model_config = ConfigDict(frozen=True, strict=True)

    portfolio_id: UUID
    project_id: UUID
    items: tuple[WorkBreakdownEffortComparisonItem, ...]

    @model_validator(mode="after")
    def _validate_item_identity(self) -> WorkBreakdownEffortComparison:
        if not self.items:
            raise ValueError("comparison must contain the project root")
        if self.items[0].entity_id != self.project_id:
            raise ValueError("first comparison item must be the selected project")
        if self.items[0].parent_id is not None or self.items[0].depth != 0:
            raise ValueError("project root must have parent_id=None and depth=0")

        seen: set[UUID] = set()
        depths: dict[UUID, int] = {}
        for item in self.items:
            if item.entity_id in seen:
                raise ValueError(f"duplicate WBS comparison entity: {item.entity_id}")
            seen.add(item.entity_id)
            depths[item.entity_id] = item.depth

            if item.parent_id is None:
                if item.entity_id != self.project_id:
                    raise ValueError("only the project root may have parent_id=None")
                continue

            parent_depth = depths.get(item.parent_id)
            if parent_depth is None:
                raise ValueError("comparison parent must precede its child")
            if item.depth != parent_depth + 1:
                raise ValueError("comparison item depth must equal parent depth + 1")

        return self


def _revalidate_plan(candidate: object) -> WorkBreakdownEffortPlan:
    if not isinstance(candidate, WorkBreakdownEffortPlan):
        raise ExecutionEffortComparisonError(
            "plan must be a WorkBreakdownEffortPlan instance, "
            f"got {type(candidate).__name__}"
        )
    try:
        return WorkBreakdownEffortPlan.model_validate(
            candidate.model_dump(), strict=True
        )
    except Exception as exc:
        raise ExecutionEffortComparisonError(
            "invalid WorkBreakdownEffortPlan supplied to comparison"
        ) from exc


def _revalidate_measurement(
    candidate: object,
) -> WorkBreakdownEffortMeasurement:
    if not isinstance(candidate, WorkBreakdownEffortMeasurement):
        raise ExecutionEffortComparisonError(
            "measurement must be a WorkBreakdownEffortMeasurement instance, "
            f"got {type(candidate).__name__}"
        )
    try:
        return WorkBreakdownEffortMeasurement.model_validate(
            candidate.model_dump(), strict=True
        )
    except Exception as exc:
        raise ExecutionEffortComparisonError(
            "invalid WorkBreakdownEffortMeasurement supplied to comparison"
        ) from exc


def _validate_structural_alignment(
    plan: WorkBreakdownEffortPlan,
    measurement: WorkBreakdownEffortMeasurement,
) -> None:
    if plan.portfolio_id != measurement.portfolio_id:
        raise ExecutionEffortComparisonError(
            "plan and measurement must share the same portfolio_id: "
            f"{plan.portfolio_id} != {measurement.portfolio_id}"
        )
    if plan.project_id != measurement.project_id:
        raise ExecutionEffortComparisonError(
            "plan and measurement must share the same project_id: "
            f"{plan.project_id} != {measurement.project_id}"
        )
    if len(plan.items) != len(measurement.items):
        raise ExecutionEffortComparisonError(
            "plan and measurement must have the same item count: "
            f"{len(plan.items)} != {len(measurement.items)}"
        )

    for i, (p_item, m_item) in enumerate(zip(plan.items, measurement.items, strict=True)):
        if p_item.entity_id != m_item.entity_id:
            raise ExecutionEffortComparisonError(
                f"item {i}: entity_id mismatch: "
                f"{p_item.entity_id} != {m_item.entity_id}"
            )
        if p_item.parent_id != m_item.parent_id:
            raise ExecutionEffortComparisonError(
                f"item {i}: parent_id mismatch: "
                f"{p_item.parent_id} != {m_item.parent_id}"
            )
        if p_item.depth != m_item.depth:
            raise ExecutionEffortComparisonError(
                f"item {i}: depth mismatch: "
                f"{p_item.depth} != {m_item.depth}"
            )


def _compute_direct_variance(
    plan_item: WorkBreakdownEffortPlanItem,
    measurement_item: WorkBreakdownEffortMeasurementItem,
) -> EffortVariance:
    actual = measurement_item.direct.duration_seconds

    if plan_item.direct_estimate is None:
        return EffortVariance(
            planned_duration_seconds=None,
            actual_duration_seconds=actual,
            variance_seconds=None,
        )

    planned = plan_item.direct_estimate.duration_seconds
    return EffortVariance(
        planned_duration_seconds=planned,
        actual_duration_seconds=actual,
        variance_seconds=actual - planned,
    )


def _compute_subtree_variance(
    plan_item: WorkBreakdownEffortPlanItem,
    measurement_item: WorkBreakdownEffortMeasurementItem,
) -> EffortVariance:
    actual = measurement_item.subtree.duration_seconds
    planned_total = plan_item.subtree.total_duration_seconds

    if planned_total is None:
        return EffortVariance(
            planned_duration_seconds=None,
            actual_duration_seconds=actual,
            variance_seconds=None,
        )

    return EffortVariance(
        planned_duration_seconds=planned_total,
        actual_duration_seconds=actual,
        variance_seconds=actual - planned_total,
    )


def compare_work_breakdown_effort(
    plan: WorkBreakdownEffortPlan,
    measurement: WorkBreakdownEffortMeasurement,
) -> WorkBreakdownEffortComparison:
    """Compare planned vs actual effort for each CURRENT WBS node.

    Both inputs must be valid, structurally aligned V1.10/V1.9 results over
    the same portfolio, project, and WBS structure. The comparison is pure:
    it computes exact signed integer variances where semantically valid and
    preserves planning uncertainty (incomplete coverage) without fabricating
    complete variances.
    """

    validated_plan = _revalidate_plan(plan)
    validated_measurement = _revalidate_measurement(measurement)

    _validate_structural_alignment(validated_plan, validated_measurement)

    items = tuple(
        WorkBreakdownEffortComparisonItem(
            entity_id=p_item.entity_id,
            parent_id=p_item.parent_id,
            depth=p_item.depth,
            direct=_compute_direct_variance(p_item, m_item),
            subtree=_compute_subtree_variance(p_item, m_item),
            planned_estimated_entity_count=p_item.subtree.estimated_entity_count,
            planned_unestimated_entity_count=p_item.subtree.unestimated_entity_count,
        )
        for p_item, m_item in zip(validated_plan.items, validated_measurement.items, strict=True)
    )

    return WorkBreakdownEffortComparison(
        portfolio_id=validated_plan.portfolio_id,
        project_id=validated_plan.project_id,
        items=items,
    )
