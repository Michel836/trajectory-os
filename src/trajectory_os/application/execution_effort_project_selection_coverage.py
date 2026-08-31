"""V1.31 — Exact selected-effort coverage from an authoritative V1.30
summary.

V1.31 is a *projection*, not a recomputation. Its SOLE input authority is
one genuine V1.30 ``PortfolioProjectEffortSelectionSummary``: V1.31 names
the already-computed V1.30 selected/remaining/total scalars as an exact
coverage numerator / denominator / remaining-numerator triple, without
changing any value. It never inspects V1.29 rankings or selections,
recomputes V1.28 shares or earlier layers, reads project rows, or touches
any repository, persistent, or provider surface.

V1.31 introduces no percentages, no ratios, no floats, no ``Decimal``, no
division, no floor division, no rounding, no GCD / fraction reduction, no
normalization, no thresholds, no coverage classification, no Pareto / 80-20
policy, and no business priority, value, urgency, strategic importance,
risk, impact, ROI, or recommendation. It performs no I/O, no wall-clock or
uuid generation, no randomness, no provider / AI calls, and writes nothing
of any kind. The input is never mutated, and repeated calls on the same
input are value-identical.

Single pure boundary:

``project_selected_portfolio_effort_coverage(summary)`` requires a genuine
V1.30 ``PortfolioProjectEffortSelectionSummary``, freshly and strictly
re-validates its COMPLETE payload, and then projects, unchanged:

* ``portfolio_id``, ``requested_limit``, ``source_project_count``,
  ``selected_project_count``, ``total_duration_seconds`` — mirrored exactly
  from the authoritative V1.30 summary;
* ``selected_numerator_duration_seconds`` — the V1.30
  ``selected_duration_seconds``;
* ``coverage_denominator_duration_seconds`` — the V1.30
  ``total_duration_seconds``;
* ``remaining_numerator_duration_seconds`` — the V1.30
  ``remaining_duration_seconds``.

The fraction is kept EXACT as supplied (e.g. ``7000/10000`` stays
``7000/10000`` — it is never simplified to ``7/10``).

Availability semantics:

* **Complete positive-total V1.30** (``total_duration_seconds > 0``) — all
  three coverage scalars are present, the denominator equals the total,
  ``0 <= selected numerator <= denominator``,
  ``0 <= remaining numerator <= denominator``, and
  ``selected numerator + remaining numerator == denominator``; a full
  selection (``selected_project_count == source_project_count``) has
  selected numerator ``== denominator`` and remaining numerator ``== 0``.
* **Incomplete V1.30** (``total_duration_seconds is None``) and **complete
  zero-total V1.30** (``total_duration_seconds == 0``), including the
  **empty** V1.30 state — all three coverage scalars are exactly ``None``.
  NO scalar is fabricated (no ``0/0``, ``0/1``, ``0/100``, or percentage).

The output model is self-validating (strict, frozen, ``extra="forbid"``,
before/after validator layers) so a
``PortfolioProjectEffortSelectionCoverage`` carries a semantically
coherent scalar state on every construction — including direct
construction.  Concretely, direct construction is rejected when:

* ``selected_project_count`` exceeds ``source_project_count``;
* an incomplete state (``total_duration_seconds is None``) exposes any
  numeric coverage numerator/denominator (or any selected project count);
* a zero-total state (``total_duration_seconds == 0``) exposes any numeric
  coverage numerator/denominator, or any selected project count;
* a positive-total state (``total_duration_seconds > 0``) exposes a ``None``
  coverage numerator or denominator;
* ``coverage_denominator_duration_seconds != total_duration_seconds`` in
  the positive-total state;
* ``selected_numerator_duration_seconds >
  coverage_denominator_duration_seconds``;
* ``remaining_numerator_duration_seconds >
  coverage_denominator_duration_seconds``;
* ``selected_numerator_duration_seconds +
  remaining_numerator_duration_seconds !=
  coverage_denominator_duration_seconds``;
* a full selection (``selected_project_count == source_project_count``)
  has a selected numerator different from the denominator, or a nonzero
  remaining numerator;
* an empty source (``source_project_count == 0``) carries a positive total
  (an empty V1.30 summary has ``total_duration_seconds == 0``);
* a positive-total summary carries no selected projects (a genuine
  positive-total V1.30 summary selects at least one project);
* any strict scalar field is a boolean, float, string, or otherwise
  invalid type.

IMPORTANT MODELING LIMIT: V1.31 is intentionally scalar-only.  It cannot
independently prove project-row provenance because project rows are not
retained; V1.31 therefore carries no project rows and no availability
boolean.
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

from trajectory_os.application.execution_effort_project_selection_summary import (
    PortfolioProjectEffortSelectionSummary,
)

__all__ = [
    "PortfolioProjectEffortSelectionCoverage",
    "PortfolioProjectEffortSelectionCoverageError",
    "project_selected_portfolio_effort_coverage",
]


class PortfolioProjectEffortSelectionCoverageError(ValueError):
    """Raised when a supplied V1.30 summary is not usable for coverage."""


# ---------------------------------------------------------------------------
# Projected coverage model (immutable, self-validating).
# ---------------------------------------------------------------------------


class PortfolioProjectEffortSelectionCoverage(BaseModel):
    """Immutable exact selected-effort coverage of one V1.30 selection summary.

    ``portfolio_id``, ``requested_limit``, ``source_project_count``,
    ``selected_project_count``, and ``total_duration_seconds`` mirror the
    authoritative V1.30 summary exactly.  For a positive-total summary, the
    exact coverage is exposed as ``selected_numerator_duration_seconds /
    coverage_denominator_duration_seconds`` with
    ``remaining_numerator_duration_seconds`` the exact remainder — the
    fraction is never reduced, rounded, or otherwise altered.

    States:

    * incomplete — ``total_duration_seconds`` is ``None``, all three
      coverage scalars are ``None``, and no project is selected (NO
      scalar coverage may be fabricated);
    * zero-total / empty — ``total_duration_seconds == 0`` and all three
      coverage scalars are exactly ``None`` (no ``0/0`` or ``0/1`` is
      fabricated);
    * positive-total — all three coverage scalars are present, the
      denominator equals the total, both numerators are within
      ``[0, denominator]``, ``selected + remaining == denominator``, and a
      full selection has ``selected == denominator`` and
      ``remaining == 0``.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    requested_limit: Annotated[StrictInt, Field(ge=1)]
    source_project_count: Annotated[StrictInt, Field(ge=0)]
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    selected_numerator_duration_seconds: (
        Annotated[StrictInt, Field(ge=0)] | None
    ) = None
    coverage_denominator_duration_seconds: (
        Annotated[StrictInt, Field(ge=0)] | None
    ) = None
    remaining_numerator_duration_seconds: (
        Annotated[StrictInt, Field(ge=0)] | None
    ) = None

    def to_payload(self) -> dict[str, object]:
        """Serialize this coverage into a plain structure (pure, no I/O)."""
        return {
            "portfolio_id": self.portfolio_id,
            "requested_limit": self.requested_limit,
            "source_project_count": self.source_project_count,
            "selected_project_count": self.selected_project_count,
            "total_duration_seconds": self.total_duration_seconds,
            "selected_numerator_duration_seconds": (
                self.selected_numerator_duration_seconds
            ),
            "coverage_denominator_duration_seconds": (
                self.coverage_denominator_duration_seconds
            ),
            "remaining_numerator_duration_seconds": (
                self.remaining_numerator_duration_seconds
            ),
        }

    @model_validator(mode="before")
    @classmethod
    def _validate_non_bool_ints(cls, value: object) -> object:
        if isinstance(value, dict):
            for field in (
                "requested_limit",
                "source_project_count",
                "selected_project_count",
                "total_duration_seconds",
                "selected_numerator_duration_seconds",
                "coverage_denominator_duration_seconds",
                "remaining_numerator_duration_seconds",
            ):
                if isinstance(value.get(field), bool):
                    raise ValueError(f"{field} must not be a boolean")
        return value

    @model_validator(mode="after")
    def _validate_coverage_invariants(
        self,
    ) -> PortfolioProjectEffortSelectionCoverage:
        if self.selected_project_count > self.source_project_count:
            raise ValueError(
                "selected_project_count may not exceed source_project_count"
            )

        coverage_scalars = (
            self.selected_numerator_duration_seconds,
            self.coverage_denominator_duration_seconds,
            self.remaining_numerator_duration_seconds,
        )

        if self.total_duration_seconds is None:
            # Incomplete state: NO coverage scalar may be exposed — all
            # three coverage fields are exactly None, and a V1.30
            # incomplete summary never carries selected projects.
            if self.selected_project_count != 0:
                raise ValueError(
                    "an incomplete coverage state may not carry selected "
                    f"projects (got {self.selected_project_count})"
                )
            if any(scalar is not None for scalar in coverage_scalars):
                raise ValueError(
                    "coverage numerators/denominator must all be None "
                    "while total_duration_seconds is None"
                )
            return self

        total = self.total_duration_seconds

        if total == 0:
            # Zero-total state (including the empty V1.30 state): the
            # available coverage is undefined — no 0/0, 0/1, or 0/100 is
            # fabricated, and a V1.30 zero-total summary never carries
            # selected projects.
            if self.selected_project_count != 0:
                raise ValueError(
                    "a zero-total coverage state may not carry selected "
                    f"projects (got {self.selected_project_count})"
                )
            if any(scalar is not None for scalar in coverage_scalars):
                raise ValueError(
                    "coverage numerators/denominator must all be None for "
                    "a zero-total summary"
                )
            return self

        # Positive-total state: the complete exact coverage must be
        # present.
        if self.selected_numerator_duration_seconds is None:
            raise ValueError(
                "selected_numerator_duration_seconds must not be None for "
                "a positive-total summary"
            )
        if self.coverage_denominator_duration_seconds is None:
            raise ValueError(
                "coverage_denominator_duration_seconds must not be None "
                "for a positive-total summary"
            )
        if self.remaining_numerator_duration_seconds is None:
            raise ValueError(
                "remaining_numerator_duration_seconds must not be None for "
                "a positive-total summary"
            )

        selected = self.selected_numerator_duration_seconds
        denominator = self.coverage_denominator_duration_seconds
        remaining = self.remaining_numerator_duration_seconds

        if denominator != total:
            raise ValueError(
                "coverage_denominator_duration_seconds must equal "
                "total_duration_seconds"
            )
        if selected > denominator:
            raise ValueError(
                "selected_numerator_duration_seconds may not exceed the "
                "coverage denominator"
            )
        if remaining > denominator:
            raise ValueError(
                "remaining_numerator_duration_seconds may not exceed the "
                "coverage denominator"
            )

        if self.selected_project_count == self.source_project_count:
            # Full selection: the selected rows ARE the whole authoritative
            # portfolio, so the selected numerator is the whole
            # denominator and nothing may remain outside them.
            if selected != denominator:
                raise ValueError(
                    "a full selection (selected_project_count == "
                    "source_project_count) must have "
                    "selected_numerator_duration_seconds == "
                    "coverage_denominator_duration_seconds"
                )
            if remaining != 0:
                raise ValueError(
                    "a full selection (selected_project_count == "
                    "source_project_count) must have "
                    "remaining_numerator_duration_seconds == 0"
                )

        if selected + remaining != denominator:
            raise ValueError(
                "selected_numerator_duration_seconds + "
                "remaining_numerator_duration_seconds must equal "
                "coverage_denominator_duration_seconds"
            )

        # Impossible direct-construction states that cannot correspond to
        # any genuine V1.30 summary.
        if self.source_project_count == 0:
            raise ValueError(
                "an empty summary (source_project_count == 0) has a "
                "zero total_duration_seconds, not a positive one"
            )

        if self.selected_project_count == 0:
            raise ValueError(
                "a positive-total selection summary must carry at least "
                "one selected project"
            )

        return self


