"""Unit tests for the work-breakdown projection and its V1.1-B invariants."""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import (
    WorkBreakdownError,
    WorkBreakdownNode,
    WorkBreakdownStructure,
    build_work_breakdown,
)


def _entity(entity_type: EntityType, title: str) -> TrajectoryEntity:
    return TrajectoryEntity(entity_type=entity_type, title=title)


def _belongs_to(
    child: TrajectoryEntity, parent: TrajectoryEntity
) -> TrajectoryRelation:
    return TrajectoryRelation(
        source_id=child.id,
        target_id=parent.id,
        relation_type=RelationType.BELONGS_TO,
    )


def _collect_ids(node: WorkBreakdownNode) -> set[UUID]:
    ids = {node.entity_id}
    for child in node.children:
        ids |= _collect_ids(child)
    return ids


def _three_level_portfolio() -> tuple[
    Portfolio, TrajectoryEntity, TrajectoryEntity, TrajectoryEntity
]:
    project = _entity(EntityType.PROJECT, "Platform")
    work_package = _entity(EntityType.WORK_PACKAGE, "Backend")
    task = _entity(EntityType.TASK, "Implement API")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, work_package, task],
        relations=[
            _belongs_to(work_package, project),
            _belongs_to(task, work_package),
        ],
    )

    return portfolio, project, work_package, task


def test_project__work_package__task() -> None:
    portfolio, project, work_package, task = _three_level_portfolio()

    result = build_work_breakdown(portfolio, project.id)

    assert isinstance(result, WorkBreakdownStructure)
    assert result.root.entity_id == project.id
    assert [child.entity_id for child in result.root.children] == [
        work_package.id,
    ]
    assert [
        child.entity_id for child in result.root.children[0].children
    ] == [task.id]
    assert result.root.children[0].children[0].children == ()


def test_project__deliverable__work_package__task() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    deliverable = _entity(EntityType.DELIVERABLE, "API spec")
    work_package = _entity(EntityType.WORK_PACKAGE, "Backend")
    task = _entity(EntityType.TASK, "Implement API")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, deliverable, work_package, task],
        relations=[
            _belongs_to(deliverable, project),
            _belongs_to(work_package, deliverable),
            _belongs_to(task, work_package),
        ],
    )

    result = build_work_breakdown(portfolio, project.id)

    assert result.root.entity_id == project.id
    assert [child.entity_id for child in result.root.children] == [
        deliverable.id,
    ]
    assert [
        child.entity_id
        for child in result.root.children[0].children
    ] == [work_package.id]
    assert [
        child.entity_id
        for child in result.root.children[0].children[0].children
    ] == [task.id]
    assert (
        result.root.children[0].children[0].children[0].children == ()
    )


def test_project__task_skipped_levels() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    deliverable = _entity(EntityType.DELIVERABLE, "API spec")
    task = _entity(EntityType.TASK, "Implement API")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, deliverable, task],
        relations=[
            _belongs_to(deliverable, project),
            _belongs_to(task, project),
        ],
    )

    result = build_work_breakdown(portfolio, project.id)

    assert [child.entity_id for child in result.root.children] == [
        deliverable.id,
        task.id,
    ]
    # TASK is always a leaf.
    assert result.root.children[1].children == ()


def test_nested_work_package__work_package() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    parent_package = _entity(EntityType.WORK_PACKAGE, "Platform")
    child_package = _entity(EntityType.WORK_PACKAGE, "API")
    task = _entity(EntityType.TASK, "Implement endpoint")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, parent_package, child_package, task],
        relations=[
            _belongs_to(parent_package, project),
            _belongs_to(child_package, parent_package),
            _belongs_to(task, child_package),
        ],
    )

    result = build_work_breakdown(portfolio, project.id)

    assert [
        child.entity_id for child in result.root.children
    ] == [parent_package.id]
    assert [
        child.entity_id for child in result.root.children[0].children
    ] == [child_package.id]
    assert [
        child.entity_id
        for child in result.root.children[0].children[0].children
    ] == [task.id]


