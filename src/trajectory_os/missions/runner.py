"""Mission 003 — bounded sub-run execution (fresh context per sub-run).

A *sub-run* is one bounded process execution of one phase's command.
Every sub-run:

* starts **fresh** — no inherited in-memory context; bounded by one process
  timeout, one worktree, and one evidence directory;
* persists its record **before** launch (``RUNNING``) and terminalizes it
  **after** — a crash in between leaves an explicit, fail-closed record
  (reconstruction never guesses);
* is classified deterministically from exit evidence (see
  :func:`classify_exit`): 0 -> completed, 1..120 -> deterministic failure
  (repair-eligible), anything else or none -> provider/infrastructure
  failure (surfaced, never silently retried).

:class:`ProcessPhaseRunner` is the production runner (subprocess).  Tests
use the scripted :class:`FakeRunner`.  Neither may exceed its bound.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from trajectory_os.missions import model


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


@dataclass(frozen=True)
class SubrunResult:
    exit_code: int | None       # process exit status, if one was observed
    classification: str         # model.CR_* (terminal)
    timed_out: bool = False


class PhaseRunner(Protocol):
    """Runs one bounded sub-run and returns deterministic evidence."""

    def run(self, request: SubrunRequest) -> SubrunResult:  # pragma: no cover
        ...


def classify_exit(exit_code: int | None, timed_out: bool = False) -> str:
    """Deterministic classification from exit evidence (fail closed)."""
    if timed_out or exit_code is None:
        return model.CR_CRASHED
    if exit_code == 0:
        return model.CR_COMPLETED
    if exit_code <= model.MAX_DETERMINISTIC_FAILURE_EXIT:
        return model.CR_FAILED
    return model.CR_CRASHED


def phase_state_for_classification(classification: str) -> str:
    if classification == model.CR_COMPLETED:
        return model.PS_PASSED
    if classification == model.CR_FAILED:
        return model.PS_FAILED
    return model.PS_UNPROVEN  # CRASHED / UNPROVEN: explicit, fail closed


class ProcessPhaseRunner:
    """Bounded subprocess runner (one bounded process per sub-run)."""

    def run(self, request: SubrunRequest) -> SubrunResult:
        out_path = Path(request.stdout_file)
        err_path = Path(request.stderr_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        err_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = list(request.command)
        if not cmd:
            return SubrunResult(exit_code=None, classification=model.CR_UNPROVEN)
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
                    timeout=request.timeout_s,
                    check=False,
                )
                exit_code = int(proc.returncode)
            except subprocess.TimeoutExpired:
                timed_out = True
            except (OSError, subprocess.SubprocessError):
                exit_code = None
        classification = classify_exit(exit_code, timed_out=timed_out)
        return SubrunResult(exit_code=exit_code, classification=classification,
                            timed_out=timed_out)


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
        result = SubrunResult(exit_code=exit_code, classification=classify_exit(exit_code))
        if self.die_after_phase is not None and request.phase_id == self.die_after_phase:
            raise FakeRunnerDeath("orchestrator crashed after sub-run for " + request.phase_id)
        return result
