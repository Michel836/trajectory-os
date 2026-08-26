"""Read-only durable execution-effort measurement orchestration (V1.9-A/C).

The application boundary loads the CURRENT persisted Portfolio, reads the durable
append-only V1.8 observation history for that portfolio through a narrow structural
reader port, and delegates all measurement semantics to the pure V1.9 domain boundary.

It performs no Portfolio save, no observation write, no status transition, no
wall-clock read, and no provider/AI call.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from trajectory_os.application.work_breakdown_acceptance import PortfolioRepository
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_measurement import (
    WorkBreakdownEffortMeasurement,
    measure_work_breakdown_effort,
)


class ExecutionEffortObservationReader(Protocol):
    """Read-only structural boundary for durable execution-effort observations."""

    def list_for_portfolio(
        self,
        portfolio_id: UUID,
    ) -> tuple[ExecutionEffortObservation, ...]:
        """Return all observations belonging to ``portfolio_id`` deterministically."""

        ...

    def list_for_entity(
        self,
        portfolio_id: UUID,
        entity_id: UUID,
    ) -> tuple[ExecutionEffortObservation, ...]:
        """Return history for one exact portfolio/entity identity pair."""

        ...


class DurableExecutionEffortMeasurementError(ValueError):
    """Raised when durable effort measurement fails at this application boundary."""


class ExecutionEffortMeasurementPortfolioNotFoundError(
    DurableExecutionEffortMeasurementError
):
    """Raised when the CURRENT Portfolio to measure is absent."""


def measure_work_breakdown_effort_durably(
    portfolio_id: UUID,
    project_id: UUID,
    portfolio_repository: PortfolioRepository,
    observation_reader: ExecutionEffortObservationReader,
) -> WorkBreakdownEffortMeasurement:
    """Measure durable execution effort against the CURRENT persisted project WBS.

    Sequence is exactly:

    ``validate portfolio_id → load CURRENT Portfolio → read Portfolio observations
    → pure V1.9 measurement → return exact immutable measurement``.

    ``project_id`` and all observation/WBS semantics are intentionally delegated to
    the pure domain boundary rather than duplicated here. Repository/reader/domain
    failures propagate unchanged except for the missing-Portfolio condition owned by
    this application boundary.
    """

    if not isinstance(portfolio_id, UUID):
        raise DurableExecutionEffortMeasurementError(
            "portfolio_id must already be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )

    current = portfolio_repository.load(portfolio_id)
    if current is None:
        raise ExecutionEffortMeasurementPortfolioNotFoundError(
            f"portfolio not found: {portfolio_id}"
        )

    observations = observation_reader.list_for_portfolio(portfolio_id)

    return measure_work_breakdown_effort(
        current,
        project_id=project_id,
        observations=observations,
    )
