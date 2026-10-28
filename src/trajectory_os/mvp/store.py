"""MVP — durable, atomic persistence for the personal portfolio.

The portfolio document and the immutable outcome ledger are the only durable
state. All writes are atomic (temp file + ``os.replace`` via the shared
:mod:`trajectory_os.intelligence.model` helpers) and append-only where history
matters. Nothing here performs a Git write or mutates any other canonical
store.

Phase 2 adds two safety properties on top of the existing atomic write:

* a cross-process advisory lock (``fcntl.flock``) so the CLI and the local
  dashboard cannot interleave a read-modify-write cycle;
* bounded, versioned backups under ``backups/`` with explicit restore.

Reads never take the write lock. The lock is reentrant inside one process so a
mutation may call another mutation (or ``save_portfolio``) without deadlock.
"""

from __future__ import annotations

import fcntl
import os
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import model

#: Default location of the user's portfolio document relative to a data root.
PORTFOLIO_NAME = "portfolio.json"

#: Outcome ledger filename (append-only JSONL).
LEDGER_NAME = "outcomes.jsonl"

#: Plan snapshot directory.
PLANS_DIR = "plans"

#: Versioned portfolio backups.
BACKUPS_DIR = "backups"

#: Advisory lock file (never contains business data).
LOCK_NAME = ".portfolio.lock"

#: Default number of backups retained.
DEFAULT_BACKUP_KEEP = 20

_SAFE_REASON = re.compile(r"[^a-z0-9._-]+")


class _RootLock:
    """Per-root reentrant process lock plus one cross-process file lock."""

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.handle: IO[str] | None = None


