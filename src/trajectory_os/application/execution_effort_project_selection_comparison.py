"""V1.32 — Deterministic comparison of two authoritative V1.31 effort
selections.

V1.32 is *comparison only*, not recommendation. Its SOLE input authority is
two genuine V1.31 ``PortfolioProjectEffortSelectionCoverage`` values. It
answers one question only:

"If I move from one already-defined focus selection to another, what
changes objectively in selected project count, selected effort, and
remaining effort?"

It never answers "which selection should I choose". It introduces no
"better" / "worse" / "preferred" label, no recommendation, no optimization,
no utility or value scoring, no importance / urgency / risk / impact / ROI,
no Pareto / 80-20 policy, no thresholds, no focus-quality classification,
no percentages, no ratios, no floats, no ``Decimal``, no division, no floor
division, no rounding, no fraction reduction, and no normalized scores.
Effort is not value: V1.32 reports factual differences between two
already-defined focus scopes and nothing else.

V1.32 performs no I/O, no wall-clock or uuid generation, no randomness, no
provider / AI calls, no repository / durable reads, and writes nothing of
any kind. It does not read or recompute V1.30 summaries, V1.29 selections,
V1.28 rankings, V1.27 shares, project rows, or any earlier layer. The
inputs are never mutated, and repeated calls on the same inputs are
value-identical.

Directional convention (EXACT):

* ``delta = right - left`` for every ``*_delta`` field:

  ``selected_project_count_delta =
      right_selected_project_count - left_selected_project_count``

  ``selected_duration_delta_seconds =
      right_selected_duration_seconds - left_selected_duration_seconds``

  ``remaining_duration_delta_seconds =
      right_remaining_duration_seconds - left_remaining_duration_seconds``

Negative, zero, and positive deltas are all valid. No absolute differences
are used. Neither ``right_requested_limit >= left_requested_limit`` nor
``right_selected_project_count >= left_selected_project_count`` is required;
the caller chooses the orientation and V1.32 reports ``right - left`` only.

Single pure boundary:

``compare_portfolio_effort_selections(left, right)`` requires TWO genuine
V1.31 ``PortfolioProjectEffortSelectionCoverage`` values (``None``, dicts,
strings, foreign models, and duck types are rejected), freshly and strictly
re-validates the COMPLETE payload of BOTH inputs, and then compares.

Comparison compatibility — two inputs are comparable only inside the same
authoritative comparison domain, requiring exact equality of:

* ``portfolio_id``;
* ``source_project_count``;
* ``total_duration_seconds`` (including exact availability: an incomplete
  state on one side and a complete state on the other is rejected, and a
  zero-total state on one side and an incomplete or positive-total state on
  the other is rejected);
* for positive-total coverage: ``coverage_denominator_duration_seconds``
  (equal totals plus the V1.31 invariants already imply equal
  denominators; the check is kept explicit and defensive).

Different ``requested_limit`` values are allowed and expected, as are
different ``selected_project_count`` values. Cross-portfolio and
cross-total comparisons are rejected, never fabricated.

Result semantics:

* **Positive-total domain** — both V1.31 inputs expose the exact coverage
  scalars, so the comparison exposes them exactly and computes
  ``right - left`` deltas with exact integer arithmetic only. Because both
  selections decompose the SAME authoritative total:

  ``selected_duration_delta_seconds
  + remaining_duration_delta_seconds == 0``

  (a widening focus: positive selected delta + equal negative remaining
  delta; a narrowing focus: the mirror image; equivalent selections: zero
  deltas).
* **Incomplete domain** (both totals are ``None``) — the authoritative
  absence of a total is preserved; copied identity/count metadata is
  exposed, and ALL effort duration/delta fields are exactly ``None`` (no
  partially misleading effort comparison is produced).
* **Zero-total domain** (both totals are ``0``) — the authoritative zero
  total is preserved; NO ratio, percentage, or scalar effort amount is
  fabricated; the V1.31 zero-selection invariants are preserved (zero
  selected counts on both sides); all duration/delta fields are exactly
  ``None``.

The output model is self-validating (strict, frozen, ``extra="forbid"``,
before/after validator layers) so a
``PortfolioProjectEffortSelectionComparison`` carries a semantically
coherent scalar state on every construction — including direct
construction.  Concretely, direct construction is rejected when:

* any strict scalar field is a boolean, float, string, or otherwise
  invalid type;
* ``left_selected_project_count`` or ``right_selected_project_count``
  exceeds ``source_project_count``;
* either ``requested_limit`` is not a positive integer;
* ``selected_project_count_delta !=
  right_selected_project_count -
  left_selected_project_count``;
* an incomplete state (``total_duration_seconds is None``) or a zero-total
  state (``total_duration_seconds == 0``) exposes any numeric effort
  duration or delta (unavailable effort must not be partially fabricated);
* a positive-total state (``total_duration_seconds > 0``) is missing any
  of the six effort duration/delta fields;
* in the positive-total state any selected or remaining duration is
  negative; or
* ``left_selected_duration_seconds +
  left_remaining_duration_seconds != total_duration_seconds``; or
* ``right_selected_duration_seconds +
  right_remaining_duration_seconds != total_duration_seconds``; or
* ``selected_duration_delta_seconds !=
  right_selected_duration_seconds - left_selected_duration_seconds``; or
* ``remaining_duration_delta_seconds !=
  right_remaining_duration_seconds - left_remaining_duration_seconds``; or
* ``selected_duration_delta_seconds +
  remaining_duration_delta_seconds != 0`` (conservation: both selections
  describe the same authoritative total).
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

from trajectory_os.application.execution_effort_project_selection_coverage import (
    PortfolioProjectEffortSelectionCoverage,
)

__all__ = [
    "PortfolioProjectEffortSelectionComparison",
    "PortfolioProjectEffortSelectionComparisonError",
    "compare_portfolio_effort_selections",
]


class PortfolioProjectEffortSelectionComparisonError(ValueError):
    """Raised when two V1.31 coverages are not comparable to each other."""


# ---------------------------------------------------------------------------
# Comparison model (immutable, self-validating).
# ---------------------------------------------------------------------------


class PortfolioProjectEffortSelectionComparison(BaseModel):
    """Immutable factual difference between two V1.31 focus selections.

    ``portfolio_id``, ``source_project_count``, and
    ``total_duration_seconds`` mirror the shared authoritative comparison
    domain EXACTLY.  The ``left_*`` / ``right_*`` fields copy the two V1.31
    values unchanged; every ``*_delta`` field is exactly ``right - left``.

    V1.32 reports factual differences only: it carries no ordering between
    the two selections, no recommendation, no scores, and no booleans of
    the "is_better" style.  In the incomplete and zero-total domains all
    effort duration/delta fields are exactly ``None`` (no partially
    misleading effort comparison); in the positive-total domain they are all
    present, non-negative on both sides, each side decomposing the same
    authoritative total, and the two deltas conserve (sum to exactly zero).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    source_project_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None

    left_requested_limit: Annotated[StrictInt, Field(ge=1)]
    right_requested_limit: Annotated[StrictInt, Field(ge=1)]

    left_selected_project_count: Annotated[StrictInt, Field(ge=0)]
    right_selected_project_count: Annotated[StrictInt, Field(ge=0)]
    selected_project_count_delta: StrictInt

    left_selected_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = (
        None
    )
    right_selected_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = (
        None
    )
    selected_duration_delta_seconds: StrictInt | None = None

    left_remaining_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = (
        None
    )
    right_remaining_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = (
        None
    )
    remaining_duration_delta_seconds: StrictInt | None = None

    def to_payload(self) -> dict[str, object]:
        """Serialize this comparison into a plain structure (pure, no I/O)."""
        return {
            "portfolio_id": self.portfolio_id,
            "source_project_count": self.source_project_count,
            "total_duration_seconds": self.total_duration_seconds,
            "left_requested_limit": self.left_requested_limit,
            "right_requested_limit": self.right_requested_limit,
            "left_selected_project_count": self.left_selected_project_count,
            "right_selected_project_count": self.right_selected_project_count,
            "selected_project_count_delta": self.selected_project_count_delta,
            "left_selected_duration_seconds": self.left_selected_duration_seconds,
            "right_selected_duration_seconds": (
                self.right_selected_duration_seconds
            ),
            "selected_duration_delta_seconds": (
                self.selected_duration_delta_seconds
            ),
            "left_remaining_duration_seconds": (
                self.left_remaining_duration_seconds
            ),
            "right_remaining_duration_seconds": (
                self.right_remaining_duration_seconds
            ),
            "remaining_duration_delta_seconds": (
                self.remaining_duration_delta_seconds
            ),
        }

    @model_validator(mode="before")
    @classmethod
    def _validate_non_bool_ints(cls, value: object) -> object:
        if isinstance(value, dict):
            int_fields = (
                "source_project_count",
                "total_duration_seconds",
                "left_requested_limit",
                "right_requested_limit",
                "left_selected_project_count",
                "right_selected_project_count",
                "selected_project_count_delta",
                "left_selected_duration_seconds",
                "right_selected_duration_seconds",
                "selected_duration_delta_seconds",
                "left_remaining_duration_seconds",
                "right_remaining_duration_seconds",
                "remaining_duration_delta_seconds",
            )
            for field in int_fields:
                if isinstance(value.get(field), bool):
                    raise ValueError(f"{field} must not be a boolean")
        return value

    @model_validator(mode="after")
    def _validate_comparison_invariants(
        self,
    ) -> PortfolioProjectEffortSelectionComparison:
        if self.left_selected_project_count > self.source_project_count:
            raise ValueError(
                "left_selected_project_count may not exceed "
                "source_project_count"
            )
        if self.right_selected_project_count > self.source_project_count:
            raise ValueError(
                "right_selected_project_count may not exceed "
                "source_project_count"
            )

        expected_count_delta = (
            self.right_selected_project_count - self.left_selected_project_count
        )
        if self.selected_project_count_delta != expected_count_delta:
            raise ValueError(
                "selected_project_count_delta must equal "
                "right_selected_project_count - left_selected_project_count"
            )

        effort_fields = (
            self.left_selected_duration_seconds,
            self.right_selected_duration_seconds,
            self.selected_duration_delta_seconds,
            self.left_remaining_duration_seconds,
            self.right_remaining_duration_seconds,
            self.remaining_duration_delta_seconds,
        )

        total = self.total_duration_seconds
        if total is None or total == 0:
            # Incomplete domain (total is None) or zero-total domain
            # (total == 0): V1.31 provides no coverage scalars in these
            # states, so NO numeric effort duration or delta may be
            # exposed — all six effort fields are exactly None.
            if any(field is not None for field in effort_fields):
                if total is None:
                    message = (
                        "an incomplete comparison "
                        "(total_duration_seconds is None) may not expose "
                        "any numeric effort duration or delta"
                    )
                else:
                    message = (
                        "a zero-total comparison "
                        "(total_duration_seconds == 0) may not expose any "
                        "numeric effort duration or delta"
                    )
                raise ValueError(message)
            return self

        # Positive-total domain: ALL six effort fields must be present.
        for field, name in zip(
            effort_fields,
            (
                "left_selected_duration_seconds",
                "right_selected_duration_seconds",
                "selected_duration_delta_seconds",
                "left_remaining_duration_seconds",
                "right_remaining_duration_seconds",
                "remaining_duration_delta_seconds",
            ),
            strict=True,
        ):
            if field is None:
                raise ValueError(
                    f"{name} must not be None for a positive-total "
                    "comparison"
                )

        left_selected = self.left_selected_duration_seconds
        right_selected = self.right_selected_duration_seconds
        left_remaining = self.left_remaining_duration_seconds
        right_remaining = self.right_remaining_duration_seconds

        if (
            left_selected is None
            or right_selected is None
            or left_remaining is None
            or right_remaining is None
        ):
            raise ValueError(
                "effort durations must not be None for a positive-total "
                "comparison"
            )

        if left_selected > total:
            raise ValueError(
                "left_selected_duration_seconds may not exceed "
                "total_duration_seconds"
            )
        if right_selected > total:
            raise ValueError(
                "right_selected_duration_seconds may not exceed "
                "total_duration_seconds"
            )
        if left_selected + left_remaining != total:
            raise ValueError(
                "left_selected_duration_seconds + "
                "left_remaining_duration_seconds must equal "
                "total_duration_seconds"
            )
        if right_selected + right_remaining != total:
            raise ValueError(
                "right_selected_duration_seconds + "
                "right_remaining_duration_seconds must equal "
                "total_duration_seconds"
            )

        if self.selected_duration_delta_seconds != (
            right_selected - left_selected
        ):
            raise ValueError(
                "selected_duration_delta_seconds must equal "
                "right_selected_duration_seconds - "
                "left_selected_duration_seconds"
            )
        if self.remaining_duration_delta_seconds != (
            right_remaining - left_remaining
        ):
            raise ValueError(
                "remaining_duration_delta_seconds must equal "
                "right_remaining_duration_seconds - "
                "left_remaining_duration_seconds"
            )

        # Conservation: both selections decompose the SAME authoritative
        # total, so the two deltas always cancel exactly.
        if (
            self.selected_duration_delta_seconds
            + self.remaining_duration_delta_seconds
            != 0
        ):
            raise ValueError(
                "selected_duration_delta_seconds + "
                "remaining_duration_delta_seconds must equal 0 (both "
                "selections describe the same authoritative total)"
            )

        return self


