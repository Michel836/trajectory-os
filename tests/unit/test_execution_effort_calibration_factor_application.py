"""Unit tests for the V1.18 pure exact-integer factor application.

Covers the strict/frozen/self-auditing result model and its full arithmetic
invariant chain (exact product, exact divmod, remainder range, half-up
rounding decision bit, exact output), the strict candidate-duration
validation (int only, bool/float/Decimal/string rejected, negative
rejected, zero accepted), the V1.17 input-integrity boundary (genuine
instance required, hostile model_construct() factor freshly rejected,
supplied factor preserved unchanged), the complete accepted-factor
evidence copying, the mandatory round-to-nearest ties-upward (half-up)
rule including the 1/2 vs bankers-rounding proof, very large exact
integer arithmetic, hostile inconsistent result construction, repeated-call
determinism, and the absence of any repository/reader, clock, or UUID
generation on the pure boundary.
"""

from __future__ import annotations

import inspect
import math
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactor,
)
from trajectory_os.domain.execution_effort_calibration_factor_application import (
    CalibratedEffortProposal,
    CalibratedEffortProposalError,
    apply_effective_effort_calibration_factor,
)

DECISION_ID = UUID("91919191-9191-4191-9191-919191919191")
OTHER_DECISION_ID = UUID("92929292-9292-4292-9292-929292929292")
DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)


def _factor(
    *,
    entity_type: EntityType = EntityType.TASK,
    decision_id: UUID = DECISION_ID,
    decided_at: datetime = DECIDED_AT,
    sample_count: int = 5,
    minimum_required_sample_count: int = 1,
    planned: int = 100,
    actual: int = 150,
    numerator: int = 3,
    denominator: int = 2,
) -> EffectiveEffortCalibrationFactor:
    """One valid V1.17 effective factor (default: 3/2 over 100 -> 150)."""
    return EffectiveEffortCalibrationFactor(
        entity_type=entity_type,
        decision_id=decision_id,
        decided_at=decided_at,
        sample_count=sample_count,
        minimum_required_sample_count=minimum_required_sample_count,
        total_planned_duration_seconds=planned,
        total_actual_duration_seconds=actual,
        factor_numerator=numerator,
        factor_denominator=denominator,
    )


def _apply(candidate: object, factor: EffectiveEffortCalibrationFactor) -> CalibratedEffortProposal:
    value = apply_effective_effort_calibration_factor(  # type: ignore[arg-type]
        candidate, factor
    )
    assert isinstance(value, CalibratedEffortProposal)
    return value


# --- Result model: strict, frozen, self-auditing -----------------------------


def test_proposal_model_is_frozen_and_strict() -> None:
    proposal = _apply(100, _factor())
    with pytest.raises(ValidationError):
        proposal.calibrated_duration_seconds = 149  # type: ignore[misc]
    with pytest.raises(ValidationError):
        proposal.candidate_duration_seconds = 99  # type: ignore[misc]
    assert proposal.calibrated_duration_seconds == 150


def test_proposal_model_rejects_extra_fields_and_coercion() -> None:
    kwargs = dict(
        entity_type=EntityType.TASK,
        decision_id=DECISION_ID,
        decided_at=DECIDED_AT,
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=150,
        factor_numerator=3,
        factor_denominator=2,
        candidate_duration_seconds=100,
        scaled_numerator=300,
        quotient_seconds=150,
        remainder=0,
        rounded_up=False,
        calibrated_duration_seconds=150,
    )
    with pytest.raises(ValidationError):
        CalibratedEffortProposal(**kwargs, unexpected="nope")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        CalibratedEffortProposal(**{**kwargs, "decision_id": str(DECISION_ID)})  # type: ignore[dict-arg]
    with pytest.raises(ValidationError):
        CalibratedEffortProposal(**{**kwargs, "candidate_duration_seconds": "100"})  # type: ignore[dict-arg]
    with pytest.raises(ValidationError):
        CalibratedEffortProposal(**{**kwargs, "rounded_up": 1})  # type: ignore[dict-arg]
    with pytest.raises(ValidationError):
        CalibratedEffortProposal(  # type: ignore[dict-arg]
            **{**kwargs, "decided_at": datetime(2025, 7, 1, 8, 30)}
        )
    with pytest.raises(ValidationError):
        CalibratedEffortProposal(**{**kwargs, "factor_numerator": 3.0})  # type: ignore[dict-arg]


