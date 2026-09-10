"""V1.60 — coordinator integration over the REAL persistent stores.

Proves the V1.50–V1.58 lifecycle chain is wired end-to-end through
genuine SQLite-backed repositories — the coverage scenarios that fake-backed
unit tests cannot prove:

1. successful coordination -> durable admission history (exactly one
   record, exact caller-supplied identity/timestamp, exact embedded V1.51
   admission with original offsets), durable decision history (exactly
   one record, exact V1.50 decision), real durable portfolio mutation
   (task COMPLETED at exactly the caller-supplied changed_at, project
   untouched, membership preserved), and durable application history
   (exactly one record embedding the exact same transition result), all
   mutually consistent across the four independent stores;
2. CURRENT rejection -> ZERO rows in ANY durable store and the portfolio
   unchanged;
3. admission-history persistence failure -> ZERO rows in the decision /
   application stores and NO portfolio mutation;
4. application-history failure AFTER a real application -> the durable
   admission and decision records EXIST, the portfolio IS durably
   COMPLETED on disk, NO application-history row exists, and the explicit
   coordinator error carries the exact transition result and original
   cause;
5. a second identical coordination call is rejected by the CURRENT state
   with ZERO store growth (no duplicate admission record, no duplicate
   decision record, no duplicate application record, no further
   mutation);
6. missing portfolio -> explicit typed error, ZERO history rows.
"""

from __future__ import annotations

import contextlib
import itertools
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from trajectory_os.adapters.persistence import (
    SqlitePortfolioRepository,
    SqliteTaskExecutionLifecycleAdmissionRepository,
    SqliteTaskExecutionLifecycleApplicationRepository,
    SqliteTaskExecutionLifecycleDecisionRepository,
)
from trajectory_os.application import (
    DurableTaskExecutionLifecycleAdmissionError,
    TaskExecutionLifecycleAdmission,
    TaskExecutionLifecycleAdmissionError,
    TaskExecutionLifecycleApplicationHistoryPersistenceError,
    TaskExecutionLifecycleCoordinatorPortfolioNotFoundError,
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDisposition,
    coordinate_task_execution_lifecycle,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

DECIDED_AT = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
EXECUTION_RECORDED_AT = datetime(
    2026, 1, 31, 22, 0, tzinfo=timezone(timedelta(hours=2))
)
ADMISSION_RECORDED_AT = datetime(
    2026, 2, 1, 23, 45, tzinfo=timezone(timedelta(hours=9, minutes=30))
)
DECISION_RECORDED_AT = datetime(
    2026, 2, 2, 10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
)
BASE_TS = datetime(2026, 2, 3, 12, 0, tzinfo=UTC)
CHANGED_AT = BASE_TS + timedelta(days=1)
APPLICATION_RECORDED_AT = datetime(2026, 2, 4, 7, 15, tzinfo=UTC)

_UUID_SEQUENCE = itertools.count(5_000)


def _uuid() -> UUID:
    """Deterministic test identity (test-only helper)."""
    return UUID(int=next(_UUID_SEQUENCE))


def _project_entity() -> TrajectoryEntity:
    return TrajectoryEntity(
        id=_uuid(),
        entity_type=EntityType.PROJECT,
        title="project",
        status=EntityStatus.ACTIVE,
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )


def _task_entity(status: EntityStatus = EntityStatus.ACTIVE) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=_uuid(),
        entity_type=EntityType.TASK,
        title="task",
        status=status,
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )


def _membership(task_id: UUID, project_id: UUID) -> TrajectoryRelation:
    return TrajectoryRelation(
        id=_uuid(),
        source_id=task_id,
        target_id=project_id,
        relation_type=RelationType.BELONGS_TO,
    )


