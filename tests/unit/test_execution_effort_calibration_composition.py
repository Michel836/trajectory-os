"""Unit tests for the V1.19 read-only resolution + application composition.

Pins the narrow application boundary that composes the EXISTING
authoritative boundaries:

* V1.17 durable read-only resolver (called exactly once per request,
  semantics owned by V1.17: latest-ACCEPT selection, REJECT/DEFER
  non-revocation);
* V1.18 pure exact-integer application (called exactly once on the
  AVAILABLE path only, exact candidate and exact selected factor passed
  unchanged, no local arithmetic or rounding in V1.19).

Also pins the strict candidate/scope guards (enforced even when the
requested factor is missing), the closed status vocabulary, the
immutable cross-field result invariants, the critical
NO_EFFECTIVE_FACTOR domain state (no identity ``1/1`` fallback, no
fabricated unchanged proposal, not an exception), exact entity-type-only
selection (no cross-type fallback or blending), and read-only behavior
(no repository writes, no repository access for invalid input).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import trajectory_os.application.execution_effort_calibration_composition as composition
from trajectory_os.application.execution_effort_calibration_composition import (
    EffectiveCalibrationApplicationError,
    EffectiveCalibrationApplicationResult,
    EffectiveCalibrationApplicationStatus,
    resolve_and_apply_effective_effort_calibration,
)
from trajectory_os.application.execution_effort_calibration_factor_decisions import (
    EffortCalibrationDecision,
    EffortCalibrationFactorDecision,
)
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactor,
    EffectiveEffortCalibrationFactorSet,
    resolve_effective_effort_calibration_factors,
)
from trajectory_os.domain.execution_effort_calibration_factor_application import (
    CalibratedEffortProposal,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
)

PORTFOLIO_ID = UUID("87878787-8787-4787-8787-878787878787")
PROJECT_ID = UUID("88888888-8888-4888-8888-888888888888")
DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
CANDIDATE = 300


@dataclass
class SpyDecisionRepository:
    """V1.16-shaped decision repository spy; ANY write attempt fails loud."""

    histories: dict[EntityType, tuple[EffortCalibrationFactorDecision, ...]]
    list_calls: list[tuple[UUID, UUID, EntityType]] = field(default_factory=list)
    list_history_impl: (
        Callable[[UUID, UUID, EntityType], tuple[EffortCalibrationFactorDecision, ...]] | None
    ) = None

    def list_history(
        self,
        portfolio_id: UUID,
        project_id: UUID,
        entity_type: EntityType,
    ) -> tuple[EffortCalibrationFactorDecision, ...]:
        if self.list_history_impl is not None:
            return self.list_history_impl(portfolio_id, project_id, entity_type)
        self.list_calls.append((portfolio_id, project_id, entity_type))
        return self.histories.get(entity_type, ())

    def add(self, decision: EffortCalibrationFactorDecision) -> None:
        raise AssertionError("V1.19 composition must never write through the repository")


def _accept(
    entity_type: EntityType,
    numerator: int = 3,
    denominator: int = 2,
    planned: int = 100,
    actual: int = 150,
    decided_at: datetime = DECIDED_AT,
) -> EffortCalibrationFactorDecision:
    """One valid V1.16 ACCEPT record over an AVAILABLE segment."""
    return EffortCalibrationFactorDecision(
        decision_id=uuid4(),
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=entity_type,
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=planned,
        total_actual_duration_seconds=actual,
        proposal_available=True,
        proposal_reason=EffortCalibrationFactorProposalReason.AVAILABLE,
        factor_numerator=numerator,
        factor_denominator=denominator,
        decision=EffortCalibrationDecision.ACCEPT,
        decided_at=decided_at,
    )


def _factor(
    *,
    entity_type: EntityType = EntityType.TASK,
    numerator: int = 3,
    denominator: int = 2,
    planned: int = 100,
    actual: int = 150,
) -> EffectiveEffortCalibrationFactor:
    return EffectiveEffortCalibrationFactor(
        entity_type=entity_type,
        decision_id=uuid4(),
        decided_at=DECIDED_AT,
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=planned,
        total_actual_duration_seconds=actual,
        factor_numerator=numerator,
        factor_denominator=denominator,
    )


def _set(*factors: EffectiveEffortCalibrationFactor) -> EffectiveEffortCalibrationFactorSet:
    return EffectiveEffortCalibrationFactorSet(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        factors=tuple(factors),
    )


# --- Status vocabulary -------------------------------------------------------


def test_status_vocabulary_is_closed_and_exact() -> None:
    assert set(EffectiveCalibrationApplicationStatus) == {
        EffectiveCalibrationApplicationStatus.AVAILABLE,
        EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR,
    }
    assert EffectiveCalibrationApplicationStatus.AVAILABLE == "available"
    assert EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR == "no_effective_factor"


def test_result_rejects_unknown_status_value() -> None:
    with pytest.raises(ValidationError):
        EffectiveCalibrationApplicationResult(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=0,
            status="identity_fallback",  # type: ignore[arg-type]
            proposal=None,
        )


# --- Result model strictness / cross-field invariants ------------------------


def test_result_model_is_frozen() -> None:
    result = EffectiveCalibrationApplicationResult(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=CANDIDATE,
        status=EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR,
        proposal=None,
    )
    with pytest.raises((TypeError, ValidationError)):
        result.status = EffectiveCalibrationApplicationStatus.AVAILABLE  # type: ignore[misc]


def test_result_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        EffectiveCalibrationApplicationResult(  # pyright: ignore[reportInvalidTypeArguments]
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR,
            proposal=None,
            calibrated_duration_seconds=450,
        )


def test_result_model_rejects_candidate_coercion() -> None:
    with pytest.raises(ValidationError):
        EffectiveCalibrationApplicationResult(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds="300",  # type: ignore[arg-type]
            status=EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR,
            proposal=None,
        )


def test_available_status_requires_a_proposal() -> None:
    with pytest.raises(ValidationError, match="AVAILABLE"):
        EffectiveCalibrationApplicationResult(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=EffectiveCalibrationApplicationStatus.AVAILABLE,
            proposal=None,
        )


def test_no_effective_factor_status_forbids_a_proposal() -> None:
    task_factor = _factor(entity_type=EntityType.TASK)
    proposal = _valid_proposal_for(EntityType.TASK, CANDIDATE, task_factor)
    with pytest.raises(ValidationError, match="NO_EFFECTIVE_FACTOR"):
        EffectiveCalibrationApplicationResult(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR,
            proposal=proposal,
        )


def _valid_proposal_for(
    entity_type: EntityType, candidate: int, factor: EffectiveEffortCalibrationFactor
) -> CalibratedEffortProposal:
    from trajectory_os.domain.execution_effort_calibration_factor_application import (
        apply_effective_effort_calibration_factor,
    )

    return apply_effective_effort_calibration_factor(
        candidate, _renamed_factor(entity_type, factor)
    )


def _renamed_factor(
    entity_type: EntityType, factor: EffectiveEffortCalibrationFactor
) -> EffectiveEffortCalibrationFactor:
    return factor.model_copy(update={"entity_type": entity_type})


def test_proposal_entity_type_must_match_requested_type() -> None:
    other_factor = _factor(entity_type=EntityType.PROJECT)
    proposal = _valid_proposal_for(EntityType.PROJECT, CANDIDATE, other_factor)
    with pytest.raises(ValidationError, match="entity type"):
        EffectiveCalibrationApplicationResult(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=EffectiveCalibrationApplicationStatus.AVAILABLE,
            proposal=proposal,
        )


def test_proposal_candidate_must_match_requested_candidate() -> None:
    task_factor = _factor(entity_type=EntityType.TASK)
    proposal = _valid_proposal_for(EntityType.TASK, 120, task_factor)
    with pytest.raises(ValidationError, match="candidate"):
        EffectiveCalibrationApplicationResult(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=EffectiveCalibrationApplicationStatus.AVAILABLE,
            proposal=proposal,
        )


def test_hostile_constructed_result_bypass_is_defeated() -> None:
    result = EffectiveCalibrationApplicationResult.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=CANDIDATE,
        status=EffectiveCalibrationApplicationStatus.AVAILABLE,
        proposal=None,
    )
    with pytest.raises(ValidationError, match="AVAILABLE"):
        EffectiveCalibrationApplicationResult.model_validate(
            result.model_dump(mode="python"), strict=True
        )


# --- Input strictness --------------------------------------------------------


@pytest.mark.parametrize(
    "bad_candidate",
    [True, False, 1.5, Decimal("100"), "300", 300.0, None, [300]],
    ids=["true", "false", "float", "decimal", "str", "py-float", "none", "list"],
)
def test_invalid_candidate_rejected_even_when_no_factor_exists(bad_candidate: object) -> None:
    spy = SpyDecisionRepository(histories={})

    def explode(*_args: object) -> tuple[EffortCalibrationFactorDecision, ...]:
        raise AssertionError("repository must not be touched for an invalid candidate")

    spy.list_history_impl = explode

    with pytest.raises(EffectiveCalibrationApplicationError):
        resolve_and_apply_effective_effort_calibration(
            PORTFOLIO_ID,
            PROJECT_ID,
            EntityType.TASK,
            bad_candidate,
            spy,
        )
    assert spy.list_calls == []


def test_negative_candidate_rejected_even_when_no_factor_exists() -> None:
    spy = SpyDecisionRepository(histories={})

    def explode(*_args: object) -> tuple[EffortCalibrationFactorDecision, ...]:
        raise AssertionError("repository must not be touched for a negative candidate")

    spy.list_history_impl = explode

    with pytest.raises(EffectiveCalibrationApplicationError, match=">= 0"):
        resolve_and_apply_effective_effort_calibration(
            PORTFOLIO_ID,
            PROJECT_ID,
            EntityType.TASK,
            -1,
            spy,
        )
    assert spy.list_calls == []


def test_invalid_candidate_rejected_even_when_factor_is_available() -> None:
    spy = SpyDecisionRepository(histories={EntityType.TASK: (_accept(EntityType.TASK),)})

    with pytest.raises(EffectiveCalibrationApplicationError):
        resolve_and_apply_effective_effort_calibration(
            PORTFOLIO_ID,
            PROJECT_ID,
            EntityType.TASK,
            True,
            spy,
        )


@pytest.mark.parametrize(
    ("bad_portfolio", "bad_project", "bad_entity"),
    [
        (str(PORTFOLIO_ID), PROJECT_ID, EntityType.TASK),
        (PORTFOLIO_ID, str(PROJECT_ID), EntityType.TASK),
        (None, PROJECT_ID, EntityType.TASK),
        (PORTFOLIO_ID, None, EntityType.TASK),
        (PORTFOLIO_ID, PROJECT_ID, "task"),
        (PORTFOLIO_ID, PROJECT_ID, None),
    ],
    ids=[
        "portfolio-str",
        "project-str",
        "portfolio-none",
        "project-none",
        "entity-str",
        "entity-none",
    ],
)
def test_hostile_scope_rejected_before_any_repository_access(
    bad_portfolio: object,
    bad_project: object,
    bad_entity: object,
) -> None:
    spy = SpyDecisionRepository(histories={})

    def explode(*_args: object) -> tuple[EffortCalibrationFactorDecision, ...]:
        raise AssertionError("repository must not be touched for hostile scope")

    spy.list_history_impl = explode

    with pytest.raises(EffectiveCalibrationApplicationError):
        resolve_and_apply_effective_effort_calibration(
            bad_portfolio, bad_project, bad_entity, CANDIDATE, spy
        )
    assert spy.list_calls == []


def test_repository_failure_propagates_unchanged() -> None:
    spy = SpyDecisionRepository(histories={})

    def boom(*_args: object) -> tuple[EffortCalibrationFactorDecision, ...]:
        raise RuntimeError("repository boom")

    spy.list_history_impl = boom

    with pytest.raises(RuntimeError, match="repository boom"):
        resolve_and_apply_effective_effort_calibration(
            PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, CANDIDATE, spy
        )


# --- AVAILABLE path ----------------------------------------------------------


def test_available_path_returns_exact_v118_proposal() -> None:
    spy = SpyDecisionRepository(histories={EntityType.TASK: (_accept(EntityType.TASK),)})
    result = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, CANDIDATE, spy
    )

    assert result.status is EffectiveCalibrationApplicationStatus.AVAILABLE
    assert result.proposal is not None
    # 300 * 3/2 = 450, exact.
    assert result.proposal.calibrated_duration_seconds == 450
    assert result.proposal.entity_type is EntityType.TASK
    assert result.proposal.candidate_duration_seconds == CANDIDATE


def test_available_path_retains_exact_scope_and_candidate() -> None:
    spy = SpyDecisionRepository(histories={EntityType.TASK: (_accept(EntityType.TASK),)})
    result = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, 777, spy
    )
    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_id == PROJECT_ID
    assert result.entity_type is EntityType.TASK
    assert result.candidate_duration_seconds == 777
    assert type(result.candidate_duration_seconds) is int


def test_zero_candidate_is_valid_on_available_path() -> None:
    spy = SpyDecisionRepository(histories={EntityType.TASK: (_accept(EntityType.TASK),)})
    result = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, 0, spy
    )
    assert result.status is EffectiveCalibrationApplicationStatus.AVAILABLE
    assert result.proposal is not None
    assert result.proposal.calibrated_duration_seconds == 0


def test_half_up_rounding_is_owned_by_v118() -> None:
    # candidate 1 with factor 1/2: 1 * 1/2 = 0.5 -> half-up -> 1,
    # banker's rounding would give 0. This must come from V1.18 untouched.
    spy = SpyDecisionRepository(
        histories={EntityType.TASK: (_accept(EntityType.TASK, 1, 2, 100, 50),)}
    )
    result = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, 1, spy
    )
    assert result.proposal is not None
    assert result.proposal.rounded_up is True
    assert result.proposal.calibrated_duration_seconds == 1


# --- NO_EFFECTIVE_FACTOR path ------------------------------------------------


def test_missing_factor_returns_explicit_status_not_exception() -> None:
    spy = SpyDecisionRepository(histories={})
    result = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, CANDIDATE, spy
    )
    assert result.status is EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR
    assert result.proposal is None
    assert result.entity_type is EntityType.TASK


def test_missing_factor_does_not_fabricate_identity_or_proposal() -> None:
    spy = SpyDecisionRepository(histories={})
    result = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, CANDIDATE, spy
    )
    # Not a silent 1/1: no proposal of ANY kind is fabricated, and the
    # result is explicitly NOT the AVAILABLE state.
    assert result.proposal is None
    assert result.status is EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR
    assert result.candidate_duration_seconds == CANDIDATE


def test_zero_candidate_is_valid_on_no_factor_path() -> None:
    spy = SpyDecisionRepository(histories={})
    result = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, 0, spy
    )
    assert result.status is EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR
    assert result.proposal is None
    assert result.candidate_duration_seconds == 0


def test_defer_or_reject_only_history_is_no_effective_factor() -> None:
    defer = _accept(EntityType.TASK).model_copy(
        update={"decision": EffortCalibrationDecision.DEFER}
    )
    reject = _accept(EntityType.TASK).model_copy(
        update={"decision": EffortCalibrationDecision.REJECT}
    )
    spy = SpyDecisionRepository(histories={EntityType.TASK: (defer, reject)})
    result = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, CANDIDATE, spy
    )
    assert result.status is EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR
    assert result.proposal is None


# --- Exact-entity-type-only selection ----------------------------------------


def test_other_type_factor_does_not_count_as_requested_type() -> None:
    spy = SpyDecisionRepository(histories={EntityType.PROJECT: (_accept(EntityType.PROJECT),)})
    result = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, CANDIDATE, spy
    )
    assert result.status is EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR
    assert result.proposal is None


def test_exact_requested_type_is_selected_when_several_types_have_factors() -> None:
    task_factor = _accept(EntityType.TASK, numerator=3, denominator=2, planned=100, actual=150)
    goal_factor = _accept(EntityType.GOAL, numerator=2, denominator=1, planned=100, actual=200)
    spy = SpyDecisionRepository(
        histories={
            EntityType.TASK: (task_factor,),
            EntityType.GOAL: (goal_factor,),
        }
    )
    result = resolve_or_fail_available(PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, CANDIDATE, spy)
    # Selected factor is the TASK 3/2 factor (300 * 3/2 = 450), NOT the
    # GOAL 2/1 factor (300 * 2/1 = 600) and NOT a blend of the two.
    assert result.proposal is not None
    assert result.proposal.entity_type is EntityType.TASK
    assert result.proposal.factor_numerator == 3
    assert result.proposal.factor_denominator == 2
    assert result.proposal.calibrated_duration_seconds == 450


def _resolve(spy: SpyDecisionRepository) -> EffectiveCalibrationApplicationResult:
    return resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK, CANDIDATE, spy
    )


def resolve_or_fail_available(
    portfolio_id: UUID,
    project_id: UUID,
    entity_type: EntityType,
    candidate: int,
    spy: SpyDecisionRepository,
) -> EffectiveCalibrationApplicationResult:
    result = resolve_and_apply_effective_effort_calibration(
        portfolio_id, project_id, entity_type, candidate, spy
    )
    assert result.status is EffectiveCalibrationApplicationStatus.AVAILABLE
    return result


def test_no_cross_type_fallback_or_blending_with_all_types_present() -> None:
    # TASK: 2/1  -> 300 * 2/1 = 600
    # PROJECT: 1/1 -> 300 * 1/1 = 300
    # GOAL: 3/1 -> 300 * 3/1 = 900
    histories = {
        EntityType.TASK: (
            _accept(EntityType.TASK, numerator=2, denominator=1, planned=100, actual=200),
        ),
        EntityType.PROJECT: (
            _accept(EntityType.PROJECT, numerator=1, denominator=1, planned=100, actual=100),
        ),
        EntityType.GOAL: (
            _accept(EntityType.GOAL, numerator=3, denominator=1, planned=100, actual=300),
        ),
    }
    spy = SpyDecisionRepository(histories=histories)
    result = _resolve(spy)
    # Only the TASK factor (2/1) may be used: 300 * 2 = 600. Any
    # fallback to PROJECT (1/1) or GOAL (3/1) or a blend would differ.
    assert result.proposal is not None
    assert result.proposal.entity_type is EntityType.TASK
    assert result.proposal.factor_numerator == 2
    assert result.proposal.factor_denominator == 1
    assert result.proposal.calibrated_duration_seconds == 600


# --- V1.17 semantics remain owned by V1.17 -----------------------------------


def test_later_accept_wins_over_earlier_accept() -> None:
    from datetime import datetime

    earlier = _accept(
        EntityType.TASK,
        numerator=1,
        denominator=1,
        planned=100,
        actual=100,
        decided_at=datetime(2025, 6, 1, 8, 30, tzinfo=UTC),
    )
    later = _accept(
        EntityType.TASK,
        numerator=3,
        denominator=2,
        planned=100,
        actual=150,
        decided_at=datetime(2025, 7, 1, 8, 30, tzinfo=UTC),
    )
    spy = SpyDecisionRepository(histories={EntityType.TASK: (earlier, later)})
    result = _resolve(spy)
    assert result.proposal is not None
    assert result.proposal.decision_id == later.decision_id
    assert result.proposal.factor_numerator == 3


def test_later_reject_does_not_revoke_earlier_accept() -> None:
    from datetime import datetime

    earlier = _accept(
        EntityType.TASK,
        decided_at=datetime(2025, 6, 1, 8, 30, tzinfo=UTC),
    )
    later_reject = _accept(
        EntityType.TASK,
        decided_at=datetime(2025, 7, 1, 8, 30, tzinfo=UTC),
    ).model_copy(update={"decision": EffortCalibrationDecision.REJECT})
    spy = SpyDecisionRepository(histories={EntityType.TASK: (earlier, later_reject)})
    result = _resolve(spy)
    assert result.status is EffectiveCalibrationApplicationStatus.AVAILABLE
    assert result.proposal is not None
    assert result.proposal.decision_id == earlier.decision_id


# --- Delegation integrity ----------------------------------------------------


def test_v117_durable_resolver_called_exactly_once_on_available_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = SpyDecisionRepository(histories={EntityType.TASK: (_accept(EntityType.TASK),)})
    real_set = composition.resolve_effective_effort_calibration_factors_durably(
        PORTFOLIO_ID, PROJECT_ID, base
    )
    calls: list[tuple[object, object, object]] = []

    def spy_resolver(
        portfolio_id: object, project_id: object, decision_repository: object
    ) -> EffectiveEffortCalibrationFactorSet:
        calls.append((portfolio_id, project_id, decision_repository))
        return real_set

    monkeypatch.setattr(
        composition,
        "resolve_effective_effort_calibration_factors_durably",
        spy_resolver,
    )

    spy = SpyDecisionRepository(histories={EntityType.TASK: (_accept(EntityType.TASK),)})
    result = _resolve(spy)

    assert len(calls) == 1
    assert calls[0][0] == PORTFOLIO_ID
    assert calls[0][1] == PROJECT_ID
    assert calls[0][2] is spy  # exact repository instance passed through
    assert result.proposal is not None


def test_v117_durable_resolver_called_exactly_once_on_missing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_set = _set()
    calls = []

    def spy_resolver(
        portfolio_id: object, project_id: object, decision_repository: object
    ) -> EffectiveEffortCalibrationFactorSet:
        calls.append((1,))
        return real_set

    monkeypatch.setattr(
        composition,
        "resolve_effective_effort_calibration_factors_durably",
        spy_resolver,
    )

    spy = SpyDecisionRepository(histories={})
    result = _resolve(spy)

    assert len(calls) == 1  # resolver is invoked even though no factor exists
    assert result.status is EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR


def test_v118_application_called_exactly_once_with_exact_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_accept = _accept(EntityType.TASK, numerator=3, denominator=2, planned=100, actual=150)
    # The exact factor V1.17 will select for this history.
    expected_set = resolve_effective_effort_calibration_factors(
        [task_accept], PORTFOLIO_ID, PROJECT_ID
    )
    expected_factor = expected_set.factors[0]

    calls: list[tuple[object, object]] = []
    real_apply = composition.apply_effective_effort_calibration_factor

    def spy_apply(candidate: object, factor: object) -> CalibratedEffortProposal:
        calls.append((candidate, factor))
        return real_apply(candidate, factor)  # type: ignore[arg-type]

    monkeypatch.setattr(composition, "apply_effective_effort_calibration_factor", spy_apply)

    spy = SpyDecisionRepository(histories={EntityType.TASK: (task_accept,)})
    result = _resolve(spy)

    assert len(calls) == 1
    candidate_arg, factor_arg = calls[0]
    assert candidate_arg == CANDIDATE  # exact candidate value
    assert type(candidate_arg) is int  # ... without any coercion/normalization
    assert isinstance(factor_arg, EffectiveEffortCalibrationFactor)
    assert factor_arg.model_dump(mode="python") == expected_factor.model_dump(
        mode="python"
    )  # exact V1.17 factor, unchanged
    assert factor_arg.entity_type is EntityType.TASK

    # The AVAILABLE result carries EXACTLY the V1.18 object the spy
    # returned: no local recompute, copy, or re-derivation.
    assert result.proposal is not None
    assert result.proposal.calibrated_duration_seconds == 450


def test_v118_application_not_called_on_missing_factor_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def spy_apply(candidate: object, factor: object) -> CalibratedEffortProposal:
        calls.append((1,))
        raise AssertionError("V1.18 must not run when no factor exists")

    monkeypatch.setattr(composition, "apply_effective_effort_calibration_factor", spy_apply)
    spy = SpyDecisionRepository(histories={EntityType.PROJECT: (_accept(EntityType.PROJECT),)})
    result = _resolve(spy)  # requested TASK, only PROJECT factor exists
    assert calls == []
    assert result.status is EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR
    assert result.proposal is None


def test_v118_failure_propagates_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(candidate: object, factor: object) -> CalibratedEffortProposal:
        raise RuntimeError("v1.18 boundary boom")

    monkeypatch.setattr(composition, "apply_effective_effort_calibration_factor", boom)
    spy = SpyDecisionRepository(histories={EntityType.TASK: (_accept(EntityType.TASK),)})
    with pytest.raises(RuntimeError, match="v1.18 boundary boom"):
        _resolve(spy)


def test_v119_performs_no_local_factor_arithmetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper must carry EXACTLY the proposal V1.18 returned.

    A marked (but valid) V1.18 proposal is returned by the spied boundary;
    the wrapper result must contain that exact object. Any local
    multiplication/divmod/rounding would replace it and fail the identity
    assertion.
    """
    real_set = _set(_factor(entity_type=EntityType.TASK, numerator=3, denominator=2))
    monkeypatch.setattr(
        composition,
        "resolve_effective_effort_calibration_factors_durably",
        lambda portfolio_id, project_id, decision_repository: real_set,
    )

    marked_factor = _factor(entity_type=EntityType.TASK, numerator=3, denominator=2)
    marked = _valid_proposal_for(EntityType.TASK, CANDIDATE, marked_factor)

    def spy_apply(candidate: object, factor: object) -> CalibratedEffortProposal:
        return marked

    monkeypatch.setattr(composition, "apply_effective_effort_calibration_factor", spy_apply)

    spy = SpyDecisionRepository(histories={EntityType.TASK: (_accept(EntityType.TASK),)})
    result = _resolve(spy)
    assert result.proposal is not None
    assert result.proposal is marked  # exact V1.18 object, no re-derivation


