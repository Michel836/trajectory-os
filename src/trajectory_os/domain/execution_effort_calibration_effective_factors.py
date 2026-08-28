"""Deterministic current effective accepted calibration resolution (V1.17).

Given the authoritative immutable V1.16 human-decision history for one
``(portfolio_id, project_id)`` scope, V1.17 resolves AT MOST ONE currently
effective accepted calibration factor per represented
:class:`EntityType` — and NOTHING ELSE.

Architecture principle (unchanged):

    AI proposes.
    Deterministic code validates.
    Human decides.
    Persistence records the accepted change.

V1.17 answers exactly one question:

> **Which exact human-accepted V1.16 calibration factor is currently
> selected for each entity type, according to an explicit deterministic
> history-resolution policy?**

It does NOT apply any factor to an estimate, does NOT create, replace, or
mutate an :class:`ExecutionEffortEstimate`, does NOT define or perform any
duration rounding, does NOT rewrite, revoke, supersede, or otherwise alter
V1.16 decision semantics, does NOT infer a decision, does NOT derive or
recompute any V1.13/V1.14/V1.15 layer, does NOT read observations or
estimates, does NOT query any repository, performs no writes or
persistence, performs no wall-clock read, involves no provider, model,
ML, or AI, and introduces no float or Decimal semantics anywhere.

**Effective-factor policy (authoritative, deliberate, documented):**

For one ``portfolio/project/entity_type`` scope::

    eligible effective candidates
        = all supplied valid V1.16 records where decision == ACCEPT

    current effective accepted calibration
        = max(eligible candidates,
              key=(decided_at chronological instant, decision_id.int))

Consequences:

* no ACCEPT history for a type  -> that type emits NO effective factor;
* exactly one ACCEPT            -> that exact accepted snapshot is the
  effective factor;
* multiple ACCEPTs              -> the latest by chronological instant
  wins; equal instants are broken deterministically by the larger
  ``decision_id`` UUID integer;
* a later REJECT or DEFER does NOT revoke an earlier ACCEPT: the V1.16
  vocabulary defines decisions about EXACT proposal snapshots, and V1.16
  deliberately contains no revoke/supersede semantics; treating
  "latest decision of any kind" as authority would invent revocation
  semantics that do not exist;
* a later ACCEPT supersedes an earlier ACCEPT ONLY for the derived
  current-effective selection; the historical V1.16 records remain
  immutable, complete, and queryable;
* explicit revocation/deactivation is a separate future architecture
  decision and is deliberately out of scope for V1.17.

**Input integrity / hostile-model defense:**

Every supplied item must be a genuine
:class:`EffortCalibrationFactorDecision` instance and is FRESHLY
revalidated with strict Pydantic revalidation
``model_validate(value.model_dump(mode="python"), strict=True)`` so that
instances bypassing construction validation via ``model_construct()`` are
defeated. ALL supplied records — including REJECT and DEFER records —
must be valid before any selection happens: an invalid record makes the
whole resolution fail, it is never silently skipped. Records whose
``portfolio_id`` or ``project_id`` differs from the requested scope are
rejected, and duplicate ``decision_id`` values fail visibly rather than
being silently deduplicated.

**Ordering:**

Candidate selection compares aware ``decided_at`` values by their actual
chronological instant (timezone offsets may differ; lexical ISO string
comparison is never used), with the ``decision_id`` UUID integer as the
deterministic tie-break.

Output factor order is deterministic FIRST-APPEARANCE order over the
supplied validated history: each :class:`EntityType` first appears at the
position of its first record of ANY decision kind, and an effective
factor is emitted at that type position only when at least one ACCEPT
exists for the type. No global enum sorting or set/dict randomness is
introduced anywhere.

**Empty semantics:**

An empty history, a history containing only REJECT records, a history
containing only DEFER records, and a mixed REJECT+DEFER history all yield
an empty factor set. This is a valid result, not an error.

**Result vocabulary:**

The result models (:class:`EffectiveEffortCalibrationFactor` and
:class:`EffectiveEffortCalibrationFactorSet`) are strict, frozen, and
self-auditing: they retain the exact accepted V1.16 snapshot evidence
(sample count, minimum required sample count, exact planned/actual
totals, and the exact reduced integer factor pair) together with the
exact ``decision_id`` and ``decided_at`` of the selected ACCEPT record.
The factor is preserved as exact reduced integers and is never
represented as a float or Decimal. V1.17 is DERIVED state recomputed from
the immutable V1.16 history on demand; nothing here is persisted,
cached, or materialized.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_factor_decisions import (
    EffortCalibrationDecision,
    EffortCalibrationFactorDecision,
)

__all__ = [
    "EffectiveEffortCalibrationFactor",
    "EffectiveEffortCalibrationFactorError",
    "EffectiveEffortCalibrationFactorSet",
    "resolve_effective_effort_calibration_factors",
]


class EffectiveEffortCalibrationFactorError(ValueError):
    """Raised when V1.17 effective-factor resolution fails."""


class EffectiveEffortCalibrationFactor(BaseModel):
    """One currently effective accepted calibration factor (V1.17).

    The value is copied EXACTLY from one valid V1.16 ACCEPT record:

    * ``decision_id`` and ``decided_at`` identify the exact human
      decision record that is currently effective;
    * ``sample_count``, ``minimum_required_sample_count``, and the exact
      planned/actual totals are the exact V1.16 snapshot evidence of that
      accepted proposal;
    * ``factor_numerator`` / ``factor_denominator`` are the exact
      reduced integer components of the accepted factor.

    Exact invariants (each instance freshly validates them; hostile
    ``model_construct()`` bypass is defeated):

    * ``decided_at`` is timezone-aware;
    * ``factor_numerator`` is a strict integer ``>= 0``;
    * ``factor_denominator`` is a strict integer ``>= 1``;
    * ``gcd(factor_numerator, factor_denominator) == 1``;
    * ``factor_numerator * total_planned_duration_seconds
      == factor_denominator * total_actual_duration_seconds``;
    * ``total_planned_duration_seconds >= 1``;
    * ``sample_count >= 1``, ``minimum_required_sample_count >= 1``, and
      ``sample_count >= minimum_required_sample_count``.

    No float, Decimal, uncertainty, confidence, or rounding semantics are
    present. REJECT/DEFER semantics are deliberately absent: those
    records are not effective-factor candidates and the original V1.16
    history remains the audit source for them.
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

    @model_validator(mode="after")
    def _validate_effective_factor(
        self,
    ) -> EffectiveEffortCalibrationFactor:
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

        return self


