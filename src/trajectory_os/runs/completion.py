"""V1.94 — Authoritative Completion and Exit Evidence.

Terminal state is produced **only from evidence**, never inferred, guessed,
or fabricated:

* a live ``observe`` reading (owner-kill proof / live probe / supervisor
  exit code) is the authoritative source;

* supervisor evidence is the recorded per-job exit code captured by the
  orchestration supervisor's process handle (``exit:<int>``) — the only
  place an exit code is *invented-free* because it is *read* from the
  kernel for that exact process;

* ``NOT_LIVE`` without evidence is explicit ``crashed`` (process is gone;
  no exit code is attributed — one must not be invented);

* unproven records are explicit ``unknown`` — a dead-but-unproven record
  is never labeled done/failed/crashed without evidence of how;

* a terminal that would contradict an earlier terminal record for the same
  (job, seq, attempts) is rejected (``TERMINAL_CONFLICT``) — deterministic,
  fail-closed, and reap stays idempotent when it sees its own prior record.

The supervisor launch registry maps job_id -> live subprocess handle so the
supervisor can read authoritative exit codes *immediately after process
exit* (eliminating the race where the process is reaped by its parent
before observation).  The registry is bounded and cleared on terminal.
"""

from __future__ import annotations

import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from trajectory_os.runs import model, spec

_MAX_LAUNCH_REGISTRY = 64


@dataclass(frozen=True)
class Classification:
    """Authoritative terminal classification for one record + evidence."""

    job_id: str
    seq: int
    attempts: int
    terminal: str            # valid member of model.TERMINALS
    exit_code: int | None    # set ONLY from authoritative evidence
    signal: int | None       # set ONLY from authoritative evidence
    evidence: str            # machine-readable evidence label (never empty)
    reason_codes: tuple[str, ...]  # owner-kill proof reason codes that applied

    @property
    def is_terminal(self) -> bool:
        return self.terminal in model.TERMINALS


class EvidenceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class TerminalConflictError(Exception):
    """An existing terminal record contradicts the new classification."""

    def __init__(self, job_id: str, seq: int, attempts: int,
                 existing: str, proposed: str) -> None:
        super().__init__(
            f"TERMINAL_CONFLICT: job={job_id} seq={seq} attempts={attempts} "
            f"existing_terminal={existing} proposed_terminal={proposed}"
        )
        self.code = model.ERR_TERMINAL_CONFLICT
        self.job_id = job_id
        self.seq = seq
        self.attempts = attempts
        self.existing_terminal = existing
        self.proposed_terminal = proposed


def classify(
    *,
    job_id: str,
    seq: int,
    attempts: int,
    owner_kill_reasons: tuple[str, ...] | list[str] | set[str],
    exit_code: int | None = None,
    signal: int | None = None,
    evidence: str | None = None,
) -> Classification:
    """Deterministically map live evidence to an authoritative terminal.

    Rules (fail-closed, no invention):
      * ``NOT_LIVE`` in reasons + exit_code known -> done/failed (with code)
      * ``NOT_LIVE`` in reasons + signal known     -> crashed (with signal)
      * ``NOT_LIVE`` in reasons + no evidence      -> crashed (no code invented)
      * no ``NOT_LIVE`` (unproven)                 -> unknown (explicit)
    """
    reasons = tuple(owner_kill_reasons)
    not_live = any(r in ("NOT_LIVE", model.REASON_OWNERSHIP_UNPROVEN)
                   for r in reasons)
    if not_live:
        if exit_code is not None:
            terminal = spec.TERMINAL_DONE if exit_code == 0 else spec.TERMINAL_FAILED
            ev = evidence or f"exit:{exit_code}"
        elif signal is not None:
            terminal = spec.TERMINAL_CRASHED
            ev = evidence or f"killed:{signal}"
        else:
            terminal = spec.TERMINAL_CRASHED
            ev = evidence or "observed_dead"
    else:
        terminal = spec.TERMINAL_UNKNOWN
        ev = evidence or ("unproven:" + ",".join(reasons) if reasons else "unproven")
    return Classification(
        job_id=job_id,
        seq=seq,
        attempts=attempts,
        terminal=terminal,
        exit_code=exit_code,
        signal=signal,
        evidence=ev,
        reason_codes=reasons,
    )


