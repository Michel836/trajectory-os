"""Integration test: V1.12 calibration evidence over real SQLite persistence."""

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
from trajectory_os.application.execution_effort_calibration import (
    build_effort_calibration_evidence_durably,
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

PORTFOLIO_ID = UUID("c0ffee01-0000-4000-8000-000000000001")
PROJECT_ID = UUID("c0ffee02-0000-4000-8000-000000000002")
TASK_DONE_ID = UUID("c0ffee03-0000-4000-8000-000000000003")
TASK_NOEST_ID = UUID("c0ffee04-0000-4000-8000-000000000004")
TASK_NO_OBS_ID = UUID("c0ffee05-0000-4000-8000-000000000005")

EST_BEFORE = datetime(2024, 8, 1, 9, 0, 0, tzinfo=UTC)
FIRST_OBS = datetime(2024, 9, 1, 9, 0, 0, tzinfo=UTC)
REVISION_AFTER = datetime(2024, 10, 1, 9, 0, 0, tzinfo=UTC)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _entity(entity_id: UUID, entity_type: EntityType, status: EntityStatus) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id,
        entity_type=entity_type,
        title=f"Entity {str(entity_id)[:8]}",
        description="",
        status=status,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=EST_BEFORE,
        updated_at=EST_BEFORE,
    )


def _build_portfolio() -> Portfolio:
    project = _entity(PROJECT_ID, EntityType.PROJECT, EntityStatus.COMPLETED)
    task_done = _entity(TASK_DONE_ID, EntityType.TASK, EntityStatus.COMPLETED)
    task_noest = _entity(TASK_NOEST_ID, EntityType.TASK, EntityStatus.COMPLETED)
    task_nobs = _entity(TASK_NO_OBS_ID, EntityType.TASK, EntityStatus.COMPLETED)
    relations = [
        TrajectoryRelation(
            id=uuid4(),
            source_id=child,
            target_id=PROJECT_ID,
            relation_type=RelationType.BELONGS_TO,
            source=SourceKind.USER_CONFIRMED,
            confidence=1.0,
        )
        for child in (TASK_DONE_ID, TASK_NOEST_ID, TASK_NO_OBS_ID)
    ]
    return Portfolio(
        id=PORTFOLIO_ID,
        name="Calibration Integration Portfolio",
        entities=[project, task_done, task_noest, task_nobs],
        relations=relations,
    )


def test_full_sqlite_calibration_evidence(db_path: Path) -> None:
    """Persist portfolio + estimate history + observation history, derive."""

    portfolio = _build_portfolio()

    with SqlitePortfolioRepository(db_path) as pf_repo:
        pf_repo.save(portfolio)

        with (
            SqliteExecutionEffortEstimateRepository(db_path) as est_repo,
            SqliteExecutionEffortObservationRepository(db_path) as obs_repo,
        ):
            # Plan recorded before work: 100s on task_done.
            est_before = ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_DONE_ID,
                duration_seconds=100,
                estimated_at=EST_BEFORE,
                source=SourceKind.USER_CONFIRMED,
            )
            est_repo.add(est_before)

            # Post-observation revision: must NOT leak into calibration.
            est_late = ExecutionEffortEstimate(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_DONE_ID,
                duration_seconds=1,
                estimated_at=REVISION_AFTER,
                source=SourceKind.USER_CONFIRMED,
            )
            est_repo.add(est_late)

            # Actuals: 130s on task_done (underplanned +30); 50s on
            # task_noest (completed, observations, no prior estimate).
            obs_done = ExecutionEffortObservation(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_DONE_ID,
                duration_seconds=130,
                observed_at=FIRST_OBS,
                source=SourceKind.USER_CONFIRMED,
            )
            obs_repo.add(obs_done)

            obs_noest = ExecutionEffortObservation(
                id=uuid4(),
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_NOEST_ID,
                duration_seconds=50,
                observed_at=FIRST_OBS,
                source=SourceKind.USER_CONFIRMED,
            )
            obs_repo.add(obs_noest)

            result = build_effort_calibration_evidence_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                portfolio_repository=pf_repo,
                estimate_reader=est_repo,
                observation_reader=obs_repo,
            )

    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_id == PROJECT_ID

    # Coverage: 4 completed entities (project + 3 tasks) = 1 sample
    # + 2 without observation (project, task_no_obs) + 1 no prior estimate.
    assert result.completed_entity_count == 4
    assert result.completed_without_observation_count == 2
    assert result.completed_without_prior_estimate_count == 1
    assert len(result.samples) == 1

    sample = result.samples[0]
    assert sample.entity_id == TASK_DONE_ID
    assert sample.estimate_id == est_before.id
    assert sample.estimated_at == EST_BEFORE
    assert sample.first_observed_at == FIRST_OBS
    assert sample.last_observed_at == FIRST_OBS
    assert sample.observation_count == 1
    assert sample.planned_duration_seconds == 100
    assert sample.actual_duration_seconds == 130
    assert sample.variance_seconds == 30
    assert sample.absolute_error_seconds == 30

    summary = result.summary
    assert summary.sample_count == 1
    assert summary.total_planned_duration_seconds == 100
    assert summary.total_actual_duration_seconds == 130
    assert summary.signed_variance_seconds == 30
    assert summary.absolute_error_seconds == 30
    assert summary.underplanned_entity_count == 1
    assert summary.exact_entity_count == 0
    assert summary.overplanned_entity_count == 0


def test_repeated_sqlite_read_is_stable(db_path: Path) -> None:
    """Repeated durable derivation over the same persisted state yields the
    same immutable evidence."""

    portfolio = _build_portfolio()

    with SqlitePortfolioRepository(db_path) as pf_repo:
        pf_repo.save(portfolio)

        with (
            SqliteExecutionEffortEstimateRepository(db_path) as est_repo,
            SqliteExecutionEffortObservationRepository(db_path) as obs_repo,
        ):
            est_repo.add(
                ExecutionEffortEstimate(
                    id=uuid4(),
                    portfolio_id=PORTFOLIO_ID,
                    entity_id=TASK_DONE_ID,
                    duration_seconds=100,
                    estimated_at=EST_BEFORE,
                    source=SourceKind.USER_CONFIRMED,
                )
            )
            obs_repo.add(
                ExecutionEffortObservation(
                    id=uuid4(),
                    portfolio_id=PORTFOLIO_ID,
                    entity_id=TASK_DONE_ID,
                    duration_seconds=64,
                    observed_at=FIRST_OBS,
                    source=SourceKind.USER_CONFIRMED,
                )
            )

            first = build_effort_calibration_evidence_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                portfolio_repository=pf_repo,
                estimate_reader=est_repo,
                observation_reader=obs_repo,
            )
            second = build_effort_calibration_evidence_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                portfolio_repository=pf_repo,
                estimate_reader=est_repo,
                observation_reader=obs_repo,
            )

    assert first == second
    sample = first.samples[0]
    assert sample.variance_seconds == 64 - 100
    assert sample.absolute_error_seconds == 36
