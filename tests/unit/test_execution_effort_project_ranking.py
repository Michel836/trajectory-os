"""Focused V1.28 — Exact project execution-effort ranking projection tests."""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from trajectory_os.application import (
    ExactProjectEffortShare,
    PortfolioProjectEffortContributionSummary,
    PortfolioProjectEffortRank,
    PortfolioProjectEffortRanking,
    PortfolioProjectEffortRankingError,
    PortfolioProjectEffortShare,
    PortfolioProjectEffortShareSummary,
    rank_portfolio_project_effort,
)

FOUR_HOURS = 14_400
TWO_HOURS = 7_200
ONE_HOUR = 3_600


def _share(
    numerator: int,
    denominator: int,
) -> ExactProjectEffortShare:
    return ExactProjectEffortShare(
        numerator_duration_seconds=numerator,
        denominator_duration_seconds=denominator,
    )


def _row(
    project_id: uuid.UUID,
    total: int | None,
    portfolio_total: int | None = None,
) -> PortfolioProjectEffortShare:
    share = (
        _share(total, portfolio_total)
        if total is not None
        and portfolio_total is not None
        and portfolio_total > 0
        else None
    )
    return PortfolioProjectEffortShare(
        project_id=project_id,
        total_duration_seconds=total,
        share=share,
    )


def _summary(
    portfolio_id: uuid.UUID,
    rows: list[PortfolioProjectEffortShare],
    total: int | None,
) -> PortfolioProjectEffortShareSummary:
    return PortfolioProjectEffortShareSummary(
        portfolio_id=portfolio_id,
        project_count=len(rows),
        total_duration_seconds=total,
        projects=tuple(rows),
    )


def _dense_positive_summary(
    totals: list[int],
) -> PortfolioProjectEffortShareSummary:
    portfolio = uuid.uuid4()
    portfolio_total = sum(totals)
    rows = [_row(uuid.uuid4(), total, portfolio_total) for total in totals]
    return _summary(portfolio, rows, portfolio_total)


def _incomplete_positive_summary() -> PortfolioProjectEffortShareSummary:
    portfolio = uuid.uuid4()
    rows = [
        _row(uuid.uuid4(), TWO_HOURS, None),
        _row(uuid.uuid4(), TWO_HOURS, None),
        _row(uuid.uuid4(), ONE_HOUR, None),
        _row(uuid.uuid4(), None, None),
    ]
    return _summary(portfolio, rows, None)


def _zero_total_summary() -> PortfolioProjectEffortShareSummary:
    portfolio = uuid.uuid4()
    rows = [
        _row(uuid.uuid4(), 0, None),
        _row(uuid.uuid4(), 0, None),
    ]
    return _summary(portfolio, rows, 0)


def _empty_summary() -> PortfolioProjectEffortShareSummary:
    return _summary(uuid.uuid4(), [], 0)


# ---------------------------------------------------------------------------
# Model invariants.
# ---------------------------------------------------------------------------


