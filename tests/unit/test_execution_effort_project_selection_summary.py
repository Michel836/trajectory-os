"""V1.30 — Deterministic selected-vs-remaining effort summary tests.

Covers:
* projected summary model invariants (strict/frozen/extra-forbid, exact
  integer fields, incomplete/full/no-selection scalar rules; rejections);
* ``summarize_selected_portfolio_project_effort`` boundary:
  - requires a genuine V1.29 selection (duck-typed/foreign inputs rejected);
  - rejects hostile ``model_construct`` values (top level, nested rank
    rows, nested exact shares) via fresh strict re-validation;
  - exact integer selected sum, exact remaining subtraction, and the
    ``selected + remaining == total`` guarantee;
  - full, partial, tie-expanded, zero-duration-row, arbitrary-order,
    incomplete, zero-total, and empty V1.29 states;
  - counts/``requested_limit`` mirroring, determinism, float-freeness,
    input immutability;
* public API export of the V1.30 surface.
"""

from __future__ import annotations

import types
import uuid

import pytest
from pydantic import ValidationError

import trajectory_os.application as app
from trajectory_os.application import (
    ExactProjectEffortShare,
    PortfolioProjectEffortRank,
    PortfolioProjectEffortRanking,
    PortfolioProjectEffortSelectionSummary,
    PortfolioProjectEffortSelectionSummaryError,
    PortfolioProjectEffortShare,
    PortfolioProjectEffortShareSummary,
    PortfolioProjectEffortTopSelection,
    rank_portfolio_project_effort,
    select_top_ranked_portfolio_project_effort,
    summarize_selected_portfolio_project_effort,
)

PROJECT_PORTFOLIO = uuid.uuid4()
TWO_HOURS = 2 * 3600
ONE_HOUR = 3600
THIRTY_MINUTES = 30 * 60


# ---------------------------------------------------------------------------
# Fixtures — real V1.27 → V1.28 → V1.29 data (never model_construct).
# ---------------------------------------------------------------------------


def _share_row(
    project_id: uuid.UUID, total: int, portfolio_total: int
) -> PortfolioProjectEffortShare:
    # Genuine exact share: numerator == project total, denominator == the
    # complete portfolio total (V1.27 invariants).
    return PortfolioProjectEffortShare(
        project_id=project_id,
        total_duration_seconds=total,
        share=ExactProjectEffortShare(
            numerator_duration_seconds=total,
            denominator_duration_seconds=portfolio_total,
        ),
    )


def _share_summary(project_totals: list[int]) -> PortfolioProjectEffortShareSummary:
    total = sum(project_totals)
    return PortfolioProjectEffortShareSummary(
        portfolio_id=PROJECT_PORTFOLIO,
        project_count=len(project_totals),
        total_duration_seconds=total,
        projects=tuple(_share_row(uuid.uuid4(), t, total) for t in project_totals),
    )


def _incomplete_share_summary() -> PortfolioProjectEffortShareSummary:
    # Genuine V1.27 unavailable state: at least one project total is None,
    # so no overall total may exist (None) and no shares may be exposed.
    return PortfolioProjectEffortShareSummary(
        portfolio_id=PROJECT_PORTFOLIO,
        project_count=2,
        total_duration_seconds=None,
        projects=(
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(), total_duration_seconds=ONE_HOUR
            ),
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(), total_duration_seconds=None
            ),
        ),
    )


def _zero_total_share_summary() -> PortfolioProjectEffortShareSummary:
    return PortfolioProjectEffortShareSummary(
        portfolio_id=PROJECT_PORTFOLIO,
        project_count=2,
        total_duration_seconds=0,
        projects=(
            PortfolioProjectEffortShare(project_id=uuid.uuid4(), total_duration_seconds=0),
            PortfolioProjectEffortShare(project_id=uuid.uuid4(), total_duration_seconds=0),
        ),
    )


def _empty_share_summary() -> PortfolioProjectEffortShareSummary:
    return PortfolioProjectEffortShareSummary(
        portfolio_id=PROJECT_PORTFOLIO,
        project_count=0,
        total_duration_seconds=0,
        projects=(),
    )


def _v128_ranking(project_totals: list[int]) -> PortfolioProjectEffortRanking:
    """A genuine V1.28 ranking built through V1.27 from real data."""
    return rank_portfolio_project_effort(_share_summary(project_totals))