def test_sibling_order_follows_portfolio_entities_order() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    second = _entity(EntityType.WORK_PACKAGE, "Second")
    first = _entity(EntityType.WORK_PACKAGE, "First")

    # Relation order lists first before second; entities order does not.
    portfolio = Portfolio(
        name="WBS",
        entities=[project, second, first],
        relations=[
            _belongs_to(first, project),
            _belongs_to(second, project),
        ],
    )

    result = build_work_breakdown(portfolio, project.id)

    assert [child.entity_id for child in result.root.children] == [
        second.id,
        first.id,
    ]


def test_unrelated_entities_are_excluded() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    work_package = _entity(EntityType.WORK_PACKAGE, "Backend")
    task = _entity(EntityType.TASK, "Implement API")
    idea = _entity(EntityType.IDEA, "Unrelated idea")
    orphan_package = _entity(EntityType.WORK_PACKAGE, "Orphan package")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, work_package, task, idea, orphan_package],
        relations=[
            _belongs_to(work_package, project),
            _belongs_to(task, work_package),
        ],
    )

    result = build_work_breakdown(portfolio, project.id)

    ids = _collect_ids(result.root)

    assert idea.id not in ids
    assert orphan_package.id not in ids
    assert ids == {project.id, work_package.id, task.id}


def test_non_belongsto_relations_are_ignored() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    deliverable = _entity(EntityType.DELIVERABLE, "API spec")
    work_package = _entity(EntityType.WORK_PACKAGE, "Backend")
    task = _entity(EntityType.TASK, "Implement API")

    # Every edge below mirrors an allowed containment pair, so treating any
    # of them as containment would incorrectly attach the child.
    portfolio = Portfolio(
        name="WBS",
        entities=[project, deliverable, work_package, task],
        relations=[
            TrajectoryRelation(
                source_id=deliverable.id,
                target_id=project.id,
                relation_type=RelationType.CONTRIBUTES_TO,
            ),
            TrajectoryRelation(
                source_id=work_package.id,
                target_id=deliverable.id,
                relation_type=RelationType.DEPENDS_ON,
            ),
            TrajectoryRelation(
                source_id=task.id,
                target_id=work_package.id,
                relation_type=RelationType.REQUIRES,
            ),
            TrajectoryRelation(
                source_id=work_package.id,
                target_id=project.id,
                relation_type=RelationType.PRODUCES,
            ),
        ],
    )

    result = build_work_breakdown(portfolio, project.id)

    assert result.root.children == ()
    assert _collect_ids(result.root) == {project.id}


def test_unknown_root_rejected() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    portfolio = Portfolio(name="WBS", entities=[project])

    with pytest.raises(ValueError, match="unknown entity"):
        build_work_breakdown(portfolio, uuid4())


@pytest.mark.parametrize(
    "root_type",
    [
        EntityType.DELIVERABLE,
        EntityType.WORK_PACKAGE,
        EntityType.TASK,
        EntityType.IDEA,
    ],
)
def test_non_project_root_rejected(root_type: EntityType) -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    candidate = _entity(root_type, "Candidate")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, candidate],
        relations=[_belongs_to(candidate, project)],
    )

    with pytest.raises(ValueError, match="PROJECT"):
        build_work_breakdown(portfolio, candidate.id)


def test_input_portfolio_unchanged() -> None:
    portfolio, project, work_package, task = _three_level_portfolio()
    before = portfolio.model_dump()

    build_work_breakdown(portfolio, project.id)

    assert portfolio.model_dump() == before
    assert [entity.id for entity in portfolio.entities] == [
        project.id,
        work_package.id,
        task.id,
    ]
    assert len(portfolio.relations) == 2
    assert portfolio.get_entity(project.id) is not None


def test_repeated_projection_is_equivalent_but_fresh() -> None:
    portfolio, project, work_package, task = _three_level_portfolio()

    first = build_work_breakdown(portfolio, project.id)
    second = build_work_breakdown(portfolio, project.id)

    assert first == second
    assert first is not second
    assert first.root is not second.root
    assert first.root.children[0] is not second.root.children[0]
    assert (
        first.root.children[0].children[0]
        is not second.root.children[0].children[0]
    )
    assert first.root.entity_id == project.id
    assert [child.entity_id for child in first.root.children] == [
        work_package.id,
    ]
    assert [
        child.entity_id for child in first.root.children[0].children
    ] == [task.id]


