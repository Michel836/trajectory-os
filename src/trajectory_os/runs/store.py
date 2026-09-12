"""V1.89 — minimal durable FIFO queue with deterministic, crash-safe semantics.

Persistence rules:

* Filesystem-backed JSON state under the state root (default
  ``.trajectory-pi/orchestration``); no daemon, no remote API, no DB.
* Every write is atomic: write to a process-unique temp file, flush +
  ``fsync``, then ``os.replace`` over the target.
* Loaders are strict and fail closed: any malformed document raises
  ``MalformedStoreError`` with a stable code — it is never silently
  "repaired" or partially applied.
* Identity duplication (same job id already queued or active) is rejected;
  recovery/retry of a TERMINAL job is idempotent (allowed), not duplicated.
* The queue is bounded (``MAX_QUEUE_ENTRIES``) and FIFO ordered by a
  monotonically increasing sequence number.

The queue alone never starts or cancels anything: startup requires an
explicit, authorized transition (V1.88), and cancellation/recovery are
explicit operators (V1.90).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trajectory_os.runs import model

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

QUEUE_FILE = "queue.json"
ACTIVE_FILE = "active.json"
CLOSED_FILE = "closed.json"
WORKSPACES_DIR = "workspaces"


class MalformedStoreError(Exception):
    """Malformed persisted state: fail closed, no partial application."""

    def __init__(self, code: str, path: Path) -> None:
        super().__init__(f"{code}: {path}")
        self.code = code
        self.path = path


class DuplicateIdentityError(Exception):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"DUPLICATE_IDENTITY: {job_id}")
        self.code = model.ERR_DUPLICATE_IDENTITY
        self.job_id = job_id


class QueueFullError(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"QUEUE_FULL: limit {limit}")
        self.code = model.ERR_QUEUE_FULL
        self.limit = limit


class JobNotFoundError(Exception):
    """Raised when a requested queued job identity does not exist."""


class QueueEmptyError(Exception):
    def __init__(self) -> None:
        super().__init__(model.ERR_QUEUE_EMPTY)
        self.code = model.ERR_QUEUE_EMPTY


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Atomic JSON I/O
# ---------------------------------------------------------------------------


def atomic_write_json(path: Path, doc: dict[str, Any]) -> None:
    """Atomic durable write: temp file + fsync + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=f".tmp.{os.getpid()}", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def read_json_strict(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise MalformedStoreError("STORE_UNREADABLE", path) from None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path) from None
    if not isinstance(doc, dict):
        raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
    return doc


def _check_schema_version(doc: dict[str, Any], path: Path) -> None:
    version = doc.get("schema_version")
    if version != model.SCHEMA_VERSION:
        raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)


# ---------------------------------------------------------------------------
# Job validation
# ---------------------------------------------------------------------------


def validate_job(
    *,
    job_id: str,
    command: list[str],
    max_attempts: int = model.DEFAULT_MAX_ATTEMPTS,
) -> tuple[str, list[str], int]:
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise ValueError(f"INVALID_JOB: job_id {job_id!r}")
    if not isinstance(command, list) or not command:
        raise ValueError(f"INVALID_JOB: empty command for {job_id!r}")
    for part in command:
        if not isinstance(part, str) or not part.strip():
            raise ValueError(f"INVALID_JOB: invalid command part for {job_id!r}")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError("INVALID_JOB: max_attempts must be an integer")
    if max_attempts < 1 or max_attempts > model.MAX_ATTEMPTS_LIMIT:
        raise ValueError(f"INVALID_JOB: max_attempts {max_attempts} out of bounds")
    return job_id, list(command), max_attempts


# ---------------------------------------------------------------------------
# Queue document (V1.89)
# ---------------------------------------------------------------------------


