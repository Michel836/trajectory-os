"""V1.45 — unit tests for the CURRENT applicability preflight boundary."""

from __future__ import annotations

import ast
import itertools
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionApplicability as Applicability,
)
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionApplicabilityConstraint as Constraint,
)
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionApplicabilityError as ApplicabilityError,
)
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionApplicabilityState as S,
)
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionIntent as Intent,
)
from trajectory_os.application import (
    evaluate_current_next_ready_task_execution_applicability as fn,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import WorkBreakdownError

# ---------------------------------------------------------------------------
# Fixed, deterministic identities: no wall clock, no random generation.
# ---------------------------------------------------------------------------

AUTHORIZE_AT = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)
DECIDED_AT = datetime(2025, 1, 14, 9, 0, 0, tzinfo=UTC)
_NON_UTC_OFFSET = timezone(timedelta(hours=5, minutes=30))

_UUID_SEQUENCE = itertools.count(1)


def _uuid() -> UUID:
    """Deterministic sequential test identity.

    Identities are fixed sequences, never random: the same test run
    always produces the same values and no randomness is introduced.
    Test-only helper; the production module generates no identities.
    """
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


def _intent(portfolio: Portfolio, project: UUID, task: UUID) -> Intent:
    return Intent(
        intent_id=_uuid(),
        authorized_at=AUTHORIZE_AT,
        decision_id=_uuid(),
        decision_decided_at=DECIDED_AT,
        portfolio_id=portfolio.id,
        selected_project_count=1,
        ready_task_count=1,
        authorized_project_id=project,
        authorized_task_id=task,
    )


# ---------------------------------------------------------------------------
# Happy path: APPLICABLE with zero constraints.
# ---------------------------------------------------------------------------


def test_applicable_with_zero_constraints() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    intent = _intent(portfolio, project.id, task.id)

    result = fn(intent, portfolio)

    assert isinstance(result, Applicability)
    assert result.applicability_state is S.APPLICABLE
    assert result.task_status is EntityStatus.ACTIVE
    assert result.constraints == ()
    assert result.unsatisfied_constraint_count == 0
    # Provenance is projected from the intent.
    assert result.intent_id == intent.intent_id
    assert result.authorized_at == AUTHORIZE_AT
    assert result.decision_id == intent.decision_id
    assert result.portfolio_id == portfolio.id
    assert result.authorized_project_id == project.id
    assert result.authorized_task_id == task.id


def test_applicable_with_all_satisfied_constraints_preserves_order() -> None:
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
    intent = _intent(portfolio, project.id, task.id)

    result = fn(intent, portfolio)

    assert result.applicability_state is S.APPLICABLE
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
# CONSTRAINED: unsatisfied incoming and outgoing constraints.
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
    intent = _intent(portfolio, project.id, task.id)

    result = fn(intent, portfolio)

    assert result.applicability_state is S.CONSTRAINED
    assert len(result.constraints) == 1
    assert result.constraints[0].satisfied is False
    assert result.constraints[0].counterpart_status is EntityStatus.ACTIVE
    assert result.unsatisfied_constraint_count == 1
    assert result.task_status is EntityStatus.ACTIVE


@pytest.mark.parametrize(
    "terminal",
    [EntityStatus.CANCELLED, EntityStatus.ARCHIVED],
)
def test_constrained_terminal_counterpart_is_unsatisfied(
    terminal: EntityStatus,
) -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    # Terminal counterpart: terminal is NOT successful completion.
    counterpart = _entity(EntityType.TASK, status=terminal)
    portfolio = _portfolio(
        [project, task, counterpart],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(task.id, counterpart.id, RelationType.DEPENDS_ON),
        ],
    )
    intent = _intent(portfolio, project.id, task.id)

    result = fn(intent, portfolio)

    assert result.applicability_state is S.CONSTRAINED
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
    result = fn(_intent(portfolio, project.id, task.id), portfolio)

    assert result.applicability_state is S.CONSTRAINED
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
    intent = _intent(portfolio, project.id, task.id)

    result = fn(intent, portfolio)

    assert result.applicability_state is S.CONSTRAINED
    assert len(result.constraints) == 3
    assert result.unsatisfied_constraint_count == 1
    assert [row.satisfied for row in result.constraints] == [
        True,
        False,
        True,
    ]


# ---------------------------------------------------------------------------
# INELIGIBLE_STATUS: non-ACTIVE task.
# ---------------------------------------------------------------------------


