"""Unit tests for validated work-breakdown proposals (V1.2-A)."""

from contextlib import contextmanager
from uuid import UUID, uuid4
from warnings import catch_warnings, filterwarnings

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import is_work_breakdown_containment_allowed
from trajectory_os.domain.work_breakdown_proposals import (
    ProposedWorkNode,
    ValidatedWorkBreakdownProposal,
    WorkBreakdownProposal,
    WorkBreakdownProposalError,
    validate_work_breakdown_proposal,
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


def _node(
    entity_type: EntityType,
    title: str,
    children: tuple[ProposedWorkNode, ...] = (),
) -> ProposedWorkNode:
    return ProposedWorkNode(
        entity_type=entity_type,
        title=title,
        confidence=0.9,
        children=children,
    )


def _project_portfolio() -> tuple[Portfolio, TrajectoryEntity]:
    project = _entity(EntityType.PROJECT, "Platform")
    portfolio = Portfolio(name="V1.2", entities=[project])
    return portfolio, project


# --- happy path -------------------------------------------------------------


def test_existing_project__proposed_work_package__task() -> None:
    portfolio, project = _project_portfolio()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[
            _node(
                EntityType.WORK_PACKAGE,
                "Backend",
                children=[_node(EntityType.TASK, "Implement API")],
            )
        ],
    )

    result = validate_work_breakdown_proposal(portfolio, proposal)

    assert isinstance(result, ValidatedWorkBreakdownProposal)
    assert result.project_id == project.id
    assert result.anchor_id == project.id
    assert [child.entity_type for child in result.children] == [
        EntityType.WORK_PACKAGE
    ]
    assert [child.title for child in result.children] == ["Backend"]
    assert [
        child.entity_type for child in result.children[0].children
    ] == [EntityType.TASK]
    assert result.children[0].children[0].title == "Implement API"
    assert result.children[0].children[0].children == ()


def test_existing_deliverable_anchor__proposed_task() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    deliverable = _entity(EntityType.DELIVERABLE, "API spec")

    portfolio = Portfolio(
        name="V1.2",
        entities=[project, deliverable],
        relations=[_belongs_to(deliverable, project)],
    )

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=deliverable.id,
        children=[_node(EntityType.TASK, "Draft spec")],
    )

    result = validate_work_breakdown_proposal(portfolio, proposal)

    assert result.anchor_id == deliverable.id
    assert [
        (child.entity_type, child.title) for child in result.children
    ] == [(EntityType.TASK, "Draft spec")]


def test_existing_task_anchor__empty_children_is_a_leaf() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    task = _entity(EntityType.TASK, "Existing task")

    portfolio = Portfolio(
        name="V1.2",
        entities=[project, task],
        relations=[_belongs_to(task, project)],
    )

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=task.id,
        children=[],
    )

    result = validate_work_breakdown_proposal(portfolio, proposal)

    assert result.anchor_id == task.id
    assert result.children == ()


def test_proposed_work_package__work_package__task() -> None:
    portfolio, project = _project_portfolio()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[
            _node(
                EntityType.WORK_PACKAGE,
                "Platform",
                children=[
                    _node(
                        EntityType.WORK_PACKAGE,
                        "API",
                        children=[_node(EntityType.TASK, "Implement endpoint")],
                    )
                ],
            )
        ],
    )

    result = validate_work_breakdown_proposal(portfolio, proposal)

    assert [
        child.entity_type for child in result.children
    ] == [EntityType.WORK_PACKAGE]
    assert [
        child.entity_type for child in result.children[0].children
    ] == [EntityType.WORK_PACKAGE]
    assert (
        result.children[0].children[0].children[0].entity_type
        is EntityType.TASK
    )
    assert result.children[0].children[0].children[0].children == ()


def test_multiple_proposed_siblings_preserve_exact_input_order() -> None:
    portfolio, project = _project_portfolio()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[
            _node(EntityType.TASK, "C"),
            _node(EntityType.WORK_PACKAGE, "B"),
            _node(EntityType.TASK, "A"),
        ],
    )

    result = validate_work_breakdown_proposal(portfolio, proposal)

    assert [child.title for child in result.children] == ["C", "B", "A"]
    assert [
        child.entity_type for child in result.children
    ] == [EntityType.TASK, EntityType.WORK_PACKAGE, EntityType.TASK]