def test_proposal_model_rejects_non_reduced_factor() -> None:
    with pytest.raises(ValidationError, match="gcd"):
        CalibratedEffortProposal(
            entity_type=EntityType.TASK,
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            sample_count=5,
            minimum_required_sample_count=1,
            total_planned_duration_seconds=200,
            total_actual_duration_seconds=150,
            factor_numerator=6,
            factor_denominator=8,
            candidate_duration_seconds=10,
            scaled_numerator=60,
            quotient_seconds=7,
            remainder=4,
            rounded_up=True,
            calibrated_duration_seconds=8,
        )


def test_proposal_model_rejects_cross_multiplication_violation() -> None:
    with pytest.raises(ValidationError, match="factor"):
        CalibratedEffortProposal(
            entity_type=EntityType.TASK,
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            sample_count=5,
            minimum_required_sample_count=1,
            total_planned_duration_seconds=100,
            total_actual_duration_seconds=150,
            factor_numerator=8,
            factor_denominator=5,
            candidate_duration_seconds=10,
            scaled_numerator=80,
            quotient_seconds=16,
            remainder=0,
            rounded_up=False,
            calibrated_duration_seconds=16,
        )


def test_proposal_model_rejects_insufficient_sample_evidence() -> None:
    with pytest.raises(ValidationError, match="minimum_required_sample_count"):
        base = _apply(100, _factor())
        CalibratedEffortProposal(
            **{
                **base.model_dump(mode="python"),
                "sample_count": 2,
                "minimum_required_sample_count": 5,
            }
        )


def test_proposal_model_rejects_sample_count_below_one() -> None:
    with pytest.raises(ValidationError, match="sample_count"):
        base = _apply(100, _factor())
        CalibratedEffortProposal(
            **{**base.model_dump(mode="python"), "sample_count": 0}
        )


def test_result_retains_exact_integer_evidence() -> None:
    proposal = _apply(101, _factor())
    for value in (
        proposal.sample_count,
        proposal.minimum_required_sample_count,
        proposal.total_planned_duration_seconds,
        proposal.total_actual_duration_seconds,
        proposal.factor_numerator,
        proposal.factor_denominator,
        proposal.candidate_duration_seconds,
        proposal.scaled_numerator,
        proposal.quotient_seconds,
        proposal.remainder,
        proposal.calibrated_duration_seconds,
    ):
        assert type(value) is int
        assert not isinstance(value, bool)
    assert type(proposal.decision_id) is UUID
    assert type(proposal.rounded_up) is bool
    assert math.gcd(proposal.factor_numerator, proposal.factor_denominator) == 1


# --- Candidate-duration validation ------------------------------------------


def test_candidate_and_negative_and_non_integer_rejected() -> None:
    bad_candidates: list[object] = [
        -1,
        -10**30,
        True,
        False,
        100.0,
        0.0,
        Decimal("100"),
        Decimal("0"),
        "100",
        "0",
        None,
        [100],
        SimpleNamespace(value=100),
    ]
    factor = _factor()
    for bad in bad_candidates:
        with pytest.raises(CalibratedEffortProposalError, match="candidate_duration_seconds"):
            _apply(bad, factor)


def test_zero_candidate_is_valid_and_meaningful() -> None:
    proposal = _apply(0, _factor())
    assert proposal.candidate_duration_seconds == 0
    assert proposal.scaled_numerator == 0
    assert proposal.quotient_seconds == 0
    assert proposal.remainder == 0
    assert proposal.rounded_up is False
    assert proposal.calibrated_duration_seconds == 0


# --- Effective-factor input integrity ----------------------------------------


@pytest.mark.parametrize(
    "bad_factor",
    [
        "3/2",
        1.5,
        {"factor_numerator": 3, "factor_denominator": 2},
        SimpleNamespace(),
        None,
        42,
    ],
)
def test_non_v117_factor_rejected(bad_factor: object) -> None:
    with pytest.raises(CalibratedEffortProposalError, match="EffectiveEffortCalibrationFactor"):
        apply_effective_effort_calibration_factor(100, bad_factor)


