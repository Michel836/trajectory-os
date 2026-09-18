"""Mission 011 — live status attribution for trajectory-pi runs (pure).

The Pi wrapper (``scripts/trajectory-pi``) emits one compact heartbeat
line per interval and a startup banner. Historically those conflated
three distinct concepts:

* the Pi **agent framework** (``backend=pi``);
* the **model the agent actually generates with**, which may live behind
  a remote provider (for example DeepSeek Flash);
* the **local Ollama reviewer** and the local GPU that reviewer uses.

For a remote agent that was actively generating, the old heartbeat still
printed ``ollama=idle`` plus local GPU/VRAM/adapter telemetry, which reads
as if the agent itself were an idle or unhealthy local Ollama/GPU
workload.  The truth is the opposite: the remote agent is active and the
local GPU belongs to the (currently idle) local reviewer.

This module is the canonical, pure attribution model.  It defines:

* provider locality classification (``classify_provider``);
* which actor owns the local GPU telemetry in a given phase
  (``gpu_role``);
* whether the agent's generation throughput is remote/local
  (``agent_generation``);
* the exact one-line heartbeat field layout (``render_heartbeat_line``)
  and the startup banner (``render_banner``).

It performs no I/O, has no clocks and no randomness, so it is fully
deterministic and directly unit-testable.  The shell producer mirrors the
field *names* defined here; the reader (``scripts/trajectory-pi-status``)
consumes the emitted locality directly, so neither side has to guess which
actor a metric belongs to.
"""

from __future__ import annotations

import re

# --- localities ---------------------------------------------------------------

REMOTE = "remote"
LOCAL = "local"
UNKNOWN = "unknown"

LOCALITIES = frozenset({REMOTE, LOCAL, UNKNOWN})

# --- who owns the local GPU telemetry ----------------------------------------

ACTOR_AGENT = "agent"
ACTOR_REVIEWER = "review"
ACTOR_NONE = "none"

# --- canonical phases ---------------------------------------------------------

PHASE_IMPLEMENT = "IMPLEMENT"
PHASE_REPAIR = "REPAIR"
PHASE_RECOVERY = "RECOVERY"
PHASE_SMOKE = "SMOKE"
PHASE_PLAN = "PLAN"
PHASE_VALIDATE = "VALIDATE"
PHASE_REVIEW = "REVIEW"

#: Phases during which the Pi agent is the (only) local generation actor.
AGENT_PHASES = frozenset({
    PHASE_IMPLEMENT, PHASE_REPAIR, PHASE_RECOVERY, PHASE_SMOKE, PHASE_PLAN,
})

# --- provider classification --------------------------------------------------

#: Providers served from the local machine (local GPU is genuinely theirs).
LOCAL_PROVIDERS = frozenset({
    "ollama", "llama.cpp", "llamacpp", "llamafile", "local", "vllm",
    "lmstudio", "lm-studio", "text-generation-webui",
})

#: Providers reached over a network/API (the local GPU is NOT theirs).
REMOTE_PROVIDERS = frozenset({
    "deepseek", "openai", "anthropic", "google", "gemini", "groq",
    "mistral", "xai", "grok", "openrouter", "together", "fireworks",
    "remote", "dsh", "azure", "bedrock", "vertex", "cerebras", "perplexity",
})

_PROVIDER_SPLIT = re.compile(r"[-/:@]")


def classify_provider(
    model: str | None,
    explicit_provider: str | None = None,
) -> tuple[str, str]:
    """Return ``(provider_name, locality)`` for an agent model (pure).

    ``explicit_provider`` always wins.  Otherwise the provider is inferred
    from the model name prefix; a model served by local Ollama
    (``qwen3.8-dev3090``) has no recognised provider prefix and defaults to
    ``ollama``/``local``, preserving the historical default.  A model whose
    prefix names a known remote provider (``deepseek-flash`` ->
    ``deepseek``/``remote``) is attributed to that provider.  Anything else
    is conservatively ``unknown`` so the display never falsely claims the
    local GPU generated a remote model.
    """
    provider = (explicit_provider or "").strip().lower()
    if provider:
        return provider, _locality_of(provider)
    name = (model or "").strip().lower()
    if not name:
        return "ollama", LOCAL
    prefix = _PROVIDER_SPLIT.split(name, maxsplit=1)[0]
    if prefix in REMOTE_PROVIDERS:
        return prefix, REMOTE
    if prefix in LOCAL_PROVIDERS:
        return ("ollama" if prefix == "local" else prefix), LOCAL
    if name.startswith("deepseek") or "deepseek" in name:
        return "deepseek", REMOTE
    # No recognised marker: the historical default agent model
    # (qwen3.8-dev3090) is a local Ollama model.
    return "ollama", LOCAL


def _locality_of(provider: str) -> str:
    if provider in LOCAL_PROVIDERS:
        return LOCAL
    if provider in REMOTE_PROVIDERS:
        return REMOTE
    return UNKNOWN


# --- attribution --------------------------------------------------------------