def _v129_selection(project_totals: list[int], limit: int) -> PortfolioProjectEffortTopSelection:
    """A genuine V1.29 selection built through V1.28 from real data."""
    return select_top_ranked_portfolio_project_effort(
        _v128_ranking(project_totals), limit
    )


# ---------------------------------------------------------------------------
# Summary model (PortfolioProjectEffortSelectionSummary) invariants.
# ---------------------------------------------------------------------------


class TestProjectedSummaryModel:
    """The projected model enforces its scalar invariants on every
    construction path, including direct construction."""

    def test_strict_frozen_and_extra_forbid(self) -> None:
        summary = PortfolioProjectEffortSelectionSummary(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=ONE_HOUR,
            selected_duration_seconds=ONE_HOUR,
            remaining_duration_seconds=0,
        )
        assert summary.model_config["frozen"] is True
        assert summary.model_config["extra"] == "forbid"
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id="not-a-uuid",
                requested_limit=1,
                source_project_count=0,
                selected_project_count=0,
                total_duration_seconds=0,
                selected_duration_seconds=0,
                remaining_duration_seconds=0,
            )
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=summary.portfolio_id,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=0,
                unexpected_extra="not-allowed",
            )
        with pytest.raises(ValidationError):
            summary.requested_limit = 2  # type: ignore[misc]
        with pytest.raises(ValidationError):
            summary.selected_duration_seconds = 0  # type: ignore[misc]

    def test_rejects_string_and_float_scalars(self) -> None:
        base: dict[str, object] = {
            "portfolio_id": PROJECT_PORTFOLIO,
            "requested_limit": 1,
            "source_project_count": 1,
            "selected_project_count": 1,
            "total_duration_seconds": ONE_HOUR,
            "selected_duration_seconds": ONE_HOUR,
            "remaining_duration_seconds": 0,
        }
        for fields in (
            {"total_duration_seconds": "100"},
            {"total_duration_seconds": 10.0},
            {"selected_duration_seconds": "50"},
            {"remaining_duration_seconds": 0.0},
            {"selected_project_count": "1"},
            {"requested_limit": "1"},
        ):
            with pytest.raises(ValidationError):
                PortfolioProjectEffortSelectionSummary(**{**base, **fields})

    def test_rejects_bool_scalars(self) -> None:
        base: dict[str, object] = {
            "portfolio_id": PROJECT_PORTFOLIO,
            "requested_limit": 1,
            "source_project_count": 1,
            "selected_project_count": 1,
            "total_duration_seconds": ONE_HOUR,
            "selected_duration_seconds": ONE_HOUR,
            "remaining_duration_seconds": 0,
        }
        for fields in (
            {"total_duration_seconds": True},
            {"selected_duration_seconds": False},
            {"remaining_duration_seconds": True},
            {"selected_project_count": True},
            {"requested_limit": True},
        ):
            with pytest.raises(
                ValidationError,
                match="must not be a boolean|Input should be a valid integer",
            ):
                PortfolioProjectEffortSelectionSummary(**{**base, **fields})

    def test_rejects_selected_count_exceeding_source_count(self) -> None:
        with pytest.raises(
            ValidationError, match="may not exceed source_project_count"
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=2,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=0,
            )

    def test_rejects_incomplete_state_with_numeric_selected(self) -> None:
        with pytest.raises(
            ValidationError,
            match="selected_duration_seconds must not be exposed",
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=0,
                total_duration_seconds=None,
                selected_duration_seconds=0,
                remaining_duration_seconds=None,
            )

    def test_rejects_incomplete_state_with_numeric_remaining(self) -> None:
        with pytest.raises(
            ValidationError,
            match="remaining_duration_seconds must not be exposed",
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=0,
                total_duration_seconds=None,
                selected_duration_seconds=None,
                remaining_duration_seconds=0,
            )

    def test_rejects_incomplete_state_with_selected_rows(self) -> None:
        with pytest.raises(
            ValidationError, match="may not carry selected"
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=None,
                selected_duration_seconds=None,
                remaining_duration_seconds=None,
            )

    def test_rejects_complete_state_with_none_selected(self) -> None:
        with pytest.raises(
            ValidationError, match="selected_duration_seconds must not be None"
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=None,
                remaining_duration_seconds=0,
            )

    def test_rejects_complete_state_with_none_remaining(self) -> None:
        with pytest.raises(
            ValidationError, match="remaining_duration_seconds must not be None"
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=None,
            )

    def test_rejects_selected_exceeding_total(self) -> None:
        with pytest.raises(
            ValidationError, match="may not exceed the portfolio total"
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=TWO_HOURS,
                remaining_duration_seconds=0,
            )

    def test_rejects_selected_plus_remaining_neq_total(self) -> None:
        with pytest.raises(
            ValidationError, match="must equal total_duration_seconds"
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=ONE_HOUR,
            )

    def test_rejects_full_selection_with_nonzero_remaining(self) -> None:
        # A full selection cannot expose a remaining duration: with
        # complete scalars the exact decomposition and the full-selection
        # invariants jointly reject any such state.
        with pytest.raises(
            ValidationError,
            match="total_duration_seconds|remaining_duration_seconds",
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=2,
                total_duration_seconds=TWO_HOURS,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=ONE_HOUR,
            )

    def test_rejects_full_selection_with_selected_neq_total(self) -> None:
        with pytest.raises(
            ValidationError, match="selected_duration_seconds == total_duration_seconds"
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=2,
                total_duration_seconds=TWO_HOURS,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=ONE_HOUR,
            )

    def test_rejects_empty_source_with_nonzero_total(self) -> None:
        # source_project_count == 0 must imply every duration total is
        # exactly 0: an empty V1.29 selection carries no duration at all.
        with pytest.raises(
            ValidationError, match="a nonzero total"
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=0,
                selected_project_count=0,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=0,
            )

    def test_rejects_zero_total_with_selected_projects(self) -> None:
        # total_duration_seconds == 0 must imply no selected projects: a
        # zero-total V1.29 selection never selects any project.
        with pytest.raises(
            ValidationError, match="may not carry selected projects"
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=0,
                selected_duration_seconds=0,
                remaining_duration_seconds=0,
            )

    def test_rejects_positive_total_non_empty_with_no_selected_projects(
        self,
    ) -> None:
        # A positive-total, non-empty V1.29 selection always selects at
        # least one project, so selected_project_count == 0 is impossible
        # here.
        with pytest.raises(
            ValidationError,
            match="at least one selected project",
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=3,
                selected_project_count=0,
                total_duration_seconds=TWO_HOURS,
                selected_duration_seconds=0,
                remaining_duration_seconds=TWO_HOURS,
            )

    def test_rejects_no_selection_with_nonzero_selected_duration(self) -> None:
        with pytest.raises(
            ValidationError, match="no-selection summary may not carry"
        ):
            PortfolioProjectEffortSelectionSummary(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=3,
                selected_project_count=0,
                total_duration_seconds=TWO_HOURS,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=ONE_HOUR,
            )

    def test_accepts_valid_states(self) -> None:
        # Incomplete.
        incomplete = PortfolioProjectEffortSelectionSummary(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=2,
            selected_project_count=0,
            total_duration_seconds=None,
            selected_duration_seconds=None,
            remaining_duration_seconds=None,
        )
        assert incomplete.total_duration_seconds is None
        assert incomplete.selected_duration_seconds is None
        assert incomplete.remaining_duration_seconds is None

        # Empty.
        empty = PortfolioProjectEffortSelectionSummary(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=0,
            selected_project_count=0,
            total_duration_seconds=0,
            selected_duration_seconds=0,
            remaining_duration_seconds=0,
        )
        assert empty.to_payload()["total_duration_seconds"] == 0

        # Complete zero-total.
        zero_total = PortfolioProjectEffortSelectionSummary(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=2,
            selected_project_count=0,
            total_duration_seconds=0,
            selected_duration_seconds=0,
            remaining_duration_seconds=0,
        )
        assert zero_total.remaining_duration_seconds == 0

        # Complete partial selection.
        partial = PortfolioProjectEffortSelectionSummary(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=3,
            selected_project_count=1,
            total_duration_seconds=TWO_HOURS,
            selected_duration_seconds=ONE_HOUR,
            remaining_duration_seconds=ONE_HOUR,
        )
        assert partial.selected_duration_seconds + partial.remaining_duration_seconds == TWO_HOURS

        # Complete full selection.
        full = PortfolioProjectEffortSelectionSummary(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=ONE_HOUR,
            selected_duration_seconds=ONE_HOUR,
            remaining_duration_seconds=0,
        )
        assert full.selected_duration_seconds == full.total_duration_seconds


