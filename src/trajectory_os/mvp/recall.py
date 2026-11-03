"""MVP — Context Recall: one explainable lookup over everything Trajectory knows.

The cockpit has three historical search surfaces with different scopes:
the List filter (only what already passes the active filters), the palette
(projects/tasks/imports/signals/changes) and the Inspector (one object). None
of them answers the question that actually matters when resuming work:

    "I typed a word I remember. Which dossier is this, and what is its
    current situation?"

This module is that single answer. It builds a *derived, rebuildable* recall
index over the canonical sources and returns ranked, explained matches plus a
bounded contextual view per dossier.

Design invariants:

* **never a source of truth** — the index is derived from ``portfolio.json``,
  ``imports/``, ``inbox/signals.jsonl``, ``inbox/sources.jsonl`` and
  ``changes.jsonl``; deleting it costs nothing;
* **never writes** — a lookup touches no file, takes no lock and appends no
  change. Search before capture stays free of side effects;
* **explainable** — every hit carries the *field* it matched and a human
  reason, so "why did this appear?" is answerable without a model;
* **declared beats inferred** — a user-declared project alias is an exact
  identity match (score 110), above every inferred textual signal;
* **bounded and fail-soft** — every sidecar read is bounded, a malformed or
  missing sidecar is skipped, and a huge query is rejected, never truncated
  silently;
* **deterministic** — same inputs, same ranking, no clock inside the scorer.

No LLM, no embedding, no network on this path (ADR-043).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import (
    changes,
    classification,
    model,
    readiness,
    search_memory,
    signals,
    sources,
    store,
)

# --- kinds (closed, extensible set) -------------------------------------------

K_PROJECT = "project"
K_TASK = "task"
K_DECISION = "decision"
K_NOTE = "note"
K_SOURCE = "source"
K_SIGNAL = "signal"
K_CHANGE = "change"

KINDS = frozenset({K_PROJECT, K_TASK, K_DECISION, K_NOTE, K_SOURCE, K_SIGNAL,
                   K_CHANGE})

#: Display order used when several kinds share the same score.
KIND_ORDER = {K_PROJECT: 0, K_TASK: 1, K_DECISION: 2, K_NOTE: 3,
              K_SOURCE: 4, K_SIGNAL: 5, K_CHANGE: 6}

# --- bounds -------------------------------------------------------------------

MAX_QUERY_CHARS = 200
MAX_LIMIT = 100
DEFAULT_LIMIT = 30
#: Documents read from each sidecar during one index build.
MAX_SOURCES_READ = 500
MAX_SIGNALS_READ = 500
MAX_CHANGES_READ = 500
#: Maximum number of dossiers in a resolved context view.
MAX_CONTEXT_PROJECTS = 5
DEFAULT_CONTEXT_PROJECTS = 3
#: Items kept per section of a resolved context view.
MAX_CONTEXT_ITEMS = 8
#: Maximum number of (root, signature) index entries kept in memory.
MAX_CACHE_ENTRIES = 8

# --- score scale (explicit, documented, stable) -------------------------------
#
# The scale is intentionally readable rather than learned: a human must be able
# to predict it. Declared identity > title > explicit field > contextual field
# > derived vocabulary > history.

S_ALIAS_EXACT = 110
S_ALIAS_PREFIX = 90
S_TITLE_EXACT = 100
S_TITLE_PREFIX = 80
S_TITLE_CONTAINS = 60
S_ALIAS_CONTAINS = 70
S_FIELD_STRONG = 35   # objective, description, next action, question
S_FIELD_WEAK = 30     # project name seen from a task/decision/source
S_SOURCE_TEXT = 30
S_SIGNAL_TEXT = 20
S_IMPORT_VOCAB = 20
S_JOURNAL = 10
S_ID = 95             # exact object id (``case-alpha``, ``alpha.accept``)


#: Bound on the excerpt shown for a note or a signal in a list.
MAX_EXCERPT_CHARS = classification.MAX_EXCERPT_CHARS


def excerpt(text: str, *, maximum: int = MAX_EXCERPT_CHARS) -> str:
    """Readable form of a free text (see ``classification.excerpt``)."""
    return classification.excerpt(text, maximum=maximum)


def _fold(value: str) -> str:
    return classification.normalize_text(value)


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and needle in _fold(haystack)


# --- index --------------------------------------------------------------------


@dataclass(frozen=True)
class RecallDocument:
    """One indexable object with the fields a match may be explained by."""

    kind: str
    ref_id: str
    title: str
    #: Owning project (empty for unattributed sources/signals).
    project_id: str = ""
    project: str = ""
    status: str = ""
    #: (field label, text) pairs, in decreasing importance for explanation.
    fields: tuple[tuple[str, str], ...] = ()
    #: Declared aliases eligible for exact matching.
    aliases: tuple[str, ...] = ()
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "title": self.title,
            "project_id": self.project_id,
            "project": self.project,
            "status": self.status,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class RecallIndex:
    """Derived, immutable recall index (rebuildable at any time)."""

    documents: tuple[RecallDocument, ...]
    #: Reverse map declared alias -> project id (declared identity).
    alias_map: Mapping[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.documents)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for document in self.documents:
            counts[document.kind] = counts.get(document.kind, 0) + 1
        return {
            "documents": len(self.documents),
            "by_kind": dict(sorted(counts.items())),
            "aliases": len(self.alias_map),
        }


@dataclass(frozen=True)
class RecallHit:
    """One ranked match, with the field that explains it."""

    kind: str
    ref_id: str
    title: str
    score: int
    reason: str
    matched_field: str
    project_id: str = ""
    project: str = ""
    status: str = ""
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "title": self.title,
            "score": self.score,
            "reason": self.reason,
            "matched_field": self.matched_field,
            "project_id": self.project_id,
            "project": self.project,
            "status": self.status,
            "hint": self.hint,
        }


# --- cache (increment 1.3) ----------------------------------------------------

_CACHE: dict[tuple[str, str], RecallIndex] = {}


def _data_root(root: str | Path) -> str:
    try:
        return str(Path(root).resolve())
    except OSError:  # pragma: no cover - defensive
        return str(root)


def _files_signature(root: Path) -> str:
    """Cheap fingerprint of every file the index derives from.

    Only ``stat`` metadata is read, so an unchanged data root costs a handful
    of syscalls instead of re-parsing every import artifact and source.
    """
    parts: list[str] = []

    def add(path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            parts.append("0:0")
            return
        parts.append(f"{stat.st_mtime_ns}:{stat.st_size}")

    add(root / store.PORTFOLIO_NAME)
    add(root / changes.CHANGES_NAME)
    add(root / signals.INBOX_DIR / signals.SIGNALS_NAME)
    add(root / signals.INBOX_DIR / sources.SOURCES_NAME)
    try:
        import_files = sorted((root / "imports").glob("*.json"))
    except OSError:
        import_files = []
    parts.append(str(len(import_files)))
    for path in import_files[:search_memory.MAX_ARTIFACTS]:
        add(path)
    return "|".join(parts)


def _cache_get(key: tuple[str, str]) -> RecallIndex | None:
    return _CACHE.get(key)


def _cache_put(key: tuple[str, str], index: RecallIndex) -> None:
    if len(_CACHE) >= MAX_CACHE_ENTRIES and key not in _CACHE:
        # Deterministic eviction: drop the oldest insertion.
        oldest = next(iter(_CACHE))
        _CACHE.pop(oldest, None)
    _CACHE[key] = index


def clear_cache(root: str | Path | None = None) -> None:
    """Drop cached indexes (used by tests and after an out-of-band write)."""
    if root is None:
        _CACHE.clear()
        return
    prefix = _data_root(root)
    for key in [k for k in _CACHE if k[0] == prefix]:
        _CACHE.pop(key, None)


# --- index building -----------------------------------------------------------


def _project_document(project: model.Project,
                      imported: str) -> RecallDocument:
    fields: list[tuple[str, str]] = [
        ("objectif", project.objective),
        ("domaine", project.domain),
    ]
    if project.description:
        fields.append(("description", project.description))
    if imported:
        fields.append(("vocabulaire importé", imported))
    return RecallDocument(
        kind=K_PROJECT,
        ref_id=project.project_id,
        title=project.name,
        project_id=project.project_id,
        project=project.name,
        status=project.status,
        fields=tuple(fields),
        aliases=tuple(project.aliases),
        hint=f"{project.status} · {project.domain}",
    )


def _task_document(task: model.Task, project: model.Project,
                   imported: str) -> RecallDocument:
    fields: list[tuple[str, str]] = []
    if task.next_action:
        fields.append(("prochaine action", task.next_action))
    if task.description:
        fields.append(("description", task.description))
    if task.workstream:
        fields.append(("workstream", task.workstream))
    if task.deliverable:
        fields.append(("livrable", task.deliverable))
    if task.waiting_for:
        fields.append(("attente", ", ".join(task.waiting_for)))
    if imported:
        fields.append(("vocabulaire importé", imported))
    return RecallDocument(
        kind=K_TASK,
        ref_id=task.task_id,
        title=task.title,
        project_id=task.project_id,
        project=project.name,
        status=task.status,
        fields=tuple(fields),
        hint=f"{project.name} · {task.status}",
    )


def _decision_document(decision: model.Decision,
                       projects: Mapping[str, model.Project]
                      ) -> RecallDocument:
    fields: list[tuple[str, str]] = []
    if decision.question:
        fields.append(("question", decision.question))
    if decision.choice:
        fields.append(("choix", decision.choice))
    if decision.rationale:
        fields.append(("justification", decision.rationale))
    for option in decision.options:
        fields.append(("option", option))
    project = projects.get(decision.project_id)
    return RecallDocument(
        kind=K_DECISION,
        ref_id=decision.decision_id,
        title=decision.title,
        project_id=decision.project_id,
        project=project.name if project is not None else "",
        status=decision.status,
        fields=tuple(fields),
        hint=f"décision · {decision.status}",
    )


def _source_document(record: sources.SourceRecord) -> RecallDocument:
    fields: list[tuple[str, str]] = [("source", record.text)]
    if record.sender:
        fields.append(("expéditeur", record.sender))
    if record.thread_key:
        fields.append(("fil", record.thread_key))
    project_id = record.project_hint
    return RecallDocument(
        kind=K_SOURCE,
        ref_id=record.source_id,
        title=record.title or record.location,
        project_id=project_id,
        status=record.status,
        fields=tuple(fields),
        hint=f"{record.origin} · {record.received_at}",
    )


def _note_document(note: model.Note,
                   projects: Mapping[str, model.Project]) -> RecallDocument:
    project = projects.get(note.project_id)
    fields: list[tuple[str, str]] = [("note", note.text)]
    if note.source_ref:
        fields.append(("provenance", note.source_ref))
    return RecallDocument(
        kind=K_NOTE,
        ref_id=note.note_id,
        title=excerpt(note.text),
        project_id=note.project_id,
        project=project.name if project is not None else "",
        status=note.kind,
        fields=tuple(fields),
        hint=f"{note.kind.lower()} · {note.captured_at}",
    )


def _signal_document(signal: signals.InboundSignal) -> RecallDocument:
    return RecallDocument(
        kind=K_SIGNAL,
        ref_id=signal.signal_id,
        title=excerpt(signal.text),
        project_id=signal.project_hint,
        status=signal.status,
        fields=(("signal", signal.text),),
        hint=f"{signal.source_ref} · {signal.status}",
    )


def _change_document(record: changes.ChangeRecord) -> RecallDocument:
    project_id = (record.object_id
                  if record.object_kind == changes.OBJECT_PROJECT else "")
    return RecallDocument(
        kind=K_CHANGE,
        ref_id=record.change_id,
        title=record.title or record.summary,
        project_id=project_id,
        status=record.kind,
        fields=(("journal", record.summary),),
        hint=record.at,
    )


def build_index(root: str | Path,
                portfolio: model.Portfolio | None = None) -> RecallIndex:
    """Build (or return the cached) recall index for one data root.

    A missing or corrupt sidecar is skipped: recall must never be less
    available than the portfolio itself.
    """
    base = Path(root)
    key = (_data_root(root), _files_signature(base))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    portfolio = portfolio if portfolio is not None else store.load_portfolio(root)
    if portfolio is None:
        index = RecallIndex(documents=(), alias_map={})
        _cache_put(key, index)
        return index

    projects = portfolio.project_map()
    memory = search_memory.build_index(
        root,
        project_ids=[project.project_id for project in portfolio.projects],
        task_ids=[task.task_id for task in portfolio.tasks],
    )

    documents: list[RecallDocument] = []
    alias_map: dict[str, str] = {}

    for project in portfolio.projects:
        imported = memory.get(search_memory.PROJECT, project.project_id)
        documents.append(_project_document(
            project, imported.text if imported is not None else ""))
        for alias in project.aliases:
            alias_map.setdefault(_fold(alias), project.project_id)

    for task in portfolio.tasks:
        owner = projects.get(task.project_id)
        if owner is None:  # pragma: no cover - portfolio validation
            continue
        imported = memory.get(search_memory.TASK, task.task_id)
        documents.append(_task_document(
            task, owner, imported.text if imported is not None else ""))

    for decision in portfolio.decisions:
        documents.append(_decision_document(decision, projects))

    for note in portfolio.notes:
        documents.append(_note_document(note, projects))

    for source_record in _safe_sources(root):
        documents.append(_source_document(source_record))

    for signal in _safe_signals(root):
        documents.append(_signal_document(signal))

    for change_record in _safe_changes(root):
        documents.append(_change_document(change_record))

    index = RecallIndex(documents=tuple(documents), alias_map=dict(alias_map))
    _cache_put(key, index)
    return index


def _safe_sources(root: str | Path) -> tuple[sources.SourceRecord, ...]:
    try:
        return sources.read_sources(root, limit=MAX_SOURCES_READ)
    except (OSError, model.MvpError, ValueError):
        return ()


def _safe_signals(root: str | Path) -> tuple[signals.InboundSignal, ...]:
    try:
        return signals.read_signals(root, limit=MAX_SIGNALS_READ)
    except (OSError, model.MvpError, ValueError):
        return ()


def _safe_changes(root: str | Path) -> tuple[changes.ChangeRecord, ...]:
    try:
        return changes.read_changes(root, limit=MAX_CHANGES_READ)
    except (OSError, model.MvpError, ValueError):
        return ()


# --- scoring ------------------------------------------------------------------


def _rank_field(field_name: str, text: str, query: str,
                alias: bool = False) -> tuple[int, str] | None:
    """Return ``(score, reason)`` when ``text`` matches ``query``."""
    folded = _fold(text)
    if not folded or not query:
        return None
    if alias:
        if folded == query:
            return S_ALIAS_EXACT, "alias exact"
        if folded.startswith(query):
            return S_ALIAS_PREFIX, "alias"
        if query in folded:
            return S_ALIAS_CONTAINS, "alias"
        return None
    if field_name in ("titre", "nom"):
        if folded == query:
            return S_TITLE_EXACT, "titre exact"
        if folded.startswith(query):
            return S_TITLE_PREFIX, "titre"
        if query in folded:
            return S_TITLE_CONTAINS, "titre"
        return None
    if field_name == "identifiant":
        if folded == query:
            return S_ID, "identifiant"
        return None
    if field_name in ("objectif", "description", "prochaine action",
                      "question", "choix", "justification", "option",
                      "workstream", "livrable", "attente"):
        if query in folded:
            return S_FIELD_STRONG, field_name
        return None
    if field_name in ("domaine", "projet", "expéditeur", "fil"):
        if query in folded:
            return S_FIELD_WEAK, field_name
        return None
    if field_name in ("note",):
        if query in folded:
            return S_FIELD_STRONG, "note"
        return None
    if field_name in ("provenance",):
        if query in folded:
            return S_JOURNAL, "provenance"
        return None
    if field_name in ("source",):
        if query in folded:
            return S_SOURCE_TEXT, "texte de la source"
        return None
    if field_name in ("signal",):
        if query in folded:
            return S_SIGNAL_TEXT, "signal"
        return None
    if field_name in ("vocabulaire importé",):
        if query in folded:
            return S_IMPORT_VOCAB, "historique d'import"
        return None
    if field_name in ("journal",):
        if query in folded:
            return S_JOURNAL, "changement"
        return None
    return None


def _score_document(document: RecallDocument,
                    query: str) -> tuple[int, str, str] | None:
    """Best (score, reason, matched field) for one document."""
    best: tuple[int, str, str] | None = None

    for alias in document.aliases:
        hit = _rank_field("alias", alias, query, alias=True)
        if hit is not None and (best is None or hit[0] > best[0]):
            best = (hit[0], hit[1], "alias")

    hit = _rank_field("titre", document.title, query)
    if hit is not None and (best is None or hit[0] > best[0]):
        best = (hit[0], hit[1], "titre")

    if best is None or best[0] < S_TITLE_EXACT:
        hit = _rank_field("identifiant", document.ref_id, query)
        if hit is not None and (best is None or hit[0] > best[0]):
            best = (hit[0], hit[1], "identifiant")

    for field_name, text in document.fields:
        if not text:
            continue
        hit = _rank_field(field_name, text, query)
        if hit is not None and (best is None or hit[0] > best[0]):
            best = (hit[0], hit[1], field_name)

    if best is None and document.project:
        hit = _rank_field("projet", document.project, query)
        if hit is not None:
            best = (hit[0], "projet", "projet")

    return best


def query(root: str | Path, text: str, *,
          limit: int = DEFAULT_LIMIT,
          kinds: Iterable[str] | None = None,
          index: RecallIndex | None = None) -> list[RecallHit]:
    """Rank every indexed object against ``text`` (read-only, deterministic)."""
    raw = str(text or "").strip()
    if not raw:
        return []
    if len(raw) > MAX_QUERY_CHARS:
        raise model.MvpError(model.E_MALFORMED, "query is too long")
    bound = max(1, min(int(limit), MAX_LIMIT))
    allowed = frozenset(kinds) if kinds is not None else KINDS
    resolved = index if index is not None else build_index(root)
    normalized = _fold(raw)

    hits: list[RecallHit] = []
    for document in resolved.documents:
        if document.kind not in allowed:
            continue
        scored = _score_document(document, normalized)
        if scored is None:
            continue
        score, reason, field = scored
        hits.append(RecallHit(
            kind=document.kind,
            ref_id=document.ref_id,
            title=document.title,
            score=score,
            reason=reason,
            matched_field=field,
            project_id=document.project_id,
            project=document.project,
            status=document.status,
            hint=document.hint,
        ))

    hits.sort(key=lambda hit: (-hit.score, KIND_ORDER.get(hit.kind, 99),
                               hit.ref_id))
    return hits[:bound]


def search(root: str | Path, text: str, *,
           limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Backward-compatible global search payload (``/api/search``)."""
    raw = str(text or "").strip()
    if not raw:
        return {"query": "", "results": [], "count": 0, "dossiers": []}
    hits = query(root, raw, limit=MAX_LIMIT)
    bound = max(1, min(int(limit), MAX_LIMIT))
    results = [hit.to_dict() for hit in hits]
    return {
        "query": raw,
        "results": results[:bound],
        "count": len(results),
        "dossiers": _dossier_summary(hits, build_index(root)),
    }