def gpu_role(phase: str, agent_locality: str, reviewer_active: bool) -> str:
    """Actor whose local GPU resource the telemetry describes (pure).

    * ``REVIEW``  -> the reviewer is the local GPU consumer;
    * ``VALIDATE``-> no model is generating; the GPU belongs to no actor
      (the validator is deterministic code, never a model);
    * agent phases -> the agent when it is local, else the (idle) local
      reviewer that owns the local GPU domain.
    """
    phase_u = (phase or "").strip().upper()
    if phase_u == PHASE_REVIEW:
        return ACTOR_REVIEWER
    if phase_u == PHASE_VALIDATE:
        return ACTOR_NONE
    if phase_u in AGENT_PHASES:
        if agent_locality == LOCAL:
            return ACTOR_AGENT
        if agent_locality == REMOTE:
            return ACTOR_REVIEWER
        return ACTOR_NONE
    if reviewer_active:
        return ACTOR_REVIEWER
    return ACTOR_NONE


def agent_generation(
    phase: str,
    agent_locality: str,
    local_rate: str | None,
) -> str:
    """Truthful agent-generation summary for the heartbeat (pure).

    A remote agent has no local generation telemetry (``remote/n-a``); a
    local agent reports its measured rate or ``local/unavailable`` when the
    native measurement is absent.  Outside agent phases nothing is
    generating on the agent's behalf, so the field is ``n/a``.
    """
    phase_u = (phase or "").strip().upper()
    if phase_u not in AGENT_PHASES:
        return "n/a"
    if agent_locality == LOCAL:
        rate = (local_rate or "").strip()
        if rate and rate != "unavailable":
            return f"{rate} tok/s"
        return "local/unavailable"
    if agent_locality == REMOTE:
        return "remote/n-a"
    return "unknown/n-a"


# --- rendering ----------------------------------------------------------------


def render_heartbeat_line(
    *,
    ts: str,
    elapsed: str,
    phase: str,
    agent_backend: str,
    agent_provider: str,
    agent_model: str,
    agent_locality: str,
    agent_state: str,
    reviewer_provider: str,
    reviewer_model: str,
    reviewer_locality: str,
    reviewer_state: str,
    gpu_util: str | None,
    gpu_used_mib: str | None,
    gpu_total_mib: str | None,
    agent_gen: str,
    files: str | None = None,
    files_delta: str | None = None,
) -> str:
    """One-line, field-stable heartbeat with true attribution (pure).

    Prefix ``[ts] elapsed=... | phase=... | ...`` is deliberately kept for
    the existing read-only status reader; every following field is
    self-describing so no reader has to guess which actor a metric belongs
    to.
    """
    role = gpu_role(phase, agent_locality, reviewer_state == "active")
    gpu = _gpu_fragment(role, gpu_util, gpu_used_mib, gpu_total_mib)
    parts = [
        f"[{ts}] elapsed={elapsed}",
        f"phase={phase}",
        f"agent={agent_backend}/{agent_provider}:{agent_model} "
        f"{agent_locality} {agent_state}",
        f"reviewer={reviewer_provider}:{reviewer_model} "
        f"{reviewer_locality} {reviewer_state}",
        gpu,
        f"agent-gen={agent_gen}",
    ]
    if files is not None:
        delta = f" ({files_delta})" if files_delta else ""
        parts.append(f"files={files}{delta}")
    return " | ".join(parts)


def _gpu_fragment(
    role: str,
    util: str | None,
    used: str | None,
    total: str | None,
) -> str:
    label = f"local-gpu({role})"
    if util is None and used is None and total is None:
        return f"{label}=unavailable"
    util_s = util if util is not None else "?"
    mem = (
        f" {used}/{total}MiB"
        if used is not None and total is not None
        else ""
    )
    return f"{label}={util_s}%{mem}"


def render_banner(
    *,
    agent_backend: str,
    agent_provider: str,
    agent_model: str,
    agent_locality: str,
    reviewer_provider: str,
    reviewer_model: str,
    reviewer_locality: str,
) -> list[str]:
    """Startup banner rows making provider locality explicit (pure)."""
    return [
        f"Agent backend   {agent_backend}",
        f"Agent provider  {agent_provider} ({agent_locality})",
        f"Agent model     {agent_model}",
        f"Reviewer        {reviewer_provider}/{reviewer_model} "
        f"({reviewer_locality})",
        f"Local GPU       reviewer resource during REVIEW; "
        f"unattributed during VALIDATE ({agent_locality} agent)",
    ]


def render_attribution(
    *,
    phase: str,
    agent_backend: str,
    agent_provider: str,
    agent_model: str,
    agent_locality: str,
    agent_state: str,
    reviewer_provider: str,
    reviewer_model: str,
    reviewer_locality: str,
    reviewer_state: str,
    agent_gen: str,
    gpu_util: str | None,
    gpu_used_mib: str | None,
    gpu_total_mib: str | None,
) -> dict[str, str]:
    """Structured attribution block used by the reader/tests (pure)."""
    role = gpu_role(phase, agent_locality, reviewer_state == "active")
    return {
        "phase": phase,
        "agent": (f"{agent_backend}/{agent_provider}:{agent_model} "
                  f"{agent_locality} {agent_state}"),
        "agent_locality": agent_locality,
        "agent_state": agent_state,
        "reviewer": (f"{reviewer_provider}:{reviewer_model} "
                     f"{reviewer_locality} {reviewer_state}"),
        "reviewer_locality": reviewer_locality,
        "reviewer_state": reviewer_state,
        "gpu_role": role,
        "local_gpu": _gpu_fragment(role, gpu_util, gpu_used_mib, gpu_total_mib),
        "agent_gen": agent_gen,
    }
