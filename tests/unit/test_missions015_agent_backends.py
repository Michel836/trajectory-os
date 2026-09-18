"""Mission 015 — agent-backend contract unit tests (no network, no Git)."""

from __future__ import annotations

import json
import queue
from typing import Any

import pytest

from trajectory_os.agents import canary, model, registry
from trajectory_os.agents import identity as agent_identity
from trajectory_os.agents.deepseek_harness import DeepSeekHarnessBackend
from trajectory_os.agents.pi import PiBackend, ProcResult

# --- fakes --------------------------------------------------------------------


class TextQueue:
    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()

    def put(self, item: str | None) -> None:
        self._q.put(item)

    def readline(self) -> str:
        item = self._q.get()
        return item if item is not None else ""


class FakeStdin:
    def __init__(self, proc: FakeProc) -> None:
        self._proc = proc

    def write(self, line: str) -> None:
        self._proc.handle(json.loads(line))

    def flush(self) -> None:
        return None


class FakeProc:
    """Scripted stdout JSON-RPC runtime (models the official SDK server)."""

    def __init__(self, *, server_name: str = "deepseek-harness-sdk-runtime",
                 rpc_error: bool = False) -> None:
        self._out = TextQueue()
        self.stdin = FakeStdin(self)
        self.stdout = self._out
        self.stderr = TextQueue()
        self._server_name = server_name
        self._rpc_error = rpc_error
        self._rc: int | None = None

    def handle(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        req_id = frame.get("id")
        if method == "initialize":
            if self._rpc_error:
                self._out.put(json.dumps({
                    "jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32000, "message": "bad route"}}) + "\n")
                return
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"serverInfo": {"name": self._server_name,
                                          "version": "0.0.1"}}}) + "\n")
        elif method == "session/prompt":
            session_id = frame["params"]["sessionId"]
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"messageId": "msg-1"}}) + "\n")
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "method": "session.status",
                "params": {"sessionId": session_id, "status": "running"}}) + "\n")
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "method": "session.event",
                "params": {"sessionId": session_id,
                           "event": {"type": "assistant",
                                     "text": "canary complete"}}}) + "\n")
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "method": "session.status",
                "params": {"sessionId": session_id, "status": "idle"}}) + "\n")
        elif method == "shutdown":
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "id": req_id, "result": {}}) + "\n")
            self._out.put(None)

    def poll(self) -> int | None:
        return self._rc

    def terminate(self) -> None:
        self._rc = 0

    def kill(self) -> None:
        self._rc = -9

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FakePi:
    name = model.BACKEND_PI

    def __init__(self, *, available: bool = True,
                 result: model.AgentResult | None = None) -> None:
        self._available = available
        self._result = result

    def probe(self) -> model.BackendProbe:
        return model.BackendProbe(
            backend=self.name, available=self._available,
            reason=model.R_OK if self._available else model.R_RUNTIME_MISSING,
            transport=model.TRANSPORT_SUBPROCESS)

    def run(self, request: model.AgentRequest, *,
            cancel: object | None = None) -> model.AgentResult:
        if self._result is not None:
            return self._result
        return model.AgentResult.build(
            backend=self.name, status=model.RS_COMPLETED, reason=model.R_OK)


def _dsh(popener: Any = None, *, sdk: bool = False, credentials: bool = True,
         runtime: bool = True, patch: str | None = None) -> DeepSeekHarnessBackend:
    return DeepSeekHarnessBackend(
        popener=popener,
        which=lambda exe: "/usr/bin/dsh" if runtime else None,
        sdk_finder=lambda name: object() if sdk else None,
        sdk_version=lambda dist: "1.0" if sdk else None,
        credentials_present=lambda: credentials,
        patch=patch)


def _request() -> model.AgentRequest:
    return model.AgentRequest(task="bounded canary", workspace="/tmp",
                              timeout_s=30)


# --- identity -----------------------------------------------------------------


def test_agent_domains_are_distinct() -> None:
    payload = {"a": 1}
    digests = {agent_identity.digest(d, payload)
               for d in agent_identity.DOMAIN_IDS}
    assert len(digests) == len(agent_identity.DOMAIN_IDS)
    with pytest.raises(ValueError):
        agent_identity.digest("nope", payload)


# --- Pi -----------------------------------------------------------------------


def _pi(runner: Any) -> PiBackend:
    return PiBackend(binary="pi", runner=runner,
                     which=lambda exe: "/usr/bin/pi")


