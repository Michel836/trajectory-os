"""Validated work-breakdown proposals (V1.2-B: hostile-boundary hardening).

A :class:`WorkBreakdownProposal` is a purely provisional, proposal-only
structure: it carries **proposed** work nodes (type, title, optional
description, confidence) without canonical entity UUIDs and never creates
or mutates :class:`~trajectory_os.domain.entities.TrajectoryEntity`
instances.

V1.2-B hardens the V1.2-A boundary against hostile input:

* :class:`ProposedWorkNode` may only carry the WBS node types
  ``DELIVERABLE``, ``WORK_PACKAGE``, and ``TASK``; any other
  :class:`~trajectory_os.domain.entities.EntityType` fails normal model
  construction.
* ``confidence`` explicitly rejects ``bool`` and non-numeric values
  (following the defensive pattern from
  :mod:`trajectory_os.domain.classification`) and must stay within
  ``[0.0, 1.0]``.
* :func:`validate_work_breakdown_proposal` does **not** trust an
  already-created :class:`WorkBreakdownProposal` instance. It first
  revalidates the instance's complete current state into a fresh
  :class:`WorkBreakdownProposal`, and every subsequent domain check uses
  the revalidated copy. Objects produced or corrupted through
  ``model_construct(...)``, ``object.__setattr__(...)``, or malformed
  nested nodes are therefore rejected at this public domain boundary,
  without being silently repaired, as
  :class:`WorkBreakdownProposalError` preserving the original exception
  as its cause.
* Serializer failures from ``model_dump(...)``, which Pydantic reports
  as ``ValueError`` (e.g. ``Circular reference detected`` for
  pathologically deep or self-referencing proposed subtrees), and
  ``ValidationError`` from the strict ``model_validate(...)`` step
  never escape the public boundary: both are translated into
  :class:`WorkBreakdownProposalError`.

The domain semantics are unchanged from V1.2-A:

* the project must resolve to an existing ``PROJECT``;
* the V1.1 ``build_work_breakdown`` projection of that project decides
  which anchors exist (no independent ``BELONGS_TO`` traversal here);
* the anchor must be part of that returned work breakdown;
* anchor-to-child and proposed-to-proposed containment is decided by the
  single WBS grammar predicate
  :func:`~trajectory_os.domain.work_breakdown.is_work_breakdown_containment_allowed`.

The proposal child order is preserved exactly (including same-title
siblings; no deduplication or identity inference), the portfolio and the
input proposal are never mutated, and every call returns a fresh frozen
:class:`ValidatedWorkBreakdownProposal`.
"""

from __future__ import annotations

from typing import Annotated, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.work_breakdown import (
    WorkBreakdownError,
    WorkBreakdownNode,
    build_work_breakdown,
    is_work_breakdown_containment_allowed,
)


class WorkBreakdownProposalError(ValueError):
    """Raised when a work-breakdown proposal fails validation."""


class ProposedWorkNode(BaseModel):
    """Immutable, proposal-only node without a canonical entity identity.

    Only the WBS node types ``DELIVERABLE``, ``WORK_PACKAGE``, and
    ``TASK`` are admissible; normal model construction with any other
    :class:`~trajectory_os.domain.entities.EntityType` fails validation.
    """

    model_config = ConfigDict(frozen=True)

    _PROPOSED_ENTITY_TYPES: ClassVar[frozenset[EntityType]] = frozenset(
        {
            EntityType.DELIVERABLE,
            EntityType.WORK_PACKAGE,
            EntityType.TASK,
        }
    )

    entity_type: EntityType
    title: str = Field(min_length=1)
    description: str | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    children: tuple[ProposedWorkNode, ...] = ()

    @field_validator("entity_type", mode="before")
    @classmethod
    def _entity_type_must_be_a_wbs_node(
        cls,
        value: object,
    ) -> EntityType:
        # A proposal may only carry WBS node types. Reject every other
        # EntityType explicitly rather than relying on downstream logic.
        if not isinstance(value, EntityType):
            raise ValueError(
                "proposed entity_type must be a DELIVERABLE, WORK_PACKAGE, "
                f"or TASK EntityType; got {value!r}"
            )
        if value not in cls._PROPOSED_ENTITY_TYPES:
            raise ValueError(
                "proposed entity_type must be a DELIVERABLE, WORK_PACKAGE, "
                f"or TASK; got {value.value!r}"
            )
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_must_be_numeric(cls, value: object) -> object:
        # Explicitly reject bool and non-numeric values (e.g. strings)
        # rather than relying on silent Pydantic coercion.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be an int or float in [0, 1]")
        return value


class WorkBreakdownProposal(BaseModel):
    """Immutable proposed work breakdown attached to an existing anchor."""

    model_config = ConfigDict(frozen=True)

    project_id: UUID
    anchor_id: UUID
    children: tuple[ProposedWorkNode, ...]


class ValidatedWorkBreakdownProposal(BaseModel):
    """Immutable, validated projection of a :class:`WorkBreakdownProposal`."""

    model_config = ConfigDict(frozen=True)

    project_id: UUID
    anchor_id: UUID
    children: tuple[ProposedWorkNode, ...]


def _wbs_included_ids(node: WorkBreakdownNode) -> set[UUID]:
    """Collect all entity ids contained in a V1.1 work-breakdown tree.

    Traversal is iterative with an explicit stack, matching V1.1's
    deliberate avoidance of recursion: determining anchor membership in a
    deep but otherwise valid canonical WBS must not raise
    ``RecursionError``. Order is irrelevant because the result is a set.
    """

    ids: set[UUID] = set()
    stack: list[WorkBreakdownNode] = [node]
    while stack:
        current = stack.pop()
        ids.add(current.entity_id)
        stack.extend(current.children)
    return ids


