"""Work-breakdown proposal acceptance (V1.3-A: materialization boundary).

A :class:`WorkBreakdownProposal` that has been judged acceptable by some
caller is materialized here into canonical portfolio state. Acceptance is
a pure function: it never mutates the source :class:`Portfolio` or the
input :class:`WorkBreakdownProposal`, and it returns a fresh, independent
:class:`Portfolio` together with the ordered ids of everything that was
created.

The public boundary is deliberately strict and layered:

* an explicit :func:`isinstance` guard rejects arguments that are not
  exactly the canonical domain types before any other work happens;
* the V1.2 boundary
  :func:`~trajectory_os.domain.work_breakdown_proposals.validate_work_breakdown_proposal`
  remains authoritative for every domain rule (project, anchor, WBS
  grammar). Any :class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposalError`
  is translated into :class:`WorkBreakdownAcceptanceError`, preserving
  the original exception as the cause, so the public acceptance boundary
  only ever raises its own error type;
* materialization uses only the fresh frozen validated proposal. The
  caller's proposal instance is never read for data after validation;
* every proposed node becomes exactly one new
  :class:`~trajectory_os.domain.entities.TrajectoryEntity`
  (``INCUBATOR`` / ``USER_CONFIRMED`` / confidence ``1.0``; the node's
  proposed ``confidence`` is not copied) and exactly one new
  :class:`~trajectory_os.domain.relations.TrajectoryRelation`
  (``BELONGS_TO`` / ``USER_CONFIRMED`` / confidence ``1.0``) whose target
  is the materialized entity's canonical parent or the existing anchor;
* traversal is iterative pre-order over an explicit stack: a parent is
  always materialized before any of its descendants, sibling order is
  preserved exactly, there is no recursion, no title matching, no
  deduplication, and no temporary proposal ids.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from trajectory_os.domain.entities import (
    EntityStatus,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown_proposals import (
    ProposedWorkNode,
    WorkBreakdownProposal,
    WorkBreakdownProposalError,
    validate_work_breakdown_proposal,
)


class WorkBreakdownAcceptanceError(ValueError):
    """Raised when a work-breakdown proposal cannot be accepted."""


class WorkBreakdownAcceptanceResult(BaseModel):
    """Immutable record of an accepted work-breakdown proposal.

    ``portfolio`` is the fresh accepted portfolio; the two id tuples are
    the ordered identifiers of the newly created canonical entities and
    relations, in exact materialization (pre-order) order.
    """

    model_config = ConfigDict(frozen=True)

    portfolio: Portfolio
    created_entity_ids: tuple[UUID, ...]
    created_relation_ids: tuple[UUID, ...]


def _materialize_pre_order(
    children: tuple[ProposedWorkNode, ...],
    anchor_id: UUID,
) -> tuple[list[TrajectoryEntity], list[TrajectoryRelation]]:
    """Materialize proposed nodes iteratively in pre-order.

    Parents are always created before their descendants and sibling order
    is preserved exactly. Every proposed node yields exactly one entity
    and exactly one ``BELONGS_TO`` relation, in that creation order.
    """

    created_entities: list[TrajectoryEntity] = []
    created_relations: list[TrajectoryRelation] = []

    stack: list[tuple[ProposedWorkNode, UUID]] = [
        (child, anchor_id) for child in reversed(children)
    ]

    while stack:
        node, parent_uuid = stack.pop()

        entity = TrajectoryEntity(
            entity_type=node.entity_type,
            title=node.title,
            description=node.description,
            status=EntityStatus.INCUBATOR,
            source=SourceKind.USER_CONFIRMED,
            confidence=1.0,
        )

        relation = TrajectoryRelation(
            source_id=entity.id,
            target_id=parent_uuid,
            relation_type=RelationType.BELONGS_TO,
            source=SourceKind.USER_CONFIRMED,
            confidence=1.0,
        )

        created_entities.append(entity)
        created_relations.append(relation)

        stack.extend((child, entity.id) for child in reversed(node.children))

    return created_entities, created_relations


def accept_work_breakdown_proposal(
    portfolio: Portfolio,
    proposal: WorkBreakdownProposal,
) -> WorkBreakdownAcceptanceResult:
    """Accept a work-breakdown proposal into a fresh canonical portfolio.

    Validation remains fully delegated to the V1.2 boundary
    ``validate_work_breakdown_proposal``; any
    :class:`WorkBreakdownProposalError` it raises is translated into
    :class:`WorkBreakdownAcceptanceError` with the original exception as
    the cause. Only the fresh validated proposal is materialized:
    iterative pre-order over the validated children, one new entity and
    one new ``BELONGS_TO`` relation per node, parents before descendants,
    sibling order preserved, no recursion, no title matching, no
    deduplication.

    The source portfolio and input proposal are never mutated and the
    returned result portfolio is a fresh object whose entity and relation
    members are all fresh instances as well.

    Raises :class:`WorkBreakdownAcceptanceError` when the arguments are
    not the canonical domain types, when V1.2 validation fails, or when
    the accepted portfolio fails canonical construction.
    """

    if not isinstance(portfolio, Portfolio):
        raise WorkBreakdownAcceptanceError(
            "portfolio must be a Portfolio instance, "
            f"got {type(portfolio).__name__}"
        )

    if not isinstance(proposal, WorkBreakdownProposal):
        raise WorkBreakdownAcceptanceError(
            "proposal must be a WorkBreakdownProposal instance, "
            f"got {type(proposal).__name__}"
        )

    try:
        validated = validate_work_breakdown_proposal(portfolio, proposal)
    except WorkBreakdownProposalError as exc:
        raise WorkBreakdownAcceptanceError(
            "work-breakdown acceptance failed during proposal validation"
        ) from exc

    base = portfolio.model_copy(deep=True)

    created_entities, created_relations = _materialize_pre_order(
        validated.children, validated.anchor_id
    )

    try:
        accepted_portfolio = Portfolio(
            id=base.id,
            name=base.name,
            entities=[*base.entities, *created_entities],
            relations=[*base.relations, *created_relations],
        )
    except ValidationError as exc:
        raise WorkBreakdownAcceptanceError(
            "work-breakdown acceptance failed while constructing "
            "accepted portfolio"
        ) from exc

    return WorkBreakdownAcceptanceResult(
        portfolio=accepted_portfolio,
        created_entity_ids=tuple(entity.id for entity in created_entities),
        created_relation_ids=tuple(relation.id for relation in created_relations),
    )
