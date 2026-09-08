"""V1.47 — CURRENT execution admission for the exact V1.46 request.

These are focused unit tests plus executable (AST) architecture guards for
``execution_effort_project_focus_next_ready_task_execution_admission``.

The module is pure and deterministic and owns its own CURRENT evidence: it
never executes anything, never calls a provider / LLM / agent, never persists,
never transitions status, never re-selects / ranks / falls back to another
task, and never reuses V1.45 applicability evidence as CURRENT evidence.
"""

from __future__ import annotations

import ast
import itertools
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionAdmission,
    PortfolioProjectFocusNextReadyTaskExecutionAdmissionConstraint,
    PortfolioProjectFocusNextReadyTaskExecutionAdmissionError,
    PortfolioProjectFocusNextReadyTaskExecutionAdmissionState,
    evaluate_current_next_ready_task_execution_admission,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_request import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskExecutionRequest,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import WorkBreakdownError

S = PortfolioProjectFocusNextReadyTaskExecutionAdmissionState
Admission = PortfolioProjectFocusNextReadyTaskExecutionAdmission
Constraint = PortfolioProjectFocusNextReadyTaskExecutionAdmissionConstraint
AdmissionError = PortfolioProjectFocusNextReadyTaskExecutionAdmissionError
Request = PortfolioProjectFocusNextReadyTaskExecutionRequest
fn = evaluate_current_next_ready_task_execution_admission


# ---------------------------------------------------------------------------
# Fixed, deterministic identities: no wall clock, no random generation.
# ---------------------------------------------------------------------------

REQUESTED_AT = datetime(2025, 2, 1, 8, 0, 0, tzinfo=UTC)
AUTHORIZED_AT = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)
_NON_UTC_OFFSET = timezone(timedelta(hours=5, minutes=30))

_UUID_SEQUENCE = itertools.count(1)


def _uuid() -> UUID:
    """Deterministic sequential test identity (test-only helper)."""
    return UUID(int=next(_UUID_SEQUENCE))


def _entity(
    entity_type: EntityType,
    status: EntityStatus = EntityStatus.ACTIVE,
) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=_uuid(),
        entity_type=entity_type,
        title=entity_type.value,
        status=status,
    )


def _portfolio(
    entities: list[TrajectoryEntity],
    relations: list[TrajectoryRelation],
) -> Portfolio:
    return Portfolio(id=_uuid(), name="unit", entities=entities, relations=relations)


def _relation(
    source: UUID,
    target: UUID,
    relation_type: RelationType,
) -> TrajectoryRelation:
    return TrajectoryRelation(
        id=_uuid(), source_id=source, target_id=target, relation_type=relation_type
    )


def _request(
    portfolio: Portfolio,
    project: UUID,
    task: UUID,
    *,
    requested_at: datetime = REQUESTED_AT,
    authorized_at: datetime = AUTHORIZED_AT,
) -> Request:
    return Request(
        request_id=_uuid(),
        requested_at=requested_at,
        intent_id=_uuid(),
        authorized_at=authorized_at,
        decision_id=_uuid(),
        portfolio_id=portfolio.id,
        authorized_project_id=project,
        authorized_task_id=task,
    )


def _admitted_portfolio():
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    return project, task, portfolio


class _NullOffsetTz(tzinfo):
    """A tzinfo that reports ``utcoffset() is None`` despite ``tzinfo``
    being non-None: the exact case the admission model must reject."""

    def utcoffset(self, instance: datetime) -> timedelta | None:
        return None

    def dst(self, instance: datetime) -> timedelta:
        return timedelta(0)

    def tzname(self, instance: datetime) -> str | None:
        return "null"


def _full_admission(
    *,
    requested_at: datetime = REQUESTED_AT,
    authorized_at: datetime = AUTHORIZED_AT,
) -> Admission:
    return Admission(
        request_id=_uuid(),
        requested_at=requested_at,
        intent_id=_uuid(),
        authorized_at=authorized_at,
        decision_id=_uuid(),
        portfolio_id=_uuid(),
        authorized_project_id=_uuid(),
        authorized_task_id=_uuid(),
        task_status=EntityStatus.ACTIVE,
        admission_state=S.ADMITTED,
        constraints=(),
        unsatisfied_constraint_count=0,
    )


# ---------------------------------------------------------------------------
# Exact protocol surface: states, fields, config.
# ---------------------------------------------------------------------------


def test_state_enum_is_exactly_the_eight_v147_states() -> None:
    assert set(S.__members__) == {
        "ADMITTED",
        "PROJECT_MISSING",
        "PROJECT_TYPE_MISMATCH",
        "TASK_MISSING",
        "TASK_TYPE_MISMATCH",
        "TASK_NOT_IN_AUTHORIZED_PROJECT",
        "INELIGIBLE_STATUS",
        "CONSTRAINED",
    }
    assert {state.value for state in S} == {
        "admitted",
        "project_missing",
        "project_type_mismatch",
        "task_missing",
        "task_type_mismatch",
        "task_not_in_authorized_project",
        "ineligible_status",
        "constrained",
    }


