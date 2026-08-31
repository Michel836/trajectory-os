"""Tests for V1.34 — explicit human-accepted focus decisions.

Covers the model, the strict fresh-revalidation trust boundary, exact
model-value membership (including duplicate ambiguity), the
positive / incomplete / zero-total domains, purity and determinism, and
the public surface / import authority.
"""

import pathlib
import uuid

import pytest
from pydantic import BaseModel, ValidationError

import trajectory_os.application as app
from trajectory_os.application import (
    PortfolioProjectEffortFocusDecision,
    PortfolioProjectEffortFocusDecisionError,
    PortfolioProjectEffortFocusScenario,
    PortfolioProjectEffortFocusScenarioSet,
    accept_portfolio_effort_focus_decision,
)

PORTFOLIO = uuid.uuid4()
OTHER_PORTFOLIO = uuid.uuid4()
TOTAL = 10000


class _ForeignModel(BaseModel):
    """A non-V1.33 model that must never satisfy the V1.34 boundary."""

    portfolio_id: uuid.UUID


def _scenario(
    requested_limit: int,
    count: int,
    count_delta: int,
    selected_dur: int | None,
    selected_delta: int | None,
    remaining_dur: int | None,
    remaining_delta: int | None,
) -> PortfolioProjectEffortFocusScenario:
    return PortfolioProjectEffortFocusScenario(
        requested_limit=requested_limit,
        selected_project_count=count,
        selected_project_count_delta=count_delta,
        selected_duration_seconds=selected_dur,
        selected_duration_delta_seconds=selected_delta,
        remaining_duration_seconds=remaining_dur,
        remaining_duration_delta_seconds=remaining_delta,
    )


def _set(
    *scenarios: PortfolioProjectEffortFocusScenario,
    total: int | None = TOTAL,
    reference: tuple[int, int, int | None, int | None] = (1, 1, 6000, 4000),
    portfolio: uuid.UUID = PORTFOLIO,
) -> PortfolioProjectEffortFocusScenarioSet:
    limit, count, selected, remaining = reference
    return PortfolioProjectEffortFocusScenarioSet(
        portfolio_id=portfolio,
        source_project_count=3,
        total_duration_seconds=total,
        reference_requested_limit=limit,
        reference_selected_project_count=count,
        reference_selected_duration_seconds=selected,
        reference_remaining_duration_seconds=remaining,
        scenarios=scenarios,
    )


def _decision_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "portfolio_id": PORTFOLIO,
        "source_project_count": 3,
        "total_duration_seconds": TOTAL,
        "reference_requested_limit": 1,
        "reference_selected_project_count": 1,
        "reference_selected_duration_seconds": 6000,
        "reference_remaining_duration_seconds": 4000,
        "accepted_requested_limit": 2,
        "accepted_selected_project_count": 2,
        "accepted_selected_project_count_delta": 1,
        "accepted_selected_duration_seconds": 9000,
        "accepted_selected_duration_delta_seconds": 3000,
        "accepted_remaining_duration_seconds": 1000,
        "accepted_remaining_duration_delta_seconds": -3000,
    }
    base.update(overrides)
    return base


def _decision(**overrides: object) -> PortfolioProjectEffortFocusDecision:
    return PortfolioProjectEffortFocusDecision(**_decision_kwargs(**overrides))  # type: ignore[call-arg] # noqa: E501


