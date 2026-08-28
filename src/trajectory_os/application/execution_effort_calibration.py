"""Read-only durable calibration-evidence orchestration (V1.12).

The application boundary loads the CURRENT persisted Portfolio exactly once,
reads the durable estimate and observation histories through narrow structural
reader ports, delegates actual measurement to the pure V1.9 domain boundary,
and finally delegates calibration selection to the pure V1.12 domain boundary.

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
from trajectory_os.domain.execution_effort_calibration import (
    WorkBreakdownEffortCalibrationEvidence,
    build_effort_calibration_evidence,
)
from trajectory_os.domain.execution_effort_measurement import (
    measure_work_breakdown_effort,
)


class DurableExecutionEffortCalibrationError(ValueError):
    """Raised when durable calibration-evidence derivation fails at this boundary."""


class ExecutionEffortCalibrationPortfolioNotFoundError(
    DurableExecutionEffortCalibrationError
):
    """Raised when the CURRENT Portfolio to calibrate is absent."""


def build_effort_calibration_evidence_durably(
    portfolio_id: UUID,
    project_id: UUID,
    portfolio_repository: PortfolioRepository,
    estimate_reader: ExecutionEffortEstimateReader,
    observation_reader: ExecutionEffortObservationReader,
) -> WorkBreakdownEffortCalibrationEvidence:
    """Derive durable leakage-safe calibration evidence for one project WBS.

    Sequence is exactly:

    ``validate portfolio_id → load CURRENT Portfolio exactly once →
    read estimate history → read observation history →
    pure V1.9 measurement → pure V1.12 calibration → STOP``.

    The CURRENT Portfolio is loaded exactly once so that measurement and
    calibration operate on the same in-memory canonical structure.
    Repository/reader/domain failures propagate unchanged except for the
    missing-Portfolio condition owned by this application boundary.
    """

    if not isinstance(portfolio_id, UUID):
        raise DurableExecutionEffortCalibrationError(
            "portfolio_id must already be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )

    current = portfolio_repository.load(portfolio_id)
    if current is None:
        raise ExecutionEffortCalibrationPortfolioNotFoundError(
            f"portfolio not found: {portfolio_id}"
        )

    estimates = estimate_reader.list_for_portfolio(portfolio_id)
    observations = observation_reader.list_for_portfolio(portfolio_id)

    measurement = measure_work_breakdown_effort(
        current,
        project_id=project_id,
        observations=observations,
    )

    return build_effort_calibration_evidence(current, measurement, estimates)
