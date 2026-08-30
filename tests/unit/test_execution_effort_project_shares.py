"""Focused V1.27 — Exact per-project effort share projection tests."""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from trajectory_os.application import (
    ExactProjectEffortShare,
    PortfolioEffectiveEffortSummary,
    PortfolioProjectEffortContribution,
    PortfolioProjectEffortContributionSummary,
    PortfolioProjectEffortShare,
    PortfolioProjectEffortShareError,
    PortfolioProjectEffortShareSummary,
    WorkBreakdownEffectiveEffortSummary,
    project_portfolio_effort_contributions,
    project_portfolio_effort_shares,
    summarize_portfolio_effective_effort,
)
from trajectory_os.domain.execution_effort_planning import PlannedEffortSummary

FOUR_HOURS = 14_400
TWO_HOURS = 7_200
ONE_HOUR = 3_600


_UNSET: Any = object()


def _subtree(
    known: int,
    estimated: int = 1,
    unestimated: int = 0,
    total: int | None = _UNSET,
) -> PlannedEffortSummary:
    if total is _UNSET:
        total = known
    return PlannedEffortSummary(
        known_duration_seconds=known,
        estimated_entity_count=estimated,
        unestimated_entity_count=unestimated,
        total_duration_seconds=total,
    )


def _v124_summary(
    portfolio_id: uuid.UUID,
    project_id: uuid.UUID,
    subtree: PlannedEffortSummary,
    ordinary: int = 1,
    calibrated: int = 0,
) -> WorkBreakdownEffectiveEffortSummary:
    return WorkBreakdownEffectiveEffortSummary(
        portfolio_id=portfolio_id,
        project_id=project_id,
        effort=subtree,
        ordinary_estimate_count=ordinary,
        calibrated_estimate_count=calibrated,
    )


def _v125_summary(
    portfolio_id: uuid.UUID,
    summaries: list[WorkBreakdownEffectiveEffortSummary],
) -> PortfolioEffectiveEffortSummary:
    if summaries:
        return summarize_portfolio_effective_effort(portfolio_id, summaries)
    return PortfolioEffectiveEffortSummary(
        portfolio_id=portfolio_id,
        project_count=0,
        known_duration_seconds=0,
        estimated_entity_count=0,
        unestimated_entity_count=0,
        ordinary_estimate_count=0,
        calibrated_estimate_count=0,
        total_duration_seconds=0,
        projects=(),
    )


def _v126_summary(
    portfolio_id: uuid.UUID,
    summaries: list[WorkBreakdownEffectiveEffortSummary],
) -> PortfolioProjectEffortContributionSummary:
    return project_portfolio_effort_contributions(
        _v125_summary(portfolio_id, summaries)
    )


def _complete_four_project_v126() -> PortfolioProjectEffortContributionSummary:
    portfolio = uuid.uuid4()
    return _v126_summary(
        portfolio,
        [
            _v124_summary(portfolio, uuid.uuid4(), _subtree(TWO_HOURS)),
            _v124_summary(
                portfolio,
                uuid.uuid4(),
                _subtree(TWO_HOURS, estimated=2),
                ordinary=1,
                calibrated=1,
            ),
            _v124_summary(
                portfolio,
                uuid.uuid4(),
                _subtree(FOUR_HOURS, estimated=3),
                ordinary=2,
                calibrated=1,
            ),
            _v124_summary(
                portfolio,
                uuid.uuid4(),
                _subtree(0, estimated=1, unestimated=0, total=0),
            ),
        ],
    )


def _incomplete_v126() -> PortfolioProjectEffortContributionSummary:
    portfolio = uuid.uuid4()
    return _v126_summary(
        portfolio,
        [
            _v124_summary(portfolio, uuid.uuid4(), _subtree(TWO_HOURS)),
            _v124_summary(
                portfolio, uuid.uuid4(), _subtree(TWO_HOURS, estimated=2),
                ordinary=1, calibrated=1,
            ),
            _v124_summary(
                portfolio, uuid.uuid4(), _subtree(FOUR_HOURS, estimated=3),
                ordinary=2, calibrated=1,
            ),
            _v124_summary(
                portfolio,
                uuid.uuid4(),
                _subtree(ONE_HOUR, estimated=0, unestimated=1, total=None),
                ordinary=0,
                calibrated=0,
            ),
        ],
    )