# --- Read-only / non-mutation ------------------------------------------------


def test_no_repository_writes_on_available_path() -> None:
    # SpyDecisionRepository.add raises AssertionError if ever invoked.
    spy = SpyDecisionRepository(histories={EntityType.TASK: (_accept(EntityType.TASK),)})
    result = _resolve(spy)
    assert result.status is EffectiveCalibrationApplicationStatus.AVAILABLE
    assert len(spy.list_calls) == len(list(EntityType))


def test_no_repository_writes_on_missing_factor_path() -> None:
    spy = SpyDecisionRepository(histories={})
    result = _resolve(spy)
    assert result.status is EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR
    assert len(spy.list_calls) == len(list(EntityType))


def test_repository_read_calls_use_exact_scope() -> None:
    spy = SpyDecisionRepository(histories={EntityType.TASK: (_accept(EntityType.TASK),)})
    _resolve(spy)
    assert all(
        (portfolio_id, project_id) == (PORTFOLIO_ID, PROJECT_ID)
        for portfolio_id, project_id, _ in spy.list_calls
    )


def test_input_factor_is_not_mutated_by_the_composition() -> None:
    task_accept = _accept(EntityType.TASK)
    before = task_accept.model_dump(mode="python")
    spy = SpyDecisionRepository(histories={EntityType.TASK: (task_accept,)})
    _resolve(spy)
    assert task_accept.model_dump(mode="python") == before