# ---------------------------------------------------------------------------
# Boundary: summarize_selected_portfolio_project_effort.
# ---------------------------------------------------------------------------


class TestBoundaryInput:
    def test_requires_genuine_v129_selection(self) -> None:
        selection = _v129_selection([ONE_HOUR, THIRTY_MINUTES], limit=1)
        with pytest.raises(
            PortfolioProjectEffortSelectionSummaryError, match="genuine V1.29"
        ):
            summarize_selected_portfolio_project_effort(None)  # type: ignore[arg-type]
        with pytest.raises(
            PortfolioProjectEffortSelectionSummaryError, match="genuine V1.29"
        ):
            summarize_selected_portfolio_project_effort("nope")  # type: ignore[arg-type]
        with pytest.raises(
            PortfolioProjectEffortSelectionSummaryError, match="genuine V1.29"
        ):
            summarize_selected_portfolio_project_effort(  # type: ignore[arg-type]
                selection.to_payload()
            )
        with pytest.raises(
            PortfolioProjectEffortSelectionSummaryError, match="genuine V1.29"
        ):
            summarize_selected_portfolio_project_effort(  # type: ignore[arg-type]
                _v128_ranking([ONE_HOUR])
            )
        # A duck-typed object exposing the same attribute names is foreign
        # and must be rejected on type.
        duck = types.SimpleNamespace(**selection.to_payload())
        with pytest.raises(
            PortfolioProjectEffortSelectionSummaryError, match="genuine V1.29"
        ):
            summarize_selected_portfolio_project_effort(duck)  # type: ignore[arg-type]

    def test_rejects_hostile_top_level_selection(self) -> None:
        # Hostile model_construct values that bypass V1.29 construction
        # invariants must be rejected by fresh strict re-validation.
        # (a) no-selection state with a positive portfolio total.
        hostile = PortfolioProjectEffortTopSelection.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=2,
            selected_project_count=0,
            total_duration_seconds=ONE_HOUR,
            projects=(),
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionSummaryError, match="strict re-validation"
        ):
            summarize_selected_portfolio_project_effort(hostile)

        # (b) selected count exceeding the source count.
        hostile_count = PortfolioProjectEffortTopSelection.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=2,
            total_duration_seconds=ONE_HOUR,
            projects=(),
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionSummaryError, match="strict re-validation"
        ):
            summarize_selected_portfolio_project_effort(hostile_count)

        # (c) boolean where a strict integer is required.
        hostile_bool = PortfolioProjectEffortTopSelection.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=True,  # type: ignore[arg-type]
            source_project_count=0,
            selected_project_count=0,
            total_duration_seconds=0,
            projects=(),
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionSummaryError, match="strict re-validation"
        ):
            summarize_selected_portfolio_project_effort(hostile_bool)

    def test_rejects_hostile_nested_rank_row(self) -> None:
        # A constructed row with a rank but no complete total/share is a
        # V1.29 invariant violation that only fresh revalidation can catch.
        hostile_row = PortfolioProjectEffortRank.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=None,
            rank=1,
            share=None,
        )
        hostile_selection = PortfolioProjectEffortTopSelection.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=ONE_HOUR,
            projects=(hostile_row,),
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionSummaryError, match="strict re-validation"
        ):
            summarize_selected_portfolio_project_effort(hostile_selection)

    def test_rejects_hostile_nested_exact_share(self) -> None:
        # A constructed exact share whose numerator exceeds its denominator
        # violates the exact-share invariant and must be rejected.
        hostile_share = ExactProjectEffortShare.model_construct(
            numerator_duration_seconds=TWO_HOURS,
            denominator_duration_seconds=ONE_HOUR,
        )
        hostile_row = PortfolioProjectEffortRank.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=ONE_HOUR,
            rank=1,
            share=hostile_share,
        )
        hostile_selection = PortfolioProjectEffortTopSelection.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=ONE_HOUR,
            projects=(hostile_row,),
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionSummaryError, match="strict re-validation"
        ):
            summarize_selected_portfolio_project_effort(hostile_selection)