def _dossier_summary(hits: Sequence[RecallHit],
                     index: RecallIndex | None = None) -> list[dict[str, Any]]:
    """Project-level roll-up: which dossiers this query points at, and why."""
    names: dict[str, str] = {}
    if index is not None:
        for document in index.documents:
            if document.kind == K_PROJECT:
                names[document.project_id] = document.title
    best: dict[str, tuple[int, str, str]] = {}
    for hit in hits:
        if not hit.project_id:
            continue
        current = best.get(hit.project_id)
        if current is None or hit.score > current[0]:
            best[hit.project_id] = (hit.score, hit.title, hit.reason)
    out: list[dict[str, Any]] = []
    for project_id, (score, _hit_title, reason) in best.items():
        out.append({
            "project_id": project_id,
            "title": names.get(project_id, project_id),
            "score": score,
            "reason": reason,
        })
    out.sort(key=lambda item: (-int(item["score"]), str(item["project_id"])))
    return out


# --- contextual resolution (increment 1.4) ------------------------------------


def _context_for_project(root: str | Path, project_id: str,
                         hits: Sequence[RecallHit],
                         today: date | None) -> dict[str, Any]:
    """Bounded, labelled context for one dossier (derived, never canonical)."""
    from trajectory_os.mvp import insights

    explanation = insights.explain_project(root, project_id, today=today)
    project = explanation["project"]
    matched = [hit.to_dict() for hit in hits
               if hit.project_id == project_id]
    linked_sources = [
        record for record in _safe_sources(root)
        if record.project_hint == project_id
    ][:MAX_CONTEXT_ITEMS]
    portfolio = store.load_portfolio(root)
    project_notes = portfolio.notes_of(project_id) if portfolio else ()
    notes = [
        {"note_id": note.note_id, "kind": note.kind,
         "title": excerpt(note.text),
         "captured_at": note.captured_at, "source_ref": note.source_ref}
        for note in project_notes
        if note.kind != model.NK_QUESTION
    ][:MAX_CONTEXT_ITEMS]
    # Open questions: a QUESTION note with no ANSWER note carrying the same
    # source. There is no explicit link yet (that is the next increment), so the
    # wording stays honest: "sans réponse enregistrée".
    answered_sources = {note.source_ref for note in project_notes
                        if note.kind == model.NK_ANSWER and note.source_ref}
    questions = [
        {"note_id": note.note_id, "text": note.text,
         "captured_at": note.captured_at, "source_ref": note.source_ref,
         "answered": note.source_ref in answered_sources}
        for note in project_notes
        if note.kind == model.NK_QUESTION
    ][:MAX_CONTEXT_ITEMS]
    waiting = [
        item for item in explanation["blockers"]
        if item["state"] == readiness.RS_WAITING
    ][:MAX_CONTEXT_ITEMS]
    blocked = [
        item for item in explanation["blockers"]
        if item["state"] != readiness.RS_WAITING
    ][:MAX_CONTEXT_ITEMS]
    return {
        "project_id": project_id,
        "title": project["name"],
        "aliases": list(project.get("aliases") or ()),
        "status": project["status"],
        "domain": project["domain"],
        "deadline": project["deadline"],
        "health": explanation["health"],
        "progress": explanation["progress"],
        "counts": {
            "tasks": explanation["tasks"],
            "open": explanation["open_tasks"],
            "completed": explanation["completed_tasks"],
        },
        "notes": notes,
        "questions": questions,
        "next_actions": explanation["next_actions"][:MAX_CONTEXT_ITEMS],
        "blocked": blocked,
        "waiting": waiting,
        "risks": explanation["risks"][:MAX_CONTEXT_ITEMS],
        "recent_changes": explanation["recent_changes"][:MAX_CONTEXT_ITEMS],
        # A signal and its attached note carry the same text: showing both
        # would print the same paragraph twice (reported from real use).
        "signals": _distinct_signals(explanation["signals"], notes,
                                     project_notes),
        "sources": [
            {
                "source_id": record.source_id,
                "title": record.title,
                "origin": record.origin,
                "received_at": record.received_at,
                "status": record.status,
            }
            for record in linked_sources
        ],
        "matched": matched,
    }