class TestPortfolioProjectEffortRankInvariants:
    def _ranked_row(self, project_id: uuid.UUID) -> PortfolioProjectEffortRank:
        return PortfolioProjectEffortRank(
            project_id=project_id,
            total_duration_seconds=TWO_HOURS,
            rank=1,
            share=_share(TWO_HOURS, FOUR_HOURS),
        )

    def test_strict_frozen_and_extra_forbid(self) -> None:
        project_id = uuid.uuid4()
        row = self._ranked_row(project_id)
        with pytest.raises(ValidationError, match="frozen"):
            row.rank = 2  # type: ignore[misc]
        with pytest.raises(ValidationError, match="frozen"):
            row.total_duration_seconds = FOUR_HOURS  # type: ignore[misc]
        with pytest.raises(ValidationError, match="frozen"):
            row.share = None  # type: ignore[misc]
        with pytest.raises(ValidationError, match="frozen"):
            row.project_id = uuid.uuid4()  # type: ignore[misc]
        with pytest.raises(ValidationError, match="Extra inputs"):
            PortfolioProjectEffortRank(
                project_id=project_id,
                total_duration_seconds=TWO_HOURS,
                rank=1,
                share=_share(TWO_HOURS, FOUR_HOURS),
                label="bogus",
            )

    def test_rank_must_be_at_least_one_when_present(self) -> None:
        with pytest.raises(ValidationError, match="rank"):
            PortfolioProjectEffortRank(
                project_id=uuid.uuid4(),
                total_duration_seconds=TWO_HOURS,
                rank=0,
                share=_share(TWO_HOURS, FOUR_HOURS),
            )
        with pytest.raises(ValidationError, match="rank"):
            PortfolioProjectEffortRank(
                project_id=uuid.uuid4(),
                total_duration_seconds=TWO_HOURS,
                rank=-1,
                share=_share(TWO_HOURS, FOUR_HOURS),
            )

    def test_rejects_boolean_rank_and_total(self) -> None:
        for bad_rank in (True, False):
            with pytest.raises(ValidationError):
                PortfolioProjectEffortRank(
                    project_id=uuid.uuid4(),
                    total_duration_seconds=TWO_HOURS,
                    rank=bad_rank,
                    share=_share(TWO_HOURS, FOUR_HOURS),
                )
        with pytest.raises(ValidationError):
            PortfolioProjectEffortRank(
                project_id=uuid.uuid4(),
                total_duration_seconds=True,
                rank=None,
                share=None,
            )

    def test_allows_unranked_row(self) -> None:
        row = PortfolioProjectEffortRank(project_id=uuid.uuid4())
        assert row.total_duration_seconds is None
        assert row.rank is None
        assert row.share is None


