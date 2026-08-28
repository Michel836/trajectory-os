"""Unit tests for the V1.17 pure effective accepted calibration resolution.

Covers the strict/frozen result models, the exact effective-factor invariants
(reduced integer factor + cross-multiplication identity), the documented
effective policy (ACCEPT only; latest by chronological instant with UUID
integer tie-break; NO revocation by later REJECT/DEFER; later ACCEPT
supersedes only in derived selection), empty and no-ACCEPT semantics,
deterministic first-appearance output ordering, hostile model_construct()
fresh revalidation (including non-ACCEPT records), mixed-scope and
duplicate-ID rejection, input immutability, and the absence of any
repository/reader dependency on the pure boundary.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactor,
    EffectiveEffortCalibrationFactorError,
    EffectiveEffortCalibrationFactorSet,
    resolve_effective_effort_calibration_factors,
)
from trajectory_os.domain.execution_effort_calibration_factor_decisions import (
    EffortCalibrationDecision,
    EffortCalibrationFactorDecision,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
)

PORTFOLIO_ID = UUID("81818181-8181-4181-8181-818181818181")
PROJECT_ID = UUID("82828282-8282-4282-8282-828282828282")
OTHER_PORTFOLIO_ID = UUID("83838383-8383-4383-8383-838383838383")
OTHER_PROJECT_ID = UUID("84848484-8484-4484-8484-848484848484")
DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
OFFSET_TZ = timezone(timedelta(hours=2))


def _decision(
    *,
    decision: EffortCalibrationDecision = EffortCalibrationDecision.ACCEPT,
    entity_type: EntityType = EntityType.TASK,
    decided_at: datetime = DECIDED_AT,
    decision_id: UUID | None = None,
    sample_count: int = 5,
    minimum_required_sample_count: int = 1,
    total_planned: int = 100,
    total_actual: int = 150,
    numerator: int | None = 3,
    denominator: int | None = 2,
    portfolio_id: UUID = PORTFOLIO_ID,
    project_id: UUID = PROJECT_ID,
) -> EffortCalibrationFactorDecision:
    """One valid V1.16 record (AVAILABLE proposal snapshot by default)."""
    return EffortCalibrationFactorDecision(
        decision_id=decision_id or uuid4(),
        portfolio_id=portfolio_id,
        project_id=project_id,
        entity_type=entity_type,
        sample_count=sample_count,
        minimum_required_sample_count=minimum_required_sample_count,
        total_planned_duration_seconds=total_planned,
        total_actual_duration_seconds=total_actual,
        proposal_available=True,
        proposal_reason=EffortCalibrationFactorProposalReason.AVAILABLE,
        factor_numerator=numerator,
        factor_denominator=denominator,
        decision=decision,
        decided_at=decided_at,
    )


def _rejection() -> EffortCalibrationFactorDecision:
    """One valid V1.16 REJECT record over an unavailable segment."""
    return EffortCalibrationFactorDecision(
        decision_id=uuid4(),
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=EntityType.TASK,
        sample_count=2,
        minimum_required_sample_count=5,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=10,
        proposal_available=False,
        proposal_reason=EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES,
        factor_numerator=None,
        factor_denominator=None,
        decision=EffortCalibrationDecision.REJECT,
        decided_at=DECIDED_AT,
    )


def _factor(
    *,
    entity_type: EntityType = EntityType.TASK,
    sample_count: int = 5,
    minimum_required_sample_count: int = 1,
    planned: int = 100,
    actual: int = 150,
    numerator: int = 3,
    denominator: int = 2,
) -> EffectiveEffortCalibrationFactor:
    return EffectiveEffortCalibrationFactor(
        entity_type=entity_type,
        decision_id=uuid4(),
        decided_at=DECIDED_AT,
        sample_count=sample_count,
        minimum_required_sample_count=minimum_required_sample_count,
        total_planned_duration_seconds=planned,
        total_actual_duration_seconds=actual,
        factor_numerator=numerator,
        factor_denominator=denominator,
    )


# --- Result models: strict, frozen, self-auditing ---------------------------


def test_factor_model_is_frozen_and_strict() -> None:
    factor = _factor()
    with pytest.raises(ValidationError):
        factor.factor_numerator = 8  # type: ignore[misc]
    with pytest.raises(ValidationError):
        factor.decision_id = uuid4()  # type: ignore[misc]
    assert factor.factor_numerator == 3


def test_factor_model_rejects_extra_fields_and_coercion() -> None:
    with pytest.raises(ValidationError):
        EffectiveEffortCalibrationFactor(
            entity_type=EntityType.TASK,
            decision_id=uuid4(),
            decided_at=DECIDED_AT,
            sample_count=5,
            minimum_required_sample_count=1,
            total_planned_duration_seconds=100,
            total_actual_duration_seconds=150,
            factor_numerator=3,
            factor_denominator=2,
            unexpected="nope",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        EffectiveEffortCalibrationFactor(
            entity_type=EntityType.TASK,
            decision_id=str(uuid4()),
            decided_at=DECIDED_AT,
            sample_count=5,
            minimum_required_sample_count=1,
            total_planned_duration_seconds=100,
            total_actual_duration_seconds=150,
            factor_numerator=3,
            factor_denominator=2,
        )
    with pytest.raises(ValidationError):
        EffectiveEffortCalibrationFactor(
            entity_type=EntityType.TASK,
            decision_id=uuid4(),
            decided_at=DECIDED_AT,
            sample_count=5,
            minimum_required_sample_count=1,
            total_planned_duration_seconds=100,
            total_actual_duration_seconds=150,
            factor_numerator=3.0,
            factor_denominator=2,
        )
    with pytest.raises(ValidationError):
        EffectiveEffortCalibrationFactor(
            entity_type=EntityType.TASK,
            decision_id=uuid4(),
            decided_at=datetime(2025, 7, 1, 8, 30),  # naive
            sample_count=5,
            minimum_required_sample_count=1,
            total_planned_duration_seconds=100,
            total_actual_duration_seconds=150,
            factor_numerator=3,
            factor_denominator=2,
        )


def test_factor_model_rejects_non_reduced_factor() -> None:
    with pytest.raises(ValidationError, match="gcd"):
        _factor(planned=200, actual=150, numerator=6, denominator=8)


def test_factor_model_rejects_cross_multiplication_violation() -> None:
    with pytest.raises(ValidationError, match="factor"):
        _factor(planned=100, actual=150, numerator=8, denominator=5)


def test_factor_model_rejects_insufficient_sample_evidence() -> None:
    with pytest.raises(ValidationError, match="minimum_required_sample_count"):
        _factor(sample_count=2, minimum_required_sample_count=5)


def test_set_model_is_frozen_and_allows_empty_factors() -> None:
    empty = EffectiveEffortCalibrationFactorSet(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        factors=(),
    )
    assert empty.factors == ()
    with pytest.raises(ValidationError):
        empty.portfolio_id = OTHER_PORTFOLIO_ID  # type: ignore[misc]


def test_set_model_rejects_duplicate_entity_type_factors() -> None:
    with pytest.raises(ValidationError, match="at most one"):
        EffectiveEffortCalibrationFactorSet(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            factors=(_factor(), _factor()),
        )


def test_factor_result_retains_exact_integer_evidence_and_uuid() -> None:
    factor = _factor()
    for value in (
        factor.sample_count,
        factor.minimum_required_sample_count,
        factor.total_planned_duration_seconds,
        factor.total_actual_duration_seconds,
        factor.factor_numerator,
        factor.factor_denominator,
    ):
        assert isinstance(value, int)
        assert not isinstance(value, bool)
        assert type(value) is int
    assert type(factor.decision_id) is UUID


# --- Public boundary integrity ----------------------------------------------


@pytest.mark.parametrize(
    "bad_portfolio",
    [str(PORTFOLIO_ID), 12345, None, True, b"\x00" * 16],
)
def test_non_uuid_portfolio_scope_rejected_without_coercion(
    bad_portfolio: object,
) -> None:
    with pytest.raises(EffectiveEffortCalibrationFactorError, match="portfolio_id"):
        resolve_effective_effort_calibration_factors([_decision()], bad_portfolio, PROJECT_ID)


@pytest.mark.parametrize(
    "bad_project",
    [str(PROJECT_ID), 12345, None, True, b"\x00" * 16],
)
def test_non_uuid_project_scope_rejected_without_coercion(
    bad_project: object,
) -> None:
    with pytest.raises(EffectiveEffortCalibrationFactorError, match="project_id"):
        resolve_effective_effort_calibration_factors([_decision()], PORTFOLIO_ID, bad_project)


@pytest.mark.parametrize("bad_item", ["accept", SimpleNamespace(), {"decision_id": 1}])
def test_non_v116_input_item_rejected(bad_item: object) -> None:
    with pytest.raises(
        EffectiveEffortCalibrationFactorError, match="EffortCalibrationFactorDecision"
    ):
        resolve_effective_effort_calibration_factors([bad_item], PORTFOLIO_ID, PROJECT_ID)


def test_hostile_model_construct_accept_record_freshly_rejected() -> None:
    """A model_construct() ACCEPT bypassing validation is defeated.

    6/8 represents the same ratio as 3/4 but violates the exact
    reduced-factor invariant (gcd == 1), so genuinely invalid data whose
    invalidity is only visible under fresh revalidation.
    """
    hostile = EffortCalibrationFactorDecision.model_construct(
        decision_id=uuid4(),
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=EntityType.TASK,
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=200,
        total_actual_duration_seconds=150,
        proposal_available=True,
        proposal_reason=EffortCalibrationFactorProposalReason.AVAILABLE,
        factor_numerator=6,
        factor_denominator=8,
        decision=EffortCalibrationDecision.ACCEPT,
        decided_at=DECIDED_AT,
    )
    with pytest.raises(EffectiveEffortCalibrationFactorError, match="gcd"):
        resolve_effective_effort_calibration_factors([hostile], PORTFOLIO_ID, PROJECT_ID)


def test_hostile_model_construct_nona_accept_record_also_rejected() -> None:
    """Every supplied record is freshly validated, REJECT included.

    A REJECT record whose availability flag disagrees with its reason
    passes isinstance but is invalid under fresh validation and must fail
    the whole resolution rather than being skipped.
    """
    hostile_reject = EffortCalibrationFactorDecision.model_construct(
        decision_id=uuid4(),
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=EntityType.TASK,
        sample_count=2,
        minimum_required_sample_count=5,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=10,
        proposal_available=True,
        proposal_reason=EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES,
        factor_numerator=None,
        factor_denominator=None,
        decision=EffortCalibrationDecision.REJECT,
        decided_at=DECIDED_AT,
    )
    with pytest.raises(EffectiveEffortCalibrationFactorError, match="revalidation"):
        resolve_effective_effort_calibration_factors(
            [_decision(), hostile_reject], PORTFOLIO_ID, PROJECT_ID
        )
    with pytest.raises(EffectiveEffortCalibrationFactorError, match="revalidation"):
        resolve_effective_effort_calibration_factors([hostile_reject], PORTFOLIO_ID, PROJECT_ID)


def test_foreign_portfolio_decision_rejected() -> None:
    foreign = _decision(portfolio_id=OTHER_PORTFOLIO_ID)
    with pytest.raises(EffectiveEffortCalibrationFactorError, match="portfolio"):
        resolve_effective_effort_calibration_factors([foreign], PORTFOLIO_ID, PROJECT_ID)


def test_foreign_project_decision_rejected() -> None:
    foreign = _decision(project_id=OTHER_PROJECT_ID)
    with pytest.raises(EffectiveEffortCalibrationFactorError, match="project"):
        resolve_effective_effort_calibration_factors([foreign], PORTFOLIO_ID, PROJECT_ID)


def test_duplicate_decision_ids_rejected_not_deduplicated() -> None:
    shared_id = uuid4()
    first = _decision(decision_id=shared_id)
    second = _decision(
        decision_id=shared_id,
        entity_type=EntityType.PROJECT,
        decided_at=DECIDED_AT + timedelta(days=1),
    )
    with pytest.raises(EffectiveEffortCalibrationFactorError, match="duplicate decision_id"):
        resolve_effective_effort_calibration_factors([first, second], PORTFOLIO_ID, PROJECT_ID)


# --- Effective policy --------------------------------------------------------


def test_empty_history_yields_empty_effective_set() -> None:
    result = resolve_effective_effort_calibration_factors([], PORTFOLIO_ID, PROJECT_ID)
    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_id == PROJECT_ID
    assert result.factors == ()


def test_reject_only_history_yields_empty_effective_set() -> None:
    result = resolve_effective_effort_calibration_factors([_rejection()], PORTFOLIO_ID, PROJECT_ID)
    assert result.factors == ()


def test_defer_only_history_yields_empty_effective_set() -> None:
    result = resolve_effective_effort_calibration_factors(
        [_decision(decision=EffortCalibrationDecision.DEFER)],
        PORTFOLIO_ID,
        PROJECT_ID,
    )
    assert result.factors == ()


def test_reject_and_defer_only_history_yields_empty_effective_set() -> None:
    result = resolve_effective_effort_calibration_factors(
        [
            _rejection(),
            _decision(decision=EffortCalibrationDecision.DEFER),
        ],
        PORTFOLIO_ID,
        PROJECT_ID,
    )
    assert result.factors == ()


def test_single_accept_yields_the_exact_effective_factor() -> None:
    source = _decision(
        sample_count=7,
        minimum_required_sample_count=3,
        total_planned=250,
        total_actual=200,
        numerator=4,
        denominator=5,
    )
    result = resolve_effective_effort_calibration_factors([source], PORTFOLIO_ID, PROJECT_ID)
    assert len(result.factors) == 1
    factor = result.factors[0]
    assert factor.entity_type is EntityType.TASK
    assert factor.decision_id == source.decision_id
    assert factor.decided_at == source.decided_at
    assert factor.sample_count == 7
    assert factor.minimum_required_sample_count == 3
    assert factor.total_planned_duration_seconds == 250
    assert factor.total_actual_duration_seconds == 200
    assert factor.factor_numerator == 4
    assert factor.factor_denominator == 5


def test_accepted_snapshot_evidence_copied_exactly() -> None:
    offset_moment = datetime(2026, 3, 2, 1, 45, tzinfo=OFFSET_TZ)
    source = _decision(
        decided_at=offset_moment,
        total_planned=480,
        total_actual=600,
        numerator=5,
        denominator=4,
    )
    factor = resolve_effective_effort_calibration_factors(
        [source], PORTFOLIO_ID, PROJECT_ID
    ).factors[0]

    dumped = factor.model_dump(mode="python")
    source_dumped = source.model_dump(mode="python")
    for key in (
        "entity_type",
        "decision_id",
        "decided_at",
        "sample_count",
        "minimum_required_sample_count",
        "total_planned_duration_seconds",
        "total_actual_duration_seconds",
        "factor_numerator",
        "factor_denominator",
    ):
        assert dumped[key] == source_dumped[key]

    # The exact caller-supplied aware timestamp (and its offset) survives.
    assert factor.decided_at == offset_moment
    assert factor.decided_at.utcoffset() == timedelta(hours=2)


def test_effective_factor_represented_only_by_exact_integers() -> None:
    factor = _factor()
    result = resolve_effective_effort_calibration_factors([_decision()], PORTFOLIO_ID, PROJECT_ID)
    dumped = result.model_dump(mode="python")
    for value in (
        dumped["factors"][0]["sample_count"],
        dumped["factors"][0]["minimum_required_sample_count"],
        dumped["factors"][0]["total_planned_duration_seconds"],
        dumped["factors"][0]["total_actual_duration_seconds"],
        dumped["factors"][0]["factor_numerator"],
        dumped["factors"][0]["factor_denominator"],
    ):
        assert type(value) is int
        assert not isinstance(value, float)
        assert (
            factor.factor_numerator * dumped["factors"][0]["total_planned_duration_seconds"]
            == factor.factor_denominator * dumped["factors"][0]["total_actual_duration_seconds"]
        )


def test_multiple_accepts_later_chronological_instant_wins() -> None:
    earlier = _decision(
        decided_at=datetime(2025, 7, 1, 8, 30, tzinfo=UTC),
        total_planned=200,
        total_actual=150,
        numerator=3,
        denominator=4,
    )
    later = _decision(
        decided_at=datetime(2025, 7, 2, 8, 30, tzinfo=UTC),
        total_planned=250,
        total_actual=150,
        numerator=3,
        denominator=5,
    )
    result = resolve_effective_effort_calibration_factors(
        [earlier, later], PORTFOLIO_ID, PROJECT_ID
    )
    assert [factor.factor_denominator for factor in result.factors] == [5]
    assert result.factors[0].decision_id == later.decision_id

    # Supplied order must not matter: only the policy key decides.
    reversed_result = resolve_effective_effort_calibration_factors(
        [later, earlier], PORTFOLIO_ID, PROJECT_ID
    )
    assert reversed_result.factors[0].decision_id == later.decision_id


def test_equal_accept_instants_broken_by_larger_uuid_integer() -> None:
    low_uuid = UUID("aaaa0000-0000-0000-0000-000000000000")
    high_uuid = UUID("bbbb0000-0000-0000-0000-000000000000")
    instant = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
    low = _decision(decision_id=low_uuid, decided_at=instant)
    high = _decision(decision_id=high_uuid, decided_at=instant)
    result = resolve_effective_effort_calibration_factors([low, high], PORTFOLIO_ID, PROJECT_ID)
    assert result.factors[0].decision_id == high_uuid

    reversed_result = resolve_effective_effort_calibration_factors(
        [high, low], PORTFOLIO_ID, PROJECT_ID
    )
    assert reversed_result.factors[0].decision_id == high_uuid


def test_mixed_timezone_offsets_compare_by_chronological_instant() -> None:
    # 10:00 UTC is LATER than 09:30 instant written as 11:30 +02:00;
    # lexical ISO comparison would pick the wrong record.
    earlier_instant = datetime(2025, 7, 1, 11, 30, tzinfo=OFFSET_TZ)  # 09:30Z
    later_instant = datetime(2025, 7, 1, 10, 0, tzinfo=UTC)  # 10:00Z
    earlier = _decision(
        decided_at=earlier_instant,
        total_planned=100,
        total_actual=100,
        numerator=1,
        denominator=1,
    )
    later = _decision(
        decided_at=later_instant,
        total_planned=100,
        total_actual=200,
        numerator=2,
        denominator=1,
    )
    result = resolve_effective_effort_calibration_factors(
        [earlier, later], PORTFOLIO_ID, PROJECT_ID
    )
    assert result.factors[0].decision_id == later.decision_id
    assert result.factors[0].factor_denominator == 1
    assert result.factors[0].factor_numerator == 2


def test_later_reject_does_not_revoke_prior_accept() -> None:
    accept = _decision(decided_at=datetime(2025, 7, 1, 8, 0, tzinfo=UTC))
    later_reject = _decision(
        decision=EffortCalibrationDecision.REJECT,
        decided_at=datetime(2025, 7, 5, 8, 0, tzinfo=UTC),
    )
    result = resolve_effective_effort_calibration_factors(
        [accept, later_reject], PORTFOLIO_ID, PROJECT_ID
    )
    assert len(result.factors) == 1
    assert result.factors[0].decision_id == accept.decision_id


def test_later_defer_does_not_revoke_prior_accept() -> None:
    accept = _decision(decided_at=datetime(2025, 7, 1, 8, 0, tzinfo=UTC))
    later_defer = _decision(
        decision=EffortCalibrationDecision.DEFER,
        decided_at=datetime(2025, 7, 5, 8, 0, tzinfo=UTC),
    )
    result = resolve_effective_effort_calibration_factors(
        [accept, later_defer], PORTFOLIO_ID, PROJECT_ID
    )
    assert len(result.factors) == 1
    assert result.factors[0].decision_id == accept.decision_id


def test_later_accept_supersedes_earlier_accept_for_selection_only() -> None:
    earlier = _decision(
        decided_at=datetime(2025, 7, 1, 8, 0, tzinfo=UTC),
        total_planned=200,
        total_actual=150,
        numerator=3,
        denominator=4,
    )
    later = _decision(
        decided_at=datetime(2025, 7, 3, 8, 0, tzinfo=UTC),
        total_planned=250,
        total_actual=150,
        numerator=3,
        denominator=5,
    )
    result = resolve_effective_effort_calibration_factors(
        [earlier, later], PORTFOLIO_ID, PROJECT_ID
    )
    assert result.factors[0].decision_id == later.decision_id
    assert result.factors[0].factor_denominator == 5

    # Supersession is DERIVED selection only: the earlier record remains
    # part of the (immutable, queryable) supplied history, unchanged.
    assert earlier in [earlier, later]
    assert earlier.total_planned_duration_seconds == 200 and earlier.factor_denominator == 4


def test_multiple_entity_types_resolve_independently() -> None:
    task_accept = _decision(
        entity_type=EntityType.TASK,
        numerator=3,
        denominator=4,
        total_planned=200,
        total_actual=150,
    )
    project_accept = _decision(
        entity_type=EntityType.PROJECT,
        numerator=5,
        denominator=2,
        total_planned=100,
        total_actual=250,
    )
    deliverable_reject = _decision(
        entity_type=EntityType.DELIVERABLE,
        decision=EffortCalibrationDecision.REJECT,
    )
    result = resolve_effective_effort_calibration_factors(
        [task_accept, project_accept, deliverable_reject],
        PORTFOLIO_ID,
        PROJECT_ID,
    )
    by_type = {factor.entity_type: factor for factor in result.factors}
    assert set(by_type) == {EntityType.TASK, EntityType.PROJECT}
    assert by_type[EntityType.TASK].factor_denominator == 4
    assert by_type[EntityType.PROJECT].factor_denominator == 2


def test_entity_type_without_accept_emits_no_factor() -> None:
    only_defer_type = _decision(
        entity_type=EntityType.ROUTINE,
        decision=EffortCalibrationDecision.DEFER,
    )
    task_accept = _decision(entity_type=EntityType.TASK)
    result = resolve_effective_effort_calibration_factors(
        [only_defer_type, task_accept], PORTFOLIO_ID, PROJECT_ID
    )
    assert [factor.entity_type for factor in result.factors] == [EntityType.TASK]


def test_output_order_is_deterministic_first_appearance_order() -> None:
    project_accept = _decision(entity_type=EntityType.PROJECT)
    task_reject = _decision(entity_type=EntityType.TASK, decision=EffortCalibrationDecision.REJECT)
    task_accept = _decision(entity_type=EntityType.TASK)

    # TASK first appears at position 0 via the REJECT record, so the
    # TASK factor must be emitted before the PROJECT factor even though
    # the TASK ACCEPT is supplied last.
    result = resolve_effective_effort_calibration_factors(
        [task_reject, project_accept, task_accept], PORTFOLIO_ID, PROJECT_ID
    )
    assert [factor.entity_type for factor in result.factors] == [
        EntityType.TASK,
        EntityType.PROJECT,
    ]

    # Different supplied history order yields a different (still
    # deterministic) output order.
    swapped = resolve_effective_effort_calibration_factors(
        [project_accept, task_reject, task_accept], PORTFOLIO_ID, PROJECT_ID
    )
    assert [factor.entity_type for factor in swapped.factors] == [
        EntityType.PROJECT,
        EntityType.TASK,
    ]


def test_source_history_remains_unchanged() -> None:
    records = [
        _decision(decided_at=datetime(2025, 7, 1, tzinfo=UTC)),
        _decision(
            decision=EffortCalibrationDecision.REJECT,
            decided_at=datetime(2025, 7, 2, tzinfo=UTC),
        ),
    ]
    before = [record.model_dump(mode="python") for record in records]
    resolve_effective_effort_calibration_factors(records, PORTFOLIO_ID, PROJECT_ID)
    after = [record.model_dump(mode="python") for record in records]
    assert before == after
    assert len(records) == 2


def test_repeated_equivalent_calls_yield_equivalent_immutable_results() -> None:
    records = [_decision(), _rejection()]
    first = resolve_effective_effort_calibration_factors(records, PORTFOLIO_ID, PROJECT_ID)
    second = resolve_effective_effort_calibration_factors(records, PORTFOLIO_ID, PROJECT_ID)
    assert first.factors == second.factors
    assert first.model_dump(mode="python") == second.model_dump(mode="python")
    with pytest.raises(ValidationError):
        first.factors = ()  # type: ignore[misc]


def test_pure_resolver_accepts_no_repository_or_reader() -> None:
    parameters = set(inspect.signature(resolve_effective_effort_calibration_factors).parameters)
    assert parameters == {"decisions", "portfolio_id", "project_id"}


def test_error_is_value_error_subclass() -> None:
    assert issubclass(EffectiveEffortCalibrationFactorError, ValueError)