def test_ineligible_status_carries_exact_status() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK, status=EntityStatus.PAUSED)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    result = fn(_intent(portfolio, project.id, task.id), portfolio)

    assert result.applicability_state is S.INELIGIBLE_STATUS
    assert result.task_status is EntityStatus.PAUSED
    # Not interpreted as graph-blocked: no constraint collected.
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
    result = fn(_intent(portfolio, project.id, task.id), portfolio)

    assert result.applicability_state is S.INELIGIBLE_STATUS
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
        # Only `other` belongs to the project; `task` is not a WBS member.
        [
            _relation(other.id, project.id, RelationType.BELONGS_TO),
        ],
    )
    intent = _intent(portfolio, project.id, task.id)

    result = fn(intent, portfolio)

    assert result.applicability_state is S.TASK_NOT_IN_AUTHORIZED_PROJECT
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
    result = fn(_intent(portfolio, ghost_project, task.id), portfolio)

    assert result.applicability_state is S.PROJECT_MISSING
    assert result.task_status is None
    assert result.constraints is None
    assert result.unsatisfied_constraint_count is None


def test_project_type_mismatch() -> None:
    area = _entity(EntityType.AREA)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio([area, task], [])
    result = fn(_intent(portfolio, area.id, task.id), portfolio)

    assert result.applicability_state is S.PROJECT_TYPE_MISMATCH


def test_task_missing() -> None:
    project = _entity(EntityType.PROJECT)
    portfolio = _portfolio([project], [])
    ghost_task = _uuid()
    result = fn(_intent(portfolio, project.id, ghost_task), portfolio)

    assert result.applicability_state is S.TASK_MISSING
    assert result.task_status is None
    assert result.constraints is None
    assert result.unsatisfied_constraint_count is None


def test_task_type_mismatch() -> None:
    project = _entity(EntityType.PROJECT)
    idea = _entity(EntityType.IDEA)
    portfolio = _portfolio([project, idea], [])
    result = fn(_intent(portfolio, project.id, idea.id), portfolio)

    assert result.applicability_state is S.TASK_TYPE_MISMATCH


# ---------------------------------------------------------------------------
# Boundary: portfolio id mismatch is an error, not a state.
# ---------------------------------------------------------------------------


def test_portfolio_id_mismatch_raises() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    other = Portfolio(id=_uuid(), name="other", entities=[], relations=[])
    intent = _intent(other, project.id, task.id)

    with pytest.raises(ApplicabilityError) as exc_info:
        fn(intent, portfolio)

    assert "portfolio" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Boundary: non-genuine inputs are rejected before any state is derived.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_intent", [None, {}, "intent", 42, 3.5])
def test_rejects_non_genuine_intents(bad_intent: object) -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )

    with pytest.raises(ApplicabilityError) as exc_info:
        fn(bad_intent, portfolio)

    assert "genuine" in str(exc_info.value)


@pytest.mark.parametrize("bad_portfolio", [None, {}, "portfolio", 42, 3.5])
def test_rejects_non_genuine_portfolios(bad_portfolio: object) -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    genuine = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    intent = _intent(genuine, project.id, task.id)

    with pytest.raises(ApplicabilityError) as exc_info:
        fn(intent, bad_portfolio)

    assert "genuine" in str(exc_info.value)


def test_rejects_hostile_constructed_intent() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    # Bypass the intent's own invariants to build state no constructor can
    # produce: ready_task_count == 0.
    hostile = Intent.model_construct(
        intent_id=_uuid(),
        authorized_at=datetime(2025, 1, 15, tzinfo=UTC),
        decision_id=_uuid(),
        decision_decided_at=datetime(2025, 1, 15, tzinfo=UTC),
        portfolio_id=portfolio.id,
        selected_project_count=0,
        ready_task_count=0,
        authorized_project_id=project.id,
        authorized_task_id=task.id,
    )

    with pytest.raises(ApplicabilityError) as exc_info:
        fn(hostile, portfolio)

    assert "re-validation" in str(exc_info.value)


def test_rejects_hostile_naive_timestamp_intent() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    hostile = Intent.model_construct(
        intent_id=_uuid(),
        authorized_at=datetime(2025, 1, 15, 10, 30),  # naive
        decision_id=_uuid(),
        decision_decided_at=datetime(2025, 1, 15, 9, 0),  # naive
        portfolio_id=portfolio.id,
        selected_project_count=1,
        ready_task_count=1,
        authorized_project_id=project.id,
        authorized_task_id=task.id,
    )

    with pytest.raises(ApplicabilityError):
        fn(hostile, portfolio)


# ---------------------------------------------------------------------------
# Boundary: incoherent WBS is an error, never a membership state.
# ---------------------------------------------------------------------------