class TestBoundarySemantics:
    def test_partial_selection_exact_integer_sum(self) -> None:
        selection = _v129_selection([TWO_HOURS, ONE_HOUR, THIRTY_MINUTES], limit=1)
        summary = summarize_selected_portfolio_project_effort(selection)
        assert summary.portfolio_id == selection.portfolio_id
        assert summary.requested_limit == selection.requested_limit
        assert summary.source_project_count == selection.source_project_count
        assert summary.selected_project_count == selection.selected_project_count
        assert summary.total_duration_seconds == TWO_HOURS + ONE_HOUR + THIRTY_MINUTES
        assert summary.selected_duration_seconds == TWO_HOURS
        assert summary.remaining_duration_seconds == ONE_HOUR + THIRTY_MINUTES
        assert (
            summary.selected_duration_seconds
            + summary.remaining_duration_seconds
            == summary.total_duration_seconds
        )
        assert summary.selected_duration_seconds >= 0
        assert summary.remaining_duration_seconds >= 0

    def test_full_selection(self) -> None:
        selection = _v129_selection([ONE_HOUR, THIRTY_MINUTES], limit=5)
        summary = summarize_selected_portfolio_project_effort(selection)
        assert summary.selected_project_count == selection.source_project_count
        assert summary.selected_project_count == 2
        assert summary.selected_duration_seconds == (
            summary.total_duration_seconds
        )
        assert summary.remaining_duration_seconds == 0

    def test_tie_expanded_v129_selection_is_summed_as_is(self) -> None:
        # Two projects tie at ONE_HOUR; V1.29 with limit=1 must include the
        # whole tie (2 rows).  V1.30 simply sums what V1.29 supplied.
        selection = _v129_selection([ONE_HOUR, ONE_HOUR, THIRTY_MINUTES], limit=1)
        assert selection.requested_limit == 1
        assert selection.selected_project_count == 2
        summary = summarize_selected_portfolio_project_effort(selection)
        assert summary.requested_limit == 1
        assert summary.selected_project_count == 2
        assert summary.selected_duration_seconds == 2 * ONE_HOUR
        assert summary.remaining_duration_seconds == THIRTY_MINUTES
        assert (
            summary.selected_duration_seconds
            + summary.remaining_duration_seconds
            == summary.total_duration_seconds
        )

    def test_selected_zero_duration_row_contributes_exact_zero(self) -> None:
        selection = _v129_selection([ONE_HOUR, 0], limit=2)
        assert selection.selected_project_count == 2
        summary = summarize_selected_portfolio_project_effort(selection)
        assert summary.total_duration_seconds == ONE_HOUR
        assert summary.selected_duration_seconds == ONE_HOUR
        assert summary.remaining_duration_seconds == 0

    def test_arbitrary_v129_row_order_yields_identical_summary(self) -> None:
        # Same multiset of project totals in different authoritative orders;
        # the scalar summary of the full selection must be value-identical.
        summary_a = summarize_selected_portfolio_project_effort(
            _v129_selection([ONE_HOUR, TWO_HOURS, THIRTY_MINUTES], limit=10)
        )
        summary_b = summarize_selected_portfolio_project_effort(
            _v129_selection([THIRTY_MINUTES, ONE_HOUR, TWO_HOURS], limit=10)
        )
        summary_c = summarize_selected_portfolio_project_effort(
            _v129_selection([THIRTY_MINUTES, TWO_HOURS, ONE_HOUR], limit=10)
        )
        assert summary_a.total_duration_seconds == summary_b.total_duration_seconds
        assert summary_b.total_duration_seconds == summary_c.total_duration_seconds
        assert summary_a.selected_duration_seconds == ONE_HOUR + TWO_HOURS + THIRTY_MINUTES
        assert summary_a.to_payload() == {
            "portfolio_id": PROJECT_PORTFOLIO,
            "requested_limit": 10,
            "source_project_count": 3,
            "selected_project_count": 3,
            "total_duration_seconds": ONE_HOUR + TWO_HOURS + THIRTY_MINUTES,
            "selected_duration_seconds": ONE_HOUR + TWO_HOURS + THIRTY_MINUTES,
            "remaining_duration_seconds": 0,
        }
        assert summary_b == summary_a
        assert summary_c == summary_a

    def test_incomplete_selection_exposes_no_scalar(self) -> None:
        selection = select_top_ranked_portfolio_project_effort(
            rank_portfolio_project_effort(_incomplete_share_summary()), 1
        )
        summary = summarize_selected_portfolio_project_effort(selection)
        assert summary.portfolio_id == PROJECT_PORTFOLIO
        assert summary.requested_limit == 1
        assert summary.source_project_count == 2
        assert summary.selected_project_count == 0
        assert summary.total_duration_seconds is None
        assert summary.selected_duration_seconds is None
        assert summary.remaining_duration_seconds is None

    def test_zero_total_selection(self) -> None:
        selection = select_top_ranked_portfolio_project_effort(
            rank_portfolio_project_effort(_zero_total_share_summary()), 1
        )
        summary = summarize_selected_portfolio_project_effort(selection)
        assert summary.source_project_count == 2
        assert summary.selected_project_count == 0
        assert summary.total_duration_seconds == 0
        assert summary.selected_duration_seconds == 0
        assert summary.remaining_duration_seconds == 0

    def test_empty_selection(self) -> None:
        selection = select_top_ranked_portfolio_project_effort(
            rank_portfolio_project_effort(_empty_share_summary()), 1
        )
        summary = summarize_selected_portfolio_project_effort(selection)
        assert summary.source_project_count == 0
        assert summary.selected_project_count == 0
        assert summary.total_duration_seconds == 0
        assert summary.selected_duration_seconds == 0
        assert summary.remaining_duration_seconds == 0

    def test_scalars_are_exact_ints(self) -> None:
        selection = _v129_selection([TWO_HOURS, ONE_HOUR], limit=1)
        summary = summarize_selected_portfolio_project_effort(selection)
        payload = summary.to_payload()
        for field in (
            "requested_limit",
            "source_project_count",
            "selected_project_count",
            "total_duration_seconds",
            "selected_duration_seconds",
            "remaining_duration_seconds",
        ):
            value = payload[field]
            assert not isinstance(value, bool)
            assert isinstance(value, int)

    def test_payload_has_only_seven_scalar_fields(self) -> None:
        selection = _v129_selection([ONE_HOUR, THIRTY_MINUTES], limit=1)
        summary = summarize_selected_portfolio_project_effort(selection)
        assert set(summary.to_payload()) == {
            "portfolio_id",
            "requested_limit",
            "source_project_count",
            "selected_project_count",
            "total_duration_seconds",
            "selected_duration_seconds",
            "remaining_duration_seconds",
        }


