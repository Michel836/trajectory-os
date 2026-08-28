"""Read-only CURRENT-WBS entity binding + revision proposal (V1.20).

Given ONE explicit immutable V1.19 :class:`EffectiveCalibrationApplicationResult`
and ONE explicit ``entity_id``, V1.20 answers exactly one question:

> **Does this exact entity belong to the exact current project/WBS scope
> represented by this V1.19 result, does its EntityType match, and if the
> V1.19 result is AVAILABLE, what exact calibrated duration would be
> eligible for later human persistence as a new estimate revision?**

V1.20 performs NO write. It never creates, persists, replaces, or mutates
an :class:`~trajectory_os.domain.execution_effort_estimates.ExecutionEffortEstimate`,
never generates a UUID, never reads the wall clock, never calls AI or a
provider, and never re-runs V1.17 resolution or V1.18 arithmetic. The
supplied V1.19 result is an explicit immutable calibration snapshot; its
nested V1.18 :class:`CalibratedEffortProposal` remains the single
authoritative provenance (NOT flattened, NOT duplicated, NOT recomputed).

Architecture principle (unchanged):

    AI proposes.
    Deterministic code validates.
    Human decides.
    Persistence records the accepted change.

**Human-decision boundary:**

The :class:`CalibratedEstimateRevisionProposal` is a REVIEWABLE PROPOSAL
only. ``READY`` means only that the exact calibrated result is validly
bound to the exact current entity and is eligible to be PRESENTED for an
explicit human persistence decision. It does NOT imply human acceptance;
the persistence action is V1.21, not V1.20.

**Fresh V1.19 integrity (hostile-model defense):**

The supplied result must be a genuine
:class:`EffectiveCalibrationApplicationResult` instance and is FRESHLY
revalidated from ordinary Python data (``model_dump(mode="python")``
followed by strict Pydantic validation) before it is trusted. A
``model_construct()`` bypass is defeated, and a hostile nested V1.18
proposal is defeated through V1.19's / V1.18's own fresh-validation
invariants re-run on the dumped data.

**CURRENT-WBS scope validation (authoritative reuse):**

After the portfolio is loaded, the EXACT entity must:

* exist in the CURRENT loaded portfolio;
* have ``entity.entity_type == result.entity_type`` EXACTLY (no
  coercion, inference, nearest-match, or broader/narrower type fallback);
* belong to the CURRENT work breakdown rooted at ``result.project_id``
  as projected by the existing authoritative V1.1
  :func:`~trajectory_os.domain.work_breakdown.build_work_breakdown`
  helper — the same helper the V1.9 measurement and V1.10 planning layers
  already treat as the sole authority for CURRENT WBS membership,
  grammar, ambiguity, and cycle detection. No second traversal or WBS
  grammar is implemented here.

If the project is absent or not a valid CURRENT project/WBS anchor, the
authoritative :class:`WorkBreakdownError` from
``build_work_breakdown`` propagates unchanged — a parallel interpretation
is never invented. Cross-project, same-type entities fail explicitly.

**NO_EFFECTIVE_FACTOR semantics remain intact:**

A V1.19 ``NO_EFFECTIVE_FACTOR`` result is VALID input and is retained
explicitly with status NO_EFFECTIVE_FACTOR and ``calibrated_duration_seconds``
absent. It is valid as a binding result but NOT READY and NOT persistable.
The candidate duration is NEVER fabricated into a calibrated duration,
never converted to an identity ``1/1`` factor, and never raised as an
exception.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from trajectory_os.application.execution_effort_calibration_composition import (
    EffectiveCalibrationApplicationResult,
    EffectiveCalibrationApplicationStatus,
)
from trajectory_os.application.work_breakdown_acceptance import PortfolioRepository
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.work_breakdown import (
    WorkBreakdownNode,
    build_work_breakdown,
)

__all__ = [
    "CalibratedEstimateRevisionBindingError",
    "CalibratedEstimateRevisionEntityNotFoundError",
    "CalibratedEstimateRevisionEntityTypeMismatchError",
    "CalibratedEstimateRevisionEntityOutOfCurrentWbsError",
    "CalibratedEstimateRevisionPortfolioNotFoundError",
    "CalibratedEstimateRevisionProposal",
    "CalibratedEstimateRevisionProposalStatus",
    "bind_effort_calibration_to_current_entity",
]


class CalibratedEstimateRevisionBindingError(ValueError):
    """Raised when a V1.20 read-only entity binding fails on invalid input."""


class CalibratedEstimateRevisionPortfolioNotFoundError(CalibratedEstimateRevisionBindingError):
    """Raised when the CURRENT portfolio for the binding is absent."""


class CalibratedEstimateRevisionEntityNotFoundError(CalibratedEstimateRevisionBindingError):
    """Raised when the target entity is absent from the CURRENT portfolio."""


class CalibratedEstimateRevisionEntityTypeMismatchError(CalibratedEstimateRevisionBindingError):
    """Raised when the target entity type differs from the V1.19 entity type."""


class CalibratedEstimateRevisionEntityOutOfCurrentWbsError(CalibratedEstimateRevisionBindingError):
    """Raised when the target entity is outside the CURRENT project WBS."""


class CalibratedEstimateRevisionProposalStatus(StrEnum):
    """Exact, closed V1.20 revision-proposal status vocabulary.

    * READY — the source V1.19 result is AVAILABLE and the exact entity
      binding against the CURRENT portfolio/WBS succeeded; the exact
      V1.18 calibrated duration is present and eligible to be presented
      for an EXPLICIT human persistence decision (V1.21). ``READY`` does
      NOT imply human acceptance.
    * NO_EFFECTIVE_FACTOR — the source V1.19 result had no effective
      accepted factor: the entity binding was still validated, but no
      calibrated duration exists and the proposal is NOT persistable.
    """

    READY = "ready"
    NO_EFFECTIVE_FACTOR = "no_effective_factor"


class CalibratedEstimateRevisionProposal(BaseModel):
    """Immutable, self-validating V1.20 human-reviewable revision proposal.

    Retains the exact V1.19 scope (``portfolio_id``, ``project_id``,
    ``entity_type``, ``candidate_duration_seconds``), the exact bound
    ``entity_id``, the exact status, and — only on the READY path — the
    EXACT V1.18 ``calibrated_duration_seconds``. The authoritative
    provenance is the nested ``source_result`` (the exact V1.19 snapshot,
    with its exact V1.18 proposal); V1.18 arithmetic evidence is
    deliberately NOT duplicated here.

    Cross-field invariants (freshly validated on EVERY construction; a
    hostile ``model_construct()`` bypass is defeated):

    * ``portfolio_id == source_result.portfolio_id``;
    * ``project_id == source_result.project_id``;
    * ``entity_type == source_result.entity_type``;
    * ``candidate_duration_seconds
      == source_result.candidate_duration_seconds``;
    * ``status == READY``      <=> ``source_result.status == AVAILABLE``
      <=> ``source_result.proposal`` is present
      <=> ``calibrated_duration_seconds`` is present;
    * ``status == NO_EFFECTIVE_FACTOR``
      <=> ``source_result.status == NO_EFFECTIVE_FACTOR``
      <=> ``source_result.proposal`` is absent
      <=> ``calibrated_duration_seconds`` is absent;
    * when READY, ``calibrated_duration_seconds
      == source_result.proposal.calibrated_duration_seconds``
      (the exact V1.18 output, never recomputed);
    * the carried V1.19 snapshot remains genuine and freshly valid,
      including its nested V1.18 proposal invariants.
    """

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        str_strip_whitespace=False,
    )

    portfolio_id: UUID
    project_id: UUID
    entity_id: UUID
    entity_type: EntityType
    candidate_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    status: CalibratedEstimateRevisionProposalStatus
    calibrated_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None
    source_result: EffectiveCalibrationApplicationResult

    @model_validator(mode="after")
    def _validate_source_binding(self) -> CalibratedEstimateRevisionProposal:
        source = self.source_result

        # A same-model instance is not itself proof that the carried V1.19
        # snapshot was validly constructed: model_construct() can bypass
        # its invariants (including a hostile nested V1.18 proposal).
        # Rebuild it from ordinary Python data through strict validation so
        # V1.20 can never carry forged calibration provenance.
        EffectiveCalibrationApplicationResult.model_validate(
            source.model_dump(mode="python"), strict=True
        )

        if self.portfolio_id != source.portfolio_id:
            raise ValueError(
                "the wrapper portfolio "
                f"({self.portfolio_id}) differs from the V1.19 snapshot "
                f"portfolio ({source.portfolio_id}); the binding must be "
                "exactly the one represented by the source result"
            )
        if self.project_id != source.project_id:
            raise ValueError(
                "the wrapper project "
                f"({self.project_id}) differs from the V1.19 snapshot "
                f"project ({source.project_id}); the binding must be "
                "exactly the one represented by the source result"
            )
        if self.entity_type is not source.entity_type:
            raise ValueError(
                "the wrapper entity type "
                f"({self.entity_type!s}) differs from the V1.19 snapshot "
                f"type ({source.entity_type!s}); exact-type binding is "
                "mandatory"
            )
        if self.candidate_duration_seconds != source.candidate_duration_seconds:
            raise ValueError(
                "the wrapper candidate "
                f"({self.candidate_duration_seconds}) differs from the "
                "V1.19 snapshot candidate "
                f"({source.candidate_duration_seconds}); the binding must "
                "carry the exact calibrated candidate"
            )

        source_status = source.status
        proposal = source.proposal

        if self.status is CalibratedEstimateRevisionProposalStatus.READY:
            if source_status is not EffectiveCalibrationApplicationStatus.AVAILABLE:
                raise ValueError(
                    "status READY requires the V1.19 source result to be "
                    f"AVAILABLE (got {source_status!s})"
                )
            if proposal is None:
                raise ValueError(
                    "status READY requires the exact V1.18 proposal to be "
                    "present in the V1.19 source result"
                )
            if self.calibrated_duration_seconds is None:
                raise ValueError(
                    "status READY requires the exact V1.18 calibrated "
                    "duration to be present"
                )
            if (
                self.calibrated_duration_seconds
                != proposal.calibrated_duration_seconds
            ):
                raise ValueError(
                    "the calibrated duration "
                    f"({self.calibrated_duration_seconds}) must equal the "
                    "exact V1.18 calibrated output "
                    f"({proposal.calibrated_duration_seconds}); it is "
                    "carried, never recomputed"
                )
        else:
            if source_status is not EffectiveCalibrationApplicationStatus.NO_EFFECTIVE_FACTOR:
                raise ValueError(
                    "status NO_EFFECTIVE_FACTOR requires the V1.19 source "
                    f"result to be NO_EFFECTIVE_FACTOR (got {source_status!s})"
                )
            if proposal is not None:
                raise ValueError(
                    "status NO_EFFECTIVE_FACTOR forbids a calibrated "
                    "proposal in the V1.19 source result; nothing may be "
                    "fabricated when no effective factor exists"
                )
            if self.calibrated_duration_seconds is not None:
                raise ValueError(
                    "status NO_EFFECTIVE_FACTOR forbids any calibrated "
                    "duration; the candidate is never an identity fallback "
                    "or an unchanged calibrated output"
                )

        return self


def _require_genuine_v119_result(
    raw: object,
) -> EffectiveCalibrationApplicationResult:
    """Require a genuine V1.19 result and freshly revalidate it strictly.

    The caller may supply a hostile instance that bypassed construction
    validation (e.g. via ``model_construct()``), so merely observing an
    instance of the right class is never trusted: the result is rebuilt
    from ordinary Python data (``model_dump(mode="python")``) through
    strict Pydantic validation, which re-runs every model-level invariant
    of V1.19 — and, through V1.19's own invariants, every arithmetic
    invariant of the nested V1.18 proposal. The revalidated instance
    carries the same values; the supplied instance is never modified.

    Raises :class:`CalibratedEstimateRevisionBindingError` on any failure.
    """
    if not isinstance(raw, EffectiveCalibrationApplicationResult):
        raise CalibratedEstimateRevisionBindingError(
            "result must be a genuine V1.19 "
            "EffectiveCalibrationApplicationResult instance; "
            f"got {type(raw).__name__}"
        )

    try:
        dumped = raw.model_dump(mode="python")
        return EffectiveCalibrationApplicationResult.model_validate(
            dumped, strict=True
        )
    except Exception as exc:  # noqa: BLE001 - surface every bypass cleanly
        raise CalibratedEstimateRevisionBindingError(
            "the supplied V1.19 EffectiveCalibrationApplicationResult "
            f"failed fresh strict revalidation and is rejected: {exc}"
        ) from exc


def _collect_wbs_entity_ids(root: WorkBreakdownNode) -> set[UUID]:
    """Collect the entity ids of a ``build_work_breakdown`` projection.

    This is a simple walk over the EXISTING authoritative V1.1 WBS
    structure (the same ``WorkBreakdownNode`` tree the V1.9 measurement
    and V1.10 planning layers flatten for membership); it implements no
    traversal grammar of its own. The project root is a WBS member of its
    own work breakdown, consistent with those layers.
    """
    ids: set[UUID] = set()
    stack: list[WorkBreakdownNode] = [root]
    while stack:
        node = stack.pop()
        ids.add(node.entity_id)
        stack.extend(node.children)
    return ids


def bind_effort_calibration_to_current_entity(
    result: EffectiveCalibrationApplicationResult,
    entity_id: UUID,
    portfolio_repository: PortfolioRepository,
) -> CalibratedEstimateRevisionProposal:
    """Bind a V1.19 result to one exact CURRENT-WBS entity (V1.20).

    Sequence is exactly:

    ``strictly require entity_id as a UUID instance (no coercion)
    → require a genuine EffectiveCalibrationApplicationResult instance
      and freshly and strictly revalidate it (defeats model_construct,
      including a hostile nested V1.18 proposal) — ALL of this BEFORE
      any repository access
    → current = portfolio_repository.load(result.portfolio_id)  # ONCE
    → a missing portfolio fails with
      CalibratedEstimateRevisionPortfolioNotFoundError
    → the exact entity must exist in the CURRENT portfolio
    → entity.entity_type must equal result.entity_type EXACTLY
    → structure = build_work_breakdown(current, result.project_id)  # V1.1,
      authoritative: an absent/invalid project anchor fails with the
      existing WorkBreakdownError, unchanged
    → the entity must be a member of that CURRENT WBS (the project root
      included, consistent with V1.9/V1.10 semantics); a same-type entity
      of ANY other project fails explicitly
    → status = READY with the EXACT
      result.proposal.calibrated_duration_seconds when the source result
      is AVAILABLE
             NO_EFFECTIVE_FACTOR with no calibrated duration when the
      source result is NO_EFFECTIVE_FACTOR (still not persistable)
    → return the immutable self-validating
      CalibratedEstimateRevisionProposal  # PROPOSAL ONLY, no persistence
    → STOP``.

    Read-only: the repository is consulted ONLY via ``load`` exactly
    once; there is no ``save``, no estimate create/update/delete, no
    calibration decision write, no clock read, no UUID generation, and no
    AI/provider call. The V1.19 snapshot is auditable provenance; V1.17
    is not re-resolved, V1.18 is not re-applied, and no factor fallback,
    blending, or identity ``1/1`` conversion occurs.

    Repository (``load``) failures and the authoritative
    :class:`WorkBreakdownError` from the CURRENT-WBS projection propagate
    unchanged; there are no broad exception catches. Invalid inputs fail
    with :class:`CalibratedEstimateRevisionBindingError` before any
    repository access; inputs are rejected, never repaired.
    """
    if not isinstance(entity_id, UUID):
        raise CalibratedEstimateRevisionBindingError(
            f"entity_id must already be a UUID instance, got {type(entity_id).__name__}"
        )

    validated_result = _require_genuine_v119_result(result)

    current: Portfolio | None = portfolio_repository.load(validated_result.portfolio_id)
    if current is None:
        raise CalibratedEstimateRevisionPortfolioNotFoundError(
            f"portfolio not found: {validated_result.portfolio_id}"
        )

    entity = current.get_entity(entity_id)
    if entity is None:
        raise CalibratedEstimateRevisionEntityNotFoundError(
            f"entity not found in current portfolio {current.id}: {entity_id}"
        )

    if entity.entity_type is not validated_result.entity_type:
        raise CalibratedEstimateRevisionEntityTypeMismatchError(
            f"entity {entity_id} has type {entity.entity_type!s}, which "
            f"differs from the V1.19 entity type "
            f"{validated_result.entity_type!s}; exact-type binding is mandatory"
        )

    # V1.1 remains the sole authority for CURRENT WBS membership, grammar,
    # ambiguity, cycle detection, and sibling ordering. An absent or
    # non-PROJECT anchor fails here with the existing WorkBreakdownError.
    structure = build_work_breakdown(current, validated_result.project_id)
    wbs_ids = _collect_wbs_entity_ids(structure.root)

    if entity_id not in wbs_ids:
        raise CalibratedEstimateRevisionEntityOutOfCurrentWbsError(
            f"entity {entity_id} is not a member of the CURRENT work "
            f"breakdown rooted at project {validated_result.project_id}"
        )

    if (
        validated_result.status is EffectiveCalibrationApplicationStatus.AVAILABLE
        and validated_result.proposal is not None
    ):
        status = CalibratedEstimateRevisionProposalStatus.READY
        calibrated_duration_seconds: int | None = (
            validated_result.proposal.calibrated_duration_seconds
        )
    else:
        status = CalibratedEstimateRevisionProposalStatus.NO_EFFECTIVE_FACTOR
        calibrated_duration_seconds = None

    return CalibratedEstimateRevisionProposal(
        portfolio_id=validated_result.portfolio_id,
        project_id=validated_result.project_id,
        entity_id=entity_id,
        entity_type=validated_result.entity_type,
        candidate_duration_seconds=validated_result.candidate_duration_seconds,
        status=status,
        calibrated_duration_seconds=calibrated_duration_seconds,
        source_result=validated_result,
    )
