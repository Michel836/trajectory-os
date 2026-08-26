"""Unit tests for deterministic CURRENT-WBS planned-effort planning (V1.10-D)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.execution_effort_planning import (
    ExecutionEffortPlanningError,
    PlannedEffortSummary,
    WorkBreakdownEffortPlan,
    plan_work_breakdown_effort,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import WorkBreakdownError

BASE = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
LATER = BASE + timedelta(hours=1)
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
        name="Planning",
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


def _estimate(
    portfolio_id: UUID,
    entity_id: UUID,
    duration_seconds: int,
    estimated_at: datetime = BASE,
    *,
    estimate_id: UUID | None = None,
    source: SourceKind = SourceKind.USER_CONFIRMED,
) -> ExecutionEffortEstimate:
    return ExecutionEffortEstimate(
        id=estimate_id or uuid4(),
        portfolio_id=portfolio_id,
        entity_id=entity_id,
        duration_seconds=duration_seconds,
        estimated_at=estimated_at,
        source=source,
    )


def test_empty_estimates_leave_every_node_explicitly_unestimated() -> None:
    portfolio, project, deliverable, wp, nested_wp, task, _ = _portfolio_tree()

    result = plan_work_breakdown_effort(portfolio, project.id, [])

    assert isinstance(result, WorkBreakdownEffortPlan)
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

    expected_unestimated = {
        project.id: 5,
        deliverable.id: 4,
        wp.id: 3,
        nested_wp.id: 2,
        task.id: 1,
    }
    for item in result.items:
        assert item.direct_estimate is None
        assert item.subtree.known_duration_seconds == 0
        assert item.subtree.estimated_entity_count == 0
        assert item.subtree.unestimated_entity_count == expected_unestimated[item.entity_id]
        assert item.subtree.total_duration_seconds is None


def test_each_current_node_appears_exactly_once_in_wbs_preorder() -> None:
    portfolio, project, _, _, _, _, _ = _portfolio_tree()

    result = plan_work_breakdown_effort(portfolio, project.id, [])

    entity_ids = [item.entity_id for item in result.items]
    assert len(entity_ids) == len(set(entity_ids))
    assert entity_ids[0] == project.id


def test_explicit_zero_estimate_is_valid_and_meaningful() -> None:
    portfolio, project, deliverable, wp, nested_wp, task, _ = _portfolio_tree()
    root_zero = _estimate(portfolio.id, project.id, 0)
    estimates = [
        root_zero,
        _estimate(portfolio.id, deliverable.id, 0),
        _estimate(portfolio.id, wp.id, 0),
        _estimate(portfolio.id, nested_wp.id, 0),
        _estimate(portfolio.id, task.id, 50),
    ]

    result = plan_work_breakdown_effort(portfolio, project.id, estimates)
    by_id = {item.entity_id: item for item in result.items}

    # A zero direct estimate is explicit coverage of that entity.
    assert by_id[project.id].direct_estimate == root_zero
    assert by_id[project.id].direct_estimate.duration_seconds == 0
    assert by_id[project.id].subtree.estimated_entity_count == 5
    assert by_id[project.id].subtree.unestimated_entity_count == 0
    # Zero is additive, not an absence of data:
    assert by_id[project.id].subtree.known_duration_seconds == 50
    assert by_id[project.id].subtree.total_duration_seconds == 50


def test_partial_coverage_never_exposes_a_total() -> None:
    portfolio, project, _, _, _, _, _ = _portfolio_tree()
    estimate = _estimate(portfolio.id, project.id, 0)

    result = plan_work_breakdown_effort(portfolio, project.id, [estimate])
    root = result.items[0]

    assert root.direct_estimate == estimate
    assert root.subtree.known_duration_seconds == 0
    assert root.subtree.estimated_entity_count == 1
    assert root.subtree.unestimated_entity_count == 4
    assert root.subtree.total_duration_seconds is None


def test_task_estimate_sums_into_parent_subtree() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    estimates = [
        _estimate(portfolio.id, project.id, 10),
        _estimate(portfolio.id, task.id, 25),
        # deliverable and work packages remain explicitly unestimated.
    ]

    result = plan_work_breakdown_effort(portfolio, project.id, estimates)
    by_id = {item.entity_id: item for item in result.items}

    root = by_id[project.id]
    assert root.subtree.known_duration_seconds == 35
    assert root.subtree.estimated_entity_count == 2
    assert root.subtree.unestimated_entity_count == 3
    assert root.subtree.total_duration_seconds is None

    task_item = by_id[task.id]
    assert task_item.direct_estimate is not None
    assert task_item.subtree.known_duration_seconds == 25
    assert task_item.subtree.estimated_entity_count == 1
    assert task_item.subtree.unestimated_entity_count == 0
    assert task_item.subtree.total_duration_seconds == 25


def test_unestimated_task_summary_is_exact() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()

    result = plan_work_breakdown_effort(portfolio, project.id, [])
    task_item = result.items[-1]

    assert task_item.entity_id == task.id
    assert task_item.direct_estimate is None
    assert task_item.subtree.known_duration_seconds == 0
    assert task_item.subtree.estimated_entity_count == 0
    assert task_item.subtree.unestimated_entity_count == 1
    assert task_item.subtree.total_duration_seconds is None


def test_estimated_task_summary_is_exact() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    estimate = _estimate(portfolio.id, task.id, 70)

    result = plan_work_breakdown_effort(portfolio, project.id, [estimate])
    task_item = next(item for item in result.items if item.entity_id == task.id)

    assert task_item.direct_estimate == estimate
    assert task_item.direct_estimate.duration_seconds == 70
    assert task_item.subtree.known_duration_seconds == 70
    assert task_item.subtree.estimated_entity_count == 1
    assert task_item.subtree.unestimated_entity_count == 0
    assert task_item.subtree.total_duration_seconds == 70


def test_nested_known_duration_rollups_are_exact() -> None:
    portfolio, project, deliverable, wp, nested_wp, task, _ = _portfolio_tree()
    estimates = [
        _estimate(portfolio.id, project.id, 10, BASE),
        _estimate(portfolio.id, deliverable.id, 20, BASE + timedelta(minutes=1)),
        _estimate(portfolio.id, wp.id, 30, BASE + timedelta(minutes=2)),
        _estimate(portfolio.id, nested_wp.id, 40, BASE + timedelta(minutes=3)),
        _estimate(portfolio.id, task.id, 50, BASE + timedelta(minutes=4)),
    ]

    result = plan_work_breakdown_effort(portfolio, project.id, estimates)
    by_id = {item.entity_id: item for item in result.items}

    assert by_id[task.id].subtree.known_duration_seconds == 50
    assert by_id[nested_wp.id].subtree.known_duration_seconds == 90
    assert by_id[wp.id].subtree.known_duration_seconds == 120
    assert by_id[deliverable.id].subtree.known_duration_seconds == 140
    assert by_id[project.id].subtree.known_duration_seconds == 150

    expected_estimated = {
        project.id: 5,
        deliverable.id: 4,
        wp.id: 3,
        nested_wp.id: 2,
        task.id: 1,
    }
    for item in result.items:
        assert item.subtree.estimated_entity_count == expected_estimated[item.entity_id]
        assert item.subtree.unestimated_entity_count == 0
        assert item.subtree.total_duration_seconds is not None

    assert by_id[project.id].subtree.total_duration_seconds == 150


def test_subtree_total_is_exposed_only_when_fully_estimated() -> None:
    portfolio, project, deliverable, wp, nested_wp, task, _ = _portfolio_tree()

    # Case 1: every node estimated -> every subtree exposes an exact total.
    all_estimated = [
        _estimate(portfolio.id, project.id, 1),
        _estimate(portfolio.id, deliverable.id, 1),
        _estimate(portfolio.id, wp.id, 1),
        _estimate(portfolio.id, nested_wp.id, 1),
        _estimate(portfolio.id, task.id, 1),
    ]
    complete = plan_work_breakdown_effort(portfolio, project.id, all_estimated)
    for item in complete.items:
        assert item.subtree.unestimated_entity_count == 0
        assert item.subtree.total_duration_seconds is not None
        assert item.subtree.total_duration_seconds == item.subtree.known_duration_seconds
    complete_by_id = {item.entity_id: item for item in complete.items}
    assert complete_by_id[project.id].subtree.total_duration_seconds == 5
    assert complete_by_id[task.id].subtree.total_duration_seconds == 1

    # Case 2: the task is missing its estimate -> no subtree is fully
    # estimated, so EVERY node must withhold its total while known rollups
    # remain exact integer additions of only estimated direct seconds.
    result = plan_work_breakdown_effort(portfolio, project.id, all_estimated[:-1])

    expected_estimated = {
        project.id: 4,
        deliverable.id: 3,
        wp.id: 2,
        nested_wp.id: 1,
        task.id: 0,
    }
    expected_known = dict(expected_estimated)
    for item in result.items:
        assert item.subtree.unestimated_entity_count == 1
        assert item.subtree.total_duration_seconds is None
        assert item.subtree.estimated_entity_count == expected_estimated[item.entity_id]
        assert item.subtree.known_duration_seconds == expected_known[item.entity_id]


def test_latest_effective_revision_wins_by_chronological_instant() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()

    # Chronological instants: A = 06:00 UTC, C = 10:00 UTC, B = 11:00 UTC.
    # Sorted lexicographically by ISO text the winner would be C ("+01" > "+00"),
    # so "B wins" can only follow from real instant comparison.
    tz_plus_one = timezone(timedelta(hours=1))
    tz_plus_two = timezone(timedelta(hours=2))
    a = _estimate(portfolio.id, task.id, 10, datetime(2026, 8, 26, 8, 0, tzinfo=tz_plus_two))
    b = _estimate(portfolio.id, task.id, 90, BASE + timedelta(hours=1))
    c = _estimate(portfolio.id, task.id, 99, datetime(2026, 8, 26, 11, 0, tzinfo=tz_plus_one))

    for order in ([a, b, c], [c, a, b], [b, c, a]):
        result = plan_work_breakdown_effort(portfolio, project.id, order)
        task_item = next(item for item in result.items if item.entity_id == task.id)
        assert task_item.direct_estimate == b
        assert task_item.direct_estimate.duration_seconds == 90


def test_equal_instant_uses_larger_estimate_uuid_tiebreak() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    low = _estimate(
        portfolio.id, task.id, 10, BASE, estimate_id=UUID(int=1)
    )
    high = _estimate(
        portfolio.id, task.id, 30, BASE, estimate_id=UUID(int=2)
    )
    # Same instant under a different UTC offset (12:00 +02 == 10:00 UTC).
    plus_two = timezone(timedelta(hours=2))
    high2 = high.model_copy(
        update={
            "estimated_at": datetime(2026, 8, 26, 12, 0, tzinfo=plus_two),
        }
    )

    for order in ([low, high], [high, low], [low, high2]):
        result = plan_work_breakdown_effort(portfolio, project.id, order)
        task_item = next(
            item for item in result.items if item.entity_id == task.id
        )
        assert task_item.direct_estimate.duration_seconds == 30
        assert task_item.subtree.known_duration_seconds == 30


def test_older_revisions_are_never_summed() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    first = _estimate(portfolio.id, task.id, 10, BASE)
    second = _estimate(portfolio.id, task.id, 20, BASE + timedelta(seconds=1))

    result = plan_work_breakdown_effort(
        portfolio, project.id, [first, second]
    )
    task_item = next(item for item in result.items if item.entity_id == task.id)

    assert task_item.subtree.known_duration_seconds == 20
    assert task_item.subtree.estimated_entity_count == 1


def test_input_order_cannot_change_selection_or_output() -> None:
    portfolio, project, deliverable, wp, nested_wp, task, _ = _portfolio_tree()
    project_baseline = _estimate(portfolio.id, project.id, 10, BASE)
    project_selected = _estimate(portfolio.id, project.id, 12, BASE + timedelta(minutes=1))
    deliverable_selected = _estimate(portfolio.id, deliverable.id, 20, BASE)
    work_package_selected = _estimate(portfolio.id, wp.id, 30, BASE)
    task_baseline = _estimate(portfolio.id, task.id, 50, BASE)
    task_selected = _estimate(portfolio.id, task.id, 40, BASE + timedelta(minutes=2))
    estimates = [
        project_baseline,
        project_selected,
        deliverable_selected,
        work_package_selected,
        task_baseline,
        task_selected,
    ]
    first = plan_work_breakdown_effort(portfolio, project.id, list(estimates))
    reversed_estimate_input = plan_work_breakdown_effort(
        portfolio, project.id, list(reversed(estimates))
    )

    assert first == reversed_estimate_input

    by_id = {item.entity_id: item for item in first.items}
    # The selected direct estimate is asserted explicitly, by value, for
    # every WBS entity type in the current tree: PROJECT, DELIVERABLE,
    # WORK_PACKAGE, and TASK.
    # PROJECT: the later revision (12s) wins over the earlier one (10s).
    assert by_id[project.id].direct_estimate == project_selected
    assert by_id[project.id].direct_estimate is not project_baseline
    assert by_id[project.id].direct_estimate.duration_seconds == 12
    # DELIVERABLE: the single revision is selected verbatim.
    assert by_id[deliverable.id].direct_estimate == deliverable_selected
    assert by_id[deliverable.id].direct_estimate.duration_seconds == 20
    # WORK_PACKAGE: the single revision is selected verbatim.
    assert by_id[wp.id].direct_estimate == work_package_selected
    assert by_id[wp.id].direct_estimate.duration_seconds == 30
    # TASK: the later revision (40s) wins over the earlier one (50s).
    assert by_id[task.id].direct_estimate == task_selected
    assert by_id[task.id].direct_estimate is not task_baseline
    assert by_id[task.id].direct_estimate.duration_seconds == 40
    # The nested WORK_PACKAGE without any estimate stays explicitly unestimated:
    # selection must never attach an estimate to it.
    assert by_id[nested_wp.id].direct_estimate is None


def test_duplicate_estimate_ids_are_rejected() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    duplicated = _estimate(portfolio.id, task.id, 10)
    copy = _estimate(
        portfolio.id,
        task.id,
        20,
        BASE + timedelta(seconds=1),
        estimate_id=duplicated.id,
    )

    with pytest.raises(ExecutionEffortPlanningError, match="duplicate estimate id"):
        plan_work_breakdown_effort(portfolio, project.id, [duplicated, copy])


def test_duplicate_estimate_ids_fail_even_outside_the_selected_wbs() -> None:
    portfolio, project, _, _, _, _, other_project = _portfolio_tree()
    duplicated = _estimate(portfolio.id, other_project.id, 10)
    copy = _estimate(
        portfolio.id,
        other_project.id,
        20,
        BASE + timedelta(seconds=1),
        estimate_id=duplicated.id,
    )

    with pytest.raises(ExecutionEffortPlanningError, match="duplicate estimate id"):
        plan_work_breakdown_effort(portfolio, project.id, [duplicated, copy])


def test_foreign_portfolio_estimate_is_rejected_not_ignored() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    foreign = _estimate(uuid4(), task.id, 10)

    with pytest.raises(
        ExecutionEffortPlanningError, match="different portfolio"
    ):
        plan_work_breakdown_effort(portfolio, project.id, [foreign])


def test_non_estimate_inputs_are_rejected() -> None:
    portfolio, project, _, _, _, _, _ = _portfolio_tree()

    with pytest.raises(ExecutionEffortPlanningError, match="ExecutionEffortEstimate"):
        plan_work_breakdown_effort(
            portfolio, project.id, [{"id": uuid4()}, "estimate", 3]
        )


def test_bypassed_malformed_estimate_is_revalidated_and_rejected() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    malformed = ExecutionEffortEstimate.model_construct(
        id=uuid4(),
        portfolio_id=portfolio.id,
        entity_id=task.id,
        duration_seconds=-1,
        estimated_at=BASE,
        source=SourceKind.USER_CONFIRMED,
    )

    with pytest.raises(ExecutionEffortPlanningError, match="invalid execution-effort"):
        plan_work_breakdown_effort(portfolio, project.id, [malformed])


def test_naive_datetime_bypass_is_revalidated_and_rejected() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    malformed = ExecutionEffortEstimate.model_construct(
        id=uuid4(),
        portfolio_id=portfolio.id,
        entity_id=task.id,
        duration_seconds=10,
        estimated_at=datetime(2026, 8, 26, 10, 0),
        source=SourceKind.USER_CONFIRMED,
    )

    with pytest.raises(ExecutionEffortPlanningError, match="invalid execution-effort"):
        plan_work_breakdown_effort(portfolio, project.id, [malformed])


def test_all_valid_provenance_kinds_count_equally() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    imported = _estimate(
        portfolio.id, task.id, 10, BASE, source=SourceKind.IMPORTED
    )
    ai_inferred = _estimate(
        portfolio.id,
        task.id,
        40,
        BASE + timedelta(seconds=1),
        source=SourceKind.AI_INFERRED,
    )

    result = plan_work_breakdown_effort(
        portfolio, project.id, [imported, ai_inferred]
    )
    task_item = next(item for item in result.items if item.entity_id == task.id)

    assert task_item.direct_estimate == ai_inferred
    assert task_item.direct_estimate.source is SourceKind.AI_INFERRED
    assert task_item.subtree.known_duration_seconds == 40


def test_estimates_outside_selected_project_wbs_are_excluded() -> None:
    portfolio, project, _, _, _, _, other_project = _portfolio_tree()
    foreign_project_estimate = _estimate(portfolio.id, other_project.id, 999)

    result = plan_work_breakdown_effort(
        portfolio, project.id, [foreign_project_estimate]
    )

    entity_ids = {item.entity_id for item in result.items}
    assert other_project.id not in entity_ids
    for item in result.items:
        assert item.subtree.known_duration_seconds == 0


def test_removed_entity_history_is_excluded_but_untouched() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    estimate = _estimate(portfolio.id, task.id, 55)
    before = estimate.model_dump()

    # The task is still in the portfolio but no longer WBS-contained:
    # it is simply unreachable through the selected project root.
    portfolio_without_task = Portfolio(
        id=portfolio.id,
        name=portfolio.name,
        entities=portfolio.entities,
        relations=[
            relation
            for relation in portfolio.relations
            if relation.source_id != task.id and relation.target_id != task.id
        ],
    )

    result = plan_work_breakdown_effort(
        portfolio_without_task, project.id, [estimate]
    )

    entity_ids = {item.entity_id for item in result.items}
    assert task.id not in entity_ids
    for item in result.items:
        assert item.subtree.known_duration_seconds == 0
    assert estimate.model_dump() == before


def test_wbs_errors_remain_authoritative() -> None:
    """V1.1 work-breakdown validation stays authoritative for V1.10 planning:
    multiple-parent/ambiguity, reachable containment cycles, and reachable
    invalid containment pairs all surface as ``WorkBreakdownError`` unchanged.
    The planning boundary never reimplements or re-wraps WBS validation.

    Construction follows the V1.1 WBS test patterns (``test_work_breakdown.py``)."""
    # Case 1: multiple WBS parents / ambiguity -- one work package claimed by
    # two distinct projects, so the included child has two distinct valid WBS
    # parents even though only one project is selected.
    project = _entity(EntityType.PROJECT, "Root")
    multi_parent = _entity(EntityType.WORK_PACKAGE, "Two parents")
    second_root = _entity(EntityType.PROJECT, "Second project")

    ambiguous_portfolio = Portfolio(
        id=uuid4(),
        name="Ambiguous",
        entities=[project, multi_parent, second_root],
        relations=[
            _belongs(multi_parent, project),
            _belongs(multi_parent, second_root),
        ],
    )

    with pytest.raises(WorkBreakdownError, match="multiple WBS parents"):
        plan_work_breakdown_effort(ambiguous_portfolio, project.id, [])

    # Case 2: the same ambiguity with both parents inside the selected
    # project: a task included under one package and the other package.
    shared_root = _entity(EntityType.PROJECT, "Root")
    first = _entity(EntityType.WORK_PACKAGE, "First")
    second = _entity(EntityType.WORK_PACKAGE, "Second")
    shared_task = _entity(EntityType.TASK, "Shared task")

    shared_portfolio = Portfolio(
        id=uuid4(),
        name="Shared task",
        entities=[shared_root, first, second, shared_task],
        relations=[
            _belongs(first, shared_root),
            _belongs(second, shared_root),
            _belongs(shared_task, first),
            _belongs(shared_task, second),
        ],
    )

    with pytest.raises(WorkBreakdownError, match="multiple WBS parents"):
        plan_work_breakdown_effort(shared_portfolio, shared_root.id, [])

    # Case 3: a containment cycle reachable from the selected root:
    # package <-> subpackage, both inside the selected project.
    cycle_project = _entity(EntityType.PROJECT, "Cycle root")
    package = _entity(EntityType.WORK_PACKAGE, "Package")
    subpackage = _entity(EntityType.WORK_PACKAGE, "Subpackage")

    cycle_portfolio = Portfolio(
        id=uuid4(),
        name="Cycle",
        entities=[cycle_project, package, subpackage],
        relations=[
            _belongs(package, cycle_project),
            _belongs(subpackage, package),
            # Completes the reachable cycle: package <-> subpackage.
            _belongs(package, subpackage),
        ],
    )

    with pytest.raises(WorkBreakdownError, match="cycle"):
        plan_work_breakdown_effort(cycle_portfolio, cycle_project.id, [])

    # Case 4: a reachable invalid containment pair: a WORK_PACKAGE may not
    # contain a DELIVERABLE.
    invalid_project = _entity(EntityType.PROJECT, "Platform")
    invalid_package = _entity(EntityType.WORK_PACKAGE, "Backend")
    invalid_deliverable = _entity(EntityType.DELIVERABLE, "Spec")

    invalid_portfolio = Portfolio(
        id=uuid4(),
        name="Invalid containment",
        entities=[invalid_project, invalid_package, invalid_deliverable],
        relations=[
            _belongs(invalid_package, invalid_project),
            # A WORK_PACKAGE may not contain a DELIVERABLE.
            _belongs(invalid_deliverable, invalid_package),
        ],
    )

    with pytest.raises(WorkBreakdownError, match="invalid WBS containment"):
        plan_work_breakdown_effort(invalid_portfolio, invalid_project.id, [])

    # Case 5: an unknown root is still a work-breakdown error.
    unknown_root = Portfolio(id=uuid4(), name="Orphan", entities=[project])
    with pytest.raises(WorkBreakdownError):
        plan_work_breakdown_effort(unknown_root, uuid4(), [])


def test_inputs_remain_unchanged() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    estimates = [
        _estimate(portfolio.id, project.id, 10, BASE),
        _estimate(portfolio.id, task.id, 25, BASE),
    ]
    portfolio_snapshot = portfolio.model_dump()
    estimate_snapshots = [estimate.model_dump() for estimate in estimates]

    result = plan_work_breakdown_effort(portfolio, project.id, estimates)

    assert portfolio.model_dump() == portfolio_snapshot
    assert [estimate.model_dump() for estimate in estimates] == estimate_snapshots
    assert result.model_dump() == plan_work_breakdown_effort(
        portfolio, project.id, estimates
    ).model_dump()


def test_repeated_equivalent_planning_yields_equivalent_immutable_output() -> None:
    portfolio, project, _, _, _, task, _ = _portfolio_tree()
    estimates = [_estimate(portfolio.id, project.id, 10), _estimate(
        portfolio.id, task.id, 25
    )]

    first = plan_work_breakdown_effort(portfolio, project.id, estimates)
    second = plan_work_breakdown_effort(portfolio, project.id, estimates)

    assert first == second
    assert first.model_config.get("frozen") is True
    assert second.items[0].model_config.get("frozen") is True

    with pytest.raises(ValidationError):
        first.items[0].depth = 99  # type: ignore[misc]


def test_invalid_portfolio_or_project_id_rejected() -> None:
    portfolio, project, _, _, _, _, _ = _portfolio_tree()

    with pytest.raises(ExecutionEffortPlanningError):
        plan_work_breakdown_effort(  # type: ignore[arg-type]
            {"id": portfolio.id}, project.id, []
        )

    with pytest.raises(ExecutionEffortPlanningError):
        plan_work_breakdown_effort(
            portfolio, cast(UUID, "not-a-uuid"), []
        )


def test_planned_effort_summary_rejects_fabricated_totals() -> None:
    with pytest.raises(ValidationError, match="must not expose"):
        PlannedEffortSummary(
            known_duration_seconds=10,
            estimated_entity_count=1,
            unestimated_entity_count=1,
            total_duration_seconds=10,
        )

    with pytest.raises(ValidationError, match="fully estimated"):
        PlannedEffortSummary(
            known_duration_seconds=10,
            estimated_entity_count=1,
            unestimated_entity_count=0,
            total_duration_seconds=20,
        )

    complete = PlannedEffortSummary(
        known_duration_seconds=10,
        estimated_entity_count=2,
        unestimated_entity_count=0,
        total_duration_seconds=10,
    )
    assert complete.total_duration_seconds == 10

    partial = PlannedEffortSummary(
        known_duration_seconds=10,
        estimated_entity_count=1,
        unestimated_entity_count=1,
    )
    assert partial.total_duration_seconds is None


def test_plan_item_rejects_direct_estimate_targeting_another_entity() -> None:
    from trajectory_os.domain.execution_effort_planning import (
        WorkBreakdownEffortPlanItem,
    )

    summary = PlannedEffortSummary(
        known_duration_seconds=5,
        estimated_entity_count=1,
        unestimated_entity_count=0,
        total_duration_seconds=5,
    )
    with pytest.raises(ValidationError, match="target the item's own entity"):
        WorkBreakdownEffortPlanItem(
            entity_id=uuid4(),
            parent_id=None,
            depth=0,
            direct_estimate=_estimate(uuid4(), uuid4(), 5),
            subtree=summary,
        )
