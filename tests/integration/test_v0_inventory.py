"""End-to-end evidence for the public V0 inventory gate.

Path under test:

    examples/v0_inventory.json
        -> import_portfolio_file
        -> canonical Portfolio
        -> V0.5 queries
        -> SqlitePortfolioRepository
        -> fresh reload
        -> V0.5 queries still work

Only production APIs are used; no JSON parsing, SQL row access, mapping
logic, or persistence logic is duplicated here.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from trajectory_os.adapters.persistence.sqlite import SqlitePortfolioRepository
from trajectory_os.domain.entities import EntityStatus, EntityType, SourceKind, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType
from trajectory_os.importers import import_portfolio_file
from trajectory_os.importers.identity import canonicalize_import_id

SOURCE_NAMESPACE = "trajectory-os-demo"
PORTFOLIO_EXTERNAL_ID = "trajectory-os-v0-demo"
PORTFOLIO_NAME = "TrajectoryOS V0 Demo Inventory"

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "examples" / "v0_inventory.json"


def _entity_id(external_id: str) -> UUID:
    """Canonical UUID for a demo entity, via the production identity contract."""

    return canonicalize_import_id(
        kind="entity",
        source_namespace=SOURCE_NAMESPACE,
        external_id=external_id,
    )


def _relation_id(external_id: str) -> UUID:
    """Canonical UUID for a demo relation, via the production identity contract."""

    return canonicalize_import_id(
        kind="relation",
        source_namespace=SOURCE_NAMESPACE,
        external_id=external_id,
    )


def _portfolio_id() -> UUID:
    """Canonical UUID for the demo portfolio, via the production identity contract."""

    return canonicalize_import_id(
        kind="portfolio",
        source_namespace=SOURCE_NAMESPACE,
        external_id=PORTFOLIO_EXTERNAL_ID,
    )


def _topology(portfolio: Portfolio) -> list[tuple[UUID, UUID, UUID, RelationType]]:
    """Ordered (id, source_id, target_id, relation_type) tuples over Portfolio.relations."""

    return [
        (relation.id, relation.source_id, relation.target_id, relation.relation_type)
        for relation in portfolio.relations
    ]


def _titles(entities: list[TrajectoryEntity]) -> list[str]:
    return [entity.title for entity in entities]


def _assert_public_fixture_shape(portfolio: Portfolio) -> None:
    """Shared V0.6-A fixture invariants: shape, identity, counts, provenance."""

    assert portfolio.id == _portfolio_id()
    assert portfolio.name == PORTFOLIO_NAME
    assert len(portfolio.entities) == 12
    assert len(portfolio.relations) == 14
    assert all(entity.source == SourceKind.IMPORTED for entity in portfolio.entities)
    assert all(relation.source == SourceKind.IMPORTED for relation in portfolio.relations)


def test_v0_inventory_import_and_navigation() -> None:
    """The committed fixture imports cleanly and is queryable through V0.5 APIs."""

    portfolio = import_portfolio_file(FIXTURE_PATH)

    _assert_public_fixture_shape(portfolio)

    # 1) TASK entities, in canonical fixture order.
    tasks = portfolio.filter_entities(entity_type=EntityType.TASK)
    assert _titles(tasks) == [
        "Create public inventory fixture",
        "Verify persistence round-trip",
    ]

    # 2) COMPLETED entities, in canonical fixture order.
    completed = portfolio.filter_entities(status=EntityStatus.COMPLETED)
    assert _titles(completed) == [
        "Create public inventory fixture",
        "Separate public demo data from private local inventory",
    ]

    # 3) Conjunctive filter.
    incubator_imported_tasks = portfolio.filter_entities(
        entity_type=EntityType.TASK,
        status=EntityStatus.INCUBATOR,
        source=SourceKind.IMPORTED,
    )
    assert _titles(incubator_imported_tasks) == ["Verify persistence round-trip"]

    # Relation navigation via canonical UUIDs.
    project_v06 = _entity_id("project-v06")

    outgoing = portfolio.outgoing_relations(project_v06)
    assert [(r.relation_type, r.target_id) for r in outgoing] == [
        (RelationType.BELONGS_TO, _entity_id("program-v0")),
        (RelationType.PRODUCES, _entity_id("deliverable-v0-gate")),
    ]

    incoming = portfolio.incoming_relations(project_v06)
    assert [(r.relation_type, r.source_id) for r in incoming] == [
        (RelationType.BELONGS_TO, _entity_id("wp-inventory-proof")),
        (RelationType.CONTRIBUTES_TO, _entity_id("research-query-proof")),
        (RelationType.RELATED_TO, _entity_id("decision-public-private")),
    ]

    # Exact RelationType filter.
    produces = portfolio.outgoing_relations(project_v06, relation_type=RelationType.PRODUCES)
    assert [(r.id, r.source_id, r.target_id, r.relation_type) for r in produces] == [
        (
            _relation_id("rel-project-v06-produces-deliverable-v0-gate"),
            _entity_id("project-v06"),
            _entity_id("deliverable-v0-gate"),
            RelationType.PRODUCES,
        )
    ]


def test_v0_inventory_reimport_is_deterministic() -> None:
    """Importing the same fixture twice yields identical canonical identities and topology."""

    first = import_portfolio_file(FIXTURE_PATH)
    second = import_portfolio_file(FIXTURE_PATH)

    assert first.id == second.id
    assert [e.id for e in first.entities] == [e.id for e in second.entities]
    assert [r.id for r in first.relations] == [r.id for r in second.relations]
    assert _topology(first) == _topology(second)


def test_v0_inventory_persistence_roundtrip_remains_navigable(tmp_path: Path) -> None:
    """A SQLite round-trip preserves the inventory and keeps V0.5 navigation intact."""

    original = import_portfolio_file(FIXTURE_PATH)
    _assert_public_fixture_shape(original)

    database = tmp_path / "v0_inventory.db"

    repository = SqlitePortfolioRepository(database)
    repository.save(original)
    repository.close()

    fresh = SqlitePortfolioRepository(database)
    loaded = fresh.load(original.id)
    fresh.close()

    assert loaded is not None

    _assert_public_fixture_shape(loaded)

    assert loaded.name == original.name
    assert [e.id for e in loaded.entities] == [e.id for e in original.entities]
    assert [r.id for r in loaded.relations] == [r.id for r in original.relations]
    assert _topology(loaded) == _topology(original)

    # Post-reload V0.5 queries on the RELOADED portfolio.
    assert _titles(loaded.filter_entities(entity_type=EntityType.TASK)) == [
        "Create public inventory fixture",
        "Verify persistence round-trip",
    ]

    project_v06 = _entity_id("project-v06")

    outgoing = loaded.outgoing_relations(project_v06)
    assert [(r.relation_type, r.target_id) for r in outgoing] == [
        (RelationType.BELONGS_TO, _entity_id("program-v0")),
        (RelationType.PRODUCES, _entity_id("deliverable-v0-gate")),
    ]

    incoming = loaded.incoming_relations(project_v06)
    assert [(r.relation_type, r.source_id) for r in incoming] == [
        (RelationType.BELONGS_TO, _entity_id("wp-inventory-proof")),
        (RelationType.CONTRIBUTES_TO, _entity_id("research-query-proof")),
        (RelationType.RELATED_TO, _entity_id("decision-public-private")),
    ]

    produces = loaded.outgoing_relations(project_v06, relation_type=RelationType.PRODUCES)
    assert [(r.id, r.source_id, r.target_id, r.relation_type) for r in produces] == [
        (
            _relation_id("rel-project-v06-produces-deliverable-v0-gate"),
            _entity_id("project-v06"),
            _entity_id("deliverable-v0-gate"),
            RelationType.PRODUCES,
        )
    ]
