"""M028 — operator-facing LifeOS integration projection (read-only)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.lifeos import model, store


def status_document(root: str | Path) -> dict[str, Any]:
    root_str = str(root)
    try:
        records = store.load_ledger(root_str)
        count, histogram = store.reconstruct(root_str)
    except model.LifeOSError as exc:
        return {
            "status": "UNAVAILABLE",
            "error": exc.code,
            "goal_id": None,
            "count": 0,
            "records": [],
        }
    return {
        "status": "OK",
        "schema_version": model.SCHEMA_VERSION,
        "lifeos_version": model.LIFEOS_VERSION,
        "root": root_str,
        "count": count,
        "counts": histogram,
        "adapters": sorted(model.ADAPTER_KINDS),
        "records": [record.to_dict() for record in records],
    }


def render(document: Mapping[str, Any]) -> str:
    if document.get("status") != "OK":
        return ("lifeos     : unavailable "
                f"({document.get('error')})")
    counts = document.get("counts")
    if not isinstance(counts, Mapping):
        counts = {}
    lines = [
        f"lifeos     : records={document.get('count')} " + " ".join(
            f"{status}={counts.get(status, 0)}" for status in sorted(counts)),
        "adapters   : " + " ".join(
            str(kind) for kind in document.get("adapters", [])),
    ]
    records = document.get("records")
    if isinstance(records, list):
        for record in records[-16:]:
            if not isinstance(record, Mapping):
                continue
            lines.append(
                f"  - {record.get('created_at')} {record.get('adapter')} "
                f"{record.get('status')} exchange="
                f"{str(record.get('exchange_id'))[:12]} "
                f"outputs={len(record.get('outputs') or [])}"
                + (f" error={record.get('error')}"
                   if record.get("error") else ""))
    return "\n".join(lines)
