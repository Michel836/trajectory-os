"""Read-only effective work-breakdown plan with optional calibrated provenance (V1.23).

This module adds a *pure composition boundary* that enriches one authoritative
:class:`~trajectory_os.domain.execution_effort_planning.WorkBreakdownEffortPlan`
(V1.10-D) with optional exact
:class:`~trajectory_os.application.execution_effort_calibration_acceptance.AcceptedCalibratedEstimateRevision`
(V1.21) provenance for every selected
:class:`~trajectory_os.domain.execution_effort_estimates.ExecutionEffortEstimate`,
**without** duplicating ANY planning, selection, or arithmetic logic.

The domain models carry their own strict Pydantic validation; the durable
orchestrator delegates all portfolio loading, estimate history reads and pure
planning to existing authoritative boundaries.
"""

from __future__ import annotations

from typing import Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from trajectory_os.application.execution_effort_calibration_acceptance import (
    AcceptedCalibratedEstimateRevision,
)
from trajectory_os.application.execution_effort_planning import ExecutionEffortEstimateReader
from trajectory_os.application.work_breakdown_acceptance import PortfolioRepository
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.execution_effort_planning import (
    PlannedEffortSummary,
    WorkBreakdownEffortPlan,
    WorkBreakdownEffortPlanItem,
    plan_work_breakdown_effort,
)


class WorkBreakdownEffectivePlanItem(BaseModel):
    """Immutable enriched WBS item — fresh re-validated wrapper."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    entity_id: UUID
    parent_id: UUID | None
    depth: int
    direct_estimate: ExecutionEffortEstimate | None = None
    calibrated_provenance: AcceptedCalibratedEstimateRevision | None = None
    subtree: PlannedEffortSummary

    @model_validator(mode="after")
    def _validate_effective_item(self) -> Self:
        estimate = self.direct_estimate
        provenance = self.calibrated_provenance

        if estimate is not None:
            estimate = _revalidate_direct_estimate(estimate)
            if estimate.entity_id != self.entity_id:
                raise ValueError(
                    "direct_estimate.entity_id must match effective-plan item entity_id"
                )

        if provenance is None:
            return self

        if estimate is None:
            raise ValueError(
                "calibrated_provenance requires a direct_estimate"
            )

        provenance = _revalidate_provenance(provenance)

        _assert_provenance_matches_estimate(
            provenance,
            estimate.id,
            portfolio_id=estimate.portfolio_id,
            entity_id=self.entity_id,
            calibrated_duration_seconds=estimate.duration_seconds,
            estimated_at=estimate.estimated_at,
        )

        return self


class WorkBreakdownEffectiveEffortPlan(BaseModel):
    """Immutable flat pre-order effective-effort plan with optional provenance.

    Preserves the *exact* V1.10-D item count, item order, and all numeric
    values; calibrated_provenance is the only additional information per item.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    project_id: UUID
    items: tuple[WorkBreakdownEffectivePlanItem, ...]

    @model_validator(mode="after")
    def _validate_effective_plan(self) -> Self:
        # Reuse the authoritative V1.10-D structural invariants rather than
        # duplicating preorder/parent/depth/root semantics here.
        WorkBreakdownEffortPlan.model_validate(
            {
                "portfolio_id": self.portfolio_id,
                "project_id": self.project_id,
                "items": tuple(
                    {
                        "entity_id": item.entity_id,
                        "parent_id": item.parent_id,
                        "depth": item.depth,
                        "direct_estimate": item.direct_estimate,
                        "subtree": item.subtree,
                    }
                    for item in self.items
                ),
            },
            strict=True,
        )

        for item in self.items:
            provenance = item.calibrated_provenance
            if provenance is None:
                continue

            if provenance.portfolio_id != self.portfolio_id:
                raise ValueError(
                    "calibrated provenance portfolio_id must match effective plan"
                )
            if provenance.project_id != self.project_id:
                raise ValueError(
                    "calibrated provenance project_id must match effective plan"
                )

        return self


# ---------------------------------------------------------------------------
# Provenance reader protocol for V1.21
# ---------------------------------------------------------------------------


class CalibrationProvenanceReader(Protocol):
    """Thin read-only access to accepted calibrated-estimate revisions."""

    def get_provenance(self, estimate_id: UUID) -> AcceptedCalibratedEstimateRevision | None:
        """Return provenance for one estimate ID, or ``None``."""

        ...


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class WorkBreakdownEffectivePlanError(ValueError):
    """Raised when the effective-plan enrichment input is invalid."""


