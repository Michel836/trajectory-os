"""MVP — document → structured portfolio import (semantic, hierarchical).

The import workflow has two strictly separated phases:

1. **Analyze / preview** — extract text, segment it into coherent semantic
   chunks, interpret each chunk with a local LLM (deterministic structural
   fallback when unavailable), build a semantic *hierarchy* (AREA → PROJECT →
   WORKSTREAM → WORK_PACKAGE → TASK → SUBTASK / DELIVERABLE), propose
   AI-generated execution structure, and detect duplicates against the live
   portfolio. Nothing is written in this phase.
2. **Confirm / merge** — apply the user's explicit decisions, flatten the
   reviewed hierarchy back onto the durable Project/Task model (reusing the
   existing ``workstream``/``deliverable``/``domain``/``dependencies`` fields),
   back up the previous document, write atomically, and persist a provenance
   artifact.

Trust rules: every value carries an evidence tag (FACT / INFERRED / SUGGESTED /
UNKNOWN); unknown stays unknown; AI-generated material is always marked
SUGGESTED and is never silently accepted; stale dates are never promoted to
deadlines; completion state, actual effort, urgency, impact and dependencies
are never invented.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib import error, request

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import document, import_engine, model, store

# --- semantic taxonomy --------------------------------------------------------

AREA = "AREA"
PROJECT = "PROJECT"
WORKSTREAM = "WORKSTREAM"
WORK_PACKAGE = "WORK_PACKAGE"
TASK = "TASK"
SUBTASK = "SUBTASK"
DELIVERABLE = "DELIVERABLE"
IDEA = "IDEA"
SOMEDAY = "SOMEDAY"
WAITING = "WAITING"
BLOCKER = "BLOCKER"
QUESTION = "QUESTION"
RESOURCE = "RESOURCE"
REFERENCE = "REFERENCE"

KINDS = frozenset({
    AREA, PROJECT, WORKSTREAM, WORK_PACKAGE, TASK, SUBTASK, DELIVERABLE,
    IDEA, SOMEDAY, WAITING, BLOCKER, QUESTION, RESOURCE, REFERENCE,
})

#: Kinds that become durable Project entities.
PROJECT_LIKE = frozenset({AREA, PROJECT, IDEA, SOMEDAY})
#: Kinds that become durable Task entities.
TASK_LIKE = frozenset({TASK, SUBTASK, WAITING, BLOCKER})
#: Kinds that are grouping labels (become workstream/deliverable fields).
GROUPING = frozenset({WORKSTREAM, WORK_PACKAGE, DELIVERABLE})
#: Kinds preserved as notes/references only.
NOTE_LIKE = frozenset({QUESTION, RESOURCE, REFERENCE})

# --- evidence -----------------------------------------------------------------

FACT = "FACT"
INFERRED = "INFERRED"
SUGGESTED = "SUGGESTED"
UNKNOWN = "UNKNOWN"
EVIDENCES = frozenset({FACT, INFERRED, SUGGESTED, UNKNOWN})

ACTION_IMPORT = "import"
ACTION_SKIP = "skip"
ACTIONS = frozenset({ACTION_IMPORT, ACTION_SKIP})

MAX_TITLE_LEN = model.MAX_STR_LEN
MAX_SOURCE_LEN = model.MAX_TEXT_LEN
CHUNK_TARGET_CHARS = int(
    os.environ.get("TRAJECTORY_MVP_CHUNK_CHARS", "1200"))
CHUNK_MAX_CHARS = CHUNK_TARGET_CHARS * 2

DEFAULT_LLM_MODEL = import_engine.LOCAL_MODEL
DEFAULT_OLLAMA_URL = import_engine.LOCAL_DEFAULT_URL

#: DeepSeek Flash (OpenAI-compatible) endpoint. The TrajectoryOS alias for the
#: Flash model is ``deepseek-flash``; Pro/reasoner variants are never used.
DEFAULT_DEEPSEEK_MODEL = import_engine.DEEPSEEK_FLASH_MODEL
DEFAULT_DEEPSEEK_URL = import_engine.DEEPSEEK_DEFAULT_URL

#: Import analysis modes.
#: ``factual`` (default) only extracts what the document states; ``semantic``
#: is the explicit, on-demand generative decomposition used for enrichment.
MODE_FACTUAL = "factual"
MODE_SEMANTIC = "semantic"
MODES = frozenset({MODE_FACTUAL, MODE_SEMANTIC})

#: The active import semantic engine is selected explicitly: local Ollama
#: (default) or DeepSeek Flash. There is no silent fallback.
IMPORT_PROVIDER_DEEPSEEK = import_engine.ENGINE_DEEPSEEK_FLASH
IMPORT_PROVIDER_OLLAMA = import_engine.ENGINE_LOCAL

# --- import progress ----------------------------------------------------------

#: Canonical pipeline stages, in order.
STAGE_QUEUED = "QUEUED"
STAGE_EXTRACTING = "EXTRACTING"
STAGE_CLASSIFYING = "CLASSIFYING"
STAGE_MATCHING = "MATCHING"
STAGE_CONSOLIDATING = "CONSOLIDATING"
STAGE_PREPARING_PREVIEW = "PREPARING_PREVIEW"
STAGE_COMPLETED = "COMPLETED"
STAGE_FAILED = "FAILED"
STAGE_CANCELLED = "CANCELLED"
STAGES = (
    STAGE_QUEUED, STAGE_EXTRACTING, STAGE_CLASSIFYING, STAGE_MATCHING,
    STAGE_CONSOLIDATING, STAGE_PREPARING_PREVIEW, STAGE_COMPLETED,
    STAGE_FAILED, STAGE_CANCELLED,
)


class ImportCancelled(Exception):
    """A cooperative cancel request arrived during analysis."""


@dataclass(frozen=True)
class ImportProgress:
    """A real backend progress snapshot (never a fake animation)."""

    stage: str
    message: str
    chunks_total: int = 0
    chunks_done: int = 0
    api_calls: int = 0
    fallback_count: int = 0

    @property
    def percent(self) -> int:
        if self.stage == STAGE_COMPLETED:
            return 100
        if self.stage == STAGE_CANCELLED:
            return 100
        if self.stage == STAGE_FAILED:
            return 100
        if self.stage in (STAGE_QUEUED, STAGE_EXTRACTING):
            return 2 if self.stage == STAGE_EXTRACTING else 0
        if self.stage == STAGE_CLASSIFYING:
            if self.chunks_total <= 0:
                return 20
            ratio = min(1.0, self.chunks_done / self.chunks_total)
            return 10 + int(ratio * 60)
        if self.stage == STAGE_MATCHING:
            return 80
        if self.stage == STAGE_CONSOLIDATING:
            return 90
        if self.stage == STAGE_PREPARING_PREVIEW:
            return 96
        return 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "message": self.message,
            "chunks_total": self.chunks_total,
            "chunks_done": self.chunks_done,
            "percent": self.percent,
            "api_calls": self.api_calls,
            "fallback_count": self.fallback_count,
        }

#: Curated merge hints (concept keyword -> existing project id). These only
#: strengthen *preview* merge suggestions; the user always confirms.
_MERGE_HINTS: dict[str, str] = {
    "inventory": "inventory-tool",
    "stock": "inventory-tool",
    "reorder": "inventory-tool",
    "garden": "garden-planner",
    "plot": "garden-planner",
    "volunteer": "garden-planner",
    "course": "training-application",
    "certificate": "training-application",
    "training": "training-application",
    "budget": "budget-tracker",
    "expense": "budget-tracker",
    "finance": "budget-tracker",
    "documentation": "docs-library",
    "docs": "docs-library",
    "index": "docs-library",
    "conference": "conference-talk",
    "talk": "conference-talk",
    "slides": "conference-talk",
    "crm": "crm-practice",
    "consulting": "crm-practice",
    "client": "crm-practice",
    "renewal": "admin-renewal",
    "permit": "admin-renewal",
    "form": "admin-renewal",
    "maintenance": "equipment-maintenance",
    "equipment": "equipment-maintenance",
    "roster": "community-roster",
    "community": "community-roster",
    "language": "language-study",
    "study": "language-study",
    "backup": "home-lab-backup",
    "restore": "home-lab-backup",
    "snapshot": "home-lab-backup",
}

_STOPWORDS = frozenset({
    "le", "la", "les", "de", "des", "du", "un", "une", "et", "ou", "a",
    "au", "aux", "en", "pour", "sur", "dans", "avec", "mon", "ma", "mes",
    "son", "sa", "ses", "ce", "cette", "ces", "que", "qui", "quoi",
    "est", "sont", "je", "j", "il", "elle", "nous", "vous", "ils",
    "elles", "plus", "pas", "ne", "se", "me", "te", "l", "d",
})


class PortfolioImportError(Exception):
    """A document cannot be semantically imported (fail closed)."""


@dataclass(frozen=True)
class DependencySuggestion:
    """One proposed dependency edge (always a suggestion until confirmed)."""

    source_title: str
    target_title: str
    rationale: str
    evidence: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_title": self.source_title,
            "target_title": self.target_title,
            "rationale": self.rationale,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ChildSuggestion:
    """One AI-proposed execution step (always SUGGESTED)."""

    title: str
    kind: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "kind": self.kind,
                "rationale": self.rationale}


@dataclass(frozen=True)
class ImportCandidate:
    """One structured, evidence-tagged item in the import hierarchy."""

    candidate_id: str
    kind: str
    title: str
    description: str
    source_text: str
    project_hint: str
    parent_id: str | None
    evidence: str
    merge_project_id: str | None
    suggested_status: str | None
    suggested_urgency: str | None
    suggested_impact: str | None
    suggested_effort_minutes: int | None
    confidence: float
    needs_review: bool
    suggested_dependencies: tuple[DependencySuggestion, ...]
    suggested_children: tuple[ChildSuggestion, ...]
    suggested_next_action: str
    prerequisites: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "source_text": self.source_text,
            "project_hint": self.project_hint,
            "parent_id": self.parent_id,
            "evidence": self.evidence,
            "merge_project_id": self.merge_project_id,
            "suggested_status": self.suggested_status,
            "suggested_urgency": self.suggested_urgency,
            "suggested_impact": self.suggested_impact,
            "suggested_effort_minutes": self.suggested_effort_minutes,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "suggested_dependencies": [
                d.to_dict() for d in self.suggested_dependencies],
            "suggested_children": [
                c.to_dict() for c in self.suggested_children],
            "suggested_next_action": self.suggested_next_action,
            "prerequisites": list(self.prerequisites),
        }


@dataclass(frozen=True)
class ImportMatch:
    """A likely duplicate between one candidate and an existing project."""

    candidate_id: str
    matched_project_id: str
    matched_project_name: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "matched_project_id": self.matched_project_id,
            "matched_project_name": self.matched_project_name,
            "score": self.score,
        }


@dataclass(frozen=True)
class ImportAnalysis:
    """The complete persisted preview (phase 1, read-only)."""

    source_name: str
    imported_at: str
    text_chars: int
    truncated: bool
    engine: str
    warning: str
    candidates: tuple[ImportCandidate, ...]
    matches: tuple[ImportMatch, ...]
    chunks: int = 0
    llm_calls: int = 0
    chunk_failures: int = 0
    total_llm_ms: int = 0
    mode: str = MODE_FACTUAL
    model: str = ""
    pricing_state: str = import_engine.PRICING_OFF_PEAK
    elapsed_ms: int = 0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    fallback_count: int = 0
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        match_map = {m.candidate_id: m for m in self.matches}
        return {
            "source_name": self.source_name,
            "imported_at": self.imported_at,
            "text_chars": self.text_chars,
            "truncated": self.truncated,
            "engine": self.engine,
            "mode": self.mode,
            "model": self.model,
            "pricing_state": self.pricing_state,
            "warning": self.warning,
            "chunks": self.chunks,
            "llm_calls": self.llm_calls,
            "chunk_failures": self.chunk_failures,
            "total_llm_ms": self.total_llm_ms,
            "elapsed_ms": self.elapsed_ms,
            "api_calls": self.api_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "fallback_count": self.fallback_count,
            "cancelled": self.cancelled,
            "candidates": [
                {**c.to_dict(),
                 "suggested_action": _suggested_action(c, match_map)}
                for c in self.candidates
            ],
            "matches": [m.to_dict() for m in self.matches],
            "hierarchy": _hierarchy(self.candidates),
            "summary": _summary(self),
            "completion_report": _completion_report(self),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ImportAnalysis:
        raw_candidates = data.get("candidates")
        raw_matches = data.get("matches")
        candidates = tuple(
            _candidate_from_dict(item)
            for item in (raw_candidates or []) if isinstance(item, Mapping)
        )
        matches = tuple(
            ImportMatch(
                candidate_id=_coerce_str(item.get("candidate_id"), 64),
                matched_project_id=_coerce_str(
                    item.get("matched_project_id"), 64),
                matched_project_name=_coerce_str(
                    item.get("matched_project_name"), MAX_TITLE_LEN),
                score=float(item.get("score", 0.0)),
            )
            for item in (raw_matches or []) if isinstance(item, Mapping)
        )
        return ImportAnalysis(
            source_name=str(data.get("source_name", "")),
            imported_at=str(data.get("imported_at", "")),
            text_chars=int(data.get("text_chars", 0) or 0),
            truncated=bool(data.get("truncated", False)),
            engine=str(data.get("engine", "fallback")),
            warning=str(data.get("warning", "")),
            chunks=int(data.get("chunks", 0) or 0),
            llm_calls=int(data.get("llm_calls", 0) or 0),
            chunk_failures=int(data.get("chunk_failures", 0) or 0),
            total_llm_ms=int(data.get("total_llm_ms", 0) or 0),
            mode=str(data.get("mode", MODE_FACTUAL)),
            model=str(data.get("model", "")),
            pricing_state=str(data.get(
                "pricing_state", import_engine.PRICING_OFF_PEAK)),
            elapsed_ms=int(data.get("elapsed_ms", 0) or 0),
            api_calls=int(data.get("api_calls", 0) or 0),
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            estimated_cost_usd=float(
                data.get("estimated_cost_usd", 0.0) or 0.0),
            fallback_count=int(data.get("fallback_count", 0) or 0),
            cancelled=bool(data.get("cancelled", False)),
            candidates=candidates,
            matches=matches,
        )


def _candidate_from_dict(item: Mapping[str, Any]) -> ImportCandidate:
    deps = tuple(
        DependencySuggestion(
            source_title=_coerce_str(d.get("source_title"), MAX_TITLE_LEN),
            target_title=_coerce_str(d.get("target_title"), MAX_TITLE_LEN),
            rationale=_coerce_str(d.get("rationale"), model.MAX_TEXT_LEN),
            evidence=_coerce_evidence(d.get("evidence"), SUGGESTED),
            confidence=_coerce_confidence(d.get("confidence")),
        )
        for d in (item.get("suggested_dependencies") or [])
        if isinstance(d, Mapping)
    )
    children = tuple(
        ChildSuggestion(
            title=str(c.get("title", "")),
            kind=str(c.get("kind", TASK)),
            rationale=str(c.get("rationale", "")),
        )
        for c in (item.get("suggested_children") or [])
        if isinstance(c, Mapping)
    )
    title = _coerce_str(item.get("title"), MAX_TITLE_LEN)
    source_text = _coerce_str(
        item.get("source_text"), MAX_SOURCE_LEN) or title
    return ImportCandidate(
        candidate_id=_coerce_str(item.get("candidate_id"), 64),
        kind=_coerce_str(item.get("kind"), 32),
        title=title,
        description=_coerce_str(item.get("description"), model.MAX_TEXT_LEN),
        source_text=source_text,
        project_hint=_coerce_str(item.get("project_hint"), MAX_TITLE_LEN),
        parent_id=_coerce_str(item.get("parent_id"), 64) or None,
        evidence=_coerce_evidence(item.get("evidence"), FACT),
        merge_project_id=_coerce_str(item.get("merge_project_id"), 64) or None,
        suggested_status=_coerce_enum(
            item.get("suggested_status"),
            model.PROJECT_STATUSES | model.TASK_STATUSES, None),
        suggested_urgency=_coerce_enum(
            item.get("suggested_urgency"), model.URGENCIES, None),
        suggested_impact=_coerce_enum(
            item.get("suggested_impact"), model.IMPACTS, None),
        suggested_effort_minutes=_coerce_int(
            item.get("suggested_effort_minutes"), 1, 1440),
        confidence=_coerce_confidence(item.get("confidence")),
        needs_review=bool(item.get("needs_review", False)),
        suggested_dependencies=deps,
        suggested_children=children,
        suggested_next_action=_coerce_str(
            item.get("suggested_next_action"), model.MAX_TEXT_LEN),
        prerequisites=_coerce_str_list(item.get("prerequisites"), 32),
    )


@dataclass(frozen=True)
class ImportDecision:
    """One user decision applied during phase 2."""

    candidate_id: str
    action: str
    merge_project_id: str | None = None
    project_id: str | None = None
    title: str | None = None
    description: str | None = None
    status: str | None = None
    urgency: str | None = None
    impact: str | None = None
    effort_minutes: int | None = None
    kind: str | None = None


# --- helpers ------------------------------------------------------------------


def _now() -> str:
    return intel_model.utc_now()


def _file_stamp() -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "-", _now())


def _slug(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("._-")
    cleaned = cleaned[:63].rstrip("._-") if cleaned else fallback
    if not cleaned or not cleaned[0].isalnum():
        cleaned = fallback
    return cleaned


def _unique_id(existing: set[str], base: str) -> str:
    candidate = base
    index = 2
    while candidate in existing:
        suffix = f"-{index}"
        candidate = base[:63 - len(suffix)] + suffix
        index += 1
    return candidate


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _significant_tokens(value: str) -> set[str]:
    return {token for token in value.split() if token not in _STOPWORDS}


def _coerce_str(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:maximum]


def _coerce_enum(value: object, allowed: frozenset[str],
                 default: str | None) -> str | None:
    if isinstance(value, str) and value in allowed:
        return value
    return default


def _coerce_evidence(value: object, default: str) -> str:
    if isinstance(value, str) and value in EVIDENCES:
        return value
    return default


def _coerce_int(value: object, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < minimum or value > maximum:
        return None
    return value


def _coerce_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.5
    number = float(value)
    return min(1.0, max(0.0, number))


def _coerce_str_list(value: object, maximum: int) -> tuple[str, ...]:
    """Coerce an optional list of free-text strings (bounded, fail closed)."""
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for entry in value:
        if isinstance(entry, str) and entry.strip():
            result.append(entry.strip()[:model.MAX_TEXT_LEN])
        if len(result) >= maximum:
            break
    return tuple(result)


# --- LLM clients -------------------------------------------------------------


class LocalLlm:
    """Minimal local Ollama ``/api/chat`` client (stdlib only).

    The local model is the **standard** import engine; DeepSeek Flash is an
    explicit opt-in alternative.
    """

    def __init__(self, *, model: str | None = None,
                 base_url: str | None = None, timeout: float = 180.0) -> None:
        self.model = model or os.environ.get(
            "TRAJECTORY_MVP_IMPORT_MODEL", DEFAULT_LLM_MODEL)
        self.base_url = (base_url or os.environ.get(
            "TRAJECTORY_OLLAMA_URL", DEFAULT_OLLAMA_URL)).rstrip("/")
        self.timeout = float(os.environ.get(
            "TRAJECTORY_MVP_LLM_TIMEOUT", timeout))

    def complete_json(self, system: str, user: str,
                      schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema if schema is not None else _LLM_SCHEMA,
            "options": {"temperature": 0},
        }
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        try:
            response = _post_json(f"{self.base_url}/api/chat", body,
                                  self.timeout)
        except (error.HTTPError, error.URLError, TimeoutError, OSError) as exc:
            raise PortfolioImportError(
                f"local LLM unavailable: {exc}") from exc
        return _parse_chat_json(response)


class DeepSeekLlm:
    """DeepSeek Flash client over the OpenAI-compatible
    ``/chat/completions`` endpoint (stdlib only). Requires
    ``DEEPSEEK_API_KEY`` (and optionally ``DEEPSEEK_BASE_URL``/
    ``TRAJECTORY_DSF_MODEL``).

    DeepSeek Pro (and any ``pro``/``reasoner`` variant) is never used.
    """

    def __init__(self, *, model: str | None = None,
                 base_url: str | None = None,
                 api_key: str | None = None,
                 timeout: float = 300.0) -> None:
        resolved = model or os.environ.get(
            "TRAJECTORY_DSF_MODEL", DEFAULT_DEEPSEEK_MODEL)
        for marker in import_engine.FORBIDDEN_MARKERS:
            if marker in resolved.lower():
                raise PortfolioImportError(
                    "DeepSeek Pro is not available in this workflow")
        self.model = resolved
        self.base_url = (base_url or os.environ.get(
            "DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_URL)).rstrip("/")
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.timeout = float(os.environ.get(
            "TRAJECTORY_MVP_LLM_TIMEOUT", timeout))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def complete_json(self, system: str, user: str,
                      schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise PortfolioImportError(
                "DeepSeek not configured (set DEEPSEEK_API_KEY)")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": 0,
            "response_format": {
                "type": "json_object",
                "json_schema": schema if schema is not None else _LLM_SCHEMA,
            },
        }
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/chat/completions", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                data = bytes(response.read())
        except error.HTTPError as exc:
            raise PortfolioImportError(
                f"DeepSeek HTTP error {exc.code}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise PortfolioImportError(
                f"DeepSeek transport error: {exc}") from exc
        return _parse_openai_chat_json(data)


def _parse_openai_chat_json(body: bytes) -> dict[str, Any]:
    try:
        outer = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortfolioImportError("DeepSeek response is not valid JSON") from exc
    if not isinstance(outer, dict):
        raise PortfolioImportError("DeepSeek response is not an object")
    choices = outer.get("choices")
    if not isinstance(choices, list) or not choices:
        raise PortfolioImportError("DeepSeek response missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise PortfolioImportError("DeepSeek response missing message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise PortfolioImportError("DeepSeek response content is empty")
    parsed = _parse_json_content(content)
    if not isinstance(parsed, dict):
        raise PortfolioImportError("DeepSeek content is not an object")
    usage = outer.get("usage")
    if isinstance(usage, Mapping):
        parsed.setdefault("_usage", _usage_from_openai(usage))
    return parsed


def _usage_from_openai(usage: Mapping[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": _coerce_usage_int(usage.get("prompt_tokens")),
        "output_tokens": _coerce_usage_int(usage.get("completion_tokens")),
    }


def _coerce_usage_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def make_import_llm(engine: str | None = None) -> (
        Callable[[str, str], Mapping[str, Any]] | None):
    """Resolve one **explicitly requested** import engine.

    Local Ollama is the default. DeepSeek Flash is only returned when the
    caller explicitly asks for it and an API key is configured. There is no
    silent fallback: a missing key raises :class:`PortfolioImportError`.
    """
    try:
        selected = import_engine.normalize_engine(
            import_engine.ENGINE_LOCAL if engine is None else engine)
    except import_engine.EngineError as exc:
        raise PortfolioImportError(str(exc)) from exc
    if selected == import_engine.ENGINE_DEEPSEEK_FLASH:
        llm = DeepSeekLlm()
        if not llm.configured:
            raise PortfolioImportError(
                "DeepSeek Flash selected but DEEPSEEK_API_KEY is not set")
        return llm.complete_json
    if selected == import_engine.ENGINE_LOCAL:
        if os.environ.get("TRAJECTORY_MVP_IMPORT_OFFLINE"):
            return None
        return LocalLlm().complete_json
    return None


def make_engine_llm(engine: str | None = None, *,
                    schema: Mapping[str, Any] | None = None) -> tuple[
        Callable[[str, str], Mapping[str, Any]] | None, str, str]:
    """Return ``(llm, engine_id, model)`` for an explicit selection.

    ``schema`` optionally overrides the JSON response schema sent to the
    selected engine. When it is omitted the importer schema (``_LLM_SCHEMA``)
    is used, so existing import callers are unchanged. The selection is
    honoured exactly: an unavailable engine fails closed with no fallback.
    """
    try:
        selected = import_engine.normalize_engine(
            import_engine.ENGINE_LOCAL if engine is None else engine)
    except import_engine.EngineError as exc:
        raise PortfolioImportError(str(exc)) from exc
    if selected == import_engine.ENGINE_FACTUAL:
        return None, selected, ""
    if selected == import_engine.ENGINE_LOCAL:
        model = os.environ.get("TRAJECTORY_MVP_IMPORT_MODEL", DEFAULT_LLM_MODEL)
        if os.environ.get("TRAJECTORY_MVP_IMPORT_OFFLINE"):
            return None, selected, model
        local = LocalLlm(model=model)
        return _bind_schema(local.complete_json, schema), selected, model
    llm = DeepSeekLlm()
    if not llm.configured:
        raise PortfolioImportError(
            "DeepSeek Flash selected but DEEPSEEK_API_KEY is not set")
    return _bind_schema(llm.complete_json, schema), selected, llm.model


def _bind_schema(
    complete: Callable[..., Mapping[str, Any]],
    schema: Mapping[str, Any] | None,
) -> Callable[[str, str], Mapping[str, Any]]:
    """Bind an optional response schema onto a completion callable.

    Without a schema the original bound method is returned untouched, which
    preserves importer behavior and introspection (e.g. ``__self__``).
    """
    if schema is None:
        return complete

    def _with_schema(system: str, user: str) -> Mapping[str, Any]:
        return complete(system, user, schema=schema)

    return _with_schema


def _post_json(url: str, body: bytes, timeout: float) -> bytes:
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with request.urlopen(req, timeout=timeout) as response:
        return bytes(response.read())


def _parse_chat_json(body: bytes) -> dict[str, Any]:
    try:
        outer = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortfolioImportError("LLM response is not valid JSON") from exc
    if not isinstance(outer, dict):
        raise PortfolioImportError("LLM response is not an object")
    message = outer.get("message")
    if not isinstance(message, dict):
        raise PortfolioImportError("LLM response missing message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise PortfolioImportError("LLM response content is empty")
    parsed = _parse_json_content(content)
    if not isinstance(parsed, dict):
        raise PortfolioImportError("LLM content is not an object")
    parsed.setdefault("_usage", {
        "input_tokens": _coerce_usage_int(outer.get("prompt_eval_count")),
        "output_tokens": _coerce_usage_int(outer.get("eval_count")),
    })
    return parsed


def _parse_json_content(content: str) -> object:
    stripped = content.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise PortfolioImportError("LLM content contains no JSON object")
    return json.loads(stripped[start:end + 1])


# --- LLM prompt + schema ------------------------------------------------------


_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(KINDS)},
                    "title": {"type": "string"},
                    "parent": {"type": ["string", "null"]},
                    "description": {"type": "string"},
                    "source_text": {"type": "string"},
                    "evidence": {"type": "string",
                                 "enum": sorted(EVIDENCES)},
                    "merge_project_id": {"type": ["string", "null"]},
                    "suggested_status": {
                        "type": ["string", "null"],
                        "enum": ["ACTIVE", "WAITING", "BLOCKED", "DEFERRED",
                                 "COMPLETED", "TODO", "IN_PROGRESS", None]},
                    "suggested_urgency": {
                        "type": ["string", "null"],
                        "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW", None]},
                    "suggested_impact": {
                        "type": ["string", "null"],
                        "enum": ["HIGH", "MEDIUM", "LOW", None]},
                    "suggested_effort_minutes": {
                        "type": ["integer", "null"]},
                    "confidence": {"type": "number"},
                    "needs_review": {"type": "boolean"},
                    "dependencies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string"},
                                "target": {"type": "string"},
                                "rationale": {"type": "string"},
                                "evidence": {"type": "string",
                                             "enum": sorted(EVIDENCES)},
                                "confidence": {"type": "number"},
                            },
                            "required": ["target"],
                        },
                    },
                    "suggested_next_action": {"type": "string"},
                    "prerequisites": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "suggested_children": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "kind": {"type": "string",
                                        "enum": sorted(KINDS)},
                                "rationale": {"type": "string"},
                            },
                            "required": ["title", "kind"],
                        },
                    },
                },
                "required": ["kind", "title", "source_text"],
            },
        },
    },
    "required": ["items"],
}

_SYSTEM_PROMPT = (
    "You convert one chunk of an unstructured personal brain-dump (often "
    "French) into a structured, execution-ready semantic hierarchy. The "
    "source may be flat; the result must NOT be. Your job is to turn vague "
    "intentions into an executable breakdown."
    "\n\nHIERARCHY (set 'parent' to the exact title of the containing "
    "item, or null for top-level): AREA > PROJECT > WORKSTREAM > "
    "WORK_PACKAGE > TASK > SUBTASK. DELIVERABLE marks a concrete output and "
    "may appear at any level. Only use PROJECT for a bounded, "
    "outcome-oriented effort; use AREA for a broad life domain, IDEA or "
    "SOMEDAY for vague future notions. Do NOT inflate every topic into a "
    "PROJECT, and do NOT mechanically create every level: a small project "
    "may be PROJECT > TASK(s) > DELIVERABLE, while a complex project should "
    "receive WORKSTREAM > WORK_PACKAGE > TASK > SUBTASK."
    "\n\nFor every meaningful PROJECT, answer: (1) the actual intended "
    "outcome (objective), (2) the major workstreams, (3) the work packages "
    "per workstream, (4) the concrete tasks, (5) useful subtasks, (6) the "
    "deliverables that indicate completion, (7) likely prerequisites, "
    "(8) dependencies (existing or proposed), (9) what this project unlocks, "
    "(10) the single most reasonable immediate next action. Put the next "
    "action in 'suggested_next_action' as a concrete imperative (e.g. "
    "'Inventory public GitHub repositories and mark each "
    "keep/private/archive'), never 'work on project' or 'continue research'. "
    "Put prerequisites in 'prerequisites'."
    "\n\nEVIDENCE: tag every item evidence=FACT (stated in source), "
    "INFERRED (strongly derived from context), or SUGGESTED (you propose "
    "missing structure). Every AI-added item (inferred workstreams, work "
    "packages, subtasks, deliverables, next actions, prerequisites) MUST be "
    "evidence=SUGGESTED AND needs_review=true. NEVER invent deadlines, "
    "completion status, actual effort, urgency, impact, or confirmed "
    "dependencies. Dependencies may only be proposed in 'dependencies' with "
    "source, target, rationale, evidence=SUGGESTED and a confidence between "
    "0 and 1. Old dates are context, NOT deadlines; mark stale actions "
    "needs_review=true."
    "\n\nPROVENANCE: every item MUST include source_text, a short "
    "(<=200 characters) exact or near-exact excerpt copied from the chunk "
    "that supports the item; never leave it empty and never invent it."
    "\n\nMERGE: set merge_project_id to an existing project id only when "
    "the item clearly belongs to that existing project; otherwise null."
    "\n\nRespond with ONLY the JSON object matching the schema."
)

_FACTUAL_SYSTEM_PROMPT = (
    "You extract ONLY the structure that is explicitly present in one chunk "
    "of a personal document (often French). Factual import is a mirror, not "
    "a planner: you never invent anything."
    "\n\nPROVENANCE (mandatory): every item MUST include source_text, a "
    "short (<=200 characters) exact or near-exact excerpt copied from the "
    "chunk that supports the item. Quote the source line; never paraphrase "
    "or invent evidence. If the item title is itself stated in the text, "
    "source_text may repeat the title, but it must never be empty."
    "\n\nReturn items that are directly stated in the text:"
    "\n- AREA: only a broad, durable life domain (e.g. Logistics, Finances, "
    "Community). A named project, initiative, grouping, tool or device is a "
    "PROJECT, never an AREA;"
    "\n- PROJECT: an explicit project/initiative the user names;"
    "\n- TASK: an explicit action/todo written as an action;"
    "\n- WORKSTREAM / WORK_PACKAGE / SUBTASK / DELIVERABLE: only when the "
    "document itself states that grouping;"
    "\n- IDEA / SOMEDAY / WAITING / BLOCKER / QUESTION / RESOURCE / "
    "REFERENCE: only when explicitly present."
    "\n\nSTRICT RULES:"
    "\n- evidence=FACT when the wording is stated verbatim; INFERRED only "
    "for an obvious structural label of stated content; UNKNOWN when the "
    "text does not say."
    "\n- NEVER use evidence=SUGGESTED during factual import."
    "\n- Do NOT create workstreams, work packages, subtasks, deliverables, "
    "dependencies, prerequisites, next actions, deadlines, effort, urgency "
    "or impact that the document does not contain. Leave "
    "suggested_next_action, prerequisites, dependencies and "
    "suggested_children empty."
    "\n- Set parent to the exact title of the containing item only when the "
    "document shows that hierarchy; otherwise null."
    "\n- Set merge_project_id only when the item clearly belongs to an "
    "existing project."
    "\n- A short flat document may legitimately produce very few items."
    "\n\nRespond with ONLY the JSON object matching the schema."
)


def _user_prompt(text: str, portfolio: model.Portfolio | None) -> str:
    projects = ""
    if portfolio is not None:
        projects = "\n".join(
            f"- {p.project_id}: {p.name}" for p in portfolio.projects)
    return (
        "Existing projects (id: name):\n" + (projects or "(none)") + "\n\n"
        "Document chunk:\n" + text
    )


# --- chunking -----------------------------------------------------------------


def _chunk_text(text: str) -> list[str]:
    """Segment extracted text into coherent chunks at topic boundaries.

    Boundaries are heading lines (``#`` or a short line ending with ``:``).
    A new chunk starts at a boundary once the current chunk has reached the
    target size; a hard cap guarantees bounded per-chunk context.
    """
    lines = text.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n".join(current).strip())
            current = []
            current_len = 0

    for line in lines:
        stripped = line.strip()
        is_boundary = (stripped.startswith("#")
                       or (stripped.endswith(":")
                           and not stripped.startswith("- ")
                           and len(stripped) < 120))
        if is_boundary and current_len >= CHUNK_TARGET_CHARS:
            flush()
        current.append(line)
        current_len += len(line) + 1
        if current_len >= CHUNK_MAX_CHARS:
            flush()
    flush()
    return chunks


# --- phase 1: analysis --------------------------------------------------------


def analyze_document(
    root: str | Path,
    filename: str,
    data: bytes,
    *,
    llm: Callable[[str, str], Mapping[str, Any]] | None = None,
    mode: str = MODE_FACTUAL,
    engine: str = "",
    model: str = "",
    pricing_state: str = import_engine.PRICING_OFF_PEAK,
    progress: Callable[[ImportProgress], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> ImportAnalysis:
    """Extract + segment + interpret one document (no portfolio writes).

    ``mode=factual`` (the default) extracts only what the document states and
    never fabricates workstreams, subtasks, deliverables, dependencies or next
    actions. ``mode=semantic`` is the explicit generative decomposition used
    on demand for enrichment, never for the initial factual import.
    """
    import time

    if mode not in MODES:
        raise PortfolioImportError(f"unknown import mode {mode!r}")
    started = time.monotonic()

    def emit(stage: str, message: str, *, chunks_total: int = 0,
             chunks_done: int = 0, api_calls: int = 0,
             fallback_count: int = 0) -> None:
        if progress is not None:
            progress(ImportProgress(
                stage=stage, message=message, chunks_total=chunks_total,
                chunks_done=chunks_done, api_calls=api_calls,
                fallback_count=fallback_count))

    def check_cancel() -> None:
        if cancel is not None and cancel():
            raise ImportCancelled()

    emit(STAGE_EXTRACTING, "Extracting document text")
    text = document.extract_text(filename, data)
    truncated = len(text) > CHUNK_MAX_CHARS * 64
    check_cancel()
    portfolio = store.load_portfolio(root)
    imported_at = _now()
    chunks = _chunk_text(text)

    resolved_engine = engine or (
        ("fallback" if mode == MODE_SEMANTIC else "factual") if llm is None
        else ("llm" if mode == MODE_SEMANTIC else "local"))
    system_prompt = (_FACTUAL_SYSTEM_PROMPT if mode == MODE_FACTUAL
                     else _SYSTEM_PROMPT)
    warning = ""
    items: list[dict[str, Any]] = []
    llm_calls = 0
    chunk_failures = 0
    fallback_count = 0
    total_llm_ms = 0
    input_tokens = 0
    output_tokens = 0
    last_error = ""

    emit(STAGE_CLASSIFYING, f"Classifying {len(chunks)} chunk(s)",
         chunks_total=len(chunks))
    if llm is not None:
        for index, chunk in enumerate(chunks, start=1):
            check_cancel()
            try:
                chunk_started = time.monotonic()
                raw = llm(system_prompt, _user_prompt(chunk, portfolio))
                total_llm_ms += int((time.monotonic() - chunk_started) * 1000)
                if mode == MODE_FACTUAL:
                    items.extend(_parse_llm_items_factual(raw))
                else:
                    items.extend(_parse_llm_items(raw))
                input_tokens += _usage_int(raw, "input_tokens")
                output_tokens += _usage_int(raw, "output_tokens")
                llm_calls += 1
            except PortfolioImportError as exc:
                chunk_failures += 1
                fallback_count += 1
                last_error = str(exc)
                items.extend(_factual_fallback_items(chunk))
            emit(STAGE_CLASSIFYING,
                 f"Classified chunk {index}/{len(chunks)}",
                 chunks_total=len(chunks), chunks_done=index,
                 api_calls=llm_calls, fallback_count=fallback_count)
        # Fail closed: an explicitly selected LLM engine that produced no
        # output on any chunk must never be silently replaced by the
        # deterministic fallback. Partial failure is allowed and reported.
        if chunks and llm_calls == 0:
            raise PortfolioImportError(
                f"selected engine {resolved_engine!r} failed on all "
                f"{len(chunks)} chunk(s) ({last_error or 'no output'}); "
                "refusing to substitute deterministic results, choose "
                "another engine")
        if fallback_count:
            warning = (
                f"{fallback_count} of {len(chunks)} chunk(s) used the "
                f"deterministic fallback; results may be incomplete "
                f"(last error: {last_error or 'unknown'})")
    else:
        for index, chunk in enumerate(chunks, start=1):
            check_cancel()
            if mode == MODE_SEMANTIC:
                items.extend(_fallback_items(chunk))
            else:
                items.extend(_factual_fallback_items(chunk))
            emit(STAGE_CLASSIFYING,
                 f"Classified chunk {index}/{len(chunks)}",
                 chunks_total=len(chunks), chunks_done=index)

    check_cancel()
    emit(STAGE_CONSOLIDATING, "Consolidating candidates",
         chunks_total=len(chunks), chunks_done=len(chunks),
         api_calls=llm_calls, fallback_count=fallback_count)
    candidates = _build_candidates(items)
    candidates = _consolidate(candidates)
    if mode == MODE_SEMANTIC:
        candidates = _assign_next_actions(candidates)
    else:
        candidates = _strip_speculative(candidates)
        candidates = _demote_merge_bound_areas(candidates)
        candidates = _apply_source_hierarchy(candidates, text)
    check_cancel()
    emit(STAGE_MATCHING, "Matching against the existing portfolio",
         chunks_total=len(chunks), chunks_done=len(chunks),
         api_calls=llm_calls, fallback_count=fallback_count)
    matches = detect_duplicates(candidates, portfolio)
    emit(STAGE_PREPARING_PREVIEW, "Preparing preview",
         chunks_total=len(chunks), chunks_done=len(chunks),
         api_calls=llm_calls, fallback_count=fallback_count)

    estimated_cost = import_engine.estimate_cost_usd(
        resolved_engine, input_tokens=input_tokens,
        output_tokens=output_tokens, pricing=pricing_state)
    return ImportAnalysis(
        source_name=filename,
        imported_at=imported_at,
        text_chars=len(text),
        truncated=truncated,
        engine=resolved_engine,
        warning=warning,
        candidates=candidates,
        matches=matches,
        chunks=len(chunks),
        llm_calls=llm_calls,
        chunk_failures=chunk_failures,
        total_llm_ms=total_llm_ms,
        mode=mode,
        model=model,
        pricing_state=pricing_state,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        api_calls=llm_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost,
        fallback_count=fallback_count,
    )


def _usage_int(raw: Mapping[str, Any], key: str) -> int:
    usage = raw.get("_usage")
    if not isinstance(usage, Mapping):
        return 0
    return _coerce_usage_int(usage.get(key))


def _parse_llm_items(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = raw.get("items")
    if not isinstance(value, list):
        raise PortfolioImportError("LLM result missing items array")
    return [item for item in value if isinstance(item, dict)]


def _parse_llm_items_factual(
        raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Keep only explicitly present items; drop every speculative field."""
    result: list[dict[str, Any]] = []
    for item in _parse_llm_items(raw):
        evidence = _coerce_evidence(item.get("evidence"), UNKNOWN)
        if evidence == SUGGESTED:
            continue
        sanitized = dict(item)
        for key in ("suggested_children", "dependencies",
                    "suggested_next_action", "prerequisites"):
            sanitized.pop(key, None)
        sanitized["evidence"] = evidence
        result.append(sanitized)
    return result


def _strip_speculative(
        candidates: tuple[ImportCandidate, ...]) -> tuple[ImportCandidate, ...]:
    """Belt-and-braces: factual preview never carries AI suggestions."""
    return tuple(
        c for c in candidates
        if c.evidence != SUGGESTED and not c.suggested_children
    )


# --- deterministic structural fallback (per chunk) ----------------------------


def _factual_fallback_items(text: str) -> list[dict[str, Any]]:
    """Extract only the structure explicitly present in the text.

    Headings become projects, list items become tasks of the current heading,
    and anything else becomes a reference. Nothing is invented and no
    speculative execution structure is generated.
    """
    items: list[dict[str, Any]] = []
    current_project: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            current_project = line.lstrip("#").strip().rstrip(":")
            if not current_project:
                continue
            items.append({
                "kind": PROJECT, "title": current_project, "parent": None,
                "source_text": line, "evidence": FACT,
                "needs_review": False, "confidence": 0.5,
            })
            continue
        if line.startswith(("- ", "* ")):
            body = line[2:].strip()
            if not body:
                continue
            if current_project:
                items.append({
                    "kind": TASK, "title": body, "parent": current_project,
                    "source_text": body, "evidence": FACT,
                    "needs_review": False, "confidence": 0.5,
                })
            else:
                items.append({
                    "kind": REFERENCE, "title": body[:200], "parent": None,
                    "source_text": body, "evidence": FACT,
                    "needs_review": True, "confidence": 0.3,
                })
            continue
        if line.endswith(":") and len(line) < 120:
            current_project = line.rstrip(":").strip()
            if not current_project:
                continue
            items.append({
                "kind": PROJECT, "title": current_project, "parent": None,
                "source_text": line, "evidence": FACT,
                "needs_review": False, "confidence": 0.5,
            })
            continue
        items.append({
            "kind": REFERENCE, "title": line[:200], "parent": None,
            "source_text": line, "evidence": FACT,
            "needs_review": True, "confidence": 0.2,
        })
    return items


def _fallback_items(text: str) -> list[dict[str, Any]]:
    """Crude-but-safe structural fallback; every item flagged for review."""
    items: list[dict[str, Any]] = []
    current_project: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            current_project = line.lstrip("#").strip().rstrip(":")
            items.append({
                "kind": PROJECT, "title": current_project, "parent": None,
                "source_text": line, "evidence": FACT, "needs_review": True,
                "confidence": 0.3,
            })
            continue
        if line.startswith("- "):
            body = line[2:].strip()
            if current_project:
                items.append({
                    "kind": TASK, "title": body, "parent": current_project,
                    "source_text": body, "evidence": FACT,
                    "needs_review": True, "confidence": 0.3,
                })
            else:
                items.append({
                    "kind": REFERENCE, "title": body, "parent": None,
                    "source_text": body, "evidence": FACT,
                    "needs_review": True, "confidence": 0.3,
                })
            continue
        if line.endswith(":") and len(line) < 120:
            current_project = line.rstrip(":").strip()
            items.append({
                "kind": PROJECT, "title": current_project, "parent": None,
                "source_text": line, "evidence": FACT, "needs_review": True,
                "confidence": 0.3,
            })
            continue
        items.append({
            "kind": REFERENCE, "title": line[:200], "parent": None,
            "source_text": line, "evidence": FACT, "needs_review": True,
            "confidence": 0.2,
        })
    return items


# --- candidate construction + consolidation ----------------------------------


def _build_candidates(items: Sequence[Mapping[str, Any]]) -> tuple[ImportCandidate, ...]:
    candidates: list[ImportCandidate] = []
    for index, item in enumerate(items):
        kind = _coerce_str(item.get("kind"), 32)
        title = _coerce_str(item.get("title"), MAX_TITLE_LEN)
        if kind not in KINDS or not title:
            continue
        parent_title = _coerce_str(item.get("parent"), MAX_TITLE_LEN)
        source_text = _coerce_str(item.get("source_text"), MAX_SOURCE_LEN)
        if not source_text:
            # Never lose provenance: fall back to the item's own title.
            source_text = title
        candidate = ImportCandidate(
            candidate_id=f"c{index:04d}",
            kind=kind,
            title=title,
            description=_coerce_str(item.get("description"),
                                    model.MAX_TEXT_LEN),
            source_text=source_text,
            project_hint=parent_title,
            parent_id=None,
            evidence=_coerce_evidence(item.get("evidence"), FACT),
            merge_project_id=_coerce_str(item.get("merge_project_id"), 64)
            or None,
            suggested_status=_coerce_enum(
                item.get("suggested_status"), model.PROJECT_STATUSES
                | model.TASK_STATUSES, None),
            suggested_urgency=_coerce_enum(
                item.get("suggested_urgency"), model.URGENCIES, None),
            suggested_impact=_coerce_enum(
                item.get("suggested_impact"), model.IMPACTS, None),
            suggested_effort_minutes=_coerce_int(
                item.get("suggested_effort_minutes"), 1, 1440),
            confidence=_coerce_confidence(item.get("confidence")),
            needs_review=bool(item.get("needs_review", False)),
            suggested_dependencies=_parse_dependencies(
                item.get("dependencies")),
            suggested_children=_parse_children(item.get("suggested_children")),
            suggested_next_action=_coerce_str(
                item.get("suggested_next_action"), model.MAX_TEXT_LEN),
            prerequisites=_coerce_str_list(item.get("prerequisites"), 32),
        )
        candidates.append(candidate)

    # Resolve parent titles -> candidate ids, and explode AI children.
    candidates = _link_parents(candidates)
    candidates = _explode_children(candidates)
    return tuple(candidates)


def _parse_dependencies(value: object) -> tuple[DependencySuggestion, ...]:
    if not isinstance(value, list):
        return ()
    result: list[DependencySuggestion] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        target = _coerce_str(entry.get("target"), MAX_TITLE_LEN)
        if not target:
            continue
        result.append(DependencySuggestion(
            source_title=_coerce_str(entry.get("source"), MAX_TITLE_LEN),
            target_title=target,
            rationale=_coerce_str(entry.get("rationale"),
                                  model.MAX_TEXT_LEN),
            evidence=_coerce_evidence(entry.get("evidence"), SUGGESTED),
            confidence=_coerce_confidence(entry.get("confidence")),
        ))
    return tuple(result)


def _parse_children(value: object) -> tuple[ChildSuggestion, ...]:
    if not isinstance(value, list):
        return ()
    result: list[ChildSuggestion] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        title = _coerce_str(entry.get("title"), MAX_TITLE_LEN)
        kind = _coerce_str(entry.get("kind"), 32)
        if not title or kind not in KINDS:
            continue
        result.append(ChildSuggestion(
            title=title,
            kind=kind,
            rationale=_coerce_str(entry.get("rationale"), model.MAX_TEXT_LEN),
        ))
    return tuple(result)


def _link_parents(candidates: list[ImportCandidate]) -> list[ImportCandidate]:
    by_title: dict[str, str] = {}
    for candidate in candidates:
        key = _normalize(candidate.title)
        if key and key not in by_title:
            by_title[key] = candidate.candidate_id
    linked: list[ImportCandidate] = []
    for candidate in candidates:
        parent_id = None
        if candidate.project_hint:
            key = _normalize(candidate.project_hint)
            parent_id = by_title.get(key)
        linked.append(replace(candidate, parent_id=parent_id))
    return linked


def _explode_children(candidates: list[ImportCandidate]) -> list[ImportCandidate]:
    result: list[ImportCandidate] = list(candidates)
    next_id = len(candidates)
    for candidate in candidates:
        for child in candidate.suggested_children:
            result.append(ImportCandidate(
                candidate_id=f"c{next_id:04d}",
                kind=child.kind,
                title=child.title,
                description=child.rationale,
                source_text=child.title,
                project_hint=candidate.title,
                parent_id=candidate.candidate_id,
                evidence=SUGGESTED,
                merge_project_id=None,
                suggested_status=None,
                suggested_urgency=None,
                suggested_impact=None,
                suggested_effort_minutes=None,
                confidence=0.5,
                needs_review=True,
                suggested_dependencies=(),
                suggested_children=(),
                suggested_next_action="",
                prerequisites=(),
            ))
            next_id += 1
    return result


def _consolidate(candidates: tuple[ImportCandidate, ...]) -> tuple[ImportCandidate, ...]:
    """Merge duplicate project-like candidates across chunks (no pseudo-dupes)."""
    rename: dict[str, str] = {}
    kept: dict[tuple[str, str], str] = {}
    result: list[ImportCandidate] = []
    for candidate in candidates:
        if candidate.kind in PROJECT_LIKE:
            key = (candidate.kind, _normalize(candidate.title))
            if key in kept:
                rename[candidate.candidate_id] = kept[key]
                continue
            kept[key] = candidate.candidate_id
        result.append(candidate)
    if not rename:
        return tuple(result)
    fixed: list[ImportCandidate] = []
    for candidate in result:
        parent = candidate.parent_id
        if parent in rename:
            parent = rename[parent]
        fixed.append(replace(candidate, parent_id=parent))
    return tuple(fixed)


#: Container kinds that may own source-derived children in factual mode.
_CONTAINER_KINDS = frozenset({
    AREA, PROJECT, WORKSTREAM, WORK_PACKAGE, IDEA, SOMEDAY, DELIVERABLE,
})
#: Leaf/work kinds that may be attached to a source heading.
_ATTACHABLE_KINDS = frozenset({
    TASK, SUBTASK, WAITING, BLOCKER, QUESTION, RESOURCE, REFERENCE,
    DELIVERABLE,
})
_LIST_PREFIXES = ("- ", "* ", "\u2022 ", "\u2013 ")


def _match_key(value: str) -> str:
    """Normalize a candidate/source string for conservative source matching.

    Trailing parenthetical qualifiers added by a model (e.g. ``Faire un RAG
    (Visiplus)``) are dropped so they still match the raw source line.
    """
    stripped = re.sub(r"\s*\([^)]*\)\s*", " ", value)
    return _normalize(stripped)


def _source_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Return explicit ``(heading, list items)`` sections from the source.

    Only unambiguous source structure is recognised: markdown/ODT headings
    (``#`` or a short line ending with ``:``) followed by bulleted list items
    (``-``, ``*``, ``•``). A heading without list items is ignored.
    """
    blocks: list[tuple[str, list[str]]] = []
    heading: str | None = None
    items: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if heading is not None:
                blocks.append((heading, items))
            heading = line.lstrip("#").strip().rstrip(":")
            items = []
            continue
        if (line.endswith(":") and len(line) < 120
                and not line.startswith(_LIST_PREFIXES)):
            if heading is not None:
                blocks.append((heading, items))
            heading = line.rstrip(":").strip()
            items = []
            continue
        for prefix in _LIST_PREFIXES:
            if line.startswith(prefix):
                body = line[len(prefix):].strip()
                if body:
                    items.append(body)
                break
    if heading is not None:
        blocks.append((heading, items))
    return [(h, its) for h, its in blocks if h and its]


def _demote_merge_bound_areas(
        candidates: tuple[ImportCandidate, ...]) -> tuple[ImportCandidate, ...]:
    """Reclassify an existing-project-bound AREA as PROJECT.

    Narrow correction for factual over-classification: an item the model has
    already linked to an existing project (``merge_project_id``) is
    project-scoped, not a new broad life area. This never creates a project;
    it only corrects the kind of an already-present candidate.
    """
    result: list[ImportCandidate] = []
    for candidate in candidates:
        if (candidate.kind == AREA and candidate.merge_project_id
                and candidate.parent_id is None):
            result.append(replace(candidate, kind=PROJECT))
        else:
            result.append(candidate)
    return tuple(result)


def _apply_source_hierarchy(
        candidates: tuple[ImportCandidate, ...],
        text: str) -> tuple[ImportCandidate, ...]:
    """Attach existing leaf candidates to their explicit source heading.

    Conservative, deterministic post-pass: when the source document clearly
    lists an item under a heading, and the candidate carries a matching title
    or ``source_text`` excerpt, the candidate is re-parented under the
    heading candidate. It never creates new candidates (no speculative WBS),
    never overrides an explicit hierarchy, and never guesses when the textual
    evidence is weak.
    """
    blocks = _source_blocks(text)
    if not blocks:
        return candidates
    by_id = {c.candidate_id: c for c in candidates}
    containers: list[tuple[str, str]] = []
    leaves: list[tuple[str, str]] = []
    for candidate in candidates:
        for key in {_match_key(candidate.title),
                    _match_key(candidate.source_text)}:
            if not key:
                continue
            if candidate.kind in _CONTAINER_KINDS:
                containers.append((key, candidate.candidate_id))
            if candidate.kind in _ATTACHABLE_KINDS:
                leaves.append((key, candidate.candidate_id))

    updates: dict[str, str] = {}
    for heading, items in blocks:
        heading_key = _match_key(heading)
        parent_id = _match_exact(containers, heading_key) or _match_contains(
            containers, heading_key)
        if parent_id is None:
            continue
        for item in items:
            item_key = _match_key(item)
            if not item_key:
                continue
            for candidate_id in _match_all(leaves, item_key):
                if candidate_id == parent_id:
                    continue
                leaf = by_id.get(candidate_id)
                if leaf is None or leaf.parent_id:
                    continue
                updates.setdefault(candidate_id, parent_id)
    if not updates:
        return candidates
    return tuple(
        replace(c, parent_id=updates[c.candidate_id])
        if c.candidate_id in updates else c
        for c in candidates)


def _match_exact(entries: Sequence[tuple[str, str]],
                 key: str) -> str | None:
    for candidate_key, candidate_id in entries:
        if candidate_key == key:
            return candidate_id
    return None


def _match_contains(entries: Sequence[tuple[str, str]],
                    key: str) -> str | None:
    for candidate_key, candidate_id in entries:
        if _is_close_match(candidate_key, key):
            return candidate_id
    return None


def _match_all(entries: Sequence[tuple[str, str]], key: str) -> list[str]:
    exact = [cid for ckey, cid in entries if ckey == key]
    if exact:
        return exact
    return [cid for ckey, cid in entries if _is_close_match(ckey, key)]


def _is_close_match(a: str, b: str, *, ratio: float = 0.7) -> bool:
    """Containment match guarded by a length ratio to avoid loose attaches."""
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter not in longer:
        return False
    return len(shorter) >= ratio * len(longer)


def _assign_next_actions(
        candidates: tuple[ImportCandidate, ...]) -> tuple[ImportCandidate, ...]:
    """Deterministic safety net: give every executable project a concrete
    immediate next action when the LLM did not propose one.

    The next action is derived from the first task-like descendant (the
    natural entry point) so it names a real, concrete step; it never invents
    effort, dates or any other fact.
    """
    by_id = {c.candidate_id: c for c in candidates}
    children: dict[str | None, list[ImportCandidate]] = {}
    for candidate in candidates:
        key = candidate.parent_id if candidate.parent_id in by_id else None
        children.setdefault(key, []).append(candidate)

    def first_task_title(candidate_id: str) -> str:
        for child in _walk(children, candidate_id):
            if child.kind in TASK_LIKE:
                return child.title
        return ""

    result: list[ImportCandidate] = []
    for candidate in candidates:
        if candidate.kind == PROJECT and not candidate.suggested_next_action:
            entry = first_task_title(candidate.candidate_id)
            if entry:
                result.append(replace(
                    candidate,
                    suggested_next_action=f"Start with: {entry}"))
                continue
        result.append(candidate)
    return tuple(result)


# --- duplicate detection ------------------------------------------------------


def detect_duplicates(
    candidates: Sequence[ImportCandidate],
    portfolio: model.Portfolio | None,
) -> tuple[ImportMatch, ...]:
    """Match project-like candidates against existing projects."""
    if portfolio is None:
        return ()
    projects = {p.project_id: p for p in portfolio.projects}
    matches: list[ImportMatch] = []
    for candidate in candidates:
        if candidate.kind not in PROJECT_LIKE:
            continue
        best: tuple[str, str, float] | None = None
        if candidate.merge_project_id in projects:
            best = (candidate.merge_project_id,
                    projects[candidate.merge_project_id].name, 1.0)
        else:
            best = _best_project_match(candidate.title, projects)
        if best is not None and best[2] >= 0.5:
            matches.append(ImportMatch(
                candidate_id=candidate.candidate_id,
                matched_project_id=best[0],
                matched_project_name=best[1],
                score=round(best[2], 3),
            ))
    return tuple(matches)


def _best_project_match(
    title: str,
    projects: Mapping[str, model.Project],
) -> tuple[str, str, float] | None:
    normalized = _normalize(title)
    if not normalized:
        return None
    hint = _hint_match(normalized, projects)
    if hint is not None:
        return hint
    best: tuple[str, str, float] | None = None
    for project_id, project in projects.items():
        score = _similarity(normalized, _normalize(project.name))
        if score >= 0.5 and (best is None or score > best[2]):
            best = (project_id, project.name, score)
    return best


def _hint_match(
    normalized: str,
    projects: Mapping[str, model.Project],
) -> tuple[str, str, float] | None:
    for keyword, project_id in _MERGE_HINTS.items():
        if project_id not in projects:
            continue
        if keyword in normalized:
            return (project_id, projects[project_id].name, 0.95)
    return None


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    a_tokens = _significant_tokens(a)
    b_tokens = _significant_tokens(b)
    if not a_tokens or not b_tokens:
        return 0.0
    if a_tokens <= b_tokens or b_tokens <= a_tokens:
        return 0.7
    overlap = len(a_tokens & b_tokens)
    return overlap / max(len(a_tokens), len(b_tokens))


# --- public matching helpers --------------------------------------------------
#
# Quick Capture (``trajectory_os.mvp.capture``) needs exactly the same
# deterministic matching semantics as the document importer. These thin
# wrappers keep one source of truth instead of duplicating the algorithm.


def normalize_text(value: str) -> str:
    """Return the accent/punctuation-insensitive matching form of ``value``."""
    return _normalize(value)


def text_similarity(a: str, b: str) -> float:
    """Deterministic similarity score in ``[0, 1]`` between two labels."""
    return _similarity(a, b)


def best_project_match(
    title: str,
    projects: Mapping[str, model.Project],
) -> tuple[str, str, float] | None:
    """Best existing-project match for ``title`` (hint or similarity).

    Returns ``(project_id, project_name, score)`` or ``None`` when nothing is
    close enough. This is a read-only projection; it never mutates anything.
    """
    return _best_project_match(title, projects)


# --- hierarchy projection (preview) -------------------------------------------


def _hierarchy(candidates: tuple[ImportCandidate, ...]) -> dict[str, Any]:
    by_id = {c.candidate_id: c for c in candidates}
    children: dict[str | None, list[ImportCandidate]] = {}
    for candidate in candidates:
        key = candidate.parent_id if candidate.parent_id in by_id else None
        children.setdefault(key, []).append(candidate)

    def node(candidate: ImportCandidate) -> dict[str, Any]:
        payload = candidate.to_dict()
        payload["children"] = [
            node(child) for child in sorted(
                children.get(candidate.candidate_id, ()),
                key=lambda c: c.candidate_id)
        ]
        return payload

    roots = sorted(children.get(None, ()), key=lambda c: c.candidate_id)
    return {"roots": [node(root) for root in roots]}


# --- actions + summary --------------------------------------------------------


def _suggested_action(candidate: ImportCandidate,
                      matches: Mapping[str, ImportMatch]) -> str:
    if candidate.kind in NOTE_LIKE:
        return ACTION_SKIP
    if candidate.evidence == SUGGESTED:
        return ACTION_SKIP  # AI suggestions are never silently accepted
    if candidate.kind in PROJECT_LIKE:
        if candidate.candidate_id in matches:
            return "merge"
        if candidate.kind in (IDEA, SOMEDAY):
            return ACTION_SKIP
        return ACTION_IMPORT
    return ACTION_IMPORT  # task-like


def _summary(analysis: ImportAnalysis) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for candidate in analysis.candidates:
        by_kind[candidate.kind] = by_kind.get(candidate.kind, 0) + 1
    match_map = {m.candidate_id: m for m in analysis.matches}
    actions: dict[str, int] = {}
    suggested = 0
    for candidate in analysis.candidates:
        if candidate.evidence == SUGGESTED:
            suggested += 1
        action = _suggested_action(candidate, match_map)
        actions[action] = actions.get(action, 0) + 1
    return {
        "items": len(analysis.candidates),
        "by_kind": by_kind,
        "areas": by_kind.get(AREA, 0),
        "projects": by_kind.get(PROJECT, 0),
        "workstreams": by_kind.get(WORKSTREAM, 0),
        "work_packages": by_kind.get(WORK_PACKAGE, 0),
        "tasks": by_kind.get(TASK, 0),
        "subtasks": by_kind.get(SUBTASK, 0),
        "deliverables": by_kind.get(DELIVERABLE, 0),
        "ideas": by_kind.get(IDEA, 0),
        "someday": by_kind.get(SOMEDAY, 0),
        "waiting": by_kind.get(WAITING, 0),
        "blocked": by_kind.get(BLOCKER, 0),
        "questions": by_kind.get(QUESTION, 0),
        "resources": by_kind.get(RESOURCE, 0),
        "references": by_kind.get(REFERENCE, 0),
        "merge_candidates": len(analysis.matches),
        "likely_merges": len(analysis.matches),
        "needs_review": sum(1 for c in analysis.candidates if c.needs_review),
        "requiring_review": sum(
            1 for c in analysis.candidates if c.needs_review),
        "factual_items": sum(
            1 for c in analysis.candidates
            if c.evidence in (FACT, INFERRED, UNKNOWN)),
        "ai_suggested": suggested,
        "next_actions": sum(1 for c in analysis.candidates
                            if c.suggested_next_action),
        "with_prerequisites": sum(1 for c in analysis.candidates
                                  if c.prerequisites),
        "suggested": actions,
    }


def _completion_report(analysis: ImportAnalysis) -> dict[str, Any]:
    """The completion report shown when an import finishes."""
    summary = _summary(analysis)
    return {
        "engine": analysis.engine,
        "mode": analysis.mode,
        "model": analysis.model,
        "pricing_state": analysis.pricing_state,
        "elapsed_ms": analysis.elapsed_ms,
        "chunks": analysis.chunks,
        "api_calls": analysis.api_calls,
        "input_tokens": analysis.input_tokens,
        "output_tokens": analysis.output_tokens,
        "estimated_cost_usd": analysis.estimated_cost_usd,
        "fallback_count": analysis.fallback_count,
        "factual_items": summary["factual_items"],
        "likely_merges": summary["likely_merges"],
        "requiring_review": summary["requiring_review"],
        "ai_generated_items": summary["ai_suggested"],
        "items": summary["items"],
    }


# --- decisions ----------------------------------------------------------------


def parse_decisions(raw: Sequence[Mapping[str, Any]]) -> tuple[ImportDecision, ...]:
    """Validate and coerce raw user decisions (fail closed)."""
    decisions: list[ImportDecision] = []
    for entry in raw:
        candidate_id = _coerce_str(entry.get("candidate_id"), 64)
        action = _coerce_str(entry.get("action"), 16)
        if not candidate_id or action not in ACTIONS:
            raise PortfolioImportError(
                f"invalid decision: {candidate_id!r}/{action!r}")
        decisions.append(ImportDecision(
            candidate_id=candidate_id,
            action=action,
            merge_project_id=_coerce_str(entry.get("merge_project_id"), 64)
            or None,
            project_id=_coerce_str(entry.get("project_id"), 64) or None,
            title=_coerce_str(entry.get("title"), MAX_TITLE_LEN) or None,
            description=_coerce_str(entry.get("description"),
                                    model.MAX_TEXT_LEN) or None,
            status=_coerce_enum(entry.get("status"), model.PROJECT_STATUSES
                                | model.TASK_STATUSES, None),
            urgency=_coerce_enum(entry.get("urgency"), model.URGENCIES, None),
            impact=_coerce_enum(entry.get("impact"), model.IMPACTS, None),
            effort_minutes=_coerce_int(entry.get("effort_minutes"), 1, 1440),
            kind=_coerce_str(entry.get("kind"), 32) or None,
        ))
    return tuple(decisions)


# --- phase 2: flatten hierarchy to durable model ------------------------------


def apply_decisions(
    portfolio: model.Portfolio,
    analysis: ImportAnalysis,
    decisions: Sequence[ImportDecision],
) -> tuple[model.Portfolio, dict[str, str]]:
    """Apply decisions by flattening the reviewed hierarchy onto the model."""
    by_id = {c.candidate_id: c for c in analysis.candidates}
    decision_map: dict[str, ImportDecision] = {}
    for raw_decision in decisions:
        if raw_decision.candidate_id in decision_map:
            raise PortfolioImportError(
                f"duplicate decision for {raw_decision.candidate_id}")
        if raw_decision.candidate_id not in by_id:
            raise PortfolioImportError(
                f"unknown candidate {raw_decision.candidate_id}")
        decision_map[raw_decision.candidate_id] = raw_decision

    children: dict[str | None, list[ImportCandidate]] = {}
    for candidate in analysis.candidates:
        key = candidate.parent_id if candidate.parent_id in by_id else None
        children.setdefault(key, []).append(candidate)

    imported_ids = _imported_closure(by_id, children, decision_map)

    projects = list(portfolio.projects)
    tasks = list(portfolio.tasks)
    project_ids = {p.project_id for p in portfolio.projects}
    task_ids = {t.task_id for t in portfolio.tasks}
    title_to_project: dict[str, str] = {
        _normalize(p.name): p.project_id for p in portfolio.projects}
    title_to_project.update({p.project_id: p.project_id
                             for p in portfolio.projects})
    title_to_task: dict[str, str] = {}
    candidate_to_project: dict[str, str] = {}
    entity_map: dict[str, str] = {
        c.candidate_id: "skipped" for c in analysis.candidates}
    parent_of = _parent_map(children)
    next_action_by_project: dict[str, str] = {}
    emitted_project_tasks: set[str] = set()

    # Pass 1: create projects for top-level / project-like candidates.
    for candidate in _walk(children, None):
        if candidate.candidate_id not in imported_ids:
            continue
        kind = _effective_kind(candidate, decision_map)
        if kind not in PROJECT_LIKE:
            continue
        decision = decision_map.get(candidate.candidate_id)
        title = (decision.title if decision and decision.title
                 else candidate.title)
        merge_target = (decision.merge_project_id if decision
                        else None) or candidate.merge_project_id
        if merge_target:
            if merge_target not in project_ids:
                raise PortfolioImportError(
                    f"merge target {merge_target!r} does not exist")
            title_to_project[_normalize(candidate.title)] = merge_target
            title_to_project[_normalize(title)] = merge_target
            candidate_to_project[candidate.candidate_id] = merge_target
            entity_map[candidate.candidate_id] = f"merged:{merge_target}"
            if candidate.suggested_next_action:
                next_action_by_project.setdefault(
                    merge_target, candidate.suggested_next_action)
            continue
        # A pure AREA with project-like children becomes the domain of its
        # children rather than a project of its own (prevents inflation).
        if kind == AREA and any(
                _effective_kind(ch, decision_map) in PROJECT_LIKE
                for ch in children.get(candidate.candidate_id, ())):
            candidate_to_project[candidate.candidate_id] = ""
            entity_map[candidate.candidate_id] = "area"
            continue
        project_id = _unique_id(project_ids, _slug(title, "project"))
        status = _project_status(candidate, decision)
        domain = _domain_of(candidate, children)
        project = model.Project(
            project_id=project_id,
            name=title,
            objective=_objective(candidate, decision),
            status=status,
            domain=domain,
            urgency=(decision.urgency if decision else None) or model.U_MEDIUM,
            impact=(decision.impact if decision else None) or model.I_MEDIUM,
            description=_provenance_note(analysis),
        )
        projects.append(project)
        project_ids.add(project_id)
        title_to_project[_normalize(candidate.title)] = project_id
        title_to_project[_normalize(title)] = project_id
        candidate_to_project[candidate.candidate_id] = project_id
        entity_map[candidate.candidate_id] = project_id
        if candidate.suggested_next_action:
            next_action_by_project[project_id] = candidate.suggested_next_action

    # Pass 2: create tasks, inheriting workstream/deliverable from ancestors.
    for candidate in _walk(children, None):
        if candidate.candidate_id not in imported_ids:
            continue
        kind = _effective_kind(candidate, decision_map)
        if kind not in TASK_LIKE:
            continue
        task_decision = decision_map.get(candidate.candidate_id)
        title = (task_decision.title if task_decision and task_decision.title
                 else candidate.title)
        project_id = _resolve_project(candidate, task_decision, children,
                                      title_to_project,
                                      candidate_to_project, parent_of)
        workstream, deliverable = _grouping_of(candidate, children)
        status = _task_status(candidate, task_decision)
        next_action = candidate.suggested_next_action
        if not next_action and project_id not in emitted_project_tasks:
            next_action = next_action_by_project.get(project_id, "")
        emitted_project_tasks.add(project_id)
        if not next_action:
            next_action = candidate.description or candidate.source_text[:200]
        task = model.Task(
            task_id=_unique_id(task_ids, _slug(title, "task")),
            project_id=project_id,
            title=title,
            description=_provenance_note(analysis),
            workstream=workstream,
            deliverable=deliverable,
            status=status,
            estimated_minutes=(task_decision.effort_minutes if task_decision else None),
            urgency=(task_decision.urgency if task_decision else None) or model.U_MEDIUM,
            impact=(task_decision.impact if task_decision else None) or model.I_MEDIUM,
            next_action=next_action,
            created_at=analysis.imported_at,
            updated_at=analysis.imported_at,
        )
        tasks.append(task)
        task_ids.add(task.task_id)
        title_to_task[_normalize(candidate.title)] = task.task_id
        entity_map[candidate.candidate_id] = task.task_id

    # Add subtask dependencies (subtask depends on its parent task).
    new_tasks = list(tasks)
    for candidate in _walk(children, None):
        if candidate.candidate_id not in imported_ids:
            continue
        kind = _effective_kind(candidate, decision_map)
        if kind != SUBTASK:
            continue
        parent_id = candidate.parent_id
        parent_task = title_to_task.get(_normalize(by_id[parent_id].title)) \
            if parent_id and parent_id in by_id else None
        task_id = entity_map[candidate.candidate_id]
        if task_id in task_ids and parent_task:
            idx = next(i for i, t in enumerate(new_tasks)
                       if t.task_id == task_id)
            current = new_tasks[idx]
            if parent_task not in current.dependencies \
                    and len(current.dependencies) < model.MAX_DEPENDENCIES_PER_TASK:
                new_tasks[idx] = replace(
                    current, dependencies=current.dependencies + (parent_task,))

    new_portfolio = replace(
        portfolio,
        projects=tuple(projects),
        tasks=tuple(new_tasks),
        updated_at=analysis.imported_at,
    ).validate()
    return new_portfolio, entity_map


def _effective_kind(candidate: ImportCandidate,
                    decision_map: Mapping[str, ImportDecision]) -> str:
    decision = decision_map.get(candidate.candidate_id)
    if decision and decision.kind and decision.kind in KINDS:
        return decision.kind
    return candidate.kind


def _imported_closure(
    by_id: Mapping[str, ImportCandidate],
    children: Mapping[str | None, list[ImportCandidate]],
    decision_map: Mapping[str, ImportDecision],
) -> set[str]:
    imported = {c.candidate_id for c in by_id.values()
                if decision_map.get(c.candidate_id)
                and decision_map[c.candidate_id].action == ACTION_IMPORT}
    # A skipped parent implicitly skips its descendants.
    result: set[str] = set()

    def visit(candidate: ImportCandidate) -> None:
        if candidate.candidate_id not in imported:
            return
        result.add(candidate.candidate_id)
        for child in children.get(candidate.candidate_id, ()):
            visit(child)

    for root in children.get(None, ()):
        visit(root)
    return result


def _walk(children: Mapping[str | None, list[ImportCandidate]],
          start: str | None) -> list[ImportCandidate]:
    result: list[ImportCandidate] = []
    for candidate in sorted(children.get(start, ()),
                            key=lambda c: c.candidate_id):
        result.append(candidate)
        result.extend(_walk(children, candidate.candidate_id))
    return result


def _resolve_project(
    candidate: ImportCandidate,
    decision: ImportDecision | None,
    children: Mapping[str | None, list[ImportCandidate]],
    title_to_project: Mapping[str, str],
    candidate_to_project: Mapping[str, str],
    parent_of: Mapping[str, str | None],
) -> str:
    if decision and decision.project_id:
        return decision.project_id
    if candidate.merge_project_id:
        return candidate.merge_project_id
    # Walk up the full ancestor chain to the nearest project/area.
    node: str | None = candidate.parent_id
    while node:
        if node in candidate_to_project and candidate_to_project[node]:
            return candidate_to_project[node]
        node = parent_of.get(node)
    # Fall back to an explicit project hint (LLM- or user-supplied).
    if candidate.project_hint:
        key = _normalize(candidate.project_hint)
        if key in title_to_project:
            return title_to_project[key]
    raise PortfolioImportError(
        f"cannot determine project for {candidate.candidate_id} "
        f"{candidate.title!r}; provide project_id")


def _parent_map(children: Mapping[str | None, list[ImportCandidate]]) \
        -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for parent_id, kids in children.items():
        for kid in kids:
            result[kid.candidate_id] = parent_id
    return result


def _ancestors(
    candidate: ImportCandidate,
    children: Mapping[str | None, list[ImportCandidate]],
) -> list[ImportCandidate]:
    """Return the candidate's ancestors nearest-first (via parent_id)."""
    by_id = {c.candidate_id: c for kids in children.values() for c in kids}
    result: list[ImportCandidate] = []
    node_id = candidate.parent_id
    while node_id and node_id in by_id:
        ancestor = by_id[node_id]
        result.append(ancestor)
        node_id = ancestor.parent_id
    return result


def _grouping_of(candidate: ImportCandidate,
                 children: Mapping[str | None, list[ImportCandidate]]
                 ) -> tuple[str, str]:
    workstream = ""
    deliverable = ""
    for ancestor in _ancestors(candidate, children):
        if ancestor.kind == WORKSTREAM and not workstream:
            workstream = ancestor.title
        elif ancestor.kind in (WORK_PACKAGE, DELIVERABLE) and not deliverable:
            deliverable = ancestor.title
    return workstream, deliverable


def _domain_of(candidate: ImportCandidate,
               children: Mapping[str | None, list[ImportCandidate]]) -> str:
    if candidate.kind == AREA:
        return candidate.title
    for ancestor in _ancestors(candidate, children):
        if ancestor.kind == AREA:
            return ancestor.title
    return "personal"


def _project_status(candidate: ImportCandidate,
                    decision: ImportDecision | None) -> str:
    if decision and decision.status in model.PROJECT_STATUSES:
        return str(decision.status)
    suggested = candidate.suggested_status
    if suggested in model.PROJECT_STATUSES and suggested != model.PS_COMPLETED:
        return suggested
    return model.PS_DEFERRED  # conservative: new projects start deferred


def _task_status(candidate: ImportCandidate,
                 decision: ImportDecision | None) -> str:
    if decision and decision.status in model.TASK_STATUSES:
        return str(decision.status)
    if candidate.kind == WAITING:
        return model.TS_WAITING
    if candidate.kind == BLOCKER:
        return model.TS_BLOCKED
    return model.TS_TODO


def _objective(candidate: ImportCandidate,
               decision: ImportDecision | None) -> str:
    return (decision.description if decision and decision.description is not None
            else candidate.description) or candidate.source_text[:200]


def _provenance_note(analysis: ImportAnalysis) -> str:
    return f"Imported {analysis.imported_at[:10]} from {analysis.source_name}."


# --- confirm / persistence ----------------------------------------------------


def confirm_import(
    root: str | Path,
    analysis: ImportAnalysis,
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply confirmed decisions: back up, atomically save, persist artifact."""
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    parsed = parse_decisions(decisions)
    new_portfolio, entity_map = apply_decisions(portfolio, analysis, parsed)

    backup_path = default_backup_path(root)
    intel_model.write_json(backup_path, portfolio.to_dict())

    store.save_portfolio(root, new_portfolio)
    artifact_path = write_import_artifact(root, analysis, parsed, entity_map)

    return {
        "status": "OK",
        "imported": _count_imported(analysis, entity_map),
        "merged": _count_prefix(entity_map, "merged:"),
        "skipped": _count_value(entity_map, "skipped"),
        "created_projects": _count_created(analysis, entity_map, True),
        "created_tasks": _count_created(analysis, entity_map, False),
        "backup": str(backup_path),
        "artifact": str(artifact_path),
        "entity_map": dict(entity_map),
    }


def _count_imported(analysis: ImportAnalysis,
                    entity_map: Mapping[str, str]) -> int:
    return sum(1 for c in analysis.candidates
               if entity_map.get(c.candidate_id, "skipped") not in ("skipped",)
               and not entity_map.get(c.candidate_id, "").startswith("merged:"))


def _count_prefix(entity_map: Mapping[str, str], prefix: str) -> int:
    return sum(1 for v in entity_map.values() if v.startswith(prefix))


def _count_value(entity_map: Mapping[str, str], value: str) -> int:
    return sum(1 for v in entity_map.values() if v == value)


def _count_created(analysis: ImportAnalysis, entity_map: Mapping[str, str],
                   projects: bool) -> int:
    wanted = PROJECT_LIKE if projects else TASK_LIKE
    return sum(1 for c in analysis.candidates
               if c.kind in wanted
               and entity_map.get(c.candidate_id, "") not in ("skipped",)
               and not entity_map.get(c.candidate_id, "").startswith("merged:"))


def default_backup_path(root: str | Path) -> Path:
    return Path(root) / f"portfolio.before-import-{_file_stamp()}.json"


def import_artifact_path(root: str | Path, source_name: str,
                         imported_at: str) -> Path:
    slug = _slug(Path(source_name).stem, "import")
    stamp = re.sub(r"[^0-9A-Za-z._-]", "-", imported_at)
    return Path(root) / "imports" / f"{slug}-{stamp}.json"


def write_import_artifact(
    root: str | Path,
    analysis: ImportAnalysis,
    decisions: Sequence[ImportDecision],
    entity_map: Mapping[str, str],
) -> str:
    path = import_artifact_path(root, analysis.source_name,
                                analysis.imported_at)
    payload: dict[str, Any] = {
        "schema_version": model.SCHEMA_VERSION,
        "kind": "mvp_import",
        "source_name": analysis.source_name,
        "imported_at": analysis.imported_at,
        "engine": analysis.engine,
        "text_chars": analysis.text_chars,
        "candidates": [c.to_dict() for c in analysis.candidates],
        "matches": [m.to_dict() for m in analysis.matches],
        "decisions": [
            {
                "candidate_id": d.candidate_id,
                "action": d.action,
                "merge_project_id": d.merge_project_id,
                "project_id": d.project_id,
                "title": d.title,
                "status": d.status,
                "kind": d.kind,
            }
            for d in decisions
        ],
        "entity_map": dict(entity_map),
    }
    intel_model.write_json(path, payload)
    return str(path)


def save_draft(root: str | Path, analysis: ImportAnalysis) -> str:
    draft_id = uuid.uuid4().hex[:12]
    path = Path(root) / "imports" / "drafts" / f"{draft_id}.json"
    intel_model.write_json(path, analysis.to_dict())
    return draft_id


def load_draft(root: str | Path, draft_id: str) -> ImportAnalysis:
    if not re.fullmatch(r"[0-9a-f]{12}", draft_id):
        raise PortfolioImportError("invalid draft id")
    path = Path(root) / "imports" / "drafts" / f"{draft_id}.json"
    data = intel_model.read_json(path)
    if data is None:
        raise PortfolioImportError(f"draft {draft_id!r} not found")
    return ImportAnalysis.from_dict(data)


__all__ = [
    "ACTION_IMPORT", "ACTION_SKIP", "ACTIONS",
    "AREA", "BLOCKER", "DELIVERABLE", "IDEA", "PROJECT", "QUESTION",
    "REFERENCE", "RESOURCE", "SOMEDAY", "SUBTASK", "TASK", "WAITING",
    "WORKSTREAM", "WORK_PACKAGE", "KINDS",
    "FACT", "INFERRED", "SUGGESTED", "UNKNOWN", "EVIDENCES",
    "DEFAULT_LLM_MODEL", "DEFAULT_OLLAMA_URL",
    "DEFAULT_DEEPSEEK_MODEL", "DEFAULT_DEEPSEEK_URL",
    "DeepSeekLlm",
    "MODE_FACTUAL", "MODE_SEMANTIC", "MODES",
    "STAGE_QUEUED", "STAGE_EXTRACTING", "STAGE_CLASSIFYING",
    "STAGE_MATCHING", "STAGE_CONSOLIDATING", "STAGE_PREPARING_PREVIEW",
    "STAGE_COMPLETED", "STAGE_FAILED", "STAGE_CANCELLED", "STAGES",
    "ImportProgress", "ImportCancelled",
    "ChildSuggestion", "DependencySuggestion", "ImportAnalysis",
    "ImportCandidate", "ImportDecision", "ImportMatch", "LocalLlm",
    "make_import_llm", "make_engine_llm",
    "PortfolioImportError",
    "analyze_document", "apply_decisions", "confirm_import",
    "default_backup_path", "detect_duplicates", "load_draft",
    "parse_decisions", "save_draft", "write_import_artifact",
    "best_project_match", "normalize_text", "text_similarity",
]
