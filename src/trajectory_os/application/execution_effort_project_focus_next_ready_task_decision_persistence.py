"""Durable persistence of the V1.42 human-accepted next READY TASK decision.

V1.42 records one EXPLICIT human accepted next READY TASK decision against a
genuine V1.41 selection — an immutable, scalar-only value with no identity
of its own beyond its provenance and no durability. V1.43 adds the
application persistence boundary that makes one already-accepted V1.42
decision durable, mirroring the established V1.35 durable focus-decision
pattern as architectural precedent:

- one strict immutable durable record,
  :class:`PortfolioProjectFocusNextReadyTaskDecisionRecord`, carrying
  EXACTLY three values: the caller-supplied ``decision_id`` (``UUID``),
  the caller-supplied timezone-aware ``decided_at`` (``datetime``), and
  the EXACT accepted V1.42
  ``PortfolioProjectFocusNextReadyTaskDecision`` (``decision``) — the
  V1.42 decision remains the sole semantic authority; V1.43 adds no new
  decision fields, no new decision semantics, and no derivation of any
  "current"/"latest"/"best" state, no execution of any task, and no
  status transition of any kind;
- one structural ``add(record)`` + ``list_history(portfolio_id)``
  repository protocol, technology-agnostic (no engine, connection, or
  transaction concept leaks into the application);
- one explicit command,
  ``record_next_ready_task_decision_durably``.

Strict ordering (all failures are raised BEFORE any repository
interaction):

1. ``decision_id`` must already be a ``UUID`` instance (no str/bytes/int
   coercion, no hidden ``uuid4()``);
2. ``decided_at`` must already be a timezone-aware ``datetime`` instance
   (no naive datetime, no string, no hidden clock, no re-normalization
   to UTC);
3. ``decision`` must be a genuine V1.42
   ``PortfolioProjectFocusNextReadyTaskDecision`` instance and must
   survive a fresh ``model_dump(mode="python")`` ->
   ``model_validate(strict=True)`` round-trip, defeating hostile
   ``model_construct()`` payloads, dicts, strings, ``None``, and foreign
   model types;
4. only then is the immutable record built and
   ``repository.add(record)`` called EXACTLY ONCE;
5. the exact record that was appended is returned.

Boundary rules:

* V1.43 depends ONLY on the V1.42
  ``PortfolioProjectFocusNextReadyTaskDecision`` value (and plain
  stdlib / Pydantic): NO V1.41 selection import beyond what the V1.42
  model itself re-validates, NO readiness/candidate/constraint/relation
  layer, NO Portfolio argument, NO WBS / work-breakdown construction, no
  status-transition boundary, no provider / AI / runtime boundary;
* no clock is introduced: ``decided_at`` is caller-supplied and stored
  with its original UTC offset (never defaulted, never re-normalized);
* no ``uuid4()`` is introduced: ``decision_id`` is caller-supplied;
* the same ``decision_id`` is stored exactly once (append-once); two
  value-equivalent V1.42 decisions with different ``decision_id`` values
  are distinct durable records and may both be stored;
* ``list_history(portfolio_id)`` returns the stored records ordered by
  their true chronological instant (``decided_at``, offset-aware) and
  then by ``decision_id.int`` (numeric UUID order, not lexical string
  order); it returns ``()`` for an empty history and performs no
  derivation, no inference of a current or effective decision;
* no broad exception catches: repository failures propagate unchanged;
* no update/delete/replace/upsert/save/patch semantic exists at this
  boundary: the only write is the single append.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from trajectory_os.application.execution_effort_project_focus_next_ready_task_decision import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskDecision,
)

__all__ = [
    "DurablePortfolioProjectFocusNextReadyTaskDecisionError",
    "PortfolioProjectFocusNextReadyTaskDecisionRecord",
    "PortfolioProjectFocusNextReadyTaskDecisionRepository",
    "record_next_ready_task_decision_durably",
]


class DurablePortfolioProjectFocusNextReadyTaskDecisionError(ValueError):
    """Raised when a durable next READY TASK decision record is structurally invalid.

    Raised for: a ``decision_id`` that is not a ``UUID`` instance, a
    ``decided_at`` that is not a timezone-aware ``datetime`` instance,
    and a V1.42 ``decision`` payload that is not a genuine, freshly
    re-validatable ``PortfolioProjectFocusNextReadyTaskDecision``
    (dict / string / ``None`` / foreign model / tampered
    ``model_construct()`` instance). Repository failures are NOT wrapped
    in this error; they propagate unchanged.
    """


class PortfolioProjectFocusNextReadyTaskDecisionRecord(BaseModel):
    """One durable, immutable V1.42 human-accepted next READY TASK decision record.

    Exactly three strict, frozen, cross-checked values:

    - ``decision_id``: the caller-supplied immutable identity of THIS
      durable record (``UUID``); duplicate ``decision_id`` is the sole
      duplicate key; it is a durable record identity only and says
      nothing about any task's state;
    - ``decided_at``: the caller-supplied timezone-aware decision
      timestamp (``datetime`` with a non-None UTC offset); the original
      UTC offset is preserved verbatim, never re-normalized;
    - ``decision``: the EXACT accepted V1.42
      ``PortfolioProjectFocusNextReadyTaskDecision`` — the sole
      semantic authority. It is re-built from
      ``model_dump(mode="python")`` through ``model_validate(...,
      strict=True)`` on every construction, so a hostile
      ``model_construct()`` nested state cannot survive ordinary
      validation.

    No other field exists: no status, no actor, no execution metadata,
    no derived "current"/"effective"/"latest" pointer, no auto-generated
    identity and no default timestamp.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_id: UUID
    decided_at: datetime
    decision: PortfolioProjectFocusNextReadyTaskDecision

    @model_validator(mode="after")
    def _enforce_record_invariants(
        self,
    ) -> PortfolioProjectFocusNextReadyTaskDecisionRecord:
        """Aware timestamp and a genuinely re-validatable V1.42 decision.

        ``decided_at`` must be timezone-aware with a real UTC offset
        (a naive datetime, or an explicit "naive" tzinfo with zero
        offset and no identity, is rejected). The nested V1.42 decision
        must survive a fresh strict ``model_validate`` round-trip of its
        own ``model_dump(mode="python")``; the rebuild raises
        ``pydantic.ValidationError`` for any state that genuine
        construction could never have produced (defeating
        ``model_construct()`` bypass of the nested model's validators).
        """
        decided_at = self.decided_at
        if decided_at.tzinfo is None or decided_at.utcoffset() is None:
            raise ValueError(
                "decided_at must be a timezone-aware datetime with a "
                "non-None UTC offset; a naive datetime is not a durable "
                "decision timestamp"
            )
        PortfolioProjectFocusNextReadyTaskDecision.model_validate(
            self.decision.model_dump(mode="python"),
            strict=True,
        )
        return self