def test_admission_model_fields_and_config_are_exact() -> None:
    assert set(Admission.model_fields) == {
        "request_id",
        "requested_at",
        "intent_id",
        "authorized_at",
        "decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
        "task_status",
        "admission_state",
        "constraints",
        "unsatisfied_constraint_count",
    }
    assert Admission.model_config["strict"] is True
    assert Admission.model_config["frozen"] is True
    assert Admission.model_config["extra"] == "forbid"
    # No defaults on any field.
    for field in Admission.model_fields.values():
        assert field.is_required()


def test_constraint_model_fields_and_config_are_exact() -> None:
    assert set(Constraint.model_fields) == {
        "relation_id",
        "relation_type",
        "counterpart_entity_id",
        "counterpart_entity_type",
        "counterpart_status",
        "satisfied",
    }
    assert Constraint.model_config["strict"] is True
    assert Constraint.model_config["frozen"] is True
    assert Constraint.model_config["extra"] == "forbid"
    for field in Constraint.model_fields.values():
        assert field.is_required()


def test_error_is_a_value_error_subclass() -> None:
    assert issubclass(AdmissionError, ValueError)


@pytest.mark.parametrize("extra_key", ["score", "confidence", "priority", "note"])
def test_extra_fields_are_forbidden(extra_key: str) -> None:
    with pytest.raises(ValidationError):
        Admission(**_admitted_kwargs(extra_key=extra_key))  # type: ignore[arg-type]


def _admitted_kwargs(extra_key: str | None = None) -> dict[str, object]:
    project, task, portfolio = _admitted_portfolio()
    request = _request(portfolio, project.id, task.id)
    kwargs: dict[str, object] = {
        "request_id": request.request_id,
        "requested_at": request.requested_at,
        "intent_id": request.intent_id,
        "authorized_at": request.authorized_at,
        "decision_id": request.decision_id,
        "portfolio_id": portfolio.id,
        "authorized_project_id": project.id,
        "authorized_task_id": task.id,
        "task_status": EntityStatus.ACTIVE,
        "admission_state": S.ADMITTED,
        "constraints": (),
        "unsatisfied_constraint_count": 0,
    }
    if extra_key is not None:
        kwargs[extra_key] = "x"
    return kwargs


# ---------------------------------------------------------------------------
# Timestamp awareness + exact original-offset preservation (model level).
# ---------------------------------------------------------------------------


def test_admitted_timestamps_remain_aware() -> None:
    project, task, portfolio = _admitted_portfolio()
    result = fn(_request(portfolio, project.id, task.id), portfolio)
    assert result.requested_at.tzinfo is not None
    assert result.requested_at.utcoffset() is not None
    assert result.authorized_at.tzinfo is not None
    assert result.authorized_at.utcoffset() is not None


def test_exact_requested_at_offset_is_preserved() -> None:
    project, task, portfolio = _admitted_portfolio()
    req = _request(
        portfolio,
        project.id,
        task.id,
        requested_at=datetime(2025, 2, 1, 8, 0, 0, tzinfo=_NON_UTC_OFFSET),
    )
    result = fn(req, portfolio)
    assert result.requested_at == req.requested_at
    assert result.requested_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_exact_authorized_at_offset_is_preserved() -> None:
    project, task, portfolio = _admitted_portfolio()
    req = _request(
        portfolio,
        project.id,
        task.id,
        authorized_at=datetime(2025, 1, 15, 10, 30, 0, tzinfo=_NON_UTC_OFFSET),
    )
    result = fn(req, portfolio)
    assert result.authorized_at == req.authorized_at
    assert result.authorized_at.utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize("which", ["requested_at", "authorized_at"])
