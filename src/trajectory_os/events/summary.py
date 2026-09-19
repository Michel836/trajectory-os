"""M026 — operator-facing event/notification projection rendering.

Derived only from the canonical event projection: counts by category/severity
and the ordered events themselves. Read-only; nothing here mutates state or
invents an event.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from trajectory_os.events import engine, model
from trajectory_os.events import store as event_store


def _counts(records: Iterable[model.EventRecord],
            field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        key = getattr(record, field)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def document(root: str | Path, goal_id: str, *,
             refresh: bool = False,
             ) -> dict[str, Any]:
    """Machine-readable event projection document (read-only by default).

    When ``refresh`` is true the bounded, deduplicated projection is durably
    re-derived and persisted; otherwise the persisted projection is served
    (and re-derived read-only when none exists yet).
    """
    result: engine.RefreshResult | None = None
    header: dict[str, object] | None = None
    if refresh:
        result = engine.refresh(root, goal_id)
        records = list(result.records)
        header = event_store.projection_summary(result.document)
    else:
        records = engine.persisted(root, goal_id)
        if not records:
            records = engine.project_events(root, goal_id)
        stored = event_store.load_projection(root, goal_id)
        header = (event_store.projection_summary(stored)
                  if stored is not None else None)
    return {
        "status": "OK",
        "schema_version": model.SCHEMA_VERSION,
        "events_version": model.EVENTS_VERSION,
        "goal_id": goal_id,
        "count": len(records),
        "counts": {
            "category": _counts(records, "category"),
            "severity": _counts(records, "severity"),
        },
        "projection": header,
        "added": ([] if result is None
                  else [record.event_id for record in result.added]),
        "events": [record.to_dict() for record in records],
    }


def notifications(root: str | Path, goal_id: str, *,
                  minimum_severity: str = model.SEV_NOTICE,
                  limit: int = 32) -> list[dict[str, Any]]:
    """Bounded operator notifications (severity >= ``minimum_severity``)."""
    order = [model.SEV_INFO, model.SEV_NOTICE, model.SEV_WARNING,
             model.SEV_CRITICAL]
    threshold = order.index(minimum_severity) if minimum_severity in order \
        else order.index(model.SEV_NOTICE)
    if limit < 1:
        return []
    records = engine.project_events(root, goal_id)
    selected = [
        record for record in records
        if order.index(record.severity) >= threshold
    ]
    return [record.to_dict() for record in selected[-limit:]]


def render(document: Mapping[str, Any]) -> str:
    counts = document.get("counts")
    if not isinstance(counts, Mapping):
        counts = {}
    categories = counts.get("category")
    if not isinstance(categories, Mapping):
        categories = {}
    severities = counts.get("severity")
    if not isinstance(severities, Mapping):
        severities = {}
    header = document.get("projection")
    if not isinstance(header, Mapping):
        header = {}
    lines = [
        f"goal       : {document.get('goal_id')}",
        f"events     : {document.get('count')} "
        f"(persisted={header.get('projection_id') or 'not-refreshed'})",
        "categories : " + " ".join(
            f"{name}={categories.get(name, 0)}" for name in sorted(categories)),
        "severity   : " + " ".join(
            f"{name}={severities.get(name, 0)}" for name in sorted(severities)),
    ]
    events = document.get("events")
    if isinstance(events, Sequence) and events:
        lines.append("recent     :")
        for entry in list(events)[-24:]:
            if not isinstance(entry, Mapping):
                continue
            lines.append(
                f"  {entry.get('occurred_at') or '-'} "
                f"[{entry.get('severity')}] {entry.get('category')}/"
                f"{entry.get('kind')} {entry.get('subject') or '-'} "
                f"{entry.get('reason') or ''}".rstrip())
    return "\n".join(lines)
