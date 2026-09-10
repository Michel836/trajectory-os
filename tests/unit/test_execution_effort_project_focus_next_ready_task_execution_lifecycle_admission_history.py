"""V1.58 — durable append-only history for V1.51 lifecycle admissions.

Focused unit tests plus executable (AST) architecture guards for
``execution_effort_project_focus_next_ready_task_execution_lifecycle_admission_history``.

The module must perform EXACTLY: genuine UUID ``admission_record_id`` ->
genuine aware ``recorded_at`` -> genuine
``TaskExecutionLifecycleAdmission`` -> fresh COMPLETE strict revalidation
of the full admission -> immutable record construction ->
``repository.add(record)`` exactly once -> return the exact record ->
stop. No ``admit_current_task_execution_lifecycle`` replay, no V1.52/
V1.53/V1.54/V1.55/V1.56 boundary call, no transition calls, no Portfolio
load/save, no provider / runtime / agent / subprocess / shell, no UUID
generation, no wall clock, no DB schema, no retry / idempotency /
exactly-once / latest / effective / transaction claims, no mutation or
cascade, no dedupe or supersession, and no inference of any CURRENT task
state from the historical ``current_task_status`` evidence.
"""

from __future__ import annotations

import ast
import inspect
import itertools
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_admission_history as admission_history_module  # noqa: E501
from trajectory_os.application import (
    DurableTaskExecutionLifecycleAdmissionError,
    TaskExecutionLifecycleAdmission,
    TaskExecutionLifecycleAdmissionRecord,
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDisposition,
    TaskExecutionResult,
    TaskExecutionResultRecord,
    admit_current_task_execution_lifecycle,
    decide_task_execution_lifecycle,
    record_task_execution_lifecycle_admission_durably,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

RECORDED_AT = datetime(2026, 3, 4, 8, 0, tzinfo=UTC)
EXECUTED_AT = datetime(2026, 3, 1, 9, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
DECIDED_AT = datetime(2026, 3, 2, 10, 0, tzinfo=timezone(timedelta(minutes=45)))
BASE_TS = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)

_UUID_SEQUENCE = itertools.count(1)


def _uuid() -> UUID:
    """Deterministic sequential test identity (test-only helper)."""
    return UUID(int=next(_UUID_SEQUENCE))


def _v149_record(portfolio_id: UUID, project_id: UUID, task_id: UUID) -> TaskExecutionResultRecord:
    result = TaskExecutionResult(
        request_id=_uuid(),
        intent_id=_uuid(),
        decision_id=_uuid(),
        portfolio_id=portfolio_id,
        authorized_project_id=project_id,
        authorized_task_id=task_id,
        succeeded=True,
    )
    return TaskExecutionResultRecord(
        execution_record_id=_uuid(),
        recorded_at=EXECUTED_AT,
        result=result,
    )


def _admission(
    task_status: EntityStatus = EntityStatus.ACTIVE,
) -> tuple[TaskExecutionLifecycleAdmission, TaskExecutionLifecycleDecision]:
    """Build one genuine V1.50 decision and then one genuine V1.51
    admission for it using the V1.51 boundary itself (the V1.58 module
    under test never re-creates it)."""

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
        status=task_status,
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
    decision = decide_task_execution_lifecycle(
        _uuid(),
        DECIDED_AT,
        _v149_record(portfolio.id, project.id, task.id),
        TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )
    admission = admit_current_task_execution_lifecycle(decision, portfolio)

    return admission, decision


class RecordingRepository:
    def __init__(self) -> None:
        self.added: list[TaskExecutionLifecycleAdmissionRecord] = []

    def add(self, record: TaskExecutionLifecycleAdmissionRecord) -> None:
        self.added.append(record)

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleAdmissionRecord, ...]:
        return tuple(
            record
            for record in self.added
            if record.admission.portfolio_id == portfolio_id
        )


# -- record model -----------------------------------------------------------


