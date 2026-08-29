"""Planned-effort estimation (V1.10-A: pure planned-effort estimate domain).

Models a single, explicitly human-confirmed estimate of the planned execution
effort expected to be spent **directly** on one entity of a
:class:`Portfolio`. This is a pure, read-only extension of the domain: the
factory never mutates the supplied portfolio, its entities, or its relations,
it performs no status change, and it makes no wall-clock calls.

The V1.10 direct-effort semantics are explicit and additive:

> an estimate is the planned execution effort expected to be spent directly
> on this canonical entity itself, excluding effort planned directly on its
> WBS descendants.

That keeps CURRENT-WBS subtree roll-ups additive and prevents double-counting
top-down estimates that already include child work.

The public boundary is deliberately strict:

* :class:`ExecutionEffortEstimate` is immutable (frozen) and uses strict
  Pydantic validation so direct construction cannot silently coerce
  incompatible Python values;
* ``duration_seconds`` must be an actual ``int`` greater than or equal to
  zero; ``bool`` is explicitly rejected; **zero is valid and meaningful** —
  it explicitly records that no direct effort is currently planned on that
  entity;
* ``estimated_at`` must be an actual ``datetime`` and timezone-aware
  (``tzinfo is not None`` AND ``utcoffset() is not None``);
* the V1.10 human factory fixes ``source`` to
  :attr:`SourceKind.USER_CONFIRMED`; the model itself intentionally does not
  pin ``source`` to a single value, since later provenance kinds may record
  estimates too;
* ``portfolio_id`` always equals the ``id`` of the supplied portfolio;
* ``estimated_at`` may legitimately precede ``entity.updated_at`` —
  backfilled estimates are valid;
* estimate revisions are append-only application-level records; the model has
  no update/delete/correction representation;
* no AI, provider, persistence, or application imports; no generic event
  abstraction; no broad exception catches.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
)

from trajectory_os.domain.entities import SourceKind
from trajectory_os.domain.portfolio import Portfolio


class ExecutionEffortEstimateError(ValueError):
    """Raised when a planned-effort estimate is invalid."""


class ExecutionEffortEstimateEntityNotFoundError(ExecutionEffortEstimateError):
    """Raised when the target entity does not exist in the portfolio."""


def _reject_bool_duration(duration_seconds: object) -> object:
    if isinstance(duration_seconds, bool):
        raise ValueError(
            "duration_seconds must be an int; bool is not a valid duration"
        )
    return duration_seconds


def _require_aware_estimated_at(estimated_at: datetime) -> datetime:
    if estimated_at.tzinfo is None or estimated_at.utcoffset() is None:
        raise ValueError(
            "estimated_at must be timezone-aware (got "
            f"{estimated_at!r})"
        )
    return estimated_at


def _require_uuid(value: object, field_name: str) -> object:
    if not isinstance(value, UUID):
        raise ValueError(
            f"{field_name} must be a UUID instance; "
            f"got {type(value).__name__}"
        )
    return value


def select_latest_execution_effort_estimate(
    estimates: Iterable[ExecutionEffortEstimate],
) -> ExecutionEffortEstimate | None:
    """Canonical V1.10 current-effective estimate selection policy.

    The authoritative V1.10 ordering is exactly::

        current effective estimate = max(valid revisions,
                                         key=(estimated_at chronological
                                              instant, estimate_id.int))

    Consequences:

    * empty input -> ``None``;
    * exactly one revision -> that exact revision;
    * the later chronological ``estimated_at`` wins (aware datetimes are
      compared by actual instant; timezone offsets may differ);
    * equal chronological instants are broken deterministically by the
      greater estimate UUID integer;
    * insertion order is irrelevant;
    * ``source`` and any provenance kind do not change the ordering.

    This function is the single authoritative expression of the V1.10
    selection policy: consumers (V1.10-D planning, V1.22 current-effective
    resolution) MUST delegate to it rather than re-interpret the ordering.

    Callers are responsible for supplying genuine ``ExecutionEffortEstimate
    `` instances (every revision validated); the selection itself performs
    no revalidation, no filtering by scope, no deduplication, and no
    ``SourceKind`` inference.
    """
    best: ExecutionEffortEstimate | None = None
    best_key: tuple[datetime, int] | None = None
    for estimate in estimates:
        key = (estimate.estimated_at, estimate.id.int)
        if best_key is None or key > best_key:
            best = estimate
            best_key = key
    return best


class ExecutionEffortEstimate(BaseModel):
    """Immutable record of one planned direct execution-effort estimate."""

    model_config = ConfigDict(frozen=True, strict=True)

    id: UUID
    portfolio_id: UUID
    entity_id: UUID

    duration_seconds: Annotated[StrictInt, Field(ge=0)]

    estimated_at: datetime

    source: SourceKind

    @field_validator("id", mode="before")
    @classmethod
    def _validate_id(cls, value: object) -> object:
        return _require_uuid(value, "id")

    @field_validator("portfolio_id", mode="before")
    @classmethod
    def _validate_portfolio_id(cls, value: object) -> object:
        return _require_uuid(value, "portfolio_id")

    @field_validator("entity_id", mode="before")
    @classmethod
    def _validate_entity_id(cls, value: object) -> object:
        return _require_uuid(value, "entity_id")

    @field_validator("estimated_at", mode="before")
    @classmethod
    def _validate_estimated_at_type(cls, value: object) -> object:
        if not isinstance(value, datetime):
            raise ValueError(
                "estimated_at must be a datetime instance; "
                f"got {type(value).__name__}"
            )
        return value

    @field_validator("duration_seconds", mode="before")
    @classmethod
    def _validate_duration_seconds(
        cls, duration_seconds: object
    ) -> object:
        return _reject_bool_duration(duration_seconds)

    @field_validator("estimated_at")
    @classmethod
    def _validate_estimated_at(cls, estimated_at: datetime) -> datetime:
        return _require_aware_estimated_at(estimated_at)


def create_execution_effort_estimate(
    portfolio: Portfolio,
    estimate_id: UUID,
    entity_id: UUID,
    duration_seconds: int,
    estimated_at: datetime,
) -> ExecutionEffortEstimate:
    """Create an immutable planned direct-effort estimate for one entity.

    ``portfolio`` must be a :class:`Portfolio` instance containing
    ``entity_id``. ``estimate_id`` and ``entity_id`` must be ``UUID``
    instances. ``duration_seconds`` must be an actual non-negative ``int``
    (``bool`` is rejected); zero explicitly records that no direct effort is
    currently planned on the entity. ``estimated_at`` must be a
    timezone-aware ``datetime``; it may legitimately precede the entity's
    ``updated_at`` because backfilled estimates are valid.

    The returned estimate always carries ``portfolio_id == portfolio.id``
    and ``source == SourceKind.USER_CONFIRMED``. A new estimate for the same
    entity does not replace an older one; revision selection is a separate
    deterministic concern. The supplied portfolio is never mutated and no
    entity status or ``updated_at`` value changes.

    Raises :class:`ExecutionEffortEstimateError` (or the narrow
    :class:`ExecutionEffortEstimateEntityNotFoundError` subclass) when an
    argument is not a canonical domain value or the entity is unknown.
    """

    if not isinstance(portfolio, Portfolio):
        raise ExecutionEffortEstimateError(
            "portfolio must be a Portfolio instance, "
            f"got {type(portfolio).__name__}"
        )

    if not isinstance(estimate_id, UUID):
        raise ExecutionEffortEstimateError(
            "estimate_id must be a UUID instance, "
            f"got {type(estimate_id).__name__}"
        )

    if not isinstance(entity_id, UUID):
        raise ExecutionEffortEstimateError(
            "entity_id must be a UUID instance, "
            f"got {type(entity_id).__name__}"
        )

    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise ExecutionEffortEstimateError(
            "duration_seconds must be an int; "
            f"got {type(duration_seconds).__name__}"
        )

    if duration_seconds < 0:
        raise ExecutionEffortEstimateError(
            "duration_seconds must be >= 0; got "
            f"{duration_seconds!r}"
        )

    if not isinstance(estimated_at, datetime):
        raise ExecutionEffortEstimateError(
            "estimated_at must be a datetime instance, "
            f"got {type(estimated_at).__name__}"
        )

    if estimated_at.tzinfo is None or estimated_at.utcoffset() is None:
        raise ExecutionEffortEstimateError(
            "estimated_at must be timezone-aware (got "
            f"{estimated_at!r})"
        )

    if portfolio.get_entity(entity_id) is None:
        raise ExecutionEffortEstimateEntityNotFoundError(
            f"unknown entity in portfolio: {entity_id}"
        )

    try:
        return ExecutionEffortEstimate(
            id=estimate_id,
            portfolio_id=portfolio.id,
            entity_id=entity_id,
            duration_seconds=duration_seconds,
            estimated_at=estimated_at,
            source=SourceKind.USER_CONFIRMED,
        )
    except ValidationError as exc:
        raise ExecutionEffortEstimateError(
            "failed while validating the planned-effort estimate"
        ) from exc