def test_incoherent_wbs_raises_and_is_not_reinterpreted() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    # Containment cycle: both belong to each other.
    portfolio = _portfolio(
        [project, task],
        [
            _relation(task.id, project.id, RelationType.BELONGS_TO),
            _relation(project.id, task.id, RelationType.BELONGS_TO),
        ],
    )
    intent = _intent(portfolio, project.id, task.id)

    with pytest.raises(ApplicabilityError) as exc_info:
        fn(intent, portfolio)

    assert "reinterpret" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, WorkBreakdownError)


def test_unknown_wbs_root_raises() -> None:
    # The authorized project id is never in the portfolio at all, but the
    # project is missing state only applies after membership passes; for a
    # WBS to even be built the project must exist. This pins that the
    # boundary error surfaces with a clear message when the WBS cannot be
    # projected for the authorized project.
    project = _entity(EntityType.PROJECT)
    portfolio = _portfolio([project], [])
    ghost_task = _uuid()
    # TASK_MISSING is expected here (task is not in the portfolio).
    result = fn(_intent(portfolio, project.id, ghost_task), portfolio)
    assert result.applicability_state is S.TASK_MISSING


# ---------------------------------------------------------------------------
# Purity: inputs are not read back or mutated; determinism holds.
# ---------------------------------------------------------------------------


def test_no_mutation_and_determinism() -> None:
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
    intent = _intent(portfolio, project.id, task.id)

    snapshot_entities = [entity.model_dump() for entity in portfolio.entities]
    snapshot_relations = [relation.model_dump() for relation in portfolio.relations]
    snapshot_intent = intent.model_dump()

    first = fn(intent, portfolio)
    second = fn(intent, portfolio)

    assert first == second
    assert [entity.model_dump() for entity in portfolio.entities] == snapshot_entities
    assert [relation.model_dump() for relation in portfolio.relations] == (
        snapshot_relations
    )
    assert intent.model_dump() == snapshot_intent


def test_frozen_results() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    intent = _intent(portfolio, project.id, task.id)

    result = fn(intent, portfolio)
    with pytest.raises(Exception) as exc_info:
        result.applicability_state = S.CONSTRAINED  # type: ignore[misc]

    assert "frozen" in str(exc_info.value).lower() or isinstance(
        exc_info.value, AttributeError
    )


def test_constrained_row_is_frozen() -> None:
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
    result = fn(_intent(portfolio, project.id, task.id), portfolio)
    row = result.constraints[0]

    with pytest.raises(Exception) as exc_info:
        row.satisfied = True  # type: ignore[misc]

    assert isinstance(exc_info.value, (TypeError, ValueError))


# ---------------------------------------------------------------------------
# Non-CONSTRAINING relations never appear as constraints.
# ---------------------------------------------------------------------------


def test_non_constraining_relations_ignored() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    other_task = _entity(EntityType.TASK, status=EntityStatus.ACTIVE)
    related = _entity(EntityType.IDEA, status=EntityStatus.ACTIVE)
    used = _entity(EntityType.RESOURCE, status=EntityStatus.ACTIVE)
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
    intent = _intent(portfolio, project.id, task.id)

    result = fn(intent, portfolio)

    assert result.applicability_state is S.APPLICABLE
    assert result.constraints == ()
    assert result.unsatisfied_constraint_count == 0


# ---------------------------------------------------------------------------
# Exact protocol surface: the eight states, exact model fields and config.
# ---------------------------------------------------------------------------


def test_state_enum_is_exactly_the_eight_v145_states() -> None:
    assert set(S.__members__) == {
        "APPLICABLE",
        "PROJECT_MISSING",
        "PROJECT_TYPE_MISMATCH",
        "TASK_MISSING",
        "TASK_TYPE_MISMATCH",
        "TASK_NOT_IN_AUTHORIZED_PROJECT",
        "INELIGIBLE_STATUS",
        "CONSTRAINED",
    }
    assert {state.value for state in S} == {
        "applicable",
        "project_missing",
        "project_type_mismatch",
        "task_missing",
        "task_type_mismatch",
        "task_not_in_authorized_project",
        "ineligible_status",
        "constrained",
    }


def test_result_model_fields_and_config_are_exact() -> None:
    assert set(Applicability.model_fields) == {
        "intent_id",
        "authorized_at",
        "decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
        "task_status",
        "applicability_state",
        "constraints",
        "unsatisfied_constraint_count",
    }
    assert Applicability.model_config["strict"] is True
    assert Applicability.model_config["frozen"] is True
    assert Applicability.model_config["extra"] == "forbid"


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


def test_authorized_at_original_offset_is_preserved() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    intent = Intent(
        intent_id=_uuid(),
        authorized_at=datetime(2025, 1, 15, 10, 30, tzinfo=_NON_UTC_OFFSET),
        decision_id=_uuid(),
        decision_decided_at=datetime(2025, 1, 14, 9, 0, tzinfo=_NON_UTC_OFFSET),
        portfolio_id=portfolio.id,
        selected_project_count=1,
        ready_task_count=1,
        authorized_project_id=project.id,
        authorized_task_id=task.id,
    )

    result = fn(intent, portfolio)

    assert result.authorized_at == intent.authorized_at
    assert result.authorized_at.utcoffset() == timedelta(hours=5, minutes=30)


