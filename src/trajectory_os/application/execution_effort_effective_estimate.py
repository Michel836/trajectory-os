"""Deterministic current-effective execution-effort estimate resolution (V1.22).

V1.22 is the single read-only boundary that answers exactly one question:

> **For one exact ``(portfolio_id, entity_id)`` scope, which ONE
> ``ExecutionEffortEstimate`` is currently effective, and if that
> estimate was accepted through the V1.21 calibration acceptance
> boundary, what is its exact accepted V1.20 provenance?**

Everything is **derived state, recomputed on demand** from two existing
authorities and nothing else:

* **V1.10 (``ExecutionEffortEstimate`` history)** — the single source of
  truth for effective-estimate candidates: an append-only history of
  validated ``ExecutionEffortEstimate`` objects that may be plain
  (``ExecutionEffortEstimateSource``) or calibrated
  (``USER_CONFIRMED``);
* **V1.21 (``AcceptedCalibratedEstimateRevision`` provenance, optional)**
  — immutable provenance for estimates that were accepted through the
  explicit durable calibration acceptance boundary.

Selection reuses the V1.10 canonical ordering via
``select_latest_execution_effort_estimate`` — the single authoritative
policy: latest ``estimated_at`` chronological instant, greater estimate
UUID int as the deterministic tie-break, ``source``/provenance kind
deliberately irrelevant, insertion order irrelevant. V1.22 reinterprets
neither the ordering nor the estimate semantics.

**What V1.22 deliberately does NOT do:**

* no Portfolio, WBS, binding, factor, application, or duration
  recomputation (V1.17--V1.21 are never re-executed);
* no estimate creation, replacement, deletion, or mutation;
* NO "current" flag, cache, or new table: the current effective
  estimate always comes from re-reading the exact same append-only
  history;
* no Portfolio load: the read is scope-internal to
  ``(portfolio_id, entity_id)`` and therefore survives removal of the
  entity from the current Portfolio, exactly like V1.10/V1.21 history;
* no clock read, no UUID generation, no network, no LLM, no provider,
  no ML: every identity and instant is supplied by the caller/history;
* no writes of ANY kind on ANY path, including failure paths.

The V1.21 provenance lookup is performed AT MOST ONCE, EXACTLY for the
selected estimate id, and ONLY when an estimate is selected:
``NO_ESTIMATE`` performs zero provenance lookups. Provenance existence
never changes the selection outcome — only the returned provenance
field. Calibrated and plain estimates compete on V1.10 ordering alone.

Every returned estimate and provenance is freshly strict-revalidated
(hostile ``model_construct()`` instances are defeated), and the provenance
result is cross-checked against the selected estimate (exact id, exact
portfolio/entity scope, exact calibrated duration, exact instant);
mismatches fail the whole resolution visibly. Invalid, foreign, or
duplicated history entries fail loudly: they are never silently skipped.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from trajectory_os.application.execution_effort_calibration_acceptance import (
    AcceptedCalibratedEstimateRevision,
    CalibratedEstimateRevisionRepository,
)
from trajectory_os.application.execution_effort_planning import (
    ExecutionEffortEstimateReader,
)
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
    select_latest_execution_effort_estimate,
)

__all__ = [
    "EffectiveExecutionEffortEstimate",
    "EffectiveExecutionEffortEstimateError",
    "EffectiveExecutionEffortEstimateHistoryError",
    "EffectiveExecutionEffortEstimateProvenanceError",
    "EffectiveExecutionEffortEstimateStatus",
    "resolve_effective_execution_effort_estimate",
    "resolve_effective_execution_effort_estimate_durably",
]


class EffectiveExecutionEffortEstimateError(ValueError):
    """Base error for the V1.22 current-effective estimate boundary."""


class EffectiveExecutionEffortEstimateHistoryError(
    EffectiveExecutionEffortEstimateError
):
    """Raised when the supplied V1.10 estimate history is invalid.

    This covers non-``ExecutionEffortEstimate`` items, hostile
    ``model_construct()`` instances that fail strict revalidation,
    estimates belonging to a different ``portfolio_id`` or ``entity_id``
    than the requested scope, and duplicate estimate ids within one
    supplied history.
    """


class EffectiveExecutionEffortEstimateProvenanceError(
    EffectiveExecutionEffortEstimateError
):
    """Raised when the optional V1.21 provenance is invalid for the
    selected estimate.

    This covers hostile ``model_construct()`` provenance that fails
    strict revalidation, provenance supplied for a scope with NO selected
    estimate (provenance is never fabricated, and an unselected
    estimate's provenance is never representable), and provenance whose
    ``estimate_id``, portfolio/entity scope, calibrated duration, or
    instant does not match the selected estimate exactly.
    """


class EffectiveExecutionEffortEstimateStatus(StrEnum):
    """Closed result vocabulary for one resolution.

    * ``AVAILABLE`` — exactly one estimate is currently effective for
      the scope;
    * ``NO_ESTIMATE`` — the scope has no estimate history: a valid and
      distinct outcome (no hidden zero, no inference, no default
      provenance).
    """

    AVAILABLE = "available"
    NO_ESTIMATE = "no_estimate"


def _revalidated_estimate(candidate: object) -> ExecutionEffortEstimate:
    """Freshly strict-revalidate one supplied history entry.

    Every item must be a genuine ``ExecutionEffortEstimate`` instance and
    is revalidated with strict Pydantic (``model_construct()`` bypass
    defeated). Field access uses explicit ``getattr`` with ``None``
    defaults so that hostile partially-constructed instances still force
    every field back through normal strict validation.
    """
    if not isinstance(candidate, ExecutionEffortEstimate):
        raise EffectiveExecutionEffortEstimateHistoryError(
            "estimate history must hold only ExecutionEffortEstimate "
            f"instances, got {type(candidate).__name__}"
        )
    payload = {
        "id": getattr(candidate, "id", None),
        "portfolio_id": getattr(candidate, "portfolio_id", None),
        "entity_id": getattr(candidate, "entity_id", None),
        "duration_seconds": getattr(candidate, "duration_seconds", None),
        "estimated_at": getattr(candidate, "estimated_at", None),
        "source": getattr(candidate, "source", None),
    }
    try:
        return ExecutionEffortEstimate.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise EffectiveExecutionEffortEstimateHistoryError(
            "estimate history contains an estimate that fails strict "
            "revalidation"
        ) from exc


def _revalidated_provenance(
    candidate: object,
) -> AcceptedCalibratedEstimateRevision:
    """Freshly strict-revalidate one supplied V1.21 provenance.

    The candidate must be a genuine ``AcceptedCalibratedEstimateRevision``
    instance (whose own after-validator already revalidates the exact
    V1.20 snapshot chain); it is then revalidated end-to-end with strict
    Pydantic so that ``model_construct()`` bypasses are defeated.
    """
    if not isinstance(candidate, AcceptedCalibratedEstimateRevision):
        raise EffectiveExecutionEffortEstimateProvenanceError(
            "calibrated provenance must be an "
            f"AcceptedCalibratedEstimateRevision instance, got "
            f"{type(candidate).__name__}"
        )
    try:
        return AcceptedCalibratedEstimateRevision.model_validate(
            candidate.model_dump(mode="python"), strict=True
        )
    except ValidationError as exc:
        raise EffectiveExecutionEffortEstimateProvenanceError(
            "calibrated provenance fails strict revalidation"
        ) from exc


class EffectiveExecutionEffortEstimate(BaseModel):
    """The single current-effective estimate result for one scope.

    ``AVAILABLE`` carries EXACTLY ONE estimate — the exact V1.10
    selected object (freshly revalidated), plus the exact V1.21 provenance
    when — and only when — that selected estimate was accepted through
    the V1.21 acceptance boundary. ``NO_ESTIMATE`` carries no estimate
    and no provenance.

    The model is strict, frozen, and self-auditing: on construction it
    revalidates the carried estimate/provenance, enforces that the carried
    estimate belongs to EXACTLY the requested result scope, and enforces
    the V1.21 identity invariants (exact estimate id, exact
    portfolio/entity scope, exact calibrated duration, exact
    ``estimated_at`` instant).
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    portfolio_id: UUID
    entity_id: UUID
    status: EffectiveExecutionEffortEstimateStatus
    estimate: ExecutionEffortEstimate | None = None
    calibrated_provenance: AcceptedCalibratedEstimateRevision | None = None

    @model_validator(mode="after")
    def _enforce_effective_estimate_consistency(
        self,
    ) -> EffectiveExecutionEffortEstimate:
        if self.status is EffectiveExecutionEffortEstimateStatus.AVAILABLE:
            if self.estimate is None:
                raise ValueError(
                    "AVAILABLE result requires exactly one estimate"
                )
            estimate = _revalidated_estimate(self.estimate)
            if estimate.portfolio_id != self.portfolio_id:
                raise ValueError(
                    "AVAILABLE estimate belongs to a different portfolio "
                    "than the requested scope: "
                    f"estimate -> {estimate.portfolio_id}, "
                    f"scope -> {self.portfolio_id}"
                )
            if estimate.entity_id != self.entity_id:
                raise ValueError(
                    "AVAILABLE estimate belongs to a different entity "
                    "than the requested scope: "
                    f"estimate -> {estimate.entity_id}, "
                    f"scope -> {self.entity_id}"
                )
        else:
            if self.estimate is not None:
                raise ValueError(
                    "NO_ESTIMATE result must not carry an estimate"
                )
            if self.calibrated_provenance is not None:
                raise ValueError(
                    "NO_ESTIMATE result must not carry calibrated provenance"
                )
            return self

        provenance: AcceptedCalibratedEstimateRevision | None = None
        if self.calibrated_provenance is not None:
            provenance = _revalidated_provenance(self.calibrated_provenance)
            if provenance.estimate_id != estimate.id:
                raise ValueError(
                    "calibrated provenance must reference the exact "
                    "selected estimate: "
                    f"provenance -> {provenance.estimate_id}, "
                    f"estimate -> {estimate.id}"
                )
            if provenance.portfolio_id != estimate.portfolio_id:
                raise ValueError(
                    "calibrated provenance portfolio does not match "
                    "the selected estimate"
                )
            if provenance.entity_id != estimate.entity_id:
                raise ValueError(
                    "calibrated provenance entity does not match "
                    "the selected estimate"
                )
            if (
                provenance.calibrated_duration_seconds
                != estimate.duration_seconds
            ):
                raise ValueError(
                    "calibrated provenance duration does not match "
                    "the selected estimate: "
                    f"{provenance.calibrated_duration_seconds} != "
                    f"{estimate.duration_seconds}"
                )
            if provenance.estimated_at != estimate.estimated_at:
                raise ValueError(
                    "calibrated provenance instant does not match "
                    "the selected estimate"
                )

        return self


def _require_strict_scope_ids(
    portfolio_id: object, entity_id: object
) -> tuple[UUID, UUID]:
    """Strictly validate the requested scope BEFORE any other work.

    Both values must already be genuine ``UUID`` instances: no string
    parsing, no bytes, no coercion, no guessing — a bad scope fails
    before ANY repository access.
    """
    if not isinstance(portfolio_id, UUID):
        raise EffectiveExecutionEffortEstimateError(
            "portfolio_id must already be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )
    if not isinstance(entity_id, UUID):
        raise EffectiveExecutionEffortEstimateError(
            "entity_id must already be a UUID instance, "
            f"got {type(entity_id).__name__}"
        )
    return portfolio_id, entity_id


def _validated_and_selected(
    portfolio_id: UUID,
    entity_id: UUID,
    estimates: Iterable[ExecutionEffortEstimate],
) -> tuple[list[ExecutionEffortEstimate], ExecutionEffortEstimate | None]:
    """Freshly validate the WHOLE supplied history, then select.

    ALL entries are validated before any selection happens; an invalid,
    foreign, or duplicated entry fails the whole resolution (never
    silently skipped). Selection then delegates to the canonical V1.10
    policy. Returns ``(validated, selected)``.
    """
    validated: list[ExecutionEffortEstimate] = []
    seen_estimate_ids: set[UUID] = set()
    for candidate in estimates:
        estimate = _revalidated_estimate(candidate)
        if estimate.portfolio_id != portfolio_id:
            raise EffectiveExecutionEffortEstimateHistoryError(
                "estimate belongs to a different portfolio: "
                f"{estimate.id} -> {estimate.portfolio_id}"
            )
        if estimate.entity_id != entity_id:
            raise EffectiveExecutionEffortEstimateHistoryError(
                "estimate belongs to a different entity: "
                f"{estimate.id} -> {estimate.entity_id}"
            )
        if estimate.id in seen_estimate_ids:
            raise EffectiveExecutionEffortEstimateHistoryError(
                f"duplicate estimate id in supplied history: {estimate.id}"
            )
        seen_estimate_ids.add(estimate.id)
        validated.append(estimate)

    selected = select_latest_execution_effort_estimate(validated)
    return validated, selected


def _check_provenance(
    provenance: AcceptedCalibratedEstimateRevision,
    selected: ExecutionEffortEstimate,
) -> None:
    """Cross-check the selected estimate's provenance for exact identity.

    The provenance must refer to EXACTLY the selected estimate, in the
    SAME portfolio/entity scope, with the EXACT calibrated duration and
    EXACT ``estimated_at`` instant. Any mismatch fails loudly with the
    precise mismatch.
    """
    if provenance.estimate_id != selected.id:
        raise EffectiveExecutionEffortEstimateProvenanceError(
            "calibrated provenance must reference the exact selected "
            f"estimate: provenance -> {provenance.estimate_id}, "
            f"selected -> {selected.id}"
        )
    if provenance.portfolio_id != selected.portfolio_id:
        raise EffectiveExecutionEffortEstimateProvenanceError(
            "calibrated provenance portfolio does not match "
            f"the selected estimate: {provenance.portfolio_id} != "
            f"{selected.portfolio_id}"
        )
    if provenance.entity_id != selected.entity_id:
        raise EffectiveExecutionEffortEstimateProvenanceError(
            "calibrated provenance entity does not match "
            f"the selected estimate: {provenance.entity_id} != "
            f"{selected.entity_id}"
        )
    if provenance.calibrated_duration_seconds != selected.duration_seconds:
        raise EffectiveExecutionEffortEstimateProvenanceError(
            "calibrated provenance duration does not match "
            f"the selected estimate: "
            f"{provenance.calibrated_duration_seconds} != "
            f"{selected.duration_seconds}"
        )
    if provenance.estimated_at != selected.estimated_at:
        raise EffectiveExecutionEffortEstimateProvenanceError(
            "calibrated provenance instant does not match "
            "the selected estimate"
        )


def resolve_effective_execution_effort_estimate(
    portfolio_id: UUID,
    entity_id: UUID,
    estimates: Iterable[ExecutionEffortEstimate],
    calibrated_provenance: AcceptedCalibratedEstimateRevision | None = None,
) -> EffectiveExecutionEffortEstimate:
    """Resolve the current-effective estimate for one exact scope.

    Pure and read-only:

    1. strictly validate the requested ``portfolio_id``/``entity_id``
       (UUID instances, no coercion);
    2. freshly strict-revalidate EVERY supplied history entry, reject
       foreign-scope and duplicate entries;
    3. select the current effective estimate with the canonical V1.10
       policy (latest ``(estimated_at, estimate_id.int)``);
    4. ``NO_ESTIMATE`` when the history is empty — provenance may not
       exist in that case and is rejected if supplied;
    5. otherwise optionally revalidate the V1.21 provenance of the
       selected estimate and enforce the exact identity invariants.

    Returns the frozen :class:`EffectiveExecutionEffortEstimate` result
    (``AVAILABLE`` + exact estimate [+ exact provenance] or
    ``NO_ESTIMATE``).
    """
    portfolio_id, entity_id = _require_strict_scope_ids(
        portfolio_id, entity_id
    )

    _, selected = _validated_and_selected(portfolio_id, entity_id, estimates)

    if selected is None:
        if calibrated_provenance is not None:
            raise EffectiveExecutionEffortEstimateProvenanceError(
                "calibrated provenance cannot be supplied for a scope "
                "with NO selected estimate"
            )
        return EffectiveExecutionEffortEstimate(
            portfolio_id=portfolio_id,
            entity_id=entity_id,
            status=EffectiveExecutionEffortEstimateStatus.NO_ESTIMATE,
        )

    provenance: AcceptedCalibratedEstimateRevision | None = None
    if calibrated_provenance is not None:
        provenance = _revalidated_provenance(calibrated_provenance)
        _check_provenance(provenance, selected)

    return EffectiveExecutionEffortEstimate(
        portfolio_id=portfolio_id,
        entity_id=entity_id,
        status=EffectiveExecutionEffortEstimateStatus.AVAILABLE,
        estimate=selected,
        calibrated_provenance=provenance,
    )


def resolve_effective_execution_effort_estimate_durably(
    portfolio_id: UUID,
    entity_id: UUID,
    estimate_reader: ExecutionEffortEstimateReader,
    calibrated_revision_repository: CalibratedEstimateRevisionRepository,
) -> EffectiveExecutionEffortEstimate:
    """V1.22 durable read-only boundary for the current-effective
    estimate of one exact ``(portfolio_id, entity_id)`` scope.

    Read path, and the ONLY path executed (never a mutation):

    1. strictly validate the scope UUIDs (BEFORE any repository
       access);
    2. read the EXACT portfolio+entity estimate history through the
       V1.10-E read port (``list_for_entity``) and materialize it;
    3. freshly strict-revalidate EVERY history entry and select the
       current effective estimate with the canonical V1.10 policy
       (latest ``estimated_at`` chronological instant, greater estimate
       UUID int as the deterministic tie-break; plain/calibrated
       ordering irrelevant; insertion order irrelevant);
    4. ``NO_ESTIMATE`` (valid result) when the history is empty — with
       EXACTLY ZERO V1.21 provenance lookups;
    5. otherwise perform EXACTLY ONE V1.21 ``get_provenance`` lookup —
       for the SELECTED estimate id only — and enforce the exact V1.21
       identity invariants before returning.

    No Portfolio load, no WBS/binding/factor/duration recomputation, no
    clock read, no UUID generation, no AI/ML, and no write on ANY path
    (fakes in the tests hold no write method at all). Calibrated
    estimates are NEVER prioritized over a later plain estimate:
    provenance changes only the returned provenance field.
    """
    portfolio_id, entity_id = _require_strict_scope_ids(
        portfolio_id, entity_id
    )

    history = list(estimate_reader.list_for_entity(portfolio_id, entity_id))

    _, selected = _validated_and_selected(portfolio_id, entity_id, history)

    if selected is None:
        return EffectiveExecutionEffortEstimate(
            portfolio_id=portfolio_id,
            entity_id=entity_id,
            status=EffectiveExecutionEffortEstimateStatus.NO_ESTIMATE,
        )

    provenance = calibrated_revision_repository.get_provenance(selected.id)
    return resolve_effective_execution_effort_estimate(
        portfolio_id=portfolio_id,
        entity_id=entity_id,
        estimates=history,
        calibrated_provenance=provenance,
    )
