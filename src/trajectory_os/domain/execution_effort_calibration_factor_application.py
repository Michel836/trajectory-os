"""Pure exact-integer application of one effective calibration factor (V1.18).

Given ONE explicit non-negative candidate direct-effort duration in integer
seconds and ONE exact V1.17 effective accepted calibration factor, V1.18
produces ONE immutable, self-auditing calibrated effort proposal using exact
integer arithmetic and one explicit deterministic rounding rule
(round-to-nearest, ties upward / half-up).

Architecture principle (unchanged):

    AI proposes.
    Deterministic code validates.
    Human decides.
    Persistence records the accepted change.

V1.18 answers exactly one question:

> **If this explicit candidate duration is calibrated using this exact
> human-accepted effective factor, what integer-second duration results,
> and what exact arithmetic produced it?**

It does NOT persist an estimate, does NOT create, replace, or mutate an
:class:`ExecutionEffortEstimate`, does NOT automatically record a revision,
does NOT select an entity, does NOT infer or resolve a factor, does NOT
decide whether calibration should be used, and does NOT call AI or any
provider.

**Rounding policy (authoritative, deliberate, documented):**

Let ``C`` be the candidate duration, ``N`` the factor numerator and ``D``
the factor denominator (all non-negative integers with ``D >= 1``)::

    scaled_numerator = C * N
    q, r = divmod(scaled_numerator, D)

    if 2 * r >= D:
        calibrated_duration_seconds = q + 1
    else:
        calibrated_duration_seconds = q

This is round-to-nearest with ties rounding UPWARD (half-up). Python
``round()`` is DELIBERATELY NOT used because its ties-to-even (bankers')
semantics differ at exact ties (e.g. ``1 * 1/2`` must yield ``1``, where
bankers rounding would yield ``0``). The rule is implemented and validated
with integer arithmetic only; no float or Decimal is introduced anywhere.

**Input integrity / hostile-model defense:**

The candidate duration must be a genuine ``int`` (``bool`` rejected, no
coercion, ``>= 0``). The factor must be a genuine
:class:`EffectiveEffortCalibrationFactor` instance and is FRESHLY
revalidated from ordinary Python data (``model_dump(mode="python")``
followed by strict Pydantic validation) so that instances bypassing
construction validation via ``model_construct()`` are defeated. Malformed
or non-canonical factor inputs are rejected, never repaired, and the
supplied factor is preserved unchanged.

**Result vocabulary:**

The result model (:class:`CalibratedEffortProposal`) is strict, frozen,
and self-auditing: it retains the exact V1.17 accepted-factor identity and
evidence (``entity_type``, ``decision_id``, ``decided_at``, sample
sufficiency, exact planned/actual totals, exact reduced integer factor
pair), the exact candidate input, the exact integer intermediate arithmetic
needed to reproduce the rounding decision (``scaled_numerator``,
``quotient_seconds``, ``remainder``, ``rounded_up``), and the exact
output. Its fresh validation re-derives and enforces the entire arithmetic
chain, so any hostile inconsistent construction is rejected.
V1.18 is DERIVED state; nothing here is persisted, cached, or materialized.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactor,
)

__all__ = [
    "CalibratedEffortProposal",
    "CalibratedEffortProposalError",
    "apply_effective_effort_calibration_factor",
]


class CalibratedEffortProposalError(ValueError):
    """Raised when V1.18 effective-factor application fails."""


class CalibratedEffortProposal(BaseModel):
    """One immutable, self-auditing calibrated effort proposal (V1.18).

    The accepted-factor evidence is copied EXACTLY from one V1.17
    effective factor instance:

    * ``entity_type``, ``decision_id``, and ``decided_at`` identify the
      exact human-accepted V1.16 record that the factor was selected from;
    * ``sample_count``, ``minimum_required_sample_count``, and the exact
      planned/actual totals are the exact accepted proposal snapshot
      evidence;
    * ``factor_numerator`` / ``factor_denominator`` are the exact reduced
      integer components of the accepted factor.

    The audit chain is complete and revalidated on EVERY construction
    (hostile ``model_construct()`` bypass is defeated):

    * ``candidate_duration_seconds`` is a strict integer ``>= 0``;
    * ``factor_numerator`` is a strict integer ``>= 0``;
    * ``factor_denominator`` is a strict integer ``>= 1``;
    * ``gcd(factor_numerator, factor_denominator) == 1``;
    * the copied accepted-factor evidence retains the exact V1.17
      cross-multiplication identity
      ``factor_numerator * total_planned_duration_seconds ==
      factor_denominator * total_actual_duration_seconds``;
    * ``scaled_numerator == candidate_duration_seconds * factor_numerator``;
    * ``quotient_seconds == scaled_numerator // factor_denominator``;
    * ``remainder == scaled_numerator % factor_denominator`` and
      ``0 <= remainder < factor_denominator``;
    * ``rounded_up == (2 * remainder >= factor_denominator)``
      (round-to-nearest, ties upward — the ONLY rounding rule accepted);
    * ``calibrated_duration_seconds == quotient_seconds
      + (1 if rounded_up else 0)``.

    No float, Decimal, uncertainty, confidence, persistence, or AI
    semantics are present.
    """

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        str_strip_whitespace=False,
    )

    entity_type: EntityType
    decision_id: UUID
    decided_at: datetime

    sample_count: Annotated[StrictInt, Field(ge=1)]
    minimum_required_sample_count: Annotated[StrictInt, Field(ge=1)]
    total_planned_duration_seconds: Annotated[StrictInt, Field(ge=1)]
    total_actual_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    factor_numerator: Annotated[StrictInt, Field(ge=0)]
    factor_denominator: Annotated[StrictInt, Field(ge=1)]

    candidate_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    scaled_numerator: Annotated[StrictInt, Field(ge=0)]
    quotient_seconds: Annotated[StrictInt, Field(ge=0)]
    remainder: Annotated[StrictInt, Field(ge=0)]
    rounded_up: bool
    calibrated_duration_seconds: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def _validate_calibrated_proposal(self) -> CalibratedEffortProposal:
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError(f"decided_at must be timezone-aware (got {self.decided_at!r})")

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
                "the accepted exact factor must satisfy "
                "factor_numerator * total_planned_duration_seconds == "
                "factor_denominator * total_actual_duration_seconds"
            )

        if self.sample_count < self.minimum_required_sample_count:
            raise ValueError(
                "an accepted effective factor requires "
                "sample_count >= minimum_required_sample_count"
            )

        expected_scaled = self.candidate_duration_seconds * self.factor_numerator
        if self.scaled_numerator != expected_scaled:
            raise ValueError(
                "scaled_numerator must equal candidate_duration_seconds * "
                f"factor_numerator (expected {expected_scaled}, "
                f"got {self.scaled_numerator})"
            )

        expected_quotient = self.scaled_numerator // self.factor_denominator
        expected_remainder = self.scaled_numerator % self.factor_denominator
        if self.quotient_seconds != expected_quotient:
            raise ValueError(
                "quotient_seconds must equal scaled_numerator // "
                f"factor_denominator (expected {expected_quotient}, "
                f"got {self.quotient_seconds})"
            )
        if self.remainder != expected_remainder:
            raise ValueError(
                "remainder must equal scaled_numerator % factor_denominator "
                f"(expected {expected_remainder}, got {self.remainder})"
            )
        if not 0 <= self.remainder < self.factor_denominator:
            raise ValueError(
                "remainder must satisfy 0 <= remainder < factor_denominator"
            )

        expected_rounded_up = 2 * self.remainder >= self.factor_denominator
        if self.rounded_up is not expected_rounded_up:
            raise ValueError(
                "rounded_up must equal (2 * remainder >= factor_denominator); "
                "only round-to-nearest ties-upward (half-up) rounding is "
                f"accepted (expected {expected_rounded_up}, got {self.rounded_up})"
            )

        expected_calibrated = self.quotient_seconds + (1 if expected_rounded_up else 0)
        if self.calibrated_duration_seconds != expected_calibrated:
            raise ValueError(
                "calibrated_duration_seconds must equal "
                "quotient_seconds + (1 if rounded_up else 0) "
                f"(expected {expected_calibrated}, "
                f"got {self.calibrated_duration_seconds})"
            )

        return self


def _require_genuine_factor(raw: object) -> EffectiveEffortCalibrationFactor:
    """Require a genuine V1.17 factor and freshly revalidate it strictly.

    The caller may supply a hostile instance that bypassed construction
    validation (e.g. via ``model_construct()``), so merely observing an
    instance of the right class is never trusted: the factor is rebuilt
    from ordinary Python data (``model_dump(mode="python")``) through
    strict Pydantic validation, which re-runs every model-level invariant
    (exact reduced factor, cross-multiplication identity, aware timestamp,
    sample sufficiency). The revalidated instance carries the same values;
    the original supplied instance is never modified.

    Raises :class:`CalibratedEffortProposalError` on any failure.
    """
    if not isinstance(raw, EffectiveEffortCalibrationFactor):
        raise CalibratedEffortProposalError(
            "factor must be a genuine V1.17 EffectiveEffortCalibrationFactor "
            f"instance; got {type(raw).__name__}"
        )

    try:
        dumped = raw.model_dump(mode="python")
        return EffectiveEffortCalibrationFactor.model_validate(dumped, strict=True)
    except Exception as exc:  # noqa: BLE001 - surface every bypass cleanly
        raise CalibratedEffortProposalError(
            "the supplied V1.17 EffectiveEffortCalibrationFactor failed "
            f"fresh strict revalidation and is rejected: {exc}"
        ) from exc


def apply_effective_effort_calibration_factor(
    candidate_duration_seconds: int,
    factor: EffectiveEffortCalibrationFactor,
) -> CalibratedEffortProposal:
    """Apply one exact V1.17 effective accepted calibration factor (V1.18).

    Sequence is exactly:

    ``strictly require candidate_duration_seconds as a genuine int (bool
    rejected, no coercion) that is >= 0
    → require a genuine EffectiveEffortCalibrationFactor instance and
      freshly and strictly revalidate it (defeats model_construct)
    → exact integer arithmetic only:
        scaled_numerator = candidate_duration_seconds
                           * factor_numerator
        quotient_seconds, remainder = divmod(scaled_numerator,
                                             factor_denominator)
        rounded_up = (2 * remainder >= factor_denominator)   # half-up
        calibrated_duration_seconds = quotient_seconds
                          + (1 if rounded_up else 0)
    → return the immutable self-auditing CalibratedEffortProposal
    → STOP``.

    Semantics:

    * the candidate duration is EXPLICIT; no entity, estimate, repository,
      or WBS lookup is or may be consulted;
    * a zero candidate yields a zero calibrated duration for any valid
      factor; a zero accepted factor (``0/1``) yields a zero calibrated
      duration;
    * rounding is exactly round-to-nearest with ties upward
      (``2 * remainder >= denominator``); Python ``round()`` is never
      used because its ties-to-even semantics differ at ties;
    * very large Python integers stay exact: no float or Decimal is
      introduced at any point;
    * the supplied factor is never mutated; identical inputs yield
      identical immutable results (deterministic, no clock, no UUID
      generation, no repository, no AI, no persistence);
    * V1.17 resolution is NOT re-run inside this boundary: the supplied
      factor is already the authoritative effective accepted factor.

    Raises :class:`CalibratedEffortProposalError` on any invalid candidate
    or factor input; inputs are rejected, never repaired.
    """
    if type(candidate_duration_seconds) is not int:
        raise CalibratedEffortProposalError(
            "candidate_duration_seconds must be a genuine non-negative "
            "integer (no bool, float, Decimal, string, or other coercion); "
            f"got {type(candidate_duration_seconds).__name__}"
        )
    if candidate_duration_seconds < 0:
        raise CalibratedEffortProposalError(
            "candidate_duration_seconds must be >= 0; "
            f"got {candidate_duration_seconds}"
        )

    validated_factor = _require_genuine_factor(factor)

    scaled_numerator = candidate_duration_seconds * validated_factor.factor_numerator
    quotient_seconds, remainder = divmod(scaled_numerator, validated_factor.factor_denominator)
    rounded_up = 2 * remainder >= validated_factor.factor_denominator
    calibrated_duration_seconds = quotient_seconds + (1 if rounded_up else 0)

    return CalibratedEffortProposal(
        entity_type=validated_factor.entity_type,
        decision_id=validated_factor.decision_id,
        decided_at=validated_factor.decided_at,
        sample_count=validated_factor.sample_count,
        minimum_required_sample_count=validated_factor.minimum_required_sample_count,
        total_planned_duration_seconds=validated_factor.total_planned_duration_seconds,
        total_actual_duration_seconds=validated_factor.total_actual_duration_seconds,
        factor_numerator=validated_factor.factor_numerator,
        factor_denominator=validated_factor.factor_denominator,
        candidate_duration_seconds=candidate_duration_seconds,
        scaled_numerator=scaled_numerator,
        quotient_seconds=quotient_seconds,
        remainder=remainder,
        rounded_up=rounded_up,
        calibrated_duration_seconds=calibrated_duration_seconds,
    )