# ---------------------------------------------------------------------------
# Pure composition boundary (no I/O)
# ---------------------------------------------------------------------------


def _revalidate_provenance(candidate: object) -> AcceptedCalibratedEstimateRevision:
    if not isinstance(candidate, AcceptedCalibratedEstimateRevision):
        raise WorkBreakdownEffectivePlanError(
            "every provenance must be an AcceptedCalibratedEstimateRevision instance"
        )

    payload = {
        "estimate_id": getattr(candidate, "estimate_id", None),
        "portfolio_id": getattr(candidate, "portfolio_id", None),
        "project_id": getattr(candidate, "project_id", None),
        "entity_id": getattr(candidate, "entity_id", None),
        "entity_type": getattr(candidate, "entity_type", None),
        "candidate_duration_seconds": getattr(
            candidate, "candidate_duration_seconds", None
        ),
        "calibrated_duration_seconds": getattr(
            candidate, "calibrated_duration_seconds", None
        ),
        "estimated_at": getattr(candidate, "estimated_at", None),
        "source_proposal": getattr(candidate, "source_proposal", None),
    }

    try:
        return AcceptedCalibratedEstimateRevision.model_validate(
            payload,
            strict=True,
        )
    except ValidationError as exc:
        raise WorkBreakdownEffectivePlanError(
            "rejected invalid provenance during enrichment validation"
        ) from exc


def _revalidate_direct_estimate(candidate: object) -> ExecutionEffortEstimate:
    if not isinstance(candidate, ExecutionEffortEstimate):
        raise WorkBreakdownEffectivePlanError(
            "every direct_estimate must be an ExecutionEffortEstimate instance"
        )

    payload = {
        "id": getattr(candidate, "id", None),
        "portfolio_id": getattr(candidate, "portfolio_id", None),
        "entity_id": getattr(candidate, "entity_id", None),
        "duration_seconds": getattr(candidate, "duration_seconds", None),
        "estimated_at": getattr(candidate, "estimated_at", None),
        "source": getattr(candidate, "source", None),
    }

    try:
        return ExecutionEffortEstimate.model_validate(
            payload,
            strict=True,
        )
    except ValidationError as exc:
        raise WorkBreakdownEffectivePlanError(
            "rejected invalid direct_estimate during enrichment validation"
        ) from exc


def _assert_provenance_matches_estimate(
    provenance: AcceptedCalibratedEstimateRevision, estimate_id: UUID, **fields: object
) -> None:
    """Assert structural consistency between provenance estimate and the plan item."""
    if provenance.estimate_id != estimate_id:
        raise WorkBreakdownEffectivePlanError(
            f"provenance.estimate_id {provenance.estimate_id} does not match "
            f"estimated entity id {estimate_id}"
        )
    for field_name, value in fields.items():
        got = getattr(provenance, field_name, None)
        if got != value:
            raise WorkBreakdownEffectivePlanError(
                f"provenance.{field_name} {got} does not match expected {value}"
            )


