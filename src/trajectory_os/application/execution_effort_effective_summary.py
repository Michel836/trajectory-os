"""V1.24 - Read-only summary boundary over a V1.23 effective effort plan.

This module answers exactly one question about a single authoritative
``WorkBreakdownEffectiveEffortPlan`` (V1.23):

> What is the authoritative project-level effort/coverage summary, and how many
> selected effective direct estimates are ordinary versus backed by accepted
> calibration provenance?

It is deliberately a *thin* boundary. Every authoritative computation - CURRENT
WBS construction, estimate-history validation, latest-estimate selection, WBS
traversal/order, exact integer subtree arithmetic, estimated/unestimated
coverage, and provenance lookup/matching - is owned by V1.10-D and V1.23.  V1.24
reuses their outputs rather than recomputing any of it.

How V1.24 avoids duplicating validation:

- A *genuine* ``WorkBreakdownEffectiveEffortPlan`` instance is required.
- The supplied plan is freshly re-validated by reading its fields back into a
  raw payload and routing that payload through ``WorkBreakdownEffectiveEffortPlan``
  **strict** validation.  Nested ``ExecutionEffortEstimate``,
  ``AcceptedCalibratedEstimateRevision`` and ``PlannedEffortSummary`` values are
  the same re-validation path V1.23 uses internally, so hostile ``model_construct``
  values are rejected by the existing validators - not by new V1.24 rules.
- The authoritative project-level effort is taken verbatim from the validated
  root item (``items[0].subtree``).
- V1.24 adds exactly one new invariant of its own: the selected estimates must
  partition into ordinary + calibrated, with the sum equal to the authoritative
  root ``estimated_entity_count``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from trajectory_os.application.execution_effort_calibration_acceptance import (
    AcceptedCalibratedEstimateRevision,
)
from trajectory_os.application.execution_effort_effective_plan import (
    CalibrationProvenanceReader,
    WorkBreakdownEffectiveEffortPlan,
    WorkBreakdownEffectivePlanItem,
    build_effective_work_breakdown_effort_plan_durably,
)
from trajectory_os.application.execution_effort_planning import (
    ExecutionEffortEstimateReader,
)
from trajectory_os.application.work_breakdown_acceptance import (
    PortfolioRepository,
)
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
)
from trajectory_os.domain.execution_effort_planning import (
    PlannedEffortSummary,
)

__all__ = [
    "WorkBreakdownEffectiveEffortSummary",
    "WorkBreakdownEffectiveSummaryError",
    "build_effective_work_breakdown_effort_summary_durably",
    "summarize_effective_work_breakdown_effort_plan",
]


class WorkBreakdownEffectiveSummaryError(ValueError):
    """Raised when a supplied plan cannot yield a trustworthy V1.24 summary.

    This is a *read-only* boundary: no state is created, mutated or deleted.
    """


# ---------------------------------------------------------------------------
# Raw-payload builders.
#
# These build plain dictionaries by *reading* already-validated model fields.
# They contain no validation logic of their own; strict ``model_validate`` of the
# assembled payload routes every field through the existing V1.10-D / V1.21 / V1.23
# validators.  Reading attributes on caller-owned instances (instead of dumping
# them) avoids serializer warnings from intentionally malicious
# ``model_construct`` values, while still rejecting those values on re-validation.
# ---------------------------------------------------------------------------


def _estimated_estimate_field(candidate: object) -> object:
    """Expose a selected effective estimate as a raw field payload.

    ``None`` and foreign values are returned unchanged so that strict validation
    of the ``ExecutionEffortEstimate`` field rejects them with its own message.
    """
    if candidate is None:
        return None
    if isinstance(candidate, ExecutionEffortEstimate):
        return {
            "id": candidate.id,
            "portfolio_id": candidate.portfolio_id,
            "entity_id": candidate.entity_id,
            "duration_seconds": candidate.duration_seconds,
            "estimated_at": candidate.estimated_at,
            "source": candidate.source,
        }
    return candidate


def _estimated_provenance_field(candidate: object) -> object:
    """Expose an accepted calibration provenance as a raw field payload.

    ``source_proposal`` is kept as the existing instance and re-validated by
    ``AcceptedCalibratedEstimateRevision``'s own snapshot-consistency validator,
    exactly as V1.23 does.
    """
    if candidate is None:
        return None
    if isinstance(candidate, AcceptedCalibratedEstimateRevision):
        return {
            "estimate_id": candidate.estimate_id,
            "portfolio_id": candidate.portfolio_id,
            "project_id": candidate.project_id,
            "entity_id": candidate.entity_id,
            "entity_type": candidate.entity_type,
            "candidate_duration_seconds": candidate.candidate_duration_seconds,
            "calibrated_duration_seconds": candidate.calibrated_duration_seconds,
            "estimated_at": candidate.estimated_at,
            "source_proposal": candidate.source_proposal,
        }
    return candidate


def _estimated_subtree_field(candidate: object) -> object:
    """Expose a planned-effort subtree summary as a raw field payload."""
    if candidate is None:
        return None
    if isinstance(candidate, PlannedEffortSummary):
        return {
            "known_duration_seconds": candidate.known_duration_seconds,
            "estimated_entity_count": candidate.estimated_entity_count,
            "unestimated_entity_count": candidate.unestimated_entity_count,
            "total_duration_seconds": candidate.total_duration_seconds,
        }
    return candidate


def _estimated_item_field(item: object) -> object:
    """Expose one effective plan item as a raw field payload.

    Foreign item values are returned unchanged so the strict
    ``WorkBreakdownEffectivePlanItem`` field rejects them transitively.
    """
    if item is None:
        return None
    if isinstance(item, WorkBreakdownEffectivePlanItem):
        return {
            "entity_id": item.entity_id,
            "parent_id": item.parent_id,
            "depth": item.depth,
            "direct_estimate": _estimated_estimate_field(item.direct_estimate),
            "calibrated_provenance": _estimated_provenance_field(
                item.calibrated_provenance
            ),
            "subtree": _estimated_subtree_field(item.subtree),
        }
    return item


def _revalidate_effort_summary(candidate: object) -> PlannedEffortSummary:
    """Freshly/strictly re-validate a ``PlannedEffortSummary`` candidate.

    Used to keep the ``WorkBreakdownEffectiveEffortSummary.effort`` field
    self-validating even when the summary is constructed directly.
    """
    payload = _estimated_subtree_field(candidate)
    if not isinstance(payload, dict):
        raise WorkBreakdownEffectiveSummaryError(
            "effort must be a genuine PlannedEffortSummary instance"
        )
    try:
        return PlannedEffortSummary.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise WorkBreakdownEffectiveSummaryError(
            "effort subtree summary failed strict re-validation"
        ) from exc


# ---------------------------------------------------------------------------
# Summary model.
# ---------------------------------------------------------------------------


class WorkBreakdownEffectiveEffortSummary(BaseModel):
    """Immutable V1.24 read-only summary of one authoritative V1.23 plan.

    ``effort`` is the exact V1.10-D root subtree summary.  The two counts are
    V1.24's only derived values: a partition of the root's selected estimates into
    ordinary and calibrated.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    project_id: UUID
    effort: PlannedEffortSummary
    ordinary_estimate_count: Annotated[StrictInt, Field(ge=0)]
    calibrated_estimate_count: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def _validate_summary(self) -> WorkBreakdownEffectiveEffortSummary:
        # Keep the ``effort`` field self-validating: a hostile ``model_construct``
        # subtree must be rejected by re-validation, not trusted as an instance.
        effort = _revalidate_effort_summary(self.effort)

        # V1.24's single new invariant: selected estimates partition exactly into
        # ordinary and calibrated, whose sum matches the authoritative coverage.
        selected = self.ordinary_estimate_count + self.calibrated_estimate_count
        if selected != effort.estimated_entity_count:
            raise ValueError(
                "ordinary_estimate_count + calibrated_estimate_count must equal "
                "effort.estimated_entity_count"
            )

        return self


