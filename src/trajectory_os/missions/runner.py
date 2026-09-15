"""Mission 003 — bounded sub-run execution (fresh context per sub-run).

A *sub-run* is one bounded process execution of one phase's command.
Every sub-run:

* starts **fresh** — no inherited in-memory context; bounded by one process
  timeout, one worktree, and one evidence directory;
* persists its record **before** launch (``RUNNING``) and terminalizes it
  **after** — a crash in between leaves an explicit, fail-closed record
  (reconstruction never guesses);
* is classified deterministically and **fail closed** (see
  :func:`classify_subrun`):

  - a non-zero exit / timeout keep the process-evidence classification
    (FAILED / CRASHED) and are NEVER upgraded by semantic evidence;
  - a bare exit 0 is NOT sufficient for COMPLETED: it becomes COMPLETED
    only when a valid semantic result (see
    :mod:`trajectory_os.missions.semantic`) exists for this exact sub-run,
    is bound to its exact ``subrun_id``, and says SUCCESS;
  - missing / malformed / stale / subrun-mismatched semantic evidence,
    or a non-SUCCESS semantic status, classify fail closed (UNPROVEN /
    FAILED / CRASHED as the semantic status dictates).

:class:`ProcessPhaseRunner` is the production runner (subprocess).  Tests
use the scripted :class:`FakeRunner`.  Neither may exceed its bound.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from trajectory_os.missions import model, semantic


@dataclass(frozen=True)
class SubrunRequest:
    mission_id: str
    subrun_id: str
    phase_id: str
    kind: str
    mode: str
    round: int
    attempt: int
    command: tuple[str, ...]
    cwd: str | None
    timeout_s: int
    stdout_file: str
    stderr_file: str
    resources: dict[str, Any] | None
    # Mission 007: only model-heavy trajectory-pi sub-runs require the
    # structured semantic completion contract. Deterministic validation /
    # consolidation commands retain bounded process-exit semantics.
    semantic_required: bool = False


@dataclass(frozen=True)
class SubrunResult:
    exit_code: int | None       # process exit status, if one was observed
    classification: str         # model.CR_* (terminal)
    timed_out: bool = False
    # Mission 007 — structured semantic evidence (verified, bound to this
    # exact sub-run) for later persistence.  ``None`` means absent or
    # fail-closed-rejected (missing/malformed/stale/mismatched); the
    # stable reason code (``MISSING``, ``MALFORMED:...``, ``SUBRUN_MISMATCH``
    # ...) is preserved for audit.  These fields NEVER alter the
    # classification — that decision is already fail closed above.
    semantic_status: str | None = None
    semantic_error: str | None = None
    semantic_agent_classification: str | None = None
    semantic_readiness: str | None = None
    semantic_reason: str | None = None


class PhaseRunner(Protocol):
    """Runs one bounded sub-run and returns deterministic evidence."""

    def run(self, request: SubrunRequest) -> SubrunResult:  # pragma: no cover
        ...


def classify_exit(exit_code: int | None, timed_out: bool = False) -> str:
    """Deterministic classification from process exit evidence (fail closed).

    NOTE: a bare exit 0 here means only "the process exited cleanly";
    it is NOT by itself sufficient for COMPLETED — semantic completion
    evidence is required (see :func:`classify_subrun`).
    """
    if timed_out or exit_code is None:
        return model.CR_CRASHED
    if exit_code == 0:
        return model.CR_COMPLETED
    if exit_code <= model.MAX_DETERMINISTIC_FAILURE_EXIT:
        return model.CR_FAILED
    return model.CR_CRASHED


#: exit 0 (process exited cleanly): verified semantic status -> terminal
#: sub-run classification.  Deterministic total mapping; SUCCESS is the
#: only status that maps to COMPLETED (everything else is fail closed).
_SEMANTIC_TO_CLASSIFICATION: dict[str, str] = {
    semantic.STATUS_SUCCESS: model.CR_COMPLETED,
    semantic.STATUS_INCOMPLETE: model.CR_UNPROVEN,
    semantic.STATUS_FAILED: model.CR_FAILED,
    semantic.STATUS_PROVIDER_FAILURE: model.CR_CRASHED,
    semantic.STATUS_UNKNOWN: model.CR_UNPROVEN,
}


def classify_subrun(exit_code: int | None,
                    *,
                    timed_out: bool = False,
                    semantic_status: str | None = None,
                    semantic_error: str | None = None,
                    semantic_agent_classification: str | None = None,
                    semantic_readiness: str | None = None,
                    semantic_reason: str | None = None) -> SubrunResult:
    """Deterministic, fail-closed sub-run classification (pure).

    * non-zero exit / timeout: the process-evidence classification is
      authoritative (FAILED / CRASHED); semantic evidence can annotate
      (``semantic_status`` is preserved for audit) but can NEVER upgrade
      a real process failure into COMPLETED;
    * exit 0: COMPLETED only if valid semantic evidence bound to this
      exact sub-run says SUCCESS; any missing/malformed/stale/mismatched
      evidence or non-SUCCESS status classifies fail closed (never
      COMPLETED).
    """
    base = classify_exit(exit_code, timed_out=timed_out)
    if base != model.CR_COMPLETED:
        classification = base
    elif semantic_status is None:
        classification = model.CR_UNPROVEN
    else:
        classification = _SEMANTIC_TO_CLASSIFICATION[semantic_status]
    return SubrunResult(
        exit_code=exit_code,
        classification=classification,
        timed_out=timed_out,
        semantic_status=semantic_status,
        semantic_error=semantic_error,
        semantic_agent_classification=semantic_agent_classification,
        semantic_readiness=semantic_readiness,
        semantic_reason=semantic_reason,
    )


def semantic_evidence_path(request: SubrunRequest) -> Path:
    """Unique, deterministic semantic evidence path for the exact sub-run.

    Derived from the sub-run's own evidence location and includes the
    exact ``subrun_id`` in the file name, so two sub-runs (or attempts)
    can never share a semantic result file.
    """
    if not request.subrun_id:
        raise ValueError("subrun_id is required (semantic evidence is "
                         "bound to the exact sub-run)")
    return Path(request.stdout_file).parent / f"{request.subrun_id}.semantic.json"


def process_env(request: SubrunRequest) -> dict[str, str]:
    """Child environment for one sub-run.

    Inherited environment PLUS the semantic contract variables:

    * ``TRAJECTORY_SUBRUN_RESULT_FILE`` — where the producer must emit
      its structured result;
    * ``TRAJECTORY_SUBRUN_ID`` — the exact sub-run the result must bind to.

    Existing environment variables are preserved (only these two contract
    names are set), so operator-provided configuration is never clobbered.
    """
    env = dict(os.environ)
    if request.semantic_required:
        env[semantic.RESULT_FILE_ENV_VAR] = str(semantic_evidence_path(request))
        env[semantic.SUBRUN_ID_ENV_VAR] = request.subrun_id
    else:
        # These are internal per-subrun contract variables. Never leak a
        # parent process value into deterministic commands.
        env.pop(semantic.RESULT_FILE_ENV_VAR, None)
        env.pop(semantic.SUBRUN_ID_ENV_VAR, None)
    return env


def phase_state_for_classification(classification: str) -> str:
    if classification == model.CR_COMPLETED:
        return model.PS_PASSED
    if classification == model.CR_FAILED:
        return model.PS_FAILED
    return model.PS_UNPROVEN  # CRASHED / UNPROVEN: explicit, fail closed


class ProcessPhaseRunner:
    """Bounded subprocess runner (one bounded process per sub-run).

    Before launch: clears any stale semantic evidence file for the exact
    sub-run and exports the semantic contract environment.  After
    completion: reads and validates that exact structured result and
    classifies fail closed — a bare exit 0 never becomes COMPLETED
    without valid, exact-subrun, SUCCESS semantic evidence.
    """

    def run(self, request: SubrunRequest) -> SubrunResult:
        out_path = Path(request.stdout_file)
        err_path = Path(request.stderr_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        err_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = list(request.command)
        if not cmd:
            return SubrunResult(exit_code=None, classification=model.CR_UNPROVEN)
        sem_path: Path | None = None
        if request.semantic_required:
            sem_path = semantic_evidence_path(request)
            # Fail-closed hygiene: clear any stale semantic evidence left by
            # a previous attempt/launch so ONLY this sub-run's own emission
            # can ever serve as its completion proof.
            with contextlib.suppress(OSError):
                sem_path.unlink(missing_ok=True)
        timed_out = False
        exit_code: int | None = None
        with out_path.open("wb") as out_fh, err_path.open("wb") as err_fh:
            try:
                proc = subprocess.run(  # noqa: S603 (command is operator-fixed)
                    cmd,
                    cwd=request.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=out_fh,
                    stderr=err_fh,
                    env=process_env(request),
                    timeout=request.timeout_s,
                    check=False,
                )
                exit_code = int(proc.returncode)
            except subprocess.TimeoutExpired:
                timed_out = True
            except (OSError, subprocess.SubprocessError):
                exit_code = None

        # Deterministic phases (VALIDATE / CONSOLIDATE) do not invoke the
        # trajectory-pi semantic producer. Their historical bounded process
        # semantics remain authoritative.
        if not request.semantic_required:
            return SubrunResult(
                exit_code=exit_code,
                classification=classify_exit(exit_code, timed_out=timed_out),
                timed_out=timed_out,
            )

        assert sem_path is not None

        # Model-heavy trajectory-pi phases require exact semantic proof.
        # Read + verify the exact structured result (fail closed: any
        # rejection yields no usable status — never a guess).
        doc, read_error = semantic.read_semantic_file(str(sem_path))
        if read_error is not None:
            status: str | None = None
            error: str | None = read_error
            agent_classification = None
            readiness = None
            reason = None
        else:
            status, error = semantic.interpret_semantic(doc, request.subrun_id)
            if error is None and doc is not None:
                agent_classification = doc.get("agent_classification")
                readiness = doc.get("readiness")
                reason = doc.get("reason")
            else:
                agent_classification = None
                readiness = None
                reason = None
        return classify_subrun(
            exit_code,
            timed_out=timed_out,
            semantic_status=status,
            semantic_error=error,
            semantic_agent_classification=agent_classification,
            semantic_readiness=readiness,
            semantic_reason=reason,
        )


class FakeRunnerDeath(Exception):
    """Simulates the orchestrator process dying mid-mission (tests only)."""


class FakeRunner:
    """Deterministic scripted runner for tests.

    * ``exits``: phase_id -> ordered list of exit codes per attempt
      (falls back to 0 once the list is exhausted);
    * ``die_after_phase``: after returning a result for that phase, raise
      :class:`FakeRunnerDeath` — simulates the orchestrator crashing after
      the sub-run's terminal record was persisted but before the phase
      transition was written (the resume/reconstruction scenario);
    * ``die_after_subruns``: raise after the Nth sub-run completed.
    """

    def __init__(self,
                 exits: dict[str, list[int]] | None = None,
                 die_after_phase: str | None = None,
                 die_after_subruns: int | None = None) -> None:
        self.exits = {k: list(v) for k, v in (exits or {}).items()}
        self.die_after_phase = die_after_phase
        self.die_after_subruns = die_after_subruns
        self.calls: list[SubrunRequest] = []
        self._count = 0

    def run(self, request: SubrunRequest) -> SubrunResult:
        self.calls.append(request)
        self._count += 1
        if self.die_after_subruns is not None and self._count > self.die_after_subruns:
            raise FakeRunnerDeath(f"orchestrator crashed after sub-run {self._count - 1}")
        queue = self.exits.get(request.phase_id)
        exit_code = queue.pop(0) if queue else 0
        result = SubrunResult(exit_code=exit_code,
                              classification=classify_exit(exit_code))
        if self.die_after_phase is not None and request.phase_id == self.die_after_phase:
            raise FakeRunnerDeath("orchestrator crashed after sub-run for " + request.phase_id)
        return result
