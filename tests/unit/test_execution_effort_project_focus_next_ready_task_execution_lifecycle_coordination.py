"""V1.57 — minimal application-layer coordinator for the V1.50–V1.56 chain.

Focused unit tests for ``coordinate_task_execution_lifecycle``:

1. successful admitted decision -> durable decision -> application ->
   durable application, with the EXACT deterministic call order and full
   identity preservation;
2. CURRENT rejection with ZERO downstream side effects;
3. decision persistence failure stops before application;
4. application failure creates NO application success-history record;
5. application-history persistence failure is explicit AFTER a real
   application;
6. mismatched / hostile identities rejected;
7. deterministic call ordering and identity preservation;
8. executable architecture guards (no UUID generation, no wall clock,
   no provider / runtime / shell surface).

Coverage 8 (SQLite-backed integration) lives in
``tests/integration/test_task_execution_lifecycle_coordination_sqlite.py``.
"""

from __future__ import annotations

import ast
import itertools
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest

import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_coordination as coordination_module  # noqa: E501
from trajectory_os.application import (
    TaskExecutionLifecycleAdmissionError,
    TaskExecutionLifecycleApplicationHistoryPersistenceError,
    TaskExecutionLifecycleCoordinatorError,
    TaskExecutionLifecycleCoordinatorPortfolioNotFoundError,
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDisposition,
    coordinate_task_execution_lifecycle,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

DECIDED_AT = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
DECISION_RECORDED_AT = datetime(
    2026, 2, 2, 10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
)
BASE_TS = datetime(2026, 2, 3, 12, 0, tzinfo=UTC)
CHANGED_AT = BASE_TS + timedelta(days=1)
APPLICATION_RECORDED_AT = datetime(2026, 2, 4, 7, 15, tzinfo=UTC)

_UUID_SEQUENCE = itertools.count(1_000)


def _uuid() -> UUID:
    """Deterministic sequential test identity (test-only helper)."""
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


def _task_entity(
    status: EntityStatus = EntityStatus.ACTIVE,
) -> TrajectoryEntity:
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


def _with_task_status(
    portfolio: Portfolio, status: EntityStatus
) -> Portfolio:
    """SAME portfolio identity, TASK entities carrying ``status``."""
    return Portfolio(
        id=portfolio.id,
        name=portfolio.name,
        entities=[
            entity.model_copy(update={"status": status})
            if entity.entity_type is EntityType.TASK
            else entity
            for entity in portfolio.entities
        ],
        relations=portfolio.relations,
    )


def _scenario() -> tuple[
    Portfolio,
    TaskExecutionLifecycleDecision,
    UUID,
    UUID,
    UUID,
]:
    """One admissible CURRENT portfolio plus one genuine COMPLETE_TASK
    V1.50 decision referencing its exact identities, plus fresh
    caller-supplied durable-record identities."""

    project = _project_entity()
    task = _task_entity(EntityStatus.ACTIVE)
    portfolio = Portfolio(
        id=_uuid(),
        name="coordination-unit",
        entities=[project, task],
        relations=[_membership(task.id, project.id)],
    )
    decision = TaskExecutionLifecycleDecision(
        lifecycle_decision_id=_uuid(),
        decided_at=DECIDED_AT,
        execution_record_id=_uuid(),
        execution_recorded_at=DECIDED_AT - timedelta(days=1),
        request_id=_uuid(),
        intent_id=_uuid(),
        execution_decision_id=_uuid(),
        portfolio_id=portfolio.id,
        authorized_project_id=project.id,
        authorized_task_id=task.id,
        execution_succeeded=True,
        disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )
    return (
        portfolio,
        decision,
        task.id,
        _uuid(),  # decision_record_id
        _uuid(),  # application_record_id
    )


class OrderedLog:
    """Shared deterministic call-order log for the fake repositories."""

    def __init__(self) -> None:
        self.actions: list[str] = []


_SENTINEL = object()


class FakePortfolioRepository:
    """Scriptable structural ``PortfolioRepository`` double.

    ``second_load`` may pin what the repository returns on its second and
    later loads (used to simulate CURRENT-state drift TOCTOU); by default
    later loads behave like the first.
    """

    def __init__(
        self,
        log: OrderedLog,
        portfolios: dict[UUID, Portfolio] | None = None,
        *,
        second_load: Portfolio | None = _SENTINEL,  # type: ignore[assignment]
        save_error: Exception | None = None,
    ) -> None:
        self._log = log
        self._portfolios = dict(portfolios or {})
        self._second_load = second_load
        self._second_loaded = False
        self._save_error = save_error
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self._log.actions.append("portfolio.load")
        if not self._second_loaded:
            self._second_loaded = True
            return self._portfolios.get(portfolio_id)
        if self._second_load is _SENTINEL:
            return self._portfolios.get(portfolio_id)
        return self._second_load

    def save(self, portfolio: Portfolio) -> None:
        self._log.actions.append("portfolio.save")
        if self._save_error is not None:
            raise self._save_error
        self.saved.append(portfolio)


class FakeDecisionRepository:
    """Scriptable V1.55 ``TaskExecutionLifecycleDecisionRepository``
    double."""

    def __init__(self, log: OrderedLog, *, add_error: Exception | None = None) -> None:
        self._log = log
        self._add_error = add_error
        self.added: list[Any] = []

    def add(self, record: Any) -> None:
        self._log.actions.append("decision.add")
        if self._add_error is not None:
            raise self._add_error
        self.added.append(record)

    def list_history(self, portfolio_id: UUID) -> tuple[Any, ...]:
        self._log.actions.append("decision.list_history")
        return tuple(self.added)


class FakeApplicationRepository:
    """Scriptable V1.53
    ``TaskExecutionLifecycleApplicationRepository`` double."""

    def __init__(
        self, log: OrderedLog, *, add_error: Exception | None = None
    ) -> None:
        self._log = log
        self._add_error = add_error
        self.added: list[Any] = []

    def add(self, record: Any) -> None:
        self._log.actions.append("application.add")
        if self._add_error is not None:
            raise self._add_error
        self.added.append(record)

    def list_history(self, portfolio_id: UUID) -> tuple[Any, ...]:
        self._log.actions.append("application.list_history")
        return tuple(self.added)


def _fake_stack(
    portfolio: Portfolio,
    *,
    second_load: Portfolio | None = _SENTINEL,  # type: ignore[assignment]
    decision_add_error: Exception | None = None,
    save_error: Exception | None = None,
    application_add_error: Exception | None = None,
) -> tuple[
    OrderedLog,
    FakePortfolioRepository,
    FakeDecisionRepository,
    FakeApplicationRepository,
]:
    log = OrderedLog()
    return (
        log,
        FakePortfolioRepository(
            log,
            {portfolio.id: portfolio},
            second_load=second_load,
            save_error=save_error,
        ),
        FakeDecisionRepository(log, add_error=decision_add_error),
        FakeApplicationRepository(log, add_error=application_add_error),
    )


def _run(
    decision: TaskExecutionLifecycleDecision,
    portfolio_repo: FakePortfolioRepository,
    decision_repo: FakeDecisionRepository,
    application_repo: FakeApplicationRepository,
    *,
    decision_record_id: Any | None = None,
    application_record_id: Any | None = None,
) -> Any:
    if decision_record_id is None:
        decision_record_id = _uuid()
    if application_record_id is None:
        application_record_id = _uuid()
    return coordinate_task_execution_lifecycle(
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


# ---------------------------------------------------------------------------
# 1 / 7 — happy path: deterministic call order + identity preservation
# ---------------------------------------------------------------------------


def test_happy_path_deterministic_order_and_identity_preservation() -> None:
    portfolio, decision, task_id, decision_record_id, application_record_id = (
        _scenario()
    )
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio)

    outcome = _run(
        decision,
        portfolio_repo,
        decision_repo,
        application_repo,
        decision_record_id=decision_record_id,
        application_record_id=application_record_id,
    )

    # Exact deterministic call order: CURRENT admission (one load) ->
    # durable decision append -> V1.52 replay (second load) -> V1.52 save
    # -> durable application append.
    assert log.actions == [
        "portfolio.load",
        "decision.add",
        "portfolio.load",
        "portfolio.save",
        "application.add",
    ]
    assert len(portfolio_repo.saved) == 1

    # Durable decision record: exact V1.50 decision, caller-supplied
    # identity and original offset preserved verbatim.
    assert outcome.decision_record.decision_record_id == decision_record_id
    assert outcome.decision_record.recorded_at == DECISION_RECORDED_AT
    assert outcome.decision_record.decision == decision
    assert decision_repo.added == [outcome.decision_record]

    # Real application: EXACT V1.52 transition result for the exact
    # target task, COMPLETED at exactly the caller-supplied changed_at.
    transition = outcome.transition_result
    assert transition.entity_id == task_id
    assert transition.previous_status is EntityStatus.ACTIVE
    assert transition.new_status is EntityStatus.COMPLETED
    assert transition.changed_at == CHANGED_AT
    assert (
        transition.portfolio.get_entity(task_id).status  # type: ignore[union-attr]
        is EntityStatus.COMPLETED
    )
    # The one and only saved portfolio IS the transitioned portfolio.
    assert portfolio_repo.saved[0] is transition.portfolio
    # The non-target project entity is untouched.
    assert (
        transition.portfolio.get_entity(
            decision.authorized_project_id
        ).status  # type: ignore[union-attr]
        is EntityStatus.ACTIVE
    )

    # Durable application record: exact same transition result embedded.
    assert (
        outcome.application_record.application_record_id == application_record_id
    )
    assert outcome.application_record.recorded_at == APPLICATION_RECORDED_AT
    assert outcome.application_record.result == transition
    assert application_repo.added == [outcome.application_record]


def test_deterministic_outcome_values_for_identical_inputs() -> None:
    """Same caller-supplied inputs and same CURRENT portfolio -> same action
    sequence and same outcome values across independent runs."""

    portfolio, decision, task_id, d_id, a_id = _scenario()

    def run_once() -> tuple[list[str], Any]:
        (
            log,
            portfolio_repo,
            decision_repo,
            application_repo,
        ) = _fake_stack(portfolio)
        outcome = _run(
            decision,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )
        return list(log.actions), outcome

    first_actions, first = run_once()
    second_actions, second = run_once()

    assert first_actions == second_actions
    assert first_actions == [
        "portfolio.load",
        "decision.add",
        "portfolio.load",
        "portfolio.save",
        "application.add",
    ]
    # Identity chain: the exact same decision, exact same transition
    # provenance, exact same record shapes and full portfolio equality.
    assert first.decision_record.decision == second.decision_record.decision
    assert (
        first.transition_result.model_dump(exclude={"portfolio"})
        == second.transition_result.model_dump(exclude={"portfolio"})
    )
    assert first.transition_result.portfolio == second.transition_result.portfolio
    assert first.application_record.result == second.application_record.result


# ---------------------------------------------------------------------------
# 2 — CURRENT rejection with ZERO downstream side effects
# ---------------------------------------------------------------------------


def test_current_rejection_already_completed_task_zero_side_effects() -> None:
    portfolio, decision, _task, d_id, a_id = _scenario()
    rejected = _with_task_status(portfolio, EntityStatus.COMPLETED)
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(rejected)

    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        _run(
            decision,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )

    assert log.actions == ["portfolio.load"]
    assert decision_repo.added == []
    assert application_repo.added == []
    assert portfolio_repo.saved == []


def test_current_rejection_no_lifecycle_change_zero_side_effects() -> None:
    portfolio, decision, _task, d_id, a_id = _scenario()
    no_change = TaskExecutionLifecycleDecision(
        **{
            **decision.model_dump(),
            "disposition": TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
        }
    )
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio)

    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        _run(
            no_change,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )

    assert log.actions == ["portfolio.load"]
    assert decision_repo.added == []
    assert application_repo.added == []
    assert portfolio_repo.saved == []


