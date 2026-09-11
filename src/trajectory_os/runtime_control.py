"""Deterministic runtime-control core for TrajectoryOS Pi runs (V1.73-V1.80).

Provider-agnostic, stdlib-only, local-only building blocks that let the
``trajectory-pi-control`` CLI and the ``trajectory-pi-status`` reader answer
control-plane questions about a **recorded** run:

* **V1.73 safe targeting** — a recorded pid is only *targetable* when it was
  producer-recorded, the process is alive, its working directory and start
  time match the run's fingerprint, and it is not one of our own processes.
* **V1.74 graceful stop** — bounded SIGTERM wait; *no SIGKILL path exists in
  this module by design* (asserted by tests).  A stop that does not finish
  within the grace window is reported as a timeout, never escalated.
* **V1.75 anti-stale / anti-reuse** — a recorded pid whose process is dead is
  never active; a live pid that matches the directory but not the recorded
  start window (the pid-reuse signature) is never stoppable; ambiguous
  targets fail closed instead of guessing.
* **V1.76 controlled recovery** — a read-only *recovery readiness* view over
  producer evidence (terminal state + valid transcript + clean exit
  evidence).  It proposes a *resume hint shape* only; it never acts.
* **V1.78 structured results** — :func:`build_result` assembles the stable,
  fixed-shape machine-readable *result contract* for one control operation:
  every field either carries measured evidence or is ``None`` (unknown is
  never invented; key order and presence are constant).
* **V1.79 bounded audit trail** — :func:`append_audit_record` appends one
  sanitized record to a bounded, local audit file under the runs root.
  Records pass through a strict allow-list so secrets, tokens and
  environment values can never enter the trail; malformed prior trail state
  is preserved byte-for-byte and never crashes the writer.

Everything here is deterministic and side-effect-free except the explicit IO
helpers (``snapshot_process``, ``classify_run_state``,
``collect_recovery_facts``, the lock functions, ``append_control_event``,
``send_sigterm``, ``append_audit_record``).  Each touches only its run
directory, the runs-root audit file, or ``/proc`` reads of a specific pid —
never network, never daemons, never arbitrary paths.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "ACTIVE_OBSERVED_STATES",
    "CONTROL_CORE",
    "EVENTS_FILENAME",
    "LOCK_ACTIONS",
    "LOCK_FILENAME",
    "RUN_DIR_RE",
    "STALE_AFTER_SECONDS",
    "AUDIT_FILENAME",
    "AUDIT_KEY_ORDER",
    "AUDIT_MAX_RECORDS",
    "AUDIT_SCHEMA",
    "IdentityVerdict",
    "LockInfo",
    "ProcSnapshot",
    "RESULT_FIELDS",
    "RESULT_SCHEMA",
    "RecoveryFacts",
    "RunControlView",
    "acquire_control_lock",
    "ancestor_chain",
    "append_audit_record",
    "append_control_event",
    "build_view",
    "build_result",
    "classify_run_state",
    "collect_recovery_facts",
    "evaluate_recovery",
    "evaluate_run",
    "is_pid_alive",
    "is_pid_terminated",
    "process_zombie",
    "make_resume_hint",
    "parse_meta",
    "read_lock",
    "release_control_lock",
    "sanitize_audit_record",
    "send_sigterm",
    "snapshot_process",
    "target_pid_for_stop",
    "verify_pid_identity",
]

#: Semver of this control-surface contract (bumped when JSON shapes change).
CONTROL_CORE = "1.76"

#: Observed run states in which a run is still a running process (or was,
#: until its last heartbeat).  ``ended``/``unknown`` are never targetable.
ACTIVE_OBSERVED_STATES = ("running", "stale")

#: A heartbeat is "fresh" within this window; beyond it the run is *stale*
#: (consistent with the V1.67 reader, which this surface composes with).
STALE_AFTER_SECONDS = 120.0

RUN_DIR_RE = re.compile(r"^\d{8}-\d{6}$")
PID_RE = re.compile(r"^\d+$")
# Producer heartbeat line: "[HH:MM:SS] elapsed=HH:MM:SS | ..." (V1.67+).
HB_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s+elapsed=(\d{2}):(\d{2}):(\d{2})")
# Producer terminal line: "[HH:MM:SS] rc=N".
END_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s+rc=(-?\d+)$")

LOCK_FILENAME = "control.lock"
EVENTS_FILENAME = "control-events.jsonl"
AUDIT_FILENAME = "control-audit.jsonl"
LOCK_ACTIONS = ("stop",)


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _epoch_from_ts(value: str) -> float | None:
    """Parse a producer timestamp (local or offset-qualified) to epoch."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        candidate = text.replace("Z", "+00:00")
        if " " in candidate:
            candidate = candidate.replace(" ", "T", 1)
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        pass
    head = text[:19].replace("T", " ")
    try:
        return time.mktime(time.strptime(head, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def parse_meta(meta_path: Path | str) -> dict[str, str]:
    """Parse a producer ``meta.txt`` (``key=value`` lines; last wins)."""
    values: dict[str, str] = {}
    path = Path(meta_path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and re.fullmatch(r"[\w.+:-]+", key):
            values[key] = value.strip()
    return values


def is_pid_alive(pid: int) -> bool:
    """True when ``pid`` references an existing process in this PID namespace.

    A zombie (terminated, awaiting reap) still answers ``kill(pid, 0)``;
    use :func:`is_pid_terminated` for control decisions.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def process_zombie(pid: int) -> bool:
    """True when ``/proc`` reports ``pid`` in the zombie (Z) state."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return False
    fields = raw.rsplit(")", 1)[-1].split()
    return bool(fields) and fields[0] == "Z"


def is_pid_terminated(pid: int) -> bool:
    """True when ``pid`` is dead or already a zombie (no longer runnable)."""
    return not is_pid_alive(pid) or process_zombie(pid)


def _clock_ticks() -> int:
    return int(os.sysconf("SC_CLK_TCK"))


def _boot_epoch() -> float | None:
    """Approximate OS boot epoch from ``/proc/uptime`` (Linux)."""
    try:
        with open("/proc/uptime", encoding="ascii") as handle:
            uptime = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return time.time() - uptime


# ---------------------------------------------------------------------------
# V1.73 / V1.75 — process observation and safe identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcSnapshot:
    """Point-in-time, read-only observation of one process via ``/proc``."""

    pid: int
    alive: bool
    cwd: str | None
    start_epoch: float | None


def snapshot_process(pid: int) -> ProcSnapshot:
    """Read a non-invasive snapshot of ``pid`` from ``/proc`` (Linux).

    ``start_epoch`` comes from ``/proc/<pid>/stat`` field 22 (``starttime``,
    clock ticks since boot) plus ``/proc/uptime`` — read-only, the target
    process is never signalled.
    """
    if not is_pid_alive(pid) or process_zombie(pid):
        return ProcSnapshot(pid=pid, alive=False, cwd=None, start_epoch=None)
    try:
        cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
    except (OSError, ValueError):
        cwd = None
    start_epoch: float | None = None
    boot = _boot_epoch()
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        # comm is field 2 and may contain spaces/parens; anchor on the last
        # ')' so the fixed fields line up.  starttime is field 22 (index 19
        # after comm) and is clock ticks since boot.
        fields = stat.rsplit(")", 1)[1].split()
        starttime_ticks = int(fields[19])
        if boot is not None:
            start_epoch = boot + starttime_ticks / _clock_ticks()
    except (OSError, ValueError, IndexError):
        start_epoch = None
    return ProcSnapshot(pid=pid, alive=True, cwd=cwd, start_epoch=start_epoch)


def ancestor_chain(pid: int, max_depth: int = 256) -> tuple[int, ...]:
    """Parents of ``pid`` up the ``/proc`` ppid chain (loop-safe)."""
    chain: list[int] = []
    current = pid
    seen: set[int] = set()
    while current > 0 and len(chain) < max_depth and current not in seen:
        seen.add(current)
        try:
            stat = Path(f"/proc/{current}/stat").read_text(encoding="ascii", errors="replace")
        except (OSError, ValueError):
            break
        fields = stat.rsplit(")", 1)[1].split()
        if len(fields) < 2:
            break
        try:
            parent = int(fields[1])
        except ValueError:
            break
        if parent <= 0:
            break
        chain.append(parent)
        current = parent
    return tuple(chain)


@dataclass(frozen=True)
class IdentityVerdict:
    """Safe-targeting verdict for one recorded pid against one expected fp."""

    state: str  # "verified" | "mismatch" | "dead" | "unknown"
    reasons: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return self.state == "verified"


def verify_pid_identity(
    *,
    pid: int,
    workspace: str | None,
    run_started_epoch: float | None,
    snapshot: ProcSnapshot,
    start_window_seconds: float = 300.0,
    own_pids: tuple[int, ...] = (),
) -> IdentityVerdict:
    """Pure identity check: is ``snapshot`` plausibly **this run's agent**?

    All factors are required; any failure fails closed with a stable reason:

    * the process must be alive;
    * its working directory must match the run's recorded workspace
      (realpath-compared) — the primary wrong-target detector;
    * its start time must fall within ``start_window_seconds`` of the run's
      recorded start — the primary pid-reuse detector (a recycled pid that
      started long after the run's recorded start cannot match);
    * it must not be one of the evaluator's own process tree pids (never
      signal our own processes).
    """
    if pid <= 0 or not PID_RE.fullmatch(str(pid)):
        return IdentityVerdict("unknown", ("invalid-pid",))
    if pid in own_pids:
        return IdentityVerdict("mismatch", ("own-process-excluded",))
    if not snapshot.alive:
        return IdentityVerdict("dead", ("process-dead",))
    reasons: list[str] = []
    if workspace is None:
        reasons.append("expected-workspace-missing")
    elif snapshot.cwd is None:
        reasons.append("live-cwd-unreadable")
    elif os.path.realpath(snapshot.cwd) != os.path.realpath(workspace):
        reasons.append("cwd-mismatch")
    if run_started_epoch is None:
        reasons.append("expected-start-time-missing")
    elif snapshot.start_epoch is None:
        reasons.append("live-start-time-unreadable")
    elif abs(snapshot.start_epoch - run_started_epoch) > start_window_seconds:
        reasons.append("start-time-window-exceeded")
    if reasons:
        return IdentityVerdict("mismatch", tuple(reasons))
    return IdentityVerdict("verified", ("alive", "cwd-matched", "start-window-matched"))


# ---------------------------------------------------------------------------
# Run state classification (composes with the V1.67 reader contract)
# ---------------------------------------------------------------------------


def _last_elapsed_seconds(status_log: Path) -> float | None:
    """Seconds of the last producer heartbeat in ``status.log`` (if any)."""
    last: float | None = None
    try:
        lines = status_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        match = HB_RE.match(line.strip())
        if match is not None:
            h, m, s = (int(part) for part in match.groups())
            last = h * 3600 + m * 60 + s
    return last


def classify_run_state(
    run_dir: Path | str,
    now_epoch: float | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Classify a run directory as ``running``/``stale``/``ended``/``unknown``.

    Same observable contract as the V1.67 reader: ended evidence wins, then
    heartbeat freshness against the run's recorded start.  Deterministic in
    ``now_epoch`` and run contents.
    """
    run_dir = Path(run_dir)
    meta = parse_meta(run_dir / "meta.txt")
    if _epoch_from_ts(meta.get("ended_at", "")) is not None:
        return "ended", ("ended-at-recorded",)
    started_epoch = _epoch_from_ts(meta.get("started_at", ""))
    now = time.time() if now_epoch is None else now_epoch
    last_elapsed = _last_elapsed_seconds(run_dir / "status.log")
    if started_epoch is None:
        return "unknown", ("started-at-missing-or-unparseable",)
    if last_elapsed is None:
        return "unknown", ("no-heartbeat-evidence", "started-parseable")
    last_age = (now - started_epoch) - last_elapsed
    if last_age <= STALE_AFTER_SECONDS:
        return "running", ("heartbeat-fresh",)
    return "stale", ("heartbeat-aged-beyond-stale-window",)


# ---------------------------------------------------------------------------
# V1.76 — recovery-readiness (read-only; proposes, never acts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryFacts:
    """Producer-evidence facts that a recovery would consume."""

    transcript_present: bool
    last_line_valid: bool
    run_id_consistent: bool
    pi_exit_code: str  # producer value, or "unknown"
    evidence_source: str  # "status.log" | "pi.log" | "none"


def collect_recovery_facts(run_dir: Path | str, expected_run_id: str) -> RecoveryFacts:
    """Read-only collection of recovery-readiness evidence from a run dir.

    The *producer transcript* is the strongest structured evidence stream in
    the run directory (``status.log``, then ``pi.log``); its last non-empty
    line must be a schema-valid producer line for readiness.
    """
    run_dir = Path(run_dir)
    meta = parse_meta(run_dir / "meta.txt")
    run_id = meta.get("run_id")
    run_id_consistent = run_id is None or run_id == expected_run_id
    pi_exit_code = meta.get("pi_exit_code", "unknown")

    transcript_present = False
    last_line_valid = False
    source = "none"
    for name in ("status.log", "pi.log"):
        path = run_dir / name
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        non_empty = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if not non_empty:
            continue
        transcript_present = True
        source = name
        last = non_empty[-1]
        last_line_valid = bool(HB_RE.match(last)) or bool(END_RE.match(last))
        break
    return RecoveryFacts(
        transcript_present=transcript_present,
        last_line_valid=last_line_valid,
        run_id_consistent=run_id_consistent,
        pi_exit_code=pi_exit_code,
        evidence_source=source,
    )


def evaluate_recovery(
    *,
    state: str,
    facts: RecoveryFacts,
) -> tuple[bool, tuple[str, ...]]:
    """Pure V1.76 decision: is this run ready for a controlled resume?

    Ready only when *all* hold (fail-closed, deterministic):

    * the run is ``ended`` (terminal, producer-recorded) — a running or
      stale run is never "recoverable";
    * the run identity is consistent (recorded run_id equals the directory);
    * the producer transcript exists and its last line is schema-valid;
    * the exit evidence is complete and clean (``pi_exit_code=0``).

    A crashed run (non-zero or missing exit evidence) is deliberately *not*
    ready, with the precise reason, so the surface never pretends a resume
    is safe.
    """
    if state != "ended":
        return False, (f"state-is-{state}",)
    reasons: list[str] = []
    if not facts.run_id_consistent:
        reasons.append("run-id-inconsistent")
    if not facts.transcript_present:
        reasons.append("transcript-missing")
    elif not facts.last_line_valid:
        reasons.append("last-evidence-line-invalid")
    exit_code = facts.pi_exit_code
    if exit_code in ("", "unknown"):
        reasons.append("exit-evidence-missing")
    elif exit_code != "0":
        reasons.append(f"abnormal-exit:{exit_code}")
    if reasons:
        return False, tuple(reasons)
    return True, ("terminal-state", "transcript-valid", "clean-exit-evidence")


def make_resume_hint(
    *,
    run_id: str,
    workspace: str | None,
    facts: RecoveryFacts,
) -> dict[str, Any]:
    """Deterministic read-only resume hint built from producer evidence.

    This is the *proposed shape* of a resume request (V1.77 implements the
    action itself).  It carries only evidence already on disk in the run
    directory; no external lookups.
    """
    return {
        "schema": "resume-hint/1",
        "run_id": run_id,
        "workspace": workspace if workspace is not None else "unknown",
        "evidence": {
            "source": facts.evidence_source,
            "exit_code": facts.pi_exit_code,
        },
                    "suggested_next_action": "resume-run (proposed only by this surface)",
            "note": "read-only proposal; no action taken by this surface",
    }


# ---------------------------------------------------------------------------
# V1.73 targetability composition (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunControlView:
    """The complete deterministic control surface for one run (V1.73-V1.76)."""

    run_id: str
    state: str
    state_reasons: tuple[str, ...]
    target_pid: int | None
    identity: IdentityVerdict
    targetable: bool
    targetable_reasons: tuple[str, ...]
    stoppable: bool
    stoppable_reasons: tuple[str, ...]
    recovery_ready: bool
    recovery_reasons: tuple[str, ...]
    resume_hint: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": f"trajectory-pi-control/{CONTROL_CORE}",
            "run_id": self.run_id,
            "state": self.state,
            "state_reasons": list(self.state_reasons),
            "target_pid": self.target_pid,
            "identity": {"state": self.identity.state, "reasons": list(self.identity.reasons)},
            "targetable_safe": self.targetable,
            "targetable_reasons": list(self.targetable_reasons),
            "stoppable": self.stoppable,
            "stoppable_reasons": list(self.stoppable_reasons),
            "recovery": {"ready": self.recovery_ready, "reasons": list(self.recovery_reasons)},
        }
        if self.resume_hint is not None:
            out["resume_hint"] = self.resume_hint
        return out


def target_pid_for_stop(meta: Mapping[str, str]) -> int | None:
    """Choose the stop signal target from producer evidence.

    Prefers the producer-observed ``pi_pid`` (the agent process itself);
    falls back to the wrapper ``pid`` (whose TERM trap forwards the signal).
    Absent or unparseable => no target (fail closed).
    """
    for key in ("pi_pid", "pid"):
        value = meta.get(key, "")
        if PID_RE.fullmatch(value):
            return int(value)
    return None


def build_view(
    *,
    run_id: str,
    state: str,
    state_reasons: tuple[str, ...],
    meta: Mapping[str, str],
    workspace: str | None,
    run_started_epoch: float | None,
    snapshot: ProcSnapshot | None,
    facts: RecoveryFacts,
    own_pids: tuple[int, ...] = (),
) -> RunControlView:
    """Pure composition of all V1.73-V1.76 verdicts into one view."""
    target = target_pid_for_stop(meta)
    identity = IdentityVerdict("unknown", ("no-recorded-pid",))
    targetable = False
    stoppable = False
    targetable_reasons: tuple[str, ...] = ("no-recorded-pid",)
    stoppable_reasons: tuple[str, ...] = ("no-recorded-pid",)

    if target is not None:
        snap = snapshot if snapshot is not None else ProcSnapshot(
            pid=target, alive=False, cwd=None, start_epoch=None
        )
        identity = verify_pid_identity(
            pid=target,
            workspace=workspace,
            run_started_epoch=run_started_epoch,
            snapshot=snap,
            own_pids=own_pids,
        )
        state_ok = state in ACTIVE_OBSERVED_STATES
        targetable = state_ok and identity.verified
        targetable_reasons = ()
        if not state_ok:
            targetable_reasons = targetable_reasons + (f"state-is-{state}",)
        if not identity.verified:
            targetable_reasons = targetable_reasons + identity.reasons
        stoppable = bool(targetable)
        stoppable_reasons = () if stoppable else targetable_reasons

    if state == "ended":
        recovery_ready, recovery_reasons = evaluate_recovery(state=state, facts=facts)
        hint = (
            make_resume_hint(run_id=run_id, workspace=workspace, facts=facts)
            if recovery_ready
            else None
        )
    else:
        recovery_ready, recovery_reasons = False, (f"state-is-{state}",)
        hint = None

    return RunControlView(
        run_id=run_id,
        state=state,
        state_reasons=state_reasons,
        target_pid=target,
        identity=identity,
        targetable=bool(targetable),
        targetable_reasons=targetable_reasons,
        stoppable=bool(stoppable),
        stoppable_reasons=stoppable_reasons,
        recovery_ready=bool(recovery_ready),
        recovery_reasons=tuple(recovery_reasons),
        resume_hint=hint,
    )


def evaluate_run(
    run_dir: Path | str,
    *,
    now_epoch: float | None = None,
    own_pids: tuple[int, ...] = (),
) -> RunControlView:
    """IO wrapper: classify + snapshot + collect evidence, then build a view.

    Read-only on the run directory and ``/proc``; never signals a process.
    """
    run_dir = Path(run_dir)
    expected_run_id = run_dir.name
    state, state_reasons = classify_run_state(run_dir, now_epoch=now_epoch)
    meta = parse_meta(run_dir / "meta.txt")
    target = target_pid_for_stop(meta)
    snapshot = snapshot_process(target) if target is not None else None
    workspace = meta.get("workspace")
    if workspace is None:
        # Runs live at <workspace>/.trajectory-pi/runs/<name>.
        workspace = str(run_dir.parents[2]) if len(run_dir.parents) >= 3 else None
    run_started_epoch = _epoch_from_ts(meta.get("started_at", ""))
    facts = collect_recovery_facts(run_dir, expected_run_id)
    return build_view(
        run_id=expected_run_id,
        state=state,
        state_reasons=state_reasons,
        meta=meta,
        workspace=workspace,
        run_started_epoch=run_started_epoch,
        snapshot=snapshot,
        facts=facts,
        own_pids=own_pids,
    )


# ---------------------------------------------------------------------------
# V1.74 — run-local locking and graceful-stop primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LockInfo:
    holder: int | None
    action: str | None
    corrupt: bool


def read_lock(run_dir: Path | str) -> LockInfo:
    path = Path(run_dir) / LOCK_FILENAME
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return LockInfo(holder=None, action=None, corrupt=False)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return LockInfo(holder=None, action=None, corrupt=True)
    if not isinstance(data, dict):
        return LockInfo(holder=None, action=None, corrupt=True)
    holder = data.get("holder")
    holder = int(holder) if isinstance(holder, str) and PID_RE.fullmatch(holder) else None
    action = data.get("action") if isinstance(data.get("action"), str) else None
    return LockInfo(holder=holder, action=action, corrupt=False)


def acquire_control_lock(
    run_dir: Path | str,
    action: str,
    holder_pid: int | None = None,
) -> str:
    """Acquire the run-local control lock.  Returns the outcome:

    ``"acquired"``   a fresh lock is now held by us;
    ``"reclaimed"``  a stale lock (dead holder) was safely replaced by us;
    ``"conflict"``   a live holder (other than us) owns it — fail closed;
    ``"corrupt"``    the lock file is unreadable — fail closed, never guess.
    """
    if action not in LOCK_ACTIONS:
        raise ValueError(f"unknown lock action: {action!r}")
    run_dir = Path(run_dir)
    holder = holder_pid if holder_pid is not None else os.getpid()
    info = read_lock(run_dir)
    if info.corrupt:
        return "corrupt"
    if info.holder is not None and info.holder != holder:
        if is_pid_alive(info.holder):
            return "conflict"
        outcome = "reclaimed"  # dead foreign holder: safe to replace
    else:
        outcome = "acquired"   # absent or already ours
    payload = json.dumps(
        {"schema": "control-lock/1", "action": action, "holder": str(holder)},
        sort_keys=True,
    )
    try:
        fd = os.open(run_dir / LOCK_FILENAME, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError:
        return "conflict"
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload + "\n")
    return outcome


def release_control_lock(run_dir: Path | str) -> None:
    """Release the lock (the lock file is run-local and ours to remove)."""
    with contextlib.suppress(OSError):
        (Path(run_dir) / LOCK_FILENAME).unlink()


def append_control_event(run_dir: Path | str, **fields: Any) -> None:
    """Append one structured, timestamped event to the run's event stream."""
    run_dir = Path(run_dir)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "component": "trajectory-pi-control",
        **fields,
    }
    with (run_dir / EVENTS_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def send_sigterm(pid: int) -> bool:
    """Send SIGTERM to exactly ``pid``.  Returns True when sent.

    This module has *no* SIGKILL path: a process that does not exit within
    the grace window is the caller's decision to report as a timeout — never
    to escalate.
    """
    if signal.SIGTERM is None:  # pragma: no cover (non-POSIX)
        raise RuntimeError("SIGTERM is unavailable on this platform")
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OverflowError):
        return False
    return True


# ---------------------------------------------------------------------------
# V1.78 — structured control result contract (pure)
# ---------------------------------------------------------------------------

#: Schema identifier of the machine-readable result envelope (stable).
RESULT_SCHEMA = "trajectory-pi-control-result/1"

#: Exact, ordered field set of every structured result.
#: Presence and order are part of the contract: a result always has exactly
#: these keys, in this order.  A field that is unknown is present with the
#: JSON value ``null`` — unknown is never omitted and never invented.
RESULT_FIELDS: tuple[str, ...] = (
    "schema",
    "command",
    "run_id",
    "outcome",
    "action_requested",
    "action_performed",
    "reasons",
    "target_state",
    "targetable",
    "stoppable",
    "target_pid",
    "signal_requested",
    "signal_sent",
    "lock",
    "grace_seconds",
    "identity",
    "lifecycle",
    "audit",
    "generated_at",
    "evidence_source",
)


def _result_identity(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize an identity evidence mapping; unknown stays ``None``."""
    if value is None:
        return None
    return {
        "state": value.get("state"),
        "reasons": [str(reason) for reason in (value.get("reasons") or [])],
    }


def _result_lifecycle(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a lifecycle evidence mapping; unknown stays ``None``."""
    if value is None:
        return None
    return {
        "state": value.get("state"),
        "reasons": [str(reason) for reason in (value.get("reasons") or [])],
        "started_at": value.get("started_at"),
        "ended_at": value.get("ended_at"),
    }


def build_result(
    *,
    command: str,
    outcome: str,
    reasons: Sequence[str] = (),
    run_id: str | None = None,
    action_requested: str | None = None,
    action_performed: str | None = None,
    target_state: str | None = None,
    targetable: bool | None = None,
    stoppable: bool | None = None,
    target_pid: int | None = None,
    signal_requested: str | None = None,
    signal_sent: bool | None = None,
    lock: str | None = None,
    grace_seconds: float | None = None,
    identity: Mapping[str, Any] | None = None,
    lifecycle: Mapping[str, Any] | None = None,
    audit: str | None = None,
    generated_at: str | None = None,
    evidence_source: str | None = "run-directory",
) -> dict[str, Any]:
    """Assemble one structured control operation result (V1.78 contract).

    Rules baked in here (not in the callers):

    * the result has exactly :data:`RESULT_FIELDS`, in that order;
    * every unknown fact is JSON ``null`` — never an invented value;
    * absent evidence mappings render as ``null``, not as empty objects;
    * ``generated_at`` defaults to the literal ``"unknown"`` so a result is
      never stamped with an unobserved clock;
    * the output is a pure function of the arguments (deterministic).
    """
    if not isinstance(command, str) or not command:
        raise ValueError("build_result: command must be a non-empty string")
    if not isinstance(outcome, str) or not outcome:
        raise ValueError("build_result: outcome must be a non-empty string")

    out: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "command": command,
        "run_id": run_id,
        "outcome": outcome,
        "action_requested": action_requested,
        "action_performed": action_performed,
        "reasons": [str(reason) for reason in reasons],
        "target_state": target_state,
        "targetable": targetable,
        "stoppable": stoppable,
        "target_pid": target_pid,
        "signal_requested": signal_requested,
        "signal_sent": signal_sent,
        "lock": lock,
        "grace_seconds": grace_seconds,
        "identity": _result_identity(identity),
        "lifecycle": _result_lifecycle(lifecycle),
        "audit": audit,
        "generated_at": generated_at if generated_at is not None else "unknown",
        "evidence_source": evidence_source,
    }
    return {key: out[key] for key in RESULT_FIELDS}


# ---------------------------------------------------------------------------
# V1.79 — bounded local audit trail for control operations
# ---------------------------------------------------------------------------

#: Schema identifier + record count bound of the audit trail contract.
AUDIT_SCHEMA = "control-audit/1"

#: Maximum number of records kept in the trail; older records are trimmed
#: deterministically (the newest records are retained).
AUDIT_MAX_RECORDS = 256

#: Strict allow-list: only these keys may ever enter an audit record.
#: Everything else (environment values, tokens, credentials, provider auth
#: state, arbitrary caller data) is dropped, so the trail cannot leak
#: secrets by construction.
_AUDIT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "schema",
        "ts",
        "component",
        "command",
        "decision",
        "outcome",
        "reasons",
        "run_id",
        "action",
        "target_pid",
        "identity_state",
        "identity_reasons",
        "signal_requested",
        "signal_sent",
        "lock",
        "grace_seconds",
        "refusal_reason",
        "runs_root",
    }
)

#: Deterministic record key order (independent of caller dict order).
AUDIT_KEY_ORDER: tuple[str, ...] = (
    "schema",
    "ts",
    "component",
    "runs_root",
    "command",
    "decision",
    "outcome",
    "reasons",
    "run_id",
    "action",
    "target_pid",
    "identity_state",
    "identity_reasons",
    "signal_requested",
    "signal_sent",
    "lock",
    "grace_seconds",
    "refusal_reason",
)


def _audit_value(key: str, value: Any) -> Any:
    """Coerce one allow-listed value to a stable JSON-safe type."""
    if key in ("reasons", "identity_reasons"):
        if not isinstance(value, (list, tuple)):
            return [str(value)]
        return [str(item) for item in value]
    if key == "target_pid":
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None
    if key == "signal_sent":
        return value if isinstance(value, bool) else None
    if key == "grace_seconds":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value:
        return value
    return None


def sanitize_audit_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce ``record`` to the audit contract (pure, deterministic).

    * only :data:`_AUDIT_ALLOWED_KEYS` survive; anything else is dropped;
    * ``None`` values are dropped ("unknown" simply does not appear);
    * surviving values are coerced to a stable scalar/list shape;
    * output keys follow :data:`AUDIT_KEY_ORDER`.
    """
    kept: dict[str, Any] = {}
    for key in _AUDIT_ALLOWED_KEYS:
        if key not in record or record[key] is None:
            continue
        value = _audit_value(key, record[key])
        if value is not None:
            kept[key] = value
    ordered = {"schema": AUDIT_SCHEMA}
    for key in AUDIT_KEY_ORDER:
        if key in kept and key != "schema":
            ordered[key] = kept[key]
    return ordered


def _audit_lines(runs_root: Path) -> list[str]:
    """Existing trail lines, preserved byte-for-byte (malformed-safe)."""
    path = runs_root / AUDIT_FILENAME
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []  # absent trail: a valid empty state, not an error
    return [line for line in raw.splitlines() if line.strip()]


def append_audit_record(runs_root: Path | str, record: Mapping[str, Any]) -> str:
    """Append one sanitized record to the runs-root audit trail (V1.79).

    Deterministic, bounded, local, fail-safe:

    * the record is sanitized through :func:`sanitize_audit_record` before
      any byte touches disk (no secrets, no arbitrary caller data);
    * existing trail lines — including malformed ones — are preserved
      byte-for-byte; they are counted, trimmed and kept, never parsed or
      dropped, so a corrupted prior state cannot block a new record;
    * the trail is bounded to :data:`AUDIT_MAX_RECORDS` newest records, so
      it cannot grow without limit;
    * the write is atomic (tmp file + ``os.replace``) and the trail file is
      created with mode ``0o600`` so the local evidence never widens access.

    Returns ``"ok"`` on success or ``"unavailable"`` when the trail cannot
    be written.  The *control decision itself* never depends on the audit
    write; this is evidence, not gating.
    """
    try:
        root = Path(runs_root)
        kept = sanitize_audit_record(record)
        lines = (_audit_lines(root) + [json.dumps(kept, sort_keys=True)])
        lines = lines[-AUDIT_MAX_RECORDS:]
        final = root / AUDIT_FILENAME
        tmp = root / (AUDIT_FILENAME + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)
    except (OSError, ValueError, TypeError):
        return "unavailable"
    return "ok"
