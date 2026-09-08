from __future__ import annotations

import ast
import enum
import inspect
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import trajectory_os.application as application_package
import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle as module  # noqa: E501
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution import (
    TaskExecutionResult,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle import (  # noqa: E501
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDecisionError,
    TaskExecutionLifecycleDisposition,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_persistence import (  # noqa: E501
    TaskExecutionResultRecord,
)

DECIDED_AT = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
RECORDED_AT = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)

PUBLIC_SYMBOLS = (
    "TaskExecutionLifecycleDecision",
    "TaskExecutionLifecycleDecisionError",
    "TaskExecutionLifecycleDisposition",
    "decide_task_execution_lifecycle",
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


def _record(
    *,
    succeeded: bool = True,
    recorded_at: datetime = RECORDED_AT,
) -> TaskExecutionResultRecord:
    return TaskExecutionResultRecord(
        execution_record_id=uuid4(),
        recorded_at=recorded_at,
        result=_result(succeeded=succeeded),
    )


class ForeignDisposition(enum.StrEnum):
    COMPLETE_TASK = "COMPLETE_TASK"


# ---------------------------------------------------------------------------
# Exact public symbols / exports
# ---------------------------------------------------------------------------


def test_module_exports_exactly_the_public_symbols() -> None:
    assert tuple(module.__all__) == PUBLIC_SYMBOLS  # type: ignore[attr-defined]
    for name in PUBLIC_SYMBOLS:
        assert getattr(module, name) is not None


def test_application_package_exports_the_public_symbols() -> None:
    assert "TaskExecutionLifecycleDecision" in application_package.__all__
    assert "TaskExecutionLifecycleDecisionError" in application_package.__all__
    assert "TaskExecutionLifecycleDisposition" in application_package.__all__
    assert "decide_task_execution_lifecycle" in application_package.__all__


def test_disposition_enum_has_exactly_two_members() -> None:
    names = {member.name for member in TaskExecutionLifecycleDisposition}
    assert names == {"NO_LIFECYCLE_CHANGE", "COMPLETE_TASK"}


def test_disposition_members_have_stable_values() -> None:
    assert TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE == "NO_LIFECYCLE_CHANGE"
    assert TaskExecutionLifecycleDisposition.COMPLETE_TASK == "COMPLETE_TASK"


def test_error_type_is_a_value_error() -> None:
    assert issubclass(TaskExecutionLifecycleDecisionError, ValueError)


# ---------------------------------------------------------------------------
# Decision model strictness / frozen / extra-forbid
# ---------------------------------------------------------------------------


def test_decision_model_exact_fields_and_order() -> None:
    assert tuple(TaskExecutionLifecycleDecision.model_fields) == (
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


def test_decision_model_is_strict_frozen_and_extra_forbid() -> None:
    config = TaskExecutionLifecycleDecision.model_config
    assert config["strict"] is True
    assert config["frozen"] is True
    assert config["extra"] == "forbid"


def test_decision_model_fields_have_no_defaults() -> None:
    for name, field in TaskExecutionLifecycleDecision.model_fields.items():
        assert field.is_required(), f"field {name} must have no default"


def test_decision_model_rejects_extra_fields() -> None:
    decision_fields = dict(
        lifecycle_decision_id=uuid4(),
        decided_at=DECIDED_AT,
        execution_record_id=uuid4(),
        execution_recorded_at=RECORDED_AT,
        request_id=uuid4(),
        intent_id=uuid4(),
        execution_decision_id=uuid4(),
        portfolio_id=uuid4(),
        authorized_project_id=uuid4(),
        authorized_task_id=uuid4(),
        execution_succeeded=True,
        disposition=TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecision(**decision_fields, retry_allowed=True)  # type: ignore[call-arg]


def test_decision_model_rejects_missing_disposition() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecision(
            lifecycle_decision_id=uuid4(),
            decided_at=DECIDED_AT,
            execution_record_id=uuid4(),
            execution_recorded_at=RECORDED_AT,
            request_id=uuid4(),
            intent_id=uuid4(),
            execution_decision_id=uuid4(),
            portfolio_id=uuid4(),
            authorized_project_id=uuid4(),
            authorized_task_id=uuid4(),
            execution_succeeded=True,
        )  # type: ignore[call-arg]


def test_decision_model_rejects_naive_decided_at() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecision(
            lifecycle_decision_id=uuid4(),
            decided_at=datetime(2026, 9, 8, 9, 0),
            execution_record_id=uuid4(),
            execution_recorded_at=RECORDED_AT,
            request_id=uuid4(),
            intent_id=uuid4(),
            execution_decision_id=uuid4(),
            portfolio_id=uuid4(),
            authorized_project_id=uuid4(),
            authorized_task_id=uuid4(),
            execution_succeeded=True,
            disposition=TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
        )


def test_decision_model_rejects_naive_execution_recorded_at() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecision(
            lifecycle_decision_id=uuid4(),
            decided_at=DECIDED_AT,
            execution_record_id=uuid4(),
            execution_recorded_at=datetime(2026, 9, 8, 8, 0),
            request_id=uuid4(),
            intent_id=uuid4(),
            execution_decision_id=uuid4(),
            portfolio_id=uuid4(),
            authorized_project_id=uuid4(),
            authorized_task_id=uuid4(),
            execution_succeeded=True,
            disposition=TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
        )


def test_decision_is_frozen() -> None:
    decision = module.decide_task_execution_lifecycle(
        uuid4(),
        DECIDED_AT,
        _record(),
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )
    with pytest.raises(ValidationError):
        decision.portfolio_id = uuid4()  # type: ignore[misc]


def test_decision_model_strict_rejects_string_uuid_and_string_disposition() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecision(
            lifecycle_decision_id=str(uuid4()),  # type: ignore[arg-type]
            decided_at=DECIDED_AT,
            execution_record_id=uuid4(),
            execution_recorded_at=RECORDED_AT,
            request_id=uuid4(),
            intent_id=uuid4(),
            execution_decision_id=uuid4(),
            portfolio_id=uuid4(),
            authorized_project_id=uuid4(),
            authorized_task_id=uuid4(),
            execution_succeeded=True,
            disposition=TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
        )
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecision(
            lifecycle_decision_id=uuid4(),
            decided_at=DECIDED_AT,
            execution_record_id=uuid4(),
            execution_recorded_at=RECORDED_AT,
            request_id=uuid4(),
            intent_id=uuid4(),
            execution_decision_id=uuid4(),
            portfolio_id=uuid4(),
            authorized_project_id=uuid4(),
            authorized_task_id=uuid4(),
            execution_succeeded=True,
            disposition="NO_LIFECYCLE_CHANGE",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Direct model construction — disposition invariant (self-validating model)
# ---------------------------------------------------------------------------


def _direct_fields(
    *,
    succeeded: bool,
    disposition: TaskExecutionLifecycleDisposition,
) -> dict[str, object]:
    return dict(
        lifecycle_decision_id=uuid4(),
        decided_at=DECIDED_AT,
        execution_record_id=uuid4(),
        execution_recorded_at=RECORDED_AT,
        request_id=uuid4(),
        intent_id=uuid4(),
        execution_decision_id=uuid4(),
        portfolio_id=uuid4(),
        authorized_project_id=uuid4(),
        authorized_task_id=uuid4(),
        execution_succeeded=succeeded,
        disposition=disposition,
    )


def test_direct_construction_true_complete_task_valid() -> None:
    decision = TaskExecutionLifecycleDecision(
        **_direct_fields(
            succeeded=True,
            disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
        )
    )
    assert decision.execution_succeeded is True
    assert decision.disposition is TaskExecutionLifecycleDisposition.COMPLETE_TASK


def test_direct_construction_true_no_lifecycle_change_valid() -> None:
    decision = TaskExecutionLifecycleDecision(
        **_direct_fields(
            succeeded=True,
            disposition=TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
        )
    )
    assert decision.execution_succeeded is True
    assert decision.disposition is TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE


def test_direct_construction_false_no_lifecycle_change_valid() -> None:
    decision = TaskExecutionLifecycleDecision(
        **_direct_fields(
            succeeded=False,
            disposition=TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
        )
    )
    assert decision.execution_succeeded is False
    assert decision.disposition is TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE


def test_direct_construction_false_complete_task_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleDecision(
            **_direct_fields(
                succeeded=False,
                disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
            )
        )


# ---------------------------------------------------------------------------
# Strict inputs
# ---------------------------------------------------------------------------


def _decide(
    record: object,
    disposition: object = TaskExecutionLifecycleDisposition.COMPLETE_TASK,
) -> object:
    return module.decide_task_execution_lifecycle(
        uuid4(),
        DECIDED_AT,
        record,
        disposition,
    )


@pytest.mark.parametrize("bad", [None, str(uuid4()), "id", 1, b"id"])
def test_lifecycle_decision_id_must_be_genuine_uuid(bad: object) -> None:
    with pytest.raises(TaskExecutionLifecycleDecisionError, match="genuine UUID"):
        module.decide_task_execution_lifecycle(
            bad,
            DECIDED_AT,
            _record(),
            TaskExecutionLifecycleDisposition.COMPLETE_TASK,
        )


@pytest.mark.parametrize("bad", [None, "2026-09-08T09:00:00+00:00", 1, UUID.__str__(uuid4())])
def test_decided_at_must_be_genuine_datetime(bad: object) -> None:
    with pytest.raises(TaskExecutionLifecycleDecisionError, match="genuine datetime"):
        module.decide_task_execution_lifecycle(
            uuid4(),
            bad,
            _record(),
            TaskExecutionLifecycleDisposition.COMPLETE_TASK,
        )


def test_decided_at_must_be_timezone_aware() -> None:
    with pytest.raises(TaskExecutionLifecycleDecisionError, match="timezone-aware"):
        module.decide_task_execution_lifecycle(
            uuid4(),
            datetime(2026, 9, 8, 9, 0),
            _record(),
            TaskExecutionLifecycleDisposition.COMPLETE_TASK,
        )


def test_decided_at_preserves_original_offset() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    decided_at = datetime(2026, 9, 8, 14, 30, tzinfo=offset)
    record_offset = timezone(timedelta(hours=-3))
    record = _record(recorded_at=datetime(2026, 9, 8, 5, 0, tzinfo=record_offset))

    decision = module.decide_task_execution_lifecycle(
        uuid4(),
        decided_at,
        record,
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )

    assert decision.decided_at == decided_at
    assert decision.decided_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert decision.execution_recorded_at.utcoffset() == timedelta(hours=-3)


def test_no_temporal_ordering_rule_between_decided_at_and_recorded_at() -> None:
    record = _record()

    before = datetime(2026, 9, 7, 23, 59, tzinfo=UTC)
    after = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)

    earlier = module.decide_task_execution_lifecycle(
        uuid4(),
        before,
        record,
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )
    later = module.decide_task_execution_lifecycle(
        uuid4(),
        after,
        record,
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )

    assert earlier.decided_at == before
    assert later.decided_at == after


# ---------------------------------------------------------------------------
# Genuine V1.49 record requirement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, {}, "record", 42, object(), _result()])
def test_execution_record_must_be_genuine_v149(bad: object) -> None:
    with pytest.raises(
        TaskExecutionLifecycleDecisionError,
        match="genuine V1.49 TaskExecutionResultRecord",
    ):
        _decide(bad)


def test_hostile_constructed_v149_record_freshly_strict_revalidated() -> None:
    hostile_result = TaskExecutionResult.model_construct(
        **{
            **_result().model_dump(mode="python"),
            "succeeded": 1,
        }
    )
    hostile = TaskExecutionResultRecord.model_construct(
        execution_record_id=uuid4(),
        recorded_at=RECORDED_AT,
        result=hostile_result,
    )

    with pytest.raises(TaskExecutionLifecycleDecisionError, match="strict re-validation"):
        _decide(hostile)


def test_hostile_constructed_v149_record_with_naive_recorded_at_rejected() -> None:
    hostile = TaskExecutionResultRecord.model_construct(
        execution_record_id=uuid4(),
        recorded_at=datetime(2026, 9, 8, 8, 0),
        result=_result(),
    )

    with pytest.raises(TaskExecutionLifecycleDecisionError, match="strict re-validation"):
        _decide(hostile)


def test_hostile_nested_v148_boolean_int_result_rejected() -> None:
    hostile_nested = TaskExecutionResult.model_construct(
        **{
            **_result().model_dump(mode="python"),
            "succeeded": True,
            "portfolio_id": str(uuid4()),
        }
    )
    record = TaskExecutionResultRecord.model_construct(
        execution_record_id=uuid4(),
        recorded_at=RECORDED_AT,
        result=hostile_nested,
    )

    with pytest.raises(TaskExecutionLifecycleDecisionError, match="strict re-validation"):
        _decide(record)


def test_no_semantic_reads_from_caller_owned_record_after_revalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    original_portfolio_id = record.result.portfolio_id
    replacement_portfolio_id = uuid4()
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

    decision = module.decide_task_execution_lifecycle(
        uuid4(),
        DECIDED_AT,
        record,
        TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )

    assert decision.portfolio_id == original_portfolio_id


def test_genuine_v149_record_accepted_and_retained_copy_is_independent() -> None:
    record = _record()
    original_ids = (
        record.execution_record_id,
        record.recorded_at,
        record.result.request_id,
        record.result.intent_id,
        record.result.decision_id,
        record.result.portfolio_id,
        record.result.authorized_project_id,
        record.result.authorized_task_id,
    )

    decision = module.decide_task_execution_lifecycle(
        uuid4(),
        DECIDED_AT,
        record,
        TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )

    assert (
        decision.execution_record_id,
        decision.execution_recorded_at,
        decision.request_id,
        decision.intent_id,
        decision.execution_decision_id,
        decision.portfolio_id,
        decision.authorized_project_id,
        decision.authorized_task_id,
    ) == original_ids


# ---------------------------------------------------------------------------
# Disposition matrix
# ---------------------------------------------------------------------------


def test_success_complete_task_accepted() -> None:
    decision = _decide(
        _record(succeeded=True),
        TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )
    assert decision.disposition is TaskExecutionLifecycleDisposition.COMPLETE_TASK
    assert decision.execution_succeeded is True


def test_success_no_lifecycle_change_accepted() -> None:
    decision = _decide(
        _record(succeeded=True),
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )
    assert decision.disposition is TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE
    assert decision.execution_succeeded is True


def test_failure_no_lifecycle_change_accepted() -> None:
    decision = _decide(
        _record(succeeded=False),
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )
    assert decision.disposition is TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE
    assert decision.execution_succeeded is False


def test_failure_complete_task_rejected() -> None:
    with pytest.raises(TaskExecutionLifecycleDecisionError, match="exactly True"):
        _decide(_record(succeeded=False), TaskExecutionLifecycleDisposition.COMPLETE_TASK)


def test_succeeded_true_does_not_automatically_imply_complete_task() -> None:
    decision = _decide(
        _record(succeeded=True),
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )
    assert decision.disposition is TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE
    assert decision.disposition is not TaskExecutionLifecycleDisposition.COMPLETE_TASK


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "COMPLETE_TASK",
        "NO_LIFECYCLE_CHANGE",
        1,
        2,
        object(),
        ForeignDisposition.COMPLETE_TASK,
    ],
)
def test_disposition_must_be_genuine_v150_member(bad: object) -> None:
    with pytest.raises(
        TaskExecutionLifecycleDecisionError,
        match="genuine TaskExecutionLifecycleDisposition",
    ):
        module.decide_task_execution_lifecycle(
            uuid4(),
            DECIDED_AT,
            _record(),
            bad,
        )


# ---------------------------------------------------------------------------
# Exact provenance
# ---------------------------------------------------------------------------


def test_all_provenance_is_projected_exactly_from_the_v149_v148_chain() -> None:
    record = _record(succeeded=False)
    decided_uuid = uuid4()

    decision = module.decide_task_execution_lifecycle(
        decided_uuid,
        DECIDED_AT,
        record,
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )

    assert decision.lifecycle_decision_id == decided_uuid
    assert decision.decided_at == DECIDED_AT
    assert decision.execution_record_id == record.execution_record_id
    assert decision.execution_recorded_at == record.recorded_at
    assert decision.request_id == record.result.request_id
    assert decision.intent_id == record.result.intent_id
    assert decision.execution_decision_id == record.result.decision_id
    assert decision.portfolio_id == record.result.portfolio_id
    assert decision.authorized_project_id == record.result.authorized_project_id
    assert decision.authorized_task_id == record.result.authorized_task_id
    assert decision.execution_succeeded is False


def test_exact_succeeded_bool_preserved() -> None:
    true_decision = _decide(_record(succeeded=True))
    false_decision = _decide(
        _record(succeeded=False),
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )
    assert true_decision.execution_succeeded is True
    assert false_decision.execution_succeeded is False


def test_caller_cannot_swap_or_override_upstream_provenance() -> None:
    # The function surface accepts exactly one V1.49 record plus the three
    # caller/human values; there is no parameter through which upstream
    # execution provenance could be swapped.
    params = inspect.signature(module.decide_task_execution_lifecycle).parameters
    for name in (
        "execution_record_id",
        "request_id",
        "intent_id",
        "execution_decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
        "execution_succeeded",
    ):
        assert name not in params

    record = _record()
    decision = module.decide_task_execution_lifecycle(
        uuid4(),
        DECIDED_AT,
        record,
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )
    assert decision.portfolio_id == record.result.portfolio_id


def test_caller_cannot_mutate_the_returned_provenance() -> None:
    record = _record()
    original_portfolio_id = record.result.portfolio_id
    decision = module.decide_task_execution_lifecycle(
        uuid4(),
        DECIDED_AT,
        record,
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
    )
    with pytest.raises(ValidationError):
        decision.portfolio_id = uuid4()  # type: ignore[misc]
    assert decision.portfolio_id == original_portfolio_id


# ---------------------------------------------------------------------------
# Architecture guards
# ---------------------------------------------------------------------------


def test_module_imports_no_persistence_provider_runtime_or_wall_clock_modules() -> None:
    tree = ast.parse(inspect.getsource(module))
    forbidden_roots = {
        "subprocess",
        "shutil",
        "sqlite3",
        "sqlalchemy",
        "ollama",
        "socket",
        "httpx",
        "requests",
        "time",
    }
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert forbidden_roots.isdisjoint(imported)


def test_module_does_not_import_or_reference_status_transition_or_current_state() -> None:
    source = inspect.getsource(module)
    for token in (
        "transition_entity_status",
        "entity_status_transition",
        "build_work_breakdown",
        "EntityStatus",
        "Portfolio.",
    ):
        assert token not in source, f"forbidden token {token!r} found in V1.50 module"


def test_module_does_not_generate_uuid_or_read_wall_clock() -> None:
    tree = ast.parse(inspect.getsource(module))
    forbidden_attrs = {"uuid1", "uuid4", "now", "utcnow"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs


def test_module_has_no_retry_idempotency_or_supersession_tokens() -> None:
    source = inspect.getsource(module).lower()
    for token in ("retry", "idempot", "exactly-once", "exactly once", "latest", "effective"):
        assert token not in source, f"forbidden token {token!r} found in V1.50 module"


def test_decide_function_exposes_only_the_three_human_inputs_and_record() -> None:
    params = tuple(inspect.signature(module.decide_task_execution_lifecycle).parameters)
    assert params == (
        "lifecycle_decision_id",
        "decided_at",
        "execution_record",
        "disposition",
    )


def test_module_namespace_has_no_repository_or_persistence_surface() -> None:
    public_names = {name for name in dir(module) if not name.startswith("_")}
    assert not any("Repository" in name for name in public_names)
    assert not any("sqlite" in name.lower() for name in public_names)
    assert "record_task_execution_result_durably" not in public_names


def test_module_performs_no_attribute_mutation() -> None:
    source = inspect.getsource(module)
    for token in ("setattr", "del ", ".append("):
        assert token not in source, f"forbidden token {token!r} found in V1.50 module"


def test_module_does_not_call_v148_execution_boundary() -> None:
    tree = ast.parse(inspect.getsource(module))
    called_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_names.add(node.func.attr)

    assert "execute_current_admitted_task" not in called_names


def test_rejected_disposition_does_not_leak_partial_state() -> None:
    before = [name for name in dir(module) if not name.startswith("_")]
    with pytest.raises(TaskExecutionLifecycleDecisionError):
        _decide(_record(succeeded=False), TaskExecutionLifecycleDisposition.COMPLETE_TASK)
    after = [name for name in dir(module) if not name.startswith("_")]
    assert before == after
