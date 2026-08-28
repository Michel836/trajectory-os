"""Read-only durable planned-vs-actual effort comparison orchestration (V1.11).

The application boundary loads the CURRENT persisted Portfolio exactly once,
reads the durable estimate and observation histories through narrow structural
reader ports, delegates planning/measurement to the pure V1.10/V1.9 domain
boundaries, and finally delegates comparison to the pure V1.11 domain boundary.

It performs no Portfolio save, no estimate write, no observation write, no
status transition, no wall-clock read, and no provider/AI call. The boundary
does not claim snapshot isolation across the Portfolio load and the reader
list reads.
"""

from __future__ import annotations

from uuid import UUID

from trajectory_os.application.execution_effort_measurement import (
    ExecutionEffortObservationReader,
)
from trajectory_os.application.execution_effort_planning import (
    ExecutionEffortEstimateReader,
)
from trajectory_os.application.work_breakdown_acceptance import PortfolioRepository
from trajectory_os.domain.execution_effort_comparison import (
    WorkBreakdownEffortComparison,
    compare_work_breakdown_effort,
)
from trajectory_os.domain.execution_effort_measurement import (
    measure_work_breakdown_effort,
)
from trajectory_os.domain.execution_effort_planning import (
    plan_work_breakdown_effort,
)


class DurableExecutionEffortComparisonError(ValueError):
    """Raised when durable effort comparison fails at this application boundary."""


class ExecutionEffortComparisonPortfolioNotFoundError(
    DurableExecutionEffortComparisonError
):
    """Raised when the CURRENT Portfolio to compare is absent."""


def compare_work_breakdown_effort_durably(
    portfolio_id: UUID,
    project_id: UUID,
    portfolio_repository: PortfolioRepository,
    estimate_reader: ExecutionEffortEstimateReader,
    observation_reader: ExecutionEffortObservationReader,
) -> WorkBreakdownEffortComparison:
    """Compare durable planned vs actual effort against the CURRENT project WBS.

    Sequence is exactly:

    ``validate portfolio_id → load CURRENT Portfolio → read estimates →
    read observations → pure V1.10 planning → pure V1.9 measurement →
    pure V1.11 comparison → return exact immutable comparison``.

    The CURRENT Portfolio is loaded exactly once so that plan and measurement
    operate on the same in-memory canonical structure. Repository/reader/domain
    failures propagate unchanged except for the missing-Portfolio condition
    owned by this application boundary.
    """

    if not isinstance(portfolio_id, UUID):
        raise DurableExecutionEffortComparisonError(
            "portfolio_id must already be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )

    current = portfolio_repository.load(portfolio_id)
    if current is None:
        raise ExecutionEffortComparisonPortfolioNotFoundError(
            f"portfolio not found: {portfolio_id}"
        )

    estimates = estimate_reader.list_for_portfolio(portfolio_id)
    observations = observation_reader.list_for_portfolio(portfolio_id)

    plan = plan_work_breakdown_effort(
        current,
        project_id=project_id,
        estimates=estimates,
    )

    measurement = measure_work_breakdown_effort(
        current,
        project_id=project_id,
        observations=observations,
    )

    return compare_work_breakdown_effort(plan, measurement)
