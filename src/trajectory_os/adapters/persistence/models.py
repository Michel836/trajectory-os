"""Explicit SQLite table models for the canonical portfolio domain.

The storage representation is intentionally explicit:

- UUID values are stored as 36-character text (RFC 4122 string form);
- domain enums are stored by their string value;
- datetime values are stored as ISO-8601 text, preserving the timezone
  offset when present; reconstruction uses ``datetime.fromisoformat()``;
- confidence is stored as a float;
- ``entities.position`` and ``relations.position`` store the zero-based
  ordinal of the row inside the portfolio's canonical ordered list, so
  ``save()``/``load()`` round-trip list order exactly.

Semantic reconstruction of domain values happens explicitly at the
adapter boundary (see ``sqlite.py``); the tables never rely on SQLite
pretending to have native UUID, enum, or timezone-aware datetime semantics.

V0 pre-release note: the schema is intentionally unversioned and there is
no migration framework. Databases created before the ``position`` columns
existed must be recreated during V0 development.
"""

from __future__ import annotations

from sqlalchemy import (
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for the SQLite persistence schema."""


class ExecutionEffortObservationRow(Base):
    """One durable execution-effort observation, owned by a portfolio.

    Append-only: the row has no update or delete representation fields.
    ``entity_id`` is intentionally NOT a foreign key into
    ``entities.id``: historical observations must remain independent from
    the replaceable per-portfolio entity snapshot rows, so deleting an
    entity from (or out of) a portfolio must never delete history.
    """

    __tablename__ = "execution_effort_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    entity_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[str] = mapped_column(String(), nullable=False)
    source: Mapped[str] = mapped_column(String(), nullable=False)


class ExecutionEffortEstimateRow(Base):
    """One durable planned direct-effort estimate, owned by a portfolio.

    Append-only: the row has no update or delete representation fields.
    ``entity_id`` is intentionally NOT a foreign key into
    ``entities.id``: historical estimates must remain independent from
    the replaceable per-portfolio entity snapshot rows, so deleting an
    entity from (or out of) a portfolio must never delete history.
    """

    __tablename__ = "execution_effort_estimates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_at: Mapped[str] = mapped_column(String(), nullable=False)
    source: Mapped[str] = mapped_column(String(), nullable=False)


class AcceptedCalibratedEstimateRevisionRow(Base):
    """One durable, immutable calibrated-estimate-revision provenance row.

    Append-only: the row has no update or delete representation fields.

    One-to-one identity: ``estimate_id`` IS the primary key and references
    exactly one row of ``execution_effort_estimates.id``. A row exists
    only for an estimate accepted through V1.21, so ``estimate_id`` is
    always present on stored provenance rows; plain V1.10 estimates
    simply have no provenance row at all (the estimate table, not this
    table, remains the source of truth for the estimate itself), and the
    one-to-one identity prevents the same estimate from ever carrying two
    different accepted calibrated revisions.

    ``portfolio_id`` is a foreign key into ``portfolios.id`` with
    ``ON DELETE CASCADE`` (same convention as the estimate/observation
    tables). ``project_id`` / ``entity_id`` / ``entity_type`` are
    explicit immutable snapshot columns and carry NO foreign keys into
    ``entities.id`` by design: historical provenance must survive the
    replacement or removal of entity snapshot rows in later portfolio
    saves.

    The exact accepted V1.20 snapshot (with its nested V1.19 result and
    V1.18 proposal provenance chain) is retained as deterministic
    explicit JSON in ``accepted_v120_snapshot``, produced by Pydantic
    JSON-compatible serialization and re-validated through ``model_validate``
    on read-back. This is NOT a pickle and NOT an opaque binary blob;
    every core identifier and the accepted arithmetic (candidate /
    calibrated durations) is additionally stored in the dedicated
    INTEGER/TEXT columns above for queryability and direct corruption
    visibility, and ``entity_type`` is stored by its enum ``.value``.
    """

    __tablename__ = "accepted_calibrated_estimate_revisions"

    # Exact one-to-one link into ``execution_effort_estimates.id``;
    # PRIMARY KEY and always present on stored rows (see class doc).
    # Deliberately NOT a foreign key into the estimate table: historical
    # history must not be deleted by dropping/recreating the estimate
    # snapshot table during V0 development, and no SQLite-specific
    # NULL-PK behavior is ever relied upon (stored values are always
    # present, so portability across SQLite versions is never an issue).
    estimate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Explicit immutable snapshot columns (no FKs into entities.id, so
    # provenance survives entity replacement/removal via portfolio save).
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(), nullable=False)
    # Exact INTEGER snapshots of the accepted V1.20 arithmetic; the
    # durations are never stored as REAL.
    candidate_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    calibrated_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # ISO-8601 aware timestamp text, same convention as the estimate row.
    estimated_at: Mapped[str] = mapped_column(String(), nullable=False)
    # Deterministic explicit JSON of the EXACT accepted V1.20 snapshot
    # (V1.20 -> V1.19 -> V1.18 provenance chain). Never a pickle.
    accepted_v120_snapshot: Mapped[str] = mapped_column(String(), nullable=False)


class ExecutionEffortCalibrationFactorDecisionRow(Base):
    """One durable, immutable human decision over a V1.15 factor proposal.

    Append-only: the row has no update or delete representation fields.
    ``portfolio_id``/``project_id``/``entity_type`` record the exact scope
    of the reviewed segment; every V1.15 evidence value is a fixed
    snapshot (exact INTEGER numerator/denominator, never REAL) and later
    V1.15 drift must not change stored rows.
    """

    __tablename__ = "execution_effort_calibration_factor_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_required_sample_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    total_planned_duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    total_actual_duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    # Exact boolean snapshot stored as 0/1 integer; there is no SQLite
    # boolean semantic assumption.
    proposal_available: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_reason: Mapped[str] = mapped_column(String(), nullable=False)
    factor_numerator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    factor_denominator: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision: Mapped[str] = mapped_column(String(), nullable=False)
    decided_at: Mapped[str] = mapped_column(String(), nullable=False)


class PortfolioRow(Base):
    """One canonical portfolio."""

    __tablename__ = "portfolios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(), nullable=False)


class EntityRow(Base):
    """One trajectory entity, owned by a portfolio."""

    __tablename__ = "entities"

    # The composite unique constraint is the referenced target of the
    # same-portfolio relation foreign keys below.
    __table_args__ = (
        UniqueConstraint("portfolio_id", "id", name="uq_entities_portfolio_id_entity_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Zero-based ordinal in the Portfolio.entities snapshot list. Storage
    # representation only; it is not a domain field.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(), nullable=False)
    title: Mapped[str] = mapped_column(String(), nullable=False)
    description: Mapped[str | None] = mapped_column(String(), nullable=True)
    status: Mapped[str] = mapped_column(String(), nullable=False)
    source: Mapped[str] = mapped_column(String(), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[str] = mapped_column(String(), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(), nullable=False)


class RelationRow(Base):
    """One directed relation between two entities of the same portfolio."""

    __tablename__ = "relations"

    # The composite foreign keys prove that both endpoints are owned by
    # this relation's portfolio, not merely that the entities exist
    # somewhere in the database.
    __table_args__ = (
        ForeignKeyConstraint(
            ["portfolio_id", "source_id"],
            ["entities.portfolio_id", "entities.id"],
            name="fk_relations_portfolio_id_source_entity",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["portfolio_id", "target_id"],
            ["entities.portfolio_id", "entities.id"],
            name="fk_relations_portfolio_id_target_entity",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Zero-based ordinal in the Portfolio.relations snapshot list. Storage
    # representation only; it is not a domain field.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str] = mapped_column(String(), index=True, nullable=False)
    target_id: Mapped[str] = mapped_column(String(), index=True, nullable=False)
    relation_type: Mapped[str] = mapped_column(String(), nullable=False)
    source: Mapped[str] = mapped_column(String(), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