def _complete_zero_v126() -> PortfolioProjectEffortContributionSummary:
    portfolio = uuid.uuid4()
    return _v126_summary(
        portfolio,
        [
            _v124_summary(portfolio, uuid.uuid4(), _subtree(0, estimated=1, total=0)),
            _v124_summary(
                portfolio, uuid.uuid4(), _subtree(0, estimated=2, total=0),
                ordinary=2, calibrated=0,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Model invariants.
# ---------------------------------------------------------------------------


class TestExactProjectEffortShareInvariants:
    def _share(self, numerator: int = 2, denominator: int = 3) -> ExactProjectEffortShare:
        return ExactProjectEffortShare(
            numerator_duration_seconds=numerator,
            denominator_duration_seconds=denominator,
        )

    def test_strict_frozen_and_extra_forbid(self) -> None:
        share = self._share()
        with pytest.raises(ValidationError, match="frozen"):
            share.numerator_duration_seconds = 3  # type: ignore[misc]
        with pytest.raises(ValidationError, match="Extra inputs"):
            ExactProjectEffortShare(
                numerator_duration_seconds=2,
                denominator_duration_seconds=3,
                percentage=0.66,
            )

    def test_rejects_negative_counts(self) -> None:
        with pytest.raises(ValidationError, match="numerator"):
            self._share(numerator=-1)
        with pytest.raises(ValidationError, match="denominator"):
            self._share(denominator=-1)

    def test_rejects_zero_denominator(self) -> None:
        with pytest.raises(ValidationError, match="denominator"):
            self._share(numerator=0, denominator=0)

    def test_rejects_numerator_greater_than_denominator(self) -> None:
        with pytest.raises(ValidationError, match="must not exceed"):
            self._share(numerator=4, denominator=2)

    def test_accepts_equal_and_zero_numerator(self) -> None:
        assert self._share(3, 3).numerator_duration_seconds == 3
        assert self._share(0, 5).numerator_duration_seconds == 0
        # An exact integer ratio is exact, never normalized.
        assert self._share(2, 6) is not None


class TestPortfolioProjectEffortShareInvariants:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        project_id = uuid.uuid4()
        record = PortfolioProjectEffortShare(
            project_id=project_id,
            total_duration_seconds=TWO_HOURS * 2,
            share=ExactProjectEffortShare(
                numerator_duration_seconds=TWO_HOURS * 2,
                denominator_duration_seconds=TWO_HOURS * 2,
            ),
        )
        with pytest.raises(ValidationError, match="frozen"):
            record.project_id = uuid.uuid4()  # type: ignore[misc]
        with pytest.raises(ValidationError, match="frozen"):
            record.share = None  # type: ignore[misc]
        with pytest.raises(ValidationError, match="frozen"):
            record.total_duration_seconds = 1  # type: ignore[misc]
        with pytest.raises(ValidationError, match="Extra inputs"):
            PortfolioProjectEffortShare(
                project_id=project_id,
                total_duration_seconds=TWO_HOURS,
                share=None,
                label="bogus",
            )

    def test_allows_incomplete_record(self) -> None:
        record = PortfolioProjectEffortShare(project_id=uuid.uuid4())
        assert record.total_duration_seconds is None
        assert record.share is None

    def test_rejects_boolean_total(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(),
                total_duration_seconds=True,
                share=None,
            )

    def test_rejects_share_present_while_total_is_none(self) -> None:
        with pytest.raises(ValidationError, match="total_duration_seconds"):
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(),
                total_duration_seconds=None,
                share=ExactProjectEffortShare(
                    numerator_duration_seconds=TWO_HOURS,
                    denominator_duration_seconds=FOUR_HOURS,
                ),
            )

    def test_rejects_share_numerator_mismatching_total(self) -> None:
        with pytest.raises(
            ValidationError, match="numerator_duration_seconds must equal"
        ):
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(),
                total_duration_seconds=TWO_HOURS,
                share=ExactProjectEffortShare(
                    numerator_duration_seconds=ONE_HOUR,
                    denominator_duration_seconds=FOUR_HOURS,
                ),
            )

    def test_rejects_hostile_model_constructed_nested_share(self) -> None:
        # Hostile shares that bypass field/invariant validation must be
        # freshly rejected, never trusted.
        with pytest.raises(ValidationError, match="numerator_duration_seconds"):
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(),
                total_duration_seconds=TWO_HOURS,
                share=ExactProjectEffortShare.model_construct(
                    numerator_duration_seconds=FOUR_HOURS,
                    denominator_duration_seconds=TWO_HOURS,
                ),
            )
        with pytest.raises(ValidationError):
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(),
                total_duration_seconds=TWO_HOURS,
                share=ExactProjectEffortShare.model_construct(
                    numerator_duration_seconds=True,
                    denominator_duration_seconds=True,
                ),
            )

    def test_rejects_hostile_model_constructed_share_bypassing_entry_invariants(self) -> None:
        # A forged share whose numerator disagrees with the total is
        # constructable at the exact-share level (denominator allows it)
        # but must be rejected by the fresh strict revalidation.
        with pytest.raises(
            ValidationError, match="numerator_duration_seconds must equal"
        ):
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(),
                total_duration_seconds=ONE_HOUR,
                share=ExactProjectEffortShare.model_construct(
                    numerator_duration_seconds=TWO_HOURS,
                    denominator_duration_seconds=FOUR_HOURS,
                ),
            )


