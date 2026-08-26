"""Integration evidence for deterministic V1.9 execution-effort measurement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence.models import ExecutionEffortObservationRow
from trajectory_os.adapters.persistence.sqlite import SqlitePortfolioRepository
from trajectory_os.adapters.persistence.sqlite_execution_effort import (
    SqliteExecutionEffortObservationRepository,
)
from trajectory_os.application.execution_effort_measurement import (
    ExecutionEffortObservationReader,
    measure_work_breakdown_effort_durably,
)
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("10000000-0000-4000-8000-000000000001")
PROJECT_ID = UUID("10000000-0000-4000-8000-000000000002")
TASK_ID = UUID("10000000-0000-4000-8000-000000000003")
OTHER_PORTFOLIO_ID = UUID("20000000-0000-4000-8000-000000000001")
OTHER_PROJECT_ID = UUID("20000000-0000-4000-8000-000000000002")
OBS_LOW_ID = UUID(int=1)
OBS_HIGH_ID = UUID(int=2)
OBS_LATER_ID = UUID(int=3)
OBS_OTHER_ID = UUID(int=4)

PLUS_TWO = timezone(timedelta(hours=2))
SAME_INSTANT_PLUS_TWO = datetime(2026, 8, 26, 10, 0, tzinfo=PLUS_TWO)
SAME_INSTANT_UTC = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
LATER_UTC = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


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
        name="V1.9 integration",
        entities=[project, task],
        relations=[
            TrajectoryRelation(
                source_id=TASK_ID,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )


def _other_portfolio() -> Portfolio:
    return Portfolio(
        id=OTHER_PORTFOLIO_ID,
        name="Other",
        entities=[
            TrajectoryEntity(
                id=OTHER_PROJECT_ID,
                entity_type=EntityType.PROJECT,
                title="Other project",
            )
        ],
    )


def _observation(
    observation_id: UUID,
    portfolio_id: UUID,
    entity_id: UUID,
    duration_seconds: int,
    observed_at: datetime,
) -> ExecutionEffortObservation:
    return ExecutionEffortObservation(
        id=observation_id,
        portfolio_id=portfolio_id,
        entity_id=entity_id,
        duration_seconds=duration_seconds,
        observed_at=observed_at,
        source=SourceKind.USER_CONFIRMED,
    )


def _open(
    database_path: Path,
) -> tuple[SqlitePortfolioRepository, SqliteExecutionEffortObservationRepository]:
    return (
        SqlitePortfolioRepository(database_path),
        SqliteExecutionEffortObservationRepository(database_path),
    )


def test_reader_filters_and_orders_by_actual_instant_then_uuid(tmp_path: Path) -> None:
    database_path = tmp_path / "v1_9.db"
    portfolio_repository, observation_repository = _open(database_path)
    portfolio_repository.save(_portfolio())
    portfolio_repository.save(_other_portfolio())

    # Deliberately append out of chronological order. The first two observations
    # represent the same instant using different timezone offsets; UUID breaks the tie.
    later = _observation(OBS_LATER_ID, PORTFOLIO_ID, TASK_ID, 30, LATER_UTC)
    high = _observation(OBS_HIGH_ID, PORTFOLIO_ID, TASK_ID, 20, SAME_INSTANT_UTC)
    low = _observation(OBS_LOW_ID, PORTFOLIO_ID, TASK_ID, 10, SAME_INSTANT_PLUS_TWO)
    other = _observation(
        OBS_OTHER_ID,
        OTHER_PORTFOLIO_ID,
        OTHER_PROJECT_ID,
        999,
        datetime(2026, 8, 25, 0, 0, tzinfo=UTC),
    )
    for observation in (later, high, low, other):
        observation_repository.add(observation)

    reader: ExecutionEffortObservationReader = observation_repository
    portfolio_results = reader.list_for_portfolio(PORTFOLIO_ID)
    entity_results = reader.list_for_entity(PORTFOLIO_ID, TASK_ID)

    assert [observation.id for observation in portfolio_results] == [
        OBS_LOW_ID,
        OBS_HIGH_ID,
        OBS_LATER_ID,
    ]
    assert entity_results == portfolio_results
    assert all(observation.portfolio_id == PORTFOLIO_ID for observation in portfolio_results)
    assert reader.list_for_portfolio(UUID(int=999)) == ()
    assert reader.list_for_entity(PORTFOLIO_ID, UUID(int=999)) == ()

    # Repeated reads reconstruct fresh strict domain values rather than returning
    # cached aliases.
    second_read = reader.list_for_portfolio(PORTFOLIO_ID)
    assert second_read == portfolio_results
    assert second_read[0] is not portfolio_results[0]

    with Session(portfolio_repository.engine) as session:
        row_count = session.scalar(
            select(func.count()).select_from(ExecutionEffortObservationRow)
        )
    assert row_count == 4

    portfolio_repository.close()
    observation_repository.close()


def test_real_sqlite_durable_measurement_is_exact(tmp_path: Path) -> None:
    database_path = tmp_path / "v1_9.db"
    portfolio_repository, observation_repository = _open(database_path)
    portfolio_repository.save(_portfolio())

    observations = (
        _observation(OBS_LATER_ID, PORTFOLIO_ID, TASK_ID, 30, LATER_UTC),
        _observation(OBS_HIGH_ID, PORTFOLIO_ID, TASK_ID, 20, SAME_INSTANT_UTC),
        _observation(OBS_LOW_ID, PORTFOLIO_ID, PROJECT_ID, 10, SAME_INSTANT_PLUS_TWO),
    )
    for observation in observations:
        observation_repository.add(observation)

    result = measure_work_breakdown_effort_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repository,
        observation_repository,
    )

    assert [item.entity_id for item in result.items] == [PROJECT_ID, TASK_ID]
    project_item, task_item = result.items
    assert project_item.direct.duration_seconds == 10
    assert project_item.direct.observation_count == 1
    assert task_item.direct.duration_seconds == 50
    assert task_item.direct.observation_count == 2
    assert task_item.subtree == task_item.direct
    assert project_item.subtree.duration_seconds == 60
    assert project_item.subtree.observation_count == 3
    assert project_item.subtree.first_observed_at == SAME_INSTANT_PLUS_TWO
    assert project_item.subtree.last_observed_at == LATER_UTC

    portfolio_repository.close()
    observation_repository.close()


def test_order_and_measurement_survive_repository_recreation(tmp_path: Path) -> None:
    database_path = tmp_path / "v1_9.db"
    portfolio_repository, observation_repository = _open(database_path)
    portfolio_repository.save(_portfolio())
    observation_repository.add(
        _observation(OBS_LATER_ID, PORTFOLIO_ID, TASK_ID, 30, LATER_UTC)
    )
    observation_repository.add(
        _observation(OBS_LOW_ID, PORTFOLIO_ID, TASK_ID, 10, SAME_INSTANT_PLUS_TWO)
    )
    portfolio_repository.close()
    observation_repository.close()

    portfolio_repository, observation_repository = _open(database_path)
    assert [
        observation.id
        for observation in observation_repository.list_for_portfolio(PORTFOLIO_ID)
    ] == [OBS_LOW_ID, OBS_LATER_ID]

    result = measure_work_breakdown_effort_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repository,
        observation_repository,
    )
    assert result.items[0].subtree.duration_seconds == 40

    portfolio_repository.close()
    observation_repository.close()


def test_removed_entity_history_remains_queryable_but_current_wbs_excludes_it(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v1_9.db"
    portfolio_repository, observation_repository = _open(database_path)
    portfolio_repository.save(_portfolio())
    historical = _observation(
        OBS_LOW_ID,
        PORTFOLIO_ID,
        TASK_ID,
        600,
        SAME_INSTANT_PLUS_TWO,
    )
    observation_repository.add(historical)

    # Replace CURRENT snapshot with the same project but without the historical task.
    portfolio_repository.save(
        Portfolio(
            id=PORTFOLIO_ID,
            name="V1.9 integration",
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
    observation_repository.close()

    portfolio_repository, observation_repository = _open(database_path)
    history = observation_repository.list_for_entity(PORTFOLIO_ID, TASK_ID)
    assert history == (historical,)

    current = portfolio_repository.load(PORTFOLIO_ID)
    assert current is not None
    assert current.get_entity(TASK_ID) is None

    result = measure_work_breakdown_effort_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        portfolio_repository,
        observation_repository,
    )
    assert [item.entity_id for item in result.items] == [PROJECT_ID]
    assert result.items[0].direct.duration_seconds == 0
    assert result.items[0].subtree.duration_seconds == 0
    assert result.items[0].subtree.observation_count == 0

    portfolio_repository.close()
    observation_repository.close()