def _distinct_signals(items: Sequence[Mapping[str, Any]],
                      notes: Sequence[Mapping[str, Any]],
                      project_notes: Sequence[model.Note],
                      ) -> list[dict[str, Any]]:
    """Signals that add information beyond the notes already attached."""
    note_texts = {classification.excerpt(note.text)
                  for note in project_notes}
    out: list[dict[str, Any]] = []
    for item in items:
        title = excerpt(str(item.get("text", "")))
        if title in note_texts:
            continue
        out.append({
            "signal_id": item.get("signal_id", ""),
            "title": title,
            "status": item.get("status", ""),
            "source_ref": item.get("source_ref", ""),
            "received_at": item.get("received_at", ""),
        })
        if len(out) >= MAX_CONTEXT_ITEMS:
            break
    return out


def resolve(root: str | Path, text: str, *,
            limit_projects: int = DEFAULT_CONTEXT_PROJECTS,
            limit_items: int = DEFAULT_LIMIT,
            today: date | None = None) -> dict[str, Any]:
    """Turn a word into a resumable context: dossiers + their current state.

    The payload is explicitly *derived* (``canonical: False``): it composes the
    canonical portfolio, readiness, priority, journal and sources for reading,
    and never proposes or applies a mutation.
    """
    raw = str(text or "").strip()
    bound = max(1, min(int(limit_projects), MAX_CONTEXT_PROJECTS))
    if not raw:
        return {
            "query": "",
            "canonical": False,
            "count": 0,
            "results": [],
            "dossiers": [],
        }
    hits = query(root, raw, limit=limit_items)
    index = build_index(root)
    summary = _dossier_summary(hits, index)
    dossiers = [
        _context_for_project(root, str(item["project_id"]), hits, today)
        for item in summary[:bound]
    ]
    return {
        "query": raw,
        "canonical": False,
        "generated_at": intel_model.utc_now(),
        "count": len(dossiers),
        "results": [hit.to_dict() for hit in hits],
        "dossiers": dossiers,
    }


