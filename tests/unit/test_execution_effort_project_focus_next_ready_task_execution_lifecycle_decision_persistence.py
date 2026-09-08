"""V1.55 — durable append-only history for explicit human lifecycle
decisions.

Focused unit tests plus executable (AST) architecture guards for
``execution_effort_project_focus_next_ready_task_execution_lifecycle_decision_persistence``.

The module must perform EXACTLY: genuine UUID ``decision_record_id`` ->
genuine aware ``recorded_at`` -> genuine
``TaskExecutionLifecycleDecision`` -> fresh COMPLETE strict revalidation
of the full decision -> immutable record construction ->
``repository.add(record)`` exactly once -> return the exact record ->
stop. No ``decide_task_execution_lifecycle`` call, no V1.51/V1.52/V1.53/
V1.54 boundary call, no transition calls, no Portfolio load/save, no
provider / runtime / agent / subprocess / shell, no UUID generation, no
wall clock, no DB schema, no retry / idempotency / exactly-once / latest /
effective / transaction claims, no mutation or cascade, no dedupe or
supersession.
"""

from __future__ import annotations

import ast
import inspect
import itertools
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_decision_persistence as decision_persistence_module  # noqa: E501
from trajectory_os.application import (
    DurableTaskExecutionLifecycleDecisionError,
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDecisionRecord,
    TaskExecutionLifecycleDisposition,
    TaskExecutionResultRecord,
    decide_task_execution_lifecycle,
    record_task_execution_lifecycle_decision_durably,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution import (  # noqa: E501
    TaskExecutionResult,
)

RECORDED_AT = datetime(2026, 3, 4, 8, 0, tzinfo=UTC)
EXECUTED_AT = datetime(2026, 3, 1, 9, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
DECIDED_AT = datetime(2026, 3, 2, 10, 0, tzinfo=timezone(timedelta(minutes=45)))

_UUID_SEQUENCE = itertools.count(1)


def _uuid() -> UUID:
    """Deterministic sequential test identity (test-only helper)."""
    return UUID(int=next(_UUID_SEQUENCE))


def _v149_record() -> TaskExecutionResultRecord:
    result = TaskExecutionResult(
        request_id=_uuid(),
        intent_id=_uuid(),
        decision_id=_uuid(),
        portfolio_id=_uuid(),
        authorized_project_id=_uuid(),
        authorized_task_id=_uuid(),
        succeeded=True,
    )
    return TaskExecutionResultRecord(
        execution_record_id=_uuid(),
        recorded_at=EXECUTED_AT,
        result=result,
    )


def _decision(
    disposition: TaskExecutionLifecycleDisposition = (
        TaskExecutionLifecycleDisposition.COMPLETE_TASK
    ),
) -> TaskExecutionLifecycleDecision:
    """One genuine V1.50 decision produced by the V1.50 boundary itself."""
    return decide_task_execution_lifecycle(
        _uuid(),
        DECIDED_AT,
        _v149_record(),
        disposition,
    )


class RecordingRepository:
    def __init__(self) -> None:
        self.added: list[TaskExecutionLifecycleDecisionRecord] = []

    def add(self, record: TaskExecutionLifecycleDecisionRecord) -> None:
        self.added.append(record)

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleDecisionRecord, ...]:
        return tuple(
            record
            for record in self.added
            if record.decision.portfolio_id == portfolio_id
        )


# -- record model -----------------------------------------------------------


def test_record_exact_shape_and_config() -> None:
    assert tuple(TaskExecutionLifecycleDecisionRecord.model_fields) == (
        "decision_record_id",
        "recorded_at",
        "decision",
    )
    assert TaskExecutionLifecycleDecisionRecord.model_config["strict"] is True
    assert TaskExecutionLifecycleDecisionRecord.model_config["frozen"] is True
    assert TaskExecutionLifecycleDecisionRecord.model_config["extra"] == "forbid"


def test_record_rejects_string_decision_record_id() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecisionRecord(
            decision_record_id=str(uuid4()),
            recorded_at=RECORDED_AT,
            decision=_decision(),
        )


def test_record_rejects_naive_recorded_at() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecisionRecord(
            decision_record_id=uuid4(),
            recorded_at=datetime(2026, 9, 8, 8, 0),
            decision=_decision(),
        )


def test_record_rejects_foreign_decision_type() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecisionRecord(
            decision_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            decision=_v149_record(),  # type: ignore[arg-type]
        )


def test_record_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecisionRecord(
            decision_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            decision=_decision(),
            superseded_by=True,  # type: ignore[call-arg]
        )


def test_record_is_frozen() -> None:
    record = TaskExecutionLifecycleDecisionRecord(
        decision_record_id=uuid4(),
        recorded_at=RECORDED_AT,
        decision=_decision(),
    )

    with pytest.raises(ValidationError):
        record.decision_record_id = uuid4()  # type: ignore[misc]


def test_record_preserves_exact_decision_offsets() -> None:
    decision = _decision()

    record = TaskExecutionLifecycleDecisionRecord(
        decision_record_id=uuid4(),
        recorded_at=RECORDED_AT,
        decision=decision,
    )

    assert record.decision.decided_at == DECIDED_AT
    assert (
        record.decision.decided_at.utcoffset() == timedelta(minutes=45)
    )
    assert record.decision.execution_recorded_at == EXECUTED_AT
    assert (
        record.decision.execution_recorded_at.utcoffset()
        == timedelta(hours=5, minutes=30)
    )
    assert record.recorded_at.utcoffset() == UTC.utcoffset(None)


def test_record_direct_construction_rejects_hostile_constructed_decision() -> None:
    """A decision that bypassed validation (``model_construct`` with an
    invalid disposition literal) can never exist inside the PUBLIC record."""

    base = _decision()
    payload: dict[str, object] = base.model_dump(mode="python")
    payload["disposition"] = "not-a-disposition"
    hostile = TaskExecutionLifecycleDecision.model_construct(**payload)

    with pytest.raises(ValidationError, match="strict re-validation"):
        TaskExecutionLifecycleDecisionRecord(
            decision_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            decision=hostile,
        )


def test_record_direct_construction_rejects_invalid_naive_decision() -> None:
    """A decision whose internal timestamps are naive — even when it
    bypasses validation via ``model_construct`` — can never exist inside
    the PUBLIC record."""

    base = _decision()
    payload: dict[str, object] = base.model_dump(mode="python")
    payload["decided_at"] = datetime(2026, 9, 8, 8, 0)
    hostile = TaskExecutionLifecycleDecision.model_construct(**payload)

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecisionRecord(
            decision_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            decision=hostile,
        )


def test_record_direct_construction_rejects_complete_task_over_failed() -> None:
    """``COMPLETE_TASK`` over a failed execution — even when it bypasses
    validation via ``model_construct`` — can never exist inside the PUBLIC
    record."""

    base = _decision()
    payload: dict[str, object] = base.model_dump(mode="python")
    payload["execution_succeeded"] = False
    hostile = TaskExecutionLifecycleDecision.model_construct(**payload)

    with pytest.raises(ValidationError, match="COMPLETE_TASK"):
        TaskExecutionLifecycleDecisionRecord(
            decision_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            decision=hostile,
        )


# -- happy path -------------------------------------------------------------


def test_valid_complete_task_decision_accepted() -> None:
    repository = RecordingRepository()
    decision = _decision(TaskExecutionLifecycleDisposition.COMPLETE_TASK)

    record = record_task_execution_lifecycle_decision_durably(
        uuid4(),
        RECORDED_AT,
        decision,
        repository=repository,
    )

    assert len(repository.added) == 1
    assert repository.added[0] is record
    assert record.decision == decision
    assert record.decision.disposition is (
        TaskExecutionLifecycleDisposition.COMPLETE_TASK
    )


def test_valid_no_lifecycle_change_decision_accepted() -> None:
    repository = RecordingRepository()
    decision = _decision(
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE
    )

    record = record_task_execution_lifecycle_decision_durably(
        uuid4(),
        RECORDED_AT,
        decision,
        repository=repository,
    )

    assert len(repository.added) == 1
    assert record.decision.disposition is (
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE
    )


def test_all_v150_provenance_fields_preserved_exactly() -> None:
    repository = RecordingRepository()
    decision = _decision()

    record = record_task_execution_lifecycle_decision_durably(
        uuid4(),
        RECORDED_AT,
        decision,
        repository=repository,
    )

    assert record.decision.lifecycle_decision_id == decision.lifecycle_decision_id
    assert record.decision.decided_at == decision.decided_at
    assert (
        record.decision.decided_at.utcoffset() == decision.decided_at.utcoffset()
    )
    assert record.decision.execution_record_id == decision.execution_record_id
    assert (
        record.decision.execution_recorded_at
        == decision.execution_recorded_at
    )
    assert (
        record.decision.execution_recorded_at.utcoffset()
        == decision.execution_recorded_at.utcoffset()
    )
    assert record.decision.request_id == decision.request_id
    assert record.decision.intent_id == decision.intent_id
    assert record.decision.execution_decision_id == decision.execution_decision_id
    assert record.decision.portfolio_id == decision.portfolio_id
    assert (
        record.decision.authorized_project_id == decision.authorized_project_id
    )
    assert record.decision.authorized_task_id == decision.authorized_task_id
    assert record.decision.execution_succeeded is decision.execution_succeeded
    assert record.decision.disposition is decision.disposition


def test_recorded_at_preserves_original_offset() -> None:
    repository = RecordingRepository()
    offset = timezone(timedelta(hours=5, minutes=30))
    recorded_at = datetime(2026, 9, 8, 13, 30, tzinfo=offset)

    record = record_task_execution_lifecycle_decision_durably(
        uuid4(),
        recorded_at,
        _decision(),
        repository=repository,
    )

    assert record.recorded_at == recorded_at
    assert record.recorded_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_embedded_decision_has_exactly_the_v150_fields() -> None:
    """No new provenance fields are invented on the record or the embedded
    decision."""

    repository = RecordingRepository()
    record = record_task_execution_lifecycle_decision_durably(
        uuid4(),
        RECORDED_AT,
        _decision(),
        repository=repository,
    )

    assert tuple(record.model_fields) == (
        "decision_record_id",
        "recorded_at",
        "decision",
    )
    assert tuple(record.decision.model_fields) == (
        "lifecycle_decision_id",
        "decided_at",
        "execution_record_id",
        "execution_recorded_at",
        "request_id",
        "intent_id",
        "execution_decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
        "execution_succeeded",
        "disposition",
    )


def test_list_history_returns_exact_appended_history() -> None:
    repository = RecordingRepository()
    decision = _decision()
    portfolio_id = decision.portfolio_id
    other_portfolio = _decision()

    first = record_task_execution_lifecycle_decision_durably(
        uuid4(), RECORDED_AT, decision, repository=repository
    )
    record_task_execution_lifecycle_decision_durably(
        uuid4(), RECORDED_AT, other_portfolio, repository=repository
    )

    history = repository.list_history(portfolio_id)

    assert len(history) == 1
    assert history[0] is first
    assert repository.list_history(other_portfolio.portfolio_id) == (
        repository.added[-1],
    )


# -- strict input validation (all before repository interaction) ------------


@pytest.mark.parametrize("bad", [None, "id", 1, b"id", str(uuid4())])
def test_decision_record_id_must_be_genuine_uuid(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(DurableTaskExecutionLifecycleDecisionError, match="UUID"):
        record_task_execution_lifecycle_decision_durably(
            bad,
            RECORDED_AT,
            _decision(),
            repository=repository,
        )

    assert repository.added == []


@pytest.mark.parametrize("bad", [None, "2026-09-08T08:00:00+00:00", 1])
def test_recorded_at_must_be_genuine_datetime(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleDecisionError, match="datetime"
    ):
        record_task_execution_lifecycle_decision_durably(
            uuid4(),
            bad,
            _decision(),
            repository=repository,
        )

    assert repository.added == []


def test_recorded_at_must_be_aware() -> None:
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleDecisionError, match="timezone-aware"
    ):
        record_task_execution_lifecycle_decision_durably(
            uuid4(),
            datetime(2026, 9, 8, 8, 0),
            _decision(),
            repository=repository,
        )

    assert repository.added == []


@pytest.mark.parametrize("bad", [None, {}, "decision", _v149_record(), object()])
def test_decision_must_be_genuine_v150_decision(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleDecisionError,
        match="TaskExecutionLifecycleDecision",
    ):
        record_task_execution_lifecycle_decision_durably(
            uuid4(),
            RECORDED_AT,
            bad,
            repository=repository,
        )

    assert repository.added == []


# -- fresh complete strict revalidation -------------------------------------


def test_hostile_constructed_decision_fails_strict_revalidation() -> None:
    """A hostile ``model_construct`` payload that carries invalid V1.50
    semantic state is rejected before any repository interaction, and only
    the retained fresh copy is used semantically."""

    valid = _decision()
    payload: dict[str, object] = valid.model_dump(mode="python")
    payload["disposition"] = "not-a-disposition"
    hostile = TaskExecutionLifecycleDecision.model_construct(**payload)
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleDecisionError,
        match="strict re-validation",
    ):
        record_task_execution_lifecycle_decision_durably(
            uuid4(),
            RECORDED_AT,
            hostile,
            repository=repository,
        )

    assert repository.added == []


