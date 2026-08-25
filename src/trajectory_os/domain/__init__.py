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
    is_work_breakdown_containment_allowed,
)
from trajectory_os.domain.work_breakdown_acceptance import (
    WorkBreakdownAcceptanceError,
    WorkBreakdownAcceptanceResult,
    accept_work_breakdown_proposal,
)
from trajectory_os.domain.work_breakdown_production import (
    WorkBreakdownProposalContextItem,
    WorkBreakdownProposalProducer,
    WorkBreakdownProposalProductionError,
    WorkBreakdownProposalRequest,
    propose_work_breakdown,
)
from trajectory_os.domain.work_breakdown_proposals import (
    ProposedWorkNode,
    ValidatedWorkBreakdownProposal,
    WorkBreakdownProposal,
    WorkBreakdownProposalError,
    validate_work_breakdown_proposal,
)

__all__ = [
    "EntityClassificationProposal",
    "EntityClassifier",
    "EntityStatus",
    "EntityType",
    "Portfolio",
    "ProposedWorkNode",
    "RelationType",
    "SourceKind",
    "TrajectoryEntity",
    "TrajectoryRelation",
    "ValidatedWorkBreakdownProposal",
    "WorkBreakdownAcceptanceError",
    "WorkBreakdownAcceptanceResult",
    "WorkBreakdownError",
    "WorkBreakdownNode",
    "WorkBreakdownProposal",
    "WorkBreakdownProposalContextItem",
    "WorkBreakdownProposalError",
    "WorkBreakdownProposalProducer",
    "WorkBreakdownProposalProductionError",
    "WorkBreakdownProposalRequest",
    "WorkBreakdownStructure",
    "accept_work_breakdown_proposal",
    "build_work_breakdown",
    "classify_entity",
    "is_work_breakdown_containment_allowed",
    "propose_work_breakdown",
    "validate_work_breakdown_proposal",
]