def _constructed_set(**overrides: object) -> PortfolioProjectEffortFocusScenarioSet:
    base: dict[str, object] = {
        "portfolio_id": PORTFOLIO,
        "source_project_count": 3,
        "total_duration_seconds": TOTAL,
        "reference_requested_limit": 1,
        "reference_selected_project_count": 1,
        "reference_selected_duration_seconds": 6000,
        "reference_remaining_duration_seconds": 4000,
        "scenarios": (_scenario(2, 2, 1, 9000, 3000, 1000, -3000),),
    }
    base.update(overrides)
    return PortfolioProjectEffortFocusScenarioSet.model_construct(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Decision model.
# ---------------------------------------------------------------------------


class TestDecisionModel:
    def test_frozen(self) -> None:
        d = _decision()
        with pytest.raises(ValidationError):
            d.portfolio_id = OTHER_PORTFOLIO  # type: ignore[misc]

    def test_extra_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(recommended=True)  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "field",
        [
            "source_project_count",
            "total_duration_seconds",
            "reference_requested_limit",
            "reference_selected_project_count",
            "reference_selected_duration_seconds",
            "reference_remaining_duration_seconds",
            "accepted_requested_limit",
            "accepted_selected_project_count",
            "accepted_selected_project_count_delta",
            "accepted_selected_duration_seconds",
            "accepted_selected_duration_delta_seconds",
            "accepted_remaining_duration_seconds",
            "accepted_remaining_duration_delta_seconds",
        ],
    )
    def test_strings_rejected_for_integer_fields(self, field: str) -> None:
        with pytest.raises(ValidationError):
            _decision(**{field: "42"})

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("source_project_count", 3.0),
            ("reference_requested_limit", 1.5),
            ("total_duration_seconds", 10000.5),
            ("accepted_selected_project_count", 2.0),
            ("accepted_selected_project_count_delta", 0.5),
            ("accepted_remaining_duration_delta_seconds", -3000.5),
        ],
    )
    def test_floats_rejected_for_integer_fields(
        self, field: str, value: float
    ) -> None:
        with pytest.raises(ValidationError):
            _decision(**{field: value})

    @pytest.mark.parametrize(
        "field",
        [
            "source_project_count",
            "reference_selected_project_count",
            "accepted_selected_project_count",
            "accepted_selected_project_count_delta",
            "accepted_selected_duration_seconds",
        ],
    )
    def test_booleans_rejected_for_integer_fields(self, field: str) -> None:
        with pytest.raises(ValidationError):
            _decision(**{field: True})

    def test_reference_count_within_source_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="reference_selected_project_count may not exceed",
        ):
            _decision(
                reference_selected_project_count=4,
                accepted_selected_project_count_delta=-2,
            )

    def test_accepted_count_within_source_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="accepted_selected_project_count may not exceed",
        ):
            _decision(
                accepted_selected_project_count=4,
                accepted_selected_project_count_delta=3,
            )

    def test_exact_count_delta_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="accepted_selected_project_count_delta must equal",
        ):
            _decision(accepted_selected_project_count_delta=9)

    def test_incomplete_semantics_accepted(self) -> None:
        d = _decision(
            total_duration_seconds=None,
            reference_selected_project_count=0,
            reference_selected_duration_seconds=None,
            reference_remaining_duration_seconds=None,
            accepted_requested_limit=1,
            accepted_selected_project_count=0,
            accepted_selected_project_count_delta=0,
            accepted_selected_duration_seconds=None,
            accepted_selected_duration_delta_seconds=None,
            accepted_remaining_duration_seconds=None,
            accepted_remaining_duration_delta_seconds=None,
        )
        assert d.total_duration_seconds is None
        assert d.reference_selected_duration_seconds is None
        assert d.accepted_remaining_duration_seconds is None

    def test_incomplete_rejects_numeric_reference_effort(self) -> None:
        with pytest.raises(
            ValidationError,
            match="incomplete decision.*may not expose any numeric effort",
        ):
            _decision(
                total_duration_seconds=None,
                reference_selected_duration_seconds=100,
            )

    def test_incomplete_rejects_numeric_accepted_effort(self) -> None:
        with pytest.raises(
            ValidationError,
            match="incomplete decision.*may not expose any numeric effort",
        ):
            _decision(
                total_duration_seconds=None,
                accepted_selected_duration_seconds=8000,
                accepted_selected_duration_delta_seconds=2000,
                accepted_remaining_duration_seconds=2000,
                accepted_remaining_duration_delta_seconds=-2000,
            )

    def test_incomplete_rejects_numeric_delta(self) -> None:
        with pytest.raises(
            ValidationError,
            match="incomplete decision.*may not expose any numeric effort",
        ):
            _decision(
                total_duration_seconds=None,
                accepted_selected_duration_seconds=None,
                accepted_selected_duration_delta_seconds=5,
            )

    def test_zero_total_semantics_accepted(self) -> None:
        d = _decision(
            total_duration_seconds=0,
            reference_selected_project_count=0,
            reference_selected_duration_seconds=None,
            reference_remaining_duration_seconds=None,
            accepted_selected_project_count=0,
            accepted_selected_project_count_delta=0,
            accepted_selected_duration_seconds=None,
            accepted_selected_duration_delta_seconds=None,
            accepted_remaining_duration_seconds=None,
            accepted_remaining_duration_delta_seconds=None,
        )
        assert d.total_duration_seconds == 0
        assert d.accepted_remaining_duration_delta_seconds is None

    def test_zero_total_rejects_numeric_effort(self) -> None:
        with pytest.raises(
            ValidationError,
            match="zero-total decision.*may not expose any numeric effort",
        ):
            _decision(
                total_duration_seconds=0,
                reference_selected_duration_seconds=0,
                reference_remaining_duration_seconds=0,
                accepted_selected_duration_seconds=0,
                accepted_selected_duration_delta_seconds=0,
                accepted_remaining_duration_seconds=0,
                accepted_remaining_duration_delta_seconds=0,
            )

    def test_positive_reference_split_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match=r"reference_selected_duration_seconds \+ "
            r"reference_remaining_duration_seconds must equal",
        ):
            _decision(
                reference_selected_duration_seconds=9000,
                reference_remaining_duration_seconds=1500,
            )

    def test_positive_reference_bound_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="reference_selected_duration_seconds may not exceed",
        ):
            _decision(reference_selected_duration_seconds=TOTAL + 1)

    def test_positive_reference_required_fields(self) -> None:
        with pytest.raises(
            ValidationError, match="reference_selected_duration_seconds must not be None"
        ):
            _decision(reference_selected_duration_seconds=None)
        with pytest.raises(
            ValidationError, match="reference_remaining_duration_seconds must not be None"
        ):
            _decision(reference_remaining_duration_seconds=None)

    def test_positive_accepted_split_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match=r"accepted_selected_duration_seconds \+ "
            r"accepted_remaining_duration_seconds must equal",
        ):
            _decision(
                accepted_selected_duration_seconds=9000,
                accepted_remaining_duration_seconds=2000,
            )

    def test_positive_accepted_bound_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="accepted_selected_duration_seconds may not exceed",
        ):
            _decision(accepted_selected_duration_seconds=TOTAL + 1)

    def test_positive_accepted_required_fields(self) -> None:
        with pytest.raises(
            ValidationError,
            match="accepted_selected_duration_seconds must not be None",
        ):
            _decision(accepted_selected_duration_seconds=None)
        with pytest.raises(
            ValidationError,
            match="accepted_selected_duration_delta_seconds must not be None",
        ):
            _decision(accepted_selected_duration_delta_seconds=None)
        with pytest.raises(
            ValidationError,
            match="accepted_remaining_duration_seconds must not be None",
        ):
            _decision(accepted_remaining_duration_seconds=None)
        with pytest.raises(
            ValidationError,
            match="accepted_remaining_duration_delta_seconds must not be None",
        ):
            _decision(accepted_remaining_duration_delta_seconds=None)

    def test_exact_selected_duration_delta_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="accepted_selected_duration_delta_seconds must equal",
        ):
            _decision(accepted_selected_duration_delta_seconds=2999)

    def test_exact_remaining_duration_delta_enforced(self) -> None:
        with pytest.raises(
            ValidationError,
            match="accepted_remaining_duration_delta_seconds must equal",
        ):
            _decision(accepted_remaining_duration_delta_seconds=-2999)

    def test_deltas_conserved_and_sign_flexible(self) -> None:
        negative = _decision(
            reference_requested_limit=2,
            reference_selected_project_count=2,
            reference_selected_duration_seconds=9000,
            reference_remaining_duration_seconds=1000,
            accepted_requested_limit=1,
            accepted_selected_project_count=1,
            accepted_selected_project_count_delta=-1,
            accepted_selected_duration_seconds=6000,
            accepted_selected_duration_delta_seconds=-3000,
            accepted_remaining_duration_seconds=4000,
            accepted_remaining_duration_delta_seconds=3000,
        )
        assert (
            negative.accepted_selected_duration_delta_seconds
            + negative.accepted_remaining_duration_delta_seconds
            == 0
        )

        zero = _decision(
            reference_requested_limit=2,
            reference_selected_project_count=2,
            reference_selected_duration_seconds=9000,
            reference_remaining_duration_seconds=1000,
            accepted_requested_limit=2,
            accepted_selected_project_count=2,
            accepted_selected_project_count_delta=0,
            accepted_selected_duration_seconds=9000,
            accepted_selected_duration_delta_seconds=0,
            accepted_remaining_duration_seconds=1000,
            accepted_remaining_duration_delta_seconds=0,
        )
        assert zero.accepted_selected_project_count_delta == 0
        assert zero.accepted_selected_duration_delta_seconds == 0
        assert zero.accepted_remaining_duration_delta_seconds == 0

        positive = _decision()
        assert (
            positive.accepted_selected_duration_delta_seconds
            + positive.accepted_remaining_duration_delta_seconds
            == 0
        )

    def test_exactly_the_intended_fields(self) -> None:
        assert set(PortfolioProjectEffortFocusDecision.model_fields) == {
            "portfolio_id",
            "source_project_count",
            "total_duration_seconds",
            "reference_requested_limit",
            "reference_selected_project_count",
            "reference_selected_duration_seconds",
            "reference_remaining_duration_seconds",
            "accepted_requested_limit",
            "accepted_selected_project_count",
            "accepted_selected_project_count_delta",
            "accepted_selected_duration_seconds",
            "accepted_selected_duration_delta_seconds",
            "accepted_remaining_duration_seconds",
            "accepted_remaining_duration_delta_seconds",
        }

    def test_no_prescriptive_vocabulary_on_fields(self) -> None:
        for field in PortfolioProjectEffortFocusDecision.model_fields:
            for forbidden in (
                "best",
                "recommended",
                "rank",
                "score",
                "preferred",
                "priority",
                "impact",
                "risk",
                "urgency",
                "importance",
                "roi",
            ):
                assert forbidden not in field, f"{field} contains {forbidden}"

    def test_error_is_narrow_value_error(self) -> None:
        assert issubclass(
            PortfolioProjectEffortFocusDecisionError, ValueError
        )


