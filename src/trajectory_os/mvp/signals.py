"""MVP — inbound signals and deterministic proposals (Phase 3).

An inbound signal is any text the human wants to feed into the portfolio: a
pasted email, a note, a document extract or a short thought. The signal is
stored in a bounded ``inbox/`` sidecar and analysed deterministically into
reviewable :class:`Proposal` objects.

Nothing is written to the portfolio at analysis time. A proposal only changes
the portfolio when the human accepts it (see :mod:`trajectory_os.mvp.proposals`).

Design invariants:

* no LLM and no network: dates, durations, status keywords and matching are
  deterministic;
* every proposal carries a source quote and an explicit confidence;
* duplicates are idempotent by content hash;
* noise is filtered before proposals are created;
* the inbox is a sidecar, never a second source of truth.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import classification, importer, model, store

# --- constants ---------------------------------------------------------------

SIGNAL_SCHEMA_VERSION = 1
PROPOSAL_SCHEMA_VERSION = 1

SOURCE_TEXT = "TEXT"
SOURCE_FILE = "FILE"
SOURCE_NOTE = "NOTE"
SOURCE_CLIPBOARD = "CLIPBOARD"
SOURCE_EMAIL_TEXT = "EMAIL_TEXT"
SOURCE_KINDS = frozenset({
    SOURCE_TEXT, SOURCE_FILE, SOURCE_NOTE, SOURCE_CLIPBOARD,
    SOURCE_EMAIL_TEXT,
})

STATUS_NEW = "NEW"
STATUS_ANALYZED = "ANALYZED"
STATUS_APPLIED = "APPLIED"
STATUS_DISMISSED = "DISMISSED"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_MUTED = "MUTED"
SIGNAL_STATUSES = frozenset({
    STATUS_NEW, STATUS_ANALYZED, STATUS_APPLIED, STATUS_DISMISSED,
    STATUS_DUPLICATE, STATUS_MUTED,
})

STATE_PENDING = "PENDING"
STATE_ACCEPTED = "ACCEPTED"
STATE_REJECTED = "REJECTED"
STATE_DEFERRED = "DEFERRED"
PROPOSAL_STATES = frozenset({
    STATE_PENDING, STATE_ACCEPTED, STATE_REJECTED, STATE_DEFERRED,
})

OP_CREATE_TASK = "CREATE_TASK"
OP_CREATE_PROJECT = "CREATE_PROJECT"
OP_UPDATE_TASK = "UPDATE_TASK"
OP_UPDATE_PROJECT = "UPDATE_PROJECT"
OP_SET_DEADLINE = "SET_DEADLINE"
OP_SET_STATUS = "SET_STATUS"
OP_SET_EFFORT = "SET_EFFORT"
OP_RESOLVE_WAITING = "RESOLVE_WAITING"
OP_ADD_BLOCKER = "ADD_BLOCKER"
OP_RECORD_OUTCOME = "RECORD_OUTCOME"
PROPOSAL_OPS = frozenset({
    OP_CREATE_TASK, OP_CREATE_PROJECT, OP_UPDATE_TASK, OP_UPDATE_PROJECT,
    OP_SET_DEADLINE, OP_SET_STATUS, OP_SET_EFFORT, OP_RESOLVE_WAITING,
    OP_ADD_BLOCKER, OP_RECORD_OUTCOME,
})

TARGET_TASK = "task"
TARGET_PROJECT = "project"
TARGET_NONE = ""
TARGET_KINDS = frozenset({TARGET_TASK, TARGET_PROJECT, TARGET_NONE})

SENSITIVITIES = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED")

INBOX_DIR = "inbox"
SIGNALS_NAME = "signals.jsonl"
PROPOSALS_DIR = "proposals"
RULES_NAME = "rules.json"
EVIDENCE_NAME = "evidence.jsonl"

MAX_TEXT_CHARS = 20_000
MAX_TITLE_CHARS = 200
MAX_QUOTE_CHARS = 400
MAX_PROPOSALS = 5
MAX_SIGNALS_PER_DAY = 200
MAX_READ_SIGNALS = 500

DEFAULT_RULES = {
    "schema_version": 1,
    "muted_sources": [],
    "source_rules": {},
    "max_proposals_per_signal": MAX_PROPOSALS,
    "max_signals_per_day": MAX_SIGNALS_PER_DAY,
}

_FR_MONTHS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "decembre": 12,
}
_COMPLETION_WORDS = (
    "termine", "terminee", "fait", "faite", "finalise", "finalisee",
    "livre", "livree", "c'est bon", "c est bon", "completed", "done",
)
_BLOCKED_WORDS = ("bloque", "bloquee", "blocage", "blocked")
_DEFERRED_WORDS = ("reporte", "reportee", "repousse", "repoussee", "deferred")
_RECEIVED_WORDS = (
    "recu", "recue", "recus", "recues", "envoye", "envoyee", "transmis",
    "transmise", "reponse", "retour", "valide", "validee", "accepte",
    "acceptee", "piece jointe", "pieces jointes", "received", "reply",
)
_NOISE_PREFIXES = (
    "bonjour", "bonsoir", "salut", "cordialement", "bien a vous",
    "bien a toi", "merci", "merci beaucoup", "cdt", "br", "sent from",
    "de :", "a :", "objet :", "subject :",
)
_ACTION_VERBS = set(classification.TASK_VERBS)


class SignalError(model.MvpError):
    """An inbound signal or proposal is invalid (fail closed)."""


@dataclass(frozen=True)
class InboundSignal:
    signal_id: str
    source_kind: str
    source_ref: str
    received_at: str
    title: str
    text: str
    content_hash: str
    status: str = STATUS_NEW
    sensitivity: str = "INTERNAL"
    duplicate_of: str = ""
    project_hint: str = ""
    thread_key: str = ""
    schema_version: int = SIGNAL_SCHEMA_VERSION

    def validate(self) -> InboundSignal:
        if self.schema_version != SIGNAL_SCHEMA_VERSION:
            raise SignalError(model.E_UNSUPPORTED_VERSION,
                              str(self.schema_version))
        if self.source_kind not in SOURCE_KINDS:
            raise SignalError(model.E_MALFORMED,
                              f"source_kind {self.source_kind!r}")
        if self.status not in SIGNAL_STATUSES:
            raise SignalError(model.E_MALFORMED,
                              f"status {self.status!r}")
        if self.sensitivity not in SENSITIVITIES:
            raise SignalError(model.E_MALFORMED, "sensitivity")
        if not self.signal_id or not self.text:
            raise SignalError(model.E_MALFORMED, "identity/text required")
        if len(self.text) > MAX_TEXT_CHARS:
            raise SignalError(model.E_MALFORMED, "text exceeds bound")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signal_id": self.signal_id,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "received_at": self.received_at,
            "title": self.title,
            "text": self.text,
            "content_hash": self.content_hash,
            "status": self.status,
            "sensitivity": self.sensitivity,
            "duplicate_of": self.duplicate_of,
            "project_hint": self.project_hint,
            "thread_key": self.thread_key,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> InboundSignal:
        return InboundSignal(
            signal_id=str(data.get("signal_id", "")),
            source_kind=str(data.get("source_kind", SOURCE_TEXT)),
            source_ref=str(data.get("source_ref", "manual")),
            received_at=str(data.get("received_at", "")),
            title=str(data.get("title", "")),
            text=str(data.get("text", "")),
            content_hash=str(data.get("content_hash", "")),
            status=str(data.get("status", STATUS_NEW)),
            sensitivity=str(data.get("sensitivity", "INTERNAL")),
            duplicate_of=str(data.get("duplicate_of", "")),
            project_hint=str(data.get("project_hint", "")),
            thread_key=str(data.get("thread_key", "")),
            schema_version=int(data.get("schema_version",
                                       SIGNAL_SCHEMA_VERSION)),
        ).validate()


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    signal_id: str
    op: str
    target_kind: str
    target_id: str
    title: str
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5
    evidence_quote: str = ""
    rationale: str = ""
    state: str = STATE_PENDING
    created_at: str = ""
    applied_at: str = ""
    source_ref: str = ""
    schema_version: int = PROPOSAL_SCHEMA_VERSION

    def validate(self) -> Proposal:
        if self.schema_version != PROPOSAL_SCHEMA_VERSION:
            raise SignalError(model.E_UNSUPPORTED_VERSION,
                              str(self.schema_version))
        if self.op not in PROPOSAL_OPS:
            raise SignalError(model.E_MALFORMED, f"op {self.op!r}")
        if self.target_kind not in TARGET_KINDS:
            raise SignalError(model.E_MALFORMED, "target_kind")
        if self.state not in PROPOSAL_STATES:
            raise SignalError(model.E_MALFORMED, f"state {self.state!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise SignalError(model.E_MALFORMED, "confidence out of range")
        if not self.proposal_id or not self.signal_id or not self.title:
            raise SignalError(model.E_MALFORMED, "identity/title required")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "signal_id": self.signal_id,
            "op": self.op,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "title": self.title,
            "before": dict(self.before),
            "after": dict(self.after),
            "confidence": round(self.confidence, 3),
            "evidence_quote": self.evidence_quote,
            "rationale": self.rationale,
            "state": self.state,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
            "source_ref": self.source_ref,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Proposal:
        raw_before = data.get("before")
        raw_after = data.get("after")
        before = raw_before if isinstance(raw_before, dict) else {}
        after = raw_after if isinstance(raw_after, dict) else {}
        return Proposal(
            proposal_id=str(data.get("proposal_id", "")),
            signal_id=str(data.get("signal_id", "")),
            op=str(data.get("op", "")),
            target_kind=str(data.get("target_kind", TARGET_NONE)),
            target_id=str(data.get("target_id", "")),
            title=str(data.get("title", "")),
            before=before,
            after=after,
            confidence=float(data.get("confidence", 0.5)),
            evidence_quote=str(data.get("evidence_quote", "")),
            rationale=str(data.get("rationale", "")),
            state=str(data.get("state", STATE_PENDING)),
            created_at=str(data.get("created_at", "")),
            applied_at=str(data.get("applied_at", "")),
            source_ref=str(data.get("source_ref", "")),
            schema_version=int(data.get("schema_version",
                                       PROPOSAL_SCHEMA_VERSION)),
        ).validate()


# --- storage -----------------------------------------------------------------


def inbox_dir(root: str | Path) -> Path:
    return Path(root) / INBOX_DIR


def signals_path(root: str | Path) -> Path:
    return inbox_dir(root) / SIGNALS_NAME


def proposals_dir(root: str | Path) -> Path:
    return inbox_dir(root) / PROPOSALS_DIR


def rules_path(root: str | Path) -> Path:
    return inbox_dir(root) / RULES_NAME


def evidence_path(root: str | Path) -> Path:
    return inbox_dir(root) / EVIDENCE_NAME


def _proposal_path(root: str | Path, signal_id: str) -> Path:
    return proposals_dir(root) / f"{_safe(signal_id)}.json"


def _safe(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")[:80]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _signal_id(source_kind: str, source_ref: str, content_hash: str) -> str:
    return intel_model.digest(
        {"source_kind": source_kind, "source_ref": source_ref,
         "content_hash": content_hash},
        domain="trajectory-os.mvp.signal.v1")[:16]


def read_signals(root: str | Path, limit: int = MAX_READ_SIGNALS
                 ) -> tuple[InboundSignal, ...]:
    """Read unique signals, newest first (append-only file, last wins)."""
    by_id: dict[str, InboundSignal] = {}
    for document in intel_model.read_jsonl(signals_path(root)):
        try:
            signal = InboundSignal.from_dict(document)
        except (SignalError, ValueError):
            continue
        by_id[signal.signal_id] = signal
    ordered = sorted(by_id.values(), key=lambda item: item.received_at,
                     reverse=True)
    return tuple(ordered[:max(1, min(limit, MAX_READ_SIGNALS))])


def get_signal(root: str | Path, signal_id: str) -> InboundSignal:
    for signal in read_signals(root):
        if signal.signal_id == signal_id:
            return signal
    raise SignalError(model.E_UNKNOWN_ID, signal_id)


def append_signal(root: str | Path, signal: InboundSignal) -> None:
    signal.validate()
    intel_model.append_jsonl(signals_path(root), signal.to_dict())


def update_signal_status(root: str | Path, signal_id: str,
                         status: str) -> InboundSignal:
    if status not in SIGNAL_STATUSES:
        raise SignalError(model.E_MALFORMED, f"status {status!r}")
    path = signals_path(root)
    if not path.is_file():
        raise SignalError(model.E_UNKNOWN_ID, signal_id)
    signal = get_signal(root, signal_id)
    updated = replace(signal, status=status)
    append_signal(root, updated)
    return updated


def load_rules(root: str | Path) -> dict[str, Any]:
    document = intel_model.read_json(rules_path(root))
    rules = dict(DEFAULT_RULES)
    if isinstance(document, dict):
        muted = document.get("muted_sources")
        if isinstance(muted, list):
            rules["muted_sources"] = sorted({str(item) for item in muted})
        for key in ("max_proposals_per_signal", "max_signals_per_day"):
            value = document.get(key)
            if isinstance(value, int) and value > 0:
                rules[key] = min(value, 50 if key.endswith("signal")
                                 else 1000)
        raw_rules = document.get("source_rules")
        if isinstance(raw_rules, dict):
            source_rules: dict[str, dict[str, Any]] = {}
            for key, value in raw_rules.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    continue
                sensitivity = str(value.get("sensitivity", "INTERNAL"))
                if sensitivity not in SENSITIVITIES:
                    sensitivity = "INTERNAL"
                cap = value.get("max_proposals", MAX_PROPOSALS)
                source_rules[key] = {
                    "project_id": str(value.get("project_id", "")),
                    "mute": bool(value.get("mute", False)),
                    "sensitivity": sensitivity,
                    "max_proposals": (int(cap)
                                      if isinstance(cap, int) and cap > 0
                                      else MAX_PROPOSALS),
                }
            rules["source_rules"] = source_rules
    return rules


def save_rules(root: str | Path, rules: Mapping[str, Any]) -> dict[str, Any]:
    raw_rules = rules.get("source_rules")
    source_rules: dict[str, dict[str, Any]] = {}
    if isinstance(raw_rules, dict):
        for key, value in raw_rules.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            sensitivity = str(value.get("sensitivity", "INTERNAL"))
            if sensitivity not in SENSITIVITIES:
                sensitivity = "INTERNAL"
            cap = value.get("max_proposals", MAX_PROPOSALS)
            source_rules[key] = {
                "project_id": str(value.get("project_id", "")),
                "mute": bool(value.get("mute", False)),
                "sensitivity": sensitivity,
                "max_proposals": (int(cap)
                                  if isinstance(cap, int) and cap > 0
                                  else MAX_PROPOSALS),
            }
    payload = {
        "schema_version": 1,
        "muted_sources": sorted({str(item) for item in
                                 rules.get("muted_sources", [])}),
        "source_rules": source_rules,
        "max_proposals_per_signal": int(
            rules.get("max_proposals_per_signal", MAX_PROPOSALS)),
        "max_signals_per_day": int(
            rules.get("max_signals_per_day", MAX_SIGNALS_PER_DAY)),
    }
    intel_model.write_json(rules_path(root), payload)
    return payload


def source_rule(root: str | Path, source_ref: str) -> dict[str, Any]:
    """Return the explicit rule for one source (safe defaults)."""
    rule = load_rules(root)["source_rules"].get(source_ref)
    if not isinstance(rule, dict):
        return {"project_id": "", "mute": False,
                "sensitivity": "INTERNAL",
                "max_proposals": MAX_PROPOSALS}
    return dict(rule)


def set_source_rule(root: str | Path, source_ref: str, *,
                    project_id: str = "", mute: bool = False,
                    sensitivity: str = "INTERNAL",
                    max_proposals: int = MAX_PROPOSALS) -> dict[str, Any]:
    rules = load_rules(root)
    rules["source_rules"][source_ref] = {
        "project_id": project_id, "mute": mute,
        "sensitivity": sensitivity, "max_proposals": max_proposals,
    }
    return save_rules(root, rules)


def is_source_muted(root: str | Path, source_ref: str) -> bool:
    return source_ref in load_rules(root)["muted_sources"]


def mute_source(root: str | Path, source_ref: str,
                *, muted: bool = True) -> dict[str, Any]:
    rules = load_rules(root)
    muted_sources = set(rules["muted_sources"])
    if muted:
        muted_sources.add(source_ref)
    else:
        muted_sources.discard(source_ref)
    rules["muted_sources"] = sorted(muted_sources)
    return save_rules(root, rules)


def load_proposals(root: str | Path, signal_id: str) -> tuple[Proposal, ...]:
    document = intel_model.read_json(_proposal_path(root, signal_id))
    if not isinstance(document, dict):
        return ()
    raw = document.get("proposals")
    if not isinstance(raw, list):
        return ()
    proposals: list[Proposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            proposals.append(Proposal.from_dict(item))
        except (SignalError, ValueError):
            continue
    return tuple(proposals)


def save_proposals(root: str | Path, signal_id: str,
                   proposals: Sequence[Proposal]) -> None:
    payload = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "signal_id": signal_id,
        "proposals": [proposal.to_dict() for proposal in proposals],
    }
    intel_model.write_json(_proposal_path(root, signal_id), payload)


def update_proposal(root: str | Path, signal_id: str,
                    proposal_id: str, *, state: str,
                    applied_at: str = "") -> Proposal:
    if state not in PROPOSAL_STATES:
        raise SignalError(model.E_MALFORMED, f"state {state!r}")
    proposals = list(load_proposals(root, signal_id))
    found: Proposal | None = None
    updated: list[Proposal] = []
    for proposal in proposals:
        if proposal.proposal_id == proposal_id:
            found = replace(proposal, state=state, applied_at=applied_at)
            updated.append(found)
        else:
            updated.append(proposal)
    if found is None:
        raise SignalError(model.E_UNKNOWN_ID, proposal_id)
    save_proposals(root, signal_id, updated)
    return found


def list_bundles(root: str | Path, *, limit: int = 50) -> list[dict[str, Any]]:
    bundles: list[dict[str, Any]] = []
    for signal in read_signals(root, limit=limit):
        bundles.append({
            "signal": signal.to_dict(),
            "proposals": [proposal.to_dict()
                          for proposal in load_proposals(root,
                                                         signal.signal_id)],
        })
    return bundles


def get_bundle(root: str | Path, signal_id: str) -> dict[str, Any]:
    signal = get_signal(root, signal_id)
    return {
        "signal": signal.to_dict(),
        "proposals": [proposal.to_dict()
                      for proposal in load_proposals(root, signal_id)],
    }


# --- analysis helpers --------------------------------------------------------


def _segments(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;:])\s+|\n+", text)
    return [part.strip() for part in parts if part.strip()]


def _quote(text: str, needle: str) -> str:
    if not needle:
        return text[:MAX_QUOTE_CHARS]
    index = text.lower().find(needle.lower())
    if index < 0:
        return text[:MAX_QUOTE_CHARS]
    start = max(0, index - 80)
    end = min(len(text), index + len(needle) + 120)
    while start > 0 and text[start - 1].isalnum():
        start -= 1
    while end < len(text) and text[end].isalnum():
        end += 1
    return text[start:end].strip()[:MAX_QUOTE_CHARS]


def _parse_date(text: str, today: date) -> tuple[str, str] | None:
    iso = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if iso:
        try:
            parsed = date(int(iso.group(1)), int(iso.group(2)),
                          int(iso.group(3)))
            return parsed.isoformat(), iso.group(0)
        except ValueError:
            pass
    numeric = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", text)
    if numeric:
        try:
            parsed = date(int(numeric.group(3)), int(numeric.group(2)),
                          int(numeric.group(1)))
            return parsed.isoformat(), numeric.group(0)
        except ValueError:
            pass
    normalized = classification.normalize_text(text)
    long_date = re.search(
        r"\b(\d{1,2})\s+([a-z]+)\s+(20\d{2})\b", normalized)
    if long_date and long_date.group(2) in _FR_MONTHS:
        try:
            parsed = date(int(long_date.group(3)),
                          _FR_MONTHS[long_date.group(2)],
                          int(long_date.group(1)))
            return parsed.isoformat(), long_date.group(0)
        except ValueError:
            pass
    if "apres demain" in normalized or "apres-demain" in normalized:
        return (today + timedelta(days=2)).isoformat(), "apres-demain"
    if "demain" in normalized:
        return (today + timedelta(days=1)).isoformat(), "demain"
    relative = re.search(r"dans\s+(\d{1,3})\s+jours?", normalized)
    if relative:
        return (today + timedelta(days=int(relative.group(1)))).isoformat(), \
            relative.group(0)
    weeks = re.search(r"dans\s+(\d{1,2})\s+semaines?", normalized)
    if weeks:
        return (today + timedelta(weeks=int(weeks.group(1)))).isoformat(), \
            weeks.group(0)
    return None


def _parse_duration(text: str) -> tuple[int, str] | None:
    normalized = classification.normalize_text(text)
    hours_minutes = re.search(r"\b(\d{1,2})\s*h\s*(\d{2})\b", normalized)
    if hours_minutes:
        minutes = int(hours_minutes.group(1)) * 60 + int(
            hours_minutes.group(2))
        if 1 <= minutes <= 1440:
            return minutes, hours_minutes.group(0)
    hours = re.search(r"\b(\d{1,2})\s*(h|heures?)\b", normalized)
    if hours:
        minutes = int(hours.group(1)) * 60
        if 1 <= minutes <= 1440:
            return minutes, hours.group(0)
    minutes_match = re.search(r"\b(\d{1,3})\s*(min|minutes?)\b", normalized)
    if minutes_match:
        minutes = int(minutes_match.group(1))
        if 1 <= minutes <= 1440:
            return minutes, minutes_match.group(0)
    return None


def _has_any(normalized: str, words: Sequence[str]) -> str:
    for word in words:
        if word in normalized:
            return word
    return ""


def _is_noise(line: str) -> bool:
    normalized = classification.normalize_text(line)
    if len(normalized) < 4:
        return True
    return any(normalized.startswith(prefix)
               for prefix in _NOISE_PREFIXES)


def _project_match(text: str, projects: Mapping[str, model.Project]
                   ) -> tuple[str, str, float] | None:
    normalized = classification.normalize_text(text)
    best: tuple[str, str, float] | None = None
    for project in projects.values():
        name = classification.normalize_text(project.name)
        if name and name in normalized:
            candidate = (project.project_id, project.name, 1.0)
            if best is None or candidate[2] > best[2]:
                best = candidate
    hinted = importer.best_project_match(text[:500], projects)
    if hinted is not None and (best is None or hinted[2] > best[2]):
        best = hinted
    if best is None or best[2] < 0.35:
        return None
    return best


def _task_match(text: str, tasks: Mapping[str, model.Task],
                *, waiting_only: bool = False
                ) -> tuple[model.Task, float] | None:
    normalized = classification.normalize_text(text)
    best: tuple[model.Task, float] | None = None
    for task in tasks.values():
        if waiting_only and not (task.waiting_for or
                                 task.status == model.TS_WAITING):
            continue
        title = classification.normalize_text(task.title)
        score = importer.text_similarity(normalized, title)
        if title and title in normalized:
            score = max(score, 0.9)
        if waiting_only:
            score += 0.1
        if best is None or score > best[1]:
            best = (task, min(1.0, score))
    if best is None or best[1] < 0.45:
        return None
    return best


def _make_proposal(
    *, signal: InboundSignal, op: str, target_kind: str,
    target_id: str, title: str, before: Mapping[str, Any] | None,
    after: Mapping[str, Any], confidence: float, quote: str,
    rationale: str, created_at: str,
) -> Proposal:
    payload = {
        "signal_id": signal.signal_id, "op": op,
        "target_kind": target_kind, "target_id": target_id,
        "after": dict(after),
    }
    proposal_id = intel_model.digest(
        payload, domain="trajectory-os.mvp.proposal.v1")[:16]
    return Proposal(
        proposal_id=proposal_id, signal_id=signal.signal_id, op=op,
        target_kind=target_kind, target_id=target_id,
        title=title[:MAX_TITLE_CHARS], before=dict(before or {}),
        after=dict(after), confidence=round(confidence, 3),
        evidence_quote=quote[:MAX_QUOTE_CHARS], rationale=rationale,
        state=STATE_PENDING, created_at=created_at,
        source_ref=signal.source_ref).validate()


def analyze_signal(root: str | Path, signal: InboundSignal,
                   *, today: date | None = None,
                   max_proposals: int = MAX_PROPOSALS
                   ) -> tuple[Proposal, ...]:
    """Deterministically derive reviewable proposals from one signal."""
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise SignalError(model.E_MALFORMED, "no portfolio loaded")
    projects = portfolio.project_map()
    tasks = portfolio.task_map()
    now = today or datetime.now(UTC).date()
    created_at = intel_model.utc_now()
    text = signal.text
    normalized = classification.normalize_text(text)
    project_match = (
        (signal.project_hint, projects[signal.project_hint].name, 1.0)
        if signal.project_hint in projects
        else _project_match(signal.title + " " + text[:800], projects))
    task_match = _task_match(text, tasks)
    waiting_match = _task_match(text, tasks, waiting_only=True)
    if waiting_match is None and project_match is not None:
        waiting_candidates = [
            task for task in tasks.values()
            if task.project_id == project_match[0]
            and (task.waiting_for or task.status == model.TS_WAITING)]
        if len(waiting_candidates) == 1:
            waiting_match = (waiting_candidates[0], 0.6)
    date_match = _parse_date(text, now)
    duration_match = _parse_duration(text)
    proposals: list[Proposal] = []

    def add(op: str, target_kind: str, target_id: str, title: str,
            before: Mapping[str, Any] | None, after: Mapping[str, Any],
            confidence: float, quote: str, rationale: str) -> None:
        proposals.append(_make_proposal(
            signal=signal, op=op, target_kind=target_kind,
            target_id=target_id, title=title, before=before, after=after,
            confidence=confidence, quote=quote, rationale=rationale,
            created_at=created_at))

    if date_match is not None:
        deadline, quote = date_match
        if task_match is not None:
            task, score = task_match
            add(OP_SET_DEADLINE, TARGET_TASK, task.task_id, task.title,
                {"deadline": task.deadline}, {"deadline": deadline},
                min(0.95, 0.55 + score * 0.4), _quote(text, quote),
                "une échéance explicite correspond à cette tâche")
        elif project_match is not None:
            project_id, project_name, score = project_match
            project = projects[project_id]
            add(OP_SET_DEADLINE, TARGET_PROJECT, project_id, project_name,
                {"deadline": project.deadline}, {"deadline": deadline},
                min(0.9, 0.5 + score * 0.4), _quote(text, quote),
                "une échéance explicite correspond à ce projet")

    if duration_match is not None and task_match is not None:
        minutes, quote = duration_match
        task, score = task_match
        add(OP_SET_EFFORT, TARGET_TASK, task.task_id, task.title,
            {"estimated_minutes": task.estimated_minutes},
            {"estimated_minutes": minutes},
            min(0.9, 0.5 + score * 0.4), _quote(text, quote),
            "une durée explicite peut servir d'estimation")

    completed_word = _has_any(normalized, _COMPLETION_WORDS)
    if completed_word and task_match is not None:
        task, score = task_match
        add(OP_RECORD_OUTCOME, TARGET_TASK, task.task_id, task.title,
            {"status": task.status}, {"outcome": model.O_COMPLETED},
            min(0.92, 0.6 + score * 0.3), _quote(text, completed_word),
            "le texte indique que cette tâche est terminée")
    elif _has_any(normalized, _BLOCKED_WORDS) and task_match is not None:
        task, score = task_match
        add(OP_SET_STATUS, TARGET_TASK, task.task_id, task.title,
            {"status": task.status}, {"status": model.TS_BLOCKED},
            min(0.9, 0.55 + score * 0.3),
            _quote(text, _has_any(normalized, _BLOCKED_WORDS)),
            "le texte indique un blocage")
    elif _has_any(normalized, _DEFERRED_WORDS) and task_match is not None:
        task, score = task_match
        add(OP_SET_STATUS, TARGET_TASK, task.task_id, task.title,
            {"status": task.status}, {"status": model.TS_DEFERRED},
            min(0.88, 0.5 + score * 0.3),
            _quote(text, _has_any(normalized, _DEFERRED_WORDS)),
            "le texte indique un report")

    received_word = _has_any(normalized, _RECEIVED_WORDS)
    if received_word and waiting_match is not None:
        task, score = waiting_match
        add(OP_RESOLVE_WAITING, TARGET_TASK, task.task_id, task.title,
            {"status": task.status, "waiting_for": list(task.waiting_for)},
            {"status": model.TS_TODO, "waiting_for": []},
            min(0.94, 0.65 + score * 0.25),
            _quote(text, received_word),
            "le texte suggère que l'attente est levée")

    # New actions / projects from individual segments (bounded and noise-free).
    for segment in _segments(text):
        if len(proposals) >= max_proposals:
            break
        if _is_noise(segment):
            continue
        verdict = classification.classify_text(segment)
        normalized_segment = classification.normalize_text(segment)
        if task_match is not None and importer.text_similarity(
                normalized_segment,
                classification.normalize_text(task_match[0].title)) >= 0.9:
            continue
        if verdict.kind in classification.PROJECT_LIKE:
            status = (model.PS_IDEA if classification.is_inbox(verdict.kind)
                      else model.PS_DEFERRED)
            add(OP_CREATE_PROJECT, TARGET_NONE, "", segment,
                None,
                {"name": segment[:MAX_TITLE_CHARS],
                 "objective": segment[:1000], "status": status},
                0.55 if verdict.ambiguous else 0.65,
                segment, "le texte ressemble à un projet ou une idée")
            continue
        words = normalized_segment.split()
        first_word = words[0] if words else ""
        if project_match is not None:
            if first_word not in _ACTION_VERBS:
                continue
            project_id, project_name, score = project_match
            after: dict[str, Any] = {
                "project_id": project_id,
                "title": segment[:MAX_TITLE_CHARS],
            }
            if duration_match is not None:
                after["estimated_minutes"] = duration_match[0]
            if date_match is not None:
                after["deadline"] = date_match[0]
            add(OP_CREATE_TASK, TARGET_NONE, project_id,
                segment[:MAX_TITLE_CHARS], None, after,
                min(0.85, 0.45 + score * 0.3), segment,
                "le texte contient une action à ajouter à ce projet")
        elif verdict.kind in classification.TASK_LIKE and (
                normalized_segment.split()[0] in _ACTION_VERBS
                if normalized_segment.split() else False):
            add(OP_CREATE_PROJECT, TARGET_NONE, "", segment,
                None,
                {"name": segment[:MAX_TITLE_CHARS],
                 "objective": segment[:1000], "status": model.PS_IDEA},
                0.5, segment,
                "aucun projet ne correspond ; l'action est parkée en Inbox")

    unique: list[Proposal] = []
    seen: set[tuple[str, str, str, str]] = set()
    for proposal in proposals:
        key = (proposal.op, proposal.target_kind, proposal.target_id,
               json.dumps(proposal.after, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        unique.append(proposal)
    unique.sort(key=lambda item: (-item.confidence, item.op))
    return tuple(unique[:max(1, max_proposals)])


# --- public creation ---------------------------------------------------------


def create_signal(
    root: str | Path,
    *,
    text: str,
    source_kind: str = SOURCE_TEXT,
    source_ref: str = "manual",
    title: str = "",
    project_hint: str = "",
    sensitivity: str = "INTERNAL",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Store and analyse one inbound signal (idempotent by content hash)."""
    if not isinstance(text, str) or not text.strip():
        raise SignalError(model.E_MALFORMED, "text is required")
    if len(text) > MAX_TEXT_CHARS:
        raise SignalError(model.E_MALFORMED, "text exceeds bound")
    if source_kind not in SOURCE_KINDS:
        raise SignalError(model.E_MALFORMED, f"source_kind {source_kind!r}")
    if is_source_muted(root, source_ref):
        raise SignalError(model.E_MALFORMED, "source is muted")
    rule = source_rule(root, source_ref)
    if rule["mute"]:
        raise SignalError(model.E_MALFORMED, "source is muted")
    project_hint = project_hint or str(rule["project_id"])
    sensitivity = str(rule["sensitivity"] or sensitivity)
    received = now or datetime.now(UTC)
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    stamp = received.astimezone(UTC).replace(microsecond=0).isoformat(
        ).replace("+00:00", "Z")
    content_hash = _hash(text)
    signal_id = _signal_id(source_kind, source_ref, content_hash)
    for existing in read_signals(root):
        if existing.signal_id == signal_id:
            return {
                "signal": existing.to_dict(),
                "proposals": [proposal.to_dict()
                              for proposal in load_proposals(
                                  root, existing.signal_id)],
                "duplicate": True,
            }
    today = stamp[:10]
    daily = sum(1 for signal in read_signals(root)
                if signal.received_at[:10] == today)
    rules = load_rules(root)
    if daily >= int(rules["max_signals_per_day"]):
        raise SignalError(model.E_OVERFLOW, "daily signal limit reached")
    first_line = next((line.strip() for line in text.splitlines()
                       if line.strip()), text.strip())
    signal = InboundSignal(
        signal_id=signal_id, source_kind=source_kind,
        source_ref=source_ref, received_at=stamp,
        title=(title or first_line)[:MAX_TITLE_CHARS], text=text,
        content_hash=content_hash, status=STATUS_NEW,
        sensitivity=sensitivity, project_hint=project_hint).validate()
    append_signal(root, signal)
    proposals = analyze_signal(
        root, signal, today=received.date(),
        max_proposals=int(rule["max_proposals"]
                          or rules["max_proposals_per_signal"]))
    save_proposals(root, signal_id, proposals)
    analyzed = update_signal_status(root, signal_id, STATUS_ANALYZED)
    return {
        "signal": analyzed.to_dict(),
        "proposals": [proposal.to_dict() for proposal in proposals],
        "duplicate": False,
    }


