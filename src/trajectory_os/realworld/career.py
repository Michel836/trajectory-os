"""M065 — real end-to-end career / consulting intelligence workflow.

Inputs (all explicitly supplied, never fetched):

* company evidence (external documents/notes);
* role / job description;
* user-provided profile / experience evidence;
* portfolio artifacts;
* research sources.

Outputs (produced when supported, otherwise an explicit unknown):

* ``company-analysis.md``
* ``role-fit.md``
* ``evidence-map.md``
* ``gaps.md``
* ``business-problem-hypotheses.md``
* ``ai-data-opportunity.md``
* ``value-proposition.md``
* ``interview-brief.md``
* ``next-actions.json``
* a LifeOS-compatible projection.

Epistemic rules (enforced, not advisory):

* personal facts come *only* from the supplied profile/portfolio;
* company facts come *only* from supplied company evidence / role text;
* a business problem is ``HYPOTHESIS`` unless the supplied company evidence
  actually supports it, in which case the excerpt is ``EVIDENCE``;
* a requirement with no supplied supporting asset is a ``GAP``/unknown, never
  an invented experience;
* external evidence and inference are always separated by label.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import workflows
from trajectory_os.realworld import model

#: Workflow family identifier.
FAMILY = "CAREER_INTELLIGENCE"

MAX_EXCERPT = 600

#: Maximum characters in one persisted role-description evidence segment.
#: Derived from (and strictly below) the global claim statement bound so that a
#: realistic long job description is split into several valid claims without
#: weakening the claim-size invariant.
ROLE_DESCRIPTION_SEGMENT_MAX = model.MAX_CLAIM_STATEMENT_LEN // 2

_ARTIFACT_NAMES = (
    "company-analysis.md",
    "role-fit.md",
    "evidence-map.md",
    "gaps.md",
    "business-problem-hypotheses.md",
    "ai-data-opportunity.md",
    "value-proposition.md",
    "interview-brief.md",
)

#: Keyword -> (problem hypothesis, ai/data opportunity hypothesis). These are
#: deterministic signal mappings, never measured findings.
_SIGNALS: Mapping[str, Mapping[str, str]] = {
    "scale": {
        "problem": "scaling current systems or operations",
        "opportunity": "scalable data platform and automated pipelines",
    },
    "cost": {
        "problem": "cost pressure or margin improvement",
        "opportunity": "cost transparency and automated reporting",
    },
    "data": {
        "problem": "data quality, governance or availability",
        "opportunity": "governed data products and observability",
    },
    "complian": {
        "problem": "regulatory / compliance burden",
        "opportunity": "regulatory evidence automation",
    },
    "legacy": {
        "problem": "legacy modernisation",
        "opportunity": "incremental migration with lineage",
    },
    "migrat": {
        "problem": "platform or data migration",
        "opportunity": "migration assurance and reconciliation",
    },
    "customer": {
        "problem": "customer experience or retention",
        "opportunity": "customer-signal analytics",
    },
    "efficien": {
        "problem": "operational efficiency",
        "opportunity": "process mining and automation",
    },
    "risk": {
        "problem": "risk management",
        "opportunity": "risk-signal triage with human oversight",
    },
    "artificial intelligence": {
        "problem": "AI / automation adoption",
        "opportunity": "reusable AI data foundation",
    },
    "machine learning": {
        "problem": "AI / automation adoption",
        "opportunity": "reusable AI data foundation",
    },
}


@dataclass(frozen=True)
class EvidenceSource:
    """One caller-supplied evidence document."""

    source_ref: str
    text: str
    sensitivity: str = "INTERNAL"

    def validate(self) -> EvidenceSource:
        if not self.source_ref:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "evidence requires a source reference")
        return self

    def excerpt(self, maximum: int = MAX_EXCERPT) -> str:
        text = " ".join(self.text.split())
        return text if len(text) <= maximum else text[:maximum] + "…"


@dataclass(frozen=True)
class CareerInputs:
    """Every career input, explicitly typed by origin."""

    company: str
    role: str
    role_description: str
    company_evidence: tuple[EvidenceSource, ...] = ()
    profile_evidence: tuple[EvidenceSource, ...] = ()
    portfolio_artifacts: tuple[EvidenceSource, ...] = ()
    research_sources: tuple[EvidenceSource, ...] = ()
    constraints: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    inputs_are_fixture: bool = False


@dataclass(frozen=True)
class CareerIntelligence:
    """The complete, persisted career artifact set."""

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
            "kind": "career_intelligence",
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


def _excerpt_claim(source: EvidenceSource, label: str, index: int,
                   prefix: str) -> model.Claim:
    kind = (model.SRC_USER_INPUT
            if prefix in ("profile", "portfolio")
            else model.SRC_SUPPLIED_EVIDENCE)
    return model.Claim(
        statement=source.excerpt(), label=label, source_kind=kind,
        source_ref=f"{prefix}:{source.source_ref}:{index}").validate()


def _matched_signals(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(sorted(signal for signal in _SIGNALS if signal in lowered))


def _role_description_segments(
    text: str, maximum: int = ROLE_DESCRIPTION_SEGMENT_MAX,
) -> tuple[str, ...]:
    """Split a role description into deterministic, bounded segments.

    Boundaries are chosen at whitespace where possible; every non-whitespace
    character is preserved in order. A description that already fits the bound
    yields a single segment identical to its stripped form, so the previous
    single-claim behaviour is preserved for short descriptions.
    """
    stripped = text.strip()
    if not stripped:
        return ()
    if len(stripped) <= maximum:
        return (stripped,)
    segments: list[str] = []
    remaining = stripped
    while len(remaining) > maximum:
        window = remaining[:maximum]
        split_at = window.rfind(" ")
        if split_at <= 0:
            split_at = maximum
        segment = remaining[:split_at].strip()
        if segment:
            segments.append(segment)
        remaining = remaining[split_at:].lstrip()
    if remaining:
        segments.append(remaining)
    return tuple(segments)


def _role_description_claims(
    inputs: CareerInputs, workflow_id: str,
) -> tuple[model.Claim, ...]:
    """Persist the supplied role description as bounded USER_INPUT evidence.

    Requirement extraction and matching still receive the full, unmodified
    role description; only the persisted evidence is segmented. A single
    segment keeps the legacy ``role-description:<id>`` source ref so short
    descriptions remain provenance-compatible, while a split description
    exposes an explicit, stable ``...:segment:<index>`` ref per segment.
    """
    segments = _role_description_segments(inputs.role_description)
    if not segments:
        return (model.Claim(
            statement=f"role: {inputs.role}",
            label=model.EVIDENCE, source_kind=model.SRC_USER_INPUT,
            source_ref=f"role-description:{workflow_id}").validate(),)
    if len(segments) == 1:
        return (model.Claim(
            statement=segments[0], label=model.EVIDENCE,
            source_kind=model.SRC_USER_INPUT,
            source_ref=f"role-description:{workflow_id}").validate(),)
    return tuple(
        model.Claim(
            statement=segment, label=model.EVIDENCE,
            source_kind=model.SRC_USER_INPUT,
            source_ref=f"role-description:{workflow_id}:segment:{index}",
            note=f"role description segment {index} of {len(segments)}"
        ).validate()
        for index, segment in enumerate(segments))


def _render_claim_bullets(claims: Sequence[model.Claim]) -> list[str]:
    return [f"- **[{claim.label}]** {claim.statement}  \n  "
            f"_source: `{claim.source_ref}`_" for claim in claims]


def _company_analysis(
    inputs: CareerInputs, claims: list[model.Claim],
    unknowns: list[model.Unknown],
) -> str:
    evidence = [claim for claim in claims
                if claim.source_kind == model.SRC_SUPPLIED_EVIDENCE]
    inferences: list[model.Claim] = []
    signals: set[str] = set()
    for source in (*inputs.company_evidence, *inputs.research_sources):
        for signal in _matched_signals(source.text):
            if signal in signals:
                continue
            signals.add(signal)
            inferences.append(model.Claim(
                statement=(f"signal '{signal}' suggests "
                           f"{_SIGNALS[signal]['problem']}"),
                label=model.INFERENCE, source_kind=model.SRC_DETERMINISTIC_RULE,
                source_ref=f"rule:company-signal:{signal}",
                note="keyword heuristic, not a measured finding"))
    if not evidence:
        unknowns.append(model.Unknown(
            description="no company evidence was supplied",
            reason="company facts are never invented",
            source_ref="company_evidence"))
    lines = [f"# Company analysis — {inputs.company}", "",
             "## External evidence (EVIDENCE)", ""]
    lines.extend(_render_claim_bullets(evidence)) if evidence else lines.append(
        "- none supplied")
    lines += ["", "## Inferences (INFERENCE, not facts)", ""]
    lines.extend(_render_claim_bullets(inferences)) if inferences else (
        lines.append("- none derivable from the supplied evidence"))
    lines += ["", "## Unknowns", ""]
    company_unknowns = [item for item in unknowns
                        if "company" in item.description
                        or item.source_ref == "company_evidence"]
    lines.extend(f"- {item.description} ({item.reason})"
                 for item in company_unknowns) if company_unknowns else (
        lines.append("- none recorded"))
    return "\n".join(lines) + "\n"


def _role_fit(
    inputs: CareerInputs, requirements: Sequence[str],
    mapping: Sequence[Mapping[str, Any]], claims: list[model.Claim],
) -> tuple[str, tuple[model.Unknown, ...]]:
    unknowns: list[model.Unknown] = []
    lines = [f"# Role fit — {inputs.role}", "",
             "## Requirements (from the supplied role description)", ""]
    if requirements:
        lines.extend(f"- {requirement}" for requirement in requirements)
    else:
        lines.append("- no requirements could be extracted")
        unknowns.append(model.Unknown(
            description="the role description yielded no extractable "
                        "requirements",
            reason="cannot assess fit without requirements",
            source_ref="role_description"))
    lines += ["", "## Match (supplied profile/portfolio only)", ""]
    for item in mapping:
        status = str(item["status"])
        matches = "; ".join(item["matched_experiences"]) or "no supplied asset"
        lines.append(f"- [{status}] {item['requirement']} — {matches}")
        if status == "GAP":
            unknowns.append(model.Unknown(
                description=f"requirement not evidenced: {item['requirement']}",
                reason="no supplied profile/portfolio asset supports it",
                source_ref="profile_evidence"))
    matched = sum(1 for item in mapping if item["status"] == "MATCHED")
    total = len(mapping)
    lines += ["", "## Coverage", "",
              f"- matched: {matched}/{total} requirement(s)",
              f"- gaps: {total - matched}"]
    note = model.Claim(
        statement=f"{matched} of {total} extracted requirements are supported "
                  "by supplied assets",
        label=model.INFERENCE, source_kind=model.SRC_DETERMINISTIC_RULE,
        source_ref="rule:requirement-coverage").validate()
    claims.append(note)
    return "\n".join(lines) + "\n", tuple(unknowns)


def _evidence_map(claims: Sequence[model.Claim]) -> str:
    lines = ["# Evidence map", "",
             "| label | statement | source |", "| --- | --- | --- |"]
    for claim in claims:
        statement = claim.statement.replace("|", "/")
        lines.append(f"| {claim.label} | {statement} | `{claim.source_ref}` |")
    lines += ["", "Labels: FACT/OBSERVATION/EVIDENCE are grounded; "
              "INFERENCE is derived; HYPOTHESIS is unverified."]
    return "\n".join(lines) + "\n"


def _gaps(
    mapping: Sequence[Mapping[str, Any]], unknowns: Sequence[model.Unknown],
) -> str:
    lines = ["# Gaps", "", "## Unevidenced requirements", ""]
    gap_items = [item for item in mapping if item["status"] == "GAP"]
    lines.extend(f"- {item['requirement']}" for item in gap_items) if gap_items \
        else lines.append("- none")
    lines += ["", "## Recorded unknowns", ""]
    lines.extend(f"- {item.description} ({item.reason})" for item in unknowns) \
        if unknowns else lines.append("- none")
    return "\n".join(lines) + "\n"


def _business_problem_hypotheses(
    inputs: CareerInputs, claims: list[model.Claim],
) -> str:
    lines = ["# Business problem hypotheses", "",
             "Each hypothesis is explicitly unverified. A problem is only "
             "promoted above HYPOTHESIS when supplied evidence states it.", ""]
    evidenced: set[str] = set()
    problem_markers = ("problem", "challenge", "issue", "pain",
                       "bottleneck", "difficulty", "struggle")
    for source in inputs.company_evidence:
        lowered = source.text.lower()
        if not any(marker in lowered for marker in problem_markers):
            continue
        for signal in _matched_signals(source.text):
            evidenced.add(signal)
    emitted: set[str] = set()
    for source in (*inputs.company_evidence, *inputs.research_sources):
        for signal in _matched_signals(source.text):
            if signal in emitted:
                continue
            emitted.add(signal)
            label = (model.EVIDENCE if signal in evidenced
                     else model.HYPOTHESIS)
            claim = model.Claim(
                statement=(f"{_SIGNALS[signal]['problem']} "
                           f"(signal: {signal})"),
                label=label,
                source_kind=(model.SRC_SUPPLIED_EVIDENCE if label
                             == model.EVIDENCE
                             else model.SRC_DETERMINISTIC_RULE),
                source_ref=(f"evidence:{signal}" if label == model.EVIDENCE
                            else f"hypothesis:problem:{signal}"),
                note=("stated or strongly supported by supplied evidence"
                      if label == model.EVIDENCE
                      else "unverified hypothesis for human confirmation"))
            claims.append(claim)
    hypotheses = [claim for claim in claims
                  if claim.source_ref.startswith(("hypothesis:problem:",
                                                  "evidence:"))
                  and "signal:" in claim.statement]
    lines.extend(_render_claim_bullets(hypotheses)) if hypotheses else (
        lines.append("- no problem signals were detected"))
    return "\n".join(lines) + "\n"


def _ai_data_opportunity(
    inputs: CareerInputs, claims: list[model.Claim],
) -> str:
    text = " ".join(source.text for source in (
        *inputs.company_evidence, *inputs.research_sources))
    text += " " + inputs.role_description
    lines = ["# AI / data opportunities", "",
             "Opportunities are HYPOTHESES derived from detected signals; "
             "none is a measured benefit.", ""]
    emitted: set[str] = set()
    for signal in _matched_signals(text):
        opportunity = _SIGNALS[signal]["opportunity"]
        if opportunity in emitted:
            continue
        emitted.add(opportunity)
        claim = model.Claim(
            statement=opportunity, label=model.HYPOTHESIS,
            source_kind=model.SRC_DETERMINISTIC_RULE,
            source_ref=f"hypothesis:opportunity:{signal}",
            note="catalogue mapping; value is unverified")
        claims.append(claim)
        lines.append(f"- **{opportunity}** — signal `{signal}`")
    if not emitted:
        lines.append("- no opportunity signal was detected in the supplied "
                     "evidence")
    return "\n".join(lines) + "\n"


def _value_proposition(mapping: Sequence[Mapping[str, Any]]) -> str:
    lines = ["# Value proposition", "",
             "Only MATCHED requirements may appear as claims.", ""]
    matched = [item for item in mapping if item["status"] == "MATCHED"]
    if not matched:
        lines.append("- not provided: no supplied asset evidences a "
                     "requirement")
        return "\n".join(lines) + "\n"
    for item in matched:
        lines.append(f"- **{item['requirement']}** — demonstrated by: "
                     + "; ".join(item["matched_experiences"]))
    return "\n".join(lines) + "\n"


def _interview_brief(
    mapping: Sequence[Mapping[str, Any]], inputs: CareerInputs,
) -> str:
    lines = ["# Interview brief", "",
             "Questions below are generated prompts (INFERENCE), not facts.", ""]
    gaps = [item for item in mapping if item["status"] == "GAP"]
    for item in gaps[:8]:
        lines.append(f"- How would you deliver: {item['requirement']}?")
    if not gaps:
        lines.append("- All extracted requirements are evidenced; ask about "
                     "impact measures and constraints.")
    lines += ["", f"- Confirm the current priorities for {inputs.role}.",
              "- Confirm constraints: "
              + (", ".join(inputs.constraints) if inputs.constraints
                 else "none supplied"),
              "- Ask which business problem is most urgent and how success "
              "is measured."]
    return "\n".join(lines) + "\n"


def _career_actions(
    mapping: Sequence[Mapping[str, Any]], inputs: CareerInputs,
) -> tuple[model.Action, ...]:
    gaps = [item for item in mapping if item["status"] == "GAP"]
    actions: list[model.Action] = []
    if gaps:
        actions.append(model.Action(
            action="Supply concrete evidence for the unmatched requirements",
            rationale=f"{len(gaps)} requirement(s) have no supplied asset",
            source="role-fit mapping", urgency="HIGH",
            dependencies=("profile_evidence",),
            uncertainty="MEDIUM",
            alternatives=("reframe the application around matched strengths",
                          "treat the requirement as a learning gap"),
            kind="INPUT_REQUIRED").validate())
    if inputs.links:
        actions.append(model.Action(
            action="Verify the supplied portfolio/reference links resolve",
            rationale="links were supplied and should be validated before use",
            source="career inputs", urgency="MEDIUM",
            uncertainty="LOW",
            alternatives=("request alternative references",),
            kind="TASK").validate())
    actions.append(model.Action(
        action="Draft the candidacy/portfolio artifact from matched evidence",
        rationale="only MATCHED requirements may appear as claims",
        source="value proposition", urgency="HIGH",
        dependencies=("value-proposition.md",),
        uncertainty="LOW",
        alternatives=("defer until more evidence is supplied",),
        kind="TASK").validate())
    actions.append(model.Action(
        action="Confirm the business-problem hypotheses with the company",
        rationale="business problems are hypotheses unless evidenced",
        source="business-problem-hypotheses.md", urgency="HIGH",
        uncertainty="HIGH",
        alternatives=("validate against additional external evidence",),
        kind="HUMAN_DECISION").validate())
    return tuple(actions)


def _lifeos_note(intelligence: CareerIntelligence) -> str:
    links: list[str] = []
    if intelligence.project_id:
        links.append(f"[[project:{intelligence.project_id}]]")
    if intelligence.mission_id:
        links.append(f"[[mission:{intelligence.mission_id}]]")
    tasks = "\n".join(f"- [ ] {action.action}"
                      for action in intelligence.next_actions)
    return (f"# {intelligence.title}\n\n"
            "#trajectory-os #career-intelligence\n\n"
            + (" ".join(links) + "\n\n" if links else "")
            + f"Generated: {intelligence.generated_at}\n\n"
            + "## Next actions\n" + tasks + "\n\n"
            + "## Unknowns\n"
            + "\n".join(f"- {item.description}"
                        for item in intelligence.unknowns) + "\n")


def run_career_intelligence(
    inputs: CareerInputs, *, root: str, workflow_id: str = "career",
    project_id: str | None = None, mission_id: str | None = None,
    generated_at: str = "",
) -> CareerIntelligence:
    """Run the full career workflow and persist every artifact."""
    stamp = generated_at or model.utc_now()
    for source in (*inputs.company_evidence, *inputs.profile_evidence,
                   *inputs.portfolio_artifacts, *inputs.research_sources):
        source.validate()

    claims: list[model.Claim] = []
    unknowns: list[model.Unknown] = []

    # External company evidence.
    for index, source in enumerate(inputs.company_evidence):
        claims.append(_excerpt_claim(source, model.EVIDENCE, index, "company"))
        for signal in _matched_signals(source.text):
            claims.append(model.Claim(
                statement=source.excerpt(240), label=model.EVIDENCE,
                source_kind=model.SRC_SUPPLIED_EVIDENCE,
                source_ref=f"company:{source.source_ref}:{index}:"
                           f"signal:{signal}").validate())

    # User-supplied personal facts.
    for index, source in enumerate(inputs.profile_evidence):
        claims.append(_excerpt_claim(source, model.FACT, index, "profile"))
    for index, source in enumerate(inputs.portfolio_artifacts):
        claims.append(_excerpt_claim(source, model.EVIDENCE, index,
                                     "portfolio"))
    for index, source in enumerate(inputs.research_sources):
        claims.append(_excerpt_claim(source, model.EVIDENCE, index, "research"))

    role_claims = _role_description_claims(inputs, workflow_id)
    claims.extend(role_claims)

    requirements = workflows.extract_requirements(inputs.role_description)
    experiences = tuple(
        " ".join(source.text.split())
        for source in (*inputs.profile_evidence, *inputs.portfolio_artifacts))
    mapping = workflows.map_experience(requirements, experiences)

    company_md = _company_analysis(inputs, claims, unknowns)
    role_fit_md, role_unknowns = _role_fit(
        inputs, requirements, mapping, claims)
    unknowns.extend(role_unknowns)
    if not inputs.profile_evidence and not inputs.portfolio_artifacts:
        unknowns.append(model.Unknown(
            description="no user-supplied experience assets were provided",
            reason="the value proposition is intentionally empty rather than "
                   "invented",
            source_ref="profile_evidence"))
    business_md = _business_problem_hypotheses(inputs, claims)
    ai_md = _ai_data_opportunity(inputs, claims)

    artifacts: dict[str, str] = {
        "company-analysis.md": company_md,
        "role-fit.md": role_fit_md,
        "evidence-map.md": _evidence_map(claims),
        "gaps.md": _gaps(mapping, unknowns),
        "business-problem-hypotheses.md": business_md,
        "ai-data-opportunity.md": ai_md,
        "value-proposition.md": _value_proposition(mapping),
        "interview-brief.md": _interview_brief(mapping, inputs),
    }
    actions = _career_actions(mapping, inputs)
    intelligence = CareerIntelligence(
        workflow_id=workflow_id,
        title=f"Career intelligence: {inputs.role} at {inputs.company}",
        project_id=project_id, mission_id=mission_id,
        inputs_are_fixture=inputs.inputs_are_fixture, claims=tuple(claims),
        unknowns=tuple(unknowns), next_actions=actions, artifacts=artifacts,
        generated_at=stamp)
    _persist(root, intelligence)
    return intelligence


def _persist(root: str, intelligence: CareerIntelligence) -> None:
    base = Path(root) / "realworld" / "career" / intelligence.workflow_id
    base.mkdir(parents=True, exist_ok=True)
    for name, content in intelligence.artifacts.items():
        (base / name).write_text(content, encoding="utf-8")
    model.write_json(
        str(base / "next-actions.json"),
        {"schema_version": model.SCHEMA_VERSION,
         "kind": "career_next_actions",
         "workflow_id": intelligence.workflow_id,
         "actions": [action.to_dict()
                     for action in intelligence.next_actions]})
    model.write_json(str(base / "analysis.json"), intelligence.to_dict())
    model.write_json(
        str(base / "lifeos-note.json"),
        {"kind": "lifeos_note", "type": "career-intelligence",
         "workflow_id": intelligence.workflow_id,
         "project_id": intelligence.project_id,
         "mission_id": intelligence.mission_id,
         "markdown": _lifeos_note(intelligence)})


def load_career_intelligence(root: str,
                             workflow_id: str = "career"
                             ) -> dict[str, Any] | None:
    return model.read_json(
        str(Path(root) / "realworld" / "career" / workflow_id
            / "analysis.json"))


__all__ = [
    "FAMILY",
    "CareerInputs",
    "CareerIntelligence",
    "EvidenceSource",
    "run_career_intelligence",
    "load_career_intelligence",
]
