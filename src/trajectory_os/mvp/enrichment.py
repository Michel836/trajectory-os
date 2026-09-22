"""MVP — on-demand AI enrichment (explicitly SUGGESTED, never auto-accepted).

Factual import deliberately does not fabricate execution structure. When the
user later wants help, the project inspector offers four explicit actions:

``next_actions`` / ``generate_wbs`` / ``suggest_dependencies`` /
``suggest_deliverables``.

Every generated item is stored in a sidecar file (never in ``portfolio.json``),
marked ``SUGGESTED`` and remains visually separate from FACT / ACCEPTED
structure. A suggestion only enters the portfolio when the user explicitly
accepts it, and acceptance goes through the validated
:mod:`trajectory_os.mvp.mutations` layer.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import import_engine, importer, model, mutations, store

#: Enrichment actions (the four inspector buttons).
ENRICH_NEXT_ACTIONS = "next_actions"
ENRICH_WBS = "generate_wbs"
ENRICH_DEPENDENCIES = "suggest_dependencies"
ENRICH_DELIVERABLES = "suggest_deliverables"
ENRICHMENT_KINDS = (
    ENRICH_NEXT_ACTIONS, ENRICH_WBS, ENRICH_DEPENDENCIES,
    ENRICH_DELIVERABLES,
)

#: Suggestion kinds.
NEXT_ACTION = "NEXT_ACTION"
DEPENDENCY = "DEPENDENCY"
WORKSTREAM = "WORKSTREAM"
WORK_PACKAGE = "WORK_PACKAGE"
TASK = "TASK"
SUBTASK = "SUBTASK"
DELIVERABLE = "DELIVERABLE"

SUGGESTION_KINDS = frozenset({
    NEXT_ACTION, DEPENDENCY, WORKSTREAM, WORK_PACKAGE, TASK, SUBTASK,
    DELIVERABLE,
})

SUGGESTED = "SUGGESTED"

#: Suggestion review states.
PENDING = "PENDING"
ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
STATES = frozenset({PENDING, ACCEPTED, REJECTED})

_ALLOWED_BY_ACTION: dict[str, frozenset[str]] = {
    ENRICH_NEXT_ACTIONS: frozenset({NEXT_ACTION}),
    ENRICH_WBS: frozenset({WORKSTREAM, WORK_PACKAGE, TASK, SUBTASK}),
    ENRICH_DEPENDENCIES: frozenset({DEPENDENCY}),
    ENRICH_DELIVERABLES: frozenset({DELIVERABLE}),
}

#: Response schema for the on-demand enrichment actions. Deliberately
#: distinct from the importer ``_LLM_SCHEMA``: enrichment returns a top-level
#: ``suggestions`` array whose kinds (NEXT_ACTION, DEPENDENCY, ...) are not
#: part of the import taxonomy. The enum reuses :data:`SUGGESTION_KINDS`.
_SUGGESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "enum": sorted(SUGGESTION_KINDS)},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "source_title": {"type": "string"},
                    "target_title": {"type": "string"},
                },
                "required": ["kind", "title"],
            },
        },
    },
    "required": ["suggestions"],
}


class EnrichmentError(Exception):
    """An enrichment request cannot be satisfied (fail closed)."""


@dataclass(frozen=True)
class EnrichmentSuggestion:
    """One reviewable, explicitly SUGGESTED item."""

    suggestion_id: str
    kind: str
    title: str
    detail: str
    evidence: str = SUGGESTED
    state: str = PENDING
    source_title: str = ""
    target_title: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "state": self.state,
            "source_title": self.source_title,
            "target_title": self.target_title,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> EnrichmentSuggestion:
        kind = str(data.get("kind", ""))
        if kind not in SUGGESTION_KINDS:
            raise EnrichmentError(f"unknown suggestion kind {kind!r}")
        state = str(data.get("state", PENDING))
        if state not in STATES:
            state = PENDING
        return EnrichmentSuggestion(
            suggestion_id=str(data.get("suggestion_id", "")),
            kind=kind,
            title=str(data.get("title", "")),
            detail=str(data.get("detail", "")),
            evidence=SUGGESTED,
            state=state,
            source_title=str(data.get("source_title", "")),
            target_title=str(data.get("target_title", "")),
            created_at=str(data.get("created_at", "")),
        )


@dataclass(frozen=True)
class EnrichmentResult:
    """The result of one on-demand enrichment action."""

    project_id: str
    kind: str
    engine: str
    model: str
    pricing_state: str
    generated_at: str
    suggestions: tuple[EnrichmentSuggestion, ...]
    estimated_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "kind": self.kind,
            "engine": self.engine,
            "model": self.model,
            "pricing_state": self.pricing_state,
            "generated_at": self.generated_at,
            "estimated_cost_usd": self.estimated_cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "suggestions": [s.to_dict() for s in self.suggestions],
            "ai_generated_items": len(self.suggestions),
            "auto_accepted": 0,
        }


# --- storage ------------------------------------------------------------------


def _store_path(root: str | Path, project_id: str) -> Path:
    safe = re.sub(r"[^a-z0-9._-]+", "-", project_id.lower()).strip("-")
    return Path(root) / "enrichment" / f"{safe or 'project'}.json"


def load_suggestions(
    root: str | Path, project_id: str,
) -> tuple[EnrichmentSuggestion, ...]:
    path = _store_path(root, project_id)
    data = intel_model.read_json(path)
    if not isinstance(data, Mapping):
        return ()
    raw = data.get("suggestions")
    if not isinstance(raw, list):
        return ()
    result: list[EnrichmentSuggestion] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            result.append(EnrichmentSuggestion.from_dict(item))
        except EnrichmentError:
            continue
    return tuple(result)


def _write_suggestions(
    root: str | Path, project_id: str,
    suggestions: Sequence[EnrichmentSuggestion],
    meta: Mapping[str, Any],
) -> None:
    payload = {
        "schema_version": model.SCHEMA_VERSION,
        "kind": "mvp_enrichment",
        "project_id": project_id,
        **dict(meta),
        "suggestions": [s.to_dict() for s in suggestions],
    }
    intel_model.write_json(_store_path(root, project_id), payload)


def _load_meta(root: str | Path, project_id: str) -> dict[str, Any]:
    """Return the stored engine/model/usage metadata for a project.

    Used by accept/reject so reviewing one suggestion never drops the engine,
    model, pricing, usage or generation timestamp recorded by ``generate``.
    """
    payload = intel_model.read_json(_store_path(root, project_id))
    if not isinstance(payload, Mapping):
        return {}
    return {key: value for key, value in payload.items()
            if key not in {"schema_version", "kind", "project_id",
                           "suggestions"}}


# --- generation ---------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are a pragmatic project-planning assistant. You receive ONE real "
    "project and its current tasks from a personal portfolio. You propose "
    "candidate additions only. You never assert facts about the project."
    "\n\nEvery item you produce is a SUGGESTION and must remain separate from "
    "confirmed structure. Keep it concrete, short and executable. Never "
    "invent deadlines, budgets, effort numbers, status or people."
    "\n\nRespond with ONLY a JSON object: "
    '{"suggestions":[{"kind":"...","title":"...","detail":"..."}]}.'
)

_ACTION_INSTRUCTION = {
    ENRICH_NEXT_ACTIONS: (
        "Propose 3 to 7 concrete immediate NEXT actions (kind=NEXT_ACTION). "
        "Each title must be an imperative step, never 'work on project'."),
    ENRICH_WBS: (
        "Propose a light work breakdown for the project: at most one "
        "WORKSTREAM, at most two WORK_PACKAGE items, and 2 to 6 TASK or "
        "SUBTASK items. Do not mechanically create every level."),
    ENRICH_DEPENDENCIES: (
        "Propose at most 5 dependencies between EXISTING tasks listed below "
        "(kind=DEPENDENCY). Set 'source_title' and 'target_title' to exact "
        "existing task titles; put the rationale in 'detail'. Never invent "
        "task titles."),
    ENRICH_DELIVERABLES: (
        "Propose 1 to 4 concrete DELIVERABLE items that would demonstrate "
        "completion of the project (kind=DELIVERABLE)."),
}


def _user_prompt(project: model.Project, tasks: Sequence[model.Task]) -> str:
    lines = [
        f"Project id: {project.project_id}",
        f"Project name: {project.name}",
        f"Objective: {project.objective or '(none stated)'}",
        f"Status: {project.status}",
        "",
        "Existing tasks:",
    ]
    if tasks:
        lines.extend(
            f"- {t.task_id}: {t.title} [{t.status}]" for t in tasks)
    else:
        lines.append("(none yet)")
    return "\n".join(lines)


def generate(
    root: str | Path,
    project_id: str,
    kind: str,
    *,
    llm: Callable[[str, str], Mapping[str, Any]] | None = None,
    engine: str | None = None,
    model_name: str = "",
    now: datetime | None = None,
) -> EnrichmentResult:
    """Run one on-demand enrichment action (never auto-accepted)."""
    if kind not in ENRICHMENT_KINDS:
        raise EnrichmentError(f"unknown enrichment action {kind!r}")
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    projects = portfolio.project_map()
    if project_id not in projects:
        raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
    project = projects[project_id]
    project_tasks = tuple(t for t in portfolio.tasks
                          if t.project_id == project_id)

    instant = now or datetime.now(tz=UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    api_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
    try:
        selection = import_engine.resolve_selection(
            engine, now=instant, api_key_configured=api_key)
    except import_engine.EngineError as exc:
        raise EnrichmentError(str(exc)) from exc
    resolved_llm = llm
    resolved_model_name = ""
    if resolved_llm is None:
        # Resolve the explicitly-selected (or time-based default) engine with
        # the enrichment response schema; a selection that cannot be honoured
        # fails closed with no fallback.
        try:
            resolved_llm, _engine_id, resolved_model = \
                importer.make_engine_llm(
                    selection.engine, schema=_SUGGESTION_SCHEMA)
        except importer.PortfolioImportError as exc:
            raise EnrichmentError(str(exc)) from exc
        resolved_model_name = resolved_model
    if resolved_llm is None:
        raise EnrichmentError(
            "no AI engine is available for enrichment; the deterministic "
            "import engine cannot invent suggestions")

    prompt = _ACTION_INSTRUCTION[kind] + "\n\n" + _user_prompt(
        project, project_tasks)
    try:
        raw = resolved_llm(_SYSTEM_PROMPT, prompt)
    except importer.PortfolioImportError as exc:
        raise EnrichmentError(str(exc)) from exc
    suggestions = _parse_suggestions(raw, kind, instant)
    input_tokens = importer._usage_int(raw, "input_tokens")
    output_tokens = importer._usage_int(raw, "output_tokens")

    # Replace only the pending suggestions produced by this same action; keep
    # pending suggestions generated for the other enrichment actions.
    allowed = _ALLOWED_BY_ACTION[kind]
    existing = tuple(
        s for s in load_suggestions(root, project_id)
        if s.state != PENDING or s.kind not in allowed)
    merged = existing + suggestions
    final_model = resolved_model_name or model_name or selection.model
    _write_suggestions(root, project_id, merged, {
        "engine": selection.engine,
        "model": final_model,
        "pricing_state": selection.pricing_state,
        "generated_at": intel_model.utc_now(),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    })
    return EnrichmentResult(
        project_id=project_id,
        kind=kind,
        engine=selection.engine,
        model=final_model,
        pricing_state=selection.pricing_state,
        generated_at=intel_model.utc_now(),
        suggestions=suggestions,
        estimated_cost_usd=selection.estimated_cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _parse_suggestions(
    raw: Mapping[str, Any], kind: str, now: datetime,
) -> tuple[EnrichmentSuggestion, ...]:
    value = raw.get("suggestions")
    if not isinstance(value, list):
        raise EnrichmentError("enrichment response missing suggestions")
    allowed = _ALLOWED_BY_ACTION[kind]
    stamp = now.astimezone(UTC).isoformat()
    result: list[EnrichmentSuggestion] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            continue
        suggestion_kind = str(item.get("kind", ""))
        if suggestion_kind not in allowed:
            continue
        title = str(item.get("title", "")).strip()[:model.MAX_STR_LEN]
        if not title:
            continue
        result.append(EnrichmentSuggestion(
            suggestion_id=f"s{index:04d}-{uuid.uuid4().hex[:6]}",
            kind=suggestion_kind,
            title=title,
            detail=str(item.get("detail", ""))[:model.MAX_TEXT_LEN],
            source_title=str(item.get("source_title", ""))[:model.MAX_STR_LEN],
            target_title=str(item.get("target_title", ""))[:model.MAX_STR_LEN],
            created_at=stamp,
        ))
    if not result:
        raise EnrichmentError(
            "the engine returned no usable suggestions for this action")
    return tuple(result)


# --- review (accept / reject / edit) ------------------------------------------


def _find(root: str | Path, project_id: str,
          suggestion_id: str) -> tuple[tuple[EnrichmentSuggestion, ...],
                                       EnrichmentSuggestion]:
    suggestions = load_suggestions(root, project_id)
    current = next((s for s in suggestions
                    if s.suggestion_id == suggestion_id), None)
    if current is None:
        raise EnrichmentError(f"unknown suggestion {suggestion_id!r}")
    return suggestions, current


def reject(root: str | Path, project_id: str, suggestion_id: str) -> dict[str, Any]:
    """Mark one suggestion rejected (no portfolio change)."""
    suggestions, current = _find(root, project_id, suggestion_id)
    updated = tuple(
        replace(s, state=REJECTED) if s.suggestion_id == suggestion_id else s
        for s in suggestions)
    _write_suggestions(root, project_id, updated, {
        **_load_meta(root, project_id),
        "reviewed_at": intel_model.utc_now()})
    return {"suggestion_id": suggestion_id, "state": REJECTED,
            "portfolio_changed": False}


def accept(
    root: str | Path, project_id: str, suggestion_id: str, *,
    title: str | None = None,
) -> dict[str, Any]:
    """Explicitly accept one suggestion and apply it through mutations."""
    suggestions, current = _find(root, project_id, suggestion_id)
    if current.state == ACCEPTED:
        return {"suggestion_id": suggestion_id, "state": ACCEPTED,
                "portfolio_changed": False}
    effective_title = (title or current.title).strip()
    if not effective_title:
        raise EnrichmentError("accepted suggestion needs a title")
    applied = _apply(root, project_id, current, effective_title)
    updated = tuple(
        replace(s, state=ACCEPTED, title=effective_title)
        if s.suggestion_id == suggestion_id else s
        for s in suggestions)
    _write_suggestions(root, project_id, updated, {
        **_load_meta(root, project_id),
        "reviewed_at": intel_model.utc_now()})
    return {"suggestion_id": suggestion_id, "state": ACCEPTED,
            "portfolio_changed": True, **applied}


def _apply(
    root: str | Path, project_id: str,
    suggestion: EnrichmentSuggestion, title: str,
) -> dict[str, Any]:
    if suggestion.kind == DEPENDENCY:
        return _apply_dependency(root, suggestion)
    if suggestion.kind == NEXT_ACTION:
        result = mutations.create_task(root, {
            "project_id": project_id,
            "title": title,
            "next_action": title,
            "description": suggestion.detail,
        })
        return {"task_id": result["task_id"], "applied_as": NEXT_ACTION}
    payload: dict[str, Any] = {"project_id": project_id, "title": title,
                               "description": suggestion.detail}
    if suggestion.kind == WORKSTREAM:
        payload["workstream"] = title
    elif suggestion.kind in (WORK_PACKAGE, DELIVERABLE):
        payload["deliverable"] = title
    result = mutations.create_task(root, payload)
    return {"task_id": result["task_id"], "applied_as": suggestion.kind}


def _apply_dependency(
    root: str | Path, suggestion: EnrichmentSuggestion,
) -> dict[str, Any]:
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    by_title = {t.title.strip().lower(): t for t in portfolio.tasks}
    source = by_title.get(suggestion.source_title.strip().lower())
    target = by_title.get(suggestion.target_title.strip().lower())
    if source is None or target is None:
        raise EnrichmentError(
            "dependency suggestion references a task that does not exist; "
            "create the tasks first or reject the suggestion")
    result = mutations.add_dependency(root, target.task_id, source.task_id)
    return {"applied_as": DEPENDENCY, "task_id": target.task_id,
            "dependency": source.task_id, "added": result.get("added", False)}


def to_json(payload: Mapping[str, Any]) -> str:
    """Small debug helper (never used for durable storage)."""
    return json.dumps(payload, indent=2, sort_keys=True)


__all__ = [
    "ENRICH_NEXT_ACTIONS", "ENRICH_WBS", "ENRICH_DEPENDENCIES",
    "ENRICH_DELIVERABLES", "ENRICHMENT_KINDS",
    "NEXT_ACTION", "DEPENDENCY", "WORKSTREAM", "WORK_PACKAGE", "TASK",
    "SUBTASK", "DELIVERABLE", "SUGGESTED",
    "PENDING", "ACCEPTED", "REJECTED",
    "EnrichmentError", "EnrichmentSuggestion", "EnrichmentResult",
    "accept", "generate", "load_suggestions", "reject", "to_json",
]