def _parse_evidence(raw: object) -> tuple[int | None, int | None, str]:
    """Parse an observe reading into ``(exit_code, signal, evidence_label)``.

    Accepted wire formats (everything else fails closed; an unrecognised
    reading never attributes an exit code or signal to a job):

    * ``None``                              -> ``(None, None, "")``;
    * signed ``int`` (not ``bool``)          -> ``(n, None, "exit:<n>")``;
    * ``str`` ``"exit:<signed int>"``        -> ``(n, None, "exit:<n>")``
      (a negative value is treated as a non-zero exit -> ``failed``, never
      reinterpreted as a signal);
    * ``str`` ``"killed:<SIGNAL_NAME>"``     -> ``(None, sig, label)`` when
      the name resolves to a real signal number, else
      ``(None, None, "killed:unknown")`` (no signal invented);
    * ``str`` of bare digits                 -> ``(n, None, "exit:<n>")``;
    * other ``str``                          -> ``(None, None,
      "evidence:unrecognized")``;
    * ``dict`` with ``"exit": int`` (not ``bool``) -> the ``exit:<int>``
      form above;
    * ``dict`` with ``"signal": str`` (a signal NAME, never a number) ->
      the ``killed:<name>`` form above;
    * any other ``dict``                      -> ``EvidenceError(
      "EVIDENCE_MALFORMED")``;
    * ``bool`` (bare ``True`` / ``False`` are a Python ``int`` subclass but
      are NOT valid exit codes) -> ``EvidenceError("EVIDENCE_MALFORMED")``;

    ``list`` / ``tuple`` / ``set`` / any other type ->
    ``EvidenceError("EVIDENCE_MALFORMED")``.
    """
    if raw is None:
        return None, None, ""
    if isinstance(raw, (bool, list, tuple, set)):
        raise EvidenceError("EVIDENCE_MALFORMED", repr(raw)[:100])
    if isinstance(raw, int):
        return raw, None, f"exit:{raw}"
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("exit:"):
            payload = text[len("exit:"):]
            if payload.lstrip("-").isdigit():
                # Signed exit code. A negative value is a kernel
                # abnormal-exit marker: it is reported as-is (non-zero
                # -> ``failed``) and is never reinterpreted as a signal.
                return int(payload), None, text
        if text.startswith("killed:"):
            sig = _signal_number(text[len("killed:"):])
            if sig is not None:
                return None, sig, text
            # unknown signal word -> no signal attributed (fail closed)
            return None, None, "killed:unknown"
        if text.isdigit():
            return int(text), None, f"exit:{text}"
        # Unrecognized reading: fail closed — no fabricated terminal fields.
        return None, None, "evidence:unrecognized"
    if isinstance(raw, dict):
        if isinstance(raw.get("exit"), int) and not isinstance(raw.get("exit"), bool):
            return int(raw["exit"]), None, f"exit:{int(raw['exit'])}"
        sig_name = raw.get("signal")
        if isinstance(sig_name, str):
            sig = _signal_number(sig_name)
            if sig is not None:
                return None, sig, f"killed:{sig_name}"
        raise EvidenceError("EVIDENCE_MALFORMED", f"unrecognized dict: {str(raw)[:60]}")
    raise EvidenceError("EVIDENCE_MALFORMED", f"unrecognized type: {type(raw)!r}")


def _signal_number(name: str) -> int | None:
    try:
        value = getattr(signal, str(name).upper())
    except (AttributeError, ValueError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def check_terminal_conflict(
    *,
    job_id: str,
    seq: int,
    attempts: int,
    proposed_terminal: str,
    existing_records: Any,
) -> None:
    """Fail closed if an earlier record already fixed a DIFFERENT terminal.

    Idempotent reap produces the same terminal for the same record +
    evidence; any divergence (e.g. a crash after a done record for the same
    job/seq/attempts) is a state conflict — it must be surfaced, not
    overwritten.
    """
    for record in existing_records:
        rec_job = getattr(record, "job_id", None)
        rec_seq = getattr(record, "seq", None)
        rec_att = getattr(record, "attempts", None)
        rec_term = getattr(record, "terminal", None)
        if rec_job == job_id and rec_seq == seq and rec_att is not None \
                and rec_att == attempts and rec_term is not None \
                and rec_term != proposed_terminal:
            raise TerminalConflictError(job_id, seq, attempts,
                                        rec_term, proposed_terminal)


# ---------------------------------------------------------------------------
# Supervisor launch registry (authoritative, immediately-available exits)
# ---------------------------------------------------------------------------

_registry: dict[str, subprocess.Popen[Any]] = {}
_registry_lock = threading.Lock()


def supervisor_register_launch(job_id: str, process: subprocess.Popen[Any]) -> None:
    """Register the supervisor's handle for a just-launched job (bounded)."""
    with _registry_lock:
        if len(_registry) >= _MAX_LAUNCH_REGISTRY:
            # Bounded: drop the oldest entry (deterministic by insertion).
            oldest = next(iter(_registry.keys()))
            _registry.pop(oldest, None)
        _registry[job_id] = process


def supervisor_release_launch(job_id: str) -> None:
    with _registry_lock:
        _registry.pop(job_id, None)


def supervisor_clear_all() -> None:
    """Test/tooling hook: drop all launches (does not affect live processes)."""
    with _registry_lock:
        _registry.clear()


def supervisor_observe(job_ids: list[str]) -> dict[str, object]:
    """Return authoritative immediate-exit evidence for finished processes.

    Concurrency and determinism contract:

    * **no blocking** — ``Popen.poll()`` uses ``waitpid`` with ``WNOHANG``;
      a running child is never awaited and simply omitted from the result
      (a live process must never yield terminal evidence);
    * **no fabrication** — an exit code is reported only when the kernel
      delivered one for *that exact* registered handle;
    * **atomic observation** — the whole scan (lookup, status sampling and
      registry release) runs inside a single critical section of
      ``_registry_lock``; concurrent ``observe`` / ``register_launch`` /
      ``release_launch`` calls therefore interleave only at critical-section
      boundaries, so one call cannot read a process it simultaneously
      releases, and a finished job is released exactly once.  Two
      concurrent observations of the same finished handle return the same
      kernel-delivered status (``Popen`` serialises ``waitpid`` internally
      and caches the result), so evidence is consistent across observers.

    Running / unregistered jobs are omitted; order of the returned dict
    follows ``job_ids`` (deterministic).
    """
    result: dict[str, object] = {}
    with _registry_lock:
        for job_id in job_ids:
            proc = _registry.get(job_id)
            if proc is None:
                continue
            try:
                returncode = proc.poll()
            except (subprocess.SubprocessError, OSError):
                # Status unsampleable for this handle: fail closed — omit
                # (no exit status invented) and keep the registration for a
                # later authoritative sampling.
                continue
            if returncode is not None:
                _registry.pop(job_id, None)
                result[job_id] = f"exit:{returncode}"
    return result
