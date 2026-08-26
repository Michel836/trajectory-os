"""Unit tests for deterministic execution-effort measurement (V1.9-B)."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_measurement import (
    ExecutionEffortMeasurementError,
    ExecutionEffortSummary,
    WorkBreakdownEffortMeasurement,
    measure_work_breakdown_effort,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import WorkBreakdownError

BASE = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
PLUS_TWO = timezone(timedelta(hours=2))


def _entity(entity_type: EntityType, title: str) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=uuid4(),
        entity_type=entity_type,
        title=title,
        created_at=BASE,
        updated_at=BASE,
    )


def _belongs(child: TrajectoryEntity, parent: TrajectoryEntity) -> TrajectoryRelation:
    return TrajectoryRelation(
        source_id=child.id,
        target_id=parent.id,
        relation_type=RelationType.BELONGS_TO,
    )


def _portfolio_tree() -> tuple[
    Portfolio,
    TrajectoryEntity,
    TrajectoryEntity,
    TrajectoryEntity,
    TrajectoryEntity,
    TrajectoryEntity,
    TrajectoryEntity,
]:
    project = _entity(EntityType.PROJECT, "Project")
    deliverable = _entity(EntityType.DELIVERABLE, "Deliverable")
    work_package = _entity(EntityType.WORK_PACKAGE, "WP")
    nested_work_package = _entity(EntityType.WORK_PACKAGE, "Nested WP")
    task = _entity(EntityType.TASK, "Task")
    other_project = _entity(EntityType.PROJECT, "Other project")
    resource = _entity(EntityType.RESOURCE, "Resource")

    portfolio = Portfolio(
        id=uuid4(),
        name="Measurement",
        entities=[
            project,
            deliverable,
            work_package,
            nested_work_package,
            task,
            other_project,
            resource,
        ],
        relations=[
            _belongs(deliverable, project),
            _belongs(work_package, deliverable),
            _belongs(nested_work_package, work_package),
            _belongs(task, nested_work_package),
            _belongs(resource, project),
        ],
    )
    return (
        portfolio,
        project,
        deliverable,
        work_package,
        nested_work_package,
        task,
        other_project,
    )


def _observation(
    portfolio_id: UUID,
    entity_id: UUID,
    duration_seconds: int,
    observed_at: datetime,
    *,
    observation_id: UUID | None = None,
) -> ExecutionEffortObservation:
    return ExecutionEffortObservation(
        id=observation_id or uuid4(),
        portfolio_id=portfolio_id,
        entity_id=entity_id,
        duration_seconds=duration_seconds,
        observed_at=observed_at,
        source=SourceKind.USER_CONFIRMED,
    )


def test_empty_measurement_has_zero_summaries_and_current_wbs_preorder() -> None:
    portfolio, project, deliverable, wp, nested_wp, task, _ = _portfolio_tree()

    result = measure_work_breakdown_effort(portfolio, project.id, [])

    assert isinstance(result, WorkBreakdownEffortMeasurement)
    assert result.portfolio_id == portfolio.id
    assert result.project_id == project.id
    assert [item.entity_id for item in result.items] == [
        project.id,
        deliverable.id,
        wp.id,
        nested_wp.id,
        task.id,
    ]
    assert [item.parent_id for item in result.items] == [
        None,
        project.id,
        deliverable.id,
        wp.id,
        nested_wp.id,
    ]
    assert [item.depth for item in result.items] == [0, 1, 2, 3, 4]

    for item in result.items:
        assert item.direct.duration_seconds == 0
        assert item.direct.observation_count == 0
        assert item.direct.first_observed_at is None
        assert item.direct.last_observed_at is None
        assert item.subtree == item.direct


def test_direct_and_subtree_rollups_are_exact_at_every_wbs_level() -> None:
    portfolio, project, deliverable, wp, nested_wp, task, _ = _portfolio_tree()
    observations = [
        _observation(portfolio.id, project.id, 10, BASE),
        _observation(portfolio.id, deliverable.id, 20, BASE + timedelta(minutes=1)),
        _observation(portfolio.id, wp.id, 30, BASE + timedelta(minutes=2)),
        _observation(portfolio.id, nested_wp.id, 40, BASE + timedelta(minutes=3)),
        _observation(portfolio.id, task.id, 50, BASE + timedelta(minutes=4)),
    ]

    result = measure_work_breakdown_effort(portfolio, project.id, observations)
    by_id = {item.entity_id: item for item in result.items}

    assert by_id[task.id].direct.duration_seconds == 50
    assert by_id[task.id].subtree.duration_seconds == 50
    assert by_id[nested_wp.id].direct.duration_seconds == 40
    assert by_id[nested_wp.id].subtree.duration_seconds == 90
    assert by_id[wp.id].direct.duration_seconds == 30
    assert by_id[wp.id].subtree.duration_seconds == 120
    assert by_id[deliverable.id].direct.duration_seconds == 20
    assert by_id[deliverable.id].subtree.duration_seconds == 140
    assert by_id[project.id].direct.duration_seconds == 10
    assert by_id[project.id].subtree.duration_seconds == 150
    assert by_id[project.id].subtree.observation_count == 5
    assert by_id[project.id].subtree.first_observed_at == BASE
    assert by_id[project.id].subtree.last_observed_at == BASE + timedelta(minutes=4)


def test_multiple_direct_observations_sum_exact_integer_seconds() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    observations = [
        _observation(portfolio.id, task.id, 1, BASE),
        _observation(portfolio.id, task.id, 59, BASE + timedelta(seconds=1)),
        _observation(portfolio.id, task.id, 3600, BASE + timedelta(seconds=2)),
    ]

    result = measure_work_breakdown_effort(portfolio, project.id, observations)
    task_item = next(item for item in result.items if item.entity_id == task.id)

    assert task_item.direct.duration_seconds == 3660
    assert task_item.direct.observation_count == 3
    assert isinstance(task_item.direct.duration_seconds, int)


def test_input_observation_order_does_not_change_measurement() -> None:
    portfolio, project, deliverable, _, _, task, _ = _portfolio_tree()
    observations = [
        _observation(portfolio.id, task.id, 40, BASE + timedelta(hours=3)),
        _observation(portfolio.id, deliverable.id, 20, BASE + timedelta(hours=1)),
        _observation(portfolio.id, project.id, 10, BASE + timedelta(hours=2)),
    ]

    forward = measure_work_breakdown_effort(portfolio, project.id, observations)
    reverse = measure_work_breakdown_effort(portfolio, project.id, reversed(observations))

    assert forward == reverse
    assert forward is not reverse


def test_mixed_timezone_offsets_use_actual_chronological_instants() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    # 10:00+02 == 08:00Z, which is earlier than 09:00Z despite its larger
    # local clock representation.
    earlier = datetime(2026, 8, 26, 10, 0, tzinfo=PLUS_TWO)
    later = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
    observations = [
        _observation(portfolio.id, task.id, 20, later),
        _observation(portfolio.id, task.id, 10, earlier),
    ]

    result = measure_work_breakdown_effort(portfolio, project.id, observations)
    task_item = next(item for item in result.items if item.entity_id == task.id)

    assert task_item.direct.first_observed_at == earlier
    assert task_item.direct.last_observed_at == later
    assert task_item.direct.first_observed_at.utcoffset() == timedelta(hours=2)


def test_equal_instants_use_uuid_tie_break_deterministically() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    low_id = UUID(int=1)
    high_id = UUID(int=2)
    utc_time = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
    plus_two_same_instant = datetime(2026, 8, 26, 10, 0, tzinfo=PLUS_TWO)
    low = _observation(
        portfolio.id,
        task.id,
        1,
        plus_two_same_instant,
        observation_id=low_id,
    )
    high = _observation(
        portfolio.id,
        task.id,
        1,
        utc_time,
        observation_id=high_id,
    )

    first = measure_work_breakdown_effort(portfolio, project.id, [high, low])
    second = measure_work_breakdown_effort(portfolio, project.id, [low, high])
    task_first = next(item for item in first.items if item.entity_id == task.id)
    task_second = next(item for item in second.items if item.entity_id == task.id)

    assert first == second
    assert task_first.direct.first_observed_at == plus_two_same_instant
    assert task_first.direct.last_observed_at == utc_time
    assert task_second.direct == task_first.direct


def test_foreign_portfolio_observation_is_rejected_not_ignored() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    foreign = _observation(uuid4(), task.id, 10, BASE)

    with pytest.raises(ExecutionEffortMeasurementError, match="different portfolio"):
        measure_work_breakdown_effort(portfolio, project.id, [foreign])


def test_duplicate_observation_id_is_rejected_not_double_counted() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    observation = _observation(portfolio.id, task.id, 10, BASE)

    with pytest.raises(ExecutionEffortMeasurementError, match="duplicate observation id"):
        measure_work_breakdown_effort(portfolio, project.id, [observation, observation])


def test_non_observation_input_is_rejected_without_coercion() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    payload = {
        "id": str(uuid4()),
        "portfolio_id": str(portfolio.id),
        "entity_id": str(task.id),
        "duration_seconds": 10,
        "observed_at": BASE.isoformat(),
        "source": "user_confirmed",
    }

    with pytest.raises(ExecutionEffortMeasurementError, match="ExecutionEffortObservation"):
        measure_work_breakdown_effort(
            portfolio,
            project.id,
            cast(list[ExecutionEffortObservation], [payload]),
        )


def test_bypassed_malformed_observation_is_revalidated_and_rejected() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    malformed = ExecutionEffortObservation.model_construct(
        id="not-a-uuid",
        portfolio_id=portfolio.id,
        entity_id=task.id,
        duration_seconds=-5,
        observed_at=datetime(2026, 8, 26, 10, 0),
        source=SourceKind.USER_CONFIRMED,
    )

    with pytest.raises(ExecutionEffortMeasurementError, match="invalid execution-effort"):
        measure_work_breakdown_effort(portfolio, project.id, [malformed])


def test_removed_entity_history_is_valid_but_excluded_from_current_wbs_rollup() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    removed_entity_id = uuid4()
    observations = [
        _observation(portfolio.id, task.id, 20, BASE),
        _observation(portfolio.id, removed_entity_id, 999, BASE + timedelta(minutes=1)),
    ]

    result = measure_work_breakdown_effort(portfolio, project.id, observations)
    project_item = result.items[0]

    assert project_item.subtree.duration_seconds == 20
    assert project_item.subtree.observation_count == 1
    assert removed_entity_id not in {item.entity_id for item in result.items}


def test_other_current_project_effort_is_excluded() -> None:
    portfolio, project, _, _, _, task, other_project = _portfolio_tree()
    observations = [
        _observation(portfolio.id, task.id, 20, BASE),
        _observation(portfolio.id, other_project.id, 500, BASE),
    ]

    result = measure_work_breakdown_effort(portfolio, project.id, observations)

    assert result.items[0].subtree.duration_seconds == 20
    assert other_project.id not in {item.entity_id for item in result.items}


def test_current_non_wbs_entity_effort_is_excluded() -> None:
    portfolio, project, _, _, _, _, _ = _portfolio_tree()
    resource = next(e for e in portfolio.entities if e.entity_type is EntityType.RESOURCE)
    observation = _observation(portfolio.id, resource.id, 123, BASE)

    result = measure_work_breakdown_effort(portfolio, project.id, [observation])

    assert result.items[0].subtree.duration_seconds == 0
    assert resource.id not in {item.entity_id for item in result.items}


def test_source_portfolio_and_observations_remain_unchanged() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    observation = _observation(portfolio.id, task.id, 50, BASE)
    portfolio_before = deepcopy(portfolio)
    observation_before = observation.model_copy(deep=True)

    measure_work_breakdown_effort(portfolio, project.id, [observation])

    assert portfolio == portfolio_before
    assert observation == observation_before


def test_invalid_portfolio_and_project_id_types_fail_explicitly() -> None:
    portfolio, project, _, _, _, _, _ = _portfolio_tree()

    with pytest.raises(ExecutionEffortMeasurementError, match="portfolio"):
        measure_work_breakdown_effort(
            cast(Portfolio, object()), project.id, []
        )

    with pytest.raises(ExecutionEffortMeasurementError, match="project_id"):
        measure_work_breakdown_effort(
            portfolio,
            cast(UUID, str(project.id)),
            [],
        )


def test_v1_1_wbs_failure_propagates_without_measurement_reinterpretation() -> None:
    portfolio, _, _, _, _, task, _ = _portfolio_tree()

    with pytest.raises(WorkBreakdownError):
        measure_work_breakdown_effort(portfolio, task.id, [])


def test_summary_model_enforces_empty_and_non_empty_semantics() -> None:
    assert ExecutionEffortSummary(
        duration_seconds=0,
        observation_count=0,
        first_observed_at=None,
        last_observed_at=None,
    ).observation_count == 0

    with pytest.raises(ValidationError):
        ExecutionEffortSummary(
            duration_seconds=10,
            observation_count=0,
            first_observed_at=None,
            last_observed_at=None,
        )

    with pytest.raises(ValidationError):
        ExecutionEffortSummary(
            duration_seconds=10,
            observation_count=1,
            first_observed_at=None,
            last_observed_at=None,
        )

    with pytest.raises(ValidationError):
        ExecutionEffortSummary(
            duration_seconds=10,
            observation_count=1,
            first_observed_at=datetime(2026, 8, 26, 10, 0),
            last_observed_at=BASE,
        )


def test_measurement_models_are_frozen() -> None:
    portfolio, project, _, _, _, _, _ = _portfolio_tree()
    result = measure_work_breakdown_effort(portfolio, project.id, [])

    assert result.model_config.get("frozen") is True
    assert result.items[0].model_config.get("frozen") is True
    assert result.items[0].direct.model_config.get("frozen") is True

    with pytest.raises(ValidationError):
        result.project_id = uuid4()