def test_naive_timestamp_rejected_at_model_level(which: str) -> None:
    kwargs = _admitted_kwargs()
    kwargs[which] = datetime(2025, 1, 1, 9, 0)  # naive
    with pytest.raises(ValidationError):
        Admission(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("which", ["requested_at", "authorized_at"])
def test_null_offset_timestamp_rejected_at_model_level(which: str) -> None:
    kwargs = _admitted_kwargs()
    kwargs[which] = datetime(2025, 1, 1, 9, 0, tzinfo=_NullOffsetTz())
    with pytest.raises(ValidationError):
        Admission(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# No temporal rule: requested_at / authorized_at are provenance only.
# ---------------------------------------------------------------------------


def test_no_temporal_rule_both_timestamps_are_provenance_only() -> None:
    project, task, portfolio = _admitted_portfolio()
    for ts in (
        datetime(1999, 3, 1, tzinfo=UTC),
        datetime(2199, 12, 31, 23, 59, 0, tzinfo=UTC),
    ):
        req = _request(
            portfolio,
            project.id,
            task.id,
            requested_at=ts,
            authorized_at=ts,
        )
        result = fn(req, portfolio)
        assert result.admission_state is S.ADMITTED
        assert result.unsatisfied_constraint_count == 0
        assert result.requested_at == ts
        assert result.authorized_at == ts


# ---------------------------------------------------------------------------
# Constraint COMPLETED-only invariant (model level, not just produced rows)
# + strict-bool behaviour.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "satisfied", "valid"),
    [
        (EntityStatus.COMPLETED, True, True),
        (EntityStatus.COMPLETED, False, False),
        (EntityStatus.ACTIVE, True, False),
        (EntityStatus.PAUSED, True, False),
        (EntityStatus.CANCELLED, True, False),
        (EntityStatus.ARCHIVED, True, False),
    ],
)
def test_constraint_satisfied_is_exactly_counterpart_completed(
    status: EntityStatus,
    satisfied: bool,
    valid: bool,
) -> None:
    kwargs: dict[str, object] = {
        "relation_id": _uuid(),
        "relation_type": RelationType.BLOCKS,
        "counterpart_entity_id": _uuid(),
        "counterpart_entity_type": EntityType.TASK,
        "counterpart_status": status,
        "satisfied": satisfied,
    }
    if valid:
        assert Constraint(**kwargs).satisfied is True  # type: ignore[arg-type]
    else:
        with pytest.raises(ValidationError):
            Constraint(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [1, 0, "true", "False", None])
def test_constraint_satisfied_is_strict_bool(bad: object) -> None:
    kwargs: dict[str, object] = {
        "relation_id": _uuid(),
        "relation_type": RelationType.BLOCKS,
        "counterpart_entity_id": _uuid(),
        "counterpart_entity_type": EntityType.TASK,
        "counterpart_status": EntityStatus.COMPLETED,
        "satisfied": bad,
    }
    with pytest.raises(ValidationError):
        Constraint(**kwargs)  # type: ignore[arg-type]


def test_constraint_satisfied_accepts_only_real_bools() -> None:
    kwargs: dict[str, object] = {
        "relation_id": _uuid(),
        "relation_type": RelationType.BLOCKS,
        "counterpart_entity_id": _uuid(),
        "counterpart_entity_type": EntityType.TASK,
        "counterpart_status": EntityStatus.ACTIVE,
    }
    assert Constraint(**kwargs, satisfied=False).satisfied is False  # type: ignore[arg-type]
    c = dict(kwargs)
    c["counterpart_status"] = EntityStatus.COMPLETED
    assert Constraint(**c, satisfied=True).satisfied is True  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ADMITTED: exact active task, no constraints.
# ---------------------------------------------------------------------------


def test_admitted_with_zero_constraints_projects_full_provenance() -> None:
    project, task, portfolio = _admitted_portfolio()
    req = _request(portfolio, project.id, task.id)

    result = fn(req, portfolio)

    assert isinstance(result, Admission)
    assert result.admission_state is S.ADMITTED
    assert result.task_status is EntityStatus.ACTIVE
    assert result.constraints == ()
    assert result.unsatisfied_constraint_count == 0
    # Full exact provenance projection from the V1.46 request.
    assert result.request_id == req.request_id
    assert result.requested_at == req.requested_at
    assert result.intent_id == req.intent_id
    assert result.authorized_at == req.authorized_at
    assert result.decision_id == req.decision_id
    assert result.portfolio_id == portfolio.id
    assert result.authorized_project_id == project.id
    assert result.authorized_task_id == task.id


def test_admitted_with_all_satisfied_constraints_preserves_two_pass_order() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    blocker_a = _entity(EntityType.TASK, status=EntityStatus.COMPLETED)
    blocker_b = _entity(EntityType.PROGRAM, status=EntityStatus.COMPLETED)
    dependency_a = _entity(EntityType.TASK, status=EntityStatus.COMPLETED)
    dependency_b = _entity(EntityType.DELIVERABLE, status=EntityStatus.COMPLETED)

    belongs_to = _relation(task.id, project.id, RelationType.BELONGS_TO)
    # Deliberately interleaved canonical order to pin the two-pass policy.
    inbound1 = _relation(blocker_a.id, task.id, RelationType.BLOCKS)
    dependency1 = _relation(task.id, dependency_a.id, RelationType.DEPENDS_ON)
    inbound2 = _relation(blocker_b.id, task.id, RelationType.PRECEDES)
    dependency2 = _relation(task.id, dependency_b.id, RelationType.REQUIRES)

    portfolio = _portfolio(
        [project, task, blocker_a, blocker_b, dependency_a, dependency_b],
        [belongs_to, inbound1, dependency1, inbound2, dependency2],
    )
    req = _request(portfolio, project.id, task.id)

    result = fn(req, portfolio)

    assert result.admission_state is S.ADMITTED
    assert len(result.constraints) == 4
    assert all(row.satisfied for row in result.constraints)
    assert result.unsatisfied_constraint_count == 0
    # First pass: incoming (canonical order), then outgoing (canonical order).
    assert [row.relation_id for row in result.constraints] == [
        inbound1.id,
        inbound2.id,
        dependency1.id,
        dependency2.id,
    ]
    assert result.constraints[0].relation_type is RelationType.BLOCKS
    assert result.constraints[1].relation_type is RelationType.PRECEDES
    assert result.constraints[2].relation_type is RelationType.DEPENDS_ON
    assert result.constraints[3].relation_type is RelationType.REQUIRES


# ---------------------------------------------------------------------------
# CONSTRAINED: unsatisfied incoming / outgoing constraints + counting.
# ---------------------------------------------------------------------------


def test_constrained_unsatisfied_incoming_blocks() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    blocker = _entity(EntityType.TASK, status=EntityStatus.ACTIVE)
    portfolio = _portfolio(
        [project, task, blocker],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(blocker.id, task.id, RelationType.BLOCKS),
        ],
    )
    result = fn(_request(portfolio, project.id, task.id), portfolio)

    assert result.admission_state is S.CONSTRAINED
    assert len(result.constraints) == 1
    assert result.constraints[0].satisfied is False
    assert result.constraints[0].counterpart_status is EntityStatus.ACTIVE
    assert result.unsatisfied_constraint_count == 1
    assert result.task_status is EntityStatus.ACTIVE