@dataclass
class QueueEntry:
    seq: int
    job_id: str
    enqueued_at: str
    attempts: int = 0  # attempts already consumed (bounded by max_attempts)
    command: list[str] = field(default_factory=list)
    max_attempts: int = model.DEFAULT_MAX_ATTEMPTS
    query_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "seq": self.seq,
            "job_id": self.job_id,
            "enqueued_at": self.enqueued_at,
            "attempts": self.attempts,
            "command": list(self.command),
            "max_attempts": self.max_attempts,
        }
        if self.query_file is not None:
            doc["query_file"] = self.query_file
        return doc

    @classmethod
    def from_dict(cls, doc: Any, path: Path) -> QueueEntry:
        if not isinstance(doc, dict):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        seq = doc.get("seq")
        job_id = doc.get("job_id")
        enqueued_at = doc.get("enqueued_at")
        command = doc.get("command")
        if (
            isinstance(seq, bool)
            or not isinstance(seq, int)
            or seq < 0
            or not isinstance(job_id, str)
            or not isinstance(enqueued_at, str)
            or not isinstance(command, list)
            or not command
        ):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        for part in command:
            if not isinstance(part, str) or not part.strip():
                raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        attempts = doc.get("attempts", 0)
        max_attempts = doc.get("max_attempts", model.DEFAULT_MAX_ATTEMPTS)
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts < 0
            or isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
            or max_attempts > model.MAX_ATTEMPTS_LIMIT
        ):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        query_file = doc.get("query_file")
        if query_file is not None and not isinstance(query_file, str):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        return cls(
            seq=seq,
            job_id=job_id,
            enqueued_at=enqueued_at,
            attempts=attempts,
            command=list(command),
            max_attempts=max_attempts,
            query_file=query_file,
        )


@dataclass
class QueueDoc:
    seq: int
    entries: list[QueueEntry]

    @classmethod
    def load(cls, path: Path) -> QueueDoc:
        if not path.exists():
            return cls(seq=0, entries=[])
        doc = read_json_strict(path)
        _check_schema_version(doc, path)
        seq = doc.get("seq")
        entries_raw = doc.get("entries")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        if not isinstance(entries_raw, list):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        entries = [QueueEntry.from_dict(item, path) for item in entries_raw]
        seen: set[int] = set()
        for entry in entries:
            if entry.seq in seen:
                raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
            seen.add(entry.seq)
        entries.sort(key=lambda entry: entry.seq)
        if len(entries) > model.MAX_QUEUE_ENTRIES:
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        return cls(seq=seq, entries=entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "seq": self.seq,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def save(self, path: Path) -> None:
        atomic_write_json(path, self.to_dict())

    def active_ids(self) -> set[str]:
        return {entry.job_id for entry in self.entries}

    def head(self) -> QueueEntry | None:
        if not self.entries:
            return None
        return min(self.entries, key=lambda entry: entry.seq)

    def enqueue(
        self,
        *,
        job_id: str,
        command: list[str],
        max_attempts: int,
        query_file: str | None = None,
        reserved_ids: frozenset[str] | None = None,
        attempts: int = 0,
    ) -> QueueEntry:
        validate_job(job_id=job_id, command=command, max_attempts=max_attempts)
        reserved = self.active_ids() | (reserved_ids or frozenset())
        if job_id in reserved:
            raise DuplicateIdentityError(job_id)
        if len(self.entries) >= model.MAX_QUEUE_ENTRIES:
            raise QueueFullError(model.MAX_QUEUE_ENTRIES)
        entry = QueueEntry(
            seq=self.seq + 1,
            job_id=job_id,
            enqueued_at=utc_now_iso(),
            command=list(command),
            max_attempts=max_attempts,
            query_file=query_file,
            attempts=attempts,
        )
        self.entries.append(entry)
        self.seq = self.seq + 1
        self.entries.sort(key=lambda e: e.seq)
        return entry

    def remove(self, job_id: str | None = None, seq: int | None = None) -> QueueEntry:
        for index, entry in enumerate(self.entries):
            if (job_id is None or entry.job_id == job_id) and (
                seq is None or entry.seq == seq
            ):
                return self.entries.pop(index)
        raise QueueEmptyError()


# ---------------------------------------------------------------------------
# Active record document (V1.88 state, persisted so V1.90 can rebuild)
# ---------------------------------------------------------------------------


@dataclass
class ActiveRecord:
    job_id: str
    slot: str
    pgid: int
    pid: int
    token: str
    workspace: str
    command: list[str]
    seq: int
    max_attempts: int
    attempts: int  # attempts consumed so far (includes the current one)
    started_at: str
    log_stdout: str
    log_stderr: str
    query_file: str | None = None
    live_proven: bool = False  # set by the ownership prover at observation time

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "slot": self.slot,
            "pgid": self.pgid,
            "pid": self.pid,
            "token": self.token,
            "workspace": self.workspace,
            "command": list(self.command),
            "seq": self.seq,
            "max_attempts": self.max_attempts,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "log_stdout": self.log_stdout,
            "log_stderr": self.log_stderr,
            "query_file": self.query_file,
        }

    @classmethod
    def from_dict(cls, doc: Any, path: Path) -> ActiveRecord:
        if not isinstance(doc, dict):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        for key in ("job_id", "slot", "token", "workspace"):
            if not isinstance(doc.get(key), str):
                raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        for key in ("pgid", "pid", "seq", "max_attempts"):
            value = doc.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        command = doc.get("command")
        if not isinstance(command, list) or not command:
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        for part in command:
            if not isinstance(part, str):
                raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        started_at = doc.get("started_at")
        if not isinstance(started_at, str):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        query_file = doc.get("query_file")
        if query_file is not None and not isinstance(query_file, str):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        attempts = doc.get("attempts", 1)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        return cls(
            job_id=str(doc["job_id"]),
            slot=str(doc["slot"]),
            pgid=int(doc["pgid"]),
            pid=int(doc["pid"]),
            token=str(doc["token"]),
            workspace=str(doc["workspace"]),
            command=[str(part) for part in command],
            seq=int(doc["seq"]),
            max_attempts=int(doc["max_attempts"]),
            attempts=int(attempts),
            started_at=str(started_at),
            log_stdout=str(doc.get("log_stdout", "")),
            log_stderr=str(doc.get("log_stderr", "")),
            query_file=query_file,
        )


