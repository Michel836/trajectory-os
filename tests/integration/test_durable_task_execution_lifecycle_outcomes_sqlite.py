"""V1.62 — SQLite persistence for V1.61 lifecycle coordination outcome
history (integration).

Proves the durable storage boundary end to end over a real SQLite store:

1. exact add/read round-trip, preserving the exact outer identity, outer
   timestamp (non-UTC offset byte-exact), and the EXACT full V1.60
   outcome — all four components, every nested identity, every nested
   datetime offset, and the embedded Portfolio snapshot inside the
   transition result;
2. value-equivalent records under distinct ``outcome_record_id`` values are
   legal separate rows; the same ``outcome_record_id`` maps to the
   dedicated duplicate error ONLY; any other integrity failure propagates
   unchanged;
3. hostile payloads (non-genuine record, bypassed-validator outer record,
   hostile nested components) are rejected before any row is written;
4. portfolio scope, deterministic ordering (true aware instant then
   ``outcome_record_id.int``), and corruption cross-checks (snapshot and
   every duplicated scalar column) all reject rather than normalize;
5. architecture guards prove the adapter exposes no
   update/delete/upsert/current/latest/effective surface and never
   replays lifecycle coordination, never reads CURRENT state, and adds no
   provider/runtime/shell/clock/UUID-generation surface.

Genuine V1.60/V1.61 values are built through the EXISTING canonical public
boundaries (V1.50 decision -> V1.51 admission -> V1.58/V1.55 records ->
V1.52 transition -> V1.53 record -> V1.60 outcome -> V1.61 record). The
adapter under test never re-creates any of it.
"""

from __future__ import annotations