# ---------------------------------------------------------------------------
# Boundary type trust.
# ---------------------------------------------------------------------------


class TestBoundaryTypeTrust:
    def test_non_genuine_set_rejected(self) -> None:
        legit = _set(
            _scenario(2, 2, 1, 9000, 3000, 1000, -3000),
        )
        cases: object = [
            None,
            legit.model_dump(mode="python"),
            "a scenario set",
            _ForeignModel(portfolio_id=PORTFOLIO),
            [legit],
        ]
        for case in cases:
            with pytest.raises(
                PortfolioProjectEffortFocusDecisionError,
                match="a genuine V1.33 PortfolioProjectEffortFocusScenarioSet",
            ):
                accept_portfolio_effort_focus_decision(
                    case,  # type: ignore[arg-type]
                    legit.scenarios[0],
                )

    def test_non_genuine_accepted_scenario_rejected(self) -> None:
        legit_scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        legit = _set(legit_scenario)
        cases: object = [
            None,
            legit_scenario.model_dump(mode="python"),
            "a scenario",
            _ForeignModel(portfolio_id=PORTFOLIO),
        ]
        for case in cases:
            with pytest.raises(
                PortfolioProjectEffortFocusDecisionError,
                match=(
                    "a genuine V1.33 PortfolioProjectEffortFocusScenario is "
                    "required for the human-accepted scenario"
                ),
            ):
                accept_portfolio_effort_focus_decision(
                    legit,
                    case,  # type: ignore[arg-type]
                )


