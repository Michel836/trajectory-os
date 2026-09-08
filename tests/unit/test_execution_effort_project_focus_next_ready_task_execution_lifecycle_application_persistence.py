"""V1.53 — durable append-only history for completed lifecycle applications.

Focused unit tests plus executable (AST) architecture guards for
``execution_effort_project_focus_next_ready_task_execution_lifecycle_application_persistence``.

The module must perform EXACTLY: genuine UUID ``application_record_id`` ->
genuine aware ``recorded_at`` -> genuine
``EntityStatusTransitionResult`` -> fresh COMPLETE strict revalidation of
the full result -> validation of ONLY the provable V1.52 completion
invariants against the retained fresh copy -> immutable record
construction -> ``repository.add(record)`` exactly once -> return the
exact record -> stop. No V1.52 application call, no transition calls, no
Portfolio load/save, no provider / runtime / agent / subprocess / shell,
no UUID generation, no wall clock, no DB schema, no retry / idempotency /
exactly-once / latest / effective / transaction claims, no mutation or
cascade, no dedupe or supersession.
"""

from __future__ import annotations

import ast
import inspect
import itertools
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_application_persistence as application_persistence_module  # noqa: E501
from trajectory_os.application import (
    DurableTaskExecutionLifecycleApplicationRecordError,
    TaskExecutionLifecycleApplicationRecord,
    record_task_execution_lifecycle_application_durably,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    TrajectoryEntity,
)
from trajectory_os.domain.entity_status_transition import (
    EntityStatusTransitionResult,
    transition_entity_status,
)
from trajectory_os.domain.portfolio import Portfolio

RECORDED_AT = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
BASE_TS = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
CHANGED_AT = BASE_TS + timedelta(days=1)

_UUID_SEQUENCE = itertools.count(1)


def _uuid() -> UUID:
    """Deterministic sequential test identity (test-only helper)."""
    return UUID(int=next(_UUID_SEQUENCE))


def _project_entity(entity_id: UUID | None = None) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id or _uuid(),
        entity_type=EntityType.PROJECT,
        title="project",
        created_at=BASE_TS,
        updated_at=BASE_TS,
    )


def _task_entity(
    entity_id: UUID | None = None,
    status: EntityStatus = EntityStatus.ACTIVE,
    updated_at: datetime = BASE_TS,
) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id or _uuid(),
        entity_type=EntityType.TASK,
        title="task",
        status=status,
        created_at=BASE_TS,
        updated_at=updated_at,
    )


def _completed_result() -> EntityStatusTransitionResult:
    """One genuine V1.52-shaped result: exact TASK transitioned to COMPLETED."""

    task = _task_entity()
    portfolio = Portfolio(
        id=_uuid(),
        name="unit",
        entities=[_project_entity(), task],
    )
    return transition_entity_status(
        portfolio,
        task.id,
        EntityStatus.COMPLETED,
        CHANGED_AT,
    )


class RecordingRepository:
    def __init__(self) -> None:
        self.added: list[TaskExecutionLifecycleApplicationRecord] = []

    def add(self, record: TaskExecutionLifecycleApplicationRecord) -> None:
        self.added.append(record)

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleApplicationRecord, ...]:
        return tuple(
            record
            for record in self.added
            if record.result.portfolio.id == portfolio_id
        )


# -- record model -----------------------------------------------------------


def test_record_exact_shape_and_config() -> None:
    assert tuple(TaskExecutionLifecycleApplicationRecord.model_fields) == (
        "application_record_id",
        "recorded_at",
        "result",
    )
    assert TaskExecutionLifecycleApplicationRecord.model_config["strict"] is True
    assert TaskExecutionLifecycleApplicationRecord.model_config["frozen"] is True
    assert TaskExecutionLifecycleApplicationRecord.model_config["extra"] == "forbid"


def test_record_rejects_naive_recorded_at() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleApplicationRecord(
            application_record_id=uuid4(),
            recorded_at=datetime(2026, 9, 8, 8, 0),
            result=_completed_result(),
        )


def test_record_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleApplicationRecord(
            application_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            result=_completed_result(),
            superseded_by=True,  # type: ignore[call-arg]
        )


