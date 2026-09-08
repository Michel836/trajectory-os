from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution as module
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution import (
    TaskExecutionBoundaryError,
    TaskExecutionCommand,
    TaskExecutionResult,
    execute_current_admitted_task,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_admission import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskExecutionAdmission,
    PortfolioProjectFocusNextReadyTaskExecutionAdmissionConstraint,
    PortfolioProjectFocusNextReadyTaskExecutionAdmissionState,
)
from trajectory_os.domain.entities import EntityStatus, EntityType
from trajectory_os.domain.relations import RelationType

State = PortfolioProjectFocusNextReadyTaskExecutionAdmissionState


def _admission(
    state: State = State.ADMITTED,
) -> PortfolioProjectFocusNextReadyTaskExecutionAdmission:
    kwargs: dict[str, object]
    if state in {
        State.PROJECT_MISSING,
        State.PROJECT_TYPE_MISMATCH,
        State.TASK_MISSING,
        State.TASK_TYPE_MISMATCH,
    }:
        kwargs = {
            "task_status": None,
            "constraints": None,
            "unsatisfied_constraint_count": None,
        }
    elif state is State.TASK_NOT_IN_AUTHORIZED_PROJECT:
        kwargs = {
            "task_status": EntityStatus.ACTIVE,
            "constraints": None,
            "unsatisfied_constraint_count": None,
        }
    elif state is State.INELIGIBLE_STATUS:
        kwargs = {
            "task_status": EntityStatus.PAUSED,
            "constraints": (),
            "unsatisfied_constraint_count": 0,
        }
    elif state is State.CONSTRAINED:
        raise AssertionError("CONSTRAINED helper case requires a real constraint")
    else:
        kwargs = {
            "task_status": EntityStatus.ACTIVE,
            "constraints": (),
            "unsatisfied_constraint_count": 0,
        }

    return PortfolioProjectFocusNextReadyTaskExecutionAdmission(
        request_id=uuid4(),
        requested_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
        intent_id=uuid4(),
        authorized_at=datetime(2026, 9, 8, 6, 0, tzinfo=UTC),
        decision_id=uuid4(),
        portfolio_id=uuid4(),
        authorized_project_id=uuid4(),
        authorized_task_id=uuid4(),
        admission_state=state,
        **kwargs,
    )


def _result_from_command(
    command: TaskExecutionCommand,
    *,
    succeeded: bool = True,
    overrides: dict[str, object] | None = None,
) -> TaskExecutionResult:
    payload: dict[str, object] = {
        "request_id": command.request_id,
        "intent_id": command.intent_id,
        "decision_id": command.decision_id,
        "portfolio_id": command.portfolio_id,
        "authorized_project_id": command.authorized_project_id,
        "authorized_task_id": command.authorized_task_id,
        "succeeded": succeeded,
    }
    if overrides:
        payload.update(overrides)
    return TaskExecutionResult(**payload)


class RecordingExecutor:
    def __init__(
        self,
        result_factory: Callable[[TaskExecutionCommand], object] | None = None,
    ) -> None:
        self.calls: list[TaskExecutionCommand] = []
        self.result_factory = result_factory

    def execute(self, command: TaskExecutionCommand) -> TaskExecutionResult:
        self.calls.append(command)
        if self.result_factory is None:
            return _result_from_command(command)
        return self.result_factory(command)  # type: ignore[return-value]


def test_command_exact_shape_and_config() -> None:
    assert tuple(TaskExecutionCommand.model_fields) == (
        "request_id",
        "intent_id",
        "decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
    )
    assert TaskExecutionCommand.model_config["strict"] is True
    assert TaskExecutionCommand.model_config["frozen"] is True
    assert TaskExecutionCommand.model_config["extra"] == "forbid"


def test_result_exact_shape_and_config() -> None:
    assert tuple(TaskExecutionResult.model_fields) == (
        "request_id",
        "intent_id",
        "decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
        "succeeded",
    )
    assert TaskExecutionResult.model_config["strict"] is True
    assert TaskExecutionResult.model_config["frozen"] is True
    assert TaskExecutionResult.model_config["extra"] == "forbid"


