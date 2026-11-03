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

import contextlib
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from trajectory_os.mvp import (
    changes,
    classification,
    importer,
    model,
    mutations,
    store,
)

#: Deterministic engine identity reported in instrumentation.
ENGINE = "deterministic"
MODEL = "rules-v1"

# --- classification categories ------------------------------------------------

CAT_NEW_TASK = "new_task"
CAT_POSSIBLE_PROJECT = "possible_project"
#: Captured as a vague notion: parked in the Inbox (IDEA / SOMEDAY) instead
#: of being forced into a project or a task.
CAT_POSSIBLE_IDEA = "possible_idea"
CAT_EXISTING_PROJECT = "existing_project"
CAT_EXISTING_TASK = "existing_task"
CAT_DUPLICATE = "duplicate"
#: A line that carries no capturable content: greeting, signature, phone,
#: e-mail, postal address, separator. Never proposed, never written.
CAT_IGNORED = "ignored"

CATEGORIES = frozenset({
    CAT_NEW_TASK, CAT_POSSIBLE_PROJECT, CAT_POSSIBLE_IDEA,
    CAT_EXISTING_PROJECT, CAT_EXISTING_TASK, CAT_DUPLICATE, CAT_IGNORED,
})

#: Reasons written for a human, in the language of the interface.
_IGNORED_REASON = "salutation, signature ou coordonnées : rien à capturer"

#: Categories where the human explicitly confirmed a matched/duplicate item.
#: Accepting such an item is an intentional decision, so Capture must be able
#: to bypass the manual-create duplicate guard explicitly (never blanketly).
_EXPLICIT_DUPLICATE_CATEGORIES = frozenset({
    CAT_DUPLICATE, CAT_EXISTING_TASK, CAT_EXISTING_PROJECT,
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
    CAT_POSSIBLE_IDEA: ACT_ACCEPT,
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

_BULLET_RE = re.compile(r"^\s*(?:[-*•·—–]|\d{1,3}[.)])\s*")

#: Markers of a written message rather than a to-do list.
_GREETING_RE = re.compile(
    r"^\s*(?:bonjour|bonsoir|salut|madame|monsieur|dear|hello|cher|chère)",
    re.IGNORECASE)
_SIGNATURE_RE = re.compile(
    r"(?:cordialement|bien à vous|bien à toi|sincèrement|respectueusement|"
    r"kind regards|best regards|sincerely|yours faithfully)", re.IGNORECASE)
_CONTACT_RE = re.compile(
    r"^\s*(?:t[ée]l[ée]phone|t[ée]l\.?|mobile|portable|adresse|"
    r"adresse [ée]lectronique|e-?mail|mail|courriel|siret|tva)\s*[:：]",
    re.IGNORECASE)
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"^(?:\+?\d[\d .()-]{7,})\d$")
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^[-_=*~\s]{3,}$")


def _is_non_content(line: str) -> bool:
    """True for a line that is structure, not information."""
    stripped = line.strip()
    if not stripped:
        return True
    if _SEPARATOR_RE.match(stripped):
        return True
    if _CONTACT_RE.match(stripped):
        return True
    if _EMAIL_RE.fullmatch(stripped) or _PHONE_RE.match(stripped):
        return True
    if _URL_RE.match(stripped):
        return True
    if _GREETING_RE.match(stripped) and len(stripped.split()) <= 6:
        return True
    if _SIGNATURE_RE.search(stripped) and len(stripped.split()) <= 6:
        return True
    # A bare name block: one to four words, all upper case.
    words = stripped.split()
    return 1 <= len(words) <= 4 and stripped.upper() == stripped


def looks_like_message(text: str) -> bool:
    """Heuristic: is this a written message rather than a list of ideas?

    Capture is designed for a brain-dump of short lines. Pasting a letter into
    it produced a dozen meaningless "task" candidates. The contract stays
    deterministic and explainable: a message has no bullet markers, long lines,
    and either a greeting or a signature.
    """
    lines = [line for line in (raw.strip() for raw in text.splitlines())
             if line]
    if len(lines) < 3:
        return False
    if any(_BULLET_RE.match(line) for line in lines):
        return False
    # Judge the *content* lines only: a letter is full of short structural
    # lines (greeting, phone, signature) that would otherwise hide its prose.
    content = [line for line in lines if not _is_non_content(line)]
    if len(content) < 2:
        return False
    average = sum(len(line) for line in content) / len(content)
    # Prose is measured in words, not characters: a wrapped paragraph has
    # many words per line, a to-do line has few.
    prose = sum(1 for line in content if len(line.split()) >= 8)
    has_greeting = any(_GREETING_RE.match(line) for line in lines[:3])
    has_signature = any(_SIGNATURE_RE.search(line) for line in lines[-4:])
    return (has_greeting or has_signature) and (average >= 60 or prose >= 2)


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
    #: True when the shared classifier found no explicit signal; the preview
    #: must not pretend an uncertain line is a settled task.
    ambiguous: bool = False

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
            "ambiguous": self.ambiguous,
            "duplicate_of": self.duplicate_of,
        }


