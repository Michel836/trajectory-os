"""V1.30 — Deterministic selected-vs-remaining effort summary from a
genuine V1.29 selection.

V1.30 is a *projection*, not a recomputation. Its sole input authority is
one genuine V1.29 ``PortfolioProjectEffortTopSelection``: V1.30 sums the
exact, already-selected V1.29 project totals and subtracts that exact sum
from the authoritative V1.29 portfolio total. It never inspects V1.28 ranks,
derives a new selection, re-orders rows, splits or infers tie groups, or
touches V1.27 or earlier layers. A tie-expanded V1.29 selection is simply
summed exactly as supplied.

V1.30 introduces no percentages, no ratios, no floats, no ``Decimal``, no
division, no rounding, no thresholds, no concentration analysis, no Pareto /
80-20 policy, and no business priority, value, urgency, strategic importance,
risk, impact, ROI, or recommendation. It performs no I/O, no wall-clock or
uuid reads, no provider / AI calls, no repository or durable composition, and
writes nothing of any kind. The input is never mutated, and repeated calls
on the same input are value-identical.

Single pure boundary:

``summarize_selected_portfolio_project_effort(selection)`` requires a
genuine ``PortfolioProjectEffortTopSelection``, freshly and strictly
re-validates its complete payload — including every nested
``PortfolioProjectEffortRank`` row and its nested exact share — and then
computes, with exact integer arithmetic only:

* ``total_duration_seconds`` — mirrors the authoritative V1.29 portfolio
  total (including ``None`` for the incomplete state);
* ``selected_duration_seconds`` — the exact integer sum of
  ``total_duration_seconds`` across the V1.29-selected rows (``None`` in
  the incomplete state; a selected zero-duration row contributes exactly
  ``0``);
* ``remaining_duration_seconds`` —
  ``total_duration_seconds - selected_duration_seconds`` (``None`` in the
  incomplete state).

Guarantees on every completed state:

* ``selected_duration_seconds >= 0``;
* ``remaining_duration_seconds >= 0``;
* ``selected_duration_seconds + remaining_duration_seconds ==
  total_duration_seconds``;
* when ``selected_project_count == source_project_count``:
  ``selected_duration_seconds == total_duration_seconds`` and
  ``remaining_duration_seconds == 0``.

Unavailable / empty states mirror V1.29 exactly — NO scalar effort amount
is ever fabricated:

* **Incomplete V1.29** — ``total_duration_seconds = None``,
  ``selected_duration_seconds = None``,
  ``remaining_duration_seconds = None`` (no numeric scalar at all).
* **Complete zero-total V1.29** — all three totals exactly ``0``.
* **Empty V1.29** — ``source_project_count = 0``,
  ``selected_project_count = 0``, and all three totals exactly ``0``.

Validation semantics mirror the repository convention: hostile
``model_construct`` values at the selection top level, inside its nested
projects tuple, and inside a nested exact share must be rejected by FRESH
STRICT re-validation, never trusted.  The output model is self-validating
(strict, frozen, ``extra="forbid"``, before/after validator layers) so a
``PortfolioProjectEffortSelectionSummary`` carries a semantically coherent
scalar state on every construction — including direct construction.
Concretely, direct construction is rejected when:

* ``selected_project_count`` exceeds ``source_project_count``;
* an incomplete state (``total_duration_seconds is None``) exposes numeric
  ``selected_duration_seconds`` / ``remaining_duration_seconds`` (or any
  selected row count);
* a complete state (``total_duration_seconds`` is an integer) exposes
  ``None`` ``selected_duration_seconds`` /
  ``remaining_duration_seconds``;
* ``selected_duration_seconds + remaining_duration_seconds !=
  total_duration_seconds`` (which also rejects ``selected`` > total);
* a full selection (``selected_project_count == source_project_count``)
  carries a nonzero ``remaining_duration_seconds`` or a
  ``selected_duration_seconds`` different from the total;
* a no-selection state (``selected_project_count == 0``) carries a nonzero
  ``selected_duration_seconds``;
* an empty source (``source_project_count == 0``) carries a nonzero
  ``total_duration_seconds`` (an empty V1.29 selection has every total
  exactly ``0``);
* a zero-total state (``total_duration_seconds == 0``) carries selected
  projects (``selected_project_count != 0``);
* a positive-total, non-empty state (``total_duration_seconds > 0`` and
  ``source_project_count > 0``) carries no selected projects
  (``selected_project_count == 0``);
* any strict scalar field is a boolean, float, string, or otherwise
  invalid type.
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

from trajectory_os.application.execution_effort_project_top_selection import (
    PortfolioProjectEffortTopSelection,
)

__all__ = [
    "PortfolioProjectEffortSelectionSummary",
    "PortfolioProjectEffortSelectionSummaryError",
    "summarize_selected_portfolio_project_effort",
]


class PortfolioProjectEffortSelectionSummaryError(ValueError):
    """Raised when a supplied V1.29 selection is not usable for summarizing."""


# ---------------------------------------------------------------------------
# Projected summary model (immutable, self-validating).
# ---------------------------------------------------------------------------


class PortfolioProjectEffortSelectionSummary(BaseModel):
    """Immutable scalar selected-vs-remaining effort summary of one V1.29 selection.

    ``requested_limit``, ``source_project_count``, and
    ``selected_project_count`` mirror the authoritative V1.29 selection
    exactly.  ``total_duration_seconds`` mirrors the authoritative V1.29
    portfolio total; ``selected_duration_seconds`` is the exact integer sum
    of the V1.29-selected project totals; ``remaining_duration_seconds`` is
    their exact difference.

    States:

    * incomplete — all three totals are ``None`` and
      ``selected_project_count`` is ``0`` (NO scalar effort amount may be
      fabricated);
    * complete zero-total / empty — all three totals are exactly ``0``;
    * complete positive-total — the exact decomposition
      ``selected + remaining == total`` with both parts ``>= 0``, and a full
      selection (``selected_project_count == source_project_count``) always
      has ``selected == total`` and ``remaining == 0``.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    requested_limit: Annotated[StrictInt, Field(ge=1)]
    source_project_count: Annotated[StrictInt, Field(ge=0)]
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    selected_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    remaining_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None

    def to_payload(self) -> dict[str, object]:
        """Serialize this summary into a plain structure (pure, no I/O)."""
        return {
            "portfolio_id": self.portfolio_id,
            "requested_limit": self.requested_limit,
            "source_project_count": self.source_project_count,
            "selected_project_count": self.selected_project_count,
            "total_duration_seconds": self.total_duration_seconds,
            "selected_duration_seconds": self.selected_duration_seconds,
            "remaining_duration_seconds": self.remaining_duration_seconds,
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
                "selected_duration_seconds",
                "remaining_duration_seconds",
            ):
                if isinstance(value.get(field), bool):
                    raise ValueError(f"{field} must not be a boolean")
        return value

    @model_validator(mode="after")
    def _validate_summary_invariants(
        self,
    ) -> PortfolioProjectEffortSelectionSummary:
        if self.selected_project_count > self.source_project_count:
            raise ValueError(
                "selected_project_count may not exceed source_project_count"
            )

        if self.total_duration_seconds is None:
            # Incomplete state: NO scalar effort amount may be exposed —
            # selected/remaining are exactly None, and a V1.29 incomplete
            # selection never carries selected rows.
            if self.selected_project_count != 0:
                raise ValueError(
                    "an incomplete selection summary may not carry selected "
                    f"projects (got {self.selected_project_count})"
                )
            if self.selected_duration_seconds is not None:
                raise ValueError(
                    "selected_duration_seconds must not be exposed while the "
                    "portfolio total is None"
                )
            if self.remaining_duration_seconds is not None:
                raise ValueError(
                    "remaining_duration_seconds must not be exposed while the "
                    "portfolio total is None"
                )
            return self

        # Complete state: both scalar decompositions must be present.
        if self.selected_duration_seconds is None:
            raise ValueError(
                "selected_duration_seconds must not be None in a complete "
                "selection summary"
            )
        if self.remaining_duration_seconds is None:
            raise ValueError(
                "remaining_duration_seconds must not be None in a complete "
                "selection summary"
            )

        selected = self.selected_duration_seconds
        remaining = self.remaining_duration_seconds
        total = self.total_duration_seconds

        if selected > total:
            raise ValueError(
                "selected_duration_seconds may not exceed the portfolio "
                "total"
            )
        if selected + remaining != total:
            raise ValueError(
                "selected_duration_seconds + remaining_duration_seconds must "
                "equal total_duration_seconds"
            )

        # Impossible direct-construction states that cannot correspond to
        # any genuine V1.29 selection.
        if self.source_project_count == 0 and total != 0:
            raise ValueError(
                "an empty selection (source_project_count == 0) may not "
                "carry a nonzero total_duration_seconds"
            )

        if total == 0 and self.selected_project_count != 0:
            raise ValueError(
                "a zero-total summary (total_duration_seconds == 0) may "
                "not carry selected projects"
            )

        if self.selected_project_count == 0:
            if selected != 0:
                raise ValueError(
                    "a no-selection summary may not carry a nonzero "
                    "selected_duration_seconds"
                )
        elif self.selected_project_count == self.source_project_count:
            # Full selection: the selected rows ARE the whole authoritative
            # portfolio, so nothing may remain outside them.
            if selected != total:
                raise ValueError(
                    "a full selection (selected_project_count == "
                    "source_project_count) must have "
                    "selected_duration_seconds == total_duration_seconds"
                )
            if remaining != 0:
                raise ValueError(
                    "a full selection (selected_project_count == "
                    "source_project_count) must have "
                    "remaining_duration_seconds == 0"
                )

        if (
            total > 0
            and self.source_project_count > 0
            and self.selected_project_count == 0
        ):
            raise ValueError(
                "a positive-total, non-empty selection summary must "
                "carry at least one selected project"
            )

        return self