@pytest.mark.parametrize(
    "terminal", [EntityStatus.CANCELLED, EntityStatus.ARCHIVED]
)
def test_constrained_terminal_counterpart_is_unsatisfied(terminal: EntityStatus) -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    counterpart = _entity(EntityType.TASK, status=terminal)
    portfolio = _portfolio(
        [project, task, counterpart],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(task.id, counterpart.id, RelationType.DEPENDS_ON),
        ],
    )
    result = fn(_request(portfolio, project.id, task.id), portfolio)

    assert result.admission_state is S.CONSTRAINED
    assert result.constraints[0].satisfied is False
    assert result.constraints[0].counterpart_status is terminal
    assert result.unsatisfied_constraint_count == 1


def test_waiting_for_counterpart_unsatisfied() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    target = _entity(EntityType.WAITING, status=EntityStatus.WAITING)
    portfolio = _portfolio(
        [project, task, target],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(task.id, target.id, RelationType.WAITING_FOR),
        ],
    )
    result = fn(_request(portfolio, project.id, task.id), portfolio)

    assert result.admission_state is S.CONSTRAINED
    assert result.constraints[0].satisfied is False
    assert result.unsatisfied_constraint_count == 1


def test_mixed_satisfied_and_unsatisfied_counts_exactly() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    satisfied_blocker = _entity(EntityType.TASK, status=EntityStatus.COMPLETED)
    unsatisfied_blocker = _entity(EntityType.TASK, status=EntityStatus.ACTIVE)
    satisfied_dep = _entity(EntityType.TASK, status=EntityStatus.COMPLETED)
    portfolio = _portfolio(
        [project, task, satisfied_blocker, unsatisfied_blocker, satisfied_dep],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(satisfied_blocker.id, task.id, RelationType.BLOCKS),
            _relation(unsatisfied_blocker.id, task.id, RelationType.PRECEDES),
            _relation(task.id, satisfied_dep.id, RelationType.REQUIRES),
        ],
    )
    result = fn(_request(portfolio, project.id, task.id), portfolio)

    assert result.admission_state is S.CONSTRAINED
    assert len(result.constraints) == 3
    assert result.unsatisfied_constraint_count == 1
    assert [row.satisfied for row in result.constraints] == [True, False, True]


# ---------------------------------------------------------------------------
# INELIGIBLE_STATUS: non-ACTIVE exact task -> empty constraint tuple, 0.
# ---------------------------------------------------------------------------


def test_ineligible_status_carries_exact_status() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK, status=EntityStatus.PAUSED)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    result = fn(_request(portfolio, project.id, task.id), portfolio)

    assert result.admission_state is S.INELIGIBLE_STATUS
    assert result.task_status is EntityStatus.PAUSED
    assert result.constraints == ()
    assert result.unsatisfied_constraint_count == 0


def test_ineligible_status_despite_unsatisfied_constraints() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK, status=EntityStatus.INCUBATOR)
    blocker = _entity(EntityType.TASK, status=EntityStatus.ACTIVE)
    portfolio = _portfolio(
        [project, task, blocker],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(blocker.id, task.id, RelationType.BLOCKS),
        ],
    )
    result = fn(_request(portfolio, project.id, task.id), portfolio)

    assert result.admission_state is S.INELIGIBLE_STATUS
    assert result.constraints == ()
    assert result.unsatisfied_constraint_count == 0


# ---------------------------------------------------------------------------
# Membership drift: TASK_NOT_IN_AUTHORIZED_PROJECT.
# ---------------------------------------------------------------------------


def test_task_not_in_authorized_project_keeps_task_status() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK, status=EntityStatus.WAITING)
    other = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task, other],
        [_relation(other.id, project.id, RelationType.BELONGS_TO)],
    )
    result = fn(_request(portfolio, project.id, task.id), portfolio)

    assert result.admission_state is S.TASK_NOT_IN_AUTHORIZED_PROJECT
    assert result.task_status is EntityStatus.WAITING
    assert result.constraints is None
    assert result.unsatisfied_constraint_count is None


# ---------------------------------------------------------------------------
# Existence / type drift states.
# ---------------------------------------------------------------------------


def test_project_missing() -> None:
    task = _entity(EntityType.TASK)
    portfolio = _portfolio([task], [])
    ghost_project = _uuid()
    result = fn(_request(portfolio, ghost_project, task.id), portfolio)

    assert result.admission_state is S.PROJECT_MISSING
    assert result.task_status is None
    assert result.constraints is None
    assert result.unsatisfied_constraint_count is None


def test_project_type_mismatch() -> None:
    area = _entity(EntityType.AREA)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio([area, task], [])
    result = fn(_request(portfolio, area.id, task.id), portfolio)
    assert result.admission_state is S.PROJECT_TYPE_MISMATCH
    assert result.task_status is None
    assert result.constraints is None
    assert result.unsatisfied_constraint_count is None


def test_task_missing() -> None:
    project = _entity(EntityType.PROJECT)
    portfolio = _portfolio([project], [])
    ghost_task = _uuid()
    result = fn(_request(portfolio, project.id, ghost_task), portfolio)

    assert result.admission_state is S.TASK_MISSING
    assert result.task_status is None
    assert result.constraints is None
    assert result.unsatisfied_constraint_count is None


