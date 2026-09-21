"""MVP — durable UI preferences and the colour language of the cockpit.

The visual execution layer needs a small amount of durable *presentation*
state that must never contaminate the validated portfolio:

* which colour identity mode is active (domain/project/status/urgency/impact);
* per-domain and per-project colour overrides;
* user-defined personal highlights (yellow = review, purple = idea, ...);
* the last selected object and the active view/filters/density.

None of this is business data. It lives in a separate, bounded, atomically
written document (``ui_state.json``) at the data root. The portfolio remains
the single source of truth for status, readiness, dependencies and effort;
personal highlights are an *overlay* and never modify business status.

All reads fail soft (a malformed file falls back to defaults so the cockpit
still opens); all writes fail closed (invalid values are rejected).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import model

#: Durable preferences document filename (relative to a data root).
UI_STATE_NAME = "ui_state.json"

#: UI preferences schema version (independent from the portfolio schema).
UI_SCHEMA_VERSION = 1

#: Colour identity modes (``BASE COLOR = identity``).
COLOR_MODES = ("domain", "project", "status", "urgency", "impact")

#: Density modes for cards/tables.
DENSITIES = ("compact", "normal", "detailed")

#: The visual view identifiers rendered by the cockpit.
VIEWS = (
    "list", "kanban", "wbs", "graph", "gantt", "eisenhower",
    "impact_effort", "portfolio_map", "treemap", "progress", "heatmap",
    "goal_flow",
)

#: Named personal highlight colours (rendered as an overlay, never as status).
HIGHLIGHT_COLORS = (
    "yellow", "purple", "blue", "green", "red", "orange", "teal", "grey",
)

#: Deterministic identity palette (base colour = domain/project identity).
PALETTE = (
    "#1f77b4", "#f58518", "#54a24b", "#e45756", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#4c78a8", "#b279a2", "#9edae5", "#ff9da6", "#72b7b2",
)

#: Fixed semantic colours. Status/urgency/impact never change meaning.
SEMANTIC_COLORS: dict[str, dict[str, str]] = {
    "status": {
        "TODO": "#9aa4b2",
        "IN_PROGRESS": "#1f77b4",
        "READY": "#54a24b",
        "WAITING": "#f58518",
        "BLOCKED": "#e45756",
        "DEFERRED": "#7f7f7f",
        "COMPLETED": "#2ca02c",
        "ABANDONED": "#5f6368",
        "ACTIVE": "#1f77b4",
        "PROJECT_CLOSED": "#7f7f7f",
    },
    "urgency": {
        "CRITICAL": "#b3261e",
        "HIGH": "#e45756",
        "MEDIUM": "#f58518",
        "LOW": "#54a24b",
    },
    "impact": {
        "HIGH": "#1f77b4",
        "MEDIUM": "#72b7b2",
        "LOW": "#9aa4b2",
    },
}

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_MAX_MAP_ENTRIES = 512
_MAX_KEY_LEN = 128
_MAX_SELECTION_LEN = 160
_SCOPES = ("all", "domain", "project")
_LEVELS = ("all", "projects", "tasks")
_EVIDENCES = ("FACT", "INFERRED", "SUGGESTED", "UNKNOWN")
_STATUS_FILTERS = (
    "ACTIVE", "READY", "WAITING", "BLOCKED", "DEFERRED", "DONE", "TODO",
    "IN_PROGRESS", "COMPLETED",
)

# --- List-view table state (presentation only) -------------------------------
#
# The cockpit List view renders two independent data tables (projects and
# tasks). Sorting and column filters are pure presentation concerns: they are
# persisted alongside the other UI preferences in ``ui_state.json`` and never
# touch the portfolio. The schema below is deliberately bounded so a malformed
# or hostile client cannot grow the preference document without limit.

#: Sortable columns per List-view table (must match the JS table definitions).
_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": (
        "title", "domain", "status", "urgency", "impact", "progress",
        "effort",
    ),
    "tasks": (
        "title", "project", "status", "ready", "urgency", "impact",
        "effort", "priority", "deadline",
    ),
}

#: Allowed column-filter keys per List-view table.
_TABLE_FILTERS: dict[str, tuple[str, ...]] = {
    "projects": (
        "title", "domain", "status", "urgency", "impact",
        "progress_min", "progress_max", "effort_min", "effort_max",
    ),
    "tasks": (
        "title", "project", "status", "ready", "urgency", "impact",
        "effort_min", "effort_max",
    ),
}

#: Hard bounds for persisted table state.
_MAX_SORT_TERMS = 8
_MAX_TABLE_FILTERS = 12
_MAX_FILTER_VALUE_LEN = 200
_TABLE_SORT_DIRECTIONS = ("asc", "desc")
_TABLE_KEYS = ("projects", "tasks")

# --- Saved presentation views (filter presets) --------------------------------
#
# A saved view captures the *presentation* configuration only (active view,
# colour mode, density, global filters and List-table state). It never stores
# portfolio data and loading one only changes UI preferences.

#: Hard bounds for saved views.
_MAX_SAVED_VIEWS = 32
_MAX_VIEW_NAME_LEN = 48
_SAVED_VIEW_FIELDS = ("view", "color_by", "density", "filters", "tables")


class UIStateError(model.MvpError):
    """A UI-preference contract violation (fail closed on write)."""


def _fail(detail: str) -> NoReturn:
    raise UIStateError(model.E_MALFORMED, detail)


def _valid_colour(value: object, *, allow_named: bool) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if _HEX_RE.fullmatch(value):
        return True
    return allow_named and value in HIGHLIGHT_COLORS


def stable_colour(key: str) -> str:
    """Return a deterministic palette colour for an identity key.

    Uses SHA-1 (not Python's salted ``hash``) so the same object keeps the
    same colour across processes and restarts.
    """
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return PALETTE[int(digest[:8], 16) % len(PALETTE)]


def _clean_map(value: object, *, allow_named: bool) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail("colour maps must be objects")
    if len(value) > _MAX_MAP_ENTRIES:
        _fail("too many colour entries")
    result: dict[str, str] = {}
    for key, colour in value.items():
        if not isinstance(key, str) or not key or len(key) > _MAX_KEY_LEN:
            _fail("invalid colour key")
        if not _valid_colour(colour, allow_named=allow_named):
            _fail(f"invalid colour for {key!r}")
        result[key] = str(colour)
    return result


@dataclass(frozen=True)
class UIPreferences:
    """Bounded, validated presentation preferences (never business data)."""

    schema_version: int = UI_SCHEMA_VERSION
    color_by: str = "domain"
    density: str = "normal"
    view: str = "list"
    domain_colors: dict[str, str] = field(default_factory=dict)
    project_colors: dict[str, str] = field(default_factory=dict)
    highlights: dict[str, str] = field(default_factory=dict)
    selected: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, Any] = field(default_factory=dict)
    saved_views: dict[str, dict[str, Any]] = field(default_factory=dict)

    def validate(self) -> UIPreferences:
        if self.schema_version != UI_SCHEMA_VERSION:
            _fail(f"unsupported ui_state schema_version={self.schema_version}")
        if self.color_by not in COLOR_MODES:
            _fail(f"color_by={self.color_by!r} not in {list(COLOR_MODES)}")
        if self.density not in DENSITIES:
            _fail(f"density={self.density!r} not in {list(DENSITIES)}")
        if self.view not in VIEWS:
            _fail(f"view={self.view!r} not in {list(VIEWS)}")
        _clean_map(self.domain_colors, allow_named=False)
        _clean_map(self.project_colors, allow_named=False)
        _clean_map(self.highlights, allow_named=True)
        if self.selected is not None and (
                not isinstance(self.selected, str)
                or len(self.selected) > _MAX_SELECTION_LEN):
            _fail("invalid selection")
        _validate_filters(self.filters)
        _validate_tables(self.tables)
        _validate_saved_views(self.saved_views)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "color_by": self.color_by,
            "density": self.density,
            "view": self.view,
            "domain_colors": dict(sorted(self.domain_colors.items())),
            "project_colors": dict(sorted(self.project_colors.items())),
            "highlights": dict(sorted(self.highlights.items())),
            "selected": self.selected,
            "filters": dict(self.filters),
            "tables": _normalise_tables(self.tables),
            "saved_views": _normalise_saved_views(self.saved_views),
        }

    @staticmethod
    def from_dict(data: object) -> UIPreferences:
        if not isinstance(data, dict):
            _fail("ui_state must be an object")
        return UIPreferences(
            schema_version=int(data.get("schema_version", UI_SCHEMA_VERSION)),
            color_by=str(data.get("color_by", "domain")),
            density=str(data.get("density", "normal")),
            view=str(data.get("view", "list")),
            domain_colors=_clean_map(data.get("domain_colors"),
                                     allow_named=False),
            project_colors=_clean_map(data.get("project_colors"),
                                      allow_named=False),
            highlights=_clean_map(data.get("highlights"), allow_named=True),
            selected=(str(data["selected"])
                      if data.get("selected") is not None else None),
            filters=_normalise_filters(data.get("filters")),
            tables=_normalise_tables(data.get("tables")),
            saved_views=_normalise_saved_views(data.get("saved_views")),
        )


def default_preferences() -> UIPreferences:
    return UIPreferences(filters=_default_filters(), tables=_default_tables())


def _default_filters() -> dict[str, Any]:
    return {
        "scope": "all",
        "scope_id": None,
        "level": "all",
        "status": [],
        "evidence": [],
    }


def _normalise_filters(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _default_filters()
    result = _default_filters()
    scope = value.get("scope", "all")
    result["scope"] = scope if scope in _SCOPES else "all"
    scope_id = value.get("scope_id")
    result["scope_id"] = (str(scope_id) if isinstance(scope_id, str)
                          and scope_id else None)
    level = value.get("level", "all")
    result["level"] = level if level in _LEVELS else "all"
    result["status"] = _filter_list(value.get("status"), _STATUS_FILTERS)
    result["evidence"] = _filter_list(value.get("evidence"), _EVIDENCES)
    return result


def _filter_list(value: object, allowed: tuple[str, ...]) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({str(item) for item in value if str(item) in allowed})


def _validate_filters(value: object) -> None:
    if not isinstance(value, dict):
        _fail("filters must be an object")
    if value.get("scope", "all") not in _SCOPES:
        _fail("invalid filter scope")
    if value.get("level", "all") not in _LEVELS:
        _fail("invalid filter level")
    _filter_list(value.get("status"), _STATUS_FILTERS)
    _filter_list(value.get("evidence"), _EVIDENCES)


def _default_tables() -> dict[str, Any]:
    return {
        table: {"sort": [], "filters": {}}
        for table in _TABLE_KEYS
    }


def _normalise_sort(value: object, allowed: tuple[str, ...]) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if len(result) >= _MAX_SORT_TERMS:
            break
        if not isinstance(item, dict):
            continue
        column = item.get("column")
        direction = item.get("dir", "asc")
        if not isinstance(column, str) or column not in allowed \
                or column in seen:
            continue
        if direction not in _TABLE_SORT_DIRECTIONS:
            direction = "asc"
        seen.add(column)
        result.append({"column": column, "dir": str(direction)})
    return result


def _normalise_table_filters(value: object,
                             allowed: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, raw in value.items():
        if len(result) >= _MAX_TABLE_FILTERS:
            break
        if not isinstance(key, str) or key not in allowed:
            continue
        if raw is None or isinstance(raw, bool):
            continue
        text = str(raw).strip()
        if not text:
            continue
        result[key] = text[:_MAX_FILTER_VALUE_LEN]
    return result


def _normalise_tables(value: object) -> dict[str, Any]:
    """Coerce arbitrary input into the bounded table-state schema."""
    result = _default_tables()
    if not isinstance(value, dict):
        return result
    for table in _TABLE_KEYS:
        spec = value.get(table)
        if not isinstance(spec, dict):
            continue
        result[table] = {
            "sort": _normalise_sort(spec.get("sort"), _TABLE_COLUMNS[table]),
            "filters": _normalise_table_filters(spec.get("filters"),
                                                _TABLE_FILTERS[table]),
        }
    return result


def _validate_tables(value: object) -> None:
    if not isinstance(value, dict):
        _fail("tables must be an object")
    for table in _TABLE_KEYS:
        spec = value.get(table, {})
        if not isinstance(spec, dict):
            _fail(f"table {table!r} must be an object")
        sort = spec.get("sort", [])
        if not isinstance(sort, (list, tuple)) or len(sort) > _MAX_SORT_TERMS:
            _fail(f"table {table!r} sort must be a bounded list")
        for term in sort:
            if not isinstance(term, dict):
                _fail(f"table {table!r} sort term must be an object")
            if term.get("column") not in _TABLE_COLUMNS[table]:
                _fail(f"invalid sort column for {table!r}")
            if term.get("dir") not in _TABLE_SORT_DIRECTIONS:
                _fail(f"invalid sort direction for {table!r}")
        filters = spec.get("filters", {})
        if not isinstance(filters, dict):
            _fail(f"table {table!r} filters must be an object")
        if len(filters) > _MAX_TABLE_FILTERS:
            _fail(f"too many filters for {table!r}")
        for key, raw in filters.items():
            if key not in _TABLE_FILTERS[table]:
                _fail(f"invalid filter {key!r} for {table!r}")
            if not isinstance(raw, str) \
                    or len(raw) > _MAX_FILTER_VALUE_LEN:
                _fail(f"invalid filter value for {key!r}")


def ui_state_path(root: str | Path) -> Path:
    return Path(root) / UI_STATE_NAME


def _normalise_saved_views(value: object) -> dict[str, dict[str, Any]]:
    """Coerce arbitrary input into the bounded saved-view schema (fail soft)."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw_name, raw_snapshot in value.items():
        if len(result) >= _MAX_SAVED_VIEWS:
            break
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        name = raw_name.strip()[:_MAX_VIEW_NAME_LEN]
        if not isinstance(raw_snapshot, dict):
            continue
        view = raw_snapshot.get("view", "list")
        color_by = raw_snapshot.get("color_by", "domain")
        density = raw_snapshot.get("density", "normal")
        result[name] = {
            "view": view if view in VIEWS else "list",
            "color_by": color_by if color_by in COLOR_MODES else "domain",
            "density": density if density in DENSITIES else "normal",
            "filters": _normalise_filters(raw_snapshot.get("filters")),
            "tables": _normalise_tables(raw_snapshot.get("tables")),
        }
    return result


def _validate_saved_views(value: object) -> None:
    if not isinstance(value, dict):
        _fail("saved_views must be an object")
    if len(value) > _MAX_SAVED_VIEWS:
        _fail("too many saved views")
    for name, snapshot in value.items():
        _validate_view_name(name)
        if not isinstance(snapshot, dict):
            _fail(f"saved view {name!r} must be an object")
        if snapshot.get("view", "list") not in VIEWS:
            _fail(f"invalid view in saved view {name!r}")
        if snapshot.get("color_by", "domain") not in COLOR_MODES:
            _fail(f"invalid color_by in saved view {name!r}")
        if snapshot.get("density", "normal") not in DENSITIES:
            _fail(f"invalid density in saved view {name!r}")
        _validate_filters(snapshot.get("filters", _default_filters()))
        _validate_tables(snapshot.get("tables", _default_tables()))


def _validate_view_name(name: object) -> str:
    if not isinstance(name, str):
        _fail("view name must be a string")
    cleaned = name.strip()
    if not cleaned:
        _fail("view name must not be empty")
    if len(cleaned) > _MAX_VIEW_NAME_LEN:
        _fail(f"view name exceeds {_MAX_VIEW_NAME_LEN} chars")
    if any(ord(ch) < 32 for ch in cleaned):
        _fail("view name must not contain control characters")
    return cleaned


def snapshot_view(prefs: UIPreferences) -> dict[str, Any]:
    """Capture the presentable configuration of ``prefs`` (no business data)."""
    return {
        "view": prefs.view,
        "color_by": prefs.color_by,
        "density": prefs.density,
        "filters": _normalise_filters(prefs.filters),
        "tables": _normalise_tables(prefs.tables),
    }


def save_view(prefs: UIPreferences, name: str,
              snapshot: Mapping[str, Any] | None = None) -> UIPreferences:
    """Create or overwrite a named saved view (fail closed)."""
    cleaned = _validate_view_name(name)
    saved = dict(prefs.saved_views)
    if cleaned not in saved and len(saved) >= _MAX_SAVED_VIEWS:
        _fail("too many saved views")
    if snapshot is None:
        saved[cleaned] = snapshot_view(prefs)
    else:
        saved[cleaned] = _normalise_saved_views({cleaned: snapshot})[cleaned]
    return replace(prefs, saved_views=saved).validate()


def load_view(prefs: UIPreferences, name: str) -> UIPreferences:
    """Apply a saved view to the preferences (presentation only)."""
    snapshot = prefs.saved_views.get(name)
    if snapshot is None:
        _fail(f"unknown saved view {name!r}")
    return replace(
        prefs,
        view=snapshot["view"],
        color_by=snapshot["color_by"],
        density=snapshot["density"],
        filters=_normalise_filters(snapshot["filters"]),
        tables=_normalise_tables(snapshot["tables"]),
    ).validate()


def rename_view(prefs: UIPreferences, name: str,
                new_name: str) -> UIPreferences:
    """Rename a saved view, preserving its snapshot."""
    if name not in prefs.saved_views:
        _fail(f"unknown saved view {name!r}")
    cleaned = _validate_view_name(new_name)
    saved = dict(prefs.saved_views)
    saved[cleaned] = saved.pop(name)
    return replace(prefs, saved_views=saved).validate()


def delete_view(prefs: UIPreferences, name: str) -> UIPreferences:
    """Delete a saved view (``_fail`` when absent)."""
    if name not in prefs.saved_views:
        _fail(f"unknown saved view {name!r}")
    saved = dict(prefs.saved_views)
    saved.pop(name)
    return replace(prefs, saved_views=saved).validate()


def load_preferences(root: str | Path) -> UIPreferences:
    """Load preferences; malformed files fall back to defaults (fail soft)."""
    path = ui_state_path(root)
    if not path.is_file():
        return default_preferences()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return UIPreferences.from_dict(raw).validate()
    except (OSError, ValueError, model.MvpError):
        return default_preferences()


def save_preferences(root: str | Path,
                     prefs: UIPreferences) -> UIPreferences:
    """Validate and atomically persist preferences (fail closed)."""
    prefs.validate()
    intel_model.write_json(ui_state_path(root), prefs.to_dict())
    return prefs


#: Preference keys a client may patch.
_EDITABLE = frozenset({
    "color_by", "density", "view", "selected", "filters", "tables",
    "domain_colors", "project_colors", "highlights",
})


def apply_update(prefs: UIPreferences,
                 payload: dict[str, Any]) -> UIPreferences:
    """Apply a validated partial update, rejecting unknown/invalid fields."""
    if not isinstance(payload, dict):
        _fail("preference update must be an object")
    for key in payload:
        if key not in _EDITABLE:
            _fail(f"unknown preference field {key!r}")
    changes: dict[str, Any] = {}
    if "color_by" in payload:
        changes["color_by"] = str(payload["color_by"])
    if "density" in payload:
        changes["density"] = str(payload["density"])
    if "view" in payload:
        changes["view"] = str(payload["view"])
    if "selected" in payload:
        value = payload["selected"]
        changes["selected"] = None if value in (None, "") else str(value)
    if "filters" in payload:
        changes["filters"] = _normalise_filters(payload["filters"])
    if "tables" in payload:
        changes["tables"] = _normalise_tables(payload["tables"])
    if "domain_colors" in payload:
        changes["domain_colors"] = _clean_map(payload["domain_colors"],
                                              allow_named=False)
    if "project_colors" in payload:
        changes["project_colors"] = _clean_map(payload["project_colors"],
                                               allow_named=False)
    if "highlights" in payload:
        changes["highlights"] = _clean_map(payload["highlights"],
                                           allow_named=True)
    return replace(prefs, **changes).validate()


def set_highlight(prefs: UIPreferences, object_id: str,
                  colour: str | None) -> UIPreferences:
    """Attach or clear one personal highlight overlay."""
    if not isinstance(object_id, str) or not object_id \
            or len(object_id) > _MAX_SELECTION_LEN:
        _fail("invalid highlight object id")
    highlights = dict(prefs.highlights)
    if colour is None or colour == "":
        highlights.pop(object_id, None)
    else:
        if not _valid_colour(colour, allow_named=True):
            _fail("invalid highlight colour")
        highlights[object_id] = colour
    if len(highlights) > _MAX_MAP_ENTRIES:
        _fail("too many highlights")
    return replace(prefs, highlights=highlights).validate()


def set_identity_colour(prefs: UIPreferences, scope: str, key: str,
                        colour: str | None) -> UIPreferences:
    """Attach or clear a domain/project identity colour override."""
    if scope not in ("domain", "project"):
        _fail("identity scope must be domain or project")
    if not isinstance(key, str) or not key or len(key) > _MAX_KEY_LEN:
        _fail("invalid identity key")
    target = dict(prefs.domain_colors if scope == "domain"
                  else prefs.project_colors)
    if colour is None or colour == "":
        target.pop(key, None)
    else:
        if not _valid_colour(colour, allow_named=False):
            _fail("identity colour must be #rrggbb")
        target[key] = colour
    if scope == "domain":
        return replace(prefs, domain_colors=target).validate()
    return replace(prefs, project_colors=target).validate()


def resolve_colour(mode: str, *, domain: str = "", project_id: str = "",
                   status: str = "", urgency: str = "", impact: str = "",
                   prefs: UIPreferences | None = None) -> str:
    """Resolve the base colour for an object under a colour mode.

    Domain/project identity colours are user-overridable and deterministic;
    status/urgency/impact colours are fixed semantics shared by every view.
    """
    if mode == "status":
        return SEMANTIC_COLORS["status"].get(status, "#9aa4b2")
    if mode == "urgency":
        return SEMANTIC_COLORS["urgency"].get(urgency, "#9aa4b2")
    if mode == "impact":
        return SEMANTIC_COLORS["impact"].get(impact, "#9aa4b2")
    if mode == "project":
        if prefs is not None and project_id in prefs.project_colors:
            return prefs.project_colors[project_id]
        return stable_colour("project:" + project_id)
    if prefs is not None and domain in prefs.domain_colors:
        return prefs.domain_colors[domain]
    return stable_colour("domain:" + domain)


__all__ = [
    "COLOR_MODES", "DENSITIES", "HIGHLIGHT_COLORS", "PALETTE",
    "SEMANTIC_COLORS", "UIStateError", "UI_STATE_NAME", "UI_SCHEMA_VERSION",
    "UIPreferences", "VIEWS",
    "apply_update", "default_preferences", "delete_view", "load_preferences",
    "load_view", "rename_view", "resolve_colour", "save_view",
    "save_preferences", "set_highlight", "set_identity_colour",
    "snapshot_view", "stable_colour", "ui_state_path",
]