class TestPortfolioProjectEffortShareSummaryInvariants:
    def _one_project(self, project_id: uuid.UUID) -> PortfolioProjectEffortShare:
        return PortfolioProjectEffortShare(
            project_id=project_id,
            total_duration_seconds=TWO_HOURS,
            share=ExactProjectEffortShare(
                numerator_duration_seconds=TWO_HOURS,
                denominator_duration_seconds=TWO_HOURS,
            ),
        )

    def test_strict_frozen_and_extra_forbid(self) -> None:
        portfolio = uuid.uuid4()
        project_id = uuid.uuid4()
        summary = PortfolioProjectEffortShareSummary(
            portfolio_id=portfolio,
            project_count=1,
            total_duration_seconds=TWO_HOURS,
            projects=(self._one_project(project_id),),
        )
        with pytest.raises(ValidationError, match="frozen"):
            summary.project_count = 2  # type: ignore[misc]
        with pytest.raises(ValidationError, match="frozen"):
            summary.projects = ()  # type: ignore[misc]
        with pytest.raises(ValidationError, match="Extra inputs"):
            PortfolioProjectEffortShareSummary(
                portfolio_id=portfolio,
                project_count=1,
                total_duration_seconds=TWO_HOURS,
                projects=(self._one_project(project_id),),
                label="bogus",
            )

    def test_rejects_mismatched_project_count(self) -> None:
        portfolio = uuid.uuid4()
        with pytest.raises(ValidationError, match="does not equal the number"):
            PortfolioProjectEffortShareSummary(
                portfolio_id=portfolio,
                project_count=0,
                total_duration_seconds=0,
                projects=(self._one_project(uuid.uuid4()),),
            )

    def test_rejects_duplicate_project_ids(self) -> None:
        portfolio = uuid.uuid4()
        project_id = uuid.uuid4()
        with pytest.raises(ValidationError, match="duplicate project IDs"):
            PortfolioProjectEffortShareSummary(
                portfolio_id=portfolio,
                project_count=2,
                total_duration_seconds=0,
                projects=(
                    PortfolioProjectEffortShare(project_id=project_id),
                    PortfolioProjectEffortShare(project_id=project_id),
                ),
            )

    def test_empty_portfolio_rejects_none_total(self) -> None:
        with pytest.raises(ValidationError, match="total_duration_seconds"):
            PortfolioProjectEffortShareSummary(
                portfolio_id=uuid.uuid4(),
                project_count=0,
                total_duration_seconds=None,
                projects=(),
            )

    def test_empty_portfolio_rejects_positive_total(self) -> None:
        with pytest.raises(ValidationError, match="total_duration_seconds"):
            PortfolioProjectEffortShareSummary(
                portfolio_id=uuid.uuid4(),
                project_count=0,
                total_duration_seconds=TWO_HOURS,
                projects=(),
            )

    def test_incomplete_portfolio_rejects_any_share_present(self) -> None:
        with pytest.raises(ValidationError, match="cannot expose shares"):
            PortfolioProjectEffortShareSummary(
                portfolio_id=uuid.uuid4(),
                project_count=2,
                total_duration_seconds=None,
                projects=(
                    PortfolioProjectEffortShare(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        share=ExactProjectEffortShare(
                            numerator_duration_seconds=TWO_HOURS,
                            denominator_duration_seconds=FOUR_HOURS,
                        ),
                    ),
                    PortfolioProjectEffortShare(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=None,
                        share=None,
                    ),
                ),
            )

    def test_complete_zero_total_portfolio_rejects_any_share_present(self) -> None:
        with pytest.raises(
            ValidationError, match="zero-total portfolio cannot expose shares"
        ):
            PortfolioProjectEffortShareSummary(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=0,
                projects=(
                    PortfolioProjectEffortShare(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=0,
                        share=ExactProjectEffortShare(
                            numerator_duration_seconds=0,
                            denominator_duration_seconds=TWO_HOURS,
                        ),
                    ),
                ),
            )

    def test_complete_positive_total_rejects_total_mismatching_exact_sum(self) -> None:
        with pytest.raises(ValidationError, match="exact sum of project totals"):
            PortfolioProjectEffortShareSummary(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=FOUR_HOURS,
                projects=(
                    PortfolioProjectEffortShare(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        share=ExactProjectEffortShare(
                            numerator_duration_seconds=TWO_HOURS,
                            denominator_duration_seconds=FOUR_HOURS,
                        ),
                    ),
                ),
            )

    def test_complete_positive_total_rejects_missing_share(self) -> None:
        with pytest.raises(ValidationError, match="must expose a share"):
            PortfolioProjectEffortShareSummary(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=TWO_HOURS,
                projects=(
                    PortfolioProjectEffortShare(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        share=None,
                    ),
                ),
            )

    def test_complete_positive_total_rejects_wrong_share_denominator(self) -> None:
        with pytest.raises(ValidationError, match="denominator"):
            PortfolioProjectEffortShareSummary(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=TWO_HOURS,
                projects=(
                    PortfolioProjectEffortShare(
                        project_id=uuid.uuid4(),
                        total_duration_seconds=TWO_HOURS,
                        share=ExactProjectEffortShare(
                            numerator_duration_seconds=TWO_HOURS,
                            denominator_duration_seconds=FOUR_HOURS,
                        ),
                    ),
                ),
            )

    def test_complete_positive_total_rejects_wrong_share_numerator(self) -> None:
        # Forgery is constructable only by bypassing validation; fresh
        # strict revalidation must reject the forged numerator.
        with pytest.raises(
            ValidationError,
            match="numerator_duration_seconds must equal",
        ):
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(),
                total_duration_seconds=ONE_HOUR,
                share=ExactProjectEffortShare.model_construct(
                    numerator_duration_seconds=TWO_HOURS,
                    denominator_duration_seconds=FOUR_HOURS,
                ),
            )

    def test_rejects_hostile_model_constructed_entry(self) -> None:
        # A hostile entry (share present, total None) is constructable
        # only by bypassing validation; the fresh strict revalidation by
        # the summary must reject it.
        hostile = PortfolioProjectEffortShare.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=None,
            share=ExactProjectEffortShare(
                numerator_duration_seconds=0,
                denominator_duration_seconds=FOUR_HOURS,
            ),
        )
        with pytest.raises(
            ValidationError,
            match="must not be exposed while the project total",
        ):
            PortfolioProjectEffortShareSummary(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=None,
                projects=(hostile,),
            )

    def test_rejects_hostile_entry_with_forged_share_numeral(self) -> None:
        # Transitive rejection: the entry invariants pass only because
        # validation was bypassed; the summary's revalidation must
        # still reject the forged numerator.
        hostile = PortfolioProjectEffortShare.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=ONE_HOUR,
            share=ExactProjectEffortShare(
                numerator_duration_seconds=TWO_HOURS,
                denominator_duration_seconds=FOUR_HOURS,
            ),
        )
        with pytest.raises(
            ValidationError,
            match="numerator_duration_seconds must equal",
        ):
            PortfolioProjectEffortShareSummary(
                portfolio_id=uuid.uuid4(),
                project_count=1,
                total_duration_seconds=TWO_HOURS,
                projects=(hostile,),
            )

    def test_rejects_complete_totals_while_any_project_incomplete(self) -> None:
        portfolio = uuid.uuid4()
        with pytest.raises(ValidationError, match="incomplete"):
            PortfolioProjectEffortShareSummary(
                portfolio_id=portfolio,
                project_count=1,
                total_duration_seconds=TWO_HOURS,
                projects=(PortfolioProjectEffortShare(project_id=uuid.uuid4()),),
            )

    def test_rejects_boolean_and_nonint_count_fields(self) -> None:
        portfolio = uuid.uuid4()
        for bad_count in (True, False, 1.5, "1"):
            with pytest.raises(ValidationError):
                PortfolioProjectEffortShareSummary(
                    portfolio_id=portfolio,
                    project_count=bad_count,
                    total_duration_seconds=0,
                    projects=(),
                )
        for bad_total in (True, False, -1, 1.5, "0"):
            with pytest.raises(ValidationError):
                PortfolioProjectEffortShareSummary(
                    portfolio_id=portfolio,
                    project_count=0,
                    total_duration_seconds=bad_total,
                    projects=(),
                )