class TestBoundaryBehavior:
    def test_deterministic_repeated_calls_are_value_identical(self) -> None:
        selection = _v129_selection(
            [TWO_HOURS, ONE_HOUR, ONE_HOUR, THIRTY_MINUTES], limit=2
        )
        first = summarize_selected_portfolio_project_effort(selection)
        second = summarize_selected_portfolio_project_effort(selection)
        third = summarize_selected_portfolio_project_effort(selection)
        assert first == second == third
        assert first.to_payload() == second.to_payload() == third.to_payload()

    def test_input_is_never_mutated(self) -> None:
        selection = _v129_selection([TWO_HOURS, ONE_HOUR], limit=1)
        before = selection.to_payload()
        summarize_selected_portfolio_project_effort(selection)
        assert selection.to_payload() == before
        assert selection.selected_project_count == 1
        assert len(selection.projects) == 1


# ---------------------------------------------------------------------------
# Public API surface.
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_application_exports_v130_surface(self) -> None:
        assert "PortfolioProjectEffortSelectionSummary" in app.__all__
        assert "PortfolioProjectEffortSelectionSummaryError" in app.__all__
        assert "summarize_selected_portfolio_project_effort" in app.__all__
        assert (
            app.PortfolioProjectEffortSelectionSummary
            is PortfolioProjectEffortSelectionSummary
        )
        assert (
            app.PortfolioProjectEffortSelectionSummaryError
            is PortfolioProjectEffortSelectionSummaryError
        )
        assert (
            app.summarize_selected_portfolio_project_effort
            is summarize_selected_portfolio_project_effort
        )
