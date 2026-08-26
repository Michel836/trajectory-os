"""Real-SQLite integration tests for durable planned-effort estimates (V1.10-C/E)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence import (
    SqliteExecutionEffortEstimateRepository,
    SqlitePortfolioRepository,
)
from trajectory_os.adapters.persistence.models import (
    EntityRow,
    ExecutionEffortEstimateRow,
    PortfolioRow,
    RelationRow,
)
from trajectory_os.application import (
    ExecutionEffortEstimateReader,
    record_execution_effort_estimate_durably,
)
from trajectory_os.application.execution_effort_planning import (
    plan_work_breakdown_effort_durably,
)
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
    create_execution_effort_estimate,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("9138cfd7-6a9c-4ba0-a8e1-000000000001")
OTHER_PORTFOLIO_ID = UUID("9138cfd7-6a9c-4ba0-a8e1-000000000002")
OTHER_PROJECT_ID = UUID("9138cfd7-6a9c-4ba0-a8e1-000000000021")
PROJECT_ID = UUID("9138cfd7-6a9c-4ba0-a8e1-000000000011")
TASK_ID = UUID("9138cfd7-6a9c-4ba0-a8e1-000000000012")
EST_LOW_ID = UUID("9138cfd7-6a9c-4ba0-a8e1-000000000101")
EST_HIGH_ID = UUID("9138cfd7-6a9c-4ba0-a8e1-000000000102")
EST_LATER_ID = UUID("9138cfd7-6a9c-4ba0-a8e1-000000000103")
EST_OTHER_ID = UUID("9138cfd7-6a9c-4ba0-a8e1-000000000104")
PLUS_TWO = timezone(timedelta(hours=2))
SAME_INSTANT_UTC = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
# 12:00 +02:00 == 10:00 UTC: same chronological instant, different offset.
SAME_INSTANT_PLUS_TWO = datetime(2026, 8, 26, 12, 0, tzinfo=PLUS_TWO)
LATER_UTC = datetime(2026, 8, 26, 11, 0, tzinfo=UTC)


def _portfolio() -> Portfolio:
    project = TrajectoryEntity(
        id=PROJECT_ID,
        entity_type=EntityType.PROJECT,
        title="Measured project",
    )
    task = TrajectoryEntity(
        id=TASK_ID,
        entity_type=EntityType.TASK,
        title="Measured task",
    )
    return Portfolio(
        id=PORTFOLIO_ID,
        name="V1.10 integration",
        entities=[project, task],
        relations=[
            TrajectoryRelation(
                source_id=task.id,
                target_id=project.id,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )


def _other_portfolio() -> Portfolio:
    project = TrajectoryEntity(
        id=OTHER_PROJECT_ID,
        entity_type=EntityType.PROJECT,
        title="Other portfolio project",
    )
    return Portfolio(id=OTHER_PORTFOLIO_ID, name="V1.10 other", entities=[project])


def _estimate(
    estimate_id: UUID,
    portfolio_id: UUID,
    entity_id: UUID,
    duration_seconds: int,
    estimated_at: datetime,
) -> ExecutionEffortEstimate:
    return ExecutionEffortEstimate(
        id=estimate_id,
        portfolio_id=portfolio_id,
        entity_id=entity_id,
        duration_seconds=duration_seconds,
        estimated_at=estimated_at,
        source=SourceKind.USER_CONFIRMED,
    )


def _open(
    database_path: Path,
) -> tuple[SqlitePortfolioRepository, SqliteExecutionEffortEstimateRepository]:
    # Portfolio repository init is authoritative for schema + migrations.
    portfolio_repository = SqlitePortfolioRepository(database_path)
    estimate_repository = SqliteExecutionEffortEstimateRepository(database_path)
    return portfolio_repository, estimate_repository


def test_real_sqlite_add_get_and_deterministic_listing(tmp_path: Path) -> None:
    database_path = tmp_path / "v1_10.db"
    portfolio_repository, estimate_repository = _open(database_path)
    portfolio = _portfolio()
    portfolio_repository.save(portfolio)
    portfolio_repository.save(_other_portfolio())

    created = create_execution_effort_estimate(
        portfolio,
        EST_LOW_ID,
        TASK_ID,
        20,
        SAME_INSTANT_PLUS_TWO,
    )
    estimate_repository.add(created)
    estimate_repository.add(
        _estimate(EST_HIGH_ID, PORTFOLIO_ID, TASK_ID, 20, SAME_INSTANT_UTC)
    )
    estimate_repository.add(
        _estimate(EST_LATER_ID, PORTFOLIO_ID, TASK_ID, 30, LATER_UTC)
    )
    estimate_repository.add(
        _estimate(EST_OTHER_ID, OTHER_PORTFOLIO_ID, TASK_ID, 999, SAME_INSTANT_UTC)
    )

    assert estimate_repository.get(EST_LATER_ID) is not None
    assert estimate_repository.get(EST_OTHER_ID) is not None
    assert estimate_repository.get(UUID(int=999)) is None

    reader: ExecutionEffortEstimateReader = estimate_repository
    portfolio_results = reader.list_for_portfolio(PORTFOLIO_ID)
    entity_results = reader.list_for_entity(PORTFOLIO_ID, TASK_ID)

    assert [estimate.id for estimate in portfolio_results] == [
        EST_LOW_ID,
        EST_HIGH_ID,
        EST_LATER_ID,
    ]
    assert entity_results == portfolio_results
    assert all(
        estimate.portfolio_id == PORTFOLIO_ID
        for estimate in portfolio_results
    )
    assert reader.list_for_portfolio(UUID(int=999)) == ()
    assert reader.list_for_entity(PORTFOLIO_ID, UUID(int=999)) == ()

    # Repeated reads reconstruct fresh strict domain values rather than
    # returning cached aliases.
    second_read = reader.list_for_portfolio(PORTFOLIO_ID)
    assert second_read == portfolio_results
    assert second_read[0] is not portfolio_results[0]

    with Session(portfolio_repository.engine) as session:
        row_count = session.scalar(
            select(func.count()).select_from(ExecutionEffortEstimateRow)
        )
    assert row_count == 4

    portfolio_repository.close()
    estimate_repository.close()


def test_real_sqlite_add_is_immutable_and_rejects_duplicate_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v1_10.db"
    portfolio_repository, estimate_repository = _open(database_path)
    portfolio_repository.save(_portfolio())
    estimate_repository.add(
        _estimate(EST_LOW_ID, PORTFOLIO_ID, TASK_ID, 20, SAME_INSTANT_UTC)
    )
    assert (
        estimate_repository.get(EST_LOW_ID).duration_seconds == 20
    )

    with pytest.raises(
        ValueError, match="execution-effort estimate already exists"
    ):
        estimate_repository.add(
            _estimate(EST_LOW_ID, PORTFOLIO_ID, TASK_ID, 99, SAME_INSTANT_UTC)
        )

    with Session(portfolio_repository.engine) as session:
        row_count = session.scalar(
            select(func.count()).select_from(ExecutionEffortEstimateRow)
        )
        stored = session.scalar(select(ExecutionEffortEstimateRow))
    assert row_count == 1
    assert stored is not None
    assert stored.duration_seconds == 20

    portfolio_repository.close()
    estimate_repository.close()


def test_real_sqlite_durable_planning_is_exact(tmp_path: Path) -> None:
    database_path = tmp_path / "v1_10.db"
    portfolio_repository, estimate_repository = _open(database_path)
    portfolio_repository.save(_portfolio())

    estimate_repository.add(
        _estimate(EST_LATER_ID, PORTFOLIO_ID, TASK_ID, 30, LATER_UTC)
    )
    estimate_repository.add(
        _estimate(EST_HIGH_ID, PORTFOLIO_ID, TASK_ID, 20, SAME_INSTANT_UTC)
    )
    estimate_repository.add(
        _estimate(EST_LOW_ID, PORTFOLIO_ID, PROJECT_ID, 10, SAME_INSTANT_PLUS_TWO)
    )

    result = plan_work_breakdown_effort_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repository,
        estimate_repository,
    )

    assert [item.entity_id for item in result.items] == [PROJECT_ID, TASK_ID]
    project_item, task_item = result.items
    assert project_item.direct_estimate is not None
    assert project_item.direct_estimate.duration_seconds == 10
    assert task_item.direct_estimate is not None
    assert task_item.direct_estimate.duration_seconds == 30
    assert task_item.subtree.known_duration_seconds == 30
    assert task_item.subtree.total_duration_seconds == 30
    assert project_item.subtree.known_duration_seconds == 40
    assert project_item.subtree.estimated_entity_count == 2
    assert project_item.subtree.unestimated_entity_count == 0
    assert project_item.subtree.total_duration_seconds == 40

    portfolio_repository.close()
    estimate_repository.close()


def test_durable_record_boundary_uses_real_sqlite_append(tmp_path: Path) -> None:
    database_path = tmp_path / "v1_10.db"
    portfolio_repository, estimate_repository = _open(database_path)
    portfolio_repository.save(_portfolio())

    created = record_execution_effort_estimate_durably(
        PORTFOLIO_ID,
        EST_LOW_ID,
        TASK_ID,
        20,
        SAME_INSTANT_UTC,
        portfolio_repository,
        estimate_repository,
    )

    assert created.id == EST_LOW_ID
    stored = estimate_repository.get(EST_LOW_ID)
    assert stored == created
    assert stored is not created
    with Session(portfolio_repository.engine) as session:
        row_count = session.scalar(
            select(func.count()).select_from(ExecutionEffortEstimateRow)
        )
    assert row_count == 1

    portfolio_repository.close()
    estimate_repository.close()


def test_selection_and_rollup_survive_repository_recreation(tmp_path: Path) -> None:
    database_path = tmp_path / "v1_10.db"
    portfolio_repository, estimate_repository = _open(database_path)
    portfolio_repository.save(_portfolio())
    estimate_repository.add(
        _estimate(EST_LATER_ID, PORTFOLIO_ID, TASK_ID, 30, LATER_UTC)
    )
    estimate_repository.add(
        _estimate(EST_LOW_ID, PORTFOLIO_ID, TASK_ID, 10, SAME_INSTANT_PLUS_TWO)
    )
    portfolio_repository.close()
    estimate_repository.close()

    portfolio_repository, estimate_repository = _open(database_path)
    assert [
        estimate.id
        for estimate in estimate_repository.list_for_portfolio(PORTFOLIO_ID)
    ] == [EST_LOW_ID, EST_LATER_ID]

    result = plan_work_breakdown_effort_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repository,
        estimate_repository,
    )
    # Both stored rows are revisions of the SAME task: only the latest
    # effective revision (30 @ 11:00 UTC) may be selected; the earlier
    # revision (10 @ 10:00 UTC) must never be summed.
    root = result.items[0]
    task_item = next(item for item in result.items if item.entity_id == TASK_ID)
    assert task_item.direct_estimate is not None
    assert task_item.direct_estimate.id == EST_LATER_ID
    assert task_item.direct_estimate.duration_seconds == 30
    assert root.subtree.known_duration_seconds == 30
    assert root.subtree.estimated_entity_count == 1
    assert root.subtree.unestimated_entity_count == 1
    assert root.subtree.total_duration_seconds is None

    # Estimating the project completes every subtree -> totals appear.
    estimate_repository.add(
        ExecutionEffortEstimate(
            id=UUID("9138cfd7-6a9c-4ba0-a8e1-000000000107"),
            portfolio_id=PORTFOLIO_ID,
            entity_id=PROJECT_ID,
            duration_seconds=50,
            estimated_at=LATER_UTC,
            source=SourceKind.USER_CONFIRMED,
        )
    )

    complete = plan_work_breakdown_effort_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repository,
        estimate_repository,
    )
    croot = complete.items[0]
    assert croot.subtree.known_duration_seconds == 80
    assert croot.subtree.estimated_entity_count == 2
    assert croot.subtree.unestimated_entity_count == 0
    assert croot.subtree.total_duration_seconds == 80

    portfolio_repository.close()
    estimate_repository.close()


def test_removed_entity_history_remains_queryable_but_current_wbs_excludes_it(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v1_10.db"
    portfolio_repository, estimate_repository = _open(database_path)
    portfolio_repository.save(_portfolio())
    historical = _estimate(
        EST_LOW_ID,
        PORTFOLIO_ID,
        TASK_ID,
        600,
        SAME_INSTANT_PLUS_TWO,
    )
    estimate_repository.add(historical)

    # Replace the CURRENT snapshot with the same project without the historical task.
    portfolio_repository.save(
        Portfolio(
            id=PORTFOLIO_ID,
            name="V1.10 integration",
            entities=[
                TrajectoryEntity(
                    id=PROJECT_ID,
                    entity_type=EntityType.PROJECT,
                    title="Measured project",
                )
            ],
        )
    )
    portfolio_repository.close()
    estimate_repository.close()

    portfolio_repository, estimate_repository = _open(database_path)
    history = estimate_repository.list_for_entity(PORTFOLIO_ID, TASK_ID)
    assert history == (historical,)

    current = portfolio_repository.load(PORTFOLIO_ID)
    assert current is not None
    assert current.get_entity(TASK_ID) is None

    result = plan_work_breakdown_effort_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repository,
        estimate_repository,
    )
    assert [item.entity_id for item in result.items] == [PROJECT_ID]
    assert result.items[0].direct_estimate is None
    assert result.items[0].subtree.known_duration_seconds == 0
    assert result.items[0].subtree.unestimated_entity_count == 1
    assert result.items[0].subtree.total_duration_seconds is None

    portfolio_repository.close()
    estimate_repository.close()


def test_schema_tables_exist(tmp_path: Path) -> None:
    database_path = tmp_path / "v1_10.db"
    portfolio_repository, estimate_repository = _open(database_path)

    with Session(portfolio_repository.engine) as session:
        engine = session.get_bind()
        table_names = set(inspect(engine).get_table_names())

    expected = {
        PortfolioRow.__table__.name,
        RelationRow.__table__.name,
        EntityRow.__table__.name,
        ExecutionEffortEstimateRow.__table__.name,
    }
    assert expected <= table_names
    assert ExecutionEffortEstimateRow.__table__.name == "execution_effort_estimates"
    assert ExecutionEffortEstimateRow.__table__.name not in (
        "execution_effort_observations",
    )

    portfolio_repository.close()
    estimate_repository.close()