class TestPortfolioProjectEffortRankingInvariants:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        portfolio = uuid.uuid4()
        row = PortfolioProjectEffortRank(
            project_id=uuid.uuid4(),
            total_duration_seconds=TWO_HOURS,
            rank=1,
            share=_share(TWO_HOURS, TWO_HOURS),
        )
        ranking = PortfolioProjectEffortRanking(
            portfolio_id=portfolio,
            project_count=1,
            total_duration_seconds=TWO_HOURS,
            projects=(row,),
        )
        with pytest.raises(ValidationError, match="frozen"):
            ranking.project_count = 2  # type: ignore[misc]
        with pytest.raises(ValidationError, match="frozen"):
            ranking.projects = ()  # type: ignore[misc]
        with pytest.raises(ValidationError, match="Extra inputs"):
            PortfolioProjectEffortRanking(
                portfolio_id=portfolio,
                project_count=1,
                total_duration_seconds=TWO_HOURS,
                projects=(row,),
                label="bogus",
            )

    def test_rejects_mismatched_project_count(self) -> None:
        with pytest.raises(ValidationError, match="does not equal the number"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=0,
                total_duration_seconds=0,
                projects=(
                    PortfolioProjectEffortRank(project_id=uuid.uuid4()),
                ),
            )

    def test_rejects_duplicate_project_ids(self) -> None:
        project_id = uuid.uuid4()
        with pytest.raises(ValidationError, match="duplicate project IDs"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=2,
                total_duration_seconds=None,
                projects=(
                    PortfolioProjectEffortRank(project_id=project_id),
                    PortfolioProjectEffortRank(project_id=project_id),
                ),
            )

    def test_empty_ranking_rejects_none_total(self) -> None:
        with pytest.raises(ValidationError, match="total_duration_seconds"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=0,
                total_duration_seconds=None,
                projects=(),
            )

    def test_empty_ranking_rejects_positive_total(self) -> None:
        with pytest.raises(ValidationError, match="total_duration_seconds"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=0,
                total_duration_seconds=TWO_HOURS,
                projects=(),
            )

    def test_incomplete_ranking_rejects_any_rank_or_share(self) -> None:
        with pytest.raises(ValidationError, match="rank|share"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=2,
                total_duration_seconds=None,
                projects=(
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        rank=1,
                        share=_share(TWO_HOURS, FOUR_HOURS),
                    ),
                    PortfolioProjectEffortRank(project_id=uuid.uuid4()),
                ),
            )

    def test_complete_zero_total_ranking_rejects_any_rank_or_share(self) -> None:
        with pytest.raises(
            ValidationError, match="zero-total portfolio cannot expose"
        ):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=0,
                projects=(
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=0,
                        rank=1,
                        share=_share(0, FOUR_HOURS),
                    ),
                ),
            )

    def test_rank_without_share_is_rejected(self) -> None:
        # A ranked row must always expose an exact share; a rank without a
        # share is rejected at the model edge, whatever the portfolio state.
        with pytest.raises(
            ValidationError, match="must expose an exact share"
        ):
            PortfolioProjectEffortRank(
                project_id=uuid.uuid4(),
                total_duration_seconds=0,
                rank=1,
                share=None,
            )

    def test_complete_zero_total_ranking_rejects_nonzero_project_total(self) -> None:
        with pytest.raises(
            ValidationError, match="every project total to be 0"
        ):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=0,
                projects=(
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=ONE_HOUR,
                        rank=None,
                        share=None,
                    ),
                ),
            )

    def test_rejects_partial_ranking_in_positive_portfolio(self) -> None:
        with pytest.raises(ValidationError, match="rank for every project"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=2,
                total_duration_seconds=TWO_HOURS,
                projects=(
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        rank=1,
                        share=_share(TWO_HOURS, TWO_HOURS),
                    ),
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=0,
                        rank=None,
                        share=None,
                    ),
                ),
            )

    def test_rejects_rank_mismatching_dense_order(self) -> None:
        # Ties must share a rank. Two distinct totals (7200, 3600) with the
        # exact complete total 10800: ranks must be {7200:1, 3600:2}; the
        # 3600 row claiming rank 1 is rejected as non-dense-consistent.
        with pytest.raises(ValidationError, match="exactly dense"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=2,
                total_duration_seconds=TWO_HOURS + ONE_HOUR,
                projects=(
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        rank=1,
                        share=_share(TWO_HOURS, TWO_HOURS + ONE_HOUR),
                    ),
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=ONE_HOUR,
                        rank=1,
                        share=_share(ONE_HOUR, TWO_HOURS + ONE_HOUR),
                    ),
                ),
            )

    def test_rejects_rank_gap_not_dense(self) -> None:
        # Distinct totals 14400 -> 1, 7200 -> 2, 3600 -> 3 (DENSE), never
        # 1/2/4 (sparse). Complete exact total is 25200.
        with pytest.raises(ValidationError, match="exactly dense"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=3,
                total_duration_seconds=(
                    FOUR_HOURS + TWO_HOURS + ONE_HOUR
                ),
                projects=(
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=FOUR_HOURS,
                        rank=1,
                        share=_share(
                            FOUR_HOURS, FOUR_HOURS + TWO_HOURS + ONE_HOUR
                        ),
                    ),
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        rank=2,
                        share=_share(
                            TWO_HOURS, FOUR_HOURS + TWO_HOURS + ONE_HOUR
                        ),
                    ),
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=ONE_HOUR,
                        rank=4,
                        share=_share(
                            ONE_HOUR, FOUR_HOURS + TWO_HOURS + ONE_HOUR
                        ),
                    ),
                ),
            )

    def test_rejects_rank_one_not_on_highest_total(self) -> None:
        # Rank 1 must sit on the highest exact total (7200), not on 3600.
        with pytest.raises(ValidationError, match="exactly dense"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=2,
                total_duration_seconds=TWO_HOURS + ONE_HOUR,
                projects=(
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=ONE_HOUR,
                        rank=1,
                        share=_share(ONE_HOUR, TWO_HOURS + ONE_HOUR),
                    ),
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        rank=2,
                        share=_share(TWO_HOURS, TWO_HOURS + ONE_HOUR),
                    ),
                ),
            )

    def test_rejects_total_mismatching_exact_sum(self) -> None:
        with pytest.raises(ValidationError, match="exact sum of project totals"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=FOUR_HOURS,
                projects=(
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        rank=1,
                        share=_share(TWO_HOURS, FOUR_HOURS),
                    ),
                ),
            )

    def test_rejects_share_numerator_mismatching_total(self) -> None:
        # A forged row whose share numerator disagrees with its project
        # total is constructable only by bypassing validation; fresh strict
        # revalidation of the nested row must reject it (the row pins the
        # share numerator to the project total).
        hostile_row = PortfolioProjectEffortRank.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=TWO_HOURS,
            rank=1,
            share=_share(ONE_HOUR, TWO_HOURS),
        )
        with pytest.raises(
            ValidationError, match="numerator_duration_seconds must equal"
        ):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=TWO_HOURS,
                projects=(hostile_row,),
            )

    def test_rejects_share_denominator_mismatching_total(self) -> None:
        with pytest.raises(ValidationError, match="share denominator"):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=TWO_HOURS,
                projects=(
                    PortfolioProjectEffortRank(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        rank=1,
                        share=_share(TWO_HOURS, FOUR_HOURS),
                    ),
                ),
            )

    def test_rejects_hostile_model_constructed_nested_share(self) -> None:
        # A hostile exact share (numerator > denominator) that bypassed
        # validation must be rejected by fresh strict revalidation of the
        # nested share model — never ranked.
        hostile_share = ExactProjectEffortShare.model_construct(
            numerator_duration_seconds=FOUR_HOURS,
            denominator_duration_seconds=TWO_HOURS,
        )
        row = PortfolioProjectEffortRank.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=TWO_HOURS,
            rank=1,
            share=hostile_share,
        )
        with pytest.raises(
            ValidationError, match="must not exceed denominator_duration_seconds"
        ):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=TWO_HOURS,
                projects=(row,),
            )

    def test_rejects_rank_row_with_unhashable_project_id(self) -> None:
        hostile = PortfolioProjectEffortRank.model_construct(
            project_id=["unhashable", "id"],
            total_duration_seconds=None,
            rank=None,
            share=None,
        )
        with pytest.raises(
            ValidationError,
            match="failed fresh strict revalidation",
        ):
            PortfolioProjectEffortRanking(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=None,
                projects=(hostile,),
            )

    def test_rejects_boolean_and_nonint_count_fields(self) -> None:
        portfolio = uuid.uuid4()
        for bad_count in (True, False, 1.5, "1"):
            with pytest.raises(ValidationError):
                PortfolioProjectEffortRanking(
                    portfolio_id=portfolio,
                    project_count=bad_count,
                    total_duration_seconds=0,
                    projects=(),
                )
        for bad_total in (True, False, -1, 1.5, "0"):
            with pytest.raises(ValidationError):
                PortfolioProjectEffortRanking(
                    portfolio_id=portfolio,
                    project_count=0,
                    total_duration_seconds=bad_total,
                    projects=(),
                )


