"""V1.57 — coordinator integration over the REAL persistent stores.

Proves the V1.50–V1.56 chain is wired end-to-end through genuine
SQLite-backed repositories — the coverage scenarios that fake-backed unit
tests cannot prove:

1. successful coordination -> durable decision history (exactly one
   record, exact V1.50 decision), real durable portfolio mutation (task
   COMPLETED at exactly the caller-supplied changed_at, project
   untouched, membership preserved), and durable application history
   (exactly one record embedding the exact same transition result),
   all mutually consistent across the three independent stores;
2. CURRENT rejection -> ZERO rows in ANY durable store and the portfolio
   unchanged;
3. application-history failure AFTER a real application -> the durable
   decision record EXISTS, the portfolio IS durably COMPLETED on disk,
   NO application-history row exists, and the explicit coordinator error
   carries the exact transition result and original cause;
4. a second identical coordination call is rejected by the CURRENT
   state with ZERO store growth (no duplicate decision record, no
   duplicate application record, no further mutation).
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
    SqliteTaskExecutionLifecycleApplicationRepository,
    SqliteTaskExecutionLifecycleDecisionRepository,
)
from trajectory_os.application import (
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
        execution_recorded_at=DECIDED_AT - timedelta(days=1),
        request_id=_uuid(),
        intent_id=_uuid(),
        execution_decision_id=_uuid(),
        portfolio_id=portfolio.id,
        authorized_project_id=project_id,
        authorized_task_id=task_id,
        execution_succeeded=True,
        disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )


def _fresh_stack(tmp_path: Path, portfolio: Portfolio) -> tuple[
    SqlitePortfolioRepository,
    SqliteTaskExecutionLifecycleDecisionRepository,
    SqliteTaskExecutionLifecycleApplicationRepository,
]:
    """Three genuine SQLite repositories sharing ONE database file (and
    therefore one shared ``portfolios`` table), so the decision and
    application history foreign keys resolve against the same durable
    portfolio header. The portfolio snapshot is persisted before
    coordination."""
    db_file = tmp_path / "coordination.sqlite"
    portfolio_repo = SqlitePortfolioRepository(db_file)
    _TRACKED.append(portfolio_repo)
    decision_repo = SqliteTaskExecutionLifecycleDecisionRepository(db_file)
    _TRACKED.append(decision_repo)
    application_repo = SqliteTaskExecutionLifecycleApplicationRepository(db_file)
    _TRACKED.append(application_repo)
    portfolio_repo.save(portfolio)
    return portfolio_repo, decision_repo, application_repo


def _run(
    decision: TaskExecutionLifecycleDecision,
    portfolio_repo: SqlitePortfolioRepository,
    decision_repo: Any,
    application_repo: Any,
) -> Any:
    return coordinate_task_execution_lifecycle(
        _uuid(),
        DECISION_RECORDED_AT,
        decision,
        CHANGED_AT,
        _uuid(),
        APPLICATION_RECORDED_AT,
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
# 1 — happy path over the three real stores
# ---------------------------------------------------------------------------


def test_success_persists_all_three_stores_consistently(
    tmp_path: Path,
) -> None:
    portfolio, project, task = _scenario()
    decision = _complete_task_decision(portfolio, project.id, task.id)
    portfolio_repo, decision_repo, application_repo = _fresh_stack(
        tmp_path, portfolio
    )
    decision_record_id = _uuid()
    application_record_id = _uuid()

    outcome = coordinate_task_execution_lifecycle(
        decision_record_id,
        DECISION_RECORDED_AT,
        decision,
        CHANGED_AT,
        application_record_id,
        APPLICATION_RECORDED_AT,
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
    assert transition.portfolio == (
        outcome.transition_result.portfolio
    )
    assert transition.portfolio.get_entity(  # type: ignore[union-attr]
        task.id
    ).status is EntityStatus.COMPLETED

    # The outcome embeds the very same records.
    assert outcome.decision_record.decision_record_id == decision_record_id
    assert outcome.decision_record.decision == decision
    assert (
        outcome.application_record.application_record_id == application_record_id
    )
    assert outcome.application_record.result == transition


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
    portfolio_repo, decision_repo, application_repo = _fresh_stack(
        tmp_path, rejected
    )

    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        _run(decision, portfolio_repo, decision_repo, application_repo)

    assert decision_repo.list_history(rejected.id) == ()
    assert application_repo.list_history(rejected.id) == ()
    on_disk = portfolio_repo.load(rejected.id)
    assert on_disk is not None
    assert on_disk.get_entity(task.id).status is EntityStatus.COMPLETED  # type: ignore[union-attr]
    # unchanged: still carrying its original updated_at
    assert on_disk.get_entity(task.id).updated_at == BASE_TS  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 3 — application-history failure AFTER a real application (all real
#    portfolio + decision stores; only the application store fails)
# ---------------------------------------------------------------------------


def test_application_history_failure_is_explicit_with_real_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    portfolio, project, task = _scenario()
    decision = _complete_task_decision(portfolio, project.id, task.id)
    portfolio_repo, decision_repo, application_repo = _fresh_stack(
        tmp_path, portfolio
    )
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
            decision_record_id,
            DECISION_RECORDED_AT,
            decision,
            CHANGED_AT,
            application_record_id,
            APPLICATION_RECORDED_AT,
            decision_repository=decision_repo,
            application_repository=application_repo,
            portfolio_repository=portfolio_repo,
        )

    error = exc_info.value
    assert error.cause is injected
    assert error.transition_result.entity_id == task.id
    assert error.transition_result.new_status is EntityStatus.COMPLETED
    assert error.transition_result.portfolio.id == portfolio.id

    # The real application happened and is durable, the durable decision
    # record was appended, and NO application-history row exists.
    on_disk = portfolio_repo.load(portfolio.id)
    assert on_disk is not None
    assert on_disk.get_entity(task.id).status is EntityStatus.COMPLETED  # type: ignore[union-attr]
    assert len(decision_repo.list_history(portfolio.id)) == 1
    assert application_repo.list_history(portfolio.id) == ()


# ---------------------------------------------------------------------------
# 4 — a second identical call is rejected by the CURRENT state with ZERO
#    store growth
# ---------------------------------------------------------------------------


def test_second_identical_call_is_rejected_with_zero_store_growth(
    tmp_path: Path,
) -> None:
    portfolio, project, task = _scenario()
    decision = _complete_task_decision(portfolio, project.id, task.id)
    portfolio_repo, decision_repo, application_repo = _fresh_stack(
        tmp_path, portfolio
    )
    decision_record_id = _uuid()
    first_application_record_id = _uuid()

    coordinate_task_execution_lifecycle(
        decision_record_id,
        DECISION_RECORDED_AT,
        decision,
        CHANGED_AT,
        first_application_record_id,
        APPLICATION_RECORDED_AT,
        decision_repository=decision_repo,
        application_repository=application_repo,
        portfolio_repository=portfolio_repo,
    )

    # The exact same coordination call again: the durable CURRENT state
    # no longer admits the transition, so the coordinator rejects it and
    # neither store grows.
    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        coordinate_task_execution_lifecycle(
            _uuid(),
            DECISION_RECORDED_AT,
            decision,
            CHANGED_AT,
            _uuid(),
            APPLICATION_RECORDED_AT,
            decision_repository=decision_repo,
            application_repository=application_repo,
            portfolio_repository=portfolio_repo,
        )

    assert len(decision_repo.list_history(portfolio.id)) == 1
    assert len(application_repo.list_history(portfolio.id)) == 1


# ---------------------------------------------------------------------------
# Missing portfolio -> explicit typed error, zero writes
# ---------------------------------------------------------------------------


def test_missing_portfolio_is_explicit_and_writes_nothing(
    tmp_path: Path,
) -> None:
    portfolio, project, task = _scenario()
    decision = _complete_task_decision(portfolio, project.id, task.id)
    # NOTE: the portfolio is NOT persisted to the store; the decision
    # still references its id, so the store cannot resolve it.
    db_file = tmp_path / "coordination.sqlite"
    portfolio_repo = SqlitePortfolioRepository(db_file)
    _TRACKED.append(portfolio_repo)
    decision_repo = SqliteTaskExecutionLifecycleDecisionRepository(db_file)
    _TRACKED.append(decision_repo)
    application_repo = SqliteTaskExecutionLifecycleApplicationRepository(db_file)
    _TRACKED.append(application_repo)

    with pytest.raises(TaskExecutionLifecycleCoordinatorPortfolioNotFoundError):
        _run(decision, portfolio_repo, decision_repo, application_repo)

    assert portfolio_repo.load(portfolio.id) is None
    assert decision_repo.list_history(portfolio.id) == ()
    assert application_repo.list_history(portfolio.id) == ()