def test_project_and_node_models_are_frozen() -> None:
    portfolio, project, _, _ = _three_level_portfolio()

    structure = build_work_breakdown(portfolio, project.id)

    with pytest.raises(ValidationError):
        structure.root.entity_id = uuid4()

    with pytest.raises(ValidationError):
        structure.root.children = ()

    with pytest.raises(ValidationError):
        structure.root = WorkBreakdownNode(entity_id=uuid4())


# --- V1.1-B invariant tests -------------------------------------------------


def test_root_violations_raise_work_breakdown_error() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    candidate = _entity(EntityType.WORK_PACKAGE, "Candidate")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, candidate],
        relations=[_belongs_to(candidate, project)],
    )

    with pytest.raises(WorkBreakdownError, match="unknown entity"):
        build_work_breakdown(portfolio, uuid4())

    with pytest.raises(WorkBreakdownError, match="PROJECT"):
        build_work_breakdown(portfolio, candidate.id)


def test_is_a_value_error() -> None:
    assert issubclass(WorkBreakdownError, ValueError)


def test_duplicate_belongs_to_edges_collapse_to_single_node() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    work_package = _entity(EntityType.WORK_PACKAGE, "Backend")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, work_package],
        relations=[
            _belongs_to(work_package, project),
            _belongs_to(work_package, project),
        ],
    )

    result = build_work_breakdown(portfolio, project.id)

    assert [child.entity_id for child in result.root.children] == [
        work_package.id,
    ]
    assert result.root.children[0].children == ()
    assert _collect_ids(result.root) == {project.id, work_package.id}


def test_reachable_invalid_wbs_containment_rejected() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    work_package = _entity(EntityType.WORK_PACKAGE, "Backend")
    deliverable = _entity(EntityType.DELIVERABLE, "Spec")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, work_package, deliverable],
        relations=[
            _belongs_to(work_package, project),
            # A WORK_PACKAGE may not contain a DELIVERABLE.
            _belongs_to(deliverable, work_package),
        ],
    )

    with pytest.raises(WorkBreakdownError, match="invalid WBS containment"):
        build_work_breakdown(portfolio, project.id)


def test_task_is_wbs_leaf_rejects_task_child() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    parent_task = _entity(EntityType.TASK, "Parent task")
    child_task = _entity(EntityType.TASK, "Child task")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, parent_task, child_task],
        relations=[
            _belongs_to(parent_task, project),
            # A TASK may not contain another TASK (TASK is a WBS leaf).
            _belongs_to(child_task, parent_task),
        ],
    )

    with pytest.raises(WorkBreakdownError, match="invalid WBS containment"):
        build_work_breakdown(portfolio, project.id)


def test_non_wbs_belongs_to_children_ignored_not_rejected() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    idea = _entity(EntityType.IDEA, "Idea")
    resource = _entity(EntityType.RESOURCE, "Resource")
    package_of_idea = _entity(EntityType.WORK_PACKAGE, "Nested package")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, idea, resource, package_of_idea],
        relations=[
            _belongs_to(idea, project),
            _belongs_to(resource, project),
            # Unreachable invalid pair (IDEA, WORK_PACKAGE); must not error
            # because the WBS child is never reachable from the root.
            _belongs_to(package_of_idea, idea),
        ],
    )

    result = build_work_breakdown(portfolio, project.id)

    assert result.root.children == ()
    assert _collect_ids(result.root) == {project.id}


def test_included_child_with_multiple_wbs_parents_rejected() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    first = _entity(EntityType.WORK_PACKAGE, "First")
    second = _entity(EntityType.WORK_PACKAGE, "Second")
    task = _entity(EntityType.TASK, "Shared task")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, first, second, task],
        relations=[
            _belongs_to(first, project),
            _belongs_to(second, project),
            _belongs_to(task, first),
            _belongs_to(task, second),
        ],
    )

    with pytest.raises(WorkBreakdownError, match="multiple WBS parents"):
        build_work_breakdown(portfolio, project.id)