def enrich_work_breakdown_effort_plan_with_calibration_provenance(
    plan: WorkBreakdownEffortPlan,
    provenances: dict[UUID, AcceptedCalibratedEstimateRevision],
) -> WorkBreakdownEffectiveEffortPlan:
    """Enrich an authoritative V1.10-D plan with optional exact V1.21 provenance.

    Parameters are freshly validated so hostile ``model_construct()`` instances
    are never trusted — even the ``plan`` is re-validated end-to-end.

    Invariants:

    - provenance rows must belong to the *same* portfolio as the plan;
    - duplicate ``estimate_id`` values among supplied provenances are rejected;
    - provenance for an estimate not selected in this plan raises rather than
      silently contaminating;
    - every selected estimate gets *either* its exact matching provenance *or* ``None``;
    - unestimated items always get provenance ``None``.

    The enriched plan preserves: item count, item order, entity and parent ids,
    depth, direct_estimate values (all fields), and subtree summaries exactly.
    """

    # --- Re-validate the incoming domain objects ---------------------------
    if not isinstance(plan, WorkBreakdownEffortPlan):
        raise WorkBreakdownEffectivePlanError(
            "plan must be a WorkBreakdownEffortPlan instance, "
            f"got {type(plan).__name__}"
        )

    try:
        raw_items = getattr(plan, "items", None)
        if not isinstance(raw_items, tuple):
            raise WorkBreakdownEffectivePlanError(
                "plan.items must be a tuple of WorkBreakdownEffortPlanItem instances"
            )

        item_payloads: list[dict[str, object]] = []
        for item in raw_items:
            if not isinstance(item, WorkBreakdownEffortPlanItem):
                raise WorkBreakdownEffectivePlanError(
                    "every plan item must be a WorkBreakdownEffortPlanItem instance"
                )

            subtree = getattr(item, "subtree", None)
            if not isinstance(subtree, PlannedEffortSummary):
                raise WorkBreakdownEffectivePlanError(
                    "every plan item subtree must be a PlannedEffortSummary instance"
                )

            direct_estimate = getattr(item, "direct_estimate", None)
            validated_direct_estimate = (
                None
                if direct_estimate is None
                else _revalidate_direct_estimate(direct_estimate)
            )

            item_payloads.append(
                {
                    "entity_id": getattr(item, "entity_id", None),
                    "parent_id": getattr(item, "parent_id", None),
                    "depth": getattr(item, "depth", None),
                    "direct_estimate": validated_direct_estimate,
                    "subtree": {
                        "known_duration_seconds": getattr(
                            subtree, "known_duration_seconds", None
                        ),
                        "estimated_entity_count": getattr(
                            subtree, "estimated_entity_count", None
                        ),
                        "unestimated_entity_count": getattr(
                            subtree, "unestimated_entity_count", None
                        ),
                        "total_duration_seconds": getattr(
                            subtree, "total_duration_seconds", None
                        ),
                    },
                }
            )

        revalidated_plan = WorkBreakdownEffortPlan.model_validate(
            {
                "portfolio_id": getattr(plan, "portfolio_id", None),
                "project_id": getattr(plan, "project_id", None),
                "items": tuple(item_payloads),
            },
            strict=True,
        )
    except ValidationError as exc:
        raise WorkBreakdownEffectivePlanError(
            "rejected invalid work-breakdown effort plan during enrichment validation"
        ) from exc

    # --- Deduplicate provenance by estimate_id ----------------------------
    seen_provenance_estimate_ids: set[UUID] = set()
    validated_provenances_by_id: dict[UUID, AcceptedCalibratedEstimateRevision] = {}
    for prov in provenances.values():
        actual = _revalidate_provenance(prov)

        if actual.estimate_id in seen_provenance_estimate_ids:
            raise WorkBreakdownEffectivePlanError(
                f"duplicate provenance estimate_id: {actual.estimate_id}"
            )
        seen_provenance_estimate_ids.add(actual.estimate_id)

        # Cross-check portfolio identity against the plan's authoritative claim.
        if actual.portfolio_id != revalidated_plan.portfolio_id:
            raise WorkBreakdownEffectivePlanError(
                f"provenance portfolio {actual.portfolio_id} "
                f"does not match plan portfolio {revalidated_plan.portfolio_id}"
            )
        validated_provenances_by_id[actual.estimate_id] = actual

    selected_estimate_ids = {
        item.direct_estimate.id
        for item in revalidated_plan.items
        if item.direct_estimate is not None
    }

    foreign_provenance_ids = (
        set(validated_provenances_by_id) - selected_estimate_ids
    )
    if foreign_provenance_ids:
        raise WorkBreakdownEffectivePlanError(
            "provenance supplied for a non-selected execution-effort estimate"
        )

    # --- Enrich each item ------------------------------------------------
    enriched_items: list[WorkBreakdownEffectivePlanItem] = []
    for item in revalidated_plan.items:
        # --- Re-validate direct_estimate fields ---------------------------
        estimate_payload = None
        if item.direct_estimate is not None:
            # Validate the direct estimate using strict pattern from V1.22
            validated_direct_estimate = _revalidate_direct_estimate(item.direct_estimate)

            estimate_payload = {
                "id": validated_direct_estimate.id,
                "portfolio_id": validated_direct_estimate.portfolio_id,
                "entity_id": item.entity_id,
                "duration_seconds": validated_direct_estimate.duration_seconds,
                "estimated_at": validated_direct_estimate.estimated_at,
                "source": validated_direct_estimate.source,
            }

        validated_item = WorkBreakdownEffectivePlanItem.model_validate(
            {
                "entity_id": item.entity_id,
                "parent_id": item.parent_id,
                "depth": item.depth,
                "direct_estimate": estimate_payload,
                "subtree": {
                    "known_duration_seconds": item.subtree.known_duration_seconds,
                    "estimated_entity_count": item.subtree.estimated_entity_count,
                    "unestimated_entity_count": item.subtree.unestimated_entity_count,
                    "total_duration_seconds": item.subtree.total_duration_seconds,
                },
            }
        )

        # --- Attach provenance if the selected estimate has one ---------
        provenance_value = None
        selected_estimate = validated_item.direct_estimate
        if selected_estimate is not None:
            est_id_for_provenance = selected_estimate.id
            prov_candidate = validated_provenances_by_id.get(est_id_for_provenance)
            if prov_candidate is not None:
                validated_prov = _revalidate_provenance(prov_candidate)

                # Tighten: all structural identifiers must match the estimate.
                _assert_provenance_matches_estimate(
                    validated_prov,
                    est_id_for_provenance,
                    portfolio_id=selected_estimate.portfolio_id,
                    entity_id=item.entity_id,
                    calibrated_duration_seconds=selected_estimate.duration_seconds,
                    estimated_at=selected_estimate.estimated_at,
                )
                provenance_value = validated_prov

        enriched_items.append(
            WorkBreakdownEffectivePlanItem(
                entity_id=validated_item.entity_id,
                parent_id=validated_item.parent_id,
                depth=validated_item.depth,
                direct_estimate=validated_item.direct_estimate,
                calibrated_provenance=provenance_value,
                subtree=validated_item.subtree,
            )
        )

    return WorkBreakdownEffectiveEffortPlan(
        portfolio_id=revalidated_plan.portfolio_id,
        project_id=revalidated_plan.project_id,
        items=tuple(enriched_items),
    )