def alias_map(root: str | Path) -> Mapping[str, str]:
    """Declared alias -> project mapping (read-only, for diagnostics)."""
    return build_index(root).alias_map


def stats(root: str | Path) -> dict[str, Any]:
    """Index composition, used by the cockpit and by tests."""
    index = build_index(root)
    payload = index.to_dict()
    payload["signature"] = _files_signature(Path(root))
    payload["cached"] = ((_data_root(root), payload["signature"]) in _CACHE)
    return payload


def cache_size() -> int:
    """Number of cached indexes (diagnostics/tests only)."""
    return len(_CACHE)


__all__ = [
    "DEFAULT_CONTEXT_PROJECTS",
    "ROUTE_MIN_MARGIN",
    "ROUTE_MIN_SCORE",
    "DEFAULT_LIMIT",
    "KINDS",
    "K_CHANGE",
    "K_DECISION",
    "K_NOTE",
    "K_PROJECT",
    "K_SIGNAL",
    "K_SOURCE",
    "K_TASK",
    "MAX_CONTEXT_PROJECTS",
    "MAX_EXCERPT_CHARS",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "RecallDocument",
    "RecallHit",
    "RecallIndex",
    "alias_map",
    "build_index",
    "cache_size",
    "clear_cache",
    "excerpt",
    "query",
    "resolve",
    "route",
    "search",
    "stats",
]


