"""Integration test for V1.13 calibration profiles over real SQLite state."""

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
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("31313131-3131-4313-8313-313131313131")
PROJECT_ID = UUID("32323232-3232-4323-8323-323232323232")
TASK_ID = UUID("33333333-3333-4333-8333-333333333333")

ESTIMATED_AT = datetime(2025, 3, 1, 9, 0, tzinfo=UTC)
FIRST_OBSERVED_AT = datetime(2025, 3, 2, 9, 0, tzinfo=UTC)
LATE_REVISION_AT = datetime(2025, 3, 3, 9, 0, tzinfo=UTC)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "v113.db"


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
        name="V1.13 SQLite Portfolio",
        entities=[
            _entity(PROJECT_ID, EntityType.PROJECT),
            _entity(TASK_ID, EntityType.TASK),
        ],
        relations=[
            TrajectoryRelation(
                id=uuid4(),
                source_id=TASK_ID,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
                source=SourceKind.USER_CONFIRMED,
                confidence=1.0,
            )
        ],
    )


def test_sqlite_composition_produces_exact_current_type_profile(db_path: Path) -> None:
    portfolio = _portfolio()

    with SqlitePortfolioRepository(db_path) as portfolio_repo:
        portfolio_repo.save(portfolio)

        with (
            SqliteExecutionEffortEstimateRepository(db_path) as estimate_repo,
            SqliteExecutionEffortObservationRepository(db_path) as observation_repo,
        ):
            project_estimate = ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=PROJECT_ID,
                duration_seconds=100,
                estimated_at=ESTIMATED_AT,
                source=SourceKind.USER_CONFIRMED,
            )
            task_estimate = ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_ID,
                duration_seconds=50,
                estimated_at=ESTIMATED_AT,
                source=SourceKind.USER_CONFIRMED,
            )
            estimate_repo.add(project_estimate)
            estimate_repo.add(task_estimate)

            estimate_repo.add(
                ExecutionEffortEstimate(
                    id=uuid4(),
                    portfolio_id=PORTFOLIO_ID,
                    entity_id=TASK_ID,
                    duration_seconds=1,
                    estimated_at=LATE_REVISION_AT,
                    source=SourceKind.USER_CONFIRMED,
                )
            )

            observation_repo.add(
                ExecutionEffortObservation(
                    id=uuid4(),
                    portfolio_id=PORTFOLIO_ID,
                    entity_id=PROJECT_ID,
                    duration_seconds=130,
                    observed_at=FIRST_OBSERVED_AT,
                    source=SourceKind.USER_CONFIRMED,
                )
            )
            observation_repo.add(
                ExecutionEffortObservation(
                    id=uuid4(),
                    portfolio_id=PORTFOLIO_ID,
                    entity_id=TASK_ID,
                    duration_seconds=40,
                    observed_at=FIRST_OBSERVED_AT,
                    source=SourceKind.USER_CONFIRMED,
                )
            )

            result = build_effort_calibration_profile_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                portfolio_repository=portfolio_repo,
                estimate_reader=estimate_repo,
                observation_reader=observation_repo,
            )

    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_id == PROJECT_ID
    assert result.completed_entity_count == 2
    assert result.completed_without_observation_count == 0
    assert result.completed_without_prior_estimate_count == 0

    assert [segment.entity_type for segment in result.segments] == [
        EntityType.PROJECT,
        EntityType.TASK,
    ]
    assert result.segments[0].sample_entity_ids == (PROJECT_ID,)
    assert result.segments[1].sample_entity_ids == (TASK_ID,)

    project = result.segments[0].summary
    assert project.sample_count == 1
    assert project.total_planned_duration_seconds == 100
    assert project.total_actual_duration_seconds == 130
    assert project.signed_variance_seconds == 30
    assert project.absolute_error_seconds == 30
    assert project.underplanned_entity_count == 1
    assert project.exact_entity_count == 0
    assert project.overplanned_entity_count == 0

    task = result.segments[1].summary
    assert task.sample_count == 1
    assert task.total_planned_duration_seconds == 50
    assert task.total_actual_duration_seconds == 40
    assert task.signed_variance_seconds == -10
    assert task.absolute_error_seconds == 10
    assert task.underplanned_entity_count == 0
    assert task.exact_entity_count == 0
    assert task.overplanned_entity_count == 1

    overall = result.overall_summary
    assert overall.sample_count == 2
    assert overall.total_planned_duration_seconds == 150
    assert overall.total_actual_duration_seconds == 170
    assert overall.signed_variance_seconds == 20
    assert overall.absolute_error_seconds == 40
    assert overall.underplanned_entity_count == 1
    assert overall.exact_entity_count == 0
    assert overall.overplanned_entity_count == 1
