"""V1.29 — Deterministic top-ranked project effort selection tests.

Covers:
* projected selection model invariants (strict/frozen/extra-forbid,
  count/source/limit consistency, unavailable/complete state rules,
  exact integer fields; rejections);
* ``select_top_ranked_portfolio_project_effort`` boundary:
  - rejects non-V1.28 and invalid ``limit``;
  - rejects hostile ``model_construct`` values (top level, nested rank
    rows, nested exact shares) via fresh strict re-validation;
  - preserves the exact V1.28 semantic ranking/order (no renumbering,
    no secondary re-sorting, value-exact IDs/totals/shares);
  - selects ``limit`` before tie expansion and expands complete dense-rank
    ties without splitting them;
  - preserves empty/incomplete/zero-total unavailable states exactly;
  - is deterministic, float-free, and never mutates the input;
* public API export of the V1.29 surface.
"""

from __future__ import annotations

import copy
import uuid

import pytest
from pydantic import ValidationError

from trajectory_os.application import (
    ExactProjectEffortShare,
    PortfolioProjectEffortRank,
    PortfolioProjectEffortRanking,
    PortfolioProjectEffortShare,
    PortfolioProjectEffortShareSummary,
    PortfolioProjectEffortTopSelection,
    PortfolioProjectEffortTopSelectionError,
    rank_portfolio_project_effort,
    select_top_ranked_portfolio_project_effort,
)

PROJECT_PORTFOLIO = uuid.uuid4()
TWO_HOURS = 2 * 3600
ONE_HOUR = 3600


# ---------------------------------------------------------------------------
# Fixtures — real V1.27 → V1.28 data (never model_construct).
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
        projects=tuple(
            _share_row(uuid.uuid4(), t, total) for t in project_totals
        ),
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


def _incomplete_v128_ranking() -> PortfolioProjectEffortRanking:
    return rank_portfolio_project_effort(_incomplete_share_summary())


def _zero_total_v128_ranking() -> PortfolioProjectEffortRanking:
    return rank_portfolio_project_effort(_zero_total_share_summary())


def _empty_v128_ranking() -> PortfolioProjectEffortRanking:
    return rank_portfolio_project_effort(_empty_share_summary())


def _ranked_row(total: int, rank: int, portfolio_total: int) -> PortfolioProjectEffortRank:
    # A valid V1.28 row: numerator == the row's own total, denominator the
    # summary total (>= the row total on complete states).
    return PortfolioProjectEffortRank(
        project_id=uuid.uuid4(),
        total_duration_seconds=total,
        rank=rank,
        share=ExactProjectEffortShare(
            numerator_duration_seconds=total,
            denominator_duration_seconds=portfolio_total,
        ),
    )


# ---------------------------------------------------------------------------
# Selection model (PortfolioProjectEffortTopSelection) invariants.
# ---------------------------------------------------------------------------