# ---------------------------------------------------------------------------
# Pure summary boundary.
# ---------------------------------------------------------------------------


def summarize_effective_work_breakdown_effort_plan(
    plan: WorkBreakdownEffectiveEffortPlan,
) -> WorkBreakdownEffectiveEffortSummary:
    """Summarize one genuine V1.23 effective plan without recomputation.

    Steps:
      1. require a genuine ``WorkBreakdownEffectiveEffortPlan`` instance;
      2. freshly/strictly re-validate the whole supplied plan (rejects hostile
         ``model_construct`` values);
      3. read the authoritative project-level effort from the validated root
         item's subtree;
      4. classify each already-selected estimate as ordinary or calibrated;
      5. return an immutable summary.

    No I/O, no writes, no planning, no provenance lookups: everything is derived
    from the caller-supplied, now-re-validated plan.
    """
    if not isinstance(plan, WorkBreakdownEffectiveEffortPlan):
        raise WorkBreakdownEffectiveSummaryError(
            "a genuine V1.23 WorkBreakdownEffectiveEffortPlan instance is required"
        )

    items = plan.items
    if not isinstance(items, (tuple, list)):
        raise WorkBreakdownEffectiveSummaryError(
            "effective plan items must be an ordered sequence"
        )

    payload = {
        "portfolio_id": plan.portfolio_id,
        "project_id": plan.project_id,
        "items": tuple(_estimated_item_field(item) for item in items),
    }

    try:
        validated = WorkBreakdownEffectiveEffortPlan.model_validate(
            payload, strict=True
        )
    except ValidationError as exc:
        raise WorkBreakdownEffectiveSummaryError(
            "supplied effective plan failed strict re-validation"
        ) from exc

    # V1.23 enforces root-first ordering, so items[0] is the authoritative root.
    root_effort = validated.items[0].subtree
    ordinary = sum(
        1
        for item in validated.items
        if item.direct_estimate is not None and item.calibrated_provenance is None
    )
    calibrated = sum(
        1
        for item in validated.items
        if item.direct_estimate is not None and item.calibrated_provenance is not None
    )

    return WorkBreakdownEffectiveEffortSummary(
        portfolio_id=validated.portfolio_id,
        project_id=validated.project_id,
        effort=root_effort,
        ordinary_estimate_count=ordinary,
        calibrated_estimate_count=calibrated,
    )


