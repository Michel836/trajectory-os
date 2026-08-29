"""SQLite integration evidence for V1.23 effective WBS effort plans."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from trajectory_os.adapters.persistence.sqlite import SqlitePortfolioRepository
from trajectory_os.adapters.persistence.sqlite_execution_effort_calibration_acceptance import (
    SqliteCalibratedEstimateRevisionRepository,
)
from trajectory_os.adapters.persistence.sqlite_execution_effort_estimates import (
    SqliteExecutionEffortEstimateRepository,
)
from trajectory_os.application.execution_effort_calibration_acceptance import (
    accept_calibrated_estimate_revision_durably,
)
from trajectory_os.application.execution_effort_calibration_composition import (
    EffectiveCalibrationApplicationResult,
    EffectiveCalibrationApplicationStatus,
)
from trajectory_os.application.execution_effort_calibration_entity_binding import (
    CalibratedEstimateRevisionProposal,
    bind_effort_calibration_to_current_entity,
)
from trajectory_os.application.execution_effort_effective_plan import (
    build_effective_work_breakdown_effort_plan_durably,
)
from trajectory_os.domain.entities import (
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactor,
)
from trajectory_os.domain.execution_effort_calibration_factor_application import (
    apply_effective_effort_calibration_factor,
)
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import (
    RelationType,
    TrajectoryRelation,
)

PORTFOLIO_ID = UUID("71111111-1111-4111-8111-111111111111")
PROJECT_ID = UUID("73333333-3333-4333-8333-333333333333")
TASK_CALIBRATED_ID = UUID("75555555-5555-4555-8555-555555555555")
TASK_ORDINARY_ID = UUID("76666666-6666-4666-8666-666666666666")

OLD_TASK_ESTIMATE_ID = UUID("10000000-0000-4000-8000-000000000001")
CALIBRATED_ESTIMATE_ID = UUID("20000000-0000-4000-8000-000000000001")
ORDINARY_ESTIMATE_ID = UUID("30000000-0000-4000-8000-000000000001")

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)


def _current_portfolio() -> Portfolio:
    project = TrajectoryEntity(
        id=PROJECT_ID,
        entity_type=EntityType.PROJECT,
        title="project",
        description="",
    )
    calibrated_task = TrajectoryEntity(
        id=TASK_CALIBRATED_ID,
        entity_type=EntityType.TASK,
        title="calibrated task",
        description="",
    )
    ordinary_task = TrajectoryEntity(
        id=TASK_ORDINARY_ID,
        entity_type=EntityType.TASK,
        title="ordinary task",
        description="",
    )

    return Portfolio(
        id=PORTFOLIO_ID,
        name="V1.23 SQLite integration",
        entities=[
            project,
            calibrated_task,
            ordinary_task,
        ],
        relations=[
            TrajectoryRelation(
                source_id=TASK_CALIBRATED_ID,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
            ),
            TrajectoryRelation(
                source_id=TASK_ORDINARY_ID,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
            ),
        ],
    )


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


def _ready_proposal(
    portfolio_repository: SqlitePortfolioRepository,
) -> CalibratedEstimateRevisionProposal:
    application = EffectiveCalibrationApplicationResult(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=300,
        status=EffectiveCalibrationApplicationStatus.AVAILABLE,
        proposal=apply_effective_effort_calibration_factor(
            300,
            _factor(),
        ),
    )

    return bind_effort_calibration_to_current_entity(
        application,
        TASK_CALIBRATED_ID,
        portfolio_repository,
    )


def _database_state(
    database_path: Path,
) -> tuple[tuple[str, ...], tuple[tuple[str, int], ...]]:
    """Return user-table schema names and row counts deterministically."""
    with sqlite3.connect(database_path) as connection:
        tables = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        )

        counts = tuple(
            (
                table,
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0],
            )
            for table in tables
        )

    return tables, counts


def test_effective_plan_real_sqlite_round_trip_is_read_only_and_deterministic(
    tmp_path: Path,
) -> None:
    """Persist mixed V1.10/V1.21 history, reopen, then build V1.23 read-only."""
    database_path = tmp_path / "trajectory-v123.sqlite3"

    # ------------------------------------------------------------------
    # Persist the authoritative CURRENT Portfolio and estimate history.
    # ------------------------------------------------------------------
    with (
        SqlitePortfolioRepository(database_path) as portfolio_repository,
        SqliteExecutionEffortEstimateRepository(database_path) as estimate_repository,
        SqliteCalibratedEstimateRevisionRepository(
            database_path
        ) as revision_repository,
    ):
        portfolio_repository.save(_current_portfolio())

        # Older ordinary history for the task that will later receive a
        # calibrated estimate. It must remain valid history but not win.
        estimate_repository.add(
            ExecutionEffortEstimate(
                id=OLD_TASK_ESTIMATE_ID,
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_CALIBRATED_ID,
                duration_seconds=300,
                estimated_at=T0,
                source=SourceKind.USER_CONFIRMED,
            )
        )

        # A separate ordinary selected estimate.
        estimate_repository.add(
            ExecutionEffortEstimate(
                id=ORDINARY_ESTIMATE_ID,
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_ORDINARY_ID,
                duration_seconds=600,
                estimated_at=T0,
                source=SourceKind.USER_CONFIRMED,
            )
        )

        # Real V1.21 acceptance atomically persists the newer calibrated
        # V1.10 estimate (450 s) and its exact V1.21 provenance.
        accept_calibrated_estimate_revision_durably(
            _ready_proposal(portfolio_repository),
            estimate_id=CALIBRATED_ESTIMATE_ID,
            estimated_at=T1,
            portfolio_repository=portfolio_repository,
            revision_repository=revision_repository,
        )

    # Repositories above are deliberately closed. Everything below must be
    # reconstructed from the durable SQLite state.
    state_before = _database_state(database_path)

    # ------------------------------------------------------------------
    # Fresh repositories: real durable V1.23 read path.
    # ------------------------------------------------------------------
    with (
        SqlitePortfolioRepository(database_path) as portfolio_repository,
        SqliteExecutionEffortEstimateRepository(database_path) as estimate_reader,
        SqliteCalibratedEstimateRevisionRepository(
            database_path
        ) as provenance_reader,
    ):
        first = build_effective_work_breakdown_effort_plan_durably(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            portfolio_repository=portfolio_repository,
            estimate_reader=estimate_reader,
            provenance_reader=provenance_reader,
        )

        second = build_effective_work_breakdown_effort_plan_durably(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            portfolio_repository=portfolio_repository,
            estimate_reader=estimate_reader,
            provenance_reader=provenance_reader,
        )

    # ------------------------------------------------------------------
    # Selection, structure and exact provenance.
    # ------------------------------------------------------------------
    assert first == second
    assert first.portfolio_id == PORTFOLIO_ID
    assert first.project_id == PROJECT_ID
    assert len(first.items) == 3

    by_entity = {item.entity_id: item for item in first.items}

    root = by_entity[PROJECT_ID]
    calibrated_task = by_entity[TASK_CALIBRATED_ID]
    ordinary_task = by_entity[TASK_ORDINARY_ID]

    assert root.parent_id is None
    assert root.depth == 0
    assert root.direct_estimate is None
    assert root.calibrated_provenance is None

    assert calibrated_task.parent_id == PROJECT_ID
    assert calibrated_task.depth == 1
    assert calibrated_task.direct_estimate is not None
    assert calibrated_task.direct_estimate.id == CALIBRATED_ESTIMATE_ID
    assert calibrated_task.direct_estimate.duration_seconds == 450

    assert calibrated_task.calibrated_provenance is not None
    assert (
        calibrated_task.calibrated_provenance.estimate_id
        == CALIBRATED_ESTIMATE_ID
    )
    assert (
        calibrated_task.calibrated_provenance.calibrated_duration_seconds
        == 450
    )
    assert calibrated_task.calibrated_provenance.entity_id == TASK_CALIBRATED_ID
    assert calibrated_task.calibrated_provenance.portfolio_id == PORTFOLIO_ID
    assert calibrated_task.calibrated_provenance.project_id == PROJECT_ID

    # The older 300-second history must not affect the selected direct value.
    assert calibrated_task.direct_estimate.id != OLD_TASK_ESTIMATE_ID

    assert ordinary_task.parent_id == PROJECT_ID
    assert ordinary_task.depth == 1
    assert ordinary_task.direct_estimate is not None
    assert ordinary_task.direct_estimate.id == ORDINARY_ESTIMATE_ID
    assert ordinary_task.direct_estimate.duration_seconds == 600
    assert ordinary_task.calibrated_provenance is None

    # ------------------------------------------------------------------
    # V1.10-D subtree arithmetic is preserved exactly by enrichment.
    # Root itself is unestimated; both child tasks are estimated.
    # ------------------------------------------------------------------
    assert calibrated_task.subtree.known_duration_seconds == 450
    assert calibrated_task.subtree.estimated_entity_count == 1
    assert calibrated_task.subtree.unestimated_entity_count == 0
    assert calibrated_task.subtree.total_duration_seconds == 450

    assert ordinary_task.subtree.known_duration_seconds == 600
    assert ordinary_task.subtree.estimated_entity_count == 1
    assert ordinary_task.subtree.unestimated_entity_count == 0
    assert ordinary_task.subtree.total_duration_seconds == 600

    assert root.subtree.known_duration_seconds == 1050
    assert root.subtree.estimated_entity_count == 2
    assert root.subtree.unestimated_entity_count == 1
    assert root.subtree.total_duration_seconds is None

    # ------------------------------------------------------------------
    # V1.23 must create neither rows nor schema while reading/enriching.
    # ------------------------------------------------------------------
    state_after = _database_state(database_path)
    assert state_after == state_before
