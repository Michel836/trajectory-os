"""V1.46 — explicit executable TASK request from CURRENT applicable
authorization.

The architectural authority chain is:

    explicit human execution
    authorization                     (V1.44 intent)
    ->
    CURRENT applicability preflight   (V1.45 applicability)
    ->
    explicit executable TASK request  (V1.46 request)   <-- this module

V1.46 is the LAST PURE deterministic boundary before any future side
effect. It MUST NOT execute anything.

PURPOSE: create exactly ONE immutable execution-request VALUE for the
EXACT task that was explicitly authorized by ONE genuine V1.44 intent
and was proven CURRENT-applicable by ONE genuine V1.45
``PortfolioProjectFocusNextReadyTaskExecutionApplicability`` whose
``applicability_state`` is exactly ``APPLICABLE``. No replacement task
may ever be selected; no alternative task is inspected, ranked, or
substituted.

The SOLE semantic input authority is that one genuine V1.45
applicability result. The caller supplies ONLY ``request_id`` and
``requested_at``. The V1.45 result is authoritative for intent
provenance, authorization provenance, decision provenance, portfolio
identity, authorized project identity, and authorized task identity.

No Portfolio argument is accepted. No CURRENT state is re-read. No
readiness / constraint / WBS recomputation occurs. The V1.45 model
already enforces the state invariants, so APPLICABLE therefore means:
task_status ACTIVE, an exact constraint tuple exists, unsatisfied
constraint count is zero, and every collected constraint is satisfied.
None of that semantics is recomputed independently here.

Temporal semantics: NO temporal validity rule is invented. Neither
``requested_at`` nor ``authorized_at`` is a current-time or expiration
value; both are provenance only, and no precedence between them is
enforced. No wall-clock read, no generated identity, no generated
timestamp.

V1.46 does NOT: import or consult a Portfolio or any domain
current-state module, rebuild the work breakdown, inspect relations or
constraints, recompute V1.39 readiness / V1.40 candidates / V1.41
selection / V1.42 acceptance / V1.43 persistence / V1.44 authorization /
V1.45 applicability, call ``transition_entity_status`` or
``transition_entity_status_durably`` or mutate any status, execute or
dispatch a task, enqueue or schedule a job, invoke any agent / provider
/ runtime, run a shell or subprocess, persist anything, access any
repository or database, generate UUIDs (no ``uuid4`` / ``uuid1``), read
the wall clock (no ``datetime.now`` / ``datetime.utcnow``), generate
timestamps, or fallback / re-rank / reselect any task.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    model_validator,
)

from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_applicability import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskExecutionApplicability,
    PortfolioProjectFocusNextReadyTaskExecutionApplicabilityState,
)

__all__ = [
    "PortfolioProjectFocusNextReadyTaskExecutionRequest",
    "PortfolioProjectFocusNextReadyTaskExecutionRequestError",
    "request_current_applicable_next_ready_task_execution",
]


class PortfolioProjectFocusNextReadyTaskExecutionRequestError(ValueError):
    """Raised when an explicit executable TASK request cannot be created
    from ONE genuine V1.45 CURRENT-applicability result.

    Raised for: a ``request_id`` that is not already a genuine ``UUID``
    instance; a ``requested_at`` that is not already a genuine
    ``datetime`` instance or that is not timezone-aware with a non-None
    ``utcoffset()``; an ``applicability`` that is not a genuine V1.45
    ``PortfolioProjectFocusNextReadyTaskExecutionApplicability``
    instance; a V1.45 payload that fails fresh COMPLETE strict
    re-validation (hostile ``model_construct`` values included); and an
    ``applicability_state`` that is not exactly ``APPLICABLE``.

    This error never carries a replacement task, a fallback selection,
    any execution, or any inference of a current, latest, or effective
    authorization.
    """


# ---------------------------------------------------------------------------
# Execution request model (immutable, self-validating, scalar-only).
# ---------------------------------------------------------------------------


class PortfolioProjectFocusNextReadyTaskExecutionRequest(BaseModel):
    """ONE immutable execution-request VALUE for exactly one
    CURRENT-applicable, human-authorized TASK.

    ``request_id`` (``UUID``) and ``requested_at`` (timezone-aware
    ``datetime``) are caller-supplied: no default identity, no default
    timestamp, no generated identity, no generated timestamp.

    ``intent_id``, ``authorized_at`` (exact original UTC offset
    preserved verbatim), ``decision_id``, ``portfolio_id``,
    ``authorized_project_id``, and ``authorized_task_id`` are EXACT
    provenance projections of the freshly revalidated V1.45
    applicability result.

    Neither timestamp carries temporal-validity semantics and no
    precedence between them is enforced. No task status, applicability
    state, constraints, unsatisfied count, target status, executor /
    provider identity, command, queue, runtime handle, persistence
    metadata, or generated metadata is carried: V1.45 owns applicability
    semantics and this request only records the exact execution-request
    handoff provenance.

    Constructing a request values nothing by itself: it authorizes no
    execution and executes nothing.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    request_id: UUID
    requested_at: datetime
    intent_id: UUID
    authorized_at: datetime
    decision_id: UUID
    portfolio_id: UUID
    authorized_project_id: UUID
    authorized_task_id: UUID

    @model_validator(mode="after")
    def _enforce_request_timestamp_invariants(
        self,
    ) -> PortfolioProjectFocusNextReadyTaskExecutionRequest:
        if (
            self.requested_at.tzinfo is None
            or self.requested_at.utcoffset() is None
        ):
            raise ValueError(
                "requested_at must be timezone-aware with a non-None "
                "utcoffset()"
            )
        if (
            self.authorized_at.tzinfo is None
            or self.authorized_at.utcoffset() is None
        ):
            raise ValueError(
                "authorized_at must remain timezone-aware with a "
                "non-None utcoffset()"
            )
        return self