def test_pi_marker_contract() -> None:
    def ok_runner(argv: Any, **kwargs: Any) -> ProcResult:
        return ProcResult(0, "HANDOFF\nnotes\nTASK_DONE_COMPLETE\n", "")

    result = _pi(ok_runner).run(_request())
    assert result.status == model.RS_COMPLETED
    assert result.completion is not None
    assert result.completion.reliable
    assert result.completion.source == model.CS_EXIT_CODE_MARKER


def test_pi_missing_marker_fails_closed() -> None:
    def no_marker(argv: Any, **kwargs: Any) -> ProcResult:
        return ProcResult(0, "HANDOFF\nno terminal marker here\n", "")

    result = _pi(no_marker).run(_request())
    assert result.status == model.RS_FAILED
    assert result.completion is not None and not result.completion.reliable


def test_pi_timeout_and_probe() -> None:
    def timeout(argv: Any, **kwargs: Any) -> ProcResult:
        return ProcResult(None, "", "", timed_out=True)

    result = _pi(timeout).run(_request())
    assert result.status == model.RS_TIMEOUT
    assert _pi(timeout).probe().available
    missing = PiBackend(which=lambda exe: None).probe()
    assert not missing.available
    assert missing.reason == model.R_RUNTIME_MISSING


# --- DeepSeek Harness probe ---------------------------------------------------


def test_dsh_probe_reasons() -> None:
    assert not _dsh(runtime=False).probe().available
    assert _dsh(runtime=False).probe().reason == model.R_RUNTIME_MISSING
    assert not _dsh(credentials=False).probe().available
    assert _dsh(credentials=False).probe().reason == model.R_CREDENTIALS_MISSING
    missing_patch = _dsh(patch="/nonexistent/patch.yml")
    assert not missing_patch.probe().available
    assert missing_patch.probe().reason == model.R_PROFILE_MISSING
    probe = _dsh(sdk=False).probe()
    assert probe.available and probe.sdk_version is None
    assert _dsh(sdk=True).probe().sdk_version == "1.0"


# --- DeepSeek Harness runtime -------------------------------------------------


def test_dsh_structured_lifecycle_completion() -> None:
    backend = _dsh(lambda argv: FakeProc())
    result = backend.run(_request())
    assert result.status == model.RS_COMPLETED
    assert result.completion is not None
    assert result.completion.source == model.CS_LIFECYCLE_IDLE
    assert result.completion.reliable
    assert result.final_response == "canary complete"
    kinds = {event.kind for event in result.events}
    assert {model.LK_INITIALIZED, model.LK_IDLE, model.LK_COMPLETED} <= kinds
    assert result.run_id == result.compute_run_id()


def test_dsh_incompatible_server_name() -> None:
    backend = _dsh(lambda argv: FakeProc(server_name="not-the-runtime"))
    result = backend.run(_request())
    assert result.status == model.RS_INCOMPATIBLE
    assert result.reason == model.R_INCOMPATIBLE_PROTOCOL


def test_dsh_rpc_error() -> None:
    backend = _dsh(lambda argv: FakeProc(rpc_error=True))
    result = backend.run(_request())
    assert result.status == model.RS_INCOMPATIBLE
    assert result.reason == model.R_INCOMPATIBLE_PROTOCOL


# --- registry + fallback ------------------------------------------------------


def test_run_with_fallback_records_provenance() -> None:
    def factory(name: str) -> Any:
        if name == model.BACKEND_DEEPSEEK_HARNESS:
            return _dsh(runtime=False)
        return FakePi()

    result = registry.run_with_fallback(
        _request(), factory=factory)
    assert result.backend == model.BACKEND_PI
    assert result.fallback_from == model.BACKEND_DEEPSEEK_HARNESS
    assert result.status == model.RS_COMPLETED


def test_registry_probe_all() -> None:
    probes = registry.probe_all(lambda name: _dsh(sdk=True)
                                if name == model.BACKEND_DEEPSEEK_HARNESS
                                else FakePi())
    assert set(probes) == set(model.BACKENDS)


# --- canary -------------------------------------------------------------------


def test_canary_unavailable_without_sdk() -> None:
    def factory(name: str) -> Any:
        if name == model.BACKEND_DEEPSEEK_HARNESS:
            return _dsh(sdk=False)
        return FakePi()

    outcome = canary.run_canary(_request(), factory=factory)
    assert outcome.status == model.CS_CANARY_UNAVAILABLE
    assert outcome.reason == model.R_SDK_MISSING
    assert outcome.fallback_backend == model.BACKEND_PI
    assert outcome.comparison is not None
    assert outcome.canary_id == outcome.compute_canary_id()


