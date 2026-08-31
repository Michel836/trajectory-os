"""Durable persistence of human-accepted portfolio focus decisions (V1.35).

V1.34 turns one already-built V1.33 focus scenario set plus one
EXPLICITLY human-accepted ``PortfolioProjectEffortFocusScenario`` into an
immutable, scalar-only ``PortfolioProjectEffortFocusDecision`` — a value
with no identity, no timestamp, and no durability. V1.35 adds the
application persistence boundary that makes one already-accepted V1.34
decision durable:

- one strict immutable durable record,
  :class:`PortfolioProjectEffortFocusDecisionRecord`, carrying EXACTLY
  three values: the caller-supplied ``decision_id`` (``UUID``), the
  caller-supplied timezone-aware ``decided_at`` (``datetime``), and the
  EXACT accepted V1.34 ``PortfolioProjectEffortFocusDecision``
  (``decision``) — the V1.34 decision remains the sole semantic
  authority; V1.35 adds no new decision fields, no new decision
  semantics, and no derivation of any "current"/"latest"/"best" state;
- one structural ``add(record)`` + ``list_history(portfolio_id)``
  repository protocol, technology-agnostic (no engine, connection, or
  transaction concept leaks into the application);
- one explicit command,
  ``record_portfolio_effort_focus_decision_durably``.

Strict ordering (all failures are raised BEFORE any repository
interaction):

1. ``decision_id`` must already be a ``UUID`` instance (no str/bytes/int
   coercion, no hidden ``uuid4()``);
2. ``decided_at`` must already be a timezone-aware ``datetime`` instance
   (no naive datetime, no string, no hidden clock, no re-normalization
   to UTC);
3. ``decision`` must be a genuine V1.34
   ``PortfolioProjectEffortFocusDecision`` instance and must survive a
   fresh ``model_dump(mode="python")`` -> ``model_validate(strict=True)``
   round-trip, defeating hostile ``model_construct()`` payloads, dicts,
   strings, ``None``, and foreign model types;
4. only then is the immutable record built and
   ``repository.add(record)`` called EXACTLY ONCE;
5. the exact record that was appended is returned.

Boundary rules:

* V1.35 depends ONLY on the V1.34
  ``PortfolioProjectEffortFocusDecision`` value (and plain stdlib /
  Pydantic): NO V1.33 scenario-set import, NO V1.32 shares, NO V1.16 and
  earlier semantic layers, NO ranking/comparison/provider/AI boundary;
* no clock is introduced: ``decided_at`` is caller-supplied and stored
  with its original UTC offset (never defaulted, never re-normalized);
* no ``uuid4()`` is introduced: ``decision_id`` is caller-supplied;
* the same ``decision_id`` is stored exactly once (append-once); two
  value-equivalent decisions with different ``decision_id`` values are
  distinct durable records and may both be stored;
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

from trajectory_os.application.execution_effort_project_focus_decision import (
    PortfolioProjectEffortFocusDecision,
)

__all__ = [
    "DurablePortfolioProjectEffortFocusDecisionError",
    "PortfolioProjectEffortFocusDecisionRecord",
    "PortfolioProjectEffortFocusDecisionRepository",
    "record_portfolio_effort_focus_decision_durably",
]


class DurablePortfolioProjectEffortFocusDecisionError(ValueError):
    """Raised when a durable focus-decision record is structurally invalid.

    Raised for: a ``decision_id`` that is not a ``UUID`` instance, a
    ``decided_at`` that is not a timezone-aware ``datetime`` instance,
    and a V1.34 ``decision`` payload that is not a genuine, freshly
    re-validatable ``PortfolioProjectEffortFocusDecision`` (dict /
    string / ``None`` / foreign model / tampered ``model_construct()``
    instance). Repository failures are NOT wrapped in this error; they
    propagate unchanged.
    """


class PortfolioProjectEffortFocusDecisionRecord(BaseModel):
    """One durable, immutable human-accepted focus decision record (V1.35).

    Exactly three strict, frozen, cross-checked values:

    - ``decision_id``: the caller-supplied immutable identity of THIS
      durable record (``UUID``); duplicate ``decision_id`` is the sole
      duplicate key;
    - ``decided_at``: the caller-supplied timezone-aware decision
      timestamp (``datetime`` with a non-None UTC offset); the original
      UTC offset is preserved verbatim, never re-normalized;
    - ``decision``: the EXACT accepted V1.34
      ``PortfolioProjectEffortFocusDecision`` — the sole semantic
      authority. It is re-built from ``model_dump(mode="python")``
      through ``model_validate(..., strict=True)`` on every construction,
      so a hostile ``model_construct()`` nested state cannot survive
      ordinary validation.

    No other field exists: no status, no actor, no basis, no derived
    "current"/"effective"/"latest" pointer, no auto-generated identity
    and no default timestamp.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_id: UUID
    decided_at: datetime
    decision: PortfolioProjectEffortFocusDecision

    @model_validator(mode="after")
    def _enforce_record_invariants(self) -> PortfolioProjectEffortFocusDecisionRecord:
        """Aware timestamp and a genuinely re-validatable V1.34 decision.

        ``decided_at`` must be timezone-aware with a real UTC offset
        (a naive datetime, or an explicit "naive" tzinfo with zero
        offset and no identity, is rejected). The nested V1.34 decision
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
        PortfolioProjectEffortFocusDecision.model_validate(
            self.decision.model_dump(mode="python"),
            strict=True,
        )
        return self


class PortfolioProjectEffortFocusDecisionRepository(Protocol):
    """Structural, technology-agnostic durable focus-decision boundary (V1.35).

    The only write entry point is ``add``: a single append of one
    immutable record. ``list_history`` is read-only. Implementations
    (e.g. the SQLite adapter) own the transaction; this protocol exposes
    no engine, connection, or transaction concept to the application,
    and deliberately exposes NO update, delete, replace, upsert, save, or
    patch method: partial or rewritten history is not representable
    through this boundary.
    """

    def add(self, record: PortfolioProjectEffortFocusDecisionRecord) -> None:
        """Append exactly one immutable durable focus-decision record."""

        ...

    def list_history(
        self, portfolio_id: UUID
    ) -> tuple[PortfolioProjectEffortFocusDecisionRecord, ...]:
        """Return the exact durable history for one portfolio.

        Ordered by true chronological instant (aware ``decided_at``) and
        then by ``decision_id.int``. Returns ``()`` when the history is
        empty. No "current"/"effective"/"latest" derivation is performed.
        """

        ...


def record_portfolio_effort_focus_decision_durably(
    decision_id: object,
    decided_at: object,
    decision: object,
    *,
    repository: PortfolioProjectEffortFocusDecisionRepository,
) -> PortfolioProjectEffortFocusDecisionRecord:
    """Durable-append one EXPLICIT, already-human-accepted V1.34 decision.

    The exact sequence (every failure raised BEFORE any repository
    interaction):

    1. ``decision_id`` must already be a ``UUID`` instance (no
       coercion, no ``uuid4()``);
    2. ``decided_at`` must already be a timezone-aware ``datetime``
       instance (no naive datetime, no string, no clock);
    3. ``decision`` must be a genuine V1.34
       ``PortfolioProjectEffortFocusDecision`` that survives a fresh
       strict re-validation round-trip (dict / string / ``None`` /
       foreign model / hostile ``model_construct()`` are rejected);
    4. the immutable record is built;
    5. ``repository.add(record)`` is called EXACTLY ONCE; repository
       failures (including a duplicate-``decision_id`` rejection)
       propagate unchanged;
    6. the exact appended record is returned.

    No AI, LLM, provider, task-scheduler, or agent-framework boundary is
    involved. No "accept" semantics are invented here: the V1.34
    decision is the pre-existing human-accepted value, accepted through
    the V1.34 boundary itself.
    """

    if not isinstance(decision_id, UUID):
        raise DurablePortfolioProjectEffortFocusDecisionError(
            "decision_id must already be a UUID instance, "
            f"got {type(decision_id).__name__}"
        )
    if not isinstance(decided_at, datetime):
        raise DurablePortfolioProjectEffortFocusDecisionError(
            "decided_at must already be a datetime instance, "
            f"got {type(decided_at).__name__}"
        )
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise DurablePortfolioProjectEffortFocusDecisionError(
            "decided_at must be a timezone-aware datetime with a "
            "non-None UTC offset"
        )
    if not isinstance(decision, PortfolioProjectEffortFocusDecision):
        raise DurablePortfolioProjectEffortFocusDecisionError(
            "decision must be a genuine V1.34 "
            "PortfolioProjectEffortFocusDecision instance, "
            f"got {type(decision).__name__}"
        )
    try:
        fresh = PortfolioProjectEffortFocusDecision.model_validate(
            decision.model_dump(mode="python"),
            strict=True,
        )
    except ValidationError as exc:  # noqa: B904 - re-raise as the boundary error
        raise DurablePortfolioProjectEffortFocusDecisionError(
            "decision did not survive strict re-validation as a genuine "
            "V1.34 PortfolioProjectEffortFocusDecision"
        ) from exc

    record = PortfolioProjectEffortFocusDecisionRecord(
        decision_id=decision_id,
        decided_at=decided_at,
        decision=fresh,
    )

    repository.add(record)

    return record