def _revalidate_v145_applicability(
    applicability: PortfolioProjectFocusNextReadyTaskExecutionApplicability,
) -> PortfolioProjectFocusNextReadyTaskExecutionApplicability:
    """Freshly strict-revalidate the COMPLETE V1.45 payload.

    Defeats hostile ``model_construct`` payloads and any state that
    ordinary construction could never produce. Every semantic V1.45
    read afterwards must use ONLY the fresh validated copy returned
    here.
    """

    try:
        payload: object = applicability.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionRequestError(
                "the supplied V1.45 applicability is not the V1.45 "
                "shape"
            )
        ) from exc

    try:
        return (
            PortfolioProjectFocusNextReadyTaskExecutionApplicability.model_validate(
                payload, strict=True
            )
        )
    except ValidationError as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionRequestError(
                "the supplied V1.45 applicability failed fresh COMPLETE "
                "strict re-validation"
            )
        ) from exc


def request_current_applicable_next_ready_task_execution(
    request_id: object,
    requested_at: object,
    applicability: object,
) -> PortfolioProjectFocusNextReadyTaskExecutionRequest:
    """Create exactly ONE immutable execution-request VALUE for the
    EXACT task proven CURRENT-applicable by ONE genuine V1.45
    ``PortfolioProjectFocusNextReadyTaskExecutionApplicability`` whose
    ``applicability_state`` is exactly ``APPLICABLE``.

    Required order:

    1. ``request_id`` must already be a genuine ``UUID`` instance;
    2. ``requested_at`` must already be a genuine ``datetime`` instance;
    3. ``requested_at`` must be timezone-aware with a non-None
       ``utcoffset()``;
    4. ``applicability`` must be a genuine V1.45 applicability
       instance;
    5. freshly strict-revalidate the COMPLETE V1.45 payload;
    6. retain ONLY the fresh validated V1.45 copy;
    7. from then on, every V1.45 semantic read uses ONLY that validated
       copy;
    8. require ``applicability_state`` to be exactly ``APPLICABLE``;
    9. construct the immutable request using the caller ``request_id`` /
       ``requested_at`` and the exact provenance fields of the
       validated V1.45 copy;
    10. return the request.

    Pure and deterministic: repeated identical calls are value-identical.
    No Portfolio, no CURRENT-state read, no WBS / relation / constraint
    or any prior-milestone recomputation, no status transition, no
    execution / dispatch / queue / scheduler / agent / provider /
    runtime surface, no shell, no persistence, no fallback / reselection
    of any kind, no temporal validity rule, no wall-clock read, and no
    generated identity or timestamp.

    Any of the seven non-APPLICABLE V1.45 states is rejected with the
    V1.46 boundary error; no request variant is mapped from them and no
    other task is selected.
    """

    if not isinstance(request_id, UUID):
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionRequestError(
                "request_id must already be a genuine UUID instance, "
                f"got {type(request_id).__name__}"
            )
        )

    if not isinstance(requested_at, datetime):
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionRequestError(
                "requested_at must already be a genuine datetime "
                f"instance, got {type(requested_at).__name__}"
            )
        )

    if (
        requested_at.tzinfo is None
        or requested_at.utcoffset() is None
    ):
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionRequestError(
                "requested_at must be timezone-aware with a non-None "
                "utcoffset()"
            )
        )

    if (
        not isinstance(
            applicability,
            PortfolioProjectFocusNextReadyTaskExecutionApplicability,
        )
    ):
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionRequestError(
                "a genuine V1.45 "
                "PortfolioProjectFocusNextReadyTaskExecutionApplicability "
                f"is required, got {type(applicability).__name__}"
            )
        )

    validated: (
        PortfolioProjectFocusNextReadyTaskExecutionApplicability
    ) = _revalidate_v145_applicability(applicability)

    if (
        validated.applicability_state
        is not PortfolioProjectFocusNextReadyTaskExecutionApplicabilityState.APPLICABLE
    ):
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionRequestError(
                "the V1.45 applicability_state must be exactly "
                f"APPLICABLE, got {validated.applicability_state.value}"
            )
        )

    try:
        return PortfolioProjectFocusNextReadyTaskExecutionRequest(
            request_id=request_id,
            requested_at=requested_at,
            intent_id=validated.intent_id,
            authorized_at=validated.authorized_at,
            decision_id=validated.decision_id,
            portfolio_id=validated.portfolio_id,
            authorized_project_id=validated.authorized_project_id,
            authorized_task_id=validated.authorized_task_id,
        )
    except (ValidationError, ValueError) as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionRequestError(
                "the validated V1.45 provenance cannot be carried by a "
                "genuine V1.46 execution request"
            )
        ) from exc
