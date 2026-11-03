"""MVP — deterministic global search across the portfolio and its history.

The cockpit's List search is a presentation filter: it only sees the objects
that already pass the active filters. This module is the *global* lookup used
by the command palette and by Search-before-Capture: it searches every
project, task, decision, import vocabulary, inbound source, inbound signal and
recent change, independent of the current view.

The ranking, the index and the explanation live in :mod:`trajectory_os.mvp.
recall`; this module keeps the historical ``search`` payload stable so the
dashboard endpoint, the CLI and existing callers keep working unchanged.

Deterministic ranking (no LLM, no fuzzy library): see ``recall`` for the
documented scale — a user-declared alias is an exact identity match (110),
above an exact title (100), a title prefix (80), a title substring (60), an
explicit field (35/30), derived import vocabulary (20) and journal history
(10). Ties are broken by kind then reference id, so results are reproducible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trajectory_os.mvp import model, recall

MAX_QUERY_CHARS = recall.MAX_QUERY_CHARS
MAX_LIMIT = recall.MAX_LIMIT
DEFAULT_LIMIT = recall.DEFAULT_LIMIT


def search(root: str | Path, query: str, *,
           limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Search projects, tasks, decisions, sources, signals and history.

    Returns ``{"query", "results", "count", "dossiers"}``. Every result carries
    the field that matched and a human reason, so the palette can explain a hit
    without asking a model. ``dossiers`` is the project-level roll-up used by
    the recall panel.
    """
    text = str(query or "").strip()
    if not text:
        return {"query": "", "results": [], "count": 0, "dossiers": []}
    if len(text) > MAX_QUERY_CHARS:
        raise model.MvpError(model.E_MALFORMED, "query is too long")
    return recall.search(root, text, limit=limit)


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "MAX_QUERY_CHARS", "search"]
