"""Integration test: durable work-breakdown acceptance against real SQLite.

Exercises the full V1.6-B path with no doubles: the real
``SqlitePortfolioRepository`` persists the canonical portfolio, and
``accept_work_breakdown_proposal_durably`` (structurally typed as
``PortfolioRepository``) loads, accepts, and saves it back to the same
SQLite file, which is then reloaded to prove durability.
"""

from pathlib import Path
from uuid import uuid4

from trajectory_os.adapters.persistence.sqlite import SqlitePortfolioRepository
from trajectory_os.application import (
    PortfolioRepository,
    accept_work_breakdown_proposal_durably,
)
from trajectory_os.domain.entities import EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown_proposals import (
    ProposedWorkNode,
    WorkBreakdownProposal,
)


def test_accept_work_breakdown_durably_round_trips_through_sqlite(
    tmp_path: Path,
) -> None:
    # 1. Create PROJECT and DELIVERABLE.
    project = TrajectoryEntity(
        entity_type=EntityType.PROJECT,
        title="TrajectoryOS",
    )
    deliverable = TrajectoryEntity(
        entity_type=EntityType.DELIVERABLE,
        title="V1.6-B Integration",
    )

    # 2. DELIVERABLE BELONGS_TO PROJECT.
    deliverable_belongs_to_project = TrajectoryRelation(
        source_id=deliverable.id,
        target_id=project.id,
        relation_type=RelationType.BELONGS_TO,
    )

    # 3. Build Portfolio with them.
    portfolio = Portfolio(
        id=uuid4(),
        name="V1.6-B",
        entities=[project, deliverable],
        relations=[deliverable_belongs_to_project],
    )

    # 4. Save it to a temporary SQLite database.
    sqlite_repository = SqlitePortfolioRepository(
        tmp_path / "portfolio.db"
    )
    repository: PortfolioRepository = sqlite_repository
    repository.save(portfolio)

    # 6. Snapshot the original Portfolio and proposal.
    portfolio_snapshot = portfolio.model_dump()
    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=deliverable.id,
        children=(
            # 7. Proposal anchored at DELIVERABLE: WORK_PACKAGE └── TASK.
            ProposedWorkNode(
                entity_type=EntityType.WORK_PACKAGE,
                title="Backend",
                confidence=0.9,
                children=(
                    ProposedWorkNode(
                        entity_type=EntityType.TASK,
                        title="Implement API",
                        confidence=0.9,
                    ),
                ),
            ),
        ),
    )
    proposal_snapshot = proposal.model_dump()

    # 8. Call durable acceptance through the structural repository boundary.
    result = accept_work_breakdown_proposal_durably(
        portfolio.id, proposal, repository
    )

    # 9. Reload the Portfolio from SQLite.
    reloaded = repository.load(portfolio.id)
    assert reloaded is not None
    sqlite_repository.close()

    # The accepted portfolio keeps the original identity.
    assert result.portfolio.id == portfolio.id

    # Exactly 2 entity ids were created, WORK_PACKAGE then TASK.
    assert len(result.created_entity_ids) == 2
    reloaded_entities = {entity.id: entity for entity in reloaded.entities}
    assert reloaded_entities[result.created_entity_ids[0]].entity_type is (
        EntityType.WORK_PACKAGE
    )
    assert reloaded_entities[result.created_entity_ids[1]].entity_type is (
        EntityType.TASK
    )

    # Exactly 2 relation ids were created; the i-th created relation is the
    # BELONGS_TO edge of the i-th created entity (parent = anchor or parent).
    assert len(result.created_relation_ids) == 2
    reloaded_relations = {
        relation.id: relation for relation in reloaded.relations
    }
    assert reloaded_relations[result.created_relation_ids[0]].source_id == (
        result.created_entity_ids[0]
    )
    assert reloaded_relations[result.created_relation_ids[1]].source_id == (
        result.created_entity_ids[1]
    )
    assert reloaded_relations[result.created_relation_ids[0]].target_id == (
        deliverable.id
    )
    assert reloaded_relations[result.created_relation_ids[1]].target_id == (
        result.created_entity_ids[0]
    )

    # The persisted state is the accepted portfolio, exactly.
    assert reloaded == result.portfolio

    # Canonical list order round-trips for both entities and relations.
    assert [entity.id for entity in reloaded.entities] == [
        entity.id for entity in result.portfolio.entities
    ]
    assert reloaded.entities == result.portfolio.entities
    assert [relation.id for relation in reloaded.relations] == [
        relation.id for relation in result.portfolio.relations
    ]
    assert reloaded.relations == result.portfolio.relations

    # Purity: the caller's portfolio and proposal snapshots are unchanged.
    assert portfolio.model_dump() == portfolio_snapshot
    assert proposal.model_dump() == proposal_snapshot

    # Exactly one acceptance occurred: SQLite holds only the original 2
    # members plus exactly the 2 new entities and 2 new relations.
    original_entity_ids = {
        entity.id for entity in [project, deliverable]
    }
    assert {entity.id for entity in reloaded.entities} == (
        original_entity_ids | set(result.created_entity_ids)
    )
    original_relation_ids = {deliverable_belongs_to_project.id}
    assert {relation.id for relation in reloaded.relations} == (
        original_relation_ids | set(result.created_relation_ids)
    )
    assert len(reloaded.entities) == 4
    assert len(reloaded.relations) == 3
