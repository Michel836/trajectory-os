"""Read-only resolution + application composition (V1.19).

The narrow application boundary composes the two EXISTING authoritative
boundaries for one explicit
``(portfolio_id, project_id, entity_type, candidate_duration_seconds)``
scope:

* the V1.17 durable read-only resolver
  (:func:`resolve_effective_effort_calibration_factors_durably`) —
  called EXACTLY ONCE per V1.19 request; and
* the pure V1.18 exact-integer application function
  (:func:`apply_effective_effort_calibration_factor`) — called exactly
  once, and ONLY on the AVAILABLE path.

V1.19 answers exactly one question:

> **For this explicit project/entity-type/candidate duration, is an
> effective accepted calibration factor available, and if so what exact
> V1.18 calibrated proposal results?**

Architecture principle (unchanged):

    AI proposes.
    Deterministic code validates.
    Human decides.
    Persistence records the accepted change.

**Critical missing-factor policy (authoritative, deliberate, documented):**

Absence of an effective accepted factor for the requested entity type is
an EXPECTED DOMAIN STATE, not a technical failure. It is never silently
converted to an identity factor ``1/1``, never fabricated into a
calibrated proposal identical to the candidate, and never raised as an
exception. Instead V1.19 returns an explicit immutable result with status
NO_EFFECTIVE_FACTOR, because::

    no calibration evidence available
        !=
    a human-accepted exact factor of 1/1

**Strictness preserved even when the factor is missing:**

The exact V1.18 candidate-domain guard (genuine ``int``, ``bool``
rejected, no float/Decimal/string coercion, ``>= 0``) is enforced at this
composition boundary BEFORE any delegation, and the exact scope guard
(``portfolio_id`` / ``project_id`` as ``UUID`` instances, ``entity_type``
as an :class:`EntityType` instance, no coercion) is enforced first.
NO_EFFECTIVE_FACTOR must not become a bypass around V1.18 candidate-domain
validity.

**Read-only:**

V1.19 performs NO writes: no V1.16 decision write, no estimate
create/update/delete, no proposal persistence, no new
table/cache/materialized view, no clock read, no UUID generation, no
repository-state mutation, no AI/provider call. It may call only the
existing V1.17 durable resolution path and the pure V1.18 application
function.

**Non-duplication:**

V1.19 does NOT re-read V1.16 history manually, does NOT reimplement
latest-ACCEPT selection, and does NOT reimplement V1.18 multiplication,
divmod, half-up rounding, or arithmetic evidence construction. If a
matching factor exists, the ENTIRE application is delegated to V1.18
unchanged; if none exists, nothing is computed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from trajectory_os.application.execution_effort_calibration_effective_factors import (
    resolve_effective_effort_calibration_factors_durably,
)
from trajectory_os.application.execution_effort_calibration_factor_decisions import (
    EffortCalibrationFactorDecisionRepository,
)
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_factor_application import (
    CalibratedEffortProposal,
    apply_effective_effort_calibration_factor,
)

__all__ = [
    "EffectiveCalibrationApplicationError",
    "EffectiveCalibrationApplicationResult",
    "EffectiveCalibrationApplicationStatus",
    "resolve_and_apply_effective_effort_calibration",
]


class EffectiveCalibrationApplicationError(ValueError):
    """Raised when V1.19 read-only composition fails on invalid input."""


class EffectiveCalibrationApplicationStatus(StrEnum):
    """Exact, closed V1.19 result status vocabulary.

    * AVAILABLE — exactly one immutable V1.18
      :class:`CalibratedEffortProposal` is present in the result;
    * NO_EFFECTIVE_FACTOR — the requested entity type has no currently
      effective accepted factor: the proposal is absent, the candidate
      scope is retained exactly, and this is a valid, expected domain
      state (NOT an identity-factor fallback, NOT an unchanged
      calibrated proposal, NOT an error).
    """

    AVAILABLE = "available"
    NO_EFFECTIVE_FACTOR = "no_effective_factor"


class EffectiveCalibrationApplicationResult(BaseModel):
    """Immutable V1.19 read-only composition result.

    Retains the EXACT explicit scope and candidate input, the exact
    status, and — on the AVAILABLE path only — the EXACT immutable V1.18
    :class:`CalibratedEffortProposal`. The V1.18 proposal is the
    authoritative audit evidence: its arithmetic fields are deliberately
    NOT copied into this wrapper; composition preserves provenance
    without duplicating it.

    Cross-field invariants (freshly validated on EVERY construction; a
    hostile ``model_construct()`` bypass is defeated):

    * ``status == AVAILABLE``      <=> ``proposal is not None``;
    * ``status == NO_EFFECTIVE_FACTOR`` <=> ``proposal is None``;
    * when present, ``proposal.entity_type
      == entity_type``;
    * when present, ``proposal.candidate_duration_seconds
      == candidate_duration_seconds``.
    """

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        str_strip_whitespace=False,
    )

    portfolio_id: UUID
    project_id: UUID
    entity_type: EntityType
    candidate_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    status: EffectiveCalibrationApplicationStatus
    proposal: CalibratedEffortProposal | None

    @model_validator(mode="after")
    def _validate_status_alignment(self) -> EffectiveCalibrationApplicationResult:
        if self.status is EffectiveCalibrationApplicationStatus.AVAILABLE and self.proposal is None:
            raise ValueError(
                "status AVAILABLE requires exactly one immutable V1.18 "
                "CalibratedEffortProposal to be present"
            )

        if (
            self.status is EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR
            and self.proposal is not None
        ):
            raise ValueError(
                "status NO_EFFECTIVE_FACTOR forbids any calibrated "
                "proposal; a proposal is only present when an effective "
                "accepted factor for the requested entity type was applied"
            )

        proposal = self.proposal
        if proposal is not None:
            # A same-model instance is not itself proof that the nested
            # V1.18 proposal was validly constructed: model_construct()
            # can bypass its arithmetic invariants. Freshly rebuild and
            # strictly validate ordinary Python data so V1.19 can never
            # carry hostile or internally inconsistent V1.18 evidence.
            CalibratedEffortProposal.model_validate(
                proposal.model_dump(mode="python"),
                strict=True,
            )

            if proposal.entity_type is not self.entity_type:
                raise ValueError(
                    "the carried V1.18 proposal belongs to entity type "
                    f"{proposal.entity_type!s}, which differs from the "
                    f"requested {self.entity_type!s}; exact-type-only "
                    "selection is mandatory"
                )
            if proposal.candidate_duration_seconds != self.candidate_duration_seconds:
                raise ValueError(
                    "the carried V1.18 candidate "
                    f"({proposal.candidate_duration_seconds}) differs from "
                    "the requested candidate "
                    f"({self.candidate_duration_seconds}); the proposal "
                    "must be computed from the exact requested candidate"
                )

        return self


def resolve_and_apply_effective_effort_calibration(
    portfolio_id: UUID,
    project_id: UUID,
    entity_type: EntityType,
    candidate_duration_seconds: int,
    decision_repository: EffortCalibrationFactorDecisionRepository,
) -> EffectiveCalibrationApplicationResult:
    """Compose V1.17 durable resolution and V1.18 exact application (V1.19).

    Sequence is exactly:

    ``strictly require portfolio_id and project_id as UUID instances
      (no coercion), entity_type as an EntityType instance (no string
      coercion), and candidate_duration_seconds as a genuine int (bool
      rejected, no float/Decimal/string coercion) that is >= 0 — BEFORE
      any delegation or repository access
    → resolve_effective_effort_calibration_factors_durably(
        portfolio_id, project_id, decision_repository)   # V1.17, ONCE
    → select the EXACT factor whose entity_type == entity_type
      (no cross-type fallback, no blending, no first-available,
      no inference)
    → if no such factor exists:
        return the immutable NO_EFFECTIVE_FACTOR result with proposal
        absent (expected domain state, NOT an error, NOT an identity
        fallback)
    → proposal = apply_effective_effort_calibration_factor(
        candidate_duration_seconds, matching)            # V1.18, unchanged
    → return the immutable AVAILABLE result carrying exactly that
      proposal
    → STOP``.

    Missing-factor semantics: absence of an effective accepted factor
    for the requested entity type is an expected domain state. It is
    never silently converted to ``1/1``, never fabricated into an
    unchanged calibrated proposal, and never raised as an exception.

    Read-only: no decision/estimate/proposal write, no clock, no UUID
    generation, no repository-state mutation, no AI/provider call.

    Repository/reader/domain failures from V1.17/V1.18 propagate
    unchanged; there are no broad exception catches. Invalid scope or
    candidate inputs fail with
    :class:`EffectiveCalibrationApplicationError`; inputs are rejected,
    never repaired.
    """
    if not isinstance(portfolio_id, UUID):
        raise EffectiveCalibrationApplicationError(
            f"portfolio_id must already be a UUID instance, got {type(portfolio_id).__name__}"
        )
    if not isinstance(project_id, UUID):
        raise EffectiveCalibrationApplicationError(
            f"project_id must already be a UUID instance, got {type(project_id).__name__}"
        )
    if not isinstance(entity_type, EntityType):
        raise EffectiveCalibrationApplicationError(
            f"entity_type must already be an EntityType instance, got {type(entity_type).__name__}"
        )

    # Exact V1.18 candidate-domain guard, enforced at the composition
    # boundary BEFORE any delegation: NO_EFFECTIVE_FACTOR must not become
    # a bypass around candidate validity. V1.18 re-validates this on the
    # AVAILABLE path; no coercion or normalization is performed here.
    if type(candidate_duration_seconds) is not int:
        raise EffectiveCalibrationApplicationError(
            "candidate_duration_seconds must be a genuine non-negative "
            "integer (no bool, float, Decimal, string, or other coercion); "
            f"got {type(candidate_duration_seconds).__name__}"
        )
    if candidate_duration_seconds < 0:
        raise EffectiveCalibrationApplicationError(
            f"candidate_duration_seconds must be >= 0; got {candidate_duration_seconds}"
        )

    effective_set = resolve_effective_effort_calibration_factors_durably(
        portfolio_id,
        project_id,
        decision_repository,
    )

    matching = next(
        (factor for factor in effective_set.factors if factor.entity_type is entity_type),
        None,
    )

    if matching is None:
        # Expected domain state: no effective accepted factor for the
        # requested entity type. Not an error; not an identity fallback;
        # not a fabricated proposal. V1.18 is deliberately NOT called.
        return EffectiveCalibrationApplicationResult(
            portfolio_id=portfolio_id,
            project_id=project_id,
            entity_type=entity_type,
            candidate_duration_seconds=candidate_duration_seconds,
            status=EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR,
            proposal=None,
        )

    proposal = apply_effective_effort_calibration_factor(
        candidate_duration_seconds,
        matching,
    )

    return EffectiveCalibrationApplicationResult(
        portfolio_id=portfolio_id,
        project_id=project_id,
        entity_type=entity_type,
        candidate_duration_seconds=candidate_duration_seconds,
        status=EffectiveCalibrationApplicationStatus.AVAILABLE,
        proposal=proposal,
    )