def test_semantic_reads_after_revalidation_use_only_fresh_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = _decision()
    original_lifecycle_decision_id = decision.lifecycle_decision_id
    replacement_lifecycle_decision_id = uuid4()
    repository = RecordingRepository()
    original_model_dump = TaskExecutionLifecycleDecision.model_dump

    def dump_then_corrupt(
        self: TaskExecutionLifecycleDecision,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        payload = original_model_dump(self, *args, **kwargs)
        if self is decision:
            object.__setattr__(self, "lifecycle_decision_id", replacement_lifecycle_decision_id)
        return payload

    monkeypatch.setattr(
        TaskExecutionLifecycleDecision, "model_dump", dump_then_corrupt
    )

    record = record_task_execution_lifecycle_decision_durably(
        uuid4(),
        RECORDED_AT,
        decision,
        repository=repository,
    )

    assert decision.lifecycle_decision_id == replacement_lifecycle_decision_id
    assert record.decision.lifecycle_decision_id == original_lifecycle_decision_id
    assert (
        repository.added[0].decision.lifecycle_decision_id
        == original_lifecycle_decision_id
    )


# -- append-only semantics ---------------------------------------------------


def test_repository_add_called_exactly_once_with_exact_record() -> None:
    calls: list[TaskExecutionLifecycleDecisionRecord] = []

    class CountingRepository:
        def add(self, record: TaskExecutionLifecycleDecisionRecord) -> None:
            calls.append(record)

        def list_history(
            self, portfolio_id: UUID
        ) -> tuple[TaskExecutionLifecycleDecisionRecord, ...]:
            return tuple(calls)

    repository = CountingRepository()
    returned = record_task_execution_lifecycle_decision_durably(
        uuid4(),
        RECORDED_AT,
        _decision(),
        repository=repository,
    )

    assert len(calls) == 1
    assert calls[0] is returned


def test_repository_exception_propagates_unchanged() -> None:
    marker = RuntimeError("storage failed")

    class ExplodingRepository(RecordingRepository):
        def add(self, record: TaskExecutionLifecycleDecisionRecord) -> None:
            raise marker

    with pytest.raises(RuntimeError) as exc_info:
        record_task_execution_lifecycle_decision_durably(
            uuid4(),
            RECORDED_AT,
            _decision(),
            repository=ExplodingRepository(),
        )

    assert exc_info.value is marker


def test_same_semantic_decision_appended_under_distinct_record_ids() -> None:
    repository = RecordingRepository()
    decision = _decision()

    first = record_task_execution_lifecycle_decision_durably(
        uuid4(), RECORDED_AT, decision, repository=repository
    )
    second = record_task_execution_lifecycle_decision_durably(
        uuid4(), RECORDED_AT, decision, repository=repository
    )
    same_identity = record_task_execution_lifecycle_decision_durably(
        first.decision_record_id, RECORDED_AT, decision, repository=repository
    )

    assert len(repository.added) == 3
    assert repository.added[0] is first
    assert repository.added[1] is second
    assert repository.added[2] is same_identity
    assert repository.added[0].decision == repository.added[1].decision
    assert (
        repository.added[0].decision_record_id
        == repository.added[2].decision_record_id
    )


# -- architecture guards -----------------------------------------------------


def _imported_modules(tree: ast.AST) -> set[str]:
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    return imported


def test_public_module_has_no_provider_runtime_or_storage_imports() -> None:
    tree = ast.parse(
        inspect.getsource(decision_persistence_module)
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
    imported = _imported_modules(tree)

    assert forbidden_roots.isdisjoint(
        {module.split(".")[0] for module in imported}
    )
    assert not any(
        module == "trajectory_os.adapters"
        or module.startswith("trajectory_os.adapters.")
        for module in imported
    )


def test_architecture_guard_detects_adapters_imports_in_hostile_source() -> None:
    hostile = ast.parse(
        "from trajectory_os.adapters import something\n"
        "import trajectory_os.adapters.persistence\n"
    )
    imported = _imported_modules(hostile)

    assert any(
        module == "trajectory_os.adapters"
        or module.startswith("trajectory_os.adapters.")
        for module in imported
    )


def test_public_module_does_not_generate_uuid_or_read_wall_clock() -> None:
    tree = ast.parse(
        inspect.getsource(decision_persistence_module)
    )
    forbidden_attrs = {"uuid1", "uuid4", "now", "utcnow"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs


def test_public_module_never_calls_upstream_boundaries_or_repository_io() -> None:
    tree = ast.parse(
        inspect.getsource(decision_persistence_module)
    )
    forbidden_calls = {
        "decide_task_execution_lifecycle",
        "admit_current_task_execution_lifecycle",
        "apply_admitted_task_execution_lifecycle_durably",
        "record_task_execution_lifecycle_application_durably",
        "record_task_execution_result_durably",
        "transition_entity_status",
        "transition_entity_status_durably",
        "execute_current_admitted_task",
        "load",
        "save",
    }
    called_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_names.add(node.func.attr)

    assert forbidden_calls.isdisjoint(called_names)
    assert "PortfolioRepository" not in vars(decision_persistence_module)


def test_public_module_exposes_no_current_or_supersession_apis() -> None:
    forbidden_tokens = {"latest", "current", "effective", "supersede", "revoke"}
    for name in vars(decision_persistence_module):
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden_tokens)


def test_repository_protocol_exposes_only_add_and_list_history() -> None:
    repository_protocol = (
        decision_persistence_module.TaskExecutionLifecycleDecisionRepository
    )
    methods = {
        name
        for name, value in repository_protocol.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert methods == {"add", "list_history"}