# --- Determinism -------------------------------------------------------------


def test_repeated_equivalent_calls_return_equivalent_results() -> None:
    # One fixed equivalent repository history: the spy is read-only, so
    # every call observes exactly the same persisted state.
    spy = SpyDecisionRepository(
        histories={
            EntityType.TASK: (_accept(EntityType.TASK),),
            EntityType.PROJECT: (
                _accept(
                    EntityType.PROJECT,
                    numerator=1,
                    denominator=1,
                    planned=100,
                    actual=100,
                ),
            ),
        }
    )

    first = _resolve(spy)
    second = _resolve(spy)
    assert first.model_dump(mode="python") == second.model_dump(mode="python")
    assert first == second

    missing_first = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.GOAL, CANDIDATE, spy
    )
    missing_second = resolve_and_apply_effective_effort_calibration(
        PORTFOLIO_ID, PROJECT_ID, EntityType.GOAL, CANDIDATE, spy
    )
    assert missing_first.model_dump(mode="python") == missing_second.model_dump(mode="python")


def test_no_wall_clock_or_uuid_generation_is_required() -> None:
    from datetime import UTC, datetime, timedelta

    # A single fixed history drives every call; the composition returns
    # equivalent results on each pass, proving no clock read or generated
    # identity participates in the outcome.
    history = (
        _accept(
            EntityType.TASK,
            decided_at=datetime(2025, 7, 1, 8, 30, tzinfo=UTC),
        ),
    )
    spy = SpyDecisionRepository(histories={EntityType.TASK: history})
    outcomes = [_resolve(spy).model_dump(mode="python") for _ in range(3)]
    first = outcomes[0]
    assert all(outcome == first for outcome in outcomes)
    # The proposed evidence is exactly the fixed decided_at from history;
    # no later instant (no clock) is ever introduced.
    assert first["proposal"]["decided_at"] == (
        datetime(2025, 7, 1, 8, 30, tzinfo=UTC) + timedelta(0)
    )


def test_error_is_value_error_subclass() -> None:
    assert issubclass(EffectiveCalibrationApplicationError, ValueError)


def test_result_rejects_hostile_nested_v118_proposal() -> None:
    """A model_construct V1.18 proposal must not bypass V1.19 integrity."""
    valid_factor = _factor(
        entity_type=EntityType.TASK,
        numerator=3,
        denominator=2,
    )
    valid = _valid_proposal_for(
        EntityType.TASK,
        CANDIDATE,
        valid_factor,
    )

    hostile = CalibratedEffortProposal.model_construct(
        **{
            **valid.model_dump(mode="python"),
            # Scope still matches V1.19, but the V1.18 arithmetic evidence
            # is deliberately impossible.
            "scaled_numerator": valid.scaled_numerator + 1,
        }
    )

    with pytest.raises(ValidationError, match="scaled_numerator"):
        EffectiveCalibrationApplicationResult(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=EffectiveCalibrationApplicationStatus.AVAILABLE,
            proposal=hostile,
        )