def test_current_rejection_missing_membership_zero_side_effects() -> None:
    portfolio, decision, _task, d_id, a_id = _scenario()
    no_membership = Portfolio(
        id=portfolio.id,
        name=portfolio.name,
        entities=portfolio.entities,
        relations=[],
    )
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(no_membership)

    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        _run(
            decision,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )

    assert log.actions == ["portfolio.load"]
    assert decision_repo.added == []
    assert application_repo.added == []
    assert portfolio_repo.saved == []


# ---------------------------------------------------------------------------
# 3 — decision persistence failure stops BEFORE application
# ---------------------------------------------------------------------------


def test_decision_append_failure_stops_before_application() -> None:
    portfolio, decision, _task, d_id, a_id = _scenario()
    injected = RuntimeError("decision store unavailable")
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio, decision_add_error=injected)

    with pytest.raises(RuntimeError, match="decision store unavailable"):
        _run(
            decision,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )

    # Stopped exactly at the decision append: no second load, no save,
    # no application or application-history record.
    assert log.actions == ["portfolio.load", "decision.add"]
    assert portfolio_repo.saved == []
    assert application_repo.added == []


def test_invalid_decision_record_identity_stops_before_application() -> None:
    """A bad decision-record identity is rejected with a typed ValueError
    BEFORE any real application or application-history record."""
    portfolio, decision, _task, _d_id, a_id = _scenario()
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio)

    with pytest.raises(ValueError):
        _run(
            decision,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id="not-a-uuid",
            application_record_id=a_id,
        )

    assert log.actions == ["portfolio.load"]
    assert decision_repo.added == []
    assert portfolio_repo.saved == []
    assert application_repo.added == []


