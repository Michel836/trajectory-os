"""Explicit human acceptance of a V1.20 calibrated estimate revision (V1.21).

Turning one already-READY V1.20 ``CalibratedEstimateRevisionProposal`` into
durable storage:

- one EXPLICIT, caller-supplied acceptance decision (a separate, dedicated
  public function; the function itself carries no "auto"/"default accept"
  semantics anywhere);
- one V1.10 ``ExecutionEffortEstimate`` created by the EXISTING V1.10-A
  factory ``create_execution_effort_estimate`` (never re-implemented) with
  the EXACT accepted ``calibrated_duration_seconds`` and
  ``SourceKind.USER_CONFIRMED`` (never ``PROVIDER_ESTIMATE`` and never an
  AI/LLM-derived source);
- one NEW immutable "accepted calibrated estimate revision" record (this
  module's ``AcceptedCalibratedEstimateRevision``) that retains the EXACT
  V1.20 provenance chain (V1.20 -> V1.19 -> V1.18) plus the new estimate
  identity and the explicit caller-supplied aware ``estimated_at``;
- one atomic persistence boundary (the structural
  ``CalibratedEstimateRevisionRepository``) where the estimate append and
  the provenance append happen in ONE transaction.

Strict ordering (all failures are raised BEFORE any repository write
transaction is opened):

1. ``proposal`` must be a genuine, fresh, strictly-validated V1.20
   ``CalibratedEstimateRevisionProposal`` (re-validated from
   ``model_dump()`` through ``model_validate(..., strict=True)`` before
   ANY repository interaction and BEFORE the ``status`` check, so a
   ``NO_EFFECTIVE_FACTOR`` payload is rejected for the wrong status, not
   merely for missing arithmetic; a raw dict, a string, a ``None`` value,
   or a tampered ``model_construct()`` instance is rejected with
   :class:`AcceptCalibratedEstimateRevisionError`);
2. ``status`` must be ``READY`` exactly (a ``NO_EFFECTIVE_FACTOR``
   payload is rejected with the dedicated
   :class:`NoEffectiveFactorCannotBeAcceptedError` BEFORE any
   ``PortfolioRepository.load`` or write call);
3. ``estimate_id`` must already be a ``UUID`` instance (no str/int/bytes
   coercion) and ``estimated_at`` must already be an aware
   ``datetime`` (no naive datetime, no str) — both BEFORE any
   repository interaction;
4. bind the exact validated V1.20 proposal against the CURRENT portfolio
   through the EXISTING public V1.20 function
   ``bind_effort_calibration_to_current_entity`` (which freshly
   re-validates the proposal's strict V1.19 -> V1.18 provenance chain and
   enforces the authoritative V1.1 CURRENT-WBS membership rules; the
   CURRENT portfolio is authoritative; stale, moved, renamed, removed,
   cross-portfolio, unknown, or wrongly-typed entities are rejected
   through the REAL V1.20 function and its REAL exceptions; this module
   adds no second WBS traversal, V1.17 re-resolution, or V1.18
   re-application);
5. the rebound proposal must value-equal the validated input proposal
   (the CURRENT portfolio, not the caller's claim, is authoritative);
6. delegate ALL estimate/domain semantics to the existing V1.10-A
   ``create_execution_effort_estimate`` factory (entity/duration
   validation stays authoritative there; this module adds no new duration
   or entity rules);
7. build the new immutable provenance record (V1.21) that captures the
   EXACT accepted V1.20 snapshot and the new estimate identity, then call
   ``revision_repository.add_accepted_revision(estimate, provenance)``
   EXACTLY ONCE with EXACTLY those two domain objects (one atomic
   transaction, never two separate repository calls, never two separate
   commits), and return the exact
   ``AcceptedCalibratedEstimateRevisionResult``.

Boundary rules:

* no new AI, LLM, provider, model, network, task-scheduler, or
  agent-framework boundary is introduced; the only new dependency at this
  boundary is the EXISTING V1.20 function and the V1.10-A factory used
  through composition;
* V1.21 adds NO new estimate domain model: the estimate is a V1.10 value;
  the provenance record type is declared HERE (application layer) so the
  domain estimate module is untouched;
* no clock is introduced: ``estimated_at`` is caller-supplied (aware,
  tz preserved, never re-normalized to UTC, never defaulted);
* no ``uuid4()`` is introduced: ``estimate_id`` is caller-supplied;
* the loaded portfolio is never mutated or saved; this module's success
  state leaves the V1.6 portfolio untouched (entity snapshot replacement
  and history append stay independent, per V1.20);
* no ``NO_ACTION``/`NO_OP` semantic: a successful acceptance always means
  exactly one new estimate row and exactly one new provenance row were
  appended atomically; there is no partial or no-op success path;
* no broad exception catches: repository and domain exceptions propagate
  unchanged;
* no auto-acceptance: the sole entry point is the explicit call of this
  function by a human-driven caller.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from trajectory_os.application.execution_effort_calibration_entity_binding import (
    CalibratedEstimateRevisionProposal,
    CalibratedEstimateRevisionProposalStatus,
    bind_effort_calibration_to_current_entity,
)
from trajectory_os.application.work_breakdown_acceptance import PortfolioRepository
from trajectory_os.domain.entities import EntityType, SourceKind
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
    create_execution_effort_estimate,
)
from trajectory_os.domain.portfolio import Portfolio


class AcceptCalibratedEstimateRevisionError(ValueError):
    """Raised when durable calibrated-revision acceptance is structurally invalid.

    Raised for: a payload that is not a genuine fresh
    ``CalibratedEstimateRevisionProposal`` (dict / string / ``None`` /
    tampered ``model_construct()`` / unknown-field payload), a non-UUID
    ``estimate_id``, a non-aware ``estimated_at``, and a rebound proposal
    that differs from the validated input.
    """


class NoEffectiveFactorCannotBeAcceptedError(AcceptCalibratedEstimateRevisionError):
    """Raised when a ``NO_EFFECTIVE_FACTOR`` V1.20 payload is passed for acceptance."""


def _require_genuine_v120_proposal(payload: object) -> CalibratedEstimateRevisionProposal:
    """Strictly rebuild ``payload`` as a fresh, fully validated V1.20 proposal.

    Rejects anything that is not already a
    ``CalibratedEstimateRevisionProposal`` instance, and anything whose
    fields do not survive ``model_validate(..., strict=True)`` of a
    ``model_dump()`` round-trip (defeating ``model_construct()`` skips and
    unknown/stale field sets). Raises
    :class:`AcceptCalibratedEstimateRevisionError` on failure.
    """

    if not isinstance(payload, CalibratedEstimateRevisionProposal):
        raise AcceptCalibratedEstimateRevisionError(
            "proposal must be a genuine CalibratedEstimateRevisionProposal "
            f"instance (V1.20), got {type(payload).__name__}"
        )
    try:
        fresh = CalibratedEstimateRevisionProposal.model_validate(
            payload.model_dump(), strict=True
        )
    except ValidationError as exc:  # noqa: B904 - re-raise as the boundary error
        raise AcceptCalibratedEstimateRevisionError(
            "proposal did not survive strict re-validation as a genuine "
            "V1.20 CalibratedEstimateRevisionProposal"
        ) from exc
    return fresh


class AcceptedCalibratedEstimateRevision(BaseModel):
    """One durable, immutable accepted calibrated estimate revision (V1.21).

    Retains the EXACT accepted V1.20 snapshot (``source_proposal``: the
    rebound READY ``CalibratedEstimateRevisionProposal`` with its nested
    V1.19 ``EffectiveCalibrationApplicationResult`` and V1.18
    ``CalibratedEffortProposal`` provenance chain), the V1.10
    ``ExecutionEffortEstimate`` identity (``estimate_id``) and accepted
    duration (``calibrated_duration_seconds``), the explicit
    caller-supplied aware ``estimated_at`` (the human acceptance
    timestamp of the new V1.10 estimate; V1.20 proposals themselves
    carry no timestamp), and mirrors the core V1.20 identifiers
    (``portfolio_id`` / ``project_id`` / ``entity_id`` /
    ``entity_type`` / ``candidate_duration_seconds``) for direct query
    and corruption visibility. All fields are strict, frozen, and
    cross-field checked: no partial or ambiguous snapshot can be built.

    ``source_proposal`` is deliberately a genuine V1.20 domain object
    (itself strict, frozen, with its after-validator), not a dict, not a
    JSON string, and not a pickle: the provenance chain is reconstructable,
    type-checked, and re-validated on every round-trip through this model
    (field validation at construction time guarantees it).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    estimate_id: UUID
    portfolio_id: UUID
    project_id: UUID
    entity_id: UUID
    entity_type: EntityType
    candidate_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    calibrated_duration_seconds: Annotated[StrictInt, Field(ge=0)]
    estimated_at: datetime
    # The EXACT accepted V1.20 snapshot (V1.20 -> V1.19 -> V1.18 chain).
    # A genuine V1.20 domain object, never a dict/JSON-string/pickle.
    source_proposal: CalibratedEstimateRevisionProposal

    @model_validator(mode="after")
    def _enforce_snapshot_consistency(self) -> AcceptedCalibratedEstimateRevision:
        """Cross-field consistency: the record and its snapshot disagree nowhere.

        Raises ``ValueError`` (surfaced as ``pydantic.ValidationError``)
        if any mirrored field deviates from the EXACT accepted
        ``source_proposal`` snapshot. ``estimated_at`` is intentionally
        NOT cross-checked against the snapshot: V1.20 proposals carry no
        timestamp; it belongs to the new V1.10 estimate (checked at the
        result level against ``estimate.estimated_at``).
        """
        p = self.source_proposal
        checks: list[tuple[object, object]] = [
            (self.portfolio_id, p.portfolio_id),
            (self.project_id, p.project_id),
            (self.entity_id, p.entity_id),
            (self.entity_type, p.entity_type),
            (self.candidate_duration_seconds, p.candidate_duration_seconds),
            (self.calibrated_duration_seconds, p.calibrated_duration_seconds),
        ]
        for ours, theirs in checks:
            if ours != theirs:
                raise ValueError(
                    "accepted revision record and its exact accepted V1.20 "
                    "source_proposal snapshot disagree "
                    f"({ours!r} != {theirs!r})"
                )
        return self


