"""V1.51 — CURRENT-state admission of one COMPLETE_TASK lifecycle decision.

Focused unit tests plus executable (AST) architecture guards for
``execution_effort_project_focus_next_ready_task_execution_lifecycle_admission``.

The module is pure and deterministic and MUST STOP BEFORE MUTATION: it never
mutates a Portfolio or entity status, never calls a status-mutation use
case, never persists, never executes, never invokes a provider / runtime /
agent, never generates a UUID or reads the wall clock, and never inspects,
ranks, or substitutes any alternative target identity.
"""

from __future__ import annotations

import ast
import enum
import inspect
import itertools
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

import trajectory_os.application as application_package
import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_admission as lifecycle_admission_module  # noqa: E501
from trajectory_os.application import (
    TaskExecutionLifecycleAdmission,
    TaskExecutionLifecycleAdmissionError,
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDisposition,
    TaskExecutionResult,
    TaskExecutionResultRecord,
    admit_current_task_execution_lifecycle,
    decide_task_execution_lifecycle,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

DECIDED_AT = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
RECORDED_AT = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)

PUBLIC_SYMBOLS = (
    "TaskExecutionLifecycleAdmission",
    "TaskExecutionLifecycleAdmissionError",
    "admit_current_task_execution_lifecycle",
)

_UUID_SEQUENCE = itertools.count(1)


def _uuid() -> UUID:
    """Deterministic sequential test identity (test-only helper)."""
    return UUID(int=next(_UUID_SEQUENCE))


def _project_entity(entity_id: UUID | None = None) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id or _uuid(),
        entity_type=EntityType.PROJECT,
        title="project",
    )


def _task_entity(
    entity_id: UUID | None = None,
    status: EntityStatus = EntityStatus.ACTIVE,
) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id or _uuid(),
        entity_type=EntityType.TASK,
        title="task",
        status=status,
    )


def _membership(task_id: UUID, project_id: UUID) -> TrajectoryRelation:
    """One canonical membership row under the existing repository
    orientation: ``source_id`` = child (task), ``target_id`` = parent.
    """
    return TrajectoryRelation(
        id=_uuid(),
        source_id=task_id,
        target_id=project_id,
        relation_type=RelationType.BELONGS_TO,
    )


def _bound_decision_and_portfolio(
    *,
    task_status: EntityStatus = EntityStatus.ACTIVE,
    disposition: TaskExecutionLifecycleDisposition = (
        TaskExecutionLifecycleDisposition.COMPLETE_TASK
    ),
    succeeded: bool = True,
    portfolio_id: UUID | None = None,
) -> tuple[TaskExecutionLifecycleDecision, Portfolio]:
    """Build one genuine V1.50 decision whose identity triple exactly
    matches a CURRENT Portfolio containing the target task with its
    exact canonical ``BELONGS_TO`` membership to the target project."""

    project = _project_entity()
    task = _task_entity(status=task_status)
    portfolio = Portfolio(
        id=portfolio_id or _uuid(),
        name="unit",
        entities=[project, task],
        relations=[_membership(task.id, project.id)],
    )
    result = TaskExecutionResult(
        request_id=_uuid(),
        intent_id=_uuid(),
        decision_id=_uuid(),
        portfolio_id=portfolio.id,
        authorized_project_id=project.id,
        authorized_task_id=task.id,
        succeeded=succeeded,
    )
    record = TaskExecutionResultRecord(
        execution_record_id=_uuid(),
        recorded_at=RECORDED_AT,
        result=result,
    )
    decision = decide_task_execution_lifecycle(_uuid(), DECIDED_AT, record, disposition)
    return decision, portfolio


# ---------------------------------------------------------------------------
# Exact public symbols / exports
# ---------------------------------------------------------------------------


def test_module_exports_exactly_the_public_symbols() -> None:
    assert tuple(lifecycle_admission_module.__all__) == PUBLIC_SYMBOLS


def test_application_package_exports_the_public_symbols() -> None:
    for name in PUBLIC_SYMBOLS:
        assert name in application_package.__all__
        assert getattr(application_package, name) is not None
    assert (
        application_package.admit_current_task_execution_lifecycle
        is lifecycle_admission_module.admit_current_task_execution_lifecycle
    )


