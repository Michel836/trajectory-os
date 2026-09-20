"""M064–M071 — shared epistemic vocabulary and safe primitives.

The one invariant that makes the real-world layer trustworthy is that a
statement is never detached from *how* it was obtained. Every surfaced
statement is a :class:`Claim` carrying a closed-set epistemic label:

``FACT``
    Stated directly by a user-supplied input (profile, brief, role
    description). Personal and company facts are only ever supplied.
``OBSERVATION``
    A dated record read from a supplied evidence set (news, notes, logs).
``EVIDENCE``
    An external evidence excerpt (company document, research source).
``INFERENCE``
    A deterministic/logical derivation from one or more claims; never a
    measurement.
``HYPOTHESIS``
    An unverified proposition offered for human confirmation; it carries no
    measured claim.

``UNKNOWN`` is *not* a claim label: it is a recorded unknown and is never
silently turned into a fact.

Nothing in this module reads a clock outside an injected callable, performs
network I/O, or touches a release Git surface.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from trajectory_os.intelligence import model as intel_model

#: Schema version of every durable M064–M071 document.
SCHEMA_VERSION = intel_model.SCHEMA_VERSION

#: Bundle version string (additive, human/machine readable).
REALWORLD_VERSION = "m064-m071.1"

#: Global upper bound on a single claim statement. This is the invariant that
#: keeps every persisted claim bounded; callers that need to preserve longer
#: source material must segment it rather than relax this bound.
MAX_CLAIM_STATEMENT_LEN = intel_model.MAX_STR_LEN * 8

#: Epistemic labels (closed set).
FACT = "FACT"
OBSERVATION = "OBSERVATION"
EVIDENCE = "EVIDENCE"
INFERENCE = "INFERENCE"
HYPOTHESIS = "HYPOTHESIS"

EPISTEMIC_LABELS = (FACT, OBSERVATION, EVIDENCE, INFERENCE, HYPOTHESIS)
EPISTEMIC_LABEL_SET = frozenset(EPISTEMIC_LABELS)

#: Labels that assert a supplied/measured basis.
GROUNDED_LABELS = frozenset({FACT, OBSERVATION, EVIDENCE})

#: Source kinds for a claim.
SRC_USER_INPUT = "USER_INPUT"
SRC_SUPPLIED_EVIDENCE = "SUPPLIED_EVIDENCE"
SRC_SOURCE_DOCUMENT = "SOURCE_DOCUMENT"
SRC_DETERMINISTIC_RULE = "DETERMINISTIC_RULE"
SRC_MODEL_PREDICTION = "MODEL_PREDICTION"

CLAIM_SOURCE_KINDS = frozenset({
    SRC_USER_INPUT, SRC_SUPPLIED_EVIDENCE, SRC_SOURCE_DOCUMENT,
    SRC_DETERMINISTIC_RULE, SRC_MODEL_PREDICTION,
})

# --- sensitive-content guard --------------------------------------------------

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(secret|password|passwd|api[_-]?key|private[_-]?key|"
    r"credential|access[_-]?token|auth[_-]?token|bearer)\b\s*[:=]\s*\S+")


def scan_for_secrets(text: str) -> list[str]:
    """Return secret-shaped assignment markers (conservative, bounded)."""
    return [match.group(0)[:64] for match in _SECRET_ASSIGNMENT.finditer(text)]


def redact_secrets(text: str) -> str:
    """Replace secret-shaped assignments with an explicit redaction marker."""
    return _SECRET_ASSIGNMENT.sub("[REDACTED_SECRET]", text)


# --- claim model --------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One typed, source-referenced statement."""

    statement: str
    label: str
    source_kind: str
    source_ref: str
    confidence: float | None = None
    note: str | None = None

    def validate(self) -> Claim:
        if self.label not in EPISTEMIC_LABEL_SET:
            intel_model.fail(intel_model.E_MALFORMED,
                             f"unknown epistemic label {self.label!r}")
        if self.source_kind not in CLAIM_SOURCE_KINDS:
            intel_model.fail(intel_model.E_MALFORMED,
                             f"unknown claim source {self.source_kind!r}")
        if not self.statement:
            intel_model.fail(intel_model.E_MALFORMED, "empty claim")
        if len(self.statement) > MAX_CLAIM_STATEMENT_LEN:
            intel_model.fail(intel_model.E_MALFORMED, "claim too long")
        if not self.source_ref:
            intel_model.fail(intel_model.E_MALFORMED,
                             "claim requires a source reference")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            intel_model.fail(intel_model.E_MALFORMED, "confidence out of range")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "label": self.label,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "confidence": self.confidence,
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: object) -> Claim:
        if not isinstance(data, Mapping):
            intel_model.fail(intel_model.E_MALFORMED,
                             "claim must be an object")
        confidence = data.get("confidence")
        return Claim(
            statement=str(data.get("statement", "")),
            label=str(data.get("label", "")),
            source_kind=str(data.get("source_kind", "")),
            source_ref=str(data.get("source_ref", "")),
            confidence=(float(confidence)
                        if isinstance(confidence, (int, float))
                        and not isinstance(confidence, bool) else None),
            note=(str(data["note"])
                  if data.get("note") is not None else None),
        ).validate()