def test_hostile_model_construct_factor_freshly_rejected() -> None:
    """A model_construct() factor bypassing validation is defeated.

    6/8 represents the same ratio as 3/4 but violates the exact
    reduced-factor invariant (gcd == 1), so this is genuinely invalid data
    whose invalidity is only visible under fresh revalidation.
    """
    hostile = EffectiveEffortCalibrationFactor.model_construct(
        entity_type=EntityType.TASK,
        decision_id=DECISION_ID,
        decided_at=DECIDED_AT,
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=200,
        total_actual_duration_seconds=150,
        factor_numerator=6,
        factor_denominator=8,
    )
    with pytest.raises(CalibratedEffortProposalError, match="gcd"):
        apply_effective_effort_calibration_factor(100, hostile)


def test_hostile_model_construct_naive_timestamp_factor_rejected() -> None:
    hostile = EffectiveEffortCalibrationFactor.model_construct(
        entity_type=EntityType.TASK,
        decision_id=DECISION_ID,
        decided_at=datetime(2025, 7, 1, 8, 30),  # naive: invalid under fresh validation
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=150,
        factor_numerator=3,
        factor_denominator=2,
    )
    with pytest.raises(CalibratedEffortProposalError, match="revalidation"):
        apply_effective_effort_calibration_factor(100, hostile)


def test_hostile_model_construct_float_fraction_factor_rejected() -> None:
    hostile = EffectiveEffortCalibrationFactor.model_construct(
        entity_type=EntityType.TASK,
        decision_id=DECISION_ID,
        decided_at=DECIDED_AT,
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=150,
        factor_numerator=3.0,  # strict model rejects this on fresh validation
        factor_denominator=2.0,
    )
    with pytest.raises(CalibratedEffortProposalError, match="revalidation"):
        apply_effective_effort_calibration_factor(100, hostile)


def test_supplied_factor_remains_unchanged() -> None:
    factor = _factor()
    before = factor.model_dump(mode="python")
    _apply(101, factor)
    after = factor.model_dump(mode="python")
    assert before == after


# --- Exact evidence copying ----------------------------------------------------


def test_factor_identity_and_evidence_copied_exactly() -> None:
    factor = _factor(
        entity_type=EntityType.PROJECT,
        decision_id=OTHER_DECISION_ID,
        sample_count=7,
        minimum_required_sample_count=3,
        planned=250,
        actual=200,
        numerator=4,
        denominator=5,
    )
    proposal = _apply(0, factor)
    assert proposal.entity_type is EntityType.PROJECT
    assert proposal.decision_id == OTHER_DECISION_ID
    assert proposal.decided_at == factor.decided_at
    assert proposal.sample_count == 7
    assert proposal.minimum_required_sample_count == 3
    assert proposal.total_planned_duration_seconds == 250
    assert proposal.total_actual_duration_seconds == 200
    assert proposal.factor_numerator == 4
    assert proposal.factor_denominator == 5

    # The copied evidence retains the exact V1.17 cross-multiplication
    # identity and sample-sufficiency invariants.
    assert (
        proposal.factor_numerator * proposal.total_planned_duration_seconds
        == proposal.factor_denominator * proposal.total_actual_duration_seconds
    )
    assert proposal.sample_count >= proposal.minimum_required_sample_count


# --- Exact integer arithmetic and half-up rounding ----------------------------


def test_unity_factor_leaves_candidate_unchanged() -> None:
    factor = _factor(planned=150, actual=150, numerator=1, denominator=1)
    proposal = _apply(12345, factor)
    assert proposal.scaled_numerator == 12345
    assert proposal.quotient_seconds == 12345
    assert proposal.remainder == 0
    assert proposal.rounded_up is False
    assert proposal.calibrated_duration_seconds == 12345


def test_exact_integer_multiplication_three_hundred_over_two() -> None:
    factor = _factor(numerator=3, denominator=2)
    proposal = _apply(100, factor)
    assert proposal.scaled_numerator == 300
    assert proposal.quotient_seconds == 150
    assert proposal.remainder == 0
    assert proposal.rounded_up is False
    assert proposal.calibrated_duration_seconds == 150


