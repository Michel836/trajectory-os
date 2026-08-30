"""V1.27 — Exact per-project execution-effort share projection.

V1.27 derives exact per-project effort shares from a genuine V1.26
``PortfolioProjectEffortContributionSummary``. A share is the exact integer
pair ``(project total duration, portfolio total duration)`` — a numerator and
a denominator in the same unit (seconds), never ``0/0`` and never a rounded or
truncated display value.

V1.27 is a projection only. It performs no I/O, no wall-clock or uuid reads,
no provider/AI calls, no repository or durable composition. Its sole input
authority is the caller_supplied V1.26 summary: V1.27 reads each project's
``project_id`` and exact complete ``total_duration_seconds``, and re-derives
nothing from raw WBS structure, estimates, provenance, or coverage.

Share availability is derived ONLY from the V1.26 complete totals:

* **Complete portfolio** — every included project exposes a complete
  ``total_duration_seconds``.
  - Positive total: every project share is exact,
    ``numerator_duration_seconds = project total`` and
    ``denominator_duration_seconds = portfolio total``, so numerator is always
    <= denominator.
  - Zero total: the portfolio total is ``0`` and NO project share is exposed
    (a ``0/0`` share never exists).
* **Incomplete portfolio** — any project total is incomplete (or no project
  has a complete total): the portfolio total is ``None`` and NO project share
  is exposed. A zero-duration project never turns an incomplete portfolio into
  a complete one, and a pseudo denominator built from known values alone is
  never constructed.

Empty, incomplete, and complete zero-total portfolios remain distinct states:
an empty portfolio has ``total_duration_seconds == 0`` and ``()`` projects and
carries NO share at all (a ``0/0`` ratio never exists); an incomplete
portfolio exposes no complete total and no shares; a complete zero-total
portfolio exposes ``total_duration_seconds == 0`` and no shares.

Validation semantics mirror the repository convention: hostile
``model_construct`` values at the top level, inside the nested projects
tuple, and inside a nested share must be rejected by FRESH STRICT
re-validation, never trusted. Both output models are self-validating (frozen,
strict, ``extra="forbid"``, before/after validator layers) so a
``PortfolioProjectEffortShare`` or ``PortfolioProjectEffortShareSummary``
carries a semantically coherent state on every construction — including
direct construction. Concretely:

* a share may never be exposed while its project total is incomplete;
* a share's numerator may never disagree with its project total;
* an empty portfolio may never carry a non-``0`` (or ``None``) total;
* an incomplete (non-empty) portfolio may never expose a complete total or
  ANY share, and a ``None`` summary total is never an ambiguous stand-in for
  a known total (on a non-empty portfolio it requires at least one
  incomplete project total);
* a complete zero-total portfolio may never expose a share (no ``0/0``);
* a complete positive-total portfolio must expose a share for every
  project with the exact numerator/denominator.

The V1.26 input is not inspected past ``project_id`` and
``total_duration_seconds`` and is never mutated.
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

from trajectory_os.application.execution_effort_project_contributions import (
    PortfolioProjectEffortContributionSummary,
)

__all__ = [
    "ExactProjectEffortShare",
    "PortfolioProjectEffortShare",
    "PortfolioProjectEffortShareError",
    "PortfolioProjectEffortShareSummary",
    "project_portfolio_effort_shares",
]


class PortfolioProjectEffortShareError(ValueError):
    """Raised when a supplied V1.26 contribution summary is not usable."""


# ---------------------------------------------------------------------------
# Projected share models (immutable, self-validating).
# ---------------------------------------------------------------------------


def _validate_non_bool_ints(
    value: dict[str, object],
) -> dict[str, object]:
    for field in ("numerator_duration_seconds", "denominator_duration_seconds"):
        if isinstance(value.get(field), bool):
            raise ValueError(f"{field} must not be a boolean")
    return value


class ExactProjectEffortShare(BaseModel):
    """Exact numerator/denominator share of one project's effort in a portfolio.

    ``denominator_duration_seconds`` must be strictly positive, and
    ``numerator_duration_seconds`` must not exceed it (V1.27 invariants).  A
    ``0/0`` share can never be constructed.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    numerator_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    denominator_duration_seconds: Annotated[StrictInt, Field(ge=1)]

    @model_validator(mode="before")
    @classmethod
    def _validate_non_bool_ints(
        cls, value: object
    ) -> object:
        if isinstance(value, dict):
            return _validate_non_bool_ints(value)
        return value

    @model_validator(mode="after")
    def _validate_ratio_bound(self) -> ExactProjectEffortShare:
        if self.numerator_duration_seconds > self.denominator_duration_seconds:
            raise ValueError(
                "numerator_duration_seconds must not exceed "
                "denominator_duration_seconds"
            )
        return self

    def to_payload(self) -> dict[str, int]:
        """Serialize this exact share into a plain dict (pure, no I/O)."""
        return {
            "numerator_duration_seconds": self.numerator_duration_seconds,
            "denominator_duration_seconds": self.denominator_duration_seconds,
        }


