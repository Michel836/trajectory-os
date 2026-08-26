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