def test_record_is_frozen() -> None:
    record = TaskExecutionLifecycleApplicationRecord(
        application_record_id=uuid4(),
        recorded_at=RECORDED_AT,
        result=_completed_result(),
    )

    with pytest.raises(ValidationError):
        record.application_record_id = uuid4()  # type: ignore[misc]


# -- record construction enforces the V1.53 semantic invariants -------------


def test_record_direct_construction_rejects_genuine_non_completed_result() -> None:
    """A genuine CANCELLED transition result cannot exist inside the PUBLIC
    record, even without going through the durable boundary."""

    task = _task_entity()
    portfolio = Portfolio(
        id=_uuid(), name="unit", entities=[_project_entity(), task]
    )
    genuine = transition_entity_status(
        portfolio, task.id, EntityStatus.CANCELLED, CHANGED_AT
    )

    with pytest.raises(ValidationError, match="new_status"):
        TaskExecutionLifecycleApplicationRecord(
            application_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            result=genuine,
        )


def test_record_direct_construction_rejects_absent_entity_id() -> None:
    valid = _completed_result()
    hostile = EntityStatusTransitionResult.model_construct(
        portfolio=valid.portfolio,
        entity_id=uuid4(),
        previous_status=EntityStatus.ACTIVE,
        new_status=EntityStatus.COMPLETED,
        changed_at=CHANGED_AT,
    )

    with pytest.raises(ValidationError, match="result.entity_id"):
        TaskExecutionLifecycleApplicationRecord(
            application_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            result=hostile,
        )


def test_record_direct_construction_rejects_non_task_target() -> None:
    project = _project_entity()
    portfolio = Portfolio(id=_uuid(), name="unit", entities=[project])
    hostile = EntityStatusTransitionResult.model_construct(
        portfolio=portfolio,
        entity_id=project.id,
        previous_status=EntityStatus.ACTIVE,
        new_status=EntityStatus.COMPLETED,
        changed_at=CHANGED_AT,
    )

    with pytest.raises(ValidationError, match="must be a TASK"):
        TaskExecutionLifecycleApplicationRecord(
            application_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            result=hostile,
        )


def test_record_direct_construction_rejects_task_not_completed() -> None:
    task = _task_entity(status=EntityStatus.ACTIVE, updated_at=CHANGED_AT)
    portfolio = Portfolio(
        id=_uuid(), name="unit", entities=[_project_entity(), task]
    )
    hostile = EntityStatusTransitionResult.model_construct(
        portfolio=portfolio,
        entity_id=task.id,
        previous_status=EntityStatus.ACTIVE,
        new_status=EntityStatus.COMPLETED,
        changed_at=CHANGED_AT,
    )

    with pytest.raises(ValidationError, match="must itself be COMPLETED"):
        TaskExecutionLifecycleApplicationRecord(
            application_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            result=hostile,
        )


def test_record_direct_construction_rejects_updated_at_mismatch() -> None:
    task = _task_entity(status=EntityStatus.COMPLETED, updated_at=BASE_TS)
    portfolio = Portfolio(
        id=_uuid(), name="unit", entities=[_project_entity(), task]
    )
    hostile = EntityStatusTransitionResult.model_construct(
        portfolio=portfolio,
        entity_id=task.id,
        previous_status=EntityStatus.ACTIVE,
        new_status=EntityStatus.COMPLETED,
        changed_at=CHANGED_AT,
    )

    with pytest.raises(ValidationError, match="updated_at must equal"):
        TaskExecutionLifecycleApplicationRecord(
            application_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            result=hostile,
        )


def test_record_direct_construction_succeeds_for_valid_result() -> None:
    result = _completed_result()

    record = TaskExecutionLifecycleApplicationRecord(
        application_record_id=uuid4(),
        recorded_at=RECORDED_AT,
        result=result,
    )

    assert record.result == result
    assert record.result.new_status is EntityStatus.COMPLETED
    entity = record.result.portfolio.get_entity(result.entity_id)
    assert entity is not None
    assert entity.entity_type is EntityType.TASK
    assert entity.status is EntityStatus.COMPLETED
    assert entity.updated_at == CHANGED_AT


# -- happy path -------------------------------------------------------------