@dataclass(frozen=True)
class CapturePreview:
    """The result of analysing a capture (read-only, never persisted)."""

    generated_at: str
    items: tuple[CaptureItem, ...]
    counts: dict[str, int]
    instrumentation: dict[str, Any]
    #: True when the text is a written message, not a list of ideas.
    looks_like_message: bool = False
    #: Plain-language explanation shown above the preview (empty = none).
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "items": [item.to_dict() for item in self.items],
            "counts": dict(self.counts),
            "instrumentation": dict(self.instrumentation),
            "looks_like_message": self.looks_like_message,
            "hint": self.hint,
        }


def _now(now: datetime | None) -> datetime:
    instant = now or datetime.now(tz=UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant


def _clean_line(raw: str) -> str:
    line = _BULLET_RE.sub("", raw.strip()).strip()
    return re.sub(r"\s+", " ", line)


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
    # A written message is not a brain-dump: capturing it line by line produced
    # a dozen meaningless "task" candidates (measured on a real letter). It is
    # refused explicitly and pointed to Intake instead.
    as_message = looks_like_message(text)
    all_lines = _split_lines(text)
    lines: list[str] = []
    ignored: list[str] = []
    for line in all_lines:
        if _is_non_content(line):
            ignored.append(line)
        elif not as_message:
            lines.append(line)

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
        "possible_ideas": sum(1 for item in items
                              if item.category == CAT_POSSIBLE_IDEA),
        "ignored": len(ignored),
    }
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    instrumentation = {
        "engine": ENGINE,
        "model": MODEL,
        "elapsed_ms": elapsed_ms,
        "items_processed": len(items),
        "matches": counts["matched_existing"],
        "duplicates": counts["duplicates"],
        "new_objects": (counts["new_tasks"] + counts["possible_projects"]
                        + counts["possible_ideas"]),
        "cost_usd": 0.0,
        "reanalyzed_portfolio": False,
    }
    hint = ""
    if as_message:
        hint = ("Ce texte est un message écrit, pas une liste à découper. "
                "Pour le conserver et le rattacher à un dossier, utilisez "
                "« Intégrer au dossier » (Signaux).")
    elif not items:
        hint = ("Aucune ligne à capturer : salutations, coordonnées et "
                "signature ont été ignorées.")
    return CapturePreview(
        generated_at=_now(now).astimezone(UTC).isoformat(),
        items=tuple(items),
        counts=counts,
        instrumentation=instrumentation,
        looks_like_message=as_message,
        hint=hint,
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
            reason=f"doublon de la ligne {earlier} de cette capture",
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
            reason=f"très proche d'une tâche existante : {best_task[1]}")

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
            reason=f"tâche existante similaire : {best_task[1]}")

    # Shared classification contract (ADR-033): the same criteria used by
    # the document importer, applied here without any LLM.
    classified = classification.classify_text(title)
    project_like = classified.kind in classification.PROJECT_LIKE
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
            reason=f"ce projet existe déjà : {project_match[1]}")

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
            reason=f"ajoute une prochaine action au projet : {project_match[1]}")

    if classified.kind in classification.INBOX_KINDS:
        kind = ("someday" if classified.kind == classification.SOMEDAY
                else "idea")
        return CaptureItem(
            capture_id=capture_id, text=line, title=title,
            category=CAT_POSSIBLE_IDEA, suggested_action=ACT_ACCEPT,
            kind=kind, matched_kind="", matched_id="", matched_title="",
            match_score=project_match[2] if project_match else 0.0,
            project_id="", project_name="",
            reason=classified.reasons[0] + " ; placé dans l'Entrée")

    if project_like:
        return CaptureItem(
            capture_id=capture_id, text=line, title=title,
            category=CAT_POSSIBLE_PROJECT, suggested_action=ACT_ACCEPT,
            kind="project", matched_kind="", matched_id="", matched_title="",
            match_score=project_match[2] if project_match else 0.0,
            project_id="", project_name="",
            reason=classified.reasons[0] + " ; à confirmer avant création")

    target_id = ""
    target_name = ""
    if project_match is not None and project_match[2] >= PROJECT_HINT_THRESHOLD:
        target_id = project_match[0]
        target_name = project_match[1]
    if classified.ambiguous:
        # Do not present uncertain text as an already-settled task: default
        # to skip and surface the ambiguity for an explicit human decision.
        action = ACT_SKIP
        reason = ("aucun signal clair : à préciser (nature et projet) avant "
                  "d'accepter")
    else:
        action = ACT_ACCEPT
        reason = (f"nouvelle tâche pour {target_name}" if target_id
                  else "nouvelle tâche : choisissez un projet avant "
                       "d'accepter")
    return CaptureItem(
        capture_id=capture_id, text=line, title=title,
        category=CAT_NEW_TASK, suggested_action=action,
        kind="task", matched_kind="project" if target_id else "",
        matched_id=target_id, matched_title=target_name,
        match_score=project_match[2] if project_match else 0.0,
        project_id=target_id, project_name=target_name,
        ambiguous=classified.ambiguous, reason=reason)


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
    if kind is not None and kind not in ("task", "project", "idea",
                                         "someday"):
        _fail("kind must be task, project, idea or someday")
    return CaptureDecision(
        capture_id=capture_id, action=action, title=_opt("title"),
        kind=kind, project_id=_opt("project_id"),
        workstream=_opt("workstream"), deliverable=_opt("deliverable"),
        description=_opt("description"))