class TestProjectedSelectionModel:
    """The projected model enforces its consistency invariants on every
    construction path."""

    def test_strict_frozen_and_extra_forbid(self) -> None:
        selection = PortfolioProjectEffortTopSelection(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=ONE_HOUR,
            projects=(_ranked_row(ONE_HOUR, 1, ONE_HOUR),),
        )
        assert selection.model_config["frozen"] is True
        assert "extra" in selection.model_config
        with pytest.raises(ValidationError):
            PortfolioProjectEffortTopSelection(
                portfolio_id="not-a-uuid",
                requested_limit=1,
                source_project_count=0,
                selected_project_count=0,
                total_duration_seconds=0,
                projects=(),
            )
        with pytest.raises(ValidationError):
            PortfolioProjectEffortTopSelection(
                portfolio_id=selection.portfolio_id,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                projects=selection.projects,
                unexpected_extra="not-allowed",
            )
        with pytest.raises(ValidationError):
            selection.requested_limit = 2

    def test_projected_model_is_immutable(self) -> None:
        selection = PortfolioProjectEffortTopSelection(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=ONE_HOUR,
            projects=(_ranked_row(ONE_HOUR, 1, ONE_HOUR),),
        )
        with pytest.raises(ValidationError):
            selection.selected_project_count = 0  # type: ignore[misc]
        with pytest.raises(ValidationError):
            selection.total_duration_seconds = 0  # type: ignore[misc]

    def test_rejects_project_total_exceeding_portfolio(self) -> None:
        # A valid V1.28 row (total 2h, share 2h/2h) cannot belong to a
        # selection whose portfolio total is smaller (1h).
        with pytest.raises(
            ValidationError, match="must not exceed the portfolio total"
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                projects=(_ranked_row(TWO_HOURS, 1, TWO_HOURS),),
            )

    def test_rejects_mismatched_selected_count(self) -> None:
        with pytest.raises(
            ValidationError, match="does not equal the number of selected"
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=2,
                source_project_count=2,
                selected_project_count=2,
                total_duration_seconds=TWO_HOURS,
                projects=(),
            )

    def test_rejects_selected_count_gt_source(self) -> None:
        with pytest.raises(
            ValidationError, match="may not exceed source_project_count"
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=2,
                total_duration_seconds=TWO_HOURS,
                projects=(
                    _ranked_row(ONE_HOUR, 1, ONE_HOUR),
                    _ranked_row(ONE_HOUR, 2, TWO_HOURS),
                ),
            )

    def test_rejects_selection_not_starting_at_rank_one(self) -> None:
        with pytest.raises(
            ValidationError,
            match="dense top-ranked prefix|rank 1",
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=TWO_HOURS,
                projects=(
                    _ranked_row(ONE_HOUR, 2, TWO_HOURS),
                ),
            )

    def test_rejects_selected_rank_gap(self) -> None:
        with pytest.raises(
            ValidationError,
            match="dense top-ranked prefix",
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=2,
                source_project_count=3,
                selected_project_count=2,
                total_duration_seconds=3 * ONE_HOUR,
                projects=(
                    _ranked_row(TWO_HOURS, 1, 3 * ONE_HOUR),
                    _ranked_row(ONE_HOUR, 3, 3 * ONE_HOUR),
                ),
            )

    def test_rejects_share_denominator_mismatching_selection_total(self) -> None:
        row = _ranked_row(ONE_HOUR, 1, TWO_HOURS)

        with pytest.raises(
            ValidationError,
            match="share denominator.*portfolio total",
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                projects=(row,),
            )

    def test_rejects_duplicate_project_ids(self) -> None:
        duplicate_id = uuid.uuid4()
        share = ExactProjectEffortShare(
            numerator_duration_seconds=ONE_HOUR,
            denominator_duration_seconds=ONE_HOUR,
        )

        def _row() -> PortfolioProjectEffortRank:
            return PortfolioProjectEffortRank(
                project_id=duplicate_id,
                total_duration_seconds=ONE_HOUR,
                rank=1,
                share=share,
            )

        with pytest.raises(
            ValidationError, match="duplicate project IDs"
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=2,
                total_duration_seconds=TWO_HOURS,
                projects=(_row(), _row()),
            )

    def test_rejects_bool_fields(self) -> None:
        with pytest.raises(ValidationError, match="must not be a boolean"):
            PortfolioProjectEffortTopSelection.model_validate(
                {
                    "portfolio_id": PROJECT_PORTFOLIO,
                    "requested_limit": True,
                    "source_project_count": 1,
                    "selected_project_count": 1,
                    "total_duration_seconds": ONE_HOUR,
                    "projects": [_ranked_row(ONE_HOUR, 1, ONE_HOUR).model_dump(mode="python")],
                },
                strict=True,
            )
        with pytest.raises(ValidationError, match="must not be a boolean"):
            PortfolioProjectEffortTopSelection.model_validate(
                {
                    "portfolio_id": PROJECT_PORTFOLIO,
                    "requested_limit": 1,
                    "source_project_count": 1,
                    "selected_project_count": 1,
                    "total_duration_seconds": True,
                    "projects": [_ranked_row(ONE_HOUR, 1, ONE_HOUR).model_dump(mode="python")],
                },
                strict=True,
            )

    def test_rejects_non_int_fields(self) -> None:
        # float is NOT a strict integer and must be rejected.
        with pytest.raises(ValidationError):
            PortfolioProjectEffortTopSelection.model_validate(
                {
                    "portfolio_id": PROJECT_PORTFOLIO,
                    "requested_limit": 1.0,
                    "source_project_count": 1,
                    "selected_project_count": 1,
                    "total_duration_seconds": ONE_HOUR,
                    "projects": [_ranked_row(ONE_HOUR, 1, ONE_HOUR).model_dump(mode="python")],
                },
                strict=True,
            )
        with pytest.raises(ValidationError):
            PortfolioProjectEffortTopSelection.model_validate(
                {
                    "portfolio_id": PROJECT_PORTFOLIO,
                    "requested_limit": 1,
                    "source_project_count": "1",
                    "selected_project_count": 1,
                    "total_duration_seconds": ONE_HOUR,
                    "projects": [_ranked_row(ONE_HOUR, 1, ONE_HOUR).model_dump(mode="python")],
                },
                strict=True,
            )

    def test_rejects_total_incomplete_state_when_rows_present(self) -> None:
        with pytest.raises(
            ValidationError,
            match="total_duration_seconds.*None|None.*total_duration_seconds|incomplete|unavailable",
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=None,
                projects=(_ranked_row(ONE_HOUR, 1, ONE_HOUR),),
            )

    def test_rejects_zero_total_when_rows_present(self) -> None:
        with pytest.raises(
            ValidationError,
            match="zero.total|exactly zero|total.*0",
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=0,
                projects=(_ranked_row(ONE_HOUR, 1, ONE_HOUR),),
            )

    def test_rejects_unranked_or_shareless_ranked_project(self) -> None:
        # A top selection may not expose an unranked or share-less project.
        unranked = PortfolioProjectEffortRank.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=ONE_HOUR,
            rank=None,
            share=None,
        )
        with pytest.raises(
            ValidationError, match="ranked|rank"
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                projects=(unranked,),
            )

    def test_rejects_hostile_model_constructed_nested_share(self) -> None:
        # A hostile model_construct share (numerator > denominator) nested in
        # a selected row is NEVER trusted: fresh strict re-validation of the
        # nested share rejects it with the exact-share ceiling error.
        hostile_share = ExactProjectEffortShare.model_construct(
            numerator_duration_seconds=2 * ONE_HOUR,
            denominator_duration_seconds=ONE_HOUR,
        )
        hostile_row = PortfolioProjectEffortRank.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=ONE_HOUR,
            rank=1,
            share=hostile_share,
        )
        with pytest.raises(
            ValidationError,
            match="numerator_duration_seconds must not exceed",
        ):
            PortfolioProjectEffortTopSelection(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                projects=(hostile_row,),
            )


