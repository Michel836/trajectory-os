"""Unit tests for read-only durable planned-effort planning (V1.10-E)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

import trajectory_os.application.execution_effort_planning as planning_module
from trajectory_os.application import (
    DurableExecutionEffortPlanningError,
    ExecutionEffortEstimateReader,
    ExecutionEffortPlanningPortfolioNotFoundError,
)
from trajectory_os.application.execution_effort_planning import (
    plan_work_breakdown_effort_durably,
)
from trajectory_os.application.work_breakdown_acceptance import PortfolioRepository
from trajectory_os.domain.entities import EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
)
from trajectory_os.domain.execution_effort_planning import (
    ExecutionEffortPlanningError,
    WorkBreakdownEffortPlan,
    plan_work_breakdown_effort,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import WorkBreakdownError

BASE = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class _SentinelError(Exception):
    """Repository/reader failure with a unique identity."""


class FakePortfolioRepository:
    def __init__(
        self,
        portfolios: dict[UUID, Portfolio] | None = None,
        *,
        load_error: Exception | None = None,
    ) -> None:
        self._portfolios = dict(portfolios or {})
        self._load_error = load_error
        self.loaded_ids: list[object] = []
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.loaded_ids.append(portfolio_id)
        if self._load_error is not None:
            raise self._load_error
        return self._portfolios.get(portfolio_id)

    def save(self, portfolio: Portfolio) -> None:
        self.saved.append(portfolio)
        raise AssertionError("V1.10 planning must never save the Portfolio")


class FakeEstimateReader:
    def __init__(
        self,
        estimates: tuple[ExecutionEffortEstimate, ...] = (),
        *,
        list_error: Exception | None = None,
    ) -> None:
        self._estimates = estimates
        self._list_error = list_error
        self.portfolio_calls: list[UUID] = []
        self.entity_calls: list[tuple[UUID, UUID]] = []

    def list_for_portfolio(
        self, portfolio_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        self.portfolio_calls.append(portfolio_id)
        if self._list_error is not None:
            raise self._list_error
        return self._estimates

    def list_for_entity(
        self, portfolio_id: UUID, entity_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        self.entity_calls.append((portfolio_id, entity_id))
        return tuple(
            estimate
            for estimate in self._estimates
            if estimate.portfolio_id == portfolio_id
            and estimate.entity_id == entity_id
        )


def _portfolio() -> tuple[Portfolio, TrajectoryEntity, TrajectoryEntity]:
    project = TrajectoryEntity(
        id=uuid4(),
        entity_type=EntityType.PROJECT,
        title="Project",
        created_at=BASE,
        updated_at=BASE,
    )
    task = TrajectoryEntity(
        id=uuid4(),
        entity_type=EntityType.TASK,
        title="Task",
        created_at=BASE,
        updated_at=BASE,
    )
    portfolio = Portfolio(
        id=uuid4(),
        name="V1.10",
        entities=[project, task],
        relations=[
            TrajectoryRelation(
                source_id=task.id,
                target_id=project.id,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )
    return portfolio, project, task


def _estimate(
    portfolio: Portfolio, entity: TrajectoryEntity
) -> ExecutionEffortEstimate:
    return ExecutionEffortEstimate(
        id=uuid4(),
        portfolio_id=portfolio.id,
        entity_id=entity.id,
        duration_seconds=90,
        estimated_at=BASE,
        source=SourceKind.USER_CONFIRMED,
    )


def test_reader_protocol_is_structurally_satisfied_by_fake() -> None:
    reader: ExecutionEffortEstimateReader = FakeEstimateReader()
    assert reader.list_for_portfolio(uuid4()) == ()


@pytest.mark.parametrize(
    "bad_portfolio_id",
    ["0148790b-ba4c-5f9e-9f6c-8c4e21d5b0c1", 42, b"bytes", [uuid4()], None, True],
)
def test_invalid_portfolio_id_rejected_before_any_repository_interaction(
    bad_portfolio_id: object,
) -> None:
    portfolio, _, _ = _portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    reader = FakeEstimateReader()

    with pytest.raises(DurableExecutionEffortPlanningError) as excinfo:
        plan_work_breakdown_effort_durably(
            cast(UUID, bad_portfolio_id),
            portfolio.entities[0].id,
            repository,
            reader,
        )

    assert type(excinfo.value) is DurableExecutionEffortPlanningError
    assert repository.loaded_ids == []
    assert reader.portfolio_calls == []


def test_missing_portfolio_fails_before_estimate_reader() -> None:
    missing_id = uuid4()
    repository = FakePortfolioRepository()
    reader = FakeEstimateReader()

    with pytest.raises(ExecutionEffortPlanningPortfolioNotFoundError):
        plan_work_breakdown_effort_durably(
            missing_id, uuid4(), repository, reader
        )

    assert repository.loaded_ids == [missing_id]
    assert reader.portfolio_calls == []


def test_loads_current_portfolio_before_reading_estimates() -> None:
    portfolio, project, task = _portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    reader = FakeEstimateReader(tuple([_estimate(portfolio, task)]))

    plan_work_breakdown_effort_durably(
        portfolio.id, project.id, repository, reader
    )

    assert repository.loaded_ids == [portfolio.id]
    assert reader.portfolio_calls == [portfolio.id]
    assert repository.saved == []


def test_reader_receives_exact_portfolio_id_and_result_is_domain_pure() -> None:
    portfolio, project, task = _portfolio()
    estimate = _estimate(portfolio, task)
    reader = FakeEstimateReader((estimate,))
    repository = FakePortfolioRepository({portfolio.id: portfolio})

    result = plan_work_breakdown_effort_durably(
        portfolio.id, project.id, repository, reader
    )

    assert reader.portfolio_calls == [portfolio.id]
    assert reader.entity_calls == []
    assert repository.saved == []
    assert isinstance(result, WorkBreakdownEffortPlan)
    assert result.portfolio_id == portfolio.id
    assert result.project_id == project.id
    by_id = {item.entity_id: item for item in result.items}
    assert by_id[task.id].direct_estimate == estimate
    assert by_id[task.id].subtree.total_duration_seconds == 90


def test_returns_exact_object_produced_by_domain_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio, project, task = _portfolio()
    estimate = _estimate(portfolio, task)
    repository: PortfolioRepository = FakePortfolioRepository({portfolio.id: portfolio})
    reader: ExecutionEffortEstimateReader = FakeEstimateReader(tuple([estimate]))
    sentinel: WorkBreakdownEffortPlan = plan_work_breakdown_effort(
        portfolio,
        project.id,
        (estimate,),
    )
    captured: list[tuple[Portfolio, UUID, tuple[ExecutionEffortEstimate, ...]]] = []

    def fake_plan(
        current: Portfolio,
        project_id: UUID,
        estimates: tuple[ExecutionEffortEstimate, ...],
    ) -> WorkBreakdownEffortPlan:
        captured.append((current, project_id, estimates))
        return sentinel

    monkeypatch.setattr(planning_module, "plan_work_breakdown_effort", fake_plan)

    result = plan_work_breakdown_effort_durably(
        portfolio.id, project.id, repository, reader
    )

    assert result is sentinel
    assert captured == [(portfolio, project.id, (estimate,))]
    assert repository.saved == []


def test_repository_load_error_propagates_unchanged() -> None:
    portfolio, project, _ = _portfolio()
    repository = FakePortfolioRepository(load_error=_SentinelError("load blew up"))
    reader = FakeEstimateReader()

    with pytest.raises(_SentinelError, match="load blew up"):
        plan_work_breakdown_effort_durably(
            portfolio.id, project.id, repository, reader
        )

    assert reader.portfolio_calls == []


def test_reader_error_propagates_unchanged() -> None:
    portfolio, project, _ = _portfolio()
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    reader = FakeEstimateReader(list_error=_SentinelError("read blew up"))

    with pytest.raises(_SentinelError, match="read blew up"):
        plan_work_breakdown_effort_durably(
            portfolio.id, project.id, repository, reader
        )

    assert repository.saved == []


def _cycle_portfolio() -> tuple[Portfolio, TrajectoryEntity]:
    root = TrajectoryEntity(
        id=uuid4(),
        entity_type=EntityType.PROJECT,
        title="Cycle root",
        created_at=BASE,
        updated_at=BASE,
    )
    package = TrajectoryEntity(
        id=uuid4(),
        entity_type=EntityType.WORK_PACKAGE,
        title="Package",
        created_at=BASE,
        updated_at=BASE,
    )
    subpackage = TrajectoryEntity(
        id=uuid4(),
        entity_type=EntityType.WORK_PACKAGE,
        title="Subpackage",
        created_at=BASE,
        updated_at=BASE,
    )
    portfolio = Portfolio(
        id=uuid4(),
        name="Durable cycle",
        entities=[root, package, subpackage],
        relations=[
            TrajectoryRelation(
                source_id=package.id,
                target_id=root.id,
                relation_type=RelationType.BELONGS_TO,
            ),
            TrajectoryRelation(
                source_id=subpackage.id,
                target_id=package.id,
                relation_type=RelationType.BELONGS_TO,
            ),
            # Completes a reachable containment cycle: package <-> subpackage.
            TrajectoryRelation(
                source_id=package.id,
                target_id=subpackage.id,
                relation_type=RelationType.BELONGS_TO,
            ),
        ],
    )
    return portfolio, root


def test_wbs_domain_error_propagates_unchanged_after_successful_load_and_read(
) -> None:
    """Acceptance #62: a V1.1 work-breakdown *domain* error must propagate
    unchanged through ``plan_work_breakdown_effort_durably`` only *after* both
    the Portfolio load and the estimate read have succeeded, and without any
    Portfolio save. The durable boundary must not wrap or swallow it."""
    portfolio, root = _cycle_portfolio()

    repository = FakePortfolioRepository({portfolio.id: portfolio})
    # Empty tuple is returned successfully: the estimate read did succeed, and
    # the failure is purely a V1.1 WBS domain error raised during planning.
    reader = FakeEstimateReader()

    with pytest.raises(WorkBreakdownError, match="cycle") as excinfo:
        plan_work_breakdown_effort_durably(
            portfolio.id, root.id, repository, reader
        )

    # Exact type, not a subclass and not re-wrapped by this boundary.
    assert type(excinfo.value) is WorkBreakdownError
    # Load happened, then reads happened -- the error surfaced only after both.
    assert repository.loaded_ids == [portfolio.id]
    assert reader.portfolio_calls == [portfolio.id]
    # No save is ever performed for a planning boundary error.
    assert repository.saved == []


def test_planning_error_propagates_unchanged_after_successful_load_and_read(
) -> None:
    """Acceptance #62: a V1.10 *planning* error must propagate unchanged
    through the durable boundary only *after* the Portfolio load and the
    estimate read have both succeeded, without triggering any save."""
    portfolio, project, task = _portfolio()

    # An estimate whose portfolio does not match the loaded portfolio is a
    # legitimate V1.10 planning error, not a repository/reader error.
    foreign = ExecutionEffortEstimate(
        id=uuid4(),
        portfolio_id=uuid4(),  # intentionally a different portfolio
        entity_id=task.id,
        duration_seconds=90,
        estimated_at=BASE,
        source=SourceKind.USER_CONFIRMED,
    )

    repository = FakePortfolioRepository({portfolio.id: portfolio})
    # The reader returns the foreign estimate successfully; the planning
    # boundary's job is to pass its domain error through unchanged.
    reader = FakeEstimateReader((foreign,))

    with pytest.raises(ExecutionEffortPlanningError, match="different portfolio") as excinfo:
        plan_work_breakdown_effort_durably(
            portfolio.id, project.id, repository, reader
        )

    assert type(excinfo.value) is ExecutionEffortPlanningError
    assert repository.loaded_ids == [portfolio.id]
    assert reader.portfolio_calls == [portfolio.id]
    assert repository.saved == []