def test_task_type_mismatch() -> None:
    project = _entity(EntityType.PROJECT)
    idea = _entity(EntityType.IDEA)
    portfolio = _portfolio([project, idea], [])
    result = fn(_request(portfolio, project.id, idea.id), portfolio)
    assert result.admission_state is S.TASK_TYPE_MISMATCH
    assert result.task_status is None
    assert result.constraints is None
    assert result.unsatisfied_constraint_count is None


# ---------------------------------------------------------------------------
# Boundary: portfolio id mismatch is an error, not a state.
# ---------------------------------------------------------------------------


def test_portfolio_id_mismatch_raises() -> None:
    project, task, portfolio = _admitted_portfolio()
    other = Portfolio(id=_uuid(), name="other", entities=[], relations=[])
    req = _request(other, project.id, task.id)

    with pytest.raises(AdmissionError) as exc_info:
        fn(req, portfolio)

    assert "portfolio" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Boundary: non-genuine inputs are rejected before any state is derived.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_request", [None, {}, "request", 42, 3.5])
def test_rejects_non_genuine_requests(bad_request: object) -> None:
    project, task, portfolio = _admitted_portfolio()

    with pytest.raises(AdmissionError) as exc_info:
        fn(bad_request, portfolio)  # type: ignore[arg-type]

    assert "genuine" in str(exc_info.value)


@pytest.mark.parametrize("bad_portfolio", [None, {}, "portfolio", 42, 3.5])
def test_rejects_non_genuine_portfolios(bad_portfolio: object) -> None:
    project, task, portfolio = _admitted_portfolio()
    req = _request(portfolio, project.id, task.id)

    with pytest.raises(AdmissionError) as exc_info:
        fn(req, bad_portfolio)  # type: ignore[arg-type]

    assert "genuine" in str(exc_info.value)


def test_rejects_hostile_constructed_request() -> None:
    project, _task, portfolio = _admitted_portfolio()
    hostile = Request.model_construct(
        request_id=_uuid(),
        requested_at=datetime(2025, 2, 1, tzinfo=UTC),
        intent_id=_uuid(),
        authorized_at=datetime(2025, 1, 15, tzinfo=UTC),
        decision_id=_uuid(),
        portfolio_id=portfolio.id,
        authorized_project_id=project.id,
        # Inject a genuine real-field violation: a string where a UUID is
        # required. model_construct bypasses validation; fresh re-validation
        # must reject it.
        authorized_task_id="not-a-uuid",
    )

    with pytest.raises(AdmissionError) as exc_info:
        fn(hostile, portfolio)

    assert "re-validation" in str(exc_info.value)


def test_rejects_hostile_naive_timestamp_request() -> None:
    project, task, portfolio = _admitted_portfolio()
    hostile = Request.model_construct(
        request_id=_uuid(),
        requested_at=datetime(2025, 2, 1, 8, 0),  # naive
        intent_id=_uuid(),
        authorized_at=datetime(2025, 1, 15, 10, 30),  # naive
        decision_id=_uuid(),
        portfolio_id=portfolio.id,
        authorized_project_id=project.id,
        authorized_task_id=task.id,
    )

    with pytest.raises(AdmissionError):
        fn(hostile, portfolio)


def test_hostile_null_offset_timestamp_rejected_by_revalidation() -> None:
    project, task, portfolio = _admitted_portfolio()
    hostile = Request.model_construct(
        request_id=_uuid(),
        requested_at=datetime(2025, 2, 1, 8, 0, tzinfo=_NullOffsetTz()),
        intent_id=_uuid(),
        authorized_at=AUTHORIZED_AT,
        decision_id=_uuid(),
        portfolio_id=portfolio.id,
        authorized_project_id=project.id,
        authorized_task_id=task.id,
    )

    with pytest.raises(AdmissionError) as exc_info:
        fn(hostile, portfolio)

    assert "re-validation" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Boundary: incoherent WBS is an error, never a membership state.
# ---------------------------------------------------------------------------


def test_incoherent_wbs_raises_and_is_not_reinterpreted() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(project.id, task.id, RelationType.BELONGS_TO),
        ],
    )
    req = _request(portfolio, project.id, task.id)

    with pytest.raises(AdmissionError) as exc_info:
        fn(req, portfolio)

    assert "reinterpret" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, WorkBreakdownError)


# ---------------------------------------------------------------------------
# Boundary: an unresolvable constraining counterpart is an error, never a
# reinterpreted unsatisfied constraint or a fallback. A genuine Portfolio
# cannot contain dangling relation endpoints, so a hostile constructed
# Portfolio exposes the boundary exactly as production sees malformed state.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relation_type", "outgoing"),
    [
        (RelationType.BLOCKS, False),
        (RelationType.PRECEDES, False),
        (RelationType.DEPENDS_ON, True),
        (RelationType.REQUIRES, True),
        (RelationType.WAITING_FOR, True),
    ],
)
def test_unresolvable_counterpart_is_a_boundary_error(
    relation_type: RelationType,
    outgoing: bool,
) -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    ghost_counterpart = _uuid()
    dangling = _relation(
        task.id if outgoing else ghost_counterpart,
        ghost_counterpart if outgoing else task.id,
        relation_type,
    )
    portfolio = Portfolio.model_construct(
        id=_uuid(),
        name="unresolvable-counterpart",
        entities=[project, task],
        relations=[
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            dangling,
        ],
    )
    req = _request(portfolio, project.id, task.id)

    with pytest.raises(AdmissionError) as exc_info:
        fn(req, portfolio)

    assert "counterpart" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Constraint direction coverage: all five constraining types, counterpart
