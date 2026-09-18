"""Mission 011 — deterministic tests for live status attribution.

GitHub issue #213 / the M011 continuation require the trajectory-pi live
display to be *truthful* for remote implementation models: a remote agent
must never be presented as an idle local Ollama/GPU workload, and local
GPU telemetry must be attributed to the local reviewer (or to no actor
during deterministic validation) instead of to the agent.

These tests exercise the pure attribution model directly, so every
required state is covered without spawning a wrapper or touching a GPU.
"""

from __future__ import annotations

from trajectory_os import live_status

# ---------------------------------------------------------------------------
# provider locality classification
# ---------------------------------------------------------------------------


def test_remote_agent_model_is_classified_remote() -> None:
    assert live_status.classify_provider("deepseek-flash") == (
        "deepseek", live_status.REMOTE)
    assert live_status.classify_provider("openai:gpt-5") == (
        "openai", live_status.REMOTE)
    assert live_status.classify_provider("anthropic/claude-sonnet") == (
        "anthropic", live_status.REMOTE)


def test_default_local_ollama_model_is_classified_local() -> None:
    assert live_status.classify_provider("qwen3.6:27b") == (
        "ollama", live_status.LOCAL)
    assert live_status.classify_provider("qwen3.8-dev3090") == (
        "ollama", live_status.LOCAL)


def test_explicit_provider_always_wins() -> None:
    assert live_status.classify_provider(
        "qwen3.8-dev3090", explicit_provider="deepseek") == (
        "deepseek", live_status.REMOTE)
    assert live_status.classify_provider(
        "deepseek-flash", explicit_provider="ollama") == (
        "ollama", live_status.LOCAL)


def test_unknown_provider_prefix_is_unknown_not_falsely_local() -> None:
    assert live_status.classify_provider(
        "some-unrecognised-model") == ("ollama", live_status.LOCAL)
    # An explicit, unrecognised provider is unknown, never assumed local.
    assert live_status.classify_provider(
        "m", explicit_provider="acme-cloud") == (
        "acme-cloud", live_status.UNKNOWN)


# ---------------------------------------------------------------------------
# GPU ownership
# ---------------------------------------------------------------------------


def test_remote_agent_gpu_is_idle_when_reviewer_idle() -> None:
    # Required state (M012): remote agent + local reviewer idle.  The
    # physical GPU sample must not be presented as reviewer ownership.
    role = live_status.gpu_role(
        live_status.PHASE_IMPLEMENT, live_status.REMOTE, False)
    assert role == live_status.ACTOR_IDLE


def test_remote_agent_gpu_is_review_only_when_reviewer_active() -> None:
    role = live_status.gpu_role(
        live_status.PHASE_IMPLEMENT, live_status.REMOTE, True)
    assert role == live_status.ACTOR_REVIEWER


def test_local_agent_gpu_is_attributed_to_the_agent() -> None:
    # Required state: local agent + local reviewer.
    role = live_status.gpu_role(
        live_status.PHASE_IMPLEMENT, live_status.LOCAL, False)
    assert role == live_status.ACTOR_AGENT


def test_validation_gpu_has_no_actor() -> None:
    # Required state: validation (deterministic, no model).
    role = live_status.gpu_role(
        live_status.PHASE_VALIDATE, live_status.REMOTE, False)
    assert role == live_status.ACTOR_NONE
    role = live_status.gpu_role(
        live_status.PHASE_VALIDATE, live_status.LOCAL, True)
    assert role == live_status.ACTOR_NONE


def test_local_review_active_attributes_gpu_to_reviewer() -> None:
    # Required state: local review active.
    role = live_status.gpu_role(
        live_status.PHASE_REVIEW, live_status.REMOTE, True)
    assert role == live_status.ACTOR_REVIEWER


def test_repair_using_remote_agent_keeps_gpu_idle() -> None:
    # Required state (M012): repair using a remote agent, reviewer idle.
    role = live_status.gpu_role(
        live_status.PHASE_REPAIR, live_status.REMOTE, False)
    assert role == live_status.ACTOR_IDLE


# ---------------------------------------------------------------------------
# generation attribution
# ---------------------------------------------------------------------------