class PortfolioProjectEffortShare(BaseModel):
    """Projected per-project share row for one V1.26 project record.

    Invariants — a share is meaningful only against its own project total,
    so:

    * ``share`` being non-``None`` REQUIRES
      ``total_duration_seconds`` to be a non-``None`` strict integer total
      (a share may never be exposed while the project total is incomplete);
    * ``share`` being non-``None`` REQUIRES
      ``share.numerator_duration_seconds == total_duration_seconds``;
    * a carried ``share`` (when present) is FRESH STRICT REVALIDATED — a
      hostile ``model_construct`` nested share is rejected, never trusted.

    ``share`` is ``None`` for incomplete states, empty portfolios, and
    zero-total portfolios (a pseudo ``0/0`` share is never constructed).
    ``total_duration_seconds`` is the exact complete total carried by the
    V1.26 record (``0`` allowed), or ``None`` when that total is incomplete.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    project_id: UUID
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    share: ExactProjectEffortShare | None = None

    def to_payload(self) -> dict[str, object]:
        """Serialize this share entry into a plain dict (pure, no I/O)."""
        return {
            "project_id": self.project_id,
            "total_duration_seconds": self.total_duration_seconds,
            "share": (
                self.share.to_payload() if self.share is not None else None
            ),
        }

    @model_validator(mode="after")
    def _validate_share_consistency(self) -> PortfolioProjectEffortShare:
        if self.share is None:
            return self

        # A nested model instance may be a hostile model_construct result,
        # so it is NEVER trusted as-is: read its exact field values back
        # (via to_payload, which reads attributes and triggers no
        # serializer), revalidate them strictly, and enforce the entry
        # invariants against the revalidated values.
        revalidated = ExactProjectEffortShare.model_validate(
            self.share.to_payload(), strict=True
        )
        if self.total_duration_seconds is None:
            raise ValueError(
                "a share must not be exposed while the project total "
                "(total_duration_seconds) is None"
            )
        if (
            revalidated.numerator_duration_seconds
            != self.total_duration_seconds
        ):
            raise ValueError(
                "share.numerator_duration_seconds must equal "
                "total_duration_seconds"
            )
        return self


class PortfolioProjectEffortShareSummary(BaseModel):
    """Complete projected per-project exact share summary for one portfolio.

    ``project_count`` must equal the number of ``projects`` entries; project
    IDs are unique. ``total_duration_seconds`` is the complete portfolio
    total (``0`` for an empty or complete zero-total portfolio; ``None`` ONLY
    when the state is genuinely incomplete — at least one project total is
    ``None`` — and never an ambiguous stand-in for a known total).

    Semantic invariants (all states pinned down; each contradictory
    combination is rejected on every construction, including via a
    ``projects`` tuple):

    * **Empty** (``project_count == 0`` ⇒ ``projects == ()``):
      ``total_duration_seconds`` MUST be exactly ``0`` (``None`` or a
      non-``0`` total on an empty portfolio is rejected; no share at all).
    * **Incomplete** (any ``PortfolioProjectEffortShare.total_duration_seconds
      is None``): ``total_duration_seconds`` MUST be ``None`` and EVERY
      project's ``share`` MUST be ``None``.
    * The converse: if ``total_duration_seconds`` is ``None`` on a non-empty
      portfolio, the state MUST genuinely be incomplete (a known total never
      disappears into ambiguity).
    * **Complete zero-total** (every project total is ``0``): the
      ``total_duration_seconds`` MUST be ``0`` and every project's ``share``
      MUST be ``None`` (a ``0/0`` share is never constructed or carried).
    * **Complete positive total** (all project totals complete and
      ``sum > 0``): EVERY project's ``share`` MUST be non-``None`` with
      ``share.numerator_duration_seconds == that project total`` and
      ``share.denominator_duration_seconds == the exact summary total``
      (``total_duration_seconds`` MUST equal that exact strict sum).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    project_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    projects: tuple[PortfolioProjectEffortShare, ...]

    @model_validator(mode="before")
    @classmethod
    def _validate_non_bool_ints(
        cls, value: object
    ) -> object:
        if isinstance(value, dict):
            for field in ("project_count", "total_duration_seconds"):
                if isinstance(value.get(field), bool):
                    raise ValueError(f"{field} must not be a boolean")
        return value

    @model_validator(mode="after")
    def _validate_semantic_invariants(
        self,
    ) -> PortfolioProjectEffortShareSummary:
        if self.project_count != len(self.projects):
            raise ValueError(
                f"project_count={self.project_count} does not equal the number "
                f"of projected project entries ({len(self.projects)})"
            )

        project_ids = [project.project_id for project in self.projects]
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("duplicate project IDs are not allowed")

        # Hostile nested rows (including their nested shares) are NEVER
        # trusted: every row is freshly strictly revalidated (this
        # re-runs the entry-level share-consistency invariants AND
        # revalidates the nested share against ExactProjectEffortShare),
        # which also rejects hostile model_construct shares.
        revalidated: list[PortfolioProjectEffortShare] = []
        for project in self.projects:
            try:
                revalidated.append(
                    PortfolioProjectEffortShare.model_validate(
                        project.to_payload(), strict=True
                    )
                )
            except (AttributeError, ValidationError) as exc:
                raise ValueError(
                    "a projected project entry failed fresh strict "
                    "revalidation and is rejected"
                ) from exc

        any_incomplete = any(
            project.total_duration_seconds is None for project in revalidated
        )

        if not revalidated:
            # Empty state: total is exactly 0 — never None (a fabricated
            # incomplete state) and never a share-bearing pseudo-state.
            if self.total_duration_seconds != 0:
                raise ValueError(
                    f"an empty portfolio must have total_duration_seconds == "
                    f"0, got {self.total_duration_seconds!r}"
                )
            return self

        if any_incomplete:
            # Incomplete state: no complete total may exist and no share
            # may be exposed at all.
            if self.total_duration_seconds is not None:
                raise ValueError(
                    f"incomplete portfolio has total_duration_seconds="
                    f"{self.total_duration_seconds}; expected None"
                )
            if any(project.share is not None for project in revalidated):
                raise ValueError(
                    "incomplete portfolio cannot expose shares"
                )
            return self

        # ALL project totals are complete ⇒ the total is DETERMINED:
        # a None total here would be ambiguous (rejected); any other value
        # must equal the exact strict sum.
        complete_totals: list[int] = []
        for project in revalidated:
            if project.total_duration_seconds is None:
                raise ValueError(
                    "unreachable: an incomplete project total bypassed the "
                    "incomplete state handled above"
                )
            complete_totals.append(project.total_duration_seconds)
        exact_total = sum(complete_totals)
        if self.total_duration_seconds != exact_total:
            raise ValueError(
                f"total_duration_seconds={self.total_duration_seconds} does "
                f"not equal the exact sum of project totals ({exact_total})"
            )

        if exact_total == 0:
            # Zero-total state: no share exists at all (no 0/0, no
            # "complete with zero denominator" pseudo-state).
            if any(project.share is not None for project in revalidated):
                raise ValueError(
                    "a complete zero-total portfolio cannot expose shares"
                )
            return self

        # Positive complete total: EVERY share must be the exact
        # (project total, portfolio total) pair.
        for project in revalidated:
            if project.share is None:
                raise ValueError(
                    "complete positive-total portfolio must expose "
                    "a share for every project"
                )
            if project.share.numerator_duration_seconds != (
                project.total_duration_seconds
            ):
                raise ValueError(
                    "share numerator must equal the project's total"
                )
            if project.share.denominator_duration_seconds != (
                self.total_duration_seconds
            ):
                raise ValueError(
                    "share denominator must equal the exact summary total"
                )

        return self