# ---------------------------------------------------------------------------
# Boundary invariants.
# ---------------------------------------------------------------------------


class TestBoundaryInputGuards:
    def test_rejects_non_v127_inputs(self) -> None:
        cases: list[Any] = [
            "nope",
            None,
            uuid.uuid4(),
            {"portfolio_id": uuid.uuid4()},
            PortfolioProjectEffortContributionSummary(
                portfolio_id=uuid.uuid4(),
                project_count=0,
                projects=(),
            ),
        ]
        for candidate in cases:
            with pytest.raises(
                PortfolioProjectEffortRankingError,
                match="genuine V1\\.27",
            ):
                rank_portfolio_project_effort(candidate)  # type: ignore[call-overload]

    def test_rejects_noniterable_projects_of_a_constructed_summary(self) -> None:
        hostile = PortfolioProjectEffortShareSummary.model_construct(
            portfolio_id=uuid.uuid4(),
            project_count=1,
            projects=42,
        )
        with pytest.raises(
            PortfolioProjectEffortRankingError,
            match="not the V1\\.27 shape",
        ):
            rank_portfolio_project_effort(hostile)

    def test_rejects_foreign_share_types_within_nested_tuple(self) -> None:
        hostile = PortfolioProjectEffortShareSummary.model_construct(
            portfolio_id=uuid.uuid4(),
            project_count=1,
            projects=("not-a-share",),
        )
        with pytest.raises(
            PortfolioProjectEffortRankingError,
            match="failed strict re-validation",
        ):
            rank_portfolio_project_effort(hostile)