def test_below_half_rounds_down() -> None:
    # 10 * 5/4 = 12.5 - 0.5? No: use 4/3: 10 * 4/3 = 40/3 = 13.333 -> 13.
    factor = _factor(planned=3, actual=4, numerator=4, denominator=3)
    proposal = _apply(10, factor)
    assert proposal.scaled_numerator == 40
    assert proposal.quotient_seconds == 13
    assert proposal.remainder == 1
    assert 2 * proposal.remainder < factor.factor_denominator
    assert proposal.rounded_up is False
    assert proposal.calibrated_duration_seconds == 13


def test_above_half_rounds_up() -> None:
    # 101 * 3/2 = 151.5? No: 303/2 = 151 remainder 1; 2*1 >= 2 -> up -> 152.
    # Use a strictly above-half case: 10 * 5/4 = 12.5 is a tie; use 14 * 4/3
    # = 56/3 = 18.667 -> 19.
    factor = _factor(planned=3, actual=4, numerator=4, denominator=3)
    proposal = _apply(14, factor)
    assert proposal.scaled_numerator == 56
    assert proposal.quotient_seconds == 18
    assert proposal.remainder == 2
    assert 2 * proposal.remainder >= factor.factor_denominator
    assert proposal.rounded_up is True
    assert proposal.calibrated_duration_seconds == 19


def test_exact_half_rounds_up_for_101_over_3_over_2() -> None:
    # 101 * 3/2 = 303/2 = 151.5 -> 152 (explicit Issue example).
    factor = _factor(numerator=3, denominator=2)
    proposal = _apply(101, factor)
    assert proposal.scaled_numerator == 303
    assert proposal.quotient_seconds == 151
    assert proposal.remainder == 1
    assert proposal.rounded_up is True
    assert proposal.calibrated_duration_seconds == 152


def test_half_up_not_bankers_rounding_one_times_one_half() -> None:
    # 1 * 1/2 = 0.5: half-up -> 1; bankers (ties-to-even) would yield 0.
    factor = _factor(planned=2, actual=1, numerator=1, denominator=2)
    proposal = _apply(1, factor)
    assert proposal.scaled_numerator == 1
    assert proposal.quotient_seconds == 0
    assert proposal.remainder == 1
    assert proposal.rounded_up is True
    assert proposal.calibrated_duration_seconds == 1


def test_one_third_rounds_to_zero() -> None:
    # 1 * 1/3 = 0.333... -> 0 (explicit Issue example).
    factor = _factor(planned=3, actual=1, numerator=1, denominator=3)
    proposal = _apply(1, factor)
    assert proposal.scaled_numerator == 1
    assert proposal.quotient_seconds == 0
    assert proposal.remainder == 1
    assert proposal.rounded_up is False
    assert proposal.calibrated_duration_seconds == 0


def test_zero_factor_yields_zero() -> None:
    factor = _factor(planned=150, actual=0, numerator=0, denominator=1)
    proposal = _apply(987654, factor)
    assert proposal.factor_numerator == 0
    assert proposal.scaled_numerator == 0
    assert proposal.quotient_seconds == 0
    assert proposal.remainder == 0
    assert proposal.rounded_up is False
    assert proposal.calibrated_duration_seconds == 0


def test_zero_candidate_with_nonzero_factor_yields_zero() -> None:
    proposal = _apply(0, _factor(numerator=3, denominator=2))
    assert proposal.calibrated_duration_seconds == 0


def test_very_large_integers_remain_exact_and_deterministic() -> None:
    huge_candidate = 10**300 + 7
    huge_factor_num = 2**100 + 3
    # denominator 5 keeps gcd(num, 5) == 1 because num ends in 3.
    assert math.gcd(huge_factor_num, 5) == 1
    # Cross-multiplication evidence: planned * num == actual * denom,
    # i.e. planned = 5*k, actual = num*k with k = 3 (actual = num*3).
    factor = _factor(
        planned=15,
        actual=huge_factor_num * 3,
        numerator=huge_factor_num,
        denominator=5,
    )
    proposal = _apply(huge_candidate, factor)

    scaled = huge_candidate * huge_factor_num
    quotient, remainder = divmod(scaled, 5)
    expected = quotient + (1 if 2 * remainder >= 5 else 0)

    assert type(proposal.scaled_numerator) is int
    assert proposal.scaled_numerator == scaled
    assert proposal.quotient_seconds == quotient
    assert proposal.remainder == remainder
    assert 0 <= proposal.remainder < 5
    assert proposal.rounded_up is (2 * remainder >= 5)
    assert proposal.calibrated_duration_seconds == expected
    assert type(proposal.calibrated_duration_seconds) is int
    assert not isinstance(proposal.calibrated_duration_seconds, float)

    second = _apply(huge_candidate, factor)
    assert second.model_dump(mode="python") == proposal.model_dump(mode="python")


