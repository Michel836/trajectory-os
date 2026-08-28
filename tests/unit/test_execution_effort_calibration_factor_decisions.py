"""Unit tests for the V1.16 immutable human-decision record over V1.15 proposals.

Covers the strict frozen record model, the closed decision vocabulary with
no default, the exact per-reason snapshot invariants (including the exact
cross-multiplication identity and reduced integer factor components), and
the human decision rule (ACCEPT only over an AVAILABLE proposal).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticUndefined

from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_factor_decisions import (
    EffortCalibrationDecision,
    EffortCalibrationFactorDecision,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
)

PORTFOLIO_ID = UUID("61616161-6161-4161-8161-616161616161")
PROJECT_ID = UUID("62626262-6262-4262-8262-626262626262")
DECISION_ID = UUID("63636363-6363-4363-8363-636363636363")
DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
OFFSET_TZ = timezone(timedelta(hours=2))


def _available_kwargs(**overrides: object) -> dict[str, object]:
    """Keyword fields of a valid AVAILABLE-proposal decision record."""
    values: dict[str, object] = {
        "decision_id": DECISION_ID,
        "portfolio_id": PORTFOLIO_ID,
        "project_id": PROJECT_ID,
        "entity_type": EntityType.TASK,
        "sample_count": 5,
        "minimum_required_sample_count": 1,
        "total_planned_duration_seconds": 100,
        "total_actual_duration_seconds": 150,
        "proposal_available": True,
        "proposal_reason": EffortCalibrationFactorProposalReason.AVAILABLE,
        "factor_numerator": 3,
        "factor_denominator": 2,
        "decision": EffortCalibrationDecision.ACCEPT,
        "decided_at": DECIDED_AT,
    }
    values.update(overrides)
    return values


def _record(**overrides: object) -> EffortCalibrationFactorDecision:
    return EffortCalibrationFactorDecision(**_available_kwargs(**overrides))


def _merged(overrides: dict[str, object]) -> dict[str, object]:
    merged: dict[str, object] = _available_kwargs()
    merged.update(overrides)
    return merged


def _insufficient_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "proposal_available": False,
        "proposal_reason": EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES,
        "factor_numerator": None,
        "factor_denominator": None,
        "sample_count": 2,
        "minimum_required_sample_count": 5,
    }
    values.update(overrides)
    return values


def _zero_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "proposal_available": False,
        "proposal_reason": (
            EffortCalibrationFactorProposalReason.ZERO_TOTAL_PLANNED_DURATION
        ),
        "factor_numerator": None,
        "factor_denominator": None,
        "total_planned_duration_seconds": 0,
    }
    values.update(overrides)
    return values


def test_valid_accept_available_record_is_preserved_exactly() -> None:
    record = _record()
    assert record.decision_id == DECISION_ID
    assert record.portfolio_id == PORTFOLIO_ID
    assert record.project_id == PROJECT_ID
    assert record.entity_type is EntityType.TASK
    assert record.sample_count == 5
    assert record.minimum_required_sample_count == 1
    assert record.total_planned_duration_seconds == 100
    assert record.total_actual_duration_seconds == 150
    assert record.proposal_available is True
    assert (
        record.proposal_reason is EffortCalibrationFactorProposalReason.AVAILABLE
    )
    assert record.factor_numerator == 3
    assert record.factor_denominator == 2
    assert record.decision is EffortCalibrationDecision.ACCEPT
    assert record.decided_at == DECIDED_AT
    assert record.decided_at is DECIDED_AT


def test_available_record_fields_are_exact_integers_not_float_or_bool() -> None:
    record = _record()
    for value in (
        record.sample_count,
        record.minimum_required_sample_count,
        record.total_planned_duration_seconds,
        record.total_actual_duration_seconds,
        record.factor_numerator,
        record.factor_denominator,
    ):
        assert isinstance(value, int)
        assert not isinstance(value, bool)
    assert type(record.factor_numerator) is int
    assert type(record.factor_denominator) is int


def test_cross_multiplication_identity_holds() -> None:
    record = _record()
    assert (
        record.factor_numerator * record.total_planned_duration_seconds
        == record.factor_denominator * record.total_actual_duration_seconds
    )


def test_record_is_frozen() -> None:
    record = _record()
    with pytest.raises(ValidationError):
        record.decision = EffortCalibrationDecision.REJECT  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record.factor_numerator = 8  # type: ignore[misc]
    assert record.decision is EffortCalibrationDecision.ACCEPT


@pytest.mark.parametrize(
    "overrides",
    [
        {"decision_id": str(DECISION_ID)},
        {"portfolio_id": "not-a-uuid"},
        {"sample_count": "5"},
        {"sample_count": True},
        {"minimum_required_sample_count": 0},
        {"total_planned_duration_seconds": -1},
        {"proposal_available": "yes"},
        {"decision": "accept"},
        {"entity_type": "task"},
        {"decided_at": "2025-07-01T08:30:00+00:00"},
        {"factor_numerator": 3.5},
    ],
)
def test_strict_validation_rejects_coerced_or_invalid_values(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _record(**overrides)


def test_unknown_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _record(unexpected_field="nope")  # type: ignore[call-arg]


def test_no_default_decision_exists() -> None:
    field_info = EffortCalibrationFactorDecision.model_fields["decision"]
    assert field_info.is_required()
    assert field_info.default is PydanticUndefined

    kwargs = _available_kwargs()
    del kwargs["decision"]
    with pytest.raises(ValidationError):
        EffortCalibrationFactorDecision(**kwargs)  # type: ignore[arg-type]


def test_decision_vocabulary_is_exactly_accept_reject_defer() -> None:
    assert set(EffortCalibrationDecision.__members__) == {
        "ACCEPT",
        "REJECT",
        "DEFER",
    }
    assert EffortCalibrationDecision.ACCEPT.value == "accept"
    assert EffortCalibrationDecision.REJECT.value == "reject"
    assert EffortCalibrationDecision.DEFER.value == "defer"


def test_naive_decided_at_is_rejected() -> None:
    naive = datetime(2025, 7, 1, 8, 30)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _record(decided_at=naive)


def test_supplied_aware_timestamp_is_preserved_exactly() -> None:
    offset_moment = datetime(2029, 1, 1, 12, 0, tzinfo=OFFSET_TZ)
    record = _record(
        decided_at=offset_moment,
        decision_id=UUID("64646464-6464-4464-8464-646464646464"),
    )
    assert record.decided_at == offset_moment
    assert record.decided_at is offset_moment
    assert record.decided_at.utcoffset() == timedelta(hours=2)
    utc_moment = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
    record2 = _record(
        decided_at=utc_moment,
        decision_id=UUID("68686868-6868-4868-8868-686868686868"),
    )
    assert record2.decided_at == utc_moment


def test_reject_and_defer_are_valid_over_any_valid_segment() -> None:
    for decision in (
        EffortCalibrationDecision.REJECT,
        EffortCalibrationDecision.DEFER,
    ):
        insufficient = EffortCalibrationFactorDecision(
            **_merged({**_insufficient_kwargs(), "decision": decision})
        )
        assert insufficient.decision is decision

        zero = EffortCalibrationFactorDecision(
            **_merged({**_zero_kwargs(), "decision": decision})
        )
        assert zero.decision is decision

        available = _record(decision=decision)
        assert available.decision is decision


def test_accept_over_insufficient_samples_segment_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ACCEPT is valid only"):
        EffortCalibrationFactorDecision(
            **_merged(
                {
                    **_insufficient_kwargs(),
                    "decision": EffortCalibrationDecision.ACCEPT,
                }
            )
        )


def test_accept_over_zero_total_planned_segment_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ACCEPT is valid only"):
        EffortCalibrationFactorDecision(
            **_merged(
                {
                    **_zero_kwargs(),
                    "decision": EffortCalibrationDecision.ACCEPT,
                }
            )
        )


def test_unavailable_snapshot_must_not_carry_factor_components() -> None:
    with pytest.raises(ValidationError, match="factor components"):
        _record(**{**_insufficient_kwargs(), "factor_numerator": 3, "factor_denominator": 2})


def test_available_requires_reduced_integer_factor() -> None:
    with pytest.raises(ValidationError, match="gcd"):
        _record(factor_numerator=6, factor_denominator=4)


def test_available_requires_cross_multiplication_identity() -> None:
    with pytest.raises(ValidationError, match="factor"):
        _record(factor_numerator=8, factor_denominator=5)


def test_available_requires_lower_bounds() -> None:
    with pytest.raises(ValidationError, match="sample_count"):
        _record(sample_count=0, factor_numerator=2, factor_denominator=3)
    with pytest.raises(ValidationError, match="total_planned_duration_seconds"):
        _record(total_planned_duration_seconds=0)


def test_zero_reason_requires_zero_planned_total() -> None:
    with pytest.raises(ValidationError, match="total_planned_duration_seconds"):
        _record(
            proposal_available=False,
            proposal_reason=(
                EffortCalibrationFactorProposalReason.ZERO_TOTAL_PLANNED_DURATION
            ),
            factor_numerator=None,
            factor_denominator=None,
            total_planned_duration_seconds=100,
        )


def test_availability_flag_must_match_reason() -> None:
    with pytest.raises(ValidationError, match="consistent"):
        _record(
            proposal_available=False,
            factor_numerator=3,
            factor_denominator=2,
        )


def test_explicit_uuids_are_preserved() -> None:
    portfolio = UUID("65656565-6565-4565-8565-656565656565")
    project = UUID("66666666-6666-4666-8666-666666666666")
    decision = UUID("67676767-6767-4767-8767-676767676767")
    record = _record(
        portfolio_id=portfolio,
        project_id=project,
        decision_id=decision,
    )
    assert record.portfolio_id == portfolio
    assert record.project_id == project
    assert record.decision_id == decision
    assert type(record.decision_id) is UUID


def test_record_dump_keeps_exact_integer_factor() -> None:
    record = _record()
    dumped = record.model_dump()
    assert dumped["factor_numerator"] == 3
    assert dumped["factor_denominator"] == 2
    assert type(dumped["factor_numerator"]) is int
    assert dumped["decision"] == EffortCalibrationDecision.ACCEPT
