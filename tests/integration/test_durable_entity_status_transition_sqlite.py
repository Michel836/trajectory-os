"""Integration test: V1.7-C durable entity status transition against real SQLite.

Exercises the full successful durable path with no doubles: the real
``SqlitePortfolioRepository`` persists the canonical initial portfolio;
``transition_entity_status_durably`` (structurally typed as
``PortfolioRepository``) loads the CURRENT persisted state, delegates to
the real V1.7-A pure domain transition, and saves the result back to the
same SQLite file, which is then reloaded to prove durability.

No transactionality-across-load/transition/save, optimistic-locking,
concurrency, or event-history claims are made here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from trajectory_os.adapters.persistence.sqlite import SqlitePortfolioRepository
from trajectory_os.application import (
    PortfolioRepository,
    transition_entity_status_durably,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("6ba7b811-9dad-41d2-80b4-000000000001")
PROJECT_ID = UUID("6ba7b811-9dad-41d2-80b4-00000000000c")
TARGET_ENTITY_ID = UUID("6ba7b811-9dad-41d2-80b4-00000000000a")
DELIVERABLE_ID = UUID("6ba7b811-9dad-41d2-80b4-00000000000b")
RELATION_FIRST_ID = UUID("6ba7b811-9dad-41d2-80b4-00000000000e")
RELATION_SECOND_ID = UUID("6ba7b811-9dad-41d2-80b4-00000000000d")

TARGET_INITIAL_UPDATED_AT = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
CHANGED_AT = datetime(2026, 8, 22, 15, 30, 0, tzinfo=UTC)
CALLER_MUTATION_STATUS = EntityStatus.ACTIVE


def _build_initial_portfolio() -> Portfolio:
    """Build the canonical initial portfolio with explicit, deterministic values.

    Deliberate orders: entities ``[PROJECT, TASK, DELIVERABLE]`` and
    relations ``[DELIVERABLE-edge, TASK-edge]`` are both the reverse of
    their UUID lexical order, so preserved ordering cannot be an accident
    of sorted storage. The target entity (the TASK) starts WAITING.
    """
    project = TrajectoryEntity(
        id=PROJECT_ID,
        entity_type=EntityType.PROJECT,
        title="TrajectoryOS",
        description="Adaptive execution and decision-intelligence platform",
        status=EntityStatus.ACTIVE,
        created_at=datetime(2026, 8, 1, 9, 30, 15, tzinfo=UTC),
        updated_at=datetime(2026, 8, 2, 10, 0, 0, tzinfo=UTC),
    )
    target = TrajectoryEntity(
        id=TARGET_ENTITY_ID,
        entity_type=EntityType.TASK,
        title="Write V1.7-C integration proof",
        description=None,
        status=EntityStatus.WAITING,
        created_at=datetime(2026, 7, 20, 8, 0, 0, tzinfo=UTC),
        updated_at=TARGET_INITIAL_UPDATED_AT,
    )
    deliverable = TrajectoryEntity(
        id=DELIVERABLE_ID,
        entity_type=EntityType.DELIVERABLE,
        title="Durable transition proof",
        description=None,
        status=EntityStatus.ACTIVE,
        created_at=datetime(2026, 7, 25, 6, 15, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 3, 7, 45, 30, tzinfo=UTC),
    )

    task_belongs_to_project = TrajectoryRelation(
        id=RELATION_SECOND_ID,
        source_id=TARGET_ENTITY_ID,
        target_id=PROJECT_ID,
        relation_type=RelationType.BELONGS_TO,
    )
    deliverable_belongs_to_project = TrajectoryRelation(
        id=RELATION_FIRST_ID,
        source_id=DELIVERABLE_ID,
        target_id=PROJECT_ID,
        relation_type=RelationType.BELONGS_TO,
    )

    portfolio = Portfolio(
        id=PORTFOLIO_ID,
        name="V1.7-C durable transition",
        entities=[project, target, deliverable],
        relations=[deliverable_belongs_to_project, task_belongs_to_project],
    )

    # Sanity-check the deliberate (reverse-lexical) ordering.
    assert [entity.id for entity in portfolio.entities] != sorted(
        entity.id for entity in portfolio.entities
    )
    assert [relation.id for relation in portfolio.relations] != sorted(
        relation.id for relation in portfolio.relations
    )

    return portfolio


def test_durable_entity_status_transition_round_trips_through_sqlite(
    tmp_path: Path,
) -> None:
    initial_portfolio = _build_initial_portfolio()

    # Repository lifecycle: the concrete repository is kept in a concrete
    # variable for ``close()``; the protocol-typed variable is used only
    # for the structural load/save boundary (it is not closeable).
    sqlite_repository = SqlitePortfolioRepository(tmp_path / "portfolio.db")

    # 1. Concrete SqlitePortfolioRepository structurally satisfies the
    #    existing PortfolioRepository protocol; typed assignment is the
    #    structural conformance evidence.
    repository: PortfolioRepository = sqlite_repository

    # 2. Persist the canonical initial portfolio.
    repository.save(initial_portfolio)

    # The CURRENT persisted state is the authoritative input: read it
    # back before any caller-side mutation.
    persisted_initial = repository.load(initial_portfolio.id)
    assert persisted_initial is not None
    assert persisted_initial == initial_portfolio
    persisted_by_id = {entity.id: entity for entity in persisted_initial.entities}

    # Caller-side mutation WITHOUT saving: the persisted WAITING state,
    # not this value, must be authoritative.
    caller_target = next(
        entity for entity in initial_portfolio.entities if entity.id == TARGET_ENTITY_ID
    )
    caller_target.status = CALLER_MUTATION_STATUS
    caller_state_after_mutation = initial_portfolio.model_dump()

    # 3. Durable transition through the real V1.7-A path, using only
    #    portfolio_id, target entity id, target_status, changed_at, repository.
    result = transition_entity_status_durably(
        portfolio_id=initial_portfolio.id,
        entity_id=TARGET_ENTITY_ID,
        target_status=EntityStatus.COMPLETED,
        changed_at=CHANGED_AT,
        repository=repository,
    )

    # 4. Durable reload from the SAME real SQLite file.
    reloaded = repository.load(initial_portfolio.id)

    # 5. Close the concrete repository after all evidence is captured.
    sqlite_repository.close()

    # --- Result field evidence ------------------------------------------

    # 2. The result names the requested target entity.
    assert result.entity_id == TARGET_ENTITY_ID

    # 3. previous_status reflects the PERSISTED WAITING state, not the
    #    caller-side ACTIVE mutation.
    assert result.previous_status is EntityStatus.WAITING
    assert result.previous_status is not CALLER_MUTATION_STATUS

    # 4. new_status is the requested COMPLETED status.
    assert result.new_status is EntityStatus.COMPLETED

    # 5. changed_at equals the explicit caller input.
    assert result.changed_at == CHANGED_AT

    # 6. Portfolio identity is kept.
    assert result.portfolio.id == PORTFOLIO_ID

    # 7. Against the persisted initial state, only the target entity's
    #    status and updated_at differ.
    expected = persisted_initial.model_copy(deep=True)
    expected_target = next(
        entity for entity in expected.entities if entity.id == TARGET_ENTITY_ID
    )
    expected_target.status = EntityStatus.COMPLETED
    expected_target.updated_at = CHANGED_AT
    assert result.portfolio == expected
    result_by_id = {entity.id: entity for entity in result.portfolio.entities}
    assert result_by_id[TARGET_ENTITY_ID].status is EntityStatus.COMPLETED
    assert result_by_id[TARGET_ENTITY_ID].updated_at == CHANGED_AT
    assert persisted_by_id[TARGET_ENTITY_ID].status is EntityStatus.WAITING
    assert persisted_by_id[TARGET_ENTITY_ID].updated_at == TARGET_INITIAL_UPDATED_AT

    # 11. Durable reload exists in the real SQLite file.
    assert reloaded is not None

    reloaded_by_id = {entity.id: entity for entity in reloaded.entities}

    # 8. Entity IDs and entity ordering are preserved.
    assert [entity.id for entity in result.portfolio.entities] == [
        entity.id for entity in initial_portfolio.entities
    ]
    assert [entity.id for entity in reloaded.entities] == [
        entity.id for entity in initial_portfolio.entities
    ]

    # 9. Relation IDs, relation values, and relation ordering preserved.
    assert [relation.id for relation in result.portfolio.relations] == [
        relation.id for relation in initial_portfolio.relations
    ]
    assert result.portfolio.relations == persisted_initial.relations
    assert reloaded.relations == persisted_initial.relations

    # 10. Unrelated entity values are preserved.
    assert reloaded_by_id[PROJECT_ID] == persisted_by_id[PROJECT_ID]
    assert reloaded_by_id[DELIVERABLE_ID] == persisted_by_id[DELIVERABLE_ID]

    # 12. Durable reload equals the transitioned portfolio exactly.
    assert reloaded == result.portfolio

    # 13/14. Reloaded target entity is COMPLETED, stamped with changed_at.
    assert reloaded_by_id[TARGET_ENTITY_ID].status is EntityStatus.COMPLETED
    assert reloaded_by_id[TARGET_ENTITY_ID].updated_at == CHANGED_AT

    # 15. The post-save caller-side mutation never leaked into persisted state.
    assert reloaded_by_id[TARGET_ENTITY_ID].status is not CALLER_MUTATION_STATUS

    # 16. No duplicate entities or relations appeared.
    assert len(reloaded.entities) == 3
    assert len(reloaded.relations) == 2
    assert len({entity.id for entity in reloaded.entities}) == 3
    assert len({relation.id for relation in reloaded.relations}) == 2

    # 17. The caller-side portfolio is exactly what the caller mutated it
    #     to: the durable use case neither reverted it nor touched it.
    assert caller_target.status is CALLER_MUTATION_STATUS
    assert caller_target.updated_at == TARGET_INITIAL_UPDATED_AT
    assert initial_portfolio.model_dump() == caller_state_after_mutation
