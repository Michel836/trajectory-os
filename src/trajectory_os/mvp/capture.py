"""MVP — Quick Capture: fast incremental daily input with targeted matching.

Quick Capture is the lightweight daily entry point. It is deliberately
deterministic and local: there is no LLM over the whole portfolio, no
re-analysis of everything the user already has, and no speculative structure.

Pipeline::

    free-form text
      -> split into one candidate per non-empty line
      -> factual classification from the line itself (task / possible project)
      -> targeted matching against the *relevant* existing context
         (project names + task titles, reusing the importer's matcher)
      -> preview: new task / attach to a project / possible project /
         existing match / duplicate
      -> human confirm
      -> accepted items only, through the validated ``mutations`` layer

Nothing is written before confirmation and the preview is a pure function of
the text plus the current portfolio (the portfolio bytes are never touched by
a preview).

The design follows the repository principles: no agent when a deterministic
function is sufficient, no LLM where an algorithm is more reliable, and no
silent mutation.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from trajectory_os.mvp import importer, model, mutations, store

#: Deterministic engine identity reported in instrumentation.
ENGINE = "deterministic"
MODEL = "rules-v1"

# --- classification categories ------------------------------------------------

CAT_NEW_TASK = "new_task"
CAT_POSSIBLE_PROJECT = "possible_project"
CAT_EXISTING_PROJECT = "existing_project"
CAT_EXISTING_TASK = "existing_task"
CAT_DUPLICATE = "duplicate"

CATEGORIES = frozenset({
    CAT_NEW_TASK, CAT_POSSIBLE_PROJECT, CAT_EXISTING_PROJECT,
    CAT_EXISTING_TASK, CAT_DUPLICATE,
})

# --- review actions (also used by the confirm endpoint) -----------------------

ACT_ACCEPT = "accept"
ACT_ATTACH = "attach"
ACT_MERGE = "merge"
ACT_SKIP = "skip"

ACTIONS = frozenset({ACT_ACCEPT, ACT_ATTACH, ACT_MERGE, ACT_SKIP})

#: Suggested default action per category (the human may override).
_SUGGESTED_ACTION = {
    CAT_NEW_TASK: ACT_ACCEPT,
    CAT_POSSIBLE_PROJECT: ACT_ACCEPT,
    CAT_EXISTING_PROJECT: ACT_ATTACH,
    CAT_EXISTING_TASK: ACT_SKIP,
    CAT_DUPLICATE: ACT_SKIP,
}

# --- thresholds (explicit, conservative) --------------------------------------

#: At/above this similarity a line is considered the same object.
DUPLICATE_THRESHOLD = 0.9
#: At/above this similarity a line is considered related to an existing object.
MATCH_THRESHOLD = 0.55
#: At/above this similarity a plain task is filed under the matched project.
PROJECT_HINT_THRESHOLD = 0.35

#: Hard bound on a single capture (fail closed on absurd input).
MAX_CAPTURE_CHARS = 16_384

_MAX_TITLE = model.MAX_STR_LEN

#: Markers that make a line project/idea-like rather than an atomic task.
_PROJECT_MARKERS = (
    "projet", "project", "idee", "idea", "concept", "someday", "un jour",
    "app", "application", "plateforme", "platform", "roadmap", "programme",
    "program", "initiative", "lancement", "lancer un", "creer un",
)

_BULLET_RE = re.compile(r"^\s*(?:[-*•·—–]|\d{1,3}[.)])\s*")


class CaptureError(model.MvpError):
    """A quick-capture request cannot be satisfied (fail closed)."""


def _fail(detail: str) -> NoReturn:
    raise CaptureError(model.E_MALFORMED, detail)


@dataclass(frozen=True)
class CaptureItem:
    """One reviewable capture line with its classification and match."""

    capture_id: str
    text: str
    title: str
    category: str
    suggested_action: str
    kind: str  # "project" | "task"
    matched_kind: str  # "project" | "task" | ""
    matched_id: str
    matched_title: str
    match_score: float
    project_id: str
    project_name: str
    reason: str
    duplicate_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "text": self.text,
            "title": self.title,
            "category": self.category,
            "suggested_action": self.suggested_action,
            "kind": self.kind,
            "matched_kind": self.matched_kind,
            "matched_id": self.matched_id,
            "matched_title": self.matched_title,
            "match_score": round(self.match_score, 3),
            "project_id": self.project_id,
            "project_name": self.project_name,
            "reason": self.reason,
            "duplicate_of": self.duplicate_of,
        }


@dataclass(frozen=True)
class CapturePreview:
    """The result of analysing a capture (read-only, never persisted)."""

    generated_at: str
    items: tuple[CaptureItem, ...]
    counts: dict[str, int]
    instrumentation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "items": [item.to_dict() for item in self.items],
            "counts": dict(self.counts),
            "instrumentation": dict(self.instrumentation),
        }


def _now(now: datetime | None) -> datetime:
    instant = now or datetime.now(tz=UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant


def _clean_line(raw: str) -> str:
    line = _BULLET_RE.sub("", raw.strip()).strip()
    return re.sub(r"\s+", " ", line)


def _is_project_like(title: str) -> bool:
    normalized = importer.normalize_text(title)
    padded = f" {normalized} "
    return any(
        f" {marker} " in padded or normalized.startswith(marker + " ")
        for marker in _PROJECT_MARKERS
    )


def _split_lines(text: str) -> list[str]:
    if len(text) > MAX_CAPTURE_CHARS:
        _fail(f"capture exceeds {MAX_CAPTURE_CHARS} chars")
    lines: list[str] = []
    for raw in text.splitlines():
        cleaned = _clean_line(raw)
        if cleaned:
            lines.append(cleaned)
    return lines


def preview(root: str | Path, text: str, *,
            now: datetime | None = None) -> CapturePreview:
    """Analyse a capture against the current portfolio (no mutation)."""
    started = time.perf_counter()
    if not isinstance(text, str) or not text.strip():
        _fail("capture text is empty")
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        _fail("no portfolio loaded")

    projects = portfolio.project_map()
    tasks = portfolio.task_map()
    lines = _split_lines(text)

    items: list[CaptureItem] = []
    seen_titles: dict[str, str] = {}
    for index, line in enumerate(lines):
        capture_id = f"c{index:04d}"
        items.append(_classify(
            capture_id, line, projects, tasks, seen_titles))
        normalized = importer.normalize_text(line)
        seen_titles.setdefault(normalized, capture_id)

    counts = {
        "items": len(items),
        "matched_existing": sum(
            1 for item in items
            if item.category in (CAT_EXISTING_PROJECT, CAT_EXISTING_TASK)),
        "duplicates": sum(1 for item in items
                          if item.category == CAT_DUPLICATE),
        "new_tasks": sum(1 for item in items
                         if item.category == CAT_NEW_TASK),
        "possible_projects": sum(1 for item in items
                                 if item.category == CAT_POSSIBLE_PROJECT),
    }
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    instrumentation = {
        "engine": ENGINE,
        "model": MODEL,
        "elapsed_ms": elapsed_ms,
        "items_processed": len(items),
        "matches": counts["matched_existing"],
        "duplicates": counts["duplicates"],
        "new_objects": counts["new_tasks"] + counts["possible_projects"],
        "cost_usd": 0.0,
        "reanalyzed_portfolio": False,
    }
    return CapturePreview(
        generated_at=_now(now).astimezone(UTC).isoformat(),
        items=tuple(items),
        counts=counts,
        instrumentation=instrumentation,
    )


def _classify(
    capture_id: str,
    line: str,
    projects: Mapping[str, model.Project],
    tasks: Mapping[str, model.Task],
    seen_titles: Mapping[str, str],
) -> CaptureItem:
    title = line[:_MAX_TITLE]
    normalized = importer.normalize_text(title)

    # 1. duplicate inside the same capture batch
    earlier = seen_titles.get(normalized)
    if earlier is not None:
        return CaptureItem(
            capture_id=capture_id, text=line, title=title,
            category=CAT_DUPLICATE, suggested_action=ACT_SKIP,
            kind="task", matched_kind="", matched_id="", matched_title="",
            match_score=1.0, project_id="", project_name="",
            reason=f"duplicate of line {earlier} in this capture",
            duplicate_of=earlier)

    # 2. best task match (never silently re-create something that exists)
    best_task: tuple[str, str, float] | None = None
    for task in tasks.values():
        score = importer.text_similarity(normalized,
                                         importer.normalize_text(task.title))
        if best_task is None or score > best_task[2]:
            best_task = (task.task_id, task.title, score)

    # 3. best project match (reuses the importer's hint + similarity matcher)
    project_match = importer.best_project_match(title, projects)

    if best_task is not None and best_task[2] >= DUPLICATE_THRESHOLD:
        return CaptureItem(
            capture_id=capture_id, text=line, title=title,
            category=CAT_DUPLICATE, suggested_action=ACT_SKIP,
            kind="task", matched_kind="task", matched_id=best_task[0],
            matched_title=best_task[1], match_score=best_task[2],
            project_id="", project_name="",
            reason=f"very close to existing task: {best_task[1]}")

    if best_task is not None and best_task[2] >= MATCH_THRESHOLD:
        matched_task = tasks.get(best_task[0])
        project_id = matched_task.project_id if matched_task else ""
        return CaptureItem(
            capture_id=capture_id, text=line, title=title,
            category=CAT_EXISTING_TASK, suggested_action=ACT_SKIP,
            kind="task", matched_kind="task", matched_id=best_task[0],
            matched_title=best_task[1], match_score=best_task[2],
            project_id=project_id,
            project_name=projects[project_id].name if project_id in projects
            else "",
            reason=f"similar existing task: {best_task[1]}")

    project_like = _is_project_like(title)

    project_like = _is_project_like(title)
    project_match = importer.best_project_match(title, projects)
    if project_match is not None and importer.text_similarity(
            normalized,
            importer.normalize_text(project_match[1])) >= DUPLICATE_THRESHOLD:
        return CaptureItem(
            capture_id=capture_id, text=line, title=title,
            category=CAT_DUPLICATE, suggested_action=ACT_SKIP,
            kind="project", matched_kind="project", matched_id=project_match[0],
            matched_title=project_match[1], match_score=project_match[2],
            project_id=project_match[0], project_name=project_match[1],
            reason=f"same project already exists: {project_match[1]}")

    if project_match is not None and project_match[2] >= MATCH_THRESHOLD:
        if project_like:
            return CaptureItem(
                capture_id=capture_id, text=line, title=title,
                category=CAT_EXISTING_PROJECT, suggested_action=ACT_SKIP,
                kind="project", matched_kind="project",
                matched_id=project_match[0], matched_title=project_match[1],
                match_score=project_match[2], project_id=project_match[0],
                project_name=project_match[1],
                reason=("an existing project already covers this: "
                        + project_match[1]))
        return CaptureItem(
            capture_id=capture_id, text=line, title=title,
            category=CAT_EXISTING_PROJECT, suggested_action=ACT_ATTACH,
            kind="task", matched_kind="project", matched_id=project_match[0],
            matched_title=project_match[1], match_score=project_match[2],
            project_id=project_match[0], project_name=project_match[1],
            reason=f"adds a next action to existing project: {project_match[1]}")

    if project_like:
        return CaptureItem(
            capture_id=capture_id, text=line, title=title,
            category=CAT_POSSIBLE_PROJECT, suggested_action=ACT_ACCEPT,
            kind="project", matched_kind="", matched_id="", matched_title="",
            match_score=project_match[2] if project_match else 0.0,
            project_id="", project_name="",
            reason="phrased as an idea/project; confirm before creating")

    target_id = ""
    target_name = ""
    if project_match is not None and project_match[2] >= PROJECT_HINT_THRESHOLD:
        target_id = project_match[0]
        target_name = project_match[1]
    return CaptureItem(
        capture_id=capture_id, text=line, title=title,
        category=CAT_NEW_TASK, suggested_action=ACT_ACCEPT,
        kind="task", matched_kind="project" if target_id else "",
        matched_id=target_id, matched_title=target_name,
        match_score=project_match[2] if project_match else 0.0,
        project_id=target_id, project_name=target_name,
        reason=(f"new task for {target_name}" if target_id
                else "new task; choose a project before accepting"))


# --- confirmation -------------------------------------------------------------


@dataclass(frozen=True)
class CaptureDecision:
    """One human decision for one capture item."""

    capture_id: str
    action: str
    title: str | None = None
    kind: str | None = None
    project_id: str | None = None
    workstream: str | None = None
    deliverable: str | None = None
    description: str | None = None


def _decision_from_dict(data: Mapping[str, Any]) -> CaptureDecision:
    capture_id = data.get("capture_id")
    if not isinstance(capture_id, str) or not capture_id:
        _fail("decision.capture_id is required")
    action = data.get("action", ACT_ACCEPT)
    if action not in ACTIONS:
        _fail(f"unknown capture action {action!r}")
    def _opt(key: str) -> str | None:
        value = data.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None
    kind = _opt("kind")
    if kind is not None and kind not in ("task", "project"):
        _fail("kind must be task or project")
    return CaptureDecision(
        capture_id=capture_id, action=action, title=_opt("title"),
        kind=kind, project_id=_opt("project_id"),
        workstream=_opt("workstream"), deliverable=_opt("deliverable"),
        description=_opt("description"))


def confirm(root: str | Path, text: str,
            decisions: Sequence[Mapping[str, Any]],
            *, now: datetime | None = None) -> dict[str, Any]:
    """Apply the accepted capture items through validated mutations.

    The preview is recomputed from ``text`` so the server stays authoritative:
    a client cannot inject a classification the engine would not produce. Only
    explicit decisions are applied; everything is skipped by default.
    """
    preview_result = preview(root, text, now=now)
    by_id = {item.capture_id: item for item in preview_result.items}
    resolved = [_decision_from_dict(raw) for raw in decisions]
    seen: set[str] = set()
    for decision in resolved:
        if decision.capture_id in seen:
            _fail(f"duplicate decision for {decision.capture_id}")
        seen.add(decision.capture_id)
        if decision.capture_id not in by_id:
            _fail(f"unknown capture item {decision.capture_id}")

    # Validate the whole batch *before* applying anything so a confirm never
    # leaves a half-written result behind.
    for decision in resolved:
        item = by_id[decision.capture_id]
        if decision.action in (ACT_SKIP, ACT_MERGE):
            continue
        project_id = decision.project_id or item.project_id
        if decision.action == ACT_ATTACH:
            project_id = project_id or item.matched_id
        if project_id:
            continue
        if decision.action == ACT_ATTACH:
            _fail(f"{item.capture_id}: a project is required to attach")
        if (decision.kind or item.kind) == "task":
            _fail(f"{item.capture_id}: a project is required to create a task")

    created_tasks: list[str] = []
    created_projects: list[str] = []
    skipped = 0
    merged = 0
    for decision in resolved:
        item = by_id[decision.capture_id]
        if decision.action == ACT_SKIP:
            skipped += 1
            continue
        if decision.action == ACT_MERGE:
            merged += 1
            continue
        if decision.action == ACT_ATTACH:
            project_id = decision.project_id or item.project_id \
                or item.matched_id
            created_tasks.append(_create_task(
                root, item, decision, project_id))
            continue
        # ACT_ACCEPT
        kind = decision.kind or item.kind
        if kind == "project":
            created_projects.append(_create_project(root, item, decision))
        else:
            project_id = decision.project_id or item.project_id
            created_tasks.append(_create_task(
                root, item, decision, project_id))

    return {
        "created_tasks": len(created_tasks),
        "created_projects": len(created_projects),
        "skipped": skipped,
        "merged": merged,
        "task_ids": created_tasks,
        "project_ids": created_projects,
        "elapsed_ms": preview_result.instrumentation["elapsed_ms"],
    }


def _create_task(root: str | Path, item: CaptureItem,
                 decision: CaptureDecision, project_id: str | None) -> str:
    if not project_id:
        _fail(f"{item.capture_id}: a project is required to create a task")
    payload: dict[str, Any] = {
        "project_id": project_id,
        "title": decision.title or item.title,
        "description": decision.description or "",
    }
    if decision.workstream:
        payload["workstream"] = decision.workstream
    if decision.deliverable:
        payload["deliverable"] = decision.deliverable
    result = mutations.create_task(root, payload)
    return str(result["task_id"])


def _create_project(root: str | Path, item: CaptureItem,
                    decision: CaptureDecision) -> str:
    title = decision.title or item.title
    payload = {
        "name": title,
        "objective": decision.description or item.text or title,
    }
    result = mutations.create_project(root, payload)
    return str(result["project_id"])


__all__ = [
    "ACT_ACCEPT", "ACT_ATTACH", "ACT_MERGE", "ACT_SKIP", "ACTIONS",
    "CAT_DUPLICATE", "CAT_EXISTING_PROJECT", "CAT_EXISTING_TASK",
    "CAT_NEW_TASK", "CAT_POSSIBLE_PROJECT", "CATEGORIES",
    "DUPLICATE_THRESHOLD", "ENGINE", "MATCH_THRESHOLD", "MODEL",
    "PROJECT_HINT_THRESHOLD", "MAX_CAPTURE_CHARS",
    "CaptureDecision", "CaptureError", "CaptureItem", "CapturePreview",
    "confirm", "preview",
]