# ---------------------------------------------------------------------------
# Boundary invariants.
# ---------------------------------------------------------------------------


class TestBoundaryInputGuards:
    def test_rejects_non_v126_inputs(self) -> None:
        with pytest.raises(
            PortfolioProjectEffortShareError,
            match="genuine V1\\.26",
        ):
            project_portfolio_effort_shares("nope")  # type: ignore[call-overload]
        with pytest.raises(
            PortfolioProjectEffortShareError,
            match="genuine V1\\.26",
        ):
            project_portfolio_effort_shares(None)  # type: ignore[call-overload]
        with pytest.raises(
            PortfolioProjectEffortShareError,
            match="genuine V1\\.26",
        ):
            project_portfolio_effort_shares(
                uuid.uuid4()
            )  # type: ignore[call-overload]

    def test_rejects_noniterable_projects_of_a_constructed_summary(self) -> None:
        hostile = PortfolioProjectEffortContributionSummary.model_construct(
            portfolio_id=uuid.uuid4(),
            project_count=1,
            projects=42,
        )
        with pytest.raises(
            PortfolioProjectEffortShareError,
            match="not the V1\\.26 shape",
        ):
            project_portfolio_effort_shares(hostile)

    def test_rejects_foreign_contribution_types_within_nested_tuple(self) -> None:
        hostile = PortfolioProjectEffortContributionSummary.model_construct(
            portfolio_id=uuid.uuid4(),
            project_count=1,
            projects=("not-a-contribution",),
        )
        with pytest.raises(
            PortfolioProjectEffortShareError,
            match="failed strict re-validation",
        ):
            project_portfolio_effort_shares(hostile)

    def test_boundary_is_stable_under_repeated_calls(self) -> None:
        first = _complete_four_project_v126()
        second = _complete_four_project_v126()
        for source in (first, second):
            projected = project_portfolio_effort_shares(source)
            assert projected.portfolio_id == source.portfolio_id
            assert projected.project_count == source.project_count
            assert len(projected.projects) == source.project_count


