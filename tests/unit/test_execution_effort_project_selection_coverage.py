"""V1.31 — Deterministic exact selected-effort coverage tests.

Covers:
* projected coverage model invariants (strict/frozen/extra-forbid, exact
  integer fields, availability semantics for incomplete / zero-total /
  empty / positive-total states, full-selection rules; rejections);
* ``project_selected_portfolio_effort_coverage`` boundary:
  - requires a genuine V1.30 summary (duck-typed/foreign inputs rejected);
  - rejects hostile ``model_construct`` values via fresh strict
    re-validation (hostile scalars, hostile decompositions);
  - exact naming of the V1.30 selected/remaining/total scalars as
    numerator / denominator / remaining numerator (7000/10000 kept
    exactly — never reduced);
  - positive-total, full, partial, incomplete, zero-total, and empty
    V1.30 states;
  - counts/``requested_limit``/``portfolio_id`` mirroring,
    determinism, float-freeness, input immutability;
* public API export of the V1.31 surface;
* no imports from V1.29 / V1.28 production modules by the V1.31 module.

``PortfolioProjectEffortSelectionSummary`` (V1.30) is the SOLE input
authority; the V1.30 summaries used here are constructed directly through
the self-validating V1.30 model (never ``model_construct`` for genuine
states).
"""

from __future__ import annotations

import inspect
import types
import uuid

import pytest
from pydantic import BaseModel, ValidationError

import trajectory_os.application as app
from trajectory_os.application import (
    PortfolioProjectEffortSelectionCoverage,
    PortfolioProjectEffortSelectionCoverageError,
    PortfolioProjectEffortSelectionSummary,
    project_selected_portfolio_effort_coverage,
)

PROJECT_PORTFOLIO = uuid.uuid4()
TWO_HOURS = 2 * 3600
ONE_HOUR = 3600
THIRTY_MINUTES = 30 * 60

# The Issue #90 example: 7000/10000 must be kept EXACT (never 7/10).
EXAMPLE_TOTAL = 10000
EXAMPLE_SELECTED = 7000
EXAMPLE_REMAINING = 3000


# ---------------------------------------------------------------------------
# Fixtures — genuine V1.30 summaries (the sole input authority).
# ---------------------------------------------------------------------------


def _positive_partial_summary() -> PortfolioProjectEffortSelectionSummary:
    # Issue #90 example: total 10000, selected 7000, remaining 3000,
    # partial selection (2 of 3 projects).
    return PortfolioProjectEffortSelectionSummary(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=2,
        source_project_count=3,
        selected_project_count=2,
        total_duration_seconds=EXAMPLE_TOTAL,
        selected_duration_seconds=EXAMPLE_SELECTED,
        remaining_duration_seconds=EXAMPLE_REMAINING,
    )


def _positive_full_summary() -> PortfolioProjectEffortSelectionSummary:
    return PortfolioProjectEffortSelectionSummary(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=5,
        source_project_count=2,
        selected_project_count=2,
        total_duration_seconds=TWO_HOURS,
        selected_duration_seconds=TWO_HOURS,
        remaining_duration_seconds=0,
    )


def _partial_summary() -> PortfolioProjectEffortSelectionSummary:
    return PortfolioProjectEffortSelectionSummary(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=1,
        source_project_count=3,
        selected_project_count=1,
        total_duration_seconds=TWO_HOURS + ONE_HOUR + THIRTY_MINUTES,
        selected_duration_seconds=TWO_HOURS,
        remaining_duration_seconds=ONE_HOUR + THIRTY_MINUTES,
    )


def _incomplete_summary() -> PortfolioProjectEffortSelectionSummary:
    return PortfolioProjectEffortSelectionSummary(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=1,
        source_project_count=2,
        selected_project_count=0,
        total_duration_seconds=None,
        selected_duration_seconds=None,
        remaining_duration_seconds=None,
    )


def _zero_total_summary() -> PortfolioProjectEffortSelectionSummary:
    return PortfolioProjectEffortSelectionSummary(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=1,
        source_project_count=2,
        selected_project_count=0,
        total_duration_seconds=0,
        selected_duration_seconds=0,
        remaining_duration_seconds=0,
    )


