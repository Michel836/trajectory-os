"""Unit tests for explicit durable acceptance of a V1.20 calibrated estimate revision (V1.21).

Pins the application boundary that turns ONE already-READY V1.20
``CalibratedEstimateRevisionProposal`` into

* one V1.10 ``ExecutionEffortEstimate`` (via the unchanged existing
  ``create_execution_effort_estimate`` factory, ``USER_CONFIRMED``
  source, exact accepted ``calibrated_duration_seconds``, caller-supplied
  aware ``estimated_at``), and
* one NEW immutable ``AcceptedCalibratedEstimateRevision`` provenance
  record retaining the EXACT V1.20 snapshot (V1.20 -> V1.19 -> V1.18
  chain),

persisted through ONE atomic repository call
(``add_accepted_revision(estimate, provenance)``) — and NEVER through
two separate calls or two commits.

Also pins:

* strict payload validation (genuine V1.20 proposal only;
  ``model_construct()`` bypass with a hostile inner value rejected;
  dict/None/str rejected) BEFORE any repository touch;
* ``READY`` required exactly: ``NO_EFFECTIVE_FACTOR`` rejected with the
  dedicated error BEFORE any repository touch;
* strict ``estimate_id`` (UUID) and ``estimated_at`` (aware datetime)
  input validation BEFORE any repository touch (no coercion);
* CURRENT state stays authoritative: stale/moved/removed/wrong-type/
  cross-portfolio entities fail through the REAL V1.20 errors and
  perform NO repository write; a rebound that differs from the input
  proposal is rejected;
* the explicit, single, atomic append boundary: the repository is called
  EXACTLY ONCE with the exact two domain objects; no ``NO_ACTION``
  success path; no portfolio ``save`` ever;
* the in-memory success state:
  ``(ExecutionEffortEstimate, AcceptedCalibratedEstimateRevision)`` with
  an exact ``SourceKind.USER_CONFIRMED`` estimate and exact provenance
  chain;
* the frozen, cross-field-validated record and result models;
* no auto-acceptance anywhere (no default acceptance semantics): this
  function is the sole explicit entry point, and it never accepts a
  non-READY payload;
* no repository is ever mutated, saved, or written to except that single
  atomic append.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.application.execution_effort_calibration_acceptance import (
    AcceptCalibratedEstimateRevisionError,
    AcceptedCalibratedEstimateRevision,
    AcceptedCalibratedEstimateRevisionResult,
    NoEffectiveFactorCannotBeAcceptedError,
    accept_calibrated_estimate_revision_durably,
)
from trajectory_os.application.execution_effort_calibration_composition import (
    EffectiveCalibrationApplicationResult,
    EffectiveCalibrationApplicationStatus,
)
from trajectory_os.application.execution_effort_calibration_entity_binding import (
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
    apply_effective_effort_calibration_factor,
)
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("71111111-1111-4111-8111-111111111111")
OTHER_PORTFOLIO_ID = UUID("72222222-2222-4222-8222-222222222222")
PROJECT_A_ID = UUID("73333333-3333-4333-8333-333333333333")
PROJECT_B_ID = UUID("74444444-4444-4444-8444-444444444444")
TASK_A1_ID = UUID("75555555-5555-4555-8555-555555555555")
TASK_B1_ID = UUID("76666666-6666-4666-8666-666666666666")
ORPHAN_TASK_ID = UUID("77777777-7777-4777-8777-777777777777")
DELIVERABLE_A_ID = UUID("78888888-8888-4888-8888-888888888888")
CANDIDATE = 300
NOW_UTC = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
PLUS_TWO = timezone(timedelta(hours=2))
NOW_PLUS_TWO = datetime(2026, 9, 1, 10, 0, tzinfo=PLUS_TWO)


# --- Fixtures / helpers -----------------------------------------------------


def _entity(entity_id: UUID, entity_type: EntityType) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id,
        entity_type=entity_type,
        title=entity_type.value,
        description="",
    )


def _belongs_to(child: UUID, parent: UUID) -> TrajectoryRelation:
    return TrajectoryRelation(
        source_id=child,
        target_id=parent,
        relation_type=RelationType.BELONGS_TO,
    )


def _current_portfolio(*, task_a1_type: EntityType = EntityType.TASK) -> Portfolio:
    """Canonical CURRENT portfolio: project A + task A1 (inside WBS)."""
    return Portfolio(
        id=PORTFOLIO_ID,
        name="canonical",
        entities=[
            _entity(PROJECT_A_ID, EntityType.PROJECT),
            _entity(TASK_A1_ID, task_a1_type),
        ],
        relations=[_belongs_to(TASK_A1_ID, PROJECT_A_ID)],
    )


def _factor(entity_type: EntityType) -> EffectiveEffortCalibrationFactor:
    return EffectiveEffortCalibrationFactor(
        entity_type=entity_type,
        decision_id=uuid4(),
        decided_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=150,
        factor_numerator=3,
        factor_denominator=2,
    )


def _available_result(
    *,
    entity_type: EntityType = EntityType.TASK,
    portfolio_id: UUID = PORTFOLIO_ID,
    project_id: UUID = PROJECT_A_ID,
) -> EffectiveCalibrationApplicationResult:
    proposal = apply_effective_effort_calibration_factor(CANDIDATE, _factor(entity_type))
    return EffectiveCalibrationApplicationResult(
        portfolio_id=portfolio_id,
        project_id=project_id,
        entity_type=entity_type,
        candidate_duration_seconds=CANDIDATE,
        status=EffectiveCalibrationApplicationStatus.AVAILABLE,
        proposal=proposal,
    )


def _no_factor_result(
    entity_type: EntityType = EntityType.TASK,
) -> EffectiveCalibrationApplicationResult:
    return EffectiveCalibrationApplicationResult(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_A_ID,
        entity_type=entity_type,
        candidate_duration_seconds=CANDIDATE,
        status=EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR,
        proposal=None,
    )


def _ready_proposal() -> CalibratedEstimateRevisionProposal:
    return bind_effort_calibration_to_current_entity(
        _available_result(),
        TASK_A1_ID,
        SpyPortfolioRepository(portfolio=_current_portfolio()),
    )


def _no_factor_proposal() -> CalibratedEstimateRevisionProposal:
    return bind_effort_calibration_to_current_entity(
        _no_factor_result(),
        TASK_A1_ID,
        SpyPortfolioRepository(portfolio=_current_portfolio()),
    )


@dataclass
class SpyPortfolioRepository:
    """PortfolioRepository spy; ANY save/write attempt fails loudly."""

    portfolio: Portfolio | None
    load_calls: list[UUID] = field(default_factory=list)
    save_calls: list[Portfolio] = field(default_factory=list)

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.load_calls.append(portfolio_id)
        return self.portfolio

    def save(self, portfolio: Portfolio) -> None:
        self.save_calls.append(portfolio)
        raise AssertionError(
            "V1.21 must never save through the portfolio repository; "
            "portfolio mutation is forbidden at this boundary"
        )


@dataclass
class SpyRevisionRepository:
    """CalibratedEstimateRevisionRepository spy: records exact call tuples."""

    added: list[tuple[object, object]] = field(default_factory=list)
    stored: dict[UUID, AcceptedCalibratedEstimateRevision] = field(default_factory=dict)

    def add_accepted_revision(
        self,
        estimate: ExecutionEffortEstimate,
        provenance: AcceptedCalibratedEstimateRevision,
    ) -> None:
        self.added.append((estimate, provenance))
        self.stored[estimate.id] = provenance

    def get_provenance(
        self, estimate_id: UUID
    ) -> AcceptedCalibratedEstimateRevision | None:
        return self.stored.get(estimate_id)


class _FailingSecondInsertRepository:
    """A revision repository whose single call raises (atomic failure probe)."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.called = 0

    def add_accepted_revision(
        self,
        estimate: ExecutionEffortEstimate,
        provenance: AcceptedCalibratedEstimateRevision,
    ) -> None:
        self.called += 1
        raise self.failure

    def get_provenance(
        self, estimate_id: UUID
    ) -> AcceptedCalibratedEstimateRevision | None:
        return None