class PortfolioProjectFocusNextReadyTaskDecisionRepository(Protocol):
    """Structural, technology-agnostic durable next READY TASK decision boundary.

    The only write entry point is ``add``: a single append of one
    immutable record. ``list_history`` is read-only. Implementations
    (e.g. the SQLite adapter) own the transaction; this protocol exposes
    no engine, connection, or transaction concept to the application,
    and deliberately exposes NO update, delete, replace, upsert, save,
    or patch method: partial or rewritten history is not representable
    through this boundary.
    """

    def add(self, record: PortfolioProjectFocusNextReadyTaskDecisionRecord) -> None:
        """Append exactly one immutable durable next READY TASK decision record."""

        ...

    def list_history(
        self, portfolio_id: UUID
    ) -> tuple[PortfolioProjectFocusNextReadyTaskDecisionRecord, ...]:
        """Return the exact durable history for one portfolio.

        Ordered by true chronological instant (aware ``decided_at``) and
        then by ``decision_id.int``. Returns ``()`` when the history is
        empty. No "current"/"effective"/"latest" derivation is
        performed.
        """

        ...


def record_next_ready_task_decision_durably(
    decision_id: object,
    decided_at: object,
    decision: object,
    *,
    repository: PortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> PortfolioProjectFocusNextReadyTaskDecisionRecord:
    """Durable-append one EXPLICIT, already-human-accepted V1.42 decision.

    The exact sequence (every failure raised BEFORE any repository
    interaction):

    1. ``decision_id`` must already be a ``UUID`` instance (no
       coercion, no ``uuid4()``);
    2. ``decided_at`` must already be a timezone-aware ``datetime``
       instance (no naive datetime, no string, no clock);
    3. ``decision`` must be a genuine V1.42
       ``PortfolioProjectFocusNextReadyTaskDecision`` that survives a
       fresh strict re-validation round-trip (dict / string / ``None``
       / foreign model / hostile ``model_construct()`` are rejected);
    4. only the retained validated copy of the V1.42 decision is used
       from this point on; the caller-owned instance is never read for
       any semantic value;
    5. the immutable record is built;
    6. ``repository.add(record)`` is called EXACTLY ONCE; repository
       failures (including a duplicate-``decision_id`` rejection)
       propagate unchanged;
    7. the exact appended record is returned.

    No AI, LLM, provider, task-scheduler, or agent-framework boundary is
    involved. No task execution, no status transition, and no "accept"
    semantics are invented here: the V1.42 decision is the pre-existing
    human-accepted value, accepted through the V1.42 boundary itself.
    """

    if not isinstance(decision_id, UUID):
        raise DurablePortfolioProjectFocusNextReadyTaskDecisionError(
            "decision_id must already be a UUID instance, "
            f"got {type(decision_id).__name__}"
        )
    if not isinstance(decided_at, datetime):
        raise DurablePortfolioProjectFocusNextReadyTaskDecisionError(
            "decided_at must already be a datetime instance, "
            f"got {type(decided_at).__name__}"
        )
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise DurablePortfolioProjectFocusNextReadyTaskDecisionError(
            "decided_at must be a timezone-aware datetime with a "
            "non-None UTC offset"
        )
    if not isinstance(
        decision, PortfolioProjectFocusNextReadyTaskDecision
    ):
        raise DurablePortfolioProjectFocusNextReadyTaskDecisionError(
            "decision must be a genuine V1.42 "
            "PortfolioProjectFocusNextReadyTaskDecision instance, "
            f"got {type(decision).__name__}"
        )
    try:
        fresh = PortfolioProjectFocusNextReadyTaskDecision.model_validate(
            decision.model_dump(mode="python"),
            strict=True,
        )
    except ValidationError as exc:  # noqa: B904 - re-raise as the boundary error
        raise (
            DurablePortfolioProjectFocusNextReadyTaskDecisionError(
                "decision did not survive strict re-validation as a "
                "genuine V1.42 "
                "PortfolioProjectFocusNextReadyTaskDecision"
            )
        ) from exc

    record = PortfolioProjectFocusNextReadyTaskDecisionRecord(
        decision_id=decision_id,
        decided_at=decided_at,
        decision=fresh,
    )

    repository.add(record)

    return record