# ---------------------------------------------------------------------------
# Fresh revalidation trust boundary.
# ---------------------------------------------------------------------------


class TestFreshRevalidation:
    def test_hostile_constructed_set_invariant_rejected(self) -> None:
        # Constructed set state breaks the reference split invariant.
        hostile = _constructed_set(reference_selected_duration_seconds=9000)
        scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError, match="strict re-validation"
        ) as exc:
            accept_portfolio_effort_focus_decision(hostile, scenario)
        assert isinstance(exc.value.__cause__, ValidationError)

    def test_hostile_constructed_set_bad_scalar_type_rejected(self) -> None:
        hostile = _constructed_set(total_duration_seconds=TOTAL / 2)
        scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError, match="strict re-validation"
        ) as exc:
            accept_portfolio_effort_focus_decision(hostile, scenario)
        assert isinstance(exc.value.__cause__, ValidationError)

    def test_hostile_constructed_set_boolean_scalar_rejected(self) -> None:
        hostile = _constructed_set(reference_selected_project_count=True)  # type: ignore[arg-type]
        scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError, match="strict re-validation"
        ) as exc:
            accept_portfolio_effort_focus_decision(hostile, scenario)
        assert isinstance(exc.value.__cause__, ValidationError)

    def test_hostile_nested_scenario_state_rejected(self) -> None:
        # A cross-field-incoherent nested scenario state (count delta
        # inconsistent with the set reference) is rejected by the
        # freshly strict-revalidated set reconstruction.
        hostile_scenario = PortfolioProjectEffortFocusScenario.model_construct(
            requested_limit=2,
            selected_project_count=2,
            selected_project_count_delta=9999,
            selected_duration_seconds=9000,
            selected_duration_delta_seconds=3000,
            remaining_duration_seconds=1000,
            remaining_duration_delta_seconds=-3000,
        )
        hostile = _constructed_set(scenarios=(hostile_scenario,))
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError,
            match="the supplied V1.33 scenario set failed strict re-validation",
        ) as exc:
            accept_portfolio_effort_focus_decision(
                hostile, _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
            )
        assert isinstance(exc.value.__cause__, ValidationError)

    def test_hostile_nested_scenario_invalid_field_rejected(self) -> None:
        hostile_scenario = PortfolioProjectEffortFocusScenario.model_construct(
            requested_limit=2,
            selected_project_count=2,
            selected_project_count_delta=1,
            selected_duration_seconds="9000",
            selected_duration_delta_seconds=3000,
            remaining_duration_seconds=1000,
            remaining_duration_delta_seconds=-3000,
        )
        hostile = _constructed_set(scenarios=(hostile_scenario,))
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError,
            match="nested V1.33 focus scenario",
        ) as exc:
            accept_portfolio_effort_focus_decision(
                hostile, _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
            )
        assert isinstance(exc.value.__cause__, ValidationError)

    def test_hostile_nested_payload_element_rejected(self) -> None:
        hostile = _constructed_set(scenarios=("not a scenario",))
        scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError,
            match="nested V1.33 focus scenario",
        ) as exc:
            accept_portfolio_effort_focus_decision(hostile, scenario)
        assert isinstance(exc.value.__cause__, ValidationError)

    def test_hostile_constructed_accepted_scenario_rejected(self) -> None:
        legit = _set(_scenario(2, 2, 1, 9000, 3000, 1000, -3000))
        hostile_accepted = PortfolioProjectEffortFocusScenario.model_construct(
            requested_limit=2,
            selected_project_count=2,
            selected_project_count_delta=1,
            selected_duration_seconds="9000",
            selected_duration_delta_seconds=3000,
            remaining_duration_seconds=1000,
            remaining_duration_delta_seconds=-3000,
        )
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError,
            match="human-accepted scenario failed strict re-validation",
        ) as exc:
            accept_portfolio_effort_focus_decision(legit, hostile_accepted)
        assert isinstance(exc.value.__cause__, ValidationError)

    def test_incoherent_accepted_scenario_rejected_as_not_present(self) -> None:
        # Field-valid but cross-field-incoherent with this set's reference
        # (count delta 99 vs reference 1): exact model-value membership is
        # the final authority, and the scenario is simply not in the set.
        legit = _set(_scenario(2, 2, 1, 9000, 3000, 1000, -3000))
        hostile_accepted = PortfolioProjectEffortFocusScenario.model_construct(
            requested_limit=2,
            selected_project_count=2,
            selected_project_count_delta=99,
            selected_duration_seconds=9000,
            selected_duration_delta_seconds=3000,
            remaining_duration_seconds=1000,
            remaining_duration_delta_seconds=-3000,
        )
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError,
            match="not present in the scenario set",
        ):
            accept_portfolio_effort_focus_decision(legit, hostile_accepted)

    def test_output_projected_only_from_revalidated_set(self) -> None:
        truthful_scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        truthful_set = _set(truthful_scenario)

        class _MisreportingSet(PortfolioProjectEffortFocusScenarioSet):
            def model_dump(self, **kwargs: object) -> dict[str, object]:
                return truthful_set.model_dump(mode="python")  # type: ignore[return-value]

        misreporting = _MisreportingSet.model_construct(
            portfolio_id=OTHER_PORTFOLIO,
            source_project_count=99,
            total_duration_seconds=123,
            reference_requested_limit=8,
            reference_selected_project_count=7,
            reference_selected_duration_seconds=2222,
            reference_remaining_duration_seconds=4444,
            scenarios=(
                PortfolioProjectEffortFocusScenario.model_construct(
                    requested_limit=7,
                    selected_project_count=7,
                    selected_project_count_delta=7,
                    selected_duration_seconds=7777,
                    selected_duration_delta_seconds=7777,
                    remaining_duration_seconds=1,
                    remaining_duration_delta_seconds=1,
                ),
            ),
        )

        result = accept_portfolio_effort_focus_decision(
            misreporting, truthful_scenario
        )
        expected = accept_portfolio_effort_focus_decision(
            truthful_set, truthful_scenario
        )
        assert result.model_dump(mode="python") == expected.model_dump(mode="python")
        assert result.portfolio_id == PORTFOLIO
        assert result.portfolio_id != OTHER_PORTFOLIO
        assert result.source_project_count == 3
        assert result.total_duration_seconds == TOTAL
        assert result.reference_requested_limit == 1

    def test_set_copy_authoritative_not_accepted_own_fields(self) -> None:
        truthful_scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        legit = _set(truthful_scenario)

        class _MisreportingAccepted(PortfolioProjectEffortFocusScenario):
            def model_dump(self, **kwargs: object) -> dict[str, object]:
                return truthful_scenario.model_dump(mode="python")  # type: ignore[return-value]

        misreporting_accepted = _MisreportingAccepted.model_construct(
            requested_limit=7,
            selected_project_count=7,
            selected_project_count_delta=7,
            selected_duration_seconds=7777,
            selected_duration_delta_seconds=7777,
            remaining_duration_seconds=1,
            remaining_duration_delta_seconds=1,
        )

        result = accept_portfolio_effort_focus_decision(legit, misreporting_accepted)
        expected = accept_portfolio_effort_focus_decision(
            legit, truthful_scenario
        )
        assert result.model_dump(mode="python") == expected.model_dump(mode="python")
        assert result.accepted_requested_limit == 2
        assert result.accepted_selected_project_count == 2
        assert result.accepted_selected_duration_seconds == 9000