def test_record_exact_shape_and_config() -> None:
    assert tuple(TaskExecutionLifecycleAdmissionRecord.model_fields) == (
        "admission_record_id",
        "recorded_at",
        "admission",
    )
    assert TaskExecutionLifecycleAdmissionRecord.model_config["strict"] is True
    assert TaskExecutionLifecycleAdmissionRecord.model_config["frozen"] is True
    assert TaskExecutionLifecycleAdmissionRecord.model_config["extra"] == "forbid"


def test_record_rejects_string_admission_record_id() -> None:
    admission, _ = _admission()

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmissionRecord(
            admission_record_id=str(uuid4()),
            recorded_at=RECORDED_AT,
            admission=admission,
        )


def test_record_rejects_naive_recorded_at() -> None:
    admission, _ = _admission()

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmissionRecord(
            admission_record_id=uuid4(),
            recorded_at=datetime(2026, 9, 8, 8, 0),
            admission=admission,
        )


def test_record_rejects_foreign_admission_type() -> None:
    admission, decision = _admission()
    # A genuine V1.50 decision is NOT a V1.51 admission.
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmissionRecord(
            admission_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            admission=decision,  # type: ignore[arg-type]
        )


def test_record_rejects_extra_fields() -> None:
    admission, _ = _admission()

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmissionRecord(
            admission_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            admission=admission,
            effective=True,  # type: ignore[call-arg]
        )


def test_record_is_frozen_and_embedded_admission_remains_immutable() -> None:
    admission, _ = _admission()

    record = TaskExecutionLifecycleAdmissionRecord(
        admission_record_id=uuid4(),
        recorded_at=RECORDED_AT,
        admission=admission,
    )

    with pytest.raises(ValidationError):
        record.admission_record_id = uuid4()  # type: ignore[misc]

    with pytest.raises(ValidationError):
        record.admission = TaskExecutionLifecycleAdmission(  # type: ignore[misc]
            **{**admission.model_dump(mode="python")}
        )

    with pytest.raises(ValidationError):
        record.admission.current_task_status = EntityStatus.ARCHIVED  # type: ignore[misc]


def test_record_preserves_exact_offsets_and_status_evidence() -> None:
    admission, _ = _admission(EntityStatus.SOMEDAY)

    record = TaskExecutionLifecycleAdmissionRecord(
        admission_record_id=uuid4(),
        recorded_at=RECORDED_AT,
        admission=admission,
    )

    assert record.admission.decided_at == DECIDED_AT
    assert record.admission.decided_at.utcoffset() == timedelta(minutes=45)
    assert record.admission.execution_recorded_at == EXECUTED_AT
    assert (
        record.admission.execution_recorded_at.utcoffset()
        == timedelta(hours=5, minutes=30)
    )
    assert record.recorded_at.utcoffset() == UTC.utcoffset(None)
    assert record.admission.current_task_status is EntityStatus.SOMEDAY


def test_record_direct_construction_rejects_hostile_constructed_admission() -> None:
    """An admission whose historical status evidence is already-COMPLETED
    — even when it bypasses validation via ``model_construct`` — can
    never exist inside the PUBLIC record."""

    admission, _ = _admission()
    payload: dict[str, object] = admission.model_dump(mode="python")
    payload["current_task_status"] = EntityStatus.COMPLETED
    hostile = TaskExecutionLifecycleAdmission.model_construct(**payload)

    with pytest.raises(ValidationError, match="already-COMPLETED"):
        TaskExecutionLifecycleAdmissionRecord(
            admission_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            admission=hostile,
        )


def test_record_direct_construction_rejects_failed_execution_admission() -> None:
    admission, _ = _admission()
    payload: dict[str, object] = admission.model_dump(mode="python")
    payload["execution_succeeded"] = False
    hostile = TaskExecutionLifecycleAdmission.model_construct(**payload)

    with pytest.raises(ValidationError, match="execution_succeeded"):
        TaskExecutionLifecycleAdmissionRecord(
            admission_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            admission=hostile,
        )