def test_valid_boundary_adds_exactly_once_and_returns_exact_record() -> None:
    repository = RecordingRepository()
    record_id = uuid4()
    result = _completed_result()

    record = record_task_execution_lifecycle_application_durably(
        record_id,
        RECORDED_AT,
        result,
        repository=repository,
    )

    assert len(repository.added) == 1
    assert repository.added[0] is record
    assert record.application_record_id == record_id
    assert record.recorded_at == RECORDED_AT
    assert record.result == result


def test_recorded_at_preserves_original_offset() -> None:
    repository = RecordingRepository()
    offset = timezone(timedelta(hours=5, minutes=30))
    recorded_at = datetime(2026, 9, 8, 13, 30, tzinfo=offset)
    result = _completed_result()

    record = record_task_execution_lifecycle_application_durably(
        uuid4(),
        recorded_at,
        result,
        repository=repository,
    )

    assert record.recorded_at == recorded_at
    assert record.recorded_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_exact_fresh_transition_result_and_provenance_preserved() -> None:
    repository = RecordingRepository()
    result = _completed_result()

    record = record_task_execution_lifecycle_application_durably(
        uuid4(),
        RECORDED_AT,
        result,
        repository=repository,
    )

    assert record.result.portfolio.id == result.portfolio.id
    assert record.result.entity_id == result.entity_id
    assert record.result.previous_status is EntityStatus.ACTIVE
    assert record.result.new_status is EntityStatus.COMPLETED
    assert record.result.changed_at == CHANGED_AT
    entity = record.result.portfolio.get_entity(result.entity_id)
    assert entity is not None
    assert entity.entity_type is EntityType.TASK
    assert entity.status is EntityStatus.COMPLETED
    assert entity.updated_at == CHANGED_AT


def test_embedded_result_has_exactly_the_transition_result_fields() -> None:
    """No V1.50/V1.51/provenance fields are invented on the record or the
    embedded result."""

    result = _completed_result()
    record = record_task_execution_lifecycle_application_durably(
        uuid4(),
        RECORDED_AT,
        result,
        repository=RecordingRepository(),
    )

    assert tuple(record.model_fields) == (
        "application_record_id",
        "recorded_at",
        "result",
    )
    assert tuple(record.result.model_fields) == (
        "portfolio",
        "entity_id",
        "previous_status",
        "new_status",
        "changed_at",
    )


# -- strict input validation (all before repository interaction) ------------


@pytest.mark.parametrize("bad", [None, "id", 1, b"id", str(uuid4())])
def test_record_id_must_be_genuine_uuid(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(DurableTaskExecutionLifecycleApplicationRecordError, match="UUID"):
        record_task_execution_lifecycle_application_durably(
            bad,
            RECORDED_AT,
            _completed_result(),
            repository=repository,
        )

    assert repository.added == []


@pytest.mark.parametrize("bad", [None, "2026-09-08T08:00:00+00:00", 1])
def test_recorded_at_must_be_genuine_datetime(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationRecordError, match="datetime"
    ):
        record_task_execution_lifecycle_application_durably(
            uuid4(),
            bad,
            _completed_result(),
            repository=repository,
        )

    assert repository.added == []


def test_recorded_at_must_be_aware() -> None:
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationRecordError, match="timezone-aware"
    ):
        record_task_execution_lifecycle_application_durably(
            uuid4(),
            datetime(2026, 9, 8, 8, 0),
            _completed_result(),
            repository=repository,
        )

    assert repository.added == []


@pytest.mark.parametrize("bad", [None, {}, "result", object()])
def test_result_must_be_genuine_transition_result(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationRecordError,
        match="EntityStatusTransitionResult",
    ):
        record_task_execution_lifecycle_application_durably(
            uuid4(),
            RECORDED_AT,
            bad,
            repository=repository,
        )

    assert repository.added == []


# -- fresh complete strict revalidation --------------------------------------


def test_hostile_constructed_result_fails_strict_revalidation() -> None:
    valid = _completed_result()
    payload = valid.model_dump(mode="python")
    payload["new_status"] = "not-a-enum"
    hostile = EntityStatusTransitionResult.model_construct(**payload)
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationRecordError,
        match="strict re-validation",
    ):
        record_task_execution_lifecycle_application_durably(
            uuid4(),
            RECORDED_AT,
            hostile,
            repository=repository,
        )

    assert repository.added == []