def _complete_task_decision(
    portfolio: Portfolio, project_id: UUID, task_id: UUID
) -> TaskExecutionLifecycleDecision:
    return TaskExecutionLifecycleDecision(
        lifecycle_decision_id=_uuid(),
        decided_at=DECIDED_AT,
        execution_record_id=_uuid(),
        execution_recorded_at=EXECUTION_RECORDED_AT,
        request_id=_uuid(),
        intent_id=_uuid(),
        execution_decision_id=_uuid(),
        portfolio_id=portfolio.id,
        authorized_project_id=project_id,
        authorized_task_id=task_id,
        execution_succeeded=True,
        disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )


def _fresh_stack(
    tmp_path: Path, portfolio: Portfolio
) -> tuple[
    SqlitePortfolioRepository,
    SqliteTaskExecutionLifecycleAdmissionRepository,
    SqliteTaskExecutionLifecycleDecisionRepository,
    SqliteTaskExecutionLifecycleApplicationRepository,
]:
    """Four genuine SQLite repositories sharing ONE database file (and
    therefore one shared ``portfolios`` table), so the admission /
    decision / application history foreign keys resolve against the same
    durable portfolio header. The portfolio snapshot is persisted before
    coordination."""
    db_file = tmp_path / "coordination.sqlite"
    portfolio_repo = SqlitePortfolioRepository(db_file)
    _TRACKED.append(portfolio_repo)
    admission_repo = SqliteTaskExecutionLifecycleAdmissionRepository(db_file)
    _TRACKED.append(admission_repo)
    decision_repo = SqliteTaskExecutionLifecycleDecisionRepository(db_file)
    _TRACKED.append(decision_repo)
    application_repo = SqliteTaskExecutionLifecycleApplicationRepository(db_file)
    _TRACKED.append(application_repo)
    portfolio_repo.save(portfolio)
    return (
        portfolio_repo,
        admission_repo,
        decision_repo,
        application_repo,
    )


def _run(
    decision: TaskExecutionLifecycleDecision,
    portfolio_repo: SqlitePortfolioRepository,
    admission_repo: Any,
    decision_repo: Any,
    application_repo: Any,
    *,
    admission_record_id: UUID | None = None,
    decision_record_id: UUID | None = None,
    application_record_id: UUID | None = None,
) -> Any:
    return coordinate_task_execution_lifecycle(
        admission_record_id or _uuid(),
        ADMISSION_RECORDED_AT,
        decision_record_id or _uuid(),
        DECISION_RECORDED_AT,
        decision,
        CHANGED_AT,
        application_record_id or _uuid(),
        APPLICATION_RECORDED_AT,
        admission_repository=admission_repo,
        decision_repository=decision_repo,
        application_repository=application_repo,
        portfolio_repository=portfolio_repo,
    )


_TRACKED: list[Any] = []


@pytest.fixture(autouse=True)
def _tracked_repositories() -> Any:
    """Close every SQLite repository a test creates (engines hold file
    handles)."""
    _TRACKED.clear()
    yield
    for repo in _TRACKED:
        with contextlib.suppress(Exception):  # best-effort cleanup
            repo.close()


def _scenario() -> tuple[Portfolio, TrajectoryEntity, TrajectoryEntity]:
    project = _project_entity()
    task = _task_entity(EntityStatus.ACTIVE)
    portfolio = Portfolio(
        id=_uuid(),
        name="coordination-integration",
        entities=[project, task],
        relations=[_membership(task.id, project.id)],
    )
    return portfolio, project, task


# ---------------------------------------------------------------------------
# 1 — happy path over the four real stores
# ---------------------------------------------------------------------------