def test_same_title_siblings_are_both_preserved_as_distinct_nodes() -> None:
    # V1.2 must not deduplicate or infer semantic identity from titles:
    # two proposed siblings with identical entity_type, title, and
    # confidence remain two distinct nodes in their original order.
    portfolio, project = _project_portfolio()

    twin_a = ProposedWorkNode(
        entity_type=EntityType.TASK, title="Report", confidence=0.7
    )
    twin_b = ProposedWorkNode(
        entity_type=EntityType.TASK, title="Report", confidence=0.7
    )

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[twin_a, twin_b],
    )

    result = validate_work_breakdown_proposal(portfolio, proposal)

    assert len(result.children) == 2
    assert (
        [child.title for child in result.children]
        == ["Report", "Report"]
    )
    assert all(child.entity_type is EntityType.TASK for child in result.children)
    assert all(child.confidence == 0.7 for child in result.children)
    assert result.children[0] is not result.children[1]


# --- rejection: project -----------------------------------------------------


def test_unknown_project_rejected() -> None:
    portfolio, _ = _project_portfolio()

    proposal = WorkBreakdownProposal(
        project_id=uuid4(),
        anchor_id=uuid4(),
        children=[_node(EntityType.TASK, "Orphan")],
    )

    with pytest.raises(WorkBreakdownProposalError, match="unknown project"):
        validate_work_breakdown_proposal(portfolio, proposal)


def test_non_project_project_id_rejected() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    work_package = _entity(EntityType.WORK_PACKAGE, "Backend")

    portfolio = Portfolio(name="V1.2", entities=[project, work_package])

    proposal = WorkBreakdownProposal(
        project_id=work_package.id,
        anchor_id=work_package.id,
        children=[_node(EntityType.TASK, "Impl")],
    )

    with pytest.raises(WorkBreakdownProposalError, match="PROJECT"):
        validate_work_breakdown_proposal(portfolio, proposal)


# --- rejection: anchor ------------------------------------------------------


@pytest.mark.parametrize(
    ("relation_type",),
    [
        (RelationType.DEPENDS_ON,),
        (RelationType.RELATED_TO,),
    ],
    ids=["DEPENDS_ON", "RELATED_TO"],
)
def test_anchor_with_only_non_belongs_to_relation_rejected(
    relation_type: RelationType,
) -> None:
    # V1.1 ``build_work_breakdown`` remains authoritative: a candidate
    # WORK_PACKAGE linked to the project only through a non-BELONGS_TO
    # relation is not part of the WBS and therefore cannot be an anchor.
    # V1.2 does not independently inspect relation types here; it simply
    # refuses an anchor that the V1.1 projection excludes.
    project = _entity(EntityType.PROJECT, "Platform")
    candidate = _entity(EntityType.WORK_PACKAGE, "Candidate")

    portfolio = Portfolio(
        name="V1.2",
        entities=[project, candidate],
        relations=[
            TrajectoryRelation(
                source_id=candidate.id,
                target_id=project.id,
                relation_type=relation_type,
            ),
        ],
    )

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=candidate.id,
        children=[_node(EntityType.TASK, "Proposed")],
    )

    with pytest.raises(
        WorkBreakdownProposalError, match="not part of the work breakdown"
    ):
        validate_work_breakdown_proposal(portfolio, proposal)


# --- rejection: public argument contract ------------------------------------


@pytest.mark.parametrize(
    "bad_proposal",
    [
        {"project_id": None, "anchor_id": None, "children": []},
        "WorkBreakdownProposal",
        None,
    ],
    ids=["dict", "str", "None"],
)
def test_non_proposal_argument_rejected__portfolio_unchanged(
    bad_proposal: object,
) -> None:
    portfolio, project = _project_portfolio()
    before = portfolio.model_dump()

    with pytest.raises(
        WorkBreakdownProposalError, match="must be a WorkBreakdownProposal"
    ):
        validate_work_breakdown_proposal(portfolio, bad_proposal)

    # Rejected at the public boundary before any portfolio access.
    assert portfolio.model_dump() == before
    assert portfolio.get_entity(project.id) is not None


def test_unknown_anchor_rejected() -> None:
    portfolio, project = _project_portfolio()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=uuid4(),
        children=[_node(EntityType.TASK, "Orphan")],
    )

    with pytest.raises(
        WorkBreakdownProposalError, match="not part of the work breakdown"
    ):
        validate_work_breakdown_proposal(portfolio, proposal)


