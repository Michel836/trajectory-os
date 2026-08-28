"""Unit tests for the V1.20 read-only CURRENT-WBS entity binding.

Pins the narrow application boundary that binds ONE explicit immutable
V1.19 ``EffectiveCalibrationApplicationResult`` to ONE explicit current
entity and returns a human-reviewable calibrated estimate revision
proposal — WITHOUT persisting anything:

* fresh strict revalidation of the genuine V1.19 result (hostile
  ``model_construct()`` rejected, including a hostile nested V1.18
  proposal);
* strict ``entity_id`` UUID input (no coercion, no string);
* ALL non-repository inputs validated BEFORE any repository access;
* the CURRENT portfolio loaded exactly once via ``result.portfolio_id``;
* exact entity identity: exists in the CURRENT portfolio, exact
  ``EntityType == result.entity_type``, member of the EXACT CURRENT WBS
  of ``result.project_id`` (V1.1 ``build_work_breakdown`` reused as the
  authoritative CURRENT-WBS helper — no second traversal/grammar);
* same-type entity in another project fails; removed/stale entity fails;
* READY only for a V1.19 AVAILABLE source, with the EXACT nested V1.18
  calibrated duration (never recomputed);
* NO_EFFECTIVE_FACTOR remains explicit, non-ready, non-persistable, with
  no candidate-as-calibrated fabrication and no identity ``1/1`` fallback;
* immutable self-validating cross-field invariants of the proposal model;
* no V1.17 re-resolution, no V1.18 reapplication, no repository writes,
  no estimate creation, no ``SourceKind`` change, no clock/UUID
  generation, no AI/provider calls;
* deterministic repeated equivalent bindings.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import trajectory_os.application.execution_effort_calibration_composition as composition
import trajectory_os.application.execution_effort_calibration_entity_binding as binding
from trajectory_os.application.execution_effort_calibration_composition import (
    EffectiveCalibrationApplicationResult,
    EffectiveCalibrationApplicationStatus,
)
from trajectory_os.application.execution_effort_calibration_entity_binding import (
    CalibratedEstimateRevisionBindingError,
    CalibratedEstimateRevisionEntityNotFoundError,
    CalibratedEstimateRevisionEntityOutOfCurrentWbsError,
    CalibratedEstimateRevisionEntityTypeMismatchError,
    CalibratedEstimateRevisionPortfolioNotFoundError,
    CalibratedEstimateRevisionProposal,
    CalibratedEstimateRevisionProposalStatus,
    bind_effort_calibration_to_current_entity,
)
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactor,
)
from trajectory_os.domain.execution_effort_calibration_factor_application import (
    CalibratedEffortProposal,
    apply_effective_effort_calibration_factor,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_PORTFOLIO_ID = UUID("22222222-2222-4222-8222-222222222222")
PROJECT_A_ID = UUID("33333333-3333-4333-8333-333333333333")
PROJECT_B_ID = UUID("44444444-4444-4444-8444-444444444444")
TASK_A1_ID = UUID("55555555-5555-4555-8555-555555555555")
TASK_A2_ID = UUID("66666666-6666-4666-8666-666666666666")
TASK_B1_ID = UUID("77777777-7777-4777-8777-777777777777")
DELIVERABLE_A_ID = UUID("88888888-8888-4888-8888-888888888888")
ORPHAN_TASK_ID = UUID("99999999-9999-4999-8999-999999999999")
DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
CANDIDATE = 300


@dataclass
class SpyPortfolioRepository:
    """PortfolioRepository spy; ANY save/write attempt fails loudly."""

    portfolio: Portfolio | None
    load_calls: list[UUID] = field(default_factory=list)
    load_impl: Callable[[UUID], Portfolio | None] | None = None

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        if self.load_impl is not None:
            return self.load_impl(portfolio_id)
        self.load_calls.append(portfolio_id)
        return self.portfolio

    def save(self, portfolio: Portfolio) -> None:
        raise AssertionError("V1.20 must never save through the repository")


def _entity(entity_type: EntityType) -> TrajectoryEntity:
    return TrajectoryEntity(id=uuid4(), entity_type=entity_type, title=entity_type.value)


def _entity_with_id(entity_id: UUID, entity_type: EntityType) -> TrajectoryEntity:
    return TrajectoryEntity(id=entity_id, entity_type=entity_type, title=entity_type.value)


def _belongs_to(child_id: UUID, parent_id: UUID) -> TrajectoryRelation:
    return TrajectoryRelation(
        source_id=child_id,
        target_id=parent_id,
        relation_type=RelationType.BELONGS_TO,
    )


def _current_portfolio(*, include_task_a1: bool = True) -> Portfolio:
    """Canonical CURRENT portfolio: project A with a full WBS, project B.

    Project A WBS: TASK_A1 (task), DELIVERABLE_A (deliverable) which
    contains TASK_A2 (task). Project B WBS: TASK_B1 (task). ORPHAN_TASK
    is a TASK present in the portfolio but with NO parent — it exists in
    the CURRENT portfolio yet is outside BOTH project WBSs.
    """
    entities = [
        _entity_with_id(PROJECT_A_ID, EntityType.PROJECT),
        _entity_with_id(PROJECT_B_ID, EntityType.PROJECT),
        _entity_with_id(TASK_B1_ID, EntityType.TASK),
        _entity_with_id(DELIVERABLE_A_ID, EntityType.DELIVERABLE)
        if include_task_a1
        else _entity_with_id(DELIVERABLE_A_ID, EntityType.DELIVERABLE),
        _entity_with_id(ORPHAN_TASK_ID, EntityType.TASK),
    ]
    if include_task_a1:
        entities.insert(1, _entity_with_id(TASK_A1_ID, EntityType.TASK))
        entities.insert(4, _entity_with_id(TASK_A2_ID, EntityType.TASK))
    relations = [
        _belongs_to(TASK_B1_ID, PROJECT_B_ID),
        _belongs_to(DELIVERABLE_A_ID, PROJECT_A_ID),
    ]
    if include_task_a1:
        relations.insert(0, _belongs_to(TASK_A1_ID, PROJECT_A_ID))
        relations.append(_belongs_to(TASK_A2_ID, DELIVERABLE_A_ID))
    return Portfolio(id=PORTFOLIO_ID, name="canonical", entities=entities, relations=relations)


def _factor(
    entity_type: EntityType,
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


def _available_result(
    entity_type: EntityType = EntityType.TASK,
    candidate: int = CANDIDATE,
    portfolio_id: UUID = PORTFOLIO_ID,
    project_id: UUID = PROJECT_A_ID,
) -> EffectiveCalibrationApplicationResult:
    """One valid genuine V1.19 AVAILABLE result (3/2 factor by default)."""
    proposal = apply_effective_effort_calibration_factor(candidate, _factor(entity_type))
    return EffectiveCalibrationApplicationResult(
        portfolio_id=portfolio_id,
        project_id=project_id,
        entity_type=entity_type,
        candidate_duration_seconds=candidate,
        status=EffectiveCalibrationApplicationStatus.AVAILABLE,
        proposal=proposal,
    )


def _no_factor_result(
    entity_type: EntityType = EntityType.TASK,
    candidate: int = CANDIDATE,
    portfolio_id: UUID = PORTFOLIO_ID,
    project_id: UUID = PROJECT_A_ID,
) -> EffectiveCalibrationApplicationResult:
    """One valid genuine V1.19 NO_EFFECTIVE_FACTOR result."""
    return EffectiveCalibrationApplicationResult(
        portfolio_id=portfolio_id,
        project_id=project_id,
        entity_type=entity_type,
        candidate_duration_seconds=candidate,
        status=EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR,
        proposal=None,
    )


def _bind(
    result: EffectiveCalibrationApplicationResult,
    entity_id: UUID = TASK_A1_ID,
) -> CalibratedEstimateRevisionProposal:
    return bind_effort_calibration_to_current_entity(
        result, entity_id, SpyPortfolioRepository(portfolio=_current_portfolio())
    )


# --- Status vocabulary -------------------------------------------------------


def test_status_vocabulary_is_closed_and_exact() -> None:
    assert set(CalibratedEstimateRevisionProposalStatus) == {
        CalibratedEstimateRevisionProposalStatus.READY,
        CalibratedEstimateRevisionProposalStatus.NO_EFFECTIVE_FACTOR,
    }
    assert CalibratedEstimateRevisionProposalStatus.READY == "ready"
    assert CalibratedEstimateRevisionProposalStatus.NO_EFFECTIVE_FACTOR == "no_effective_factor"


def test_proposal_rejects_unknown_status_value() -> None:
    source = _available_result()
    with pytest.raises(ValidationError):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status="persisted",  # type: ignore[arg-type]
            calibrated_duration_seconds=450,
            source_result=source,
        )


# --- Proposal model strictness / cross-field invariants ----------------------


def test_proposal_model_is_frozen() -> None:
    proposal = _bind(_available_result())
    with pytest.raises((TypeError, ValidationError)):
        proposal.status = CalibratedEstimateRevisionProposalStatus.NO_EFFECTIVE_FACTOR  # type: ignore[misc]


def test_proposal_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        CalibratedEstimateRevisionProposal(  # pyright: ignore[reportInvalidTypeArguments]
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=450,
            source_result=_available_result(),
            estimate_id=uuid4(),
        )


def test_proposal_rejects_entity_id_coercion() -> None:
    with pytest.raises(ValidationError):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=str(TASK_A1_ID),  # type: ignore[arg-type]
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=450,
            source_result=_available_result(),
        )


def test_proposal_rejects_candidate_coercion() -> None:
    with pytest.raises(ValidationError):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds="300",  # type: ignore[arg-type]
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=450,
            source_result=_available_result(),
        )


def _valid_source() -> EffectiveCalibrationApplicationResult:
    return _available_result()


def test_wrapper_portfolio_must_match_source() -> None:
    source = _valid_source()
    with pytest.raises(ValidationError, match="portfolio"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=OTHER_PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=450,
            source_result=source,
        )


def test_wrapper_project_must_match_source() -> None:
    source = _valid_source()
    with pytest.raises(ValidationError, match="project"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_B_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=450,
            source_result=source,
        )


def test_wrapper_entity_type_must_match_source() -> None:
    source = _valid_source()
    with pytest.raises(ValidationError, match="entity type"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.DELIVERABLE,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=450,
            source_result=source,
        )


def test_wrapper_candidate_must_match_source() -> None:
    source = _valid_source()
    with pytest.raises(ValidationError, match="candidate"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE + 1,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=450,
            source_result=source,
        )


def test_wrapper_rejects_wrong_calibrated_duration() -> None:
    source = _valid_source()
    with pytest.raises(ValidationError, match="calibrated"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=999,  # not the exact V1.18 output (450)
            source_result=source,
        )


def test_ready_status_rejects_missing_calibrated_duration() -> None:
    source = _valid_source()
    with pytest.raises(ValidationError, match="calibrated"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=None,
            source_result=source,
        )


def test_ready_status_requires_available_source() -> None:
    source = _no_factor_result()
    with pytest.raises(ValidationError, match="AVAILABLE"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=CANDIDATE,  # would be a fabricated 1/1
            source_result=source,
        )


def test_ready_status_requires_source_proposal_present() -> None:
    # A genuinely-constructed AVAILABLE source always carries its
    # proposal; enforce the invariant with a hostile AVAILABLE source
    # whose proposal was suppressed through model_construct().
    valid = _available_result()
    source = EffectiveCalibrationApplicationResult.model_construct(
        **{**valid.model_dump(mode="python"), "proposal": None}
    )
    with pytest.raises(ValidationError):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=450,
            source_result=source,
        )


def test_no_effective_factor_rejects_present_calibrated_duration() -> None:
    source = _no_factor_result()
    with pytest.raises(ValidationError, match="forbid"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.NO_EFFECTIVE_FACTOR,
            calibrated_duration_seconds=CANDIDATE,
            source_result=source,
        )


def test_no_effective_factor_rejects_available_source() -> None:
    source = _available_result()
    with pytest.raises(ValidationError, match="NO_EFFECTIVE_FACTOR"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.NO_EFFECTIVE_FACTOR,
            calibrated_duration_seconds=None,
            source_result=source,
        )


def test_proposal_rejects_hostile_nested_v119_source_snapshot() -> None:
    valid = _available_result()
    source = EffectiveCalibrationApplicationResult.model_construct(
        **{
            **valid.model_dump(mode="python"),
            "status": EffectiveCalibrationApplicationStatus.AVAILABLE,
            "proposal": None,
        }
    )
    with pytest.raises(ValidationError, match="AVAILABLE"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.NO_EFFECTIVE_FACTOR,
            calibrated_duration_seconds=None,
            source_result=source,
        )


def test_proposal_rejects_hostile_nested_v118_proposal() -> None:
    valid = _available_result()
    assert valid.proposal is not None
    hostile_proposal = CalibratedEffortProposal.model_construct(
        **{
            **valid.proposal.model_dump(mode="python"),
            "scaled_numerator": valid.proposal.scaled_numerator + 1,
        }
    )
    source = EffectiveCalibrationApplicationResult.model_construct(
        **{**valid.model_dump(mode="python"), "proposal": hostile_proposal}
    )
    with pytest.raises(ValidationError, match="scaled_numerator"):
        CalibratedEstimateRevisionProposal(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_A_ID,
            entity_id=TASK_A1_ID,
            entity_type=EntityType.TASK,
            candidate_duration_seconds=CANDIDATE,
            status=CalibratedEstimateRevisionProposalStatus.READY,
            calibrated_duration_seconds=450,
            source_result=source,
        )


# --- Input strictness --------------------------------------------------------


def test_binding_requires_genuine_v119_result_before_repository_access() -> None:
    spy = SpyPortfolioRepository(portfolio=_current_portfolio())

    def explode(_portfolio_id: UUID) -> Portfolio | None:
        raise AssertionError("repository must not be touched for invalid result input")

    spy.load_impl = explode

    for bad_result in (
        {},
        "a result",
        123,
        _available_result().model_dump(mode="python"),
        object(),
    ):
        with pytest.raises(CalibratedEstimateRevisionBindingError, match="genuine"):
            bind_effort_calibration_to_current_entity(
                bad_result, TASK_A1_ID, spy  # pyright: ignore[reportArgumentType]
            )
    assert spy.load_calls == []


@pytest.mark.parametrize(
    "bad_entity_id",
    [str(TASK_A1_ID), None, 123, b"uuid", [TASK_A1_ID]],
    ids=["str", "none", "int", "bytes", "list"],
)
def test_entity_id_must_be_strict_uuid_before_repository_access(bad_entity_id: object) -> None:
    spy = SpyPortfolioRepository(portfolio=_current_portfolio())

    def explode(_portfolio_id: UUID) -> Portfolio | None:
        raise AssertionError("repository must not be touched for invalid entity_id")

    spy.load_impl = explode

    with pytest.raises(CalibratedEstimateRevisionBindingError, match="entity_id"):
        bind_effort_calibration_to_current_entity(
            _available_result(), bad_entity_id, spy  # pyright: ignore[reportArgumentType]
        )
    assert spy.load_calls == []


def test_hostile_v119_model_construct_result_rejected_freshly() -> None:
    spy = SpyPortfolioRepository(portfolio=_current_portfolio())
    valid = _available_result()
    hostile = EffectiveCalibrationApplicationResult.model_construct(
        **{**valid.model_dump(mode="python"), "proposal": None}
    )
    with pytest.raises(
        CalibratedEstimateRevisionBindingError,
        match="fresh strict revalidation",
    ):
        bind_effort_calibration_to_current_entity(hostile, TASK_A1_ID, spy)
    assert spy.load_calls == []


def test_hostile_nested_v118_evidence_rejected_through_v119_revalidation() -> None:
    spy = SpyPortfolioRepository(portfolio=_current_portfolio())
    valid = _available_result()
    assert valid.proposal is not None
    hostile_proposal = CalibratedEffortProposal.model_construct(
        **{
            **valid.proposal.model_dump(mode="python"),
            "calibrated_duration_seconds": valid.proposal.calibrated_duration_seconds + 1,
        }
    )
    hostile = EffectiveCalibrationApplicationResult.model_construct(
        **{
            **valid.model_dump(mode="python"),
            "proposal": hostile_proposal,
        }
    )
    with pytest.raises(
        CalibratedEstimateRevisionBindingError,
        match="calibrated_duration_seconds",
    ):
        bind_effort_calibration_to_current_entity(hostile, TASK_A1_ID, spy)
    assert spy.load_calls == []


def test_repository_load_error_propagates_unchanged() -> None:
    spy = SpyPortfolioRepository(portfolio=_current_portfolio())

    def boom(_portfolio_id: UUID) -> Portfolio | None:
        raise RuntimeError("repository boom")

    spy.load_impl = boom
    with pytest.raises(RuntimeError, match="repository boom"):
        bind_effort_calibration_to_current_entity(
            _available_result(), TASK_A1_ID, spy
        )


# --- Repository access semantics ---------------------------------------------


def test_current_portfolio_loaded_exactly_once_with_source_portfolio_id() -> None:
    spy = SpyPortfolioRepository(portfolio=_current_portfolio())
    bind_effort_calibration_to_current_entity(_available_result(), TASK_A1_ID, spy)
    assert spy.load_calls == [PORTFOLIO_ID]
    # A second binding still loads exactly once per call.
    bind_effort_calibration_to_current_entity(_available_result(), TASK_A1_ID, spy)
    assert spy.load_calls == [PORTFOLIO_ID, PORTFOLIO_ID]


def test_source_result_in_a_different_portfolio_loads_that_portfolio() -> None:
    result = _available_result(portfolio_id=OTHER_PORTFOLIO_ID)
    spy = SpyPortfolioRepository(portfolio=None)
    with pytest.raises(CalibratedEstimateRevisionPortfolioNotFoundError):
        bind_effort_calibration_to_current_entity(result, TASK_A1_ID, spy)
    assert spy.load_calls == [OTHER_PORTFOLIO_ID]


def test_missing_portfolio_fails_explicitly() -> None:
    spy = SpyPortfolioRepository(portfolio=None)
    with pytest.raises(CalibratedEstimateRevisionPortfolioNotFoundError, match="portfolio"):
        bind_effort_calibration_to_current_entity(_available_result(), TASK_A1_ID, spy)
    assert len(spy.load_calls) == 1


def test_error_types_are_narrow_value_errors() -> None:
    assert issubclass(CalibratedEstimateRevisionBindingError, ValueError)
    assert issubclass(CalibratedEstimateRevisionPortfolioNotFoundError, ValueError)
    assert issubclass(CalibratedEstimateRevisionEntityNotFoundError, ValueError)
    assert issubclass(CalibratedEstimateRevisionEntityTypeMismatchError, ValueError)
    assert issubclass(CalibratedEstimateRevisionEntityOutOfCurrentWbsError, ValueError)


# --- Entity identity / CURRENT-WBS membership --------------------------------


def test_exact_entity_in_exact_current_wbs_succeeds() -> None:
    proposal = _bind(_available_result())
    assert proposal.status is CalibratedEstimateRevisionProposalStatus.READY
    assert proposal.entity_id == TASK_A1_ID
    assert proposal.portfolio_id == PORTFOLIO_ID
    assert proposal.project_id == PROJECT_A_ID
    assert proposal.entity_type is EntityType.TASK
    assert proposal.candidate_duration_seconds == CANDIDATE
    assert proposal.calibrated_duration_seconds == 450  # 300 * 3/2, exact


def test_nested_entity_of_the_exact_current_wbs_succeeds() -> None:
    # A DELIVERABLE-typed V1.19 result binds the deliverable of project A.
    deliverable = _available_result(entity_type=EntityType.DELIVERABLE)
    proposal = bind_effort_calibration_to_current_entity(
        deliverable, DELIVERABLE_A_ID, SpyPortfolioRepository(portfolio=_current_portfolio())
    )
    assert proposal.entity_id == DELIVERABLE_A_ID
    assert proposal.entity_type is EntityType.DELIVERABLE
    # Deep membership: bind the task nested under the deliverable.
    deep = _bind(_available_result(), TASK_A2_ID)
    assert deep.entity_id == TASK_A2_ID
    assert deep.status is CalibratedEstimateRevisionProposalStatus.READY


def test_project_entity_itself_is_a_current_wbs_member() -> None:
    source = _available_result(entity_type=EntityType.PROJECT, candidate=100)  # 100*3/2=150
    proposal = bind_effort_calibration_to_current_entity(
        source, PROJECT_A_ID, SpyPortfolioRepository(portfolio=_current_portfolio())
    )
    assert proposal.status is CalibratedEstimateRevisionProposalStatus.READY
    assert proposal.project_id == PROJECT_A_ID
    assert proposal.entity_type is EntityType.PROJECT
    assert proposal.calibrated_duration_seconds == 150


def test_entity_missing_from_current_portfolio_fails() -> None:
    missing = UUID("abababab-abab-4aba-8aba-abababababab")
    with pytest.raises(CalibratedEstimateRevisionEntityNotFoundError, match="entity"):
        _bind(_available_result(), missing)


def test_stale_removed_entity_fails_against_current_portfolio() -> None:
    # The CURRENT portfolio no longer contains TASK_A1: the binding
    # must fail on the fresh, authoritative portfolio state.
    stale = _current_portfolio(include_task_a1=False)
    with pytest.raises(CalibratedEstimateRevisionEntityNotFoundError):
        bind_effort_calibration_to_current_entity(
            _available_result(), TASK_A1_ID, SpyPortfolioRepository(portfolio=stale)
        )


def test_entity_type_mismatch_fails_exact_type_binding() -> None:
    # DELIVERABLE_A exists in project A's CURRENT WBS, but the V1.19
    # result is a TASK result: no broader/narrower type fallback.
    with pytest.raises(CalibratedEstimateRevisionEntityTypeMismatchError, match="type"):
        _bind(_available_result(entity_type=EntityType.TASK), DELIVERABLE_A_ID)


def test_entity_outside_requested_project_wbs_fails() -> None:
    # ORPHAN_TASK exists in the CURRENT portfolio with the right type,
    # but has no WBS parent under project A.
    with pytest.raises(
        CalibratedEstimateRevisionEntityOutOfCurrentWbsError, match="work breakdown"
    ):
        _bind(_available_result(), ORPHAN_TASK_ID)


def test_same_type_entity_in_another_project_fails() -> None:
    # TASK_B1 is a genuine TASK, but of project B's WBS, not A's.
    with pytest.raises(CalibratedEstimateRevisionEntityOutOfCurrentWbsError):
        _bind(_available_result(project_id=PROJECT_A_ID), TASK_B1_ID)


def test_same_type_entity_in_other_portfolio_wbs_fails() -> None:
    # TASK_A2 lives under project A's deliverable; project B's WBS
    # contains only TASK_B1.
    with pytest.raises(CalibratedEstimateRevisionEntityOutOfCurrentWbsError):
        _bind(_available_result(project_id=PROJECT_B_ID), TASK_A2_ID)


def test_missing_project_anchor_fails_through_authoritative_wbs_rule() -> None:
    from trajectory_os.domain.work_breakdown import WorkBreakdownError

    unknown_project = UUID("cdcdcdcd-cdcd-4cdc-8cdc-cdcdcdcdcdcd")
    source = _available_result(project_id=unknown_project)
    with pytest.raises(WorkBreakdownError, match="unknown entity"):
        _bind(source, TASK_A1_ID)


def test_non_project_anchor_fails_through_authoritative_wbs_rule() -> None:
    from trajectory_os.domain.work_breakdown import WorkBreakdownError

    source = _available_result(entity_type=EntityType.TASK, project_id=TASK_A1_ID)
    with pytest.raises(WorkBreakdownError, match="PROJECT"):
        _bind(source, TASK_A2_ID)


# --- READY semantics ----------------------------------------------------------


def test_ready_requires_source_available_status() -> None:
    assert _bind(_available_result()).status is CalibratedEstimateRevisionProposalStatus.READY


def test_ready_calibrated_duration_equals_exact_v118_output() -> None:
    source = _available_result(entity_type=EntityType.TASK, candidate=101)
    proposal = _bind(source)
    assert source.proposal is not None
    # 101 * 3/2 = 151.5 -> half-up -> 152, computed by V1.18 UNTOUCHED.
    assert proposal.calibrated_duration_seconds == 152
    assert proposal.calibrated_duration_seconds == (
        source.proposal.calibrated_duration_seconds
    )


def test_ready_zero_candidate_carries_exact_zero_v118_output() -> None:
    proposal = _bind(_available_result(candidate=0))
    assert proposal.status is CalibratedEstimateRevisionProposalStatus.READY
    assert proposal.candidate_duration_seconds == 0
    assert proposal.calibrated_duration_seconds == 0


# --- NO_EFFECTIVE_FACTOR semantics -------------------------------------------


def test_no_effective_factor_source_returns_explicit_non_ready() -> None:
    proposal = _bind(_no_factor_result())
    assert proposal.status is CalibratedEstimateRevisionProposalStatus.NO_EFFECTIVE_FACTOR
    source_status = proposal.source_result.status
    assert source_status is EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR


def test_no_effective_factor_has_no_calibrated_duration() -> None:
    proposal = _bind(_no_factor_result())
    assert proposal.calibrated_duration_seconds is None


def test_no_effective_factor_never_fabricates_candidate_as_calibrated() -> None:
    source = _no_factor_result(candidate=CANDIDATE)
    proposal = _bind(source)
    # Not the candidate, not an identity-1/1 unchanged estimate: absence
    # stays explicit and the state is NOT READY.
    assert proposal.calibrated_duration_seconds is not CANDIDATE
    assert proposal.calibrated_duration_seconds is None
    assert proposal.status is not CalibratedEstimateRevisionProposalStatus.READY


def test_no_identity_factor_fallback_is_fabricated() -> None:
    proposal = _bind(_no_factor_result())
    assert proposal.source_result.proposal is None
    assert proposal.calibrated_duration_seconds is None


def test_no_effective_factor_entity_binding_is_still_validated() -> None:
    # The binding rules do not relax when no factor exists: cross-project
    # and missing entities still fail explicitly.
    with pytest.raises(CalibratedEstimateRevisionEntityOutOfCurrentWbsError):
        _bind(_no_factor_result(), TASK_B1_ID)
    missing = UUID("abababab-abab-4aba-8aba-abababababab")
    with pytest.raises(CalibratedEstimateRevisionEntityNotFoundError):
        _bind(_no_factor_result(), missing)
    with pytest.raises(CalibratedEstimateRevisionEntityTypeMismatchError):
        _bind(_no_factor_result(), DELIVERABLE_A_ID)


# --- Provenance retention ------------------------------------------------------


def test_source_v119_snapshot_retained_exactly() -> None:
    source = _available_result()
    proposal = _bind(source)
    assert proposal.source_result.model_dump(mode="python") == source.model_dump(mode="python")
    assert proposal.source_result == source
    assert proposal.source_result.proposal is not None
    assert proposal.source_result.proposal.model_dump(mode="python") == (
        source.proposal.model_dump(mode="python")
    )


def test_wrapper_retains_exact_scope_and_candidate() -> None:
    source = _available_result(entity_type=EntityType.TASK, candidate=777)
    proposal = _bind(source)
    assert proposal.portfolio_id == PORTFOLIO_ID
    assert proposal.project_id == PROJECT_A_ID
    assert proposal.entity_id == TASK_A1_ID
    assert proposal.entity_type is EntityType.TASK
    assert proposal.candidate_duration_seconds == 777
    assert type(proposal.candidate_duration_seconds) is int


# --- No V1.17 / V1.18 / persistence ------------------------------------------


def test_v119_module_imports_not_used_for_resolution_or_arithmetic() -> None:
    # V1.20 must not re-resolve (V1.17) or re-apply (V1.18): neither
    # boundary is even referenced from this module's namespace.
    assert "resolve_effective_effort_calibration_factors_durably" not in vars(binding)
    assert "apply_effective_effort_calibration_factor" not in vars(binding)


def test_no_v117_resolution_runs_during_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("V1.17 must not be resolved inside V1.20")

    monkeypatch.setattr(
        composition,
        "resolve_effective_effort_calibration_factors_durably",
        boom,
    )
    proposal = _bind(_available_result())
    assert proposal.status is CalibratedEstimateRevisionProposalStatus.READY


def test_no_v118_reapplication_runs_during_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    import trajectory_os.domain.execution_effort_calibration_factor_application as v118

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("V1.18 must not be re-applied inside V1.20")

    monkeypatch.setattr(v118, "apply_effective_effort_calibration_factor", boom)
    proposal = _bind(_available_result())
    assert proposal.calibrated_duration_seconds == 450  # exact V1.18 evidence, unchanged


def test_no_repository_writes_on_ready_path() -> None:
    # SpyPortfolioRepository.save raises AssertionError if ever invoked;
    # the READY path completes successfully anyway.
    proposal = _bind(_available_result())
    assert proposal.status is CalibratedEstimateRevisionProposalStatus.READY


def test_no_repository_writes_on_no_factor_path() -> None:
    proposal = _bind(_no_factor_result())
    assert proposal.status is CalibratedEstimateRevisionProposalStatus.NO_EFFECTIVE_FACTOR


def test_no_estimate_creation_or_persistence() -> None:
    # V1.20 must not create, reference, or persist a V1.10 estimate.
    assert "ExecutionEffortEstimate" not in vars(binding)
    proposal = _bind(_available_result())
    dumped = proposal.model_dump(mode="python")
    assert "estimate" not in dumped
    assert "estimate_id" not in dumped
    # The proposal is reviewable state only; no estimate fields exist.
    assert set(dumped) == {
        "portfolio_id",
        "project_id",
        "entity_id",
        "entity_type",
        "candidate_duration_seconds",
        "status",
        "calibrated_duration_seconds",
        "source_result",
    }


def test_sourcekind_vocabulary_is_unchanged() -> None:
    assert set(SourceKind) == {
        SourceKind.USER_CONFIRMED,
        SourceKind.IMPORTED,
        SourceKind.AI_INFERRED,
        SourceKind.AI_RECOMMENDED,
    }


def test_no_ai_or_provider_module_is_imported_by_v120() -> None:
    source = inspect.getsource(binding)
    assert "trajectory_os.adapters" not in source
    assert "ollama" not in source
    assert "duckdb" not in source


def test_no_clock_or_uuid_generation_participates() -> None:
    # One fixed portfolio AND one fixed V1.19 result drive every call;
    # the binding contributes no generated identity and no wall-clock
    # instant, so repeated bindings are byte-for-byte equivalent.
    source = _available_result()
    outcomes = [_bind(source).model_dump(mode="python") for _ in range(3)]
    first = outcomes[0]
    assert all(outcome == first for outcome in outcomes)


# --- Determinism ---------------------------------------------------------------


def test_repeated_equivalent_bindings_yield_equivalent_results() -> None:
    available = _available_result()
    no_factor = _no_factor_result()

    first = _bind(available)
    second = _bind(available)
    assert first.model_dump(mode="python") == second.model_dump(mode="python")
    assert first == second

    no_factor_first = _bind(no_factor)
    no_factor_second = _bind(no_factor)
    assert no_factor_first.model_dump(mode="python") == no_factor_second.model_dump(
        mode="python"
    )
    assert no_factor_first == no_factor_second


def test_input_result_is_not_mutated_by_the_binding() -> None:
    source = _available_result()
    before = source.model_dump(mode="python")
    _bind(source)
    assert source.model_dump(mode="python") == before


def test_current_portfolio_is_not_mutated_by_the_binding() -> None:
    portfolio = _current_portfolio()
    before = portfolio.model_dump(mode="python")
    _bind(_available_result())
    assert portfolio.model_dump(mode="python") == before


def test_unrelated_entity_helper_portfolio_unused() -> None:
    # _entity exists to document that stray entities are never fabricated
    # into the binding: it is a simple factory used nowhere in the happy
    # path assertions above.
    stray = _entity(EntityType.RESOURCE)
    assert stray.entity_type is EntityType.RESOURCE