def test_record_direct_construction_rejects_naive_inner_timestamps() -> None:
    admission, _ = _admission()
    payload: dict[str, object] = admission.model_dump(mode="python")
    payload["decided_at"] = datetime(2026, 9, 8, 8, 0)
    hostile = TaskExecutionLifecycleAdmission.model_construct(**payload)

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmissionRecord(
            admission_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            admission=hostile,
        )


def test_record_direct_construction_rejects_invalid_disposition_literal() -> None:
    admission, _ = _admission()
    payload: dict[str, object] = admission.model_dump(mode="python")
    payload["disposition"] = "not-a-disposition"
    hostile = TaskExecutionLifecycleAdmission.model_construct(**payload)

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmissionRecord(
            admission_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            admission=hostile,
        )


# -- happy path -------------------------------------------------------------


def test_valid_v151_admission_accepted() -> None:
    repository = RecordingRepository()
    admission, decision = _admission()

    record = record_task_execution_lifecycle_admission_durably(
        uuid4(),
        RECORDED_AT,
        admission,
        repository=repository,
    )

    assert len(repository.added) == 1
    assert repository.added[0] is record
    assert record.admission == admission
    assert record.admission.lifecycle_decision_id == decision.lifecycle_decision_id


def test_all_v151_fields_preserved_exactly() -> None:
    repository = RecordingRepository()
    admission, decision = _admission()

    record = record_task_execution_lifecycle_admission_durably(
        uuid4(),
        RECORDED_AT,
        admission,
        repository=repository,
    )

    assert record.admission.lifecycle_decision_id == decision.lifecycle_decision_id
    assert record.admission.decided_at == decision.decided_at
    assert (
        record.admission.decided_at.utcoffset() == decision.decided_at.utcoffset()
    )
    assert record.admission.execution_record_id == decision.execution_record_id
    assert (
        record.admission.execution_recorded_at
        == decision.execution_recorded_at
    )
    assert (
        record.admission.execution_recorded_at.utcoffset()
        == decision.execution_recorded_at.utcoffset()
    )
    assert record.admission.request_id == decision.request_id
    assert record.admission.intent_id == decision.intent_id
    assert (
        record.admission.execution_decision_id == decision.execution_decision_id
    )
    assert record.admission.portfolio_id == decision.portfolio_id
    assert (
        record.admission.authorized_project_id
        == decision.authorized_project_id
    )
    assert record.admission.authorized_task_id == decision.authorized_task_id
    assert record.admission.execution_succeeded is decision.execution_succeeded
    assert record.admission.disposition is decision.disposition
    assert record.admission.current_task_status is admission.current_task_status


def test_embedded_admission_has_exactly_the_v151_fields() -> None:
    """No new fields are invented on the record or the embedded
    admission."""

    repository = RecordingRepository()
    record = record_task_execution_lifecycle_admission_durably(
        uuid4(),
        RECORDED_AT,
        _admission()[0],
        repository=repository,
    )

    assert tuple(record.model_fields) == (
        "admission_record_id",
        "recorded_at",
        "admission",
    )
    assert tuple(record.admission.model_fields) == (
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
        "current_task_status",
    )


def test_recorded_at_preserves_original_offset() -> None:
    repository = RecordingRepository()
    offset = timezone(timedelta(hours=5, minutes=30))
    recorded_at = datetime(2026, 9, 8, 13, 30, tzinfo=offset)

    record = record_task_execution_lifecycle_admission_durably(
        uuid4(),
        recorded_at,
        _admission()[0],
        repository=repository,
    )

    assert record.recorded_at == recorded_at
    assert record.recorded_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_list_history_returns_exact_appended_history() -> None:
    repository = RecordingRepository()
    first_admission, _ = _admission()
    other_admission, _ = _admission()

    first = record_task_execution_lifecycle_admission_durably(
        uuid4(), RECORDED_AT, first_admission, repository=repository
    )
    record_task_execution_lifecycle_admission_durably(
        uuid4(), RECORDED_AT, other_admission, repository=repository
    )

    portfolio_id = first_admission.portfolio_id

    history = repository.list_history(portfolio_id)

    assert len(history) == 1
    assert history[0] is first
    assert repository.list_history(other_admission.portfolio_id) == (
        repository.added[-1],
    )


