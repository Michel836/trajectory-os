"""M023 — isolated DeepSeek Harness qualification unit tests (no network/Git)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trajectory_os.agents import harness_qualification as hq
from trajectory_os.agents import identity as agent_identity
from trajectory_os.agents import model
from trajectory_os.agents.contract import AgentBackend


def _make_env(root: Path, *, sdk_version: str = "0.1.5rc1",
              python: bool = True, runtime: bool = True) -> Path:
    (root / "bin").mkdir(parents=True, exist_ok=True)
    if python:
        p = root / "bin" / "python"
        p.write_text("#!/bin/sh\n", encoding="utf-8")
        p.chmod(0o755)
    if runtime:
        r = root / "bin" / "dsh"
        r.write_text("#!/bin/sh\n", encoding="utf-8")
        r.chmod(0o755)
    site = root / "lib" / "python3.14" / "site-packages"
    dist = site / f"{hq.SDK_DISTRIBUTION.replace('-', '_')}-{sdk_version}.dist-info"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: deepseek-harness-sdk\nVersion: {sdk_version}\n",
        encoding="utf-8")
    return root


# --- environment discovery ----------------------------------------------------


def test_missing_environment_is_deterministic(tmp_path: Path) -> None:
    env = hq.resolve_environment(root=str(tmp_path / "absent"))
    assert env.status == hq.ES_MISSING
    assert env.present is False
    assert env.reason == model.R_SDK_MISSING
    assert env.environment_id == env.compute_environment_id()


def test_environment_discovers_python_runtime_and_version(
        tmp_path: Path) -> None:
    root = _make_env(tmp_path / "sdk")
    env = hq.resolve_environment(root=str(root), which=lambda _: None)
    assert env.status == hq.ES_PRESENT
    assert env.present is True
    assert env.python == str(root / "bin" / "python")
    assert env.runtime == str(root / "bin" / "dsh")
    assert env.sdk_version == "0.1.5rc1"
    assert env.reason == model.R_OK


def test_environment_without_interpreter_is_invalid(tmp_path: Path) -> None:
    root = _make_env(tmp_path / "sdk", python=False)
    env = hq.resolve_environment(root=str(root), which=lambda _: None)
    assert env.status == hq.ES_INVALID
    assert env.python is None


def test_environment_from_env_var(tmp_path: Path) -> None:
    root = _make_env(tmp_path / "sdk")
    env = hq.resolve_environment(
        environment={hq.HARNESS_SDK_ROOT_ENV: str(root)},
        which=lambda _: None)
    assert env.root == str(root)
    assert env.status == hq.ES_PRESENT


def test_dist_version_uses_numeric_ordering(tmp_path: Path) -> None:
    root = tmp_path / "sdk"
    _make_env(root, sdk_version="0.1.5rc1")
    _make_env(root, sdk_version="0.1.10")
    env = hq.resolve_environment(root=str(root), which=lambda _: None)
    assert env.sdk_version == "0.1.10"


# --- isolation ----------------------------------------------------------------


def test_sanitized_environment_drops_every_python_path() -> None:
    base = {
        "PYTHONPATH": "/sdk/site-packages",
        "PYTHONHOME": "/sdk",
        "PYTHONSTARTUP": "/sdk/start.py",
        "PATH": "/usr/bin",
        "HOME": "/home/u",
    }
    env = hq.sanitized_environment(base, venv_root="/sdk")
    for banned in hq.ISOLATED_ENV_DROP:
        assert banned not in env
    assert env["VIRTUAL_ENV"] == "/sdk"
    assert env["PATH"].startswith("/sdk/bin" + ":")
    assert env["HOME"] == "/home/u"


# --- identity probe -----------------------------------------------------------


def _ok_runner(python: str, script: str, env: dict[str, str],
               timeout_s: int) -> tuple[int, str, str]:
    assert "PYTHONPATH" not in env
    return 0, ('{"module_present": true, "sdk_version": "0.1.5rc1", '
               '"python_version": "3.14.4"}\n'), ""


def _ok_runtime(argv: list[str], timeout_s: int) -> tuple[int, str, str]:
    return 0, "0.1.5-rc.1\n", ""


def test_identity_probe_ok(tmp_path: Path) -> None:
    root = _make_env(tmp_path / "sdk")
    env = hq.resolve_environment(root=str(root), which=lambda _: None)
    identity = hq.probe_identity(env, runner=_ok_runner,
                                 runtime_runner=_ok_runtime)
    assert identity.status == hq.IS_OK
    assert identity.module_present is True
    assert identity.sdk_version == "0.1.5rc1"
    assert identity.python_version == "3.14.4"
    assert identity.runtime_version == "0.1.5-rc.1"
    assert identity.identity_id == identity.compute_identity_id()


def test_identity_probe_timeout_is_incompatible(tmp_path: Path) -> None:
    import subprocess

    root = _make_env(tmp_path / "sdk")
    env = hq.resolve_environment(root=str(root), which=lambda _: None)

    def timeout_runner(python: str, script: str, env_: dict[str, str],
                       timeout_s: int) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired(cmd=python, timeout=timeout_s)

    identity = hq.probe_identity(env, runner=timeout_runner)
    assert identity.status == hq.IS_INCOMPATIBLE
    assert identity.reason == model.R_TIMEOUT


def test_identity_probe_missing_environment() -> None:
    env = hq.resolve_environment(root="/nonexistent/sdk")
    identity = hq.probe_identity(env)
    assert identity.status == hq.IS_UNAVAILABLE
    assert identity.module_present is False


# --- isolated backend factory -------------------------------------------------


def test_isolated_factory_injects_sdk_facts(tmp_path: Path) -> None:
    root = _make_env(tmp_path / "sdk")
    env = hq.resolve_environment(root=str(root), which=lambda _: None)
    backend: Any = hq.isolated_backend_factory(env)(
        model.BACKEND_DEEPSEEK_HARNESS)
    assert isinstance(backend, AgentBackend)
    probe = backend.probe()
    assert probe.available is True
    assert probe.sdk_version == "0.1.5rc1"


# --- composed qualification ---------------------------------------------------


class _FakeHarness:
    name = model.BACKEND_DEEPSEEK_HARNESS

    def probe(self) -> model.BackendProbe:
        return model.BackendProbe(
            backend=self.name, available=True, reason=model.R_OK,
            transport=model.TRANSPORT_RUNTIME, sdk_version="0.1.5rc1")

    def run(self, request: model.AgentRequest, *,
            cancel: object | None = None) -> model.AgentResult:
        return model.AgentResult.build(
            backend=self.name, status=model.RS_COMPLETED, reason=model.R_OK,
            events=[model.AgentEvent.build(
                sequence=0, kind=model.LK_INITIALIZED,
                method="initialize")],
            completion=model.CompletionEvidence.build(
                source=model.CS_LIFECYCLE_IDLE, reliable=True, detail="idle"),
            transport=model.TRANSPORT_RUNTIME)


class _FakePi:
    name = model.BACKEND_PI

    def probe(self) -> model.BackendProbe:
        return model.BackendProbe(
            backend=self.name, available=True, reason=model.R_OK,
            transport=model.TRANSPORT_SUBPROCESS)

    def run(self, request: model.AgentRequest, *,
            cancel: object | None = None) -> model.AgentResult:
        return model.AgentResult.build(
            backend=self.name, status=model.RS_COMPLETED, reason=model.R_OK,
            completion=model.CompletionEvidence.build(
                source=model.CS_EXIT_CODE_MARKER, reliable=True, detail="pi"))


def _factory(name: str) -> AgentBackend:
    return _FakeHarness() if name == model.BACKEND_DEEPSEEK_HARNESS \
        else _FakePi()


def test_qualify_isolated_records_deterministic_evidence(
        tmp_path: Path) -> None:
    root = _make_env(tmp_path / "sdk")
    env = hq.resolve_environment(root=str(root), which=lambda _: None)
    identity = hq.probe_identity(env, runner=_ok_runner,
                                 runtime_runner=_ok_runtime)
    outcome = hq.qualify_isolated(
        workspace=str(tmp_path / "ws"), environment=env, identity=identity,
        factory=_factory)
    document = outcome.to_dict()
    assert outcome.git_writes is False
    assert document["environment"]["status"] == hq.ES_PRESENT
    assert document["identity"]["status"] == hq.IS_OK
    assert outcome.checks["sdk_identity"] == hq.CK_PASS
    assert outcome.checks["runtime_identity"] == hq.CK_PASS
    assert outcome.isolation["sdk_imported_into_project"] is False
    assert outcome.isolation["pythonpath_dropped"] is True
    assert outcome.fallback["pi_route"]["model"] == "deepseek-flash"


def test_qualify_isolated_is_unavailable_when_sdk_missing() -> None:
    from trajectory_os.agents import qualification

    env = hq.resolve_environment(root="/nonexistent/sdk")
    identity = hq.probe_identity(env)
    outcome = hq.qualify_isolated(
        workspace="/tmp", environment=env, identity=identity)
    assert outcome.status == qualification.QS_UNAVAILABLE
    assert hq.pi_continues(outcome) is True


# --- identity domains ---------------------------------------------------------


def test_harness_identity_domains_are_distinct() -> None:
    payload = {"a": 1}
    assert agent_identity.harness_environment_id(payload) != \
        agent_identity.harness_identity_id(payload)
    assert agent_identity.HARNESS_ENVIRONMENT_DOMAIN not in \
        (agent_identity.RUN_DOMAIN, agent_identity.CANARY_DOMAIN)
    assert agent_identity.HARNESS_IDENTITY_DOMAIN in agent_identity.DOMAIN_IDS


# --- full JSON-RPC handshake through the isolated factory ---------------------

import json  # noqa: E402
import queue  # noqa: E402

from trajectory_os.agents.cancellation import CancellationToken  # noqa: E402


class _TextQueue:
    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()

    def put(self, item: str | None) -> None:
        self._q.put(item)

    def readline(self) -> str:
        item = self._q.get()
        return item if item is not None else ""


class _ScriptedProc:
    """Minimal scripted JSON-RPC runtime for the isolated factory seam."""

    def __init__(self, *, respond: bool = True, hang_prompt: bool = False,
                 hang_idle: bool = False) -> None:
        self._out = _TextQueue()
        self.stdin = self
        self.stdout = self._out
        self.stderr = _TextQueue()
        self._respond = respond
        self._hang_prompt = hang_prompt
        self._hang_idle = hang_idle
        self._rc: int | None = None

    def write(self, line: str) -> None:
        frame = json.loads(line)
        req_id = frame.get("id")
        method = frame.get("method")
        if method == "initialize":
            if not self._respond:
                return
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"serverInfo": {
                    "name": "deepseek-harness-sdk-runtime"}}}) + "\n")
        elif method == "session/prompt":
            if not self._respond or self._hang_prompt:
                return
            session_id = frame["params"]["sessionId"]
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"messageId": "msg-1"}}) + "\n")
            if self._hang_idle:
                return
            self._out.put(json.dumps({
                "jsonrpc": "2.0", "method": "session.status",
                "params": {"sessionId": session_id,
                           "status": "idle"}}) + "\n")
            self._out.put(None)
        elif method == "shutdown":
            self._out.put(None)

    def flush(self) -> None:
        return None

    def poll(self) -> int | None:
        return self._rc

    def terminate(self) -> None:
        self._rc = 0
        self._out.put(None)

    def kill(self) -> None:
        self._rc = -9

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _isolated_backend(tmp_path: Path, proc: Any) -> Any:
    root = _make_env(tmp_path / "sdk")
    env = hq.resolve_environment(root=str(root), which=lambda _: None)
    factory = hq.isolated_backend_factory(
        env, popener=lambda argv: proc, credentials_present=lambda: True)
    return factory(model.BACKEND_DEEPSEEK_HARNESS)


def test_isolated_backend_full_handshake_completes(tmp_path: Path) -> None:
    backend = _isolated_backend(tmp_path, _ScriptedProc())
    result = backend.run(model.AgentRequest(
        task="canary", workspace=str(tmp_path), timeout_s=5))
    assert result.status == model.RS_COMPLETED
    assert result.completion is not None
    assert result.completion.source == model.CS_LIFECYCLE_IDLE
    kinds = [event.kind for event in result.events]
    assert model.LK_INITIALIZED in kinds
    assert model.LK_IDLE in kinds


def test_isolated_backend_timeout_is_structured(tmp_path: Path) -> None:
    backend = _isolated_backend(tmp_path, _ScriptedProc(hang_prompt=True))
    result = backend.run(model.AgentRequest(
        task="canary", workspace=str(tmp_path), timeout_s=1))
    assert result.status == model.RS_TIMEOUT
    assert result.reason == model.R_TIMEOUT
    assert result.completion is not None
    assert result.completion.reliable is False


def test_isolated_backend_cancellation_is_observed(tmp_path: Path) -> None:
    backend = _isolated_backend(tmp_path, _ScriptedProc(hang_idle=True))
    cancel = CancellationToken()
    cancel.cancel()
    result = backend.run(
        model.AgentRequest(task="canary", workspace=str(tmp_path),
                           timeout_s=5),
        cancel=cancel)
    assert result.status == model.RS_CANCELLED
    assert result.reason == model.R_CANCELLED