# --- routing: which dossier does this text belong to? -------------------------
#
# `query` answers "what matches this word?". `route` answers the opposite and
# harder question: "this whole message — where does it belong?".
#
# Measured on a real 56-dossier portfolio: a pure token-overlap scorer picks the
# right dossier when the text names it or uses its declared vocabulary, and it
# picks *nonsense* on an off-topic message. Therefore routing never assigns:
# it proposes up to three candidates with a reason, and it **abstains** below an
# explicit threshold or when the top two candidates are too close. The human (or
# the intake screen) decides. See ADR-046.

#: Minimum score for a dossier to be proposed as the destination.
ROUTE_MIN_SCORE = 2.0
#: Minimum top/second ratio; below it the answer is considered ambiguous.
ROUTE_MIN_MARGIN = 1.2
#: Where a dossier name must appear to count as *identity* rather than a passing
#: mention: in the header (subject/opening) or in the signature block. A name
#: quoted mid-body is evidence that the word is shared vocabulary, not that the
#: message belongs to that dossier (measured on a real letter where an expense
#: mentioned in the fifth paragraph was the name of another dossier).
ROUTE_IDENTITY_HEADER = 0.35
ROUTE_IDENTITY_SIGNATURE = 0.75
#: Text tokens shorter than this are ignored when scoring.
ROUTE_MIN_TOKEN_LEN = 3
#: Bounded number of tokens read from a pasted text.
ROUTE_MAX_TOKENS = 4000