def _empty_summary() -> PortfolioProjectEffortSelectionSummary:
    return PortfolioProjectEffortSelectionSummary(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=1,
        source_project_count=0,
        selected_project_count=0,
        total_duration_seconds=0,
        selected_duration_seconds=0,
        remaining_duration_seconds=0,
    )


def _foreign_model() -> object:
    class _ForeignSummary(BaseModel):
        model_config = {"frozen": True}

        portfolio_id: uuid.UUID

    return _ForeignSummary(portfolio_id=PROJECT_PORTFOLIO)


# ---------------------------------------------------------------------------
# Coverage model (PortfolioProjectEffortSelectionCoverage) invariants.
# ---------------------------------------------------------------------------


class TestProjectedCoverageModel:
    """The projected model enforces its scalar invariants on every
    construction path, including direct construction."""

    def test_strict_frozen_and_extra_forbid(self) -> None:
        coverage = PortfolioProjectEffortSelectionCoverage(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=ONE_HOUR,
            selected_numerator_duration_seconds=ONE_HOUR,
            coverage_denominator_duration_seconds=ONE_HOUR,
            remaining_numerator_duration_seconds=0,
        )
        assert coverage.model_config["frozen"] is True
        assert coverage.model_config["extra"] == "forbid"
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id="not-a-uuid",
                requested_limit=1,
                source_project_count=0,
                selected_project_count=0,
                total_duration_seconds=0,
                selected_numerator_duration_seconds=None,
                coverage_denominator_duration_seconds=None,
                remaining_numerator_duration_seconds=None,
            )
        with pytest.raises(ValidationError):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                selected_numerator_duration_seconds=ONE_HOUR,
                coverage_denominator_duration_seconds=ONE_HOUR,
                remaining_numerator_duration_seconds=0,
                unexpected_extra="not-allowed",
            )
        with pytest.raises(ValidationError):
            coverage.requested_limit = 2  # type: ignore[misc]
        with pytest.raises(ValidationError):
            coverage.coverage_denominator_duration_seconds = 0  # type: ignore[misc]

    def _positive_base(self) -> dict[str, object]:
        return {
            "portfolio_id": PROJECT_PORTFOLIO,
            "requested_limit": 1,
            "source_project_count": 3,
            "selected_project_count": 1,
            "total_duration_seconds": TWO_HOURS,
            "selected_numerator_duration_seconds": ONE_HOUR,
            "coverage_denominator_duration_seconds": TWO_HOURS,
            "remaining_numerator_duration_seconds": ONE_HOUR,
        }

    def test_rejects_string_and_float_scalars(self) -> None:
        base = self._positive_base()
        for fields in (
            {"total_duration_seconds": "100"},
            {"total_duration_seconds": 10.0},
            {"selected_numerator_duration_seconds": "50"},
            {"selected_numerator_duration_seconds": 0.5},
            {"coverage_denominator_duration_seconds": "3600"},
            {"remaining_numerator_duration_seconds": 0.0},
            {"selected_project_count": "1"},
            {"requested_limit": "1"},
        ):
            with pytest.raises(ValidationError):
                PortfolioProjectEffortSelectionCoverage(**{**base, **fields})

    def test_rejects_bool_scalars(self) -> None:
        base = self._positive_base()
        for fields in (
            {"total_duration_seconds": True},
            {"selected_numerator_duration_seconds": False},
            {"coverage_denominator_duration_seconds": True},
            {"remaining_numerator_duration_seconds": True},
            {"selected_project_count": True},
            {"requested_limit": True},
        ):
            with pytest.raises(
                ValidationError,
                match="must not be a boolean|Input should be a valid integer",
            ):
                PortfolioProjectEffortSelectionCoverage(**{**base, **fields})

    def test_rejects_selected_count_exceeding_source_count(self) -> None:
        with pytest.raises(
            ValidationError, match="may not exceed source_project_count"
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=1,
                selected_project_count=2,
                total_duration_seconds=ONE_HOUR,
                selected_numerator_duration_seconds=ONE_HOUR,
                coverage_denominator_duration_seconds=ONE_HOUR,
                remaining_numerator_duration_seconds=0,
            )

    def test_incomplete_state_requires_all_coverage_none(self) -> None:
        for field in (
            "selected_numerator_duration_seconds",
            "coverage_denominator_duration_seconds",
            "remaining_numerator_duration_seconds",
        ):
            base: dict[str, object] = {
                "portfolio_id": PROJECT_PORTFOLIO,
                "requested_limit": 1,
                "source_project_count": 2,
                "selected_project_count": 0,
                "total_duration_seconds": None,
                "selected_numerator_duration_seconds": None,
                "coverage_denominator_duration_seconds": None,
                "remaining_numerator_duration_seconds": None,
            }
            base[field] = 0
            with pytest.raises(
                ValidationError,
                match="while total_duration_seconds is None",
            ):
                PortfolioProjectEffortSelectionCoverage(**base)

    def test_incomplete_state_may_not_carry_selected_projects(self) -> None:
        with pytest.raises(
            ValidationError, match="may not carry selected"
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=None,
                selected_numerator_duration_seconds=None,
                coverage_denominator_duration_seconds=None,
                remaining_numerator_duration_seconds=None,
            )

    def test_zero_total_state_requires_all_coverage_none(self) -> None:
        for field in (
            "selected_numerator_duration_seconds",
            "coverage_denominator_duration_seconds",
            "remaining_numerator_duration_seconds",
        ):
            base: dict[str, object] = {
                "portfolio_id": PROJECT_PORTFOLIO,
                "requested_limit": 1,
                "source_project_count": 2,
                "selected_project_count": 0,
                "total_duration_seconds": 0,
                "selected_numerator_duration_seconds": None,
                "coverage_denominator_duration_seconds": None,
                "remaining_numerator_duration_seconds": None,
            }
            base[field] = 0
            with pytest.raises(
                ValidationError,
                match="a zero-total summary",
            ):
                PortfolioProjectEffortSelectionCoverage(**base)

    def test_zero_total_state_may_not_carry_selected_projects(self) -> None:
        with pytest.raises(
            ValidationError, match="may not carry selected"
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=0,
                selected_numerator_duration_seconds=None,
                coverage_denominator_duration_seconds=None,
                remaining_numerator_duration_seconds=None,
            )

    def test_positive_total_requires_all_coverage_scalars(self) -> None:
        for field in (
            "selected_numerator_duration_seconds",
            "coverage_denominator_duration_seconds",
            "remaining_numerator_duration_seconds",
        ):
            base: dict[str, object] = {
                "portfolio_id": PROJECT_PORTFOLIO,
                "requested_limit": 1,
                "source_project_count": 3,
                "selected_project_count": 1,
                "total_duration_seconds": TWO_HOURS,
                "selected_numerator_duration_seconds": ONE_HOUR,
                "coverage_denominator_duration_seconds": TWO_HOURS,
                "remaining_numerator_duration_seconds": ONE_HOUR,
            }
            base[field] = None
            with pytest.raises(
                ValidationError,
                match="must not be None for a positive-total summary",
            ):
                PortfolioProjectEffortSelectionCoverage(**base)

    def test_rejects_denominator_neq_total(self) -> None:
        with pytest.raises(
            ValidationError, match="must equal total_duration_seconds"
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=TWO_HOURS,
                selected_numerator_duration_seconds=ONE_HOUR,
                coverage_denominator_duration_seconds=ONE_HOUR,
                remaining_numerator_duration_seconds=ONE_HOUR,
            )

    def test_rejects_selected_numerator_exceeding_denominator(self) -> None:
        with pytest.raises(
            ValidationError, match="may not exceed the coverage denominator"
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                selected_numerator_duration_seconds=TWO_HOURS,
                coverage_denominator_duration_seconds=ONE_HOUR,
                remaining_numerator_duration_seconds=0,
            )

    def test_rejects_remaining_numerator_exceeding_denominator(self) -> None:
        with pytest.raises(
            ValidationError, match="may not exceed the coverage denominator"
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=TWO_HOURS,
                selected_numerator_duration_seconds=ONE_HOUR,
                coverage_denominator_duration_seconds=TWO_HOURS,
                remaining_numerator_duration_seconds=3 * ONE_HOUR,
            )

    def test_rejects_selected_plus_remaining_neq_denominator(self) -> None:
        with pytest.raises(
            ValidationError, match="must equal coverage_denominator"
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=TWO_HOURS,
                selected_numerator_duration_seconds=ONE_HOUR,
                coverage_denominator_duration_seconds=TWO_HOURS,
                remaining_numerator_duration_seconds=TWO_HOURS,
            )

    def test_rejects_full_selection_with_selected_neq_denominator(self) -> None:
        with pytest.raises(
            ValidationError,
            match="selected_numerator_duration_seconds == coverage_denominator_duration_seconds",
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=2,
                total_duration_seconds=TWO_HOURS,
                selected_numerator_duration_seconds=ONE_HOUR,
                coverage_denominator_duration_seconds=TWO_HOURS,
                remaining_numerator_duration_seconds=ONE_HOUR,
            )

    def test_rejects_full_selection_with_nonzero_remaining(self) -> None:
        with pytest.raises(
            ValidationError, match="remaining_numerator_duration_seconds == 0"
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=2,
                selected_project_count=2,
                total_duration_seconds=TWO_HOURS,
                selected_numerator_duration_seconds=TWO_HOURS,
                coverage_denominator_duration_seconds=TWO_HOURS,
                remaining_numerator_duration_seconds=ONE_HOUR,
            )

    def test_rejects_empty_source_with_positive_total(self) -> None:
        # An empty V1.30 summary has total_duration_seconds == 0; a
        # positive total with an empty source is impossible.
        with pytest.raises(
            ValidationError, match="a zero total_duration_seconds, not a positive one"
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=0,
                selected_project_count=0,
                total_duration_seconds=ONE_HOUR,
                selected_numerator_duration_seconds=ONE_HOUR,
                coverage_denominator_duration_seconds=ONE_HOUR,
                remaining_numerator_duration_seconds=0,
            )

    def test_rejects_positive_total_with_no_selected_projects(self) -> None:
        # A genuine positive-total V1.30 summary selects at least one
        # project, so selected_project_count == 0 is impossible here.
        with pytest.raises(
            ValidationError, match="at least one selected project"
        ):
            PortfolioProjectEffortSelectionCoverage(
                portfolio_id=PROJECT_PORTFOLIO,
                requested_limit=1,
                source_project_count=3,
                selected_project_count=0,
                total_duration_seconds=TWO_HOURS,
                selected_numerator_duration_seconds=0,
                coverage_denominator_duration_seconds=TWO_HOURS,
                remaining_numerator_duration_seconds=TWO_HOURS,
            )

    def test_accepts_valid_states(self) -> None:
        # Incomplete: all coverage scalars None.
        incomplete = PortfolioProjectEffortSelectionCoverage(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=2,
            selected_project_count=0,
            total_duration_seconds=None,
            selected_numerator_duration_seconds=None,
            coverage_denominator_duration_seconds=None,
            remaining_numerator_duration_seconds=None,
        )
        assert incomplete.selected_numerator_duration_seconds is None
        assert incomplete.coverage_denominator_duration_seconds is None
        assert incomplete.remaining_numerator_duration_seconds is None

        # Zero-total: total 0, all coverage scalars None (no 0/0 either).
        zero_total = PortfolioProjectEffortSelectionCoverage(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=2,
            selected_project_count=0,
            total_duration_seconds=0,
            selected_numerator_duration_seconds=None,
            coverage_denominator_duration_seconds=None,
            remaining_numerator_duration_seconds=None,
        )
        assert zero_total.to_payload()["total_duration_seconds"] == 0
        assert zero_total.to_payload()["coverage_denominator_duration_seconds"] is None

        # Empty: counts 0/0, total 0, all coverage scalars None.
        empty = PortfolioProjectEffortSelectionCoverage(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=0,
            selected_project_count=0,
            total_duration_seconds=0,
            selected_numerator_duration_seconds=None,
            coverage_denominator_duration_seconds=None,
            remaining_numerator_duration_seconds=None,
        )
        assert empty.to_payload()["selected_numerator_duration_seconds"] is None

        # Positive-total partial: exact decomposition.
        partial = PortfolioProjectEffortSelectionCoverage(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=3,
            selected_project_count=1,
            total_duration_seconds=TWO_HOURS,
            selected_numerator_duration_seconds=ONE_HOUR,
            coverage_denominator_duration_seconds=TWO_HOURS,
            remaining_numerator_duration_seconds=ONE_HOUR,
        )
        assert (
            partial.selected_numerator_duration_seconds
            + partial.remaining_numerator_duration_seconds
            == partial.coverage_denominator_duration_seconds
        )

        # Positive-total full selection.
        full = PortfolioProjectEffortSelectionCoverage(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=ONE_HOUR,
            selected_numerator_duration_seconds=ONE_HOUR,
            coverage_denominator_duration_seconds=ONE_HOUR,
            remaining_numerator_duration_seconds=0,
        )
        assert (
            full.selected_numerator_duration_seconds
            == full.coverage_denominator_duration_seconds
        )
        assert full.remaining_numerator_duration_seconds == 0