class TestBoundaryRevalidation:
    def test_rejects_top_level_wrong_project_count(self) -> None:
        hostile = PortfolioProjectEffortShareSummary.model_construct(
            portfolio_id=uuid.uuid4(),
            project_count=2,
            total_duration_seconds=TWO_HOURS,
            projects=(
                _row(uuid.uuid4(), TWO_HOURS, TWO_HOURS),
            ),
        )
        with pytest.raises(
            PortfolioProjectEffortRankingError,
            match="failed strict re-validation",
        ):
            rank_portfolio_project_effort(hostile)

    def test_rejects_forged_nested_project_share(self) -> None:
        # A forged row (valid share shape, total inconsistent with it) is
        # constructable only by bypassing validation; fresh strict
        # revalidation must reject it.
        forged_row = PortfolioProjectEffortShare.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=ONE_HOUR,
            share=_share(TWO_HOURS, FOUR_HOURS),
        )
        hostile = PortfolioProjectEffortShareSummary.model_construct(
            portfolio_id=uuid.uuid4(),
            project_count=1,
            total_duration_seconds=TWO_HOURS,
            projects=(forged_row,),
        )
        with pytest.raises(
            PortfolioProjectEffortRankingError,
            match="failed strict re-validation",
        ):
            rank_portfolio_project_effort(hostile)

    def test_rejects_hostile_nested_exact_share(self) -> None:
        # A hostile exact share (numerator > denominator) that bypassed
        # validation must be rejected, never ranked.
        hostile_share = ExactProjectEffortShare.model_construct(
            numerator_duration_seconds=FOUR_HOURS,
            denominator_duration_seconds=TWO_HOURS,
        )
        forged_row = PortfolioProjectEffortShare.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=FOUR_HOURS,
            share=hostile_share,
        )
        hostile = PortfolioProjectEffortShareSummary.model_construct(
            portfolio_id=uuid.uuid4(),
            project_count=1,
            total_duration_seconds=FOUR_HOURS,
            projects=(forged_row,),
        )
        with pytest.raises(
            PortfolioProjectEffortRankingError,
            match="failed strict re-validation",
        ):
            rank_portfolio_project_effort(hostile)

    def test_rejects_boolean_total_inside_constructed_row(self) -> None:
        forged_row = PortfolioProjectEffortShare.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=True,
            share=None,
        )
        hostile = PortfolioProjectEffortShareSummary.model_construct(
            portfolio_id=uuid.uuid4(),
            project_count=1,
            total_duration_seconds=TWO_HOURS,
            projects=(forged_row,),
        )
        with pytest.raises(
            PortfolioProjectEffortRankingError,
            match="failed strict re-validation",
        ):
            rank_portfolio_project_effort(hostile)

    def test_accepts_genuine_v127_summaries_end_to_end(self) -> None:
        for shares in (
            _dense_positive_summary([FOUR_HOURS, TWO_HOURS, TWO_HOURS, ONE_HOUR]),
            _incomplete_positive_summary(),
            _zero_total_summary(),
            _empty_summary(),
        ):
            ranking = rank_portfolio_project_effort(shares)
            assert ranking.portfolio_id == shares.portfolio_id


# ---------------------------------------------------------------------------
# Projection semantics.
# ---------------------------------------------------------------------------