# identity / type / status projected exactly.
# ---------------------------------------------------------------------------


def test_all_five_constraining_types_produce_exact_rows() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    c1 = _entity(EntityType.TASK)  # incoming BLOCKS source
    c2 = _entity(EntityType.PROGRAM)  # incoming PRECEDES source
    c3 = _entity(EntityType.DELIVERABLE)  # outgoing DEPENDS_ON target
    c4 = _entity(EntityType.RESOURCE)  # outgoing REQUIRES target
    c5 = _entity(EntityType.WAITING)  # outgoing WAITING_FOR target

    rels = [
        _relation(task.id, project.id, RelationType.BELONGS_TO),
        _relation(c1.id, task.id, RelationType.BLOCKS),
        _relation(c2.id, task.id, RelationType.PRECEDES),
        _relation(task.id, c3.id, RelationType.DEPENDS_ON),
        _relation(task.id, c4.id, RelationType.REQUIRES),
        _relation(task.id, c5.id, RelationType.WAITING_FOR),
    ]
    portfolio = _portfolio([project, task, c1, c2, c3, c4, c5], rels)
    req = _request(portfolio, project.id, task.id)

    result = fn(req, portfolio)

    assert result.admission_state is S.CONSTRAINED
    assert len(result.constraints) == 5
    assert result.unsatisfied_constraint_count == 5

    by_type = {row.relation_type: row for row in result.constraints}
    assert set(by_type) == {
        RelationType.BLOCKS,
        RelationType.PRECEDES,
        RelationType.DEPENDS_ON,
        RelationType.REQUIRES,
        RelationType.WAITING_FOR,
    }
    # Counterpart identity resolves to the correct side per direction.
    assert by_type[RelationType.BLOCKS].counterpart_entity_id is c1.id
    assert by_type[RelationType.PRECEDES].counterpart_entity_id is c2.id
    assert by_type[RelationType.DEPENDS_ON].counterpart_entity_id is c3.id
    assert by_type[RelationType.REQUIRES].counterpart_entity_id is c4.id
    assert by_type[RelationType.WAITING_FOR].counterpart_entity_id is c5.id
    assert by_type[RelationType.BLOCKS].counterpart_entity_type is EntityType.TASK
    assert by_type[RelationType.PRECEDES].counterpart_entity_type is EntityType.PROGRAM
    for counterpart_row in by_type.values():
        assert counterpart_row.satisfied is False


def test_only_completed_satisfies_across_statuses() -> None:
    """Only COMPLETED satisfies; every other terminal/active status does not."""

    def _run(counterpart_status: EntityStatus) -> tuple[S, bool]:
        project = _entity(EntityType.PROJECT)
        task = _entity(EntityType.TASK)
        counterpart = _entity(EntityType.TASK, status=counterpart_status)
        portfolio = _portfolio(
            [project, task, counterpart],
            [
                _relation(task.id, project.id, RelationType.BELONGS_TO),
                _relation(counterpart.id, task.id, RelationType.BLOCKS),
            ],
        )
        req = _request(portfolio, project.id, task.id)
        result = fn(req, portfolio)
        row = result.constraints[0]
        assert row.relation_id is not None
        assert row.counterpart_entity_id is counterpart.id
        assert row.counterpart_entity_type is EntityType.TASK
        assert row.counterpart_status is counterpart_status
        expected = counterpart_status is EntityStatus.COMPLETED
        assert row.satisfied is expected
        return result.admission_state, expected

    assert _run(EntityStatus.COMPLETED) == (S.ADMITTED, True)
    for status in (
        EntityStatus.ACTIVE,
        EntityStatus.PAUSED,
        EntityStatus.WAITING,
        EntityStatus.INCUBATOR,
        EntityStatus.SOMEDAY,
        EntityStatus.CANCELLED,
        EntityStatus.ARCHIVED,
    ):
        assert _run(status) == (S.CONSTRAINED, False)


def test_non_constraining_relations_ignored() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    other_task = _entity(EntityType.TASK)
    related = _entity(EntityType.IDEA)
    used = _entity(EntityType.RESOURCE)
    portfolio = _portfolio(
        [project, task, other_task, related, used],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(other_task.id, task.id, RelationType.USES),
            _relation(related.id, task.id, RelationType.RELATED_TO),
            _relation(task.id, used.id, RelationType.USES),
            _relation(task.id, related.id, RelationType.RELATED_TO),
        ],
    )
    result = fn(_request(portfolio, project.id, task.id), portfolio)

    assert result.admission_state is S.ADMITTED
    assert result.constraints == ()
    assert result.unsatisfied_constraint_count == 0