def test_error_type_is_a_value_error() -> None:
    assert issubclass(TaskExecutionLifecycleAdmissionError, ValueError)


# ---------------------------------------------------------------------------
# Admission model strictness / frozen / extra-forbid / invariants
# ---------------------------------------------------------------------------


def _admission_fields(
    *,
    succeeded: bool = True,
    disposition: TaskExecutionLifecycleDisposition = (
        TaskExecutionLifecycleDisposition.COMPLETE_TASK
    ),
    current_task_status: EntityStatus = EntityStatus.ACTIVE,
) -> dict[str, Any]:
    return dict(
        lifecycle_decision_id=_uuid(),
        decided_at=DECIDED_AT,
        execution_record_id=_uuid(),
        execution_recorded_at=RECORDED_AT,
        request_id=_uuid(),
        intent_id=_uuid(),
        execution_decision_id=_uuid(),
        portfolio_id=_uuid(),
        authorized_project_id=_uuid(),
        authorized_task_id=_uuid(),
        execution_succeeded=succeeded,
        disposition=disposition,
        current_task_status=current_task_status,
    )


def test_admission_model_exact_fields_and_order() -> None:
    assert tuple(TaskExecutionLifecycleAdmission.model_fields) == (
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


def test_admission_model_is_strict_frozen_and_extra_forbid() -> None:
    config = TaskExecutionLifecycleAdmission.model_config
    assert config["strict"] is True
    assert config["frozen"] is True
    assert config["extra"] == "forbid"


def test_admission_model_fields_have_no_defaults() -> None:
    for name, field in TaskExecutionLifecycleAdmission.model_fields.items():
        assert field.is_required(), f"field {name} must have no default"


def test_admission_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmission(**_admission_fields(), retry_ok=True)  # type: ignore[call-arg]


def test_admission_model_rejects_missing_field() -> None:
    payload = _admission_fields()
    del payload["current_task_status"]
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmission(**payload)  # type: ignore[call-arg]


def test_admission_model_strict_rejects_string_uuid() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmission(
            **{**_admission_fields(), "lifecycle_decision_id": str(_uuid())}  # type: ignore[dict-item]
        )


def test_admission_model_strict_rejects_string_disposition() -> None:
    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmission(
            **{**_admission_fields(), "disposition": "COMPLETE_TASK"}  # type: ignore[dict-item]
        )


def test_admission_model_rejects_foreign_disposition_enum_member() -> None:
    class ForeignDisposition(enum.StrEnum):
        COMPLETE_TASK = "COMPLETE_TASK"

    with pytest.raises(ValidationError):
        TaskExecutionLifecycleAdmission(
            **{  # type: ignore[dict-item]
                **_admission_fields(),
                "disposition": ForeignDisposition.COMPLETE_TASK,
            }
        )


def test_admission_model_is_frozen() -> None:
    admission = TaskExecutionLifecycleAdmission(**_admission_fields())
    with pytest.raises(ValidationError):
        admission.portfolio_id = _uuid()  # type: ignore[misc]


def test_admission_model_invariant_rejects_non_complete_task_disposition() -> None:
    with pytest.raises(ValidationError, match="COMPLETE_TASK disposition"):
        TaskExecutionLifecycleAdmission(
            **_admission_fields(disposition=TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE)
        )


def test_admission_model_invariant_rejects_failed_execution_outcome() -> None:
    with pytest.raises(ValidationError, match="exactly True"):
        TaskExecutionLifecycleAdmission(**_admission_fields(succeeded=False))


def test_admission_model_invariant_rejects_already_completed_status() -> None:
    with pytest.raises(ValidationError, match="already-COMPLETED"):
        TaskExecutionLifecycleAdmission(
            **_admission_fields(current_task_status=EntityStatus.COMPLETED)
        )


@pytest.mark.parametrize("field", ["decided_at", "execution_recorded_at"])
def test_admission_model_invariant_rejects_naive_timestamp(field: str) -> None:
    naive = DECIDED_AT.replace(tzinfo=None)
    with pytest.raises(
        ValidationError,
        match="timezone-aware datetime with a non-None UTC offset",
    ):
        TaskExecutionLifecycleAdmission(
            **{**_admission_fields(), field: naive}  # type: ignore[dict-item]
        )


# ---------------------------------------------------------------------------
# Genuine V1.50 decision requirement
# ---------------------------------------------------------------------------


class _ForeignDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    lifecycle_decision_id: UUID

    def model_dump(  # noqa: D102 - duck-typed foreign surface
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return {"lifecycle_decision_id": self.lifecycle_decision_id}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        "decision",
        42,
        object(),
        _ForeignDecision(lifecycle_decision_id=UUID(int=1)),
        TaskExecutionResultRecord(
            execution_record_id=UUID(int=2),
            recorded_at=RECORDED_AT,
            result=TaskExecutionResult(
                request_id=UUID(int=3),
                intent_id=UUID(int=4),
                decision_id=UUID(int=5),
                portfolio_id=UUID(int=6),
                authorized_project_id=UUID(int=7),
                authorized_task_id=UUID(int=8),
                succeeded=True,
            ),
        ),
    ],
)
def test_decision_must_be_genuine_v150(bad: object) -> None:
    _, portfolio = _bound_decision_and_portfolio()
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="genuine V1.50 TaskExecutionLifecycleDecision",
    ):
        admit_current_task_execution_lifecycle(bad, portfolio)  # type: ignore[arg-type]