# -- strict input validation (all before repository interaction) ------------


@pytest.mark.parametrize("bad", [None, "id", 1, b"id", str(uuid4())])
def test_admission_record_id_must_be_genuine_uuid(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(DurableTaskExecutionLifecycleAdmissionError, match="UUID"):
        record_task_execution_lifecycle_admission_durably(
            bad,
            RECORDED_AT,
            _admission()[0],
            repository=repository,
        )

    assert repository.added == []


@pytest.mark.parametrize("bad", [None, "2026-09-08T08:00:00+00:00", 1])
def test_recorded_at_must_be_genuine_datetime(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleAdmissionError, match="datetime"
    ):
        record_task_execution_lifecycle_admission_durably(
            uuid4(),
            bad,
            _admission()[0],
            repository=repository,
        )

    assert repository.added == []


def test_recorded_at_must_be_aware() -> None:
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleAdmissionError, match="timezone-aware"
    ):
        record_task_execution_lifecycle_admission_durably(
            uuid4(),
            datetime(2026, 9, 8, 8, 0),
            _admission()[0],
            repository=repository,
        )

    assert repository.added == []


def test_admission_must_be_genuine_v151_admission() -> None:
    repository = RecordingRepository()
    admission, decision = _admission()
    # A genuine V1.50 decision, dicts, strings, and foreign values are
    # NOT a V1.51 admission.
    foreign = [
        None,
        {},
        "admission",
        decision,
        TaskExecutionResultRecord(
            execution_record_id=_uuid(),
            recorded_at=RECORDED_AT,
            result=TaskExecutionResult(
                request_id=_uuid(),
                intent_id=_uuid(),
                decision_id=_uuid(),
                portfolio_id=admission.portfolio_id,
                authorized_project_id=admission.authorized_project_id,
                authorized_task_id=admission.authorized_task_id,
                succeeded=True,
            ),
        ),
        object(),
    ]

    for bad in foreign:
        with pytest.raises(
            DurableTaskExecutionLifecycleAdmissionError,
            match="TaskExecutionLifecycleAdmission",
        ):
            record_task_execution_lifecycle_admission_durably(
                uuid4(),
                RECORDED_AT,
                bad,  # type: ignore[arg-type]
                repository=repository,
            )

    assert repository.added == []


# -- fresh complete strict revalidation -------------------------------------


def test_hostile_constructed_admission_fails_strict_revalidation() -> None:
    """A hostile ``model_construct`` payload carrying invalid V1.51
    semantic state is rejected before any repository interaction, and
    only the retained fresh copy is used semantically."""

    admission, _ = _admission()
    payload: dict[str, object] = admission.model_dump(mode="python")
    payload["current_task_status"] = EntityStatus.COMPLETED
    hostile = TaskExecutionLifecycleAdmission.model_construct(**payload)
    repository = RecordingRepository()

    with pytest.raises(
        DurableTaskExecutionLifecycleAdmissionError,
        match="strict re-validation",
    ):
        record_task_execution_lifecycle_admission_durably(
            uuid4(),
            RECORDED_AT,
            hostile,
            repository=repository,
        )

    assert repository.added == []