def test_semantic_reads_after_revalidation_use_only_fresh_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _completed_result()
    original_changed_at = result.changed_at
    replacement_changed_at = datetime(2030, 1, 1, tzinfo=UTC)
    repository = RecordingRepository()
    original_model_dump = EntityStatusTransitionResult.model_dump

    def dump_then_corrupt(
        self: EntityStatusTransitionResult,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        payload = original_model_dump(self, *args, **kwargs)
        object.__setattr__(self, "changed_at", replacement_changed_at)
        return payload

    monkeypatch.setattr(EntityStatusTransitionResult, "model_dump", dump_then_corrupt)

    record = record_task_execution_lifecycle_application_durably(
        uuid4(),
        RECORDED_AT,
        result,
        repository=repository,
    )

    assert result.changed_at == replacement_changed_at
    assert record.result.changed_at == original_changed_at
    assert repository.added[0].result.changed_at == original_changed_at


# -- V1.52-compatible completion invariants only ----------------------------


def test_non_completed_new_status_rejected() -> None:
    task = _task_entity()
    portfolio = Portfolio(
        id=_uuid(), name="unit", entities=[_project_entity(), task]
    )
    genuine = transition_entity_status(
        portfolio, task.id, EntityStatus.CANCELLED, CHANGED_AT
    )
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationRecordError,
        match="new_status",
    ):
        record_task_execution_lifecycle_application_durably(
            uuid4(), RECORDED_AT, genuine, repository=repository
        )

    assert repository.added == []


def test_missing_entity_id_in_portfolio_rejected() -> None:
    valid = _completed_result()
    hostile = EntityStatusTransitionResult.model_construct(
        portfolio=valid.portfolio,
        entity_id=uuid4(),
        previous_status=EntityStatus.ACTIVE,
        new_status=EntityStatus.COMPLETED,
        changed_at=CHANGED_AT,
    )
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationRecordError,
        match="result.entity_id",
    ):
        record_task_execution_lifecycle_application_durably(
            uuid4(), RECORDED_AT, hostile, repository=repository
        )

    assert repository.added == []


def test_target_entity_must_be_task() -> None:
    project = _project_entity()
    portfolio = Portfolio(
        id=_uuid(),
        name="unit",
        entities=[project],
    )
    hostile = EntityStatusTransitionResult.model_construct(
        portfolio=portfolio,
        entity_id=project.id,
        previous_status=EntityStatus.ACTIVE,
        new_status=EntityStatus.COMPLETED,
        changed_at=CHANGED_AT,
    )
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationRecordError,
        match="must be a TASK",
    ):
        record_task_execution_lifecycle_application_durably(
            uuid4(), RECORDED_AT, hostile, repository=repository
        )

    assert repository.added == []


def test_target_task_must_itself_be_completed() -> None:
    task = _task_entity(status=EntityStatus.ACTIVE, updated_at=CHANGED_AT)
    portfolio = Portfolio(
        id=_uuid(), name="unit", entities=[_project_entity(), task]
    )
    hostile = EntityStatusTransitionResult.model_construct(
        portfolio=portfolio,
        entity_id=task.id,
        previous_status=EntityStatus.ACTIVE,
        new_status=EntityStatus.COMPLETED,
        changed_at=CHANGED_AT,
    )
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationRecordError,
        match="must itself be COMPLETED",
    ):
        record_task_execution_lifecycle_application_durably(
            uuid4(), RECORDED_AT, hostile, repository=repository
        )

    assert repository.added == []


def test_target_task_updated_at_must_equal_changed_at() -> None:
    task = _task_entity(status=EntityStatus.COMPLETED, updated_at=BASE_TS)
    portfolio = Portfolio(
        id=_uuid(), name="unit", entities=[_project_entity(), task]
    )
    hostile = EntityStatusTransitionResult.model_construct(
        portfolio=portfolio,
        entity_id=task.id,
        previous_status=EntityStatus.ACTIVE,
        new_status=EntityStatus.COMPLETED,
        changed_at=CHANGED_AT,
    )
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationRecordError,
        match="updated_at",
    ):
        record_task_execution_lifecycle_application_durably(
            uuid4(), RECORDED_AT, hostile, repository=repository
        )

    assert repository.added == []