def test_anchor_outside_selected_projects_work_breakdown_rejected() -> None:
    selected = _entity(EntityType.PROJECT, "Selected")
    other = _entity(EntityType.PROJECT, "Other")
    outside_package = _entity(EntityType.WORK_PACKAGE, "Outside")
    outside_task = _entity(EntityType.TASK, "Outside task")

    portfolio = Portfolio(
        name="V1.2",
        entities=[selected, other, outside_package, outside_task],
        relations=[
            _belongs_to(outside_package, other),
            _belongs_to(outside_task, outside_package),
        ],
    )

    proposal_a = WorkBreakdownProposal(
        project_id=selected.id,
        anchor_id=outside_package.id,
        children=[_node(EntityType.TASK, "Proposed")],
    )
    proposal_b = WorkBreakdownProposal(
        project_id=selected.id,
        anchor_id=outside_task.id,
        children=[],
    )

    with pytest.raises(
        WorkBreakdownProposalError, match="not part of the work breakdown"
    ):
        validate_work_breakdown_proposal(portfolio, proposal_a)

    with pytest.raises(
        WorkBreakdownProposalError, match="not part of the work breakdown"
    ):
        validate_work_breakdown_proposal(portfolio, proposal_b)


# --- rejection: proposal containment ----------------------------------------


def test_disallowed_proposed_containment_rejected() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    work_package = _entity(EntityType.WORK_PACKAGE, "Build")

    portfolio = Portfolio(
        name="V1.2",
        entities=[project, work_package],
        relations=[_belongs_to(work_package, project)],
    )

    # A WORK_PACKAGE anchor may not contain a DELIVERABLE proposed child,
    # nor may a proposed WORK_PACKAGE contain a DELIVERABLE.
    proposal_invalid_root = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=work_package.id,
        children=[_node(EntityType.DELIVERABLE, "Inner spec")],
    )
    proposal_invalid_nested = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[
            _node(
                EntityType.WORK_PACKAGE,
                "Build",
                children=[_node(EntityType.DELIVERABLE, "Inner spec")],
            )
        ],
    )

    with pytest.raises(
        WorkBreakdownProposalError, match="may not contain.*deliverable"
    ):
        validate_work_breakdown_proposal(portfolio, proposal_invalid_root)

    with pytest.raises(
        WorkBreakdownProposalError, match="may not contain.*deliverable"
    ):
        validate_work_breakdown_proposal(portfolio, proposal_invalid_nested)


def test_task_proposed_anchor_rejects_proposed_children() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    task = _entity(EntityType.TASK, "Existing")

    portfolio = Portfolio(
        name="V1.2",
        entities=[project, task],
        relations=[_belongs_to(task, project)],
    )

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=task.id,
        children=[_node(EntityType.TASK, "Child task")],
    )

    with pytest.raises(WorkBreakdownProposalError, match="may not contain"):
        validate_work_breakdown_proposal(portfolio, proposal)


# --- schema hardening: proposed node types ----------------------------------


@pytest.mark.parametrize(
    "entity_type",
    [
        EntityType.PROJECT,
        EntityType.IDEA,
        EntityType.GOAL,
    ],
    ids=["PROJECT", "IDEA", "GOAL"],
)
def test_non_wbs_type__proposed_node_rejected_at_construction(
    entity_type: EntityType,
) -> None:
    with pytest.raises(ValidationError, match="entity_type"):
        _node(entity_type, "Nested")


def test_project__proposed_type_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        ProposedWorkNode(
            entity_type=EntityType.PROJECT,
            title="Nested project",
            confidence=0.5,
        )


def test_idea__proposed_type_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        ProposedWorkNode(
            entity_type=EntityType.IDEA,
            title="Spark",
            confidence=0.5,
        )


def test_wbs_node_types__proposed_types_accepted() -> None:
    for entity_type in (
        EntityType.DELIVERABLE,
        EntityType.WORK_PACKAGE,
        EntityType.TASK,
    ):
        node = ProposedWorkNode(
            entity_type=entity_type, title="OK", confidence=0.5
        )
        assert node.entity_type is entity_type


# --- schema hardening: title ------------------------------------------------


