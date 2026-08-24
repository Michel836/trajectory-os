"""Deterministic work-breakdown projection with hardened invariants (V1.1-B).

Projects a ``Portfolio`` into an immutable work-breakdown tree rooted at a
``PROJECT`` using only ``BELONGS_TO`` containment edges whose direction is
``source_id`` = child and ``target_id`` = parent.

V1.1-B hardens the V1.1-A happy-path projection:

* Duplicate semantic ``BELONGS_TO`` edges (same child, same parent) collapse
  to a single link. Duplicate-edge handling is **not** deferred.
* A reachable WBS child (``DELIVERABLE``, ``WORK_PACKAGE``, ``TASK``) whose
  containment pair with its parent is not allowed raises
  :class:`WorkBreakdownError`.
* An included WBS entity with more than one distinct valid WBS parent
  raises :class:`WorkBreakdownError` (duplicate edges to the *same* parent
  do not count).
* Containment cycles reachable from the selected root are detected
  explicitly — before they could become unbounded recursion — and raise
  :class:`WorkBreakdownError`.

Non-WBS ``BELONGS_TO`` children (``IDEA``, ``RESOURCE``, ``RESEARCH``, ...)
are still excluded from the work breakdown, not rejected. Validation is
scoped to reachability from the selected root: ambiguity and invalid pairs
entirely unreachable from it do not affect the projection.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trajectory_os.domain.entities import EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType


class WorkBreakdownError(ValueError):
    """Raised when a work-breakdown projection violates its invariants."""


class WorkBreakdownNode(BaseModel):
    """Immutable node of a projected work breakdown."""

    model_config = ConfigDict(frozen=True)

    entity_id: UUID
    children: tuple[WorkBreakdownNode, ...] = ()


class WorkBreakdownStructure(BaseModel):
    """Immutable work-breakdown tree rooted at a project node."""

    model_config = ConfigDict(frozen=True)

    root: WorkBreakdownNode


_ELIGIBLE_DESCENDANTS: Final[frozenset[EntityType]] = frozenset(
    {
        EntityType.DELIVERABLE,
        EntityType.WORK_PACKAGE,
        EntityType.TASK,
    },
)

_ALLOWED_CONTAINMENT: Final[frozenset[tuple[EntityType, EntityType]]] = (
    frozenset(
        {
            (EntityType.PROJECT, EntityType.DELIVERABLE),
            (EntityType.PROJECT, EntityType.WORK_PACKAGE),
            (EntityType.PROJECT, EntityType.TASK),
            (EntityType.DELIVERABLE, EntityType.WORK_PACKAGE),
            (EntityType.DELIVERABLE, EntityType.TASK),
            (EntityType.WORK_PACKAGE, EntityType.WORK_PACKAGE),
            (EntityType.WORK_PACKAGE, EntityType.TASK),
        },
    )
)


def is_work_breakdown_containment_allowed(
    parent_type: EntityType,
    child_type: EntityType,
) -> bool:
    """Report whether ``parent_type`` may directly contain ``child_type`` in a WBS.

    This is the single public source of the WBS containment grammar and
    the only predicate consumers outside this module should use to decide
    containment validity.
    """

    return (parent_type, child_type) in _ALLOWED_CONTAINMENT


def _children_by_parent(portfolio: Portfolio) -> dict[UUID, set[UUID]]:
    """Map each ``BELONGS_TO`` parent id to the set of its child ids.

    Only ``BELONGS_TO`` relations participate, with ``source_id`` as child
    and ``target_id`` as parent. All other relation types are ignored.
    ``set`` semantics intentionally collapse duplicate semantic edges
    (same child, same parent) into a single link.
    """

    children: dict[UUID, set[UUID]] = {}

    for relation in portfolio.relations:
        if relation.relation_type is not RelationType.BELONGS_TO:
            continue

        children.setdefault(relation.target_id, set()).add(relation.source_id)

    return children


def _valid_wbs_parents(
    portfolio: Portfolio,
    entities: dict[UUID, TrajectoryEntity],
) -> dict[UUID, set[UUID]]:
    """Map each WBS entity id to its distinct valid WBS parent ids.

    A valid WBS parent is a relation whose ``(parent, child)`` types form
    an allowed containment pair. This spans the *entire* portfolio: a
    second valid parent outside the selected root still makes an included
    child ambiguous. Duplicate edges to the same parent collapse via ``set``.
    """

    valid: dict[UUID, set[UUID]] = {}

    for relation in portfolio.relations:
        if relation.relation_type is not RelationType.BELONGS_TO:
            continue

        child = entities.get(relation.source_id)
        parent = entities.get(relation.target_id)

        if child is None or parent is None:
            continue

        if not is_work_breakdown_containment_allowed(parent.entity_type, child.entity_type):
            continue

        valid.setdefault(relation.source_id, set()).add(relation.target_id)

    return valid


def _discover_from_root(
    root_id: UUID,
    entities: dict[UUID, TrajectoryEntity],
    children_of: dict[UUID, list[UUID]],
) -> tuple[set[UUID], dict[UUID, UUID]]:
    """Deterministically discover the WBS subtree reachable from ``root_id``.

    Returns the included entity ids and, for every non-root included entity,
    the id of the included parent it is discovered under. Raises
    :class:`WorkBreakdownError` for a reachable invalid WBS containment
    pair and for containment cycles detected explicitly (a child already on
    the current ancestor chain), so a cycle is reported before recursion
    could run away.

    Traversal is iterative and single-pass; sibling order is irrelevant
    here because it is re-established from ``Portfolio.entities`` order in
    the final projection.
    """

    included: set[UUID] = {root_id}
    parent_of: dict[UUID, UUID] = {}
    ancestors: set[UUID] = {root_id}

    frames: list[tuple[UUID, Iterator[UUID]]] = [
        (root_id, iter(children_of.get(root_id, ())))
    ]

    while frames:
        node_id, children = frames[-1]
        child_id = next(children, None)

        if child_id is None:
            frames.pop()
            ancestors.discard(node_id)
            continue

        if child_id in ancestors:
            raise WorkBreakdownError(
                "work-breakdown containment cycle detected at entity "
                f"{child_id}"
            )

        child_type = entities[child_id].entity_type

        if child_type not in _ELIGIBLE_DESCENDANTS:
            # Non-WBS types (IDEA, RESOURCE, ...) stay out of the WBS.
            continue

        node_type = entities[node_id].entity_type

        if not is_work_breakdown_containment_allowed(node_type, child_type):
            raise WorkBreakdownError(
                "invalid WBS containment: "
                f"{node_type.value} may not contain {child_type.value} "
                f"(entity {child_id})"
            )

        if child_id in included:
            # Reaching an included entity through a second parent is
            # reported by the multiple-parent check, not duplicated here.
            continue

        included.add(child_id)
        parent_of[child_id] = node_id
        ancestors.add(child_id)
        frames.append((child_id, iter(children_of.get(child_id, ()))))

    return included, parent_of


def build_work_breakdown(
    portfolio: Portfolio,
    root_id: UUID,
) -> WorkBreakdownStructure:
    """Project ``portfolio`` into an immutable work breakdown rooted at ``root_id``.

    Only ``BELONGS_TO`` containment edges whose ``(parent, child)`` types
    form an allowed containment pair are projected. Sibling order follows
    ``Portfolio.entities`` order, not relation storage order. Entities that
    are not reachable from the root through WBS containment edges are
    excluded. The input portfolio is never mutated, and each call produces
    a fresh, equivalent tree for the same input.

    Projects that violate the invariants — unknown root, non-``PROJECT``
    root, reachable invalid WBS containment, multiple distinct WBS parents
    for an included entity, or a containment cycle reachable from the root —
    raise :class:`WorkBreakdownError`.
    """

    root_entity = portfolio.get_entity(root_id)

    if root_entity is None:
        raise WorkBreakdownError(f"unknown entity in portfolio: {root_id}")

    if root_entity.entity_type is not EntityType.PROJECT:
        raise WorkBreakdownError(
            "work breakdown root must be a PROJECT, "
            f"got {root_entity.entity_type.value}",
        )

    entities = {entity.id: entity for entity in portfolio.entities}
    entity_order = {
        entity.id: index for index, entity in enumerate(portfolio.entities)
    }
    membership = _children_by_parent(portfolio)
    children_of = {
        parent_id: sorted(child_ids, key=entity_order.__getitem__)
        for parent_id, child_ids in membership.items()
    }

    included, parent_of = _discover_from_root(root_id, entities, children_of)

    # Multiple distinct *valid* WBS parents make an included child
    # ambiguous, even if the second parent lies outside the selected root.
    # Runs after traversal: any cycle reachable from the selected root was
    # already raised during traversal, so reachable cycles are reported
    # before multi-parent ambiguity.
    valid_parents = _valid_wbs_parents(portfolio, entities)
    for child_id in included:
        parents = valid_parents.get(child_id)
        if parents and len(parents) > 1:
            raise WorkBreakdownError(
                f"entity {child_id} has multiple WBS parents: "
                + ", ".join(str(parent) for parent in sorted(parents))
            )

    # Validation guarantees a single-parent, acyclic reachable subtree,
    # so assembly is a plain tree walk in deterministic entity order.
    children_list: dict[UUID, list[UUID]] = {}
    for child_id, parent_id in parent_of.items():
        children_list.setdefault(parent_id, []).append(child_id)

    nodes: dict[UUID, WorkBreakdownNode] = {}
    pre_order: list[UUID] = [root_id]
    for node_id in pre_order:
        pre_order.extend(children_list.get(node_id, ()))

    for node_id in reversed(pre_order):
        child_ids = children_list.get(node_id, ())
        nodes[node_id] = WorkBreakdownNode(
            entity_id=node_id,
            children=tuple(nodes[child_id] for child_id in child_ids),
        )

    return WorkBreakdownStructure(root=nodes[root_id])
