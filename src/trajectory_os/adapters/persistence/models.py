"""Explicit SQLite table models for the canonical portfolio domain.

The storage representation is intentionally explicit:

- UUID values are stored as 36-character text (RFC 4122 string form);
- domain enums are stored by their string value;
- aware datetimes are stored as ISO-8601 text preserving the UTC offset;
- confidence is stored as a float.

Semantic reconstruction of domain values happens explicitly at the
adapter boundary (see ``sqlite.py``); the tables never rely on SQLite
pretending to have native UUID, enum, or timezone-aware datetime semantics.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for the SQLite persistence schema."""


class PortfolioRow(Base):
    """One canonical portfolio."""

    __tablename__ = "portfolios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(), nullable=False)


class EntityRow(Base):
    """One trajectory entity, owned by a portfolio."""

    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
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

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    relation_type: Mapped[str] = mapped_column(String(), nullable=False)
    source: Mapped[str] = mapped_column(String(), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