def test_success_persists_all_four_stores_consistently(
    tmp_path: Path,
) -> None:
    portfolio, project, task = _scenario()
    decision = _complete_task_decision(portfolio, project.id, task.id)
    (
        portfolio_repo,
        admission_repo,
        decision_repo,
        application_repo,
    ) = _fresh_stack(tmp_path, portfolio)
    admission_record_id = _uuid()
    decision_record_id = _uuid()
    application_record_id = _uuid()

    outcome = coordinate_task_execution_lifecycle(
        admission_record_id,
        ADMISSION_RECORDED_AT,
        decision_record_id,
        DECISION_RECORDED_AT,
        decision,
        CHANGED_AT,
        application_record_id,
        APPLICATION_RECORDED_AT,
        admission_repository=admission_repo,
        decision_repository=decision_repo,
        application_repository=application_repo,
        portfolio_repository=portfolio_repo,
    )

    # Portfolio store: the task IS durably COMPLETED at exactly the
    # caller-supplied changed_at; the project is untouched; the
    # membership relation survived the full snapshot round-trip.
    on_disk = portfolio_repo.load(portfolio.id)
    assert on_disk is not None
    assert on_disk.get_entity(task.id).status is EntityStatus.COMPLETED  # type: ignore[union-attr]
    assert (
        on_disk.get_entity(task.id).updated_at  # type: ignore[union-attr]
        == CHANGED_AT
    )
    assert on_disk.get_entity(project.id).status is EntityStatus.ACTIVE  # type: ignore[union-attr]
    assert len(on_disk.relations) == 1  # type: ignore[union-attr]
    assert on_disk.relations[0].source_id == task.id  # type: ignore[index]
    assert on_disk.relations[0].target_id == project.id  # type: ignore[index]
    assert on_disk.relations[0].relation_type is RelationType.BELONGS_TO  # type: ignore[index]

    # Admission store: exactly one durable record with the EXACT
    # caller-supplied identity and ORIGINAL offset, and the EXACT embedded
    # V1.51 admission evidence (all thirteen fields, original offsets).
    admission_history = admission_repo.list_history(portfolio.id)
    assert len(admission_history) == 1
    admission_record = admission_history[0]
    assert admission_record.admission_record_id == admission_record_id
    assert admission_record.recorded_at == ADMISSION_RECORDED_AT
    assert admission_record.recorded_at.utcoffset() == ADMISSION_RECORDED_AT.utcoffset()
    expected_admission = TaskExecutionLifecycleAdmission(
        lifecycle_decision_id=decision.lifecycle_decision_id,
        decided_at=DECIDED_AT,
        execution_record_id=decision.execution_record_id,
        execution_recorded_at=EXECUTION_RECORDED_AT,
        request_id=decision.request_id,
        intent_id=decision.intent_id,
        execution_decision_id=decision.execution_decision_id,
        portfolio_id=portfolio.id,
        authorized_project_id=project.id,
        authorized_task_id=task.id,
        execution_succeeded=True,
        disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
        current_task_status=EntityStatus.ACTIVE,
    )
    assert admission_record.admission == expected_admission
    assert admission_record.admission.decided_at.utcoffset() == DECIDED_AT.utcoffset()
    assert (
        admission_record.admission.execution_recorded_at.utcoffset()
        == EXECUTION_RECORDED_AT.utcoffset()
    )

    # Decision store: exactly one durable record with the caller-supplied
    # identity, the original offset, and the EXACT same V1.50 decision.
    decision_history = decision_repo.list_history(portfolio.id)
    assert len(decision_history) == 1
    assert decision_history[0].decision_record_id == decision_record_id
    assert decision_history[0].recorded_at == DECISION_RECORDED_AT
    assert decision_history[0].decision == decision

    # Application store: exactly one durable record embedding the EXACT
    # same transition result — identical to the one in the outcome and to
    # what is durably reflected in the portfolio store.
    application_history = application_repo.list_history(portfolio.id)
    assert len(application_history) == 1
    assert (
        application_history[0].application_record_id == application_record_id
    )
    assert application_history[0].recorded_at == APPLICATION_RECORDED_AT
    transition = application_history[0].result
    assert transition.entity_id == task.id
    assert transition.previous_status is EntityStatus.ACTIVE
    assert transition.new_status is EntityStatus.COMPLETED
    assert transition.changed_at == CHANGED_AT
    assert transition.portfolio == outcome.transition_result.portfolio
    assert transition.portfolio.get_entity(  # type: ignore[union-attr]
        task.id
    ).status is EntityStatus.COMPLETED

    # All four durable stores refer to the SAME portfolio/task/decision
    # chain: the admission evidence, the decision record, and the
    # application result agree on the exact decision identity, project,
    # task, and portfolio.
    assert (
        admission_record.admission.lifecycle_decision_id
        == decision_history[0].decision.lifecycle_decision_id
    )
    assert (
        admission_record.admission.authorized_task_id
        == decision_history[0].decision.authorized_task_id
        == transition.entity_id
        == task.id
    )
    assert (
        admission_record.admission.authorized_project_id
        == decision_history[0].decision.authorized_project_id
        == project.id
    )
    assert (
        admission_record.admission.portfolio_id
        == decision_history[0].decision.portfolio_id
        == transition.portfolio.id
        == portfolio.id
    )

    # The outcome embeds the very same records as the durable stores.
    assert outcome.admission_record.admission_record_id == admission_record_id
    assert outcome.admission_record.recorded_at == ADMISSION_RECORDED_AT
    assert outcome.admission_record.admission == expected_admission
    assert outcome.admission_record == admission_record
    assert outcome.decision_record.decision_record_id == decision_record_id
    assert outcome.decision_record.decision == decision
    assert outcome.decision_record == decision_history[0]
    assert (
        outcome.application_record.application_record_id == application_record_id
    )
    assert outcome.application_record.result == transition
    assert outcome.application_record == application_history[0]