# ---------------------------------------------------------------------------
# boundary: select_top_ranked_portfolio_project_effort
# ---------------------------------------------------------------------------


class TestBoundaryGuards:
    def test_rejects_non_v128_inputs(self) -> None:
        real_v127_summary = _share_summary([ONE_HOUR, TWO_HOURS])
        cases = [
            (None, "V1\\.28 PortfolioProjectEffortRanking"),
            ("not-a-ranking", "V1\\.28"),
            (PROJECT_PORTFOLIO, "V1\\.28"),
            ({}, "V1\\.28"),
            # a genuine V1.27 summary is a different layer and must NOT be
            # accepted here (no re-derivation from V1.27 or below).
            (real_v127_summary, "V1\\.28"),
            # a single V1.28 rank row is also not a V1.28 ranking.
            (_ranked_row(ONE_HOUR, 1, ONE_HOUR), "V1\\.28"),
            # a top-level summary-ish V1.29 selection is not itself a ranking.
            (
                PortfolioProjectEffortTopSelection(
                    portfolio_id=PROJECT_PORTFOLIO,
                    requested_limit=1,
                    source_project_count=1,
                    selected_project_count=1,
                    total_duration_seconds=ONE_HOUR,
                    projects=(_ranked_row(ONE_HOUR, 1, ONE_HOUR),),
                ),
                "V1\\.28",
            ),
        ]
        for value, _pattern in cases:
            with pytest.raises(
                PortfolioProjectEffortTopSelectionError, match="V1\\.28"
            ):
                select_top_ranked_portfolio_project_effort(value, 1)  # type: ignore[arg-type]

    def test_rejects_invalid_limit(self) -> None:
        ranking = _v128_ranking([ONE_HOUR, TWO_HOURS])
        bad_limits = [0, -1, -TWO_HOURS, 1.5, "2", None, True, False]
        for bad in bad_limits:
            with pytest.raises(
                PortfolioProjectEffortTopSelectionError,
                match=r"limit .*(positive integer|\>= 1)",
            ):
                select_top_ranked_portfolio_project_effort(ranking, bad)  # type: ignore[arg-type]

    def test_rejects_hostile_model_constructed_v128_ranking(self) -> None:
        hostile = PortfolioProjectEffortRanking.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            project_count=2,
            total_duration_seconds=None,
            projects=(_ranked_row(ONE_HOUR, 1, ONE_HOUR),),  # count mismatch
        )
        with pytest.raises(
            PortfolioProjectEffortTopSelectionError,
            match=r"strict (re-)?validation|V1\.28",
        ):
            select_top_ranked_portfolio_project_effort(hostile, 1)

    def test_rejects_non_iterable_projects(self) -> None:
        hostile = PortfolioProjectEffortRanking.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            project_count=0,
            total_duration_seconds=0,
            projects=42,  # non-iterable / not the V1.28 shape
        )
        with pytest.raises(
            PortfolioProjectEffortTopSelectionError,
            match=r"(strict re-validation|not the V1\.28 shape|V1\.28)",
        ):
            select_top_ranked_portfolio_project_effort(hostile, 1)

    def test_rejects_v128_rank_row_inside_projects_tuple(self) -> None:
        hostiled = PortfolioProjectEffortRanking.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            project_count=2,
            total_duration_seconds=TWO_HOURS * 2,
            projects=("rank-row-not-a-dict",),
        )
        with pytest.raises(PortfolioProjectEffortTopSelectionError):
            select_top_ranked_portfolio_project_effort(hostiled, 1)

    def test_rejects_non_rank_inside_projects_tuple(self) -> None:
        hostiled = PortfolioProjectEffortRanking.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            project_count=1,
            total_duration_seconds=ONE_HOUR,
            projects=(42,),  # a raw int is not a V1.28 rank row
        )
        with pytest.raises(PortfolioProjectEffortTopSelectionError):
            select_top_ranked_portfolio_project_effort(hostiled, 1)