# ---------------------------------------------------------------------------
# Boundary: project_selected_portfolio_effort_coverage.
# ---------------------------------------------------------------------------


class TestBoundaryInput:
    def test_requires_genuine_v130_summary(self) -> None:
        summary = _positive_partial_summary()
        for foreign in (
            None,
            "nope",
            summary.to_payload(),
            types.SimpleNamespace(**summary.to_payload()),
            _foreign_model(),
        ):
            with pytest.raises(
                PortfolioProjectEffortSelectionCoverageError,
                match="genuine V1.30",
            ):
                project_selected_portfolio_effort_coverage(  # type: ignore[arg-type]
                    foreign
                )

    def test_rejects_hostile_top_level_summary(self) -> None:
        # Hostile model_construct values that bypass V1.30 construction
        # invariants must be rejected by fresh strict re-validation.
        # (a) contradictory selected/remaining decomposition.
        hostile = PortfolioProjectEffortSelectionSummary.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=2,
            selected_project_count=1,
            total_duration_seconds=TWO_HOURS,
            selected_duration_seconds=ONE_HOUR,
            remaining_duration_seconds=TWO_HOURS,
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionCoverageError,
            match="strict re-validation",
        ):
            project_selected_portfolio_effort_coverage(hostile)

        # (b) selected count exceeding the source count.
        hostile_count = PortfolioProjectEffortSelectionSummary.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=2,
            total_duration_seconds=ONE_HOUR,
            selected_duration_seconds=ONE_HOUR,
            remaining_duration_seconds=0,
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionCoverageError,
            match="strict re-validation",
        ):
            project_selected_portfolio_effort_coverage(hostile_count)

        # (c) incomplete state exposing numeric selected/remaining scalars.
        hostile_incomplete = PortfolioProjectEffortSelectionSummary.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=2,
            selected_project_count=0,
            total_duration_seconds=None,
            selected_duration_seconds=ONE_HOUR,
            remaining_duration_seconds=None,
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionCoverageError,
            match="strict re-validation",
        ):
            project_selected_portfolio_effort_coverage(hostile_incomplete)

    def test_rejects_hostile_scalar_types(self) -> None:
        for field, value in (
            ("total_duration_seconds", True),
            ("total_duration_seconds", "3600"),
            ("selected_duration_seconds", 0.5),
            ("remaining_duration_seconds", "0"),
        ):
            base: dict[str, object] = {
                "portfolio_id": PROJECT_PORTFOLIO,
                "requested_limit": 1,
                "source_project_count": 2,
                "selected_project_count": 1,
                "total_duration_seconds": ONE_HOUR,
                "selected_duration_seconds": ONE_HOUR,
                "remaining_duration_seconds": 0,
            }
            base[field] = value
            hostile = PortfolioProjectEffortSelectionSummary.model_construct(
                **base
            )
            with pytest.raises(
                PortfolioProjectEffortSelectionCoverageError,
                match="strict re-validation",
            ):
                project_selected_portfolio_effort_coverage(hostile)

    def test_rejects_hostile_decomposition(self) -> None:
        # An authoritative positive-total V1.30 summary cannot expose a
        # selected/remaining split that does not decompose the total; such
        # a constructed state must be rejected.
        hostile = PortfolioProjectEffortSelectionSummary.model_construct(
            portfolio_id=PROJECT_PORTFOLIO,
            requested_limit=1,
            source_project_count=2,
            selected_project_count=1,
            total_duration_seconds=TWO_HOURS,
            selected_duration_seconds=TWO_HOURS,
            remaining_duration_seconds=TWO_HOURS,
        )
        with pytest.raises(
            PortfolioProjectEffortSelectionCoverageError,
            match="strict re-validation",
        ):
            project_selected_portfolio_effort_coverage(hostile)


