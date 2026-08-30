"""V1.26 — Pure per-project effort-contribution projection over V1.25.

V1.25 owns the portfolio-level aggregate: it composes and validates the
per-project effective-effort summaries into ``PortfolioEffectiveEffortSummary``.
V1.24 owns each project summary and its ``PlannedEffortSummary`` coverage
semantics.

V1.26 is a *projection*, not a recomputation. It takes exactly one input — a
genuine, authoritative ``PortfolioEffectiveEffortSummary`` — and copies the
flat per-project contribution fields out of it, preserving the authoritative
project order. It creates no estimates, no calibration provenance, no
provenance, no projects or tasks, and no decisions. It reads no repositories,
inspects no WBS, recomputes no effort, performs no rounding or relative
measurement (no percentages, ratios, shares, or rankings), and writes nothing
of any kind.

Single pure boundary:

``project_portfolio_effort_contributions(summary)`` requires a genuine
``PortfolioEffectiveEffortSummary``, freshly and strictly re-validates its
complete payload — including every nested V1.24 project summary and its
``PlannedEffortSummary`` subtree — and then traverses
``summary.projects`` exactly once, in order, copying exactly:

* ``project_id``
* ``known_duration_seconds``
* ``estimated_entity_count``
* ``unestimated_entity_count``
* ``ordinary_estimate_count``
* ``calibrated_estimate_count``
* ``total_duration_seconds`` (including ``None``)

into the strict, frozen ``PortfolioProjectEffortContribution`` entries of the
result.

Invariants of the result:

* ``result.portfolio_id == summary.portfolio_id``
* ``result.project_count == summary.project_count == len(result.projects)``
* an empty V1.25 summary projects to ``project_count == 0`` and
  ``projects == ()``
* project IDs preserve the authoritative V1.25 order and are unique (they are
  unique in the re-validated input).

A hostile ``model_construct`` V1.25 summary — including model-constructed
nested V1.24 project summaries and ``PlannedEffortSummary`` values that bypass
their construction invariants — is rejected by strict re-validation, not
trusted.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from trajectory_os.application.execution_effort_effective_summary import (
    WorkBreakdownEffectiveEffortSummary,
)
from trajectory_os.application.execution_effort_portfolio_summary import (
    PortfolioEffectiveEffortSummary,
)
from trajectory_os.domain.execution_effort_planning import PlannedEffortSummary

__all__ = [
    "PortfolioProjectEffortContribution",
    "PortfolioProjectEffortContributionError",
    "PortfolioProjectEffortContributionSummary",
    "project_portfolio_effort_contributions",
]


class PortfolioProjectEffortContributionError(Exception):
    """Raised when the V1.26 contribution projection boundary sees invalid input.

    This is the sole failure channel of the V1.26 boundary itself.
    """


class PortfolioProjectEffortContribution(BaseModel):
    """Strict, frozen flat per-project effort contribution.

    A verbatim copy of the flat fields of one authoritative V1.24 project
    summary (its ``PlannedEffortSummary`` coverage fields alongside its
    ordinary/calibrated partition).

    V1.26 is a *projection*: ``total_duration_seconds`` — including its
    authoritative ``None`` value — is copied exactly from the source. This model
    deliberately does NOT infer or reinterpret project completeness from
    ``unestimated_entity_count`` (or any other coverage field). It only
    re-validates pure structural invariants (strict types, non-negativity, and
    the ordinary/calibrated partition of the estimated count) so a hostile
    ``model_construct`` value must still be rejected on re-validation. The
    completeness semantics of the authoritative source remain owned solely by
    V1.24's ``PlannedEffortSummary``.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    project_id: UUID
    known_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    estimated_entity_count: Annotated[StrictInt, Field(ge=0)]
    unestimated_entity_count: Annotated[StrictInt, Field(ge=0)]
    ordinary_estimate_count: Annotated[StrictInt, Field(ge=0)]
    calibrated_estimate_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _validate_contribution_invariants(self) -> PortfolioProjectEffortContribution:
        # Structural invariants only. V1.26 must NOT infer completeness from
        # unestimated_entity_count: total_duration_seconds is copied verbatim
        # from the authoritative V1.24 summary (including None).
        if (
            self.ordinary_estimate_count + self.calibrated_estimate_count
            != self.estimated_entity_count
        ):
            raise ValueError(
                "ordinary_estimate_count + calibrated_estimate_count must equal "
                "estimated_entity_count"
            )
        return self