# ---------------------------------------------------------------------------
# 2 — CURRENT rejection -> ZERO rows in any durable store
# ---------------------------------------------------------------------------


def test_current_rejection_writes_nothing_to_any_store(tmp_path: Path) -> None:
    portfolio, project, task = _scenario()
    # The durable CURRENT state does not admit the transition: the task
    # is already COMPLETED.
    rejected = Portfolio(
        id=portfolio.id,
        name=portfolio.name,
        entities=[
            entity.model_copy(update={"status": EntityStatus.COMPLETED})
            if entity.entity_type is EntityType.TASK
            else entity
            for entity in portfolio.entities
        ],
        relations=portfolio.relations,
    )
    decision = _complete_task_decision(rejected, project.id, task.id)
    (
        portfolio_repo,
        admission_repo,
        decision_repo,
        application_repo,
    ) = _fresh_stack(tmp_path, rejected)

    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        _run(decision, portfolio_repo, admission_repo, decision_repo, application_repo)

    assert admission_repo.list_history(rejected.id) == ()
    assert decision_repo.list_history(rejected.id) == ()
    assert application_repo.list_history(rejected.id) == ()
    on_disk = portfolio_repo.load(rejected.id)
    assert on_disk is not None
    assert on_disk.get_entity(task.id).status is EntityStatus.COMPLETED  # type: ignore[union-attr]
    # unchanged: still carrying its original updated_at
    assert on_disk.get_entity(task.id).updated_at == BASE_TS  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 3 — admission-history failure -> ZERO rows in the later stores and NO
#    portfolio mutation
# ---------------------------------------------------------------------------


