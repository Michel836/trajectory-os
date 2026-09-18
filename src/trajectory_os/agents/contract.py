"""Mission 015 — the Trajectory_OS-owned agent-backend contract (pure).

The core depends on this protocol, never on a specific harness or SDK. A
backend exposes a deterministic capability probe and one bounded run.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from trajectory_os.agents.model import AgentRequest, AgentResult, BackendProbe


@runtime_checkable
class AgentBackend(Protocol):
    """Stable provider-agnostic agent-backend contract."""

    name: str

    def probe(self) -> BackendProbe:
        """Return a deterministic capability probe (never launches a model)."""
        ...  # pragma: no cover

    def run(self, request: AgentRequest, *,
            cancel: object | None = None) -> AgentResult:
        """Run one bounded task, returning structured lifecycle evidence."""
        ...  # pragma: no cover
