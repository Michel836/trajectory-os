"""Integration tests: V1.17 effective resolution over real SQLite V1.16 state.

Covers: exact-resolution after repository recreation, chronological
instant ordering with mixed timezone offsets and UUID integer tie-breaks,
later REJECT/DEFER records NOT changing persisted V1.16 history or the
effective selection, zero writes (row counts, table set, and row data all
unchanged), no new persistence table or materialized state, and NO V1.15
re-derivation through any patched derivation boundary.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from trajectory_os.adapters.persistence import (
    SqliteExecutionEffortCalibrationFactorDecisionRepository,
    SqlitePortfolioRepository,
)
from trajectory_os.application import (
    execution_effort_calibration_factor_decisions as decisions_app,
)
from trajectory_os.application import (
    execution_effort_calibration_factor_proposals as proposals_app,
)
from trajectory_os.application import (
    execution_effort_comparison as comparison_app,
)
from trajectory_os.application import (
    execution_effort_measurement as measurement_app,
)
from trajectory_os.application import (
    execution_effort_planning as planning_app,
)
from trajectory_os.application.execution_effort_calibration_effective_factors import (
    resolve_effective_effort_calibration_factors_durably,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort_calibration_factor_decisions import (
    EffortCalibrationDecision,
    EffortCalibrationFactorDecision,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
)
from trajectory_os.domain.portfolio import Portfolio

PORTFOLIO_ID = UUID("7d7d7d7d-7d7d-4d7d-8d7d-7d7d7d7d7d7d")
PROJECT_ID = UUID("7e7e7e7e-7e7e-4e7e-8e7e-7e7e7e7e7e7e")
NOW = datetime(2025, 6, 1, 9, 0, tzinfo=UTC)

TIE_UUID_LOW = UUID("7f000000-0000-4000-8000-000000000001")
TIE_UUID_HIGH = UUID("7f000000-0000-4000-8000-0000000000ff")


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "v117.db"
    with SqlitePortfolioRepository(path) as portfolio_repo:
        portfolio_repo.save(
            Portfolio(
                id=PORTFOLIO_ID,
                name="V1.17 SQLite Portfolio",
                entities=[
                    TrajectoryEntity(
                        id=PROJECT_ID,
                        entity_type=EntityType.PROJECT,
                        title="V1.17 Project",
                        status=EntityStatus.COMPLETED,
                        source=SourceKind.USER_CONFIRMED,
                        confidence=1.0,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                ],
                relations=[],
            )
        )
    return path


def _decision(
    *,
    entity_type: EntityType = EntityType.TASK,
    decision: EffortCalibrationDecision = EffortCalibrationDecision.ACCEPT,
    decided_at: datetime,
    decision_id: UUID | None = None,
    sample_count: int = 5,
    minimum_required_sample_count: int = 1,
    planned: int = 100,
    actual: int = 150,
    numerator: int | None = 3,
    denominator: int | None = 2,
) -> EffortCalibrationFactorDecision:
    """One valid V1.16 decision record (AVAILABLE snapshot by default)."""
    return EffortCalibrationFactorDecision(
        decision_id=decision_id or uuid4(),
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=entity_type,
        sample_count=sample_count,
        minimum_required_sample_count=minimum_required_sample_count,
        total_planned_duration_seconds=planned,
        total_actual_duration_seconds=actual,
        proposal_available=True,
        proposal_reason=EffortCalibrationFactorProposalReason.AVAILABLE,
        factor_numerator=numerator,
        factor_denominator=denominator,
        decision=decision,
        decided_at=decided_at,
    )


def _append(
    db_path: Path,
    records: tuple[EffortCalibrationFactorDecision, ...],
) -> None:
    """Persist exact V1.16 records through the V1.16 append-only boundary."""
    with SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo:
        for record in records:
            decision_repo.add(record)


def _table_snapshot(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            name: conn.execute(
                f"SELECT COUNT(*) FROM {name}"  # noqa: S608 - fixture table names
            ).fetchone()[0]
            for name in tables
        }


# --- Exact resolution after repository recreation ---------------------------


def test_resolution_after_repository_recreation_returns_exact_factors(
    db_path: Path,
) -> None:
    source_accept = _decision(
        decided_at=datetime(2025, 7, 1, 10, 30, tzinfo=timezone(timedelta(hours=2))),
        sample_count=6,
        minimum_required_sample_count=3,
        planned=250,
        actual=200,
        numerator=4,
        denominator=5,
    )
    _append(db_path, (source_accept,))

    with SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo:
        result = resolve_effective_effort_calibration_factors_durably(
            PORTFOLIO_ID, PROJECT_ID, decision_repo
        )

    assert len(result.factors) == 1
    factor = result.factors[0]
    assert factor.entity_type is EntityType.TASK
    assert factor.decision_id == source_accept.decision_id
    assert factor.decided_at == source_accept.decided_at
    assert factor.decided_at.utcoffset() == timedelta(hours=2)
    assert factor.sample_count == 6
    assert factor.minimum_required_sample_count == 3
    assert factor.total_planned_duration_seconds == 250
    assert factor.total_actual_duration_seconds == 200
    assert factor.factor_numerator == 4
    assert factor.factor_denominator == 5
    for value in (
        factor.sample_count,
        factor.factor_numerator,
        factor.factor_denominator,
    ):
        assert type(value) is int

    # A second repository recreation reproduces the identical result.
    with SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo:
        again = resolve_effective_effort_calibration_factors_durably(
            PORTFOLIO_ID, PROJECT_ID, decision_repo
        )
    assert again.model_dump(mode="python") == result.model_dump(mode="python")


def test_empty_history_resolves_to_empty_set(db_path: Path) -> None:
    with SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo:
        result = resolve_effective_effort_calibration_factors_durably(
            PORTFOLIO_ID, PROJECT_ID, decision_repo
        )
    assert result.factors == ()


def test_reject_and_defer_only_history_resolves_to_empty_set(
    db_path: Path,
) -> None:
    _append(
        db_path,
        (
            _decision(
                decision=EffortCalibrationDecision.REJECT,
                decided_at=datetime(2025, 7, 1, 8, 0, tzinfo=UTC),
            ),
            _decision(
                decision=EffortCalibrationDecision.DEFER,
                decided_at=datetime(2025, 7, 2, 8, 0, tzinfo=UTC),
            ),
        ),
    )
    with SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo:
        result = resolve_effective_effort_calibration_factors_durably(
            PORTFOLIO_ID, PROJECT_ID, decision_repo
        )
    assert result.factors == ()


# --- Selection policy over persisted history --------------------------------


def test_multiple_accepts_chronological_instant_then_uuid_tie_break(
    db_path: Path,
) -> None:
    instant = datetime(2025, 7, 1, 12, 30, tzinfo=UTC)
    earliest = _decision(
        decided_at=datetime(2025, 7, 1, 12, 0, tzinfo=UTC),
        planned=200,
        actual=150,
        numerator=3,
        denominator=4,
    )
    tied_low = _decision(
        decided_at=instant,
        planned=100,
        actual=200,
        numerator=2,
        denominator=1,
        decision_id=TIE_UUID_LOW,
    )
    tied_high = _decision(
        # Written with a +02:00 offset; same chronological instant.
        decided_at=datetime(2025, 7, 1, 14, 30, 0, tzinfo=timezone(timedelta(hours=2))),
        planned=100,
        actual=150,
        numerator=3,
        denominator=2,
        decision_id=TIE_UUID_HIGH,
    )
    _append(db_path, (earliest, tied_low, tied_high))

    with SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo:
        result = resolve_effective_effort_calibration_factors_durably(
            PORTFOLIO_ID, PROJECT_ID, decision_repo
        )

    assert len(result.factors) == 1
    factor = result.factors[0]
    assert factor.decision_id == TIE_UUID_HIGH
    assert factor.factor_denominator == 2
    assert factor.factor_numerator == 3
    assert factor.decided_at == datetime(2025, 7, 1, 14, 30, 0, tzinfo=timezone(timedelta(hours=2)))


def test_later_reject_and_defer_do_not_change_history_or_selection(
    db_path: Path,
) -> None:
    accept = _decision(
        decided_at=datetime(2025, 7, 1, 8, 0, tzinfo=UTC),
        planned=200,
        actual=150,
        numerator=3,
        denominator=4,
    )
    later_reject = _decision(
        decision=EffortCalibrationDecision.REJECT,
        decided_at=datetime(2025, 7, 3, 8, 0, tzinfo=UTC),
        planned=100,
        actual=200,
        numerator=2,
        denominator=1,
    )
    later_defer = _decision(
        decision=EffortCalibrationDecision.DEFER,
        decided_at=datetime(2025, 7, 5, 8, 0, tzinfo=UTC),
        planned=100,
        actual=250,
        numerator=5,
        denominator=2,
    )
    _append(db_path, (accept, later_reject, later_defer))

    with (
        SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo,
    ):
        before_history = decision_repo.list_history(PORTFOLIO_ID, PROJECT_ID, EntityType.TASK)
        history_before = [record.model_dump(mode="python") for record in before_history]

        result = resolve_effective_effort_calibration_factors_durably(
            PORTFOLIO_ID, PROJECT_ID, decision_repo
        )

        history_after = [
            record.model_dump(mode="python")
            for record in decision_repo.list_history(PORTFOLIO_ID, PROJECT_ID, EntityType.TASK)
        ]

    assert len(result.factors) == 1
    assert result.factors[0].decision_id == accept.decision_id
    assert result.factors[0].factor_denominator == 4
    assert history_before == history_after
    assert len(history_before) == 3


def test_multiple_entity_types_resolve_independently(db_path: Path) -> None:
    task_accept = _decision(
        entity_type=EntityType.TASK,
        decided_at=datetime(2025, 7, 1, 8, 0, tzinfo=UTC),
        planned=200,
        actual=150,
        numerator=3,
        denominator=4,
    )
    project_accept = _decision(
        entity_type=EntityType.PROJECT,
        decided_at=datetime(2025, 7, 1, 9, 0, tzinfo=UTC),
        planned=100,
        actual=250,
        numerator=5,
        denominator=2,
    )
    _append(db_path, (task_accept, project_accept))

    with SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo:
        result = resolve_effective_effort_calibration_factors_durably(
            PORTFOLIO_ID, PROJECT_ID, decision_repo
        )

    by_type = {factor.entity_type: factor for factor in result.factors}
    assert set(by_type) == {EntityType.TASK, EntityType.PROJECT}
    assert by_type[EntityType.TASK].factor_denominator == 4
    assert by_type[EntityType.PROJECT].factor_denominator == 2


# --- Zero writes, zero new state --------------------------------------------


def test_resolution_never_writes_and_creates_no_new_state(db_path: Path) -> None:
    _append(db_path, (_decision(decided_at=NOW),))

    snapshot_before = _table_snapshot(db_path)

    with (
        SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo,
    ):
        history_before = [
            record.model_dump(mode="python")
            for record in decision_repo.list_history(PORTFOLIO_ID, PROJECT_ID, EntityType.TASK)
        ]
        for _ in range(2):  # repeated reads must not accumulate either
            resolve_effective_effort_calibration_factors_durably(
                PORTFOLIO_ID, PROJECT_ID, decision_repo
            )
        history_after = [
            record.model_dump(mode="python")
            for record in decision_repo.list_history(PORTFOLIO_ID, PROJECT_ID, EntityType.TASK)
        ]

    snapshot_after = _table_snapshot(db_path)

    assert snapshot_after == snapshot_before
    assert history_before == history_after
    assert "execution_effort_calibration_factor_decisions" in snapshot_after
    assert not any("effective" in name for name in snapshot_after)


def test_no_new_persistence_table_or_materialized_state(db_path: Path) -> None:
    _append(db_path, (_decision(decided_at=NOW),))
    with SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo:
        resolve_effective_effort_calibration_factors_durably(
            PORTFOLIO_ID, PROJECT_ID, decision_repo
        )
    with sqlite3.connect(db_path) as conn:
        names = [
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
    assert not any("effective" in name for name in names)
    # Only the known pre-existing tables may exist.
    for name in names:
        if name == "sqlite_sequence":
            continue
        assert name in {
            "portfolios",
            "entities",
            "relations",
            "execution_effort_observations",
            "execution_effort_estimates",
            "execution_effort_calibration_factor_decisions",
        }


# --- No V1.15 re-derivation ---------------------------------------------------


def test_resolution_never_rederives_v115(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("V1.17 must not rederive any V1.15 layer")

    monkeypatch.setattr(
        decisions_app,
        "build_effort_calibration_factor_proposals_durably",
        explode,
    )
    monkeypatch.setattr(
        proposals_app,
        "build_effort_calibration_factor_proposals_durably",
        explode,
    )
    monkeypatch.setattr(measurement_app, "measure_work_breakdown_effort_durably", explode)
    monkeypatch.setattr(planning_app, "plan_work_breakdown_effort_durably", explode)
    monkeypatch.setattr(comparison_app, "compare_work_breakdown_effort_durably", explode)

    _append(db_path, (_decision(decided_at=NOW),))

    with SqliteExecutionEffortCalibrationFactorDecisionRepository(db_path) as decision_repo:
        result = resolve_effective_effort_calibration_factors_durably(
            PORTFOLIO_ID, PROJECT_ID, decision_repo
        )

    assert len(result.factors) == 1
    assert result.factors[0].decision_id is not None


def test_durable_boundary_exposes_only_scope_and_decision_repository() -> None:
    parameters = set(
        inspect.signature(resolve_effective_effort_calibration_factors_durably).parameters
    )
    assert parameters == {"portfolio_id", "project_id", "decision_repository"}