@dataclass(frozen=True)
class Unknown:
    """An explicitly recorded unknown (never converted into a fact)."""

    description: str
    reason: str
    source_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"description": self.description, "reason": self.reason,
                "source_ref": self.source_ref}


def claims_of(claims: Sequence[Claim], label: str) -> tuple[Claim, ...]:
    return tuple(claim for claim in claims if claim.label == label)


def claim_counts(claims: Sequence[Claim]) -> dict[str, int]:
    counts = {label: 0 for label in EPISTEMIC_LABELS}
    for claim in claims:
        counts[claim.label] = counts.get(claim.label, 0) + 1
    return counts


@dataclass(frozen=True)
class Action:
    """A human-readable next action with an explicit rationale chain."""

    action: str
    rationale: str
    source: str
    urgency: str
    dependencies: tuple[str, ...] = ()
    uncertainty: str = "NONE"
    alternatives: tuple[str, ...] = ()
    kind: str = "TASK"
    expected_effort: str | None = None
    confidence: float | None = None

    def validate(self) -> Action:
        if not self.action or not self.rationale or not self.source:
            intel_model.fail(intel_model.E_MALFORMED,
                             "action requires action/rationale/source")
        if not self.urgency:
            intel_model.fail(intel_model.E_MALFORMED,
                             "action requires an urgency basis")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "rationale": self.rationale,
            "source": self.source,
            "urgency": self.urgency,
            "dependencies": list(self.dependencies),
            "uncertainty": self.uncertainty,
            "alternatives": list(self.alternatives),
            "kind": self.kind,
            "expected_effort": self.expected_effort,
            "confidence": self.confidence,
        }


# --- persistence helpers ------------------------------------------------------


def write_json(path: str, payload: Mapping[str, Any]) -> None:
    intel_model.write_json(path, payload)


def read_json(path: str) -> dict[str, Any] | None:
    return intel_model.read_json(path)


def append_jsonl(path: str, record: Mapping[str, Any]) -> None:
    intel_model.append_jsonl(path, record)


def read_jsonl(path: str) -> list[dict[str, Any]]:
    return intel_model.read_jsonl(path)


def utc_now() -> str:
    return intel_model.utc_now()


def digest(payload: object, *, domain: str) -> str:
    return intel_model.digest(payload, domain=domain)


__all__ = [
    "EPISTEMIC_LABELS",
    "EPISTEMIC_LABEL_SET",
    "EVIDENCE",
    "FACT",
    "GROUNDED_LABELS",
    "HYPOTHESIS",
    "INFERENCE",
    "MAX_CLAIM_STATEMENT_LEN",
    "OBSERVATION",
    "REALWORLD_VERSION",
    "SCHEMA_VERSION",
    "SRC_DETERMINISTIC_RULE",
    "SRC_MODEL_PREDICTION",
    "SRC_SOURCE_DOCUMENT",
    "SRC_SUPPLIED_EVIDENCE",
    "SRC_USER_INPUT",
    "Action",
    "Claim",
    "Unknown",
    "append_jsonl",
    "claim_counts",
    "claims_of",
    "digest",
    "intel_model",
    "read_json",
    "read_jsonl",
    "redact_secrets",
    "scan_for_secrets",
    "utc_now",
    "write_json",
]
