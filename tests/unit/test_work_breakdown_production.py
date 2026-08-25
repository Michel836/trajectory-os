"""Tests for V1.4-A work-breakdown proposal production."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

import trajectory_os.domain as domain
from trajectory_os.domain import (
    Portfolio,
    ProposedWorkNode,
    RelationType,
    TrajectoryEntity,
    TrajectoryRelation,
    WorkBreakdownProposal,
    accept_work_breakdown_proposal,
    build_work_breakdown,
)
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.work_breakdown_production import (
    WorkBreakdownProposalContextItem,
    WorkBreakdownProposalProductionError,
    WorkBreakdownProposalRequest,
    propose_work_breakdown,
)
from trajectory_os.domain.work_breakdown_proposals import (
    WorkBreakdownProposalError,
    validate_work_breakdown_proposal,
)


def make_entity(entity_type: EntityType, title: str, description: str | None = None):
    return TrajectoryEntity(
        entity_type=entity_type, title=title, description=description
    )


def relation(source: TrajectoryEntity, target: TrajectoryEntity, rtype: RelationType):
    return TrajectoryRelation(
        source_id=source.id, target_id=target.id, relation_type=rtype
    )


@pytest.fixture
def fixture():
    project = make_entity(EntityType.PROJECT, "Project")
    other_project = make_entity(EntityType.PROJECT, "Other Project")
    deliverable = make_entity(EntityType.DELIVERABLE, "Deliverable")
    work_package_a = make_entity(EntityType.WORK_PACKAGE, "WP A")
    work_package_b = make_entity(EntityType.WORK_PACKAGE, "WP B")
    task = make_entity(EntityType.TASK, "Task", description="do the thing")
    idea = make_entity(EntityType.IDEA, "Idea")
    other_task = make_entity(EntityType.TASK, "Other Task")

    portfolio = Portfolio(
        name="wbs",
        entities=[
            project,
            other_project,
            deliverable,
            work_package_a,
            work_package_b,
            task,
            idea,
            other_task,
        ],
        relations=[
            relation(deliverable, project, RelationType.BELONGS_TO),
            relation(work_package_a, deliverable, RelationType.BELONGS_TO),
            relation(work_package_b, deliverable, RelationType.BELONGS_TO),
            relation(task, work_package_a, RelationType.BELONGS_TO),
            relation(idea, project, RelationType.BELONGS_TO),
            relation(other_task, other_project, RelationType.BELONGS_TO),
        ],
    )
    return (
        portfolio,
        project,
        deliverable,
        work_package_a,
        work_package_b,
        task,
        idea,
        other_project,
        other_task,
    )


class CaptureProducer:
    def __init__(self, response: object):
        self.response = response
        self.calls: list[WorkBreakdownProposalRequest] = []

    def propose(self, request: WorkBreakdownProposalRequest) -> object:
        self.calls.append(request)
        return self.response


def make_proposal(
    project: TrajectoryEntity,
    anchor: TrajectoryEntity,
    children: tuple[ProposedWorkNode, ...] = (),
) -> WorkBreakdownProposal:
    return WorkBreakdownProposal(
        project_id=project.id, anchor_id=anchor.id, children=children
    )


def test_a2_project_root_context(fixture):
    # A1 producer fake exercised here; A2 project root first with None parent.
    portfolio, project, deliverable, *_ = fixture
    producer = CaptureProducer(make_proposal(project, deliverable))

    result = propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    assert len(producer.calls) == 1
    request = producer.calls[0]
    root_item = request.existing_work[0]
    assert root_item.entity_id == project.id
    assert root_item.parent_id is None
    assert root_item.entity_type is EntityType.PROJECT
    assert root_item.title == "Project"
    assert root_item.description is None
    assert result is not None


def test_a3_pre_order_project_deliverable_wp_task(fixture):
    portfolio, project, deliverable, wp_a, wp_b, task, *_ = fixture
    producer = CaptureProducer(make_proposal(project, deliverable))

    propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    ids = [item.entity_id for item in producer.calls[0].existing_work]
    assert ids == [project.id, deliverable.id, wp_a.id, task.id, wp_b.id]


def test_a4_sibling_order_preserved(fixture):
    portfolio, project, deliverable, wp_a, wp_b, task, *_ = fixture
    producer = CaptureProducer(make_proposal(project, deliverable))

    propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    ids = [item.entity_id for item in producer.calls[0].existing_work]
    assert ids.index(wp_a.id) < ids.index(wp_b.id)
    assert [ids.index(wp_a.id), ids.index(task.id), ids.index(wp_b.id)] == [2, 3, 4]


def test_a5_exact_parent_ids_and_description(fixture):
    portfolio, project, deliverable, wp_a, wp_b, task, *_ = fixture
    producer = CaptureProducer(make_proposal(project, deliverable))

    propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    by_id = {item.entity_id: item for item in producer.calls[0].existing_work}
    assert by_id[project.id].parent_id is None
    assert by_id[deliverable.id].parent_id == project.id
    assert by_id[wp_a.id].parent_id == deliverable.id
    assert by_id[wp_b.id].parent_id == deliverable.id
    assert by_id[task.id].parent_id == wp_a.id
    assert by_id[task.id].description == "do the thing"


def test_a6_unrelated_project_excluded(fixture):
    portfolio, project, deliverable, *_ = fixture
    portfolio_entities = list(portfolio.entities)
    other_project = next(e for e in portfolio_entities if e.title == "Other Project")
    other_task = next(
        e
        for e in portfolio_entities
        if e.entity_type is EntityType.TASK and e.title == "Other Task"
    )
    producer = CaptureProducer(make_proposal(project, deliverable))

    propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    ids = {item.entity_id for item in producer.calls[0].existing_work}
    assert other_project.id not in ids
    assert other_task.id not in ids


def test_a7_non_wbs_excluded(fixture):
    portfolio, project, deliverable, *_ = fixture
    idea = next(e for e in portfolio.entities if e.title == "Idea")
    producer = CaptureProducer(make_proposal(project, deliverable))

    propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    ids = {item.entity_id for item in producer.calls[0].existing_work}
    assert idea.id not in ids


def test_a8_no_canonical_objects_in_request(fixture):
    portfolio, project, deliverable, *_ = fixture
    producer = CaptureProducer(make_proposal(project, deliverable))

    propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    request = producer.calls[0]
    assert isinstance(request, WorkBreakdownProposalRequest)
    assert type(request.project_id) is UUID
    assert type(request.anchor_id) is UUID
    for item in request.existing_work:
        assert isinstance(item, WorkBreakdownProposalContextItem)
        assert type(item.entity_id) is UUID
        assert item.parent_id is None or type(item.parent_id) is UUID
        assert item.title
        assert item.description is None or isinstance(item.description, str)

    dumped = request.model_dump()
    allowed = (str, int, float, bool, list, dict, UUID)

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for inner in value.values():
                walk(inner)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                walk(inner)
        else:
            assert value is None or isinstance(value, allowed), f"unexpected {value!r}"

    walk(dumped)
    assert dumped["project_id"] == project.id
    assert dumped["anchor_id"] == deliverable.id


def test_a9_request_and_items_are_frozen(fixture):
    portfolio, project, deliverable, *_ = fixture
    producer = CaptureProducer(make_proposal(project, deliverable))

    propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    request = producer.calls[0]
    item = request.existing_work[0]
    with pytest.raises(ValueError):
        request.project_id = uuid4()  # type: ignore[misc]
    with pytest.raises(ValueError):
        item.title = "mutated"  # type: ignore[misc]
    with pytest.raises(ValueError):
        request.existing_work = ()  # type: ignore[misc]


def test_a10_invalid_or_missing_project_calls_no_producer(fixture):
    portfolio, project, deliverable, *_ = fixture
    producer = CaptureProducer(make_proposal(project, deliverable))

    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(portfolio, uuid4(), deliverable.id, producer)

    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(portfolio, "not a uuid", deliverable.id, producer)

    assert producer.calls == []


def test_a11_non_project_anchor_target_calls_no_producer(fixture):
    portfolio, project, deliverable, *_ = fixture
    producer = CaptureProducer(make_proposal(project, deliverable))

    with pytest.raises(WorkBreakdownProposalProductionError):
        # Anchor is fine, but "project" is a DELIVERABLE, so the WBS cannot be built.
        propose_work_breakdown(portfolio, deliverable.id, deliverable.id, producer)

    assert producer.calls == []


def test_a12_invalid_anchor_calls_no_producer(fixture):
    portfolio, project, deliverable, *_ = fixture
    idea = next(e for e in portfolio.entities if e.title == "Idea")
    producer = CaptureProducer(make_proposal(project, deliverable))

    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(portfolio, project.id, idea.id, producer)

    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(portfolio, project.id, uuid4(), producer)

    assert producer.calls == []


def test_a13_non_belongs_to_relation_does_not_validate_anchor():
    project = make_entity(EntityType.PROJECT, "P")
    task = make_entity(EntityType.TASK, "T")
    portfolio = Portfolio(
        name="related-only",
        entities=[project, task],
        relations=[relation(task, project, RelationType.RELATED_TO)],
    )
    producer = CaptureProducer(make_proposal(project, task))

    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(portfolio, project.id, task.id, producer)

    assert producer.calls == []


def test_a14_wrong_producer_return_type(fixture):
    portfolio, project, deliverable, *_ = fixture
    producer = CaptureProducer("definitely not a proposal")

    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(portfolio, project.id, deliverable.id, producer)


def test_a15_valid_proposal_passes_v12(fixture):
    portfolio, project, deliverable, *_ = fixture
    child = ProposedWorkNode(
        entity_type=EntityType.TASK, title="Proposed task", confidence=0.8
    )
    expected = make_proposal(project, deliverable, (child,))
    producer = CaptureProducer(expected)

    result = propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    assert isinstance(result, WorkBreakdownProposal)
    assert result.project_id == project.id
    assert result.anchor_id == deliverable.id
    assert result.children == (child,)


def test_a16_returned_proposal_is_fresh(fixture):
    portfolio, project, deliverable, *_ = fixture
    produced = make_proposal(project, deliverable)
    producer = CaptureProducer(produced)

    result = propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    assert result is not produced


def test_a17_project_anchor_children_order_preserved(fixture):
    portfolio, project, *_ = fixture
    first = ProposedWorkNode(
        entity_type=EntityType.DELIVERABLE, title="first", confidence=0.5
    )
    second = ProposedWorkNode(
        entity_type=EntityType.TASK, title="second", confidence=0.7
    )
    expected = make_proposal(project, project, (first, second))
    producer = CaptureProducer(expected)

    result = propose_work_breakdown(portfolio, project.id, project.id, producer)

    assert result.project_id == project.id
    assert result.anchor_id == project.id
    assert [child.title for child in result.children] == ["first", "second"]


def test_a18_empty_proposal_valid(fixture):
    portfolio, project, deliverable, *_ = fixture
    producer = CaptureProducer(make_proposal(project, deliverable))

    result = propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    assert result.children == ()


def test_a19_source_portfolio_unchanged(fixture):
    portfolio, project, deliverable, *_ = fixture
    before = portfolio.model_dump()
    producer = CaptureProducer(make_proposal(project, deliverable))

    propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    assert portfolio.model_dump() == before


def test_a20_producer_exception_propagates_unchanged(fixture):
    portfolio, project, deliverable, *_ = fixture
    boom = RuntimeError("provider exploded")

    class RaisingProducer:
        def __init__(self):
            self.calls = 0

        def propose(self, request):
            self.calls += 1
            raise boom

    producer = RaisingProducer()
    with pytest.raises(RuntimeError) as exc_info:
        propose_work_breakdown(portfolio, project.id, deliverable.id, producer)
    assert exc_info.value is boom
    assert producer.calls == 1


def test_redirect_to_other_project_or_anchor_is_rejected(fixture):
    portfolio, project, deliverable, *_ = fixture
    other_project = next(e for e in portfolio.entities if e.title == "Other Project")
    other_task = next(
        e for e in portfolio.entities if e.title == "Other Task"
    )

    redirect_project = CaptureProducer(
        make_proposal(other_project, other_task)
    )
    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(portfolio, project.id, deliverable.id, redirect_project)

    redirect_anchor = CaptureProducer(make_proposal(project, project))
    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(portfolio, project.id, deliverable.id, redirect_anchor)


class HostileRequestMutationProducer:
    """B1.1: mutates the frozen request via object.__setattr__."""

    def __init__(self, proposal: WorkBreakdownProposal):
        self.proposal = proposal
        self.calls = 0

    def propose(self, request: WorkBreakdownProposalRequest) -> WorkBreakdownProposal:
        self.calls += 1
        object.__setattr__(request, "project_id", uuid4())
        object.__setattr__(request, "anchor_id", uuid4())
        return self.proposal


def test_b1_1_hostile_request_mutation_does_not_block_production(fixture):
    portfolio, project, deliverable, *_ = fixture
    producer = HostileRequestMutationProducer(make_proposal(project, deliverable))

    result = propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    assert producer.calls == 1
    assert result.project_id == project.id
    assert result.anchor_id == deliverable.id


def test_b1_2_tampered_proposal_is_rejected_with_v12_cause(fixture):
    portfolio, project, deliverable, *_ = fixture
    proposal = make_proposal(project, deliverable)
    # Corrupt an otherwise valid proposal after construction: strict V1.2
    # revalidation must reject the current state.
    object.__setattr__(proposal, "anchor_id", "not-a-uuid")

    with (
        pytest.warns(UserWarning, match="Pydantic serializer warnings"),
        pytest.raises(WorkBreakdownProposalProductionError) as exc_info,
    ):
        propose_work_breakdown(
            portfolio,
            project.id,
            deliverable.id,
            CaptureProducer(proposal),
        )

    assert isinstance(exc_info.value.__cause__, WorkBreakdownProposalError)


def test_b1_3_valid_other_project_redirection_rejected_after_v12(fixture):
    portfolio, project, deliverable, *_ = fixture
    other_project = next(e for e in portfolio.entities if e.title == "Other Project")
    other_task = next(
        e for e in portfolio.entities if e.title == "Other Task"
    )
    redirected = make_proposal(other_project, other_task)
    # The proposal is valid under V1.2 by itself: the rejection must come
    # from the post-validation redirection check, not from V1.2.
    validated = validate_work_breakdown_proposal(portfolio, redirected)
    assert validated.project_id == other_project.id

    producer = CaptureProducer(redirected)
    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(portfolio, project.id, deliverable.id, producer)


def test_b1_4_valid_other_anchor_redirection_rejected_after_v12(fixture):
    portfolio, project, deliverable, *_ = fixture
    redirected = make_proposal(project, project)  # anchor B: project root
    # Valid under V1.2 by itself (same project, different valid anchor).
    validated = validate_work_breakdown_proposal(portfolio, redirected)
    assert validated.anchor_id == project.id
    assert validated.project_id == project.id

    producer = CaptureProducer(redirected)
    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(portfolio, project.id, deliverable.id, producer)


def test_non_portfolio_rejected(fixture):
    portfolio, project, deliverable, *_ = fixture
    producer = CaptureProducer(make_proposal(project, deliverable))

    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown("nope", project.id, deliverable.id, producer)

    assert producer.calls == []


def _find_wbs_node(node: object, entity_id: object) -> object:
    """Iteratively find a WBS node by entity id (test helper)."""
    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if current.entity_id == entity_id:  # type: ignore[union-attr]
            return current
        stack.extend(current.children)  # type: ignore[union-attr]
    raise AssertionError(f"entity {entity_id} not found in work breakdown")


def _build_deep_wbs_portfolio(depth: int):
    """Build PROJECT -> WORK_PACKAGE x (depth-2) -> TASK, exactly ``depth`` nodes."""
    project = make_entity(EntityType.PROJECT, "Deep Project")
    intermediates = [
        make_entity(EntityType.WORK_PACKAGE, f"Deep WP {index}")
        for index in range(depth - 2)
    ]
    final_task = make_entity(EntityType.TASK, "Deep Final Task")

    relations: list[TrajectoryRelation] = []
    previous = project
    for entity in [*intermediates, final_task]:
        relations.append(relation(entity, previous, RelationType.BELONGS_TO))
        previous = entity

    portfolio = Portfolio(
        name="deep",
        entities=[project, *intermediates, final_task],
        relations=relations,
    )
    return portfolio, project, intermediates, final_task


def test_b2_1_deep_canonical_wbs_context_is_iterative():
    depth = 5000
    portfolio, project, intermediates, final_task = _build_deep_wbs_portfolio(depth)
    empty_proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=final_task.id,
        children=(),
    )
    producer = CaptureProducer(empty_proposal)

    # A 5000-deep valid canonical WBS must not raise RecursionError:
    # V1.4 context flattening is iterative.
    result = propose_work_breakdown(portfolio, project.id, final_task.id, producer)

    assert len(producer.calls) == 1
    request = producer.calls[0]
    assert len(request.existing_work) == depth
    assert request.existing_work[0].entity_id == project.id
    assert request.existing_work[0].parent_id is None

    last = request.existing_work[-1]
    assert last.entity_id == final_task.id
    assert last.parent_id == intermediates[-1].id

    expected_ids = [project.id, *(wp.id for wp in intermediates), final_task.id]
    assert [item.entity_id for item in request.existing_work] == expected_ids

    assert isinstance(result, WorkBreakdownProposal)
    assert result.project_id == project.id
    assert result.anchor_id == final_task.id
    assert result.children == ()


def test_b2_2_production_acceptance_reconstruction_composition(fixture):
    portfolio, project, deliverable, *_ = fixture
    implementation = ProposedWorkNode(
        entity_type=EntityType.WORK_PACKAGE,
        title="Implementation",
        confidence=0.9,
        children=(
            ProposedWorkNode(
                entity_type=EntityType.TASK, title="Implement", confidence=0.8
            ),
            ProposedWorkNode(
                entity_type=EntityType.TASK, title="Verify", confidence=0.7
            ),
        ),
    )
    producer = CaptureProducer(make_proposal(project, deliverable, (implementation,)))
    before = portfolio.model_dump()

    proposed = propose_work_breakdown(portfolio, project.id, deliverable.id, producer)

    # V1.4 production does not mutate the source portfolio.
    assert portfolio.model_dump() == before

    # Proposal ordering is preserved through the V1.4 boundary.
    assert [child.title for child in proposed.children] == ["Implementation"]
    top = proposed.children[0]
    assert top.entity_type is EntityType.WORK_PACKAGE
    assert [child.title for child in top.children] == ["Implement", "Verify"]

    # Explicit V1.3 acceptance materializes the work in a fresh portfolio.
    accepted = accept_work_breakdown_proposal(portfolio, proposed)

    assert accepted.portfolio is not portfolio
    assert len(accepted.created_entity_ids) == 3
    assert len(accepted.created_relation_ids) == 3

    # The source portfolio remains unchanged after acceptance.
    assert portfolio.model_dump() == before

    # V1.1 reconstruction over the accepted portfolio contains the hierarchy.
    structure = build_work_breakdown(accepted.portfolio, project.id)
    implementation_entity = next(
        entity
        for entity in accepted.portfolio.entities
        if entity.title == "Implementation"
    )
    deliverable_node = _find_wbs_node(structure.root, deliverable.id)
    assert implementation_entity.id in {
        child.entity_id for child in deliverable_node.children  # type: ignore[union-attr]
    }
    implementation_node = _find_wbs_node(structure.root, implementation_entity.id)
    child_titles = [  # type: ignore[union-attr]
        accepted.portfolio.get_entity(child.entity_id).title  # type: ignore[union-attr]
        for child in implementation_node.children
    ]
    assert child_titles == ["Implement", "Verify"]


def test_all_public_v14_symbols_exported():
    expected = {
        "WorkBreakdownProposalContextItem",
        "WorkBreakdownProposalProducer",
        "WorkBreakdownProposalProductionError",
        "WorkBreakdownProposalRequest",
        "propose_work_breakdown",
    }
    assert expected <= set(domain.__all__)
    for name in expected:
        assert getattr(domain, name) is not None
