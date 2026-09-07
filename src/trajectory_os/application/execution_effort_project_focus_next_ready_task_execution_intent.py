"""V1.44 — explicit human-authorized execution intent for the durable
accepted next READY TASK decision.

The architectural authority chain is:

    deterministic derivation          (V1.41 selection)
    ->
    explicit human acceptance         (V1.42 decision)
    ->
    durable append                    (V1.43 durable record)
    ->
    explicit human execution
    authorization                     (V1.44 intent)   <-- this module

V1.44 MUST NOT execute anything. It creates exactly one immutable
authorization VALUE: a
``PortfolioProjectFocusNextReadyTaskExecutionIntent`` that says one human
explicitly authorized execution of the (project, task) pair already
accepted inside one genuine durable V1.43
``PortfolioProjectFocusNextReadyTaskDecisionRecord``.

The SOLE semantic input authority is that one genuine V1.43 record. The
boundary:

1. requires a genuine V1.43
   ``PortfolioProjectFocusNextReadyTaskDecisionRecord`` instance (``None``,
   dicts, strings, foreign models, and duck types are rejected);
2. freshly strict-revalidates the COMPLETE V1.43 record and thereby
   revalidates its nested genuine V1.42
   ``PortfolioProjectFocusNextReadyTaskDecision`` through the V1.43
   record invariants (hostile ``model_construct`` values at the V1.43
   level or the nested V1.42 level are rejected);
3. retains ONLY the fresh validated copy;
4. from that point on performs EVERY semantic read ONLY from the
   retained validated copy — never from the caller-owned instance;
5. consults NO current Portfolio, WBS, readiness, candidate, relation,
   constraint, or status-transition authority, and no provider / AI /
   runtime boundary.

Temporal rule (deliberate): V1.44 does NOT enforce any precedence between
``authorized_at`` and the record's outer ``decided_at`` or the nested
V1.42 ``decided_at`` — those two timestamps carry distinct, not-yet-formally
unified temporal semantics. The only requirement is that ``authorized_at``
is a genuine timezone-aware datetime with a non-None UTC offset.

V1.44 does NOT: call ``transition_entity_status_durably`` or
``transition_entity_status``, import or inspect ``EntityStatus``, mark a
TASK ACTIVE / WAITING / PAUSED / COMPLETED / CANCELLED, mutate a Portfolio
or any entity or relation, execute or dispatch a task, invoke Pi / Aider /
Ollama / any LLM / provider / runtime, invoke a shell or subprocess, access
execution filesystem surfaces, enqueue or schedule jobs, call agents,
recompute V1.42 acceptance / V1.41 selection / V1.40 candidates / V1.39
readiness, inspect current constraints or relations, rebuild the work
breakdown (no ``build_work_breakdown`` call), consult a current Portfolio,
inspect titles / descriptions / priority / urgency / deadlines / effort /
rank / score / business value, recommend, optimize, or select anything new,
generate UUIDs (no ``uuid4``), read the wall clock (no
``datetime.now`` / ``datetime.utcnow``), generate timestamps, persist the
execution intent, add any repository, SQLite table, or schema, infer any
current / latest / effective authorization, or mutate the supplied V1.43
record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from trajectory_os.application.execution_effort_project_focus_next_ready_task_decision_persistence import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskDecisionRecord,
)

__all__ = [
    "PortfolioProjectFocusNextReadyTaskExecutionIntent",
    "PortfolioProjectFocusNextReadyTaskExecutionIntentError",
    "authorize_next_ready_task_execution",
]


class PortfolioProjectFocusNextReadyTaskExecutionIntentError(ValueError):
    """Raised when an explicit human execution authorization cannot be
    recorded against a genuine durable V1.43 decision record.

    Raised for: an ``intent_id`` that is not a genuine ``UUID``, an
    ``authorized_at`` that is not a timezone-aware ``datetime`` with a
    non-None ``utcoffset()``, a ``durable_decision`` that is not a genuine
    V1.43 ``PortfolioProjectFocusNextReadyTaskDecisionRecord``, and a
    record that fails fresh COMPLETE strict re-validation (hostile
    ``model_construct`` payloads at the V1.43 level or the nested V1.42
    level).
    """


# ---------------------------------------------------------------------------
# Execution intent model (immutable, self-validating, scalar-only).
# ---------------------------------------------------------------------------


class PortfolioProjectFocusNextReadyTaskExecutionIntent(BaseModel):
    """One immutable, explicit human-authorized execution intent.

    ``intent_id`` (``UUID``) and ``authorized_at`` (aware ``datetime``)
    are caller-supplied: no default identity, no default timestamp, no
    generated identity, no generated timestamp.

    Everything else is an EXACT projection of the freshly revalidated
    durable V1.43 record's accepted V1.42 decision:
    ``decision_id``, ``decision_decided_at`` (the record's outer
    ``decided_at``, its original UTC offset preserved verbatim),
    ``portfolio_id``, ``selected_project_count``, ``ready_task_count``,
    ``authorized_project_id`` (the accepted project), and
    ``authorized_task_id`` (the accepted task).

    Invariants (enforced): both timestamps are timezone-aware with a
    non-None ``utcoffset()``; ``selected_project_count >= 0``;
    ``ready_task_count >= 1`` (a durable V1.42 acceptance is always an
    accepted task, never an empty selection).

    This value authorizes nothing in any runtime: it carries no status,
    no queue identity, no scheduler handle, no execution metadata, and no
    side effect of any kind.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    intent_id: UUID
    authorized_at: datetime
    decision_id: UUID
    decision_decided_at: datetime
    portfolio_id: UUID
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    ready_task_count: Annotated[StrictInt, Field(ge=1)]
    authorized_project_id: UUID
    authorized_task_id: UUID

    @model_validator(mode="after")
    def _enforce_intent_invariants(
        self,
    ) -> PortfolioProjectFocusNextReadyTaskExecutionIntent:
        """Both timestamps must be timezone-aware with a real UTC offset.

        A naive datetime, or an explicit tzinfo whose ``utcoffset()``
        returns ``None``, is rejected for BOTH ``authorized_at`` and
        ``decision_decided_at``. No other cross-check is enforced here —
        in particular, NO temporal precedence between ``authorized_at``
        and ``decision_decided_at`` (the V1.43 outer and nested V1.42
        ``decided_at`` semantics are not yet formally unified).
        """
        for name in ("authorized_at", "decision_decided_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(
                    f"{name} must be a timezone-aware datetime with a "
                    "non-None UTC offset"
                )
        return self


