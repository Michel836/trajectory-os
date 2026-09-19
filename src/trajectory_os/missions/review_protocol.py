"""M026/M028 review-protocol — the reviewer semantic distinction (pure).

The independent reviewer emits a fixed prose protocol (``VERDICT`` /
``BLOCKERS`` / ``MAJORS`` / ``MINORS`` / ``FINAL RECOMMENDATION``). This module
normalizes that protocol into exactly one of three fail-closed outcomes:

* ``VALID_PASS`` — ``PASS`` + ``GO COMMIT`` + zero blocking findings
  (``BLOCKERS``/``MAJORS`` empty). Non-blocking ``MINORS`` notes are permitted;
* ``VALID_REJECT`` — ``REJECT`` (or the legacy ``FAIL``) + ``REPAIR`` (or the
  legacy ``FIX``) + at least one concrete blocker/major finding;
* ``REVIEW_PROTOCOL_INVALID`` — any missing/duplicated/out-of-order section,
  unknown verdict/recommendation, or contradiction (PASS with a blocking
  finding, REJECT with no blocker/major, PASS without GO COMMIT, REJECT
  without REPAIR).

The classifier is pure and deterministic and never repairs or guesses. The
legacy ``FAIL``/``FIX`` spellings are accepted as aliases so the shipped
``trajectory-pi`` reviewer parser stays compatible, but a FAIL with no
blocking finding is *invalid* under this strict distinction -- it can never
be promoted to a pass.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# --- outcomes (closed set) ----------------------------------------------------

OUTCOME_VALID_PASS = "VALID_PASS"
OUTCOME_VALID_REJECT = "VALID_REJECT"
OUTCOME_INVALID = "REVIEW_PROTOCOL_INVALID"

OUTCOMES = frozenset({
    OUTCOME_VALID_PASS, OUTCOME_VALID_REJECT, OUTCOME_INVALID,
})

# --- stable reason codes ------------------------------------------------------

R_VALID_PASS = "PASS_GO_COMMIT_NO_BLOCKING_FINDINGS"
R_VALID_REJECT = "REJECT_REPAIR_WITH_BLOCKING_FINDING"
R_MISSING = "MISSING_REQUIRED_HEADING"
R_DUPLICATE = "DUPLICATE_REQUIRED_HEADING"
R_ORDER = "REQUIRED_HEADING_OUT_OF_ORDER"
R_UNKNOWN_VERDICT = "UNKNOWN_VERDICT"
R_UNKNOWN_RECOMMENDATION = "UNKNOWN_RECOMMENDATION"
R_PASS_WITH_BLOCKERS = "PASS_WITH_BLOCKING_FINDINGS"
R_PASS_RECOMMENDATION = "PASS_WITHOUT_GO_COMMIT"
R_REJECT_WITHOUT_FINDINGS = "REJECT_WITHOUT_BLOCKING_FINDINGS"
R_REJECT_RECOMMENDATION = "REJECT_WITHOUT_REPAIR"

# --- bounded limits -----------------------------------------------------------

MAX_RESPONSE_BYTES = 512 * 1024
MAX_FINDINGS = 512
MAX_FINDING_LEN = 2048

_HEADINGS = ("VERDICT", "BLOCKERS", "MAJORS", "MINORS",
             "FINAL RECOMMENDATION")
_SECTION_HEADINGS = ("BLOCKERS", "MAJORS", "MINORS")

_HEADING_RE = re.compile(
    r"(?m)^\s*(VERDICT|BLOCKERS|MAJORS|MINORS|FINAL RECOMMENDATION)"
    r"\s*:(?P<rest>.*)$"
)
_NONE_RE = re.compile(r"^[-*]\s*none\.?\s*$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^[-*]\s+(?P<item>\S.*)$")

_VERDICT_ALIASES = {
    "PASS": "PASS",
    "REJECT": "REJECT",
    "FAIL": "REJECT",
}
_RECOMMENDATION_ALIASES = {
    "GO COMMIT": "GO_COMMIT",
    "REPAIR": "REPAIR",
    "FIX": "REPAIR",
    "STOP": "STOP",
}


class ReviewProtocolError(ValueError):
    """The response exceeds a hard bound and cannot be classified."""


@dataclass(frozen=True)
class ReviewAssessment:
    """Normalized reviewer-protocol assessment (pure, fail closed)."""

    outcome: str
    reason: str
    verdict: str | None
    recommendation: str | None
    blockers: tuple[str, ...]
    majors: tuple[str, ...]
    minors: tuple[str, ...]
    classification_id: str = ""

    @property
    def blocking_count(self) -> int:
        return len(self.blockers) + len(self.majors)

    @property
    def valid(self) -> bool:
        return self.outcome in (OUTCOME_VALID_PASS, OUTCOME_VALID_REJECT)

    @property
    def permits_commit(self) -> bool:
        return self.outcome == OUTCOME_VALID_PASS

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "outcome": self.outcome,
            "reason": self.reason,
            "verdict": self.verdict,
            "recommendation": self.recommendation,
            "blockers": list(self.blockers),
            "majors": list(self.majors),
            "minors": list(self.minors),
        }

    def compute_classification_id(self) -> str:
        payload = json.dumps(
            self.identity_payload(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=True)
        return hashlib.sha256(
            b"trajectory-os.review-protocol.v1\x00" + payload.encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "classification_id": self.classification_id,
            "blocking_count": self.blocking_count,
            "valid": self.valid,
            "permits_commit": self.permits_commit,
        }


def _finding(text: str) -> str:
    cleaned = text.strip()
    return cleaned[:MAX_FINDING_LEN]


def _section_findings(body: list[str]) -> list[str]:
    findings: list[str] = []
    for raw in body:
        line = raw.strip()
        if not line or _NONE_RE.match(line):
            continue
        match = _BULLET_RE.match(line)
        item = match.group("item") if match else line
        findings.append(_finding(item))
    return findings[:MAX_FINDINGS]


def _section_body(text: str, start: int, end: int) -> list[str]:
    return [raw for raw in text[start:end].splitlines() if raw.strip()]


def assess(text: object) -> ReviewAssessment:
    """Classify one reviewer response (pure, deterministic, fail closed)."""
    if not isinstance(text, str):
        return _invalid("UNREADABLE_RESPONSE", R_MISSING, None, None)
    if len(text.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
        raise ReviewProtocolError("reviewer response exceeds hard bound")

    occurrences = [
        (m.start(), m.end(), m.group(1), m.group("rest").strip())
        for m in _HEADING_RE.finditer(text)
    ]
    occurrences.sort(key=lambda item: item[0])

    def indices(name: str) -> list[int]:
        return [i for i, item in enumerate(occurrences) if item[2] == name]

    counts = {name: len(indices(name)) for name in _HEADINGS}
    for name in _HEADINGS:
        if counts[name] == 0:
            return _invalid("MISSING_SECTION", R_MISSING, None, None)
        if counts[name] > 1:
            return _invalid("DUPLICATE_SECTION", R_DUPLICATE, None, None)

    positions = [indices(name)[0] for name in _HEADINGS]
    if positions != sorted(positions):
        return _invalid("OUT_OF_ORDER", R_ORDER, None, None)

    bodies: dict[str, list[str]] = {}
    for name in _HEADINGS:
        idx = indices(name)[0]
        end = occurrences[idx + 1][0] if idx + 1 < len(occurrences) \
            else len(text)
        bodies[name] = _section_body(text, occurrences[idx][1], end)

    raw_verdict = occurrences[indices("VERDICT")[0]][3].upper()
    raw_recommendation = occurrences[indices("FINAL RECOMMENDATION")[0]][3] \
        .upper()
    verdict = _VERDICT_ALIASES.get(raw_verdict)
    recommendation = _RECOMMENDATION_ALIASES.get(raw_recommendation)
    if verdict is None:
        return _invalid("UNKNOWN_VERDICT", R_UNKNOWN_VERDICT, None, None)
    if recommendation is None:
        return _invalid("UNKNOWN_RECOMMENDATION", R_UNKNOWN_RECOMMENDATION,
                        verdict, None)

    blockers = tuple(_section_findings(bodies["BLOCKERS"]))
    majors = tuple(_section_findings(bodies["MAJORS"]))
    minors = tuple(_section_findings(bodies["MINORS"]))

    if verdict == "PASS":
        if recommendation != "GO_COMMIT":
            return _invalid("PASS_WITHOUT_GO_COMMIT", R_PASS_RECOMMENDATION,
                            verdict, recommendation, blockers, majors, minors)
        if blockers or majors:
            return _invalid("PASS_WITH_BLOCKING_FINDINGS",
                            R_PASS_WITH_BLOCKERS, verdict, recommendation,
                            blockers, majors, minors)
        return _make(OUTCOME_VALID_PASS, R_VALID_PASS, verdict,
                     recommendation, blockers, majors, minors)

    # verdict == REJECT
    if recommendation != "REPAIR":
        return _invalid("REJECT_WITHOUT_REPAIR", R_REJECT_RECOMMENDATION,
                        verdict, recommendation, blockers, majors, minors)
    if not blockers and not majors:
        return _invalid("REJECT_WITHOUT_BLOCKING_FINDINGS",
                        R_REJECT_WITHOUT_FINDINGS, verdict, recommendation,
                        blockers, majors, minors)
    return _make(OUTCOME_VALID_REJECT, R_VALID_REJECT, verdict,
                 recommendation, blockers, majors, minors)


def _make(outcome: str, reason: str, verdict: str | None,
          recommendation: str | None, blockers: tuple[str, ...] = (),
          majors: tuple[str, ...] = (), minors: tuple[str, ...] = (),
          ) -> ReviewAssessment:
    base = ReviewAssessment(
        outcome=outcome, reason=reason, verdict=verdict,
        recommendation=recommendation, blockers=blockers, majors=majors,
        minors=minors)
    return replace(base,
                   classification_id=base.compute_classification_id())


def _invalid(detail: str, reason: str, verdict: str | None,
             recommendation: str | None, blockers: tuple[str, ...] = (),
             majors: tuple[str, ...] = (),
             minors: tuple[str, ...] = ()) -> ReviewAssessment:
    assessment = _make(OUTCOME_INVALID, reason, verdict, recommendation,
                       blockers, majors, minors)
    return replace(assessment, reason=reason)


def assess_file(path: str | Path) -> ReviewAssessment:
    """Read (bounded) and classify one reviewer response file."""
    data = Path(path).read_bytes()
    if len(data) > MAX_RESPONSE_BYTES:
        raise ReviewProtocolError("reviewer response exceeds hard bound")
    return assess(data.decode("utf-8", errors="replace"))


def classify_parser_output(parsed: Mapping[str, str]) -> ReviewAssessment:
    """Map the shipped ``trajectory-pi`` parser output to the distinction.

    ``parsed`` carries the ``RESULT``/``VERDICT``/``RECOMMENDATION`` keys the
    shell parser emits. A ``REJECTED`` parser result (its own fail-closed
    verdict for contradictions/malformed structure) maps to
    ``REVIEW_PROTOCOL_INVALID`` even when the reviewer text happened to spell
    a syntactically valid verdict.
    """
    result = (parsed.get("RESULT") or "").upper()
    verdict = (parsed.get("VERDICT") or "").upper() or None
    recommendation = (parsed.get("RECOMMENDATION") or "").upper() or None
    if result == "REJECTED":
        return _invalid("PARSER_REJECTED", R_MISSING, verdict, recommendation)
    if result == "PASS":
        return _make(OUTCOME_VALID_PASS, R_VALID_PASS, verdict,
                     recommendation)
    # result == FAIL (only remaining parser result)
    blocking = verdict in ("REJECT", "FAIL")
    if recommendation in ("REPAIR", "FIX") and blocking:
        # The parser does not expose finding text; a FAIL+FIX is a reject,
        # but without concrete blocking findings it is unproven and stays
        # invalid.
        return _invalid("REJECT_FINDINGS_UNPROVEN", R_REJECT_WITHOUT_FINDINGS,
                        verdict, recommendation)
    return _invalid("PARSER_FAIL", R_MISSING, verdict, recommendation)