class EffectiveEffortCalibrationFactorSet(BaseModel):
    """Immutable effective accepted calibration factor set (V1.17).

    ``factors`` is a tuple in DETERMINISTIC FIRST-APPEARANCE order over
    the supplied validated V1.16 history: each :class:`EntityType` first
    appears at the position of its first record of any decision kind, and
    a factor is emitted at that type position only when at least one
    ACCEPT exists for the type. At most ONE factor is ever present per
    entity type; types without any ACCEPT are simply absent.

    An empty ``factors`` tuple is a valid, non-error result for an empty
    history or a history containing only REJECT and/or DEFER records.

    The set is derived state recomputed from the immutable V1.16 history
    on demand; it is never persisted, cached, or materialized.
    """

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        str_strip_whitespace=False,
    )

    portfolio_id: UUID
    project_id: UUID
    factors: tuple[EffectiveEffortCalibrationFactor, ...]

    @model_validator(mode="after")
    def _validate_at_most_one_per_type(
        self,
    ) -> EffectiveEffortCalibrationFactorSet:
        entity_types = [factor.entity_type for factor in self.factors]
        if len(set(entity_types)) != len(entity_types):
            raise ValueError("at most one effective factor may be present per entity type")

        return self


def _require_genuine_decision(raw: object) -> EffortCalibrationFactorDecision:
    """Require a genuine V1.16 record and freshly revalidate it strictly.

    The caller may supply a hostile instance that bypassed construction
    validation (e.g. via ``model_construct()``), so genuine revalidation
    is mandatory: the record is rebuilt from ordinary Python data through
    strict Pydantic validation, which re-runs every model-level invariant
    (exact reduced factor, cross-multiplication identity, decision rule,
    aware timestamp).

    Raises :class:`EffectiveEffortCalibrationFactorError` on any failure.
    """
    if not isinstance(raw, EffortCalibrationFactorDecision):
        raise EffectiveEffortCalibrationFactorError(
            "every supplied record must be a genuine V1.16 "
            "EffortCalibrationFactorDecision instance; "
            f"got {type(raw).__name__}"
        )

    try:
        return EffortCalibrationFactorDecision.model_validate(
            raw.model_dump(mode="python"), strict=True
        )
    except ValueError as exc:
        raise EffectiveEffortCalibrationFactorError(
            "a supplied V1.16 decision record failed fresh strict "
            f"revalidation and the history is rejected: {exc}"
        ) from exc


