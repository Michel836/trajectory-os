"""Mission 015 — the official DeepSeek Harness backend adapter.

This adapter owns the DeepSeek Harness wire contract behind the stable
Trajectory_OS ``AgentBackend`` interface. It targets the official runtime
interface, ``dsh --profile sdk``, which serves newline-delimited JSON-RPC 2.0
over stdio:

* client -> server requests: ``initialize``, ``session/prompt``, ``shutdown``;
* server -> client notifications: ``session.event``, ``session.status``,
  ``subagent.started``, ``subagent.finished``.

Completion is proven by **structured lifecycle evidence** (an observed
``session.status`` ``idle`` transition and/or a structured result), never by
grepping free-form stdout. The adapter is developer-preview tolerant: any
unavailable or incompatible runtime/SDK yields a deterministic result and the
caller falls back to the proven Pi backend. Provider-specific runtime model
names are adapter-owned and never globally rewritten.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import queue
import shutil
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from trajectory_os.agents import model

#: Wire-stable server identity returned by a successful SDK handshake.
SERVER_INFO_NAME = "deepseek-harness-sdk-runtime"

#: Default official provider/model route (adapter-owned naming).
DEFAULT_PROVIDER = "deepseek-official"
DEFAULT_MODEL = "deepseek-flash"

#: Credential environment variables recognized by the default probe.
CREDENTIAL_ENV_VARS = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_HARNESS_API_KEY", "DSH_API_KEY",
)


class ProcLike(Protocol):
    """Minimal text-mode subprocess surface used by the JSON-RPC transport."""

    stdin: Any
    stdout: Any
    stderr: Any

    def poll(self) -> int | None: ...  # pragma: no cover

    def terminate(self) -> None: ...  # pragma: no cover

    def kill(self) -> None: ...  # pragma: no cover

    def wait(self, timeout: float | None = None) -> int: ...  # pragma: no cover


def default_credentials_present() -> bool:
    """Best-effort, read-only credential presence check (never reads values)."""
    for name in CREDENTIAL_ENV_VARS:
        if os.environ.get(name):
            return True
    return Path.home().joinpath(".dsh", ".credentials.yaml").is_file()


def _default_popen(argv: Sequence[str]) -> ProcLike:
    import subprocess  # local import keeps the module import-light
    return subprocess.Popen(  # noqa: S603
        list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1)


def _deterministic_session_id(task: str, workspace: str) -> str:
    digest = hashlib.sha256(
        f"{workspace}\x00{task}".encode()).hexdigest()
    return f"dsh-{digest[:24]}"


def _extract_text(payload: object, *, limit: int = 16) -> list[str]:
    """Bounded, structural extraction of assistant text (never stdout grep)."""
    found: list[str] = []

    def walk(node: object, depth: int) -> None:
        if len(found) >= limit or depth > 6:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("text", "content", "lastAssistantMessage") \
                        and isinstance(value, str) and value:
                    found.append(value)
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(payload, 0)
    return found


class _RpcError(Exception):
    """A JSON-RPC error response from the runtime (structured failure)."""


class _RuntimeClient:
    """Minimal newline-delimited JSON-RPC client over a subprocess stdio."""

    def __init__(self, proc: ProcLike) -> None:
        self._proc = proc
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            stream = self._proc.stdout
            while True:
                line = stream.readline()
                if not line:
                    break
                self._queue.put(line)
        except Exception:  # pragma: no cover - transport failure path
            pass
        finally:
            self._queue.put(None)

    def send(self, method: str, params: Mapping[str, Any] | None,
             req_id: str) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id,
                                 "method": method}
        if params is not None:
            frame["params"] = dict(params)
        self._proc.stdin.write(json.dumps(frame) + "\n")
        self._proc.stdin.flush()

    def next_frame(self, deadline: float) -> Mapping[str, Any] | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = self._queue.get(timeout=remaining)
        except queue.Empty:
            return None
        if line is None:
            return None
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(frame, dict):
            return None
        return frame

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._proc.terminate()


class DeepSeekHarnessBackend:
    """The official DeepSeek Harness backend adapter."""

    name = model.BACKEND_DEEPSEEK_HARNESS

    def __init__(
        self,
        *,
        executable: str = "dsh",
        profile: str = "sdk",
        patch: str | None = None,
        provider: str = DEFAULT_PROVIDER,
        model_name: str = DEFAULT_MODEL,
        popener: Callable[[Sequence[str]], ProcLike] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        sdk_finder: Callable[[str], object | None] | None = None,
        sdk_version: Callable[[str], str | None] | None = None,
        credentials_present: Callable[[], bool] | None = None,
        sdk_factory: Callable[
            [model.AgentRequest, object | None], model.AgentResult
        ] | None = None,
    ) -> None:
        self._executable = executable
        self._profile = profile
        self._patch = patch
        self._provider = provider
        self._model = model_name
        self._popener = popener or _default_popen
        self._which = which
        self._sdk_finder = sdk_finder or importlib.util.find_spec
        self._sdk_version = sdk_version or _package_version
        self._credentials_present = (credentials_present
                                     or default_credentials_present)
        self._sdk_factory = sdk_factory

    # -- probe -----------------------------------------------------------------

    def probe(self) -> model.BackendProbe:
        sdk_available = self._sdk_finder("deepseek_harness") is not None
        version = (self._sdk_version("deepseek-harness-sdk")
                   if sdk_available else None)
        runtime = self._which(self._executable)
        if runtime is None:
            return model.BackendProbe(
                backend=self.name, available=False,
                reason=model.R_RUNTIME_MISSING,
                detail=f"runtime not found: {self._executable}",
                sdk_version=version)
        if self._patch is not None and not Path(self._patch).is_file():
            return model.BackendProbe(
                backend=self.name, available=False,
                reason=model.R_PROFILE_MISSING,
                detail=f"patch not found: {self._patch}",
                transport=model.TRANSPORT_RUNTIME, sdk_version=version)
        if not self._credentials_present():
            return model.BackendProbe(
                backend=self.name, available=False,
                reason=model.R_CREDENTIALS_MISSING,
                detail="no DeepSeek Harness credentials detected",
                transport=model.TRANSPORT_RUNTIME, sdk_version=version)
        return model.BackendProbe(
            backend=self.name, available=True, reason=model.R_OK,
            detail=f"runtime: {runtime}; sdk={'yes' if sdk_available else 'no'}",
            transport=model.TRANSPORT_RUNTIME, sdk_version=version)

    # -- run -------------------------------------------------------------------

    def run(self, request: model.AgentRequest, *,
            cancel: object | None = None) -> model.AgentResult:
        request.validate()
        probe = self.probe()
        if not probe.available:
            return model.AgentResult.build(
                backend=self.name, status=model.RS_UNAVAILABLE,
                reason=probe.reason, error=probe.detail,
                transport=probe.transport)
        if self._sdk_factory is not None:
            return self._sdk_factory(request, cancel)
        return self._run_runtime(request, cancel)

    # -- runtime JSON-RPC ------------------------------------------------------

    def _argv(self) -> list[str]:
        argv = [self._executable]
        if self._patch:
            argv += ["--patch", self._patch]
        argv += ["--profile", self._profile]
        return argv

    def _run_runtime(self, request: model.AgentRequest, cancel: object | None,
                     ) -> model.AgentResult:
        started = time.monotonic()
        deadline = started + request.timeout_s
        events: list[model.AgentEvent] = []

        def emit(kind: str, method: str, session_id: str | None = None,
                 payload: Mapping[str, Any] | None = None) -> None:
            events.append(model.AgentEvent.build(
                sequence=len(events), kind=kind, method=method,
                session_id=session_id, payload=payload))

        proc: ProcLike | None = None
        client: _RuntimeClient | None = None
        session_id = request.session_id or _deterministic_session_id(
            request.task, request.workspace)
        final_text = ""
        try:
            proc = self._popener(self._argv())
            client = _RuntimeClient(proc)
            emit(model.LK_STARTED, "spawn",
                 payload={"profile": self._profile, "provider": self._provider,
                          "model": self._model})
            initialize_params: dict[str, Any] = {
                "cwd": request.workspace,
                "provider": request.provider or self._provider,
                "model": request.model or self._model,
            }
            if request.reasoning_effort is not None:
                initialize_params["reasoningEffort"] = request.reasoning_effort
            if request.max_tokens is not None:
                initialize_params["maxTokens"] = request.max_tokens
            client.send("initialize", initialize_params, "1")
            try:
                init_result = self._await_result(client, "1", deadline, emit,
                                                 session_id)
            except _RpcError as exc:
                emit(model.LK_ERROR, "initialize",
                     payload={"rpc_error": str(exc)[:model.MAX_DETAIL_LEN]})
                return self._finish(client, proc, events, model.RS_INCOMPATIBLE,
                                    model.R_INCOMPATIBLE_PROTOCOL, started,
                                    final_text)
            if init_result is None:
                return self._finish(client, proc, events, model.RS_TIMEOUT,
                                    model.R_TIMEOUT, started, final_text)
            server_info = init_result.get("serverInfo")
            name = (server_info.get("name")
                    if isinstance(server_info, Mapping) else None)
            if name != SERVER_INFO_NAME:
                emit(model.LK_ERROR, "initialize",
                     payload={"server_info": server_info})
                return self._finish(client, proc, events,
                                    model.RS_INCOMPATIBLE,
                                    model.R_INCOMPATIBLE_PROTOCOL, started,
                                    final_text)
            emit(model.LK_INITIALIZED, "initialize",
                 session_id=session_id, payload={"server_info": server_info})

            client.send("session/prompt", {
                "sessionId": session_id,
                "contentBlocks": [{"type": "text", "text": request.task}],
            }, "2")
            try:
                prompt_result = self._await_result(client, "2", deadline,
                                                   emit, session_id)
            except _RpcError as exc:
                emit(model.LK_ERROR, "session/prompt",
                     payload={"rpc_error": str(exc)[:model.MAX_DETAIL_LEN]})
                return self._finish(client, proc, events, model.RS_FAILED,
                                    model.R_RPC_ERROR, started, final_text)
            if prompt_result is None:
                return self._finish(client, proc, events, model.RS_TIMEOUT,
                                    model.R_TIMEOUT, started, final_text)
            message_id = prompt_result.get("messageId")
            emit(model.LK_RESULT, "session/prompt", session_id=session_id,
                 payload={"message_id": message_id})

            if _cancel_requested(cancel):
                return self._finish(client, proc, events, model.RS_CANCELLED,
                                    model.R_CANCELLED, started, final_text)
            idle, texts = self._await_idle(client, session_id, deadline, emit)
            final_text = texts or final_text
            if not idle:
                return self._finish(client, proc, events, model.RS_TIMEOUT,
                                    model.R_TIMEOUT, started, final_text)
            completion = model.CompletionEvidence.build(
                source=model.CS_LIFECYCLE_IDLE, reliable=True,
                detail="session.status idle observed over structured JSON-RPC",
                message_id=(message_id if isinstance(message_id, str)
                            else None))
            emit(model.LK_COMPLETED, "session.status", session_id=session_id)
            with contextlib.suppress(Exception):
                client.send("shutdown", None, "9")
            return model.AgentResult.build(
                backend=self.name, status=model.RS_COMPLETED,
                reason=model.R_OK, events=tuple(events),
                session_id=session_id, final_response=final_text,
                completion=completion, transport=model.TRANSPORT_RUNTIME,
                runtime_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:  # fail closed with a stable reason
            emit(model.LK_ERROR, "exception",
                 payload={"error": type(exc).__name__})
            return model.AgentResult.build(
                backend=self.name, status=model.RS_FAILED,
                reason=model.R_LAUNCH_FAILED, events=tuple(events),
                session_id=session_id, final_response=final_text,
                error=f"{type(exc).__name__}: {exc}"[:model.MAX_DETAIL_LEN],
                transport=model.TRANSPORT_RUNTIME,
                runtime_ms=int((time.monotonic() - started) * 1000))
        finally:
            if client is not None:
                client.close()

    def _await_result(
        self, client: _RuntimeClient, req_id: str, deadline: float,
        emit: Callable[..., None], session_id: str,
    ) -> Mapping[str, Any] | None:
        while True:
            frame = client.next_frame(deadline)
            if frame is None:
                return None
            if _handle_notification(frame, emit, session_id):
                continue
            if frame.get("id") == req_id and "method" not in frame:
                if frame.get("error") is not None:
                    detail = frame.get("error")
                    raise _RpcError(json.dumps(detail, default=str)[:512])
                result = frame.get("result")
                return result if isinstance(result, Mapping) else {}

    def _await_idle(
        self, client: _RuntimeClient, session_id: str, deadline: float,
        emit: Callable[..., None],
    ) -> tuple[bool, str]:
        texts: list[str] = []
        while True:
            frame = client.next_frame(deadline)
            if frame is None:
                return False, "\n".join(texts).strip()
            kind, payload = _classify_notification(frame)
            if kind is None:
                continue
            if kind == model.LK_MESSAGE:
                texts.extend(_extract_text(payload))
            emit(kind, str(frame.get("method")), session_id=session_id,
                 payload=_bounded_payload(payload))
            if kind == model.LK_IDLE:
                return True, "\n".join(texts).strip()

    def _finish(self, client: _RuntimeClient | None, proc: ProcLike | None,
                events: Sequence[model.AgentEvent], status: str, reason: str,
                started: float, final_text: str) -> model.AgentResult:
        with contextlib.suppress(Exception):
            if client is not None:
                client.send("shutdown", None, "9")
        return model.AgentResult.build(
            backend=self.name, status=status, reason=reason,
            events=tuple(events), final_response=final_text or None,
            completion=model.CompletionEvidence.build(
                source=model.CS_NONE, reliable=False, detail=reason),
            transport=model.TRANSPORT_RUNTIME,
            runtime_ms=int((time.monotonic() - started) * 1000))


def _package_version(distribution: str) -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version(distribution)
    except (ImportError, PackageNotFoundError):  # pragma: no cover
        return None


def _cancel_requested(cancel: object | None) -> bool:
    if cancel is None:
        return False
    is_set = getattr(cancel, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _bounded_payload(payload: object) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    text = json.dumps(dict(payload), sort_keys=True, default=str)
    if len(text) > model.MAX_PAYLOAD_BYTES:
        return {"truncated": True, "bytes": len(text)}
    return dict(payload)


def _handle_notification(frame: Mapping[str, Any],
                         emit: Callable[..., None],
                         session_id: str) -> bool:
    kind, payload = _classify_notification(frame)
    if kind is None:
        return False
    emit(kind, str(frame.get("method")), session_id=session_id,
         payload=_bounded_payload(payload))
    return True


def _classify_notification(
    frame: Mapping[str, Any],
) -> tuple[str | None, Mapping[str, Any]]:
    if "method" not in frame or "id" in frame:
        return None, {}
    method = frame.get("method")
    params = frame.get("params")
    payload = params if isinstance(params, Mapping) else {}
    if method == "session.status":
        return (model.LK_IDLE if payload.get("status") == "idle"
                else model.LK_RUNNING), payload
    if method == "session.event":
        return model.LK_MESSAGE, payload
    if method == "subagent.started":
        return model.LK_SUBAGENT_STARTED, payload
    if method == "subagent.finished":
        status = payload.get("status")
        return (model.LK_ERROR if status == "error"
                else model.LK_SUBAGENT_FINISHED), payload
    return None, {}