def _hostile_constructed_decision(
    **overrides: object,
) -> TaskExecutionLifecycleDecision:
    base = _admission_fields()
    base.update(overrides)
    return TaskExecutionLifecycleDecision.model_construct(**base)  # type: ignore[arg-type]


def test_hostile_constructed_v150_with_string_id_freshly_revalidated() -> None:
    hostile = _hostile_constructed_decision(lifecycle_decision_id=str(_uuid()))
    _, portfolio = _bound_decision_and_portfolio()
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="strict re-validation",
    ):
        admit_current_task_execution_lifecycle(hostile, portfolio)


def test_hostile_constructed_v150_with_naive_timestamp_rejected() -> None:
    hostile = _hostile_constructed_decision(decided_at=DECIDED_AT.replace(tzinfo=None))
    _, portfolio = _bound_decision_and_portfolio()
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="strict re-validation",
    ):
        admit_current_task_execution_lifecycle(hostile, portfolio)


def test_hostile_constructed_v150_with_failed_outcome_rejected() -> None:
    # COMPLETE_TASK + execution_succeeded=False fails the V1.50 decision
    # invariant, so it must fail before any V1.51 semantic check runs.
    hostile = _hostile_constructed_decision(execution_succeeded=False)
    _, portfolio = _bound_decision_and_portfolio()
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="strict re-validation",
    ):
        admit_current_task_execution_lifecycle(hostile, portfolio)


def test_hostile_completed_current_status_cannot_survive_admission_validation() -> None:
    hostile = TaskExecutionLifecycleAdmission.model_construct(
        **_admission_fields(current_task_status=EntityStatus.COMPLETED)
    )
    with pytest.raises(ValidationError, match="already-COMPLETED"):
        TaskExecutionLifecycleAdmission.model_validate(
            hostile.model_dump(mode="python"),
            strict=True,
        )