# ---------------------------------------------------------------------------
# Pure projection boundary.
# ---------------------------------------------------------------------------


def project_portfolio_effort_shares(
    contributions: PortfolioProjectEffortContributionSummary,
) -> PortfolioProjectEffortShareSummary:
    """Project exact per-project effort shares from one genuine V1.26 summary.

    Steps:
      1. require a genuine ``PortfolioProjectEffortContributionSummary``
         (V1.26);
      2. freshly/strictly re-validate the whole supplied V1.26 summary (rejects
         hostile ``model_construct`` values at the top level and in the nested
         projects tuple);
      3. derive every project's exact share and the complete/empty state ONLY
         from each project's ``total_duration_seconds`` and the exact sum of
         the complete totals;
      4. return an immutable
         ``PortfolioProjectEffortShareSummary``.

    No I/O, no writes, no WBS reconstruction, no estimate/provenance access,
    no repository composition: everything is derived from the caller-supplied,
    now re-validated V1.26 summary. The input is never mutated.
    """
    if not isinstance(contributions, PortfolioProjectEffortContributionSummary):
        raise PortfolioProjectEffortShareError(
            "a genuine V1.26 PortfolioProjectEffortContributionSummary instance "
            f"is required, got {type(contributions).__name__}"
        )

    try:
        projects = tuple(contributions.projects)
        payload: object = {
            "portfolio_id": contributions.portfolio_id,
            "project_count": contributions.project_count,
            "projects": projects,
        }
    except (AttributeError, TypeError) as exc:
        raise PortfolioProjectEffortShareError(
            "supplied V1.26 contribution summary is not the V1.26 shape"
        ) from exc

    try:
        validated = PortfolioProjectEffortContributionSummary.model_validate(
            payload, strict=True
        )
    except ValidationError as exc:
        raise PortfolioProjectEffortShareError(
            "supplied V1.26 contribution summary failed strict re-validation"
        ) from exc

    project_totals = [
        contribution.total_duration_seconds for contribution in validated.projects
    ]
    any_incomplete = any(total is None for total in project_totals)

    if not any_incomplete and any(total is not None for total in project_totals):
        # Fully estimated: every included project exposes a complete total.
        complete_totals = [
            total for total in project_totals if total is not None
        ]
        portfolio_total = sum(complete_totals)
        shares = tuple(
            PortfolioProjectEffortShare(
                project_id=contribution.project_id,
                total_duration_seconds=complete_totals[index],
                share=(
                    ExactProjectEffortShare(
                        numerator_duration_seconds=complete_totals[index],
                        denominator_duration_seconds=portfolio_total,
                    )
                    if portfolio_total > 0
                    else None
                ),
            )
            for index, contribution in enumerate(validated.projects)
        )
        return PortfolioProjectEffortShareSummary(
            portfolio_id=validated.portfolio_id,
            project_count=validated.project_count,
            total_duration_seconds=portfolio_total,
            projects=shares,
        )

    if any_incomplete:
        incomplete_totals: list[int | None] = project_totals
        project_shares = tuple(
            PortfolioProjectEffortShare(
                project_id=contribution.project_id,
                total_duration_seconds=incomplete_totals[index],
                share=None,
            )
            for index, contribution in enumerate(validated.projects)
        )
        return PortfolioProjectEffortShareSummary(
            portfolio_id=validated.portfolio_id,
            project_count=validated.project_count,
            total_duration_seconds=None,
            projects=project_shares,
        )

    # Empty portfolio: zero projects, zero total, no share is exposed.
    return PortfolioProjectEffortShareSummary(
        portfolio_id=validated.portfolio_id,
        project_count=0,
        total_duration_seconds=0,
        projects=(),
    )