def confirm(root: str | Path, text: str,
            decisions: Sequence[Mapping[str, Any]],
            *, now: datetime | None = None) -> dict[str, Any]:
    """Confirm a capture under the portfolio write lock."""
    with store.portfolio_lock(root):
        return _confirm_locked(root, text, decisions, now=now)


def _confirm_locked(root: str | Path, text: str,
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
            created_projects.append(
                _create_project(root, item, decision, model.PS_ACTIVE))
        elif kind in ("idea", "someday"):
            status = (model.PS_SOMEDAY if kind == "someday"
                      else model.PS_IDEA)
            created_projects.append(
                _create_project(root, item, decision, status))
        else:
            project_id = decision.project_id or item.project_id
            created_tasks.append(_create_task(
                root, item, decision, project_id))

    if created_tasks or created_projects:
        with contextlib.suppress(OSError, changes.ChangeError):
            changes.append_change(
                root, kind=changes.KIND_CAPTURE,
                object_kind=changes.OBJECT_BATCH,
                object_id=f"capture-{preview_result.generated_at}",
                title="Capture rapide",
                summary=(f"{len(created_tasks)} tâche(s), "
                         f"{len(created_projects)} projet(s)"),
                after={"created_task_ids": created_tasks,
                       "created_project_ids": created_projects},
                source="capture", undoable=True)

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
    if item.category in _EXPLICIT_DUPLICATE_CATEGORIES:
        payload["allow_duplicate"] = True
    if decision.workstream:
        payload["workstream"] = decision.workstream
    if decision.deliverable:
        payload["deliverable"] = decision.deliverable
    result = mutations.create_task(root, payload)
    return str(result["task_id"])


def _create_project(root: str | Path, item: CaptureItem,
                    decision: CaptureDecision,
                    status: str = model.PS_ACTIVE) -> str:
    title = decision.title or item.title
    payload: dict[str, Any] = {
        "name": title,
        "objective": decision.description or item.text or title,
        "status": status,
    }
    if item.category in _EXPLICIT_DUPLICATE_CATEGORIES:
        payload["allow_duplicate"] = True
    result = mutations.create_project(root, payload)
    return str(result["project_id"])


__all__ = [
    "ACT_ACCEPT", "ACT_ATTACH", "ACT_MERGE", "ACT_SKIP", "ACTIONS",
    "CAT_DUPLICATE", "CAT_EXISTING_PROJECT", "CAT_EXISTING_TASK",
    "CAT_NEW_TASK", "CAT_POSSIBLE_IDEA", "CAT_POSSIBLE_PROJECT",
    "CATEGORIES",
    "DUPLICATE_THRESHOLD", "ENGINE", "MATCH_THRESHOLD", "MODEL",
    "PROJECT_HINT_THRESHOLD", "MAX_CAPTURE_CHARS",
    "CaptureDecision", "CaptureError", "CaptureItem", "CapturePreview",
    "confirm", "preview",
]
