"""V1.32 — Deterministic comparison of authoritative V1.31 effort selections.

Covers Issue #92 acceptance criteria:

* ``PortfolioProjectEffortSelectionComparison`` model invariants
  (strict/frozen/extra-forbid, strings/floats/bools rejected, count
  bounds, count delta exactly right - left, positive-total completeness,
  per-side decomposition of the shared total, exact positive/remaining
  delta identity, conservation selected delta + remaining delta == 0,
  negative and zero deltas accepted, contradictory unavailable/positive
  states rejected, zero-total and incomplete domains expose no effort
  scalars);
* ``compare_portfolio_effort_selections`` boundary:
  - genuine V1.31 left and right required (None/dicts/strings/foreign/
    duck types rejected);
  - hostile ``model_construct`` values rejected via fresh strict
    re-validation of BOTH inputs (hostile scalars, hostile invariants);
  - incompatible domains rejected (portfolio mismatch, source count
    mismatch, total mismatch, mixed availability, positive denominator
    mismatch);
  - exact right - left count/selected/remaining deltas;
  - widening focus (positive selected delta + equal negative remaining
    delta), narrowing focus (mirror image), equivalent selections (zero
    deltas);
  - differing requested limits preserved exactly;
  - common portfolio/source count/total mirrored exactly;
  - determinism, input immutability, payload shape (no project rows,
    percentages, ratios, scores, or labels/recommendations);
* public API export of the V1.32 surface;
* V1.32 imports V1.31 but NOT V1.30 / V1.29 / V1.28 / earlier production
  modules, and has no repository/provider/clock/randomness use.

The V1.31 coverages used here are genuine
``PortfolioProjectEffortSelectionCoverage`` states; hostile states use
``model_construct`` deliberately.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

import trajectory_os.application as app
from trajectory_os.application import (
    PortfolioProjectEffortSelectionComparison,
    PortfolioProjectEffortSelectionComparisonError,
    PortfolioProjectEffortSelectionCoverage,
    PortfolioProjectEffortSelectionSummary,
    compare_portfolio_effort_selections,
)

PROJECT_PORTFOLIO = uuid.uuid4()
OTHER_PORTFOLIO = uuid.uuid4()

TOTAL = 10000
TWO_HOURS = 2 * 3600

# Issue #90-style example kept exact and integer-only:
# portfolio total 10000s, 3 projects.
# left  focus: limit 1 -> 1 project selected, 6000s selected / 4000s
#       remaining.
# right focus: limit 2 -> 2 projects selected, 9000s selected / 1000s
#       remaining.
LEFT_SELECTED = 6000
LEFT_REMAINING = 4000
RIGHT_SELECTED = 9000
RIGHT_REMAINING = 1000


# ---------------------------------------------------------------------------
# Fixtures — genuine V1.31 coverages (the sole input authority).
# ---------------------------------------------------------------------------


def _left_coverage() -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=1,
        source_project_count=3,
        selected_project_count=1,
        total_duration_seconds=TOTAL,
        selected_numerator_duration_seconds=LEFT_SELECTED,
        coverage_denominator_duration_seconds=TOTAL,
        remaining_numerator_duration_seconds=LEFT_REMAINING,
    )


def _right_coverage() -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=2,
        source_project_count=3,
        selected_project_count=2,
        total_duration_seconds=TOTAL,
        selected_numerator_duration_seconds=RIGHT_SELECTED,
        coverage_denominator_duration_seconds=TOTAL,
        remaining_numerator_duration_seconds=RIGHT_REMAINING,
    )


def _identical_coverage() -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=4,
        source_project_count=3,
        selected_project_count=2,
        total_duration_seconds=TWO_HOURS,
        selected_numerator_duration_seconds=2 * 3600,
        coverage_denominator_duration_seconds=2 * 3600,
        remaining_numerator_duration_seconds=0,
    )


def _other_portfolio_coverage() -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=OTHER_PORTFOLIO,
        requested_limit=1,
        source_project_count=3,
        selected_project_count=1,
        total_duration_seconds=TOTAL,
        selected_numerator_duration_seconds=LEFT_SELECTED,
        coverage_denominator_duration_seconds=TOTAL,
        remaining_numerator_duration_seconds=LEFT_REMAINING,
    )


def _other_source_count_coverage() -> PortfolioProjectEffortSelectionCoverage:
    # 4 projects in the source, 1 selected with 6000s of a 10000s total.
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=1,
        source_project_count=4,
        selected_project_count=1,
        total_duration_seconds=TOTAL,
        selected_numerator_duration_seconds=LEFT_SELECTED,
        coverage_denominator_duration_seconds=TOTAL,
        remaining_numerator_duration_seconds=LEFT_REMAINING,
    )


def _other_total_coverage() -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=1,
        source_project_count=3,
        selected_project_count=1,
        total_duration_seconds=2 * TOTAL,
        selected_numerator_duration_seconds=LEFT_SELECTED,
        coverage_denominator_duration_seconds=2 * TOTAL,
        remaining_numerator_duration_seconds=2 * TOTAL - LEFT_SELECTED,
    )


def _incomplete_coverage() -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=1,
        source_project_count=3,
        selected_project_count=0,
        total_duration_seconds=None,
        selected_numerator_duration_seconds=None,
        coverage_denominator_duration_seconds=None,
        remaining_numerator_duration_seconds=None,
    )


def _zero_total_coverage() -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=1,
        source_project_count=3,
        selected_project_count=0,
        total_duration_seconds=0,
        selected_numerator_duration_seconds=None,
        coverage_denominator_duration_seconds=None,
        remaining_numerator_duration_seconds=None,
    )


def _empty_coverage() -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=1,
        source_project_count=0,
        selected_project_count=0,
        total_duration_seconds=0,
        selected_numerator_duration_seconds=None,
        coverage_denominator_duration_seconds=None,
        remaining_numerator_duration_seconds=None,
    )


# ---------------------------------------------------------------------------
# Model strictness and self-validation.
# ---------------------------------------------------------------------------


class TestComparisonModelStrictness:
    def test_config_is_strict_frozen_and_extra_forbid(self) -> None:
        config = PortfolioProjectEffortSelectionComparison.model_config
        assert config["strict"] is True
        assert config["frozen"] is True
        assert config["extra"] == "forbid"

    def test_frozen_instance_rejects_mutation(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _left_coverage(), _right_coverage()
        )
        with pytest.raises(ValidationError):
            comparison.selected_project_count_delta = (  # pyright: ignore
                99
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionComparison(
                portfolio_id=PROJECT_PORTFOLIO,
                source_project_count=3,
                total_duration_seconds=TOTAL,
                left_requested_limit=1,
                right_requested_limit=2,
                left_selected_project_count=1,
                right_selected_project_count=2,
                selected_project_count_delta=1,
                left_selected_duration_seconds=LEFT_SELECTED,
                right_selected_duration_seconds=RIGHT_SELECTED,
                selected_duration_delta_seconds=RIGHT_SELECTED - LEFT_SELECTED,
                left_remaining_duration_seconds=LEFT_REMAINING,
                right_remaining_duration_seconds=RIGHT_REMAINING,
                remaining_duration_delta_seconds=(
                    RIGHT_REMAINING - LEFT_REMAINING
                ),
                is_right_better=True,
            )

    @staticmethod
    def _valid_positive_kwargs() -> dict[str, object]:
        return dict(
            portfolio_id=PROJECT_PORTFOLIO,
            source_project_count=3,
            total_duration_seconds=TOTAL,
            left_requested_limit=1,
            right_requested_limit=2,
            left_selected_project_count=1,
            right_selected_project_count=2,
            selected_project_count_delta=1,
            left_selected_duration_seconds=LEFT_SELECTED,
            right_selected_duration_seconds=RIGHT_SELECTED,
            selected_duration_delta_seconds=RIGHT_SELECTED - LEFT_SELECTED,
            left_remaining_duration_seconds=LEFT_REMAINING,
            right_remaining_duration_seconds=RIGHT_REMAINING,
            remaining_duration_delta_seconds=(
                RIGHT_REMAINING - LEFT_REMAINING
            ),
        )

    def test_string_rejected_for_integer_fields(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["selected_project_count_delta"] = "1"
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_float_rejected_for_integer_fields(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["selected_duration_delta_seconds"] = 3000.0
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_bool_rejected_for_integer_fields(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["left_selected_project_count"] = True
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionComparison(**kwargs)
        kwargs = self._valid_positive_kwargs()
        kwargs["remaining_duration_delta_seconds"] = False
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_negative_source_project_count_rejected(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["source_project_count"] = -1
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_zero_requested_limit_rejected(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["left_requested_limit"] = 0
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_left_selected_count_exceeding_source_rejected(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["left_selected_project_count"] = 4
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_right_selected_count_exceeding_source_rejected(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["right_selected_project_count"] = 4
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_count_delta_must_equal_right_minus_left(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["selected_project_count_delta"] = 2
        with pytest.raises(ValidationError, match="left_selected_project_count"):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_count_delta_left_minus_right_rejected(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["selected_project_count_delta"] = -1
        with pytest.raises(ValidationError, match="left_selected_project_count"):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_negative_delta_accepted(self) -> None:
        # Orientation flipped: left=right focus, right=left focus.
        comparison = compare_portfolio_effort_selections(
            _right_coverage(), _left_coverage()
        )
        assert comparison.selected_project_count_delta == -1
        assert comparison.selected_duration_delta_seconds == (
            LEFT_SELECTED - RIGHT_SELECTED
        )
        assert comparison.selected_duration_delta_seconds < 0
        assert comparison.remaining_duration_delta_seconds == (
            LEFT_REMAINING - RIGHT_REMAINING
        )
        assert comparison.remaining_duration_delta_seconds > 0

    def test_zero_delta_accepted(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _identical_coverage(), _identical_coverage()
        )
        assert comparison.selected_project_count_delta == 0
        assert comparison.selected_duration_delta_seconds == 0
        assert comparison.remaining_duration_delta_seconds == 0

    def test_positive_total_missing_selected_duration_rejected(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["left_selected_duration_seconds"] = None
        with pytest.raises(ValidationError, match="left_selected"):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_positive_total_missing_delta_rejected(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["remaining_duration_delta_seconds"] = None
        with pytest.raises(ValidationError, match="remaining_duration_delta"):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_positive_total_zero_selected_remaining_breaks_total_rejected(
        self,
    ) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["left_remaining_duration_seconds"] = 0
        with pytest.raises(ValidationError, match="left_remaining"):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_left_side_must_decompose_total(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["left_remaining_duration_seconds"] = 7000
        with pytest.raises(ValidationError, match="left.* must equal"):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_right_side_must_decompose_total(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["right_remaining_duration_seconds"] = 5000
        with pytest.raises(ValidationError, match="right.* must equal"):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_selected_delta_must_equal_right_minus_left(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["selected_duration_delta_seconds"] = 123
        with pytest.raises(ValidationError, match="right_selected"):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_remaining_delta_must_equal_right_minus_left(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["remaining_duration_delta_seconds"] = 123
        with pytest.raises(ValidationError, match="right_remaining"):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_selected_duration_may_not_exceed_total(self) -> None:
        kwargs = self._valid_positive_kwargs()
        kwargs["right_selected_duration_seconds"] = 10001
        with pytest.raises(ValidationError, match="right_selected"):
            PortfolioProjectEffortSelectionComparison(**kwargs)

    def test_conservation_violation_rejected(self) -> None:
        # Both sides decompose TOTAL, but deltas that cannot cancel.
        kwargs = self._valid_positive_kwargs()
        kwargs["left_selected_duration_seconds"] = 5700
        kwargs["left_remaining_duration_seconds"] = 4300
        kwargs["selected_duration_delta_seconds"] = 3300
        kwargs["remaining_duration_delta_seconds"] = 3300
        with pytest.raises(ValidationError, match="right_remaining"):
            PortfolioProjectEffortSelectionComparison(**kwargs)


class TestUnavailableDomainStates:
    def test_incomplete_state_exposes_no_effort_scalars(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _incomplete_coverage(), _incomplete_coverage()
        )
        assert comparison.total_duration_seconds is None
        assert comparison.left_selected_project_count == 0
        assert comparison.right_selected_project_count == 0
        assert comparison.selected_project_count_delta == 0
        assert comparison.left_selected_duration_seconds is None
        assert comparison.right_selected_duration_seconds is None
        assert comparison.selected_duration_delta_seconds is None
        assert comparison.left_remaining_duration_seconds is None
        assert comparison.right_remaining_duration_seconds is None
        assert comparison.remaining_duration_delta_seconds is None

    def test_zero_total_state_preserves_zero_total(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _zero_total_coverage(), _zero_total_coverage()
        )
        assert comparison.total_duration_seconds == 0
        assert comparison.source_project_count == 3
        assert comparison.left_selected_duration_seconds is None
        assert comparison.selected_duration_delta_seconds is None
        assert comparison.remaining_duration_delta_seconds is None

    def test_empty_state_compares(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _empty_coverage(), _empty_coverage()
        )
        assert comparison.source_project_count == 0
        assert comparison.total_duration_seconds == 0
        assert comparison.selected_project_count_delta == 0
        assert comparison.selected_duration_delta_seconds is None
        assert comparison.remaining_duration_delta_seconds is None

    def test_incomplete_rejects_numeric_effort_scalars(self) -> None:
        with pytest.raises(
            ValidationError,
            match="incomplete comparison.* may not expose",
        ):
            PortfolioProjectEffortSelectionComparison(
                portfolio_id=PROJECT_PORTFOLIO,
                source_project_count=3,
                total_duration_seconds=None,
                left_requested_limit=1,
                right_requested_limit=1,
                left_selected_project_count=0,
                right_selected_project_count=0,
                selected_project_count_delta=0,
                left_selected_duration_seconds=0,
                right_selected_duration_seconds=0,
                selected_duration_delta_seconds=0,
                left_remaining_duration_seconds=0,
                right_remaining_duration_seconds=0,
                remaining_duration_delta_seconds=0,
            )

    def test_zero_total_rejects_numeric_effort_scalars(self) -> None:
        with pytest.raises(
            ValidationError,
            match="zero-total comparison.* may not expose",
        ):
            PortfolioProjectEffortSelectionComparison(
                portfolio_id=PROJECT_PORTFOLIO,
                source_project_count=3,
                total_duration_seconds=0,
                left_requested_limit=1,
                right_requested_limit=1,
                left_selected_project_count=0,
                right_selected_project_count=0,
                selected_project_count_delta=0,
                left_selected_duration_seconds=0,
                right_selected_duration_seconds=0,
                selected_duration_delta_seconds=0,
                left_remaining_duration_seconds=0,
                right_remaining_duration_seconds=0,
                remaining_duration_delta_seconds=0,
            )


# ---------------------------------------------------------------------------
# Boundary trust.
# ---------------------------------------------------------------------------


class TestBoundaryTrust:
    def test_left_must_be_genuine_v131_coverage(self) -> None:
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="left",
        ):
            compare_portfolio_effort_selections(None, _right_coverage())  # pyright: ignore
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="left",
        ):
            compare_portfolio_effort_selections(
                _left_coverage().to_payload(), _right_coverage()
            )
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="left",
        ):
            compare_portfolio_effort_selections(
                "left", _right_coverage()
            )  # pyright: ignore

    def test_right_must_be_genuine_v131_coverage(self) -> None:
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="right",
        ):
            compare_portfolio_effort_selections(_left_coverage(), None)  # pyright: ignore
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="right",
        ):
            compare_portfolio_effort_selections(
                _left_coverage(), _right_coverage().to_payload()
            )

    def test_foreign_model_with_same_shape_rejected(self) -> None:
        class Lookalike(BaseModel):
            model_config = ConfigDict(frozen=True, strict=True)

            portfolio_id: uuid.UUID
            requested_limit: int

        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
        ):
            compare_portfolio_effort_selections(
                Lookalike(portfolio_id=PROJECT_PORTFOLIO, requested_limit=1),  # pyright: ignore
                _right_coverage(),
            )

    def test_v130_summary_is_not_a_valid_side(self) -> None:
        summary = PortfolioProjectEffortSelectionSummary(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=3,
            selected_project_count=1,
            total_duration_seconds=TOTAL,
            selected_duration_seconds=LEFT_SELECTED,
            remaining_duration_seconds=LEFT_REMAINING,
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="left",
        ):
            compare_portfolio_effort_selections(summary, _right_coverage())  # pyright: ignore

    def test_hostile_left_model_construct_rejected(self) -> None:
        hostile = PortfolioProjectEffortSelectionCoverage.model_construct(
            **_left_coverage().model_dump()
            | {
                "selected_numerator_duration_seconds": 100,
                "remaining_numerator_duration_seconds": 200,
            }
        )
        assert (
            hostile.selected_numerator_duration_seconds
            + hostile.remaining_numerator_duration_seconds
            != hostile.coverage_denominator_duration_seconds
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="left.*re-validation",
        ):
            compare_portfolio_effort_selections(hostile, _right_coverage())

    def test_hostile_right_model_construct_rejected(self) -> None:
        hostile = PortfolioProjectEffortSelectionCoverage.model_construct(
            **_right_coverage().model_dump()
            | {"selected_project_count": 9999, "source_project_count": 3}
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="right.*re-validation",
        ):
            compare_portfolio_effort_selections(_left_coverage(), hostile)

    def test_hostile_bool_scalar_rejected(self) -> None:
        hostile = PortfolioProjectEffortSelectionCoverage.model_construct(
            **_left_coverage().model_dump()
            | {"requested_limit": True}
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="left.*re-validation",
        ):
            compare_portfolio_effort_selections(hostile, _right_coverage())

    def test_hostile_incomplete_with_selected_projects_rejected(self) -> None:
        hostile = PortfolioProjectEffortSelectionCoverage.model_construct(
            **_incomplete_coverage().model_dump() | {"selected_project_count": 3}
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="left.*re-validation",
        ):
            compare_portfolio_effort_selections(hostile, _left_coverage())

    def test_portfolio_mismatch_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="same portfolio",
        ):
            compare_portfolio_effort_selections(
                _other_portfolio_coverage(), _right_coverage()
            )

    def test_source_count_mismatch_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="source_project_count",
        ):
            compare_portfolio_effort_selections(
                _other_source_count_coverage(), _right_coverage()
            )

    def test_total_mismatch_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="total_duration_seconds",
        ):
            compare_portfolio_effort_selections(
                _other_total_coverage(), _right_coverage()
            )

    def test_mixed_incomplete_and_positive_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="availability",
        ):
            compare_portfolio_effort_selections(
                _incomplete_coverage(), _right_coverage()
            )

    def test_mixed_zero_total_and_positive_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="total_duration_seconds",
        ):
            compare_portfolio_effort_selections(
                _zero_total_coverage(), _right_coverage()
            )

    def test_mixed_incomplete_and_zero_total_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
            match="availability|total_duration_seconds",
        ):
            compare_portfolio_effort_selections(
                _incomplete_coverage(), _zero_total_coverage()
            )

    def test_positive_denominator_mismatch_rejected(self) -> None:
        # Hostile left shares the total with right but carries a different
        # denominator (and broken decomposition): fresh strict
        # re-validation of the left side must reject the comparison.
        hostile = PortfolioProjectEffortSelectionCoverage.model_construct(
            **_left_coverage().model_dump()
            | {"coverage_denominator_duration_seconds": TOTAL - 1}
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionComparisonError,
        ):
            compare_portfolio_effort_selections(hostile, _right_coverage())

    def test_inputs_are_never_mutated(self) -> None:
        left = _left_coverage()
        right = _right_coverage()
        before_left = left.to_payload()
        before_right = right.to_payload()
        compare_portfolio_effort_selections(left, right)
        assert left.to_payload() == before_left
        assert right.to_payload() == before_right

    def test_output_is_projected_only_from_revalidated_payload(self) -> None:
        # Trust boundary: after fresh strict re-validation, every semantic
        # read (compatibility AND projection) must use the re-validated
        # objects, never the original instances.  A genuine V1.31 subclass
        # whose own fields describe one valid state but whose to_payload()
        # exposes a DIFFERENT valid state discriminates the two: only the
        # re-validation payload may be authoritative.
        class _PayloadAuthoritativeCoverage(
            PortfolioProjectEffortSelectionCoverage
        ):
            def to_payload(self) -> dict[str, object]:  # pyright: ignore
                return _left_coverage().to_payload()

        hostile = _PayloadAuthoritativeCoverage(
            **_right_coverage().model_dump()
        )
        # The raw instance fields differ from its payload authority.
        assert hostile.requested_limit == 2
        assert hostile.to_payload() == _left_coverage().to_payload()

        comparison = compare_portfolio_effort_selections(
            hostile, _right_coverage()
        )
        # EVERY left-side read is projected from the re-validated payload
        # (left coverage: limit 1, count 1, 6000s selected), while the
        # right side comes from its own re-validated payload.
        assert comparison.portfolio_id == PROJECT_PORTFOLIO
        assert comparison.source_project_count == 3
        assert comparison.total_duration_seconds == TOTAL
        assert comparison.left_requested_limit == 1
        assert comparison.right_requested_limit == 2
        assert comparison.left_selected_project_count == 1
        assert comparison.right_selected_project_count == 2
        assert comparison.selected_project_count_delta == 1
        assert comparison.left_selected_duration_seconds == LEFT_SELECTED
        assert comparison.right_selected_duration_seconds == RIGHT_SELECTED
        assert comparison.selected_duration_delta_seconds == (
            RIGHT_SELECTED - LEFT_SELECTED
        )
        assert comparison.left_remaining_duration_seconds == LEFT_REMAINING
        assert comparison.right_remaining_duration_seconds == RIGHT_REMAINING
        assert comparison.remaining_duration_delta_seconds == (
            RIGHT_REMAINING - LEFT_REMAINING
        )


# ---------------------------------------------------------------------------
# Projection semantics.
# ---------------------------------------------------------------------------


class TestProjectionSemantics:
    def test_exact_count_and_effort_deltas_right_minus_left(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _left_coverage(), _right_coverage()
        )
        assert comparison.source_project_count == 3
        assert comparison.total_duration_seconds == TOTAL
        assert comparison.left_requested_limit == 1
        assert comparison.right_requested_limit == 2
        assert comparison.left_selected_project_count == 1
        assert comparison.right_selected_project_count == 2
        assert comparison.selected_project_count_delta == 1
        assert comparison.left_selected_duration_seconds == LEFT_SELECTED
        assert comparison.right_selected_duration_seconds == RIGHT_SELECTED
        assert comparison.selected_duration_delta_seconds == (
            RIGHT_SELECTED - LEFT_SELECTED
        )
        assert comparison.left_remaining_duration_seconds == LEFT_REMAINING
        assert comparison.right_remaining_duration_seconds == RIGHT_REMAINING
        assert comparison.remaining_duration_delta_seconds == (
            RIGHT_REMAINING - LEFT_REMAINING
        )

    def test_widening_focus_positive_selected_and_negative_remaining(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _left_coverage(), _right_coverage()
        )
        assert comparison.selected_project_count_delta > 0
        assert comparison.selected_duration_delta_seconds > 0
        assert comparison.remaining_duration_delta_seconds < 0
        assert comparison.selected_duration_delta_seconds == (
            -comparison.remaining_duration_delta_seconds
        )

    def test_narrowing_focus_negative_selected_and_positive_remaining(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _right_coverage(), _left_coverage()
        )
        assert comparison.selected_project_count_delta < 0
        assert comparison.selected_duration_delta_seconds < 0
        assert comparison.remaining_duration_delta_seconds > 0
        assert comparison.selected_duration_delta_seconds == (
            -comparison.remaining_duration_delta_seconds
        )

    def test_equivalent_selections_zero_deltas(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _identical_coverage(), _identical_coverage()
        )
        assert comparison.selected_project_count_delta == 0
        assert comparison.selected_duration_delta_seconds == 0
        assert comparison.remaining_duration_delta_seconds == 0

    def test_conservation_holds(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _left_coverage(), _right_coverage()
        )
        assert (
            comparison.selected_duration_delta_seconds
            + comparison.remaining_duration_delta_seconds
            == 0
        )

    def test_portfolios_are_mirrored(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _left_coverage(), _right_coverage()
        )
        assert comparison.portfolio_id == PROJECT_PORTFOLIO

    def test_deterministic_repeated_calls_are_value_identical(self) -> None:
        left = _left_coverage()
        right = _right_coverage()
        first = compare_portfolio_effort_selections(left, right)
        second = compare_portfolio_effort_selections(left, right)
        third = compare_portfolio_effort_selections(left, right)
        assert first == second == third
        assert (
            first.to_payload() == second.to_payload() == third.to_payload()
        )

    def test_payload_carries_exactly_the_intended_scalar_fields(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _left_coverage(), _right_coverage()
        )
        assert set(comparison.to_payload()) == {
            "portfolio_id",
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
        }

    def test_payload_has_no_project_rows_percentages_or_scores(self) -> None:
        comparison = compare_portfolio_effort_selections(
            _left_coverage(), _right_coverage()
        )
        payload = comparison.to_payload()
        assert "projects" not in payload
        count_fields = {
            "source_project_count",
            "left_selected_project_count",
            "right_selected_project_count",
            "selected_project_count_delta",
        }
        assert "ratio" not in payload
        for key in payload:
            if key in count_fields:
                continue
            lowered = key.lower()
            for banned in (
                "project",
                "percent",
                "share",
                "score",
                "better",
                "worse",
                "preferred",
                "recommend",
                "rank",
                "classification",
            ):
                assert banned not in lowered, key

    def test_payload_has_no_float_decimal_or_boolean_values(self) -> None:
        for state in (
            _left_coverage(),
            _right_coverage(),
            _incomplete_coverage(),
            _zero_total_coverage(),
        ):
            comparison = compare_portfolio_effort_selections(state, state)
            for value in comparison.to_payload().values():
                assert value is None or isinstance(
                    value, (int, uuid.UUID)
                ), value
                assert not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Production-module import discipline.
# ---------------------------------------------------------------------------


class TestImportDiscipline:
    def test_module_imports_v131_but_not_earlier_production_modules(self) -> None:
        from trajectory_os.application import (
            execution_effort_project_selection_comparison as module,
        )

        source = inspect.getsource(module)
        assert "execution_effort_project_selection_coverage" in source
        assert "execution_effort_project_selection_summary" not in source
        assert "execution_effort_project_top_selection" not in source
        assert "execution_effort_project_ranking" not in source
        assert "execution_effort_project_shares" not in source
        assert "execution_effort_project_contributions" not in source

    def test_module_has_no_repository_provider_clock_or_randomness_use(self) -> None:
        from trajectory_os.application import (
            execution_effort_project_selection_comparison as module,
        )

        source = inspect.getsource(module)
        for banned in (
            "import time",
            "import random",
            "datetime",
            "uuid4",
            "open(",
            "sqlite",
            "http",
        ):
            assert banned not in source.lower()


# ---------------------------------------------------------------------------
# Public API surface.
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_application_exports_v132_surface(self) -> None:
        assert "PortfolioProjectEffortSelectionComparison" in app.__all__
        assert "PortfolioProjectEffortSelectionComparisonError" in app.__all__
        assert "compare_portfolio_effort_selections" in app.__all__
        assert (
            app.PortfolioProjectEffortSelectionComparison
            is PortfolioProjectEffortSelectionComparison
        )
        assert (
            app.PortfolioProjectEffortSelectionComparisonError
            is PortfolioProjectEffortSelectionComparisonError
        )
        assert (
            app.compare_portfolio_effort_selections
            is compare_portfolio_effort_selections
        )