# ---------------------------------------------------------------------------
# 4 — application failure creates NO application success-history record
# ---------------------------------------------------------------------------


def test_application_replay_rejection_creates_no_application_record() -> None:
    portfolio, decision, _task, d_id, a_id = _scenario()
    # TOCTOU double: the CURRENT state re-derived at the V1.52 boundary
    # (fresh load) no longer admits the transition (task already
    # COMPLETED), so the V1.52 application fails before any save.
    drifted = _with_task_status(portfolio, EntityStatus.COMPLETED)
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio, second_load=drifted)

    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        _run(
            decision,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )

    # The durable decision WAS appended (earlier canonical step), the
    # application was rejected at its own authoritative boundary with
    # ZERO saves, and NO application-history record exists.
    assert log.actions == [
        "portfolio.load",
        "decision.add",
        "portfolio.load",
    ]
    assert len(decision_repo.added) == 1
    assert decision_repo.added[0].decision == decision
    assert portfolio_repo.saved == []
    assert application_repo.added == []


def test_application_save_failure_creates_no_application_record() -> None:
    portfolio, decision, _task, d_id, a_id = _scenario()
    injected = RuntimeError("portfolio store unavailable")
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio, save_error=injected)

    with pytest.raises(RuntimeError, match="portfolio store unavailable"):
        _run(
            decision,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )

    # The real transition was attempted and its save failed; the
    # canonical sequence then STOPS — no application-history record.
    assert log.actions == [
        "portfolio.load",
        "decision.add",
        "portfolio.load",
        "portfolio.save",
    ]
    assert portfolio_repo.saved == []
    assert application_repo.added == []