class TestBoundaryRevalidation:
    def _healthy_contribution(self) -> PortfolioProjectEffortContribution:
        return PortfolioProjectEffortContribution(
            project_id=uuid.uuid4(),
            known_duration_seconds=TWO_HOURS,
            estimated_entity_count=1,
            unestimated_entity_count=0,
            ordinary_estimate_count=1,
            calibrated_estimate_count=0,
            total_duration_seconds=TWO_HOURS,
        )

    def test_rejects_top_level_wrong_project_count(self) -> None:
        hostile = PortfolioProjectEffortContributionSummary.model_construct(
            portfolio_id=uuid.uuid4(),
            project_count=2,
            projects=(self._healthy_contribution(),),
        )
        with pytest.raises(
            PortfolioProjectEffortShareError,
            match="failed strict re-validation",
        ):
            project_portfolio_effort_shares(hostile)

    def test_rejects_forged_nested_contribution(self) -> None:
        forged = PortfolioProjectEffortContribution.model_construct(
            project_id=uuid.uuid4(),
            known_duration_seconds=0,
            estimated_entity_count=2,
            unestimated_entity_count=0,
            ordinary_estimate_count=0,
            calibrated_estimate_count=0,
            total_duration_seconds=86_400,
        )
        hostile = PortfolioProjectEffortContributionSummary.model_construct(
            portfolio_id=uuid.uuid4(),
            project_count=1,
            projects=(forged,),
        )
        with pytest.raises(
            PortfolioProjectEffortShareError,
            match="failed strict re-validation",
        ):
            project_portfolio_effort_shares(hostile)

    def test_accepts_genuine_v126_summaries_end_to_end(self) -> None:
        for contributions in (
            _complete_four_project_v126(),
            _incomplete_v126(),
            _complete_zero_v126(),
        ):
            projected = project_portfolio_effort_shares(contributions)
            assert projected.portfolio_id == contributions.portfolio_id


