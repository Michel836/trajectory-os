"""SQLite integration evidence for the V1.25 portfolio effective-effort summary.

This test performs a real SQLite round trip:

1.  the authoritative CURRENT Portfolio, ordinary estimates, and an accepted
    calibrated revision (through the real V1.21 acceptance boundary) are
    persisted, then every repository is closed;
2.  fresh repositories reconstruct the durable read path and the V1.25 durable
    boundary composes the per-project V1.24 summaries exactly;
3.  the aggregate is read-only (identical database state before and after),
    deterministic, and equal to the pure V1.25 boundary over the same durable
    per-project summaries.
"""

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
from trajectory_os.application.execution_effort_effective_summary import (
    build_effective_work_breakdown_effort_summary_durably,
)
from trajectory_os.application.execution_effort_portfolio_summary import (
    PortfolioEffectiveEffortSummary,
    build_portfolio_effective_effort_summary_durably,
    summarize_portfolio_effective_effort,
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

PORTFOLIO_ID = UUID("73111111-1111-4111-8111-111111111111")
PROJECT_A_ID = UUID("73333333-3333-4333-8333-333333333333")
PROJECT_B_ID = UUID("73444444-4444-4444-8444-444444444444")

TASK_A_ORDINARY_ID = UUID("73555555-5555-4555-8555-555555555555")
TASK_B_ORDINARY_ID = UUID("73666666-6666-4666-8666-666666666666")
TASK_B_CALIBRATED_ID = UUID("73777777-7777-4777-8777-777777777777")
TASK_B_NEVER_ESTIMATED_ID = UUID("73888888-8888-4888-8888-888888888888")

PROJECT_A_ESTIMATE_ID = UUID("20000000-0000-4000-8000-0000000000a1")
PROJECT_A_ROOT_ESTIMATE_ID = UUID("21000000-0000-4000-8000-0000000000a0")
PROJECT_B_ORDINARY_ESTIMATE_ID = UUID("30000000-0000-4000-8000-0000000000b1")
PROJECT_B_CALIBRATED_ESTIMATE_ID = UUID("40000000-0000-4000-8000-0000000000b2")

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)


