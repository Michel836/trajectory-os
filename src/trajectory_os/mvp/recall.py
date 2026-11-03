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
K_SOURCE = "source"
K_SIGNAL = "signal"
K_CHANGE = "change"

KINDS = frozenset({K_PROJECT, K_TASK, K_DECISION, K_SOURCE, K_SIGNAL,
                   K_CHANGE})

#: Display order used when several kinds share the same score.
KIND_ORDER = {K_PROJECT: 0, K_TASK: 1, K_DECISION: 2, K_SOURCE: 3,
              K_SIGNAL: 4, K_CHANGE: 5}

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


def _signal_document(signal: signals.InboundSignal) -> RecallDocument:
    return RecallDocument(
        kind=K_SIGNAL,
        ref_id=signal.signal_id,
        title=signal.title,
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
        "next_actions": explanation["next_actions"][:MAX_CONTEXT_ITEMS],
        "blocked": blocked,
        "waiting": waiting,
        "risks": explanation["risks"][:MAX_CONTEXT_ITEMS],
        "recent_changes": explanation["recent_changes"][:MAX_CONTEXT_ITEMS],
        "signals": explanation["signals"][:MAX_CONTEXT_ITEMS],
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
    "DEFAULT_LIMIT",
    "KINDS",
    "K_CHANGE",
    "K_DECISION",
    "K_PROJECT",
    "K_SIGNAL",
    "K_SOURCE",
    "K_TASK",
    "MAX_CONTEXT_PROJECTS",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "RecallDocument",
    "RecallHit",
    "RecallIndex",
    "alias_map",
    "build_index",
    "cache_size",
    "clear_cache",
    "query",
    "resolve",
    "search",
    "stats",
]