# ---------------------------------------------------------------------------
# Pure summary boundary.
# ---------------------------------------------------------------------------


def summarize_selected_portfolio_project_effort(
    selection: PortfolioProjectEffortTopSelection,
) -> PortfolioProjectEffortSelectionSummary:
    """Summarize the exact selected-vs-remaining effort of one V1.29 selection.

    Steps:
      1. require a genuine ``PortfolioProjectEffortTopSelection`` (V1.29) —
         duck-typed/foreign inputs are rejected;
      2. freshly and strictly re-validate the WHOLE supplied V1.29 selection
         (rejecting hostile ``model_construct`` values at the top level and
         in the nested projects tuple, including nested exact shares);
      3. with exact integer arithmetic only, sum the complete
         ``total_duration_seconds`` of every already-selected V1.29 row and
         subtract that exact sum from the authoritative V1.29 portfolio
         total;
      4. return an immutable ``PortfolioProjectEffortSelectionSummary``
         mirroring the V1.29 counts and ``requested_limit`` exactly.

    V1.30 summarizes ONLY what V1.29 already selected: it does not inspect
    V1.28 rankings, recompute ranks, derive a new selection, or infer tie
    groups — tie-expanded V1.29 selections are simply summed exactly as
    supplied.  No I/O, no writes, no repository access, no
    V1.27/V1.28 recomputation, no division or rounding, no ranking, no
    selection policy.  The input is never mutated and repeated calls are
    value-identical.
    """
    if not isinstance(selection, PortfolioProjectEffortTopSelection):
        raise PortfolioProjectEffortSelectionSummaryError(
            "a genuine V1.29 PortfolioProjectEffortTopSelection instance "
            f"is required, got {type(selection).__name__}"
        )

    try:
        payload: dict[str, object] = {
            "portfolio_id": selection.portfolio_id,
            "requested_limit": selection.requested_limit,
            "source_project_count": selection.source_project_count,
            "selected_project_count": selection.selected_project_count,
            "total_duration_seconds": selection.total_duration_seconds,
            "projects": tuple(
                project.to_payload() for project in selection.projects
            ),
        }
    except (AttributeError, TypeError) as exc:
        raise PortfolioProjectEffortSelectionSummaryError(
            "supplied V1.29 selection is not the V1.29 shape"
        ) from exc

    try:
        validated = PortfolioProjectEffortTopSelection.model_validate(
            payload, strict=True
        )
    except ValidationError as exc:
        raise PortfolioProjectEffortSelectionSummaryError(
            "supplied V1.29 selection failed strict re-validation"
        ) from exc

    portfolio_total = validated.total_duration_seconds
    if portfolio_total is None:
        # Incomplete V1.29: NO scalar effort amount is fabricated — all
        # three totals are exactly None.
        selected_total: int | None = None
        remaining_total: int | None = None
    else:
        # Complete V1.29 state (empty, zero-total, or positive-total): the
        # V1.29 invariants guarantee that rows (when present) all carry
        # complete non-negative totals.  V1.30 only sums and subtracts exact
        # integers — no floats, no rounding, no division.
        selected_total = 0
        for project in validated.projects:
            if project.total_duration_seconds is not None:
                selected_total += project.total_duration_seconds
        remaining_total = portfolio_total - selected_total

    return PortfolioProjectEffortSelectionSummary(
        portfolio_id=validated.portfolio_id,
        requested_limit=validated.requested_limit,
        source_project_count=validated.source_project_count,
        selected_project_count=validated.selected_project_count,
        total_duration_seconds=portfolio_total,
        selected_duration_seconds=selected_total,
        remaining_duration_seconds=remaining_total,
    )
