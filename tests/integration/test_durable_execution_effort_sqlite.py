"""Integration test: durable execution-effort persistence against real SQLite.

Exercises the full V1.8-C path with no doubles: the real
``SqlitePortfolioRepository`` persists the canonical portfolio, the real
``SqliteExecutionEffortObservationRepository`` durably stores the
append-only observations, and ``record_execution_effort_durably``
(structurally typed against ``ExecutionEffortObservationRepository``)
records them. The same SQLite file is reloaded to prove durability,
including survival after the target entity is removed from the current
portfolio snapshot.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence.models import ExecutionEffortObservationRow
from trajectory_os.adapters.persistence.sqlite import SqlitePortfolioRepository
from trajectory_os.adapters.persistence.sqlite_execution_effort import (
    DuplicateExecutionEffortObservationError,
    SqliteExecutionEffortObservationRepository,
)
from trajectory_os.application import (
    ExecutionEffortObservationRepository,
    PortfolioRepository,
    record_execution_effort_durably,
)
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.portfolio import Portfolio

PORTFOLIO_ID = UUID("6f9d2c10-1a2b-4c3d-8e9f-0a1b2c3d4e5f")
ENTITY_ID = UUID("6f9d2c11-1a2b-4c3d-8e9f-0a1b2c3d4e5f")
SUCCESSOR_ENTITY_ID = UUID("6f9d2c33-1a2b-4c3d-8e9f-0a1b2c3d4e5f")
OBSERVATION_ID_ONE = UUID("6f9d2c21-1a2b-4c3d-8e9f-0a1b2c3d4e5f")
OBSERVATION_ID_TWO = UUID("6f9d2c22-1a2b-4c3d-8e9f-0a1b2c3d4e5f")

OFFSET = timezone(timedelta(hours=2))
OBSERVED_AT_ONE = datetime(2026, 8, 24, 9, 30, 45, tzinfo=OFFSET)
OBSERVED_AT_TWO = datetime(2026, 8, 24, 16, 0, 0, tzinfo=OFFSET)

TARGET_ENTITY = TrajectoryEntity(
    id=ENTITY_ID,
    entity_type=EntityType.PROJECT,
    title="V1.8-C target",
)
SUCCESSOR_ENTITY = TrajectoryEntity(
    id=SUCCESSOR_ENTITY_ID,
    entity_type=EntityType.PROJECT,
    title="V1.8-C successor",
)


def _setup(
    tmp_path: Path,
) -> tuple[SqlitePortfolioRepository, SqliteExecutionEffortObservationRepository]:
    portfolio_repository = SqlitePortfolioRepository(tmp_path / "portfolio.db")
    observation_repository = SqliteExecutionEffortObservationRepository(
        tmp_path / "portfolio.db"
    )
    portfolio_repository.save(
        Portfolio(id=PORTFOLIO_ID, name="V1.8-C", entities=[TARGET_ENTITY])
    )
    return portfolio_repository, observation_repository


def _record(
    portfolio_repository: PortfolioRepository,
    observation_repository: ExecutionEffortObservationRepository,
    observation_id: UUID,
    duration_seconds: int,
    observed_at: datetime,
) -> ExecutionEffortObservation:
    return record_execution_effort_durably(
        PORTFOLIO_ID,
        observation_id,
        ENTITY_ID,
        duration_seconds,
        observed_at,
        portfolio_repository,
        observation_repository,
    )


def test_record_and_reload_two_observations_exactly(tmp_path: Path) -> None:
    (sqlite_portfolio_repository, sqlite_observation_repository) = _setup(tmp_path)

    # (B) Structural typing against the REAL adapter instances.
    portfolio_repository: PortfolioRepository = sqlite_portfolio_repository
    observation_repository: ExecutionEffortObservationRepository = (
        sqlite_observation_repository
    )

    # (C) Real durable recording.
    observation_one = _record(
        portfolio_repository, observation_repository, OBSERVATION_ID_ONE, 45, OBSERVED_AT_ONE
    )

    # (D) Exact returned observation fields.
    assert observation_one.id == OBSERVATION_ID_ONE
    assert observation_one.portfolio_id == PORTFOLIO_ID
    assert observation_one.entity_id == ENTITY_ID
    assert observation_one.duration_seconds == 45
    assert observation_one.duration_seconds > 0
    assert observation_one.observed_at == OBSERVED_AT_ONE
    assert observation_one.source is SourceKind.USER_CONFIRMED

    # (E) Reload proves exact value equality.
    reloaded_one = observation_repository.get(OBSERVATION_ID_ONE)
    assert reloaded_one == observation_one

    # (F) A second observation for the SAME entity coexists independently.
    observation_two = _record(
        portfolio_repository, observation_repository, OBSERVATION_ID_TWO, 120, OBSERVED_AT_TWO
    )
    assert observation_two.id == OBSERVATION_ID_TWO
    assert observation_two.entity_id == ENTITY_ID
    assert observation_repository.get(OBSERVATION_ID_ONE) == observation_one
    assert observation_repository.get(OBSERVATION_ID_TWO) == observation_two

    # (K) Exactly two rows: no duplicates after all operations.
    with Session(sqlite_portfolio_repository.engine) as session:
        row_count = session.scalar(
            select(func.count()).select_from(ExecutionEffortObservationRow)
        )
    assert row_count == 2

    sqlite_portfolio_repository.close()
    sqlite_observation_repository.close()


def test_duplicate_observation_id_rejected_and_original_unchanged(
    tmp_path: Path,
) -> None:
    (sqlite_portfolio_repository, sqlite_observation_repository) = _setup(tmp_path)

    portfolio_repository: PortfolioRepository = sqlite_portfolio_repository
    observation_repository: ExecutionEffortObservationRepository = (
        sqlite_observation_repository
    )

    observation = _record(
        portfolio_repository, observation_repository, OBSERVATION_ID_ONE, 60, OBSERVED_AT_ONE
    )
    stored = observation_repository.get(OBSERVATION_ID_ONE)
    assert stored is not None
    assert stored == observation

    with pytest.raises(DuplicateExecutionEffortObservationError):
        observation_repository.add(stored)

    # The original stored observation is unchanged and untouched.
    assert observation_repository.get(OBSERVATION_ID_ONE) == observation
    with Session(sqlite_portfolio_repository.engine) as session:
        row_count = session.scalar(
            select(func.count()).select_from(ExecutionEffortObservationRow)
        )
    assert row_count == 1

    sqlite_portfolio_repository.close()
    sqlite_observation_repository.close()


def test_get_unknown_observation_returns_none(tmp_path: Path) -> None:
    (sqlite_portfolio_repository, sqlite_observation_repository) = _setup(tmp_path)

    observation_repository: ExecutionEffortObservationRepository = (
        sqlite_observation_repository
    )
    assert observation_repository.get(UUID(int=1)) is None

    sqlite_portfolio_repository.close()
    sqlite_observation_repository.close()


def test_entity_removal_does_not_delete_effort_history(tmp_path: Path) -> None:
    (sqlite_portfolio_repository, sqlite_observation_repository) = _setup(tmp_path)

    portfolio_repository: PortfolioRepository = sqlite_portfolio_repository
    observation_repository: ExecutionEffortObservationRepository = (
        sqlite_observation_repository
    )

    observation_one = _record(
        portfolio_repository, observation_repository, OBSERVATION_ID_ONE, 45, OBSERVED_AT_ONE
    )
    observation_two = _record(
        portfolio_repository, observation_repository, OBSERVATION_ID_TWO, 120, OBSERVED_AT_TWO
    )

    # Current snapshot is replaced with the target entity REMOVED.
    portfolio_repository.save(
        Portfolio(id=PORTFOLIO_ID, name="V1.8-C", entities=[SUCCESSOR_ENTITY])
    )

    reloaded_portfolio = portfolio_repository.load(PORTFOLIO_ID)
    assert reloaded_portfolio is not None
    assert reloaded_portfolio.get_entity(ENTITY_ID) is None

    # Both previously stored observations still load exactly: entity removal
    # did NOT cascade into or delete the effort history (entity_id has no FK).
    assert observation_repository.get(OBSERVATION_ID_ONE) == observation_one
    assert observation_repository.get(OBSERVATION_ID_TWO) == observation_two

    sqlite_portfolio_repository.close()
    sqlite_observation_repository.close()


def test_observation_for_missing_portfolio_not_silently_accepted(
    tmp_path: Path,
) -> None:
    (sqlite_portfolio_repository, sqlite_observation_repository) = _setup(tmp_path)

    observation_repository: ExecutionEffortObservationRepository = (
        sqlite_observation_repository
    )

    orphan = ExecutionEffortObservation(
        id=uuid4(),
        portfolio_id=uuid4(),  # No such portfolios row exists.
        entity_id=ENTITY_ID,
        duration_seconds=1,
        observed_at=OBSERVED_AT_ONE,
        source=SourceKind.USER_CONFIRMED,
    )

    # PRAGMA foreign_keys=ON rejects the portfolio-level ownership violation.
    with pytest.raises(IntegrityError):
        observation_repository.add(orphan)

    assert observation_repository.get(orphan.id) is None
    with Session(sqlite_portfolio_repository.engine) as session:
        row_count = session.scalar(
            select(func.count()).select_from(ExecutionEffortObservationRow)
        )
    assert row_count == 0

    sqlite_portfolio_repository.close()
    sqlite_observation_repository.close()