def test_arithmetic_evidence_is_exact_divmod() -> None:
    factor = _factor(numerator=7, denominator=9, planned=9, actual=7)
    proposal = _apply(37, factor)
    scaled = 37 * 7
    quotient, remainder = divmod(scaled, 9)
    assert proposal.scaled_numerator == scaled
    assert proposal.quotient_seconds == quotient
    assert proposal.remainder == remainder
    assert 0 <= proposal.remainder < 9
    assert proposal.rounded_up is (2 * remainder >= 9)
    assert proposal.calibrated_duration_seconds == quotient + int(2 * remainder >= 9)


@pytest.mark.parametrize(
    ("candidate", "numerator", "denominator", "calibrated"),
    [
        (100, 3, 2, 150),
        (101, 3, 2, 152),
        (1, 1, 2, 1),
        (1, 1, 3, 0),
        (0, 3, 2, 0),
        (1, 0, 1, 0),
        (1000000, 499999, 1000000 - 1, 499999 * 1000000 // 999999
         + (1 if 2 * (499999 * 1000000 % 999999) >= 999999 else 0)),
        (7, 5, 2, 18),  # 35/2 = 17.5 -> 18
        (3, 2, 5, 1),   # 6/5 = 1.2 -> 1
        (11, 2, 5, 4),  # 22/5 = 4.4 -> 4
    ],
)
def test_issue_examples_and_table(
    candidate: int, numerator: int, denominator: int, calibrated: int
) -> None:
    # valid evidence: planned = denominator, actual = numerator (k = 1).
    factor = _factor(
        planned=denominator,
        actual=numerator,
        numerator=numerator,
        denominator=denominator,
    )
    proposal = _apply(candidate, factor)
    assert proposal.calibrated_duration_seconds == calibrated
    assert proposal.rounded_up is (
        2 * (candidate * numerator % denominator) >= denominator
    )


# --- Hostile inconsistent result construction --------------------------------


def _hostile_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "entity_type": EntityType.TASK,
        "decision_id": DECISION_ID,
        "decided_at": DECIDED_AT,
        "sample_count": 5,
        "minimum_required_sample_count": 1,
        "total_planned_duration_seconds": 100,
        "total_actual_duration_seconds": 150,
        "factor_numerator": 3,
        "factor_denominator": 2,
        "candidate_duration_seconds": 100,
        "scaled_numerator": 300,
        "quotient_seconds": 150,
        "remainder": 0,
        "rounded_up": False,
        "calibrated_duration_seconds": 150,
    }
    base.update(overrides)
    return base


def test_hostile_result_with_wrong_scaled_numerator_rejected() -> None:
    with pytest.raises(ValidationError, match="scaled_numerator"):
        CalibratedEffortProposal(**_hostile_kwargs(scaled_numerator=301))


def test_hostile_result_with_wrong_scaled_numerator_minus_rejected() -> None:
    with pytest.raises(ValidationError, match="scaled_numerator"):
        CalibratedEffortProposal(**_hostile_kwargs(scaled_numerator=0))


def test_hostile_result_with_wrong_quotient_rejected() -> None:
    with pytest.raises(ValidationError, match="quotient_seconds"):
        CalibratedEffortProposal(**_hostile_kwargs(quotient_seconds=149))


def test_hostile_result_with_wrong_remainder_rejected() -> None:
    # For 300/2 the only valid remainder is 0; 1 is inconsistent.
    kwargs = _hostile_kwargs(
        remainder=1, rounded_up=True, calibrated_duration_seconds=151
    )
    with pytest.raises(ValidationError, match="remainder"):
        CalibratedEffortProposal(**kwargs)