class TestBoundarySemantics:
    def test_issue_example_projection_keeps_fraction_exact(self) -> None:
        coverage = project_selected_portfolio_effort_coverage(
            _positive_partial_summary()
        )
        assert coverage.total_duration_seconds == EXAMPLE_TOTAL
        assert coverage.selected_numerator_duration_seconds == EXAMPLE_SELECTED
        assert coverage.coverage_denominator_duration_seconds == EXAMPLE_TOTAL
        assert coverage.remaining_numerator_duration_seconds == EXAMPLE_REMAINING
        # 7000/10000 is kept EXACT — never reduced to 7/10.
        assert coverage.selected_numerator_duration_seconds != 7
        assert coverage.coverage_denominator_duration_seconds != 10
        assert (
            coverage.selected_numerator_duration_seconds
            + coverage.remaining_numerator_duration_seconds
            == coverage.coverage_denominator_duration_seconds
        )

    def test_partial_projection_exact(self) -> None:
        summary = _partial_summary()
        coverage = project_selected_portfolio_effort_coverage(summary)
        assert coverage.portfolio_id == summary.portfolio_id
        assert coverage.requested_limit == summary.requested_limit
        assert coverage.source_project_count == summary.source_project_count
        assert coverage.selected_project_count == summary.selected_project_count
        assert coverage.total_duration_seconds == (
            TWO_HOURS + ONE_HOUR + THIRTY_MINUTES
        )
        assert coverage.selected_numerator_duration_seconds == TWO_HOURS
        assert (
            coverage.coverage_denominator_duration_seconds
            == TWO_HOURS + ONE_HOUR + THIRTY_MINUTES
        )
        assert (
            coverage.remaining_numerator_duration_seconds
            == ONE_HOUR + THIRTY_MINUTES
        )
        assert (
            coverage.selected_numerator_duration_seconds
            + coverage.remaining_numerator_duration_seconds
            == coverage.coverage_denominator_duration_seconds
        )
        assert (
            0
            <= coverage.selected_numerator_duration_seconds
            <= coverage.coverage_denominator_duration_seconds
        )
        assert (
            0
            <= coverage.remaining_numerator_duration_seconds
            <= coverage.coverage_denominator_duration_seconds
        )

    def test_full_projection_exact(self) -> None:
        summary = _positive_full_summary()
        coverage = project_selected_portfolio_effort_coverage(summary)
        assert (
            coverage.selected_project_count
            == coverage.source_project_count
        )
        assert (
            coverage.selected_numerator_duration_seconds
            == coverage.coverage_denominator_duration_seconds
        )
        assert coverage.remaining_numerator_duration_seconds == 0
        assert (
            coverage.coverage_denominator_duration_seconds
            == summary.total_duration_seconds
            == TWO_HOURS
        )

    def test_incomplete_projection_exposes_no_coverage_scalar(self) -> None:
        summary = _incomplete_summary()
        coverage = project_selected_portfolio_effort_coverage(summary)
        assert coverage.portfolio_id == PROJECT_PORTFOLIO
        assert coverage.requested_limit == 1
        assert coverage.source_project_count == 2
        assert coverage.selected_project_count == 0
        assert coverage.total_duration_seconds is None
        assert coverage.selected_numerator_duration_seconds is None
        assert coverage.coverage_denominator_duration_seconds is None
        assert coverage.remaining_numerator_duration_seconds is None

    def test_zero_total_projection_exposes_no_coverage_scalar(self) -> None:
        summary = _zero_total_summary()
        coverage = project_selected_portfolio_effort_coverage(summary)
        assert summary.total_duration_seconds == 0
        assert summary.selected_duration_seconds == 0
        assert summary.remaining_duration_seconds == 0
        assert coverage.total_duration_seconds == 0
        assert coverage.selected_numerator_duration_seconds is None
        assert coverage.coverage_denominator_duration_seconds is None
        assert coverage.remaining_numerator_duration_seconds is None

    def test_empty_projection_exposes_no_coverage_scalar(self) -> None:
        summary = _empty_summary()
        coverage = project_selected_portfolio_effort_coverage(summary)
        assert coverage.source_project_count == 0
        assert coverage.selected_project_count == 0
        assert coverage.total_duration_seconds == 0
        assert coverage.selected_numerator_duration_seconds is None
        assert coverage.coverage_denominator_duration_seconds is None
        assert coverage.remaining_numerator_duration_seconds is None

    def test_positive_coverage_scalars_are_exact_ints(self) -> None:
        coverage = project_selected_portfolio_effort_coverage(
            _partial_summary()
        )
        payload = coverage.to_payload()
        for field in (
            "requested_limit",
            "source_project_count",
            "selected_project_count",
            "total_duration_seconds",
            "selected_numerator_duration_seconds",
            "coverage_denominator_duration_seconds",
            "remaining_numerator_duration_seconds",
        ):
            value = payload[field]
            assert not isinstance(value, bool)
            assert isinstance(value, int)

    def test_payload_has_only_intended_scalar_fields(self) -> None:
        coverage = project_selected_portfolio_effort_coverage(
            _partial_summary()
        )
        assert set(coverage.to_payload()) == {
            "portfolio_id",
            "requested_limit",
            "source_project_count",
            "selected_project_count",
            "total_duration_seconds",
            "selected_numerator_duration_seconds",
            "coverage_denominator_duration_seconds",
            "remaining_numerator_duration_seconds",
        }

    def test_payload_has_no_project_rows_or_percent_fields(self) -> None:
        coverage = project_selected_portfolio_effort_coverage(
            _partial_summary()
        )
        payload = coverage.to_payload()
        assert "projects" not in payload
        for key in payload:
            lowered = key.lower()
            assert "percent" not in lowered
            assert "share" not in lowered
            assert "fraction" not in lowered
            assert "classification" not in lowered
            assert "availability" not in lowered

    def test_no_float_decimal_or_percentage_anywhere_in_payload(self) -> None:
        coverage = project_selected_portfolio_effort_coverage(
            _partial_summary()
        )
        for value in coverage.to_payload().values():
            assert value is None or isinstance(value, (int, uuid.UUID)), value


