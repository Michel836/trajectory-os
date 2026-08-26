"""Durable planned-effort estimate recording (V1.10-B persistence boundary).

Turns a single explicitly human-confirmed planned direct-effort estimate for
one entity of a portfolio into durable storage through the existing, minimal,
provider-agnostic ``PortfolioRepository`` boundary (V1.6) plus a narrow
structural estimate repository:

1. reject ``portfolio_id`` unless it is already a ``UUID`` instance,
   before ANY repository interaction;
2. load the CURRENT persisted portfolio;
3. a missing portfolio raises
   :class:`ExecutionEffortEstimatePortfolioNotFoundError`;
4. delegate ALL estimate/entity/duration/time semantics to the
   existing V1.10-A factory ``create_execution_effort_estimate``;
5. ``estimate_repository.add`` is called exactly once, only after
   successful domain creation, with exactly the domain estimate;
6. return the EXACT same estimate object produced by the V1.10-A factory.

Strict boundary rules:

* the CURRENT persisted portfolio is authoritative for entity membership;
  removed or missing entities fail through the real V1.10-A
  ``ExecutionEffortEstimateEntityNotFoundError`` (membership is not
  re-validated here);
* estimate/entity/duration/``estimated_at`` validation is never duplicated
  in this module; only ``portfolio_id`` is validated at this boundary;
* no append after any validation or domain failure, including a missing
  portfolio;
* ``PortfolioRepository.load`` and ``estimate_repository.add`` exceptions
  propagate unchanged; there are no broad exception catches;
* the loaded portfolio is never mutated; there is no portfolio save, no
  status transition, no observation write, and no wall-clock call;
* no AI, provider, SQLite, or distributed-transaction claims; the boundary
  does not claim a transaction across Portfolio load and estimate append.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from trajectory_os.application.work_breakdown_acceptance import (
    PortfolioRepository,
)
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
    create_execution_effort_estimate,
)


class ExecutionEffortEstimateRepository(Protocol):
    """Structural read/add boundary for planned-effort estimates.

    Intentionally non-runtime-checkable: only structural compatibility
    matters, and no persistence technology, engine, or transaction concept
    is part of this boundary.
    """

    def add(self, estimate: ExecutionEffortEstimate) -> None:
        """Persist one durable planned-effort estimate."""

        ...

    def get(self, estimate_id: UUID) -> ExecutionEffortEstimate | None:
        """Return the stored estimate, or ``None`` if absent."""

        ...


class DurableExecutionEffortEstimateError(ValueError):
    """Raised when durable planned-effort recording fails at this boundary."""


class ExecutionEffortEstimatePortfolioNotFoundError(
    DurableExecutionEffortEstimateError
):
    """Raised when the portfolio to record an estimate for is absent."""


def record_execution_effort_estimate_durably(
    portfolio_id: UUID,
    estimate_id: UUID,
    entity_id: UUID,
    duration_seconds: int,
    estimated_at: datetime,
    portfolio_repository: PortfolioRepository,
    estimate_repository: ExecutionEffortEstimateRepository,
) -> ExecutionEffortEstimate:
    """Record a planned direct-effort estimate against the current portfolio.

    Sequence is exactly: reject ``portfolio_id`` unless it is already a
    ``UUID`` instance, load the CURRENT persisted portfolio, delegate all
    estimate/entity/duration/time semantics to the V1.10-A
    ``create_execution_effort_estimate`` factory, add the exact resulting
    estimate to ``estimate_repository`` exactly once, and return that same
    estimate object.

    The loaded portfolio is never mutated and is never saved. A missing
    portfolio raises :class:`ExecutionEffortEstimatePortfolioNotFoundError`.
    V1.10-A domain errors and any repository exception propagate unchanged.
    """

    if not isinstance(portfolio_id, UUID):
        raise DurableExecutionEffortEstimateError(
            "portfolio_id must already be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )

    current = portfolio_repository.load(portfolio_id)
    if current is None:
        raise ExecutionEffortEstimatePortfolioNotFoundError(
            f"portfolio not found: {portfolio_id}"
        )

    estimate = create_execution_effort_estimate(
        current,
        estimate_id=estimate_id,
        entity_id=entity_id,
        duration_seconds=duration_seconds,
        estimated_at=estimated_at,
    )

    estimate_repository.add(estimate)

    return estimate