# ---------------------------------------------------------------------------
# Pure comparison boundary.
# ---------------------------------------------------------------------------


def compare_portfolio_effort_selections(
    left: PortfolioProjectEffortSelectionCoverage,
    right: PortfolioProjectEffortSelectionCoverage,
) -> PortfolioProjectEffortSelectionComparison:
    """Report the objective difference between two V1.31 focus selections.

    Steps:
      1. require TWO genuine V1.31
         ``PortfolioProjectEffortSelectionCoverage`` values (``None``,
         dicts, strings, foreign models, and duck types are rejected);
      2. freshly and strictly re-validate the COMPLETE payload of BOTH
         inputs (rejecting hostile ``model_construct`` values and hostile
         scalar types in either input);
      3. require the same authoritative comparison domain: equal
         ``portfolio_id``, equal ``source_project_count``, equal
         ``total_duration_seconds`` (including equal availability), and —
         for positive-total coverage — equal
         ``coverage_denominator_duration_seconds``;
      4. with exact integer arithmetic only (no division, no rounds, no
         percentages, no floats), report
         ``delta = right - left`` for selected project count, selected
         duration, and remaining duration — or, in the incomplete and
         zero-total domains, expose the shared domain metadata with every
         effort duration/delta exactly ``None``.

    V1.32 is comparison only: it never chooses, recommends, ranks, or
    labels the two selections as better / worse / preferred.  No I/O, no
    writes, no repository access, no V1.30 or earlier recomputation, no
    project rows, no classification or recommendation.  The inputs are
    never mutated and repeated calls are value-identical.
    """
    validated: dict[str, PortfolioProjectEffortSelectionCoverage] = {}
    for side, coverage in (("left", left), ("right", right)):
        if not isinstance(coverage, PortfolioProjectEffortSelectionCoverage):
            raise PortfolioProjectEffortSelectionComparisonError(
                "a genuine V1.31 PortfolioProjectEffortSelectionCoverage "
                f"is required for {side}, got {type(coverage).__name__}"
            )

        try:
            payload: dict[str, object] = coverage.to_payload()
        except (AttributeError, TypeError) as exc:
            raise PortfolioProjectEffortSelectionComparisonError(
                f"supplied {side} V1.31 coverage is not the V1.31 shape"
            ) from exc

        try:
            validated[side] = PortfolioProjectEffortSelectionCoverage.model_validate(
                payload, strict=True
            )
        except ValidationError as exc:
            raise PortfolioProjectEffortSelectionComparisonError(
                f"supplied {side} V1.31 coverage failed strict "
                "re-validation"
            ) from exc

    validated_left = validated["left"]
    validated_right = validated["right"]

    if validated_left.portfolio_id != validated_right.portfolio_id:
        raise PortfolioProjectEffortSelectionComparisonError(
            "left and right must describe the same portfolio"
        )
    if validated_left.source_project_count != validated_right.source_project_count:
        raise PortfolioProjectEffortSelectionComparisonError(
            "left and right must describe the same source_project_count"
        )

    left_total = validated_left.total_duration_seconds
    right_total = validated_right.total_duration_seconds

    if (left_total is None) != (right_total is None):
        raise PortfolioProjectEffortSelectionComparisonError(
            "left and right must have the same total availability "
            "(incomplete and complete domains cannot be mixed)"
        )
    if left_total != right_total:
        raise PortfolioProjectEffortSelectionComparisonError(
            "left and right must describe the same authoritative "
            "total_duration_seconds"
        )

    positive_total = left_total is not None and left_total > 0
    if positive_total:
        if (
            validated_left.coverage_denominator_duration_seconds
            != validated_right.coverage_denominator_duration_seconds
        ):
            raise PortfolioProjectEffortSelectionComparisonError(
                "left and right positive-total coverages must share the "
                "same coverage_denominator_duration_seconds"
            )
        left_selected = validated_left.selected_numerator_duration_seconds
        right_selected = validated_right.selected_numerator_duration_seconds
        if left_selected is None or right_selected is None:
            raise PortfolioProjectEffortSelectionComparisonError(
                "positive-total coverage must expose "
                "selected_numerator_duration_seconds"
            )
        left_remaining = validated_left.remaining_numerator_duration_seconds
        right_remaining = validated_right.remaining_numerator_duration_seconds
        if left_remaining is None or right_remaining is None:
            raise PortfolioProjectEffortSelectionComparisonError(
                "positive-total coverage must expose "
                "remaining_numerator_duration_seconds"
            )
        selected_delta = right_selected - left_selected
        remaining_delta = right_remaining - left_remaining
    else:
        # Incomplete (total is None) or zero-total domain: V1.31 provides
        # NO coverage scalars, so NO effort duration or delta is exposed.
        left_selected = None
        right_selected = None
        left_remaining = None
        right_remaining = None
        selected_delta = None
        remaining_delta = None

    return PortfolioProjectEffortSelectionComparison(
        portfolio_id=validated_left.portfolio_id,
        source_project_count=validated_left.source_project_count,
        total_duration_seconds=left_total,
        left_requested_limit=validated_left.requested_limit,
        right_requested_limit=validated_right.requested_limit,
        left_selected_project_count=validated_left.selected_project_count,
        right_selected_project_count=validated_right.selected_project_count,
        selected_project_count_delta=(
            validated_right.selected_project_count
            - validated_left.selected_project_count
        ),
        left_selected_duration_seconds=left_selected,
        right_selected_duration_seconds=right_selected,
        selected_duration_delta_seconds=selected_delta,
        left_remaining_duration_seconds=left_remaining,
        right_remaining_duration_seconds=right_remaining,
        remaining_duration_delta_seconds=remaining_delta,
    )
