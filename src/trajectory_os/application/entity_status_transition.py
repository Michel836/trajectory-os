"""Durable entity status transition (V1.7-B persistence boundary).

Turns an explicit human status decision into durable storage through
the minimal, provider-agnostic ``PortfolioRepository`` boundary
introduced in V1.6:

1. load the CURRENT persisted portfolio;
2. hand the freshly loaded portfolio to the V1.7-A pure domain use
   case ``transition_entity_status``;
3. save exactly the fresh transitioned portfolio produced by that
   result;
4. return the exact V1.7-A ``EntityStatusTransitionResult`` and stop.

Strict boundary rules:

* ``portfolio_id`` must already be a ``UUID`` instance; anything else
  is rejected before any repository interaction;
* a missing portfolio raises :class:`StatusTransitionPortfolioNotFoundError`
  before any transition work;
* every entity, status, and timestamp rule remains authoritative in
  V1.7-A; nothing here duplicates domain validation;
* ``repository.save`` is called at most once, and only after a
  successful V1.7-A transition, with exactly ``result.portfolio``;
* the V1.7-A ``EntityStatusTransitionError`` propagates unchanged;
* repository load/save exceptions propagate unchanged; there are no
  broad exception catches here;
* the loaded portfolio is never mutated;
* there are no wall-clock reads, no automatic status propagation, and
  no concurrency, transaction, or versioning claims.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from trajectory_os.application.work_breakdown_acceptance import (
    PortfolioRepository,
)
from trajectory_os.domain.entities import EntityStatus
from trajectory_os.domain.entity_status_transition import (
    EntityStatusTransitionResult,
    transition_entity_status,
)


class DurableEntityStatusTransitionError(ValueError):
    """Raised when the durable entity status transition fails at this boundary."""


class StatusTransitionPortfolioNotFoundError(
    DurableEntityStatusTransitionError
):
    """Raised when the portfolio to transition does not exist."""


def transition_entity_status_durably(
    portfolio_id: UUID,
    entity_id: UUID,
    target_status: EntityStatus,
    changed_at: datetime,
    repository: PortfolioRepository,
) -> EntityStatusTransitionResult:
    """Transition one entity's status and persist the transitioned portfolio.

    Sequence is exactly: reject ``portfolio_id`` unless it is already
    a ``UUID`` instance, load the current portfolio, delegate the
    transition to the V1.7-A ``transition_entity_status`` use case,
    save ``result.portfolio`` only after a successful transition, and
    return the exact V1.7-A result object.

    The loaded portfolio is never mutated. A missing portfolio raises
    :class:`StatusTransitionPortfolioNotFoundError`. The V1.7-A
    ``EntityStatusTransitionError`` and any repository load/save
    exception propagate unchanged.
    """

    if not isinstance(portfolio_id, UUID):
        raise DurableEntityStatusTransitionError(
            "portfolio_id must already be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )

    current = repository.load(portfolio_id)
    if current is None:
        raise StatusTransitionPortfolioNotFoundError(
            f"portfolio not found: {portfolio_id}"
        )

    result = transition_entity_status(current, entity_id, target_status, changed_at)

    repository.save(result.portfolio)

    return result
