"""M066 — reusable life-sciences / pharma intelligence workflow.

The workflow ingests an evidence set, classifies events/themes, identifies
companies/entities, detects recurring problems and cautious trends, generates
AI/Data opportunity **hypotheses**, builds concise mini business cases and
emits portfolio-readable deliverables that preserve evidence and uncertainty.

The epistemic distinction is explicit and enforced everywhere:

``OBSERVATION``
    A dated record read directly from the supplied evidence set.
``EVIDENCE``
    An external evidence excerpt.
``INFERENCE``
    A deterministic classification/aggregation over observations.
``HYPOTHESIS``
    An opportunity or business case offered for human validation; it never
    asserts a measured company fact.

No unsupported claim is made about any company: entity names are only surfaced
when they appear verbatim in supplied evidence; otherwise the entity is left
unknown.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import workflows
from trajectory_os.realworld import model

FAMILY = "LIFE_SCIENCES_INTELLIGENCE"
MAX_EXCERPT = 600

_ARTIFACT_NAMES = (
    "monitoring-digest.md",
    "themes.md",
    "entities.md",
    "trends.md",
    "recurring-problems.md",
    "opportunities.md",
    "business-cases.md",
    "evidence-map.md",
)

#: Deterministic opportunity catalogue keyed by theme (hypotheses only).
_OPPORTUNITIES: Mapping[str, Mapping[str, str]] = {
    "oncology": {
        "opportunity": "Precision-oncology patient identification",
        "required_data": "Real-world genomics + claims data",
        "value_hypothesis": "higher trial enrichment / faster enrolment",
    },
    "clinical_trial": {
        "opportunity": "Trial feasibility and site-selection analytics",
        "required_data": "Historical trial + site performance data",
        "value_hypothesis": "fewer non-enrolling sites, shorter timelines",
    },
    "regulatory": {
        "opportunity": "Regulatory intelligence document automation",
        "required_data": "Submission and guidance corpora",
        "value_hypothesis": "less manual review effort",
    },
    "supply_chain": {
        "opportunity": "Supply-risk early-warning model",
        "required_data": "Shipment, inventory and shortage signals",
        "value_hypothesis": "earlier mitigation of shortages",
    },
    "market_access": {
        "opportunity": "Payer evidence package assembly",
        "required_data": "Real-world evidence and claims data",
        "value_hypothesis": "faster, more consistent submissions",
    },
    "safety": {
        "opportunity": "Safety-signal triage assistance",
        "required_data": "Case narratives and literature",
        "value_hypothesis": "faster signal review with human oversight",
    },
    "ai_data": {
        "opportunity": "Governed data product for AI reuse",
        "required_data": "Governed cross-study data assets",
        "value_hypothesis": "reusable data products, lower marginal cost",
    },
}

_PROBLEM_MARKERS = ("problem", "challenge", "shortage", "issue", "delay",
                    "recall", "risk", "barrier", "constraint")

_ENTITY_SUFFIXES = (
    "inc", "inc.", "ltd", "ltd.", "plc", "gmbh", "corp", "corp.",
    "corporation", "therapeutics", "pharma", "pharmaceuticals",
    "biosciences", "bioscience", "biotech", "holdings", "group",
)

_ENTITY_RE = re.compile(
    r"\b([A-Z][A-Za-z&.\-]+(?:\s+[A-Z][A-Za-z&.\-]+){0,3})\s+"
    r"(?i:" + "|".join(re.escape(s) for s in _ENTITY_SUFFIXES) + r")\b")


@dataclass(frozen=True)
class MonitoringRecord:
    """One dated evidence record (news, note, filing excerpt)."""

    source_ref: str
    date: str
    text: str
    sensitivity: str = "INTERNAL"

    def validate(self) -> MonitoringRecord:
        if not self.source_ref or not self.text:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "monitoring record requires ref and text")
        return self

    def excerpt(self, maximum: int = MAX_EXCERPT) -> str:
        text = " ".join(self.text.split())
        return text if len(text) <= maximum else text[:maximum] + "…"


@dataclass(frozen=True)
class LifeSciencesIntelligence:
    workflow_id: str
    title: str
    project_id: str | None
    mission_id: str | None
    inputs_are_fixture: bool
    claims: tuple[model.Claim, ...]
    unknowns: tuple[model.Unknown, ...]
    next_actions: tuple[model.Action, ...]
    artifacts: Mapping[str, str]
    generated_at: str
    canonical: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "realworld_version": model.REALWORLD_VERSION,
            "kind": "life_sciences_intelligence",
            "family": FAMILY,
            "workflow_id": self.workflow_id,
            "title": self.title,
            "project_id": self.project_id,
            "mission_id": self.mission_id,
            "inputs_are_fixture": self.inputs_are_fixture,
            "claims": [claim.to_dict() for claim in self.claims],
            "claim_counts": model.claim_counts(self.claims),
            "unknowns": [unknown.to_dict() for unknown in self.unknowns],
            "next_actions": [action.to_dict() for action in self.next_actions],
            "artifact_names": sorted(self.artifacts),
            "generated_at": self.generated_at,
            "canonical": self.canonical,
        }


def detect_entities(text: str) -> tuple[str, ...]:
    """Deterministic detection of company-like entities in supplied text."""
    found: set[str] = set()
    for match in _ENTITY_RE.finditer(text):
        name = " ".join(match.group(1).split())
        if len(name) >= 3:
            found.add(name)
    return tuple(sorted(found, key=lambda value: (value.lower(), value)))


def _render(claims: Sequence[model.Claim]) -> list[str]:
    return [f"- **[{claim.label}]** {claim.statement}  \n  "
            f"_source: `{claim.source_ref}`_" for claim in claims]


def _refs_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def run_life_sciences_intelligence(
    records: Sequence[MonitoringRecord], *, root: str,
    workflow_id: str = "life-sciences",
    title: str = "Life-sciences monitoring digest",
    project_id: str | None = None, mission_id: str | None = None,
    inputs_are_fixture: bool = False, generated_at: str = "",
) -> LifeSciencesIntelligence:
    """Run the life-sciences workflow and persist every deliverable."""
    stamp = generated_at or model.utc_now()
    for record in records:
        record.validate()

    claims: list[model.Claim] = []
    unknowns: list[model.Unknown] = []
    theme_counts: dict[str, int] = {}
    theme_records: dict[str, list[MonitoringRecord]] = {}
    dated_themes: dict[str, set[str]] = {}
    entities: set[str] = set()

    for index, record in enumerate(records):
        ref = f"monitoring:{workflow_id}:{index}"
        claims.append(model.Claim(
            statement=record.excerpt(), label=model.OBSERVATION,
            source_kind=model.SRC_SOURCE_DOCUMENT,
            source_ref=f"{ref} ({record.source_ref})").validate())
        themes = workflows.classify_life_sciences_text(record.text)
        for theme in themes:
            theme_counts[theme] = theme_counts.get(theme, 0) + 1
            theme_records.setdefault(theme, []).append(record)
            dated_themes.setdefault(theme, set()).add(record.date[:7])
        entities.update(detect_entities(record.text))

    if not records:
        unknowns.append(model.Unknown(
            description="no monitoring records were supplied",
            reason="no evidence to classify",
            source_ref="monitoring_evidence"))

    for entity in sorted(entities):
        claims.append(model.Claim(
            statement=f"entity mentioned in supplied evidence: {entity}",
            label=model.EVIDENCE, source_kind=model.SRC_SOURCE_DOCUMENT,
            source_ref=f"entity:{entity}").validate())
    if not entities:
        unknowns.append(model.Unknown(
            description="no company/entity name was detected in the supplied "
                        "evidence",
            reason="entity names are never guessed",
            source_ref="monitoring_evidence"))

    recurring = tuple(sorted(
        (theme for theme, count in theme_counts.items() if count >= 2),
        key=lambda theme: (-theme_counts[theme], theme)))
    trends: list[dict[str, Any]] = []
    for theme in sorted(dated_themes):
        periods = sorted(dated_themes[theme])
        if len(periods) < 2:
            continue
        if len(periods) >= 4:
            uncertainty = "LOW"
        elif len(periods) == 3:
            uncertainty = "MEDIUM"
        else:
            uncertainty = "HIGH"
        trends.append({
            "theme": theme,
            "period_count": len(periods),
            "periods": periods,
            "observation_count": theme_counts[theme],
            "direction": "REPEATED_SIGNAL",
            "uncertainty": uncertainty,
            "note": "repeated observation across periods; not a forecast",
        })
        claims.append(model.Claim(
            statement=(f"theme '{theme}' observed in {len(periods)} distinct "
                       f"months ({', '.join(periods)})"),
            label=model.INFERENCE, source_kind=model.SRC_DETERMINISTIC_RULE,
            source_ref=f"rule:trend:{theme}",
            confidence={"LOW": 0.8, "MEDIUM": 0.6, "HIGH": 0.4}[uncertainty],
            note="aggregation over observations; not a forecast").validate())

    problems: list[dict[str, Any]] = []
    for theme in recurring:
        excerpts = []
        for record in theme_records[theme]:
            lowered = record.text.lower()
            if any(marker in lowered for marker in _PROBLEM_MARKERS):
                excerpts.append(record.excerpt(240))
        if excerpts:
            problems.append({
                "theme": theme,
                "observation_count": theme_counts[theme],
                "excerpts": excerpts,
                "label": model.EVIDENCE,
            })

    opportunities: list[dict[str, Any]] = []
    for theme in recurring:
        template = _OPPORTUNITIES.get(theme)
        if template is None:
            continue
        opportunity = {
            "theme": theme,
            "observation_count": theme_counts[theme],
            "opportunity": template["opportunity"],
            "required_data": template["required_data"],
            "value_hypothesis": template["value_hypothesis"],
            "classification": model.HYPOTHESIS,
            "evidence_refs": sorted(
                record.source_ref for record in theme_records[theme]),
        }
        opportunities.append(opportunity)
        claims.append(model.Claim(
            statement=str(opportunity["opportunity"]),
            label=model.HYPOTHESIS,
            source_kind=model.SRC_DETERMINISTIC_RULE,
            source_ref=f"hypothesis:opportunity:{theme}",
            note="catalogue mapping; value is unverified").validate())

    business_cases: list[dict[str, Any]] = []
    for opportunity in opportunities[:3]:
        business_cases.append({
            "problem": opportunity["theme"],
            "problem_evidence_refs": opportunity["evidence_refs"],
            "proposed_opportunity": opportunity["opportunity"],
            "required_data": opportunity["required_data"],
            "value_hypothesis": opportunity["value_hypothesis"],
            "classification": model.HYPOTHESIS,
            "effort": "UNKNOWN",
            "risks": ["data access", "governance", "validation bias"],
            "next_step": "validate the problem with a domain owner",
        })
    if not business_cases:
        unknowns.append(model.Unknown(
            description="no recurring theme supports a mini business case",
            reason="a business case requires recurring evidence",
            source_ref="theme_classification"))

    # --- artifacts -----------------------------------------------------------
    digest = [f"# {title}", "",
              f"- records analysed: {len(records)}",
              f"- inputs marked as fixture/sample: {inputs_are_fixture}",
              f"- entities detected: {len(entities)}",
              f"- recurring themes: {len(recurring)}", "",
              "## Observations", ""]
    digest.extend(_render(
        [claim for claim in claims
         if claim.label == model.OBSERVATION]))

    themes_md = ["# Themes", "", "| theme | observations | periods |",
                 "| --- | --- | --- |"]
    for theme in sorted(theme_counts, key=lambda x: (-theme_counts[x], x)):
        themes_md.append(
            f"| {theme} | {theme_counts[theme]} | "
            f"{len(dated_themes.get(theme, set()))} |")
    if not theme_counts:
        themes_md.append("| _none_ | 0 | 0 |")

    entities_md = ["# Entities", "",
                   "Entities are surfaced only when they appear verbatim in "
                   "supplied evidence.", ""]
    entities_md.extend(f"- {entity}" for entity in sorted(entities)) \
        if entities else entities_md.append("- none detected")

    trends_md = ["# Trends (cautious)", "",
                 "A trend is repeated observation across >=2 periods. It is "
                 "never a forecast.", ""]
    for trend in trends:
        trends_md.append(
            f"- **{trend['theme']}** — {trend['period_count']} period(s) "
            f"({', '.join(trend['periods'])}), "
            f"uncertainty {trend['uncertainty']}")
    if not trends:
        trends_md.append("- no multi-period trend is supportable")

    problems_md = ["# Recurring problems", ""]
    for problem in problems:
        problems_md.append(
            f"- **{problem['theme']}** ({problem['observation_count']} "
            "observations)")
        for excerpt in problem["excerpts"][:3]:
            problems_md.append(f"  > {excerpt}")
    if not problems:
        problems_md.append("- no recurring problem reached the threshold")

    opportunities_md = ["# AI / data opportunities (HYPOTHESIS)", ""]
    for opportunity in opportunities:
        opportunities_md += [
            f"- **{opportunity['opportunity']}** — theme "
            f"`{opportunity['theme']}`",
            f"  - required data: {opportunity['required_data']}",
            f"  - value hypothesis (unverified): "
            f"{opportunity['value_hypothesis']}",
            "  - evidence: "
            + _refs_text(opportunity["evidence_refs"])]
    if not opportunities:
        opportunities_md.append("- none: no recurring theme mapped")

    cases_md = ["# Mini business cases (HYPOTHESIS)", ""]
    for case in business_cases:
        cases_md += [
            f"## {case['proposed_opportunity']}",
            f"- problem: {case['problem']}",
            f"- evidence: {', '.join(case['problem_evidence_refs'])}",
            f"- required data: {case['required_data']}",
            f"- value hypothesis (unverified): {case['value_hypothesis']}",
            f"- effort: {case['effort']}",
            f"- risks: {', '.join(case['risks'])}",
            f"- next step: {case['next_step']}", ""]
    if not business_cases:
        cases_md.append("- insufficient recurring evidence to support a case")

    evidence_map = ["# Evidence map", "",
                    "| label | statement | source |",
                    "| --- | --- | --- |"]
    for claim in claims:
        evidence_map.append(
            f"| {claim.label} | {claim.statement.replace('|', '/')} | "
            f"`{claim.source_ref}` |")

    actions = (
        model.Action(
            action="Validate the top recurring theme with a domain owner",
            rationale="theme classification is deterministic but domain "
                      "confirmation is required",
            source="themes.md", urgency="HIGH", uncertainty="HIGH",
            dependencies=("domain_owner",),
            alternatives=("collect more evidence first",),
            kind="HUMAN_DECISION"),
        model.Action(
            action="Scope data availability for the leading opportunity",
            rationale="data access determines feasibility",
            source="opportunities.md", urgency="MEDIUM",
            uncertainty="MEDIUM", dependencies=("data_governance",),
            alternatives=("start with a narrow feasibility pilot",),
            kind="TASK"),
        model.Action(
            action="Add more monitoring periods to support a trend",
            rationale="trend claims require multiple periods",
            source="trends.md", urgency="LOW", uncertainty="LOW",
            alternatives=("treat as a one-off observation",),
            kind="TASK"),
    )
    for action in actions:
        action.validate()

    artifacts: dict[str, str] = {
        "monitoring-digest.md": "\n".join(digest) + "\n",
        "themes.md": "\n".join(themes_md) + "\n",
        "entities.md": "\n".join(entities_md) + "\n",
        "trends.md": "\n".join(trends_md) + "\n",
        "recurring-problems.md": "\n".join(problems_md) + "\n",
        "opportunities.md": "\n".join(opportunities_md) + "\n",
        "business-cases.md": "\n".join(cases_md) + "\n",
        "evidence-map.md": "\n".join(evidence_map) + "\n",
    }
    intelligence = LifeSciencesIntelligence(
        workflow_id=workflow_id, title=title, project_id=project_id,
        mission_id=mission_id, inputs_are_fixture=inputs_are_fixture,
        claims=tuple(claims), unknowns=tuple(unknowns),
        next_actions=actions, artifacts=artifacts, generated_at=stamp)
    _persist(root, intelligence)
    return intelligence


def _persist(root: str, intelligence: LifeSciencesIntelligence) -> None:
    base = (Path(root) / "realworld" / "life-sciences"
            / intelligence.workflow_id)
    base.mkdir(parents=True, exist_ok=True)
    for name, content in intelligence.artifacts.items():
        (base / name).write_text(content, encoding="utf-8")
    model.write_json(str(base / "analysis.json"), intelligence.to_dict())
    model.write_json(
        str(base / "next-actions.json"),
        {"schema_version": model.SCHEMA_VERSION,
         "kind": "life_sciences_next_actions",
         "workflow_id": intelligence.workflow_id,
         "actions": [action.to_dict()
                     for action in intelligence.next_actions]})
    links = []
    if intelligence.project_id:
        links.append(f"[[project:{intelligence.project_id}]]")
    if intelligence.mission_id:
        links.append(f"[[mission:{intelligence.mission_id}]]")
    note = (f"# {intelligence.title}\n\n"
            "#trajectory-os #life-sciences-intelligence\n\n"
            + (" ".join(links) + "\n\n" if links else "")
            + f"Generated: {intelligence.generated_at}\n\n"
            + "## Next actions\n"
            + "\n".join(f"- [ ] {action.action}"
                        for action in intelligence.next_actions) + "\n")
    model.write_json(
        str(base / "lifeos-note.json"),
        {"kind": "lifeos_note", "type": "life-sciences-intelligence",
         "workflow_id": intelligence.workflow_id,
         "project_id": intelligence.project_id,
         "mission_id": intelligence.mission_id, "markdown": note})


def load_life_sciences_intelligence(
    root: str, workflow_id: str = "life-sciences",
) -> dict[str, Any] | None:
    return model.read_json(
        str(Path(root) / "realworld" / "life-sciences" / workflow_id
            / "analysis.json"))


__all__ = [
    "FAMILY",
    "LifeSciencesIntelligence",
    "MonitoringRecord",
    "detect_entities",
    "load_life_sciences_intelligence",
    "run_life_sciences_intelligence",
]
