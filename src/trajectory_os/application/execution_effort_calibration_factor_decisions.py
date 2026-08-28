"""Durable recording of human decisions over V1.15 factor proposals (V1.16).

V1.16 is ONLY the human decision + history layer: it delegates the ENTIRE
proposal derivation to the EXISTING durable V1.15 boundary
(:func:`build_effort_calibration_factor_proposals_durably`) exactly once,
selects the EXACT segment for the requested CURRENT canonical
:class:`EntityType`, validates the explicit human decision against that
exact segment, builds the immutable self-auditing snapshot record
(:class:`EffortCalibrationFactorDecision`) by copying the V1.15 evidence
exactly, appends it ONCE through a narrow structural decision repository,
and returns the persisted record.

It does NOT apply any factor to an estimate, does NOT mutate or replace
estimates, does NOT define a current effective calibration, does NOT
supersede or revoke decisions, does NOT recompute any V1.9-V1.15
semantics, performs no wall-clock read, and no provider/AI call.

Strict boundary rules:

* ``portfolio_id``, ``project_id``, and ``decision_id`` must already be
  ``UUID`` instances;
* ``entity_type`` must already be an :class:`EntityType` instance;
* ``minimum_sample_count`` is a strict integer >= 1 (``bool``, floats,
  strings, and coercion are rejected) — validated before ANY I/O;
* ``decision`` must be an :class:`EffortCalibrationDecision` instance
  (plain strings are rejected);
* ``decided_at`` must be a timezone-aware ``datetime`` (naive datetimes
  are rejected) — no hidden clock, ever;
* a missing portfolio, an absent entity-type segment, or an ACCEPT over a
  non-AVAILABLE segment all fail BEFORE any append;
* ``decision_repository.add`` is called exactly once, only after the
  record is fully constructed, with exactly that record;
* repository/reader/domain failures propagate unchanged; there are no
  broad exception catches.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from trajectory_os.application.execution_effort_calibration_factor_proposals import (
    build_effort_calibration_factor_proposals_durably,
)
from trajectory_os.application.execution_effort_measurement import (
    ExecutionEffortObservationReader,
)
from trajectory_os.application.execution_effort_planning import (
    ExecutionEffortEstimateReader,
)
from trajectory_os.application.work_breakdown_acceptance import PortfolioRepository
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_factor_decisions import (
    EffortCalibrationDecision,
    EffortCalibrationFactorDecision,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
)
from trajectory_os.domain.execution_effort_calibration_sufficiency import (
    EffortCalibrationSufficiencyError,
    _require_strict_minimum_sample_count,
)


class DurableEffortCalibrationFactorDecisionError(ValueError):
    """Raised when durable decision recording fails at this boundary."""


class EffortCalibrationFactorDecisionSegmentNotFoundError(
    DurableEffortCalibrationFactorDecisionError
):
    """Raised when the requested entity type has no V1.15 segment."""


class EffortCalibrationFactorDecisionRejectedForSegmentError(
    DurableEffortCalibrationFactorDecisionError
):
    """Raised when the human decision is invalid for the exact segment."""


class EffortCalibrationFactorDecisionRepository(Protocol):
    """Structural append/history boundary for V1.16 decision records.

    Intentionally non-runtime-checkable: only structural compatibility
    matters, and no persistence technology, engine, or transaction concept
    is part of this boundary. Append-only: there is deliberately no
    update, replace, or delete method.
    """

    def add(self, decision: EffortCalibrationFactorDecision) -> None:
        """Persist one durable, immutable decision record."""

        ...

    def list_history(
        self,
        portfolio_id: UUID,
        project_id: UUID,
        entity_type: EntityType,
    ) -> tuple[EffortCalibrationFactorDecision, ...]:
        """Return the exact persisted history for one portfolio/project/type.

        Returns ``()`` when empty. Reconstructs the exact stored records
        only; it never infers a "current" or "effective" decision and
        never derives V1.15.
        """

        ...


def record_effort_calibration_factor_decision(
    portfolio_id: UUID,
    project_id: UUID,
    entity_type: EntityType,
    minimum_sample_count: int,
    decision: EffortCalibrationDecision,
    decision_id: UUID,
    decided_at: datetime,
    portfolio_repository: PortfolioRepository,
    estimate_reader: ExecutionEffortEstimateReader,
    observation_reader: ExecutionEffortObservationReader,
    decision_repository: EffortCalibrationFactorDecisionRepository,
) -> EffortCalibrationFactorDecision:
    """Record ONE exact human decision over one exact V1.15 segment.

    Sequence is exactly:

    ``strictly validate command values that can be checked before I/O
    (portfolio_id, project_id, decision_id, entity_type,
    minimum_sample_count, decision, decided_at)
    → derive the proposal set using the EXISTING durable V1.15 boundary
    (build_effort_calibration_factor_proposals_durably) exactly once
    → locate exactly one segment by the requested CURRENT canonical
    EntityType (fail if absent)
    → validate the human decision against the exact V1.15 segment
    → construct the record by copying the V1.15 evidence exactly
    → append ONCE via decision_repository.add
    → return the persisted record
    → STOP``.

    Human decision rule (V1.16): ACCEPT is valid ONLY when the exact
    V1.15 segment reasons AVAILABLE with an available proposal; any ACCEPT
    over INSUFFICIENT_SAMPLES or ZERO_TOTAL_PLANNED_DURATION is rejected
    before any write. REJECT and DEFER may be recorded for any valid V1.15
    segment.

    The record is a snapshot of the EXACT reviewed proposal, not a
    pointer: later V1.15 drift must not change recorded decisions.

    Any validation, derivation, selection, or decision-rule failure stops
    before the append: an invalid command appends exactly zero times.
    Repository/reader/domain exceptions propagate unchanged. The command
    performs no estimation, no factor application, and no wall-clock read.
    """
    if not isinstance(portfolio_id, UUID):
        raise DurableEffortCalibrationFactorDecisionError(
            "portfolio_id must already be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )
    if not isinstance(project_id, UUID):
        raise DurableEffortCalibrationFactorDecisionError(
            "project_id must already be a UUID instance, "
            f"got {type(project_id).__name__}"
        )
    if not isinstance(decision_id, UUID):
        raise DurableEffortCalibrationFactorDecisionError(
            "decision_id must already be a UUID instance, "
            f"got {type(decision_id).__name__}"
        )
    if not isinstance(entity_type, EntityType):
        raise DurableEffortCalibrationFactorDecisionError(
            "entity_type must already be an EntityType instance, "
            f"got {type(entity_type).__name__}"
        )
    try:
        minimum_required = _require_strict_minimum_sample_count(
            minimum_sample_count
        )
    except EffortCalibrationSufficiencyError as exc:
        raise DurableEffortCalibrationFactorDecisionError(str(exc)) from exc
    if not isinstance(decision, EffortCalibrationDecision):
        raise DurableEffortCalibrationFactorDecisionError(
            "decision must be an explicit EffortCalibrationDecision "
            f"instance (no default, no inference); got "
            f"{type(decision).__name__}"
        )
    if not isinstance(decided_at, datetime):
        raise DurableEffortCalibrationFactorDecisionError(
            "decided_at must be a datetime instance, "
            f"got {type(decided_at).__name__}"
        )
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise DurableEffortCalibrationFactorDecisionError(
            "decided_at must be timezone-aware (got "
            f"{decided_at!r})"
        )

    proposal_set = build_effort_calibration_factor_proposals_durably(
        portfolio_id=portfolio_id,
        project_id=project_id,
        minimum_sample_count=minimum_required,
        portfolio_repository=portfolio_repository,
        estimate_reader=estimate_reader,
        observation_reader=observation_reader,
    )

    matching = tuple(
        segment for segment in proposal_set.segments
        if segment.entity_type == entity_type
    )
    if len(matching) != 1:
        raise EffortCalibrationFactorDecisionSegmentNotFoundError(
            f"no exact V1.15 segment for entity_type {entity_type!r} in "
            f"project {project_id}; the segment must exist to be decided"
        )
    segment = matching[0]

    if (
        decision is EffortCalibrationDecision.ACCEPT
        and (
            segment.reason is not EffortCalibrationFactorProposalReason.AVAILABLE
            or segment.proposal_available is not True
        )
    ):
        raise EffortCalibrationFactorDecisionRejectedForSegmentError(
            "ACCEPT is valid only for an exact AVAILABLE V1.15 proposal; "
            f"the exact segment for {entity_type!r} reviewed reason "
            f"{segment.reason} (proposal available: "
            f"{segment.proposal_available})"
        )

    record = EffortCalibrationFactorDecision(
        decision_id=decision_id,
        portfolio_id=proposal_set.portfolio_id,
        project_id=proposal_set.project_id,
        entity_type=segment.entity_type,
        sample_count=segment.sample_count,
        minimum_required_sample_count=proposal_set.minimum_required_sample_count,
        total_planned_duration_seconds=(
            segment.total_planned_duration_seconds
        ),
        total_actual_duration_seconds=segment.total_actual_duration_seconds,
        proposal_available=segment.proposal_available,
        proposal_reason=segment.reason,
        factor_numerator=segment.factor_numerator,
        factor_denominator=segment.factor_denominator,
        decision=decision,
        decided_at=decided_at,
    )

    decision_repository.add(record)

    return record