def test_reverse_direction_of_constraining_type_does_not_constrain() -> None:
    """BLOCKS constrains only when INCOMING to the exact task. The task
    BLOCKING another task (outgoing BLOCKS) is not a self-constraint and is
    ignored; a genuine incoming PRECEDES from an ACTIVE project IS kept."""

    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    downstream = _entity(EntityType.TASK, status=EntityStatus.ACTIVE)
    portfolio = _portfolio(
        [project, task, downstream],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            # `task` blocks `downstream` (OUTGOING BLOCKS): NOT constraining.
            _relation(task.id, downstream.id, RelationType.BLOCKS),
            # INCOMING PRECEDES to `task` from ACTIVE project: constraining.
            _relation(project.id, task.id, RelationType.PRECEDES),
        ],
    )
    result = fn(_request(portfolio, project.id, task.id), portfolio)

    assert result.admission_state is S.CONSTRAINED
    # Only the genuine incoming PRECEDES constraint is collected; the
    # outgoing BLOCKS edge is ignored in both directions of self-reference.
    assert len(result.constraints) == 1
    assert result.constraints[0].relation_type is RelationType.PRECEDES
    assert result.constraints[0].counterpart_entity_id is project.id
    assert result.unsatisfied_constraint_count == 1


# ---------------------------------------------------------------------------
# Purity: inputs are not read back or mutated; determinism holds.
# ---------------------------------------------------------------------------


def test_no_mutation_and_determinism() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    blocker = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task, blocker],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(blocker.id, task.id, RelationType.BLOCKS),
        ],
    )
    req = _request(portfolio, project.id, task.id)

    snapshot_entities = [entity.model_dump() for entity in portfolio.entities]
    snapshot_relations = [relation.model_dump() for relation in portfolio.relations]
    snapshot_request = req.model_dump()

    first = fn(req, portfolio)
    second = fn(req, portfolio)

    assert first == second
    assert [entity.model_dump() for entity in portfolio.entities] == snapshot_entities
    assert [relation.model_dump() for relation in portfolio.relations] == (
        snapshot_relations
    )
    assert req.model_dump() == snapshot_request


def test_frozen_results() -> None:
    project, task, portfolio = _admitted_portfolio()
    req = _request(portfolio, project.id, task.id)

    result = fn(req, portfolio)
    with pytest.raises(Exception) as exc_info:
        result.admission_state = S.CONSTRAINED  # type: ignore[misc]

    assert "frozen" in str(exc_info.value).lower() or isinstance(
        exc_info.value, AttributeError
    )


def test_constrained_row_is_frozen() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    blocker = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task, blocker],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(blocker.id, task.id, RelationType.BLOCKS),
        ],
    )
    result = fn(_request(portfolio, project.id, task.id), portfolio)
    row = result.constraints[0]

    with pytest.raises(Exception) as exc_info:
        row.satisfied = True  # type: ignore[misc]

    assert isinstance(exc_info.value, (TypeError, ValueError))


# ---------------------------------------------------------------------------
# No fallback / reselection / ranking: the exact authorized task is the only
# task ever admitted or rejected; a stale request never transfers to another.
# ---------------------------------------------------------------------------


def test_never_falls_back_to_a_differently_authorized_task() -> None:
    """Even when a perfectly ACTIVE, WBS-member, unconstrained task exists in
    the same project, a request authorizing a different (constrained) task is
    evaluated ONLY for that exact task; the attractive replacement is never
    inspected, ranked, or substituted."""

    project = _entity(EntityType.PROJECT)
    authorized = _entity(EntityType.TASK)  # the task the request names
    attractive = _entity(EntityType.TASK)  # a fresh, attractive replacement
    blocker = _entity(EntityType.TASK)  # a genuine inbound blocker of `authorized`
    portfolio = _portfolio(
        [project, authorized, attractive, blocker],
        [
            _relation(authorized.id, project.id, RelationType.BELONGS_TO),
            _relation(attractive.id, project.id, RelationType.BELONGS_TO),
            _relation(blocker.id, authorized.id, RelationType.BLOCKS),
        ],
    )
    req = _request(portfolio, project.id, authorized.id)
    result = fn(req, portfolio)

    # The result is about the EXACT authorized task, never the replacement.
    assert result.authorized_task_id is authorized.id
    assert result.admission_state is S.CONSTRAINED
    # The single constraint is the genuine inbound blocker, not `attractive`.
    assert len(result.constraints) == 1
    assert result.constraints[0].counterpart_entity_id is blocker.id
    assert result.constraints[0].counterpart_entity_id is not attractive.id


# ---------------------------------------------------------------------------
# Executable architecture guards: inspect the V1.47 module's AST (imports,
# loaded names, callable attributes) rather than docstring prose.
# ---------------------------------------------------------------------------

_MODULE_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src/trajectory_os/application"
    / "execution_effort_project_focus_next_ready_task_execution_admission.py"
)

_ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "datetime",
        "enum",
        "pydantic",
        "uuid",
        "trajectory_os.application.execution_effort_project_focus"
        "_next_ready_task_execution_request",
        "trajectory_os.domain.entities",
        "trajectory_os.domain.portfolio",
        "trajectory_os.domain.relations",
        "trajectory_os.domain.work_breakdown",
    }
)

