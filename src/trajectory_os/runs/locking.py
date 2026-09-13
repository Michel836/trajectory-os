"""V1.93 — Inter-process state-lock discipline (filesystem-backed, bounded).

All queue and active-record mutations are protected by an exclusive
advisory lock (``fcntl.flock``) held on a dedicated lock file inside the
orchestration state base.  The discipline:

* every state-mutating operation (enqueue, dequeue, start, cancel, reap,
  discharge) runs inside the same exclusive lock, serializing concurrent
  tool instances (including concurrent ``runs start`` invocations);

* the lock is **bounded**: acquisition polls with a deterministic, bounded
  timeout (default 5 s, step 50 ms) and fails closed with
  ``LockAcquireError(ERR_LOCK_TIMEOUT)`` — it never blocks forever;

* the lock is **crash-safe by construction**: it is a kernel advisory lock
  on an open file descriptor, so it is released automatically when the
  holding process dies (no PID-only ownership file to go stale); any
  recorded holder identity in the lock file is best-effort metadata only
  and is never the source of truth — a lock acquired while "another
  holder" is recorded is trusted exactly as one acquired unheld;

* acquisition is atomic with respect to the kernel (``LOCK_EX|LOCK_NB``
  inside a bounded polling loop), and every release closes the descriptor
  (dropping the lock) — no separate unlock-and-unlink race.

The lock file itself is inert (its contents are never read for authority);
its *existence path* is the shared coordination point.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from trajectory_os.runs import model

LOCK_FILE_NAME = "state.lock"
DEFAULT_TIMEOUT_SECONDS = 5.0
POLL_STEP_SECONDS = 0.05
MAX_POLL_ITERATIONS = 10_000  # hard bound even if timeout math is misused


class LockAcquireError(Exception):
    """Raised when the state lock cannot be acquired within the bound."""

    def __init__(self, code: str = model.ERR_LOCK_TIMEOUT, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class _LockHandle:
    fd: int
    path: Path


class StateLock:
    """Exclusive, bounded, kernel-enforced state lock (context manager).

    Usage::

        with StateLock(state_base, timeout=5.0) as lock:
            ... mutate queue / active files ...

    Exiting the context (normally or via exception) releases the lock.
    """

    def __init__(
        self,
        state_base: Path,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        poll_step_seconds: float = POLL_STEP_SECONDS,
        max_iterations: int = MAX_POLL_ITERATIONS,
    ) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be >= 0")
        self.state_base = Path(state_base)
        self.timeout_seconds = float(timeout_seconds)
        self.poll_step_seconds = max(float(poll_step_seconds), 0.001)
        self.max_iterations = int(max_iterations)
        self.path = self.state_base / LOCK_FILE_NAME
        self._handle: _LockHandle | None = None

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> StateLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        self.release()
        return False

    # -- acquisition (bounded, fail closed) ---------------------------------
    def acquire(self) -> None:
        if self._handle is not None:
            return  # already held (idempotent)
        try:
            self.state_base.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise LockAcquireError(
                model.ERR_LOCK_TIMEOUT, f"cannot open lock file {self.path}: {exc}"
            ) from exc
        # Best-effort holder metadata (identity only, never authoritative):
        # written AFTER winning the lock, so only the current holder's data
        # is present; a stale document from a dead process is ignored.
        deadline = time.monotonic() + self.timeout_seconds
        acquired = False
        iterations = 0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, PermissionError, OSError):
                iterations += 1
                if iterations >= self.max_iterations or time.monotonic() >= deadline:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                    raise LockAcquireError(
                        model.ERR_LOCK_TIMEOUT,
                        f"state lock held by another process: {self.path} "
                        f"(bounded wait {self.timeout_seconds:.1f}s)",
                    ) from None
                time.sleep(self.poll_step_seconds)
        assert acquired
        try:
            self._write_holder_metadata(fd)
        except LockAcquireError:
            # Fail closed: an acquisition whose own bookkeeping write failed
            # is not a trusted acquisition.  Close the descriptor (drops
            # the kernel lock) before surfacing the error.
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        self._handle = _LockHandle(fd=fd, path=self.path)

    def release(self) -> None:
        if self._handle is None:
            return
        fd = self._handle.fd
        self._handle = None
        with contextlib.suppress(OSError):
            os.close(fd)  # closing the descriptor drops the flock

    # -- metadata (best-effort identity, authoritative-irrelevant) ----------
    def _write_holder_metadata(self, fd: int) -> None:
        """Write holder identity metadata after winning the lock.

        The content remains identity-only (the kernel ``flock`` is the
        authority), but the WRITE itself must be durable and complete:

        * one ``fsync`` after the full write (durable metadata; the earlier
          pre-write flush was redundant and is gone); ``os.write`` is
          looped because it may short-write;
        * any write/fsync failure raises ``LockAcquireError`` — the state
          mutation discipline is fail-closed, so an acquisition that cannot
          even record its own metadata is not treated as a safe
          acquisition (callers abort instead of mutating state).
        """
        doc = {
            "holder_pid": os.getpid(),
            "acquired_monotonic": time.monotonic(),
            "note": "identity metadata only; authority is the kernel lock",
        }
        data = (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except OSError as exc:
            raise LockAcquireError(
                model.ERR_LOCK_MALFORMED,
                f"holder metadata write failed for {self.path}: {exc}",
            ) from exc

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"StateLock(path={self.path!s}, held={self._handle is not None})"


def lock_path(state_base: Path) -> Path:
    return Path(state_base) / LOCK_FILE_NAME


def inspect_lock(state_base: Path) -> dict[str, object]:
    """Read-only inspection: whether the lock file exists + holder metadata.

    The presence of stale metadata NEVER implies exclusivity — only the
    kernel lock does.  This helper is diagnostic (for humans / tests).
    """
    path = lock_path(state_base)
    result: dict[str, object] = {
        "lock_path": str(path),
        "lock_file_present": path.exists(),
        "holder_pid": None,
        "stale_metadata_note": (
            "stale metadata may persist after holder death; the kernel lock "
            "is released on process exit, so stale metadata never blocks or "
            "authenticates"
        ),
    }
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("holder_pid"), int):
                result["holder_pid"] = doc["holder_pid"]
        except (ValueError, OSError):
            pass
    return result


def try_lock(state_base: Path, *, timeout_seconds: float = 0.5) -> bool:
    """Attempt a bounded acquisition; returns True iff the lock was won.

    Convenience for diagnostics (used by tests to prove exclusivity
    semantics without raising).
    """
    lock = StateLock(state_base, timeout_seconds=timeout_seconds)
    try:
        lock.acquire()
    except LockAcquireError:
        return False
    try:
        return True
    finally:
        lock.release()


def state_lock(
    state_base: Path,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> StateLock:
    """Factory used by the orchestration mutation paths."""
    return StateLock(state_base, timeout_seconds=timeout_seconds)