# --- Model strictness -------------------------------------------------------


def test_accepted_revision_model_is_frozen_and_forbids_extra_fields() -> None:
    record = _record()
    with pytest.raises((TypeError, ValidationError)):
        record.estimate_id = uuid4()  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra"):
        AcceptedCalibratedEstimateRevision(  # pyright: ignore[reportCallIssue]
            **_record_kwargs(estimate_id=uuid4()),
            unexpected="nope",
        )


def test_record_rejects_mismatched_portfolio_snapshot() -> None:
    with pytest.raises(ValidationError):
        _record(portfolio_id=OTHER_PORTFOLIO_ID)


def test_record_rejects_mismatched_entity_snapshot() -> None:
    with pytest.raises(ValidationError):
        _record(entity_id=TASK_B1_ID)


def test_record_rejects_mismatched_candidate_duration() -> None:
    with pytest.raises(ValidationError):
        _record(candidate_duration_seconds=CANDIDATE + 1)


def test_record_rejects_mismatched_calibrated_duration() -> None:
    with pytest.raises(ValidationError):
        _record(calibrated_duration_seconds=CANDIDATE * 3 // 2 + 1)


def test_record_rejects_no_effective_factor_snapshot() -> None:
    with pytest.raises(ValidationError):
        AcceptedCalibratedEstimateRevision(
            **_record_kwargs(source_proposal=_no_factor_proposal())
        )


def test_record_requires_non_negative_durations() -> None:
    with pytest.raises(ValidationError):
        _record(candidate_duration_seconds=-1, calibrated_duration_seconds=-1)


def _record(**overrides: object) -> AcceptedCalibratedEstimateRevision:
    return AcceptedCalibratedEstimateRevision(**_record_kwargs(**overrides))


def _record_kwargs(**overrides: object) -> dict[str, object]:
    proposal = _ready_proposal()
    kwargs: dict[str, object] = dict(
        estimate_id=uuid4(),
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_A_ID,
        entity_id=TASK_A1_ID,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=CANDIDATE,
        calibrated_duration_seconds=CANDIDATE * 3 // 2,
        estimated_at=NOW_UTC,
        source_proposal=proposal,
    )
    kwargs.update(overrides)
    return kwargs


def _result_kwargs(**overrides: object) -> dict[str, object]:
    kwargs = _record_kwargs(**overrides)
    est = ExecutionEffortEstimate(
        id=UUID(str(kwargs["estimate_id"])),  # pyright: ignore[reportOptionalMemberAccess]
        portfolio_id=PORTFOLIO_ID,
        entity_id=TASK_A1_ID,
        duration_seconds=CANDIDATE * 3 // 2,
        estimated_at=NOW_UTC,
        source=SourceKind.USER_CONFIRMED,
    )
    return dict(
        estimate=est,
        provenance=AcceptedCalibratedEstimateRevision(**kwargs),
    )


def test_result_model_is_frozen_and_cross_field_validated() -> None:
    result = AcceptedCalibratedEstimateRevisionResult(**_result_kwargs())
    with pytest.raises((TypeError, ValidationError)):
        result.estimate = result.estimate  # type: ignore[misc]

    with pytest.raises(ValidationError):
        AcceptedCalibratedEstimateRevisionResult(
            **_result_kwargs(
                estimate=ExecutionEffortEstimate(
                    id=uuid4(),  # different id than provenance
                    portfolio_id=PORTFOLIO_ID,
                    entity_id=TASK_A1_ID,
                    duration_seconds=CANDIDATE * 3 // 2,
                    estimated_at=NOW_UTC,
                ),
            )
        )


def test_error_hierarchy_is_precise() -> None:
    assert issubclass(NoEffectiveFactorCannotBeAcceptedError, AcceptCalibratedEstimateRevisionError)


# --- Input strictness (before ANY repository touch) -------------------------


def test_payload_must_be_a_genuine_v120_proposal_instance() -> None:
    spy_repo = SpyPortfolioRepository(portfolio=_current_portfolio())
    revision = SpyRevisionRepository()
    proposal = _ready_proposal()
    for bad in (
        "ready",
        None,
        {"status": "ready"},
        proposal.model_dump(),
        proposal.model_dump_json(),
    ):
        with pytest.raises(AcceptCalibratedEstimateRevisionError):
            accept_calibrated_estimate_revision_durably(
                bad,  # pyright: ignore[reportArgumentType]
                estimate_id=uuid4(),
                estimated_at=NOW_UTC,
                portfolio_repository=spy_repo,
                revision_repository=revision,
            )
    assert spy_repo.load_calls == [], "no repository access on invalid payload"
    assert spy_repo.save_calls == []
    assert revision.added == []


def test_payload_hostile_model_construct_with_invalid_inner_value_is_rejected() -> None:
    proposal = CalibratedEstimateRevisionProposal.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_A_ID,
        entity_id=TASK_A1_ID,
        entity_type=EntityType.TASK,
        candidate_duration_seconds="not-an-int",  # pyright: ignore[reportCallIssue]
        status=CalibratedEstimateRevisionProposalStatus.READY,
        calibrated_duration_seconds=CANDIDATE * 3 // 2,
        source_result=object(),  # pyright: ignore[reportArgumentType]
    )
    with pytest.raises(AcceptCalibratedEstimateRevisionError):
        accept_calibrated_estimate_revision_durably(
            proposal,
            estimate_id=uuid4(),
            estimated_at=NOW_UTC,
            portfolio_repository=SpyPortfolioRepository(portfolio=_current_portfolio()),
            revision_repository=SpyRevisionRepository(),
        )