def test_no_ordering_between_recorded_at_and_changed_at() -> None:
    """Only freshness, genuine typed values, and the V1.52 invariants are
    enforced — no ``recorded_at >= changed_at`` rule is invented (a
    recorded_at strictly before changed_at is accepted)."""

    repository = RecordingRepository()
    result = _completed_result()
    early_recorded_at = datetime(
        CHANGED_AT.year - 1, CHANGED_AT.month, CHANGED_AT.day, tzinfo=UTC
    )

    record = record_task_execution_lifecycle_application_durably(
        uuid4(),
        early_recorded_at,
        result,
        repository=repository,
    )

    assert record.recorded_at == early_recorded_at


# -- append-only semantics ---------------------------------------------------


def test_repository_add_called_exactly_once_with_exact_record() -> None:
    calls: list[TaskExecutionLifecycleApplicationRecord] = []

    class CountingRepository:
        def add(self, record: TaskExecutionLifecycleApplicationRecord) -> None:
            calls.append(record)

        def list_history(
            self, portfolio_id: UUID
        ) -> tuple[TaskExecutionLifecycleApplicationRecord, ...]:
            return tuple(calls)

    repository = CountingRepository()
    returned = record_task_execution_lifecycle_application_durably(
        uuid4(),
        RECORDED_AT,
        _completed_result(),
        repository=repository,
    )

    assert len(calls) == 1
    assert calls[0] is returned


def test_repository_exception_propagates_unchanged() -> None:
    marker = RuntimeError("storage failed")

    class ExplodingRepository(RecordingRepository):
        def add(self, record: TaskExecutionLifecycleApplicationRecord) -> None:
            raise marker

    with pytest.raises(RuntimeError) as exc_info:
        record_task_execution_lifecycle_application_durably(
            uuid4(),
            RECORDED_AT,
            _completed_result(),
            repository=ExplodingRepository(),
        )

    assert exc_info.value is marker


def test_repeated_valid_calls_append_repeatedly_without_dedupe() -> None:
    repository = RecordingRepository()
    result = _completed_result()

    first = record_task_execution_lifecycle_application_durably(
        uuid4(), RECORDED_AT, result, repository=repository
    )
    second = record_task_execution_lifecycle_application_durably(
        uuid4(), RECORDED_AT, result, repository=repository
    )
    same_identity = record_task_execution_lifecycle_application_durably(
        first.application_record_id,
        RECORDED_AT,
        result,
        repository=repository,
    )

    assert len(repository.added) == 3
    assert repository.added[0] is first
    assert repository.added[1] is second
    assert repository.added[2] is same_identity
    assert same_identity.application_record_id == first.application_record_id


# -- architecture guards -----------------------------------------------------


def test_public_module_has_no_provider_runtime_or_storage_imports() -> None:
    tree = ast.parse(inspect.getsource(application_persistence_module))
    forbidden_roots = {
        "subprocess",
        "sqlite3",
        "sqlalchemy",
        "ollama",
        "time",
    }
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert forbidden_roots.isdisjoint(imported)


def test_public_module_does_not_generate_uuid_or_read_wall_clock() -> None:
    tree = ast.parse(inspect.getsource(application_persistence_module))
    forbidden_attrs = {"uuid1", "uuid4", "now", "utcnow"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs


def test_public_module_never_replays_transitions_or_repository_io() -> None:
    tree = ast.parse(inspect.getsource(application_persistence_module))
    forbidden_calls = {
        "apply_admitted_task_execution_lifecycle_durably",
        "transition_entity_status",
        "transition_entity_status_durably",
        "admit_current_task_execution_lifecycle",
        "decide_task_execution_lifecycle",
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


def test_repository_protocol_exposes_only_add_and_list_history() -> None:
    repository_protocol = (
        application_persistence_module.TaskExecutionLifecycleApplicationRepository
    )
    methods = {
        name
        for name, value in repository_protocol.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert methods == {"add", "list_history"}
