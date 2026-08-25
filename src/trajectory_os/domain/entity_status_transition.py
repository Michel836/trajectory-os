"""Entity status transition (V1.7-A: pure deterministic state mutation).

Transitions the status of exactly one :class:`TrajectoryEntity` inside a
:class:`Portfolio`. This is a pure function: it never mutates the source
portfolio, its entities, or its relations, and it returns a fresh,
independent :class:`Portfolio` together with an immutable
:class:`EntityStatusTransitionResult`.

The public boundary is deliberately strict:

* explicit :func:`isinstance` guards reject arguments that are not the
  canonical domain types before any other work happens;
* no wall-clock access: the transition timestamp is always the
  caller-supplied ``changed_at``;
* any two distinct :class:`EntityStatus` values are allowed; only
  same-status transitions and stale timestamps are rejected;
* the fresh canonical state is revalidated from a Python data
  representation of the portfolio through the existing
  :class:`Portfolio` model before it is returned, so nested entities
  and relations are constructed fresh and their fields revalidated.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from trajectory_os.domain.entities import EntityStatus
from trajectory_os.domain.portfolio import Portfolio


class EntityStatusTransitionError(ValueError):
    """Raised when an entity status transition is invalid."""


class UnknownEntityError(EntityStatusTransitionError):
    """Raised when the target entity does not exist in the portfolio."""


class SameStatusTransitionError(EntityStatusTransitionError):
    """Raised when the target status equals the entity's current status."""


class StaleChangedAtError(EntityStatusTransitionError):
    """Raised when ``changed_at`` precedes the entity's ``updated_at``."""


class EntityStatusTransitionResult(BaseModel):
    """Immutable record of a successful entity status transition.

    ``portfolio`` is the fresh transitioned portfolio; the remaining
    fields describe exactly what changed and when.
    """

    model_config = ConfigDict(frozen=True)

    portfolio: Portfolio
    entity_id: UUID
    previous_status: EntityStatus
    new_status: EntityStatus
    changed_at: datetime


def transition_entity_status(
    portfolio: Portfolio,
    entity_id: UUID,
    target_status: EntityStatus,
    changed_at: datetime,
) -> EntityStatusTransitionResult:
    """Return a fresh portfolio with one entity's status transitioned.

    The target entity ``entity_id`` must exist in ``portfolio``, its
    current status must differ from ``target_status``, and the
    timezone-aware ``changed_at`` must not precede the entity's current
    ``updated_at`` (equality is allowed). On success exactly two fields
    of the target entity change: ``status`` and ``updated_at``.

    The source portfolio is never mutated and the returned portfolio is
    a fresh, deeply independent object rebuilt by revalidating the
    transitioned data representation through the :class:`Portfolio`
    model.

    Raises :class:`EntityStatusTransitionError` (or a narrow subclass)
    when the arguments are not the canonical domain types, when the
    entity is unknown, when the status does not change, or when
    ``changed_at`` is stale.
    """

    if not isinstance(portfolio, Portfolio):
        raise EntityStatusTransitionError(
            "portfolio must be a Portfolio instance, "
            f"got {type(portfolio).__name__}"
        )

    if not isinstance(entity_id, UUID):
        raise EntityStatusTransitionError(
            "entity_id must be a UUID instance, "
            f"got {type(entity_id).__name__}"
        )

    if not isinstance(target_status, EntityStatus):
        raise EntityStatusTransitionError(
            "target_status must be an EntityStatus instance, "
            f"got {type(target_status).__name__}"
        )

    if not isinstance(changed_at, datetime):
        raise EntityStatusTransitionError(
            "changed_at must be a datetime instance, "
            f"got {type(changed_at).__name__}"
        )

    if changed_at.tzinfo is None or changed_at.utcoffset() is None:
        raise EntityStatusTransitionError(
            "changed_at must be timezone-aware "
            f"(got {changed_at!r})"
        )

    entity = portfolio.get_entity(entity_id)

    if entity is None:
        raise UnknownEntityError(f"unknown entity in portfolio: {entity_id}")

    if entity.status == target_status:
        raise SameStatusTransitionError(
            f"entity {entity_id} is already in status "
            f"{target_status.value!r}; same-status transitions are rejected"
        )

    entity_updated_at = entity.updated_at
    if entity_updated_at.tzinfo is None or entity_updated_at.utcoffset() is None:
        raise EntityStatusTransitionError(
            f"entity {entity_id} has a naive updated_at {entity_updated_at!r}; "
            "timezone-aware timestamps are required"
        )

    if changed_at < entity_updated_at:
        raise StaleChangedAtError(
            f"changed_at {changed_at.isoformat()} precedes the entity's "
            f"updated_at {entity_updated_at.isoformat()}"
        )

    data = portfolio.model_dump()

    target_data = next(
        entity_data
        for entity_data in data["entities"]
        if entity_data["id"] == entity_id
    )
    target_data["status"] = target_status
    target_data["updated_at"] = changed_at

    try:
        transitioned = Portfolio.model_validate(data)
    except ValidationError as exc:
        raise EntityStatusTransitionError(
            "entity status transition failed while revalidating "
            "the transitioned portfolio"
        ) from exc

    return EntityStatusTransitionResult(
        portfolio=transitioned,
        entity_id=entity_id,
        previous_status=entity.status,
        new_status=target_status,
        changed_at=changed_at,
    )
