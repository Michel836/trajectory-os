"""V1.52 — durable application of one admitted COMPLETE_TASK lifecycle
transition.

Focused unit tests plus executable (AST) architecture guards for
``execution_effort_project_focus_next_ready_task_execution_lifecycle_admission_persistence``.

The module must perform EXACTLY: one genuine V1.51 admission -> fresh
COMPLETE strict re-validation -> genuine aware changed_at -> ONE
repository load -> missing-portfolio boundary error -> exact V1.50
reconstruction from the V1.51 provenance -> canonical V1.51 replay on
the EXACT loaded Portfolio -> V1.7-A ``transition_entity_status`` on
that SAME loaded Portfolio (exact authorized TASK -> COMPLETED, caller
changed_at) -> ONE save of exactly result.portfolio -> return the exact
V1.7-A result -> stop. No second load, no second save, no provider /
runtime / agent surface, no UUID generation, no wall clock, no cascade,
no re-execution.
"""

from __future__ import annotations

import ast
import inspect
import itertools
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

import trajectory_os.application as application_package
import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_admission_persistence as lifecycle_persistence_module  # noqa: E501
from trajectory_os.application import (
    DurableTaskExecutionLifecycleApplicationError,
    TaskExecutionLifecycleAdmission,
    TaskExecutionLifecycleAdmissionError,
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDisposition,
    TaskExecutionResult,
    TaskExecutionResultRecord,
    admit_current_task_execution_lifecycle,
    apply_admitted_task_execution_lifecycle_durably,
    decide_task_execution_lifecycle,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    TrajectoryEntity,
)
from trajectory_os.domain.entity_status_transition import (
    EntityStatusTransitionResult,
    StaleChangedAtError,
    transition_entity_status,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

DECIDED_AT = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
RECORDED_AT = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
BASE_TS = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
CHANGED_AT = BASE_TS + timedelta(days=1)

PUBLIC_SYMBOLS = (
    "DurableTaskExecutionLifecycleApplicationError",
    "TaskExecutionLifecyclePortfolioNotFoundError",
    "apply_admitted_task_execution_lifecycle_durably",
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
        created_at=BASE_TS,
        updated_at=BASE_TS,
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
        created_at=BASE_TS,
        updated_at=BASE_TS,
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


def _genuine_admission_and_portfolio(
    *,
    task_status: EntityStatus = EntityStatus.ACTIVE,
) -> tuple[TaskExecutionLifecycleAdmission, Portfolio]:
    """Build one genuine V1.50 decision, one CURRENT Portfolio
    containing the exact identities, and one genuine V1.51 admission
    over that CURRENT state (the historical ``current_task_status``
    evidence)."""

    project = _project_entity()
    task = _task_entity(status=task_status)
    portfolio = Portfolio(
        id=_uuid(),
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
        succeeded=True,
    )
    record = TaskExecutionResultRecord(
        execution_record_id=_uuid(),
        recorded_at=RECORDED_AT,
        result=result,
    )
    decision = decide_task_execution_lifecycle(
        _uuid(), DECIDED_AT, record, TaskExecutionLifecycleDisposition.COMPLETE_TASK
    )
    admission = admit_current_task_execution_lifecycle(decision, portfolio)
    return admission, portfolio


def _current_portfolio_like(
    current: Portfolio,
    task: TrajectoryEntity,
    *,
    relations: list[TrajectoryRelation] | None = None,
    extra_entities: list[TrajectoryEntity] | None = None,
) -> Portfolio:
    """Rebuild the SAME portfolio identity with a different CURRENT
    entity/relation layout (the TOCTOU double)."""

    project = next(
        e for e in current.entities if e.entity_type is EntityType.PROJECT
    )
    if relations is None:
        relations = [_membership(task.id, project.id)]
    return Portfolio(
        id=current.id,
        name="unit",
        entities=[project, task, *(extra_entities or [])],
        relations=relations,
    )


class FakePortfolioRepository:
    """In-memory, behaviorally scriptable PortfolioRepository double."""

    def __init__(
        self,
        portfolios: dict[UUID, Portfolio] | None = None,
        *,
        load_error: Exception | None = None,
        save_error: Exception | None = None,
    ) -> None:
        self._portfolios = dict(portfolios or {})
        self._load_error = load_error
        self._save_error = save_error
        self.loaded_ids: list[object] = []
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.loaded_ids.append(portfolio_id)
        if self._load_error is not None:
            raise self._load_error
        return self._portfolios.get(portfolio_id)

    def save(self, portfolio: Portfolio) -> None:
        if self._save_error is not None:
            raise self._save_error
        self.saved.append(portfolio)


# ---------------------------------------------------------------------------
# Exact public symbols / exports
# ---------------------------------------------------------------------------


def test_module_exports_exactly_the_public_symbols() -> None:
    assert tuple(lifecycle_persistence_module.__all__) == PUBLIC_SYMBOLS


def test_application_package_exports_the_public_symbols() -> None:
    for name in PUBLIC_SYMBOLS:
        assert name in application_package.__all__
        assert getattr(application_package, name) is not None
    assert (
        application_package.apply_admitted_task_execution_lifecycle_durably
        is lifecycle_persistence_module.apply_admitted_task_execution_lifecycle_durably
    )


def test_error_type_hierarchy() -> None:
    assert issubclass(DurableTaskExecutionLifecycleApplicationError, ValueError)
    assert issubclass(
        lifecycle_persistence_module.TaskExecutionLifecyclePortfolioNotFoundError,
        DurableTaskExecutionLifecycleApplicationError,
    )


# ---------------------------------------------------------------------------
# Admission strictness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        "admission",
        42,
        object(),
        TaskExecutionLifecycleDecision,
        TaskExecutionResultRecord,
        Portfolio,
    ],
)
def test_admission_must_be_genuine_v151_instance(bad: object) -> None:
    _, portfolio = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationError,
        match="genuine V1.51",
    ):
        apply_admitted_task_execution_lifecycle_durably(bad, CHANGED_AT, repository)  # type: ignore[arg-type]
    assert repository.loaded_ids == []
    assert repository.saved == []


