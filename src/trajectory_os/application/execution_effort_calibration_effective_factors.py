"""Durable read-only effective calibration-factor orchestration (V1.17).

The application boundary reads the EXACT persisted V1.16 decision history
for the requested ``(portfolio_id, project_id)`` scope and passes those
EXACT records to the pure V1.17 resolver
(:func:`resolve_effective_effort_calibration_factors`), which performs
the ENTIRE effective-factor derivation deterministically.

The narrow structural repository boundary is the EXISTING V1.16 decision
repository (:class:`EffortCalibrationFactorDecisionRepository`), which
exposes only per-entity-type read-only ``list_history(...)``. The closed
:class:`EntityType` vocabulary is therefore iterated and each per-type
history is read with the existing public repository semantics. The
per-type histories are read using the closed vocabulary only as a
repository-access mechanism. The collected records are then reconstructed
into one deterministic project-scope history ordered by
``(decided_at chronological instant, decision_id.int)`` before the pure
resolver applies its FIRST-APPEARANCE rule. Enum iteration therefore never
becomes output-order policy. No SQL aggregation, no hidden effective-factor
selection logic, and no new read path is introduced to do V1.17 selection
work.

It performs NO writes, NO status transitions, NO V1.13/V1.14/V1.15
derivation, NO Portfolio load, NO estimate or observation read,
NO wall-clock reads, and NO provider/AI calls. Nothing is persisted:
V1.17 is derived state recomputed from the immutable V1.16 history on
demand, and V1.17 deliberately introduces no new persistence table,
cache, or materialized state.

Strict boundary rules:

* ``portfolio_id`` and ``project_id`` must already be ``UUID`` instances;
  they are validated BEFORE any repository access;
* repository/reader/domain failures propagate unchanged; there are no
  broad exception catches.
"""

from __future__ import annotations

from uuid import UUID

from trajectory_os.application.execution_effort_calibration_factor_decisions import (
    EffortCalibrationFactorDecisionRepository,
)
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactorSet,
    resolve_effective_effort_calibration_factors,
)
from trajectory_os.domain.execution_effort_calibration_factor_decisions import (
    EffortCalibrationFactorDecision,
)


class DurableEffectiveEffortCalibrationFactorError(ValueError):
    """Raised when durable effective-factor resolution fails at this boundary."""


def resolve_effective_effort_calibration_factors_durably(
    portfolio_id: UUID,
    project_id: UUID,
    decision_repository: EffortCalibrationFactorDecisionRepository,
) -> EffectiveEffortCalibrationFactorSet:
    """Resolve the current effective accepted calibration factors durably.

    Sequence is exactly:

    ``strictly validate portfolio_id and project_id (UUID instances, no
    coercion) — BEFORE any repository access
    → read the EXACT persisted V1.16 history for the requested scope by
    iterating the closed EntityType vocabulary through the existing
    read-only V1.16 list_history(...) semantics
    → pass those EXACT records to the pure V1.17 resolver
    → return the immutable effective factor set
    → STOP``.

    The repository boundary is read-only for this use case: no
    ``add``/update/delete call is made, and no write of any kind occurs.
    V1.17 resolves accepted persisted policy only; it does not need a
    CURRENT WBS load, proposal recomputation, or factor application.

    Repository/domain failures propagate unchanged. Any invalid scope or
    history condition fails with a visible error; nothing is silently
    skipped or deduplicated.
    """
    if not isinstance(portfolio_id, UUID):
        raise DurableEffectiveEffortCalibrationFactorError(
            f"portfolio_id must already be a UUID instance, got {type(portfolio_id).__name__}"
        )
    if not isinstance(project_id, UUID):
        raise DurableEffectiveEffortCalibrationFactorError(
            f"project_id must already be a UUID instance, got {type(project_id).__name__}"
        )

    history: list[EffortCalibrationFactorDecision] = []
    for entity_type in EntityType:
        history.extend(
            decision_repository.list_history(
                portfolio_id,
                project_id,
                entity_type,
            )
        )

    # V1.16 exposes history per entity type. Reconstruct the one
    # deterministic project-scope history before invoking the pure V1.17
    # first-appearance resolver. Enum iteration is only a read mechanism;
    # it must not become output-order policy.
    history.sort(
        key=lambda record: (
            record.decided_at,
            record.decision_id.int,
        )
    )

    return resolve_effective_effort_calibration_factors(
        history,
        portfolio_id,
        project_id,
    )
