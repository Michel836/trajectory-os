"""M029 — trial executors (live backends + deterministic fixture).

Two executors implement the same bounded contract:

* :class:`LiveExecutor` invokes the real production backends. The proven Pi
  subprocess path runs in the exact isolated workspace (``cwd`` is mandatory;
  a benchmark trial may never mutate the operator's repository). The qualified
  DeepSeek Harness adapter is used unchanged; when its runtime/credentials
  cannot be proven the trial is recorded ``UNAVAILABLE`` — never fabricated.
* :class:`FixtureExecutor` applies a deterministic solution and emits a
  synthetic result for pipeline validation only. Its evidence is labelled
  ``PIPELINE_FIXTURE`` and excluded from the runtime decision.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from trajectory_os.agents import model as agent_model
from trajectory_os.agents import pi as pi_backend
from trajectory_os.benchmark import model, patch

#: Completion marker contract for the Pi text path (proven M015 semantics).
COMPLETION_MARKER = "TRAJECTORY_IMPLEMENT_COMPLETE"

#: Fixture evidence label (never presented as a real backend measurement).
FIXTURE_EVIDENCE = "PIPELINE_FIXTURE"


@dataclass(frozen=True)
class ExecutionRequest:
    workload: model.WorkloadSpec
    workspace: str
    backend: str
    provider: str | None
    model_name: str | None
    locality: str
    mode: str
    attempt: int = 0
    timeout_s: int = 600
    thinking: str = "medium"
    cancel: object | None = None


@dataclass(frozen=True)
class ExecutionOutcome:
    """The bounded result of one trial execution (pre trust-gate)."""

    backend: str
    provider: str | None
    model_name: str | None
    locality: str
    wall_ms: int
    agent_result: agent_model.AgentResult | None
    cancelled: bool = False
    backend_unavailable: bool = False
    unavailable_reason: str | None = None
    runtime_version: str | None = None
    sdk_version: str | None = None
    first_useful_patch_ms: int | None = None
    phase_durations: tuple[tuple[str, int], ...] = ()
    error: str | None = None
    evidence: str = "LIVE"
    extra: Mapping[str, Any] = field(default_factory=dict)


class TrialExecutor(Protocol):
    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        ...  # pragma: no cover


def implementation_prompt(workload: model.WorkloadSpec) -> str:
    """Bounded task prompt with the proven Pi completion-marker contract."""
    return (
        f"{workload.objective}\n\n"
        "Work only inside the current working directory. Do not run git "
        "commit, push, merge, reset, clean, stash, rebase, checkout or "
        "switch. When the task is genuinely complete, include a HANDOFF "
        "line and make the exact final non-blank line of your response:\n"
        f"{COMPLETION_MARKER}\n"
        "Do not emit that marker unless you actually completed the task."
    )


# --- live executor ------------------------------------------------------------


PiRunner = Any


def _default_pi_runner(
    argv: Sequence[str], workspace: str, timeout_s: int,
    env: Mapping[str, str] | None,
) -> tuple[int | None, str, str, bool, int]:
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - bounded backend-owned argv
            list(argv), cwd=workspace, stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            timeout=max(1, timeout_s), check=False,
            env=(dict(env) if env is not None else None))
    except subprocess.TimeoutExpired:
        return (None, "", "", True, int((time.monotonic() - started) * 1000))
    except (OSError, subprocess.SubprocessError) as exc:
        return (None, "", f"{type(exc).__name__}: {exc}", False,
                int((time.monotonic() - started) * 1000))
    return (int(proc.returncode), proc.stdout, proc.stderr, False,
            int((time.monotonic() - started) * 1000))


class LiveExecutor:
    """The real production backends, isolated per trial."""

    def __init__(
        self,
        *,
        pi_binary: str = "pi",
        harness_runtime: str = "dsh",
        pi_runner: PiRunner | None = None,
        backend_factory: Any | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._pi_binary = pi_binary
        self._harness_runtime = harness_runtime
        self._pi_runner = pi_runner or _default_pi_runner
        self._backend_factory = backend_factory
        self._environ = environ

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        if request.backend == agent_model.BACKEND_PI:
            return self._execute_pi(request)
        if request.backend == agent_model.BACKEND_DEEPSEEK_HARNESS:
            return self._execute_harness(request)
        return ExecutionOutcome(
            backend=request.backend, provider=request.provider,
            model_name=request.model_name, locality=request.locality,
            wall_ms=0, agent_result=None, backend_unavailable=True,
            unavailable_reason=agent_model.R_BACKEND_UNAVAILABLE,
            error=f"unknown backend {request.backend!r}")

    def _execute_pi(self, request: ExecutionRequest) -> ExecutionOutcome:
        task = implementation_prompt(request.workload)
        argv = [
            self._pi_binary, "--no-session", "--no-context-files",
            "--model", request.model_name or "deepseek-flash",
            "--thinking", request.thinking, "-p", task,
        ]
        if cancel_requested(request.cancel):
            return _cancelled(request)
        env = dict(self._environ) if self._environ is not None else None
        code, stdout, stderr, timed_out, wall_ms = self._pi_runner(
            argv, request.workspace, request.timeout_s, env)
        if timed_out:
            return ExecutionOutcome(
                backend=request.backend, provider=request.provider,
                model_name=request.model_name, locality=request.locality,
                wall_ms=wall_ms, agent_result=None, backend_unavailable=True,
                unavailable_reason=agent_model.R_TIMEOUT,
                phase_durations=(("IMPLEMENT", wall_ms),),
                error="pi timed out")
        marker = pi_backend.completion_marker_present(stdout)
        completion = agent_model.CompletionEvidence.build(
            source=agent_model.CS_EXIT_CODE_MARKER, reliable=marker,
            detail=("exact terminal marker present" if marker
                    else "no exact terminal marker"))
        result = agent_model.AgentResult.build(
            backend=agent_model.BACKEND_PI,
            status=(agent_model.RS_COMPLETED
                    if code == 0 and marker else agent_model.RS_FAILED),
            reason=(agent_model.R_OK if code == 0 and marker
                    else agent_model.R_LAUNCH_FAILED),
            events=(agent_model.AgentEvent.build(
                sequence=0, kind=agent_model.LK_STARTED,
                method="subprocess",
                payload={"binary": self._pi_binary}),),
            exit_code=code, final_response=stdout, completion=completion,
            error=(None if code == 0 and marker
                   else (stderr[-agent_model.MAX_DETAIL_LEN:] or None)),
            transport=agent_model.TRANSPORT_SUBPROCESS, runtime_ms=wall_ms)
        return ExecutionOutcome(
            backend=request.backend, provider=request.provider,
            model_name=request.model_name, locality=request.locality,
            wall_ms=wall_ms, agent_result=result,
            first_useful_patch_ms=wall_ms if marker else None,
            phase_durations=(("IMPLEMENT", wall_ms),),
            runtime_version=None, sdk_version=None,
            error=(None if code == 0 else f"exit={code}"))

    def _execute_harness(self, request: ExecutionRequest) -> ExecutionOutcome:
        from trajectory_os.agents import registry

        build = self._backend_factory or (
            lambda name, **kw: registry.make_backend(name, **kw))
        try:
            backend = build(
                agent_model.BACKEND_DEEPSEEK_HARNESS,
                executable=self._harness_runtime,
                model_name=request.model_name or "deepseek-flash",
                provider=request.provider or "deepseek-official")
        except (TypeError, ValueError, agent_model.AgentBackendError) as exc:
            return ExecutionOutcome(
                backend=request.backend, provider=request.provider,
                model_name=request.model_name, locality=request.locality,
                wall_ms=0, agent_result=None, backend_unavailable=True,
                unavailable_reason=agent_model.R_BACKEND_UNAVAILABLE,
                error=str(exc))
        probe = backend.probe()
        if not probe.available:
            return ExecutionOutcome(
                backend=request.backend, provider=request.provider,
                model_name=request.model_name, locality=request.locality,
                wall_ms=0, agent_result=None, backend_unavailable=True,
                unavailable_reason=probe.reason, error=probe.detail,
                runtime_version=probe.runtime_version,
                sdk_version=probe.sdk_version)
        machine_request = agent_model.AgentRequest(
            task=implementation_prompt(request.workload),
            workspace=request.workspace, mode="IMPLEMENT",
            timeout_s=request.timeout_s, provider=request.provider,
            model=request.model_name).validate()
        started = time.monotonic()
        result = backend.run(machine_request, cancel=request.cancel)
        wall_ms = int((time.monotonic() - started) * 1000)
        unavailable = result.status in (agent_model.RS_UNAVAILABLE,
                                        agent_model.RS_INCOMPATIBLE)
        return ExecutionOutcome(
            backend=request.backend, provider=request.provider,
            model_name=request.model_name, locality=request.locality,
            wall_ms=wall_ms, agent_result=result, backend_unavailable=unavailable,
            unavailable_reason=(result.reason if unavailable else None),
            runtime_version=probe.runtime_version,
            sdk_version=probe.sdk_version,
            phase_durations=(("IMPLEMENT", wall_ms),),
            error=result.error)


def cancel_requested(cancel: object | None) -> bool:
    if cancel is None:
        return False
    is_set = getattr(cancel, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _cancelled(request: ExecutionRequest) -> ExecutionOutcome:
    return ExecutionOutcome(
        backend=request.backend, provider=request.provider,
        model_name=request.model_name, locality=request.locality,
        wall_ms=0, agent_result=None, cancelled=True,
        unavailable_reason=model.R_CANCELLED)


# --- fixture executor ---------------------------------------------------------

#: Deterministic solved file contents for the fixture executor.
_FIXTURE_SOLUTIONS: dict[str, dict[str, str]] = {
    "small-targeted-repair": {
        "calc.py": "def add(a, b):\n    return a + b\n"},
    "medium-feature-implementation": {
        "strings_util.py": (
            "import re\n"
            "\n"
            "\n"
            "def slugify(text):\n"
            "    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n"
            "    return slug\n")},
    "multi-file-integration-change": {
        "service.py": (
            "class Service:\n"
            "    def greet(self, name):\n"
            "        return f\"Hello, {name}\"\n"
            "\n"
            "    def farewell(self, name):\n"
            "        return f\"Goodbye, {name}\"\n"),
        "app.py": (
            "from service import Service\n"
            "\n"
            "\n"
            "def greet(name):\n"
            "    return Service().greet(name)\n"
            "\n"
            "\n"
            "def farewell(name):\n"
            "    return Service().farewell(name)\n")},
    "interruption-resume": {
        "total.py": "def total(values):\n    return sum(values)\n"},
}


class FixtureExecutor:
    """Deterministic pipeline-validation executor (never a live measurement)."""

    def __init__(
        self,
        *,
        violate_fail_closed: bool = False,
        interrupt_once: bool = True,
        runtime_ms: int = 1200,
        prompt_tokens: int = 900,
        completion_tokens: int = 120,
        cache_read_tokens: int = 256,
        cost_usd: float | None = 0.0004,
    ) -> None:
        self._violate = violate_fail_closed
        self._interrupt_once = interrupt_once
        self._runtime_ms = runtime_ms
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._cache_read_tokens = cache_read_tokens
        self._cost_usd = cost_usd

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        workload = request.workload
        # Backend-neutral: every backend identity traverses the exact same
        # resumable lifecycle in fixture mode (interrupt on the first attempt,
        # complete on resume). No live Harness success is ever fabricated; the
        # evidence stays labelled PIPELINE_FIXTURE.
        if (workload.interrupt and self._interrupt_once
                and request.attempt == 0):
            return ExecutionOutcome(
                backend=request.backend, provider=request.provider,
                model_name=request.model_name, locality=request.locality,
                wall_ms=200, agent_result=None, cancelled=True,
                evidence=FIXTURE_EVIDENCE)
        if workload.fail_closed_case:
            if self._violate and workload.protected_paths:
                _write(request.workspace, workload.protected_paths[0],
                       "SECRET = 'gone'\n")
            solution: dict[str, str] = {}
        else:
            solution = _FIXTURE_SOLUTIONS.get(workload.workload_id, {})
        for name, content in solution.items():
            _write(request.workspace, name, content)
        result = _fixture_agent_result(
            request, runtime_ms=self._runtime_ms,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            cache_read_tokens=self._cache_read_tokens,
            cost_usd=self._cost_usd)
        return ExecutionOutcome(
            backend=request.backend, provider=request.provider,
            model_name=request.model_name, locality=request.locality,
            wall_ms=self._runtime_ms, agent_result=result,
            first_useful_patch_ms=self._runtime_ms,
            phase_durations=(("IMPLEMENT", self._runtime_ms),),
            evidence=FIXTURE_EVIDENCE,
            runtime_version="fixture",
            sdk_version="fixture")


def _write(workspace: str, name: str, content: str) -> None:
    path = Path(workspace) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture_agent_result(
    request: ExecutionRequest, *, runtime_ms: int, prompt_tokens: int,
    completion_tokens: int, cache_read_tokens: int, cost_usd: float | None,
) -> agent_model.AgentResult:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_read_input_tokens": cache_read_tokens,
    }
    if cost_usd is not None:
        usage["cost_usd"] = cost_usd
    events = (
        agent_model.AgentEvent.build(
            sequence=0, kind=agent_model.LK_STARTED, method="fixture"),
        agent_model.AgentEvent.build(
            sequence=1, kind=agent_model.LK_RESULT, method="fixture",
            payload={"usage": usage}),
    )
    return agent_model.AgentResult.build(
        backend=request.backend,
        status=agent_model.RS_COMPLETED, reason=agent_model.R_OK,
        events=events, exit_code=0, completion=agent_model.CompletionEvidence.build(
            source=agent_model.CS_EXIT_CODE_MARKER, reliable=True,
            detail="fixture"), transport=agent_model.TRANSPORT_SUBPROCESS,
        runtime_ms=runtime_ms)


def prepare_workspace(workload: model.WorkloadSpec,
                      workspace: str) -> patch.WorkspaceSnapshot:
    """Materialize one isolated workspace and return its baseline snapshot."""
    base = Path(workspace)
    base.mkdir(parents=True, exist_ok=True)
    for name, content in workload.fixture.items():
        _write(workspace, name, content)
    return patch.capture(workspace)