# ---------------------------------------------------------------------------
# Exact model-value membership.
# ---------------------------------------------------------------------------


class TestMembership:
    X = _scenario(5, 3, 2, 10000, 4000, 0, -4000)
    Y = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
    Z = _scenario(2, 1, 0, 6000, 0, 4000, 0)

    def test_exact_unique_scenario_accepted_with_exact_projections(self) -> None:
        s = _set(self.X, self.Y, self.Z)
        result = accept_portfolio_effort_focus_decision(s, self.Y)
        assert result.portfolio_id == PORTFOLIO
        assert result.source_project_count == 3
        assert result.total_duration_seconds == TOTAL
        assert result.reference_requested_limit == 1
        assert result.reference_selected_project_count == 1
        assert result.reference_selected_duration_seconds == 6000
        assert result.reference_remaining_duration_seconds == 4000
        assert result.accepted_requested_limit == 2
        assert result.accepted_selected_project_count == 2
        assert result.accepted_selected_project_count_delta == 1
        assert result.accepted_selected_duration_seconds == 9000
        assert result.accepted_selected_duration_delta_seconds == 3000
        assert result.accepted_remaining_duration_seconds == 1000
        assert result.accepted_remaining_duration_delta_seconds == -3000
        self.assertEqual_to_independently_constructed(result)

    def assertEqual_to_independently_constructed(
        self, result: PortfolioProjectEffortFocusDecision
    ) -> None:
        expected = _decision()
        assert result.model_dump(mode="python") == expected.model_dump(mode="python")

    def test_scenario_not_present_rejected(self) -> None:
        s = _set(self.X, self.Y, self.Z)
        absent = _scenario(2, 2, 1, 8000, 2000, 2000, -2000)
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError,
            match="not present in the scenario set",
        ):
            accept_portfolio_effort_focus_decision(s, absent)

    def test_duplicated_exact_equal_scenario_rejected_as_ambiguous(self) -> None:
        s = _set(self.Y, self.Y)
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError,
            match="ambiguous",
        ):
            accept_portfolio_effort_focus_decision(s, self.Y)

    def test_same_limit_distinct_scenarios_selects_exactly(self) -> None:
        s = _set(self.Y, self.Z)
        result = accept_portfolio_effort_focus_decision(s, self.Z)
        assert result.accepted_requested_limit == 2
        assert result.accepted_selected_project_count == 1
        assert result.accepted_selected_project_count_delta == 0
        assert result.accepted_selected_duration_seconds == 6000
        assert result.accepted_selected_duration_delta_seconds == 0
        assert result.accepted_remaining_duration_seconds == 4000
        assert result.accepted_remaining_duration_delta_seconds == 0
        assert result.accepted_selected_duration_seconds != 9000

    def test_not_selected_by_requested_limit(self) -> None:
        s = _set(self.Y)
        # Same requested_limit as a member but a different scenario value.
        lookalike = _scenario(2, 3, 2, 10000, 4000, 0, -4000)
        with pytest.raises(
            PortfolioProjectEffortFocusDecisionError,
            match="not present in the scenario set",
        ):
            accept_portfolio_effort_focus_decision(s, lookalike)

    def test_negative_delta_scenario_preserved(self) -> None:
        negative = _scenario(1, 1, -1, 6000, -3000, 4000, 3000)
        s = _set(negative, reference=(2, 2, 9000, 1000))
        result = accept_portfolio_effort_focus_decision(s, negative)
        assert result.accepted_selected_project_count_delta == -1
        assert result.accepted_selected_duration_delta_seconds == -3000
        assert result.accepted_remaining_duration_delta_seconds == 3000

    def test_zero_delta_scenario_preserved(self) -> None:
        zero = _scenario(2, 2, 0, 9000, 0, 1000, 0)
        s = _set(zero, reference=(2, 2, 9000, 1000))
        result = accept_portfolio_effort_focus_decision(s, zero)
        assert result.accepted_selected_project_count_delta == 0
        assert result.accepted_selected_duration_delta_seconds == 0
        assert result.accepted_remaining_duration_delta_seconds == 0

    def test_positive_delta_scenario_preserved(self) -> None:
        positive = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        s = _set(positive)
        result = accept_portfolio_effort_focus_decision(s, positive)
        assert result.accepted_selected_project_count_delta == 1
        assert result.accepted_selected_duration_delta_seconds == 3000
        assert result.accepted_remaining_duration_delta_seconds == -3000