def test_hostile_result_with_remainder_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        CalibratedEffortProposal(**_hostile_kwargs(
            scaled_numerator=0,
            quotient_seconds=0,
            remainder=5,
            rounded_up=True,
            calibrated_duration_seconds=1,
        ))


def test_hostile_result_with_wrong_rounded_up_rejected() -> None:
    # Exact tie 1 * 3/2: remainder=1, denominator=2 -> rounded_up must be True.
    kwargs = _hostile_kwargs(
        candidate_duration_seconds=1,
        scaled_numerator=3,
        quotient_seconds=1,
        remainder=1,
        rounded_up=False,
        calibrated_duration_seconds=1,
    )
    with pytest.raises(ValidationError, match="rounded_up"):
        CalibratedEffortProposal(**kwargs)


def test_hostile_result_with_wrong_calibrated_output_rejected() -> None:
    with pytest.raises(ValidationError, match="calibrated_duration_seconds"):
        CalibratedEffortProposal(**_hostile_kwargs(calibrated_duration_seconds=149))


def test_hostile_result_accepting_alternate_rounding_interpretation_rejected() -> None:
    # 1 * 1/2 under bankers rounding would be quotient=0, remainder=1,
    # rounded_up=False, calibrated=0. Half-up is the only accepted rule.
    factor_fields = dict(
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=2,
        total_actual_duration_seconds=1,
        factor_numerator=1,
        factor_denominator=2,
    )
    with pytest.raises(ValidationError, match="rounded_up"):
        CalibratedEffortProposal(
            entity_type=EntityType.TASK,
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            **factor_fields,
            candidate_duration_seconds=1,
            scaled_numerator=1,
            quotient_seconds=0,
            remainder=1,
            rounded_up=False,
            calibrated_duration_seconds=0,
        )


def test_hostile_model_construct_result_inconsistent_rejected() -> None:
    hostile = CalibratedEffortProposal.model_construct(
        entity_type=EntityType.TASK,
        decision_id=DECISION_ID,
        decided_at=DECIDED_AT,
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=150,
        factor_numerator=3,
        factor_denominator=2,
        candidate_duration_seconds=100,
        scaled_numerator=999,
        quotient_seconds=499,
        remainder=1,
        rounded_up=True,
        calibrated_duration_seconds=500,
    )
    # The object exists but is invalid; fresh strict revalidation must
    # refuse it and downstream consumers must see a clean rejection.
    dumped = hostile.model_dump(mode="python")
    with pytest.raises(ValidationError, match="scaled_numerator"):
        CalibratedEffortProposal.model_validate(dumped, strict=True)


# --- Determinism, purity, and boundary hygiene --------------------------------


def test_repeated_equivalent_calls_return_equivalent_immutable_results() -> None:
    factor = _factor()
    first = apply_effective_effort_calibration_factor(101, factor)
    second = apply_effective_effort_calibration_factor(101, factor)
    assert first == second
    assert first.model_dump(mode="python") == second.model_dump(mode="python")
    with pytest.raises(ValidationError):
        first.scaled_numerator = 1  # type: ignore[misc]


def test_all_values_are_plain_integers_no_float_or_decimal() -> None:
    proposal = _apply(101, _factor())
    dumped = proposal.model_dump(mode="python")
    for key, value in dumped.items():
        if key == "entity_type":
            assert isinstance(value, EntityType)
            assert not isinstance(value, (float, Decimal))
        elif key == "decision_id":
            assert isinstance(value, UUID)
        elif key == "decided_at":
            assert isinstance(value, datetime)
        else:
            assert key not in ("rounded_up",) or isinstance(value, bool)
            if key != "rounded_up":
                assert type(value) is int
                assert not isinstance(value, (float, Decimal))


def test_pure_boundary_accepts_no_repository_or_reader() -> None:
    parameters = set(inspect.signature(apply_effective_effort_calibration_factor).parameters)
    assert parameters == {"candidate_duration_seconds", "factor"}


def test_error_is_value_error_subclass() -> None:
    assert issubclass(CalibratedEffortProposalError, ValueError)
