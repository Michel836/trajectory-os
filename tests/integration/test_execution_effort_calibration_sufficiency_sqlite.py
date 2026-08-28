"""Integration test for V1.14 sufficiency over real SQLite state."""

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
from trajectory_os.application.execution_effort_calibration_profile import (
    build_effort_calibration_profile_durably,
)
from trajectory_os.application.execution_effort_calibration_sufficiency import (
    assess_effort_calibration_sufficiency_durably,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_calibration_sufficiency import (
    EffortCalibrationTypeSufficiency,
)
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("41414141-4141-4141-8141-414141414141")
PROJECT_ID = UUID("42424242-4242-4242-8242-424242424242")
TASK_A_ID = UUID("43434343-4343-4343-8343-434343434343")
TASK_B_ID = UUID("44444444-4444-4444-8444-444444444444")

ESTIMATED_AT = datetime(2025, 5, 1, 9, 0, tzinfo=UTC)
FIRST_OBSERVED_AT = datetime(2025, 5, 2, 9, 0, tzinfo=UTC)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "v114.db"


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
        name="V1.14 SQLite Portfolio",
        entities=[
            _entity(PROJECT_ID, EntityType.PROJECT),
            _entity(TASK_A_ID, EntityType.TASK),
            _entity(TASK_B_ID, EntityType.TASK),
        ],
        relations=[
            TrajectoryRelation(
                id=uuid4(),
                source_id=TASK_A_ID,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
                source=SourceKind.USER_CONFIRMED,
                confidence=1.0,
            ),
            TrajectoryRelation(
                id=uuid4(),
                source_id=TASK_B_ID,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
                source=SourceKind.USER_CONFIRMED,
                confidence=1.0,
            ),
        ],
    )


def _populate(
    estimate_repo: SqliteExecutionEffortEstimateRepository,
    observation_repo: SqliteExecutionEffortObservationRepository,
) -> None:
    # Leakage-safe pairs: estimate strictly before first observation for
    # every completed entity.
    for entity_id, (planned, actual) in {
        PROJECT_ID: (100, 130),
        TASK_A_ID: (50, 45),
        TASK_B_ID: (60, 75),
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
    assessment: object, entity_type: EntityType
) -> EffortCalibrationTypeSufficiency:
    segments = tuple(assessment.segments)  # type: ignore[attr-defined]
    matches = [segment for segment in segments if segment.entity_type == entity_type]
    assert len(matches) == 1
    return matches[0]


def test_sqlite_sufficiency_is_policy_gated_only(
    db_path: Path,
) -> None:
    portfolio = _portfolio()

    with SqlitePortfolioRepository(db_path) as portfolio_repo:
        portfolio_repo.save(portfolio)

        with (
            SqliteExecutionEffortEstimateRepository(db_path) as estimate_repo,
            SqliteExecutionEffortObservationRepository(db_path) as observation_repo,
        ):
            _populate(estimate_repo, observation_repo)

            # The SAME authoritative V1.13 evidence chain feeds both calls.
            profile = build_effort_calibration_profile_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                portfolio_repository=portfolio_repo,
                estimate_reader=estimate_repo,
                observation_reader=observation_repo,
            )

            threshold_two = assess_effort_calibration_sufficiency_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                minimum_sample_count=2,
                portfolio_repository=portfolio_repo,
                estimate_reader=estimate_repo,
                observation_reader=observation_repo,
            )

    # Sanity: authoritative V1.13 chain contains exactly one sample per
    # completed entity (leakage-safe), grouped into two type segments.
    assert profile.completed_entity_count == 3
    assert {
        (segment.entity_type, len(segment.sample_entity_ids))
        for segment in profile.segments
    } == {
        (EntityType.PROJECT, 1),
        (EntityType.TASK, 2),
    }

    # Threshold 2: only the TASK segment (2 leakage-safe samples) is
    # sufficient; the PROJECT segment (1 sample) is not.
    assert threshold_two.portfolio_id == PORTFOLIO_ID
    assert threshold_two.project_id == PROJECT_ID
    assert threshold_two.minimum_required_sample_count == 2

    task = _segment_by_type(threshold_two, EntityType.TASK)
    assert task.sample_count == 2
    assert task.minimum_required_sample_count == 2
    assert task.has_sufficient_samples is True

    project = _segment_by_type(threshold_two, EntityType.PROJECT)
    assert project.sample_count == 1
    assert project.minimum_required_sample_count == 2
    assert project.has_sufficient_samples is False

    assert threshold_two.sufficient_segment_count == 1
    assert threshold_two.insufficient_segment_count == 1
    assert (
        threshold_two.sufficient_segment_count
        + threshold_two.insufficient_segment_count
        == len(threshold_two.segments)
    )

    # No statistical fields, correction factors, or summaries are invented.
    assert not any(
        "total" in name or "variance" in name or "error" in name
        for name in threshold_two.model_fields
    )


def test_sqlite_sufficiency_policy_is_explicit_and_changes_with_threshold(
    db_path: Path,
) -> None:
    portfolio = _portfolio()

    with SqlitePortfolioRepository(db_path) as portfolio_repo:
        portfolio_repo.save(portfolio)

        with (
            SqliteExecutionEffortEstimateRepository(db_path) as estimate_repo,
            SqliteExecutionEffortObservationRepository(db_path) as observation_repo,
        ):
            _populate(estimate_repo, observation_repo)

            one = assess_effort_calibration_sufficiency_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                minimum_sample_count=1,
                portfolio_repository=portfolio_repo,
                estimate_reader=estimate_repo,
                observation_reader=observation_repo,
            )
            three = assess_effort_calibration_sufficiency_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                minimum_sample_count=3,
                portfolio_repository=portfolio_repo,
                estimate_reader=estimate_repo,
                observation_reader=observation_repo,
            )

    # Same authoritative evidence, different explicit policy:
    # threshold 1 → both segments sufficient; threshold 3 → none.
    assert one.sufficient_segment_count == 2
    assert one.insufficient_segment_count == 0
    assert all(
        segment.has_sufficient_samples for segment in one.segments
    )

    assert three.sufficient_segment_count == 0
    assert three.insufficient_segment_count == 2
    assert not any(
        segment.has_sufficient_samples for segment in three.segments
    )

    # Sample counts are copied exactly from the authoritative V1.13 profile
    # regardless of the policy threshold.
    assert {
        segment.sample_count for segment in one.segments
    } == {1, 2}
    assert {
        segment.sample_count for segment in three.segments
    } == {1, 2}