_LOCKS: dict[str, _RootLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_state(root: str | Path) -> _RootLock:
    key = str(Path(root).resolve())
    with _LOCKS_GUARD:
        state = _LOCKS.get(key)
        if state is None:
            state = _RootLock()
            _LOCKS[key] = state
        return state


@contextmanager
def portfolio_lock(root: str | Path) -> Iterator[None]:
    """Serialize portfolio writers within and across processes.

    The same process may re-enter the lock (nested mutations); a second
    process blocks until the first releases it. The lock file contains no
    business data and is created next to ``portfolio.json``.
    """
    state = _lock_state(root)
    with state.rlock:
        if state.depth == 0:
            lock_path = Path(root) / LOCK_NAME
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                handle.close()
                raise
            state.handle = handle
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0 and state.handle is not None:
                try:
                    fcntl.flock(state.handle.fileno(), fcntl.LOCK_UN)
                finally:
                    state.handle.close()
                    state.handle = None


def default_portfolio_path(root: str | Path) -> Path:
    return Path(root) / PORTFOLIO_NAME


def outcome_ledger_path(root: str | Path) -> Path:
    return Path(root) / LEDGER_NAME


def plans_dir(root: str | Path) -> Path:
    return Path(root) / PLANS_DIR


def backups_dir(root: str | Path) -> Path:
    return Path(root) / BACKUPS_DIR


def save_portfolio(root: str | Path, portfolio: model.Portfolio) -> str:
    """Atomically persist a validated portfolio; return the written path."""
    portfolio.validate()
    path = default_portfolio_path(root)
    with portfolio_lock(root):
        intel_model.write_json(path, portfolio.to_dict())
    return str(path)


def load_portfolio(root: str | Path) -> model.Portfolio | None:
    """Load and validate the portfolio; ``None`` when absent/corrupt."""
    document = intel_model.read_json(default_portfolio_path(root))
    if document is None:
        return None
    try:
        return model.Portfolio.from_dict(document)
    except model.MvpError:
        return None


def append_outcome(root: str | Path, record: model.OutcomeRecord) -> None:
    """Append one immutable outcome revision to the ledger."""
    record.validate()
    intel_model.append_jsonl(outcome_ledger_path(root), record.to_dict())


def read_outcomes(root: str | Path) -> tuple[model.OutcomeRecord, ...]:
    """Read every outcome record, oldest first, skipping malformed lines."""
    records = intel_model.read_jsonl(outcome_ledger_path(root))
    outcomes: list[model.OutcomeRecord] = []
    for document in records:
        try:
            outcomes.append(model.OutcomeRecord.from_dict(document))
        except model.MvpError:
            continue
    return tuple(outcomes)


def latest_outcome_by_task(root: str | Path) -> dict[str, model.OutcomeRecord]:
    """Return the most recent outcome per task (deterministic)."""
    by_task: dict[str, model.OutcomeRecord] = {}
    for record in read_outcomes(root):
        by_task[record.task_id] = record
    return by_task


def _safe_reason(reason: str) -> str:
    value = _SAFE_REASON.sub("-", reason.strip().lower()).strip("-")
    return value[:48] or "manual"


def _prune_backups(root: str | Path, keep: int) -> None:
    if keep <= 0:
        keep = 1
    paths = sorted(backups_dir(root).glob("portfolio-*.json"))
    for stale in paths[:-keep]:
        try:
            stale.unlink()
        except OSError:
            continue


def backup_portfolio(
    root: str | Path,
    *,
    reason: str = "manual",
    keep: int = DEFAULT_BACKUP_KEEP,
) -> dict[str, Any]:
    """Copy the current portfolio into a versioned backup.

    The copy is byte-for-byte and atomically replaced, so a corrupt portfolio
    can still be preserved for diagnosis. The backup directory is bounded by
    ``keep`` (oldest files are pruned first).
    """
    source = default_portfolio_path(root)
    if not source.is_file():
        raise model.MvpError(model.E_MALFORMED, "no portfolio to back up")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    name = f"portfolio-{stamp}-{_safe_reason(reason)}.json"
    with portfolio_lock(root):
        data = source.read_bytes()
        target = backups_dir(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _prune_backups(root, keep)
    return {
        "name": name,
        "path": str(target),
        "reason": _safe_reason(reason),
        "size": len(data),
        "created_at": stamp,
    }


def list_backups(root: str | Path) -> tuple[dict[str, Any], ...]:
    """List backups newest first (bounded by what is on disk)."""
    folder = backups_dir(root)
    if not folder.is_dir():
        return ()
    items: list[dict[str, Any]] = []
    for path in sorted(folder.glob("portfolio-*.json"), reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({
            "name": path.name,
            "path": str(path),
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime, UTC).isoformat(),
        })
    return tuple(items)


def latest_backup(root: str | Path) -> dict[str, Any] | None:
    backups = list_backups(root)
    return backups[0] if backups else None


def restore_portfolio(
    root: str | Path,
    *,
    name: str = "",
    keep_current: bool = True,
) -> dict[str, Any]:
    """Restore a validated backup by name, or the latest one.

    The current portfolio is backed up first (``pre-restore``) unless the
    caller explicitly opts out, so a restore is itself reversible.
    """
    folder = backups_dir(root)
    if name:
        if Path(name).name != name:
            raise model.MvpError(model.E_MALFORMED, "invalid backup name")
        source = folder / name
        if not source.is_file():
            raise model.MvpError(model.E_UNKNOWN_ID, name)
    else:
        latest = latest_backup(root)
        if latest is None:
            raise model.MvpError(model.E_MALFORMED, "no backup available")
        source = Path(str(latest["path"]))
    document = intel_model.read_json(source)
    if document is None:
        raise model.MvpError(model.E_MALFORMED,
                             f"backup {source.name} is unreadable")
    portfolio = model.Portfolio.from_dict(document)
    with portfolio_lock(root):
        pre_restore = ""
        if keep_current and default_portfolio_path(root).is_file():
            pre_restore = backup_portfolio(
                root, reason="pre-restore")["name"]
        save_portfolio(root, portfolio)
    return {
        "restored": source.name,
        "portfolio": portfolio.name,
        "pre_restore_backup": pre_restore,
    }


def maybe_daily_backup(
    root: str | Path,
    *,
    keep: int = DEFAULT_BACKUP_KEEP,
) -> dict[str, Any] | None:
    """Create at most one ``daily`` backup per UTC day."""
    if not default_portfolio_path(root).is_file():
        return None
    today = datetime.now(UTC).strftime("%Y%m%d")
    for item in list_backups(root):
        if str(item["name"]).startswith(f"portfolio-{today}"):
            return None
    return backup_portfolio(root, reason="daily", keep=keep)


def save_plan(root: str | Path, name: str,
              payload: Mapping[str, Any]) -> str:
    """Persist a plan snapshot (today/week) under ``plans/<name>.json``."""
    path = plans_dir(root) / f"{name}.json"
    intel_model.write_json(path, payload)
    return str(path)


def load_plan(root: str | Path, name: str) -> dict[str, Any] | None:
    return intel_model.read_json(plans_dir(root) / f"{name}.json")


def list_projects(root: str | Path) -> Sequence[model.Project]:
    portfolio = load_portfolio(root)
    return portfolio.projects if portfolio is not None else ()


__all__ = [
    "BACKUPS_DIR",
    "DEFAULT_BACKUP_KEEP",
    "LEDGER_NAME",
    "LOCK_NAME",
    "PLANS_DIR",
    "PORTFOLIO_NAME",
    "append_outcome",
    "backup_portfolio",
    "backups_dir",
    "default_portfolio_path",
    "latest_backup",
    "latest_outcome_by_task",
    "list_backups",
    "load_plan",
    "load_portfolio",
    "list_projects",
    "maybe_daily_backup",
    "outcome_ledger_path",
    "plans_dir",
    "portfolio_lock",
    "read_outcomes",
    "restore_portfolio",
    "save_plan",
    "save_portfolio",
]
