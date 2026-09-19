"""M026 — provider-grounded token/context telemetry unit tests."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.agents import model as agent_model
from trajectory_os.agents import telemetry


def _result(*payloads: dict[str, object],
            runtime_ms: int | None = None) -> agent_model.AgentResult:
    events = [
        agent_model.AgentEvent.build(
            sequence=index, kind=agent_model.LK_RESULT, method="result",
            payload=payload)
        for index, payload in enumerate(payloads)
    ]
    return agent_model.AgentResult.build(
        backend=agent_model.BACKEND_PI, status=agent_model.RS_COMPLETED,
        reason=agent_model.R_OK, events=events, runtime_ms=runtime_ms,
        completion=agent_model.CompletionEvidence.build(
            source=agent_model.CS_EXIT_CODE_MARKER, reliable=True,
            detail="ok"))


def test_provider_usage_is_read_and_total_is_derived() -> None:
    result = _result({"usage": {"prompt_tokens": 120,
                                "completion_tokens": 30,
                                "cache_read_input_tokens": 64}},
                     runtime_ms=1500)
    metrics = telemetry.from_result(result, provider="ollama",
                                    model="qwen3.6:27b")
    assert metrics.prompt_tokens == 120
    assert metrics.completion_tokens == 30
    assert metrics.total_tokens == 150
    assert metrics.sources["total_tokens"] == telemetry.SRC_DERIVED
    assert metrics.cache_read_tokens == 64
    assert metrics.cache_hit is True
    assert metrics.runtime_ms == 1500
    assert metrics.sources["runtime_ms"] == telemetry.SRC_LOCAL
    assert metrics.sources["cost_usd"] == telemetry.SRC_UNAVAILABLE


def test_nested_provider_cost_alias_is_grounded() -> None:
    result = _result({"response": {"usage_metadata": {
        "input_tokens": 10, "output_tokens": 5, "total_cost_usd": 0.002}}})
    metrics = telemetry.from_result(result)
    assert metrics.prompt_tokens == 10
    assert metrics.completion_tokens == 5
    assert metrics.total_tokens == 15
    assert metrics.cost_usd == 0.002
    assert metrics.sources["cost_usd"] == telemetry.SRC_PROVIDER


def test_unavailable_metrics_are_never_invented() -> None:
    result = _result({"note": "no usage block"})
    metrics = telemetry.from_result(result, provider="ollama", model="m")
    assert metrics.prompt_tokens is None
    assert metrics.completion_tokens is None
    assert metrics.total_tokens is None
    assert metrics.cache_read_tokens is None
    assert metrics.cache_hit is None
    assert metrics.cost_usd is None
    assert metrics.runtime_ms is None
    for metric in ("prompt_tokens", "completion_tokens", "total_tokens",
                   "cache_read_tokens", "cache_hit", "cost_usd", "runtime_ms"):
        assert metrics.sources[metric] == telemetry.SRC_UNAVAILABLE


def test_cache_miss_is_grounded_not_guessed() -> None:
    result = _result({"input_tokens": 7, "cached_tokens": 0})
    metrics = telemetry.from_result(result)
    assert metrics.cache_read_tokens == 0
    assert metrics.cache_hit is False


def test_identity_is_deterministic_and_record_is_idempotent(
        tmp_path: Path) -> None:
    result = _result({"total_tokens": 42, "cost": 0.01})
    metrics = telemetry.from_result(result, provider="p", model="m")
    assert metrics.telemetry_id == metrics.compute_telemetry_id()
    assert telemetry.record(tmp_path, metrics) is True
    assert telemetry.record(tmp_path, metrics) is False
    document = telemetry.reconstruct(tmp_path)
    assert document["count"] == 1
    assert document["records"][0]["telemetry_id"] == metrics.telemetry_id


def test_render_is_grounded() -> None:
    result = _result({"prompt_tokens": 3, "completion_tokens": 4})
    text = telemetry.render(telemetry.from_result(result))
    assert "prompt=3" in text
    assert "total=7" in text
    assert "cost      : - usd" in text


def test_from_dict_round_trip() -> None:
    result = _result({"total_tokens": 9})
    metrics = telemetry.from_result(result, provider="p", model="m")
    restored = telemetry.UsageMetrics.from_dict(metrics.to_dict())
    assert restored == metrics