def test_genuine_v151_admission_accepted_with_genuine_changed_at() -> None:
    admission, portfolio = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    result = apply_admitted_task_execution_lifecycle_durably(
        admission, CHANGED_AT, repository
    )
    assert isinstance(result, EntityStatusTransitionResult)
    assert result.new_status is EntityStatus.COMPLETED
    assert len(repository.loaded_ids) == 1
    assert len(repository.saved) == 1
    assert repository.saved[0] is result.portfolio


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"current_task_status": EntityStatus.COMPLETED}, "re-validation"),
        ({"execution_succeeded": 1}, "re-validation"),
        ({"lifecycle_decision_id": "not-a-uuid"}, "re-validation"),
        (
            {"disposition": TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE},
            "re-validation",
        ),
    ],
)
def test_hostile_model_construct_v151_rejected(
    overrides: dict[str, Any],
    match: str,
) -> None:
    """Hostile ``model_construct`` admissions must fail in the fresh
    COMPLETE strict re-validation (or in the V1.51 replay it enables)
    and must never reach the repository."""

    admission, portfolio = _genuine_admission_and_portfolio()
    merged = admission.model_dump()
    merged.update(overrides)
    hostile = TaskExecutionLifecycleAdmission.model_construct(**merged)
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    with pytest.raises(Exception, match=match):
        apply_admitted_task_execution_lifecycle_durably(hostile, CHANGED_AT, repository)
    assert repository.saved == []