def test_non_uuid_estimate_id_rejected_before_any_repository_access() -> None:
    spy_repo = SpyPortfolioRepository(portfolio=_current_portfolio())
    revision = SpyRevisionRepository()
    for bad_id in ("a-string-id", 42, 4.5, b"bytes"):
        with pytest.raises(AcceptCalibratedEstimateRevisionError):
            accept_calibrated_estimate_revision_durably(
                _ready_proposal(),
                estimate_id=bad_id,  # pyright: ignore[reportArgumentType]
                estimated_at=NOW_UTC,
                portfolio_repository=spy_repo,
                revision_repository=revision,
            )
    assert spy_repo.load_calls == []
    assert revision.added == []


def test_naive_or_non_datetime_estimated_at_rejected_before_any_repository_access() -> None:
    spy_repo = SpyPortfolioRepository(portfolio=_current_portfolio())
    revision = SpyRevisionRepository()
    for bad_at in (
        datetime(2026, 9, 1, 8, 0),  # naive
        "2026-09-01T08:00:00+00:00",
        1234567,
    ):
        with pytest.raises(AcceptCalibratedEstimateRevisionError):
            accept_calibrated_estimate_revision_durably(
                _ready_proposal(),
                estimate_id=uuid4(),
                estimated_at=bad_at,  # pyright: ignore[reportArgumentType]
                portfolio_repository=spy_repo,
                revision_repository=revision,
            )
    assert spy_repo.load_calls == []
    assert revision.added == []


