"""Durable execution-effort recording (V1.8-B persistence boundary).

Turns a single human-recorded execution-effort observation for one entity
of a portfolio into durable storage through the existing, minimal,
provider-agnostic ``PortfolioRepository`` boundary (V1.6) plus a new
structural observation-observation repository:

1. reject ``portfolio_id`` unless it is already a ``UUID`` instance,
   before ANY repository interaction;
2. load the CURRENT persisted portfolio;
3. a missing portfolio raises
   :class:`ExecutionEffortPortfolioNotFoundError`;
4. delegate ALL observation/entity/duration/time semantics to the
   existing V1.8-A factory ``create_execution_effort_observation``;
5. ``observation_repository.add`` is called exactly once, only after
   successful domain creation, with exactly the domain observation;
6. return the EXACT same observation object produced by the V1.8-A
   factory.

Strict boundary rules:

* the CURRENT persisted portfolio is authoritative for entity
  membership; removed or missing entities fail through the real
  V1.8-A ``ExecutionEffortEntityNotFoundError`` (membership is not
  re-validated here);
* observation/entity/duration/``observed_at`` validation is never
  duplicated in this module; only ``portfolio_id`` is validated at this
  boundary;
* no append after any validation or domain failure, including a missing
  portfolio;
* ``PortfolioRepository.load`` and ``observation_repository.add``
  exceptions propagate unchanged; there are no broad exception catches;
* the loaded portfolio is never mutated; there is no portfolio save, no
  status transition, and no wall-clock call;
* no AI, provider, SQLite, or distributed-transaction claims.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from trajectory_os.application.work_breakdown_acceptance import (
    PortfolioRepository,
)
from trajectory_os.domain.execution_effort import (
    ExecutionEffortObservation,
    create_execution_effort_observation,
)


class ExecutionEffortObservationRepository(Protocol):
    """Structural read/add boundary for execution-effort observations.

    Intentionally non-runtime-checkable: only structural compatibility
    matters, and no persistence technology, engine, or transaction
    concept is part of this boundary.
    """

    def add(self, observation: ExecutionEffortObservation) -> None:
        """Persist one durable execution-effort observation."""

        ...

    def get(
        self,
        observation_id: UUID,
    ) -> ExecutionEffortObservation | None:
        """Return the stored observation, or ``None`` if absent."""

        ...


class DurableExecutionEffortError(ValueError):
    """Raised when durable execution-effort recording fails at this boundary."""


class ExecutionEffortPortfolioNotFoundError(DurableExecutionEffortError):
    """Raised when the portfolio to record an observation for is absent."""


def record_execution_effort_durably(
    portfolio_id: UUID,
    observation_id: UUID,
    entity_id: UUID,
    duration_seconds: int,
    observed_at: datetime,
    portfolio_repository: PortfolioRepository,
    observation_repository: ExecutionEffortObservationRepository,
) -> ExecutionEffortObservation:
    """Record an execution-effort observation against the current portfolio.

    Sequence is exactly: reject ``portfolio_id`` unless it is already a
    ``UUID`` instance, load the CURRENT persisted portfolio, delegate all
    observation/entity/duration/time semantics to the V1.8-A
    ``create_execution_effort_observation`` factory, add the exact
    resulting observation to ``observation_repository`` exactly once, and
    return that same observation object.

    The loaded portfolio is never mutated and is never saved. A missing
    portfolio raises :class:`ExecutionEffortPortfolioNotFoundError`.
    V1.8-A domain errors and any repository exception propagate
    unchanged.
    """

    if not isinstance(portfolio_id, UUID):
        raise DurableExecutionEffortError(
            "portfolio_id must already be a UUID instance, "
            f"got {type(portfolio_id).__name__}"
        )

    current = portfolio_repository.load(portfolio_id)
    if current is None:
        raise ExecutionEffortPortfolioNotFoundError(
            f"portfolio not found: {portfolio_id}"
        )

    observation = create_execution_effort_observation(
        current,
        observation_id=observation_id,
        entity_id=entity_id,
        duration_seconds=duration_seconds,
        observed_at=observed_at,
    )

    observation_repository.add(observation)

    return observation