class TestProjectionSemantics:
    def test_empty_portfolio_stays_empty(self) -> None:
        shares = _empty_summary()
        ranking = rank_portfolio_project_effort(shares)

        assert ranking.portfolio_id == shares.portfolio_id
        assert ranking.project_count == 0
        assert ranking.total_duration_seconds == 0
        assert ranking.projects == ()

    def test_single_positive_project_is_rank_one(self) -> None:
        shares = _dense_positive_summary([FOUR_HOURS])
        ranking = rank_portfolio_project_effort(shares)

        assert ranking.project_count == 1
        assert ranking.total_duration_seconds == FOUR_HOURS
        row = ranking.projects[0]
        assert row.rank == 1
        assert row.total_duration_seconds == FOUR_HOURS
        assert row.share is not None
        assert row.share.numerator_duration_seconds == FOUR_HOURS
        assert row.share.denominator_duration_seconds == FOUR_HOURS

    def test_descending_exact_effort_ordering(self) -> None:
        shares = _dense_positive_summary([FOUR_HOURS, TWO_HOURS, ONE_HOUR])
        ranking = rank_portfolio_project_effort(shares)

        assert [row.rank for row in ranking.projects] == [1, 2, 3]
        assert [
            row.total_duration_seconds for row in ranking.projects
        ] == [FOUR_HOURS, TWO_HOURS, ONE_HOUR]

    def test_dense_ties_and_next_distinct_effort_rank(self) -> None:
        shares = _dense_positive_summary(
            [FOUR_HOURS, TWO_HOURS, TWO_HOURS, ONE_HOUR]
        )
        ranking = rank_portfolio_project_effort(shares)

        assert [row.rank for row in ranking.projects] == [1, 2, 2, 3]
        distinct_ranks = {row.rank for row in ranking.projects}
        assert distinct_ranks == {1, 2, 3}

    def test_authoritative_v127_order_preserved_inside_ties(self) -> None:
        shares = _dense_positive_summary(
            [TWO_HOURS, ONE_HOUR, FOUR_HOURS, TWO_HOURS]
        )
        ranking = rank_portfolio_project_effort(shares)

        assert [row.project_id for row in ranking.projects] == [
            share.project_id for share in shares.projects
        ]
        assert [row.rank for row in ranking.projects] == [2, 3, 1, 2]
        by_id = {row.project_id: row for row in ranking.projects}
        tied = [
            by_id[shares.projects[index].project_id]
            for index in (0, 3)
        ]
        # Both TWO_HOURS projects share rank 2 while keeping V1.27 order.
        assert [row.rank for row in tied] == [2, 2]
        assert (
            tied[0].total_duration_seconds
            == tied[1].total_duration_seconds
            == TWO_HOURS
        )

    def test_exact_ids_totals_and_semantically_equivalent_shares(self) -> None:
        # Issue #84 contract: the projected share must be EXACTLY EQUAL (by
        # value) to the authoritative V1.27 share — a freshly constructed
        # equivalent share is permitted, and no Python object-identity
        # guarantee exists. Only value/semantic equivalence is asserted.
        shares = _dense_positive_summary(
            [FOUR_HOURS, TWO_HOURS, TWO_HOURS, ONE_HOUR]
        )
        ranking = rank_portfolio_project_effort(shares)
        portfolio_total = sum(t for t in (FOUR_HOURS, TWO_HOURS, TWO_HOURS, ONE_HOUR))

        assert [row.project_id for row in ranking.projects] == [
            share.project_id for share in shares.projects
        ]
        for row, share in zip(ranking.projects, shares.projects, strict=True):
            assert row.total_duration_seconds == share.total_duration_seconds
            assert row.rank is not None and row.rank >= 1
            assert row.share is not None
            assert row.share == share.share
            assert row.share.numerator_duration_seconds == (
                row.total_duration_seconds
            )
            assert row.share.denominator_duration_seconds == portfolio_total

    def test_zero_duration_project_ranks_after_positive_projects(self) -> None:
        shares = _dense_positive_summary(
            [TWO_HOURS, 0, ONE_HOUR]
        )
        ranking = rank_portfolio_project_effort(shares)

        by_id = {row.project_id: row for row in ranking.projects}
        zero_row = by_id[shares.projects[1].project_id]
        assert zero_row.total_duration_seconds == 0
        assert zero_row.rank is not None and zero_row.rank >= 1
        positive_ranks = [
            row.rank
            for row in ranking.projects
            if row.total_duration_seconds > 0
        ]
        assert all(
            rank < zero_row.rank for rank in positive_ranks if rank is not None
        )
        assert zero_row.rank == 3
        assert zero_row.share is not None
        assert zero_row.share.numerator_duration_seconds == 0
        assert zero_row.share.denominator_duration_seconds == (
            TWO_HOURS + ONE_HOUR
        )

    def test_all_equal_positive_projects_are_all_rank_one(self) -> None:
        shares = _dense_positive_summary([TWO_HOURS, TWO_HOURS, TWO_HOURS])
        ranking = rank_portfolio_project_effort(shares)

        assert [row.rank for row in ranking.projects] == [1, 1, 1]
        assert {rank for rank in (row.rank for row in ranking.projects)} == {1}

    def test_incomplete_portfolio_gets_no_partial_ranking(self) -> None:
        shares = _incomplete_positive_summary()
        ranking = rank_portfolio_project_effort(shares)

        assert ranking.total_duration_seconds is None
        assert ranking.project_count == 4
        assert len(ranking.projects) == 4
        for row, share in zip(ranking.projects, shares.projects, strict=True):
            assert row.rank is None
            assert row.share is None
            assert row.total_duration_seconds == (
                share.total_duration_seconds
            )

    def test_complete_zero_total_portfolio_gets_no_invented_ranking(self) -> None:
        shares = _zero_total_summary()
        ranking = rank_portfolio_project_effort(shares)

        assert ranking.total_duration_seconds == 0
        assert ranking.project_count == 2
        for row, share in zip(ranking.projects, shares.projects, strict=True):
            assert row.rank is None
            assert row.share is None
            assert row.total_duration_seconds == share.total_duration_seconds == 0

    def test_deterministic_repeated_projection(self) -> None:
        for shares in (
            _empty_summary(),
            _dense_positive_summary([FOUR_HOURS, TWO_HOURS, TWO_HOURS, ONE_HOUR]),
            _dense_positive_summary([ONE_HOUR, 0]),
            _dense_positive_summary([TWO_HOURS] * 3),
            _incomplete_positive_summary(),
            _zero_total_summary(),
        ):
            first = rank_portfolio_project_effort(shares)
            second = rank_portfolio_project_effort(shares)
            assert first.model_dump() == second.model_dump()

    def test_input_not_mutated(self) -> None:
        shares = _dense_positive_summary(
            [FOUR_HOURS, TWO_HOURS, TWO_HOURS, ONE_HOUR]
        )
        snapshot = copy.deepcopy(shares)
        rank_portfolio_project_effort(shares)
        assert shares.model_dump() == snapshot.model_dump()

    def test_ranking_is_exact_integers_without_floats(self) -> None:
        shares = _dense_positive_summary(
            [FOUR_HOURS, TWO_HOURS, TWO_HOURS, ONE_HOUR]
        )
        ranking = rank_portfolio_project_effort(shares)
        assert isinstance(ranking.total_duration_seconds, int)
        for row in ranking.projects:
            assert isinstance(row.project_id, uuid.UUID)
            assert isinstance(row.total_duration_seconds, int)
            assert isinstance(row.rank, int)
            assert row.share is not None
            assert isinstance(row.share.numerator_duration_seconds, int)
            assert isinstance(row.share.denominator_duration_seconds, int)

    def test_projected_models_carry_only_integer_fields(self) -> None:
        assert set(PortfolioProjectEffortRank.__pydantic_fields__) == {
            "project_id",
            "total_duration_seconds",
            "rank",
            "share",
        }
        assert set(PortfolioProjectEffortRanking.__pydantic_fields__) == {
            "portfolio_id",
            "project_count",
            "total_duration_seconds",
            "projects",
        }

    def test_public_api_exports(self) -> None:
        import trajectory_os.application as app

        for symbol in (
            "PortfolioProjectEffortRank",
            "PortfolioProjectEffortRanking",
            "PortfolioProjectEffortRankingError",
            "rank_portfolio_project_effort",
        ):
            assert symbol in app.__all__
            assert hasattr(app, symbol)