class TestBoundarySemantics:
    def test_selects_limit_before_tie_expansion_and_expands_ties_without_splitting(self) -> None:
        # Totals: A=2h (rank 1), B=1h (rank 2), C=1h (rank 2 tied with B).
        ranking = _v128_ranking([TWO_HOURS, ONE_HOUR, ONE_HOUR])
        selection_1 = select_top_ranked_portfolio_project_effort(ranking, 1)
        # limit=1 -> only the rank-1 project.
        assert selection_1.selected_project_count == 1
        assert len(selection_1.projects) == 1
        assert selection_1.projects[0].rank == 1

        ranking_2 = _v128_ranking([TWO_HOURS, ONE_HOUR, ONE_HOUR])
        selection_2 = select_top_ranked_portfolio_project_effort(ranking_2, 2)
        # limit=2 -> rank 1 + rank 2 group (both 1h projects) -> 3 projects.
        assert selection_2.selected_project_count == 3
        assert sorted(p.rank for p in selection_2.projects) == [1, 2, 2]
        # the tie (rank 2) is expanded as a whole, never split.
        assert sum(1 for p in selection_2.projects if p.rank == 2) == 2

    def test_preserves_exact_v128_ranks_without_renaming(self) -> None:
        # Totals in an order where a later project outranks an earlier one.
        # A=1h, B=2h (rank 1), C=30min... keep integer hours: A=1h, B=2h, C=1h.
        ranking = _v128_ranking([ONE_HOUR, TWO_HOURS, ONE_HOUR])
        all_ranks = {
            p.project_id: p.rank for p in ranking.projects
        }
        # The V1.28 ordering places B (2h) at rank 1 even though it is the
        # second project in source order.
        assert ranking.projects[1].rank == 1

        selection = select_top_ranked_portfolio_project_effort(ranking, 1)
        assert selection.selected_project_count == 1
        assert selection.projects[0].rank == 1
        # exact ranks preserved, never renumbered:
        assert selection.projects[0].project_id in all_ranks
        assert selection.projects[0].rank == all_ranks[selection.projects[0].project_id]

    def test_preserves_project_id_total_and_share_without_floats(self) -> None:
        ranking = _v128_ranking([TWO_HOURS, ONE_HOUR, ONE_HOUR])
        source_by_id = {p.project_id: p for p in ranking.projects}
        selection = select_top_ranked_portfolio_project_effort(ranking, 3)
        assert selection.selected_project_count == 3
        for project in selection.projects:
            source = source_by_id[project.project_id]
            # value-exact (no identity guarantee)
            assert project.project_id == source.project_id
            assert project.total_duration_seconds == source.total_duration_seconds
            assert project.rank == source.rank
            if project.share is not None and source.share is not None:
                assert (
                    project.share.numerator_duration_seconds
                    == source.share.numerator_duration_seconds
                )
                assert (
                    project.share.denominator_duration_seconds
                    == source.share.denominator_duration_seconds
                )
        payload = selection.to_payload()
        assert payload["total_duration_seconds"] == 4 * ONE_HOUR
        # no floats anywhere in the flattened payload
        def _no_float(obj: object) -> bool:
            if isinstance(obj, float):
                return False
            if isinstance(obj, dict):
                return all(_no_float(v) for v in obj.values())
            if isinstance(obj, (list, tuple)):
                return all(_no_float(v) for v in obj)
            return True

        assert _no_float(payload)
        assert all(p.rank is not None for p in selection.projects)

    def test_limit_ge_project_count_returns_complete_ranking(self) -> None:
        totals = [TWO_HOURS, ONE_HOUR, ONE_HOUR, ONE_HOUR]
        ranking = _v128_ranking(totals)
        for limit in (4, 4, 5, 1000):
            selection = select_top_ranked_portfolio_project_effort(ranking, limit)
            assert selection.source_project_count == 4
            assert selection.selected_project_count == 4
            assert len(selection.projects) == 4
            # complete semantic ranking preserved in V1.28 order.
            assert [p.rank for p in selection.projects] == [
                p.rank for p in ranking.projects
            ]

    def test_limit_boundary_with_no_ties(self) -> None:
        # A=4h, B=2h, C=1h (ranks 1, 2, 3 — no ties).
        ranking = _v128_ranking([4 * ONE_HOUR, TWO_HOURS, ONE_HOUR])
        selection = select_top_ranked_portfolio_project_effort(ranking, 2)
        assert selection.selected_project_count == 2
        assert sorted(p.rank for p in selection.projects) == [1, 2]

    def test_cutoff_tie_includes_full_tie_group(self) -> None:
        # A=4h (r1), B=2h (r2), C=2h (r2), D=1h (r3).
        ranking = _v128_ranking([4 * ONE_HOUR, TWO_HOURS, TWO_HOURS, ONE_HOUR])
        # limit=2 -> rank 1 + full rank-2 tie -> 3 projects.
        selection = select_top_ranked_portfolio_project_effort(ranking, 2)
        assert selection.selected_project_count == 3
        assert sorted(p.rank for p in selection.projects) == [1, 2, 2]
        # limit=3 -> same (the 3rd ordinal also maps to rank 2).
        ranking_b = _v128_ranking([4 * ONE_HOUR, TWO_HOURS, TWO_HOURS, ONE_HOUR])
        selection_b = select_top_ranked_portfolio_project_effort(ranking_b, 3)
        assert selection_b.selected_project_count == 3

    def test_selected_count_exceeds_limit_only_via_tie_groups(self) -> None:
        # no tie: selected == limit.
        ranking = _v128_ranking([4 * ONE_HOUR, TWO_HOURS, ONE_HOUR])
        selection = select_top_ranked_portfolio_project_effort(ranking, 2)
        assert selection.selected_project_count == selection.requested_limit

    def test_mixed_positive_and_zero_durations(self) -> None:
        # A=2h, B=0s, C=1h — B=0 participates, ranks preserved.
        ranking = _v128_ranking([TWO_HOURS, 0, ONE_HOUR])
        assert ranking.total_duration_seconds == 3 * ONE_HOUR
        # B=0 still participates and gets the worst rank in V1.28 semantics.
        zero_projects = [p for p in ranking.projects if p.total_duration_seconds == 0]
        assert len(zero_projects) == 1
        assert zero_projects[0].rank is not None

        # top-1 selection excludes the zero project deterministically.
        selection = select_top_ranked_portfolio_project_effort(ranking, 1)
        assert selection.selected_project_count == 1
        assert selection.projects[0].rank == 1
        assert selection.projects[0].total_duration_seconds == TWO_HOURS

    def test_incomplete_ranking_preserves_unavailable_state(self) -> None:
        ranking = _incomplete_v128_ranking()
        assert ranking.total_duration_seconds is None
        selection = select_top_ranked_portfolio_project_effort(ranking, 1)
        # unavailable state is preserved exactly: no selected projects.
        assert selection.total_duration_seconds is None
        assert selection.selected_project_count == 0
        assert selection.projects == ()
        assert selection.source_project_count == 2

    def test_zero_total_ranking_preserves_unavailable_state(self) -> None:
        ranking = _zero_total_v128_ranking()
        assert ranking.total_duration_seconds == 0
        selection = select_top_ranked_portfolio_project_effort(ranking, 1)
        assert selection.total_duration_seconds == 0
        assert selection.selected_project_count == 0
        assert selection.projects == ()
        assert selection.source_project_count == 2

    def test_empty_ranking_remains_empty(self) -> None:
        ranking = _empty_v128_ranking()
        assert ranking.project_count == 0
        selection = select_top_ranked_portfolio_project_effort(ranking, 1)
        assert selection.source_project_count == 0
        assert selection.selected_project_count == 0
        assert selection.projects == ()
        assert selection.total_duration_seconds == 0

    def test_deterministic_across_repeated_calls(self) -> None:
        ranking = _v128_ranking([TWO_HOURS, ONE_HOUR, ONE_HOUR])
        first = select_top_ranked_portfolio_project_effort(ranking, 2)
        second = select_top_ranked_portfolio_project_effort(ranking, 2)
        # value-exact equality (no identity guarantee).
        assert first.model_dump(mode="python") == second.model_dump(mode="python")
        for a, b in zip(first.projects, second.projects, strict=True):
            assert a.project_id == b.project_id
            assert a.total_duration_seconds == b.total_duration_seconds
            assert a.rank == b.rank

    def test_does_not_mutate_input(self) -> None:
        ranking = _v128_ranking([TWO_HOURS, ONE_HOUR, ONE_HOUR])
        snapshot = copy.deepcopy(ranking)
        select_top_ranked_portfolio_project_effort(ranking, 1)
        select_top_ranked_portfolio_project_effort(ranking, 2)
        assert ranking.model_dump(mode="python") == snapshot.model_dump(mode="python")

    def test_rejected_limit_does_not_fabricate_selection(self) -> None:
        ranking = _v128_ranking([TWO_HOURS])
        with pytest.raises(PortfolioProjectEffortTopSelectionError):
            select_top_ranked_portfolio_project_effort(ranking, 0)
        # after a rejected call the ranking is still intact
        after = select_top_ranked_portfolio_project_effort(ranking, 1)
        assert after.selected_project_count == 1

    def test_no_business_or_float_or_provenance_fields(self) -> None:
        # the projected model must not expose value/urgency/float/provenance
        field_names = set(PortfolioProjectEffortTopSelection.model_fields)
        forbidden_substr = (
            "value",
            "urgent",
            "impact",
            "risk",
            "recommend",
            "float",
            "ratio",
            "confidence",
            "percent",
            "provenance",
            "entity_id",
            "provider",
            "ai",
            "source_version",
            "priority",
        )
        for field in field_names:
            words = frozenset(field.split("_"))
            for bad in forbidden_substr:
                assert bad not in words, (
                    f"V1.29 selection model must not expose business/value/"
                    f"float/urgency/impact/risk/provenance fields: "
                    f"{field!r} contains {bad!r} as a word"
                )

    def test_public_api_exports_v129_surface(self) -> None:
        import trajectory_os.application as app

        assert "PortfolioProjectEffortTopSelection" in app.__all__
        assert "PortfolioProjectEffortTopSelectionError" in app.__all__
        assert "select_top_ranked_portfolio_project_effort" in app.__all__
        # V1.28 surface remains exported and unchanged.
        assert "PortfolioProjectEffortRanking" in app.__all__
        assert "rank_portfolio_project_effort" in app.__all__
        assert "PortfolioProjectEffortRank" in app.__all__

    def test_selection_preserves_v128_project_order(self) -> None:
        # The V1.28 order is the authoritative order; V1.29 must not re-sort
        # the selected projects by any secondary key.
        totals = [ONE_HOUR, 4 * ONE_HOUR, TWO_HOURS, ONE_HOUR]
        ranking = _v128_ranking(totals)
        # ranks: 4h=1, 2h=2, 1h=3 (two of them, tie).
        assert [p.rank for p in ranking.projects] == [3, 1, 2, 3]
        selection_1 = select_top_ranked_portfolio_project_effort(ranking, 1)
        assert [p.rank for p in selection_1.projects] == [1]

        # limit=2 means dense ranks 1 and 2.  The selected rows remain in
        # their original V1.28 relative order; V1.29 does not sort projects.
        selection_2 = select_top_ranked_portfolio_project_effort(ranking, 2)
        assert [p.rank for p in selection_2.projects] == [1, 2]
        expected_2_ids = [
            p.project_id
            for p in ranking.projects
            if p.rank is not None and p.rank <= 2
        ]
        assert [p.project_id for p in selection_2.projects] == expected_2_ids

        selection_tie = select_top_ranked_portfolio_project_effort(ranking, 3)
        # limit=3 -> rank1 + rank2 + rank3 tie = all 4 projects, in source
        # V1.28 order (not re-sorted).
        assert [p.rank for p in selection_tie.projects] == [3, 1, 2, 3]
        assert [
            p.project_id for p in selection_tie.projects
        ] == [p.project_id for p in ranking.projects]