def test_semantic_reads_after_revalidation_use_only_fresh_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission, _ = _admission()
    original_lifecycle_decision_id = admission.lifecycle_decision_id
    replacement_lifecycle_decision_id = uuid4()
    repository = RecordingRepository()
    original_model_dump = TaskExecutionLifecycleAdmission.model_dump

    def dump_then_corrupt(
        self: TaskExecutionLifecycleAdmission,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        payload = original_model_dump(self, *args, **kwargs)
        if self is admission:
            object.__setattr__(
                self, "lifecycle_decision_id", replacement_lifecycle_decision_id
            )
        return payload

    monkeypatch.setattr(
        TaskExecutionLifecycleAdmission, "model_dump", dump_then_corrupt
    )

    record = record_task_execution_lifecycle_admission_durably(
        uuid4(),
        RECORDED_AT,
        admission,
        repository=repository,
    )

    assert (
        admission.lifecycle_decision_id == replacement_lifecycle_decision_id
    )
    assert (
        record.admission.lifecycle_decision_id == original_lifecycle_decision_id
    )
    assert (
        repository.added[0].admission.lifecycle_decision_id
        == original_lifecycle_decision_id
    )


# -- append-only semantics ---------------------------------------------------


def test_repository_add_called_exactly_once_with_exact_record() -> None:
    calls: list[TaskExecutionLifecycleAdmissionRecord] = []

    class CountingRepository:
        def add(self, record: TaskExecutionLifecycleAdmissionRecord) -> None:
            calls.append(record)

        def list_history(
            self, portfolio_id: UUID
        ) -> tuple[TaskExecutionLifecycleAdmissionRecord, ...]:
            return tuple(calls)

    repository = CountingRepository()
    returned = record_task_execution_lifecycle_admission_durably(
        uuid4(),
        RECORDED_AT,
        _admission()[0],
        repository=repository,
    )

    assert len(calls) == 1
    assert calls[0] is returned


def test_repository_exception_propagates_unchanged() -> None:
    marker = RuntimeError("storage failed")

    class ExplodingRepository(RecordingRepository):
        def add(self, record: TaskExecutionLifecycleAdmissionRecord) -> None:
            raise marker

    with pytest.raises(RuntimeError) as exc_info:
        record_task_execution_lifecycle_admission_durably(
            uuid4(),
            RECORDED_AT,
            _admission()[0],
            repository=ExplodingRepository(),
        )

    assert exc_info.value is marker


def test_same_semantic_admission_appended_under_distinct_record_ids() -> None:
    repository = RecordingRepository()
    admission, _ = _admission()

    first = record_task_execution_lifecycle_admission_durably(
        uuid4(), RECORDED_AT, admission, repository=repository
    )
    second = record_task_execution_lifecycle_admission_durably(
        uuid4(), RECORDED_AT, admission, repository=repository
    )
    same_identity = record_task_execution_lifecycle_admission_durably(
        first.admission_record_id, RECORDED_AT, admission, repository=repository
    )

    assert len(repository.added) == 3
    assert repository.added[0] is first
    assert repository.added[1] is second
    assert repository.added[2] is same_identity
    assert repository.added[0].admission == repository.added[1].admission
    assert (
        repository.added[0].admission_record_id
        == repository.added[2].admission_record_id
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
    tree = ast.parse(inspect.getsource(admission_history_module))
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
    tree = ast.parse(inspect.getsource(admission_history_module))
    forbidden_attrs = {"uuid1", "uuid4", "now", "utcnow"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs


def test_public_module_never_replays_v151_or_calls_lifecycle_boundaries() -> None:
    tree = ast.parse(inspect.getsource(admission_history_module))
    forbidden_calls = {
        "decide_task_execution_lifecycle",
        "admit_current_task_execution_lifecycle",
        "apply_admitted_task_execution_lifecycle_durably",
        "coordinate_task_execution_lifecycle",
        "record_task_execution_lifecycle_application_durably",
        "record_task_execution_lifecycle_decision_durably",
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
    assert "PortfolioRepository" not in vars(admission_history_module)


def test_public_module_exposes_no_current_or_supersession_apis() -> None:
    forbidden_tokens = {"latest", "effective", "supersede", "revoke", "idempot"}
    for name in vars(admission_history_module):
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden_tokens)


def test_repository_protocol_exposes_only_add_and_list_history() -> None:
    repository_protocol = (
        admission_history_module.TaskExecutionLifecycleAdmissionRepository
    )
    methods = {
        name
        for name, value in repository_protocol.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert methods == {"add", "list_history"}