# ---------------------------------------------------------------------------
# Domains.
# ---------------------------------------------------------------------------


class TestDomains:
    def test_positive_domain_decision(self) -> None:
        scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        s = _set(scenario)
        result = accept_portfolio_effort_focus_decision(s, scenario)
        assert result.total_duration_seconds == TOTAL
        assert result.reference_selected_duration_seconds == 6000
        assert result.accepted_selected_duration_seconds == 9000
        assert result.accepted_remaining_duration_delta_seconds == -3000

    def test_incomplete_domain_decision(self) -> None:
        low = _scenario(1, 0, 0, None, None, None, None)
        high = _scenario(4, 0, 0, None, None, None, None)
        s = _set(
            high,
            low,
            total=None,
            reference=(1, 0, None, None),
        )
        result = accept_portfolio_effort_focus_decision(s, high)
        assert result.total_duration_seconds is None
        assert result.reference_selected_project_count == 0
        assert result.reference_selected_duration_seconds is None
        assert result.reference_remaining_duration_seconds is None
        assert result.accepted_requested_limit == 4
        assert result.accepted_selected_project_count == 0
        assert result.accepted_selected_project_count_delta == 0
        assert result.accepted_selected_duration_seconds is None
        assert result.accepted_selected_duration_delta_seconds is None
        assert result.accepted_remaining_duration_seconds is None
        assert result.accepted_remaining_duration_delta_seconds is None

    def test_zero_total_domain_decision(self) -> None:
        scenario = _scenario(3, 0, 0, None, None, None, None)
        other = _scenario(2, 0, 0, None, None, None, None)
        s = _set(scenario, other, total=0, reference=(1, 0, None, None))
        result = accept_portfolio_effort_focus_decision(s, scenario)
        assert result.total_duration_seconds == 0
        assert result.accepted_selected_duration_seconds is None
        assert result.accepted_remaining_duration_delta_seconds is None
        result_other = accept_portfolio_effort_focus_decision(s, other)
        assert result_other.accepted_requested_limit == 2
        assert result_other.accepted_selected_duration_seconds is None