_FORBIDDEN_BOUNDARY_MODULES = (
    # V1.44 intent boundary (must not be read as CURRENT evidence).
    "trajectory_os.application.execution_effort_project_focus"
    "_next_ready_task_execution_intent",
    # V1.45 applicability boundary: V1.47 must not call it as a shortcut.
    "trajectory_os.application.execution_effort_project_focus"
    "_next_ready_task_execution_applicability",
    # V1.46 request is allowed; prior boundaries are not.
    "trajectory_os.application.execution_effort_project_focus_task_current_relations",
    "trajectory_os.application.execution_effort_project_focus_task_current_constraints",
    "trajectory_os.application.execution_effort_project_focus_ready_task_candidates",
    "trajectory_os.application.execution_effort_project_focus_next_ready_task_selection",
    "trajectory_os.application.execution_effort_project_focus_next_ready_task_decision",
    "trajectory_os.application.execution_effort_project_focus"
    "_next_ready_task_decision_persistence",
    # Entity status transitions.
    "trajectory_os.application.entity_status_transition",
    # Shell / subprocess / SQL / persistence surface.
    "subprocess",
    "sqlite3",
    "shlex",
    "shutil",
    "os",
    "os.path",
    "shlex",
    "socket",
    "http",
    "httpx",
    "urllib",
    "sqlalchemy",
    "duckdb",
    "psycopg",
    "threading",
    "queue",
    "asyncio",
    "ollama",
    "openai",
    "anthropic",
)

_FORBIDDEN_NAMES = frozenset(
    {
        "uuid4",
        "uuid1",
        "uuid3",
        "uuid5",
        "transition_entity_status",
        "transition_entity_status_durably",
        "subprocess",
        "sqlite3",
        "shutil",
        "shlex",
        "system",
        "check_output",
        "check_call",
        "Popen",
        "eval",
        "exec",
        "provider",
        "adapter",
        "ollama",
        "openai",
        "anthropic",
        "httpx",
        "socket",
        "dispatch",
        "enqueue",
        "schedule",
        "persist",
        "sqlalchemy",
        "duckdb",
        "psycopg",
    }
)

_FORBIDDEN_CALL_ATTRIBUTES = frozenset(
    {
        "uuid4",  # uuid.uuid4
        "uuid1",  # uuid.uuid1
        "now",  # datetime.now
        "utcnow",  # datetime.utcnow
        "system",
        "check_output",
        "check_call",
        "getoutput",
        "Popen",
        "eval",
        "exec",
        "execute",
        "executemany",
        "query",
        "connect",
        "send",
        "post",
        "generate",  # no generated execution receipts / provider commands
        "dispatch",
        "start",
        "join",
    }
)


def _module_tree() -> ast.Module:
    return ast.parse(_MODULE_SOURCE.read_text(encoding="utf-8"))


def _imported_modules(tree: ast.Module) -> frozenset[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return frozenset(modules)


def _loaded_names(tree: ast.Module) -> frozenset[str]:
    return frozenset(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))


def _called_attributes(tree: ast.Module) -> frozenset[str]:
    return frozenset(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )


def test_module_imports_only_the_allowed_boundary_modules() -> None:
    modules = _imported_modules(_module_tree())
    assert modules <= _ALLOWED_IMPORTS
    for forbidden in _FORBIDDEN_BOUNDARY_MODULES:
        assert forbidden not in modules


def test_module_never_references_forbidden_names() -> None:
    names = _loaded_names(_module_tree())
    assert names.isdisjoint(_FORBIDDEN_NAMES)
    # No fallback / alternative-task selection / ranking helper of any kind.
    assert not any(
        name.startswith("select_")
        or name.startswith("infer_")
        or name.startswith("rank_")
        or "fallback" in name
        or "candidate" in name
        or "alternative" in name
        or "provider" in name
        for name in names
    )


def test_module_makes_no_forbidden_calls() -> None:
    attrs = _called_attributes(_module_tree())
    assert attrs.isdisjoint(_FORBIDDEN_CALL_ATTRIBUTES)


_PROVENANCE_FIELDS = frozenset(
    {
        "request_id",
        "requested_at",
        "intent_id",
        "authorized_at",
        "decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
    }
)


def _caller_provenance_reads(tree: ast.Module) -> set[tuple[str, str]]:
    """``(base_name, field)`` pairs where a caller-owned variable (``request``
    or ``genuine``) is read by a provenance field. Must be empty: after fresh
    revalidation, all semantic request reads MUST use the validated copy.
    """
    callers = {"request", "genuine"}
    return {
        (node.value.id, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in _PROVENANCE_FIELDS
        and isinstance(node.value, ast.Name)
        and node.value.id in callers
    }


def test_module_never_reads_semantic_values_from_caller_request() -> None:
    """Semantic provenance is only ever read from the fresh validated copy.
    The caller-owned ``request`` and the pre-revalidation ``genuine`` object
    are never read for a single semantic field.
    """
    tree = _module_tree()
    assert _caller_provenance_reads(tree) == set()
    # The validated copy MUST be read for at least the membership/IDs fields.
    source = _MODULE_SOURCE.read_text(encoding="utf-8")
    for field in ("portfolio_id", "authorized_project_id", "authorized_task_id"):
        assert f"validated.{field}" in source, field


def test_module_never_uses_v145_applicability() -> None:
    """V1.47 must not import or consume V1.45 applicability: its own CURRENT
    evidence is re-derived. Structurally guaranteed, not prose-checked.
    """
    modules = _imported_modules(_module_tree())
    v145 = (
        "trajectory_os.application.execution_effort_project_focus"
        "_next_ready_task_execution_applicability"
    )
    assert v145 not in modules
    assert not any("applicability" in module for module in modules)
    assert not any("applicability" in name.lower() for name in _loaded_names(_module_tree()))