# --- NO_EFFECTIVE_FACTOR must be rejected before ANY repository touch -------


def test_no_effective_factor_rejected_dedicated_error_no_repo_access() -> None:
    spy_repo = SpyPortfolioRepository(portfolio=_current_portfolio())
    revision = SpyRevisionRepository()
    with pytest.raises(NoEffectiveFactorCannotBeAcceptedError):
        accept_calibrated_estimate_revision_durably(
            _no_factor_proposal(),
            estimate_id=uuid4(),
            estimated_at=NOW_UTC,
            portfolio_repository=spy_repo,
            revision_repository=revision,
        )
    assert spy_repo.load_calls == []
    assert spy_repo.save_calls == []
    assert revision.added == []
    assert NoEffectiveFactorCannotBeAcceptedError is not AcceptCalibratedEstimateRevisionError


# --- Current-state authority: rebind failures perform NO write --------------


def test_stale_entity_type_mismatch_fails_with_real_v120_error_and_no_write() -> None:
    spy_repo = SpyPortfolioRepository(
        portfolio=_current_portfolio(task_a1_type=EntityType.DELIVERABLE)
    )
    revision = SpyRevisionRepository()
    with pytest.raises(CalibratedEstimateRevisionEntityTypeMismatchError):
        accept_calibrated_estimate_revision_durably(
            _ready_proposal(),
            estimate_id=uuid4(),
            estimated_at=NOW_UTC,
            portfolio_repository=spy_repo,
            revision_repository=revision,
        )
    assert spy_repo.save_calls == []
    assert revision.added == []


def test_removed_entity_fails_with_real_v120_error_and_no_write() -> None:
    portfolio_without_task = _current_portfolio().model_copy(
        update={"entities": [_entity(PROJECT_A_ID, EntityType.PROJECT)]},
    )
    spy_repo = SpyPortfolioRepository(portfolio=portfolio_without_task)
    revision = SpyRevisionRepository()
    with pytest.raises(CalibratedEstimateRevisionEntityNotFoundError):
        accept_calibrated_estimate_revision_durably(
            _ready_proposal(),
            estimate_id=uuid4(),
            estimated_at=NOW_UTC,
            portfolio_repository=spy_repo,
            revision_repository=revision,
        )
    assert revision.added == []