def test_admission_history_failure_writes_nothing_else(tmp_path: Path) -> None:
    portfolio, project, task = _scenario()
    decision = _complete_task_decision(portfolio, project.id, task.id)
    (
        portfolio_repo,
        admission_repo,
        decision_repo,
        application_repo,
    ) = _fresh_stack(tmp_path, portfolio)
    injected = RuntimeError("admission history store unavailable")

    def failing_add(record: Any) -> None:
        raise injected

    admission_repo.add = failing_add  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="admission history store unavailable"):
        _run(
            decision,
            portfolio_repo,
            admission_repo,
            decision_repo,
            application_repo,
            admission_record_id=_uuid(),
            decision_record_id=_uuid(),
            application_record_id=_uuid(),
        )

    # Stopped exactly at the admission append: zero rows in every other
    # durable store and NO portfolio mutation.
    assert admission_repo.list_history(portfolio.id) == ()  # type: ignore[union-attr]
    assert decision_repo.list_history(portfolio.id) == ()
    assert application_repo.list_history(portfolio.id) == ()
    on_disk = portfolio_repo.load(portfolio.id)
    assert on_disk is not None
    assert on_disk.get_entity(task.id).status is EntityStatus.ACTIVE  # type: ignore[union-attr]
    # unchanged: still carrying its original updated_at
    assert on_disk.get_entity(task.id).updated_at == BASE_TS  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 4 — application-history failure AFTER a real application (all real
#    portfolio + admission + decision stores; only the application store
#    fails)
# ---------------------------------------------------------------------------


def test_application_history_failure_is_explicit_with_real_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    portfolio, project, task = _scenario()
    decision = _complete_task_decision(portfolio, project.id, task.id)
    (
        portfolio_repo,
        admission_repo,
        decision_repo,
        application_repo,
    ) = _fresh_stack(tmp_path, portfolio)
    admission_record_id = _uuid()
    decision_record_id = _uuid()
    application_record_id = _uuid()
    injected = RuntimeError("application history store unavailable")

    def failing_add(record: Any) -> None:
        raise injected

    monkeypatch.setattr(application_repo, "add", failing_add)

    with pytest.raises(
        TaskExecutionLifecycleApplicationHistoryPersistenceError,
        match="ACTUALLY applied and durably saved",
    ) as exc_info:
        coordinate_task_execution_lifecycle(
            admission_record_id,
            ADMISSION_RECORDED_AT,
            decision_record_id,
            DECISION_RECORDED_AT,
            decision,
            CHANGED_AT,
            application_record_id,
            APPLICATION_RECORDED_AT,
            admission_repository=admission_repo,
            decision_repository=decision_repo,
            application_repository=application_repo,
            portfolio_repository=portfolio_repo,
        )

    error = exc_info.value
    assert error.cause is injected
    assert error.transition_result.entity_id == task.id
    assert error.transition_result.new_status is EntityStatus.COMPLETED
    assert error.transition_result.portfolio.id == portfolio.id

    # The real application happened and is durable, the durable admission
    # AND decision records were appended, and NO application-history row
    # exists (no compensation, no removal, no retry).
    on_disk = portfolio_repo.load(portfolio.id)
    assert on_disk is not None
    assert on_disk.get_entity(task.id).status is EntityStatus.COMPLETED  # type: ignore[union-attr]
    admission_history = admission_repo.list_history(portfolio.id)
    assert len(admission_history) == 1
    assert admission_history[0].admission_record_id == admission_record_id
    assert admission_history[0].recorded_at == ADMISSION_RECORDED_AT
    decision_history = decision_repo.list_history(portfolio.id)
    assert len(decision_history) == 1
    assert decision_history[0].decision_record_id == decision_record_id
    assert decision_history[0].decision == decision
    assert application_repo.list_history(portfolio.id) == ()


# ---------------------------------------------------------------------------
# 5 — a second identical call is rejected by the CURRENT state with ZERO
#    store growth
# ---------------------------------------------------------------------------


def test_second_identical_call_is_rejected_with_zero_store_growth(
    tmp_path: Path,
) -> None:
    portfolio, project, task = _scenario()
    decision = _complete_task_decision(portfolio, project.id, task.id)
    (
        portfolio_repo,
        admission_repo,
        decision_repo,
        application_repo,
    ) = _fresh_stack(tmp_path, portfolio)
    admission_record_id = _uuid()
    first_application_record_id = _uuid()

    _run(
        decision,
        portfolio_repo,
        admission_repo,
        decision_repo,
        application_repo,
        admission_record_id=admission_record_id,
        decision_record_id=_uuid(),
        application_record_id=first_application_record_id,
    )

    # The exact same coordination call again: the durable CURRENT state
    # no longer admits the transition, so the coordinator rejects it and
    # NO store grows — not even the admission history.
    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        _run(
            decision,
            portfolio_repo,
            admission_repo,
            decision_repo,
            application_repo,
            admission_record_id=_uuid(),
            decision_record_id=_uuid(),
            application_record_id=_uuid(),
        )

    assert len(admission_repo.list_history(portfolio.id)) == 1
    assert len(decision_repo.list_history(portfolio.id)) == 1
    assert len(application_repo.list_history(portfolio.id)) == 1
    on_disk = portfolio_repo.load(portfolio.id)
    assert on_disk is not None
    assert on_disk.get_entity(task.id).status is EntityStatus.COMPLETED  # type: ignore[union-attr]
    assert on_disk.get_entity(task.id).updated_at == CHANGED_AT  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Missing portfolio -> explicit typed error, zero writes
# ---------------------------------------------------------------------------


def test_missing_portfolio_is_explicit_and_writes_nothing(tmp_path: Path) -> None:
    portfolio, project, task = _scenario()
    decision = _complete_task_decision(portfolio, project.id, task.id)
    # NOTE: the portfolio is NOT persisted to the store; the decision
    # still references its id, so the store cannot resolve it.
    db_file = tmp_path / "coordination.sqlite"
    portfolio_repo = SqlitePortfolioRepository(db_file)
    _TRACKED.append(portfolio_repo)
    admission_repo = SqliteTaskExecutionLifecycleAdmissionRepository(db_file)
    _TRACKED.append(admission_repo)
    decision_repo = SqliteTaskExecutionLifecycleDecisionRepository(db_file)
    _TRACKED.append(decision_repo)
    application_repo = SqliteTaskExecutionLifecycleApplicationRepository(db_file)
    _TRACKED.append(application_repo)

    with pytest.raises(TaskExecutionLifecycleCoordinatorPortfolioNotFoundError):
        _run(decision, portfolio_repo, admission_repo, decision_repo, application_repo)

    assert portfolio_repo.load(portfolio.id) is None
    assert admission_repo.list_history(portfolio.id) == ()
    assert decision_repo.list_history(portfolio.id) == ()
    assert application_repo.list_history(portfolio.id) == ()


# ---------------------------------------------------------------------------
# Hostile admission-history identity/timestamp rejected by the canonical
# V1.58 boundary with ZERO rows
# ---------------------------------------------------------------------------


def test_hostile_admission_record_identity_rejected_by_v158_boundary(
    tmp_path: Path,
) -> None:
    portfolio, project, _task = _scenario()
    decision = _complete_task_decision(portfolio, project.id, _task.id)
    (
        portfolio_repo,
        admission_repo,
        decision_repo,
        application_repo,
    ) = _fresh_stack(tmp_path, portfolio)
    injected = RuntimeError("must not be reached")

    def failing_add(record: Any) -> None:
        raise injected

    admission_repo.add = failing_add  # type: ignore[method-assign]

    with pytest.raises(DurableTaskExecutionLifecycleAdmissionError):
        coordinate_task_execution_lifecycle(
            "not-a-uuid",
            ADMISSION_RECORDED_AT,
            _uuid(),
            DECISION_RECORDED_AT,
            decision,
            CHANGED_AT,
            _uuid(),
            APPLICATION_RECORDED_AT,
            admission_repository=admission_repo,
            decision_repository=decision_repo,
            application_repository=application_repo,
            portfolio_repository=portfolio_repo,
        )

    # Rejected BEFORE any repository add (the failing add never ran), and
    # ZERO rows in any durable store.
    assert admission_repo.list_history(portfolio.id) == ()  # type: ignore[union-attr]
    assert decision_repo.list_history(portfolio.id) == ()
    assert application_repo.list_history(portfolio.id) == ()
    on_disk = portfolio_repo.load(portfolio.id)
    assert on_disk is not None
    assert on_disk.get_entity(_task.id).status is EntityStatus.ACTIVE  # type: ignore[union-attr]