class PortfolioProjectEffortContributionSummary(BaseModel):
    """Strict, frozen ordered portfolio of per-project effort contributions.

    ``projects`` preserves the exact order of the authoritative V1.25
    summary. All invariants below are re-validated on construction, so a
    hostile ``model_construct`` contribution subtree must be rejected by
    re-validation, not trusted.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    project_count: Annotated[StrictInt, Field(ge=0)]
    projects: tuple[PortfolioProjectEffortContribution, ...]

    @model_validator(mode="after")
    def _validate_summary_invariants(self) -> PortfolioProjectEffortContributionSummary:
        contributions: list[PortfolioProjectEffortContribution] = []
        for entry in self.projects:
            try:
                revalidated = _revalidate_contribution(entry)
            except PortfolioProjectEffortContributionError as exc:
                # Surface uniformly as a model ValidationError at the model edge.
                raise ValueError(str(exc)) from exc
            contributions.append(revalidated)

        if self.project_count != len(contributions):
            raise ValueError(
                f"project_count={self.project_count} does not equal the number "
                f"of contributions ({len(contributions)})"
            )

        project_ids = [contribution.project_id for contribution in contributions]
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("duplicate project IDs are not allowed")

        return self


def _revalidate_contribution(
    candidate: object,
) -> PortfolioProjectEffortContribution:
    """Re-validate one contribution's payload with strict ``model_validate``.

    Only genuine ``PortfolioProjectEffortContribution`` instances qualify; a
    hostile ``model_construct`` result is a genuine instance whose field values
    must survive the V1.26 model's own validators again. Foreign duck-typed
    objects are rejected before any attribute is read.
    """
    if not isinstance(candidate, PortfolioProjectEffortContribution):
        raise PortfolioProjectEffortContributionError(
            "expected a genuine PortfolioProjectEffortContribution, "
            f"got {type(candidate).__name__}"
        )
    payload: dict[str, Any] = {
        "project_id": candidate.project_id,
        "known_duration_seconds": candidate.known_duration_seconds,
        "estimated_entity_count": candidate.estimated_entity_count,
        "unestimated_entity_count": candidate.unestimated_entity_count,
        "ordinary_estimate_count": candidate.ordinary_estimate_count,
        "calibrated_estimate_count": candidate.calibrated_estimate_count,
        "total_duration_seconds": candidate.total_duration_seconds,
    }
    try:
        return PortfolioProjectEffortContribution.model_validate(payload, strict=True)
    except ValidationError as exc:
        # A ``model_construct`` value whose field values fail the model's own
        # invariants is not a genuine contribution: report it uniformly as a
        # V1.26 boundary error so the summary validator converts it to a
        # validation error at the model edge.
        raise PortfolioProjectEffortContributionError(
            "expected a genuine PortfolioProjectEffortContribution "
            "(field values failed strict re-validation), "
            f"got {type(candidate).__name__}"
        ) from exc


def _project_summary_payload(candidate: object) -> object:
    """Expose one V1.24 project summary as a raw field payload.

    Only genuine ``WorkBreakdownEffectiveEffortSummary`` instances whose
    ``effort`` is a genuine ``PlannedEffortSummary`` are read into raw
    dictionaries; any other value is passed through unchanged so strict
    ``model_validate`` of the V1.25 field rejects it with its own message.
    Reading attributes (instead of dumping) avoids serializer warnings from
    intentionally malicious ``model_construct`` values, while still rejecting
    those values on re-validation.
    """
    if candidate is None:
        return None
    if not isinstance(candidate, WorkBreakdownEffectiveEffortSummary):
        return candidate
    effort = candidate.effort
    if not isinstance(effort, PlannedEffortSummary):
        # ``effort`` is passed through unchanged so the V1.24 field definition
        # rejects it with its own message.
        return candidate
    return {
        "portfolio_id": candidate.portfolio_id,
        "project_id": candidate.project_id,
        "effort": {
            "known_duration_seconds": effort.known_duration_seconds,
            "estimated_entity_count": effort.estimated_entity_count,
            "unestimated_entity_count": effort.unestimated_entity_count,
            "total_duration_seconds": effort.total_duration_seconds,
        },
        "ordinary_estimate_count": candidate.ordinary_estimate_count,
        "calibrated_estimate_count": candidate.calibrated_estimate_count,
    }


def _portfolio_summary_payload(
    candidate: PortfolioEffectiveEffortSummary,
) -> dict[str, Any]:
    """Expose a V1.25 portfolio summary as a raw field payload for re-validation."""
    projects = tuple(_project_summary_payload(entry) for entry in candidate.projects)
    return {
        "portfolio_id": candidate.portfolio_id,
        "project_count": candidate.project_count,
        "known_duration_seconds": candidate.known_duration_seconds,
        "estimated_entity_count": candidate.estimated_entity_count,
        "unestimated_entity_count": candidate.unestimated_entity_count,
        "ordinary_estimate_count": candidate.ordinary_estimate_count,
        "calibrated_estimate_count": candidate.calibrated_estimate_count,
        "total_duration_seconds": candidate.total_duration_seconds,
        "projects": projects,
    }


def project_portfolio_effort_contributions(
    summary: PortfolioEffectiveEffortSummary,
) -> PortfolioProjectEffortContributionSummary:
    """Project the per-project contributions of one authoritative V1.25 summary.

    Steps:
      1. require a genuine ``PortfolioEffectiveEffortSummary`` instance;
      2. freshly and strictly re-validate its complete payload (rejecting
         hostile ``model_construct`` V1.24 project summaries and
         ``PlannedEffortSummary`` subtrees, foreign or duplicate project
         scoping, and inexact aggregate invariants);
      3. traverse the re-validated ``summary.projects`` exactly once, in the
         authoritative order, copying the flat contribution fields verbatim
         (including ``total_duration_seconds is None``);
      4. return a strict, frozen contribution summary with
         ``project_count == len(projects)``.

    No I/O, no repository access, no V1.24/V1.25 durable builders, no WBS
    inspection, no estimate or provenance reads, no recomputation, no sorting,
    no relative metrics, no persistence.
    """
    if not isinstance(summary, PortfolioEffectiveEffortSummary):
        raise PortfolioProjectEffortContributionError(
            "a genuine V1.25 PortfolioEffectiveEffortSummary instance is required, "
            f"got {type(summary).__name__}"
        )

    try:
        payload = _portfolio_summary_payload(summary)
        validated = PortfolioEffectiveEffortSummary.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise PortfolioProjectEffortContributionError(
            "supplied V1.25 portfolio summary failed strict re-validation"
        ) from exc
    except (AttributeError, TypeError) as exc:
        raise PortfolioProjectEffortContributionError(
            "supplied V1.25 portfolio summary is not the V1.25 shape"
        ) from exc

    contributions: list[PortfolioProjectEffortContribution] = []
    for project_summary in validated.projects:
        contributions.append(
            PortfolioProjectEffortContribution(
                project_id=project_summary.project_id,
                known_duration_seconds=project_summary.effort.known_duration_seconds,
                estimated_entity_count=project_summary.effort.estimated_entity_count,
                unestimated_entity_count=project_summary.effort.unestimated_entity_count,
                ordinary_estimate_count=project_summary.ordinary_estimate_count,
                calibrated_estimate_count=project_summary.calibrated_estimate_count,
                total_duration_seconds=project_summary.effort.total_duration_seconds,
            )
        )

    return PortfolioProjectEffortContributionSummary(
        portfolio_id=validated.portfolio_id,
        project_count=validated.project_count,
        projects=tuple(contributions),
    )
