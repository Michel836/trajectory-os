"""M061 — practical, reusable workflow families over the intelligence stack.

Three families are implemented end-to-end. Each consumes workflow inputs plus
the M060 knowledge workspace and produces a durable, human-readable
deliverable with an auditable evidence chain and next actions:

* :func:`run_career_workflow` — career / consulting (company/role/brief
  analysis, requirement extraction, experience mapping, evidence-based value
  proposition, candidacy artifact);
* :func:`run_life_sciences_workflow` — life-sciences/pharma monitoring
  (event/theme classification, trends, recurring problems, AI/Data
  opportunity candidates, mini business case);
* :func:`run_research_workflow` — research/strategy (question decomposition,
  evidence retrieval, contradiction surfacing, uncertainty, synthesis).

The workflows **never invent personal or client facts**: every statement is
either a ``FACT`` traceable to a supplied input/source, a clearly-labelled
``HEURISTIC``, or an explicitly-``HYPOTHESIS`` value that carries no measured
claim. They reuse the platform by accepting a project id and mission id for
linkage and by emitting LifeOS-compatible markdown notes; they never perform
a release Git write.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from trajectory_os.intelligence import knowledge, model

#: Workflow families (closed set of implemented families).
WF_CAREER = "CAREER_CONSULTING"
WF_LIFE_SCIENCES = "LIFE_SCIENCES_PHARMA"
WF_RESEARCH = "RESEARCH_STRATEGY"

WORKFLOW_FAMILIES = frozenset({WF_CAREER, WF_LIFE_SCIENCES, WF_RESEARCH})

#: Evidence classifications (closed set).
FACT = "FACT"
HEURISTIC = "HEURISTIC"
HYPOTHESIS = "HYPOTHESIS"
PREDICTION = "PREDICTION"

EVIDENCE_KINDS = frozenset({FACT, HEURISTIC, HYPOTHESIS, PREDICTION})

#: Input source kinds.
SRC_USER_INPUT = "USER_INPUT"
SRC_SOURCE_DOCUMENT = "SOURCE_DOCUMENT"
SRC_RULE = "DETERMINISTIC_RULE"
SRC_MODEL = "MODEL_PREDICTION"

MAX_ITEMS = 200
MAX_TEXT = 20_000

_WORD_RE = re.compile(r"[a-z0-9]+")
_NUMBER_RE = re.compile(r"(?P<value>\d[\d,\.]*)\s*(?P<unit>%|percent|"
                        r"million|billion|bn|m|k|days?|weeks?|months?|"
                        r"patients?|sites?)?")


@dataclass(frozen=True)
class EvidenceItem:
    """One auditable statement with an explicit classification."""

    statement: str
    kind: str
    source_kind: str
    source_ref: str

    def validate(self) -> EvidenceItem:
        if self.kind not in EVIDENCE_KINDS:
            model.fail(model.E_MALFORMED, f"unknown kind {self.kind!r}")
        if not self.statement:
            model.fail(model.E_MALFORMED, "statement required")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"statement": self.statement, "kind": self.kind,
                "source_kind": self.source_kind, "source_ref": self.source_ref}


@dataclass(frozen=True)
class NextAction:
    action: str
    rationale: str
    kind: str = "TASK"

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "rationale": self.rationale,
                "kind": self.kind}


@dataclass(frozen=True)
class WorkflowResult:
    """The durable output of one workflow run."""

    workflow_id: str
    family: str
    title: str
    project_id: str | None
    mission_id: str | None
    inputs_are_fixture: bool
    deliverable_markdown: str
    artifact: Mapping[str, Any]
    evidence: tuple[EvidenceItem, ...]
    unknowns: tuple[str, ...]
    next_actions: tuple[NextAction, ...]
    generated_at: str
    canonical: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "kind": "workflow_result",
            "workflow_id": self.workflow_id,
            "family": self.family,
            "title": self.title,
            "project_id": self.project_id,
            "mission_id": self.mission_id,
            "inputs_are_fixture": self.inputs_are_fixture,
            "deliverable_markdown": self.deliverable_markdown,
            "artifact": dict(self.artifact),
            "evidence": [e.to_dict() for e in self.evidence],
            "unknowns": list(self.unknowns),
            "next_actions": [a.to_dict() for a in self.next_actions],
            "generated_at": self.generated_at,
            "canonical": self.canonical,
            "reuse": {
                "project_registry": self.project_id,
                "canonical_mission": self.mission_id,
                "scheduler": "advisory-only (M059)",
                "artifacts": "workflow artifact document",
                "observability": "workflow result document",
                "lifeos": "markdown note projection",
            },
        }


def _normalise(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _truncate(text: str, maximum: int = MAX_TEXT) -> str:
    return text if len(text) <= maximum else text[:maximum]


# --- career / consulting ------------------------------------------------------


@dataclass(frozen=True)
class CareerBrief:
    """User-supplied career/consulting inputs (never augmented)."""

    company: str
    role: str
    brief_text: str
    experiences: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    links: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"company": self.company, "role": self.role,
                "brief_chars": len(self.brief_text),
                "experience_count": len(self.experiences),
                "constraints": list(self.constraints),
                "links": list(self.links)}


_REQUIREMENT_HINTS = (
    "must", "require", "required", "responsib", "experience", "proficien",
    "familiar", "ability", "knowledge", "degree", "years", "lead", "own",
    "build", "design", "manage", "deliver",
)

_PROBLEM_HINTS: Mapping[str, str] = {
    "scale": "scaling current systems/operations",
    "cost": "cost pressure or margin improvement",
    "data": "data quality, governance or availability",
    "compliance": "regulatory/compliance burden",
    "legacy": "legacy modernisation",
    "migration": "platform/data migration",
    "customer": "customer experience or retention",
    "efficiency": "operational efficiency",
    "risk": "risk management",
    "ai": "AI/automation adoption",
}


def extract_requirements(brief_text: str) -> tuple[str, ...]:
    """Extract requirement-like statements from a brief (deterministic)."""
    candidates: list[str] = []
    for raw_line in brief_text.splitlines():
        line = raw_line.strip().lstrip("-*•0123456789. ").strip()
        if not line or len(line) < 12:
            continue
        lowered = line.lower()
        if raw_line.strip().startswith(("-", "*", "•")) or any(
                hint in lowered for hint in _REQUIREMENT_HINTS):
            candidates.append(_truncate(line, 400))
    if not candidates:
        for sentence in re.split(r"(?<=[.!?])\s+", brief_text):
            stripped = sentence.strip()
            if len(stripped) >= 20:
                candidates.append(_truncate(stripped, 400))
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _normalise(candidate)
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return tuple(deduped[:MAX_ITEMS])


def identify_business_problems(brief_text: str) -> tuple[dict[str, Any], ...]:
    """Heuristic plausible business problems (explicitly labelled)."""
    lowered = brief_text.lower()
    found: list[dict[str, Any]] = []
    for keyword, description in sorted(_PROBLEM_HINTS.items()):
        if keyword in lowered:
            found.append({
                "problem": description,
                "matched_signal": keyword,
                "classification": HEURISTIC,
                "note": "signal-keyword heuristic, not a measured finding",
            })
    return tuple(found[:MAX_ITEMS])


def map_experience(requirements: Sequence[str],
                   experiences: Sequence[str],
                   ) -> tuple[dict[str, Any], ...]:
    """Map only user-supplied experiences to requirements (no invention)."""
    mapped: list[dict[str, Any]] = []
    for requirement in requirements:
        requirement_tokens = _tokens(requirement)
        matches: list[str] = []
        for experience in experiences:
            overlap = requirement_tokens & _tokens(experience)
            if len(overlap) >= 2:
                matches.append(experience)
        mapped.append({
            "requirement": requirement,
            "matched_experiences": matches,
            "status": "MATCHED" if matches else "GAP",
        })
    return tuple(mapped)


def run_career_workflow(
    brief: CareerBrief, *, root: str, workflow_id: str = "career",
    project_id: str | None = None, mission_id: str | None = None,
    inputs_are_fixture: bool = False,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> WorkflowResult:
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    requirements = extract_requirements(brief.brief_text)
    problems = identify_business_problems(brief.brief_text)
    mapping = map_experience(requirements, brief.experiences)
    matched = [m for m in mapping if m["status"] == "MATCHED"]
    gaps = [m for m in mapping if m["status"] == "GAP"]

    evidence: list[EvidenceItem] = []
    for index, requirement in enumerate(requirements):
        evidence.append(EvidenceItem(
            statement=requirement, kind=FACT, source_kind=SRC_USER_INPUT,
            source_ref=f"brief:{workflow_id}:req:{index}").validate())
    for index, experience in enumerate(brief.experiences):
        evidence.append(EvidenceItem(
            statement=experience, kind=FACT, source_kind=SRC_USER_INPUT,
            source_ref=f"experience:{workflow_id}:{index}").validate())
    for problem in problems:
        evidence.append(EvidenceItem(
            statement=str(problem["problem"]), kind=HEURISTIC,
            source_kind=SRC_RULE,
            source_ref=f"rule:problem-hint:{problem['matched_signal']}"
            ).validate())

    value_proposition = [
        f"**{requirement['requirement']}** — demonstrated by: "
        + "; ".join(requirement["matched_experiences"])
        for requirement in matched]
    unknowns: list[str] = []
    if not brief.experiences:
        unknowns.append(
            "no user-supplied experience assets were provided; the value "
            "proposition is intentionally empty rather than invented")
    for gap in gaps[:10]:
        unknowns.append(f"requirement not evidenced by supplied assets: "
                        f"{gap['requirement']}")
    if not requirements:
        unknowns.append("the brief did not yield extractable requirements")

    lines = [
        f"# Career / consulting analysis — {brief.role} at {brief.company}",
        "",
        f"- workflow: `{workflow_id}`",
        f"- inputs marked as fixture/sample: {inputs_are_fixture}",
        f"- project linkage: {project_id or '-'} / mission "
        f"{mission_id or '-'}",
        "",
        "## Role snapshot",
        f"- company: {brief.company}",
        f"- role: {brief.role}",
        "- declared constraints: "
        + (", ".join(brief.constraints) if brief.constraints else "none"),
        "",
        "## Extracted requirements (FACT — from the supplied brief)",
    ]
    lines.extend(f"- {r}" for r in requirements) if requirements else lines.append(
        "- none extracted from the supplied brief")
    lines += ["", "## Plausible business problems (HEURISTIC — signals only)"]
    lines.extend(
        f"- {p['problem']} (signal: `{p['matched_signal']}`)" for p in problems
    ) if problems else lines.append("- no signal keywords matched")
    lines += ["", "## Experience map (supplied assets only)"]
    for item in mapping:
        marker = "MATCHED" if item["status"] == "MATCHED" else "GAP"
        detail = ("; ".join(item["matched_experiences"])
                  if item["matched_experiences"] else "no supplied evidence")
        lines.append(f"- [{marker}] {item['requirement']} — {detail}")
    lines += ["", "## Evidence-based value proposition"]
    lines.extend(f"- {bullet}" for bullet in value_proposition) if (
        value_proposition) else lines.append(
        "- not provided: no user-supplied experience assets to evidence it")
    lines += ["", "## Unknowns / gaps"]
    lines.extend(f"- {item}" for item in unknowns) if unknowns else lines.append(
        "- none recorded")
    lines += ["", "## Next actions"]
    actions = _career_actions(requirements, gaps, brief)
    lines.extend(f"- {action.action} — {action.rationale}" for action in actions)
    lines += ["", "## Provenance",
              "- every FACT traces to a supplied input; HEURISTIC items are "
              "deterministic signal matches; HYPOTHESIS items carry no "
              "measured claim."]
    deliverable = "\n".join(lines) + "\n"
    artifact = {
        "requirements": list(requirements),
        "business_problems": list(problems),
        "experience_map": list(mapping),
        "value_proposition": value_proposition,
        "gaps": [g["requirement"] for g in gaps],
    }
    result = WorkflowResult(
        workflow_id=workflow_id, family=WF_CAREER,
        title=f"Career analysis: {brief.role} at {brief.company}",
        project_id=project_id, mission_id=mission_id,
        inputs_are_fixture=inputs_are_fixture,
        deliverable_markdown=deliverable, artifact=artifact,
        evidence=tuple(evidence), unknowns=tuple(unknowns),
        next_actions=actions, generated_at=stamp)
    return persist_workflow(root, result)


def _career_actions(requirements: Sequence[str],
                    gaps: Sequence[Mapping[str, Any]],
                    brief: CareerBrief) -> tuple[NextAction, ...]:
    actions: list[NextAction] = []
    if gaps:
        actions.append(NextAction(
            action="Supply concrete evidence for the unmatched requirements",
            rationale=f"{len(gaps)} requirement(s) have no supplied asset",
            kind="INPUT_REQUIRED"))
    if brief.links:
        actions.append(NextAction(
            action="Verify the supplied portfolio/reference links resolve",
            rationale="links were provided and should be validated before use"))
    actions.append(NextAction(
        action="Draft the candidacy/portfolio artifact from matched evidence",
        rationale="only MATCHED requirements may appear as claims"))
    actions.append(NextAction(
        action="Confirm the brief's implicit business problem with the client",
        rationale="business problems are heuristic and need human confirmation",
        kind="HUMAN_DECISION"))
    return tuple(actions)


# --- life sciences / pharma ---------------------------------------------------


@dataclass(frozen=True)
class MonitoringRecord:
    source_ref: str
    date: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"source_ref": self.source_ref, "date": self.date,
                "text_chars": len(self.text)}


_LS_THEMES: Mapping[str, tuple[str, ...]] = {
    "oncology": ("oncology", "tumour", "tumor", "carcinoma", "car-t", "cart"),
    "clinical_trial": ("trial", "phase 1", "phase 2", "phase 3", "endpoint",
                       "randomis", "randomiz"),
    "regulatory": ("fda", "ema", "approval", "regulatory", "label",
                   "submission", "chmp"),
    "supply_chain": ("supply", "shortage", "manufactur", "capacity",
                     "logistics", "recall"),
    "market_access": ("reimburs", "payer", "pricing", "access", "hta"),
    "safety": ("adverse", "safety", "signal", "pharmacovigilance", "toxicity"),
    "ai_data": ("artificial intelligence", "machine learning", " ai ",
                "real-world", "real world", "biomarker", "data"),
}

_LS_OPPORTUNITIES: Mapping[str, dict[str, str]] = {
    "oncology": {
        "opportunity": "Precision-oncology patient identification",
        "data": "Real-world genomics + claims data",
        "value_hypothesis": "higher trial enrichment / faster enrolment",
    },
    "clinical_trial": {
        "opportunity": "Trial feasibility and site-selection analytics",
        "data": "Historical trial + EHR site performance data",
        "value_hypothesis": "fewer non-enrolling sites, shorter timelines",
    },
    "regulatory": {
        "opportunity": "Regulatory intelligence document automation",
        "data": "Submission and guidance corpora",
        "value_hypothesis": "less manual review effort",
    },
    "supply_chain": {
        "opportunity": "Supply-risk early-warning model",
        "data": "Shipment, inventory and shortage signals",
        "value_hypothesis": "earlier mitigation of shortages",
    },
    "market_access": {
        "opportunity": "Payer evidence package assembly",
        "data": "RWE, outcomes and claims evidence",
        "value_hypothesis": "faster, more consistent HTA submissions",
    },
    "safety": {
        "opportunity": "Safety-signal triage assistance",
        "data": "Case narratives and literature",
        "value_hypothesis": "faster signal review with human oversight",
    },
    "ai_data": {
        "opportunity": "Data-product foundation for AI reuse",
        "data": "Governed cross-study data assets",
        "value_hypothesis": "reusable data products, lower marginal cost",
    },
}


def classify_life_sciences_text(text: str) -> tuple[str, ...]:
    lowered = f" {text.lower()} "
    return tuple(sorted(theme for theme, keys in _LS_THEMES.items()
                        if any(key in lowered for key in keys)))


def run_life_sciences_workflow(
    records: Sequence[MonitoringRecord], *, root: str,
    workflow_id: str = "life-sciences", title: str = "Monitoring digest",
    project_id: str | None = None, mission_id: str | None = None,
    inputs_are_fixture: bool = False,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> WorkflowResult:
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    evidence: list[EvidenceItem] = []
    theme_counts: dict[str, int] = {}
    dated_themes: dict[str, dict[str, int]] = {}
    for index, record in enumerate(records):
        ref = f"monitoring:{workflow_id}:{index}"
        evidence.append(EvidenceItem(
            statement=_truncate(record.text, 400), kind=FACT,
            source_kind=SRC_SOURCE_DOCUMENT,
            source_ref=f"{ref} ({record.source_ref})").validate())
        themes = classify_life_sciences_text(record.text)
        for theme in themes:
            theme_counts[theme] = theme_counts.get(theme, 0) + 1
            dated_themes.setdefault(theme, {})
            dated_themes[theme][record.date] = (
                dated_themes[theme].get(record.date, 0) + 1)
    trending = tuple(sorted(
        (theme for theme, by_date in dated_themes.items()
         if len({date[:7] for date in by_date}) >= 2),
        key=lambda theme: (-theme_counts[theme], theme)))
    recurring = tuple(sorted(
        (theme for theme, count in theme_counts.items() if count >= 2),
        key=lambda theme: (-theme_counts[theme], theme)))
    opportunities: list[dict[str, Any]] = []
    for theme in recurring:
        template = _LS_OPPORTUNITIES.get(theme)
        if template is None:
            continue
        opportunities.append({
            "theme": theme,
            "opportunity": template["opportunity"],
            "required_data": template["data"],
            "value_hypothesis": template["value_hypothesis"],
            "classification": HYPOTHESIS,
            "evidence_count": theme_counts[theme],
            "note": "opportunity catalogue mapping; value is a hypothesis, "
                    "not a measured result",
        })
        evidence.append(EvidenceItem(
            statement=f"{template['opportunity']} ({theme})",
            kind=HYPOTHESIS, source_kind=SRC_RULE,
            source_ref=f"rule:opportunity:{theme}").validate())

    lines = [
        f"# Life-sciences / pharma intelligence — {title}", "",
        f"- records analysed: {len(records)}",
        f"- inputs marked as fixture/sample: {inputs_are_fixture}",
        "- theme counts: "
        + (", ".join(f"{t}={theme_counts[t]}" for t in
                     sorted(theme_counts, key=lambda x: -theme_counts[x]))
           or "none"),
        "",
        "## Classified themes (FACT — source text signals)",
    ]
    lines.extend(f"- {theme}: {theme_counts[theme]} record(s)"
                 for theme in sorted(theme_counts,
                                     key=lambda x: (-theme_counts[x], x))
                 ) if theme_counts else lines.append("- no themes matched")
    lines += ["", "## Trends (>=2 distinct months of evidence)",
              *([f"- {theme}" for theme in trending] if trending
                else ["- no multi-period trend is supportable"])]
    lines += ["", "## Recurring business problems",
              *([f"- {theme} ({theme_counts[theme]} records)"
                 for theme in recurring] if recurring
                else ["- no recurring theme reached the threshold"])]
    lines += ["", "## AI/Data opportunity candidates (HYPOTHESIS)"]
    for item in opportunities:
        lines.append(
            f"- **{item['opportunity']}** — theme `{item['theme']}`; "
            f"data: {item['required_data']}; value hypothesis: "
            f"{item['value_hypothesis']}")
    if not opportunities:
        lines.append("- none: no recurring theme mapped to the catalogue")
    lines += ["", "## Mini business case"]
    if opportunities:
        top = opportunities[0]
        lines += [
            f"- problem: {top['theme']} (recurring across "
            f"{top['evidence_count']} sources)",
            f"- proposed opportunity: {top['opportunity']}",
            f"- required data: {top['required_data']}",
            f"- value hypothesis (not measured): {top['value_hypothesis']}",
            "- effort: unresolved — requires a scoping estimate",
            "- risks: data access, governance and validation bias",
            "- next step: validate the problem with a domain owner before "
            "committing effort",
        ]
    else:
        lines.append("- insufficient recurring evidence to support a case")
    lines += ["", "## Unknowns"]
    unknowns = ["effort and cost are not measured here",
                "value statements are hypotheses, not forecasts"]
    if not records:
        unknowns.append("no monitoring records were supplied")
    lines.extend(f"- {item}" for item in unknowns)
    lines += ["", "## Next actions"]
    actions = (
        NextAction(action="Validate the top recurring theme with a domain "
                          "owner",
                   rationale="theme classification is deterministic but "
                             "domain confirmation is required",
                   kind="HUMAN_DECISION"),
        NextAction(action="Scope data availability for the leading "
                          "opportunity",
                   rationale="data access determines feasibility"),
        NextAction(action="Add more monitoring periods to support a trend",
                   rationale="trend claims require multiple periods"),
    )
    lines.extend(f"- {action.action} — {action.rationale}" for action in actions)
    lines += ["", "## Provenance",
              "- every FACT traces to a supplied monitoring record; "
              "opportunities are HYPOTHESES from an explicit rule catalogue."]
    artifact = {
        "theme_counts": theme_counts,
        "trending_themes": list(trending),
        "recurring_themes": list(recurring),
        "opportunities": opportunities,
    }
    result = WorkflowResult(
        workflow_id=workflow_id, family=WF_LIFE_SCIENCES, title=title,
        project_id=project_id, mission_id=mission_id,
        inputs_are_fixture=inputs_are_fixture,
        deliverable_markdown="\n".join(lines) + "\n", artifact=artifact,
        evidence=tuple(evidence), unknowns=tuple(unknowns),
        next_actions=actions, generated_at=stamp)
    return persist_workflow(root, result)


# --- research / strategy ------------------------------------------------------


def decompose_question(question: str) -> tuple[str, ...]:
    """Deterministic decomposition into sub-questions (HEURISTIC framing)."""
    parts = [part.strip() for part in re.split(
        r"\band\b|;|\?", question) if part.strip()]
    sub_questions: list[str] = []
    for part in parts:
        if len(part) >= 8:
            sub_questions.append(part if part.endswith("?") else part + "?")
    templates = (
        "What evidence supports the current answer?",
        "What evidence contradicts the current answer?",
        "What is still unknown or uncertain?",
    )
    for template in templates:
        sub_questions.append(template)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in sub_questions:
        key = _normalise(item)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return tuple(deduped[:MAX_ITEMS])


def _numeric_claims(text: str) -> tuple[tuple[str, str], ...]:
    claims: list[tuple[str, str]] = []
    for match in _NUMBER_RE.finditer(text):
        value = match.group("value").replace(",", "")
        unit = match.group("unit") or ""
        claims.append((value, unit))
    return tuple(claims)


def run_research_workflow(
    question: str, *, root: str, adapters: Sequence[knowledge.SourceAdapter] = (),
    sub_questions: Sequence[str] = (),
    workflow_id: str = "research", project_id: str | None = None,
    mission_id: str | None = None, inputs_are_fixture: bool = False,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> WorkflowResult:
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    index = knowledge.build_index(adapters, generated_at=stamp)
    plan = tuple(sub_questions) if sub_questions else decompose_question(
        question)
    evidence: list[EvidenceItem] = []
    synthesis: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []
    retrieved_chunks: dict[str, knowledge.Chunk] = {}
    for sub_question in plan:
        trace = knowledge.retrieve(index, sub_question, top_k=3,
                                   generated_at=stamp)
        for result_chunk in trace.results:
            retrieved_chunks[result_chunk.chunk.chunk_id] = result_chunk.chunk
        synthesis.append({
            "sub_question": sub_question,
            "status": trace.status,
            "citations": [
                {"source_ref": r.chunk.source_ref,
                 "chunk_id": r.chunk.chunk_id,
                 "score": round(r.score, 4)}
                for r in trace.results],
            "excerpts": [
                {"source_ref": r.chunk.source_ref,
                 "text": _truncate(r.chunk.text, 240)}
                for r in trace.results],
        })
        for r in trace.results:
            evidence.append(EvidenceItem(
                statement=_truncate(r.chunk.text, 300), kind=FACT,
                source_kind=SRC_SOURCE_DOCUMENT,
                source_ref=f"{r.chunk.source_ref}#{r.chunk.ordinal}"
                ).validate())
    # Contradictions: same-unit numeric claims with different values.
    by_unit: dict[str, list[tuple[str, str]]] = {}
    for chunk in index.chunks:
        for value, unit in _numeric_claims(chunk.text):
            if not unit:
                continue
            by_unit.setdefault(unit, []).append((value, chunk.chunk_id))
    for unit, claims in sorted(by_unit.items()):
        values = {value for value, _ in claims}
        if len(values) < 2:
            continue
        ordered = sorted(claims)
        left, right = ordered[0], ordered[-1]
        if left[1] == right[1]:
            continue
        chunk_a = retrieved_chunks.get(left[1])
        chunk_b = retrieved_chunks.get(right[1])
        if chunk_a is None or chunk_b is None:
            continue
        contradiction = knowledge.record_contradiction(
            index, left_chunk_id=left[1], right_chunk_id=right[1],
            kind=knowledge.CT_NUMERIC,
            description=(f"numeric claims with unit '{unit}' differ: "
                         f"{left[0]} vs {right[0]}"))
        contradictions.append({**contradiction.to_dict(),
                               "left_source": chunk_a.source_ref,
                               "right_source": chunk_b.source_ref})
    unknowns: list[str] = []
    no_evidence = [item["sub_question"] for item in synthesis
                   if item["status"] == knowledge.NO_EVIDENCE]
    if no_evidence:
        unknowns.extend(
            f"no source evidence for: {item}" for item in no_evidence)
    if contradictions:
        unknowns.append(
            f"{len(contradictions)} numeric contradiction(s) remain "
            "unresolved and require human review")
    if not adapters:
        unknowns.append("no evidence adapters were supplied")

    lines = [
        "# Research / strategy analysis", "", f"- question: {question}",
        f"- inputs marked as fixture/sample: {inputs_are_fixture}",
        f"- index: {index.index_id[:16]}… ({index.document_count} docs / "
        f"{index.chunk_count} chunks)", "",
        "## Decomposed sub-questions (HEURISTIC framing)"]
    lines.extend(f"- {item}" for item in plan)
    lines += ["", "## Evidence synthesis"]
    for item in synthesis:
        header = f"### {item['sub_question']}"
        lines.append(header)
        if not item["citations"]:
            lines.append("- NO_EVIDENCE: no supplied source supports this; "
                         "model knowledge is not substituted")
            continue
        for citation in item["citations"]:
            lines.append(f"- `{citation['source_ref']}` "
                         f"(score {citation['score']})")
        for excerpt in item["excerpts"]:
            lines.append(f"  > {excerpt['text']}")
    lines += ["", "## Contradictions (representable, unresolved)"]
    lines.extend(
        f"- {c['kind']}: {c['description']} "
        f"[{c['left_source']} vs {c['right_source']}]"
        for c in contradictions) if contradictions else lines.append(
        "- none detected among retrieved numeric claims")
    lines += ["", "## Uncertainty"]
    lines.extend(f"- {item}" for item in unknowns) if unknowns else lines.append(
        "- none recorded")
    lines += ["", "## Next actions"]
    actions = (
        NextAction(action="Close the evidence gaps flagged as NO_EVIDENCE",
                   rationale="a synthesis without evidence is not auditable"),
        NextAction(action="Resolve numeric contradictions with a primary "
                          "source",
                   rationale="contradictory figures must not be averaged or "
                             "silently chosen",
                   kind="HUMAN_DECISION"),
        NextAction(action="Confirm the decision context with the requester",
                   rationale="scope and acceptance criteria are not in the "
                             "evidence"),
    )
    lines.extend(f"- {action.action} — {action.rationale}" for action in actions)
    lines += ["", "## Provenance",
              "- every excerpt cites a chunk and source; results are context, "
              "not canonical runtime truth."]
    artifact = {
        "question": question,
        "sub_questions": list(plan),
        "synthesis": synthesis,
        "contradictions": contradictions,
        "index_id": index.index_id,
        "document_count": index.document_count,
        "chunk_count": index.chunk_count,
    }
    result = WorkflowResult(
        workflow_id=workflow_id, family=WF_RESEARCH,
        title=f"Research: {_truncate(question, 80)}", project_id=project_id,
        mission_id=mission_id, inputs_are_fixture=inputs_are_fixture,
        deliverable_markdown="\n".join(lines) + "\n", artifact=artifact,
        evidence=tuple(evidence), unknowns=tuple(unknowns),
        next_actions=actions, generated_at=stamp)
    return persist_workflow(root, result)


# --- persistence --------------------------------------------------------------


def workflow_root(root: str, workflow_id: str) -> str:
    return f"{root}/workflows/{workflow_id}"


def persist_workflow(root: str, result: WorkflowResult) -> WorkflowResult:
    """Persist the machine artifact, deliverable and a LifeOS markdown note."""
    base = workflow_root(root, result.workflow_id)
    model.write_json(f"{base}/artifact.json", result.to_dict())
    lifeos_note = _lifeos_note(result)
    model.write_json(f"{base}/lifeos-note.json",
                     {"kind": "lifeos_note",
                      "workflow_id": result.workflow_id,
                      "family": result.family,
                      "title": result.title,
                      "markdown": lifeos_note,
                      "project_id": result.project_id,
                      "mission_id": result.mission_id})
    from pathlib import Path

    path = Path(base) / "deliverable.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.deliverable_markdown, encoding="utf-8")
    return result


def _lifeos_note(result: WorkflowResult) -> str:
    tags = f"#trajectory-os #{result.family.lower().replace('_', '-')}"
    links = []
    if result.project_id:
        links.append(f"[[project:{result.project_id}]]")
    if result.mission_id:
        links.append(f"[[mission:{result.mission_id}]]")
    tasks = "\n".join(f"- [ ] {action.action}" for action in result.next_actions)
    return (f"# {result.title}\n\n{tags}\n\n"
            + (" ".join(links) + "\n\n" if links else "")
            + f"Generated: {result.generated_at}\n\n"
            + "## Next actions\n" + tasks + "\n\n"
            + "## Deliverable\n\n" + result.deliverable_markdown)


def load_workflow(root: str, workflow_id: str) -> dict[str, Any] | None:
    return model.read_json(f"{workflow_root(root, workflow_id)}/artifact.json")


__all__ = [
    "CareerBrief",
    "EVIDENCE_KINDS",
    "EvidenceItem",
    "FACT",
    "HEURISTIC",
    "HYPOTHESIS",
    "MonitoringRecord",
    "NextAction",
    "PREDICTION",
    "WORKFLOW_FAMILIES",
    "WF_CAREER",
    "WF_LIFE_SCIENCES",
    "WF_RESEARCH",
    "WorkflowResult",
    "classify_life_sciences_text",
    "decompose_question",
    "extract_requirements",
    "identify_business_problems",
    "load_workflow",
    "map_experience",
    "persist_workflow",
    "run_career_workflow",
    "run_life_sciences_workflow",
    "run_research_workflow",
    "workflow_root",
]
