from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_persistence as module  # noqa: E501
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution import (
    TaskExecutionResult,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_persistence import (  # noqa: E501
    DurableTaskExecutionResultError,
    TaskExecutionResultRecord,
    record_task_execution_result_durably,
)


def _result(*, succeeded: bool = True) -> TaskExecutionResult:
    return TaskExecutionResult(
        request_id=uuid4(),
        intent_id=uuid4(),
        decision_id=uuid4(),
        portfolio_id=uuid4(),
        authorized_project_id=uuid4(),
        authorized_task_id=uuid4(),
        succeeded=succeeded,
    )


class RecordingRepository:
    def __init__(self) -> None:
        self.added: list[TaskExecutionResultRecord] = []

    def add(self, record: TaskExecutionResultRecord) -> None:
        self.added.append(record)

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionResultRecord, ...]:
        return tuple(
            record
            for record in self.added
            if record.result.portfolio_id == portfolio_id
        )


def test_record_exact_shape_and_config() -> None:
    assert tuple(TaskExecutionResultRecord.model_fields) == (
        "execution_record_id",
        "recorded_at",
        "result",
    )
    assert TaskExecutionResultRecord.model_config["strict"] is True
    assert TaskExecutionResultRecord.model_config["frozen"] is True
    assert TaskExecutionResultRecord.model_config["extra"] == "forbid"


def test_record_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionResultRecord(
            execution_record_id=uuid4(),
            recorded_at=datetime(2026, 9, 8, 8, 0),
            result=_result(),
        )


def test_valid_boundary_adds_exactly_once_and_returns_exact_record() -> None:
    repository = RecordingRepository()
    record_id = uuid4()
    recorded_at = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
    result = _result()

    record = record_task_execution_result_durably(
        record_id,
        recorded_at,
        result,
        repository=repository,
    )

    assert len(repository.added) == 1
    assert repository.added[0] is record
    assert record.execution_record_id == record_id
    assert record.recorded_at == recorded_at
    assert record.result == result


@pytest.mark.parametrize("bad", [None, "id", 1, b"id"])
def test_record_id_must_be_genuine_uuid(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(DurableTaskExecutionResultError, match="UUID"):
        record_task_execution_result_durably(
            bad,
            datetime(2026, 9, 8, 8, 0, tzinfo=UTC),
            _result(),
            repository=repository,
        )

    assert repository.added == []


@pytest.mark.parametrize("bad", [None, "2026-09-08T08:00:00+00:00", 1])
def test_recorded_at_must_be_genuine_datetime(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(DurableTaskExecutionResultError, match="datetime"):
        record_task_execution_result_durably(
            uuid4(),
            bad,
            _result(),
            repository=repository,
        )

    assert repository.added == []


def test_recorded_at_must_be_aware() -> None:
    repository = RecordingRepository()

    with pytest.raises(DurableTaskExecutionResultError, match="timezone-aware"):
        record_task_execution_result_durably(
            uuid4(),
            datetime(2026, 9, 8, 8, 0),
            _result(),
            repository=repository,
        )

    assert repository.added == []


def test_recorded_at_preserves_original_offset() -> None:
    repository = RecordingRepository()
    offset = timezone(timedelta(hours=5, minutes=30))
    recorded_at = datetime(2026, 9, 8, 13, 30, tzinfo=offset)

    record = record_task_execution_result_durably(
        uuid4(),
        recorded_at,
        _result(),
        repository=repository,
    )

    assert record.recorded_at == recorded_at
    assert record.recorded_at.utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize("bad", [None, {}, "result", object()])
def test_result_must_be_genuine_v148_result(bad: object) -> None:
    repository = RecordingRepository()

    with pytest.raises(DurableTaskExecutionResultError, match="genuine V1.48"):
        record_task_execution_result_durably(
            uuid4(),
            datetime(2026, 9, 8, 8, 0, tzinfo=UTC),
            bad,
            repository=repository,
        )

    assert repository.added == []


def test_hostile_constructed_result_is_freshly_revalidated() -> None:
    valid = _result()
    payload = valid.model_dump(mode="python")
    payload["succeeded"] = 1
    hostile = TaskExecutionResult.model_construct(**payload)
    repository = RecordingRepository()

    with pytest.raises(DurableTaskExecutionResultError, match="strict re-validation"):
        record_task_execution_result_durably(
            uuid4(),
            datetime(2026, 9, 8, 8, 0, tzinfo=UTC),
            hostile,
            repository=repository,
        )

    assert repository.added == []


def test_semantic_reads_after_revalidation_use_only_fresh_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _result()
    original_portfolio_id = result.portfolio_id
    replacement_portfolio_id = uuid4()
    repository = RecordingRepository()
    original_model_dump = TaskExecutionResult.model_dump

    def dump_then_corrupt(
        self: TaskExecutionResult,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        payload = original_model_dump(self, *args, **kwargs)
        object.__setattr__(self, "portfolio_id", replacement_portfolio_id)
        return payload

    monkeypatch.setattr(TaskExecutionResult, "model_dump", dump_then_corrupt)

    record = record_task_execution_result_durably(
        uuid4(),
        datetime(2026, 9, 8, 8, 0, tzinfo=UTC),
        result,
        repository=repository,
    )

    assert result.portfolio_id == replacement_portfolio_id
    assert record.result.portfolio_id == original_portfolio_id
    assert repository.added[0].result.portfolio_id == original_portfolio_id


def test_repository_exception_propagates_unchanged() -> None:
    marker = RuntimeError("storage failed")

    class ExplodingRepository(RecordingRepository):
        def add(self, record: TaskExecutionResultRecord) -> None:
            raise marker

    with pytest.raises(RuntimeError) as exc_info:
        record_task_execution_result_durably(
            uuid4(),
            datetime(2026, 9, 8, 8, 0, tzinfo=UTC),
            _result(),
            repository=ExplodingRepository(),
        )

    assert exc_info.value is marker


def test_equivalent_results_with_distinct_record_ids_remain_distinct() -> None:
    repository = RecordingRepository()
    result = _result()
    recorded_at = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)

    first = record_task_execution_result_durably(
        uuid4(), recorded_at, result, repository=repository
    )
    second = record_task_execution_result_durably(
        uuid4(), recorded_at, result, repository=repository
    )

    assert first.result == second.result
    assert first.execution_record_id != second.execution_record_id
    assert len(repository.added) == 2


def test_record_is_frozen() -> None:
    record = TaskExecutionResultRecord(
        execution_record_id=uuid4(),
        recorded_at=datetime(2026, 9, 8, 8, 0, tzinfo=UTC),
        result=_result(),
    )

    with pytest.raises(ValidationError):
        record.execution_record_id = uuid4()  # type: ignore[misc]


def test_public_module_has_no_execution_provider_lifecycle_or_storage_imports() -> None:
    tree = ast.parse(inspect.getsource(module))
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
    tree = ast.parse(inspect.getsource(module))
    forbidden_attrs = {"uuid1", "uuid4", "now", "utcnow"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs


def test_public_module_does_not_call_v148_execution_boundary() -> None:
    tree = ast.parse(inspect.getsource(module))
    called_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_names.add(node.func.attr)

    assert "execute_current_admitted_task" not in called_names


def test_repository_protocol_exposes_only_add_and_list_history() -> None:
    methods = {
        name
        for name, value in module.TaskExecutionResultRepository.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert methods == {"add", "list_history"}
