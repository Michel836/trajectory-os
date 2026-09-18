"""Mission 015 — the proven Pi agent backend (subprocess contract).

Pi keeps its historical, validated execution path. Completion uses Pi's exact
terminal-marker contract (the last non-blank line is an uppercase
``*_COMPLETE`` marker and the output contains a handoff section). Pi is the
proven fallback for any harness that is unavailable or incompatible.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from trajectory_os.agents import model

#: Exact terminal completion marker (fail closed): the last non-blank line.
_MARKER_RE = re.compile(r"^[A-Z][A-Z0-9_.]*_COMPLETE$")


@dataclass(frozen=True)
class ProcResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


Runner = Callable[..., ProcResult]


def default_runner(argv: Sequence[str], *, timeout_s: int,
                   cancel: object | None = None,
                   env: dict[str, str] | None = None) -> ProcResult:
    """Run one bounded subprocess with timeout (no shell, no git writes)."""
    if cancel is not None and getattr(cancel, "is_set", lambda: False)():
        return ProcResult(None, "", "", cancelled=True)
    proc = subprocess.Popen(  # noqa: S603 - argv is backend-owned
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return ProcResult(None, stdout, stderr, timed_out=True)
    return ProcResult(proc.returncode, stdout, stderr)


def completion_marker_present(text: str) -> bool:
    """True when the exact terminal completion marker contract holds."""
    if "handoff" not in text.lower():
        return False
    last = ""
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.strip():
            last = stripped
    return bool(_MARKER_RE.match(last))


class PiBackend:
    """The proven Pi/Ollama subprocess backend."""

    name = model.BACKEND_PI

    def __init__(
        self,
        *,
        binary: str = "pi",
        model: str = "qwen3.8-dev3090",
        thinking: str = "medium",
        runner: Runner | None = None,
        env: dict[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._binary = binary
        self._model = model
        self._thinking = thinking
        self._runner = runner or default_runner
        self._env = env
        self._which = which

    def probe(self) -> model.BackendProbe:
        resolved = self._which(self._binary)
        if resolved is None:
            return model.BackendProbe(
                backend=self.name, available=False,
                reason=model.R_RUNTIME_MISSING,
                detail=f"executable not found: {self._binary}",
                transport=model.TRANSPORT_SUBPROCESS)
        return model.BackendProbe(
            backend=self.name, available=True, reason=model.R_OK,
            detail=f"pi executable: {resolved}",
            transport=model.TRANSPORT_SUBPROCESS)

    def run(self, request: model.AgentRequest, *,
            cancel: object | None = None) -> model.AgentResult:
        request.validate()
        probe = self.probe()
        if not probe.available:
            return model.AgentResult.build(
                backend=self.name, status=model.RS_UNAVAILABLE,
                reason=probe.reason, error=probe.detail,
                transport=model.TRANSPORT_SUBPROCESS)
        task = request.task
        if request.prompt_file:
            task = f"@{request.prompt_file}\n\n{task}"
        argv = [self._binary, "--model", self._model, "--thinking",
                self._thinking, "-p", task]
        events = [model.AgentEvent.build(
            sequence=0, kind=model.LK_STARTED, method="subprocess",
            payload={"binary": self._binary, "model": self._model})]
        started = time.monotonic()
        outcome = self._runner(argv, timeout_s=request.timeout_s,
                               cancel=cancel, env=self._env)
        runtime_ms = int((time.monotonic() - started) * 1000)
        if outcome.cancelled:
            return model.AgentResult.build(
                backend=self.name, status=model.RS_CANCELLED,
                reason=model.R_CANCELLED, events=tuple(events),
                transport=model.TRANSPORT_SUBPROCESS,
                runtime_ms=runtime_ms)
        if outcome.timed_out:
            return model.AgentResult.build(
                backend=self.name, status=model.RS_TIMEOUT,
                reason=model.R_TIMEOUT, events=tuple(events),
                final_response=outcome.stdout,
                transport=model.TRANSPORT_SUBPROCESS,
                runtime_ms=runtime_ms)
        marker = completion_marker_present(outcome.stdout)
        completion = model.CompletionEvidence.build(
            source=model.CS_EXIT_CODE_MARKER, reliable=marker,
            detail=("exact terminal marker present" if marker
                    else "no exact terminal marker"),
            stop_reason=f"exit={outcome.exit_code}")
        completed = outcome.exit_code == 0 and marker
        events.append(model.AgentEvent.build(
            sequence=1,
            kind=model.LK_COMPLETED if completed else model.LK_ERROR,
            method="subprocess",
            payload={"exit_code": outcome.exit_code, "marker": marker}))
        return model.AgentResult.build(
            backend=self.name,
            status=model.RS_COMPLETED if completed else model.RS_FAILED,
            reason=model.R_OK if completed else model.R_LAUNCH_FAILED,
            events=tuple(events), exit_code=outcome.exit_code,
            final_response=outcome.stdout,
            completion=completion,
            error=None if completed else outcome.stderr[-model.MAX_DETAIL_LEN:],
            transport=model.TRANSPORT_SUBPROCESS, runtime_ms=runtime_ms)
