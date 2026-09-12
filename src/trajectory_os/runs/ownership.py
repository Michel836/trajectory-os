"""V1.88 — controlled concurrency with explicit, multi-factor ownership.

Ownership of a started job is PROVEN, never assumed:

* the active record must name the job, the pid, the expected workspace
  (recorded at launch) and the process-group id;
* the process must be alive and not a zombie;
* its cwd must match the recorded workspace (PID reuse with a different
  program is rejected);
* its environment must carry the launch token (a process-unique secret set
  at launch — PID reuse by an unrelated process lacks it);
* its process-group id must match the recorded pgid.

Only a record that passes every check is "proven".  Signals (cancellation)
are delivered to the recorded process GROUP only, and only after proof —
so one job's cancellation can never signal another job's group or
unrelated processes that happen to share the pid.

Launches run each job in its own session/process group with a per-run
workspace and per-run final-query file, so concurrent jobs can never share
mutable ``PI_FINAL_QUERY`` state or clobber each other's artifacts.

Policy (consistent with V1.74 signal-safety): cancellation is graceful —
SIGTERM to the verified group, bounded grace period, then observation;
no SIGKILL, no unbounded waiting.
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.runs import model, store

TOKEN_ENV_VAR = "TRAJECTORY_RUN_TOKEN"
JOB_ID_ENV_VAR = "TRAJECTORY_JOB_ID"
SLOT_ENV_VAR = "TRAJECTORY_RUN_SLOT"
FINAL_QUERY_ENV_VAR = "TRAJECTORY_FINAL_QUERY_FILE"


@dataclass(frozen=True)
class OwnershipProof:
    proven: bool
    reasons: tuple[str, ...]
    pid: int


def _pgid_of(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except (OSError, ValueError):
        return None
    # The comm field may contain spaces/parens: take the part after the last ')'.
    # Remaining tokens: state(3) ppid(4) pgrp(5) session(6) ...  => pgrp is index 2.
    tail = stat.rsplit(")", 1)[-1].split()
    if len(tail) >= 3:
        try:
            return int(tail[2])
        except ValueError:
            return None
    return None


def _environ_contains(pid: int, needle: str) -> bool:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    for entry in raw.split(b"\x00"):
        try:
            if entry.decode("utf-8", errors="strict") == needle:
                return True
        except UnicodeDecodeError:
            continue
    return False


def _self_pids() -> frozenset[int]:
    pids = {os.getpid(), os.getppid()}
    return frozenset(pids)


def prove_active_record(record: store.ActiveRecord) -> OwnershipProof:
    """Multi-factor ownership proof for one active record (read-only)."""
    if record.pid in _self_pids():
        return OwnershipProof(False, ("SELF_PROCESS",), record.pid)
    if record.pid <= 0 or record.pgid <= 0:
        return OwnershipProof(False, ("OWNERSHIP_MALFORMED",), record.pid)

    try:
        import trajectory_os.runtime_control as rc

        live = rc.is_pid_alive(record.pid) and not rc.process_zombie(record.pid)
    except Exception:  # pragma: no cover - defensive
        live = False
    if not live:
        return OwnershipProof(False, ("NOT_LIVE",), record.pid)

    try:
        import trajectory_os.runtime_control as rc

        snapshot = rc.snapshot_process(record.pid)
    except Exception:  # pragma: no cover - defensive
        snapshot = None
    if snapshot is None or snapshot.cwd is None:
        return OwnershipProof(False, ("OWNERSHIP_UNPROVEN",), record.pid)
    cwd_match = False
    try:
        cwd_match = Path(snapshot.cwd).resolve() == Path(record.workspace).resolve()
    except (OSError, RuntimeError):
        cwd_match = False
    if not cwd_match:
        return OwnershipProof(False, ("CWD_MISMATCH",), record.pid)

    if not _environ_contains(record.pid, f"{TOKEN_ENV_VAR}={record.token}"):
        return OwnershipProof(False, ("TOKEN_MISMATCH",), record.pid)

    observed_pgid = _pgid_of(record.pid)
    if observed_pgid is None or observed_pgid != record.pgid:
        return OwnershipProof(False, ("PGID_MISMATCH",), record.pid)

    return OwnershipProof(True, ("PROVEN",), record.pid)


# ---------------------------------------------------------------------------
# Launch (isolated process group, per-run artifacts, bounded env injection)
# ---------------------------------------------------------------------------


def launch_job(
    *,
    job_id: str,
    slot: str,
    command: list[str],
    workspace: Path,
    log_stdout: Path,
    log_stderr: Path,
    final_query_file: Path,
    query_content: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, int, str]:
    """Launch a job in its own process group.  Returns ``(pid, pgid, token)``.

    Every mutable input used by the agent is per-run (workspace, final
    query file), so concurrent jobs cannot share ``PI_FINAL_QUERY`` state or
    interleave artifacts.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    final_query_file.parent.mkdir(parents=True, exist_ok=True)
    if query_content is not None:
        final_query_file.write_text(query_content, encoding="utf-8")
    token = secrets.token_hex(16)
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    env[TOKEN_ENV_VAR] = token
    env[JOB_ID_ENV_VAR] = job_id
    env[SLOT_ENV_VAR] = slot
    env[FINAL_QUERY_ENV_VAR] = str(final_query_file)
    log_stdout.parent.mkdir(parents=True, exist_ok=True)
    log_stderr.parent.mkdir(parents=True, exist_ok=True)
    log_stdout.touch(exist_ok=True)
    log_stderr.touch(exist_ok=True)
    with open(log_stdout, "ab", buffering=0) as stdout, open(
        log_stderr, "ab", buffering=0
    ) as stderr:
        process = subprocess.Popen(
            command,
            cwd=str(workspace),
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    pid = process.pid
    # start_new_session(): the child leads its own group == session (pgid == pid).
    return pid, pid, token


# ---------------------------------------------------------------------------
# Graceful group termination (bounded; never SIGKILL, never unbounded loop)
# ---------------------------------------------------------------------------


def _group_has_live_member(pgid: int) -> bool:
    """True iff any non-zombie process belongs to the group (fail closed)."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return False
    for name in names:
        if not name.isdigit():
            continue
        try:
            stat = Path(f"/proc/{name}/stat").read_text(encoding="ascii", errors="replace")
            tail = stat.rsplit(")", 1)[-1].split()
            if len(tail) < 3:
                continue
            if int(tail[2]) == pgid and tail[0] != "Z":
                return True
        except (OSError, ValueError):
            continue
    return False


def group_alive(pgid: int) -> bool:
    """True iff the group has a live (non-zombie) member.

    ``killpg(pgid, 0)`` succeeds against zombie members, so a /proc scan is
    required to report honest liveness.
    """
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return _group_has_live_member(pgid)


def graceful_terminate_group(pgid: int, grace_seconds: float = 3.0) -> dict[str, Any]:
    """SIGTERM the VERIFIED process group; bounded grace; then observe.

    Outcome values: ``cancelled`` (dead within the grace window),
    ``cancel_pending`` (still alive after the grace window — explicitly
    reported, never escalated), ``group_gone`` (no live group observed).
    """
    grace_seconds = max(0.0, min(30.0, float(grace_seconds)))
    sent = False
    try:
        os.killpg(pgid, signal.SIGTERM)
        sent = True
    except (ProcessLookupError, PermissionError):
        return {"outcome": "cancelled" if sent else "group_gone", "sigterm_sent": sent}
    except OSError:
        return {"outcome": "group_gone", "sigterm_sent": sent}
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        _try_reap_child(pgid)
        if not group_alive(pgid):
            return {"outcome": "cancelled", "sigterm_sent": True}
        time.sleep(0.05)
    _try_reap_child(pgid)
    if not group_alive(pgid):
        return {"outcome": "cancelled", "sigterm_sent": True}
    return {"outcome": model.TERMINAL_CANCEL_PENDING, "sigterm_sent": True}


def _try_reap_child(pgid: int) -> None:
    """Reap a directly-parented child (non-blocking; best effort).

    Only valid when the group leader is our own child; never blocks and
    never escalates. Multi-threaded contention is tolerated.
    """
    try:
        os.waitpid(pgid, os.WNOHANG)
    except OSError:
        # Not our child, already reaped, or resource contention: ignore.
        return