# ---------------------------------------------------------------------------
# Pure explicit human execution-authorization boundary.
# ---------------------------------------------------------------------------


def authorize_next_ready_task_execution(
    intent_id: object,
    authorized_at: object,
    durable_decision: object,
) -> PortfolioProjectFocusNextReadyTaskExecutionIntent:
    """Create one immutable execution intent from ONE genuine durable
    V1.43 accepted next READY TASK decision record.

    ``intent_id`` is the caller-supplied identity of THIS authorization;
    ``authorized_at`` is the caller-supplied timezone-aware authorization
    instant. Every other semantic value is an EXACT projection of the
    freshly revalidated V1.43 record — the caller cannot supply or
    override any projected value.

    Pure and deterministic: no repository argument, no I/O, no side
    effect, no execution, no status transition, no provider / AI /
    runtime boundary, no work-breakdown construction, no readiness /
    selection / candidate recomputation, no ranking / scoring, no
    generated identity or timestamp, no clock read, and the caller-owned
    V1.43 record is never mutated.

    Steps (deliberately ordered; every failure is raised BEFORE the
    intent is built):

      1. ``intent_id`` must already be a ``UUID`` instance (no
         str/bytes/int coercion, no hidden ``uuid4()``);
      2. ``authorized_at`` must already be a ``datetime`` instance (no
         string, no hidden clock);
      3. ``authorized_at`` must be timezone-aware with a non-None
         ``utcoffset()``;
      4. ``durable_decision`` must be a genuine V1.43
         ``PortfolioProjectFocusNextReadyTaskDecisionRecord`` instance
         (``None``, dicts, strings, and foreign models are rejected);
      5. fresh COMPLETE strict re-validation of the whole V1.43 record,
         which through the record invariants revalidates the nested
         genuine V1.42 decision (hostile ``model_construct`` values at
         either level are rejected);
      6. only the fresh validated copy is retained and used;
      7. the exact immutable execution intent is constructed from ONLY
         the two caller-supplied values plus the exact projected values
         from the retained validated V1.43 copy;
      8. the intent is returned.

    Repeated identical calls are value-identical.
    """

    # -- 1. intent_id must already be a genuine UUID ----------------------
    if not isinstance(intent_id, UUID):
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionIntentError(
                "intent_id must already be a UUID instance, "
                f"got {type(intent_id).__name__}"
            )
        )

    # -- 2. authorized_at must already be a datetime ----------------------
    if not isinstance(authorized_at, datetime):
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionIntentError(
                "authorized_at must already be a datetime instance, "
                f"got {type(authorized_at).__name__}"
            )
        )

    # -- 3. authorized_at must be timezone-aware with a real offset -------
    if (
        authorized_at.tzinfo is None
        or authorized_at.utcoffset() is None
    ):
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionIntentError(
                "authorized_at must be a timezone-aware datetime with "
                "a non-None UTC offset"
            )
        )

    # -- 4. genuine durable V1.43 decision record -------------------------
    if not isinstance(
        durable_decision, PortfolioProjectFocusNextReadyTaskDecisionRecord
    ):
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionIntentError(
                "durable_decision must be a genuine V1.43 "
                "PortfolioProjectFocusNextReadyTaskDecisionRecord "
                f"instance, got {type(durable_decision).__name__}"
            )
        )

    # -- 5. fresh COMPLETE strict re-validation of the WHOLE record -------
    try:
        record_payload: object = (
            durable_decision.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionIntentError(
                "the supplied durable V1.43 decision record is not "
                "the V1.43 shape"
            )
        ) from exc

    try:
        validated = (
            PortfolioProjectFocusNextReadyTaskDecisionRecord.model_validate(
                record_payload, strict=True
            )
        )
    except ValidationError as exc:  # noqa: B904 - re-raise as the boundary error
        raise (
            PortfolioProjectFocusNextReadyTaskExecutionIntentError(
                "the supplied durable V1.43 decision record failed "
                "fresh COMPLETE strict re-validation"
            )
        ) from exc

    # -- 6. from here on: ONLY the retained validated V1.43 record --------

    # -- 7. exact caller values + exact projected values, immutable -------
    return PortfolioProjectFocusNextReadyTaskExecutionIntent(
        intent_id=intent_id,
        authorized_at=authorized_at,
        decision_id=validated.decision_id,
        decision_decided_at=validated.decided_at,
        portfolio_id=validated.decision.portfolio_id,
        selected_project_count=validated.decision.selected_project_count,
        ready_task_count=validated.decision.ready_task_count,
        authorized_project_id=validated.decision.accepted_project_id,
        authorized_task_id=validated.decision.accepted_task_id,
    )
