"""Canonical domain model for TrajectoryOS."""

from trajectory_os.domain.classification import (
    EntityClassificationProposal,
    EntityClassifier,
    classify_entity,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import (
    WorkBreakdownError,
    WorkBreakdownNode,
    WorkBreakdownStructure,
    build_work_breakdown,
)

__all__ = [
    "EntityClassificationProposal",
    "EntityClassifier",
    "EntityStatus",
    "EntityType",
    "Portfolio",
    "RelationType",
    "SourceKind",
    "TrajectoryEntity",
    "TrajectoryRelation",
    "WorkBreakdownError",
    "WorkBreakdownNode",
    "WorkBreakdownStructure",
    "build_work_breakdown",
    "classify_entity",
]