# ---------------------------------------------------------------------------


def build_effective_work_breakdown_effort_plan_durably(
    portfolio_id: UUID,
    project_id: UUID,
    portfolio_repository: PortfolioRepository,
    estimate_reader: ExecutionEffortEstimateReader,
    provenance_reader: CalibrationProvenanceReader,
) -> WorkBreakdownEffectiveEffortPlan:
    """Build an effective plan through existing authoritative boundaries.

    Exact sequence:

    1. validate portfolio_id / project_id;
    2. load CURRENT Portfolio via the provided repository (unchanged V1.10-D convention);
    3. read V1.10 estimate history once via the reader port;
    4. build authoritative V1.10-D WorkBreakdownEffortPlan **once**
       (``plan_work_breakdown_effort`` — never reimplemented);
    5. collect only selected direct_estimate IDs from that plan;
    6. for *each* selected estimate ID, query provenance exactly once;
    7. freshly validate each returned provenance row inside the enrichment function;
    8. pure V1.23 enrichment;
    9. return immutable enriched plan;
    10. STOP (no writes, no saves).
    """

    # --- Step 1: validate inputs -------------------------------------------
    if not isinstance(portfolio_id, UUID):
        raise WorkBreakdownEffectivePlanError(
            "portfolio_id must be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )
    if not isinstance(project_id, UUID):
        raise WorkBreakdownEffectivePlanError(
            "project_id must be a UUID instance, "
            f"got {type(project_id).__name__}"
        )

    # --- Step 2: load CURRENT Portfolio (unchanged V1.10-D) -----------------
    current = portfolio_repository.load(portfolio_id)
    if current is None:
        from trajectory_os.application.execution_effort_planning import (  # noqa: E402, F401
            ExecutionEffortPlanningPortfolioNotFoundError,
        )

        raise ExecutionEffortPlanningPortfolioNotFoundError(
            f"portfolio not found: {portfolio_id}"
        )

    # --- Step 3-4: read estimates and build the authoritative plan ----------
    estimates = estimate_reader.list_for_portfolio(portfolio_id)
    effective_plan = plan_work_breakdown_effort(current, project_id, estimates)

    # --- Step 5: collect selected direct_estimate IDs -----------------------
    selected_estimate_ids: set[UUID] = {
        item.direct_estimate.id
        for item in effective_plan.items
        if item.direct_estimate is not None
    }

    # --- Step 6-7: query provenance for each selected estimate --------------
    provenances_by_id: dict[UUID, AcceptedCalibratedEstimateRevision] = {}
    for est_id in selected_estimate_ids:
        prov = provenance_reader.get_provenance(est_id)
        if prov is not None:
            provenances_by_id[est_id] = _revalidate_provenance(prov)

    # --- Step 8-9: pure enrichment & return --------------------------------
    return enrich_work_breakdown_effort_plan_with_calibration_provenance(
        effective_plan, provenances_by_id
    )