def test_included_child_ambiguous_with_outside_root_parent_rejected() -> None:
    root_project = _entity(EntityType.PROJECT, "Selected")
    other_project = _entity(EntityType.PROJECT, "Other")
    chosen_package = _entity(EntityType.WORK_PACKAGE, "Chosen")
    outside_package = _entity(EntityType.WORK_PACKAGE, "Outside")
    task = _entity(EntityType.TASK, "Ambiguous task")

    portfolio = Portfolio(
        name="WBS",
        entities=[
            root_project,
            other_project,
            chosen_package,
            outside_package,
            task,
        ],
        relations=[
            _belongs_to(chosen_package, root_project),
            _belongs_to(outside_package, other_project),
            _belongs_to(task, chosen_package),
            # Valid WBS containment pair outside the selected root still
            # makes the included task ambiguous.
            _belongs_to(task, outside_package),
        ],
    )

    with pytest.raises(WorkBreakdownError, match="multiple WBS parents"):
        build_work_breakdown(portfolio, root_project.id)


def test_reachable_cycle_rejected_with_cycle_message() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    package = _entity(EntityType.WORK_PACKAGE, "Package")
    subpackage = _entity(EntityType.WORK_PACKAGE, "Subpackage")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, package, subpackage],
        relations=[
            _belongs_to(package, project),
            _belongs_to(subpackage, package),
            # Completes the cycle: package <-> subpackage.
            _belongs_to(package, subpackage),
        ],
    )

    with pytest.raises(WorkBreakdownError, match="cycle"):
        build_work_breakdown(portfolio, project.id)


def test_cycle_returning_to_selected_project_root_rejected() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    package = _entity(EntityType.WORK_PACKAGE, "Package")

    portfolio = Portfolio(
        name="WBS",
        entities=[project, package],
        relations=[
            _belongs_to(package, project),
            # Completes the cycle by returning to the PROJECT root.
            _belongs_to(project, package),
        ],
    )

    with pytest.raises(WorkBreakdownError, match="cycle"):
        build_work_breakdown(portfolio, project.id)


def test_unreachable_cycle_does_not_affect_selected_root() -> None:
    root_project = _entity(EntityType.PROJECT, "Selected")
    other_project = _entity(EntityType.PROJECT, "Other")
    package = _entity(EntityType.WORK_PACKAGE, "Fine package")
    a = _entity(EntityType.WORK_PACKAGE, "A")
    b = _entity(EntityType.WORK_PACKAGE, "B")

    portfolio = Portfolio(
        name="WBS",
        entities=[root_project, other_project, package, a, b],
        relations=[
            _belongs_to(package, root_project),
            _belongs_to(a, other_project),
            _belongs_to(b, other_project),
            _belongs_to(b, a),
            _belongs_to(a, b),
        ],
    )

    result = build_work_breakdown(portfolio, root_project.id)

    assert result.root.entity_id == root_project.id
    assert [child.entity_id for child in result.root.children] == [
        package.id,
    ]


def test_unreachable_invalid_containment_does_not_affect_selected_root() -> None:
    root_project = _entity(EntityType.PROJECT, "Selected")
    other_project = _entity(EntityType.PROJECT, "Other")
    package = _entity(EntityType.WORK_PACKAGE, "Fine package")
    bad_package = _entity(EntityType.WORK_PACKAGE, "Bad parent")
    deliverable = _entity(EntityType.DELIVERABLE, "Deliverable")

    portfolio = Portfolio(
        name="WBS",
        entities=[
            root_project,
            other_project,
            package,
            bad_package,
            deliverable,
        ],
        relations=[
            _belongs_to(package, root_project),
            _belongs_to(bad_package, other_project),
            # Invalid pair (WORK_PACKAGE, DELIVERABLE), but unreachable from
            # the selected root, so it must not raise.
            _belongs_to(deliverable, bad_package),
        ],
    )

    result = build_work_breakdown(portfolio, root_project.id)

    assert [child.entity_id for child in result.root.children] == [
        package.id,
    ]
