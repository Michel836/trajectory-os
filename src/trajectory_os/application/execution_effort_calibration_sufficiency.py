"""Read-only durable sufficient-sample orchestration (V1.14).

The application boundary applies an explicit caller-supplied
``minimum_sample_count`` policy over the authoritative V1.13 durable
calibration profile. It delegates the entire Portfolio/estimate/observation
pipeline to the existing V1.13 durable boundary
(:func:`build_effort_calibration_profile_durably`) and then to the pure
V1.14 domain boundary over the SAME authoritative profile.

It performs no Portfolio/estimate/observation pipeline duplication, no
additional repository/read-history pass, no writes, no status transitions,
no wall-clock reads, and no provider/AI calls. Nothing is persisted.
"""

from __future__ import annotations

from uuid import UUID

from trajectory_os.application.execution_effort_calibration_profile import (
    build_effort_calibration_profile_durably,
)
from trajectory_os.application.execution_effort_measurement import (
    ExecutionEffortObservationReader,
)
from trajectory_os.application.execution_effort_planning import (
    ExecutionEffortEstimateReader,
)
from trajectory_os.application.work_breakdown_acceptance import PortfolioRepository
from trajectory_os.domain.execution_effort_calibration_profile import (
    WorkBreakdownEffortCalibrationProfile,
)
from trajectory_os.domain.execution_effort_calibration_sufficiency import (
    EffortCalibrationSufficiencyError,
    WorkBreakdownEffortCalibrationSufficiencyAssessment,
    _require_strict_minimum_sample_count,
    assess_effort_calibration_sufficiency,
)


class DurableEffortCalibrationSufficiencyError(ValueError):
    """Raised when durable sufficiency derivation fails at this boundary."""


def _validate_threshold(minimum_sample_count: object) -> int:
    """Validate the explicit policy before any repository/reader access."""
    try:
        return _require_strict_minimum_sample_count(minimum_sample_count)
    except EffortCalibrationSufficiencyError as exc:
        raise DurableEffortCalibrationSufficiencyError(str(exc)) from exc


def assess_effort_calibration_sufficiency_durably(
    portfolio_id: UUID,
    project_id: UUID,
    minimum_sample_count: int,
    portfolio_repository: PortfolioRepository,
    estimate_reader: ExecutionEffortEstimateReader,
    observation_reader: ExecutionEffortObservationReader,
) -> WorkBreakdownEffortCalibrationSufficiencyAssessment:
    """Assess explicit sample sufficiency for one project's evidence chain.

    Sequence is exactly:

    ``validate minimum_sample_count (AND portfolio_id, both before any
    repository/reader access)
    → delegate once to V1.13 build_effort_calibration_profile_durably(...)
    → pure V1.14 assess_effort_calibration_sufficiency(profile,
    minimum_sample_count)
    → return immutable assessment
    → STOP``.

    V1.13 remains authoritative for loading the CURRENT Portfolio exactly
    once and composing V1.9 → V1.12 → V1.13; V1.14 deliberately does not
    duplicate that orchestration and performs no additional read pass.

    Repository/reader/domain failures propagate unchanged. The explicit
    ``minimum_sample_count`` is a strict integer >= 1 (``bool``, floats,
    strings, and coercion are rejected) and, when invalid, fails before
    any repository/reader access.
    """
    minimum_required = _validate_threshold(minimum_sample_count)

    profile: WorkBreakdownEffortCalibrationProfile = (
        build_effort_calibration_profile_durably(
            portfolio_id=portfolio_id,
            project_id=project_id,
            portfolio_repository=portfolio_repository,
            estimate_reader=estimate_reader,
            observation_reader=observation_reader,
        )
    )

    return assess_effort_calibration_sufficiency(profile, minimum_required)