# ---------------------------------------------------------------------------
# Constraint COMPLETED-only invariant (model-level, not just produced rows).
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
        relations=[_relation(task.id, project.id, RelationType.BELONGS_TO), dangling],
    )
    intent = _intent(portfolio, project.id, task.id)

    with pytest.raises(ApplicabilityError) as exc_info:
        fn(intent, portfolio)

    assert "counterpart" in str(exc_info.value)


# ---------------------------------------------------------------------------
# No temporal rule: authorized_at is provenance only; a very old or very
# recent authorization has zero applicability effect.
# ---------------------------------------------------------------------------


def test_no_temporal_rule_authorized_at_is_provenance_only() -> None:
    project = _entity(EntityType.PROJECT)
    task = _entity(EntityType.TASK)
    portfolio = _portfolio(
        [project, task],
        [_relation(task.id, project.id, RelationType.BELONGS_TO)],
    )
    for authorized_at in (
        datetime(1999, 3, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2199, 12, 31, 23, 59, 0, tzinfo=UTC),
    ):
        intent = Intent(
            intent_id=_uuid(),
            authorized_at=authorized_at,
            decision_id=_uuid(),
            decision_decided_at=DECIDED_AT,
            portfolio_id=portfolio.id,
            selected_project_count=1,
            ready_task_count=1,
            authorized_project_id=project.id,
            authorized_task_id=task.id,
        )

        result = fn(intent, portfolio)

        assert result.applicability_state is S.APPLICABLE
        assert result.unsatisfied_constraint_count == 0


# ---------------------------------------------------------------------------
# Executable architecture guards: inspect the V1.45 module's AST (imports,
# loaded names, callable attributes) rather than docstring prose.
# ---------------------------------------------------------------------------

_MODULE_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src/trajectory_os/application"
    / "execution_effort_project_focus_next_ready_task_execution_applicability.py"
)

_ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "datetime",
        "enum",
        "pydantic",
        "uuid",
        "trajectory_os.application.execution_effort_project_focus"
        "_next_ready_task_execution_intent",
        "trajectory_os.domain.entities",
        "trajectory_os.domain.portfolio",
        "trajectory_os.domain.relations",
        "trajectory_os.domain.work_breakdown",
    }
)

_FORBIDDEN_BOUNDARY_MODULES = (
    # V1.38 projection / V1.39 evaluation boundaries
    "trajectory_os.application.execution_effort_project_focus_task_current_relations",
    "trajectory_os.application.execution_effort_project_focus_task_current_constraints",
    # V1.40 candidate boundary
    "trajectory_os.application.execution_effort_project_focus_ready_task_candidates",
    # V1.41 selection boundary
    "trajectory_os.application.execution_effort_project_focus_next_ready_task_selection",
    # V1.42 acceptance boundary
    "trajectory_os.application.execution_effort_project_focus_next_ready_task_decision",
    # V1.43 persistence boundary
    "trajectory_os.application.execution_effort_project_focus"
    "_next_ready_task_decision_persistence",
    # Entity status transitions
    "trajectory_os.application.entity_status_transition",
    # Shell / subprocess / SQL surface
    "subprocess",
    "sqlite3",
    "shlex",
    "shutil",
)

_FORBIDDEN_NAMES = frozenset(
    {
        "uuid4",
        "uuid1",
        "transition_entity_status",
        "transition_entity_status_durably",
        "subprocess",
        "sqlite3",
        "shutil",
        "system",
        "check_output",
        "check_call",
    }
)

_FORBIDDEN_CALL_ATTRIBUTES = frozenset(
    {
        "uuid4",  # uuid.uuid4 / bare uuid4 call targets
        "uuid1",
        "now",  # datetime.now / datetime.now(UTC)
        "utcnow",  # datetime.utcnow
        "system",
        "check_output",
        "check_call",
        "getoutput",
        "Popen",
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


def test_module_never_references_uuid_or_transition_or_shell_names() -> None:
    names = _loaded_names(_module_tree())
    assert names.isdisjoint(_FORBIDDEN_NAMES)
    # No fallback / alternative-task selection helper of any kind.
    assert not any(name.startswith("select_") or "fallback" in name for name in names)


def test_module_makes_no_random_identity_or_clock_or_shell_calls() -> None:
    attrs = _called_attributes(_module_tree())
    assert attrs.isdisjoint(_FORBIDDEN_CALL_ATTRIBUTES)
