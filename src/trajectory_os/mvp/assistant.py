"""MVP — bounded, read-only assistant over canonical project state.

The assistant has two backends:

* ``deterministic`` (default) — composes the existing project explanation or
  weekly review into a human-readable answer with citations; no model;
* ``local`` — optional Ollama call through the existing ``LocalLlm`` client,
  with a strict JSON schema, mandatory citations and a deterministic fallback.

There is no write path, no tool loop, no autonomous action and no cloud by
default. An invalid/missing citation from a local model falls back to the
deterministic answer instead of being presented as fact.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import (
    changes,
    importer,
    insights,
    planning,
    signals,
)

KIND_PROJECT = "PROJECT_EXPLANATION"
KIND_WEEKLY = "WEEKLY_REVIEW"
KINDS = frozenset({KIND_PROJECT, KIND_WEEKLY})

ENGINE_DETERMINISTIC = "deterministic"
ENGINE_LOCAL = "local"
ENGINES = frozenset({ENGINE_DETERMINISTIC, ENGINE_LOCAL})

MAX_FACTS = 60
MAX_QUESTION = 1_000


class AssistantError(Exception):
    """The assistant cannot answer safely (fail closed)."""


@dataclass(frozen=True)
class AssistantRequest:
    kind: str
    project_id: str = ""
    question: str = ""
    max_items: int = 8

    def validate(self) -> AssistantRequest:
        if self.kind not in KINDS:
            raise AssistantError(f"unknown kind {self.kind!r}")
        if self.kind == KIND_PROJECT and not self.project_id:
            raise AssistantError("project_id required")
        if len(self.question) > MAX_QUESTION:
            raise AssistantError("question is too long")
        if not 1 <= self.max_items <= 20:
            raise AssistantError("max_items out of bounds")
        return self


@dataclass(frozen=True)
class AssistantAnswer:
    answer: str
    citations: tuple[str, ...]
    unknowns: tuple[str, ...]
    engine: str
    canonical: bool = False
    deterministic_fallback: bool = False
    fallback_reason: str = ""
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": list(self.citations),
            "unknowns": list(self.unknowns),
            "engine": self.engine,
            "canonical": self.canonical,
            "deterministic_fallback": self.deterministic_fallback,
            "fallback_reason": self.fallback_reason,
            "generated_at": self.generated_at or intel_model.utc_now(),
        }


class AssistantBackend(Protocol):
    name: str

    def answer(self, request: AssistantRequest,
               context: Mapping[str, Any]) -> AssistantAnswer:
        ...  # pragma: no cover


def _fact(fact_id: str, text: str) -> dict[str, str]:
    return {"id": fact_id, "text": text}


def project_context(root: str | Path, project_id: str,
                    *, today: date | None = None) -> dict[str, Any]:
    explanation = insights.explain_project(root, project_id, today=today)
    project = explanation["project"]
    facts = [
        _fact("project", f"Projet {project['name']} · statut "
              f"{project['status']} · santé {explanation['health']} · "
              f"progression {explanation['progress']}"),
    ]
    for index, action in enumerate(explanation["next_actions"], 1):
        facts.append(_fact(f"next:{index}",
                           f"Prochaine action: {action['title']} "
                           f"({action['effort_minutes']} min)"))
    for index, blocker in enumerate(explanation["blockers"], 1):
        facts.append(_fact(f"blocker:{index}",
                           f"Blocage: {blocker['title']} · "
                           f"{blocker['state']} · "
                           f"{'; '.join(blocker['reasons'])}"))
    for index, risk in enumerate(explanation["risks"], 1):
        facts.append(_fact(f"risk:{index}", f"Risque: {risk}"))
    for index, change in enumerate(explanation["recent_changes"], 1):
        facts.append(_fact(f"change:{index}",
                           f"Changement: {change.get('summary', '')} "
                           f"({change.get('at', '')})"))
    for index, signal in enumerate(explanation["signals"], 1):
        facts.append(_fact(f"signal:{index}",
                           f"Signal: {signal.get('title', '')} · "
                           f"{signal.get('source_ref', '')}"))
    return {
        "kind": KIND_PROJECT, "title": project["name"],
        "facts": facts[:MAX_FACTS], "explanation": explanation,
    }


def weekly_context(root: str | Path, *, today: date | None = None
                   ) -> dict[str, Any]:
    now = today or date.today()
    review = planning.build_review(root, today=now)
    facts = [
        _fact("review", f"Revue du {now.isoformat()}"),
    ]
    for index, item in enumerate(review.get("at_risk", [])[:20], 1):
        facts.append(_fact(f"at_risk:{index}",
                           f"À risque: {item.get('title', '')} · "
                           f"échéance {item.get('deadline', '')}"))
    for index, item in enumerate(review.get("stale_projects", [])[:20], 1):
        facts.append(_fact(f"stale:{index}",
                           f"Projet stale: {item.get('name', '')} · "
                           f"{item.get('reason', '')}"))
    for index, item in enumerate(review.get("blocked", [])[:20], 1):
        facts.append(_fact(f"blocked:{index}",
                           f"Bloqué: {item.get('title', '')} · "
                           f"{item.get('blocked_reason', '')}"))
    for index, item in enumerate(review.get("completed", [])[:20], 1):
        facts.append(_fact(f"completed:{index}",
                           f"Terminé: {item.get('title', '')}"))
    for index, item in enumerate(review.get("unfinished_planned", [])[:20], 1):
        facts.append(_fact(f"unfinished:{index}",
                           f"Non terminé: {item.get('title', '')}"))
    estimation = review.get("estimation", {})
    facts.append(_fact("estimation",
                       f"Précision: {estimation.get('completed_with_actual', 0)}"
                       f" terminées avec durée réelle · erreur moyenne "
                       f"{estimation.get('mean_absolute_error_minutes')}"))
    since = (now - timedelta(days=7)).isoformat()
    for index, change in enumerate(changes.read_changes(root, limit=100), 1):
        if str(change.at)[:10] < since:
            continue
        facts.append(_fact(f"change:{index}",
                           f"Changement: {change.summary} ({change.at})"))
    for index, signal in enumerate(signals.read_signals(root, limit=100), 1):
        if str(signal.received_at)[:10] < since:
            continue
        facts.append(_fact(f"signal:{index}",
                           f"Signal: {signal.title} · {signal.source_ref}"))
    return {"kind": KIND_WEEKLY, "title": "Revue hebdomadaire",
            "facts": facts[:MAX_FACTS], "review": review}


def _deterministic_text(context: Mapping[str, Any]) -> str:
    kind = context.get("kind")
    if kind == KIND_PROJECT:
        explanation = context.get("explanation", {})
        project = explanation.get("project", {})
        lines = [
            f"## {project.get('name', 'Projet')}",
            f"Santé : {explanation.get('health', '')} · statut : "
            f"{project.get('status', '')} · progression : "
            f"{explanation.get('progress')}",
            "",
            "### Prochaines actions",
        ]
        actions = explanation.get("next_actions", [])
        lines += [f"- {item.get('title')} ({item.get('effort_minutes')} min)"
                  for item in actions] or ["- aucune"]
        lines += ["", "### Blocages"]
        blockers = explanation.get("blockers", [])
        lines += [f"- {item.get('title')} · {item.get('state')}"
                  for item in blockers] or ["- aucun"]
        lines += ["", "### Risques"]
        lines += [f"- {risk}" for risk in explanation.get("risks", [])] \
            or ["- aucun signal"]
        lines += ["", "### Sources"]
        lines += [f"- {item.get('title')} · {item.get('source_ref')}"
                  for item in explanation.get("signals", [])] or ["- aucune"]
        return "\n".join(lines)
    review = context.get("review", {})
    lines = [
        "## Revue hebdomadaire",
        f"- À risque : {len(review.get('at_risk', []))}",
        f"- Bloqués : {len(review.get('blocked', []))}",
        f"- Terminés aujourd'hui : {len(review.get('completed', []))}",
        f"- Non terminés planifiés : "
        f"{len(review.get('unfinished_planned', []))}",
    ]
    estimation = review.get("estimation", {})
    lines.append(
        f"- Erreur moyenne d'estimation : "
        f"{estimation.get('mean_absolute_error_minutes')}")
    for item in review.get("at_risk", [])[:5]:
        lines.append(f"- À risque : {item.get('title')}")
    for item in review.get("blocked", [])[:5]:
        lines.append(f"- Bloqué : {item.get('title')}")
    return "\n".join(lines)


class DeterministicBackend:
    name = ENGINE_DETERMINISTIC

    def answer(self, request: AssistantRequest,
               context: Mapping[str, Any]) -> AssistantAnswer:
        return AssistantAnswer(
            answer=_deterministic_text(context),
            citations=tuple(fact["id"] for fact in context.get("facts", [])),
            unknowns=(), engine=self.name)


_ASSISTANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "citations", "unknowns"],
}


class LocalLlmBackend:
    """Optional local model, strictly grounded in supplied fact ids."""

    name = ENGINE_LOCAL

    def __init__(self, llm: Any | None = None) -> None:
        self.llm = llm or importer.LocalLlm()

    def answer(self, request: AssistantRequest,
               context: Mapping[str, Any]) -> AssistantAnswer:
        facts = context.get("facts", [])
        allowed = {fact["id"] for fact in facts}
        system = (
            "Tu es un assistant read-only pour un portefeuille personnel. "
            "Réponds uniquement à partir des faits fournis. Cite les ids "
            "des faits utilisés. Si une information manque, mets-la dans "
            "unknowns. N'invente aucune date, aucun montant, aucune action."
        )
        user = json.dumps({
            "question": request.question or "Résume la situation et les "
                                            "prochaines actions.",
            "facts": facts,
        }, ensure_ascii=False)
        payload = self.llm.complete_json(system, user, _ASSISTANT_SCHEMA)
        answer = payload.get("answer")
        citations = payload.get("citations")
        unknowns = payload.get("unknowns")
        if not isinstance(answer, str) or not answer.strip():
            raise AssistantError("assistant returned no answer")
        if not isinstance(citations, list):
            raise AssistantError("assistant returned invalid citations")
        cleaned = tuple(str(item) for item in citations if str(item) in allowed)
        if not cleaned:
            raise AssistantError("assistant returned no valid citation")
        return AssistantAnswer(
            answer=answer.strip(), citations=cleaned,
            unknowns=tuple(str(item) for item in (unknowns or [])
                           if isinstance(item, (str, int, float))),
            engine=self.name)


def run_assistant(
    root: str | Path,
    request: AssistantRequest,
    *,
    backend: AssistantBackend | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    request.validate()
    if request.kind == KIND_PROJECT:
        context = project_context(root, request.project_id, today=today)
    else:
        context = weekly_context(root, today=today)
    deterministic = DeterministicBackend().answer(request, context)
    selected = backend
    engine = os.environ.get("TRAJECTORY_MVP_ASSISTANT_ENGINE",
                            ENGINE_DETERMINISTIC).strip().lower()
    if selected is None and engine == ENGINE_LOCAL:
        selected = LocalLlmBackend()
    if selected is None:
        return deterministic.to_dict()
    try:
        answer = selected.answer(request, context)
        return answer.to_dict()
    except Exception as exc:  # local model/network/schema failure
        fallback = AssistantAnswer(
            answer=deterministic.answer,
            citations=deterministic.citations,
            unknowns=deterministic.unknowns,
            engine=ENGINE_DETERMINISTIC,
            deterministic_fallback=True,
            fallback_reason=str(exc)[:300])
        return fallback.to_dict()


__all__ = [
    "AssistantAnswer", "AssistantBackend", "AssistantError",
    "AssistantRequest", "DeterministicBackend", "ENGINES", "KINDS",
    "KIND_PROJECT", "KIND_WEEKLY", "LocalLlmBackend", "project_context",
    "run_assistant", "weekly_context",
]