def _validate_proposed_containment(
    parent_type: EntityType,
    children: tuple[ProposedWorkNode, ...],
) -> None:
    """Validate every proposed parent/child pair in input order.

    Uses only the single V1.1 WBS grammar predicate; raises
    :class:`WorkBreakdownProposalError` on the first disallowed pair.
    """

    for child in children:
        if not is_work_breakdown_containment_allowed(parent_type, child.entity_type):
            raise WorkBreakdownProposalError(
                "invalid proposed WBS containment: "
                f"{parent_type.value} may not contain {child.entity_type.value} "
                f"({child.title!r})"
            )
        _validate_proposed_containment(child.entity_type, child.children)


def _revalidate_proposal(proposal: WorkBreakdownProposal) -> WorkBreakdownProposal:
    """Revalidate a proposal's complete current state into a fresh instance.

    An already-created instance is not trusted: its current field state is
    serialized and revalidated with Pydantic *strict* semantics. Strict
    revalidation rejects malformed current state outright instead of
    allowing Pydantic coercion/repair, so ``model_construct`` bypass,
    ``object.__setattr__`` tampering, and malformed nested nodes all fail
    and are translated into :class:`WorkBreakdownProposalError` that
    preserves the original exception as its cause; nothing is silently
    repaired, coerced, or mutated in the caller's state.

    Two low-level failures are translated here so the public boundary
    only ever raises :class:`WorkBreakdownProposalError`:

    1. serializer failures from ``model_dump(...)``, which Pydantic
       reports as ``ValueError`` (e.g. ``Circular reference detected``
       for pathologically deep or self-referencing subtrees); and
    2. ``ValidationError`` from the strict ``model_validate(...)`` step.

    Both are raised with the original exception preserved as the cause.
    Arbitrarily deep proposed trees are deliberately *not* made valid;
    the contract is coherent failure semantics, not unlimited depth.
    """

    try:
        serialized = proposal.model_dump()
    except ValueError as exc:
        # A structurally valid but pathologically deep or self-
        # referencing subtree makes Pydantic's serializer raise
        # ValueError; translate it instead of leaking the serializer
        # exception across the public domain boundary.
        raise WorkBreakdownProposalError(
            "work-breakdown proposal state failed revalidation at the "
            "public domain boundary"
        ) from exc

    try:
        return WorkBreakdownProposal.model_validate(serialized, strict=True)
    except ValidationError as exc:
        raise WorkBreakdownProposalError(
            "work-breakdown proposal state failed revalidation at the "
            "public domain boundary"
        ) from exc


def validate_work_breakdown_proposal(
    portfolio: Portfolio,
    proposal: WorkBreakdownProposal,
) -> ValidatedWorkBreakdownProposal:
    """Validate a proposal against the portfolio's V1.1 work breakdown.

    The project id must resolve to an existing ``PROJECT``. The anchor id
    must be part of that project's V1.1 work breakdown (computed via
    ``build_work_breakdown``; no separate ``BELONGS_TO`` traversal). Every
    anchor-to-child and proposed-to-proposed containment pair must be
    allowed by the shared WBS grammar. Child order is preserved exactly,
    the portfolio and proposal are not mutated, and a fresh frozen
    :class:`ValidatedWorkBreakdownProposal` is returned.

    The caller's proposal instance is never trusted: before any domain
    check, its complete current state is revalidated into a fresh
    :class:`WorkBreakdownProposal`, and all subsequent validation uses
    that copy. Hostile instances (``model_construct`` bypass,
    ``object.__setattr__`` tampering, malformed nested nodes) fail this
    revalidation and are rejected without being silently repaired.

    Raises :class:`WorkBreakdownProposalError` for a
    :class:`WorkBreakdownProposal` argument that is not a
    :class:`WorkBreakdownProposal`, for a proposal whose current state
    fails schema revalidation, for a missing or non-``PROJECT`` project,
    for an anchor outside the selected project's work breakdown
    (including V1.1 invariants that ``build_work_breakdown`` rejects), or
    for a disallowed containment pair.
    """

    if not isinstance(proposal, WorkBreakdownProposal):
        raise WorkBreakdownProposalError(
            "proposal must be a WorkBreakdownProposal instance, "
            f"got {type(proposal).__name__}"
        )

    # Hostile boundary: do not trust the caller's instance. Revalidate its
    # complete current state and use only the fresh revalidated copy.
    revalidated = _revalidate_proposal(proposal)

    project = portfolio.get_entity(revalidated.project_id)

    if project is None:
        raise WorkBreakdownProposalError(
            f"unknown project in proposal: {revalidated.project_id}"
        )

    if project.entity_type is not EntityType.PROJECT:
        raise WorkBreakdownProposalError(
            "proposal project_id must resolve to a PROJECT, "
            f"got {project.entity_type.value}",
        )

    try:
        structure = build_work_breakdown(portfolio, project.id)
    except WorkBreakdownError as exc:
        raise WorkBreakdownProposalError(str(exc)) from exc

    if revalidated.anchor_id not in _wbs_included_ids(structure.root):
        raise WorkBreakdownProposalError(
            f"anchor {revalidated.anchor_id} is not part of the work breakdown "
            f"of project {project.id}"
        )

    # The anchor id came from the portfolio's own WBS, so it resolves to a
    # canonical entity by construction.
    anchor = portfolio.get_entity(revalidated.anchor_id)
    assert anchor is not None

    _validate_proposed_containment(anchor.entity_type, revalidated.children)

    # Rebuild from serialized data so the result is a fresh, fully
    # independent frozen instance for the same revalidated input.
    return ValidatedWorkBreakdownProposal.model_validate(revalidated.model_dump())