# ---------------------------------------------------------------------------
# Purity, determinism, scope, surface.
# ---------------------------------------------------------------------------


class TestPureDeterminism:
    def test_repeated_calls_value_identical(self) -> None:
        scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        s = _set(scenario)
        first = accept_portfolio_effort_focus_decision(s, scenario)
        second = accept_portfolio_effort_focus_decision(s, scenario)
        assert first.model_dump(mode="python") == second.model_dump(mode="python")
        assert first == second

    def test_inputs_never_mutated(self) -> None:
        scenario = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)
        s = _set(scenario)
        set_before = s.model_dump(mode="python")
        scenario_before = scenario.model_dump(mode="python")
        accept_portfolio_effort_focus_decision(s, scenario)
        assert s.model_dump(mode="python") == set_before
        assert scenario.model_dump(mode="python") == scenario_before

    def test_no_side_effects_in_scope(self) -> None:
        scope_source = (
            pathlib.Path(__file__)
            .resolve()
            .parents[2]
            / "src"
            / "trajectory_os"
            / "application"
            / "execution_effort_project_focus_decision.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "os.environ",
            "datetime",
            "time.time",
            "import random",
            "random.",
            "socket",
            "httpx",
            "requests",
            "openai",
            "anthropic",
            "sqlalchemy",
            "duckdb",
            "open(",
            "pathlib.Path(",
        ):
            assert forbidden not in scope_source

    def test_v134_uses_v133_semantic_authority_only(self) -> None:
        module_source = (
            pathlib.Path(__file__)
            .resolve()
            .parents[2]
            / "src"
            / "trajectory_os"
            / "application"
            / "execution_effort_project_focus_decision.py"
        ).read_text(encoding="utf-8")
        assert (
            "execution_effort_project_focus_scenario_set" in module_source
        )
        for forbidden in (
            "execution_effort_project_selection_comparison",
            "execution_effort_project_coverage",
            "execution_effort_project_selection_summary",
            "execution_effort_selection",
            "execution_effort_project_ranking",
            "execution_effort_shares",
        ):
            assert forbidden not in module_source

    def test_public_surface_exports(self) -> None:
        assert "PortfolioProjectEffortFocusDecision" in app.__all__
        assert "PortfolioProjectEffortFocusDecisionError" in app.__all__
        assert "accept_portfolio_effort_focus_decision" in app.__all__
        assert (
            app.PortfolioProjectEffortFocusDecision
            is PortfolioProjectEffortFocusDecision
        )
        assert (
            app.PortfolioProjectEffortFocusDecisionError
            is PortfolioProjectEffortFocusDecisionError
        )
        assert (
            app.accept_portfolio_effort_focus_decision
            is accept_portfolio_effort_focus_decision
        )
