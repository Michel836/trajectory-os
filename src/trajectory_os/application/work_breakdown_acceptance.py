"""Durable work-breakdown proposal acceptance (V1.6-A persistence boundary).

Turns an explicit human acceptance decision into durable storage through
the minimal, provider-agnostic ``PortfolioRepository`` boundary:

1. load the CURRENT persisted portfolio;
2. hand the freshly loaded portfolio and the caller's proposal to the
   V1.3 pure domain use case ``accept_work_breakdown_proposal``;
3. save exactly the fresh accepted portfolio produced by that result;
4. return the exact V1.3 ``WorkBreakdownAcceptanceResult`` and stop.

Strict boundary rules:

* ``portfolio_id`` must already be a ``UUID`` instance; anything else is
  rejected before any repository interaction;
* a missing portfolio raises :class:`PortfolioNotFoundError` before any
  acceptance work;
* every domain rule (project, anchor, WBS grammar) and all
  materialization remain authoritative in V1.3; nothing here duplicates
  validation or materialization;
* ``repository.save`` is called at most once, and only after successful
  V1.3 acceptance, with exactly ``result.portfolio``;
* the V1.3 ``WorkBreakdownAcceptanceError`` propagates unchanged;
* repository load/save exceptions propagate unchanged; there are no
  broad exception catches here;
* the caller's proposal and the loaded portfolio are never mutated;
* there are no concurrency, transaction, or versioning claims.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.work_breakdown_acceptance import (
    WorkBreakdownAcceptanceResult,
    accept_work_breakdown_proposal,
)
from trajectory_os.domain.work_breakdown_proposals import WorkBreakdownProposal


class PortfolioRepository(Protocol):
    """Structural read/save boundary for canonical portfolios.

    Intentionally non-runtime-checkable: only structural compatibility
    matters, and no persistence technology, engine, or transaction
    concept is part of this boundary.
    """

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        """Return the current persisted portfolio, or ``None`` if absent."""

        ...

    def save(self, portfolio: Portfolio) -> None:
        """Persist the canonical portfolio snapshot."""

        ...


class DurableWorkBreakdownAcceptanceError(ValueError):
    """Raised when durable work-breakdown acceptance fails at this boundary."""


class PortfolioNotFoundError(DurableWorkBreakdownAcceptanceError):
    """Raised when the portfolio to accept does not exist."""


def accept_work_breakdown_proposal_durably(
    portfolio_id: UUID,
    proposal: WorkBreakdownProposal,
    repository: PortfolioRepository,
) -> WorkBreakdownAcceptanceResult:
    """Accept a work-breakdown proposal and persist the accepted portfolio.

    Sequence is exactly: reject ``portfolio_id`` unless it is already a
    ``UUID`` instance, load the current portfolio, delegate acceptance to
    the V1.3 ``accept_work_breakdown_proposal`` use case, save
    ``result.portfolio`` only after successful acceptance, and return the
    exact V1.3 result object.

    The loaded portfolio and the caller's proposal are never mutated. A
    missing portfolio raises :class:`PortfolioNotFoundError`. The V1.3
    ``WorkBreakdownAcceptanceError`` and any repository load/save
    exception propagate unchanged.
    """

    if not isinstance(portfolio_id, UUID):
        raise DurableWorkBreakdownAcceptanceError(
            "portfolio_id must already be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )

    current = repository.load(portfolio_id)
    if current is None:
        raise PortfolioNotFoundError(f"portfolio not found: {portfolio_id}")

    result = accept_work_breakdown_proposal(current, proposal)

    repository.save(result.portfolio)

    return result
