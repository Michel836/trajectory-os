"""Immutable, self-auditing human decisions over V1.15 factor proposals (V1.16).

V1.16 is ONLY the human decision + history layer. The architecture is:

    AI proposes.
    Deterministic code validates.
    Human decides.
    Persistence records the accepted change.

This module defines the closed human decision vocabulary
(:class:`EffortCalibrationDecision`) and ONE strict, frozen, immutable
record (:class:`EffortCalibrationFactorDecision`) describing the EXACT
V1.15 per-segment proposal reviewed at decision time:

* one ``Accept / Reject / Defer`` human decision (explicit closed
  vocabulary, no default decision);
* an immutable self-auditing snapshot of the exact V1.15 segment evidence
  (sample count, exact planned/actual totals, proposal availability, exact
  V1.15 reason, and the exact reduced integer numerator/denominator when
  the proposal was AVAILABLE);
* the explicit caller-supplied decision id and timezone-aware decision
  timestamp.

The record is a SNAPSHOT, never a pointer: it must NOT reference "the
current proposal". Later V1.15 drift must not change previously recorded
decisions, so every evidence value is copied exactly at decision time.

This module only MODELS the record. It does not apply any factor to an
estimate, does not mutate or replace estimates, does not define a current
effective calibration, does not supersede or revoke decisions, does not
derive V1.15 (no profile, no sufficiency, no proposal inputs), performs no
persistence, performs no wall-clock read, and involves no provider, model,
or AI.

Deterministic-only invariants:

* No floats, Decimals, or statistics; exact integer numerator/denominator
  only; the factor is never stored, computed, or compared as a float.
* No default decision vocabulary value and no default record fields.
* Naive ``decided_at`` values are rejected; supplied aware timestamps are
  preserved exactly.
* No mutation: the model is frozen with strict validation.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
)

__all__ = [
    "EffortCalibrationDecision",
    "EffortCalibrationFactorDecision",
    "EffortCalibrationFactorDecisionError",
]


class EffortCalibrationFactorDecisionError(ValueError):
    """Raised when a V1.16 decision record is invalid."""


class EffortCalibrationDecision(StrEnum):
    """Exact, closed human decision vocabulary (V1.16).

    Explicit only: there is deliberately NO default decision.

    * ACCEPT: the human accepts the exact V1.15 AVAILABLE factor proposal
      reviewed at decision time;
    * REJECT: the human rejects the exact V1.15 segment reviewed at
      decision time (regardless of its availability reason);
    * DEFER: the human defers deciding on the exact V1.15 segment reviewed
      at decision time (regardless of its availability reason).

    No inference, no auto-accept, and no AI involvement is implied by any
    value: every decision is an explicit human act.
    """

    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"


class EffortCalibrationFactorDecision(BaseModel):
    """One immutable, self-auditing human decision record (V1.16).

    Every V1.15 evidence value is a SNAPSHOT of the EXACT aligned V1.15
    segment reviewed at decision time — never "the current proposal":

    * ``sample_count`` and the exact planned/actual totals are copied from
      the V1.15 segment;
    * ``minimum_required_sample_count`` is copied from the V1.14 policy
      recorded on the V1.15 proposal set;
    * ``proposal_available`` and ``proposal_reason`` are the exact V1.15
      values for that segment;
    * ``factor_numerator`` / ``factor_denominator`` are the exact reduced
      integer values (``>= 0`` / ``>= 1``, gcd == 1) whenever the reviewed
      proposal was AVAILABLE and absent otherwise.

    Exact per-record invariants:

    * ``decided_at`` is timezone-aware; naive datetimes are rejected; the
      supplied aware timestamp is preserved exactly (no clock is consulted);
    * AVAILABLE proposal: ``proposal_available`` is true, the reason is
      :attr:`EffortCalibrationFactorProposalReason.AVAILABLE`, both factor
      components are present, ``total_planned_duration_seconds >= 1``, the
      pair is reduced (gcd == 1), and the exact cross-multiplication
      identity::

          factor_numerator * total_planned_duration_seconds
              == factor_denominator * total_actual_duration_seconds

      holds;
    * UNAVAILABLE proposal: ``proposal_available`` is false and both factor
      components are absent;
    * :attr:`EffortCalibrationFactorProposalReason.ZERO_TOTAL_PLANNED_DURATION`
      requires ``total_planned_duration_seconds == 0``;
    * :attr:`EffortCalibrationDecision.ACCEPT` is valid ONLY for an
      AVAILABLE proposal: any ACCEPT combined with
      INSUFFICIENT_SAMPLES or ZERO_TOTAL_PLANNED_DURATION (or an
      unavailable proposal) is rejected; REJECT and DEFER are valid for
      any valid segment.

    Never: floats, Decimals, uncertainty, confidence, a "current"/
    "effective" decision marker, supersession, or revocation.
    """

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        str_strip_whitespace=False,
    )

    decision_id: UUID
    portfolio_id: UUID
    project_id: UUID
    entity_type: EntityType

    sample_count: Annotated[StrictInt, Field(ge=0)]
    minimum_required_sample_count: Annotated[StrictInt, Field(ge=1)]
    total_planned_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    total_actual_duration_seconds: Annotated[StrictInt, Field(ge=0)]

    proposal_available: bool
    proposal_reason: EffortCalibrationFactorProposalReason
    factor_numerator: Annotated[StrictInt, Field(ge=0)] | None = None
    factor_denominator: Annotated[StrictInt, Field(ge=1)] | None = None

    decision: EffortCalibrationDecision
    decided_at: datetime

    @model_validator(mode="after")
    def _validate_decision_record(
        self,
    ) -> EffortCalibrationFactorDecision:
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError(
                "decided_at must be timezone-aware (got "
                f"{self.decided_at!r})"
            )

        if (self.proposal_available is True) != (
            self.proposal_reason
            is EffortCalibrationFactorProposalReason.AVAILABLE
        ):
            raise ValueError(
                "proposal_available and proposal_reason must be consistent: "
                "only an AVAILABLE reason is available, no other reason is"
            )

        if self.proposal_available is True:
            if self.sample_count < 1:
                raise ValueError(
                    "an AVAILABLE proposal snapshot requires "
                    "sample_count >= 1"
                )
            if self.total_planned_duration_seconds < 1:
                raise ValueError(
                    "an AVAILABLE proposal snapshot requires "
                    "total_planned_duration_seconds >= 1"
                )
            if self.factor_numerator is None or self.factor_denominator is None:
                raise ValueError(
                    "an AVAILABLE proposal snapshot requires both "
                    "factor_numerator and factor_denominator"
                )
            if math.gcd(self.factor_numerator, self.factor_denominator) != 1:
                raise ValueError(
                    "factor_numerator and factor_denominator must be exact "
                    "reduced integer values with gcd == 1"
                )
            if (
                self.factor_numerator * self.total_planned_duration_seconds
                != self.factor_denominator * self.total_actual_duration_seconds
            ):
                raise ValueError(
                    "the snapshotted exact factor must satisfy "
                    "factor_numerator * total_planned_duration_seconds == "
                    "factor_denominator * total_actual_duration_seconds"
                )
        elif self.factor_numerator is not None or self.factor_denominator is not None:
            raise ValueError(
                "an unavailable proposal snapshot must not carry any factor "
                "components"
            )

        if (
            self.proposal_reason
            is EffortCalibrationFactorProposalReason.ZERO_TOTAL_PLANNED_DURATION
            and self.total_planned_duration_seconds != 0
        ):
            raise ValueError(
                "a ZERO_TOTAL_PLANNED_DURATION proposal snapshot requires "
                "total_planned_duration_seconds == 0"
            )

        if (
            self.decision is EffortCalibrationDecision.ACCEPT
            and (
                self.proposal_available is not True
                or self.proposal_reason
                is not EffortCalibrationFactorProposalReason.AVAILABLE
            )
        ):
            raise ValueError(
                "ACCEPT is valid only for an exact AVAILABLE V1.15 proposal; "
                f"reviewed reason was {self.proposal_reason}"
            )

        return self