def test_empty_title__rejected_at_construction() -> None:
    with pytest.raises(ValidationError, match="title"):
        ProposedWorkNode(
            entity_type=EntityType.TASK, title="", confidence=0.5
        )


# --- schema hardening: confidence -------------------------------------------


def test_confidence__accepts_int_and_float() -> None:
    for confidence in (0, 1, 0.25):
        node = ProposedWorkNode(
            entity_type=EntityType.TASK, title="OK", confidence=confidence
        )
        assert node.confidence == float(confidence)


def test_confidence_below_zero_rejected() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        ProposedWorkNode(
            entity_type=EntityType.TASK, title="OK", confidence=-0.1
        )


def test_confidence_above_one_rejected() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        ProposedWorkNode(
            entity_type=EntityType.TASK, title="OK", confidence=1.5
        )


def test_confidence__true_rejected_explicitly() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        ProposedWorkNode(
            entity_type=EntityType.TASK, title="OK", confidence=True
        )


def test_confidence__false_rejected_explicitly() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        ProposedWorkNode(
            entity_type=EntityType.TASK, title="OK", confidence=False
        )


def test_confidence__string_rejected() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        ProposedWorkNode(
            entity_type=EntityType.TASK, title="OK", confidence="0.5"
        )


def test_confidence__nested_node_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        ProposedWorkNode(
            entity_type=EntityType.WORK_PACKAGE,
            title="Outer",
            confidence=0.5,
            children=[
                ProposedWorkNode(
                    entity_type=EntityType.TASK,
                    title="Bad",
                    confidence=True,
                )
            ],
        )


# --- purity and freshness ---------------------------------------------------


def test_v11_invariant_violation_surfaces_as_proposal_error() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    package = _entity(EntityType.WORK_PACKAGE, "P")
    subpackage = _entity(EntityType.WORK_PACKAGE, "S")

    portfolio = Portfolio(
        name="V1.2",
        entities=[project, package, subpackage],
        relations=[
            _belongs_to(package, project),
            _belongs_to(subpackage, package),
            _belongs_to(package, subpackage),
        ],
    )

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[_node(EntityType.TASK, "Whatever")],
    )

    with pytest.raises(WorkBreakdownProposalError, match="cycle"):
        validate_work_breakdown_proposal(portfolio, proposal)


def test_source_portfolio_unchanged() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    deliverable = _entity(EntityType.DELIVERABLE, "Spec")
    task = _entity(EntityType.TASK, "Existing")

    portfolio = Portfolio(
        name="V1.2",
        entities=[project, deliverable, task],
        relations=[
            _belongs_to(deliverable, project),
            _belongs_to(task, deliverable),
        ],
    )
    before = portfolio.model_dump()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=deliverable.id,
        children=[_node(EntityType.TASK, "Draft spec")],
    )

    validate_work_breakdown_proposal(portfolio, proposal)

    assert portfolio.model_dump() == before
    assert [entity.id for entity in portfolio.entities] == [
        project.id,
        deliverable.id,
        task.id,
    ]
    assert len(portfolio.relations) == 2
    assert portfolio.get_entity(project.id) is not None


def test_input_proposal_unchanged() -> None:
    portfolio, project = _project_portfolio()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[
            _node(
                EntityType.WORK_PACKAGE,
                "Backend",
                children=[_node(EntityType.TASK, "Implement API")],
            )
        ],
    )
    before = proposal.model_dump()
    original_children = proposal.children

    validate_work_breakdown_proposal(portfolio, proposal)

    assert proposal.model_dump() == before
    assert proposal.children is original_children
    assert proposal.children[0].children[0].title == "Implement API"


def test_validated_result_is_frozen() -> None:
    portfolio, project = _project_portfolio()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[_node(EntityType.WORK_PACKAGE, "Backend")],
    )

    result = validate_work_breakdown_proposal(portfolio, proposal)

    with pytest.raises(ValidationError):
        result.project_id = uuid4()

    with pytest.raises(ValidationError):
        result.children = ()

    with pytest.raises(ValidationError):
        result.children[0].title = "mutated"


