"""Deterministic CURRENT-WBS planned-effort projection (V1.10-D).

This module derives an immutable plan from two existing trusted inputs:

- the CURRENT canonical :class:`Portfolio` and its V1.1 work-breakdown projection;
- durable V1.10 :class:`ExecutionEffortEstimate` values.

Planned effort has explicit **direct-effort** semantics: each estimate is the
planned effort expected directly on its entity itself, excluding effort planned
directly on its WBS descendants. Subtree roll-ups are therefore exact additive
integer sums of per-node latest direct estimates.

The boundary is deliberately pure. It performs no persistence writes, no
wall-clock reads, no provider/AI calls, and no historical WBS reconstruction.
Estimate history for entities that no longer belong to the CURRENT selected WBS
remains valid history, but is excluded from the CURRENT-structure plan.

Coverage is explicit: a subtree whose CURRENT nodes are not all estimated
never exposes a numeric ``total_duration_seconds``. The known sum is still
reported exactly, and the missing coverage stays visible through the
unestimated count.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.work_breakdown import WorkBreakdownNode, build_work_breakdown


class ExecutionEffortPlanningError(ValueError):
    """Raised when planned-effort planning input is invalid."""


class PlannedEffortSummary(BaseModel):
    """Immutable exact planned-effort aggregate with explicit coverage."""

    model_config = ConfigDict(frozen=True, strict=True)

    known_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    estimated_entity_count: Annotated[StrictInt, Field(ge=0)]
    unestimated_entity_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None

    @field_validator("total_duration_seconds", mode="before")
    @classmethod
    def _validate_total_not_bool(
        cls, value: object
    ) -> object:
        if isinstance(value, bool):
            raise ValueError("total_duration_seconds must be an int or None")
        return value

    @model_validator(mode="after")
    def _validate_coverage_consistency(self) -> PlannedEffortSummary:
        if self.unestimated_entity_count == 0:
            if self.total_duration_seconds != self.known_duration_seconds:
                raise ValueError(
                    "a fully estimated subtree total must equal "
                    "the exact known sum"
                )
        elif self.total_duration_seconds is not None:
            raise ValueError(
                "a partially estimated subtree must not expose a "
                "complete numeric total"
            )
        return self


class WorkBreakdownEffortPlanItem(BaseModel):
    """One CURRENT WBS node with its latest direct estimate and subtree summary."""

    model_config = ConfigDict(frozen=True, strict=True)

    entity_id: UUID
    parent_id: UUID | None
    depth: Annotated[StrictInt, Field(ge=0)]
    direct_estimate: ExecutionEffortEstimate | None = None
    subtree: PlannedEffortSummary

    @model_validator(mode="after")
    def _validate_direct_estimate_target(self) -> WorkBreakdownEffortPlanItem:
        if (
            self.direct_estimate is not None
            and self.direct_estimate.entity_id != self.entity_id
        ):
            raise ValueError(
                "direct_estimate must target the item's own entity"
            )
        return self


class WorkBreakdownEffortPlan(BaseModel):
    """Immutable flat pre-order planned-effort plan of one CURRENT project WBS."""

    model_config = ConfigDict(frozen=True, strict=True)

    portfolio_id: UUID
    project_id: UUID
    items: tuple[WorkBreakdownEffortPlanItem, ...]

    @model_validator(mode="after")
    def _validate_item_identity(self) -> WorkBreakdownEffortPlan:
        if not self.items:
            raise ValueError("plan must contain the project root")
        if self.items[0].entity_id != self.project_id:
            raise ValueError("first plan item must be the selected project")
        if self.items[0].parent_id is not None or self.items[0].depth != 0:
            raise ValueError("project root must have parent_id=None and depth=0")

        seen: set[UUID] = set()
        depths: dict[UUID, int] = {}
        for item in self.items:
            if item.entity_id in seen:
                raise ValueError(f"duplicate WBS plan entity: {item.entity_id}")
            seen.add(item.entity_id)
            depths[item.entity_id] = item.depth

            if item.parent_id is None:
                if item.entity_id != self.project_id:
                    raise ValueError("only the project root may have parent_id=None")
                continue

            parent_depth = depths.get(item.parent_id)
            if parent_depth is None:
                raise ValueError("plan parent must precede its child")
            if item.depth != parent_depth + 1:
                raise ValueError("plan item depth must equal parent depth + 1")

        return self


def _revalidate_estimate(candidate: object) -> ExecutionEffortEstimate:
    if not isinstance(candidate, ExecutionEffortEstimate):
        raise ExecutionEffortPlanningError(
            "every estimate must be an ExecutionEffortEstimate instance"
        )

    # Access fields directly instead of serializing the caller-owned instance.
    # This avoids serializer warnings for deliberately hostile ``model_construct``
    # values while still forcing every field back through normal strict validation.
    payload = {
        "id": getattr(candidate, "id", None),
        "portfolio_id": getattr(candidate, "portfolio_id", None),
        "entity_id": getattr(candidate, "entity_id", None),
        "duration_seconds": getattr(candidate, "duration_seconds", None),
        "estimated_at": getattr(candidate, "estimated_at", None),
        "source": getattr(candidate, "source", None),
    }

    try:
        return ExecutionEffortEstimate.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise ExecutionEffortPlanningError(
            "invalid execution-effort estimate supplied to planning"
        ) from exc


def _flatten_wbs_preorder(
    root: WorkBreakdownNode,
) -> list[tuple[UUID, UUID | None, int]]:
    flattened: list[tuple[UUID, UUID | None, int]] = []
    stack: list[tuple[WorkBreakdownNode, UUID | None, int]] = [(root, None, 0)]

    while stack:
        node, parent_id, depth = stack.pop()
        flattened.append((node.entity_id, parent_id, depth))
        for child in reversed(node.children):
            stack.append((child, node.entity_id, depth + 1))

    return flattened


def plan_work_breakdown_effort(
    portfolio: Portfolio,
    project_id: UUID,
    estimates: Iterable[ExecutionEffortEstimate],
) -> WorkBreakdownEffortPlan:
    """Plan direct and subtree planned effort for one CURRENT project WBS.

    The CURRENT V1.1 WBS is authoritative for structure. All supplied estimates
    are revalidated, must belong to ``portfolio.id``, and must have globally
    unique estimate IDs within the supplied set. For each CURRENT WBS entity at
    most one latest effective direct estimate is selected
    (``max(estimated_at instant, estimate id)``); older revisions are never
    summed and input order cannot change the selection. Valid estimates
    attached to entities outside the selected CURRENT WBS are preserved as
    legitimate history but contribute nothing to this plan.
    """

    if not isinstance(portfolio, Portfolio):
        raise ExecutionEffortPlanningError(
            "portfolio must be a Portfolio instance, "
            f"got {type(portfolio).__name__}"
        )
    if not isinstance(project_id, UUID):
        raise ExecutionEffortPlanningError(
            "project_id must be a UUID instance, "
            f"got {type(project_id).__name__}"
        )

    # V1.1 remains the sole authority for CURRENT WBS membership, grammar,
    # ambiguity, cycle detection, and sibling ordering.
    wbs = build_work_breakdown(portfolio, project_id)
    flattened = _flatten_wbs_preorder(wbs.root)
    wbs_ids = {entity_id for entity_id, _, _ in flattened}

    latest: dict[UUID, ExecutionEffortEstimate] = {}
    latest_keys: dict[UUID, tuple[datetime, int]] = {}
    seen_estimate_ids: set[UUID] = set()

    for candidate in estimates:
        estimate = _revalidate_estimate(candidate)

        if estimate.portfolio_id != portfolio.id:
            raise ExecutionEffortPlanningError(
                "estimate belongs to a different portfolio: "
                f"{estimate.id} -> {estimate.portfolio_id}"
            )
        if estimate.id in seen_estimate_ids:
            raise ExecutionEffortPlanningError(
                f"duplicate estimate id in planning input: {estimate.id}"
            )
        seen_estimate_ids.add(estimate.id)

        if estimate.entity_id not in wbs_ids:
            # Historical/out-of-WBS estimates are legitimate history but are
            # excluded from this CURRENT-structure plan.
            continue

        key = (estimate.estimated_at, estimate.id.int)
        current_key = latest_keys.get(estimate.entity_id)
        if current_key is None or key > current_key:
            latest[estimate.entity_id] = estimate
            latest_keys[estimate.entity_id] = key

    known: dict[UUID, int] = {
        entity_id: latest[entity_id].duration_seconds
        if entity_id in latest
        else 0
        for entity_id in wbs_ids
    }
    estimated_count = {
        entity_id: 1 if entity_id in latest else 0 for entity_id in wbs_ids
    }
    unestimated_count = {
        entity_id: 0 if entity_id in latest else 1 for entity_id in wbs_ids
    }

    # Reverse pre-order guarantees a child's complete subtree aggregates exist
    # before they are merged into its parent. The operation is exact integer
    # arithmetic and never mutates the per-node direct estimates.
    for entity_id, parent_id, _ in reversed(flattened):
        if parent_id is not None:
            known[parent_id] += known[entity_id]
            estimated_count[parent_id] += estimated_count[entity_id]
            unestimated_count[parent_id] += unestimated_count[entity_id]

    items = tuple(
        WorkBreakdownEffortPlanItem(
            entity_id=entity_id,
            parent_id=parent_id,
            depth=depth,
            direct_estimate=latest.get(entity_id),
            subtree=PlannedEffortSummary(
                known_duration_seconds=known[entity_id],
                estimated_entity_count=estimated_count[entity_id],
                unestimated_entity_count=unestimated_count[entity_id],
                total_duration_seconds=(
                    known[entity_id]
                    if unestimated_count[entity_id] == 0
                    else None
                ),
            ),
        )
        for entity_id, parent_id, depth in flattened
    )

    return WorkBreakdownEffortPlan(
        portfolio_id=portfolio.id,
        project_id=project_id,
        items=items,
    )
