"""Unit tests for durable work-breakdown acceptance (V1.6-A)."""

from uuid import UUID, uuid4

import pytest

from trajectory_os.application import (
    DurableWorkBreakdownAcceptanceError,
    PortfolioNotFoundError,
    accept_work_breakdown_proposal_durably,
)
from trajectory_os.domain.entities import EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.work_breakdown_acceptance import (
    WorkBreakdownAcceptanceError,
    WorkBreakdownAcceptanceResult,
    accept_work_breakdown_proposal,
)
from trajectory_os.domain.work_breakdown_proposals import (
    ProposedWorkNode,
    WorkBreakdownProposal,
)


class _SentinelError(Exception):
    """Repository-side failure with a non-colliding identity."""


class FakePortfolioRepository:
    """In-memory, behaviorally scriptable PortfolioRepository double."""

    def __init__(
        self,
        portfolios: dict[UUID, Portfolio] | None = None,
        *,
        load_error: Exception | None = None,
        save_error: Exception | None = None,
    ) -> None:
        self._portfolios = dict(portfolios or {})
        self._load_error = load_error
        self._save_error = save_error
        self.loaded_ids: list[object] = []
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.loaded_ids.append(portfolio_id)
        if self._load_error is not None:
            raise self._load_error
        return self._portfolios.get(portfolio_id)

    def save(self, portfolio: Portfolio) -> None:
        if self._save_error is not None:
            raise self._save_error
        self.saved.append(portfolio)


def _project_portfolio(name: str) -> Portfolio:
    project = TrajectoryEntity(entity_type=EntityType.PROJECT, title="Platform")
    return Portfolio(id=uuid4(), name=name, entities=[project])


def _valid_proposal(portfolio: Portfolio) -> WorkBreakdownProposal:
    project = portfolio.entities[0]
    task = ProposedWorkNode(
        entity_type=EntityType.TASK, title="Implement API", confidence=0.9
    )
    work_package = ProposedWorkNode(
        entity_type=EntityType.WORK_PACKAGE,
        title="Backend",
        confidence=0.9,
        children=(task,),
    )
    return WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=(work_package,),
    )


def _stale_proposal(portfolio: Portfolio) -> WorkBreakdownProposal:
    """Proposal anchored at an entity that is not part of the portfolio."""

    task = ProposedWorkNode(
        entity_type=EntityType.TASK, title="Implement API", confidence=0.9
    )
    return WorkBreakdownProposal(
        project_id=portfolio.entities[0].id,
        anchor_id=uuid4(),
        children=(task,),
    )


def test_loads_the_requested_portfolio_and_returns_the_exact_v1_3_result() -> None:
    portfolio = _project_portfolio("V1.6-A")
    proposal = _valid_proposal(portfolio)
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    proposal_snapshot = proposal.model_dump()
    portfolio_snapshot = portfolio.model_dump()

    result = accept_work_breakdown_proposal_durably(portfolio.id, proposal, repository)

    # load received exactly the requested portfolio_id
    assert repository.loaded_ids == [portfolio.id]
    # save happened exactly once, with exactly result.portfolio (identity)
    assert len(repository.saved) == 1
    assert repository.saved[0] is result.portfolio
    # the returned object is the exact V1.3 result from the application flow
    assert type(result) is WorkBreakdownAcceptanceResult
    assert result.portfolio.id == portfolio.id
    assert len(result.portfolio.entities) == len(
        result.created_entity_ids
    ) + len(portfolio.entities)
    base_entity_ids = {entity.id for entity in portfolio.entities}
    assert set(result.created_entity_ids).isdisjoint(base_entity_ids)
    new_member_ids = {
        entity.id for entity in result.portfolio.entities if entity.id not in base_entity_ids
    }
    assert new_member_ids == set(result.created_entity_ids)
    assert len(result.portfolio.relations) == len(result.created_relation_ids)
    # independent re-run of the real V1.3 use case is structurally identical
    expected = accept_work_breakdown_proposal(portfolio, proposal)
    assert [
        (entity.entity_type, entity.title)
        for entity in result.portfolio.entities
    ] == [
        (entity.entity_type, entity.title)
        for entity in expected.portfolio.entities
    ]
    # purity: caller proposal and loaded portfolio remain unchanged
    assert proposal.model_dump() == proposal_snapshot
    assert portfolio.model_dump() == portfolio_snapshot