def test_no_semantic_reads_from_caller_owned_decision_after_revalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutating the caller-owned decision during the boundary call must not
    change the semantic value admitted: only the fresh validated copy is
    read for semantics."""

    decision, portfolio = _bound_decision_and_portfolio()
    original_model_dump = TaskExecutionLifecycleDecision.model_dump

    def dump_then_corrupt(
        self: TaskExecutionLifecycleDecision,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        payload = original_model_dump(self, *args, **kwargs)
        object.__setattr__(
            self,
            "disposition",
            TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE,
        )
        return payload

    monkeypatch.setattr(TaskExecutionLifecycleDecision, "model_dump", dump_then_corrupt)

    admission = admit_current_task_execution_lifecycle(decision, portfolio)

    assert admission.disposition is TaskExecutionLifecycleDisposition.COMPLETE_TASK
    assert admission.execution_succeeded is True


# ---------------------------------------------------------------------------
# Disposition and execution-outcome checks
# ---------------------------------------------------------------------------


def test_no_lifecycle_change_disposition_rejected() -> None:
    decision, portfolio = _bound_decision_and_portfolio(
        disposition=TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE
    )
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="COMPLETE_TASK disposition",
    ):
        admit_current_task_execution_lifecycle(decision, portfolio)


def test_contradictory_failed_complete_evidence_cannot_admit() -> None:
    # A genuine COMPLETE_TASK decision over a failed execution cannot be
    # built in the first place: the V1.50 authority already forbids it.
    with pytest.raises(Exception, match="exactly True"):
        decision, portfolio = _bound_decision_and_portfolio(
            disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
            succeeded=False,
        )
        _ = (decision, portfolio)
    # The V1.51 admission value enforces the same invariant on its own.
    with pytest.raises(ValidationError, match="exactly True"):
        TaskExecutionLifecycleAdmission(**_admission_fields(succeeded=False))


# ---------------------------------------------------------------------------
# Portfolio / target identity resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [None, {}, "portfolio", 42, object(), Portfolio],
)
def test_portfolio_must_be_genuine_current_portfolio(bad: object) -> None:
    decision, _ = _bound_decision_and_portfolio()
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="genuine CURRENT Portfolio",
    ):
        admit_current_task_execution_lifecycle(decision, bad)  # type: ignore[arg-type]


def test_exact_portfolio_id_mismatch_rejected() -> None:
    decision, _ = _bound_decision_and_portfolio()
    other = Portfolio(id=_uuid(), name="other", entities=[])
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="does not exactly match",
    ):
        admit_current_task_execution_lifecycle(decision, other)


def test_missing_project_rejected() -> None:
    decision, _ = _bound_decision_and_portfolio()
    task_only = Portfolio(
        id=decision.portfolio_id,
        name="unit",
        entities=[_task_entity(entity_id=decision.authorized_task_id)],
    )
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="authorized project is missing",
    ):
        admit_current_task_execution_lifecycle(decision, task_only)


def test_missing_task_rejected() -> None:
    decision, _ = _bound_decision_and_portfolio()
    project_only = Portfolio(
        id=decision.portfolio_id,
        name="unit",
        entities=[_project_entity(entity_id=decision.authorized_project_id)],
    )
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="authorized task is missing",
    ):
        admit_current_task_execution_lifecycle(decision, project_only)


def test_mismatched_project_identity_rejected() -> None:
    decision, _ = _bound_decision_and_portfolio()
    # The authorized project identity is a TASK-typed entity in CURRENT.
    task_typed_slot = Portfolio(
        id=decision.portfolio_id,
        name="unit",
        entities=[
            TrajectoryEntity(
                id=decision.authorized_project_id,
                entity_type=EntityType.TASK,
                title="task-typed-project-slot",
            ),
            _task_entity(entity_id=decision.authorized_task_id),
        ],
    )
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="PROJECT entity type",
    ):
        admit_current_task_execution_lifecycle(decision, task_typed_slot)


def test_mismatched_task_identity_rejected() -> None:
    decision, _ = _bound_decision_and_portfolio()
    # The authorized task identity is a PROJECT-typed entity in CURRENT.
    project_typed_slot = Portfolio(
        id=decision.portfolio_id,
        name="unit",
        entities=[
            _project_entity(entity_id=decision.authorized_project_id),
            TrajectoryEntity(
                id=decision.authorized_task_id,
                entity_type=EntityType.PROJECT,
                title="project-typed-task-slot",
            ),
        ],
    )
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="TASK entity type",
    ):
        admit_current_task_execution_lifecycle(decision, project_typed_slot)


# ---------------------------------------------------------------------------
# CURRENT project -> task membership (canonical BELONGS_TO semantics)
# ---------------------------------------------------------------------------


def test_exact_correct_membership_relation_accepted() -> None:
    # The exact authorized task carries exactly one BELONGS_TO row to the
    # exact authorized project: this is the repository membership
    # semantics V1.51 requires, and admission must succeed on it.
    decision, portfolio = _bound_decision_and_portfolio()
    assert any(
        relation.relation_type is RelationType.BELONGS_TO
        and relation.source_id == decision.authorized_task_id
        and relation.target_id == decision.authorized_project_id
        for relation in portfolio.relations
    )
    admission = admit_current_task_execution_lifecycle(decision, portfolio)
    assert admission.authorized_project_id is decision.authorized_project_id
    assert admission.authorized_task_id is decision.authorized_task_id


def test_task_exists_but_belongs_to_another_project_rejected() -> None:
    # Both authorized identities exist with the right types, but the
    # task's only membership row points at a DIFFERENT project: the
    # exact authorized project/task pairing must be rejected.
    decision, _ = _bound_decision_and_portfolio()
    other_project = _project_entity()
    task = _task_entity(entity_id=decision.authorized_task_id)
    mismatched = Portfolio(
        id=decision.portfolio_id,
        name="unit",
        entities=[
            _project_entity(entity_id=decision.authorized_project_id),
            other_project,
            task,
        ],
        relations=[_membership(task.id, other_project.id)],
    )
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="BELONGS_TO",
    ):
        admit_current_task_execution_lifecycle(decision, mismatched)


def test_project_exists_task_exists_membership_relation_missing_rejected() -> None:
    # Both authorized identities exist with the right types, but no
    # membership relation of any kind exists: rejected, no inference.
    decision, _ = _bound_decision_and_portfolio()
    unlinked = Portfolio(
        id=decision.portfolio_id,
        name="unit",
        entities=[
            _project_entity(entity_id=decision.authorized_project_id),
            _task_entity(entity_id=decision.authorized_task_id),
        ],
        relations=[],
    )
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="BELONGS_TO",
    ):
        admit_current_task_execution_lifecycle(decision, unlinked)


def test_reversed_direction_belongs_to_row_not_accepted() -> None:
    # source_id = project, target_id = task is the WRONG direction for
    # the repository membership orientation (child -> parent) and must
    # not establish membership.
    decision, _ = _bound_decision_and_portfolio()
    reversed_row = Portfolio(
        id=decision.portfolio_id,
        name="unit",
        entities=[
            _project_entity(entity_id=decision.authorized_project_id),
            _task_entity(entity_id=decision.authorized_task_id),
        ],
        relations=[
            TrajectoryRelation(
                id=_uuid(),
                source_id=decision.authorized_project_id,
                target_id=decision.authorized_task_id,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="BELONGS_TO",
    ):
        admit_current_task_execution_lifecycle(decision, reversed_row)


def test_other_relation_type_between_exact_identities_not_accepted() -> None:
    # A non-membership relation row between the exact identities is not
    # membership semantics and must not be reinterpreted as one.
    decision, _ = _bound_decision_and_portfolio()
    unrelated_row = Portfolio(
        id=decision.portfolio_id,
        name="unit",
        entities=[
            _project_entity(entity_id=decision.authorized_project_id),
            _task_entity(entity_id=decision.authorized_task_id),
        ],
        relations=[
            TrajectoryRelation(
                id=_uuid(),
                source_id=decision.authorized_task_id,
                target_id=decision.authorized_project_id,
                relation_type=RelationType.RELATED_TO,
            )
        ],
    )
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="BELONGS_TO",
    ):
        admit_current_task_execution_lifecycle(decision, unrelated_row)


# ---------------------------------------------------------------------------
# Status semantics: only already-COMPLETED is rejected
# ---------------------------------------------------------------------------


def test_already_completed_task_rejected() -> None:
    decision, portfolio = _bound_decision_and_portfolio(task_status=EntityStatus.COMPLETED)
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="already carries EntityStatus.COMPLETED",
    ):
        admit_current_task_execution_lifecycle(decision, portfolio)


@pytest.mark.parametrize(
    "status",
    [
        EntityStatus.ACTIVE,
        EntityStatus.WAITING,
        EntityStatus.PAUSED,
        EntityStatus.INCUBATOR,
        EntityStatus.SOMEDAY,
        # The existing repository status-mutation authority forbids only
        # same-status changes out of COMPLETED. It makes no claim that
        # forbids a change out of CANCELLED or out of ARCHIVED, so V1.51
        # invents no lifecycle graph and rejects no other status.
        EntityStatus.CANCELLED,
        EntityStatus.ARCHIVED,
    ],
)
def test_every_other_current_status_is_admissible(status: EntityStatus) -> None:
    decision, portfolio = _bound_decision_and_portfolio(task_status=status)
    admission = admit_current_task_execution_lifecycle(decision, portfolio)
    assert admission.current_task_status is status
    assert admission.disposition is TaskExecutionLifecycleDisposition.COMPLETE_TASK
    assert admission.execution_succeeded is True


def test_no_failed_status_exists_in_the_domain() -> None:
    assert not any(status.name == "FAILED" for status in EntityStatus)


# ---------------------------------------------------------------------------
# No fallback / reselection
# ---------------------------------------------------------------------------


def test_no_fallback_to_another_eligible_task() -> None:
    # The exact authorized task is already COMPLETED, yet a perfectly
    # eligible, ACTIVE, same-project task exists in CURRENT. V1.51 must
    # still reject: no alternative task is ever inspected or substituted.
    project = _project_entity()
    completed = _task_entity(status=EntityStatus.COMPLETED)
    other_eligible = _task_entity(status=EntityStatus.ACTIVE)
    portfolio = Portfolio(
        id=_uuid(),
        name="unit",
        entities=[project, completed, other_eligible],
        relations=[_membership(completed.id, project.id)],
    )
    result = TaskExecutionResult(
        request_id=_uuid(),
        intent_id=_uuid(),
        decision_id=_uuid(),
        portfolio_id=portfolio.id,
        authorized_project_id=project.id,
        authorized_task_id=completed.id,
        succeeded=True,
    )
    record = TaskExecutionResultRecord(
        execution_record_id=_uuid(),
        recorded_at=RECORDED_AT,
        result=result,
    )
    decision = decide_task_execution_lifecycle(
        _uuid(),
        DECIDED_AT,
        record,
        TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )

    with pytest.raises(
        TaskExecutionLifecycleAdmissionError,
        match="already carries EntityStatus.COMPLETED",
    ):
        admit_current_task_execution_lifecycle(decision, portfolio)
    assert other_eligible.status is EntityStatus.ACTIVE


# ---------------------------------------------------------------------------
# Provenance / determinism / no mutation
# ---------------------------------------------------------------------------


def test_provenance_is_projected_exactly_from_the_revalidated_decision() -> None:
    decision, portfolio = _bound_decision_and_portfolio()
    admission = admit_current_task_execution_lifecycle(decision, portfolio)

    assert admission.lifecycle_decision_id == decision.lifecycle_decision_id
    assert admission.decided_at == decision.decided_at
    assert admission.execution_record_id == decision.execution_record_id
    assert admission.execution_recorded_at == decision.execution_recorded_at
    assert admission.request_id == decision.request_id
    assert admission.intent_id == decision.intent_id
    assert admission.execution_decision_id == decision.execution_decision_id
    assert admission.portfolio_id == decision.portfolio_id
    assert admission.authorized_project_id == decision.authorized_project_id
    assert admission.authorized_task_id == decision.authorized_task_id
    assert admission.execution_succeeded is True
    assert admission.disposition is TaskExecutionLifecycleDisposition.COMPLETE_TASK


def test_admission_cannot_carry_any_new_identity_or_timestamp() -> None:
    decision, portfolio = _bound_decision_and_portfolio()
    admission = admit_current_task_execution_lifecycle(decision, portfolio)
    admission_uuids = {
        admission.lifecycle_decision_id,
        admission.execution_record_id,
        admission.request_id,
        admission.intent_id,
        admission.execution_decision_id,
        admission.portfolio_id,
        admission.authorized_project_id,
        admission.authorized_task_id,
    }
    decision_uuids = {
        decision.lifecycle_decision_id,
        decision.execution_record_id,
        decision.request_id,
        decision.intent_id,
        decision.execution_decision_id,
        decision.portfolio_id,
        decision.authorized_project_id,
        decision.authorized_task_id,
    }
    assert admission_uuids <= decision_uuids
    assert admission.decided_at == decision.decided_at
    assert admission.decided_at.tzinfo is not None
    assert admission.decided_at.utcoffset() is not None
    assert admission.execution_recorded_at == RECORDED_AT


def test_deterministic_same_input_equality() -> None:
    decision, portfolio = _bound_decision_and_portfolio()
    first = admit_current_task_execution_lifecycle(decision, portfolio)
    second = admit_current_task_execution_lifecycle(decision, portfolio)
    assert first == second
    assert first.model_dump(mode="python") == second.model_dump(mode="python")


def test_non_utc_offset_is_preserved_verbatim() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    decided_at = datetime(2026, 1, 2, 14, 30, tzinfo=offset)

    decision, portfolio = _bound_decision_and_portfolio()
    # Rebuild the same decision with the non-UTC offset preserved.
    rebuild = TaskExecutionLifecycleDecision.model_validate(
        {
            **decision.model_dump(mode="python"),
            "decided_at": decided_at,
        },
        strict=True,
    )
    admission = admit_current_task_execution_lifecycle(rebuild, portfolio)
    assert admission.decided_at == decided_at
    assert admission.decided_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_portfolio_is_never_mutated_by_rejection() -> None:
    decision, portfolio = _bound_decision_and_portfolio(task_status=EntityStatus.COMPLETED)
    before = portfolio.model_dump(mode="python")
    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        admit_current_task_execution_lifecycle(decision, portfolio)
    assert portfolio.model_dump(mode="python") == before


def test_rejected_admission_leaks_no_module_state() -> None:
    before = {name for name in dir(lifecycle_admission_module) if not name.startswith("_")}
    decision, portfolio = _bound_decision_and_portfolio(task_status=EntityStatus.COMPLETED)
    with pytest.raises(TaskExecutionLifecycleAdmissionError):
        admit_current_task_execution_lifecycle(decision, portfolio)
    after = {name for name in dir(lifecycle_admission_module) if not name.startswith("_")}
    assert before == after


# ---------------------------------------------------------------------------
# Signature surface
# ---------------------------------------------------------------------------


def test_admit_function_exposes_exactly_two_semantic_inputs() -> None:
    params = tuple(
        inspect.signature(
            lifecycle_admission_module.admit_current_task_execution_lifecycle
        ).parameters
    )
    assert params == ("decision", "portfolio")


# ---------------------------------------------------------------------------
# Architecture guards
# ---------------------------------------------------------------------------


def test_module_imports_no_persistence_provider_runtime_or_wall_clock_modules() -> None:
    tree = ast.parse(inspect.getsource(lifecycle_admission_module))
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


def test_module_does_not_reference_forbidden_authority_or_abstraction_tokens() -> None:
    source = inspect.getsource(lifecycle_admission_module)
    for token in (
        "transition_entity_status",
        "entity_status_transition",
        "execute_current_admitted_task",
        "record_task_execution_result_durably",
        "PortfolioRepository",
        "build_work_breakdown",
        "sqlite",
    ):
        assert token not in source, f"forbidden token {token!r} found in V1.51 module"


def test_module_does_not_generate_uuid_or_read_wall_clock() -> None:
    tree = ast.parse(inspect.getsource(lifecycle_admission_module))
    forbidden_attrs = {"uuid1", "uuid4", "uuid5", "now", "utcnow"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs, (
                f"forbidden call attribute {node.func.attr!r}"
            )


def test_module_has_no_retry_idempotency_or_supersession_tokens() -> None:
    source = inspect.getsource(lifecycle_admission_module).lower()
    for token in (
        "retry",
        "idempot",
        "exactly-once",
        "exactly once",
        "latest",
        "effective",
    ):
        assert token not in source, f"forbidden token {token!r} found in V1.51 module"


def test_module_performs_no_attribute_mutation() -> None:
    source = inspect.getsource(lifecycle_admission_module)
    for token in ("setattr", ".append(", ".remove()"):
        assert token not in source, f"forbidden token {token!r} found in V1.51 module"


def test_module_namespace_has_no_repository_or_persistence_surface() -> None:
    public_names = {name for name in dir(lifecycle_admission_module) if not name.startswith("_")}
    assert not any("Repository" in name for name in public_names)
    assert not any("sqlite" in name.lower() for name in public_names)
    assert "record_task_execution_result_durably" not in public_names
