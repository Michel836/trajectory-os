"""V1.86 — unified state model + deterministic CLI queries.

Consumes the V1.85 registry and provides stable filters, human-readable
output, and a machine-readable JSON view.  The same data is represented in
both formats (the JSON document is the source of truth); unknown filters
fail closed instead of defaulting silently.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trajectory_os.runs import model, registry


class UnknownFilterError(ValueError):
    def __init__(self, filter_name: str) -> None:
        super().__init__(f"unknown filter: {filter_name}")
        self.filter_name = filter_name


def _matches(view: registry.RunView, filter_name: str) -> bool:
    if filter_name == model.FILTER_ALL:
        return True
    if filter_name == model.FILTER_RECENT:
        return True  # recency is expressed by ordering + limit, handled below
    if filter_name == model.FILTER_ACTIVE:
        return view.lifecycle == model.LIFECYCLE_ACTIVE
    if filter_name == model.FILTER_READY:
        return view.state == model.STATE_READY
    if filter_name == model.FILTER_FAILED:
        return view.state == model.STATE_FAILED
    if filter_name == model.FILTER_INCOMPLETE:
        return view.state in (model.STATE_INCOMPLETE, model.STATE_INVALID)
    if filter_name == model.FILTER_REPAIRED:
        return view.repaired.requested
    raise UnknownFilterError(filter_name)


def apply_filter(
    runs: list[registry.RunView], filter_name: str, limit: int | None = None
) -> list[registry.RunView]:
    """Apply a deterministic filter; fails closed on unknown filters."""
    if filter_name not in model.ALL_FILTERS:
        raise UnknownFilterError(filter_name)
    selected = [view for view in runs if _matches(view, filter_name)]
    if limit is not None:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        selected = selected[:limit]
    return selected


def query_payload(runs_root: Path, filter_name: str, limit: int | None = None) -> dict[str, Any]:
    """Stable machine-readable query result (schema-versioned envelope)."""
    runs = registry.load_runs(runs_root)
    if filter_name not in model.ALL_FILTERS:
        raise UnknownFilterError(filter_name)
    if filter_name == model.FILTER_RECENT and limit is None:
        limit = model.DEFAULT_RECENT_LIMIT
    selected = apply_filter(runs, filter_name, limit)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "tool": model.CLI_NAME,
        "filter": filter_name,
        "limit": limit,
        "total_runs": len(runs),
        "selected_count": len(selected),
        "order": "newest_first",
        "runs": [view.to_dict() for view in selected],
    }


def render_json(doc: dict[str, Any]) -> str:
    """Deterministic JSON rendering (sorted keys, stable formatting)."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_table(doc: dict[str, Any]) -> str:
    """Human-readable table; data is identical to the JSON document."""

    def text(value: Any) -> str:
        if value is None:
            return "-"
        return str(value)

    runs = doc.get("runs", []) if isinstance(doc.get("runs"), list) else []

    def short(value: Any, width: int) -> str:
        rendered = text(value)
        if rendered == "-":
            return rendered
        if len(rendered) > width:
            return rendered[: width - 1] + ">"
        return rendered

    columns: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
        ("RUN_ID", lambda run: run.get("run_id")),
        ("STATE", lambda run: run.get("state")),
        ("LIFECYCLE", lambda run: run.get("lifecycle")),
        ("CLASS", lambda run: short(run.get("run_class"), 20)),
        ("BRANCH", lambda run: short(run.get("branch"), 28)),
        ("VALIDATION", lambda run: run.get("validation")),
        ("REVIEW", lambda run: short(run.get("review_status"), 12)),
        ("REPAIRED", lambda run: "yes" if run.get("repaired", {}).get("requested") else "no"),
        ("STARTED_AT", lambda run: run.get("started_at")),
    ]
    if not runs:
        lines = [
            f"{doc.get('run_count', doc.get('selected_count', 0))} run(s); "
            f"filter={doc.get('filter', 'all')}"
        ]
        lines.append("(none matched)")
        return "\n".join(lines) + "\n"

    widths = [len(name) for name, _ in columns]
    rows = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        row = [text(getter(run)) for _, getter in columns]
        rows.append(row)
        for col, cell in enumerate(row):
            widths[col] = max(widths[col], len(cell))

    header = "  ".join(name.ljust(widths[col]) for col, (name, _) in enumerate(columns))
    sep = "  ".join("-" * widths[col] for col in range(len(columns)))
    body = "\n".join(
        "  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)) for row in rows
    )
    header_line = (
        f"{doc.get('run_count', doc.get('selected_count', 0))} run(s) "
        f"[filter={doc.get('filter', 'all')}]"
    )
    return header_line + "\n" + header + "\n" + sep + "\n" + body + "\n"


def find_run(runs_root: Path, run_id: str) -> registry.RunView | None:
    for view in registry.load_runs(runs_root):
        if view.run_id == run_id:
            return view
    return None