def _current_portfolio() -> Portfolio:
    """Two PROJECTs: one fully estimated, one partially estimated.

    * PROJECT_A: the project root itself (900 s) and one ordinary task
      (600 s) are both estimated -> complete, total 1500 s.
    * PROJECT_B: the root and one task are deliberately never estimated;
      one ordinary task (120 s) and one calibrated task (450 s through the
      real V1.21 acceptance) are estimated -> partial, no complete total.
    """
    entities = [
        TrajectoryEntity(
            id=PROJECT_A_ID,
            entity_type=EntityType.PROJECT,
            title="project a",
            description="",
        ),
        TrajectoryEntity(
            id=TASK_A_ORDINARY_ID,
            entity_type=EntityType.TASK,
            title="a ordinary task",
            description="",
        ),
        TrajectoryEntity(
            id=PROJECT_B_ID,
            entity_type=EntityType.PROJECT,
            title="project b",
            description="",
        ),
        TrajectoryEntity(
            id=TASK_B_ORDINARY_ID,
            entity_type=EntityType.TASK,
            title="b ordinary task",
            description="",
        ),
        TrajectoryEntity(
            id=TASK_B_CALIBRATED_ID,
            entity_type=EntityType.TASK,
            title="b calibrated task",
            description="",
        ),
        TrajectoryEntity(
            id=TASK_B_NEVER_ESTIMATED_ID,
            entity_type=EntityType.TASK,
            title="b unestimated task",
            description="",
        ),
    ]
    relations = [
        TrajectoryRelation(
            source_id=TASK_A_ORDINARY_ID,
            target_id=PROJECT_A_ID,
            relation_type=RelationType.BELONGS_TO,
        ),
        TrajectoryRelation(
            source_id=TASK_B_ORDINARY_ID,
            target_id=PROJECT_B_ID,
            relation_type=RelationType.BELONGS_TO,
        ),
        TrajectoryRelation(
            source_id=TASK_B_CALIBRATED_ID,
            target_id=PROJECT_B_ID,
            relation_type=RelationType.BELONGS_TO,
        ),
        TrajectoryRelation(
            source_id=TASK_B_NEVER_ESTIMATED_ID,
            target_id=PROJECT_B_ID,
            relation_type=RelationType.BELONGS_TO,
        ),
    ]

    return Portfolio(
        id=PORTFOLIO_ID,
        name="V1.25 SQLite integration",
        entities=entities,
        relations=relations,
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
        project_id=PROJECT_B_ID,
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
        TASK_B_CALIBRATED_ID,
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


def test_portfolio_effective_summary_sqlite_round_trip_is_read_only_and_deterministic(
    tmp_path: Path,
) -> None:
    """Persist mixed history, reopen, then aggregate the portfolio read-only."""
    database_path = tmp_path / "trajectory-v125.sqlite3"

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

        estimate_repository.add(
            ExecutionEffortEstimate(
                id=PROJECT_A_ESTIMATE_ID,
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_A_ORDINARY_ID,
                duration_seconds=600,
                estimated_at=T0,
                source=SourceKind.USER_CONFIRMED,
            )
        )
        estimate_repository.add(
            ExecutionEffortEstimate(
                id=PROJECT_A_ROOT_ESTIMATE_ID,
                portfolio_id=PORTFOLIO_ID,
                entity_id=PROJECT_A_ID,
                duration_seconds=900,
                estimated_at=T0,
                source=SourceKind.USER_CONFIRMED,
            )
        )
        estimate_repository.add(
            ExecutionEffortEstimate(
                id=PROJECT_B_ORDINARY_ESTIMATE_ID,
                portfolio_id=PORTFOLIO_ID,
                entity_id=TASK_B_ORDINARY_ID,
                duration_seconds=120,
                estimated_at=T0,
                source=SourceKind.USER_CONFIRMED,
            )
        )

        # Real V1.21 acceptance atomically persists the calibrated V1.10
        # estimate (450 s) and its exact V1.21 provenance.
        accept_calibrated_estimate_revision_durably(
            _ready_proposal(portfolio_repository),
            estimate_id=PROJECT_B_CALIBRATED_ESTIMATE_ID,
            estimated_at=T1,
            portfolio_repository=portfolio_repository,
            revision_repository=revision_repository,
        )

    # Repositories above are deliberately closed. Everything below must be
    # reconstructed from the durable SQLite state.
    state_before = _database_state(database_path)

    # ------------------------------------------------------------------
    # Fresh repositories: real durable V1.25 read path.
    # ------------------------------------------------------------------
    with (
        SqlitePortfolioRepository(database_path) as portfolio_repository,
        SqliteExecutionEffortEstimateRepository(database_path) as estimate_reader,
        SqliteCalibratedEstimateRevisionRepository(
            database_path
        ) as provenance_reader,
    ):
        first = build_portfolio_effective_effort_summary_durably(
            portfolio_id=PORTFOLIO_ID,
            portfolio_repository=portfolio_repository,
            estimate_reader=estimate_reader,
            provenance_reader=provenance_reader,
        )

        second = build_portfolio_effective_effort_summary_durably(
            portfolio_id=PORTFOLIO_ID,
            portfolio_repository=portfolio_repository,
            estimate_reader=estimate_reader,
            provenance_reader=provenance_reader,
        )

        # The durable per-project V1.24 summaries recomputed independently
        # must feed the pure V1.25 boundary to the exact same aggregate.
        durably_per_project = [
            build_effective_work_breakdown_effort_summary_durably(
                portfolio_id=PORTFOLIO_ID,
                project_id=project_id,
                portfolio_repository=portfolio_repository,
                estimate_reader=estimate_reader,
                provenance_reader=provenance_reader,
            )
            for project_id in (PROJECT_A_ID, PROJECT_B_ID)
        ]

    assert isinstance(first, PortfolioEffectiveEffortSummary)
    assert first == second, "identical durable reads must be deterministic"
    assert first == summarize_portfolio_effective_effort(
        PORTFOLIO_ID,
        durably_per_project,
    )

    # ------------------------------------------------------------------
    # Exact portfolio-level classification and effort arithmetic.
    # (In V1.24 the PROJECT root itself is an entity of the work breakdown.)
    #
    # PROJECT_A: known 1500 s (900 s root + 600 s task), 2 estimated,
    #            0 unestimated, total 1500 s.
    # PROJECT_B: known 570 s (120 s + 450 s), 2 estimated,
    #            2 unestimated (root + one task), no total.
    # Portfolio: known 2070 s, 4 estimated, 2 unestimated, no total.
    # ------------------------------------------------------------------
    project_a = durably_per_project[0]
    project_b = durably_per_project[1]

    assert project_a.project_id == PROJECT_A_ID
    assert project_a.ordinary_estimate_count == 2
    assert project_a.calibrated_estimate_count == 0
    assert project_a.effort.known_duration_seconds == 1500
    assert project_a.effort.estimated_entity_count == 2
    assert project_a.effort.unestimated_entity_count == 0
    assert project_a.effort.total_duration_seconds == 1500

    assert project_b.project_id == PROJECT_B_ID
    assert project_b.ordinary_estimate_count == 1
    assert project_b.calibrated_estimate_count == 1
    assert project_b.effort.known_duration_seconds == 570
    assert project_b.effort.estimated_entity_count == 2
    assert project_b.effort.unestimated_entity_count == 2
    assert project_b.effort.total_duration_seconds is None

    assert first.portfolio_id == PORTFOLIO_ID
    assert first.project_count == 2
    assert first.ordinary_estimate_count == 3
    assert first.calibrated_estimate_count == 1

    assert first.known_duration_seconds == 2070
    assert first.estimated_entity_count == 4
    assert first.unestimated_entity_count == 2
    assert first.total_duration_seconds is None
    assert len(first.projects) == 2
    assert first.projects[0].project_id == project_a.project_id
    assert first.projects[1].project_id == project_b.project_id

    # ------------------------------------------------------------------
    # V1.25 must create neither rows nor schema while building.
    # ------------------------------------------------------------------
    state_after = _database_state(database_path)
    assert state_after == state_before
