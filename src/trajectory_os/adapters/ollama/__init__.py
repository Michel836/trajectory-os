"""Ollama adapter for work-breakdown proposals (V1.5-A/V1.5-B).

Provides only the adapter package, the adapter-specific error type,
validated producer configuration, the deterministic V1.5-B request-context
serialization and ``/api/chat`` payload builders, and a minimal stdlib-only
HTTP POST seam. Response parsing and
:class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposal`
construction belong to later micro-steps and are intentionally absent here.
"""

from trajectory_os.adapters.ollama.work_breakdown import (
    OllamaWorkBreakdownProposalError,
    OllamaWorkBreakdownProposalProducer,
)

__all__ = [
    "OllamaWorkBreakdownProposalError",
    "OllamaWorkBreakdownProposalProducer",
]