def test_cross_project_entity_fails_with_real_v120_error_and_no_write() -> None:
    """Same-type task of ANOTHER project: valid once, outside A-WBS now.

    The proposal was legitimately built when the orphan task WAS inside
    project A's WBS; CURRENT now anchors it to project B instead, so it is
    outside A's WBS and V1.20 must reject it with the real error.
    """
    orphan = TrajectoryEntity(id=ORPHAN_TASK_ID, entity_type=EntityType.TASK, title="t")
    other_project = TrajectoryEntity(
        id=PROJECT_B_ID, entity_type=EntityType.PROJECT, title="b"
    )
    # Proposal built against an earlier CURRENT where the orphan was in A-WBS:
    earlier = Portfolio(
        id=PORTFOLIO_ID,
        name="earlier",
        entities=[_entity(PROJECT_A_ID, EntityType.PROJECT), orphan],
        relations=[_belongs_to(ORPHAN_TASK_ID, PROJECT_A_ID)],
    )
    proposal = bind_effort_calibration_to_current_entity(
        _available_result(), ORPHAN_TASK_ID, SpyPortfolioRepository(portfolio=earlier)
    )
    # CURRENT now anchors the orphan to project B: outside project A's WBS.
    current_cross_project = Portfolio(
        id=PORTFOLIO_ID,
        name="cur",
        entities=[_entity(PROJECT_A_ID, EntityType.PROJECT), other_project, orphan],
        relations=[_belongs_to(ORPHAN_TASK_ID, PROJECT_B_ID)],
    )
    spy_repo = SpyPortfolioRepository(portfolio=current_cross_project)
    revision = SpyRevisionRepository()
    with pytest.raises(CalibratedEstimateRevisionEntityOutOfCurrentWbsError):
        accept_calibrated_estimate_revision_durably(
            proposal,
            estimate_id=uuid4(),
            estimated_at=NOW_UTC,
            portfolio_repository=spy_repo,
            revision_repository=revision,
        )
    assert revision.added == []


def test_missing_portfolio_fails_with_real_v120_error_and_no_write() -> None:
    spy_repo = SpyPortfolioRepository(portfolio=None)
    revision = SpyRevisionRepository()
    with pytest.raises(CalibratedEstimateRevisionPortfolioNotFoundError):
        accept_calibrated_estimate_revision_durably(
            _ready_proposal(),
            estimate_id=uuid4(),
            estimated_at=NOW_UTC,
            portfolio_repository=spy_repo,
            revision_repository=revision,
        )
    assert revision.added == []


def test_rebound_must_value_equal_input_proposal() -> None:
    # The ONLY way the current portfolio can disagree is a different
    # entity-type (caught by V1.20) or a different WBS (caught by V1.20),
    # both covered above. A genuine value-equality mismatch cannot be
    # produced through the real V1.20 function (it returns either an
    # equal proposal or raises), which is exactly the invariant we want:
    # the CURRENT state is authoritative and no "silent" drift is
    # accepted.
    proposal = _ready_proposal()
    spy_repo = SpyPortfolioRepository(portfolio=_current_portfolio())
    revision = SpyRevisionRepository()
    result = accept_calibrated_estimate_revision_durably(
        proposal,
        estimate_id=uuid4(),
        estimated_at=NOW_UTC,
        portfolio_repository=spy_repo,
        revision_repository=revision,
    )
    assert result.provenance.source_proposal == proposal
    assert result.provenance.source_proposal.status == (
        CalibratedEstimateRevisionProposalStatus.READY
    )
    assert result.estimate.duration_seconds == (
        proposal.calibrated_duration_seconds
    )


# --- Explicit, single, atomic append ----------------------------------------


def test_append_is_called_exactly_once_with_exact_estimate_and_provenance() -> None:
    spy_repo = SpyPortfolioRepository(portfolio=_current_portfolio())
    revision = SpyRevisionRepository()
    estimate_id = uuid4()
    result = accept_calibrated_estimate_revision_durably(
        _ready_proposal(),
        estimate_id=estimate_id,
        estimated_at=NOW_UTC,
        portfolio_repository=spy_repo,
        revision_repository=revision,
    )

    # Exactly ONE append call, with EXACTLY the two domain objects.
    assert len(revision.added) == 1
    (est, prov) = revision.added[0]
    assert est is result.estimate
    assert prov is result.provenance
    assert est.id == estimate_id
    assert prov.estimate_id == estimate_id
    assert prov is revision.stored[estimate_id]


