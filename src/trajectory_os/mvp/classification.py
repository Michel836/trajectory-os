"""MVP — shared, deterministic classification contract.

This module is the single source of truth for two things that used to live in
different places:

* the **taxonomy** (AREA / PROJECT / WORKSTREAM / TASK / ... / IDEA /
  SOMEDAY) and the mapping from a taxonomy ``kind`` to the durable entity it
  becomes (Project, Task, a grouping label, or a note);
* the **deterministic criteria** that decide, from the text alone, whether an
  item is project-like (project / idea / someday / area) or a leaf
  (task/subtask/waiting/blocker).

Both the document importer (which also applies it as an LLM post-pass and
schema enum) and Quick Capture (which applies it directly, with no LLM) use
this contract, so a captured line and an imported line are classified by the
same rules.

Design principles:

* deterministic and dependency-free (no LLM, no network);
* explicit reasons and an ``ambiguous`` flag — the classifier never pretends
  to be certain when the text carries no signal;
* "I don't know yet" is a first-class outcome via the Inbox kinds
  (:data:`INBOX_KINDS`), never a forced project/task choice.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

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

#: Project-like kinds that are deliberately *parked*: they are durable
#: projects, but they are excluded from readiness, priority and scheduling.
#: This is the persisted "Inbox / to triage later" bucket.
INBOX_KINDS = frozenset({IDEA, SOMEDAY})

# --- text normalization -------------------------------------------------------


def normalize_text(value: str) -> str:
    """Return the accent/punctuation-insensitive matching form of ``value``."""
    text = unicodedata.normalize("NFKD", value.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# --- deterministic markers ----------------------------------------------------

#: Markers that make a line project-like rather than an atomic task.
PROJECT_MARKERS = (
    "projet", "project", "initiative", "roadmap", "programme", "program",
    "plateforme", "platform", "app", "application", "lancement",
    "lancer un", "creer un", "mettre en place", "refonte", "migration",
)

#: Markers that make a line a vague future notion (parked as SOMEDAY).
SOMEDAY_MARKERS = (
    "someday", "un jour", "un de ces jours", "plus tard", "eventuellement",
    "peut etre", "pourquoi pas", "a l occasion", "dans le futur",
    "backlog", "wishlist", "envie de",
)

#: Markers that make a line an open idea to explore (parked as IDEA).
IDEA_MARKERS = (
    "idee", "idea", "concept", "piste", "reflexion", "explorer",
    "envisager", "etudier la possibilite", "brainstorm", "inspiration",
)

#: Markers of a broad, durable life domain (never inferred aggressively).
AREA_MARKERS = (
    "domaine de vie", "domaine", "area of life", "sphere",
)

#: Leading action verbs that mark an atomic, actionable task.
TASK_VERBS = (
    "faire", "appeler", "envoyer", "ecrire", "preparer", "verifier",
    "acheter", "lire", "relire", "corriger", "publier", "contacter",
    "relancer", "planifier", "organiser", "finaliser", "valider",
    "installer", "configurer", "revoir", "terminer", "commencer", "creer",
    "mettre", "prendre", "demander", "reserver", "payer", "signer",
    # Common English equivalents, so a plain English action is not reported
    # as ambiguous.
    "book", "call", "send", "write", "review", "check", "fix", "update",
    "prepare", "buy", "read", "plan", "organize", "finish", "start",
    "create", "email", "schedule", "pay", "sign", "confirm", "follow",
)

#: Public kind alias used by Quick Capture (durable entity vocabulary).
ENTITY_PROJECT = "project"
ENTITY_TASK = "task"
ENTITY_GROUPING = "grouping"
ENTITY_NOTE = "note"


def entity_of(kind: str) -> str:
    """Map a taxonomy ``kind`` to the durable entity it becomes."""
    if kind in PROJECT_LIKE:
        return ENTITY_PROJECT
    if kind in TASK_LIKE:
        return ENTITY_TASK
    if kind in GROUPING:
        return ENTITY_GROUPING
    if kind in NOTE_LIKE:
        return ENTITY_NOTE
    return ENTITY_TASK


def is_project_like(kind: str) -> bool:
    return kind in PROJECT_LIKE


def is_task_like(kind: str) -> bool:
    return kind in TASK_LIKE


def is_inbox(kind: str) -> bool:
    """Whether a kind is parked ("to triage later") rather than executed."""
    return kind in INBOX_KINDS


@dataclass(frozen=True)
class Classification:
    """Deterministic classification of one line of text."""

    kind: str
    entity: str
    confidence: float
    ambiguous: bool
    inbox: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "entity": self.entity,
            "confidence": self.confidence,
            "ambiguous": self.ambiguous,
            "inbox": self.inbox,
            "reasons": list(self.reasons),
        }


def _has_marker(normalized: str, markers: tuple[str, ...]) -> bool:
    padded = f" {normalized} "
    return any(
        f" {marker} " in padded or normalized.startswith(marker + " ")
        for marker in markers)


def classify_text(title: str, *, default: str = TASK) -> Classification:
    """Classify one line of text with the shared deterministic criteria.

    Order matters and is conservative: an explicit "someday"/"idea" phrasing
    wins over a project marker (parking a vague notion is safer than creating
    a project), a project marker wins over a task verb, and a line with no
    signal is reported as ``ambiguous`` instead of being asserted.
    """
    normalized = normalize_text(title)
    if not normalized:
        return Classification(
            kind=default, entity=entity_of(default), confidence=0.0,
            ambiguous=True, inbox=is_inbox(default),
            reasons=("empty title; nothing to classify",))

    if _has_marker(normalized, SOMEDAY_MARKERS):
        return Classification(
            kind=SOMEDAY, entity=entity_of(SOMEDAY), confidence=0.9,
            ambiguous=False, inbox=True,
            reasons=("phrased as a vague future notion ('someday')",))
    if _has_marker(normalized, IDEA_MARKERS):
        return Classification(
            kind=IDEA, entity=entity_of(IDEA), confidence=0.85,
            ambiguous=False, inbox=True,
            reasons=("phrased as an open idea to explore",))
    if _has_marker(normalized, AREA_MARKERS):
        return Classification(
            kind=AREA, entity=entity_of(AREA), confidence=0.75,
            ambiguous=False, inbox=False,
            reasons=("phrased as a broad, durable life domain",))
    if _has_marker(normalized, PROJECT_MARKERS):
        return Classification(
            kind=PROJECT, entity=entity_of(PROJECT), confidence=0.75,
            ambiguous=False, inbox=False,
            reasons=("phrased as a bounded, outcome-oriented project",))
    if normalized.split()[0] in TASK_VERBS:
        return Classification(
            kind=TASK, entity=entity_of(TASK), confidence=0.6,
            ambiguous=False, inbox=False,
            reasons=("starts with an action verb (atomic task)",))
    return Classification(
        kind=default, entity=entity_of(default), confidence=0.3,
        ambiguous=True, inbox=is_inbox(default),
        reasons=(
            "no explicit project/idea marker; treated as actionable work",
        ))


__all__ = [
    "AREA", "AREA_MARKERS", "BLOCKER", "Classification", "DELIVERABLE",
    "ENTITY_GROUPING", "ENTITY_NOTE", "ENTITY_PROJECT", "ENTITY_TASK",
    "GROUPING", "IDEA", "IDEA_MARKERS", "INBOX_KINDS", "KINDS", "NOTE_LIKE",
    "PROJECT", "PROJECT_LIKE", "PROJECT_MARKERS", "QUESTION", "REFERENCE",
    "RESOURCE", "SOMEDAY", "SOMEDAY_MARKERS", "SUBTASK", "TASK", "TASK_LIKE",
    "TASK_VERBS", "WAITING", "WORKSTREAM", "WORK_PACKAGE", "classify_text",
    "entity_of", "is_inbox", "is_project_like", "is_task_like",
    "normalize_text",
]