# ---------------------------------------------------------------------------
# Pure coverage boundary.
# ---------------------------------------------------------------------------


def project_selected_portfolio_effort_coverage(
    summary: PortfolioProjectEffortSelectionSummary,
) -> PortfolioProjectEffortSelectionCoverage:
    """Project the exact selected-effort coverage of one V1.30 summary.

    Steps:
      1. require a genuine ``PortfolioProjectEffortSelectionSummary``
         (V1.30) — duck-typed/foreign inputs are rejected;
      2. freshly and strictly re-validate the WHOLE supplied V1.30 payload
         (rejecting hostile ``model_construct`` values and hostile
         scalar types);
      3. project the V1.30 scalars unchanged into the exact coverage
         representation:
         ``selected_numerator == selected_duration_seconds``,
         ``coverage_denominator == total_duration_seconds``,
         ``remaining_numerator == remaining_duration_seconds`` — for a
         positive total; all three coverage scalars are exactly ``None``
         for incomplete and zero-total (including empty) V1.30 states;
      4. return an immutable
         ``PortfolioProjectEffortSelectionCoverage`` mirroring the V1.30
         counts and ``requested_limit`` exactly.

    The fraction is kept exact: no reduction, no GCD, no division, no
    rounding, no percentage, no float.  No I/O, no writes, no repository
    access, no V1.29/V1.28 recomputation, no project rows, no
    classification or recommendation.  The input is never mutated and
    repeated calls are value-identical.
    """
    if not isinstance(summary, PortfolioProjectEffortSelectionSummary):
        raise PortfolioProjectEffortSelectionCoverageError(
            "a genuine V1.30 PortfolioProjectEffortSelectionSummary "
            f"instance is required, got {type(summary).__name__}"
        )

    try:
        payload: dict[str, object] = summary.to_payload()
    except (AttributeError, TypeError) as exc:
        raise PortfolioProjectEffortSelectionCoverageError(
            "supplied V1.30 summary is not the V1.30 shape"
        ) from exc

    try:
        validated = PortfolioProjectEffortSelectionSummary.model_validate(
            payload, strict=True
        )
    except ValidationError as exc:
        raise PortfolioProjectEffortSelectionCoverageError(
            "supplied V1.30 summary failed strict re-validation"
        ) from exc

    total = validated.total_duration_seconds
    selected_total = validated.selected_duration_seconds
    remaining_total = validated.remaining_duration_seconds

    if total is None or total == 0:
        # Incomplete or zero-total (including empty) V1.30: NO coverage
        # scalar is fabricated — all three coverage fields are exactly
        # None.
        selected_numerator: int | None = None
        coverage_denominator: int | None = None
        remaining_numerator: int | None = None
    else:
        # Positive-total V1.30: the V1.30 invariants (enforced by the
        # strict re-validation above) guarantee complete exact selected
        # and remaining scalars that decompose the total.  V1.31 keeps
        # them EXACT — no arithmetic at all.
        selected_numerator = selected_total
        coverage_denominator = total
        remaining_numerator = remaining_total

    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=validated.portfolio_id,
        requested_limit=validated.requested_limit,
        source_project_count=validated.source_project_count,
        selected_project_count=validated.selected_project_count,
        total_duration_seconds=total,
        selected_numerator_duration_seconds=selected_numerator,
        coverage_denominator_duration_seconds=coverage_denominator,
        remaining_numerator_duration_seconds=remaining_numerator,
    )
