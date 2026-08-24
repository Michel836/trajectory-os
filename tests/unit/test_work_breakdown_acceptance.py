"""Unit tests for work-breakdown proposal acceptance (V1.3-A + V1.3-B)."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import build_work_breakdown
from trajectory_os.domain.work_breakdown_acceptance import (
    WorkBreakdownAcceptanceError,
    WorkBreakdownAcceptanceResult,
    accept_work_breakdown_proposal,
)
from trajectory_os.domain.work_breakdown_proposals import (
    ProposedWorkNode,
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
    confidence: float = 0.9,
    children: tuple[ProposedWorkNode, ...] = (),
) -> ProposedWorkNode:
    return ProposedWorkNode(
        entity_type=entity_type,
        title=title,
        confidence=confidence,
        children=children,
    )


def _project_portfolio() -> tuple[Portfolio, TrajectoryEntity]:
    project = _entity(EntityType.PROJECT, "Platform")
    return Portfolio(name="V1.3-A", entities=[project]), project


def _shape(result: WorkBreakdownAcceptanceResult) -> list[tuple[str, str]]:
    return [
        (entity.entity_type.name, entity.title)
        for entity in result.portfolio.entities
    ]


def _attachment_shape(
    result: WorkBreakdownAcceptanceResult,
) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """Semantic parentage (child -> parent) over ALL relations, UUID-independent."""
    by_id = {entity.id: entity for entity in result.portfolio.entities}

    def label(entity_id: object) -> tuple[str, str]:
        entity = by_id[entity_id]
        return (entity.entity_type.name, entity.title)

    return [
        (label(relation.source_id), label(relation.target_id))
        for relation in result.portfolio.relations
    ]


def _proposal_for(
    portfolio: Portfolio,
    project: TrajectoryEntity,
    children: list[ProposedWorkNode],
    *,
    anchor_id: "object" = None,
) -> WorkBreakdownProposal:
    return WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=anchor_id if anchor_id is not None else project.id,
        children=children,
    )


# --- A: PROJECT -> WORK_PACKAGE -> TASK -------------------------------------


def test_A__project__proposed_work_package__task() -> None:
    portfolio, project = _project_portfolio()

    task = _node(EntityType.TASK, "Implement API")
    wp = _node(EntityType.WORK_PACKAGE, "Backend", children=[task])

    result = accept_work_breakdown_proposal(
        portfolio, _proposal_for(portfolio, project, [wp])
    )

    assert len(result.portfolio.entities) == 3
    by_title = {
        entity.title: entity for entity in result.portfolio.entities
    }
    assert by_title["Backend"].entity_type is EntityType.WORK_PACKAGE
    assert by_title["Implement API"].entity_type is EntityType.TASK
    assert [
        entity.entity_type for entity in result.portfolio.entities[1:]
    ] == [EntityType.WORK_PACKAGE, EntityType.TASK]


# --- B: TASK under existing DELIVERABLE --------------------------------------


def test_B__proposed_task_under_existing_deliverable_anchor() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    deliverable = _entity(EntityType.DELIVERABLE, "API spec")

    portfolio = Portfolio(
        name="V1.3-A",
        entities=[project, deliverable],
        relations=[_belongs_to(deliverable, project)],
    )

    result = accept_work_breakdown_proposal(
        portfolio,
        _proposal_for(
            portfolio,
            project,
            [_node(EntityType.TASK, "Draft spec")],
            anchor_id=deliverable.id,
        ),
    )

    assert len(result.portfolio.entities) == 3
    new_task = result.portfolio.entities[-1]
    assert new_task.entity_type is EntityType.TASK
    assert new_task.title == "Draft spec"

    relation = result.portfolio.relations[-1]
    assert relation.source_id == new_task.id
    assert relation.target_id == deliverable.id


# --- C: WP -> WP -> TASK parentage -------------------------------------------


def test_C__work_package__work_package__task_parentage() -> None:
    portfolio, project = _project_portfolio()

    leaf = _node(EntityType.TASK, "Implement endpoint")
    inner = _node(EntityType.WORK_PACKAGE, "API", children=[leaf])
    outer = _node(EntityType.WORK_PACKAGE, "Platform", children=[inner])

    result = accept_work_breakdown_proposal(
        portfolio, _proposal_for(portfolio, project, [outer])
    )

    outer_e, inner_e, leaf_e = (
        result.portfolio.entities[1],
        result.portfolio.entities[2],
        result.portfolio.entities[3],
    )

    new_relations = result.portfolio.relations[len(portfolio.relations) :]
    assert len(new_relations) == 3  # none pre-existing + 3 new
    assert [
        (rel.source_id, rel.target_id) for rel in new_relations
    ] == [
        (outer_e.id, project.id),
        (inner_e.id, outer_e.id),
        (leaf_e.id, inner_e.id),
    ]


# --- D: sibling order ----------------------------------------------------------


def test_D__sibling_order_preserved_exactly() -> None:
    portfolio, project = _project_portfolio()

    children = [
        _node(EntityType.TASK, "C"),
        _node(EntityType.WORK_PACKAGE, "B"),
        _node(EntityType.TASK, "A"),
    ]

    result = accept_work_breakdown_proposal(
        portfolio, _proposal_for(portfolio, project, children)
    )

    assert [
        entity.title for entity in result.portfolio.entities[1:]
    ] == ["C", "B", "A"]
    assert [entity.entity_type for entity in result.portfolio.entities[1:]] == [
        EntityType.TASK,
        EntityType.WORK_PACKAGE,
        EntityType.TASK,
    ]


# --- E: parent before descendants ----------------------------------------------


def test_E__parent_always_materialized_before_descendants() -> None:
    portfolio, project = _project_portfolio()

    leaf = _node(EntityType.TASK, "Leaf")
    mid = _node(EntityType.WORK_PACKAGE, "Mid", children=[leaf])
    root_wp = _node(EntityType.WORK_PACKAGE, "Root", children=[mid])
    other = _node(EntityType.TASK, "Other")

    result = accept_work_breakdown_proposal(
        portfolio, _proposal_for(portfolio, project, [root_wp, other])
    )

    titles = [entity.title for entity in result.portfolio.entities]
    positions = {title: index for index, title in enumerate(titles)}

    assert positions["Root"] < positions["Mid"] < positions["Leaf"]
    assert positions["Root"] < positions["Other"]


# --- F: relation order == entity creation order --------------------------------


def test_F__relation_order_matches_entity_creation_order() -> None:
    portfolio, project = _project_portfolio()

    children = [
        _node(EntityType.TASK, "T-first"),
        _node(
            EntityType.WORK_PACKAGE,
            "W",
            children=[
                _node(EntityType.TASK, "T-inner-a"),
                _node(EntityType.TASK, "T-inner-b"),
            ],
        ),
    ]

    result = accept_work_breakdown_proposal(
        portfolio, _proposal_for(portfolio, project, children)
    )

    created_entity_ids = result.created_entity_ids
    new_entities = result.portfolio.entities[len(portfolio.entities) :]
    new_relations = result.portfolio.relations[len(portfolio.relations) :]
    assert [
        entity.id for entity in new_entities
    ] == list(created_entity_ids)

    assert [
        relation.source_id for relation in new_relations
    ] == list(created_entity_ids)
    assert len(result.created_relation_ids) == len(created_entity_ids)


# --- G: top-level BELONGS_TO anchor --------------------------------------------


def test_G__top_level_child_relates_belongs_to_anchor() -> None:
    portfolio, project = _project_portfolio()

    proposal = _proposal_for(
        portfolio, project, [_node(EntityType.TASK, "Top task")]
    )

    result = accept_work_breakdown_proposal(portfolio, proposal)

    relation = result.portfolio.relations[-1]
    assert relation.relation_type is RelationType.BELONGS_TO
    assert relation.target_id == project.id
    assert relation.source_id == result.portfolio.entities[-1].id


# --- H: nested BELONGS_TO new parent --------------------------------------------


def test_H__nested_child_relates_belongs_to_new_parent() -> None:
    portfolio, project = _project_portfolio()

    wp = _node(
        EntityType.WORK_PACKAGE,
        "Backend",
        children=[_node(EntityType.TASK, "Child task")],
    )

    result = accept_work_breakdown_proposal(
        portfolio, _proposal_for(portfolio, project, [wp])
    )

    new_wp = result.portfolio.entities[1]
    new_task = result.portfolio.entities[2]

    nested_relation = result.portfolio.relations[-1]
    assert nested_relation.relation_type is RelationType.BELONGS_TO
    assert nested_relation.source_id == new_task.id
    assert nested_relation.target_id == new_wp.id
    # The nested parent is a freshly materialized entity, not the anchor.
    assert new_wp.id != project.id


# --- I: entity canonical defaults -------------------------------------------------


def test_I__created_entities_are_incubator_user_confirmed_full_confidence() -> None:
    portfolio, project = _project_portfolio()

    result = accept_work_breakdown_proposal(
        portfolio,
        _proposal_for(
            portfolio,
            project,
            [
                _node(
                    EntityType.WORK_PACKAGE,
                    "WP",
                    children=[_node(EntityType.TASK, "T")],
                )
            ],
        ),
    )

    for entity in result.portfolio.entities[1:]:
        assert entity.status is EntityStatus.INCUBATOR
        assert entity.source is SourceKind.USER_CONFIRMED
        assert entity.confidence == 1.0


# --- J: relation canonical defaults ---------------------------------------------


def test_J__created_relations_are_belongs_to_user_confirmed_full_confidence() -> None:
    portfolio, project = _project_portfolio()

    result = accept_work_breakdown_proposal(
        portfolio,
        _proposal_for(portfolio, project, [_node(EntityType.TASK, "T")]),
    )

    for relation in result.portfolio.relations:
        assert relation.relation_type is RelationType.BELONGS_TO
        assert relation.source is SourceKind.USER_CONFIRMED
        assert relation.confidence == 1.0


# --- K: proposal confidence not copied --------------------------------------------


def test_K__proposed_node_confidence_is_not_copied() -> None:
    portfolio, project = _project_portfolio()

    children = [
        _node(EntityType.WORK_PACKAGE, "WP", confidence=0.25),
        _node(EntityType.TASK, "T", confidence=0.6),
    ]

    result = accept_work_breakdown_proposal(
        portfolio, _proposal_for(portfolio, project, children)
    )

    assert [entity.confidence for entity in result.portfolio.entities[1:]] == [
        1.0,
        1.0,
    ]


# --- L: source portfolio unchanged --------------------------------------------------


def test_L__source_portfolio_is_unchanged() -> None:
    portfolio, project = _project_portfolio()
    before = portfolio.model_dump()

    proposal = _proposal_for(
        portfolio,
        project,
        [
            _node(
                EntityType.WORK_PACKAGE,
                "WP",
                children=[_node(EntityType.TASK, "T")],
            )
        ],
    )

    accept_work_breakdown_proposal(portfolio, proposal)

    assert portfolio.model_dump() == before
    assert [entity.id for entity in portfolio.entities] == [project.id]
    assert portfolio.relations == []


# --- M: proposal unchanged ------------------------------------------------------------


def test_M__input_proposal_is_unchanged() -> None:
    portfolio, project = _project_portfolio()

    proposal = _proposal_for(
        portfolio,
        project,
        [
            _node(
                EntityType.WORK_PACKAGE,
                "WP",
                children=[_node(EntityType.TASK, "T")],
            )
        ],
    )
    before = proposal.model_dump()
    original_children = proposal.children

    accept_work_breakdown_proposal(portfolio, proposal)

    assert proposal.model_dump() == before
    assert proposal.children is original_children
    assert proposal.children[0].children[0].title == "T"


# --- N: returned portfolio same id/name ---------------------------------------------


def test_N__returned_portfolio_keeps_source_id_and_name() -> None:
    portfolio, project = _project_portfolio()

    result = accept_work_breakdown_proposal(
        portfolio, _proposal_for(portfolio, project, [_node(EntityType.TASK, "T")])
    )

    assert result.portfolio.id == portfolio.id
    assert result.portfolio.name == portfolio.name
    assert result.portfolio is not portfolio


# --- O: returned members are fresh objects --------------------------------------------


def test_O__returned_entities_and_relations_are_not_source_objects() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    deliverable = _entity(EntityType.DELIVERABLE, "Spec")
    existing_relation = _belongs_to(deliverable, project)

    portfolio = Portfolio(
        name="V1.3-A",
        entities=[project, deliverable],
        relations=[existing_relation],
    )

    result = accept_work_breakdown_proposal(
        portfolio,
        _proposal_for(
            portfolio,
            project,
            [_node(EntityType.TASK, "T")],
            anchor_id=deliverable.id,
        ),
    )

    source_entities = [project, deliverable]
    for entity in result.portfolio.entities[:2]:
        assert all(entity is not source for source in source_entities)

    source_relation = existing_relation
    for relation in result.portfolio.relations[:1]:
        assert relation is not source_relation
        assert relation.id == source_relation.id

    # Newly created members are fresh objects with fresh ids too.
    created_entities = result.portfolio.entities[2:]
    created_relations = result.portfolio.relations[1:]
    source_ids = {project.id, deliverable.id}
    for entity in created_entities:
        assert all(entity is not source for source in source_entities)
        assert entity.id not in source_ids
    for relation in created_relations:
        assert relation is not source_relation
        assert relation.id != source_relation.id


# --- P: result frozen -------------------------------------------------------------------


def test_P__result_is_frozen() -> None:
    portfolio, project = _project_portfolio()

    result = accept_work_breakdown_proposal(
        portfolio, _proposal_for(portfolio, project, [_node(EntityType.TASK, "T")])
    )

    assert isinstance(result, WorkBreakdownAcceptanceResult)

    with pytest.raises(ValidationError):
        result.portfolio = Portfolio(name="other")

    with pytest.raises(ValidationError):
        result.created_entity_ids = ()

    with pytest.raises(ValidationError):
        result.created_relation_ids = (uuid4(),)


# --- Q: trace UUID order exact -----------------------------------------------------------


def test_Q__created_uuid_trace_order_is_exact() -> None:
    portfolio, project = _project_portfolio()
    pre_entities = len(portfolio.entities)
    pre_relations = len(portfolio.relations)

    wp_node = _node(
        EntityType.WORK_PACKAGE,
        "WP",
        children=[
            _node(EntityType.TASK, "T-a"),
            _node(EntityType.TASK, "T-b"),
        ],
    )

    result = accept_work_breakdown_proposal(
        portfolio, _proposal_for(portfolio, project, [wp_node])
    )

    new_entities = result.portfolio.entities[pre_entities:]
    new_relations = result.portfolio.relations[pre_relations:]

    assert [entity.title for entity in new_entities] == ["WP", "T-a", "T-b"]
    assert [
        (relation.source_id, relation.target_id) for relation in new_relations
    ] == [
        (new_entities[0].id, project.id),
        (new_entities[1].id, new_entities[0].id),
        (new_entities[2].id, new_entities[0].id),
    ]

    # The id traces must equal the actual members in materialization order.
    assert result.created_entity_ids == tuple(
        entity.id for entity in new_entities
    )
    assert result.created_relation_ids == tuple(
        relation.id for relation in new_relations
    )

    assert len(result.portfolio.entities) == pre_entities + 3


# --- R: V1.2 failure translated ------------------------------------------------------------


def test_R__v1_2_validation_failure_translated_with_cause() -> None:
    portfolio, project = _project_portfolio()

    # Unknown project: V1.2 rejects this and acceptance must translate.
    proposal = WorkBreakdownProposal(
        project_id=uuid4(),
        anchor_id=uuid4(),
        children=[_node(EntityType.TASK, "Orphan")],
    )

    with pytest.raises(
        WorkBreakdownAcceptanceError, match="during proposal validation"
    ) as excinfo:
        accept_work_breakdown_proposal(portfolio, proposal)

    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value.__cause__, WorkBreakdownProposalError)
    assert not isinstance(excinfo.value, WorkBreakdownProposalError)

    # Disallowed containment: also translated from the V1.2 error.
    bad_proposal = _proposal_for(
        portfolio,
        project,
        [
            _node(
                EntityType.WORK_PACKAGE,
                "WP",
                children=[_node(EntityType.DELIVERABLE, "Nope")],
            )
        ],
    )

    with pytest.raises(WorkBreakdownAcceptanceError) as excinfo2:
        accept_work_breakdown_proposal(portfolio, bad_proposal)

    assert isinstance(excinfo2.value.__cause__, WorkBreakdownProposalError)

    # The source portfolio was not mutated by either failed call.
    assert len(portfolio.entities) == 1
    assert portfolio.relations == []


# --- S: wrong public argument types rejected --------------------------------------------------


@pytest.mark.parametrize(
    ("bad_portfolio", "bad_proposal"),
    [
        (None, None),
        ({}, None),
        ("Portfolio", None),
        (Portfolio(name="ok"), None),
        (Portfolio(name="ok"), {}),
        (Portfolio(name="ok"), "WorkBreakdownProposal"),
        (Portfolio(name="ok"), 42),
    ],
    ids=[
        "none-none",
        "dict-none",
        "str-none",
        "portfolio-none",
        "portfolio-dict",
        "portfolio-str",
        "portfolio-int",
    ],
)
def test_S__wrong_argument_types_rejected(
    bad_portfolio: "object", bad_proposal: "object"
) -> None:
    portfolio, project = _project_portfolio()
    before = portfolio.model_dump()
    bad_before = (
        bad_portfolio.model_dump() if isinstance(bad_portfolio, Portfolio) else None
    )

    with pytest.raises(WorkBreakdownAcceptanceError):
        accept_work_breakdown_proposal(bad_portfolio, bad_proposal)
    with pytest.raises(WorkBreakdownAcceptanceError):
        accept_work_breakdown_proposal(portfolio, None)

    # Guard failures happen before any portfolio access: state unchanged.
    if bad_before is not None:
        assert bad_portfolio.model_dump() == bad_before
    assert portfolio.model_dump() == before


def test_work_breakdown_acceptance_error_is_a_value_error() -> None:
    assert issubclass(WorkBreakdownAcceptanceError, ValueError)


def test_domain_package_exports_v13a_symbols() -> None:
    import trajectory_os.domain as domain

    assert domain.WorkBreakdownAcceptanceError is WorkBreakdownAcceptanceError
    assert (
        domain.WorkBreakdownAcceptanceResult is WorkBreakdownAcceptanceResult
    )
    assert domain.accept_work_breakdown_proposal is accept_work_breakdown_proposal


# --- V1.3-B1: stale anchor / revalidation at acceptance time ----------------


def test_B1__stale_anchor__acceptance_revalidates_against_current_state() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    anchor = _entity(EntityType.DELIVERABLE, "Spec")

    p1 = Portfolio(
        name="V1.3-B",
        entities=[project, anchor],
        relations=[_belongs_to(anchor, project)],
    )

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=anchor.id,
        children=[_node(EntityType.TASK, "T")],
    )

    # V1.2 succeeds against P1: the anchor is inside the current WBS and the
    # proposal is a valid DELIVERABLE -> TASK pair.
    validated = validate_work_breakdown_proposal(p1, proposal)
    assert validated.anchor_id == anchor.id

    # P2 is a fresh, CURRENT portfolio with the same canonical entities but
    # the BELONGS_TO relation that placed the anchor in the project WBS has
    # been removed. The same proposal is no longer anchor-valid.
    p2 = Portfolio(
        id=p1.id,
        name=p1.name,
        entities=list(p1.entities),
        relations=[],
    )

    p1_before = p1.model_dump()
    p2_before = p2.model_dump()

    with pytest.raises(WorkBreakdownAcceptanceError) as excinfo:
        accept_work_breakdown_proposal(p2, proposal)

    # Acceptance re-runs V1.2 against the CURRENT canonical state and
    # translates its failure, preserving the V1.2 error as the cause.
    assert excinfo.value.__cause__ is not None
    assert isinstance(excinfo.value.__cause__, WorkBreakdownProposalError)

    # No source mutation: neither P1, P2, nor the proposal changed.
    assert p1.model_dump() == p1_before
    assert p2.model_dump() == p2_before
    assert proposal.children == (proposal.children,)[0]


# --- V1.3-B2: same-title siblings --------------------------------------------


def test_B2__same_title_siblings__neither_collapsed() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    anchor = _entity(EntityType.DELIVERABLE, "Spec")

    portfolio = Portfolio(
        name="V1.3-B",
        entities=[project, anchor],
        relations=[_belongs_to(anchor, project)],
    )

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=anchor.id,
        children=[
            _node(EntityType.TASK, "Review"),
            _node(EntityType.TASK, "Review"),
        ],
    )

    result = accept_work_breakdown_proposal(portfolio, proposal)

    created = result.portfolio.entities[-2:]
    assert len(created) == 2

    # Both canonical entities created, neither collapsed, both titles kept.
    assert [entity.title for entity in created] == ["Review", "Review"]
    assert [
        entity.entity_type for entity in created
    ] == [EntityType.TASK, EntityType.TASK]

    # Distinct uuids and order preserved exactly.
    assert created[0].id != created[1].id
    assert result.created_entity_ids == (created[0].id, created[1].id)
    assert all(entity.id not in {project.id, anchor.id} for entity in created)

    # Both new tasks attach to the anchor, in proposal order.
    new_relations = result.portfolio.relations[-2:]
    assert [relation.source_id for relation in new_relations] == [
        created[0].id,
        created[1].id,
    ]
    assert all(relation.target_id == anchor.id for relation in new_relations)


# --- V1.3-B3: same title as existing canonical work ---------------------------


def test_B3__same_title_as_existing_canonical_work__no_deduplication() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    existing = _entity(EntityType.TASK, "Analyse")

    portfolio = Portfolio(
        name="V1.3-B",
        entities=[project, existing],
        relations=[_belongs_to(existing, project)],
    )
    before_entities = [entity.id for entity in portfolio.entities]

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[_node(EntityType.TASK, "Analyse")],
    )

    result = accept_work_breakdown_proposal(portfolio, proposal)

    analyses = [entity for entity in result.portfolio.entities if entity.title == "Analyse"]
    assert len(analyses) == 2

    # The existing canonical entity remains, with its original id, in its
    # original position; the proposed node became ONE additional entity.
    assert analyses[0].id == existing.id
    assert analyses[0].entity_type is EntityType.TASK
    assert [entity.id for entity in result.portfolio.entities[:2]] == before_entities
    assert len(result.portfolio.entities) == len(before_entities) + 1

    # Distinct uuid: no deduplication or identity inference by title.
    assert analyses[1].id != existing.id
    assert result.created_entity_ids == (analyses[1].id,)


# --- V1.3-B4: non-BELONGS_TO does not create anchor membership ----------------


@pytest.mark.parametrize(
    ("relation_type", "case"),
    [
        (RelationType.RELATED_TO, "related_to"),
        (RelationType.CONTRIBUTES_TO, "contributes_to"),
    ],
)
def test_B4__non_belongs_to_project_link__not_an_anchor(
    relation_type: RelationType, case: str
) -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    anchor = _entity(EntityType.DELIVERABLE, "Spec")

    # The candidate anchor is connected to the project ONLY through a
    # non-containment relation, never BELONGS_TO.
    portfolio = Portfolio(
        name="V1.3-B",
        entities=[project, anchor],
        relations=[
            TrajectoryRelation(
                source_id=anchor.id,
                target_id=project.id,
                relation_type=relation_type,
            )
        ],
    )

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=anchor.id,
        children=[_node(EntityType.TASK, "T")],
    )

    before = portfolio.model_dump()

    # V1.3 does no independent relation inspection: anchor membership is
    # decided by V1.2, which fails because the anchor is outside the WBS.
    with pytest.raises(WorkBreakdownAcceptanceError) as excinfo:
        accept_work_breakdown_proposal(portfolio, proposal)

    assert excinfo.value.__cause__ is not None
    assert isinstance(excinfo.value.__cause__, WorkBreakdownProposalError)
    assert portfolio.model_dump() == before


# --- V1.3-B5: deep canonical WBS -----------------------------------------------


def test_B5__deep_canonical_wbs__acceptance_without_recursion() -> None:
    # A WORK_PACKAGE BELONGS_TO chain of depth 5000 far exceeds CPython's
    # default recursion limit; acceptance over the deepest anchor must
    # succeed without RecursionError (iterative V1.2 membership + V1.3).
    depth = 5000

    project = _entity(EntityType.PROJECT, "Platform")
    packages = [
        _entity(EntityType.WORK_PACKAGE, f"Level {level}")
        for level in range(depth)
    ]

    relations = [_belongs_to(packages[0], project)]
    relations.extend(
        _belongs_to(packages[level], packages[level - 1]) for level in range(1, depth)
    )

    portfolio = Portfolio(
        name="V1.3-B",
        entities=[project, *packages],
        relations=relations,
    )
    before_entities = portfolio.model_dump()

    deepest = packages[-1]
    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=deepest.id,
        children=[_node(EntityType.TASK, "Leaf")],  # the proposal is shallow
    )

    result = accept_work_breakdown_proposal(portfolio, proposal)

    # Exactly one new entity: the single proposed TASK.
    assert len(result.portfolio.entities) == len(portfolio.entities) + 1
    new_task = result.portfolio.entities[-1]
    assert new_task.entity_type is EntityType.TASK
    assert new_task.title == "Leaf"
    assert result.created_entity_ids == (new_task.id,)

    # It BELONGS_TO the deepest anchor.
    new_relation = result.portfolio.relations[-1]
    assert new_relation.source_id == new_task.id
    assert new_relation.target_id == deepest.id
    assert new_relation.relation_type is RelationType.BELONGS_TO

    # The source portfolio was untouched.
    assert portfolio.model_dump() == before_entities


# --- V1.3-B6: end-to-end canonical evidence ------------------------------------


def test_B6__validation_acceptance_then_reconstruction_compose() -> None:
    project = _entity(EntityType.PROJECT, "Platform")
    anchor = _entity(EntityType.DELIVERABLE, "Spec")

    portfolio = Portfolio(
        name="V1.3-B",
        entities=[project, anchor],
        relations=[_belongs_to(anchor, project)],
    )

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=anchor.id,
        children=[
            _node(
                EntityType.WORK_PACKAGE,
                "Implementation",
                children=[
                    _node(EntityType.TASK, "A"),
                    _node(EntityType.TASK, "B"),
                ],
            )
        ],
    )

    # Append-only + source order preserved (V1.3-B contract check).
    before_entities = [entity.id for entity in portfolio.entities]
    before_relations = [relation.id for relation in portfolio.relations]

    result = accept_work_breakdown_proposal(portfolio, proposal)

    assert [entity.id for entity in result.portfolio.entities[:2]] == before_entities
    assert [
        relation.id for relation in result.portfolio.relations[:1]
    ] == before_relations
    assert [
        entity.id for entity in result.portfolio.entities
    ] == [project.id, anchor.id, *result.created_entity_ids]
    assert [
        relation.id for relation in result.portfolio.relations
    ] == [*before_relations, *result.created_relation_ids]

    # Composition: V1.1 reconstruction over the ACCEPTED portfolio must show
    # the exact proposed structure beneath the existing anchor.
    structure = build_work_breakdown(result.portfolio, project.id)
    root = structure.root

    assert root.entity_id == project.id
    assert [child.entity_id for child in root.children] == [anchor.id]

    wp_node = root.children[0]
    assert [child.entity_id for child in wp_node.children] == [
        result.created_entity_ids[0],
    ]
    # A single WORK_PACKAGE "Implementation" owns BOTH tasks as children.
    implementation = wp_node.children[0]
    assert [child.entity_id for child in implementation.children] == [
        result.created_entity_ids[1],
        result.created_entity_ids[2],
    ]
    assert all(not child.children for child in implementation.children)

    by_id = {entity.id: entity for entity in result.portfolio.entities}
    assert by_id[anchor.id].title == "Spec"
    assert by_id[anchor.id].entity_type is EntityType.DELIVERABLE
    assert by_id[result.created_entity_ids[0]].title == "Implementation"
    assert by_id[result.created_entity_ids[0]].entity_type is EntityType.WORK_PACKAGE
    assert by_id[result.created_entity_ids[1]].title == "A"
    assert by_id[result.created_entity_ids[1]].entity_type is EntityType.TASK
    assert by_id[result.created_entity_ids[2]].title == "B"
    assert by_id[result.created_entity_ids[2]].entity_type is EntityType.TASK


# --- V1.3-B7: double acceptance on the same unchanged input --------------------


def test_B7__double_acceptance_same_input__both_succeed_independently() -> None:
    portfolio, project = _project_portfolio()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=[
            _node(
                EntityType.WORK_PACKAGE,
                "WP",
                children=[_node(EntityType.TASK, "T")],
            )
        ],
    )

    before = portfolio.model_dump()
    proposal_before = proposal.model_dump()

    first = accept_work_breakdown_proposal(portfolio, proposal)
    second = accept_work_breakdown_proposal(portfolio, proposal)

    # Both calls succeed independently on the same unchanged input.
    assert first.created_entity_ids and second.created_entity_ids
    assert first.created_entity_ids != second.created_entity_ids
    assert first.created_relation_ids != second.created_relation_ids

    # Not idempotent: every run materializes fresh, distinct identities.
    assert set(first.created_entity_ids).isdisjoint(second.created_entity_ids)
    assert all(entity_id != project.id for entity_id in first.created_entity_ids)
    assert all(entity_id != project.id for entity_id in second.created_entity_ids)

    # Structurally equivalent: same titles/types/order in both results, and
    # equivalent semantic parentage over ALL relations while the fresh UUID
    # identities differ.
    assert _shape(first) == _shape(second)
    assert _attachment_shape(first) == _attachment_shape(second)
    assert _shape(first) == [
        (EntityType.PROJECT.name, "Platform"),
        (EntityType.WORK_PACKAGE.name, "WP"),
        (EntityType.TASK.name, "T"),
    ]
    assert _attachment_shape(first) == [
        ((EntityType.WORK_PACKAGE.name, "WP"), (EntityType.PROJECT.name, "Platform")),
        ((EntityType.TASK.name, "T"), (EntityType.WORK_PACKAGE.name, "WP")),
    ]

    # Neither call mutated the source portfolio or the proposal.
    assert portfolio.model_dump() == before
    assert [entity.id for entity in portfolio.entities] == [project.id]
    assert portfolio.relations == []
    assert proposal.model_dump() == proposal_before


# --- V1.3-B8: empty / no-op proposal --------------------------------------------


def test_B8__empty_proposal__accepted_as_no_op() -> None:
    # Under the V1.2 contract an empty children tuple is valid: the grammar
    # check is a no-op and every anchor of the project WBS admits it, so V1.3
    # simply materializes nothing. No new rejection policy is introduced.
    portfolio, project = _project_portfolio()
    before_entities = [
        (entity.id, entity.entity_type, entity.title) for entity in portfolio.entities
    ]

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=(),
    )

    validated = validate_work_breakdown_proposal(portfolio, proposal)
    assert validated.children == ()

    result = accept_work_breakdown_proposal(portfolio, proposal)

    # Fresh portfolio, but with the same canonical values and order.
    assert result.portfolio is not portfolio
    assert result.portfolio.name == portfolio.name
    assert [
        (entity.id, entity.entity_type, entity.title)
        for entity in result.portfolio.entities
    ] == before_entities
    assert not result.portfolio.relations

    # No aliases to source objects.
    assert all(entity is not project for entity in result.portfolio.entities)

    # The id traces are exactly empty.
    assert result.created_entity_ids == ()
    assert result.created_relation_ids == ()
