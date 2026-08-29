"""Unit tests for the V1.22 deterministic current-effective execution-effort
estimate resolution.

Covers the required evidence surface for

* ``select_latest_execution_effort_estimate`` — the single canonical
  V1.10 selection policy function, and
* ``resolve_effective_execution_effort_estimate`` (pure) and
  ``resolve_effective_execution_effort_estimate_durably`` (repository
  boundary):

1.  strict/frozen result model,
2.  exact closed status vocabulary,
3.  strict public UUID scope inputs BEFORE any repository access,
4.  empty history -> explicit NO_ESTIMATE,
5.  NO_ESTIMATE performs ZERO provenance lookups,
6.  one estimate -> AVAILABLE carrying the exact estimate,
7.  later chronological estimate wins,
8.  equal instants -> greater estimate UUID integer wins,
9.  mixed timezone offsets compare actual instants, never ISO text,
10. insertion order cannot affect selection,
11. duplicate estimate IDs rejected,
12. foreign-portfolio estimates rejected,
13. foreign-entity estimates rejected,
14. hostile ``model_construct()`` estimates freshly rejected,
15. ordinary selected estimate without provenance -> provenance ``None``,
16. selected calibrated estimate -> exact V1.21 provenance returned,
17. a historical calibrated estimate is NOT preferred over a newer
    ordinary estimate,
18. a newer calibrated estimate can win only by V1.10 ordering,
19. provenance queried exactly once, for the selected estimate only,
20. provenance of non-selected historical estimates is never queried,
21. hostile ``model_construct()`` provenance freshly rejected,
22. provenance/estimate identity mismatch rejected,
23. provenance duration mismatch rejected,
24. provenance timestamp mismatch rejected,
25. provenance portfolio/entity mismatch rejected,
26. read-only: no write/update/delete method exists on the boundary and
    nothing is ever written,
27. no clock reads / UUID generation / AI / provider calls on the path,
28. deterministic repeated equivalent reads yield equivalent results.

The repository/reader fakes expose the ONLY read methods of the
``CalibratedEstimateRevisionRepository``/``ExecutionEffortEstimateReader``
protocols that V1.22 uses, plus a failing write probe: any accidental
write attempt fails the test loudly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.application import (
    EffectiveExecutionEffortEstimate,
    EffectiveExecutionEffortEstimateError,
    EffectiveExecutionEffortEstimateHistoryError,
    EffectiveExecutionEffortEstimateProvenanceError,
    EffectiveExecutionEffortEstimateStatus,
    resolve_effective_execution_effort_estimate,
    resolve_effective_execution_effort_estimate_durably,
)
from trajectory_os.application.execution_effort_calibration_acceptance import (
    AcceptedCalibratedEstimateRevision,
)
from trajectory_os.application.execution_effort_calibration_composition import (
    EffectiveCalibrationApplicationResult,
    EffectiveCalibrationApplicationStatus,
)
from trajectory_os.application.execution_effort_calibration_entity_binding import (
    CalibratedEstimateRevisionProposal,
    bind_effort_calibration_to_current_entity,
)
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactor,
)
from trajectory_os.domain.execution_effort_calibration_factor_application import (
    apply_effective_effort_calibration_factor,
)
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
    select_latest_execution_effort_estimate,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

# --- Fixed identities / constants -------------------------------------------

PORTFOLIO_ID = UUID("71111111-1111-4111-8111-111111111111")
OTHER_PORTFOLIO_ID = UUID("72222222-2222-4222-8222-222222222222")
PROJECT_A_ID = UUID("73333333-3333-4333-8333-333333333333")
PROJECT_B_ID = UUID("74444444-4444-4444-8444-444444444444")
TASK_A_ID = UUID("75555555-5555-4555-8555-555555555555")
TASK_B_ID = UUID("76666666-6666-4666-8666-666666666666")

CANDIDATE = 300
CALIBRATED = CANDIDATE * 3 // 2  # 450

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
PLUS_TWO = timezone(timedelta(hours=2))
MINUS_FIVE = timezone(timedelta(hours=-5))

# Deterministic UUID pair for the tie-break tests: LOW < HIGH by .int.
LOW_EST_ID = UUID("00112233-4455-6677-8899-00000000000a")
HIGH_EST_ID = UUID("00112233-4455-6677-8899-00000000000b")
assert LOW_EST_ID.int < HIGH_EST_ID.int


# --- Scope A / scope B fixtures (for the V1.21 provenance chain) -------------


def _entity(entity_id: UUID, entity_type: EntityType) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id,
        entity_type=entity_type,
        title=entity_type.value,
        description="",
    )


def _belongs_to(child: UUID, parent: UUID) -> TrajectoryRelation:
    return TrajectoryRelation(
        source_id=child, target_id=parent, relation_type=RelationType.BELONGS_TO
    )


def _portfolio_a() -> Portfolio:
    return Portfolio(
        id=PORTFOLIO_ID,
        name="scope-a",
        entities=[
            _entity(PROJECT_A_ID, EntityType.PROJECT),
            _entity(TASK_A_ID, EntityType.TASK),
        ],
        relations=[_belongs_to(TASK_A_ID, PROJECT_A_ID)],
    )


def _portfolio_b() -> Portfolio:
    return Portfolio(
        id=OTHER_PORTFOLIO_ID,
        name="scope-b",
        entities=[
            _entity(PROJECT_B_ID, EntityType.PROJECT),
            _entity(TASK_B_ID, EntityType.TASK),
        ],
        relations=[_belongs_to(TASK_B_ID, PROJECT_B_ID)],
    )


_SCOPES: list[tuple[UUID, UUID, UUID, Portfolio]] = [
    (PORTFOLIO_ID, PROJECT_A_ID, TASK_A_ID, _portfolio_a()),
    (OTHER_PORTFOLIO_ID, PROJECT_B_ID, TASK_B_ID, _portfolio_b()),
]


@dataclass
class SpyPortfolioRepository:
    """Read-only PortfolioRepository probe for the V1.20 binding chain.

    ANY save attempt fails the test: V1.22 never mutates portfolios.
    """

    portfolio: Portfolio
    load_calls: list[UUID] = field(default_factory=list)

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.load_calls.append(portfolio_id)
        if portfolio_id == self.portfolio.id:
            return self.portfolio
        return None

    def save(self, portfolio: Portfolio) -> None:
        raise AssertionError("V1.22 must never save a portfolio")


def _factor() -> EffectiveEffortCalibrationFactor:
    return EffectiveEffortCalibrationFactor(
        entity_type=EntityType.TASK,
        decision_id=UUID("aaaa1111-1111-4111-8111-111111111111"),
        decided_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=150,
        factor_numerator=3,
        factor_denominator=2,
    )


def _ready_proposal(scope: int = 0) -> CalibratedEstimateRevisionProposal:
    portfolio_id, project_id, entity_id, portfolio = _SCOPES[scope]
    result = EffectiveCalibrationApplicationResult(
        portfolio_id=portfolio_id,
        project_id=project_id,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=CANDIDATE,
        status=EffectiveCalibrationApplicationStatus.AVAILABLE,
        proposal=apply_effective_effort_calibration_factor(CANDIDATE, _factor()),
    )
    return bind_effort_calibration_to_current_entity(
        result, entity_id, SpyPortfolioRepository(portfolio=portfolio)
    )


def _record(scope: int = 0, **overrides: object) -> AcceptedCalibratedEstimateRevision:
    portfolio_id, project_id, entity_id, _portfolio = _SCOPES[scope]
    kwargs: dict[str, object] = dict(
        estimate_id=uuid4(),
        portfolio_id=portfolio_id,
        project_id=project_id,
        entity_id=entity_id,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=CANDIDATE,
        calibrated_duration_seconds=CALIBRATED,
        estimated_at=T0,
        source_proposal=_ready_proposal(scope),
    )
    kwargs.update(overrides)
    return AcceptedCalibratedEstimateRevision(**kwargs)


# --- Estimate builders --------------------------------------------------------


def _estimate(
    *,
    estimate_id: UUID | None = None,
    portfolio_id: UUID = PORTFOLIO_ID,
    entity_id: UUID = TASK_A_ID,
    duration_seconds: int = 100,
    estimated_at: datetime = T0,
) -> ExecutionEffortEstimate:
    return ExecutionEffortEstimate(
        # A FRESH unique id per call: every fixture estimate represents a
        # distinct revision. (A mutable/once-evaluated default would silently
        # duplicate ids and trigger the legitimate duplicate-id rejection.)
        id=uuid4() if estimate_id is None else estimate_id,
        portfolio_id=portfolio_id,
        entity_id=entity_id,
        duration_seconds=duration_seconds,
        estimated_at=estimated_at,
        source=SourceKind.USER_CONFIRMED,
    )


# --- Repository / reader fakes ------------------------------------------------


@dataclass
class SpyEstimateReader:
    """ExecutionEffortEstimateReader fake: history provider + call probe.

    Exposes NO write method: the reader protocol itself is read-only, and
    V1.22 must perform reads only.
    """

    history: tuple[ExecutionEffortEstimate, ...] = ()
    entity_calls: list[tuple[object, object]] = field(default_factory=list)
    portfolio_calls: list[object] = field(default_factory=list)

    def list_for_portfolio(self, portfolio_id: UUID) -> tuple[ExecutionEffortEstimate, ...]:
        self.portfolio_calls.append(portfolio_id)
        raise AssertionError("V1.22 must resolve per exact (portfolio, entity) scope")

    def list_for_entity(
        self, portfolio_id: UUID, entity_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        self.entity_calls.append((portfolio_id, entity_id))
        return self.history


@dataclass
class SpyRevisionRepository:
    """CalibratedEstimateRevisionRepository read fake + call probe.

    ``add_accepted_revision`` exists ONLY as a write probe: if V1.22 ever
    attempted a write, the test fails loudly instead of hiding the bug.
    """

    stored: dict[UUID, object] = field(default_factory=dict)
    provenance_calls: list[UUID] = field(default_factory=list)
    write_attempts: list[tuple[object, object]] = field(default_factory=list)

    def get_provenance(self, estimate_id: UUID) -> object:
        self.provenance_calls.append(estimate_id)
        return self.stored.get(estimate_id)

    def add_accepted_revision(
        self,
        estimate: ExecutionEffortEstimate,
        provenance: AcceptedCalibratedEstimateRevision,
    ) -> None:
        self.write_attempts.append((estimate, provenance))
        raise AssertionError(
            "V1.22 is read-only and must never append estimates/provenance"
        )


def _resolve(
    history: list[ExecutionEffortEstimate] | tuple[ExecutionEffortEstimate, ...],
    stored: dict[UUID, object] | None = None,
    portfolio_id: object = PORTFOLIO_ID,
    entity_id: object = TASK_A_ID,
) -> tuple[EffectiveExecutionEffortEstimate, SpyEstimateReader, SpyRevisionRepository]:
    reader = SpyEstimateReader(history=tuple(history))
    repository = SpyRevisionRepository(stored=dict(stored or {}))
    result = resolve_effective_execution_effort_estimate_durably(
        portfolio_id,  # type: ignore[arg-type]
        entity_id,  # type: ignore[arg-type]
        reader,
        repository,
    )
    return result, reader, repository


def _resolve_pure(
    estimates: list[ExecutionEffortEstimate],
    calibrated_provenance: object = None,
    portfolio_id: object = PORTFOLIO_ID,
    entity_id: object = TASK_A_ID,
) -> EffectiveExecutionEffortEstimate:
    return resolve_effective_execution_effort_estimate(
        portfolio_id,  # type: ignore[arg-type]
        entity_id,  # type: ignore[arg-type]
        estimates,
        calibrated_provenance=calibrated_provenance,  # type: ignore[arg-type]
    )


# --- Canonical V1.10 selection policy (select_latest_execution_effort_estimate)


class TestSelectLatestPolicy:
    def test_empty_input_returns_none(self) -> None:
        assert select_latest_execution_effort_estimate(()) is None

    def test_single_estimate_is_selected_verbatim(self) -> None:
        est = _estimate()
        assert select_latest_execution_effort_estimate((est,)) is est

    def test_later_chronological_instant_wins(self) -> None:
        old = _estimate(estimated_at=T0)
        new = _estimate(estimated_at=T1)
        assert select_latest_execution_effort_estimate((new, old)) is new

    def test_equal_instant_greater_uuid_int_wins(self) -> None:
        low = _estimate(estimate_id=LOW_EST_ID, estimated_at=T0)
        high = _estimate(estimate_id=HIGH_EST_ID, estimated_at=T0)
        assert select_latest_execution_effort_estimate((high, low)) is high
        assert select_latest_execution_effort_estimate((low, high)) is high

    def test_mixed_offsets_compare_instants_not_iso_text(self) -> None:
        # Lexicographic ISO winner: "2026-09-01T13:00:00+02:00".
        # Actual-instant winner:  "2026-09-01T12:30:00+00:00" (later instant).
        lex_wins = _estimate(
            estimated_at=datetime(2026, 9, 1, 13, 0, tzinfo=PLUS_TWO)
        )  # 11:00Z
        instant_wins = _estimate(
            estimated_at=datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
        )  # 12:30Z — truly later than 11:00Z
        assert instant_wins.estimated_at > lex_wins.estimated_at
        # The ISO TEXT still misleads: "13:00:...+02:00" sorts after
        # "12:30:...+00:00" even though its instant is earlier.
        assert str(lex_wins.estimated_at) > str(instant_wins.estimated_at)
        assert (
            select_latest_execution_effort_estimate((lex_wins, instant_wins))
            is instant_wins
        )

    def test_insertion_order_cannot_affect_selection(self) -> None:
        a = _estimate(duration_seconds=10, estimated_at=T0)
        b = _estimate(duration_seconds=20, estimated_at=T1)
        c = _estimate(duration_seconds=30, estimated_at=T2)
        order1 = (c, a, b)
        order2 = (a, c, b)
        assert select_latest_execution_effort_estimate(order1) is c
        assert select_latest_execution_effort_estimate(order2) is c


# --- Result model strictness / status vocabulary ------------------------------


class TestResultModel:
    def test_status_vocabulary_is_exactly_closed(self) -> None:
        assert {status.value for status in EffectiveExecutionEffortEstimateStatus} == {
            "available",
            "no_estimate",
        }
        assert len(EffectiveExecutionEffortEstimateStatus) == 2

    def test_result_model_is_frozen_and_forbids_extra_fields(self) -> None:
        est = _estimate()
        result = EffectiveExecutionEffortEstimate(
            portfolio_id=PORTFOLIO_ID,
            entity_id=TASK_A_ID,
            status=EffectiveExecutionEffortEstimateStatus.AVAILABLE,
            estimate=est,
        )
        with pytest.raises((TypeError, ValidationError)):
            result.estimate = est  # type: ignore[misc]
        with pytest.raises(ValidationError, match="extra"):
            EffectiveExecutionEffortEstimate(  # pyright: ignore[reportCallIssue]
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ID,
                status=EffectiveExecutionEffortEstimateStatus.AVAILABLE,
                estimate=est,
                unexpected="nope",
            )

    def test_available_result_requires_exactly_one_estimate(self) -> None:
        with pytest.raises(ValidationError):
            EffectiveExecutionEffortEstimate(
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ID,
                status=EffectiveExecutionEffortEstimateStatus.AVAILABLE,
            )

    def test_no_estimate_result_cannot_carry_estimate_or_provenance(self) -> None:
        est = _estimate()
        record = _record()
        with pytest.raises(ValidationError):
            EffectiveExecutionEffortEstimate(
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ID,
                status=EffectiveExecutionEffortEstimateStatus.NO_ESTIMATE,
                estimate=est,
            )
        with pytest.raises(ValidationError):
            EffectiveExecutionEffortEstimate(
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ID,
                status=EffectiveExecutionEffortEstimateStatus.NO_ESTIMATE,
                calibrated_provenance=record,
            )

    def test_result_rejects_estimate_outside_requested_scope(self) -> None:
        foreign = _estimate(entity_id=TASK_B_ID)
        with pytest.raises(ValidationError):
            EffectiveExecutionEffortEstimate(
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ID,
                status=EffectiveExecutionEffortEstimateStatus.AVAILABLE,
                estimate=foreign,
            )

    def test_result_rejects_provenance_identity_mismatch(self) -> None:
        est = _estimate(duration_seconds=CALIBRATED)
        record = _record(estimate_id=uuid4())  # references a different estimate
        with pytest.raises(ValidationError):
            EffectiveExecutionEffortEstimate(
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ID,
                status=EffectiveExecutionEffortEstimateStatus.AVAILABLE,
                estimate=est,
                calibrated_provenance=record,
            )


# --- Strict scope inputs (before ANY repository access) ------------------------


class TestScopeInputStrictness:
    @pytest.mark.parametrize(
        ("portfolio_id", "entity_id"),
        [
            (str(PORTFOLIO_ID), TASK_A_ID),
            (PORTFOLIO_ID, str(TASK_A_ID)),
            ("a-string", "another-string"),
            (42, 43),
            (b"bytes", b"more-bytes"),
            (None, TASK_A_ID),
        ],
    )
    def test_coercible_or_foreign_scope_ids_rejected_before_any_access(
        self, portfolio_id: object, entity_id: object
    ) -> None:
        reader = SpyEstimateReader(history=())
        repository = SpyRevisionRepository()
        with pytest.raises(EffectiveExecutionEffortEstimateError):
            resolve_effective_execution_effort_estimate_durably(
                portfolio_id,  # type: ignore[arg-type]
                entity_id,  # type: ignore[arg-type]
                reader,
                repository,
            )
        assert reader.entity_calls == [], "no reader access on invalid scope"
        assert reader.portfolio_calls == []
        assert repository.provenance_calls == []
        assert repository.write_attempts == []

    def test_error_hierarchy_is_precise(self) -> None:
        assert issubclass(
            EffectiveExecutionEffortEstimateHistoryError,
            EffectiveExecutionEffortEstimateError,
        )
        assert issubclass(
            EffectiveExecutionEffortEstimateProvenanceError,
            EffectiveExecutionEffortEstimateError,
        )


# --- NO_ESTIMATE semantics ------------------------------------------------------


class TestNoEstimate:
    def test_empty_history_is_explicit_no_estimate(self) -> None:
        result, reader, repository = _resolve(())
        assert result.status is EffectiveExecutionEffortEstimateStatus.NO_ESTIMATE
        assert result.estimate is None
        assert result.calibrated_provenance is None
        assert result.portfolio_id == PORTFOLIO_ID
        assert result.entity_id == TASK_A_ID
        assert reader.entity_calls == [(PORTFOLIO_ID, TASK_A_ID)]

    def test_no_estimate_performs_zero_provenance_lookups(self) -> None:
        _, _, repository = _resolve(())
        assert repository.provenance_calls == []
        assert repository.write_attempts == []

    def test_pure_no_estimate_with_provenance_supplied_is_rejected(self) -> None:
        record = _record()
        with pytest.raises(EffectiveExecutionEffortEstimateProvenanceError):
            _resolve_pure([], calibrated_provenance=record)
        # A NO_ESTIMATE result is otherwise valid and carries nothing.
        result = _resolve_pure([])
        assert result.status is EffectiveExecutionEffortEstimateStatus.NO_ESTIMATE


# --- AVAILABLE selection semantics (durable path) --------------------------------


class TestSelection:
    def test_single_estimate_is_available_with_exact_estimate(self) -> None:
        est = _estimate(estimated_at=T1, duration_seconds=120)
        result, _, repository = _resolve([est])
        assert result.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE
        assert result.estimate == est
        assert result.estimate.entity_id == TASK_A_ID
        assert result.estimate.portfolio_id == PORTFOLIO_ID
        assert repository.write_attempts == []

    def test_later_chronological_estimate_wins(self) -> None:
        old = _estimate(duration_seconds=10, estimated_at=T0)
        new = _estimate(duration_seconds=20, estimated_at=T1)
        result, _, _ = _resolve([old, new])
        assert result.estimate is not None
        assert result.estimate.id == new.id
        assert result.estimate.duration_seconds == 20

    def test_equal_instant_greater_uuid_int_wins(self) -> None:
        low = _estimate(estimate_id=LOW_EST_ID, estimated_at=T0)
        high = _estimate(estimate_id=HIGH_EST_ID, estimated_at=T0)
        result, _, _ = _resolve([low, high])
        assert result.estimate is not None
        assert result.estimate.id == HIGH_EST_ID
        # And the same with the reversed insertion order.
        result_reversed, _, _ = _resolve([high, low])
        assert result_reversed.estimate is not None
        assert result_reversed.estimate.id == HIGH_EST_ID

    def test_mixed_timezone_offsets_compare_actual_instants(self) -> None:
        # The two revisions share the same absolute instant; tie-break by id.
        plus = _estimate(
            estimated_at=datetime(2026, 9, 1, 12, 0, tzinfo=PLUS_TWO)
        )  # 10:00Z, id=plus.id
        minus = _estimate(
            estimated_at=datetime(2026, 9, 1, 5, 0, tzinfo=MINUS_FIVE)
        )  # 10:00Z
        assert plus.estimated_at == minus.estimated_at
        true_winner = _estimate(
            estimated_at=datetime(2026, 9, 1, 11, 0, tzinfo=UTC)
        )  # 11:00Z — the true latest instant
        losing_lex = _estimate(
            estimated_at=datetime(2026, 9, 1, 12, 30, tzinfo=PLUS_TWO)
        )  # 10:30Z — ISO text "12:30+02:00" sorts AFTER "11:00+00:00"
        # 11:00Z (true_winner) is the true latest instant even though its
        # ISO/textual form sorts BEFORE losing_lex: selection must compare
        # actual instants, never ISO text.
        assert true_winner.estimated_at > losing_lex.estimated_at
        result, _, _ = _resolve([losing_lex, true_winner])
        assert result.estimate is not None
        assert result.estimate.id == true_winner.id

    def test_insertion_order_cannot_affect_selection(self) -> None:
        a = _estimate(duration_seconds=10, estimated_at=T0)
        b = _estimate(duration_seconds=20, estimated_at=T1)
        c = _estimate(duration_seconds=30, estimated_at=T2)
        first, _, _ = _resolve([c, a, b])
        second, _, _ = _resolve([a, c, b])
        assert first.estimate == second.estimate
        assert first.estimate is not None
        assert first.estimate.id == c.id

    @pytest.mark.parametrize("shuffle_seed", range(1, 5))
    def test_many_revisions_select_the_same_canonical_late_revision(
        self, shuffle_seed: int
    ) -> None:
        base = (
            _estimate(duration_seconds=1, estimated_at=T0),
            _estimate(duration_seconds=2, estimated_at=T0),
            _estimate(duration_seconds=3, estimated_at=T1),
            _estimate(duration_seconds=4, estimated_at=T1),
        )
        expected = max(
            base, key=lambda est: (est.estimated_at, est.id.int)
        )
        shuffled = tuple(random.Random(shuffle_seed).sample(base, len(base)))
        result, _, _ = _resolve(shuffled)
        assert result.estimate is not None
        assert result.estimate.id == expected.id


# --- History integrity (invalid / foreign / duplicate / hostile) ---------------
class TestHistoryIntegrity:
    def test_duplicate_estimate_ids_rejected(self) -> None:
        clone_id = uuid4()
        est = _estimate(estimate_id=clone_id, estimated_at=T0)
        dup = _estimate(estimate_id=clone_id, estimated_at=T1)
        with pytest.raises(EffectiveExecutionEffortEstimateHistoryError):
            _resolve([est, dup])

    def test_foreign_portfolio_estimate_rejected(self) -> None:
        foreign = _estimate(portfolio_id=OTHER_PORTFOLIO_ID)
        with pytest.raises(EffectiveExecutionEffortEstimateHistoryError):
            _resolve([foreign])

    def test_foreign_entity_estimate_rejected(self) -> None:
        foreign = _estimate(entity_id=TASK_B_ID)
        with pytest.raises(EffectiveExecutionEffortEstimateHistoryError):
            _resolve([foreign])

    def test_non_estimate_history_item_rejected(self) -> None:
        with pytest.raises(EffectiveExecutionEffortEstimateHistoryError):
            _resolve_pure(["not-an-estimate"])  # type: ignore[list-item]

    def test_hostile_model_construct_estimate_rejected(self) -> None:
        good = _estimate(estimated_at=T0)
        hostile = ExecutionEffortEstimate.model_construct(
            id=good.id,
            portfolio_id=good.portfolio_id,
            entity_id=good.entity_id,
            duration_seconds=100.0,  # pyright: ignore[reportCallIssue] - hostile float
            estimated_at=good.estimated_at,
            source=SourceKind.USER_CONFIRMED,
        )
        # Value-equal (100.0 == 100) yet hostile: fresh strict revalidation
        # must reject it, not value equality.
        assert hostile.duration_seconds == good.duration_seconds
        with pytest.raises(EffectiveExecutionEffortEstimateHistoryError):
            _resolve([hostile])
        with pytest.raises(EffectiveExecutionEffortEstimateHistoryError):
            _resolve_pure([hostile])

    def test_hostile_estimate_with_naive_datetime_rejected(self) -> None:
        good = _estimate(estimated_at=T0)
        hostile = ExecutionEffortEstimate.model_construct(
            id=good.id,
            portfolio_id=good.portfolio_id,
            entity_id=good.entity_id,
            duration_seconds=100,
            estimated_at=datetime(2026, 9, 1, 8, 0),  # naive
            source=SourceKind.USER_CONFIRMED,
        )
        with pytest.raises(EffectiveExecutionEffortEstimateHistoryError):
            _resolve([hostile])

    def test_pure_path_rejects_hostile_estimate_identically(self) -> None:
        good = _estimate(estimated_at=T0)
        hostile = ExecutionEffortEstimate.model_construct(
            id=good.id,
            portfolio_id=good.portfolio_id,
            entity_id=good.entity_id,
            duration_seconds=100.0,  # pyright: ignore[reportCallIssue]
            estimated_at=good.estimated_at,
            source=SourceKind.USER_CONFIRMED,
        )
        with pytest.raises(EffectiveExecutionEffortEstimateHistoryError):
            _resolve_pure([hostile])


# --- Provenance semantics --------------------------------------------------------


class TestProvenance:
    def test_ordinary_selected_estimate_without_provenance_returns_none(self) -> None:
        est = _estimate(duration_seconds=CALIBRATED, estimated_at=T1)
        result, _, repository = _resolve([est])
        assert result.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE
        assert result.estimate is not None
        assert result.estimate.id == est.id
        assert result.calibrated_provenance is None
        assert repository.provenance_calls == [est.id]

    def test_selected_calibrated_estimate_returns_exact_v121_provenance(self) -> None:
        est = _estimate(duration_seconds=CALIBRATED, estimated_at=T0)
        record = _record(estimate_id=est.id)
        assert record.estimate_id == est.id
        result, _, repository = _resolve([est], stored={est.id: record})
        assert result.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE
        assert result.estimate is not None
        assert result.estimate == est
        assert result.calibrated_provenance is not None
        assert result.calibrated_provenance.estimate_id == est.id
        assert result.calibrated_provenance.portfolio_id == PORTFOLIO_ID
        assert result.calibrated_provenance.entity_id == TASK_A_ID
        assert (
            result.calibrated_provenance.calibrated_duration_seconds
            == est.duration_seconds
        )
        assert result.calibrated_provenance.estimated_at == est.estimated_at
        assert repository.provenance_calls == [est.id]

    def test_historical_calibrated_estimate_not_preferred_over_newer_ordinary(
        self,
    ) -> None:
        # Older, calibrated, persisted provenance — but OLDER by ordering.
        old_cal = _estimate(duration_seconds=CALIBRATED, estimated_at=T0)
        record = _record(estimate_id=old_cal.id)
        newer_plain = _estimate(duration_seconds=999, estimated_at=T1)
        result, _, repository = _resolve(
            [old_cal, newer_plain], stored={old_cal.id: record}
        )
        assert result.estimate is not None
        assert result.estimate.id == newer_plain.id
        assert result.estimate.duration_seconds == 999
        # Provenance of the OLD calibrated estimate must NOT be reported.
        assert result.calibrated_provenance is None
        assert repository.provenance_calls == [newer_plain.id]
        assert old_cal.id not in repository.provenance_calls

    def test_newer_calibrated_estimate_wins_only_by_ordering(self) -> None:
        old_plain = _estimate(duration_seconds=77, estimated_at=T0)
        new_cal = _estimate(duration_seconds=CALIBRATED, estimated_at=T1)
        record = _record(estimate_id=new_cal.id, estimated_at=T1)
        result, _, repository = _resolve(
            [old_plain, new_cal], stored={new_cal.id: record}
        )
        assert result.estimate is not None
        assert result.estimate.id == new_cal.id
        assert result.calibrated_provenance is not None
        assert result.calibrated_provenance.estimate_id == new_cal.id
        # Exactly one provenance lookup, for the selected estimate only.
        assert repository.provenance_calls == [new_cal.id]
        assert old_plain.id not in repository.provenance_calls

    def test_provenance_queried_exactly_once_for_selected_estimate_only(self) -> None:
        a = _estimate(estimated_at=T0)
        b = _estimate(estimated_at=T1)
        c = _estimate(estimated_at=T2)
        selected = max((a, b, c), key=lambda est: (est.estimated_at, est.id.int))
        result, _, repository = _resolve([a, c, b])
        assert result.estimate is not None
        assert result.estimate.id == selected.id
        assert repository.provenance_calls == [selected.id]
        assert len(repository.provenance_calls) == 1
        for skipped in (a, b, c):
            if skipped.id != selected.id:
                assert skipped.id not in repository.provenance_calls

    def test_repository_returning_none_provenance_is_ordinary(self) -> None:
        est = _estimate(estimated_at=T0)
        result, _, repository = _resolve([est])
        assert result.calibrated_provenance is None
        assert repository.provenance_calls == [est.id]


class TestProvenanceHostilityAndMismatch:
    def test_hostile_model_construct_provenance_rejected(self) -> None:
        est = _estimate(duration_seconds=CALIBRATED)
        good = _record(estimate_id=est.id)
        hostile = AcceptedCalibratedEstimateRevision.model_construct(
            estimate_id=good.estimate_id,
            portfolio_id=good.portfolio_id,
            project_id=good.project_id,
            entity_id=good.entity_id,
            entity_type=good.entity_type,
            candidate_duration_seconds=300.0,  # pyright: ignore - hostile float
            calibrated_duration_seconds=good.calibrated_duration_seconds,
            estimated_at=good.estimated_at,
            source_proposal=good.source_proposal,
        )
        with pytest.raises(EffectiveExecutionEffortEstimateProvenanceError):
            _resolve([est], stored={est.id: hostile})
        with pytest.raises(EffectiveExecutionEffortEstimateProvenanceError):
            _resolve_pure([est], calibrated_provenance=hostile)

    def test_non_provenance_instance_rejected(self) -> None:
        est = _estimate(duration_seconds=CALIBRATED)
        with pytest.raises(EffectiveExecutionEffortEstimateProvenanceError):
            _resolve_pure([est], calibrated_provenance="not-a-record")
        with pytest.raises(EffectiveExecutionEffortEstimateProvenanceError):
            _resolve([est], stored={est.id: "not-a-record"})

    def test_provenance_identity_mismatch_rejected(self) -> None:
        est = _estimate(duration_seconds=CALIBRATED)
        record = _record(estimate_id=uuid4())  # references some other estimate
        with pytest.raises(EffectiveExecutionEffortEstimateProvenanceError):
            _resolve_pure([est], calibrated_provenance=record)

    def test_provenance_duration_mismatch_rejected(self) -> None:
        # Record chain is internally consistent at 450s, but the selected
        # estimate carries a different duration: cross-check must fail.
        est = _estimate(duration_seconds=CALIBRATED + 1, estimated_at=T0)
        record = _record(estimate_id=est.id)
        assert record.calibrated_duration_seconds != est.duration_seconds
        with pytest.raises(EffectiveExecutionEffortEstimateProvenanceError):
            _resolve_pure([est], calibrated_provenance=record)

    def test_provenance_timestamp_mismatch_rejected(self) -> None:
        est = _estimate(duration_seconds=CALIBRATED, estimated_at=T1)
        record = _record(estimate_id=est.id, estimated_at=T0)
        assert record.estimated_at != est.estimated_at
        with pytest.raises(EffectiveExecutionEffortEstimateProvenanceError):
            _resolve_pure([est], calibrated_provenance=record)

    def test_provenance_portfolio_entity_mismatch_rejected(self) -> None:
        # A fully internally-consistent scope-B record must not be
        # acceptable for a scope-A selected estimate.
        est = _estimate(duration_seconds=CALIBRATED)
        other_scope_record = _record(scope=1, estimate_id=est.id)
        assert other_scope_record.portfolio_id != est.portfolio_id
        assert other_scope_record.entity_id != est.entity_id
        with pytest.raises(EffectiveExecutionEffortEstimateProvenanceError):
            _resolve_pure([est], calibrated_provenance=other_scope_record)


# --- Read-only / no side-effects -------------------------------------------------


class TestReadOnlyBehavior:
    def test_resolution_never_writes_and_never_recomputes(self) -> None:
        est = _estimate(duration_seconds=CALIBRATED)
        record = _record(estimate_id=est.id)
        _, reader, repository = _resolve([est], stored={est.id: record})
        assert repository.write_attempts == []
        assert reader.portfolio_calls == []
        assert len(reader.entity_calls) == 1  # exactly one history read

    def test_deterministic_repeated_equivalent_reads(self) -> None:
        est1 = _estimate(duration_seconds=10, estimated_at=T0)
        est2 = _estimate(duration_seconds=CALIBRATED, estimated_at=T1)
        record = _record(estimate_id=est2.id, estimated_at=T1)
        results = []
        for _ in range(3):
            result, _, _ = _resolve([est1, est2], stored={est2.id: record})
            results.append(result)
        assert results[0] == results[1] == results[2]
        assert results[0].status is EffectiveExecutionEffortEstimateStatus.AVAILABLE
        assert results[0].estimate == est2
        assert results[0].calibrated_provenance is not None

    def test_no_uuid_generation_or_clock_reads_on_the_path(self, monkeypatch) -> None:
        import uuid as uuid_module

        def _explode(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("V1.22 must not generate UUIDs or read the clock")

        monkeypatch.setattr(uuid_module, "uuid4", _explode)
        monkeypatch.setattr(uuid_module, "uuid5", _explode)
        monkeypatch.setattr(uuid_module, "uuid1", _explode)

        est = _estimate()
        result, _, _ = _resolve([est])
        assert result.estimate is not None
        assert result.estimate.id == est.id