def test_no_semantic_reads_from_caller_owned_admission_after_revalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupting the caller-owned admission while its payload is being
    revalidated must not change the outcome: only the fresh validated
    copy is read for semantics."""

    admission, portfolio = _genuine_admission_and_portfolio()
    original_model_dump = TaskExecutionLifecycleAdmission.model_dump

    def dump_then_corrupt(
        self: TaskExecutionLifecycleAdmission,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        payload = original_model_dump(self, *args, **kwargs)
        object.__setattr__(self, "current_task_status", EntityStatus.COMPLETED)
        object.__setattr__(self, "disposition", "RAW-STRING")
        return payload

    monkeypatch.setattr(TaskExecutionLifecycleAdmission, "model_dump", dump_then_corrupt)

    repository = FakePortfolioRepository({portfolio.id: portfolio})
    result = apply_admitted_task_execution_lifecycle_durably(
        admission, CHANGED_AT, repository
    )

    assert result.new_status is EntityStatus.COMPLETED
    assert len(repository.saved) == 1


# ---------------------------------------------------------------------------
# Exact internal V1.50 reconstruction (caller can never override provenance)
# ---------------------------------------------------------------------------


def test_exact_internal_v150_reconstruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted: list[
        tuple[TaskExecutionLifecycleDecision, Portfolio]
    ] = []
    replay = lifecycle_persistence_module.admit_current_task_execution_lifecycle

    def spy(
        decision: TaskExecutionLifecycleDecision,
        portfolio: Portfolio,
    ) -> TaskExecutionLifecycleAdmission:
        admitted.append((decision, portfolio))
        return replay(decision, portfolio)

    monkeypatch.setattr(
        lifecycle_persistence_module,
        "admit_current_task_execution_lifecycle",
        spy,
    )

    admission, portfolio = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    apply_admitted_task_execution_lifecycle_durably(admission, CHANGED_AT, repository)

    assert len(admitted) == 1
    decision, replayed_portfolio = admitted[0]
    assert isinstance(decision, TaskExecutionLifecycleDecision)
    expected = TaskExecutionLifecycleDecision(
        lifecycle_decision_id=admission.lifecycle_decision_id,
        decided_at=admission.decided_at,
        execution_record_id=admission.execution_record_id,
        execution_recorded_at=admission.execution_recorded_at,
        request_id=admission.request_id,
        intent_id=admission.intent_id,
        execution_decision_id=admission.execution_decision_id,
        portfolio_id=admission.portfolio_id,
        authorized_project_id=admission.authorized_project_id,
        authorized_task_id=admission.authorized_task_id,
        execution_succeeded=admission.execution_succeeded,
        disposition=admission.disposition,
    )
    assert decision == expected
    assert replayed_portfolio is portfolio


# ---------------------------------------------------------------------------
# changed_at strictness (before any repository interaction)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [None, "2026-02-02", 42, date(2026, 2, 2)],
)
def test_non_datetime_changed_at_rejected_before_repository(bad: object) -> None:
    admission, portfolio = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationError,
        match="genuine datetime",
    ):
        apply_admitted_task_execution_lifecycle_durably(
            admission, bad, repository
        )
    assert repository.loaded_ids == []
    assert repository.saved == []


def test_naive_changed_at_rejected_before_repository() -> None:
    admission, portfolio = _genuine_admission_and_portfolio()
    naive = CHANGED_AT.replace(tzinfo=None)
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    with pytest.raises(
        DurableTaskExecutionLifecycleApplicationError,
        match="timezone-aware",
    ):
        apply_admitted_task_execution_lifecycle_durably(admission, naive, repository)
    assert repository.loaded_ids == []
    assert repository.saved == []


# ---------------------------------------------------------------------------
# CURRENT replay / TOCTOU: exact loaded Portfolio everywhere, single load
# ---------------------------------------------------------------------------


def test_load_exactly_once_with_validated_portfolio_id() -> None:
    admission, portfolio = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    apply_admitted_task_execution_lifecycle_durably(admission, CHANGED_AT, repository)
    assert repository.loaded_ids == [admission.portfolio_id]


def test_exact_loaded_portfolio_passed_to_v151_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Portfolio] = []
    replay = lifecycle_persistence_module.admit_current_task_execution_lifecycle

    def spy(
        decision: TaskExecutionLifecycleDecision,
        portfolio: Portfolio,
    ) -> TaskExecutionLifecycleAdmission:
        seen.append(portfolio)
        return replay(decision, portfolio)

    monkeypatch.setattr(
        lifecycle_persistence_module,
        "admit_current_task_execution_lifecycle",
        spy,
    )

    admission, portfolio = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    apply_admitted_task_execution_lifecycle_durably(admission, CHANGED_AT, repository)
    assert len(seen) == 1
    assert seen[0] is portfolio


def test_exact_same_loaded_portfolio_passed_to_v17a_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[
        tuple[Portfolio, UUID, EntityStatus, datetime]
    ] = []

    def spy(
        portfolio: object,
        entity_id: object,
        target_status: object,
        changed_at: object,
    ) -> EntityStatusTransitionResult:
        assert isinstance(portfolio, Portfolio)
        assert isinstance(entity_id, UUID)
        assert isinstance(target_status, EntityStatus)
        assert isinstance(changed_at, datetime)
        seen.append((portfolio, entity_id, target_status, changed_at))
        return transition_entity_status(
            portfolio, entity_id, target_status, changed_at
        )

    monkeypatch.setattr(lifecycle_persistence_module, "transition_entity_status", spy)

    admission, portfolio = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    result = apply_admitted_task_execution_lifecycle_durably(
        admission, CHANGED_AT, repository
    )
    assert len(seen) == 1
    portfolio_arg, entity_arg, target_arg, ts_arg = seen[0]
    assert portfolio_arg is portfolio
    assert entity_arg is admission.authorized_task_id
    assert target_arg is EntityStatus.COMPLETED
    assert ts_arg is CHANGED_AT
    assert result.new_status is EntityStatus.COMPLETED


def test_old_current_task_status_is_not_trusted_when_task_already_completed() -> None:
    """The admission ``current_task_status`` (ACTIVE) is historical
    evidence only: if the CURRENT loaded task is already COMPLETED, the
    V1.51 replay must reject even though the admission says ACTIVE."""

    admission, current = _genuine_admission_and_portfolio()
    assert admission.current_task_status is not EntityStatus.COMPLETED

    current_task = next(
        e for e in current.entities if e.id is admission.authorized_task_id
    )
    completed_layout = _current_portfolio_like(
        current, _task_entity(entity_id=current_task.id, status=EntityStatus.COMPLETED)
    )
    repository = FakePortfolioRepository({current.id: completed_layout})
    with pytest.raises(TaskExecutionLifecycleAdmissionError, match="already carries"):
        apply_admitted_task_execution_lifecycle_durably(
            admission, CHANGED_AT, repository
        )
    assert repository.saved == []


def test_moved_task_membership_rejected() -> None:
    """The task currently belongs to a DIFFERENT project: the V1.51
    replay must reject; the other project is never substituted."""

    admission, current = _genuine_admission_and_portfolio()
    current_project = next(
        e for e in current.entities if e.id is admission.authorized_project_id
    )
    current_task = next(
        e for e in current.entities if e.id is admission.authorized_task_id
    )
    other_project = _project_entity()
    moved_layout = Portfolio(
        id=current.id,
        name="unit",
        entities=[current_project, other_project, current_task],
        relations=[_membership(current_task.id, other_project.id)],
    )
    repository = FakePortfolioRepository({current.id: moved_layout})
    with pytest.raises(TaskExecutionLifecycleAdmissionError, match="BELONGS_TO"):
        apply_admitted_task_execution_lifecycle_durably(
            admission, CHANGED_AT, repository
        )
    assert repository.saved == []


def test_reversed_membership_relation_rejected() -> None:
    admission, current = _genuine_admission_and_portfolio()
    current_project = next(
        e for e in current.entities if e.id is admission.authorized_project_id
    )
    current_task = next(
        e for e in current.entities if e.id is admission.authorized_task_id
    )
    reversed_layout = Portfolio(
        id=current.id,
        name="unit",
        entities=[current_project, current_task],
        relations=[
            TrajectoryRelation(
                id=_uuid(),
                source_id=current_project.id,
                target_id=current_task.id,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )
    repository = FakePortfolioRepository({current.id: reversed_layout})
    with pytest.raises(TaskExecutionLifecycleAdmissionError, match="BELONGS_TO"):
        apply_admitted_task_execution_lifecycle_durably(
            admission, CHANGED_AT, repository
        )
    assert repository.saved == []


def test_missing_membership_relation_rejected() -> None:
    admission, current = _genuine_admission_and_portfolio()
    current_project = next(
        e for e in current.entities if e.id is admission.authorized_project_id
    )
    current_task = next(
        e for e in current.entities if e.id is admission.authorized_task_id
    )
    no_relation_layout = Portfolio(
        id=current.id,
        name="unit",
        entities=[current_project, current_task],
        relations=[],
    )
    repository = FakePortfolioRepository({current.id: no_relation_layout})
    with pytest.raises(TaskExecutionLifecycleAdmissionError, match="BELONGS_TO"):
        apply_admitted_task_execution_lifecycle_durably(
            admission, CHANGED_AT, repository
        )
    assert repository.saved == []


def test_missing_project_rejected() -> None:
    admission, current = _genuine_admission_and_portfolio()
    current_task = next(
        e for e in current.entities if e.id is admission.authorized_task_id
    )
    foreign_project = _project_entity()
    orphan_task = Portfolio(
        id=current.id,
        name="unit",
        entities=[foreign_project, current_task],
        relations=[],
    )
    repository = FakePortfolioRepository({current.id: orphan_task})
    with pytest.raises(TaskExecutionLifecycleAdmissionError, match="authorized project"):
        apply_admitted_task_execution_lifecycle_durably(
            admission, CHANGED_AT, repository
        )
    assert repository.saved == []


def test_missing_task_rejected() -> None:
    admission, current = _genuine_admission_and_portfolio()
    lone_project = Portfolio(
        id=current.id,
        name="unit",
        entities=[
            next(
                e for e in current.entities if e.id is admission.authorized_project_id
            )
        ],
        relations=[],
    )
    repository = FakePortfolioRepository({current.id: lone_project})
    with pytest.raises(TaskExecutionLifecycleAdmissionError, match="authorized task"):
        apply_admitted_task_execution_lifecycle_durably(
            admission, CHANGED_AT, repository
        )
    assert repository.saved == []


def test_type_mismatch_rejected() -> None:
    admission, current = _genuine_admission_and_portfolio()
    current_project = next(
        e for e in current.entities if e.id is admission.authorized_project_id
    )
    task_as_project = TrajectoryEntity(
        id=admission.authorized_task_id,
        entity_type=EntityType.PROJECT,
        title="task-but-project",
    )
    mismatched = Portfolio(
        id=current.id,
        name="unit",
        entities=[current_project, task_as_project],
        relations=[_membership(task_as_project.id, current_project.id)],
    )
    repository = FakePortfolioRepository({current.id: mismatched})
    with pytest.raises(
        TaskExecutionLifecycleAdmissionError, match="TASK entity type"
    ):
        apply_admitted_task_execution_lifecycle_durably(
            admission, CHANGED_AT, repository
        )
    assert repository.saved == []


def test_no_fallback_to_other_task_in_same_portfolio() -> None:
    """A different, validly membered TASK in the same portfolio must never
    be substituted for the exact authorized task."""

    admission, current = _genuine_admission_and_portfolio()
    current_project = next(
        e for e in current.entities if e.id is admission.authorized_project_id
    )
    decoy = _task_entity()
    decoy_layout = Portfolio(
        id=current.id,
        name="unit",
        entities=[current_project, decoy],
        relations=[_membership(decoy.id, current_project.id)],
    )
    repository = FakePortfolioRepository({current.id: decoy_layout})
    with pytest.raises(TaskExecutionLifecycleAdmissionError, match="authorized task"):
        apply_admitted_task_execution_lifecycle_durably(
            admission, CHANGED_AT, repository
        )
    assert repository.saved == []


# ---------------------------------------------------------------------------
# Durable transition semantics
# ---------------------------------------------------------------------------


def test_exact_authorized_task_transitions_to_completed_and_saves_once() -> None:
    admission, current = _genuine_admission_and_portfolio()
    task = current.get_entity(admission.authorized_task_id)
    project = current.get_entity(admission.authorized_project_id)
    assert task is not None and project is not None
    previous_status = task.status
    snapshot = current.model_dump()

    repository = FakePortfolioRepository({current.id: current})
    result = apply_admitted_task_execution_lifecycle_durably(
        admission, CHANGED_AT, repository
    )

    assert isinstance(result, EntityStatusTransitionResult)
    assert result.entity_id is admission.authorized_task_id
    assert result.previous_status is previous_status
    assert result.new_status is EntityStatus.COMPLETED
    assert result.changed_at == CHANGED_AT
    assert result.portfolio is not current
    transitioned_task = result.portfolio.get_entity(admission.authorized_task_id)
    assert transitioned_task is not None
    assert transitioned_task.status is EntityStatus.COMPLETED
    assert transitioned_task.updated_at == CHANGED_AT
    assert len(repository.saved) == 1
    assert repository.saved[0] is result.portfolio
    # The loaded source portfolio is never mutated.
    assert current.model_dump() == snapshot
    assert current.get_entity(admission.authorized_task_id) is not None
    assert current.get_entity(admission.authorized_task_id).status is previous_status  # type: ignore[union-attr]


def test_returned_result_is_the_exact_v17a_result_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[EntityStatusTransitionResult] = []

    def spy(
        portfolio: object,
        entity_id: object,
        target_status: object,
        changed_at: object,
    ) -> EntityStatusTransitionResult:
        assert isinstance(portfolio, Portfolio)
        out = transition_entity_status(
            portfolio, entity_id, target_status, changed_at
        )
        captured.append(out)
        return out

    monkeypatch.setattr(lifecycle_persistence_module, "transition_entity_status", spy)

    admission, current = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({current.id: current})
    result = apply_admitted_task_execution_lifecycle_durably(
        admission, CHANGED_AT, repository
    )
    assert len(captured) == 1
    assert result is captured[0]


def test_target_status_is_fixed_to_completed_not_overridable() -> None:
    """The signature has no target-status parameter: whatever the
    current status (here PAUSED), the outcome is exactly COMPLETED."""

    admission, current = _genuine_admission_and_portfolio(
        task_status=EntityStatus.PAUSED
    )
    repository = FakePortfolioRepository({current.id: current})
    result = apply_admitted_task_execution_lifecycle_durably(
        admission, CHANGED_AT, repository
    )
    assert result.previous_status is EntityStatus.PAUSED
    assert result.new_status is EntityStatus.COMPLETED


def test_stale_changed_at_error_propagates_without_save() -> None:
    admission, current = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({current.id: current})
    stale = CHANGED_AT - timedelta(days=1) - timedelta(hours=1)
    with pytest.raises(StaleChangedAtError):
        apply_admitted_task_execution_lifecycle_durably(admission, stale, repository)
    assert repository.saved == []


def test_load_exception_propagates_unchanged() -> None:
    class _LoadBoom(Exception):
        pass

    boom = _LoadBoom("load boom")
    admission, current = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({current.id: current}, load_error=boom)
    with pytest.raises(_LoadBoom, match="load boom"):
        apply_admitted_task_execution_lifecycle_durably(admission, CHANGED_AT, repository)
    assert repository.saved == []


def test_save_exception_propagates_unchanged() -> None:
    class _SaveBoom(Exception):
        pass

    boom = _SaveBoom("save boom")
    admission, current = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository({current.id: current}, save_error=boom)
    with pytest.raises(_SaveBoom, match="save boom"):
        apply_admitted_task_execution_lifecycle_durably(admission, CHANGED_AT, repository)
    assert repository.saved == []


def test_missing_portfolio_uses_narrow_boundary_error() -> None:
    admission, _ = _genuine_admission_and_portfolio()
    repository = FakePortfolioRepository()
    with pytest.raises(
        lifecycle_persistence_module.TaskExecutionLifecyclePortfolioNotFoundError,
        match="portfolio not found",
    ):
        apply_admitted_task_execution_lifecycle_durably(admission, CHANGED_AT, repository)
    assert repository.saved == []


# ---------------------------------------------------------------------------
# Architecture guards (executable)
# ---------------------------------------------------------------------------

_SOURCE = inspect.getsource(lifecycle_persistence_module)
_TREE = ast.parse(_SOURCE)


def _call_names() -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _imported_modules() -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_guard_no_v17b_durable_helper_used() -> None:
    assert "transition_entity_status_durably" not in _call_names()


def test_guard_no_provider_runtime_subprocess_shell_imports() -> None:
    forbidden = {
        "subprocess",
        "socket",
        "sqlite3",
        "requests",
        "httpx",
        "urllib",
        "anthropic",
        "openai",
        "ollama",
        "multiprocessing",
        "threading",
        "os",
    }
    assert _imported_modules() & forbidden == set()


def test_guard_no_uuid_or_time_generation() -> None:
    names = _call_names()
    assert names & {"uuid4", "uuid5", "uuid1", "uuid3"} == set()
    assert "now" not in names
    assert "utcnow" not in names
    assert "sleep" not in names
    assert "uuid4" not in _SOURCE
    assert "datetime.now" not in _SOURCE
    assert " utcnow" not in _SOURCE


def test_guard_exactly_one_load_and_one_save_call() -> None:
    loads = [
        node
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "load"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "repository"
    ]
    saves = [
        node
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "save"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "repository"
    ]
    assert len(loads) == 1
    assert len(saves) == 1


def test_guard_no_retry_idempotency_or_projection_language() -> None:
    lowered = _SOURCE.lower()
    for word in (
        "retry",
        "retries",
        "idempoten",
        "exactly-once",
        "transaction",
        "versioning",
        "latest",
        "effective",
    ):
        assert word not in lowered, word


def test_guard_no_direct_mutation_patterns() -> None:
    for pattern in (".append(", ".extend(", ".clear("):
        assert pattern not in _SOURCE, pattern
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in {"current", "loaded"}
                )


def test_guard_no_sql_or_db_boundary() -> None:
    lowered = _SOURCE.lower()
    for word in ("sqlite", "create table", "insert into", "update set"):
        assert word not in lowered, word