def test_command_rejects_string_uuid() -> None:
    value = uuid4()
    with pytest.raises(ValidationError):
        TaskExecutionCommand(
            request_id=str(value),
            intent_id=value,
            decision_id=value,
            portfolio_id=value,
            authorized_project_id=value,
            authorized_task_id=value,
        )


@pytest.mark.parametrize("bad", [1, 0, "true", "false"])
def test_result_succeeded_is_strict_bool(bad: object) -> None:
    value = uuid4()
    with pytest.raises(ValidationError):
        TaskExecutionResult(
            request_id=value,
            intent_id=value,
            decision_id=value,
            portfolio_id=value,
            authorized_project_id=value,
            authorized_task_id=value,
            succeeded=bad,
        )


def test_models_are_frozen() -> None:
    admission = _admission()
    executor = RecordingExecutor()
    result = execute_current_admitted_task(admission, executor)
    command = executor.calls[0]

    with pytest.raises(ValidationError):
        command.authorized_task_id = uuid4()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        result.succeeded = False  # type: ignore[misc]


def test_valid_admission_invokes_executor_once_with_exact_identity() -> None:
    admission = _admission()
    executor = RecordingExecutor()

    result = execute_current_admitted_task(admission, executor)

    assert len(executor.calls) == 1
    command = executor.calls[0]
    assert command.request_id == admission.request_id
    assert command.intent_id == admission.intent_id
    assert command.decision_id == admission.decision_id
    assert command.portfolio_id == admission.portfolio_id
    assert command.authorized_project_id == admission.authorized_project_id
    assert command.authorized_task_id == admission.authorized_task_id
    assert result.succeeded is True


def test_explicit_unsuccessful_result_is_returned_without_lifecycle_inference() -> None:
    admission = _admission()
    executor = RecordingExecutor(
        lambda command: _result_from_command(command, succeeded=False)
    )

    result = execute_current_admitted_task(admission, executor)

    assert result.succeeded is False
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    "state",
    [
        State.PROJECT_MISSING,
        State.PROJECT_TYPE_MISMATCH,
        State.TASK_MISSING,
        State.TASK_TYPE_MISMATCH,
        State.TASK_NOT_IN_AUTHORIZED_PROJECT,
        State.INELIGIBLE_STATUS,
    ],
)
def test_every_constructible_non_admitted_state_is_rejected_before_execution(
    state: State,
) -> None:
    executor = RecordingExecutor()

    with pytest.raises(TaskExecutionBoundaryError, match="ADMITTED"):
        execute_current_admitted_task(_admission(state), executor)

    assert executor.calls == []


def test_constrained_admission_is_rejected_before_execution() -> None:
    constraint = PortfolioProjectFocusNextReadyTaskExecutionAdmissionConstraint(
        relation_id=uuid4(),
        relation_type=RelationType.DEPENDS_ON,
        counterpart_entity_id=uuid4(),
        counterpart_entity_type=EntityType.TASK,
        counterpart_status=EntityStatus.ACTIVE,
        satisfied=False,
    )
    admission = PortfolioProjectFocusNextReadyTaskExecutionAdmission(
        request_id=uuid4(),
        requested_at=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
        intent_id=uuid4(),
        authorized_at=datetime(2026, 9, 8, 6, 0, tzinfo=UTC),
        decision_id=uuid4(),
        portfolio_id=uuid4(),
        authorized_project_id=uuid4(),
        authorized_task_id=uuid4(),
        task_status=EntityStatus.ACTIVE,
        admission_state=State.CONSTRAINED,
        constraints=(constraint,),
        unsatisfied_constraint_count=1,
    )
    executor = RecordingExecutor()

    with pytest.raises(TaskExecutionBoundaryError, match="ADMITTED"):
        execute_current_admitted_task(admission, executor)

    assert executor.calls == []


