"""V1.25 — Exact portfolio-level execution effort summary boundary.

V1.24 owns each PROJECT's effective-effort summary: it composes the effective
V1.23 plan into the ``WorkBreakdownEffectiveEffortSummary`` shape and keeps the
ordinary/calibrated classification and the known/estimated/unestimated effort
counts self-consistent.

V1.25 composes *existing*, already-persisted per-project results into a single
Portfolio-level aggregate. It creates no estimates, no calibration revisions,
no provenance, no projects, tasks, work items, and no decisions. It writes
nothing durable of its own and never mutates a stored Portfolio.

Two read-only boundaries, both pure functions over read-only input:

1. ``summarize_portfolio_effective_effort(portfolio_id, project_summaries)``
   accepts a genuine UUID and an ordered ``Iterable`` of V1.24 project
   summaries, consumes that iterable exactly once, validates and scopes every
   entry exactly as the V1.24 model defines it, and returns the strict, frozen
   ``PortfolioEffectiveEffortSummary``. The iterable may be a plain generator:
   entries are pulled one at a time and the aggregate is built as they stream
   in — nothing is retained beyond the validated ``projects`` tuple of the
   result.

2. ``build_portfolio_effective_effort_summary_durably(...)`` loads the CURRENT
   Portfolio through the supplied read-only repository exactly once, revalidates
   that loaded Portfolio with strict validation before any project discovery,
   and (and only for that one load) delegates every discovered PROJECT in the
   Portfolio's canonical order to the authoritative V1.24 durable boundary.
   It never loads or rewrites the Portfolio for summarization and performs no
   writes. ``portfolio_repository``, ``estimate_reader`` and
   ``provenance_reader`` are separate read-only ports.

Aggregation semantics are exact:

* ``project_count`` is the number of included PROJECTs (zero for an empty
  sequence / empty Portfolio).
* ``known_duration_seconds``, ``estimated_entity_count``,
  ``unestimated_entity_count``, ``ordinary_estimate_count`` and
  ``calibrated_estimate_count`` are the exact sum across the included projects.
  They are not recomputed from raw estimates.
* ``total_duration_seconds`` is exposed exactly only when **every** project
  summary exposes a complete total (V1.24 exposes one only when unestimated is
  zero); in that case it is their exact sum. A single unestimated entity in
  any project (or any project without a complete total) makes the portfolio
  total ``None``. The flag is never derived from the aggregate unestimated
  count.
* Every included project must belong to the exact ``portfolio_id`` and project
  IDs must be unique.

The result is deterministic for identical ordered summaries and
``model_validate``'s own invariants guarantee a complete aggregate total equals
the exact known sum.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from trajectory_os.application.execution_effort_effective_plan import (
    CalibrationProvenanceReader,
)
from trajectory_os.application.execution_effort_effective_summary import (
    WorkBreakdownEffectiveEffortSummary,
    build_effective_work_breakdown_effort_summary_durably,
)
from trajectory_os.application.execution_effort_planning import (
    ExecutionEffortEstimateReader,
    ExecutionEffortPlanningPortfolioNotFoundError,
)
from trajectory_os.application.work_breakdown_acceptance import (
    PortfolioRepository,
)
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.portfolio import Portfolio

__all__ = [
    "PortfolioEffectiveEffortSummary",
    "PortfolioEffectiveEffortSummaryError",
    "build_portfolio_effective_effort_summary_durably",
    "summarize_portfolio_effective_effort",
]


class PortfolioEffectiveEffortSummaryError(Exception):
    """Raised when the V1.25 portfolio summary boundary sees invalid input.

    This is the sole failure channel of the V1.25 boundary itself; failure of
    the delegated V1.24 durable boundary propagates unchanged (see issue #70).
    """


class PortfolioEffectiveEffortSummary(BaseModel):
    """Strict, frozen, self-consistent portfolio-level execution effort summary.

    The exact aggregate fields are exposed directly alongside the ordered
    per-project V1.24 summaries. All invariants below are re-validated on
    construction, so a hostile ``model_construct`` subtree (in a project
    summary) must be rejected by re-validation, not trusted.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    project_count: Annotated[StrictInt, Field(ge=0)]
    known_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    estimated_entity_count: Annotated[StrictInt, Field(ge=0)]
    unestimated_entity_count: Annotated[StrictInt, Field(ge=0)]
    ordinary_estimate_count: Annotated[StrictInt, Field(ge=0)]
    calibrated_estimate_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    projects: tuple[WorkBreakdownEffectiveEffortSummary, ...]

    @model_validator(mode="after")
    def _validate_portfolio_invariants(self) -> PortfolioEffectiveEffortSummary:
        projects: list[WorkBreakdownEffectiveEffortSummary] = []
        for entry in self.projects:
            try:
                projects.append(_revalidate_project_summary(entry))
            except PortfolioEffectiveEffortSummaryError as exc:
                # Surface uniformly as a model ValidationError at the model edge.
                raise ValueError(str(exc)) from exc

        if self.project_count != len(projects):
            raise ValueError(
                f"project_count={self.project_count} does not equal the number "
                f"of project summaries ({len(projects)})"
            )

        # The remaining checks operate on the freshly re-validated summaries:
        # their V1.24 invariants (strict shapes, explainable partitions) already
        # hold; only the V1.25 portfolio-level invariants are checked here.

        for summary in projects:
            if summary.portfolio_id != self.portfolio_id:
                raise ValueError(
                    f"project summary {summary.project_id} belongs to a different "
                    f"portfolio than {self.portfolio_id}"
                )

        project_ids = [summary.project_id for summary in projects]
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("duplicate project IDs are not allowed")

        if self.known_duration_seconds != sum(
            s.effort.known_duration_seconds for s in projects
        ):
            raise ValueError(
                f"known_duration_seconds={self.known_duration_seconds} is not the "
                "exact sum over the included projects"
            )
        for field in (
            "estimated_entity_count",
            "unestimated_entity_count",
        ):
            expected = sum(getattr(s.effort, field) for s in projects)
            if getattr(self, field) != expected:
                raise ValueError(f"{field} is not the exact sum over the projects")
        expected_ordinary = sum(s.ordinary_estimate_count for s in projects)
        if self.ordinary_estimate_count != expected_ordinary:
            raise ValueError("ordinary_estimate_count is not the exact sum over the projects")
        expected_calibrated = sum(s.calibrated_estimate_count for s in projects)
        if self.calibrated_estimate_count != expected_calibrated:
            raise ValueError("calibrated_estimate_count is not the exact sum over the projects")

        if self.ordinary_estimate_count + self.calibrated_estimate_count != (
            self.estimated_entity_count
        ):
            raise ValueError(
                "ordinary_estimate_count + calibrated_estimate_count must equal "
                "estimated_entity_count"
            )

        complete = all(s.effort.total_duration_seconds is not None for s in projects)
        if complete:
            expected_total = sum(
                s.effort.total_duration_seconds
                for s in projects
                if s.effort.total_duration_seconds is not None
            )
            if self.total_duration_seconds != expected_total:
                raise ValueError(
                    f"total_duration_seconds must equal the exact sum of the "
                    f"project totals ({expected_total}) when every project is complete"
                )
        elif self.total_duration_seconds is not None:
            raise ValueError(
                "a complete total must not be exposed while any project summary "
                "has unestimated entities"
            )

        return self


def _revalidate_project_summary(candidate: object) -> WorkBreakdownEffectiveEffortSummary:
    """Re-validate one project summary's payload with strict ``model_validate``.

    Only genuine ``WorkBreakdownEffectiveEffortSummary`` instances qualify; a
    hostile ``model_construct`` result is a genuine instance whose field values
    and nested ``PlannedEffortSummary`` must survive the V1.24 model's own
    validators again. Foreign duck-typed objects are rejected before any
    attribute is read.
    """
    if not isinstance(candidate, WorkBreakdownEffectiveEffortSummary):
        raise PortfolioEffectiveEffortSummaryError(
            f"expected a genuine WorkBreakdownEffectiveEffortSummary, "
            f"got {type(candidate).__name__}"
        )
    try:
        payload: dict[str, Any] = {
            "portfolio_id": candidate.portfolio_id,
            "project_id": candidate.project_id,
            "effort": {
                "known_duration_seconds": candidate.effort.known_duration_seconds,
                "estimated_entity_count": candidate.effort.estimated_entity_count,
                "unestimated_entity_count": candidate.effort.unestimated_entity_count,
                "total_duration_seconds": candidate.effort.total_duration_seconds,
            },
            "ordinary_estimate_count": candidate.ordinary_estimate_count,
            "calibrated_estimate_count": candidate.calibrated_estimate_count,
        }
    except AttributeError as exc:
        raise PortfolioEffectiveEffortSummaryError(
            "project summary is not the V1.24 shape"
        ) from exc
    return WorkBreakdownEffectiveEffortSummary.model_validate(payload, strict=True)


def _revalidate_loaded_portfolio(portfolio: Portfolio) -> Portfolio:
    """Strictly re-validate the repository-loaded Portfolio before discovery.

    The loaded object is trusted only after it survives strict validation of
    its full field payload. A hostile ``model_construct`` result from a
    repository must therefore be rejected before any project is discovered.
    """
    try:
        payload = portfolio.model_dump()
    except Exception as exc:  # defensive: unserializable hostile field
        raise PortfolioEffectiveEffortSummaryError(
            "loaded portfolio could not be serialized for re-validation"
        ) from exc
    try:
        return Portfolio.model_validate(payload, strict=True)
    except Exception as exc:
        msg = (
            f"loaded portfolio for {getattr(portfolio, 'id', portfolio)!r} failed "
            "strict re-validation"
        )
        raise PortfolioEffectiveEffortSummaryError(msg) from exc


def summarize_portfolio_effective_effort(
    portfolio_id: UUID,
    project_summaries: Iterable[WorkBreakdownEffectiveEffortSummary],
) -> PortfolioEffectiveEffortSummary:
    """Aggregate validated V1.24 project summaries into one portfolio summary.

    ``project_summaries`` is a general ``Iterable`` consumed exactly once, in
    the order the iterable yields. The iterable may be a plain generator;
    entries stream in one at a time and are immediately validated, scoped, and
    aggregated (nothing is retained beyond the resulting ``projects`` tuple).

    Raises ``PortfolioEffectiveEffortSummaryError`` for non-UUID, non-iterable,
    foreign-portfolio, or duplicate project inputs, or when a project summary
    fails strict re-validation. Success returns a strict model that
    self-validates all invariants described in the class docstring.
    """
    if not isinstance(portfolio_id, UUID):
        msg = (
            f"portfolio_id must be a UUID, got {type(portfolio_id).__name__}; "
            "callers must not pass strings, integers, or other values"
        )
        raise PortfolioEffectiveEffortSummaryError(msg)

    try:
        iterator = iter(project_summaries)
    except TypeError as exc:
        msg = (
            f"project_summaries must be an Iterable of V1.24 project summaries, "
            f"got {type(project_summaries).__name__}"
        )
        raise PortfolioEffectiveEffortSummaryError(msg) from exc

    projects: list[WorkBreakdownEffectiveEffortSummary] = []
    seen_project_ids: set[UUID] = set()

    for index, candidate in enumerate(iterator):
        try:
            summary = _revalidate_project_summary(candidate)
        except PortfolioEffectiveEffortSummaryError as exc:
            raise PortfolioEffectiveEffortSummaryError(
                f"project summary #{index}: {exc}"
            ) from exc
        except ValueError as exc:  # strict re-validation failure (V1.24 invariant)
            raise PortfolioEffectiveEffortSummaryError(
                f"project summary #{index} failed strict re-validation"
            ) from exc

        if summary.portfolio_id != portfolio_id:
            msg = (
                f"project summary #{index} for project {summary.project_id} belongs "
                f"to a different portfolio ({summary.portfolio_id} vs {portfolio_id})"
            )
            raise PortfolioEffectiveEffortSummaryError(msg)
        if summary.project_id in seen_project_ids:
            msg = (
                f"project summary #{index} duplicates project {summary.project_id}"
            )
            raise PortfolioEffectiveEffortSummaryError(msg)
        seen_project_ids.add(summary.project_id)
        projects.append(summary)

    complete_projects = [
        s for s in projects if s.effort.total_duration_seconds is not None
    ]
    complete_totals = [
        s.effort.total_duration_seconds
        for s in complete_projects
        if s.effort.total_duration_seconds is not None
    ]
    return PortfolioEffectiveEffortSummary(
        portfolio_id=portfolio_id,
        project_count=len(projects),
        known_duration_seconds=sum(s.effort.known_duration_seconds for s in projects),
        estimated_entity_count=sum(
            s.effort.estimated_entity_count for s in projects
        ),
        unestimated_entity_count=sum(
            s.effort.unestimated_entity_count for s in projects
        ),
        ordinary_estimate_count=sum(s.ordinary_estimate_count for s in projects),
        calibrated_estimate_count=sum(s.calibrated_estimate_count for s in projects),
        total_duration_seconds=(
            sum(complete_totals) if len(complete_projects) == len(projects) else None
        ),
        projects=tuple(projects),
    )


def build_portfolio_effective_effort_summary_durably(
    portfolio_id: UUID,
    portfolio_repository: PortfolioRepository,
    estimate_reader: ExecutionEffortEstimateReader,
    provenance_reader: CalibrationProvenanceReader,
) -> PortfolioEffectiveEffortSummary:
    """Build a durable portfolio effective-effort summary, read-only.

    Loads the CURRENT Portfolio through ``portfolio_repository`` exactly once,
    strictly re-validates it (rejecting e.g. a hostile ``model_construct``
    result), rejects missing Portfolios with the established
    ``ExecutionEffortPlanningPortfolioNotFoundError`` (and foreign Portfolios),
    then discovers every PROJECT in the Portfolio's canonical order and
    delegates each one to the authoritative V1.24 durable boundary in
    dependency order. It composes the delegated summaries with
    :func:`summarize_portfolio_effective_effort`, which validates scope,
    uniqueness, and exact aggregation again from the composed shape.

    Per-project V1.24 failures propagate unchanged (a V1.25 boundary error must
    not mask or rewrite them). This function performs no writes of any kind and
    no Portfolio load or re-write for summarization.
    """
    if not isinstance(portfolio_id, UUID):
        msg = (
            f"portfolio_id must be a genuine UUID, got {type(portfolio_id).__name__}"
        )
        raise PortfolioEffectiveEffortSummaryError(msg)

    loaded = portfolio_repository.load(portfolio_id)
    if loaded is None:
        raise ExecutionEffortPlanningPortfolioNotFoundError(
            f"portfolio {portfolio_id} was not found"
        )
    portfolio = _revalidate_loaded_portfolio(loaded)
    if portfolio.id != portfolio_id:
        msg = (
            f"repository returned portfolio {portfolio.id!r} while "
            f"{portfolio_id!r} was requested; refusing to summarize a foreign portfolio"
        )
        raise PortfolioEffectiveEffortSummaryError(msg)

    project_ids = tuple(
        entity.id for entity in portfolio.filter_entities(entity_type=EntityType.PROJECT)
    )

    summaries: list[WorkBreakdownEffectiveEffortSummary] = []
    for project_id in project_ids:
        summaries.append(
            build_effective_work_breakdown_effort_summary_durably(
                portfolio_id,
                project_id,
                portfolio_repository,
                estimate_reader,
                provenance_reader,
            )
        )

    return summarize_portfolio_effective_effort(portfolio_id, summaries)