# ---------------------------------------------------------------------------
# Projection semantics.
# ---------------------------------------------------------------------------


class TestProjectionSemantics:
    def test_empty_portfolio_is_zero_zero_and_empty(self) -> None:
        portfolio = uuid.uuid4()
        contributions = _v126_summary(portfolio, [])
        projected = project_portfolio_effort_shares(contributions)

        assert projected.portfolio_id == portfolio
        assert projected.project_count == 0
        assert projected.total_duration_seconds == 0
        assert projected.projects == ()

    def test_single_fully_estimated_project_is_full_ratio(self) -> None:
        portfolio = uuid.uuid4()
        project_id = uuid.uuid4()
        contributions = _v126_summary(
            portfolio,
            [_v124_summary(portfolio, project_id, _subtree(FOUR_HOURS))],
        )
        projected = project_portfolio_effort_shares(contributions)

        assert projected.project_count == 1
        assert projected.total_duration_seconds == FOUR_HOURS
        record = projected.projects[0]
        assert record.project_id == project_id
        assert record.total_duration_seconds == FOUR_HOURS
        assert record.share is not None
        assert record.share.numerator_duration_seconds == FOUR_HOURS
        assert record.share.denominator_duration_seconds == FOUR_HOURS

    def test_multiple_projects_share_the_exact_portfolio_denominator(self) -> None:
        contributions = _complete_four_project_v126()
        projected = project_portfolio_effort_shares(contributions)
        original_totals = [
            contribution.total_duration_seconds for contribution in contributions.projects
        ]
        portfolio_total = sum(total for total in original_totals if total is not None)

        assert projected.total_duration_seconds == portfolio_total
        for record, original in zip(
            projected.projects, original_totals, strict=True
        ):
            assert record.total_duration_seconds == original
            assert record.share is not None
            assert record.share.numerator_duration_seconds == original
            assert record.share.denominator_duration_seconds == portfolio_total
            assert record.share.numerator_duration_seconds <= (
                record.share.denominator_duration_seconds
            )

    def test_preserves_input_order_and_project_ids(self) -> None:
        contributions = _complete_four_project_v126()
        projected = project_portfolio_effort_shares(contributions)
        assert [record.project_id for record in projected.projects] == [
            contribution.project_id for contribution in contributions.projects
        ]
        assert projected.project_count == contributions.project_count

    def test_zero_duration_project_in_positive_portfolio_is_zero_over_denominator(
        self,
    ) -> None:
        contributions = _complete_four_project_v126()
        projected = project_portfolio_effort_shares(contributions)
        portfolio_total = projected.total_duration_seconds
        assert portfolio_total == TWO_HOURS + TWO_HOURS + FOUR_HOURS

        zero_records = [
            record
            for record in projected.projects
            if record.total_duration_seconds == 0
        ]
        assert len(zero_records) == 1
        assert zero_records[0].share is not None
        assert zero_records[0].share.numerator_duration_seconds == 0
        assert zero_records[0].share.denominator_duration_seconds == portfolio_total

    def test_incomplete_portfolio_has_no_total_and_no_shares(self) -> None:
        contributions = _incomplete_v126()
        projected = project_portfolio_effort_shares(contributions)

        assert projected.total_duration_seconds is None
        assert projected.project_count == 4
        assert len(projected.projects) == 4
        for record, contribution in zip(projected.projects, contributions.projects, strict=True):
            assert record.share is None
            # The per-project record still mirrors the V1.26 total (known or None).
            assert record.total_duration_seconds == contribution.total_duration_seconds

    def test_incomplete_portfolio_exposes_no_known_only_pseudo_denominator(self) -> None:
        contributions = _incomplete_v126()
        projected = project_portfolio_effort_shares(contributions)

        # The known-only sum is positive, so a pseudo denominator would have
        # been constructable — none may appear.
        assert sum(
            contribution.known_duration_seconds
            for contribution in contributions.projects
        ) > 0
        assert all(record.share is None for record in projected.projects)

    def test_zero_duration_project_does_not_complete_an_incomplete_portfolio(
        self,
    ) -> None:
        portfolio = uuid.uuid4()
        contributions = _v126_summary(
            portfolio,
            [
                _v124_summary(
                    portfolio,
                    uuid.uuid4(),
                    _subtree(0, estimated=1, unestimated=0, total=0),
                ),
                _v124_summary(
                    portfolio,
                    uuid.uuid4(),
                    _subtree(ONE_HOUR, estimated=0, unestimated=1, total=None),
                    ordinary=0,
                    calibrated=0,
                ),
            ],
        )
        projected = project_portfolio_effort_shares(contributions)
        assert projected.total_duration_seconds is None
        assert all(record.share is None for record in projected.projects)

    def test_complete_zero_total_portfolio_has_zero_total_and_no_shares(self) -> None:
        contributions = _complete_zero_v126()
        projected = project_portfolio_effort_shares(contributions)

        assert contributions.project_count == 2
        assert all(
            contribution.total_duration_seconds == 0
            for contribution in contributions.projects
        )
        assert projected.total_duration_seconds == 0
        assert len(projected.projects) == 2
        for record, contribution in zip(
            projected.projects, contributions.projects, strict=True
        ):
            assert record.share is None
            assert record.total_duration_seconds == contribution.total_duration_seconds

    def test_never_builds_a_zero_over_zero_share(self) -> None:
        for contributions in (
            _v126_summary(uuid.uuid4(), []),
            _complete_zero_v126(),
            _incomplete_v126(),
        ):
            projected = project_portfolio_effort_shares(contributions)
            for record in projected.projects:
                if record.share is not None:
                    assert record.share.numerator_duration_seconds >= 0
                    assert record.share.denominator_duration_seconds > 0

    def test_deterministic_repeated_projection(self) -> None:
        for contributions in (
            _v126_summary(uuid.uuid4(), []),
            _complete_four_project_v126(),
            _incomplete_v126(),
            _complete_zero_v126(),
        ):
            first = project_portfolio_effort_shares(contributions)
            second = project_portfolio_effort_shares(contributions)
            assert first.model_dump() == second.model_dump()

    def test_input_not_mutated(self) -> None:
        contributions = _incomplete_v126()
        snapshot = copy.deepcopy(contributions)
        project_portfolio_effort_shares(contributions)
        assert contributions.model_dump() == snapshot.model_dump()

    def test_shares_are_exact_integer_pairs_without_floats(self) -> None:
        contributions = _complete_four_project_v126()
        projected = project_portfolio_effort_shares(contributions)
        assert isinstance(projected.total_duration_seconds, int)
        for record in projected.projects:
            assert isinstance(record.project_id, uuid.UUID)
            assert isinstance(record.total_duration_seconds, int)
            if record.share is not None:
                assert isinstance(record.share.numerator_duration_seconds, int)
                assert isinstance(record.share.denominator_duration_seconds, int)

    def test_projected_models_carry_only_integer_fields(self) -> None:
        assert set(ExactProjectEffortShare.__pydantic_fields__) == {
            "numerator_duration_seconds",
            "denominator_duration_seconds",
        }
        assert set(PortfolioProjectEffortShare.__pydantic_fields__) == {
            "project_id",
            "total_duration_seconds",
            "share",
        }
        assert set(PortfolioProjectEffortShareSummary.__pydantic_fields__) == {
            "portfolio_id",
            "project_count",
            "total_duration_seconds",
            "projects",
        }

    def test_public_api_exports(self) -> None:
        import trajectory_os.application as app

        for symbol in (
            "ExactProjectEffortShare",
            "PortfolioProjectEffortShare",
            "PortfolioProjectEffortShareError",
            "PortfolioProjectEffortShareSummary",
            "project_portfolio_effort_shares",
        ):
            assert symbol in app.__all__
            assert hasattr(app, symbol)
