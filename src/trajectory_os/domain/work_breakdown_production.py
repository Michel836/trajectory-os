"""Deterministic production orchestration for work-breakdown proposals (V1.4-A).

This module bridges the existing domain pieces:

* :func:`~trajectory_os.domain.work_breakdown.build_work_breakdown` (V1.1)
  supplies the canonical work breakdown of a project;
* an untrusted producer behind the
  :class:`WorkBreakdownProposalProducer` protocol receives only a fresh,
  frozen snapshot of that work breakdown (scalars and frozen context items,
  never ``Portfolio``/``TrajectoryEntity``/``TrajectoryRelation`` instances)
  and returns a :class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposal`;
* :func:`~trajectory_os.domain.work_breakdown_proposals.validate_work_breakdown_proposal`
  (V1.2) revalidates the producer output before it is accepted.

Guarantees:

* explicit input guards: ``portfolio`` must be a ``Portfolio`` and
  ``project_id``/``anchor_id`` must be ``UUID`` values; the producer itself
  is *not* isinstance-checked (it is a protocol boundary);
* ``build_work_breakdown`` runs first; only its
  :class:`~trajectory_os.domain.work_breakdown.WorkBreakdownError` is
  translated (with cause) into
  :class:`WorkBreakdownProposalProductionError` — before the producer is
  ever called;
* the V1.1 tree is flattened *iteratively* into the entire project WBS,
  project root first (``parent_id=None``), in exact pre-order with exact
  parent ids, resolving titles/descriptions through a single
  ``entities_by_id`` dictionary; no independent ``Portfolio`` relation
  traversal happens here;
* the anchor must occur in that flattened WBS before the producer call;
* the producer receives only the frozen snapshot request and only
  :class:`WorkBreakdownProposal` outputs are accepted; provider exceptions
  propagate unchanged;
* outputs are never trusted: they revalidate through V1.2, only
  :class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposalError`
  is translated (with cause), and the validated project/anchor ids are
  compared against the *original* function arguments so a producer cannot
  redirect the proposal to another project or anchor;
* the returned :class:`WorkBreakdownProposal` is a fresh instance
  reconstructed solely from the validated state.

No proposal acceptance (V1.3), no provider-specific behavior, no network,
no persistence, and no new dependencies are introduced.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trajectory_os.domain.entities import EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.work_breakdown import (
    WorkBreakdownError,
    WorkBreakdownNode,
    WorkBreakdownStructure,
    build_work_breakdown,
)
from trajectory_os.domain.work_breakdown_proposals import (
    WorkBreakdownProposal,
    WorkBreakdownProposalError,
    validate_work_breakdown_proposal,
)


class WorkBreakdownProposalContextItem(BaseModel):
    """Frozen scalar snapshot of one canonical WBS node for a producer.

    Carries no ``Portfolio``, relation, or other canonical object
    references: only the entity id, its WBS parent id (``None`` for the
    project root), type, title, and optional description.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: UUID
    parent_id: UUID | None
    entity_type: EntityType
    title: str
    description: str | None = None


class WorkBreakdownProposalRequest(BaseModel):
    """Frozen, proposal-only request handed to a producer.

    ``existing_work`` is the complete pre-order flattening of the project's
    canonical WBS (project root first).
    """

    model_config = ConfigDict(frozen=True)

    project_id: UUID
    anchor_id: UUID
    existing_work: tuple[WorkBreakdownProposalContextItem, ...]


class WorkBreakdownProposalProducer(Protocol):
    """Provider-agnostic, proposal-only work-breakdown production boundary."""

    def propose(
        self,
        request: WorkBreakdownProposalRequest,
    ) -> WorkBreakdownProposal:
        """Return a proposal for ``request`` without touching canonical state."""
        ...


class WorkBreakdownProposalProductionError(ValueError):
    """Raised when work-breakdown proposal production fails at this boundary."""