import ast
import inspect as _inspect
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import trajectory_os.adapters.persistence as persistence_package
import trajectory_os.adapters.persistence.sqlite_task_execution_lifecycle_outcomes as _adapter  # noqa: E501
from trajectory_os.adapters.persistence import (
    DuplicateTaskExecutionLifecycleOutcomeRecordError as _ExportedDuplicateError,
)
from trajectory_os.adapters.persistence import (
    SqliteTaskExecutionLifecycleOutcomeRepository,
    SqliteTaskExecutionLifecycleOutcomeRepository as _ExportedRepository,
)
from trajectory_os.adapters.persistence.models import (
    PortfolioRow,
    TaskExecutionLifecycleOutcomeRecordRow as Row,
)
from trajectory_os.application import (
    TaskExecutionLifecycleAdmission,
    TaskExecutionLifecycleAdmissionRecord,
    TaskExecutionLifecycleApplicationRecord,
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDecisionRecord,
    TaskExecutionLifecycleDisposition,
    TaskExecutionLifecycleOutcome,
    TaskExecutionLifecycleOutcomeRecord,
    TaskExecutionLifecycleOutcomeRepository,
    admit_current_task_execution_lifecycle,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.entity_status_transition import transition_entity_status
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

# --- fixed test identities / timestamps (test-only) ------------------------


class _Scenario:
    """Fixed scenario identities with a base offset."""

    def __init__(self, base: int) -> None:
        self.base = base

    @property
    def portfolio_id(self) -> UUID:
        return UUID(int=self.base)

    @property
    def project_id(self) -> UUID:
        return UUID(int=self.base + 1)

    @property
    def task_id(self) -> UUID:
        return UUID(int=self.base + 2)

    @property
    def lifecycle_decision_id(self) -> UUID:
        return UUID(int=self.base + 3)

    @property
    def execution_record_id(self) -> UUID:
        return UUID(int=self.base + 4)

    @property
    def request_id(self) -> UUID:
        return UUID(int=self.base + 5)

    @property
    def intent_id(self) -> UUID:
        return UUID(int=self.base + 6)

    @property
    def execution_decision_id(self) -> UUID:
        return UUID(int=self.base + 7)

    @property
    def admission_record_id(self) -> UUID:
        return UUID(int=self.base + 10)

    @property
    def decision_record_id(self) -> UUID:
        return UUID(int=self.base + 11)

    @property
    def application_record_id(self) -> UUID:
        return UUID(int=self.base + 12)


_ids = _Scenario(100)

# Distinct original UTC offsets across the whole chain — every one of them
# must survive the round trip byte-exact.
DECIDED_AT = datetime(2026, 9, 5, 12, 30, tzinfo=UTC)
EXECUTION_RECORDED_AT = datetime(  # +05:30
    2026, 9, 4, 16, 45, tzinfo=timezone(timedelta(hours=5, minutes=30))
)
ADMISSION_RECORDED_AT = datetime(  # +07:00
    2026, 9, 6, 3, 0, tzinfo=timezone(timedelta(hours=7))
)
DECISION_RECORDED_AT = datetime(  # -05:00
    2026, 9, 6, 5, 30, tzinfo=timezone(timedelta(hours=-5))
)
APPLICATION_RECORDED_AT = datetime(  # -03:30
    2026, 9, 6, 7, 0, tzinfo=timezone(timedelta(hours=-3, minutes=30))
)
BASE_TS = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
CHANGED_AT = datetime(  # +02:00
    2026, 9, 6, 22, 15, tzinfo=timezone(timedelta(hours=2))
)
RECORDED_AT = datetime(  # outer, +05:30
    2026, 9, 8, 23, 45, 12, tzinfo=timezone(timedelta(hours=5, minutes=30))
)


def _scenario_ids(base: int) -> _Scenario:
    return _Scenario(base)


def _decision(ids: _Scenario = _ids) -> TaskExecutionLifecycleDecision:
    return TaskExecutionLifecycleDecision(
        lifecycle_decision_id=ids.lifecycle_decision_id,
        decided_at=DECIDED_AT,
        execution_record_id=ids.execution_record_id,
        execution_recorded_at=EXECUTION_RECORDED_AT,
        request_id=ids.request_id,
        intent_id=ids.intent_id,
        execution_decision_id=ids.execution_decision_id,
        portfolio_id=ids.portfolio_id,
        authorized_project_id=ids.project_id,
        authorized_task_id=ids.task_id,
        execution_succeeded=True,
        disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )


def _outcome(ids: _Scenario = _ids) -> TaskExecutionLifecycleOutcome:
    """One genuine, complete V1.60 outcome built through the EXISTING
    canonical boundaries (V1.50 -> V1.51 -> V1.58/V1.55 -> V1.52 -> V1.53
    -> V1.60). The V1.62 adapter under test never re-creates any of it."""

    project = TrajectoryEntity(
        id=ids.project_id,
        entity_type=EntityType.PROJECT,
        title="project",
        status=EntityStatus.ACTIVE,
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )
    task = TrajectoryEntity(
        id=ids.task_id,
        entity_type=EntityType.TASK,
        title="task",
        status=EntityStatus.SOMEDAY,
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )
    portfolio = Portfolio(
        id=ids.portfolio_id,
        name=f"portfolio-{ids.base}",
        entities=[project, task],
        relations=[
            TrajectoryRelation(
                id=UUID(int=ids.base + 20),
                source_id=ids.task_id,
                target_id=ids.project_id,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )

    decision = _decision(ids)
    admission = admit_current_task_execution_lifecycle(decision, portfolio)
    admission_record = TaskExecutionLifecycleAdmissionRecord(
        admission_record_id=ids.admission_record_id,
        recorded_at=ADMISSION_RECORDED_AT,
        admission=admission,
    )
    decision_record = TaskExecutionLifecycleDecisionRecord(
        decision_record_id=ids.decision_record_id,
        recorded_at=DECISION_RECORDED_AT,
        decision=decision,
    )
    result = transition_entity_status(
        portfolio, ids.task_id, EntityStatus.COMPLETED, CHANGED_AT
    )
    application_record = TaskExecutionLifecycleApplicationRecord(
        application_record_id=ids.application_record_id,
        recorded_at=APPLICATION_RECORDED_AT,
        result=result,
    )
    return TaskExecutionLifecycleOutcome(
        admission_record=admission_record,
        decision_record=decision_record,
        transition_result=result,
        application_record=application_record,
    )


def _record(
    outcome_record_id: UUID,
    recorded_at: datetime = RECORDED_AT,
    ids: _Scenario = _ids,
    outcome: TaskExecutionLifecycleOutcome | None = None,
) -> TaskExecutionLifecycleOutcomeRecord:
    return TaskExecutionLifecycleOutcomeRecord(
        outcome_record_id=outcome_record_id,
        recorded_at=recorded_at,
        outcome=outcome if outcome is not None else _outcome(ids),
    )


def _seed_portfolio(
    repository: SqliteTaskExecutionLifecycleOutcomeRepository,
    portfolio_id: UUID,
) -> None:
    with Session(repository.engine) as session:
        session.execute(
            insert(PortfolioRow).values(
                id=str(portfolio_id),
                name=f"portfolio-{portfolio_id}",
            )
        )
        session.commit()


def _update_row(
    repository: SqliteTaskExecutionLifecycleOutcomeRepository,
    outcome_record_id: UUID,
    **columns: str,
) -> None:
    with Session(repository.engine) as session:
        session.execute(
            update(Row)
            .where(Row.outcome_record_id == str(outcome_record_id))
            .values(**columns)
        )
        session.commit()


def _stored_rows(
    repository: SqliteTaskExecutionLifecycleOutcomeRepository,
) -> list[Any]:
    with Session(repository.engine) as session:
        return list(session.scalars(select(Row)).all())


# ---------------------------------------------------------------------------
# ROUND TRIP
# ---------------------------------------------------------------------------


def test_round_trip_preserves_exact_record_and_original_offsets(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "outcomes.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)
    record = _record(UUID(int=500))
    repository.add(record)

    history = repository.list_history(_ids.portfolio_id)

    assert history == (record,)
    stored = history[0]
    original = record

    # Exact outer identity and outer timestamp, non-UTC offset exact.
    assert stored.outcome_record_id == original.outcome_record_id
    assert stored.recorded_at == original.recorded_at
    assert stored.recorded_at.isoformat() == original.recorded_at.isoformat()
    assert stored.recorded_at.utcoffset() == timedelta(hours=5, minutes=30)

    # EXACT full V1.60 outcome round-trips.
    assert stored.outcome == original.outcome
    assert stored.outcome.model_dump(mode="python") == original.outcome.model_dump(  # type: ignore[union-attr]
        mode="python"
    )

    # Admission record identity + nested timestamp offsets.
    assert stored.outcome.admission_record == original.outcome.admission_record
    assert stored.outcome.admission_record.admission_record_id == (  # type: ignore[union-attr]
        original.outcome.admission_record.admission_record_id
    )
    assert stored.outcome.admission_record.recorded_at.utcoffset() == (  # type: ignore[union-attr]
        timedelta(hours=7)
    )
    admission = stored.outcome.admission_record.admission
    original_admission = original.outcome.admission_record.admission
    assert admission == original_admission
    assert admission.lifecycle_decision_id == original_admission.lifecycle_decision_id
    assert admission.decided_at.isoformat() == original_admission.decided_at.isoformat()
    assert admission.execution_record_id == original_admission.execution_record_id
    assert admission.execution_recorded_at.utcoffset() == timedelta(
        hours=5, minutes=30
    )
    assert admission.portfolio_id == original_admission.portfolio_id
    assert admission.authorized_task_id == original_admission.authorized_task_id
    assert admission.current_task_status is EntityStatus.SOMEDAY

    # Decision record identity + offset.
    assert (
        stored.outcome.decision_record.decision_record_id
        == original.outcome.decision_record.decision_record_id
    )
    assert stored.outcome.decision_record.recorded_at.utcoffset() == timedelta(
        hours=-5
    )
    assert (
        stored.outcome.decision_record.decision.decided_at.isoformat()
        == original.outcome.decision_record.decision.decided_at.isoformat()
    )

    # Application record identity + offset.
    assert (
        stored.outcome.application_record.application_record_id
        == original.outcome.application_record.application_record_id
    )
    assert (
        stored.outcome.application_record.recorded_at.utcoffset()
        == timedelta(hours=-3, minutes=30)
    )

    # Transition result: entity id and changed_at with exact offset.
    assert stored.outcome.transition_result.entity_id == original.outcome.transition_result.entity_id
    assert (
        stored.outcome.transition_result.changed_at.isoformat()
        == original.outcome.transition_result.changed_at.isoformat()
    )
    assert stored.outcome.transition_result.changed_at.utcoffset() == timedelta(
        hours=2
    )

    repository.close()


def test_transition_result_portfolio_snapshot_preserved_exactly(
    tmp_path: Path,
) -> None:
    """The resulting Portfolio snapshot embedded in the transition result
    round-trips exactly — and remains HISTORICAL evidence only (the task
    status there is the historical COMPLETED state, never current state)."""

    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "snapshot.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)
    record = _record(UUID(int=501))
    repository.add(record)

    stored = repository.list_history(_ids.portfolio_id)[0].outcome.transition_result
    original = record.outcome.transition_result

    assert stored.portfolio == original.portfolio
    assert stored.portfolio.id == original.portfolio.id
    assert [entity for entity in stored.portfolio.entities if entity.id == _ids.task_id][0].status is EntityStatus.COMPLETED
    assert stored.portfolio.entities == original.portfolio.entities
    assert stored.portfolio.relations == original.portfolio.relations

    repository.close()


def test_duplicated_scalar_columns_persisted_exactly(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "columns.sqlite"
    )

    record_id = UUID(int=502)
    _seed_portfolio(repository, _ids.portfolio_id)
    record = _record(record_id)
    repository.add(record)

    row = _stored_rows(repository)[0]
    original = record.outcome

    assert row.outcome_record_id == str(record_id)
    assert row.portfolio_id == str(original.admission_record.admission.portfolio_id)
    assert row.admission_record_id == str(original.admission_record.admission_record_id)
    assert row.decision_record_id == str(original.decision_record.decision_record_id)
    assert row.application_record_id == str(original.application_record.application_record_id)
    assert row.lifecycle_decision_id == str(original.admission_record.admission.lifecycle_decision_id)
    assert row.execution_record_id == str(original.admission_record.admission.execution_record_id)
    assert row.authorized_task_id == str(original.admission_record.admission.authorized_task_id)
    assert row.transition_entity_id == str(original.transition_result.entity_id)
    assert row.transition_changed_at == original.transition_result.changed_at.isoformat()
    assert row.recorded_at == record.recorded_at.isoformat()
    # Deterministic explicit JSON — never a pickle / opaque binary.
    assert row.outcome_snapshot == original.model_dump_json()

    repository.close()


# ---------------------------------------------------------------------------
# IDENTITY / APPEND SEMANTICS
# ---------------------------------------------------------------------------


def test_value_equivalent_records_under_distinct_outcome_record_ids(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "identities.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)
    shared_outcome = _outcome()

    first = _record(UUID(int=600), outcome=shared_outcome)
    second = _record(UUID(int=601), outcome=shared_outcome)

    repository.add(first)
    repository.add(second)

    assert repository.list_history(_ids.portfolio_id) == (first, second)

    repository.close()


def test_duplicate_outcome_record_id_maps_to_specific_error(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "duplicates.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)
    original = _record(UUID(int=700))
    conflicting = _record(
        UUID(int=700),
        recorded_at=datetime(2026, 9, 9, 0, 0, tzinfo=UTC),
        outcome=_outcome(),
    )

    repository.add(original)

    with pytest.raises(
        _adapter.DuplicateTaskExecutionLifecycleOutcomeRecordError,
        match="already exists",
    ):
        repository.add(conflicting)

    # Duplicate failure preserves the original row unchanged.
    assert repository.list_history(_ids.portfolio_id) == (original,)

    repository.close()


def test_foreign_key_integrity_error_is_not_misclassified_as_duplicate(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "foreign-key.sqlite"
    )

    # Deliberately do NOT seed the referenced portfolio.
    record = _record(UUID(int=701))

    with pytest.raises(IntegrityError):
        try:
            repository.add(record)
        except _adapter.DuplicateTaskExecutionLifecycleOutcomeRecordError:
            pytest.fail(
                "an unrelated FK integrity violation must NOT be "
                "translated into the duplicate primary-key error"
            )

    assert _stored_rows(repository) == []

    repository.close()


# ---------------------------------------------------------------------------
# HOSTILE INPUTS (rejected BEFORE any database interaction)
# ---------------------------------------------------------------------------


def test_add_requires_a_genuine_v161_record(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "genuine.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)

    with pytest.raises(TypeError):
        repository.add(_outcome())  # the embedded V1.60 value, not a record
    with pytest.raises(TypeError):
        repository.add(None)
    with pytest.raises(TypeError):
        repository.add({"outcome_record_id": "5" * 32})

    assert _stored_rows(repository) == []

    repository.close()


def test_hostile_model_construct_outer_record_rejected_before_write(
    tmp_path: Path,
) -> None:
    """A bypassed-validator outer record (naive recorded_at) must be
    rejected by ADD's fresh strict revalidation before any row is
    written."""

    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "hostile-outer.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)

    hostile = TaskExecutionLifecycleOutcomeRecord.model_construct(
        outcome_record_id=UUID(int=800),
        recorded_at=RECORDED_AT.replace(tzinfo=None),  # naive: bypasses validator
        outcome=_outcome(),
    )

    with pytest.raises(ValueError):
        repository.add(hostile)

    assert _stored_rows(repository) == []

    repository.close()


def test_hostile_nested_admission_state_rejected_before_write(
    tmp_path: Path,
) -> None:
    """A V1.51 admission claiming COMPLETE_TASK over a failed execution
    is semantically invalid: it must be rejected before any row is
    written, even though it is embedded inside a bypassed V1.60 outcome."""

    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "hostile-admission.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)

    genuine = _outcome()
    hostile_admission = TaskExecutionLifecycleAdmission.model_construct(
        lifecycle_decision_id=_ids.lifecycle_decision_id,
        decided_at=DECIDED_AT,
        execution_record_id=_ids.execution_record_id,
        execution_recorded_at=EXECUTION_RECORDED_AT,
        request_id=_ids.request_id,
        intent_id=_ids.intent_id,
        execution_decision_id=_ids.execution_decision_id,
        portfolio_id=_ids.portfolio_id,
        authorized_project_id=_ids.project_id,
        authorized_task_id=_ids.task_id,
        execution_succeeded=False,
        disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
        current_task_status=EntityStatus.SOMEDAY,
    )
    hostile_admission_record = TaskExecutionLifecycleAdmissionRecord.model_construct(
        admission_record_id=_ids.admission_record_id,
        recorded_at=ADMISSION_RECORDED_AT,
        admission=hostile_admission,
    )
    hostile_outcome = TaskExecutionLifecycleOutcome.model_construct(
        admission_record=hostile_admission_record,
        decision_record=genuine.decision_record,
        transition_result=genuine.transition_result,
        application_record=genuine.application_record,
    )
    hostile_record = TaskExecutionLifecycleOutcomeRecord.model_construct(
        outcome_record_id=UUID(int=801),
        recorded_at=RECORDED_AT,
        outcome=hostile_outcome,
    )

    with pytest.raises(ValueError):
        repository.add(hostile_record)

    assert _stored_rows(repository) == []

    repository.close()


def test_hostile_nested_transition_and_application_state_rejected_before_write(  # noqa: E501
    tmp_path: Path,
) -> None:
    """Hostile nested values that break V1.52/V1.53/V1.60 invariants (a
    CANCELLED transition, a naive recorded_at inside the application
    record) must be rejected before any row is written."""

    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "hostile-application.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)

    genuine = _outcome()

    # A non-COMPLETED transition result is a genuine V1.52 value yet
    # invalid inside a V1.60 coordination outcome.
    project = TrajectoryEntity(
        id=_ids.project_id,
        entity_type=EntityType.PROJECT,
        title="project",
        status=EntityStatus.ACTIVE,
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )
    task = TrajectoryEntity(
        id=_ids.task_id,
        entity_type=EntityType.TASK,
        title="task",
        status=EntityStatus.SOMEDAY,
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )
    portfolio = Portfolio(
        id=_ids.portfolio_id,
        name="hostile",
        entities=[project, task],
        relations=[
            TrajectoryRelation(
                id=UUID(int=_ids.base + 20),
                source_id=_ids.task_id,
                target_id=_ids.project_id,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )
    cancelled_result = transition_entity_status(
        portfolio, _ids.task_id, EntityStatus.CANCELLED, CHANGED_AT
    )
    hostile_application_record = (
        TaskExecutionLifecycleApplicationRecord.model_construct(
            application_record_id=_ids.application_record_id,
            recorded_at=APPLICATION_RECORDED_AT.replace(tzinfo=None),
            result=cancelled_result,
        )
    )
    hostile_outcome = TaskExecutionLifecycleOutcome.model_construct(
        admission_record=genuine.admission_record,
        decision_record=genuine.decision_record,
        transition_result=cancelled_result,
        application_record=hostile_application_record,
    )
    hostile_record = TaskExecutionLifecycleOutcomeRecord.model_construct(
        outcome_record_id=UUID(int=802),
        recorded_at=RECORDED_AT,
        outcome=hostile_outcome,
    )

    with pytest.raises(ValueError):
        repository.add(hostile_record)

    assert _stored_rows(repository) == []

    repository.close()


# ---------------------------------------------------------------------------
# PORTFOLIO SCOPE
# ---------------------------------------------------------------------------


def test_list_history_rejects_non_uuid_scope_before_sql(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "scope.sqlite"
    )

    for bad_scope in (
        str(_ids.portfolio_id),
        _ids.portfolio_id.int,
        b"portfolio",
        None,
    ):
        with pytest.raises(TypeError):
            repository.list_history(bad_scope)  # type: ignore[arg-type]

    repository.close()


def test_list_history_scopes_exactly_by_portfolio(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "scoping.sqlite"
    )

    a = _scenario_ids(100)
    b = _scenario_ids(1000)

    _seed_portfolio(repository, a.portfolio_id)
    _seed_portfolio(repository, b.portfolio_id)

    record_a = _record(UUID(int=1100), ids=a)
    record_b = _record(UUID(int=1101), ids=b)

    repository.add(record_a)
    repository.add(record_b)

    assert repository.list_history(a.portfolio_id) == (record_a,)
    assert repository.list_history(b.portfolio_id) == (record_b,)

    repository.close()


# ---------------------------------------------------------------------------
# ORDERING
# ---------------------------------------------------------------------------


def test_ordering_uses_true_aware_instant_not_lexicographic_iso(
    tmp_path: Path,
) -> None:
    """+05:30 09:00 is INSTANT-before UTC 04:00, although its lexicographic
    ISO text sorts after it. Ordering must follow the true instant."""

    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "ordering.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)
    shared_outcome = _outcome()

    earlier_instant = _record(
        UUID(int=900),
        recorded_at=datetime(2026, 9, 8, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),  # noqa: E501
        outcome=shared_outcome,
    )
    later_instant = _record(
        UUID(int=901),
        recorded_at=datetime(2026, 9, 8, 4, 0, tzinfo=UTC),
        outcome=shared_outcome,
    )

    assert earlier_instant.recorded_at.isoformat() > later_instant.recorded_at.isoformat()  # noqa: E501
    assert earlier_instant.recorded_at < later_instant.recorded_at

    repository.add(earlier_instant)
    repository.add(later_instant)

    assert repository.list_history(_ids.portfolio_id) == (
        earlier_instant,
        later_instant,
    )

    repository.close()


def test_equal_instants_order_by_outcome_record_id_int(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "tiebreak.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)
    shared_outcome = _outcome()

    big_id = _record(
        UUID(int=902),
        recorded_at=datetime(
            2026, 9, 8, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
        ),
        outcome=shared_outcome,
    )
    small_id = _record(
        UUID(int=901),
        recorded_at=datetime(2026, 9, 8, 3, 30, tzinfo=UTC),
        outcome=shared_outcome,
    )

    assert big_id.recorded_at == small_id.recorded_at
    assert small_id.outcome_record_id.int < big_id.outcome_record_id.int

    repository.add(big_id)
    repository.add(small_id)

    assert repository.list_history(_ids.portfolio_id) == (small_id, big_id)

    repository.close()


# ---------------------------------------------------------------------------
# CORRUPTION CROSS-CHECKS (reject, never repair)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column_name", "corrupt_value"),
    [
        ("admission_record_id", str(UUID(int=42_002))),
        ("decision_record_id", str(UUID(int=42_003))),
        ("application_record_id", str(UUID(int=42_004))),
        ("lifecycle_decision_id", str(UUID(int=42_005))),
        ("execution_record_id", str(UUID(int=42_006))),
        ("authorized_task_id", str(UUID(int=42_007))),
        ("transition_entity_id", str(UUID(int=42_008))),
        (
            "transition_changed_at",
            datetime(2026, 9, 7, 0, 0, tzinfo=UTC).isoformat(),
        ),
    ],
    ids=[
        "admission-record-id",
        "decision-record-id",
        "application-record-id",
        "lifecycle-decision-id",
        "execution-record-id",
        "authorized-task-id",
        "transition-entity-id",
        "transition-changed-at",
    ],
)
def test_corrupt_duplicated_scalar_column_is_rejected(
    tmp_path: Path,
    column_name: str,
    corrupt_value: str,
) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / f"corrupt-{column_name}.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)
    record = _record(UUID(int=1200))
    repository.add(record)

    _update_row(repository, record.outcome_record_id, **{column_name: corrupt_value})

    with pytest.raises(ValueError):
        repository.list_history(_ids.portfolio_id)

    repository.close()


def test_corrupt_duplicated_portfolio_id_is_rejected(tmp_path: Path) -> None:
    """The row's portfolio column is crossed to a DIFFERENT (seeded) portfolio
    while the canonical snapshot still names the original one: the
    duplicated-scalar cross-check must reject, never repair."""

    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "corrupt-portfolio.sqlite"
    )

    other_portfolio = UUID(int=42_099)
    _seed_portfolio(repository, _ids.portfolio_id)
    _seed_portfolio(repository, other_portfolio)

    record = _record(UUID(int=1203))
    repository.add(record)

    _update_row(
        repository, record.outcome_record_id, portfolio_id=str(other_portfolio)
    )

    # The row now matches the other portfolio's scope, but the stored
    # portfolio column disagrees with the snapshot's canonical
    # admission.portfolio_id — rejected, never normalized.
    with pytest.raises(ValueError):
        repository.list_history(other_portfolio)

    # And the original portfolio's history no longer returns it silently.
    assert repository.list_history(_ids.portfolio_id) == ()

    repository.close()


def test_corrupt_outcome_json_snapshot_is_rejected(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "corrupt-snapshot.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)
    record = _record(UUID(int=1201))
    repository.add(record)

    _update_row(
        repository, record.outcome_record_id, outcome_snapshot="{not-json"
    )

    with pytest.raises(ValueError):
        repository.list_history(_ids.portfolio_id)

    repository.close()


def test_naive_stored_outer_recorded_at_is_rejected(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionLifecycleOutcomeRepository(
        tmp_path / "corrupt-recorded-at.sqlite"
    )

    _seed_portfolio(repository, _ids.portfolio_id)
    record = _record(UUID(int=1202))
    repository.add(record)

    _update_row(
        repository,
        record.outcome_record_id,
        recorded_at=datetime(2026, 9, 8, 8, 0).isoformat(),
    )

    # A naive stored timestamp is corruption: rejected, never silently
    # normalized to an aware value.
    with pytest.raises(ValueError):
        repository.list_history(_ids.portfolio_id)

    repository.close()


# ---------------------------------------------------------------------------
# PERSISTENCE EXPORTS / PROTOCOL SURFACE / ARCHITECTURE
# ---------------------------------------------------------------------------


def test_persistence_package_exports_the_public_symbols() -> None:
    assert (
        persistence_package.DuplicateTaskExecutionLifecycleOutcomeRecordError
        is _ExportedDuplicateError
    )
    assert (
        persistence_package.DuplicateTaskExecutionLifecycleOutcomeRecordError
        is _adapter.DuplicateTaskExecutionLifecycleOutcomeRecordError
    )
    assert (
        persistence_package.SqliteTaskExecutionLifecycleOutcomeRepository
        is _ExportedRepository
    )
    assert (
        persistence_package.SqliteTaskExecutionLifecycleOutcomeRepository
        is _adapter.SqliteTaskExecutionLifecycleOutcomeRepository
    )


def test_repository_exposes_only_append_and_read_api() -> None:
    public = [
        name
        for name, member in vars(
            SqliteTaskExecutionLifecycleOutcomeRepository
        ).items()
        if not name.startswith("__") and callable(member)
    ]

    forbidden = {
        "update",
        "delete",
        "replace",
        "upsert",
        "save",
        "patch",
        "latest",
        "current",
        "effective",
        "supersede",
        "revoke",
    }

    assert not (forbidden & set(public)), (
        "repository must not expose mutable / current / effective APIs: "
        f"{sorted(forbidden & set(public))}"
    )
    assert "add" in public
    assert "list_history" in public


def test_adapter_satisfies_the_v161_repository_protocol_structurally() -> None:
    """The adapter implements the V1.61 structural protocol
    (``add`` + ``list_history`` over
    ``TaskExecutionLifecycleOutcomeRecord``) with matching parameter
    names."""

    for method_name in ("add", "list_history"):
        assert method_name in vars(TaskExecutionLifecycleOutcomeRepository)
        assert callable(
            getattr(TaskExecutionLifecycleOutcomeRepository, method_name)
        )
        assert method_name in vars(
            SqliteTaskExecutionLifecycleOutcomeRepository
        )
        assert callable(
            getattr(SqliteTaskExecutionLifecycleOutcomeRepository, method_name)
        )

    add_signature = _inspect.signature(
        SqliteTaskExecutionLifecycleOutcomeRepository.add
    )
    assert list(add_signature.parameters) == ["self", "record"]

    list_signature = _inspect.signature(
        SqliteTaskExecutionLifecycleOutcomeRepository.list_history
    )
    assert list(list_signature.parameters) == ["self", "portfolio_id"]


def test_module_never_invokes_lifecycle_replay_portfolio_or_io() -> None:
    source = _inspect.getsource(_adapter)
    tree = ast.parse(source)

    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called_names.add(_callee_name(node.func))

    forbidden = {
        "admit_current_task_execution_lifecycle",
        "apply_admitted_task_execution_lifecycle_durably",
        "coordinate_task_execution_lifecycle",
        "record_task_execution_lifecycle_admission_durably",
        "record_task_execution_lifecycle_application_durably",
        "record_task_execution_lifecycle_decision_durably",
        "record_task_execution_lifecycle_outcome",
        "record_task_execution_result_durably",
        "transition_entity_status",
        "transition_entity_status_durably",
        "load",
        "save",
        "uuid4",
        "now",
        "utcnow",
        "subprocess",
        "open",
        "Popen",
        "run",
        "shell",
    }

    assert not (forbidden & called_names), (
        "adapter must not invoke lifecycle replay/coordination, Portfolio "
        f"load/save, clock/UUID generation, or IO: "
        f"{sorted(forbidden & called_names)}"
    )


def test_module_does_not_reference_forbidden_symbols() -> None:
    source = _inspect.getsource(_adapter)

    forbidden = {
        "model_construct",
        "uuid4",
        "utcnow",
        "datetime.now",
        "subprocess",
        "pickle",
        "admit_current_task_execution_lifecycle",
        "apply_admitted_task_execution_lifecycle_durably",
        "coordinate_task_execution_lifecycle",
        "transition_entity_status",
        "record_task_execution_lifecycle_admission_durably",
        "record_task_execution_lifecycle_application_durably",
    }

    hits = sorted(symbol for symbol in forbidden if symbol in source)
    assert hits == [], (
        "adapter must not reference lifecycle replay/coordination, "
        f"clock/UUID generation, subprocess, or pickle: {hits}"
    )


def test_module_imports_no_provider_runtime_or_shell_surface() -> None:
    tree = ast.parse(_inspect.getsource(_adapter))
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_roots = {
        "subprocess",
        "shutil",
        "os",
        "sys",
        "time",
        "ollama",
        "requests",
        "httpx",
        "socket",
    }
    imported_roots = {module.split(".")[0] for module in imported}

    assert forbidden_roots.isdisjoint(imported_roots)


def _callee_name(func: Any) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""
