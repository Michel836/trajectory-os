"""MVP — deterministic global search across the portfolio and its history.

The cockpit's List search is a presentation filter: it only sees the objects
that already pass the active filters. This module is the *global* lookup used
by the command palette: it searches every project, task, import vocabulary and
recent change, independent of the current view.

Deterministic ranking (no LLM, no fuzzy library):

* exact normalized title = 100;
* title starts with the query = 80;
* title contains the query = 60;
* project / domain / next action / description match = 35;
* imported vocabulary match = 20;
* recent change match = 10.

Ties are broken by kind then reference id, so results are reproducible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trajectory_os.mvp import changes, classification, model, search_memory, store

MAX_QUERY_CHARS = 200
MAX_LIMIT = 100
DEFAULT_LIMIT = 30
MAX_CHANGES = 200


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and needle in classification.normalize_text(haystack)


def _title_score(title: str, query: str) -> tuple[int, str]:
    normalized = classification.normalize_text(title)
    if normalized == query:
        return 100, "titre exact"
    if normalized.startswith(query):
        return 80, "titre"
    if query in normalized:
        return 60, "titre"
    return 0, ""


def search(root: str | Path, query: str, *,
           limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Search projects, tasks, imported vocabulary and recent changes."""
    text = str(query or "").strip()
    if not text:
        return {"query": "", "results": [], "count": 0}
    if len(text) > MAX_QUERY_CHARS:
        raise model.MvpError(model.E_MALFORMED, "query is too long")
    bound = max(1, min(int(limit), MAX_LIMIT))
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    normalized = classification.normalize_text(text)
    projects = portfolio.project_map()
    memory = search_memory.build_index(
        root,
        project_ids=[project.project_id for project in portfolio.projects],
        task_ids=[task.task_id for task in portfolio.tasks],
    )
    results: list[dict[str, Any]] = []

    for project in portfolio.projects:
        score, reason = _title_score(project.name, normalized)
        if not score and _contains(project.objective, normalized):
            score, reason = 35, "objectif"
        if not score and _contains(project.description, normalized):
            score, reason = 35, "description"
        if not score and _contains(project.domain, normalized):
            score, reason = 35, "domaine"
        if score:
            results.append({
                "kind": "project", "ref_id": project.project_id,
                "title": project.name, "project": project.name,
                "status": project.status, "score": score, "reason": reason,
                "hint": f"{project.status} · {project.domain}",
            })

    for task in portfolio.tasks:
        project = projects[task.project_id]
        score, reason = _title_score(task.title, normalized)
        if not score and _contains(task.next_action, normalized):
            score, reason = 35, "prochaine action"
        if not score and _contains(task.description, normalized):
            score, reason = 35, "description"
        if not score and _contains(project.name, normalized):
            score, reason = 30, "projet"
        if not score:
            imported = memory.get("task", task.task_id)
            if imported is not None and _contains(imported.text, normalized):
                score, reason = 20, "historique d'import"
        if score:
            results.append({
                "kind": "task", "ref_id": task.task_id,
                "title": task.title, "project_id": task.project_id,
                "project": project.name, "status": task.status,
                "score": score, "reason": reason,
                "hint": f"{project.name} · {task.status}",
            })

    for record in changes.read_changes(root, limit=MAX_CHANGES):
        score, reason = _title_score(record.title, normalized)
        if not score and _contains(record.summary, normalized):
            score, reason = 10, "changement"
        if score:
            results.append({
                "kind": "change", "ref_id": record.change_id,
                "title": record.title or record.summary,
                "status": record.kind, "score": score, "reason": reason,
                "hint": record.summary,
            })

    results.sort(key=lambda item: (-int(item["score"]),
                                   str(item["kind"]),
                                   str(item["ref_id"])))
    return {"query": text, "results": results[:bound],
            "count": len(results)}


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "MAX_QUERY_CHARS", "search"]