# ---------------------------------------------------------------------------
# Durable read-only composition.
# ---------------------------------------------------------------------------


def _require_uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise WorkBreakdownEffectiveSummaryError(
            f"{name} must be a genuine UUID instance"
        )
    return value


def build_effective_work_breakdown_effort_summary_durably(
    portfolio_id: UUID,
    project_id: UUID,
    portfolio_repository: PortfolioRepository,
    estimate_reader: ExecutionEffortEstimateReader,
    provenance_reader: CalibrationProvenanceReader,
) -> WorkBreakdownEffectiveEffortSummary:
    """Build the V1.24 summary by delegating exactly once to V1.23.

    Sequence: strict public identifier validation -> authorize the single V1.23
    durable effective-plan builder -> pass its exact returned plan to the pure
    V1.24 summary boundary -> return the immutable summary -> STOP.

    No second portfolio load, estimate-history read, provenance lookup, WBS
    planning pass, or persistence operation is performed here.  Repository,
    estimate-reader and provenance-reader exceptions raised by the delegated
    V1.23 builder propagate unchanged.
    """
    portfolio_id = _require_uuid(portfolio_id, "portfolio_id")
    project_id = _require_uuid(project_id, "project_id")

    plan = build_effective_work_breakdown_effort_plan_durably(
        portfolio_id=portfolio_id,
        project_id=project_id,
        portfolio_repository=portfolio_repository,
        estimate_reader=estimate_reader,
        provenance_reader=provenance_reader,
    )

    return summarize_effective_work_breakdown_effort_plan(plan)
