"""``trajectory_os.agents`` — M015 bounded agent-backend abstraction.

A stable, provider-agnostic contract around one bounded agent run: launch,
session identity, lifecycle/status, structured events, final result,
cancellation/timeout, completion evidence, and error/fallback provenance.

Backends:

* ``pi`` — the proven local autonomous developer (subprocess contract);
* ``deepseek-harness`` — the official DeepSeek Harness runtime adapter
  (``dsh --profile sdk``, newline-delimited JSON-RPC 2.0).

Design invariants (ADR-014): the core depends on the contract, never on a
specific SDK; structured lifecycle/result evidence is the primary completion
proof for DeepSeek Harness; unavailable/incompatible harnesses yield
deterministic results and fall back to proven Pi; provider-specific model
naming stays adapter-owned.
"""

from trajectory_os.agents.model import (  # noqa: F401
    AGENT_VERSION,
    BACKEND_DEEPSEEK_HARNESS,
    BACKEND_PI,
    SCHEMA_VERSION,
    AgentRequest,
    AgentResult,
    BackendProbe,
    CanaryOutcome,
)

__all__ = [
    "AGENT_VERSION", "BACKEND_DEEPSEEK_HARNESS", "BACKEND_PI",
    "SCHEMA_VERSION", "AgentRequest", "AgentResult", "BackendProbe",
    "CanaryOutcome",
]
