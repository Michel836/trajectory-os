"""M026 — durable, restart-safe, bounded, deduplicated event store.

Layout (all under ``<root>/events/``)::

    <goal_id>/events.jsonl       bounded, deduplicated event projection
    <goal_id>/projection.json    strict projection header (schema 1)

Rules:

* every write is atomic (temp file + fsync + rename);
* the projection is a pure function of the canonical persisted state: the
  same state always yields the same bounded, deduplicated set of event ids;
* every read is strict — unsupported schema, malformed records, identity
  mismatches and projection/log divergence fail closed (data is never
  guessed or repaired);
* the event log is the *projection*, never a second source of truth: it can
  always be reconstructed from the canonical graph/mission/scheduler/replan/
  artifact/proof stores.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from trajectory_os.events import identity as event_identity
from trajectory_os.events import model
from trajectory_os.runs.store import atomic_write_json


def event_paths(root: str | Path, goal_id: str) -> dict[str, Path]:
    base = Path(root) / "events" / goal_id
    return {
        "root": base,
        "events": base / "events.jsonl",
        "projection": base / "projection.json",
    }


def sort_key(record: model.EventRecord) -> tuple[str, str, str, str]:
    """Deterministic ordering key (occurred_at, category, kind, event_id)."""
    return (
        record.occurred_at or "",
        record.category,
        record.kind,
        record.event_id,
    )


def dedupe_sorted(records: Iterable[model.EventRecord],
                  ) -> list[model.EventRecord]:
    """Deterministically dedupe by ``event_id`` and order the projection."""
    unique: dict[str, model.EventRecord] = {}
    for record in records:
        unique.setdefault(record.event_id, record)
    ordered = sorted(unique.values(), key=sort_key)
    if len(ordered) > model.MAX_EVENTS:
        ordered = ordered[-model.MAX_EVENTS:]
    return ordered


def projection_id(goal_id: str,
                  records: Sequence[model.EventRecord]) -> str:
    """Deterministic identity of one bounded event projection."""
    return event_identity.projection_id({
        "schema_version": model.SCHEMA_VERSION,
        "goal_id": goal_id,
        "event_ids": [record.event_id for record in records],
    })


def _write_events_atomic(path: Path,
                         records: Sequence[model.EventRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=f".tmp.{os.getpid()}",
        dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                line = json.dumps(record.to_dict(), sort_keys=True,
                                  separators=(",", ":"))
                handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def load_events(root: str | Path, goal_id: str) -> list[model.EventRecord]:
    """Strictly load the persisted event projection (fail closed)."""
    path = event_paths(root, goal_id)["events"]
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.EventError(
            model.E_MALFORMED, f"{path}: {type(exc).__name__}") from exc
    records: list[model.EventRecord] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.EventError(
                model.E_MALFORMED, f"{path}[{index}]") from exc
        records.append(model.EventRecord.from_dict(doc))
    return records


def load_projection(root: str | Path,
                    goal_id: str) -> dict[str, object] | None:
    path = event_paths(root, goal_id)["projection"]
    if not path.is_file():
        return None
    try:
        doc: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise model.EventError(
            model.E_MALFORMED, f"{path}: {type(exc).__name__}") from exc
    if not isinstance(doc, dict):
        raise model.EventError(model.E_MALFORMED, "projection object expected")
    return doc


def _projection_document(goal_id: str,
                         records: Sequence[model.EventRecord],
                         *, updated_at: str) -> dict[str, object]:
    return {
        "schema_version": model.SCHEMA_VERSION,
        "events_version": model.EVENTS_VERSION,
        "goal_id": goal_id,
        "count": len(records),
        "event_ids": [record.event_id for record in records],
        "last_event_id": records[-1].event_id if records else None,
        "projection_id": projection_id(goal_id, records),
        "updated_at": updated_at,
    }


def save_projection(root: str | Path, goal_id: str,
                    records: Sequence[model.EventRecord],
                    *, updated_at: str) -> dict[str, object]:
    """Atomically persist the bounded, deduplicated projection."""
    paths = event_paths(root, goal_id)
    document = _projection_document(goal_id, records, updated_at=updated_at)
    _write_events_atomic(paths["events"], records)
    atomic_write_json(paths["projection"], document)
    return document


def append_events(root: str | Path, goal_id: str,
                  records: Iterable[model.EventRecord],
                  *, updated_at: str) -> list[model.EventRecord]:
    """Incrementally append deduplicated events and persist the projection.

    Existing event ids are idempotent no-ops. The result stays bounded: the
    newest :data:`model.MAX_EVENTS` events are retained. Returns the records
    that were genuinely new.
    """
    existing = load_events(root, goal_id)
    known = {record.event_id for record in existing}
    added: list[model.EventRecord] = []
    for record in records:
        if record.event_id in known:
            continue
        known.add(record.event_id)
        added.append(record)
    if not added:
        if load_projection(root, goal_id) is None:
            save_projection(root, goal_id, dedupe_sorted(existing),
                            updated_at=updated_at)
        return []
    combined = dedupe_sorted([*existing, *added])
    save_projection(root, goal_id, combined, updated_at=updated_at)
    retained = {record.event_id for record in combined}
    return [record for record in added if record.event_id in retained]


def reconstruct(root: str | Path, goal_id: str,
                ) -> tuple[dict[str, object], tuple[model.EventRecord, ...]]:
    """Strictly reconstruct the persisted projection (fail closed).

    Validates that the projection header, the append-only log and the
    deterministic projection identity all agree. Any divergence is a
    corruption and raises rather than being silently repaired.
    """
    paths = event_paths(root, goal_id)
    document = load_projection(root, goal_id)
    if document is None:
        raise model.EventError(
            model.E_MALFORMED, f"{paths['projection']}: missing projection")
    if document.get("schema_version") != model.SCHEMA_VERSION:
        raise model.EventError(
            model.E_UNSUPPORTED_VERSION, "projection schema")
    if document.get("goal_id") != goal_id:
        raise model.EventError(
            model.E_IDENTITY_MISMATCH, "projection goal_id")
    records = load_events(root, goal_id)
    count = document.get("count")
    if not isinstance(count, int) or isinstance(count, bool) \
            or count != len(records):
        raise model.EventError(
            model.E_IDENTITY_MISMATCH, "projection count")
    ids = document.get("event_ids")
    if not isinstance(ids, list) or any(
            not isinstance(item, str) for item in ids):
        raise model.EventError(model.E_MALFORMED, "projection event_ids")
    if list(ids) != [record.event_id for record in records]:
        raise model.EventError(
            model.E_IDENTITY_MISMATCH, "projection event_ids")
    if document.get("projection_id") != projection_id(goal_id, records):
        raise model.EventError(
            model.E_IDENTITY_MISMATCH, "projection_id")
    return document, tuple(records)


def changed(previous: Sequence[model.EventRecord],
            current: Sequence[model.EventRecord],
            ) -> tuple[tuple[model.EventRecord, ...],
                       tuple[model.EventRecord, ...]]:
    """Return ``(added, removed)`` between two ordered projections."""
    before = {record.event_id: record for record in previous}
    after = {record.event_id: record for record in current}
    added = tuple(record for event_id, record in after.items()
                  if event_id not in before)
    removed = tuple(record for event_id, record in before.items()
                    if event_id not in after)
    return added, removed


def projection_summary(document: Mapping[str, object]) -> dict[str, object]:
    """Bounded machine-readable header of a projection document."""
    return {
        "schema_version": document.get("schema_version"),
        "events_version": document.get("events_version"),
        "goal_id": document.get("goal_id"),
        "count": document.get("count"),
        "projection_id": document.get("projection_id"),
        "last_event_id": document.get("last_event_id"),
        "updated_at": document.get("updated_at"),
    }