def test_remote_agent_generation_is_remote_na() -> None:
    assert live_status.agent_generation(
        live_status.PHASE_IMPLEMENT, live_status.REMOTE, "10.5") == (
        "remote/n-a")
    assert live_status.agent_generation(
        live_status.PHASE_REPAIR, live_status.REMOTE, None) == (
        "remote/n-a")


def test_local_agent_generation_reports_rate_or_unavailable() -> None:
    assert live_status.agent_generation(
        live_status.PHASE_IMPLEMENT, live_status.LOCAL, "36.4") == (
        "36.4 tok/s")
    # Required state: unavailable generation telemetry.
    assert live_status.agent_generation(
        live_status.PHASE_IMPLEMENT, live_status.LOCAL, None) == (
        "local/unavailable")
    assert live_status.agent_generation(
        live_status.PHASE_IMPLEMENT, live_status.LOCAL, "unavailable") == (
        "local/unavailable")


def test_generation_is_na_outside_agent_phases() -> None:
    for phase in (live_status.PHASE_VALIDATE, live_status.PHASE_REVIEW):
        assert live_status.agent_generation(
            phase, live_status.LOCAL, "10.5") == "n/a"


# ---------------------------------------------------------------------------
# one-line heartbeat rendering
# ---------------------------------------------------------------------------


def _heartbeat(**overrides: object) -> str:
    base: dict[str, object] = {
        "ts": "12:00:10",
        "elapsed": "00:01:00",
        "phase": live_status.PHASE_IMPLEMENT,
        "agent_backend": "pi",
        "agent_provider": "deepseek",
        "agent_model": "deepseek-flash",
        "agent_locality": live_status.REMOTE,
        "agent_state": "active",
        "reviewer_provider": "ollama",
        "reviewer_model": "qwen3.6:27b",
        "reviewer_locality": live_status.LOCAL,
        "reviewer_state": "idle",
        "gpu_util": "0",
        "gpu_used_mib": "21961",
        "gpu_total_mib": "24576",
        "agent_gen": "remote/n-a",
        "files": "3",
        "files_delta": "+2",
    }
    base.update(overrides)
    return live_status.render_heartbeat_line(**base)  # type: ignore[arg-type]


def test_remote_heartbeat_is_one_line_with_true_attribution() -> None:
    line = _heartbeat()
    assert "\n" not in line
    assert "phase=IMPLEMENT" in line
    assert "agent=pi/deepseek:deepseek-flash remote active" in line
    assert "reviewer=ollama:qwen3.6:27b local idle" in line
    assert "local-gpu=idle 0% 21961/24576MiB" in line
    assert "agent-gen=remote/n-a" in line
    assert "files=3 (+2)" in line
    # A remote agent is never presented as an idle local Ollama workload
    # and the new canonical line carries no duplicated legacy tokens.
    assert "ollama=" not in line
    assert "GPU " not in line
    assert "VRAM " not in line
    assert "gen_3s=" not in line
    assert "local-gpu(" not in line


def test_provider_prefix_is_never_duplicated_in_the_heartbeat() -> None:
    # Required state (M012): the raw model already begins with the
    # provider name; presentation must not repeat it.
    line = _heartbeat(agent_model="deepseek/deepseek-flash")
    assert "agent=pi/deepseek:deepseek-flash remote active" in line
    assert "pi/deepseek:deepseek/deepseek-flash" not in line


def test_provider_prefix_normalization_is_generic() -> None:
    line = _heartbeat(
        agent_provider="openai",
        agent_model="openai/gpt-5",
        agent_locality=live_status.REMOTE,
        agent_gen="remote/n-a",
    )
    assert "agent=pi/openai:gpt-5 remote active" in line
    assert "openai:openai/gpt-5" not in line


def test_local_heartbeat_attributes_gpu_and_generation_to_agent() -> None:
    line = _heartbeat(
        phase=live_status.PHASE_IMPLEMENT,
        agent_provider="ollama",
        agent_model="qwen3.8-dev3090",
        agent_locality=live_status.LOCAL,
        agent_gen="36.4 tok/s",
    )
    assert "agent=pi/ollama:qwen3.8-dev3090 local active" in line
    assert "local-gpu=agent 0% 21961/24576MiB" in line
    assert "agent-gen=36.4 tok/s" in line


