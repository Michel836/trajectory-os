"""Real-SQLite integration tests for the V1.22 current-effective
execution-effort estimate resolution.

Verifies the durable boundary against a real SQLite database through the
REAL adapters (``SqliteExecutionEffortEstimateRepository`` as the V1.10
reader and ``SqliteCalibratedEstimateRevisionRepository`` as the V1.21
provenance repository):

* a full round-trip (plain V1.10 revision + V1.21 accepted calibrated
  revision persisted, ALL repositories closed, repositories re-created)
  resolves EXACTLY the V1.10 latest revision and, only when that selected
  revision was calibrated, the EXACT V1.21 provenance;

* empty scope resolves explicit NO_ESTIMATE and changes nothing;

* a historical estimate of an entity REMOVED from the CURRENT portfolio
  snapshot remains resolvable — no Portfolio load is involved at all (the
  boundary takes no portfolio repository argument);

* mixed timezone offsets are resolved by actual chronological instant,
  never by lexical ISO text;

* a historical calibrated (provenanced) estimate is NOT preferred over a
  newer ordinary estimate; the result carries no provenance;

* V1.22 introduces NO new durable state: after resolution the exact same
  tables and the exact same row counts exist — no writes, no updates,
  no deletes, no ``is_current`` flag, no materialized effective state,
  no new schema;

* repeated equivalent resolutions are deterministic and equivalent.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence import (
    SqliteCalibratedEstimateRevisionRepository,
    SqliteExecutionEffortEstimateRepository,
    SqlitePortfolioRepository,
)
from trajectory_os.adapters.persistence.models import (
    AcceptedCalibratedEstimateRevisionRow,
    EntityRow,
    ExecutionEffortEstimateRow,
    PortfolioRow,
    RelationRow,
)
from trajectory_os.application import (
    EffectiveExecutionEffortEstimate,
    EffectiveExecutionEffortEstimateStatus,
    accept_calibrated_estimate_revision_durably,
    resolve_effective_execution_effort_estimate_durably,
)
from trajectory_os.application.execution_effort_calibration_composition import (
    EffectiveCalibrationApplicationResult,
    EffectiveCalibrationApplicationStatus,
)
from trajectory_os.application.execution_effort_calibration_entity_binding import (
    CalibratedEstimateRevisionProposal,
    bind_effort_calibration_to_current_entity,
)
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactor,
)
from trajectory_os.domain.execution_effort_calibration_factor_application import (
    apply_effective_effort_calibration_factor,
)
from trajectory_os.domain.execution_effort_estimates import (
    create_execution_effort_estimate,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("71111111-1111-4111-8111-111111111111")
PROJECT_ID = UUID("73333333-3333-4333-8333-333333333333")
TASK_ID = UUID("75555555-5555-4555-8555-555555555555")
CANDIDATE = 300
CALIBRATED = CANDIDATE * 3 // 2
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
PLUS_TWO = timezone(timedelta(hours=2))


# --- Fixtures / helpers ---------------------------------------------------------


def _current_portfolio() -> Portfolio:
    project = TrajectoryEntity(
        id=PROJECT_ID, entity_type=EntityType.PROJECT, title="p", description=""
    )
    task = TrajectoryEntity(id=TASK_ID, entity_type=EntityType.TASK, title="t", description="")
    return Portfolio(
        id=PORTFOLIO_ID,
        name="canonical",
        entities=[project, task],
        relations=[
            TrajectoryRelation(
                source_id=TASK_ID,
                target_id=PROJECT_ID,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )


def _trimmed_portfolio() -> Portfolio:
    """CURRENT snapshot in which the task was REMOVED (entity id is not an FK)."""
    project = TrajectoryEntity(
        id=PROJECT_ID, entity_type=EntityType.PROJECT, title="p", description=""
    )
    return Portfolio(id=PORTFOLIO_ID, name="trimmed", entities=[project], relations=[])


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


class _InMemoryPortfolioRepository:
    """Read-only CURRENT-authority repository for the V1.21 acceptance step."""

    def __init__(self, portfolio: Portfolio | None) -> None:
        self._portfolio = portfolio

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        if self._portfolio is not None and portfolio_id == self._portfolio.id:
            return self._portfolio
        return None

    def save(self, portfolio: Portfolio) -> None:
        raise AssertionError(
            "the V1.21 step under test must never save a portfolio here"
        )


def _ready_proposal() -> CalibratedEstimateRevisionProposal:
    result = EffectiveCalibrationApplicationResult(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=CANDIDATE,
        status=EffectiveCalibrationApplicationStatus.AVAILABLE,
        proposal=apply_effective_effort_calibration_factor(CANDIDATE, _factor()),
    )
    return bind_effort_calibration_to_current_entity(
        result, TASK_ID, _InMemoryPortfolioRepository(_current_portfolio())
    )


def _setup_db(db_path: Path) -> SqlitePortfolioRepository:
    repository = SqlitePortfolioRepository(database_path=db_path)
    repository.save(_current_portfolio())
    return repository


def _add_plain_estimate(
    db_path: Path, duration_seconds: int, estimated_at: datetime
) -> UUID:
    """Persist one plain V1.10 revision through the REAL adapter."""
    repo = SqliteExecutionEffortEstimateRepository(database_path=db_path)
    try:
        estimate = create_execution_effort_estimate(
            _current_portfolio(), uuid4(), TASK_ID, duration_seconds, estimated_at
        )
        repo.add(estimate)
        return estimate.id
    finally:
        repo.close()


def _accept_calibrated(db_path: Path, estimate_id: UUID, estimated_at: datetime) -> None:
    """Run the REAL V1.21 acceptance to persist a calibrated revision."""
    revision_repo = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
    try:
        result = accept_calibrated_estimate_revision_durably(
            _ready_proposal(),
            estimate_id=estimate_id,
            estimated_at=estimated_at,
            portfolio_repository=_InMemoryPortfolioRepository(_current_portfolio()),
            revision_repository=revision_repo,
        )
        assert result.estimate.duration_seconds == CALIBRATED
    finally:
        revision_repo.close()


def _counts(db_path: Path) -> dict[str, int]:
    """Row counts of every relevant durable table (read-only probe)."""
    repo = SqlitePortfolioRepository(database_path=db_path)
    try:
        with Session(repo.engine) as session:
            return {
                "portfolios": int(
                    session.scalar(select(func.count()).select_from(PortfolioRow)) or 0
                ),
                "entities": int(
                    session.scalar(select(func.count()).select_from(EntityRow)) or 0
                ),
                "relations": int(
                    session.scalar(select(func.count()).select_from(RelationRow)) or 0
                ),
                "estimates": int(
                    session.scalar(
                        select(func.count()).select_from(ExecutionEffortEstimateRow)
                    )
                    or 0
                ),
                "provenance": int(
                    session.scalar(
                        select(func.count()).select_from(
                            AcceptedCalibratedEstimateRevisionRow
                        )
                    )
                    or 0
                ),
            }
    finally:
        repo.close()


def _tables(db_path: Path) -> frozenset[str]:
    """The exact durable schema (table names) as a read-only probe."""
    connection = sqlite3.connect(db_path)
    try:
        return frozenset(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        )
    finally:
        connection.close()


def _resolve(db_path: Path, entity_id: UUID = TASK_ID) -> EffectiveExecutionEffortEstimate:
    """Resolve through freshly created REAL repositories, closing them after."""
    estimates = SqliteExecutionEffortEstimateRepository(database_path=db_path)
    provenance = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
    try:
        return resolve_effective_execution_effort_estimate_durably(
            PORTFOLIO_ID,
            entity_id,
            estimates,
            provenance,
        )
    finally:
        estimates.close()
        provenance.close()


# --- Round-trip after repository recreation -------------------------------------


def test_roundtrip_resolves_latest_estimate_and_exact_provenance(tmp_path: Path) -> None:
    db_path = tmp_path / "v122.db"
    portfolio_repo = _setup_db(db_path)
    try:
        plain_id = _add_plain_estimate(db_path, 100, T0)
        calibrated_id = uuid4()
        _accept_calibrated(db_path, calibrated_id, T1)
    finally:
        portfolio_repo.close()
        del plain_id

    # ALL repositories closed at this point: the resolution below MUST be
    # able to resolve the durable history through freshly created ones.
    result = _resolve(db_path)
    assert result.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE
    assert result.estimate is not None
    assert result.estimate.id == calibrated_id
    assert result.estimate.portfolio_id == PORTFOLIO_ID
    assert result.estimate.entity_id == TASK_ID
    assert result.estimate.duration_seconds == CALIBRATED
    assert result.estimate.estimated_at == T1
    assert result.estimate.source is SourceKind.USER_CONFIRMED

    # The selected revision WAS accepted through V1.21: the EXACT provenance.
    assert result.calibrated_provenance is not None
    assert result.calibrated_provenance.estimate_id == calibrated_id
    assert result.calibrated_provenance.portfolio_id == PORTFOLIO_ID
    assert result.calibrated_provenance.entity_id == TASK_ID
    assert result.calibrated_provenance.calibrated_duration_seconds == CALIBRATED
    assert result.calibrated_provenance.estimated_at == T1
    # The full nested V1.20 -> V1.19 -> V1.18 chain survives the round-trip.
    assert result.calibrated_provenance.source_proposal is not None
    assert (
        result.calibrated_provenance.source_proposal.calibrated_duration_seconds
        == CALIBRATED
    )

    # History intact: both revisions + the one provenance row.
    counts = _counts(db_path)
    assert counts["estimates"] == 2
    assert counts["provenance"] == 1


def test_roundtrip_historical_calibrated_not_preferred_over_newer_plain(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "notpreferred.db"
    portfolio_repo = _setup_db(db_path)
    try:
        # OLDER calibrated revision (persisted via the REAL V1.21 path):
        _accept_calibrated(db_path, uuid4(), T0)
        # NEWER plain V1.10 revision:
        newer_id = _add_plain_estimate(db_path, 999, T1)
    finally:
        portfolio_repo.close()

    result = _resolve(db_path)
    assert result.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE
    assert result.estimate is not None
    assert result.estimate.id == newer_id
    assert result.estimate.duration_seconds == 999
    # The old calibrated row exists, but must NOT be reported and its
    # provenance must NOT be attached to a plain estimate.
    assert result.calibrated_provenance is None
    counts = _counts(db_path)
    assert counts["estimates"] == 2
    assert counts["provenance"] == 1


# --- Empty scope ------------------------------------------------------------------


def test_empty_scope_is_no_estimate_and_changes_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    portfolio_repo = _setup_db(db_path)
    try:
        before_counts = _counts(db_path)
        before_tables = _tables(db_path)
        result = _resolve(db_path)
    finally:
        portfolio_repo.close()

    assert result.status is EffectiveExecutionEffortEstimateStatus.NO_ESTIMATE
    assert result.estimate is None
    assert result.calibrated_provenance is None

    # NO new durable state of any kind was introduced by the resolution:
    assert _counts(db_path) == before_counts
    assert _tables(db_path) == before_tables


# --- Removed CURRENT entity ---------------------------------------------------------


def test_removed_current_entity_history_remains_resolvable(tmp_path: Path) -> None:
    db_path = tmp_path / "removed-entity.db"
    portfolio_repo = _setup_db(db_path)
    try:
        estimate_id = _add_plain_estimate(db_path, 250, T0)
        # Replace the CURRENT snapshot with one where TASK_ID no longer
        # exists — the durable estimate rows must survive.
        portfolio_repo.save(_trimmed_portfolio())
    finally:
        portfolio_repo.close()

    result = _resolve(db_path)
    assert result.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE
    assert result.estimate is not None
    assert result.estimate.id == estimate_id
    assert result.estimate.entity_id == TASK_ID
    assert result.calibrated_provenance is None
    # The task is gone from the CURRENT snapshot, but the estimate remains
    # resolvable — with no portfolio argument to the boundary at all.
    counts = _counts(db_path)
    assert counts["entities"] == 1  # only the project remains in the snapshot
    assert counts["estimates"] == 1


def test_removed_entity_calibrated_history_still_returns_provenance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "removed-calibrated.db"
    portfolio_repo = _setup_db(db_path)
    try:
        calibrated_id = uuid4()
        _accept_calibrated(db_path, calibrated_id, T0)
        portfolio_repo.save(_trimmed_portfolio())
    finally:
        portfolio_repo.close()

    result = _resolve(db_path)
    assert result.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE
    assert result.estimate is not None
    assert result.estimate.id == calibrated_id
    assert result.calibrated_provenance is not None
    assert result.calibrated_provenance.estimate_id == calibrated_id


# --- Chronology / determinism ---------------------------------------------------------


def test_mixed_timezone_offsets_resolved_by_actual_instant(tmp_path: Path) -> None:
    db_path = tmp_path / "offsets.db"
    portfolio_repo = _setup_db(db_path)
    try:
        # Lexical ISO winner: "2026-09-01T13:00:00+02:00" (instant 11:00Z).
        _add_plain_estimate(db_path, 10, datetime(2026, 9, 1, 13, 0, tzinfo=PLUS_TWO))
        # Actual-instant winner: 11:30Z — later even though its ISO text
        # ("2026-09-01T11:30:00+00:00") sorts lexically BEFORE the other one.
        true_id = _add_plain_estimate(db_path, 20, datetime(2026, 9, 1, 11, 30, tzinfo=UTC))
    finally:
        portfolio_repo.close()

    result = _resolve(db_path)
    assert result.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE
    assert result.estimate is not None
    assert result.estimate.id == true_id
    assert result.estimate.duration_seconds == 20


def test_repeated_equivalent_resolutions_are_deterministic(tmp_path: Path) -> None:
    db_path = tmp_path / "deterministic.db"
    portfolio_repo = _setup_db(db_path)
    try:
        _add_plain_estimate(db_path, 100, T0)
        _accept_calibrated(db_path, uuid4(), T1)
        results = [_resolve(db_path) for _ in range(3)]
    finally:
        portfolio_repo.close()

    first, second, third = results
    assert second == first
    assert third == first
    assert first.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE
    assert first.calibrated_provenance is not None


def test_foreign_entity_scope_resolves_no_estimate_without_state_change(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "foreign-scope.db"
    portfolio_repo = _setup_db(db_path)
    try:
        _add_plain_estimate(db_path, 100, T0)
        before_counts = _counts(db_path)
        before_tables = _tables(db_path)

        result = _resolve(db_path)
        assert result.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE

        # A foreign (unknown) entity scope in the same portfolio resolves
        # to an explicit NO_ESTIMATE without touching any durable state.
        foreign_entity = uuid4()
        foreign_result = _resolve(db_path, entity_id=foreign_entity)
    finally:
        portfolio_repo.close()

    assert foreign_result.status is EffectiveExecutionEffortEstimateStatus.NO_ESTIMATE
    assert foreign_result.estimate is None
    assert foreign_result.calibrated_provenance is None
    # Read-only: the whole exercise changed nothing durable.
    assert _counts(db_path) == before_counts
    assert _tables(db_path) == before_tables
