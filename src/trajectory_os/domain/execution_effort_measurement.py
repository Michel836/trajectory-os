"""Deterministic execution-effort measurement over the CURRENT WBS (V1.9-B).

This module derives immutable measurements from two existing trusted inputs:

- the CURRENT canonical :class:`Portfolio` and its V1.1 work-breakdown projection;
- durable V1.8 :class:`ExecutionEffortObservation` values.

The boundary is deliberately pure. It performs no persistence writes, no wall-clock
reads, no provider/AI calls, and no historical WBS reconstruction. Historical
observations for entities that no longer belong to the CURRENT selected WBS remain
valid observations, but are excluded from the CURRENT-structure roll-up.
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
    model_validator,
)

from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.work_breakdown import WorkBreakdownNode, build_work_breakdown


class ExecutionEffortMeasurementError(ValueError):
    """Raised when execution-effort measurement input is invalid."""


def _require_aware_datetime(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class ExecutionEffortSummary(BaseModel):
    """Immutable exact aggregate of a set of execution-effort observations."""

    model_config = ConfigDict(frozen=True, strict=True)

    duration_seconds: Annotated[StrictInt, Field(ge=0)]
    observation_count: Annotated[StrictInt, Field(ge=0)]
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None

    @field_validator("first_observed_at", "last_observed_at")
    @classmethod
    def _validate_aware_datetime(cls, value: datetime | None, info: object) -> datetime | None:
        field_name = getattr(info, "field_name", "observed_at")
        return _require_aware_datetime(value, field_name)

    @model_validator(mode="after")
    def _validate_summary_consistency(self) -> ExecutionEffortSummary:
        if self.observation_count == 0:
            if self.duration_seconds != 0:
                raise ValueError("zero observations require zero duration")
            if self.first_observed_at is not None or self.last_observed_at is not None:
                raise ValueError("zero observations require None first/last timestamps")
            return self

        if self.duration_seconds <= 0:
            raise ValueError("positive observation count requires positive duration")
        if self.first_observed_at is None or self.last_observed_at is None:
            raise ValueError("non-empty summary requires first/last timestamps")
        if self.first_observed_at > self.last_observed_at:
            raise ValueError("first_observed_at must not be after last_observed_at")
        return self


class WorkBreakdownEffortMeasurementItem(BaseModel):
    """One CURRENT WBS node with direct and rolled-up observed effort."""

    model_config = ConfigDict(frozen=True, strict=True)

    entity_id: UUID
    parent_id: UUID | None
    depth: Annotated[StrictInt, Field(ge=0)]
    direct: ExecutionEffortSummary
    subtree: ExecutionEffortSummary


class WorkBreakdownEffortMeasurement(BaseModel):
    """Immutable flat pre-order measurement of one CURRENT project WBS."""

    model_config = ConfigDict(frozen=True, strict=True)

    portfolio_id: UUID
    project_id: UUID
    items: tuple[WorkBreakdownEffortMeasurementItem, ...]

    @model_validator(mode="after")
    def _validate_item_identity(self) -> WorkBreakdownEffortMeasurement:
        if not self.items:
            raise ValueError("measurement must contain the project root")
        if self.items[0].entity_id != self.project_id:
            raise ValueError("first measurement item must be the selected project")
        if self.items[0].parent_id is not None or self.items[0].depth != 0:
            raise ValueError("project root must have parent_id=None and depth=0")

        seen: set[UUID] = set()
        depths: dict[UUID, int] = {}
        for item in self.items:
            if item.entity_id in seen:
                raise ValueError(f"duplicate WBS measurement entity: {item.entity_id}")
            seen.add(item.entity_id)
            depths[item.entity_id] = item.depth

            if item.parent_id is None:
                if item.entity_id != self.project_id:
                    raise ValueError("only the project root may have parent_id=None")
                continue

            parent_depth = depths.get(item.parent_id)
            if parent_depth is None:
                raise ValueError("measurement parent must precede its child")
            if item.depth != parent_depth + 1:
                raise ValueError("measurement item depth must equal parent depth + 1")

        return self


_ObservationKey = tuple[datetime, int]


class _Accumulator:
    """Internal exact aggregate retaining deterministic first/last tie-break keys."""

    __slots__ = (
        "duration_seconds",
        "observation_count",
        "first_key",
        "first_observed_at",
        "last_key",
        "last_observed_at",
    )

    def __init__(self) -> None:
        self.duration_seconds = 0
        self.observation_count = 0
        self.first_key: _ObservationKey | None = None
        self.first_observed_at: datetime | None = None
        self.last_key: _ObservationKey | None = None
        self.last_observed_at: datetime | None = None

    def copy(self) -> _Accumulator:
        result = _Accumulator()
        result.duration_seconds = self.duration_seconds
        result.observation_count = self.observation_count
        result.first_key = self.first_key
        result.first_observed_at = self.first_observed_at
        result.last_key = self.last_key
        result.last_observed_at = self.last_observed_at
        return result

    def add_observation(self, observation: ExecutionEffortObservation) -> None:
        key = (observation.observed_at, observation.id.int)
        self.duration_seconds += observation.duration_seconds
        self.observation_count += 1

        if self.first_key is None or key < self.first_key:
            self.first_key = key
            self.first_observed_at = observation.observed_at
        if self.last_key is None or key > self.last_key:
            self.last_key = key
            self.last_observed_at = observation.observed_at

    def merge(self, other: _Accumulator) -> None:
        if other.observation_count == 0:
            return

        self.duration_seconds += other.duration_seconds
        self.observation_count += other.observation_count

        if self.first_key is None or (
            other.first_key is not None and other.first_key < self.first_key
        ):
            self.first_key = other.first_key
            self.first_observed_at = other.first_observed_at

        if self.last_key is None or (
            other.last_key is not None and other.last_key > self.last_key
        ):
            self.last_key = other.last_key
            self.last_observed_at = other.last_observed_at

    def summary(self) -> ExecutionEffortSummary:
        return ExecutionEffortSummary(
            duration_seconds=self.duration_seconds,
            observation_count=self.observation_count,
            first_observed_at=self.first_observed_at,
            last_observed_at=self.last_observed_at,
        )


def _revalidate_observation(candidate: object) -> ExecutionEffortObservation:
    if not isinstance(candidate, ExecutionEffortObservation):
        raise ExecutionEffortMeasurementError(
            "every observation must be an ExecutionEffortObservation instance"
        )

    try:
        return ExecutionEffortObservation.model_validate(
            candidate.model_dump(mode="python"),
            strict=True,
        )
    except ValidationError as exc:
        raise ExecutionEffortMeasurementError(
            "invalid execution-effort observation supplied to measurement"
        ) from exc


def _flatten_wbs_preorder(
    root: WorkBreakdownNode,
) -> list[tuple[UUID, UUID | None, int]]:
    flattened: list[tuple[UUID, UUID | None, int]] = []
    stack: list[tuple[WorkBreakdownNode, UUID | None, int]] = [(root, None, 0)]

    while stack:
        node, parent_id, depth = stack.pop()
        flattened.append((node.entity_id, parent_id, depth))
        for child in reversed(node.children):
            stack.append((child, node.entity_id, depth + 1))

    return flattened


def measure_work_breakdown_effort(
    portfolio: Portfolio,
    project_id: UUID,
    observations: Iterable[ExecutionEffortObservation],
) -> WorkBreakdownEffortMeasurement:
    """Measure direct and subtree observed effort for one CURRENT project WBS.

    The CURRENT V1.1 WBS is authoritative for structure. All supplied observations
    are revalidated, must belong to ``portfolio.id``, and must have globally unique
    observation IDs within the supplied set. Valid observations attached to entities
    outside the selected CURRENT WBS are preserved as legitimate history but excluded
    from this measurement.
    """

    if not isinstance(portfolio, Portfolio):
        raise ExecutionEffortMeasurementError(
            "portfolio must be a Portfolio instance, "
            f"got {type(portfolio).__name__}"
        )
    if not isinstance(project_id, UUID):
        raise ExecutionEffortMeasurementError(
            "project_id must be a UUID instance, "
            f"got {type(project_id).__name__}"
        )

    # V1.1 remains the sole authority for CURRENT WBS membership, grammar,
    # ambiguity, cycle detection, and sibling ordering.
    wbs = build_work_breakdown(portfolio, project_id)
    flattened = _flatten_wbs_preorder(wbs.root)
    wbs_ids = {entity_id for entity_id, _, _ in flattened}

    direct = {entity_id: _Accumulator() for entity_id in wbs_ids}
    seen_observation_ids: set[UUID] = set()

    for candidate in observations:
        observation = _revalidate_observation(candidate)

        if observation.portfolio_id != portfolio.id:
            raise ExecutionEffortMeasurementError(
                "observation belongs to a different portfolio: "
                f"{observation.id} -> {observation.portfolio_id}"
            )
        if observation.id in seen_observation_ids:
            raise ExecutionEffortMeasurementError(
                f"duplicate observation id in measurement input: {observation.id}"
            )
        seen_observation_ids.add(observation.id)

        accumulator = direct.get(observation.entity_id)
        if accumulator is not None:
            accumulator.add_observation(observation)

    subtree = {
        entity_id: accumulator.copy() for entity_id, accumulator in direct.items()
    }

    # Reverse pre-order guarantees a child's complete subtree aggregate exists
    # before it is merged into its parent. The operation is exact integer
    # arithmetic and never mutates the direct measurements.
    for entity_id, parent_id, _ in reversed(flattened):
        if parent_id is not None:
            subtree[parent_id].merge(subtree[entity_id])

    items = tuple(
        WorkBreakdownEffortMeasurementItem(
            entity_id=entity_id,
            parent_id=parent_id,
            depth=depth,
            direct=direct[entity_id].summary(),
            subtree=subtree[entity_id].summary(),
        )
        for entity_id, parent_id, depth in flattened
    )

    return WorkBreakdownEffortMeasurement(
        portfolio_id=portfolio.id,
        project_id=project_id,
        items=items,
    )