def resolve_effective_effort_calibration_factors(
    decisions: Iterable[EffortCalibrationFactorDecision],
    portfolio_id: UUID,
    project_id: UUID,
) -> EffectiveEffortCalibrationFactorSet:
    """Resolve the current effective accepted calibration factors (V1.17).

    Sequence is exactly:

    ``strictly require UUID portfolio_id and project_id (no coercion)
    → for every supplied record (in supplied authoritative order):
        require a genuine EffortCalibrationFactorDecision instance
        → freshly and strictly revalidate it (defeats model_construct)
        → reject any record whose portfolio_id or project_id differs
          from the requested scope
        → reject duplicate decision_id values (no silent dedup)
    → scan the validated history in order to fix the deterministic
    FIRST-APPEARANCE order of represented entity types
    → for each represented type, select max(ACCEPT candidates,
    key=(decided_at chronological instant, decision_id.int)); a type
    with no ACCEPT emits no factor
    → return the immutable effective factor set
    → STOP``.

    The effective-factor policy is exactly:

        eligible candidates  = records where decision == ACCEPT
        effective candidate  = max(candidates,
                                   key=(decided_at, decision_id.int))

    REJECT and DEFER records are valid history but are never candidates:
    a later REJECT or DEFER does NOT revoke an earlier ACCEPT, because
    V1.16 did not define revoke/supersede semantics. A later ACCEPT
    supersedes an earlier ACCEPT only for this derived selection; the
    historical records remain immutable and queryable.

    Empty history, or history containing only REJECT/DEFER records,
    yields an empty ``factors`` tuple — a valid result, not an error.

    The supplied records are never mutated and are never consumed into
    mutable copies. Identical inputs yield identical immutable results.
    No repository, reader, provider, wall-clock, write, float, Decimal,
    or AI is accepted or consulted.
    """
    if not isinstance(portfolio_id, UUID):
        raise EffectiveEffortCalibrationFactorError(
            f"portfolio_id must already be a UUID instance, got {type(portfolio_id).__name__}"
        )
    if not isinstance(project_id, UUID):
        raise EffectiveEffortCalibrationFactorError(
            f"project_id must already be a UUID instance, got {type(project_id).__name__}"
        )

    validated: list[EffortCalibrationFactorDecision] = []
    seen_decision_ids: set[UUID] = set()
    for raw in decisions:
        record = _require_genuine_decision(raw)

        if record.portfolio_id != portfolio_id:
            raise EffectiveEffortCalibrationFactorError(
                "a supplied V1.16 record belongs to portfolio "
                f"{record.portfolio_id}, which differs from the "
                f"requested scope {portfolio_id}; "
                "mixed-scope histories are rejected"
            )
        if record.project_id != project_id:
            raise EffectiveEffortCalibrationFactorError(
                "a supplied V1.16 record belongs to project "
                f"{record.project_id}, which differs from the "
                f"requested scope {project_id}; "
                "mixed-scope histories are rejected"
            )
        if record.decision_id in seen_decision_ids:
            raise EffectiveEffortCalibrationFactorError(
                f"duplicate decision_id {record.decision_id} in the "
                "supplied V1.16 history; duplicates are rejected rather "
                "than silently deduplicated"
            )
        seen_decision_ids.add(record.decision_id)
        validated.append(record)

    ordered_entity_types: list[EntityType] = []
    seen_entity_types: set[EntityType] = set()
    for record in validated:
        entity_type = record.entity_type
        if entity_type not in seen_entity_types:
            seen_entity_types.add(entity_type)
            ordered_entity_types.append(entity_type)

    factors: list[EffectiveEffortCalibrationFactor] = []
    for entity_type in ordered_entity_types:
        candidates = [
            record
            for record in validated
            if record.entity_type == entity_type
            and record.decision is EffortCalibrationDecision.ACCEPT
        ]
        if not candidates:
            # No ACCEPT for this type: no effective factor is emitted.
            # This is not an error and REJECT/DEFER records never revoke.
            continue

        selected = max(candidates, key=lambda rec: (rec.decided_at, rec.decision_id.int))

        factor_numerator = selected.factor_numerator
        factor_denominator = selected.factor_denominator
        if factor_numerator is None or factor_denominator is None:
            # Unreachable for any freshly revalidated ACCEPT record, but
            # the effective factor must retain EXACT integer components
            # and never guesses them.
            raise EffectiveEffortCalibrationFactorError(
                "an ACCEPT record must retain exact integer factor "
                "components in its V1.16 snapshot"
            )

        factors.append(
            EffectiveEffortCalibrationFactor(
                entity_type=selected.entity_type,
                decision_id=selected.decision_id,
                decided_at=selected.decided_at,
                sample_count=selected.sample_count,
                minimum_required_sample_count=selected.minimum_required_sample_count,
                total_planned_duration_seconds=selected.total_planned_duration_seconds,
                total_actual_duration_seconds=selected.total_actual_duration_seconds,
                factor_numerator=factor_numerator,
                factor_denominator=factor_denominator,
            )
        )

    return EffectiveEffortCalibrationFactorSet(
        portfolio_id=portfolio_id,
        project_id=project_id,
        factors=tuple(factors),
    )