class AcceptedCalibratedEstimateRevisionResult(BaseModel):
    """The exact in-memory outcome of one explicit V1.21 acceptance.

    Strict (``frozen=True, extra='forbid'``) and cross-field validated:
    ``estimate`` and ``provenance`` must agree on the estimate identity,
    the accepted duration, the timestamp, the estimate source
    (``USER_CONFIRMED``), and the proposal status (``READY``). A
    ``NO_ACTION``/no-op field deliberately does not exist: the only
    successful state is "one estimate + one provenance record appended
    atomically".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    estimate: ExecutionEffortEstimate
    provenance: AcceptedCalibratedEstimateRevision

    @model_validator(mode="after")
    def _enforce_estimate_provenance_consistency(
        self,
    ) -> AcceptedCalibratedEstimateRevisionResult:
        est = self.estimate
        prov = self.provenance
        if est.id != prov.estimate_id:
            raise ValueError("estimate.id and provenance.estimate_id disagree")
        if est.portfolio_id != prov.portfolio_id:
            raise ValueError("estimate.portfolio_id and provenance disagree")
        if est.entity_id != prov.entity_id:
            raise ValueError("estimate.entity_id and provenance.entity_id disagree")
        if est.duration_seconds != prov.calibrated_duration_seconds:
            raise ValueError(
                "estimate.duration_seconds must equal the exact accepted "
                "calibrated_duration_seconds"
            )
        if est.estimated_at != prov.estimated_at:
            raise ValueError("estimate.estimated_at and provenance.estimated_at disagree")
        if est.source is not SourceKind.USER_CONFIRMED:
            raise ValueError("estimate.source must be USER_CONFIRMED exactly")
        if prov.source_proposal.status is not CalibratedEstimateRevisionProposalStatus.READY:
            raise ValueError(
                "provenance.source_proposal.status must be READY exactly "
                "(a NO_EFFECTIVE_FACTOR payload is never an accepted revision)"
            )
        return self


class CalibratedEstimateRevisionRepository(Protocol):
    """Structural, technology-agnostic atomic provenance append boundary (V1.21).

    The single write entry point: the V1.10 estimate append AND the V1.21
    accepted-revision provenance append in ONE transaction (one
    commit). Implementations (e.g. the SQLite adapter) own the
    transaction; this protocol exposes no engine, connection, or
    transaction concept to the application, and deliberately exposes no
    two-call ``add_estimate``/``add_provenance`` pair: partial
    persistence state is not representable through this boundary.
    """

    def add_accepted_revision(
        self,
        estimate: ExecutionEffortEstimate,
        provenance: AcceptedCalibratedEstimateRevision,
    ) -> None:
        """Atomically append one estimate and its accepted-revision provenance."""

        ...

    def get_provenance(self, estimate_id: UUID) -> AcceptedCalibratedEstimateRevision | None:
        """Return the stored provenance for the estimate, or ``None`` if absent."""

        ...


def accept_calibrated_estimate_revision_durably(
    proposal: object,
    *,
    estimate_id: UUID,
    estimated_at: datetime,
    portfolio_repository: PortfolioRepository,
    revision_repository: CalibratedEstimateRevisionRepository,
) -> AcceptedCalibratedEstimateRevisionResult:
    """Explicitly accept one READY V1.20 calibrated estimate revision durably.

    The exact sequence is:

    1. ``proposal`` is strictly re-validated as a genuine, fresh V1.20
       ``CalibratedEstimateRevisionProposal`` (``model_construct()``
       skips and dict/string/None payloads are rejected);
    2. ``status`` must be ``READY`` exactly (``NO_EFFECTIVE_FACTOR`` is
       rejected with :class:`NoEffectiveFactorCannotBeAcceptedError)`);
    3. ``estimate_id`` must already be a ``UUID`` instance and
       ``estimated_at`` must already be an aware ``datetime`` (both
       BEFORE any repository access, no coercion);
    4. the exact validated proposal is rebound against the CURRENT
       portfolio through the EXISTING public V1.20 function
       ``bind_effort_calibration_to_current_entity`` (which freshly
       re-validates its strict V1.19 -> V1.18 provenance chain and the
       authoritative V1.1 CURRENT-WBS rules; the rebound proposal must
       value-equal the validated input — the CURRENT state is
       authoritative);
    5. the CURRENT portfolio is loaded and the EXISTING V1.10-A
       ``create_execution_effort_estimate`` factory produces the exact
       V1.10 estimate (``USER_CONFIRMED`` source, exact accepted
       ``calibrated_duration_seconds``);
    6. the new immutable V1.21 ``AcceptedCalibratedEstimateRevision``
       record is built;
    7. ``revision_repository.add_accepted_revision(estimate,
       provenance)`` is called EXACTLY ONCE with EXACTLY those two
       domain objects (one atomic transaction);
    8. the exact ``AcceptedCalibratedEstimateRevisionResult`` is
       returned.

    On ANY step 1–6 failure, no repository write is performed at all;
    step 7's single write is all-or-nothing (the atomic append boundary
    owns the transaction). No step 7/8 partial state exists; there is no
    ``NO_ACTION`` success path; no clock, no ``uuid4()``, no AI, no
    auto-acceptance, no portfolio mutation or save.
    """

    # 1) Genuine, fresh, strictly re-validated V1.20 payload (before any
    #    repository interaction and before the status check, so a
    #    NO_EFFECTIVE_FACTOR payload fails for what it is, not for a
    #    missing arithmetic).
    validated_proposal = _require_genuine_v120_proposal(proposal)

    # 2) READY exactly; NO_EFFECTIVE_FACTOR is rejected before any
    #    repository call (dedicated, precise exception type).
    if validated_proposal.status is not CalibratedEstimateRevisionProposalStatus.READY:
        raise NoEffectiveFactorCannotBeAcceptedError(
            "a NO_EFFECTIVE_FACTOR V1.20 payload cannot be accepted: it "
            "carries no effective calibration factor and no accepted "
            "calibrated duration is available"
        )

    # 3) Explicit identities and timestamps; UUID and aware-datetime
    #    strictness, BEFORE any repository interaction, no coercion.
    if not isinstance(estimate_id, UUID):
        raise AcceptCalibratedEstimateRevisionError(
            "estimate_id must already be a UUID instance, "
            f"got {type(estimate_id).__name__}"
        )
    if not isinstance(estimated_at, datetime) or estimated_at.tzinfo is None:
        raise AcceptCalibratedEstimateRevisionError(
            "estimated_at must already be an aware datetime instance, "
            f"got {type(estimated_at).__name__}"
        )

    # 4) Rebind the exact validated V1.20 proposal against the CURRENT
    #    portfolio through the EXISTING public V1.20 function (this is
    #    the only rebind; V1.17/V1.18/entity-WBS rules are never
    #    re-invented here — the V1.20 function freshly re-validates the
    #    strict V1.19 -> V1.18 provenance chain and enforces the
    #    authoritative V1.1 CURRENT-WBS rules itself). The rebinding
    #    loads the current portfolio (ONCE, inside V1.20).
    rebound = bind_effort_calibration_to_current_entity(
        validated_proposal.source_result,
        validated_proposal.entity_id,
        portfolio_repository,
    )
    if rebound.model_dump() != validated_proposal.model_dump():
        raise AcceptCalibratedEstimateRevisionError(
            "the CURRENT portfolio does not support this exact V1.20 "
            "proposal: the rebound V1.20 proposal differs from the "
            "validated input (current state is authoritative)"
        )

    # READY + exact equivalence already guarantee a non-None calibrated
    # duration (the V1.20 model's own after-validator enforces
    # READY -> calibrated non-None); narrow the type explicitly so the
    # V1.10-A factory receives a fully checked int.
    calibrated = rebound.calibrated_duration_seconds
    if calibrated is None:  # pragma: no cover - the invariant above
        raise AcceptCalibratedEstimateRevisionError(
            "a READY V1.20 rebound must carry an exact accepted "
            "calibrated_duration_seconds"
        )

    # 5) Load the current portfolio and delegate ALL estimate/domain
    #    semantics to the EXISTING V1.10-A factory (entity membership and
    #    duration validation stay authoritative there).
    current: Portfolio | None = portfolio_repository.load(
        validated_proposal.portfolio_id
    )
    if current is None:  # pragma: no cover - the V1.20 rebind already guaranteed this
        raise AcceptCalibratedEstimateRevisionError(
            f"portfolio not found: {validated_proposal.portfolio_id}"
        )
    estimate = create_execution_effort_estimate(
        current,
        estimate_id=estimate_id,
        entity_id=validated_proposal.entity_id,
        duration_seconds=calibrated,
        estimated_at=estimated_at,
    )

    # 6) Build the new immutable V1.21 provenance record (strict,
    #    cross-field checked; the exact accepted V1.20 snapshot is
    #    embedded as a genuine domain object).
    provenance = AcceptedCalibratedEstimateRevision(
        estimate_id=estimate_id,
        portfolio_id=validated_proposal.portfolio_id,
        project_id=validated_proposal.project_id,
        entity_id=validated_proposal.entity_id,
        entity_type=validated_proposal.entity_type,
        candidate_duration_seconds=validated_proposal.candidate_duration_seconds,
        calibrated_duration_seconds=calibrated,
        estimated_at=estimated_at,
        source_proposal=rebound,
    )

    # 7) One atomic append: estimate + provenance in ONE transaction.
    revision_repository.add_accepted_revision(estimate, provenance)

    # 8) Exact result (strict, frozen, cross-field validated).
    return AcceptedCalibratedEstimateRevisionResult(estimate=estimate, provenance=provenance)