def test_load_receives_requested_portfolio_id_exactly() -> None:
    portfolio = _project_portfolio("V1.6-A")
    proposal = _valid_proposal(portfolio)
    repository = FakePortfolioRepository({portfolio.id: portfolio})

    accept_work_breakdown_proposal_durably(portfolio.id, proposal, repository)

    assert repository.loaded_ids == [portfolio.id]
    assert repository.loaded_ids[0] is portfolio.id


def test_missing_portfolio_raises_portfolio_not_found_without_saving() -> None:
    missing_id = uuid4()
    repository = FakePortfolioRepository()
    proposal = WorkBreakdownProposal(
        project_id=uuid4(),
        anchor_id=uuid4(),
        children=(),
    )

    with pytest.raises(PortfolioNotFoundError) as excinfo:
        accept_work_breakdown_proposal_durably(missing_id, proposal, repository)

    assert isinstance(excinfo.value, DurableWorkBreakdownAcceptanceError)
    assert repository.loaded_ids == [missing_id]
    assert repository.saved == []


@pytest.mark.parametrize(
    "bad_id",
    ["0148790b-ba4c-5f9e-9f6c-8c4e21d5b0c1", 42, b"0148790b", [uuid4()], None, True],
)
def test_invalid_portfolio_id_fails_before_repository_load(bad_id: object) -> None:
    repository = FakePortfolioRepository()

    with pytest.raises(DurableWorkBreakdownAcceptanceError) as excinfo:
        accept_work_breakdown_proposal_durably(  # type: ignore[arg-type]
            bad_id, _valid_proposal(_project_portfolio("V1.6-A")), repository
        )

    # rejected at this boundary, not as a missing portfolio
    assert type(excinfo.value) is DurableWorkBreakdownAcceptanceError
    assert repository.loaded_ids == []
    assert repository.saved == []


def test_stale_proposal_rejection_propagates_and_does_not_save() -> None:
    portfolio = _project_portfolio("V1.6-A")
    repository = FakePortfolioRepository({portfolio.id: portfolio})
    proposal = _stale_proposal(portfolio)
    proposal_snapshot = proposal.model_dump()

    with pytest.raises(WorkBreakdownAcceptanceError):
        accept_work_breakdown_proposal_durably(portfolio.id, proposal, repository)

    assert repository.saved == []
    assert proposal.model_dump() == proposal_snapshot


def test_repository_load_exception_propagates_unchanged() -> None:
    sentinel = _SentinelError("load exploded")
    portfolio_id = uuid4()
    repository = FakePortfolioRepository(load_error=sentinel)

    with pytest.raises(_SentinelError) as excinfo:
        accept_work_breakdown_proposal_durably(
            portfolio_id,
            _valid_proposal(_project_portfolio("V1.6-A")),
            repository,
        )

    assert excinfo.value is sentinel
    assert repository.loaded_ids == [portfolio_id]
    assert repository.saved == []


def test_repository_save_exception_propagates_unchanged() -> None:
    sentinel = _SentinelError("save exploded")
    portfolio = _project_portfolio("V1.6-A")
    proposal = _valid_proposal(portfolio)
    repository = FakePortfolioRepository(
        {portfolio.id: portfolio}, save_error=sentinel
    )

    with pytest.raises(_SentinelError) as excinfo:
        accept_work_breakdown_proposal_durably(portfolio.id, proposal, repository)

    assert excinfo.value is sentinel
    assert repository.loaded_ids == [portfolio.id]
    assert repository.saved == []