def test_estimate_carries_exact_accepted_duration_and_user_confirmed_source() -> None:
    revision = SpyRevisionRepository()
    res = accept_calibrated_estimate_revision_durably(
        _ready_proposal(),
        estimate_id=uuid4(),
        estimated_at=NOW_UTC,
        portfolio_repository=SpyPortfolioRepository(portfolio=_current_portfolio()),
        revision_repository=revision,
    )
    est = res.estimate
    assert est.duration_seconds == CANDIDATE * 3 // 2
    assert est.entity_id == TASK_A1_ID
    assert est.estimated_at == NOW_UTC


def test_estimated_at_offset_is_preserved_verbatim() -> None:
    revision = SpyRevisionRepository()
    proposal = _ready_proposal()
    result = accept_calibrated_estimate_revision_durably(
        proposal,
        estimate_id=uuid4(),
        estimated_at=NOW_PLUS_TWO,
        portfolio_repository=SpyPortfolioRepository(portfolio=_current_portfolio()),
        revision_repository=revision,
    )
    assert result.estimate.estimated_at.tzinfo is PLUS_TWO
    assert result.estimate.estimated_at == NOW_PLUS_TWO
    assert result.provenance.estimated_at == NOW_PLUS_TWO


def test_no_portfolio_save_and_no_unexpected_loads() -> None:
    spy_repo = SpyPortfolioRepository(portfolio=_current_portfolio())
    revision = SpyRevisionRepository()
    accept_calibrated_estimate_revision_durably(
        _ready_proposal(),
        estimate_id=uuid4(),
        estimated_at=NOW_UTC,
        portfolio_repository=spy_repo,
        revision_repository=revision,
    )
    assert spy_repo.save_calls == [], "portfolio must never be saved/mutated"
    # TWO reads: one from V1.20 rebind, one for the V1.10-A factory.
    assert spy_repo.load_calls == [PORTFOLIO_ID, PORTFOLIO_ID]


def test_repository_failure_propagates_unchanged_and_no_second_call_is_made() -> None:
    revision = _FailingSecondInsertRepository(RuntimeError("simulated storage failure"))
    with pytest.raises(RuntimeError, match="simulated storage failure"):
        accept_calibrated_estimate_revision_durably(
            _ready_proposal(),
            estimate_id=uuid4(),
            estimated_at=NOW_UTC,
            portfolio_repository=SpyPortfolioRepository(portfolio=_current_portfolio()),
            revision_repository=revision,
        )
    assert revision.called == 1  # exactly ONE atomic append attempt


def test_repeated_equivalent_acceptances_are_independent_appends() -> None:
    """Same proposal accepted twice with DIFFERENT caller-supplied ids.

    Each acceptance is an independent append-only revision: two distinct
    estimate rows, two distinct provenance records, and the proposal
    itself is never mutated and stays usable (per the V1.10 rule "a new
    estimate for the same entity does not replace an older one").
    """
    spy_repo = SpyPortfolioRepository(portfolio=_current_portfolio())
    revision = SpyRevisionRepository()
    first_id = uuid4()
    second_id = uuid4()
    proposal = _ready_proposal()

    r1 = accept_calibrated_estimate_revision_durably(
        proposal,
        estimate_id=first_id,
        estimated_at=NOW_UTC,
        portfolio_repository=spy_repo,
        revision_repository=revision,
    )
    r2 = accept_calibrated_estimate_revision_durably(
        proposal,  # same proposal object, unchanged
        estimate_id=second_id,
        estimated_at=NOW_UTC,
        portfolio_repository=spy_repo,
        revision_repository=revision,
    )

    assert r1.estimate.id == first_id
    assert r2.estimate.id == second_id
    assert r1.provenance.source_proposal == proposal
    assert r2.provenance.source_proposal == proposal
    assert len(revision.added) == 2
    assert first_id in revision.stored and second_id in revision.stored
    assert r1.estimate != r2.estimate
    assert spy_repo.save_calls == []