#: Function words ignored by the router (French + English administrative
#: mail). Kept as a source list so the vocabulary stays readable and reviewable.
_ROUTE_STOPWORDS_SOURCE = """
the and for you your with this that have has had will would could should please kind
regards dear before from about also whether there afterwards then once been being all
any can not but its his her she him they them was were are is our out up down over
under again further here when where why how both each few more most other some such
only own same than too very just now le la les des du de un une et ou en au aux ce cet
cette ces que qui quoi dont pour dans sur avec par sans sous vers chez est sont ete
etait seront serait comme plus moins tres tout tous toute toutes autre autres meme
aussi afin donc car si non oui je tu il elle nous vous ils elles mon ma mes ton ta tes
son sa ses notre nos votre vos leur leurs me te se lui y
"""
_ROUTE_STOPWORDS = frozenset(_ROUTE_STOPWORDS_SOURCE.split())


def _route_tokens(text: str) -> set[str]:
    tokens = classification.normalize_text(text).split()
    return {token for token in tokens[:ROUTE_MAX_TOKENS]
            if len(token) >= ROUTE_MIN_TOKEN_LEN
            and token not in _ROUTE_STOPWORDS}


def _project_vocabulary(portfolio: model.Portfolio) -> dict[str, str]:
    """Everything a dossier is *known* to talk about, per project id."""
    tasks: dict[str, list[str]] = {}
    notes: dict[str, list[str]] = {}
    for task in portfolio.tasks:
        tasks.setdefault(task.project_id, []).append(task.title)
    for note in portfolio.notes:
        notes.setdefault(note.project_id, []).append(note.text[:2000])
    out: dict[str, str] = {}
    for project in portfolio.projects:
        out[project.project_id] = classification.normalize_text(" ".join([
            project.name,
            project.objective,
            project.description,
            project.domain,
            " ".join(project.aliases),
            " ".join(tasks.get(project.project_id, ())),
            " ".join(notes.get(project.project_id, ())),
        ]))
    return out