def test_repeated_validation_is_equivalent_but_fresh() -> None:
    portfolio, project = _project_portfolio()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[
            _node(
                EntityType.WORK_PACKAGE,
                "Backend",
                children=[_node(EntityType.TASK, "Implement API")],
            )
        ],
    )

    first = validate_work_breakdown_proposal(portfolio, proposal)
    second = validate_work_breakdown_proposal(portfolio, proposal)

    assert first == second
    assert first is not second
    assert first.children is not second.children
    assert first.children[0] is not second.children[0]
    assert (
        first.children[0].children[0] is not second.children[0].children[0]
    )
    assert first.project_id == project.id
    assert first.anchor_id == project.id
    assert [child.title for child in first.children] == ["Backend"]


def test_work_breakdown_proposal_error_is_a_value_error() -> None:
    assert issubclass(WorkBreakdownProposalError, ValueError)


# --- single source of the WBS grammar predicate ------------------------------


@pytest.mark.parametrize(
    ("parent_type", "child_type"),
    [
        (EntityType.PROJECT, EntityType.DELIVERABLE),
        (EntityType.PROJECT, EntityType.WORK_PACKAGE),
        (EntityType.PROJECT, EntityType.TASK),
        (EntityType.DELIVERABLE, EntityType.WORK_PACKAGE),
        (EntityType.DELIVERABLE, EntityType.TASK),
        (EntityType.WORK_PACKAGE, EntityType.WORK_PACKAGE),
        (EntityType.WORK_PACKAGE, EntityType.TASK),
    ],
)
def test_predicate_allows_representative_granular_pairs(
    parent_type: EntityType,
    child_type: EntityType,
) -> None:
    assert is_work_breakdown_containment_allowed(parent_type, child_type) is True


@pytest.mark.parametrize(
    ("parent_type", "child_type"),
    [
        (EntityType.PROJECT, EntityType.PROJECT),
        (EntityType.DELIVERABLE, EntityType.DELIVERABLE),
        (EntityType.WORK_PACKAGE, EntityType.DELIVERABLE),
        (EntityType.WORK_PACKAGE, EntityType.PROJECT),
        (EntityType.TASK, EntityType.TASK),
        (EntityType.TASK, EntityType.WORK_PACKAGE),
        (EntityType.PROJECT, EntityType.GOAL),
        (EntityType.PROJECT, EntityType.IDEA),
        (EntityType.IDEA, EntityType.TASK),
        (EntityType.GOAL, EntityType.PROJECT),
    ],
)
def test_predicate_rejects_representative_disallowed_pairs(
    parent_type: EntityType,
    child_type: EntityType,
) -> None:
    assert is_work_breakdown_containment_allowed(parent_type, child_type) is False


def test_predicate_is_exposed_by_the_work_breakdown_module() -> None:
    import trajectory_os.domain.work_breakdown as work_breakdown_module

    bound = work_breakdown_module.is_work_breakdown_containment_allowed
    assert bound is is_work_breakdown_containment_allowed
    assert isinstance(
        work_breakdown_module.is_work_breakdown_containment_allowed(
            EntityType.WORK_PACKAGE, EntityType.TASK
        ),
        bool,
    )


def test_proposed_nodes_carry_no_canonical_entity_identity() -> None:
    # Proposed nodes carry no canonical entity UUID; only the proposal's
    # project/anchor ids reference existing portfolio entities.
    assert "entity_id" not in ProposedWorkNode.model_fields
    assert set(ProposedWorkNode.model_fields) == {
        "entity_type",
        "title",
        "description",
        "confidence",
        "children",
    }
    assert set(WorkBreakdownProposal.model_fields) == {
        "project_id",
        "anchor_id",
        "children",
    }


# --- recursion safety: canonical WBS membership (V1.2-B2b) -----------------


def test_deep_wbs__anchor_membership_does_not_depend_on_recursion() -> None:
    # A WORK_PACKAGE chain whose depth (5000) exceeds CPython's default
    # recursion limit (1000). Anchor membership over this deep but valid
    # canonical WBS must succeed via iterative traversal; a recursive
    # membership helper would raise RecursionError here.
    depth = 5000

    project = _entity(EntityType.PROJECT, "Platform")
    packages = [
        _entity(EntityType.WORK_PACKAGE, f"Level {level}")
        for level in range(depth)
    ]

    relations = [_belongs_to(packages[0], project)]
    relations.extend(
        _belongs_to(packages[level], packages[level - 1])
        for level in range(1, depth)
    )

    portfolio = Portfolio(
        name="V1.2",
        entities=[project, *packages],
        relations=relations,
    )

    deepest = packages[-1]
    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=deepest.id,
        children=[_node(EntityType.TASK, "Leaf task")],
    )

    result = validate_work_breakdown_proposal(portfolio, proposal)

    assert result.anchor_id == deepest.id
    assert [child.title for child in result.children] == ["Leaf task"]


