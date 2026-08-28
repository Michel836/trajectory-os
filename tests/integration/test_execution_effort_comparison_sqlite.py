"""Integration test: V1.11 comparison over real SQLite persistence."""

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
from trajectory_os.application.execution_effort_comparison import (
    compare_work_breakdown_effort_durably,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
PROJECT_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
TASK_A_ID = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
TASK_B_ID = UUID("00000000-0000-0000-0000-000000000001")

T0 = datetime(2024, 3, 1, 10, 0, 0, tzinfo=UTC)
T1 = datetime(2024, 3, 2, 10, 0, 0, tzinfo=UTC)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _build_portfolio() -> Portfolio:
    project = TrajectoryEntity(
        id=PROJECT_ID,
        entity_type=EntityType.PROJECT,
        title="Integration Project",
        description="",
        status=EntityStatus.ACTIVE,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=T0,
        updated_at=T0,
    )
    task_a = TrajectoryEntity(
        id=TASK_A_ID,
        entity_type=EntityType.TASK,
        title="Task A",
        description="",
        status=EntityStatus.ACTIVE,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=T0,
        updated_at=T0,
    )
    task_b = TrajectoryEntity(
        id=TASK_B_ID,
        entity_type=EntityType.TASK,
        title="Task B",
        description="",
        status=EntityStatus.ACTIVE,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=T0,
        updated_at=T0,
    )
    rel_a = TrajectoryRelation(
        id=uuid4(),
        source_id=TASK_A_ID,
        target_id=PROJECT_ID,
        relation_type=RelationType.BELONGS_TO,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
    )
    rel_b = TrajectoryRelation(
        id=uuid4(),
        source_id=TASK_B_ID,
        target_id=PROJECT_ID,
        relation_type=RelationType.BELONGS_TO,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
    )
    return Portfolio(
        id=PORTFOLIO_ID,
        name="Integration Portfolio",
        entities=[project, task_a, task_b],
        relations=[rel_a, rel_b],
    )


def test_full_sqlite_comparison(db_path: Path) -> None:
    """End-to-end: persist portfolio + estimates + observations, then compare."""

    portfolio = _build_portfolio()

    with SqlitePortfolioRepository(db_path) as pf_repo:
        pf_repo.save(portfolio)

        with (
            SqliteExecutionEffortEstimateRepository(db_path) as est_repo,
            SqliteExecutionEffortObservationRepository(db_path) as obs_repo,
        ):
            # Plan: project = 0s, task_a = 100s, task_b = 200s.
            # The explicit project zero makes the complete 3-node WBS estimated.
            est_project = ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=PROJECT_ID,
                duration_seconds=0,
                estimated_at=T0,
                source=SourceKind.USER_CONFIRMED,
            )
            est_repo.add(est_project)

            est_a = ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ID,
                duration_seconds=100,
                estimated_at=T0,
                source=SourceKind.USER_CONFIRMED,
            )
            est_b = ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_B_ID,
                duration_seconds=200,
                estimated_at=T0,
                source=SourceKind.USER_CONFIRMED,
            )
            est_repo.add(est_a)
            est_repo.add(est_b)

            # Actual: task_a = 120s, task_b = 180s
            obs_a = ExecutionEffortObservation(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ID,
                duration_seconds=120,
                observed_at=T1,
                source=SourceKind.USER_CONFIRMED,
            )
            obs_b = ExecutionEffortObservation(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_B_ID,
                duration_seconds=180,
                observed_at=T1,
                source=SourceKind.USER_CONFIRMED,
            )
            obs_repo.add(obs_a)
            obs_repo.add(obs_b)

            result = compare_work_breakdown_effort_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                portfolio_repository=pf_repo,
                estimate_reader=est_repo,
                observation_reader=obs_repo,
            )

    # Verify structure
    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_id == PROJECT_ID
    assert len(result.items) == 3

    # Root (project)
    root = result.items[0]
    assert root.entity_id == PROJECT_ID
    assert root.parent_id is None
    assert root.depth == 0
    # Subtree: planned 300, actual 300 → variance 0
    assert root.subtree.planned_duration_seconds == 300
    assert root.subtree.actual_duration_seconds == 300
    assert root.subtree.variance_seconds == 0
    assert root.planned_estimated_entity_count == 3
    assert root.planned_unestimated_entity_count == 0

    # Task A
    task_a = result.items[1]
    assert task_a.entity_id == TASK_A_ID
    assert task_a.parent_id == PROJECT_ID
    assert task_a.depth == 1
    assert task_a.direct.planned_duration_seconds == 100
    assert task_a.direct.actual_duration_seconds == 120
    assert task_a.direct.variance_seconds == 20

    # Task B
    task_b = result.items[2]
    assert task_b.entity_id == TASK_B_ID
    assert task_b.parent_id == PROJECT_ID
    assert task_b.depth == 1
    assert task_b.direct.planned_duration_seconds == 200
    assert task_b.direct.actual_duration_seconds == 180
    assert task_b.direct.variance_seconds == -20


def test_incomplete_plan_sqlite(db_path: Path) -> None:
    """Project zero + task_a are estimated; task_b remains unestimated."""

    portfolio = _build_portfolio()

    with SqlitePortfolioRepository(db_path) as pf_repo:
        pf_repo.save(portfolio)

        with (
            SqliteExecutionEffortEstimateRepository(db_path) as est_repo,
            SqliteExecutionEffortObservationRepository(db_path) as obs_repo,
        ):
            # Project direct effort is explicitly zero; task_a is estimated.
            # task_b alone remains unestimated.
            est_project = ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=PROJECT_ID,
                duration_seconds=0,
                estimated_at=T0,
                source=SourceKind.USER_CONFIRMED,
            )
            est_repo.add(est_project)

            est_a = ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ID,
                duration_seconds=100,
                estimated_at=T0,
                source=SourceKind.USER_CONFIRMED,
            )
            est_repo.add(est_a)

            # Both have actual observations
            obs_a = ExecutionEffortObservation(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ID,
                duration_seconds=150,
                observed_at=T1,
                source=SourceKind.USER_CONFIRMED,
            )
            obs_b = ExecutionEffortObservation(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_B_ID,
                duration_seconds=80,
                observed_at=T1,
                source=SourceKind.USER_CONFIRMED,
            )
            obs_repo.add(obs_a)
            obs_repo.add(obs_b)

            result = compare_work_breakdown_effort_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                portfolio_repository=pf_repo,
                estimate_reader=est_repo,
                observation_reader=obs_repo,
            )

    root = result.items[0]
    # Incomplete plan → no subtree variance
    assert root.subtree.planned_duration_seconds is None
    assert root.subtree.actual_duration_seconds == 230
    assert root.subtree.variance_seconds is None
    # Coverage still explicit
    assert root.planned_estimated_entity_count == 2
    assert root.planned_unestimated_entity_count == 1

    # Task A: direct variance valid
    task_a = result.items[1]
    assert task_a.direct.planned_duration_seconds == 100
    assert task_a.direct.actual_duration_seconds == 150
    assert task_a.direct.variance_seconds == 50

    # Task B: no estimate → direct variance None
    task_b = result.items[2]
    assert task_b.direct.planned_duration_seconds is None
    assert task_b.direct.actual_duration_seconds == 80
    assert task_b.direct.variance_seconds is None