def test_canary_pass_with_sdk_and_fake_runtime() -> None:
    def factory(name: str) -> Any:
        if name == model.BACKEND_DEEPSEEK_HARNESS:
            return _dsh(lambda argv: FakeProc(), sdk=True)
        return FakePi()

    outcome = canary.run_canary(_request(), factory=factory)
    assert outcome.status == model.CS_CANARY_PASS
    assert outcome.reason == model.R_CANARY_PASS
    assert outcome.primary is not None
    assert outcome.comparison is not None
    assert outcome.comparison["completion_reliable"] is True


def test_canary_never_greps_stdout_for_completion() -> None:
    """A backend that only prints text (no structured lifecycle) is not pass."""
    backend = _dsh(lambda argv: _SilentProc(), sdk=True)
    outcome = canary.run_canary(
        _request(), factory=lambda name: backend
        if name == model.BACKEND_DEEPSEEK_HARNESS else FakePi())
    assert outcome.status in (model.CS_CANARY_UNAVAILABLE,
                              model.CS_CANARY_FAIL)


class _SilentProc(FakeProc):
    def handle(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        req_id = frame.get("id")
        if method == "initialize":
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"serverInfo": {"name": "deepseek-harness-sdk-runtime",
                                          "version": "0.0.1"}}}) + "\n")
        elif method == "session/prompt":
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"messageId": "msg-1"}}) + "\n")
            self._out.put(None)


class _TurnErrorProc(FakeProc):
    """Runtime that reports an explicitly errored turn before going idle.

    Models the real DeepSeek Harness runtime when the provider route has no
    credential: the session reaches ``idle`` but the turn ended with a
    structured ``reason.kind == "error"``.
    """

    def handle(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        req_id = frame.get("id")
        if method == "initialize":
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"serverInfo": {"name":
                                           "deepseek-harness-sdk-runtime",
                                           "version": "0.0.1"}}}) + "\n")
        elif method == "session/prompt":
            session_id = frame["params"]["sessionId"]
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"messageId": "msg-1"}}) + "\n")
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "method": "session.status",
                "params": {"sessionId": session_id,
                           "status": "running"}}) + "\n")
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "method": "session.event",
                "params": {
                    "sessionId": session_id,
                    "event": {"type": "turn/end", "data": {
                        "turn": 1,
                        "reason": {"kind": "error", "error": {
                            "code": "MISSING_CREDENTIAL",
                            "message": "no API key for provider route"}},
                    }}}}) + "\n")
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "method": "session.status",
                "params": {"sessionId": session_id,
                           "status": "idle"}}) + "\n")
        elif method == "shutdown":
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "id": req_id, "result": {}}) + "\n")
            self._out.put(None)


def test_dsh_turn_error_fails_closed_not_completed() -> None:
    """A reachable idle after an errored turn is not a reliable completion."""
    backend = _dsh(lambda argv: _TurnErrorProc(), sdk=True)
    result = backend.run(_request())
    assert result.status == model.RS_UNAVAILABLE
    assert result.reason == model.R_CREDENTIALS_MISSING
    assert result.completion is not None
    assert result.completion.reliable is False


def test_canary_turn_error_is_unavailable() -> None:
    backend = _dsh(lambda argv: _TurnErrorProc(), sdk=True)
    outcome = canary.run_canary(
        _request(), factory=lambda name: backend
        if name == model.BACKEND_DEEPSEEK_HARNESS else FakePi())
    assert outcome.status == model.CS_CANARY_UNAVAILABLE
    assert outcome.reason == model.R_CREDENTIALS_MISSING


# --- model --------------------------------------------------------------------


def test_completion_evidence_and_result_identities() -> None:
    evidence = model.CompletionEvidence.build(
        source=model.CS_LIFECYCLE_IDLE, reliable=True, detail="idle")
    assert evidence.evidence_id == evidence.compute_evidence_id()
    assert model.CompletionEvidence.build(
        source=model.CS_NONE, reliable=False, detail="none").reliable is False
    with pytest.raises(model.AgentBackendError):
        model.CompletionEvidence.build(
            source="BOGUS", reliable=True, detail="x")
    result = model.AgentResult.build(
        backend=model.BACKEND_PI, status=model.RS_COMPLETED, reason=model.R_OK,
        completion=evidence)
    assert result.completed
    assert result.run_id and result.to_dict()["run_id"] == result.run_id
    with pytest.raises(model.AgentBackendError):
        model.AgentResult.build(
            backend=model.BACKEND_PI, status="BOGUS", reason=model.R_OK)