def dismiss_signal(root: str | Path, signal_id: str) -> dict[str, Any]:
    signal = update_signal_status(root, signal_id, STATUS_DISMISSED)
    return {"signal": signal.to_dict()}


__all__ = [
    "DEFAULT_RULES", "EVIDENCE_NAME", "INBOX_DIR", "InboundSignal",
    "MAX_PROPOSALS", "MAX_SIGNALS_PER_DAY", "MAX_TEXT_CHARS",
    "OP_ADD_BLOCKER", "OP_CREATE_PROJECT", "OP_CREATE_TASK",
    "OP_RECORD_OUTCOME", "OP_RESOLVE_WAITING", "OP_SET_DEADLINE",
    "OP_SET_EFFORT", "OP_SET_STATUS", "OP_UPDATE_PROJECT", "OP_UPDATE_TASK",
    "PROPOSAL_OPS", "PROPOSAL_STATES", "PROPOSALS_DIR", "Proposal",
    "RULES_NAME", "SIGNALS_NAME", "SIGNAL_STATUSES", "SIGNAL_SCHEMA_VERSION",
    "SOURCE_CLIPBOARD", "SOURCE_EMAIL_TEXT", "SOURCE_FILE", "SOURCE_KINDS",
    "SOURCE_NOTE", "SOURCE_TEXT", "STATE_ACCEPTED", "STATE_DEFERRED",
    "STATE_PENDING", "STATE_REJECTED", "STATUS_ANALYZED", "STATUS_APPLIED",
    "STATUS_DISMISSED", "STATUS_DUPLICATE", "STATUS_MUTED", "STATUS_NEW",
    "SignalError", "TARGET_KINDS", "TARGET_NONE", "TARGET_PROJECT",
    "TARGET_TASK", "analyze_signal", "append_signal", "create_signal",
    "dismiss_signal", "evidence_path", "get_bundle", "get_signal",
    "inbox_dir", "is_source_muted", "list_bundles", "load_proposals",
    "load_rules", "mute_source", "proposals_dir", "read_signals",
    "rules_path", "save_proposals", "save_rules", "signals_path",
    "update_proposal", "update_signal_status",
]
