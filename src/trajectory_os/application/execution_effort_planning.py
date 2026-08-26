"""Read-only durable planned-effort planning orchestration (V1.10-E).

The application boundary loads the CURRENT persisted Portfolio, reads the
durable append-only V1.10 estimate history for that portfolio through a
narrow structural reader port, and delegates all planning semantics to the
pure V1.10-D domain boundary.

It performs no Portfolio save, no estimate write, no observation write, no
status transition, no wall-clock read, and no provider/AI call. The boundary
does not claim snapshot isolation across the Portfolio load and the reader
list read.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from trajectory_os.application.work_breakdown_acceptance import PortfolioRepository
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
)
from trajectory_os.domain.execution_effort_planning import (
    WorkBreakdownEffortPlan,
    plan_work_breakdown_effort,
)


class ExecutionEffortEstimateReader(Protocol):
    """Read-only structural boundary for durable planned-effort estimates."""

    def list_for_portfolio(
        self,
        portfolio_id: UUID,
    ) -> tuple[ExecutionEffortEstimate, ...]:
        """Return all estimates belonging to ``portfolio_id`` deterministically."""

        ...

    def list_for_entity(
        self,
        portfolio_id: UUID,
        entity_id: UUID,
    ) -> tuple[ExecutionEffortEstimate, ...]:
        """Return history for one exact portfolio/entity identity pair."""

        ...


class DurableExecutionEffortPlanningError(ValueError):
    """Raised when durable planned-effort planning fails at this boundary."""


class ExecutionEffortPlanningPortfolioNotFoundError(
    DurableExecutionEffortPlanningError
):
    """Raised when the CURRENT Portfolio to plan is absent."""


def plan_work_breakdown_effort_durably(
    portfolio_id: UUID,
    project_id: UUID,
    portfolio_repository: PortfolioRepository,
    estimate_reader: ExecutionEffortEstimateReader,
) -> WorkBreakdownEffortPlan:
    """Plan durable planned effort against the CURRENT persisted project WBS.

    Sequence is exactly:

    ``validate portfolio_id → load CURRENT Portfolio → read Portfolio
    estimates → pure V1.10 planning → return exact immutable plan``.

    ``project_id`` and all estimate/WBS semantics are intentionally delegated
    to the pure domain boundary rather than duplicated here.
    Repository/reader/domain failures propagate unchanged except for the
    missing-Portfolio condition owned by this application boundary.
    """

    if not isinstance(portfolio_id, UUID):
        raise DurableExecutionEffortPlanningError(
            "portfolio_id must already be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )

    current = portfolio_repository.load(portfolio_id)
    if current is None:
        raise ExecutionEffortPlanningPortfolioNotFoundError(
            f"portfolio not found: {portfolio_id}"
        )

    estimates = estimate_reader.list_for_portfolio(portfolio_id)

    return plan_work_breakdown_effort(
        current,
        project_id=project_id,
        estimates=estimates,
    )