def test_validation_heartbeat_never_describes_the_validator() -> None:
    line = _heartbeat(
        phase=live_status.PHASE_VALIDATE,
        agent_state="idle",
        agent_gen="n/a",
        gpu_util="0",
    )
    assert "phase=VALIDATE" in line
    assert "local-gpu=n/a" in line
    assert "agent-gen=n/a" in line
    # No actor label may claim the GPU belongs to the validator.
    assert "local-gpu=agent" not in line
    assert "local-gpu=review" not in line
    # Even a physical sample must not leak into the VALIDATE rendering.
    assert "0%" not in line


def test_local_review_heartbeat_shows_reviewer_active() -> None:
    line = _heartbeat(
        phase=live_status.PHASE_REVIEW,
        agent_state="idle",
        reviewer_state="active",
        agent_gen="n/a",
    )
    assert "phase=REVIEW" in line
    assert "reviewer=ollama:qwen3.6:27b local active" in line
    assert "local-gpu=review " in line


def test_unavailable_gpu_telemetry_is_explicit() -> None:
    line = _heartbeat(
        phase=live_status.PHASE_IMPLEMENT,
        agent_locality=live_status.REMOTE,
        gpu_util=None,
        gpu_used_mib=None,
        gpu_total_mib=None,
    )
    assert "local-gpu=idle unavailable" in line


# ---------------------------------------------------------------------------
# banner rendering
# ---------------------------------------------------------------------------


def test_banner_makes_provider_locality_obvious() -> None:
    banner = live_status.render_banner(
        agent_backend="pi",
        agent_provider="deepseek",
        agent_model="deepseek-flash",
        agent_locality=live_status.REMOTE,
        reviewer_provider="ollama",
        reviewer_model="qwen3.6:27b",
        reviewer_locality=live_status.LOCAL,
    )
    text = "\n".join(banner)
    assert "Agent backend   pi" in text
    assert "Agent provider  deepseek (remote)" in text
    assert "Agent model     deepseek-flash" in text
    assert "Reviewer        ollama/qwen3.6:27b (local)" in text
    assert "Local GPU       idle until local model activity; " \
        "reviewer during REVIEW" in text


def test_banner_model_is_normalized() -> None:
    banner = live_status.render_banner(
        agent_backend="pi",
        agent_provider="deepseek",
        agent_model="deepseek/deepseek-flash",
        agent_locality=live_status.REMOTE,
        reviewer_provider="ollama",
        reviewer_model="qwen3.6:27b",
        reviewer_locality=live_status.LOCAL,
    )
    text = "\n".join(banner)
    assert "Agent model     deepseek-flash" in text
    assert "deepseek/deepseek-flash" not in text


def test_normalize_model_is_idempotent_and_scoped() -> None:
    assert live_status.normalize_model(
        "deepseek", "deepseek/deepseek-flash") == "deepseek-flash"
    assert live_status.normalize_model(
        "deepseek", "deepseek-flash") == "deepseek-flash"
    assert live_status.normalize_model(
        "ollama", "qwen3.8-dev3090") == "qwen3.8-dev3090"
    assert live_status.normalize_model(
        "deepseek", "openai/gpt-5") == "openai/gpt-5"


def test_render_attribution_block_is_consistent() -> None:
    block = live_status.render_attribution(
        phase=live_status.PHASE_IMPLEMENT,
        agent_backend="pi",
        agent_provider="deepseek",
        agent_model="deepseek-flash",
        agent_locality=live_status.REMOTE,
        agent_state="active",
        reviewer_provider="ollama",
        reviewer_model="qwen3.6:27b",
        reviewer_locality=live_status.LOCAL,
        reviewer_state="idle",
        agent_gen="remote/n-a",
        gpu_util="0",
        gpu_used_mib="21961",
        gpu_total_mib="24576",
    )
    assert block["gpu_role"] == live_status.ACTOR_IDLE
    assert block["local_gpu"] == "local-gpu=idle 0% 21961/24576MiB"
    assert block["agent_gen"] == "remote/n-a"