class TestBoundaryBehavior:
    def test_deterministic_repeated_calls_are_value_identical(self) -> None:
        summary = _partial_summary()
        first = project_selected_portfolio_effort_coverage(summary)
        second = project_selected_portfolio_effort_coverage(summary)
        third = project_selected_portfolio_effort_coverage(summary)
        assert first == second == third
        assert first.to_payload() == second.to_payload() == third.to_payload()

    def test_input_is_never_mutated(self) -> None:
        summary = _positive_partial_summary()
        before = summary.to_payload()
        project_selected_portfolio_effort_coverage(summary)
        assert summary.to_payload() == before
        assert summary.selected_duration_seconds == EXAMPLE_SELECTED
        assert summary.remaining_duration_seconds == EXAMPLE_REMAINING


# ---------------------------------------------------------------------------
# Production-module import discipline.
# ---------------------------------------------------------------------------


class TestImportDiscipline:
    def test_module_does_not_import_v129_v128_production_modules(self) -> None:
        from trajectory_os.application import (
            execution_effort_project_selection_coverage as coverage_module,
        )

        source = inspect.getsource(coverage_module)
        assert "execution_effort_project_top_selection" not in source
        assert "execution_effort_project_ranking" not in source
        assert "execution_effort_project_shares" not in source

    def test_module_has_no_repository_provider_clock_or_randomness_use(self) -> None:
        from trajectory_os.application import (
            execution_effort_project_selection_coverage as coverage_module,
        )

        source = inspect.getsource(coverage_module)
        for banned in (
            "import time",
            "import random",
            "datetime",
            "uuid4",
            "open(",
            "sqlite",
            "http",
        ):
            assert banned not in source


# ---------------------------------------------------------------------------
# Public API surface.
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_application_exports_v131_surface(self) -> None:
        assert "PortfolioProjectEffortSelectionCoverage" in app.__all__
        assert "PortfolioProjectEffortSelectionCoverageError" in app.__all__
        assert "project_selected_portfolio_effort_coverage" in app.__all__
        assert (
            app.PortfolioProjectEffortSelectionCoverage
            is PortfolioProjectEffortSelectionCoverage
        )
        assert (
            app.PortfolioProjectEffortSelectionCoverageError
            is PortfolioProjectEffortSelectionCoverageError
        )
        assert (
            app.project_selected_portfolio_effort_coverage
            is project_selected_portfolio_effort_coverage
        )