# ---------------------------------------------------------------------------
# 5 — application-history persistence failure AFTER a real application is
#    surfaced explicitly
# ---------------------------------------------------------------------------


def test_application_history_failure_is_explicit_after_real_application() -> None:
    portfolio, decision, task_id, d_id, a_id = _scenario()
    injected = RuntimeError("application history store unavailable")
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio, application_add_error=injected)

    with pytest.raises(
        TaskExecutionLifecycleApplicationHistoryPersistenceError,
        match="ACTUALLY applied and durably saved",
    ) as exc_info:
        _run(
            decision,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )

    # The real application DID happen and WAS durably saved.
    assert log.actions == [
        "portfolio.load",
        "decision.add",
        "portfolio.load",
        "portfolio.save",
        "application.add",
    ]
    assert len(portfolio_repo.saved) == 1
    assert (
        portfolio_repo.saved[0].get_entity(task_id).status  # type: ignore[union-attr]
        is EntityStatus.COMPLETED
    )

    # Explicitly surfaced: exact real transition result attached,
    # original cause chained, and NO application-history success record
    # exists.
    error = exc_info.value
    assert isinstance(error, TaskExecutionLifecycleCoordinatorError)
    assert error.cause is injected
    assert error.__cause__ is injected
    assert error.transition_result.entity_id == task_id
    assert error.transition_result.new_status is EntityStatus.COMPLETED
    assert application_repo.added == []


