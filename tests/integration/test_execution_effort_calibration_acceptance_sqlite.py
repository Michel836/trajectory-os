"""Real-SQLite integration tests for durable acceptance of a V1.20
calibrated estimate revision (V1.21).

Verifies the actual adapter against a real SQLite database:

* ONE atomic transaction writes BOTH the V1.10 estimate row AND the V1.21
  provenance row; a simulated failure of the second INSERT rolls back
  BOTH (append-only, no partial states);
* storage shapes: 36-char TEXT UUIDs, INTEGER durations, TEXT
  ``entity_type``, and a genuine JSON ``accepted_v120_snapshot`` carrying
  the full nested V1.20 -> V1.19 -> V1.18 chain;
* durable read-back through the V1.10 repository AND the new
  ``get_provenance`` read path returns value-equal objects;
* plain V1.10 estimates (written without provenance) read back with
  ``provenance is None``;
* duplicate ``estimate_id`` (estimate or provenance) is refused with the
  precise existing domain error / the V1.21 persistence error, writing
  nothing;
* repeated equivalent acceptances (different caller-supplied ids) coexist;
* surviving snapshot replacement: removing the entity from the portfolio
  never deletes either row;
* ``estimated_at`` offset preservation across storage;
* a closed repository refuses late use.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, insert, select
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence import (
    DuplicateCalibratedEstimateRevisionError,
    SqliteCalibratedEstimateRevisionRepository,
    SqliteExecutionEffortEstimateRepository,
    SqlitePortfolioRepository,
)
from trajectory_os.adapters.persistence.models import (
    AcceptedCalibratedEstimateRevisionRow,
    ExecutionEffortEstimateRow,
)
from trajectory_os.adapters.persistence.sqlite_execution_effort_estimates import (
    DuplicateExecutionEffortEstimateError,
)
from trajectory_os.application import accept_calibrated_estimate_revision_durably
from trajectory_os.application.execution_effort_calibration_composition import (
    EffectiveCalibrationApplicationResult,
    EffectiveCalibrationApplicationStatus,
)
from trajectory_os.application.execution_effort_calibration_entity_binding import (
    CalibratedEstimateRevisionProposal,
    bind_effort_calibration_to_current_entity,
)
from trajectory_os.domain.entities import EntityType, TrajectoryEntity
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
ESTIMATE_NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
DECIDED_AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
PLUS_TWO = timezone(timedelta(hours=2))
ESTIMATE_NOW_PLUS_TWO = datetime(2026, 9, 1, 10, 0, tzinfo=PLUS_TWO)


# --- Fixtures / helpers -----------------------------------------------------


def _factor() -> EffectiveEffortCalibrationFactor:
    return EffectiveEffortCalibrationFactor(
        entity_type=EntityType.TASK,
        decision_id=UUID("aaaa1111-1111-4111-8111-111111111111"),
        decided_at=DECIDED_AT,
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=150,
        factor_numerator=3,
        factor_denominator=2,
    )


def _available_result() -> EffectiveCalibrationApplicationResult:
    return EffectiveCalibrationApplicationResult(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=EntityType.TASK,
        candidate_duration_seconds=CANDIDATE,
        status=EffectiveCalibrationApplicationStatus.AVAILABLE,
        proposal=apply_effective_effort_calibration_factor(CANDIDATE, _factor()),
    )


def _current_portfolio() -> Portfolio:
    project = TrajectoryEntity(
        id=PROJECT_ID,
        entity_type=EntityType.PROJECT,
        title="p",
        description="",
    )
    task = TrajectoryEntity(
        id=TASK_ID,
        entity_type=EntityType.TASK,
        title="t",
        description="",
    )
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


class _InMemoryPortfolioRepository:
    """Read-only PortfolioRepository used as CURRENT authority in tests."""

    def __init__(self, portfolio: Portfolio | None) -> None:
        self._portfolio = portfolio

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        if self._portfolio is not None and portfolio_id == self._portfolio.id:
            return self._portfolio
        return None

    def save(self, portfolio: Portfolio) -> None:
        del portfolio
        raise AssertionError("V1.21 acceptance must never save the portfolio")


def _ready_proposal() -> CalibratedEstimateRevisionProposal:
    portfolio = _current_portfolio()
    repository = _InMemoryPortfolioRepository(portfolio)
    return bind_effort_calibration_to_current_entity(
        _available_result(), TASK_ID, repository
    )


def _setup_db(db_path: Path) -> SqlitePortfolioRepository:
    """Persist the canonical portfolio (FK target) into the real DB."""
    repository = SqlitePortfolioRepository(database_path=db_path)
    repository.save(_current_portfolio())
    return repository


def _estimate_count(db_path: Path) -> int:
    repository = SqlitePortfolioRepository(database_path=db_path)
    try:
        with Session(repository.engine) as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(ExecutionEffortEstimateRow)
                )
            )
    finally:
        repository.close()


def _provenance_count(db_path: Path) -> int:
    repository = SqlitePortfolioRepository(database_path=db_path)
    try:
        with Session(repository.engine) as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(
                        AcceptedCalibratedEstimateRevisionRow
                    )
                )
            )
    finally:
        repository.close()


def _accept(db_path: Path, estimate_id: UUID, estimated_at: datetime) -> Any:
    revision_repo = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
    try:
        return accept_calibrated_estimate_revision_durably(
            _ready_proposal(),
            estimate_id=estimate_id,
            estimated_at=estimated_at,
            portfolio_repository=_InMemoryPortfolioRepository(_current_portfolio()),
            revision_repository=revision_repo,
        )
    finally:
        revision_repo.close()


# --- Happy path --------------------------------------------------------------


def test_accept_persists_both_rows_and_reads_them_back(tmp_path: Path) -> None:
    db_path = tmp_path / "v121.db"
    portfolio_repo = _setup_db(db_path)
    proposal = _ready_proposal()
    estimate_id = uuid4()
    try:
        result = _accept(db_path, estimate_id, ESTIMATE_NOW)

        assert _estimate_count(db_path) == 1
        assert _provenance_count(db_path) == 1

        # V1.10 read path: exact values.
        estimate_repo = SqliteExecutionEffortEstimateRepository(database_path=db_path)
        revision_repo = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
        try:
            stored = estimate_repo.get(estimate_id)
            assert stored is not None
            assert stored.id == result.estimate.id
            assert stored.portfolio_id == PORTFOLIO_ID
            assert stored.entity_id == TASK_ID
            assert stored.duration_seconds == CALIBRATED
            assert stored.estimated_at == ESTIMATE_NOW

            # V1.21 read path: EXACT provenance record, full nested chain.
            stored_prov = revision_repo.get_provenance(estimate_id)
            assert stored_prov is not None
            assert stored_prov == result.provenance
            assert stored_prov.source_proposal == proposal
            assert stored_prov.source_proposal.status.value == "ready"
            assert stored_prov.source_proposal.source_result.proposal is not None
            assert (
                stored_prov.source_proposal.source_result.proposal.calibrated_duration_seconds
                == CALIBRATED
            )
            assert revision_repo.get_provenance(uuid4()) is None
        finally:
            estimate_repo.close()
            revision_repo.close()
    finally:
        portfolio_repo.close()


def test_provenance_row_storage_shapes_and_snapshot(tmp_path: Path) -> None:
    """36-char TEXT UUIDs, INTEGER durations, TEXT entity_type, JSON chain."""
    db_path = tmp_path / "shapes.db"
    portfolio_repo = _setup_db(db_path)
    estimate_id = uuid4()
    try:
        _accept(db_path, estimate_id, ESTIMATE_NOW)

        revision_repo = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
        try:
            with Session(revision_repo.engine) as session:
                est_row = session.scalars(select(ExecutionEffortEstimateRow)).one()
                assert est_row.id == str(estimate_id)
                assert est_row.portfolio_id == str(PORTFOLIO_ID)
                assert est_row.entity_id == str(TASK_ID)
                assert est_row.duration_seconds == CALIBRATED
                assert est_row.estimated_at == ESTIMATE_NOW.isoformat()
                assert est_row.source == "user_confirmed"

                prov_row = session.scalars(
                    select(AcceptedCalibratedEstimateRevisionRow)
                ).one()
                assert prov_row.estimate_id == str(estimate_id)
                assert prov_row.portfolio_id == str(PORTFOLIO_ID)
                assert prov_row.project_id == str(PROJECT_ID)
                assert prov_row.entity_id == str(TASK_ID)
                assert prov_row.entity_type == "task"
                assert prov_row.candidate_duration_seconds == CANDIDATE
                assert prov_row.calibrated_duration_seconds == CALIBRATED
                assert prov_row.estimated_at == ESTIMATE_NOW.isoformat()

                # The snapshot is genuine JSON carrying the full V1.20 ->
                # V1.19 -> V1.18 provenance chain.
                payload: dict[str, Any] = json.loads(prov_row.accepted_v120_snapshot)
                assert payload["status"] == "ready"
                assert payload["candidate_duration_seconds"] == CANDIDATE
                assert payload["calibrated_duration_seconds"] == CALIBRATED
                assert payload["source_result"]["status"] == "available"
                v118 = payload["source_result"]["proposal"]
                assert v118["factor_numerator"] == 3
                assert v118["factor_denominator"] == 2
                assert v118["calibrated_duration_seconds"] == CALIBRATED
        finally:
            revision_repo.close()
    finally:
        portfolio_repo.close()


# --- Plain V1.10 estimates carry NO provenance -------------------------------


def test_plain_v110_estimate_has_no_provenance(tmp_path: Path) -> None:
    db_path = tmp_path / "plain.db"
    portfolio_repo = _setup_db(db_path)
    estimate_id = uuid4()
    try:
        estimate = create_execution_effort_estimate(
            _current_portfolio(), estimate_id, TASK_ID, 120, ESTIMATE_NOW
        )
        estimate_repo = SqliteExecutionEffortEstimateRepository(database_path=db_path)
        revision_repo = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
        try:
            estimate_repo.add(estimate)
            assert estimate_repo.get(estimate_id) == estimate
            assert revision_repo.get_provenance(estimate_id) is None
            assert revision_repo.get_provenance(uuid4()) is None
        finally:
            estimate_repo.close()
            revision_repo.close()
    finally:
        portfolio_repo.close()


# --- Duplicate handling -------------------------------------------------------


def test_duplicate_estimate_id_rejected_with_existing_domain_error(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dup_est.db"
    portfolio_repo = _setup_db(db_path)
    estimate_id = uuid4()
    try:
        _accept(db_path, estimate_id, ESTIMATE_NOW)
        with pytest.raises(DuplicateExecutionEffortEstimateError):
            _accept(db_path, estimate_id, ESTIMATE_NOW)
        # Nothing doubled up.
        assert _estimate_count(db_path) == 1
        assert _provenance_count(db_path) == 1
    finally:
        portfolio_repo.close()


def test_preexisting_provenance_without_estimate_rejected_with_v121_error(
    tmp_path: Path,
) -> None:
    """A provenance row whose id is NOT a known estimate row (e.g. the
    estimate row was removed out-of-band) still collides at the one-to-one
    append boundary and must surface the V1.21 persistence error.
    """
    db_path = tmp_path / "dup_prov.db"
    portfolio_repo = _setup_db(db_path)
    proposal = _ready_proposal()
    estimate_id = uuid4()
    try:
        # Seed ONLY a provenance row (test fixture, not part of the API).
        revision_repo = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
        try:
            with Session(revision_repo.engine) as session:
                session.execute(
                    insert(AcceptedCalibratedEstimateRevisionRow).values(
                        estimate_id=str(estimate_id),
                        portfolio_id=str(PORTFOLIO_ID),
                        project_id=str(PROJECT_ID),
                        entity_id=str(TASK_ID),
                        entity_type="task",
                        candidate_duration_seconds=CANDIDATE,
                        calibrated_duration_seconds=CALIBRATED,
                        estimated_at=ESTIMATE_NOW.isoformat(),
                        accepted_v120_snapshot=proposal.model_dump_json(),
                    )
                )
                session.commit()
            assert _provenance_count(db_path) == 1

            with pytest.raises(DuplicateCalibratedEstimateRevisionError):
                _accept(db_path, estimate_id, ESTIMATE_NOW)

            # The failed attempt wrote NOTHING new.
            assert _estimate_count(db_path) == 0
            assert _provenance_count(db_path) == 1
        finally:
            revision_repo.close()
    finally:
        portfolio_repo.close()


# --- Repeated equivalent acceptances coexist ---------------------------------


def test_repeated_equivalent_acceptances_with_distinct_ids_all_persist(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "repeat.db"
    portfolio_repo = _setup_db(db_path)
    proposal = _ready_proposal()
    first = uuid4()
    second = uuid4()
    try:
        r1 = _accept(db_path, first, ESTIMATE_NOW)
        r2 = _accept(db_path, second, ESTIMATE_NOW)
        assert r1.estimate.id == first
        assert r2.estimate.id == second
        assert _estimate_count(db_path) == 2
        assert _provenance_count(db_path) == 2

        revision_repo = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
        try:
            p1 = revision_repo.get_provenance(first)
            p2 = revision_repo.get_provenance(second)
            assert p1 is not None and p2 is not None
            assert p1.estimate_id != p2.estimate_id
            assert p1.source_proposal == proposal
            assert p2.source_proposal == proposal
        finally:
            revision_repo.close()
    finally:
        portfolio_repo.close()


# --- Survival: entity removal does not remove persisted rows -----------------


def test_entity_removal_does_not_delete_estimate_or_provenance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "survive.db"
    portfolio_repo = _setup_db(db_path)
    estimate_id = uuid4()
    try:
        _accept(db_path, estimate_id, ESTIMATE_NOW)

        # A later durable snapshot replacement WITHOUT the task (the
        # entity was removed from the portfolio in a subsequent transition).
        trimmed = Portfolio(
            id=PORTFOLIO_ID,
            name="canonical",
            entities=[
                TrajectoryEntity(
                    id=PROJECT_ID,
                    entity_type=EntityType.PROJECT,
                    title="p",
                    description="",
                )
            ],
            relations=[],
        )
        portfolio_repo.save(trimmed)

        # BOTH rows survive the entity removal.
        estimate_repo = SqliteExecutionEffortEstimateRepository(database_path=db_path)
        revision_repo = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
        try:
            assert estimate_repo.get(estimate_id) is not None
            assert revision_repo.get_provenance(estimate_id) is not None
        finally:
            estimate_repo.close()
            revision_repo.close()
    finally:
        portfolio_repo.close()


# --- Atomicity: second INSERT failure rolls BOTH back -------------------------


def test_second_insert_failure_rolls_back_both_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "atomic.db"
    portfolio_repo = _setup_db(db_path)
    estimate_id = uuid4()

    revision_repo = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
    try:
        # Force the provenance INSERT — the SECOND statement of the single
        # transaction — to fail inside the engine.
        @event.listens_for(revision_repo.engine, "before_cursor_execute")
        def _fail_provenance_insert(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: Any,
        ) -> None:
            words = statement.split()
            if (
                len(words) >= 3
                and words[2].upper() == "ACCEPTED_CALIBRATED_ESTIMATE_REVISIONS"
                and words[0].upper() == "INSERT"
            ):
                raise RuntimeError("simulated provenance INSERT failure")

        with pytest.raises(RuntimeError, match="simulated provenance INSERT failure"):
            accept_calibrated_estimate_revision_durably(
                _ready_proposal(),
                estimate_id=estimate_id,
                estimated_at=ESTIMATE_NOW,
                portfolio_repository=_InMemoryPortfolioRepository(
                    _current_portfolio()
                ),
                revision_repository=revision_repo,
            )

        # NO partial state: BOTH tables empty.
        assert _estimate_count(db_path) == 0
        assert _provenance_count(db_path) == 0
    finally:
        portfolio_repo.close()
        revision_repo.close()


# --- estimated_at offset preservation ----------------------------------------


def test_estimated_at_offset_preserved_through_storage(tmp_path: Path) -> None:
    db_path = tmp_path / "offset.db"
    portfolio_repo = _setup_db(db_path)
    estimate_id = uuid4()
    try:
        result = _accept(db_path, estimate_id, ESTIMATE_NOW_PLUS_TWO)
        revision_repo = SqliteCalibratedEstimateRevisionRepository(database_path=db_path)
        estimate_repo = SqliteExecutionEffortEstimateRepository(database_path=db_path)
        try:
            est = estimate_repo.get(estimate_id)
            stored = revision_repo.get_provenance(estimate_id)
            assert est is not None
            assert est.estimated_at == ESTIMATE_NOW_PLUS_TWO
            assert est.estimated_at.utcoffset() == timedelta(hours=2)
            assert stored is not None
            assert stored.estimated_at == ESTIMATE_NOW_PLUS_TWO
            assert result.estimate.estimated_at == ESTIMATE_NOW_PLUS_TWO
        finally:
            estimate_repo.close()
            revision_repo.close()
    finally:
        portfolio_repo.close()


# --- Lifecycle ----------------------------------------------------------------


def test_repository_close_is_safe_and_context_manager_works(tmp_path: Path) -> None:
    db_path = tmp_path / "close.db"
    portfolio_repo = _setup_db(db_path)
    try:
        with SqliteCalibratedEstimateRevisionRepository(database_path=db_path) as repo:
            assert repo.engine is not None
        # close() releases pools (house convention) and is safe to repeat.
        SqliteCalibratedEstimateRevisionRepository(database_path=db_path).close()
    finally:
        portfolio_repo.close()