def route(root: str | Path, text: str, *,
          limit: int = 3,
          portfolio: model.Portfolio | None = None,
          threshold: float = ROUTE_MIN_SCORE,
          margin: float = ROUTE_MIN_MARGIN) -> dict[str, Any]:
    """Propose the dossier a pasted text belongs to (never assign it).

    Returns candidates ordered by score with an explicit reason, plus
    ``abstain`` when no dossier is convincing. A caller must present the
    candidates and let the human confirm before anything is written.
    """
    raw = str(text or "").strip()
    bound = max(1, min(int(limit), MAX_CONTEXT_PROJECTS))
    if not raw:
        return {"candidates": [], "abstain": True, "top": None,
                "reason": "texte vide", "threshold": threshold,
                "margin": margin}
    if len(raw) > model.MAX_NOTE_TEXT:
        raise model.MvpError(model.E_MALFORMED, "text is too long")
    loaded = portfolio if portfolio is not None else store.load_portfolio(root)
    if loaded is None:
        return {"candidates": [], "abstain": True, "top": None,
                "reason": "aucun portefeuille", "threshold": threshold,
                "margin": margin}

    tokens = _route_tokens(raw)
    folded = classification.normalize_text(raw)
    total_chars = max(1, len(folded))
    vocabulary = _project_vocabulary(loaded)
    project_tokens = {pid: set(hay.split()) for pid, hay in vocabulary.items()}
    document_frequency: Counter[str] = Counter()
    for words in project_tokens.values():
        for token in words:
            document_frequency[token] += 1

    # The *leading* identity token of a dossier name is its brand ("orcinvest",
    # "donation"). Other name words are ordinary vocabulary: counting them as
    # identity made "restauration" propose a company-restoration dossier for a
    # backup-restore message (measured, then fixed here).
    def _brand(project: model.Project) -> str:
        for token in classification.normalize_text(project.name).split():
            if len(token) >= 4:
                return token
        return ""

    brand_tokens = {project.project_id: _brand(project)
                    for project in loaded.projects}
    # Aliases are matched as normalized *phrases* inside the folded text: an
    # alias is often several words ("aduriz bonneau"), which can never match a
    # set of single tokens.
    alias_phrases = {
        project.project_id: tuple(
            phrase for phrase in (classification.normalize_text(alias)
                                  for alias in project.aliases)
            if len(phrase) >= 3)
        for project in loaded.projects
    }

    scored: list[tuple[float, model.Project, str, list[str], bool,
                       float | None]] = []
    for project in loaded.projects:
        words = project_tokens.get(project.project_id, set())
        matched = sorted(token for token in tokens if token in words)
        score = sum(1.0 / math.log(1 + document_frequency[token])
                    for token in matched)
        reasons: list[str] = []
        identity: set[str] = set()
        positions: list[float] = []
        declared_hit = False
        brand = brand_tokens.get(project.project_id, "")
        if brand and brand in tokens:
            identity.add(brand)
            score += 3.0
            at = folded.find(brand)
            position = (at / total_chars) if at >= 0 else None
            if position is not None:
                positions.append(position)
                reasons.append("le texte nomme « " + brand + " » ("
                               + str(round(position * 100)) + " %)")
            else:  # pragma: no cover - brand is in the tokens by construction
                reasons.append("le texte nomme « " + brand + " »")
        declared: list[str] = []
        for phrase in alias_phrases.get(project.project_id, ()):
            at = folded.find(phrase)
            if at < 0:
                continue
            declared.append(phrase)
            positions.append(at / total_chars)
        if declared:
            declared_hit = True
            score += 3.0 * len(declared)
            identity |= set(" ".join(declared).split())
            reasons.append("le texte contient l'alias « "
                           + " », « ".join(declared) + " »")
        context = [token for token in matched if token not in identity]
        if context:
            reasons.append("vocabulaire commun : " + ", ".join(context[:6]))
        if score <= 0:
            continue
        best_position = min(positions) if positions else None
        scored.append((round(score, 2), project, "; ".join(reasons), context,
                       declared_hit, best_position))

    scored.sort(key=lambda item: (-item[0], item[1].project_id))
    ranked = scored[:bound]
    candidates = [
        {"project_id": project.project_id, "title": project.name,
         "status": project.status, "score": score,
         "reason": reason or "vocabulaire commun",
         "matched": matched[:8],
         "identity": declared_hit or position is not None,
         "identity_position": position}
        for score, project, reason, matched, declared_hit, position in ranked
    ]
    if not candidates:
        return {"candidates": [], "abstain": True, "top": None,
                "reason": "aucun vocabulaire commun avec un dossier",
                "threshold": threshold, "margin": margin}

    top_score, _, _, _, top_alias, top_position = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    positioned = (top_position is not None and (
        top_position <= ROUTE_IDENTITY_HEADER
        or top_position >= ROUTE_IDENTITY_SIGNATURE))
    # Preselecting a dossier is a *claim*. Vocabulary alone is not enough: a
    # word shared with a dossier (an expense quoted mid-letter) is not identity.
    # Only a declared alias (anywhere) or the dossier name in the header or the
    # signature justifies a proposal.
    strong_identity = top_alias or positioned
    abstain = top_score < threshold
    reason = ""
    if abstain:
        reason = ("score insuffisant (" + str(top_score) + " < "
                  + str(threshold) + ") : / rattachement non proposé")
    elif not strong_identity:
        abstain = True
        where = ("mentionné en passant"
                 if top_position is not None else
                 "aucun nom ni alias dans l'en-tête ou la signature")
        reason = (where + " : / rattachement non proposé, choisissez le "
                  "dossier")
    elif second_score and top_score / second_score < margin:
        abstain = True
        reason = ("deux dossiers trop proches (" + str(top_score) + " / "
                  + str(second_score) + ") : / rattachement ambigu")
    return {
        "candidates": candidates,
        "abstain": abstain,
        "top": None if abstain else candidates[0]["project_id"],
        "reason": reason or "dossier le plus proche du texte",
        "threshold": threshold,
        "margin": margin,
    }