# ---------------------------------------------------------------------------
# 6 — mismatched / hostile identities rejected
# ---------------------------------------------------------------------------


def test_missing_portfolio_rejected_before_any_side_effect() -> None:
    portfolio, decision, _task, d_id, a_id = _scenario()
    other_portfolio_id = _uuid()
    mismatched = TaskExecutionLifecycleDecision(
        **{**decision.model_dump(), "portfolio_id": other_portfolio_id}
    )
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio)

    with pytest.raises(TaskExecutionLifecycleCoordinatorPortfolioNotFoundError):
        _run(
            mismatched,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )

    # The decision references a portfolio the store cannot resolve:
    # the coordinator must reject it before any durable decision
    # record, application, or application-history interaction happens.
    assert log.actions == ["portfolio.load"]
    assert decision_repo.added == []
    assert application_repo.added == []
    assert portfolio_repo.saved == []


def test_unknown_task_identity_rejected_zero_side_effects() -> None:
    portfolio, decision, _task, d_id, a_id = _scenario()
    unknown_task = TaskExecutionLifecycleDecision(
        **{**decision.model_dump(), "authorized_task_id": _uuid()}
    )
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio)

    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        _run(
            unknown_task,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )

    assert log.actions == ["portfolio.load"]
    assert decision_repo.added == []
    assert application_repo.added == []
    assert portfolio_repo.saved == []


def test_project_task_identity_swap_rejected_zero_side_effects() -> None:
    portfolio, decision, _task, d_id, a_id = _scenario()
    swapped = TaskExecutionLifecycleDecision(
        **{
            **decision.model_dump(),
            "authorized_project_id": decision.authorized_task_id,
            "authorized_task_id": decision.authorized_project_id,
        }
    )
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio)

    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        _run(
            swapped,
            portfolio_repo,
            decision_repo,
            application_repo,
            decision_record_id=d_id,
            application_record_id=a_id,
        )

    assert log.actions == ["portfolio.load"]
    assert decision_repo.added == []
    assert application_repo.added == []
    assert portfolio_repo.saved == []


def test_non_genuine_decision_rejected_before_any_repository_interaction() -> None:
    portfolio, decision, _task, d_id, a_id = _scenario()
    (
        log,
        portfolio_repo,
        decision_repo,
        application_repo,
    ) = _fake_stack(portfolio)
    hostile = decision.model_dump()

    with pytest.raises(TaskExecutionLifecycleCoordinatorError):
        coordinate_task_execution_lifecycle(
            d_id,
            DECISION_RECORDED_AT,
            hostile,
            CHANGED_AT,
            a_id,
            APPLICATION_RECORDED_AT,
            decision_repository=decision_repo,
            application_repository=application_repo,
            portfolio_repository=portfolio_repo,
        )

    assert log.actions == []
    assert decision_repo.added == []
    assert application_repo.added == []
    assert portfolio_repo.saved == []


# ---------------------------------------------------------------------------
# Architecture guards (executable)
# ---------------------------------------------------------------------------


def _source() -> str:
    assert coordination_module.__file__ is not None
    with open(coordination_module.__file__, encoding="utf-8") as handle:
        return handle.read()


def test_module_never_generates_identities_or_reads_the_wall_clock() -> None:
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            value_name = func.value.id if isinstance(func.value, ast.Name) else None
            assert not (value_name == "datetime" and func.attr == "now"), (
                "module reads the wall clock"
            )
            assert func.attr not in {"uuid4", "uuid5", "uuid6", "uuid7"}, (
                f"module generates an identity: {func.attr}"
            )
        elif isinstance(func, ast.Name):
            assert func.id not in {"uuid4", "uuid5", "uuid6", "uuid7"}, (
                f"module generates an identity: {func.id}"
            )


def test_module_does_not_reference_forbidden_surfaces() -> None:
    text = _source()
    for forbidden in (
        "subprocess",
        "os.system",
        "requests.",
        "httpx",
        "urllib",
        "import sqlite3",
        "threading",
    ):
        assert forbidden not in text, f"forbidden surface referenced: {forbidden}"
