"""V1.61 — durable append-only history for V1.60 lifecycle outcomes.

Focused unit tests plus executable (AST) architecture guards for
``execution_effort_project_focus_next_ready_task_execution_lifecycle_outcome_history``.

The module must perform EXACTLY: genuine UUID ``outcome_record_id`` ->
genuine aware ``recorded_at`` -> genuine ``TaskExecutionLifecycleOutcome``
-> fresh COMPLETE strict revalidation of the full outcome -> immutable
record construction -> ``repository.add(record)`` exactly once -> return
the exact record -> stop. No ``coordinate_task_execution_lifecycle``
replay, no V1.51 admission or V1.52 application replay, no Portfolio
load/save, no provider / runtime / agent / subprocess / shell, no UUID
generation, no wall clock, no DB schema, no retry / idempotency /
exactly-once / latest / effective / transaction claims, no mutation or
cascade, no dedupe or supersession, and no inference of any CURRENT
Portfolio/task state from the embedded historical evidence.
"""

from __future__ import annotations

import ast
import inspect
import itertools
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_outcome_history as outcome_history_module  # noqa: E501
from trajectory_os.application import (
    DurableTaskExecutionLifecycleOutcomeError,
    TaskExecutionLifecycleAdmission,
    TaskExecutionLifecycleAdmissionRecord,
    TaskExecutionLifecycleApplicationRecord,
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDecisionRecord,
    TaskExecutionLifecycleDisposition,
    TaskExecutionLifecycleOutcome,
    TaskExecutionLifecycleOutcomeRecord,
    admit_current_task_execution_lifecycle,
    record_task_execution_lifecycle_outcome_durably,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.entity_status_transition import (
    EntityStatusTransitionResult,
    transition_entity_status,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

RECORDED_AT = datetime(2026, 3, 5, 8, 0, tzinfo=UTC)
EXECUTED_AT = datetime(
    2026, 3, 1, 9, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
)
DECIDED_AT = datetime(2026, 3, 2, 10, 0, tzinfo=timezone(timedelta(minutes=45)))
BASE_TS = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
CHANGED_AT = datetime(2026, 3, 3, 7, 30, tzinfo=timezone(timedelta(hours=2)))
ADMISSION_RECORDED_AT = datetime(2026, 3, 4, 1, 0, tzinfo=UTC)
DECISION_RECORDED_AT = datetime(
    2026, 3, 4, 3, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
)
APPLICATION_RECORDED_AT = datetime(
    2026, 3, 4, 5, 30, tzinfo=timezone(timedelta(hours=-3, minutes=30))
)

_UUID_SEQUENCE = itertools.count(1)


def _uuid() -> UUID:
    """Deterministic sequential test identity (test-only helper)."""
    return UUID(int=next(_UUID_SEQUENCE))


def _outcome(task_status: EntityStatus = EntityStatus.SOMEDAY) -> dict[str, Any]:
    """Build one genuine, complete V1.60 outcome through the EXISTING
    canonical boundaries (V1.50 decision -> V1.51 admission -> V1.58/V1.55
    records -> V1.52 transition -> V1.53 record -> V1.60 outcome). The
    V1.61 module under test never re-creates any of it."""

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
    return {
        "project": project,
        "task": task,
        "portfolio": portfolio,
        "decision": decision,
        "admission": admission,
        "admission_record": admission_record,
        "decision_record": decision_record,
        "result": result,
        "application_record": application_record,
        "outcome": outcome,
    }


def _hostile_outcome(**component_replacements: Any) -> TaskExecutionLifecycleOutcome:
    """One genuine ``TaskExecutionLifecycleOutcome`` whose EXACT named
    components are replaced by hostile, validation-bypassing nested
    states built via ``model_construct``. Unreplaced components remain the
    genuine canonical instances."""

    scenario = _outcome()
    base = {
        "admission_record": scenario["admission_record"],
        "decision_record": scenario["decision_record"],
        "transition_result": scenario["result"],
        "application_record": scenario["application_record"],
    }
    assert set(component_replacements) <= set(base)
    base.update(component_replacements)
    return TaskExecutionLifecycleOutcome.model_construct(**base)


class RecordingRepository:
    def __init__(self) -> None:
        self.added: list[TaskExecutionLifecycleOutcomeRecord] = []

    def add(self, record: TaskExecutionLifecycleOutcomeRecord) -> None:
        self.added.append(record)

    def list_history(
        self,
        portfolio_id: UUID,
    ) -> tuple[TaskExecutionLifecycleOutcomeRecord, ...]:
        return tuple(
            record
            for record in self.added
            if record.outcome.admission_record.admission.portfolio_id == portfolio_id
        )


# -- record model -----------------------------------------------------------


def test_record_exact_shape_and_config() -> None:
    assert tuple(TaskExecutionLifecycleOutcomeRecord.model_fields) == (
        "outcome_record_id",
        "recorded_at",
        "outcome",
    )
    assert TaskExecutionLifecycleOutcomeRecord.model_config["strict"] is True
    assert TaskExecutionLifecycleOutcomeRecord.model_config["frozen"] is True
    assert TaskExecutionLifecycleOutcomeRecord.model_config["extra"] == "forbid"


def test_record_and_outcome_invent_no_new_fields() -> None:
    outcome = _outcome()["outcome"]

    assert tuple(outcome.model_fields) == (
        "admission_record",
        "decision_record",
        "transition_result",
        "application_record",
    )


def test_record_rejects_string_outcome_record_id() -> None:
    outcome = _outcome()["outcome"]

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleOutcomeRecord(
            outcome_record_id=str(uuid4()),
            recorded_at=RECORDED_AT,
            outcome=outcome,
        )


def test_record_rejects_naive_recorded_at() -> None:
    outcome = _outcome()["outcome"]

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleOutcomeRecord(
            outcome_record_id=uuid4(),
            recorded_at=datetime(2026, 9, 8, 8, 0),
            outcome=outcome,
        )


def test_record_rejects_foreign_outcome_type() -> None:
    # A genuine V1.51 admission is NOT a V1.60 outcome.
    admission = _outcome()["admission"]
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleOutcomeRecord(
            outcome_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            outcome=admission,  # type: ignore[arg-type]
        )


def test_record_rejects_extra_fields() -> None:
    outcome = _outcome()["outcome"]

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleOutcomeRecord(
            outcome_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            outcome=outcome,
            effective=True,  # type: ignore[call-arg]
        )


def test_record_is_frozen_and_embedded_outcome_remains_immutable() -> None:
    outcome = _outcome()["outcome"]

    record = TaskExecutionLifecycleOutcomeRecord(
        outcome_record_id=uuid4(),
        recorded_at=RECORDED_AT,
        outcome=outcome,
    )

    with pytest.raises(ValidationError):
        record.outcome_record_id = uuid4()  # type: ignore[misc]

    with pytest.raises(ValidationError):
        record.outcome = TaskExecutionLifecycleOutcome(  # type: ignore[misc]
            **outcome.model_dump(mode="python")
        )

    with pytest.raises(ValidationError):
        record.outcome.transition_result.new_status = (  # type: ignore[misc]
            EntityStatus.ARCHIVED
        )


# -- happy path -------------------------------------------------------------


def test_happy_path_returns_one_exact_immutable_outcome_record() -> None:
    outcome = _outcome()["outcome"]
    repository = RecordingRepository()
    outcome_record_id = uuid4()

    record = record_task_execution_lifecycle_outcome_durably(
        outcome_record_id,
        RECORDED_AT,
        outcome,
        repository=repository,
    )

    assert len(repository.added) == 1
    assert repository.added[0] is record
    assert record.outcome_record_id == outcome_record_id
    assert record.recorded_at == RECORDED_AT
    assert record.outcome == outcome


def test_caller_supplied_outcome_record_id_preserved_exactly() -> None:
    outcome = _outcome()["outcome"]
    outcome_record_id = uuid4()
    repository = RecordingRepository()

    record = record_task_execution_lifecycle_outcome_durably(
        outcome_record_id,
        RECORDED_AT,
        outcome,
        repository=repository,
    )

    assert record.outcome_record_id == outcome_record_id
    assert repository.added[0].outcome_record_id == outcome_record_id


def test_caller_supplied_recorded_at_preserved_exactly_with_non_utc_offset() -> None:
    outcome = _outcome()["outcome"]
    offset = timezone(timedelta(hours=5, minutes=30))
    recorded_at = datetime(2026, 9, 8, 13, 30, tzinfo=offset)
    repository = RecordingRepository()

    record = record_task_execution_lifecycle_outcome_durably(
        uuid4(),
        recorded_at,
        outcome,
        repository=repository,
    )

    assert record.recorded_at == recorded_at
    assert record.recorded_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_embedded_outcome_is_freshly_retained_canonical_copy() -> None:
    outcome = _outcome()["outcome"]

    record = TaskExecutionLifecycleOutcomeRecord(
        outcome_record_id=uuid4(),
        recorded_at=RECORDED_AT,
        outcome=outcome,
    )

    assert record.outcome is not outcome
    assert type(record.outcome) is TaskExecutionLifecycleOutcome
    assert record.outcome == outcome


def test_all_four_v160_components_preserved_exactly() -> None:
    scenario = _outcome()
    repository = RecordingRepository()

    record = record_task_execution_lifecycle_outcome_durably(
        uuid4(),
        RECORDED_AT,
        scenario["outcome"],
        repository=repository,
    )

    assert record.outcome.admission_record == scenario["admission_record"]
    assert record.outcome.decision_record == scenario["decision_record"]
    assert record.outcome.transition_result == scenario["result"]
    assert (
        record.outcome.application_record == scenario["application_record"]
    )


def test_nested_admission_identities_timestamps_and_status_preserved() -> None:
    scenario = _outcome(EntityStatus.SOMEDAY)
    repository = RecordingRepository()

    record = record_task_execution_lifecycle_outcome_durably(
        uuid4(), RECORDED_AT, scenario["outcome"], repository=repository
    )

    embedded_record = record.outcome.admission_record
    embedded_admission = embedded_record.admission

    assert (
        embedded_record.admission_record_id
        == scenario["admission_record"].admission_record_id
    )
    assert embedded_record.recorded_at == ADMISSION_RECORDED_AT
    assert (
        embedded_admission.lifecycle_decision_id
        == scenario["decision"].lifecycle_decision_id
    )
    assert embedded_admission.decided_at == DECIDED_AT
    assert embedded_admission.decided_at.utcoffset() == timedelta(minutes=45)
    assert embedded_admission.execution_recorded_at == EXECUTED_AT
    assert embedded_admission.execution_recorded_at.utcoffset() == timedelta(
        hours=5, minutes=30
    )
    assert embedded_admission.portfolio_id == scenario["portfolio"].id
    assert embedded_admission.current_task_status is EntityStatus.SOMEDAY


def test_nested_decision_identities_and_timestamps_preserved() -> None:
    scenario = _outcome()
    repository = RecordingRepository()

    record = record_task_execution_lifecycle_outcome_durably(
        uuid4(), RECORDED_AT, scenario["outcome"], repository=repository
    )

    embedded_record = record.outcome.decision_record
    embedded_decision = embedded_record.decision

    assert (
        embedded_record.decision_record_id
        == scenario["decision_record"].decision_record_id
    )
    assert embedded_record.recorded_at == DECISION_RECORDED_AT
    assert embedded_record.recorded_at.utcoffset() == timedelta(
        hours=5, minutes=30
    )
    assert (
        embedded_decision.lifecycle_decision_id
        == scenario["decision"].lifecycle_decision_id
    )
    assert embedded_decision.decided_at == DECIDED_AT
    assert embedded_decision.decided_at.utcoffset() == timedelta(minutes=45)
    assert embedded_decision.execution_recorded_at == EXECUTED_AT
    assert embedded_decision.disposition is (
        TaskExecutionLifecycleDisposition.COMPLETE_TASK
    )


def test_transition_result_identities_statuses_and_snapshot_preserved() -> None:
    scenario = _outcome(EntityStatus.SOMEDAY)
    repository = RecordingRepository()

    record = record_task_execution_lifecycle_outcome_durably(
        uuid4(), RECORDED_AT, scenario["outcome"], repository=repository
    )

    result = record.outcome.transition_result
    original = scenario["result"]

    assert result.portfolio.id == scenario["portfolio"].id
    assert result.portfolio is not original.portfolio
    assert result.portfolio == original.portfolio
    assert result.entity_id == scenario["task"].id
    assert result.previous_status is EntityStatus.SOMEDAY
    assert result.new_status is EntityStatus.COMPLETED
    assert result.changed_at == CHANGED_AT
    assert result.changed_at.utcoffset() == timedelta(hours=2)

    task = result.portfolio.get_entity(result.entity_id)
    assert task is not None
    assert task.entity_type is EntityType.TASK
    assert task.status is EntityStatus.COMPLETED
    assert task.updated_at == CHANGED_AT


def test_application_record_identities_timestamp_and_result_preserved() -> None:
    scenario = _outcome()
    repository = RecordingRepository()

    record = record_task_execution_lifecycle_outcome_durably(
        uuid4(), RECORDED_AT, scenario["outcome"], repository=repository
    )

    embedded_record = record.outcome.application_record

    assert (
        embedded_record.application_record_id
        == scenario["application_record"].application_record_id
    )
    assert embedded_record.recorded_at == APPLICATION_RECORDED_AT
    assert embedded_record.recorded_at.utcoffset() == timedelta(hours=-3, minutes=30)
    assert embedded_record.result == scenario["result"]
    assert embedded_record.result.new_status is EntityStatus.COMPLETED


def test_repository_add_called_exactly_once_with_exact_returned_record() -> None:
    outcome = _outcome()["outcome"]

    calls: list[TaskExecutionLifecycleOutcomeRecord] = []

    class CountingRepository:
        def add(self, record: TaskExecutionLifecycleOutcomeRecord) -> None:
            calls.append(record)

        def list_history(
            self, portfolio_id: UUID
        ) -> tuple[TaskExecutionLifecycleOutcomeRecord, ...]:
            return tuple(calls)

    repository = CountingRepository()
    returned = record_task_execution_lifecycle_outcome_durably(
        uuid4(),
        RECORDED_AT,
        outcome,
        repository=repository,
    )

    assert len(calls) == 1
    assert calls[0] is returned


def test_list_history_returns_exact_appended_history() -> None:
    first_scenario = _outcome()
    other_scenario = _outcome()
    repository = RecordingRepository()

    first = record_task_execution_lifecycle_outcome_durably(
        uuid4(), RECORDED_AT, first_scenario["outcome"], repository=repository
    )
    second = record_task_execution_lifecycle_outcome_durably(
        uuid4(),
        RECORDED_AT,
        other_scenario["outcome"],
        repository=repository,
    )

    assert repository.list_history(first_scenario["portfolio"].id) == (first,)
    assert repository.list_history(other_scenario["portfolio"].id) == (second,)


# -- append-only semantics ---------------------------------------------------


def test_equal_outcomes_under_different_record_ids_are_two_separate_appends(
) -> None:
    outcome = _outcome()["outcome"]
    repository = RecordingRepository()

    first = record_task_execution_lifecycle_outcome_durably(
        uuid4(), RECORDED_AT, outcome, repository=repository
    )
    second = record_task_execution_lifecycle_outcome_durably(
        uuid4(), RECORDED_AT, outcome, repository=repository
    )
    same_identity = record_task_execution_lifecycle_outcome_durably(
        first.outcome_record_id, RECORDED_AT, outcome, repository=repository
    )

    assert len(repository.added) == 3
    assert repository.added[0] is first
    assert repository.added[1] is second
    assert repository.added[2] is same_identity
    assert repository.added[0].outcome == repository.added[1].outcome
    assert (
        repository.added[0].outcome_record_id
        == repository.added[2].outcome_record_id
    )


# -- strict input validation (all before repository interaction) ------------


@pytest.mark.parametrize("bad", [None, "id", 1, b"id", str(uuid4())])
def test_outcome_record_id_must_be_genuine_uuid(bad: object) -> None:
    repository = RecordingRepository()
    outcome = _outcome()["outcome"]

    with pytest.raises(DurableTaskExecutionLifecycleOutcomeError, match="UUID"):
        record_task_execution_lifecycle_outcome_durably(
            bad,
            RECORDED_AT,
            outcome,
            repository=repository,
        )

    assert repository.added == []


@pytest.mark.parametrize("bad", [None, "2026-09-08T08:00:00+00:00", 1])
def test_recorded_at_must_be_genuine_datetime(bad: object) -> None:
    repository = RecordingRepository()
    outcome = _outcome()["outcome"]

    with pytest.raises(DurableTaskExecutionLifecycleOutcomeError, match="datetime"):
        record_task_execution_lifecycle_outcome_durably(
            uuid4(),
            bad,
            outcome,
            repository=repository,
        )

    assert repository.added == []


def test_recorded_at_must_be_aware() -> None:
    repository = RecordingRepository()
    outcome = _outcome()["outcome"]

    with pytest.raises(
        DurableTaskExecutionLifecycleOutcomeError, match="timezone-aware"
    ):
        record_task_execution_lifecycle_outcome_durably(
            uuid4(),
            datetime(2026, 9, 8, 8, 0),
            outcome,
            repository=repository,
        )

    assert repository.added == []


def test_outcome_must_be_genuine_v160_outcome() -> None:
    repository = RecordingRepository()
    scenario = _outcome()

    foreign: list[object] = [
        None,
        {},
        "outcome",
        scenario["decision"],
        scenario["admission"],
        scenario["admission_record"],
        scenario["decision_record"],
        scenario["result"],
        scenario["application_record"],
        object(),
    ]

    for bad in foreign:
        with pytest.raises(
            DurableTaskExecutionLifecycleOutcomeError,
            match="TaskExecutionLifecycleOutcome",
        ):
            record_task_execution_lifecycle_outcome_durably(
                uuid4(),
                RECORDED_AT,
                bad,  # type: ignore[arg-type]
                repository=repository,
            )

    assert repository.added == []


# -- fresh complete strict revalidation -------------------------------------
#
# Every hostile case below is a genuine ``TaskExecutionLifecycleOutcome``
# instance passed to the durable boundary, but built via ``model_construct``
# so that exactly one canonical nested component carries invalid state.
# Fresh COMPLETE strict revalidation must reject them all before any
# repository interaction.


def test_outcome_component_supplied_as_invalid_strict_dict_rejected() -> None:
    """An embedded canonical component supplied as a dict carrying an
    invalid strict value (string where UUID is required) must be
    rejected by fresh COMPLETE strict re-validation before any
    repository interaction."""

    repository = RecordingRepository()
    outcome = _outcome()["outcome"]
    payload: dict[Any, Any] = outcome.model_dump(mode="python")
    payload["admission_record"] = {
        **outcome.admission_record.model_dump(mode="python"),
        "admission_record_id": "not-a-uuid",
    }
    hostile = TaskExecutionLifecycleOutcome.model_construct(**payload)

    with pytest.raises(
        DurableTaskExecutionLifecycleOutcomeError,
        match="strict re-validation",
    ):
        record_task_execution_lifecycle_outcome_durably(
            uuid4(),
            RECORDED_AT,
            hostile,
            repository=repository,
        )

    assert repository.added == []


def test_outcome_component_supplied_as_valid_dict_is_canonicalized() -> None:
    """Pydantic strict mode legitimately coerces model-dicts: a dict
    carrying the EXACT genuine record state must be accepted and
    retained as a fresh CANONICAL record instance (never as the raw
    dict)."""

    repository = RecordingRepository()
    outcome = _outcome()["outcome"]
    payload: dict[Any, Any] = outcome.model_dump(mode="python")
    payload["admission_record"] = outcome.admission_record.model_dump(mode="python")
    hostile = TaskExecutionLifecycleOutcome.model_construct(**payload)

    record = record_task_execution_lifecycle_outcome_durably(
        uuid4(),
        RECORDED_AT,
        hostile,
        repository=repository,
    )

    assert len(repository.added) == 1
    assert type(record.outcome.admission_record) is type(outcome.admission_record)
    assert record.outcome == outcome


def test_hostile_nested_admission_state_rejected() -> None:
    """The embedded V1.51 admission carrying hostile historical
    ``current_task_status == COMPLETED`` is rejected before any
    repository interaction."""

    repository = RecordingRepository()
    scenario = _outcome()
    hostile_admission = TaskExecutionLifecycleAdmission.model_construct(
        **{
            **scenario["admission"].model_dump(mode="python"),
            "current_task_status": EntityStatus.COMPLETED,
        }
    )
    hostile = _hostile_outcome(
        admission_record=TaskExecutionLifecycleAdmissionRecord.model_construct(
            admission_record_id=scenario["admission_record"].admission_record_id,
            recorded_at=scenario["admission_record"].recorded_at,
            admission=hostile_admission,
        )
    )

    with pytest.raises(
        DurableTaskExecutionLifecycleOutcomeError,
        match="strict re-validation",
    ):
        record_task_execution_lifecycle_outcome_durably(
            uuid4(),
            RECORDED_AT,
            hostile,
            repository=repository,
        )

    assert repository.added == []


def test_hostile_nested_decision_state_rejected() -> None:
    """The embedded V1.50 decision carrying an invalid disposition literal
    is rejected before any repository interaction."""

    repository = RecordingRepository()
    scenario = _outcome()
    hostile_decision = TaskExecutionLifecycleDecision.model_construct(
        **{
            **scenario["decision"].model_dump(mode="python"),
            "disposition": "not-a-disposition",
        }
    )
    hostile = _hostile_outcome(
        decision_record=TaskExecutionLifecycleDecisionRecord.model_construct(
            decision_record_id=scenario["decision_record"].decision_record_id,
            recorded_at=scenario["decision_record"].recorded_at,
            decision=hostile_decision,
        )
    )

    with pytest.raises(
        DurableTaskExecutionLifecycleOutcomeError,
        match="strict re-validation",
    ):
        record_task_execution_lifecycle_outcome_durably(
            uuid4(),
            RECORDED_AT,
            hostile,
            repository=repository,
        )

    assert repository.added == []


def test_hostile_nested_transition_and_application_state_rejected() -> None:
    """A V1.52 transition result whose embedded portfolio snapshot does
    NOT carry the COMPLETED target task violates the V1.53
    application-record invariants and is rejected before any repository
    interaction."""

    repository = RecordingRepository()
    scenario = _outcome()
    hostile_result = EntityStatusTransitionResult.model_construct(
        **{
            **scenario["result"].model_dump(mode="python"),
            "portfolio": scenario["portfolio"],  # pre-transition snapshot
        }
    )
    hostile = _hostile_outcome(
        transition_result=hostile_result,
        application_record=TaskExecutionLifecycleApplicationRecord.model_construct(
            application_record_id=(
                scenario["application_record"].application_record_id
            ),
            recorded_at=scenario["application_record"].recorded_at,
            result=hostile_result,
        ),
    )

    with pytest.raises(
        DurableTaskExecutionLifecycleOutcomeError,
        match="strict re-validation",
    ):
        record_task_execution_lifecycle_outcome_durably(
            uuid4(),
            RECORDED_AT,
            hostile,
            repository=repository,
        )

    assert repository.added == []


def test_record_direct_construction_enforces_fresh_nested_validity() -> None:
    """Direct construction of the public record MUST also reject hostile
    nested outcome states and naive inner timestamps."""

    scenario = _outcome()
    hostile_admission = TaskExecutionLifecycleAdmission.model_construct(
        **{
            **scenario["admission"].model_dump(mode="python"),
            "execution_succeeded": False,
        }
    )
    hostile = _hostile_outcome(
        admission_record=TaskExecutionLifecycleAdmissionRecord.model_construct(
            admission_record_id=scenario["admission_record"].admission_record_id,
            recorded_at=scenario["admission_record"].recorded_at,
            admission=hostile_admission,
        )
    )

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleOutcomeRecord(
            outcome_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            outcome=hostile,
        )

    naive_record = _hostile_outcome(
        decision_record=TaskExecutionLifecycleDecisionRecord.model_construct(
            decision_record_id=scenario["decision_record"].decision_record_id,
            recorded_at=datetime(2026, 9, 8, 8, 0),  # naive
            decision=scenario["decision"],
        )
    )

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleOutcomeRecord(
            outcome_record_id=uuid4(),
            recorded_at=RECORDED_AT,
            outcome=naive_record,
        )


def test_semantic_reads_after_revalidation_use_only_fresh_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _outcome()["outcome"]
    original_a_record_id = outcome.admission_record.admission_record_id
    replacement_a_record_id = uuid4()
    repository = RecordingRepository()
    original_model_dump = TaskExecutionLifecycleOutcome.model_dump

    def dump_then_corrupt(
        self: TaskExecutionLifecycleOutcome,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        payload = original_model_dump(self, *args, **kwargs)
        if self is outcome:
            object.__setattr__(
                self,
                "admission_record",
                self.admission_record.model_copy(
                    update={"admission_record_id": replacement_a_record_id}
                ),
            )
        return payload

    monkeypatch.setattr(TaskExecutionLifecycleOutcome, "model_dump", dump_then_corrupt)

    record = record_task_execution_lifecycle_outcome_durably(
        uuid4(),
        RECORDED_AT,
        outcome,
        repository=repository,
    )

    assert outcome.admission_record.admission_record_id == replacement_a_record_id
    assert (
        record.outcome.admission_record.admission_record_id == original_a_record_id
    )
    assert (
        repository.added[0].outcome.admission_record.admission_record_id
        == original_a_record_id
    )


# -- repository failure propagation -----------------------------------------


def test_repository_exception_propagates_unchanged() -> None:
    marker = RuntimeError("storage failed")
    outcome = _outcome()["outcome"]

    class ExplodingRepository(RecordingRepository):
        def add(self, record: TaskExecutionLifecycleOutcomeRecord) -> None:
            raise marker

    with pytest.raises(RuntimeError) as exc_info:
        record_task_execution_lifecycle_outcome_durably(
            uuid4(),
            RECORDED_AT,
            outcome,
            repository=ExplodingRepository(),
        )

    assert exc_info.value is marker


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
    tree = ast.parse(inspect.getsource(outcome_history_module))
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
    imported_roots = {module.split(".")[0] for module in imported}

    assert forbidden_roots.isdisjoint(imported_roots)
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
    tree = ast.parse(inspect.getsource(outcome_history_module))
    forbidden_attrs = {"uuid1", "uuid4", "now", "utcnow"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs


def test_public_module_never_replays_lifecycle_or_calls_boundaries() -> None:
    tree = ast.parse(inspect.getsource(outcome_history_module))
    forbidden_calls = {
        "decide_task_execution_lifecycle",
        "admit_current_task_execution_lifecycle",
        "apply_admitted_task_execution_lifecycle_durably",
        "coordinate_task_execution_lifecycle",
        "record_task_execution_lifecycle_admission_durably",
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
    assert "PortfolioRepository" not in vars(outcome_history_module)
    assert (
        "trajectory_os.application.work_breakdown_acceptance"
        not in _imported_modules(tree)
    )


def test_public_module_exposes_no_current_or_supersession_apis() -> None:
    forbidden_tokens = {"latest", "effective", "supersede", "revoke", "idempot"}
    for name in vars(outcome_history_module):
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden_tokens)


def test_repository_protocol_exposes_only_add_and_list_history() -> None:
    repository_protocol = (
        outcome_history_module.TaskExecutionLifecycleOutcomeRepository
    )
    methods = {
        name
        for name, value in repository_protocol.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert methods == {"add", "list_history"}