def test_semantic_reads_after_revalidation_use_only_fresh_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    executor = RecordingExecutor()
    admission_type = PortfolioProjectFocusNextReadyTaskExecutionAdmission
    original_model_dump = admission_type.model_dump

    def dump_then_corrupt(
        self: PortfolioProjectFocusNextReadyTaskExecutionAdmission,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        payload = original_model_dump(self, *args, **kwargs)
        object.__setattr__(
            self,
            "admission_state",
            State.PROJECT_MISSING,
        )
        return payload

    monkeypatch.setattr(admission_type, "model_dump", dump_then_corrupt)

    result = execute_current_admitted_task(admission, executor)

    assert result.succeeded is True
    assert len(executor.calls) == 1
    assert admission.admission_state is State.PROJECT_MISSING


@pytest.mark.parametrize("bad", [None, {}, "admitted", object()])
def test_non_genuine_admission_is_rejected_before_execution(bad: object) -> None:
    executor = RecordingExecutor()

    with pytest.raises(TaskExecutionBoundaryError, match="genuine V1.47"):
        execute_current_admitted_task(bad, executor)  # type: ignore[arg-type]

    assert executor.calls == []


def test_hostile_constructed_admission_is_freshly_revalidated() -> None:
    admission = _admission()
    payload = admission.model_dump(mode="python")
    payload["authorized_task_id"] = "not-a-uuid"
    hostile = PortfolioProjectFocusNextReadyTaskExecutionAdmission.model_construct(
        **payload
    )
    executor = RecordingExecutor()

    with pytest.raises(TaskExecutionBoundaryError, match="strict re-validation"):
        execute_current_admitted_task(hostile, executor)

    assert executor.calls == []


def test_executor_exception_propagates_unchanged_and_is_not_retried() -> None:
    marker = RuntimeError("provider exploded")

    def explode(command: TaskExecutionCommand) -> object:
        raise marker

    executor = RecordingExecutor(explode)

    with pytest.raises(RuntimeError) as exc_info:
        execute_current_admitted_task(_admission(), executor)

    assert exc_info.value is marker
    assert len(executor.calls) == 1


@pytest.mark.parametrize("bad", [None, {}, "result", object()])
def test_non_result_executor_return_is_rejected_without_retry(bad: object) -> None:
    executor = RecordingExecutor(lambda command: bad)

    with pytest.raises(TaskExecutionBoundaryError, match="genuine TaskExecutionResult"):
        execute_current_admitted_task(_admission(), executor)

    assert len(executor.calls) == 1


def test_hostile_constructed_result_is_freshly_revalidated() -> None:
    def hostile(command: TaskExecutionCommand) -> object:
        return TaskExecutionResult.model_construct(
            request_id=command.request_id,
            intent_id=command.intent_id,
            decision_id=command.decision_id,
            portfolio_id=command.portfolio_id,
            authorized_project_id=command.authorized_project_id,
            authorized_task_id=command.authorized_task_id,
            succeeded=1,
        )

    executor = RecordingExecutor(hostile)

    with pytest.raises(TaskExecutionBoundaryError, match="strict re-validation"):
        execute_current_admitted_task(_admission(), executor)

    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "intent_id",
        "decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
    ],
)
def test_executor_result_cannot_redirect_any_identity(field: str) -> None:
    def redirected(command: TaskExecutionCommand) -> object:
        return _result_from_command(command, overrides={field: uuid4()})

    executor = RecordingExecutor(redirected)

    with pytest.raises(TaskExecutionBoundaryError, match="exactly match"):
        execute_current_admitted_task(_admission(), executor)

    assert len(executor.calls) == 1


def test_repeated_explicit_boundary_calls_are_separate_executions() -> None:
    admission = _admission()
    executor = RecordingExecutor()

    first = execute_current_admitted_task(admission, executor)
    second = execute_current_admitted_task(admission, executor)

    assert first == second
    assert len(executor.calls) == 2


def test_public_module_has_no_provider_persistence_lifecycle_or_clock_imports() -> None:
    tree = ast.parse(inspect.getsource(module))
    forbidden_roots = {
        "subprocess",
        "sqlite3",
        "time",
        "ollama",
    }
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert forbidden_roots.isdisjoint(imported)


def test_public_module_does_not_call_uuid_or_wall_clock_generation() -> None:
    tree = ast.parse(inspect.getsource(module))
    forbidden_attrs = {"uuid1", "uuid4", "now", "utcnow"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs


def test_uuid_identity_fields_are_genuine_uuid_values() -> None:
    admission = _admission()
    executor = RecordingExecutor()
    result = execute_current_admitted_task(admission, executor)

    for field in (
        "request_id",
        "intent_id",
        "decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
    ):
        assert isinstance(getattr(result, field), UUID)