def load_active_records(path: Path) -> list[ActiveRecord]:
    if not path.exists():
        return []
    doc = read_json_strict(path)
    _check_schema_version(doc, path)
    records_raw = doc.get("records")
    if not isinstance(records_raw, list):
        raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
    return [ActiveRecord.from_dict(item, path) for item in records_raw]


def save_active_records(path: Path, records: list[ActiveRecord]) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": model.SCHEMA_VERSION,
            "records": [record.to_dict() for record in records],
        },
    )


# ---------------------------------------------------------------------------
# Closed (terminal/observed) records
# ---------------------------------------------------------------------------


@dataclass
class ClosedRecord:
    job_id: str
    seq: int
    observed_at: str
    terminal: str
    attempts: int
    exit_code: int | None = None
    signal: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "seq": self.seq,
            "observed_at": self.observed_at,
            "terminal": self.terminal,
            "attempts": self.attempts,
            "exit_code": self.exit_code,
            "signal": self.signal,
        }

    @classmethod
    def from_dict(cls, doc: Any, path: Path) -> ClosedRecord:
        if not isinstance(doc, dict):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        job_id = doc.get("job_id")
        seq = doc.get("seq")
        observed_at = doc.get("observed_at")
        terminal = doc.get("terminal")
        attempts = doc.get("attempts")
        exit_code = doc.get("exit_code")
        signal = doc.get("signal")
        if (
            not isinstance(job_id, str)
            or isinstance(seq, bool)
            or not isinstance(seq, int)
            or not isinstance(observed_at, str)
            or not isinstance(terminal, str)
            or isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts < 0
        ):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        if (
            exit_code is not None
            and (isinstance(exit_code, bool) or not isinstance(exit_code, int))
        ):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        if signal is not None and (isinstance(signal, bool) or not isinstance(signal, int)):
            raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
        return cls(
            job_id=job_id,
            seq=seq,
            observed_at=observed_at,
            terminal=terminal,
            attempts=attempts,
            exit_code=exit_code,
            signal=signal,
        )


def load_closed_records(path: Path) -> list[ClosedRecord]:
    if not path.exists():
        return []
    doc = read_json_strict(path)
    _check_schema_version(doc, path)
    records_raw = doc.get("records")
    if not isinstance(records_raw, list):
        raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
    records = [ClosedRecord.from_dict(item, path) for item in records_raw]
    if len(records) > model.MAX_CLOSED_RECORDS:
        raise MalformedStoreError(model.ERR_QUEUE_MALFORMED, path)
    return records


def save_closed_records(path: Path, records: list[ClosedRecord]) -> None:
    bounded = records[-model.MAX_CLOSED_RECORDS:]
    atomic_write_json(
        path,
        {
            "schema_version": model.SCHEMA_VERSION,
            "records": [record.to_dict() for record in bounded],
        },
    )


def append_closed_record(path: Path, record: ClosedRecord) -> None:
    records = load_closed_records(path)
    records.append(record)
    save_closed_records(path, records)


def slot_name(seq: int) -> str:
    return f"s{seq}"


def state_paths(state_root: Path) -> dict[str, Path]:
    base = state_root / "orchestration"
    return {
        "base": base,
        "queue": base / QUEUE_FILE,
        "active": base / ACTIVE_FILE,
        "closed": base / CLOSED_FILE,
        "workspaces": base / WORKSPACES_DIR,
    }