def _flatten_wbs(
    structure: WorkBreakdownStructure,
    entities_by_id: dict[UUID, TrajectoryEntity],
) -> tuple[tuple[WorkBreakdownProposalContextItem, ...], set[UUID]]:
    """Iteratively flatten the V1.1 tree in exact pre-order.

    The project root comes first with ``parent_id=None``. Titles and
    descriptions are resolved through the single ``entities_by_id``
    dictionary; no ``Portfolio`` relations are traversed here.
    """

    items: list[WorkBreakdownProposalContextItem] = []
    included: set[UUID] = set()
    stack: list[tuple[WorkBreakdownNode, UUID | None]] = [(structure.root, None)]

    while stack:
        node, parent_id = stack.pop()
        entity = entities_by_id[node.entity_id]
        items.append(
            WorkBreakdownProposalContextItem(
                entity_id=node.entity_id,
                parent_id=parent_id,
                entity_type=entity.entity_type,
                title=entity.title,
                description=entity.description,
            )
        )
        included.add(node.entity_id)
        for child in reversed(node.children):
            stack.append((child, node.entity_id))

    return tuple(items), included


def propose_work_breakdown(
    portfolio: Portfolio,
    project_id: UUID,
    anchor_id: UUID,
    producer: WorkBreakdownProposalProducer,
) -> WorkBreakdownProposal:
    """Produce and validate a work-breakdown proposal for ``project_id``.

    The V1.1 work breakdown of ``project_id`` is computed first and
    flattened into a frozen snapshot; the anchor must occur in that WBS
    before the producer is invoked. The producer receives only the frozen
    snapshot request and must return a
    :class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposal`.
    Its output is then revalidated through the V1.2 public domain boundary
    against the same portfolio, and its project/anchor ids are compared
    against the original function arguments — a redirected proposal is
    rejected.

    A fresh frozen :class:`WorkBreakdownProposal` reconstructed only from
    the validated state is returned. The portfolio and the producer's
    output instance are never mutated.

    Raises :class:`WorkBreakdownProposalProductionError` for non-conforming
    inputs, an anchor outside the project's WBS, a non-proposal producer
    return value, a producer output that fails V1.2 validation, or a
    producer output redirected to a different project or anchor. Producer
    (provider) exceptions propagate unchanged.
    """

    if not isinstance(portfolio, Portfolio):
        raise WorkBreakdownProposalProductionError(
            "portfolio must be a Portfolio instance, "
            f"got {type(portfolio).__name__}"
        )

    if not isinstance(project_id, UUID):
        raise WorkBreakdownProposalProductionError(
            "project_id must be a UUID, got "
            f"{type(project_id).__name__} ({project_id!r})"
        )

    if not isinstance(anchor_id, UUID):
        raise WorkBreakdownProposalProductionError(
            "anchor_id must be a UUID, got "
            f"{type(anchor_id).__name__} ({anchor_id!r})"
        )

    try:
        structure = build_work_breakdown(portfolio, project_id)
    except WorkBreakdownError as exc:
        raise WorkBreakdownProposalProductionError(
            "work-breakdown projection failed before producer invocation"
        ) from exc

    entities_by_id = {entity.id: entity for entity in portfolio.entities}
    existing_work, included_ids = _flatten_wbs(structure, entities_by_id)

    if anchor_id not in included_ids:
        raise WorkBreakdownProposalProductionError(
            f"anchor {anchor_id} is not part of the work breakdown "
            f"of project {project_id}"
        )

    request = WorkBreakdownProposalRequest(
        project_id=project_id,
        anchor_id=anchor_id,
        existing_work=existing_work,
    )

    proposal = producer.propose(request)

    if not isinstance(proposal, WorkBreakdownProposal):
        raise WorkBreakdownProposalProductionError(
            "producer must return a WorkBreakdownProposal instance, "
            f"got {type(proposal).__name__}"
        )

    try:
        validated = validate_work_breakdown_proposal(portfolio, proposal)
    except WorkBreakdownProposalError as exc:
        raise WorkBreakdownProposalProductionError(
            "producer output failed V1.2 validation"
        ) from exc

    if (
        validated.project_id != project_id
        or validated.anchor_id != anchor_id
    ):
        raise WorkBreakdownProposalProductionError(
            "producer output was redirected: project_id/anchor_id "
            "differ from the original request arguments"
        )

    return WorkBreakdownProposal.model_validate(validated.model_dump())
