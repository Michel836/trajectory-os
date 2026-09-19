"""M028 — durable LifeOS exchange ledger (atomic, strict, idempotent).

Layout::

    <root>/lifeos/ledger.json   bounded ordered exchange records

The ledger is provenance only — it is never the source of truth for canonical
goal state and it is never consulted to advance a goal. It exists so an
unchanged projection is exchanged at most once per adapter (idempotency) and
so an operator can audit exactly what left the canonical boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

from trajectory_os.lifeos import model
from trajectory_os.runs.store import atomic_write_json


def ledger_path(root: str | Path) -> Path:
    return Path(root) / "lifeos" / "ledger.json"


def load_ledger(root: str | Path) -> list[model.ExchangeRecord]:
    path = ledger_path(root)
    if not path.is_file():
        return []
    try:
        doc: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise model.LifeOSError(
            model.E_MALFORMED, f"{path}: {type(exc).__name__}") from exc
    if not isinstance(doc, dict):
        raise model.LifeOSError(model.E_MALFORMED, "ledger object expected")
    version = doc.get("schema_version", model.SCHEMA_VERSION)
    if version != model.SCHEMA_VERSION:
        raise model.LifeOSError(
            model.E_UNSUPPORTED_VERSION, f"ledger schema {version!r}")
    raw = doc.get("records") or []
    if not isinstance(raw, list):
        raise model.LifeOSError(model.E_MALFORMED, "records must be a list")
    return [model.ExchangeRecord.from_dict(item) for item in raw]


def save_ledger(root: str | Path,
                records: list[model.ExchangeRecord]) -> None:
    if len(records) > model.MAX_RECORDS:
        records = records[-model.MAX_RECORDS:]
    atomic_write_json(ledger_path(root), {
        "schema_version": model.SCHEMA_VERSION,
        "lifeos_version": model.LIFEOS_VERSION,
        "records": [record.to_dict() for record in records],
    })


def find_record(root: str | Path, adapter: str,
                exchange_id: str) -> model.ExchangeRecord | None:
    for record in load_ledger(root):
        if record.adapter == adapter and record.exchange_id == exchange_id:
            return record
    return None


def append_record(root: str | Path,
                  record: model.ExchangeRecord) -> model.ExchangeRecord:
    records = load_ledger(root)
    for index, existing in enumerate(records):
        if existing.adapter == record.adapter \
                and existing.exchange_id == record.exchange_id:
            records[index] = record
            save_ledger(root, records)
            return record
    records.append(record)
    save_ledger(root, records)
    return record


def reconstruct(root: str | Path) -> tuple[int, dict[str, int]]:
    """Strictly validate the ledger and return (count, status histogram)."""
    records = load_ledger(root)
    histogram: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (record.adapter, record.exchange_id)
        if key in seen:
            raise model.LifeOSError(
                model.E_MALFORMED,
                f"duplicate ledger entry {record.adapter}/{record.exchange_id}")
        seen.add(key)
        histogram[record.status] = histogram.get(record.status, 0) + 1
    return len(records), dict(sorted(histogram.items()))