# --- hostile-boundary revalidation (V1.2-B2a: strict) -----------------------
# These prove the public domain boundary is STRICT: Pydantic strict
# revalidation of the proposal's current state rejects malformed/corrupted
# instances (model_construct bypass, object.__setattr__ tampering, malformed
# nested nodes) rather than allowing coercion/repair. In every case the
# portfolio is left unchanged and the caller's proposal/current state is not
# repaired or mutated by validation.


def _assert_portfolio_unchanged(
    portfolio: Portfolio, before: object
) -> None:
    assert portfolio.model_dump() == before


@contextmanager
def _suppress_pydantic_serializer_warnings():
    # Revalidating intentionally-malformed state makes Pydantic's python
    # serializer emit "PydanticSerializationUnexpectedValue" UserWarnings.
    # Those are expected here: the malformed values are exactly what we are
    # asserting get rejected, so silence the noise while keeping the warning
    # visible for any other source.
    with catch_warnings():
        filterwarnings(
            "ignore",
            message=r"Pydantic serializer warnings",
            category=UserWarning,
            module=r"pydantic",
        )
        yield


def test_hostile_A__project_id_string_via_model_construct__rejected() -> None:
    portfolio, project = _project_portfolio()
    before = portfolio.model_dump()

    proposal = WorkBreakdownProposal.model_construct(
        project_id=str(project.id),
        anchor_id=project.id,
        children=[_node(EntityType.TASK, "Child")],
    )
    assert isinstance(proposal.project_id, str)

    with (
        pytest.raises(WorkBreakdownProposalError, match="revalidation"),
        _suppress_pydantic_serializer_warnings(),
    ):
        validate_work_breakdown_proposal(portfolio, proposal)

    # Not repaired: project_id is still the original str, not a UUID.
    assert isinstance(proposal.project_id, str)
    _assert_portfolio_unchanged(portfolio, before)


def test_hostile_B__project_id_tampered_to_string__rejected() -> None:
    portfolio, project = _project_portfolio()
    before = portfolio.model_dump()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[_node(EntityType.TASK, "Child")],
    )
    assert isinstance(proposal.project_id, UUID)
    object.__setattr__(proposal, "project_id", str(project.id))
    assert isinstance(proposal.project_id, str)

    with (
        pytest.raises(WorkBreakdownProposalError, match="revalidation"),
        _suppress_pydantic_serializer_warnings(),
    ):
        validate_work_breakdown_proposal(portfolio, proposal)

    # Not repaired: still the tampered str.
    assert isinstance(proposal.project_id, str)
    _assert_portfolio_unchanged(portfolio, before)


def test_hostile_C__nested_non_wbs_node_via_model_construct__rejected() -> None:
    portfolio, project = _project_portfolio()
    before = portfolio.model_dump()

    bad_child = ProposedWorkNode.model_construct(
        entity_type=EntityType.IDEA,
        title="Bad",
        confidence=0.5,
        children=(),
    )
    proposal = WorkBreakdownProposal.model_construct(
        project_id=project.id,
        anchor_id=project.id,
        children=[bad_child],
    )

    with (
        pytest.raises(WorkBreakdownProposalError, match="revalidation"),
        _suppress_pydantic_serializer_warnings(),
    ):
        validate_work_breakdown_proposal(portfolio, proposal)

    # The nested node is not repaired: still an IDEA.
    assert bad_child.entity_type is EntityType.IDEA
    _assert_portfolio_unchanged(portfolio, before)


def test_hostile_D__nested_bool_confidence_via_model_construct__rejected() -> None:
    portfolio, project = _project_portfolio()
    before = portfolio.model_dump()

    bad_child = ProposedWorkNode.model_construct(
        entity_type=EntityType.TASK,
        title="Bad",
        confidence=True,
        children=(),
    )
    proposal = WorkBreakdownProposal.model_construct(
        project_id=project.id,
        anchor_id=project.id,
        children=[bad_child],
    )

    with (
        pytest.raises(WorkBreakdownProposalError, match="revalidation"),
        _suppress_pydantic_serializer_warnings(),
    ):
        validate_work_breakdown_proposal(portfolio, proposal)

    # The nested node is not repaired: confidence is still the bool True.
    assert bad_child.confidence is True
    _assert_portfolio_unchanged(portfolio, before)


