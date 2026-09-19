"""M022 — provider-neutral agent-backend hardening unit tests."""

from __future__ import annotations

from typing import Any

import pytest

from trajectory_os.agents import cancellation, capabilities, lifecycle, model, retry, route
from trajectory_os.agents import identity as agent_identity


class FakeBackend:
    name = model.BACKEND_PI

    def __init__(self, results: list[model.AgentResult], *,
                 available: bool = True, name: str | None = None) -> None:
        self._results = results
        self._available = available
        self.calls = 0
        if name is not None:
            self.name = name

    def probe(self) -> model.BackendProbe:
        return model.BackendProbe(
            backend=self.name,
            available=self._available,
            reason=model.R_OK if self._available
            else model.R_RUNTIME_MISSING,
            transport=model.TRANSPORT_SUBPROCESS)

    def run(self, request: model.AgentRequest, *,
            cancel: object | None = None) -> model.AgentResult:
        self.calls += 1
        return self._results[min(self.calls - 1, len(self._results) - 1)]


def _result(status: str, reason: str, *,
            source: str = model.CS_NONE,
            reliable: bool = False) -> model.AgentResult:
    return model.AgentResult.build(
        backend=model.BACKEND_PI, status=status, reason=reason,
        completion=model.CompletionEvidence.build(
            source=source, reliable=reliable, detail=reason))


def _request() -> model.AgentRequest:
    return model.AgentRequest(task="bounded", workspace="/tmp")


def test_cancellation_token_is_thread_safe_flag() -> None:
    token = cancellation.CancellationToken()
    assert token.cancelled is False
    assert token.is_set() is False
    token.cancel()
    assert token.cancelled is True
    assert token.wait(0) is True
    token.reset()
    assert token.is_set() is False


def test_route_build_fails_closed_on_invalid_input() -> None:
    with pytest.raises(model.AgentBackendError):
        route.ProviderRoute.build(
            backend="unknown", provider="p", model_name="m")
    with pytest.raises(model.AgentBackendError):
        route.ProviderRoute.build(
            backend=model.BACKEND_PI, provider="", model_name="m")
    with pytest.raises(model.AgentBackendError):
        route.ProviderRoute.build(
            backend=model.BACKEND_PI, provider="p", model_name="m",
            transport="carrier-pigeon")


def test_route_identity_is_exact_and_stable() -> None:
    route_a = route.resolve_route(
        model.BACKEND_DEEPSEEK_HARNESS,
        model.AgentRequest(task="x", workspace="/tmp", model="deepseek-pro"),
        transport=model.TRANSPORT_RUNTIME)
    route_b = route.resolve_route(
        model.BACKEND_DEEPSEEK_HARNESS,
        model.AgentRequest(task="x", workspace="/tmp", model="deepseek-pro"),
        transport=model.TRANSPORT_RUNTIME)
    assert route_a.route_id == route_b.route_id
    assert route_a.route_id == route_a.compute_route_id()
    assert route_a.to_dict()["route_id"] == route_a.route_id


def test_capabilities_unavailable_and_unknown() -> None:
    unavailable = capabilities.discover(
        FakeBackend([], available=False))
    assert unavailable.available is False
    assert unavailable.capabilities == ()
    assert unavailable.reason == model.R_RUNTIME_MISSING
    unknown = capabilities.discover(
        FakeBackend([], name="mystery-backend"))
    assert unknown.available is True
    assert unknown.reason == model.R_CAPABILITY_UNKNOWN
    assert unknown.capabilities == ()


def test_retry_policy_validation() -> None:
    retry.RetryPolicy().validate()
    with pytest.raises(model.AgentBackendError):
        retry.RetryPolicy(max_attempts=0).validate()
    with pytest.raises(model.AgentBackendError):
        retry.RetryPolicy(max_attempts=retry.MAX_ATTEMPTS + 1).validate()
    with pytest.raises(model.AgentBackendError):
        retry.RetryPolicy(retry_statuses=(model.RS_UNAVAILABLE,)).validate()


def test_retry_pre_cancel_never_launches_backend() -> None:
    backend = FakeBackend([_result(model.RS_COMPLETED, model.R_OK,
                                   source=model.CS_EXIT_CODE_MARKER,
                                   reliable=True)])
    token = cancellation.CancellationToken()
    token.cancel()
    outcome = retry.run_with_retries(backend, _request(), cancel=token)
    assert backend.calls == 0
    assert outcome.result.status == model.RS_CANCELLED
    assert outcome.attempt_count == 0


def test_retry_does_not_retry_incompatible() -> None:
    backend = FakeBackend([
        _result(model.RS_INCOMPATIBLE, model.R_INCOMPATIBLE_PROTOCOL)])
    outcome = retry.run_with_retries(
        backend, _request(), retry.RetryPolicy(max_attempts=3))
    assert backend.calls == 1
    assert outcome.exhausted is False


def test_lifecycle_summary_roundtrip_and_tamper() -> None:
    result = model.AgentResult.build(
        backend=model.BACKEND_PI, status=model.RS_COMPLETED,
        reason=model.R_OK,
        events=[model.AgentEvent.build(
            sequence=0, kind=model.LK_STARTED, method="subprocess")],
        completion=model.CompletionEvidence.build(
            source=model.CS_EXIT_CODE_MARKER, reliable=True, detail="marker"))
    summary = lifecycle.LifecycleSummary.of(result)
    document = summary.to_dict()
    assert lifecycle.LifecycleSummary.from_dict(document).summary_id == \
        summary.summary_id
    document["flags"] = ["bogus"]
    with pytest.raises(model.AgentBackendError):
        lifecycle.LifecycleSummary.from_dict(document)


def test_m022_identity_domains_are_distinct() -> None:
    payload: dict[str, Any] = {"a": 1}
    digests = {agent_identity.digest(d, payload)
               for d in agent_identity.DOMAIN_IDS}
    assert len(digests) == len(agent_identity.DOMAIN_IDS)
    for domain in (agent_identity.ROUTE_DOMAIN,
                   agent_identity.CAPABILITY_DOMAIN,
                   agent_identity.LIFECYCLE_DOMAIN):
        assert domain in agent_identity.DOMAIN_IDS
