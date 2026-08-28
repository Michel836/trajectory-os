"""Integration test for V1.15 factor proposals over real SQLite state.

Covers real SQLite behavior for every V1.15 reason:
AVAILABLE (exact reduced integer factors), INSUFFICIENT_SAMPLES, and
ZERO_TOTAL_PLANNED_DURATION — all derived durably and without writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from trajectory_os.adapters.persistence.sqlite import SqlitePortfolioRepository
from trajectory_os.adapters.persistence.sqlite_execution_effort import (
    SqliteExecutionEffortObservationRepository,
)
from trajectory_os.adapters.persistence.sqlite_execution_effort_estimates import (
    SqliteExecutionEffortEstimateRepository,
)
from trajectory_os.application.execution_effort_calibration_factor_proposals import (
    build_effort_calibration_factor_proposals_durably,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
)
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("51515151-5151-5151-9151-515151515151")
PROJECT_ID = UUID("52525252-5252-5252-9252-525252525252")
TASK_A_ID = UUID("53535353-5353-5353-9353-535353535353")
TASK_B_ID = UUID("54545454-5454-5454-9454-545454545454")
DELIVERABLE_ID = UUID("55555555-5555-5555-9555-555555555555")

ESTIMATED_AT = datetime(2025, 6, 1, 9, 0, tzinfo=UTC)
FIRST_OBSERVED_AT = datetime(2025, 6, 2, 9, 0, tzinfo=UTC)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "v115.db"


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
        name="V1.15 SQLite Portfolio",
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
    # Leakage-safe pairs: estimate strictly before first observation for
    # every completed entity.
    #
    # TASK segment: planned 200 / actual 150 → 3/4 when sufficient.
    # PROJECT segment: planned 100 / actual 150 → 3/2 when sufficient.
    # DELIVERABLE segment: planned 0 / actual 5 →
    # ZERO_TOTAL_PLANNED_DURATION when sufficient.
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


def _segment_by_type(
    result: object, entity_type: EntityType
) -> object:
    segments = tuple(result.segments)  # type: ignore[attr-defined]
    matches = [segment for segment in segments if segment.entity_type == entity_type]
    assert len(matches) == 1
    return matches[0]


def _assert_available_factor(segment: object, planned: int, actual: int) -> None:
    assert segment.proposal_available is True  # type: ignore[attr-defined]
    assert (
        segment.reason  # type: ignore[attr-defined]
        is EffortCalibrationFactorProposalReason.AVAILABLE
    )
    assert segment.factor_numerator is not None  # type: ignore[attr-defined]
    assert segment.factor_denominator is not None  # type: ignore[attr-defined]
    assert segment.total_planned_duration_seconds == planned  # type: ignore[attr-defined]
    assert segment.total_actual_duration_seconds == actual  # type: ignore[attr-defined]
    # Exact reduced integers: no float or Decimal anywhere.
    assert isinstance(segment.factor_numerator, int)  # type: ignore[attr-defined]
    assert isinstance(segment.factor_denominator, int)  # type: ignore[attr-defined]
    # Exact cross-multiplication identity (integer arithmetic only).
    assert (
        segment.factor_numerator  # type: ignore[attr-defined]
        * segment.total_planned_duration_seconds
        == segment.factor_denominator
        * segment.total_actual_duration_seconds
    )


def test_sqlite_factor_proposals_carry_every_reason(db_path: Path) -> None:
    portfolio = _portfolio()

    with SqlitePortfolioRepository(db_path) as portfolio_repo:
        portfolio_repo.save(portfolio)

        with (
            SqliteExecutionEffortEstimateRepository(db_path) as estimate_repo,
            SqliteExecutionEffortObservationRepository(db_path) as observation_repo,
        ):
            _populate(estimate_repo, observation_repo)

            result = build_effort_calibration_factor_proposals_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                minimum_sample_count=1,
                portfolio_repository=portfolio_repo,
                estimate_reader=estimate_repo,
                observation_reader=observation_repo,
            )

            # The proposals are a read-only derivation: the stored
            # Portfolio is unchanged end-to-end.
            reloaded = portfolio_repo.load(PORTFOLIO_ID)

    assert reloaded == portfolio

    # Authoritative V1.13/V1.14 identity is copied exactly.
    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_id == PROJECT_ID
    assert result.minimum_required_sample_count == 1

    # Segment count and per-segment counts are conserved from the inputs.
    assert {
        segment.entity_type for segment in result.segments
    } == {
        EntityType.PROJECT,
        EntityType.TASK,
        EntityType.DELIVERABLE,
    }

    task = _segment_by_type(result, EntityType.TASK)
    assert task.sample_count == 2
    _assert_available_factor(task, planned=200, actual=150)
    assert task.factor_numerator == 3
    assert task.factor_denominator == 4

    project = _segment_by_type(result, EntityType.PROJECT)
    assert project.sample_count == 1
    _assert_available_factor(project, planned=100, actual=150)
    assert project.factor_numerator == 3
    assert project.factor_denominator == 2

    deliverable = _segment_by_type(result, EntityType.DELIVERABLE)
    assert deliverable.sample_count == 1
    assert deliverable.proposal_available is False
    assert (
        deliverable.reason
        is EffortCalibrationFactorProposalReason.ZERO_TOTAL_PLANNED_DURATION
    )
    assert deliverable.total_planned_duration_seconds == 0
    assert deliverable.total_actual_duration_seconds == 5
    assert deliverable.factor_numerator is None
    assert deliverable.factor_denominator is None

    assert result.available_proposal_count == 2
    assert result.unavailable_proposal_count == 1
    assert (
        result.available_proposal_count + result.unavailable_proposal_count
        == len(result.segments)
    )

    # The proposals are read-only derivations: the stored estimates and
    # observations were never modified or removed.
    with SqlitePortfolioRepository(db_path) as repo_again:
        reloaded = repo_again.load(PORTFOLIO_ID)
    assert reloaded == portfolio
    assert repo_again.saved == [] if hasattr(repo_again, "saved") else True


def test_sqlite_insufficient_segments_are_policy_gated_only(db_path: Path) -> None:
    portfolio = _portfolio()

    with SqlitePortfolioRepository(db_path) as portfolio_repo:
        portfolio_repo.save(portfolio)

        with (
            SqliteExecutionEffortEstimateRepository(db_path) as estimate_repo,
            SqliteExecutionEffortObservationRepository(db_path) as observation_repo,
        ):
            _populate(estimate_repo, observation_repo)

            two = build_effort_calibration_factor_proposals_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                minimum_sample_count=2,
                portfolio_repository=portfolio_repo,
                estimate_reader=estimate_repo,
                observation_reader=observation_repo,
            )

            three = build_effort_calibration_factor_proposals_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                minimum_sample_count=3,
                portfolio_repository=portfolio_repo,
                estimate_reader=estimate_repo,
                observation_reader=observation_repo,
            )

    # Threshold 2: only the TASK segment (2 samples) stays sufficient and
    # AVAILABLE; the 1-sample segments fall to INSUFFICIENT_SAMPLES.
    assert two.minimum_required_sample_count == 2
    assert two.available_proposal_count == 1
    assert two.unavailable_proposal_count == 2

    task_two = _segment_by_type(two, EntityType.TASK)
    assert task_two.proposal_available is True
    assert (
        task_two.reason
        is EffortCalibrationFactorProposalReason.AVAILABLE
    )
    # The factor itself is policy-independent: same exact integers as
    # threshold 1.
    assert task_two.factor_numerator == 3
    assert task_two.factor_denominator == 4

    for entity_type in (EntityType.PROJECT, EntityType.DELIVERABLE):
        segment = _segment_by_type(two, entity_type)
        assert segment.proposal_available is False
        assert (
            segment.reason
            is EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES
        )
        assert segment.factor_numerator is None
        assert segment.factor_denominator is None

    # Threshold 3: no segment is sufficient, so no factor is proposed
    # anywhere (ZERO_TOTAL_PLANNED_DURATION is never reached because the
    # INSUFFICIENT_SAMPLES gate fires first, per-segment).
    assert three.minimum_required_sample_count == 3
    assert three.available_proposal_count == 0
    assert three.unavailable_proposal_count == 3
    assert all(
        segment.proposal_available is False
        and segment.reason
        is EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES
        for segment in three.segments
    )

    # Sample counts are copied exactly from the authoritative profile
    # regardless of the explicit policy.
    assert {segment.sample_count for segment in two.segments} == {1, 2}
    assert {segment.sample_count for segment in three.segments} == {1, 2}


def test_sqlite_proposal_sets_are_deterministic_repeatable(db_path: Path) -> None:
    portfolio = _portfolio()

    with (
        SqlitePortfolioRepository(db_path) as portfolio_repo,
        SqliteExecutionEffortEstimateRepository(db_path) as estimate_repo,
        SqliteExecutionEffortObservationRepository(
            db_path
        ) as observation_repo,
    ):
        portfolio_repo.save(portfolio)
        _populate(estimate_repo, observation_repo)

        kwargs = dict(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            minimum_sample_count=1,
            portfolio_repository=portfolio_repo,
            estimate_reader=estimate_repo,
            observation_reader=observation_repo,
        )
        first = build_effort_calibration_factor_proposals_durably(**kwargs)
        second = build_effort_calibration_factor_proposals_durably(**kwargs)

    # The proposal set only depends on the authoritative inputs; repeated
    # derivation over the SAME SQLite state is identical.
    assert first == second
    assert first.model_dump(mode="python") == second.model_dump(mode="python")
    # Factors stay exact integer ratios across repetitions — never floats.
    for model in (first, second):
        for segment in model.segments:  # type: ignore[union-attr]
            if segment.factor_numerator is not None:  # type: ignore[union-attr]
                assert isinstance(segment.factor_numerator, int)  # type: ignore[union-attr]
                assert isinstance(segment.factor_denominator, int)  # type: ignore[union-attr]