def test_hostile_E__nested_confidence_tampered_above_one__rejected() -> None:
    portfolio, project = _project_portfolio()
    before = portfolio.model_dump()

    child = _node(EntityType.TASK, "Child")
    assert child.confidence == 0.9
    object.__setattr__(child, "confidence", 1.7)
    assert child.confidence == 1.7

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[child],
    )

    with (
        pytest.raises(WorkBreakdownProposalError, match="revalidation"),
        _suppress_pydantic_serializer_warnings(),
    ):
        validate_work_breakdown_proposal(portfolio, proposal)

    # The nested node is not repaired/clamped: confidence is still 1.7.
    assert child.confidence == 1.7
    _assert_portfolio_unchanged(portfolio, before)


def test_hostile_F__nested_entity_type_tampered_to_idea__rejected() -> None:
    portfolio, project = _project_portfolio()
    before = portfolio.model_dump()

    child = _node(EntityType.TASK, "Child")
    assert child.entity_type is EntityType.TASK
    object.__setattr__(child, "entity_type", EntityType.IDEA)
    assert child.entity_type is EntityType.IDEA

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[child],
    )

    with (
        pytest.raises(WorkBreakdownProposalError, match="revalidation"),
        _suppress_pydantic_serializer_warnings(),
    ):
        validate_work_breakdown_proposal(portfolio, proposal)

    # The nested node is not repaired: still an IDEA.
    assert child.entity_type is EntityType.IDEA
    _assert_portfolio_unchanged(portfolio, before)


# --- hostile boundary: serializer depth/self-reference -----------------------
# The serializer can fail on an otherwise-construction-valid proposal whose
# proposed subtree is pathologically deep (depth limit) or self-referencing
# (id repeated). The public boundary must translate that into
# WorkBreakdownProposalError instead of leaking Pydantic's ValueError, and
# it must not make such trees valid.


def test_deep_proposed_subtree__serializer_failure_translated() -> None:
    portfolio, project = _project_portfolio()
    before_portfolio = portfolio.model_dump()

    # Depth (5000) is far above Pydantic's serializer recursion limit, so
    # model_dump raises "ValueError: Circular reference detected (depth
    # exceeded)". The subtree is still otherwise valid; the test only
    # requires the failure to surface through the error contract.
    depth = 5000
    node = _node(EntityType.TASK, "L0")
    for level in range(1, depth + 1):
        node = _node(EntityType.TASK, f"L{level}", children=[node])

    outer = node
    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[outer],
    )

    with pytest.raises(
        WorkBreakdownProposalError, match="revalidation"
    ) as excinfo:
        validate_work_breakdown_proposal(portfolio, proposal)

    # The original serializer failure is preserved as the cause.
    assert excinfo.value.__cause__ is not None

    # The caller's proposal was not touched or repaired.
    assert proposal.project_id == project.id
    assert proposal.anchor_id == project.id
    assert len(proposal.children) == 1
    assert proposal.children[0] is outer
    assert outer.title == f"L{depth}"

    # The portfolio was not touched either.
    assert portfolio.model_dump() == before_portfolio


def test_self_referencing_child__serializer_failure_translated() -> None:
    # A real id-cycle: a node whose children tuple contains itself. Normal
    # proposal construction would itself recurse before reaching the
    # boundary, so the host is built with model_construct and the tampered
    # node is attached as-is.
    portfolio, project = _project_portfolio()
    before_portfolio = portfolio.model_dump()

    node = _node(EntityType.TASK, "Self-reference")
    object.__setattr__(node, "children", (node,))
    assert node.children[0] is node

    proposal = WorkBreakdownProposal.model_construct(
        project_id=project.id,
        anchor_id=project.id,
        children=[node],
    )

    with pytest.raises(
        WorkBreakdownProposalError, match="revalidation"
    ) as excinfo:
        validate_work_breakdown_proposal(portfolio, proposal)

    # The serializer exception is translated, not leaked.
    assert excinfo.value.__cause__ is not None

    # The node was not repaired: it still refers to itself.
    assert node.children[0] is node

    _assert_portfolio_unchanged(portfolio, before_portfolio)
