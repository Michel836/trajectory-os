"""Real local Ollama evidence for TrajectoryOS V1.5.

This script exercises:

Portfolio
→ V1.4 deterministic proposal production
→ real Ollama /api/chat
→ V1.5 strict response parsing
→ V1.2 deterministic validation
→ safe WorkBreakdownProposal

It deliberately stops before V1.3 acceptance.
"""

from __future__ import annotations

import os

from trajectory_os.adapters.ollama import (
    OllamaWorkBreakdownProposalProducer,
)
from trajectory_os.domain import (
    EntityType,
    Portfolio,
    RelationType,
    TrajectoryEntity,
    TrajectoryRelation,
    WorkBreakdownProposal,
    propose_work_breakdown,
)


def main() -> None:
    model = os.environ["OLLAMA_MODEL"]
    base_url = os.environ.get(
        "OLLAMA_BASE_URL",
        "http://localhost:11434",
    )
    timeout = float(os.environ.get("OLLAMA_TIMEOUT", "120"))

    project = TrajectoryEntity(
        entity_type=EntityType.PROJECT,
        title="TrajectoryOS V1.5 real Ollama evidence",
        description=(
            "Prove that the real local Ollama adapter can propose "
            "missing implementation work through the controlled V1.4 boundary."
        ),
    )

    deliverable = TrajectoryEntity(
        entity_type=EntityType.DELIVERABLE,
        title="Validated local Ollama adapter",
        description=(
            "A real Ollama model must propose concrete missing work "
            "needed to verify the adapter."
        ),
    )

    portfolio = Portfolio(
        name="V1.5 Ollama evidence",
        entities=[project, deliverable],
        relations=[
            TrajectoryRelation(
                source_id=deliverable.id,
                target_id=project.id,
                relation_type=RelationType.BELONGS_TO,
            )
        ],
    )

    before = portfolio.model_dump()

    producer = OllamaWorkBreakdownProposalProducer(
        model=model,
        base_url=base_url,
        timeout=timeout,
    )

    proposal = propose_work_breakdown(
        portfolio,
        project.id,
        deliverable.id,
        producer,
    )

    assert isinstance(proposal, WorkBreakdownProposal)
    assert proposal.project_id == project.id
    assert proposal.anchor_id == deliverable.id

    # Proposal production must not mutate canonical state.
    assert portfolio.model_dump() == before

    print("=== V1.5 REAL OLLAMA EVIDENCE ===")
    print(f"model: {model}")
    print(f"endpoint: {producer.chat_endpoint}")
    print(f"project_id: {project.id}")
    print(f"anchor_id: {deliverable.id}")
    print()
    print(proposal.model_dump_json(indent=2))
    print()
    print("RESULT: PASS")
    print("No V1.3 acceptance performed.")
    print("Canonical Portfolio remained unchanged.")


if __name__ == "__main__":
    main()
