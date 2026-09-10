"""V1.63 — durable lifecycle coordination orchestration.

Focused unit tests plus executable (AST) architecture guards for
``execution_effort_project_focus_next_ready_task_execution_lifecycle_durable_coordination``.

The public boundary
``coordinate_task_execution_lifecycle_durably`` must perform EXACTLY:

1. V1.60 ``coordinate_task_execution_lifecycle`` EXACTLY ONCE with every
   caller-supplied V1.60 value/repository passed through unchanged; a
   V1.60 failure propagates the EXACT original error unchanged and the
   V1.61 boundary and the outcome repository are never touched;
2. ONLY after the exact genuine V1.60 outcome is returned successfully,
   V1.61 ``record_task_execution_lifecycle_outcome_durably`` EXACTLY
   ONCE with the exact caller ``outcome_record_id``, the exact caller
   ``outcome_recorded_at``, that EXACT outcome, and the exact
   ``outcome_repository``;
3. return the EXACT ``TaskExecutionLifecycleOutcomeRecord`` returned by
   V1.61, unchanged; STOP.

A V1.61 failure — possible ONLY after a successful V1.60 — must be
surfaced as ``TaskExecutionLifecycleOutcomeHistoryPersistenceError``
carrying the exact successful ``outcome`` (``outcome``) and the exact
original ``cause`` (``cause``, chained). V1.60 failures are NEVER
wrapped. The module must contain no retry, no compensation, no
rollback, no recovery, no UUID generation, no wall clock, no concrete
persistence / provider / runtime / storage dependency, and no direct
V1.51/V1.52/V1.53/V1.55/V1.58 calls of its own.
"""

from __future__ import annotations

import ast
import inspect
import itertools
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest

import trajectory_os.application as application  # noqa: E501
import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_durable_coordination as durable_coord  # noqa: E501
from trajectory_os.application import (
    DurableTaskExecutionLifecycleOutcomeError,
    TaskExecutionLifecycleAdmissionRecord,
    TaskExecutionLifecycleApplicationRecord,
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDecisionRecord,
    TaskExecutionLifecycleDisposition,
    TaskExecutionLifecycleOutcome,
    TaskExecutionLifecycleOutcomeHistoryPersistenceError,
    TaskExecutionLifecycleOutcomeRecord,
    admit_current_task_execution_lifecycle,
    coordinate_task_execution_lifecycle_durably,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.entity_status_transition import transition_entity_status
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

BASE_TS = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
EXECUTED_AT = datetime(
    2026, 3, 1, 9, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
)
DECIDED_AT = datetime(2026, 3, 2, 10, 0, tzinfo=timezone(timedelta(minutes=45)))
CHANGED_AT = datetime(2026, 3, 3, 7, 30, tzinfo=timezone(timedelta(hours=2)))
ADMISSION_RECORDED_AT = datetime(2026, 3, 4, 1, 0, tzinfo=UTC)
DECISION_RECORDED_AT = datetime(
    2026, 3, 4, 3, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
)
APPLICATION_RECORDED_AT = datetime(
    2026, 3, 4, 5, 30, tzinfo=timezone(timedelta(hours=-3, minutes=30))
)
OUTCOME_RECORDED_AT = datetime(
    2026, 3, 4, 9, 45, tzinfo=timezone(timedelta(hours=-5, minutes=45))
)

_UUID_SEQUENCE = itertools.count(10_000)


def _uuid() -> UUID:
    """Deterministic sequential test identity (test-only helper)."""
    return UUID(int=next(_UUID_SEQUENCE))


def _genuine_v160_outcome() -> tuple[Any, TaskExecutionLifecycleOutcome]:
    """Build one genuine, complete V1.60 outcome through the EXISTING
    canonical public boundaries (V1.50 decision -> V1.51 admission ->
    V1.58/V1.55 records -> V1.52 transition -> V1.53 record -> V1.60
    outcome). The module under test never re-creates any of it."""

    project = TrajectoryEntity(
        id=_uuid(),
        entity_type=EntityType.PROJECT,
        title="project",
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )
    task = TrajectoryEntity(
        id=_uuid(),
        entity_type=EntityType.TASK,
        title="task",
        status=EntityStatus.SOMEDAY,
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )
    portfolio = Portfolio(
        id=_uuid(),
        name="unit",
        entities=[project, task],
        relations=[
            TrajectoryRelation(
                id=_uuid(),
                source_id=task.id,
                target_id=project.id,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )
    decision = TaskExecutionLifecycleDecision(
        lifecycle_decision_id=_uuid(),
        decided_at=DECIDED_AT,
        execution_record_id=_uuid(),
        execution_recorded_at=EXECUTED_AT,
        request_id=_uuid(),
        intent_id=_uuid(),
        execution_decision_id=_uuid(),
        portfolio_id=portfolio.id,
        authorized_project_id=project.id,
        authorized_task_id=task.id,
        execution_succeeded=True,
        disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )
    admission = admit_current_task_execution_lifecycle(decision, portfolio)
    admission_record = TaskExecutionLifecycleAdmissionRecord(
        admission_record_id=_uuid(),
        recorded_at=ADMISSION_RECORDED_AT,
        admission=admission,
    )
    decision_record = TaskExecutionLifecycleDecisionRecord(
        decision_record_id=_uuid(),
        recorded_at=DECISION_RECORDED_AT,
        decision=decision,
    )
    result = transition_entity_status(
        portfolio, task.id, EntityStatus.COMPLETED, CHANGED_AT
    )
    application_record = TaskExecutionLifecycleApplicationRecord(
        application_record_id=_uuid(),
        recorded_at=APPLICATION_RECORDED_AT,
        result=result,
    )
    outcome = TaskExecutionLifecycleOutcome(
        admission_record=admission_record,
        decision_record=decision_record,
        transition_result=result,
        application_record=application_record,
    )
    return portfolio, outcome


class _RecordingRepository:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.add_calls = 0

    def add(self, record: Any) -> None:
        self.add_calls += 1
        self.added.append(record)

    def list_history(self, portfolio_id: UUID) -> tuple[Any, ...]:
        return tuple(self.added)


class _PortfolioRepository:
    def __init__(self, portfolio: Portfolio) -> None:
        self._portfolio = portfolio
        self.loaded: list[UUID] = []
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.loaded.append(portfolio_id)
        if self._portfolio.id == portfolio_id:
            return self._portfolio
        return None

    def save(self, portfolio: Portfolio) -> None:
        self.saved.append(portfolio)


class _ExplodingRecordingRepository(_RecordingRepository):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    def add(self, record: Any) -> None:
        self.add_calls += 1
        raise self._error


def _caller_args() -> tuple[tuple[Any, ...], dict[str, Any]]:
    """One full caller argument signature (V1.60 values + V1.61 values)."""

    portfolio, outcome = _genuine_v160_outcome()
    positions = (
        _uuid(),
        ADMISSION_RECORDED_AT,
        _uuid(),
        DECISION_RECORDED_AT,
        outcome.decision_record.decision,
        CHANGED_AT,
        _uuid(),
        APPLICATION_RECORDED_AT,
    )
    keyword_only = {
        "admission_repository": _RecordingRepository(),
        "decision_repository": _RecordingRepository(),
        "application_repository": _RecordingRepository(),
        "portfolio_repository": _PortfolioRepository(portfolio),
        "outcome_record_id": _uuid(),
        "outcome_recorded_at": OUTCOME_RECORDED_AT,
        "outcome_repository": _RecordingRepository(),
    }
    return positions, keyword_only


def _record_v161_failure(
    monkeypatch: pytest.MonkeyPatch,
    events: list[Any],
    outcome: TaskExecutionLifecycleOutcome,
    error: Exception,
) -> None:
    """Spy BOTH boundaries: V1.60 succeeds returning EXACTLY ``outcome``;
    V1.61 raises exactly the supplied original error."""

    def _fake_v160(*args: Any, **kwargs: Any) -> Any:
        events.append(("v160.call", args, kwargs))
        events.append(("v160.success", outcome))
        return outcome

    def _fake_v161(*args: Any, **kwargs: Any) -> Any:
        events.append(("v161.call", args, kwargs))
        raise error

    monkeypatch.setattr(
        durable_coord,
        "coordinate_task_execution_lifecycle",
        _fake_v160,
    )
    monkeypatch.setattr(
        durable_coord,
        "record_task_execution_lifecycle_outcome_durably",
        _fake_v161,
    )


# -- genuine end-to-end orchestration (real V1.60 + real V1.61) -----------


def test_happy_path_composes_real_v160_then_real_v161_once_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    real_v160 = durable_coord.coordinate_task_execution_lifecycle
    real_v161 = durable_coord.record_task_execution_lifecycle_outcome_durably
    outcome_box: list[TaskExecutionLifecycleOutcome] = []
    record_box: list[TaskExecutionLifecycleOutcomeRecord] = []

    def _counting_v160(*args: Any, **kwargs: Any) -> TaskExecutionLifecycleOutcome:
        order.append("v160.call")
        outcome = real_v160(*args, **kwargs)
        outcome_box.append(outcome)
        order.append("v160.success")
        return outcome

    def _counting_v161(*args: Any, **kwargs: Any) -> TaskExecutionLifecycleOutcomeRecord:
        order.append("v161.call")
        assert "v160.success" in order  # strictly AFTER V1.60 success
        record = real_v161(*args, **kwargs)
        record_box.append(record)
        order.append("v161.success")
        return record

    monkeypatch.setattr(durable_coord, "coordinate_task_execution_lifecycle", _counting_v160)
    monkeypatch.setattr(
        durable_coord,
        "record_task_execution_lifecycle_outcome_durably",
        _counting_v161,
    )

    positions, keyword_only = _caller_args()
    record = coordinate_task_execution_lifecycle_durably(*positions, **keyword_only)

    # 1. V1.60 invoked EXACTLY once, then V1.61 invoked EXACTLY once after it.
    assert order == ["v160.call", "v160.success", "v161.call", "v161.success"]
    # 2. The EXACT V1.60 outcome was supplied to V1.61 (identity).
    assert len(outcome_box) == 1 and len(record_box) == 1
    assert record_box[0].outcome == outcome_box[0]
    # 3. The returned value is the EXACT V1.61 record, unchanged.
    assert record is record_box[0]
    assert isinstance(record, TaskExecutionLifecycleOutcomeRecord)
    # 4. Caller V1.61 identities preserved EXACTLY.
    assert record.outcome_record_id == keyword_only["outcome_record_id"]
    assert record.recorded_at == OUTCOME_RECORDED_AT
    # 5. Every repository append happened EXACTLY once.
    assert keyword_only["admission_repository"].add_calls == 1
    assert keyword_only["decision_repository"].add_calls == 1
    assert keyword_only["application_repository"].add_calls == 1
    assert keyword_only["outcome_repository"].add_calls == 1
    assert (
        keyword_only["outcome_repository"].added[0] is record_box[0]
    )


def test_happy_path_uninstrumented_genuinely_orchestrates() -> None:
    positions, keyword_only = _caller_args()
    record = coordinate_task_execution_lifecycle_durably(*positions, **keyword_only)

    assert isinstance(record, TaskExecutionLifecycleOutcomeRecord)
    assert record.outcome_record_id == keyword_only["outcome_record_id"]
    assert record.recorded_at == OUTCOME_RECORDED_AT
    for repository in (
        keyword_only["admission_repository"],
        keyword_only["decision_repository"],
        keyword_only["application_repository"],
        keyword_only["outcome_repository"],
    ):
        assert repository.add_calls == 1
    # The single outcome-history append holds the returned record exactly.
    assert keyword_only["outcome_repository"].added[0] is record
    # Portfolio saved exactly once (inside V1.60's V1.52 step); V1.63 adds none.
    portfolio_repository = keyword_only["portfolio_repository"]
    assert len(portfolio_repository.saved) == 1


# -- orchestration order and exact forwarding (narrow spies) ---------------


def test_v160_once_then_v161_exactly_once_strictly_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []
    outcome = _genuine_v160_outcome()[1]
    sentinel_record = TaskExecutionLifecycleOutcomeRecord(
        outcome_record_id=_uuid(),
        recorded_at=OUTCOME_RECORDED_AT,
        outcome=outcome,
    )

    def _fake_v160(*args: Any, **kwargs: Any) -> TaskExecutionLifecycleOutcome:
        events.append(("v160.call", args, kwargs))
        events.append(("v160.success", outcome))
        return outcome

    def _fake_v161(*args: Any, **kwargs: Any) -> TaskExecutionLifecycleOutcomeRecord:
        events.append(("v161.call", args, kwargs))
        return sentinel_record

    monkeypatch.setattr(durable_coord, "coordinate_task_execution_lifecycle", _fake_v160)
    monkeypatch.setattr(
        durable_coord,
        "record_task_execution_lifecycle_outcome_durably",
        _fake_v161,
    )

    positions, keyword_only = _caller_args()
    result = coordinate_task_execution_lifecycle_durably(*positions, **keyword_only)

    kinds = [event[0] for event in events]
    assert kinds == ["v160.call", "v160.success", "v161.call"]  # each EXACTLY once
    # 17: the exact V1.60 outcome (identity) reached V1.61.
    v161_call = events[2]
    assert v161_call[1][2] is outcome
    # 14/15: caller outcome identities forwarded EXACTLY.
    assert v161_call[1][0] is keyword_only["outcome_record_id"]
    assert v161_call[1][1] is keyword_only["outcome_recorded_at"]
    assert v161_call[2]["repository"] is keyword_only["outcome_repository"]
    # 16: the EXACT V1.61 record is returned, by identity.
    assert result is sentinel_record


def test_v160_receives_all_caller_values_and_repositories_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []
    outcome = _genuine_v160_outcome()[1]
    sentinel_record = TaskExecutionLifecycleOutcomeRecord(
        outcome_record_id=_uuid(),
        recorded_at=OUTCOME_RECORDED_AT,
        outcome=outcome,
    )

    def _fake_v160(*args: Any, **kwargs: Any) -> TaskExecutionLifecycleOutcome:
        events.append(("v160.call", args, kwargs))
        return outcome

    def _fake_v161(*args: Any, **kwargs: Any) -> TaskExecutionLifecycleOutcomeRecord:
        events.append(("v161.call", args, kwargs))
        return sentinel_record

    monkeypatch.setattr(durable_coord, "coordinate_task_execution_lifecycle", _fake_v160)
    monkeypatch.setattr(
        durable_coord,
        "record_task_execution_lifecycle_outcome_durably",
        _fake_v161,
    )

    positions, keyword_only = _caller_args()
    coordinate_task_execution_lifecycle_durably(*positions, **keyword_only)

    (tag, args, kwargs) = events[0]
    assert tag == "v160.call"
    assert tuple(args) == positions
    for forwarded, supplied in zip(args, positions, strict=True):
        assert forwarded is supplied
    assert kwargs == {
        "admission_repository": keyword_only["admission_repository"],
        "decision_repository": keyword_only["decision_repository"],
        "application_repository": keyword_only["application_repository"],
        "portfolio_repository": keyword_only["portfolio_repository"],
    }


# -- V1.60 failure semantics ------------------------------------------------


class _V160BoundaryError(ValueError):
    """One original, genuine V1.60 boundary error (test double)."""


def test_v160_failure_propagates_exact_original_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []
    original = _V160BoundaryError("v160 exploded")

    def _fake_v160(*args: Any, **kwargs: Any) -> Any:
        events.append(("v160.call",))
        raise original

    def _fake_v161(*args: Any, **kwargs: Any) -> Any:
        events.append(("v161.call",))
        return None

    monkeypatch.setattr(durable_coord, "coordinate_task_execution_lifecycle", _fake_v160)
    monkeypatch.setattr(
        durable_coord,
        "record_task_execution_lifecycle_outcome_durably",
        _fake_v161,
    )

    positions, keyword_only = _caller_args()
    assert keyword_only["outcome_repository"].add_calls == 0

    with pytest.raises(_V160BoundaryError) as excinfo:
        coordinate_task_execution_lifecycle_durably(*positions, **keyword_only)

    # Exact original error propagated unchanged: same instance, not wrapped.
    assert excinfo.value is original
    # 8: zero V1.61 interaction, zero outcome_repository interaction.
    assert [event[0] for event in events] == ["v160.call"]
    assert keyword_only["outcome_repository"].add_calls == 0


def test_v160_failure_is_never_type_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A V1.60 failure must never surface as the POST-V1.60 error type."""

    def _fake_v160(*args: Any, **kwargs: Any) -> Any:
        raise _V160BoundaryError("v160 exploded")

    def _fake_v161(*args: Any, **kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(durable_coord, "coordinate_task_execution_lifecycle", _fake_v160)
    monkeypatch.setattr(
        durable_coord,
        "record_task_execution_lifecycle_outcome_durably",
        _fake_v161,
    )

    positions, _keyword_only = _caller_args()
    with pytest.raises(_V160BoundaryError) as excinfo:
        coordinate_task_execution_lifecycle_durably(*positions, **_keyword_only)

    assert type(excinfo.value) is _V160BoundaryError
    assert not isinstance(
        excinfo.value, TaskExecutionLifecycleOutcomeHistoryPersistenceError
    )


# -- POST-V1.60 (V1.61) failure semantics -----------------------------------


def test_v161_failure_only_happens_after_successful_v160(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []
    outcome = _genuine_v160_outcome()[1]
    original = RuntimeError("outcome repository persistence failed")
    _record_v161_failure(monkeypatch, events, outcome, original)

    positions, keyword_only = _caller_args()
    with pytest.raises(TaskExecutionLifecycleOutcomeHistoryPersistenceError):
        coordinate_task_execution_lifecycle_durably(*positions, **keyword_only)
    # The V1.61 failure is possible ONLY after the V1.60 success.
    assert [event[0] for event in events] == [
        "v160.call",
        "v160.success",
        "v161.call",
    ]


def test_v161_validation_failure_wrapped_with_exact_outcome_and_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []
    outcome = _genuine_v160_outcome()[1]
    original = DurableTaskExecutionLifecycleOutcomeError("v161 validation failed")
    _record_v161_failure(monkeypatch, events, outcome, original)

    positions, _keyword_only = _caller_args()
    with pytest.raises(TaskExecutionLifecycleOutcomeHistoryPersistenceError) as excinfo:
        coordinate_task_execution_lifecycle_durably(*positions, **_keyword_only)

    error = excinfo.value
    # 11: carries the EXACT successful V1.60 outcome (identity).
    assert error.outcome is outcome
    # 12: carries and chains the EXACT original cause.
    assert error.cause is original
    assert error.__cause__ is original
    # No retry of either boundary.
    assert [event[0] for event in events] == ["v160.call", "v160.success", "v161.call"]


def test_v161_repository_failure_wrapped_with_exact_outcome_and_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real V1.61 with a failing outcome repository: the ORIGINAL
    repository error is chained, the outcome is the real V1.60 one."""

    events: list[Any] = []

    real_v160 = durable_coord.coordinate_task_execution_lifecycle

    def _counting_v160_real(*args: Any, **kwargs: Any) -> TaskExecutionLifecycleOutcome:
        events.append(("v160.call",))
        produced = real_v160(*args, **kwargs)
        events.append(("v160.success", produced))
        return produced

    monkeypatch.setattr(
        durable_coord,
        "coordinate_task_execution_lifecycle",
        _counting_v160_real,
    )

    positions, keyword_only = _caller_args()
    original = _PersistenceError("sqlite-style add failure")
    keyword_only["outcome_repository"] = _ExplodingRecordingRepository(original)

    with pytest.raises(TaskExecutionLifecycleOutcomeHistoryPersistenceError) as excinfo:
        coordinate_task_execution_lifecycle_durably(*positions, **keyword_only)

    error = excinfo.value
    assert error.cause is original
    assert error.__cause__ is original
    # Carries the EXACT genuine V1.60 outcome returned successfully.
    assert error.outcome is events[1][1]
    # Exactly one V1.60 run; zero retry of the failing add.
    assert [event[0] for event in events if event[0] != "v160.success"] == [
        "v160.call"
    ]
    assert keyword_only["outcome_repository"].add_calls == 1


class _PersistenceError(RuntimeError):
    """One original outcome-history repository failure (test double)."""


def test_persistence_error_is_not_the_cause_and_makes_no_recovery_claims() -> None:
    outcome = _genuine_v160_outcome()[1]
    original = RuntimeError("boom")
    error = TaskExecutionLifecycleOutcomeHistoryPersistenceError(outcome, original)

    assert isinstance(error, ValueError)
    assert not isinstance(original, type(error))
    lowered = str(error).lower()
    for claim in ("roll", "compensat", "retried", "recovered", "repaired"):
        assert claim not in lowered


# -- no retry / no compensation (source-level) -------------------------------


def test_module_contains_no_retry_rollback_or_compensation_logic() -> None:
    tree = ast.parse(inspect.getsource(durable_coord))
    call_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                call_names.add(node.func.attr)

    forbidden = {
        "rollback",
        "compensate",
        "compensation",
        "retry",
        "replay",
        "revoke",
        "supersede",
    }
    assert forbidden.isdisjoint(call_names)

    # The composed public boundaries are the only lifecycle calls present.
    assert {
        "coordinate_task_execution_lifecycle",
        "record_task_execution_lifecycle_outcome_durably",
    }.issubset(call_names)


# -- architecture / source guards ---------------------------------------------


def _imported_modules(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_module_has_no_persistence_provider_or_runtime_imports() -> None:
    tree = ast.parse(inspect.getsource(durable_coord))
    imported = _imported_modules(tree)

    for module in imported:
        assert "sqlite" not in module
        assert "sqlalchemy" not in module.lower()
        assert not (
            module == "trajectory_os.adapters"
            or module.startswith("trajectory_os.adapters.")
        )
    forbidden_roots = {
        "subprocess",
        "sqlite3",
        "sqlalchemy",
        "ollama",
        "time",
        "shutil",
        "os",
        "sys",
    }
    assert forbidden_roots.isdisjoint(
        {module.split(".")[0] for module in imported}
    )


def test_module_does_not_generate_uuid_or_read_wall_clock() -> None:
    tree = ast.parse(inspect.getsource(durable_coord))
    forbidden_attrs = {"uuid1", "uuid3", "uuid4", "uuid5", "now", "utcnow"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs


def test_module_never_directly_calls_lower_boundaries() -> None:
    """V1.63 must compose ONLY V1.60 -> V1.61 and stop: no direct V1.51 /
    V1.52 / V1.53 / V1.55 / V1.58 calls, no transition/execution replay."""

    tree = ast.parse(inspect.getsource(durable_coord))
    call_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                call_names.add(node.func.attr)

    forbidden_calls = {
        "admit_current_task_execution_lifecycle",
        "apply_admitted_task_execution_lifecycle_durably",
        "record_task_execution_lifecycle_application_durably",
        "record_task_execution_lifecycle_admission_durably",
        "record_task_execution_lifecycle_decision_durably",
        "transition_entity_status",
        "transition_entity_status_durably",
        "execute_current_admitted_task",
        "record_task_execution_result_durably",
        "load",
        "save",
        "model_construct",
    }
    assert forbidden_calls.isdisjoint(call_names)
    # Only the two composed public boundaries are invoked.
    assert {
        "coordinate_task_execution_lifecycle",
        "record_task_execution_lifecycle_outcome_durably",
    }.issubset(call_names)


def test_module_exposes_no_current_or_supersession_apis() -> None:
    forbidden_tokens = {"latest", "effective", "supersede", "revoke", "idempot"}
    for name in vars(durable_coord):
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden_tokens)


def test_module_reexports_not_reimplemented() -> None:
    """The composed boundaries are the EXACT canonical public objects."""
    import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_coordination as v160  # noqa: E501
    import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_outcome_history as v161  # noqa: E501

    assert (
        durable_coord.coordinate_task_execution_lifecycle
        is v160.coordinate_task_execution_lifecycle
    )
    assert (
        durable_coord.record_task_execution_lifecycle_outcome_durably
        is v161.record_task_execution_lifecycle_outcome_durably
    )
    assert (
        durable_coord.TaskExecutionLifecycleOutcome is v160.TaskExecutionLifecycleOutcome
    )
    assert (
        durable_coord.TaskExecutionLifecycleOutcomeRecord
        is v161.TaskExecutionLifecycleOutcomeRecord
    )


def test_public_package_exports_are_correct() -> None:
    package_all = application.__all__
    assert "coordinate_task_execution_lifecycle_durably" in package_all
    assert "TaskExecutionLifecycleOutcomeHistoryPersistenceError" in package_all
    assert (
        application.coordinate_task_execution_lifecycle_durably
        is durable_coord.coordinate_task_execution_lifecycle_durably
    )
    assert (
        application.TaskExecutionLifecycleOutcomeHistoryPersistenceError
        is durable_coord.TaskExecutionLifecycleOutcomeHistoryPersistenceError
    )
    assert durable_coord.__all__ == [
        "TaskExecutionLifecycleOutcomeHistoryPersistenceError",
        "coordinate_task_execution_lifecycle_durably",
    ]


def test_public_signature_requires_no_invented_inputs() -> None:
    signature = inspect.signature(coordinate_task_execution_lifecycle_durably)
    expected = {
        "admission_record_id",
        "admission_recorded_at",
        "decision_record_id",
        "decision_recorded_at",
        "decision",
        "changed_at",
        "application_record_id",
        "application_recorded_at",
        "admission_repository",
        "decision_repository",
        "application_repository",
        "portfolio_repository",
        "outcome_record_id",
        "outcome_recorded_at",
        "outcome_repository",
    }
    assert set(signature.parameters) == expected


def test_module_annotations_preserve_public_return_type() -> None:
    import typing

    hints = typing.get_type_hints(coordinate_task_execution_lifecycle_durably)
    assert hints["return"] is TaskExecutionLifecycleOutcomeRecord
