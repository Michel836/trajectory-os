"""V1.33 — Deterministic focus-scenario set from authoritative V1.32
comparisons.

Covers Issue #94 acceptance criteria:

* ``PortfolioProjectEffortFocusScenario`` /
  ``PortfolioProjectEffortFocusScenarioSet`` model invariants
  (strict/frozen/extra-forbid, strings/floats/bools rejected for integer
  fields, empty scenarios rejected, reference/scenario selected count
  bounds, positive reference decomposition, positive scenario
  decomposition, exact count delta, exact selected/remaining duration
  deltas, conservation, negative/zero/positive deltas accepted,
  contradictory unavailable numeric effort rejected, valid incomplete /
  zero-total / positive states accepted);
* ``build_portfolio_effort_focus_scenario_set`` boundary trust:
  - non-empty ordered tuple required (list/empty rejected);
  - every element a genuine V1.32 comparison
    (None/dict/string/foreign/duck rejected);
  - hostile ``model_construct`` values and hostile scalars rejected via
    fresh strict re-validation of EVERY element (with the
    ``ValidationError`` preserved as cause);
  - every comparison freshly strict-revalidated and the OUTPUT projected
    ONLY from the revalidated objects (discriminative
    payload-vs-own-fields test double);
  - common-reference equivalence: portfolio, source count, total
    (including mixed availability), left requested limit, left selected
    count, left selected duration, left remaining duration;
* projection: one comparison -> one scenario, N -> N, EXACT caller order
  preserved, no sorting/deduplication, scalar-equal alternatives
  preserved, reference and right-side metadata exact, delta scalars
  exact, incomplete/zero-total states exact, deterministic repeated
  calls, inputs never mutated, only intended fields present (no project
  rows, percentages, ratios, scores, ranks, or recommendation flags);
* public API export of the V1.33 surface;
* the V1.33 production module imports V1.32 only (never V1.31 / V1.30 /
  V1.29 / V1.28 / ...), and has no repository / persistence /
  provider / clock / randomness dependency;
* the narrow ``PortfolioProjectEffortFocusScenarioSetError`` is a
  ``ValueError`` subclass, consistent with adjacent application modules.

The V1.32 comparisons used here are genuine
``PortfolioProjectEffortSelectionComparison`` values; hostile states use
``model_construct`` deliberately.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

import trajectory_os.application as app
from trajectory_os.application import (
    PortfolioProjectEffortFocusScenario,
    PortfolioProjectEffortFocusScenarioSet,
    PortfolioProjectEffortFocusScenarioSetError,
    PortfolioProjectEffortSelectionComparison,
    PortfolioProjectEffortSelectionCoverage,
    build_portfolio_effort_focus_scenario_set,
    compare_portfolio_effort_selections,
)

PROJECT_PORTFOLIO = uuid.uuid4()
OTHER_PORTFOLIO = uuid.uuid4()
FALSE_PORTFOLIO = uuid.uuid4()

TOTAL = 10000


# ---------------------------------------------------------------------------
# Fixtures — genuine V1.31 coverages -> genuine V1.32 comparisons (the
# sole input authority).
# ---------------------------------------------------------------------------


def _coverage(
    requested_limit: int,
    selected_project_count: int,
    selected_duration: int,
    *,
    portfolio_id: uuid.UUID = PROJECT_PORTFOLIO,
    source_project_count: int = 3,
    total_duration_seconds: int = TOTAL,
) -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=portfolio_id,
        requested_limit=requested_limit,
        source_project_count=source_project_count,
        selected_project_count=selected_project_count,
        total_duration_seconds=total_duration_seconds,
        selected_numerator_duration_seconds=selected_duration,
        coverage_denominator_duration_seconds=total_duration_seconds,
        remaining_numerator_duration_seconds=total_duration_seconds
        - selected_duration,
    )


def _incomplete_coverage(
    requested_limit: int = 1,
) -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=requested_limit,
        source_project_count=3,
        selected_project_count=0,
        total_duration_seconds=None,
        selected_numerator_duration_seconds=None,
        coverage_denominator_duration_seconds=None,
        remaining_numerator_duration_seconds=None,
    )


def _zero_coverage(requested_limit: int = 1) -> PortfolioProjectEffortSelectionCoverage:
    return PortfolioProjectEffortSelectionCoverage(
        portfolio_id=PROJECT_PORTFOLIO,
        requested_limit=requested_limit,
        source_project_count=3,
        selected_project_count=0,
        total_duration_seconds=0,
        selected_numerator_duration_seconds=None,
        coverage_denominator_duration_seconds=None,
        remaining_numerator_duration_seconds=None,
    )


def _compare(
    left: PortfolioProjectEffortSelectionCoverage,
    right: PortfolioProjectEffortSelectionCoverage,
) -> PortfolioProjectEffortSelectionComparison:
    return compare_portfolio_effort_selections(left, right)


# One common reference: limit 1, 1 selected, 6000s selected / 4000s
# remaining, out of a 10000s total with 3 source projects.
LEFT = _coverage(1, 1, 6000)
# (label, right requested limit, right selected count, right selected
# seconds, count delta, selected delta, remaining delta):
C_A = _compare(LEFT, _coverage(5, 3, TOTAL))  # (+2, +4000, -4000)
C_B = _compare(LEFT, _coverage(2, 2, 9000))  # (+1, +3000, -3000)
C_C = _compare(LEFT, _coverage(2, 1, 6000))  # (0, 0, 0)
C_D = _compare(LEFT, _coverage(1, 2, 9000))  # (+1, +3000, -3000)

# C_B and C_D are scalar-equal right-side/delta alternatives that differ
# only in their requested limit — both must remain distinct scenarios.

# A second, consistent reference with a NEGATIVE-delta scenario.
LEFT_NEG = _coverage(2, 2, 9000)
C_NEG = _compare(LEFT_NEG, _coverage(1, 1, 6000))  # (-1, -3000, +3000)

C_INC_A = _compare(_incomplete_coverage(1), _incomplete_coverage(4))
C_INC_B = _compare(_incomplete_coverage(1), _incomplete_coverage(1))

C_ZERO_A = _compare(_zero_coverage(1), _zero_coverage(3))
C_ZERO_B = _compare(_zero_coverage(1), _zero_coverage(2))


# ---------------------------------------------------------------------------
# Scenario / set model invariants (direct construction).
# ---------------------------------------------------------------------------


def _scenario(
    requested_limit: int,
    selected_project_count: int,
    selected_project_count_delta: int,
    selected_duration_seconds: int | None,
    selected_duration_delta_seconds: int | None,
    remaining_duration_seconds: int | None,
    remaining_duration_delta_seconds: int | None,
) -> PortfolioProjectEffortFocusScenario:
    return PortfolioProjectEffortFocusScenario(
        requested_limit=requested_limit,
        selected_project_count=selected_project_count,
        selected_project_count_delta=selected_project_count_delta,
        selected_duration_seconds=selected_duration_seconds,
        selected_duration_delta_seconds=selected_duration_delta_seconds,
        remaining_duration_seconds=remaining_duration_seconds,
        remaining_duration_delta_seconds=remaining_duration_delta_seconds,
    )


def _default_scenario() -> PortfolioProjectEffortFocusScenario:
    return _scenario(2, 2, 1, 9000, 3000, 1000, -3000)


def _scenario_set(
    *scenarios: PortfolioProjectEffortFocusScenario,
    **overrides: object,
) -> PortfolioProjectEffortFocusScenarioSet:
    if not scenarios and "scenarios" not in overrides:
        scenarios = (_default_scenario(),)
    overrides.setdefault("scenarios", scenarios)
    base: dict[str, object] = {
        "portfolio_id": PROJECT_PORTFOLIO,
        "source_project_count": 3,
        "total_duration_seconds": TOTAL,
        "reference_requested_limit": 1,
        "reference_selected_project_count": 1,
        "reference_selected_duration_seconds": 6000,
        "reference_remaining_duration_seconds": 4000,
    }
    base.update(overrides)
    return PortfolioProjectEffortFocusScenarioSet(**base)


class _ForeignModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    anything: int | None = None


def _scenario_kwargs(
    field: str,
    replacement: object,
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "requested_limit": 2,
        "selected_project_count": 2,
        "selected_project_count_delta": 1,
    }
    kwargs[field] = replacement
    return kwargs


class TestScenarioModel:
    def test_frozen(self) -> None:
        scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        with pytest.raises(ValidationError, match="frozen"):
            scenario.requested_limit = 5  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusScenario(  # type: ignore[call-arg]
                requested_limit=2,
                selected_project_count=2,
                selected_project_count_delta=1,
                recommended=True,
            )

    @pytest.mark.parametrize("field", (
        "requested_limit",
        "selected_project_count",
        "selected_project_count_delta",
        "selected_duration_seconds",
        "selected_duration_delta_seconds",
        "remaining_duration_seconds",
        "remaining_duration_delta_seconds",
    ))
    def test_string_rejected_for_integer_fields(self, field: str) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusScenario(  # type: ignore[call-arg]
                **_scenario_kwargs(field, "42"),
            )

    @pytest.mark.parametrize("field", ("requested_limit", "selected_project_count_delta"))
    def test_float_rejected_for_integer_fields(self, field: str) -> None:
        for value in (2.5, 2.0):
            with pytest.raises(ValidationError):
                PortfolioProjectEffortFocusScenario(  # type: ignore[call-arg]
                    **_scenario_kwargs(field, value),
                )

    @pytest.mark.parametrize(
        "field",
        (
            "requested_limit",
            "selected_project_count",
            "selected_project_count_delta",
            "selected_duration_seconds",
        ),
    )
    def test_bool_rejected_for_integer_fields(self, field: str) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusScenario(  # type: ignore[call-arg]
                **_scenario_kwargs(field, True),
            )

    def test_zero_and_negative_deltas_constructible(self) -> None:
        assert _scenario(1, 0, -1, 3000, -3000, 7000, 3000).selected_project_count_delta == -1

    def test_exactly_the_intended_fields(self) -> None:
        assert set(PortfolioProjectEffortFocusScenario.model_fields) == {
            "requested_limit",
            "selected_project_count",
            "selected_project_count_delta",
            "selected_duration_seconds",
            "selected_duration_delta_seconds",
            "remaining_duration_seconds",
            "remaining_duration_delta_seconds",
        }


class TestSetModel:
    def test_frozen(self) -> None:
        st = _scenario_set()
        with pytest.raises(ValidationError, match="frozen"):
            st.portfolio_id = OTHER_PORTFOLIO  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusScenarioSet(  # type: ignore[call-arg]
                portfolio_id=PROJECT_PORTFOLIO,
                source_project_count=3,
                total_duration_seconds=TOTAL,
                reference_requested_limit=1,
                reference_selected_project_count=1,
                reference_selected_duration_seconds=6000,
                reference_remaining_duration_seconds=4000,
                scenarios=(),
                best_scenario=0,
            )

    def test_exactly_the_intended_fields(self) -> None:
        assert set(PortfolioProjectEffortFocusScenarioSet.model_fields) == {
            "portfolio_id",
            "source_project_count",
            "total_duration_seconds",
            "reference_requested_limit",
            "reference_selected_project_count",
            "reference_selected_duration_seconds",
            "reference_remaining_duration_seconds",
            "scenarios",
        }

    def test_empty_scenarios_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one scenario"):
            _scenario_set(*(), scenarios=())

    @pytest.mark.parametrize(
        "scenarios",
        ([_scenario(2, 2, 1, 9000, 3000, 1000, -3000)], (
            {"requested_limit": 2, "selected_project_count": 2},
        )),
        ids=["list-instead-of-tuple", "dict-scenario-element"],
    )
    def test_non_tuple_or_non_genuine_scenarios_rejected(
        self,
        scenarios: object,
    ) -> None:
        with pytest.raises(ValidationError):
            _scenario_set(*(), scenarios=scenarios)

    def test_foreign_model_scenario_rejected(self) -> None:
        with pytest.raises(ValidationError, match="genuine"):
            _scenario_set(*(), scenarios=(_ForeignModel(),))

    def test_reference_selected_count_exceeding_source_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="reference_selected_project_count"
        ):
            _scenario_set(*(), reference_selected_project_count=4)

    def test_scenario_selected_count_exceeding_source_rejected(self) -> None:
        with pytest.raises(
            ValidationError,
            match="scenario 1.*selected_project_count may not exceed",
        ):
            _scenario_set(_scenario(2, 4, 3, 9000, 3000, 1000, -3000))

    def test_reference_selected_count_beyond_total_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="reference_selected_duration_seconds"
        ):
            _scenario_set(*(), reference_selected_duration_seconds=TOTAL + 1)

    def test_reference_decomposition_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="reference_selected_duration_seconds \\+ .*must equal",
        ):
            _scenario_set(
                *(),
                reference_selected_duration_seconds=9000,
                reference_remaining_duration_seconds=1500,
            )

    def test_positive_total_requires_full_reference(self) -> None:
        with pytest.raises(ValidationError, match="must not be.*None"):
            _scenario_set(
                *(),
                reference_selected_duration_seconds=None,
                reference_remaining_duration_seconds=4000,
            )

    def test_scenario_decomposition_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="scenario 1.*selected_duration_seconds \\+ .*must equal",
        ):
            _scenario_set(_scenario(2, 2, 1, 9000, 3000, 2000, -2900))

    def test_positive_total_requires_full_scenario(self) -> None:
        with pytest.raises(ValidationError, match="must not be None"):
            _scenario_set(_scenario(2, 2, 1, None, 3000, 1000, -3000))

    def test_exact_count_delta_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="selected_project_count_delta must equal",
        ):
            _scenario_set(_scenario(2, 2, 2, 9000, 3000, 1000, -3000))

    def test_exact_selected_duration_delta_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="selected_duration_delta_seconds must equal",
        ):
            _scenario_set(_scenario(2, 2, 1, 9000, 2999, 1000, -2999))

    def test_exact_remaining_duration_delta_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="remaining_duration_delta_seconds must equal",
        ):
            _scenario_set(_scenario(2, 2, 1, 9000, 3000, 1000, -2999))

    def test_positive_total_requires_duration_deltas(self) -> None:
        with pytest.raises(
            ValidationError,
            match="selected_duration_delta_seconds must not be None",
        ):
            _scenario_set(
                _scenario(2, 2, 1, 9000, None, 1000, -3000)
            )

    def test_string_rejected_for_set_integer_fields(self) -> None:
        for field in (
            "source_project_count",
            "reference_requested_limit",
            "reference_selected_project_count",
            "reference_selected_duration_seconds",
        ):
            with pytest.raises(ValidationError):
                _scenario_set(*(), **{field: "42"})

    def test_float_rejected_for_set_integer_fields(self) -> None:
        for value in (3.0, 2.5):
            with pytest.raises(ValidationError):
                _scenario_set(*(), total_duration_seconds=value)

    def test_bool_rejected_for_set_integer_fields(self) -> None:
        for field in ("source_project_count", "reference_requested_limit"):
            with pytest.raises(ValidationError):
                _scenario_set(*(), **{field: True})

    @pytest.mark.parametrize(
        "scenario",
        (
            _scenario(1, 1, 0, 3000, -3000, 7000, 3000),  # negative deltas
            _scenario(2, 1, 0, 6000, 0, 4000, 0),  # zero deltas
            _scenario(2, 2, 1, 9000, 3000, 1000, -3000),  # positive
        ),
        ids=["negative", "zero", "positive"],
    )
    def test_negative_zero_positive_deltas_accepted(
        self,
        scenario: PortfolioProjectEffortFocusScenario,
    ) -> None:
        assert _scenario_set(scenario).scenarios == (scenario,)

    def test_valid_positive_state_accepted(self) -> None:
        st = _scenario_set(
            _scenario(5, 3, 2, TOTAL, 4000, 0, -4000),
            _scenario(2, 2, 1, 9000, 3000, 1000, -3000),
            _scenario(2, 1, 0, 6000, 0, 4000, 0),
        )
        assert st.reference_selected_duration_seconds == 6000
        assert st.reference_remaining_duration_seconds == 4000
        assert len(st.scenarios) == 3
        # Conservation: every scenario's two duration deltas cancel
        # exactly (scenario and reference describe the same total).
        for scenario in st.scenarios:
            assert scenario.selected_duration_delta_seconds is not None
            assert scenario.remaining_duration_delta_seconds is not None
            assert (
                scenario.selected_duration_delta_seconds
                + scenario.remaining_duration_delta_seconds
                == 0
            )

    def test_valid_incomplete_state_accepted(self) -> None:
        st = _scenario_set(
            *(),
            total_duration_seconds=None,
            reference_selected_duration_seconds=None,
            reference_remaining_duration_seconds=None,
            reference_selected_project_count=0,
            scenarios=(_scenario(4, 0, 0, None, None, None, None),
                       _scenario(1, 0, 0, None, None, None, None)),
        )
        assert st.total_duration_seconds is None
        assert st.reference_selected_project_count == 0
        for scenario in st.scenarios:
            assert scenario.selected_duration_seconds is None
            assert scenario.selected_duration_delta_seconds is None
            assert scenario.remaining_duration_seconds is None
            assert scenario.remaining_duration_delta_seconds is None

    def test_valid_zero_total_state_accepted(self) -> None:
        st = _scenario_set(
            *(),
            total_duration_seconds=0,
            reference_selected_project_count=0,
            reference_selected_duration_seconds=None,
            reference_remaining_duration_seconds=None,
            scenarios=(_scenario(3, 0, 0, None, None, None, None),),
        )
        assert st.total_duration_seconds == 0

    def test_incomplete_state_rejects_numeric_reference_effort(self) -> None:
        with pytest.raises(
            ValidationError,
            match="incomplete set.*may not expose",
        ):
            _scenario_set(
                *(),
                total_duration_seconds=None,
                reference_selected_duration_seconds=100,
                reference_remaining_duration_seconds=None,
                reference_selected_project_count=0,
                scenarios=(_scenario(4, 0, 0, None, None, None, None),),
            )

    def test_incomplete_state_rejects_numeric_scenario_effort(self) -> None:
        with pytest.raises(
            ValidationError,
            match="scenario 1.*may not expose",
        ):
            _scenario_set(
                *(),
                total_duration_seconds=None,
                reference_selected_duration_seconds=None,
                reference_remaining_duration_seconds=None,
                reference_selected_project_count=0,
                scenarios=(
                    _scenario(4, 0, 0, 100, None, None, None),
                ),
            )

    def test_zero_total_state_rejects_numeric_effort(self) -> None:
        with pytest.raises(
            ValidationError,
            match="zero-total set.*may not expose",
        ):
            _scenario_set(
                *(),
                total_duration_seconds=0,
                reference_selected_project_count=0,
                reference_selected_duration_seconds=5,
                reference_remaining_duration_seconds=None,
                scenarios=(_scenario(3, 0, 0, None, None, None, None),),
            )


# ---------------------------------------------------------------------------
# Boundary trust.
# ---------------------------------------------------------------------------


def _hostile_comparison(**overrides: object) -> PortfolioProjectEffortSelectionComparison:
    base: dict[str, object] = {
        "portfolio_id": PROJECT_PORTFOLIO,
        "source_project_count": 3,
        "total_duration_seconds": TOTAL,
        "left_requested_limit": 1,
        "right_requested_limit": 2,
        "left_selected_project_count": 1,
        "right_selected_project_count": 2,
        "selected_project_count_delta": 1,
        "left_selected_duration_seconds": 6000,
        "right_selected_duration_seconds": 9000,
        "selected_duration_delta_seconds": 3000,
        "left_remaining_duration_seconds": 4000,
        "right_remaining_duration_seconds": 1000,
        "remaining_duration_delta_seconds": -3000,
    }
    base.update(overrides)
    return PortfolioProjectEffortSelectionComparison.model_construct(**base)


def _misreporting_comparison(
    true_comparison: PortfolioProjectEffortSelectionComparison,
) -> PortfolioProjectEffortSelectionComparison:
    """A genuine V1.32-compatible subclass whose OWN fields deliberately
    disagree with a DIFFERENT (but valid) payload returned by
    ``to_payload()``."""

    class _MisreportingComparison(PortfolioProjectEffortSelectionComparison):
        def to_payload(self) -> dict[str, object]:
            return true_comparison.to_payload()

    return _MisreportingComparison.model_construct(
        portfolio_id=FALSE_PORTFOLIO,
        source_project_count=99,
        total_duration_seconds=123,
        left_requested_limit=8,
        right_requested_limit=7,
        left_selected_project_count=4,
        right_selected_project_count=9,
        selected_project_count_delta=98,
        left_selected_duration_seconds=2222,
        right_selected_duration_seconds=3333,
        selected_duration_delta_seconds=9999,
        left_remaining_duration_seconds=4444,
        right_remaining_duration_seconds=5555,
        remaining_duration_delta_seconds=8888,
    )


class TestBoundaryTrust:
    def test_non_empty_tuple_required(self) -> None:
        cases: list[object] = [
            [],  # list, not tuple
            (),  # empty
            None,
            {"comparison": C_A},
            "comparison",
        ]
        for case in cases:
            with pytest.raises(PortfolioProjectEffortFocusScenarioSetError):
                build_portfolio_effort_focus_scenario_set(case)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "element",
        [None, {"portfolio_id": PROJECT_PORTFOLIO}, "comparison", _ForeignModel()],
        ids=["none", "dict", "string", "foreign-model"],
    )
    def test_non_genuine_elements_rejected(self, element: object) -> None:
        with pytest.raises(PortfolioProjectEffortFocusScenarioSetError):
            build_portfolio_effort_focus_scenario_set((element,))

    def test_duck_type_rejected(self) -> None:
        duck = C_A.to_payload()
        assert isinstance(duck, dict)
        with pytest.raises(PortfolioProjectEffortFocusScenarioSetError):
            build_portfolio_effort_focus_scenario_set((duck,))

    def test_hostile_model_construct_rejected(self) -> None:
        hostile = _hostile_comparison(selected_project_count_delta=98)
        with pytest.raises(
            PortfolioProjectEffortFocusScenarioSetError,
            match="failed strict re-validation",
        ) as excinfo:
            build_portfolio_effort_focus_scenario_set((hostile,))
        assert isinstance(excinfo.value.__cause__, ValidationError)

    def test_hostile_scalar_rejected(self) -> None:
        hostile = _hostile_comparison(total_duration_seconds=TOTAL / 2)
        with pytest.raises(
            PortfolioProjectEffortFocusScenarioSetError,
            match="failed strict re-validation",
        ):
            build_portfolio_effort_focus_scenario_set((hostile,))

    def test_hostile_invariant_violation_rejected(self) -> None:
        hostile = _hostile_comparison(right_selected_duration_seconds=TOTAL + 1)
        with pytest.raises(PortfolioProjectEffortFocusScenarioSetError):
            build_portfolio_effort_focus_scenario_set((hostile,))

    def test_output_projected_only_from_revalidated_objects(self) -> None:
        misreporting = _misreporting_comparison(C_B)
        original_payload_snapshot = C_B.to_payload()

        result = build_portfolio_effort_focus_scenario_set((misreporting,))

        expected = build_portfolio_effort_focus_scenario_set((C_B,))
        assert result == expected
        # The output must reflect ONLY the revalidated payload, never the
        # object's own disagreeing fields.
        assert result.portfolio_id == PROJECT_PORTFOLIO
        assert result.source_project_count == 3
        assert result.total_duration_seconds == TOTAL
        assert (
            result.reference_requested_limit
            == original_payload_snapshot["left_requested_limit"]
        )
        scenario = result.scenarios[0]
        assert scenario.requested_limit == original_payload_snapshot[
            "right_requested_limit"
        ]
        assert scenario.selected_project_count == original_payload_snapshot[
            "right_selected_project_count"
        ]
        assert scenario.selected_project_count_delta == original_payload_snapshot[
            "selected_project_count_delta"
        ]
        assert scenario.selected_duration_seconds == original_payload_snapshot[
            "right_selected_duration_seconds"
        ]
        assert scenario.selected_duration_delta_seconds == original_payload_snapshot[
            "selected_duration_delta_seconds"
        ]
        assert scenario.remaining_duration_seconds == original_payload_snapshot[
            "right_remaining_duration_seconds"
        ]
        assert scenario.remaining_duration_delta_seconds == original_payload_snapshot[
            "remaining_duration_delta_seconds"
        ]
        # And none of the object's own hostile fields may leak through.
        assert result.source_project_count != 99
        assert scenario.selected_project_count != 9
        assert scenario.selected_duration_seconds != 3333

    def test_two_misreporting_elements_revalidated_in_order(self) -> None:
        a = _misreporting_comparison(C_A)
        b = _misreporting_comparison(C_B)
        result = build_portfolio_effort_focus_scenario_set((b, a))
        expected = build_portfolio_effort_focus_scenario_set((C_B, C_A))
        assert result == expected
        assert [s.requested_limit for s in result.scenarios] == [
            s.requested_limit for s in expected.scenarios
        ]

    def test_portfolio_mismatch_rejected(self) -> None:
        other = _compare(
            _coverage(1, 1, 6000, portfolio_id=OTHER_PORTFOLIO),
            _coverage(2, 2, 9000, portfolio_id=OTHER_PORTFOLIO),
        )
        with pytest.raises(
            PortfolioProjectEffortFocusScenarioSetError, match="same portfolio"
        ):
            build_portfolio_effort_focus_scenario_set((C_B, other))

    def test_source_count_mismatch_rejected(self) -> None:
        other = _compare(
            _coverage(1, 1, 6000, source_project_count=4),
            _coverage(2, 2, 9000, source_project_count=4),
        )
        with pytest.raises(
            PortfolioProjectEffortFocusScenarioSetError,
            match="source_project_count",
        ):
            build_portfolio_effort_focus_scenario_set((C_B, other))

    def test_total_mismatch_rejected(self) -> None:
        other = _compare(
            _coverage(1, 1, 6000, total_duration_seconds=2 * TOTAL),
            _coverage(2, 2, 18000, total_duration_seconds=2 * TOTAL),
        )
        with pytest.raises(
            PortfolioProjectEffortFocusScenarioSetError,
            match="total_duration_seconds",
        ):
            build_portfolio_effort_focus_scenario_set((C_B, other))

    def test_mixed_availability_rejected(self) -> None:
        with pytest.raises(
            PortfolioProjectEffortFocusScenarioSetError,
            match="total availability",
        ):
            build_portfolio_effort_focus_scenario_set((C_B, C_INC_A))

    def test_left_requested_limit_mismatch_rejected(self) -> None:
        other = _compare(
            _coverage(2, 1, 6000),
            _coverage(3, 2, 9000),
        )
        with pytest.raises(
            PortfolioProjectEffortFocusScenarioSetError,
            match="requested_limit",
        ):
            build_portfolio_effort_focus_scenario_set((C_B, other))

    def test_left_selected_count_mismatch_rejected(self) -> None:
        other = _compare(
            _coverage(1, 2, 9000),
            _coverage(2, 3, TOTAL),
        )
        with pytest.raises(
            PortfolioProjectEffortFocusScenarioSetError,
            match="selected_project_count",
        ):
            build_portfolio_effort_focus_scenario_set((C_B, other))

    def test_left_selected_duration_mismatch_rejected(self) -> None:
        other = _compare(
            _coverage(1, 1, 5000),
            _coverage(2, 2, 9000),
        )
        with pytest.raises(
            PortfolioProjectEffortFocusScenarioSetError,
            match="selected_duration_seconds",
        ):
            build_portfolio_effort_focus_scenario_set((C_B, other))

    def test_left_remaining_duration_mismatch_rejected(self) -> None:
        # A comparison whose common (left) reference exposes a different
        # left remaining duration is not part of the same reference. In
        # the positive-total domain the left remaining duration of a
        # genuine, revalidated V1.32 comparison is co-determined with its
        # left selected duration and total, so the mismatch is caught by
        # the common-reference/domain checks below; either way the set is
        # rejected, never fabricated.
        other = _compare(
            _coverage(1, 2, 9000),
            _coverage(1, 3, TOTAL),
        )
        with pytest.raises(
            PortfolioProjectEffortFocusScenarioSetError,
            match="comparisons must",
        ):
            build_portfolio_effort_focus_scenario_set((C_B, other))

    def test_error_is_narrow_value_error(self) -> None:
        assert issubclass(PortfolioProjectEffortFocusScenarioSetError, ValueError)


# ---------------------------------------------------------------------------
# Projection semantics.
# ---------------------------------------------------------------------------


class TestProjection:
    def test_one_comparison_one_scenario(self) -> None:
        result = build_portfolio_effort_focus_scenario_set((C_B,))
        assert len(result.scenarios) == 1

    def test_n_comparisons_n_scenarios(self) -> None:
        result = build_portfolio_effort_focus_scenario_set((C_A, C_B, C_C, C_D))
        assert len(result.scenarios) == 4

    def test_caller_order_exactly_preserved(self) -> None:
        ordered = (C_C, C_B, C_D, C_A)
        result = build_portfolio_effort_focus_scenario_set(ordered)
        expected_requested_limits = [
            c.right_requested_limit for c in ordered
        ]
        assert [s.requested_limit for s in result.scenarios] == (
            expected_requested_limits
        )
        for scenario, comparison in zip(
            result.scenarios, ordered, strict=True
        ):
            assert (
                scenario.requested_limit,
                scenario.selected_project_count,
                scenario.selected_project_count_delta,
                scenario.selected_duration_seconds,
                scenario.selected_duration_delta_seconds,
                scenario.remaining_duration_seconds,
                scenario.remaining_duration_delta_seconds,
            ) == (
                comparison.right_requested_limit,
                comparison.right_selected_project_count,
                comparison.selected_project_count_delta,
                comparison.right_selected_duration_seconds,
                comparison.selected_duration_delta_seconds,
                comparison.right_remaining_duration_seconds,
                comparison.remaining_duration_delta_seconds,
            )

    def test_no_sorting_by_limit_count_or_delta(self) -> None:
        # Deliberately un-sorted input: requested limits 5, 2, 2, 1 with
        # count deltas +2, +1, +1, +1 and magnitudes that would sort
        # differently.
        result = build_portfolio_effort_focus_scenario_set((C_A, C_B, C_C, C_D))
        assert [s.requested_limit for s in result.scenarios] == [5, 2, 2, 1]
        assert [s.selected_project_count for s in result.scenarios] == [
            3,
            2,
            1,
            2,
        ]
        assert [s.selected_project_count_delta for s in result.scenarios] == [
            2,
            1,
            0,
            1,
        ]
        assert [
            s.selected_duration_delta_seconds for s in result.scenarios
        ] == [4000, 3000, 0, 3000]

    def test_scalar_equal_alternatives_preserved(self) -> None:
        result = build_portfolio_effort_focus_scenario_set((C_B, C_D))
        b, d = result.scenarios
        assert b.selected_project_count == d.selected_project_count
        assert b.selected_duration_seconds == d.selected_duration_seconds
        assert b.selected_duration_delta_seconds == d.selected_duration_delta_seconds
        assert b.selected_project_count_delta == d.selected_project_count_delta
        assert b.remaining_duration_seconds == d.remaining_duration_seconds
        assert len(result.scenarios) == 2
        assert b.requested_limit != d.requested_limit

    def test_reference_metadata_exact(self) -> None:
        result = build_portfolio_effort_focus_scenario_set((C_B, C_A))
        assert result.portfolio_id == PROJECT_PORTFOLIO
        assert result.source_project_count == 3
        assert result.total_duration_seconds == TOTAL
        assert result.reference_requested_limit == 1
        assert result.reference_selected_project_count == 1
        assert result.reference_selected_duration_seconds == 6000
        assert result.reference_remaining_duration_seconds == 4000
        # Reference really is a coherent decomposition / reference state.
        assert (
            result.reference_selected_duration_seconds
            + result.reference_remaining_duration_seconds
            == result.total_duration_seconds
        )

    def test_right_side_scenario_metadata_and_deltas_exact(self) -> None:
        result = build_portfolio_effort_focus_scenario_set((C_B,))
        scenario = result.scenarios[0]
        assert scenario.requested_limit == 2
        assert scenario.selected_project_count == 2
        assert scenario.selected_project_count_delta == 1
        assert scenario.selected_duration_seconds == 9000
        assert scenario.selected_duration_delta_seconds == 3000
        assert scenario.remaining_duration_seconds == 1000
        assert scenario.remaining_duration_delta_seconds == -3000
        assert (
            scenario.selected_duration_seconds
            + scenario.remaining_duration_seconds
            == result.total_duration_seconds
        )

    def test_negative_deltas_preserved_exactly(self) -> None:
        result = build_portfolio_effort_focus_scenario_set((C_NEG,))
        scenario = result.scenarios[0]
        assert scenario.selected_project_count_delta == -1
        assert scenario.selected_duration_delta_seconds == -3000
        assert scenario.remaining_duration_delta_seconds == 3000
        assert (
            scenario.selected_duration_delta_seconds
            + scenario.remaining_duration_delta_seconds
            == 0
        )

    def test_incomplete_state_exact(self) -> None:
        result = build_portfolio_effort_focus_scenario_set((C_INC_A, C_INC_B))
        assert result.total_duration_seconds is None
        assert result.reference_selected_project_count == 0
        assert result.reference_selected_duration_seconds is None
        assert result.reference_remaining_duration_seconds is None
        assert [s.requested_limit for s in result.scenarios] == [4, 1]
        for scenario in result.scenarios:
            assert scenario.selected_project_count == 0
            assert scenario.selected_project_count_delta == 0
            assert scenario.selected_duration_seconds is None
            assert scenario.selected_duration_delta_seconds is None
            assert scenario.remaining_duration_seconds is None
            assert scenario.remaining_duration_delta_seconds is None

    def test_zero_total_state_exact(self) -> None:
        result = build_portfolio_effort_focus_scenario_set((C_ZERO_A, C_ZERO_B))
        assert result.total_duration_seconds == 0
        assert result.reference_selected_project_count == 0
        assert result.reference_selected_duration_seconds is None
        assert result.reference_remaining_duration_seconds is None
        assert [s.requested_limit for s in result.scenarios] == [3, 2]
        for scenario in result.scenarios:
            assert scenario.selected_project_count == 0
            assert scenario.selected_project_count_delta == 0
            assert scenario.selected_duration_seconds is None
            assert scenario.selected_duration_delta_seconds is None
            assert scenario.remaining_duration_seconds is None
            assert scenario.remaining_duration_delta_seconds is None

    def test_deterministic_repeated_calls_value_identical(self) -> None:
        first = build_portfolio_effort_focus_scenario_set((C_C, C_B, C_A))
        second = build_portfolio_effort_focus_scenario_set((C_C, C_B, C_A))
        assert first == second
        assert first.model_dump() == second.model_dump()

    def test_inputs_never_mutated(self) -> None:
        input_snapshot = tuple(c.to_payload() for c in (C_A, C_B, C_C))
        build_portfolio_effort_focus_scenario_set((C_A, C_B, C_C))
        assert tuple(c.to_payload() for c in (C_A, C_B, C_C)) == input_snapshot

    def test_no_prescriptive_vocabulary_on_output(self) -> None:
        result = build_portfolio_effort_focus_scenario_set((C_A, C_B))
        banned_tokens = {
            "best",
            "worst",
            "recommended",
            "preferred",
            "optimal",
            "rank",
            "score",
            "priority",
            "importance",
            "urgency",
            "risk",
            "impact",
            "roi",
            "pareto",
            "threshold",
            "percentage",
            "ratio",
            "projects",
            "is_better",
        }
        fields = set(PortfolioProjectEffortFocusScenarioSet.model_fields) | set(
            PortfolioProjectEffortFocusScenario.model_fields
        )
        for field in fields:
            assert not banned_tokens.intersection(
                set(field.lower().split("_"))
            )
        # Every output value is a plain int/None/UUID/tuple copy: no
        # floats, no percentages, no scores, no project rows.
        for scenario in result.scenarios:
            for value in scenario.model_dump().values():
                assert value is None or isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Scope.
# ---------------------------------------------------------------------------


class TestScope:
    def test_public_api_exports_v133_surface(self) -> None:
        for name in (
            "PortfolioProjectEffortFocusScenario",
            "PortfolioProjectEffortFocusScenarioSet",
            "PortfolioProjectEffortFocusScenarioSetError",
            "build_portfolio_effort_focus_scenario_set",
        ):
            assert getattr(app, name, None) is not None
            assert name in app.__all__

    def test_production_module_imports_v132_only(self) -> None:
        module = (
            "trajectory_os.application.execution_effort_project_focus_scenario_set"
        )
        source = inspect.getsource(
            __import__(module, fromlist=["__name__"])
        )
        assert (
            "execution_effort_project_selection_comparison"
        ) in source  # V1.32 (the sole authority)
        for banned in (
            "execution_effort_project_selection_coverage",  # V1.31
            "execution_effort_project_selection_summary",  # V1.30
            "execution_effort_project_top_selection",  # V1.29
            "execution_effort_project_ranking",  # V1.28
            "execution_effort_project_shares",  # V1.27
            "execution_effort_portfolio_summary",
        ):
            assert banned not in source

    def test_no_repository_provider_clock_randomness(self) -> None:
        module = (
            "trajectory_os.application.execution_effort_project_focus_scenario_set"
        )
        source = inspect.getsource(
            __import__(module, fromlist=["__name__"])
        )
        for banned in (
            "import time",
            "import datetime",
            "import random",
            "import httpx",
            "import openai",
            "import anthropic",
            "uuid4(",
            "os.environ",
            "open(",
            "requests",
        ):
            assert banned not in source
