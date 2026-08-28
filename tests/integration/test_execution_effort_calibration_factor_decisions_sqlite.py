"""Integration test for V1.16 human decisions over real SQLite state.

Covers: exact-snapshot roundtrip, timezone/offset preservation and true
instant ordering, append-once duplicate rejection, history stability under
later V1.15 input drift, empty history as ``()``, no update/delete API,
pre-append rejection failures (invalid decision rule, missing segment),
and reader independence from the V1.15 derivation boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from trajectory_os.adapters.persistence import (
    SqliteExecutionEffortCalibrationFactorDecisionRepository,
    SqliteExecutionEffortEstimateRepository,
    SqliteExecutionEffortObservationRepository,
    SqlitePortfolioRepository,
)
from trajectory_os.adapters.persistence.sqlite_execution_effort_calibration_factor_decisions import (  # noqa: E501
    DuplicateEffortCalibrationFactorDecisionError,
)
from trajectory_os.application import (
    execution_effort_calibration_factor_decisions as decisions_app,
)
from trajectory_os.application.execution_effort_calibration_factor_decisions import (
    DurableEffortCalibrationFactorDecisionError,
    EffortCalibrationFactorDecisionRejectedForSegmentError,
    EffortCalibrationFactorDecisionSegmentNotFoundError,
    record_effort_calibration_factor_decision,
)
from trajectory_os.application.execution_effort_calibration_factor_proposals import (
    DurableEffortCalibrationFactorProposalError,
    build_effort_calibration_factor_proposals_durably,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_calibration_factor_decisions import (
    EffortCalibrationDecision,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
)
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("71717171-7171-4171-8171-717171717171")
PROJECT_ID = UUID("72727272-7272-4272-8272-727272727272")
TASK_A_ID = UUID("73737373-7373-4373-8373-737373737373")
TASK_B_ID = UUID("74747474-7474-4474-8474-747474747474")
DELIVERABLE_ID = UUID("75757575-7575-4575-8575-757575757575")
ANOTHER_PROJECT_ID = UUID("76767676-7676-4676-8676-767676767676")

ESTIMATED_AT = datetime(2025, 6, 1, 9, 0, tzinfo=UTC)
FIRST_OBSERVED_AT = datetime(2025, 6, 2, 9, 0, tzinfo=UTC)

DECISION_ID_1 = UUID("77777777-7777-4777-8777-777777777777")
DECISION_ID_2 = UUID("78787878-7878-4878-9878-787878787878")
DECISION_ID_3 = UUID("79797979-7979-4979-8979-797979797979")
DECISION_ID_4 = UUID("7a7a7a7a-7a7a-4a7a-8a7a-7a7a7a7a7a7a")
DECISION_ID_5 = UUID("7b7b7b7b-7b7b-4b7b-8b7b-7b7b7b7b7b7b")
DECISION_ID_6 = UUID("7c7c7c7c-7c7c-4c7c-8c7c-7c7c7c7c7c7c")


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "v116.db"


def _entity(entity_id: UUID, entity_type: EntityType) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id,
        entity_type=entity_type,
        title=f"Entity {str(entity_id)[:8]}",
        status=EntityStatus.COMPLETED,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=ESTIMATED_AT,
        updated_at=ESTIMATED_AT,
    )


def _portfolio() -> Portfolio:
    return Portfolio(
        id=PORTFOLIO_ID,
        name="V1.16 SQLite Portfolio",
        entities=[
            _entity(PROJECT_ID, EntityType.PROJECT),
            _entity(TASK_A_ID, EntityType.TASK),
            _entity(TASK_B_ID, EntityType.TASK),
            _entity(DELIVERABLE_ID, EntityType.DELIVERABLE),
        ],
        relations=[
            TrajectoryRelation(
                id=uuid4(),
                source_id=entity_id,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
                source=SourceKind.USER_CONFIRMED,
                confidence=1.0,
            )
            for entity_id in (
                TASK_A_ID,
                TASK_B_ID,
                DELIVERABLE_ID,
            )
        ],
    )


def _populate(
    estimate_repo: SqliteExecutionEffortEstimateRepository,
    observation_repo: SqliteExecutionEffortObservationRepository,
) -> None:
    # TASK segment: planned 200 / actual 150 -> 3/4 AVAILABLE (min 1).
    # PROJECT segment: planned 100 / actual 150 -> 3/2 AVAILABLE (min 1).
    # DELIVERABLE segment: planned 0 / actual 5 ->
    # ZERO_TOTAL_PLANNED_DURATION even at min 1.
    for entity_id, (planned, actual) in {
        PROJECT_ID: (100, 150),
        TASK_A_ID: (100, 100),
        TASK_B_ID: (100, 50),
        DELIVERABLE_ID: (0, 5),
    }.items():
        estimate_repo.add(
            ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=entity_id,
                duration_seconds=planned,
                estimated_at=ESTIMATED_AT,
                source=SourceKind.USER_CONFIRMED,
            )
        )
        observation_repo.add(
            ExecutionEffortObservation(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=entity_id,
                duration_seconds=actual,
                observed_at=FIRST_OBSERVED_AT,
                source=SourceKind.USER_CONFIRMED,
            )
        )


@pytest.fixture()
def repos(db_path: Path) -> SimpleNamespace:
    with (
        SqlitePortfolioRepository(db_path) as portfolio_repo,
        SqliteExecutionEffortEstimateRepository(db_path) as estimate_repo,
        SqliteExecutionEffortObservationRepository(
            db_path
        ) as observation_repo,
        SqliteExecutionEffortCalibrationFactorDecisionRepository(
            db_path
        ) as decision_repo,
    ):
        portfolio_repo.save(_portfolio())
        _populate(estimate_repo, observation_repo)
        yield SimpleNamespace(
            portfolio=portfolio_repo,
            estimates=estimate_repo,
            observations=observation_repo,
            decisions=decision_repo,
        )


def _record(
    repos: SimpleNamespace,
    *,
    entity_type: EntityType,
    decision: EffortCalibrationDecision,
    decision_id: UUID,
    decided_at: datetime,
    minimum_sample_count: int = 1,
) -> object:
    return record_effort_calibration_factor_decision(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=entity_type,
        minimum_sample_count=minimum_sample_count,
        decision=decision,
        decision_id=decision_id,
        decided_at=decided_at,
        portfolio_repository=repos.portfolio,
        estimate_reader=repos.estimates,
        observation_reader=repos.observations,
        decision_repository=repos.decisions,
    )


def test_sqlite_accept_decision_roundtrips_the_exact_v115_snapshot(
    repos: SimpleNamespace,
) -> None:
    decided_at = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
    record = _record(
        repos,
        entity_type=EntityType.TASK,
        decision=EffortCalibrationDecision.ACCEPT,
        decision_id=DECISION_ID_1,
        decided_at=decided_at,
    )

    history = repos.decisions.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK
    )
    assert len(history) == 1
    stored = history[0]

    # The roundtripped record is exactly the returned record: no
    # re-derivation, no replacement.
    assert stored.model_dump(mode="python") == record.model_dump(mode="python")

    # Exact V1.15 snapshot, copied at decision time.
    assert stored.decision_id == DECISION_ID_1
    assert stored.portfolio_id == PORTFOLIO_ID
    assert stored.project_id == PROJECT_ID
    assert stored.entity_type == EntityType.TASK
    assert stored.sample_count == 2
    assert stored.minimum_required_sample_count == 1
    assert stored.total_planned_duration_seconds == 200
    assert stored.total_actual_duration_seconds == 150
    assert stored.proposal_available is True
    assert (
        stored.proposal_reason
        is EffortCalibrationFactorProposalReason.AVAILABLE
    )
    assert stored.factor_numerator == 3
    assert stored.factor_denominator == 4
    assert type(stored.factor_numerator) is int
    assert type(stored.factor_denominator) is int

    # Human decision identity and the caller-supplied exact timestamp.
    assert stored.decision is EffortCalibrationDecision.ACCEPT
    assert stored.decided_at == decided_at


def test_sqlite_preserves_explicit_offsets_and_orders_by_instant(
    repos: SimpleNamespace,
) -> None:
    plus_two = timezone(timedelta(hours=2))
    earlier_instant = datetime(2025, 7, 1, 10, 30, tzinfo=plus_two)  # 08:30Z
    later_instant = datetime(2025, 7, 1, 9, 0, tzinfo=UTC)  # 09:00Z

    # Insert in the OPPOSITE order of chronological instant.
    _record(
        repos,
        entity_type=EntityType.PROJECT,
        decision=EffortCalibrationDecision.DEFER,
        decision_id=DECISION_ID_1,
        decided_at=later_instant,
    )
    _record(
        repos,
        entity_type=EntityType.PROJECT,
        decision=EffortCalibrationDecision.REJECT,
        decision_id=DECISION_ID_2,
        decided_at=earlier_instant,
    )

    history = repos.decisions.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.PROJECT
    )
    assert [record.decision_id for record in history] == [
        DECISION_ID_2,  # 08:30Z instant first, even though inserted second
        DECISION_ID_1,
    ]

    # Offsets are preserved exactly, not silently normalized to UTC.
    assert history[0].decided_at == earlier_instant
    assert history[0].decided_at.utcoffset() == timedelta(hours=2)
    assert history[1].decided_at == later_instant


def test_sqlite_history_orders_by_decided_at_then_uuid_instant(
    repos: SimpleNamespace,
) -> None:
    t_late = datetime(2025, 7, 3, 0, 0, tzinfo=UTC)
    t_early = datetime(2025, 7, 1, 0, 0, tzinfo=UTC)
    t_mid = datetime(2025, 7, 2, 0, 0, tzinfo=UTC)

    # Insert deliberately out of chronological order.
    for decision_id, decided_at in (
        (DECISION_ID_3, t_late),
        (DECISION_ID_1, t_early),
        (DECISION_ID_2, t_mid),
    ):
        _record(
            repos,
            entity_type=EntityType.DELIVERABLE,
            decision=EffortCalibrationDecision.DEFER,
            decision_id=decision_id,
            decided_at=decided_at,
        )

    history = repos.decisions.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.DELIVERABLE
    )
    assert [record.decision_id for record in history] == [
        DECISION_ID_1,
        DECISION_ID_2,
        DECISION_ID_3,
    ]

    # Same-instant records order deterministically by UUID instant.
    same = datetime(2025, 7, 5, 0, 0, tzinfo=UTC)
    low_uuid = DECISION_ID_5
    high_uuid = DECISION_ID_6
    _record(
        repos,
        entity_type=EntityType.TASK,
        decision=EffortCalibrationDecision.DEFER,
        decision_id=high_uuid,
        decided_at=same,
    )
    _record(
        repos,
        entity_type=EntityType.TASK,
        decision=EffortCalibrationDecision.DEFER,
        decision_id=low_uuid,
        decided_at=same,
    )
    task_history = repos.decisions.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK
    )
    same_instant = [
        record for record in task_history if record.decided_at == same
    ]
    assert [record.decision_id for record in same_instant] == [
        low_uuid,
        high_uuid,
    ]


def test_sqlite_duplicate_decision_id_is_rejected_and_existing_untouched(
    repos: SimpleNamespace,
) -> None:
    first_at = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
    initial = _record(
        repos,
        entity_type=EntityType.TASK,
        decision=EffortCalibrationDecision.ACCEPT,
        decision_id=DECISION_ID_1,
        decided_at=first_at,
    )

    with pytest.raises(DuplicateEffortCalibrationFactorDecisionError):
        _record(
            repos,
            entity_type=EntityType.TASK,  # same id, same scope
            decision=EffortCalibrationDecision.ACCEPT,
            decision_id=DECISION_ID_1,
            decided_at=datetime(2025, 7, 2, 8, 30, tzinfo=UTC),  # later ts
        )

    history = repos.decisions.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK
    )
    assert len(history) == 1
    assert (
        history[0].model_dump(mode="python")
        == initial.model_dump(mode="python")
    )
    assert history[0].decided_at == first_at


def test_sqlite_empty_history_returns_empty_tuple(
    repos: SimpleNamespace,
) -> None:
    assert (
        repos.decisions.list_history(
            PORTFOLIO_ID, PROJECT_ID, EntityType.WORK_PACKAGE
        )
        == ()
    )
    assert (
        repos.decisions.list_history(
            ANOTHER_PROJECT_ID, PROJECT_ID, EntityType.TASK
        )
        == ()
    )


def test_sqlite_failing_paths_append_nothing(repos: SimpleNamespace) -> None:
    decided_at = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)

    # ACCEPT over the ZERO_TOTAL_PLANNED_DURATION DELIVERABLE segment is
    # invalid for that exact V1.15 proposal.
    with pytest.raises(EffortCalibrationFactorDecisionRejectedForSegmentError):
        _record(
            repos,
            entity_type=EntityType.DELIVERABLE,
            decision=EffortCalibrationDecision.ACCEPT,
            decision_id=DECISION_ID_1,
            decided_at=decided_at,
        )

    # REQUESTING a segment that has no V1.15 proposal at all.
    with pytest.raises(EffortCalibrationFactorDecisionSegmentNotFoundError):
        _record(
            repos,
            entity_type=EntityType.WORK_PACKAGE,
            decision=EffortCalibrationDecision.REJECT,
            decision_id=DECISION_ID_2,
            decided_at=decided_at,
        )

    # Strict boundary failure (invalid minimum_sample_count) fails before
    # ANY I/O.
    with pytest.raises(DurableEffortCalibrationFactorDecisionError):
        _record(
            repos,
            entity_type=EntityType.TASK,
            decision=EffortCalibrationDecision.ACCEPT,
            decision_id=DECISION_ID_3,
            decided_at=decided_at,
            minimum_sample_count=0,
        )

    # Nothing was appended for any scope.
    for entity_type in (
        EntityType.TASK,
        EntityType.DELIVERABLE,
        EntityType.WORK_PACKAGE,
        EntityType.PROJECT,
    ):
        assert (
            repos.decisions.list_history(PORTFOLIO_ID, PROJECT_ID, entity_type)
            == ()
        )


def test_sqlite_recorded_history_survives_later_v115_drift(
    repos: SimpleNamespace,
) -> None:
    first_at = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
    initial = _record(
        repos,
        entity_type=EntityType.TASK,
        decision=EffortCalibrationDecision.ACCEPT,
        decision_id=DECISION_ID_1,
        decided_at=first_at,
    )

    # Drift: a NEW estimate (latest per entity) strictly between the
    # estimate and the first observation changes the CURRENT V1.15 TASK
    # segment (planned 200 -> 250, factor 3/4 -> 3/5).
    repos.estimates.add(
        ExecutionEffortEstimate(
            id=uuid4(),
            portfolio_id=PORTFOLIO_ID,
            entity_id=TASK_B_ID,
            duration_seconds=150,
            estimated_at=datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
            source=SourceKind.USER_CONFIRMED,
        )
    )
    derived = build_effort_calibration_factor_proposals_durably(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        minimum_sample_count=1,
        portfolio_repository=repos.portfolio,
        estimate_reader=repos.estimates,
        observation_reader=repos.observations,
    )
    current_task = next(
        segment
        for segment in derived.segments
        if segment.entity_type == EntityType.TASK
    )
    assert current_task.total_planned_duration_seconds == 250
    assert current_task.factor_numerator == 3
    assert current_task.factor_denominator == 5

    # The recorded decision is a SNAPSHotted record, not a pointer: the
    # durable history is unchanged by the later V1.15 drift.
    history = repos.decisions.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK
    )
    assert len(history) == 1
    assert (
        history[0].model_dump(mode="python")
        == initial.model_dump(mode="python")
    )
    assert history[0].total_planned_duration_seconds == 200
    assert history[0].factor_numerator == 3
    assert history[0].factor_denominator == 4


def test_sqlite_repository_exposes_no_update_or_delete_api(
    repos: SimpleNamespace,
) -> None:
    public = {
        name
        for name in dir(type(repos.decisions))
        if not name.startswith("_")
    }
    assert not (
        {"update", "replace", "upsert", "delete", "remove", "patch"}
        & public
    )
    assert {"add", "list_history"} <= public


def test_sqlite_history_reader_never_rederives_v115(
    repos: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(
        repos,
        entity_type=EntityType.TASK,
        decision=EffortCalibrationDecision.ACCEPT,
        decision_id=DECISION_ID_1,
        decided_at=datetime(2025, 7, 1, 8, 30, tzinfo=UTC),
    )

    def _explode(**_kwargs: object) -> object:
        raise DurableEffortCalibrationFactorProposalError(
            "the decision history reader must never derive V1.15"
        )

    monkeypatch.setattr(
        decisions_app,
        "build_effort_calibration_factor_proposals_durably",
        _explode,
    )

    first = repos.decisions.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK
    )
    second = repos.decisions.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK
    )
    assert len(first) == 1
    assert first == second
    assert (
        first[0].model_dump(mode="python")
        == second[0].model_dump(mode="python")
    )
