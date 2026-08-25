"""Execution-effort observation (V1.8-A: pure execution-effort observation domain).

Models a single, human-recorded observation of the execution effort spent on
one entity of a :class:`Portfolio`. This is a pure, read-only extension of the
domain: the factory never mutates the supplied portfolio, its entities, or its
relations, it performs no status change, and it makes no wall-clock calls.

The public boundary is deliberately strict:

* :class:`ExecutionEffortObservation` is immutable (frozen) and uses strict
  Pydantic validation so direct construction cannot silently coerce
  incompatible Python values;
* ``duration_seconds`` must be an actual ``int`` greater than zero; ``bool``
  is explicitly rejected;
* ``observed_at`` must be an actual ``datetime`` and timezone-aware
  (``tzinfo is not None`` AND ``utcoffset() is not None``);
* the V1.8 human factory fixes ``source`` to
  :attr:`SourceKind.USER_CONFIRMED`; the model itself intentionally does not
  pin ``source`` to a single value, since later provenance kinds may record
  observations too;
* ``portfolio_id`` always equals the ``id`` of the supplied portfolio;
* ``observed_at`` may legitimately precede ``entity.updated_at`` —
  backfilled observations are valid;
* no AI, provider, persistence, or application imports; no generic event
  abstraction; no broad exception catches.
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
    field_validator,
)

from trajectory_os.domain.entities import SourceKind
from trajectory_os.domain.portfolio import Portfolio


class ExecutionEffortObservationError(ValueError):
    """Raised when an execution-effort observation is invalid."""


class ExecutionEffortEntityNotFoundError(ExecutionEffortObservationError):
    """Raised when the target entity does not exist in the portfolio."""


def _reject_bool_duration(duration_seconds: object) -> object:
    if isinstance(duration_seconds, bool):
        raise ValueError(
            "duration_seconds must be an int; bool is not a valid duration"
        )
    return duration_seconds


def _require_aware_observed_at(observed_at: datetime) -> datetime:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError(
            "observed_at must be timezone-aware (got "
            f"{observed_at!r})"
        )
    return observed_at


def _require_uuid(value: object, field_name: str) -> object:
    if not isinstance(value, UUID):
        raise ValueError(
            f"{field_name} must be a UUID instance; "
            f"got {type(value).__name__}"
        )
    return value


class ExecutionEffortObservation(BaseModel):
    """Immutable record of one observed execution-effort expenditure."""

    model_config = ConfigDict(frozen=True, strict=True)

    id: UUID
    portfolio_id: UUID
    entity_id: UUID

    duration_seconds: Annotated[StrictInt, Field(gt=0)]

    observed_at: datetime

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

    @field_validator("observed_at", mode="before")
    @classmethod
    def _validate_observed_at_type(cls, value: object) -> object:
        if not isinstance(value, datetime):
            raise ValueError(
                "observed_at must be a datetime instance; "
                f"got {type(value).__name__}"
            )
        return value

    @field_validator("duration_seconds", mode="before")
    @classmethod
    def _validate_duration_seconds(
        cls, duration_seconds: object
    ) -> object:
        return _reject_bool_duration(duration_seconds)

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, observed_at: datetime) -> datetime:
        return _require_aware_observed_at(observed_at)


def create_execution_effort_observation(
    portfolio: Portfolio,
    observation_id: UUID,
    entity_id: UUID,
    duration_seconds: int,
    observed_at: datetime,
) -> ExecutionEffortObservation:
    """Create an immutable execution-effort observation for one entity.

    ``portfolio`` must be a :class:`Portfolio` instance containing
    ``entity_id``. ``observation_id`` and ``entity_id`` must be ``UUID``
    instances. ``duration_seconds`` must be an actual positive ``int``
    (``bool`` is rejected). ``observed_at`` must be a timezone-aware
    ``datetime``; it may legitimately precede the entity's ``updated_at``
    because backfilled observations are valid.

    The returned observation always carries ``portfolio_id ==
    portfolio.id`` and ``source == SourceKind.USER_CONFIRMED``. The
    supplied portfolio is never mutated and no entity status or
    ``updated_at`` value changes.

    Raises :class:`ExecutionEffortObservationError` (or the narrow
    :class:`ExecutionEffortEntityNotFoundError` subclass) when an
    argument is not a canonical domain value or the entity is unknown.
    """

    if not isinstance(portfolio, Portfolio):
        raise ExecutionEffortObservationError(
            "portfolio must be a Portfolio instance, "
            f"got {type(portfolio).__name__}"
        )

    if not isinstance(observation_id, UUID):
        raise ExecutionEffortObservationError(
            "observation_id must be a UUID instance, "
            f"got {type(observation_id).__name__}"
        )

    if not isinstance(entity_id, UUID):
        raise ExecutionEffortObservationError(
            "entity_id must be a UUID instance, "
            f"got {type(entity_id).__name__}"
        )

    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise ExecutionEffortObservationError(
            "duration_seconds must be an int; "
            f"got {type(duration_seconds).__name__}"
        )

    if duration_seconds <= 0:
        raise ExecutionEffortObservationError(
            "duration_seconds must be > 0; got "
            f"{duration_seconds!r}"
        )

    if not isinstance(observed_at, datetime):
        raise ExecutionEffortObservationError(
            "observed_at must be a datetime instance, "
            f"got {type(observed_at).__name__}"
        )

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ExecutionEffortObservationError(
            "observed_at must be timezone-aware (got "
            f"{observed_at!r})"
        )

    if portfolio.get_entity(entity_id) is None:
        raise ExecutionEffortEntityNotFoundError(
            f"unknown entity in portfolio: {entity_id}"
        )

    try:
        return ExecutionEffortObservation(
            id=observation_id,
            portfolio_id=portfolio.id,
            entity_id=entity_id,
            duration_seconds=duration_seconds,
            observed_at=observed_at,
            source=SourceKind.USER_CONFIRMED,
        )
    except ValidationError as exc:
        raise ExecutionEffortObservationError(
            "failed while validating the execution-effort observation"
        ) from exc
