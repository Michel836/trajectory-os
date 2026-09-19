"""M071 — portfolio / external proof: professional, privacy-safe evidence.

Produces, deterministically and without fabricating any metric:

1. an executive project overview (readable in ~60 seconds);
2. a text/DOT architecture representation of the value chain;
3. three case studies (career, life-sciences, adaptive execution/learning);
4. a reproducible, privacy-safe demo (sample inputs only);
5. an external evidence pack (metrics, trust boundaries, artifacts,
   reproducibility);
6. build-in-public / LinkedIn **draft** material (never auto-published).

Every metric in a generated document carries an explicit evidence source. A
draft never claims a metric that is not present in the evidence pack.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.realworld import model

FAMILY = "PORTFOLIO_PROOF"

#: Value metrics (practical, not infrastructure volume).
METRIC_TIME_TO_DELIVERABLE = "time_to_usable_deliverable_s"
METRIC_EVIDENCE_COVERAGE = "evidence_coverage"
METRIC_SOURCE_COVERAGE = "source_coverage"
METRIC_OUTPUT_COMPLETENESS = "output_completeness"
METRIC_PREDICTION_ERROR = "prediction_error"
METRIC_OUTCOME_CAPTURE = "outcome_capture_completeness"
METRIC_UNRESOLVED_UNKNOWNS = "unresolved_unknowns"
METRIC_CORRECTIONS = "corrections_required"
METRIC_REUSE = "reuse_across_workflow_families"

#: The deterministic architecture value chain.
ARCHITECTURE_CHAIN = (
    "Objective",
    "Project / Workflow",
    "Knowledge",
    "Prediction",
    "Decision",
    "Execution",
    "Validation",
    "Outcome",
    "Learning",
)


@dataclass(frozen=True)
class ValueMetric:
    name: str
    value: float | int | str | None
    unit: str
    source: str
    evidence: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "unit": self.unit,
                "source": self.source, "evidence": self.evidence}


@dataclass(frozen=True)
class CaseStudy:
    case_id: str
    title: str
    problem: str
    inputs: tuple[str, ...]
    approach: tuple[str, ...]
    architecture: tuple[str, ...]
    evidence: tuple[str, ...]
    result: tuple[str, ...]
    limitations: tuple[str, ...]
    lessons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "problem": self.problem,
            "inputs": list(self.inputs),
            "approach": list(self.approach),
            "architecture": list(self.architecture),
            "evidence": list(self.evidence),
            "result": list(self.result),
            "limitations": list(self.limitations),
            "lessons": list(self.lessons),
        }

    def to_markdown(self) -> str:
        lines = [f"## {self.title}", "",
                 f"**Problem.** {self.problem}", "",
                 "**Inputs.**"]
        lines.extend(f"- {item}" for item in self.inputs)
        lines += ["", "**Approach.**"]
        lines.extend(f"- {item}" for item in self.approach)
        lines += ["", "**Architecture.**"]
        lines.extend(f"- {item}" for item in self.architecture)
        lines += ["", "**Evidence.**"]
        lines.extend(f"- {item}" for item in self.evidence)
        lines += ["", "**Result.**"]
        lines.extend(f"- {item}" for item in self.result)
        lines += ["", "**Limitations.**"]
        lines.extend(f"- {item}" for item in self.limitations)
        lines += ["", "**Lessons.**"]
        lines.extend(f"- {item}" for item in self.lessons)
        return "\n".join(lines) + "\n"


@dataclass
class PortfolioEvidence:
    """The evidence the portfolio documents must trace back to."""

    generated_at: str
    version: str
    acceptance_passed: int
    acceptance_total: int
    dogfood_history_source: str
    dogfood_real_rows: int
    workflow_families: tuple[str, ...]
    outcome_links: int
    outcome_reconciled: int
    unresolved_unknowns: int
    corrections_required: int
    prediction_error: float | None
    trust_clean: bool
    trust_scanned_files: int
    case_studies: tuple[CaseStudy, ...]
    source_coverage: int
    output_completeness: float
    time_to_deliverable_s: float | None
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "realworld_version": model.REALWORLD_VERSION,
            "kind": "portfolio_evidence",
            "generated_at": self.generated_at,
            "version": self.version,
            "acceptance": {"passed": self.acceptance_passed,
                           "total": self.acceptance_total},
            "dogfood": {"history_source": self.dogfood_history_source,
                        "real_rows": self.dogfood_real_rows,
                        "workflow_families": list(self.workflow_families)},
            "outcomes": {"links": self.outcome_links,
                         "reconciled": self.outcome_reconciled,
                         "prediction_error": self.prediction_error},
            "unknowns": self.unresolved_unknowns,
            "corrections_required": self.corrections_required,
            "source_coverage": self.source_coverage,
            "output_completeness": self.output_completeness,
            "time_to_deliverable_s": self.time_to_deliverable_s,
            "trust": {"clean": self.trust_clean,
                      "scanned_files": self.trust_scanned_files},
            "case_studies": [study.to_dict() for study in self.case_studies],
            "limitations": list(self.limitations),
        }

    def value_metrics(self) -> tuple[ValueMetric, ...]:
        total = self.acceptance_total or 1
        return (
            ValueMetric(METRIC_TIME_TO_DELIVERABLE,
                        self.time_to_deliverable_s, "seconds",
                        "dogfood timing"),
            ValueMetric(METRIC_EVIDENCE_COVERAGE,
                        round(self.acceptance_passed / total, 4), "ratio",
                        "acceptance matrix"),
            ValueMetric(METRIC_SOURCE_COVERAGE, self.source_coverage,
                        "sources", "ingestion manifest"),
            ValueMetric(METRIC_OUTPUT_COMPLETENESS,
                        round(self.output_completeness, 4), "ratio",
                        "workflow artifacts"),
            ValueMetric(METRIC_PREDICTION_ERROR, self.prediction_error,
                        "mean_absolute_error", "outcome ledger"),
            ValueMetric(METRIC_OUTCOME_CAPTURE,
                        (round(self.outcome_reconciled / self.outcome_links, 4)
                         if self.outcome_links else None), "ratio",
                        "outcome ledger"),
            ValueMetric(METRIC_UNRESOLVED_UNKNOWNS, self.unresolved_unknowns,
                        "count", "workflow unknowns"),
            ValueMetric(METRIC_CORRECTIONS, self.corrections_required,
                        "count", "review findings"),
            ValueMetric(METRIC_REUSE, len(self.workflow_families), "families",
                        "workflow families"),
        )


def architecture_text() -> str:
    lines = ["Value chain (deterministic):", ""]
    for index, stage in enumerate(ARCHITECTURE_CHAIN):
        indent = "  " * index
        lines.append(f"{indent}{stage}")
        if index < len(ARCHITECTURE_CHAIN) - 1:
            lines.append(f"{indent}  |")
            lines.append(f"{indent}  v")
    return "\n".join(lines) + "\n"


def architecture_dot() -> str:
    lines = ["digraph trajectory_os {", '  rankdir="LR";']
    for index, stage in enumerate(ARCHITECTURE_CHAIN):
        node = f"n{index}"
        lines.append(f'  {node} [label="{stage}"];')
        if index:
            lines.append(f"  n{index - 1} -> {node};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def executive_overview(evidence: PortfolioEvidence) -> str:
    """A ~60-second overview, every claim traceable to evidence."""
    metrics = {metric.name: metric for metric in evidence.value_metrics()}
    acceptance = f"{evidence.acceptance_passed}/{evidence.acceptance_total}"
    lines = [
        "# TrajectoryOS — executive overview", "",
        "## What it is",
        "TrajectoryOS is an adaptive AI execution and decision-intelligence "
        "platform. It turns unstructured intentions into a structured, "
        "auditable portfolio and then tracks what actually happened after each "
        "recommendation.", "",
        "## Problem solved",
        "People and teams accumulate intentions, projects and decisions in "
        "notes, inboxes and documents. Priorities drift, evidence goes stale "
        "and nobody reconciles predictions with outcomes. TrajectoryOS makes "
        "the portfolio explicit, keeps evidence attached, and records reality "
        "after the fact.", "",
        "## Architecture",
        "```", architecture_text().rstrip(), "```", "",
        "## Implemented capabilities",
        "- real input adapters with a deterministic provenance manifest;",
        "- career and life-sciences intelligence with typed claims;",
        "- operational intelligence over the LifeOS-compatible project model;",
        "- outcome tracking with prediction-error reconciliation;",
        "- guarded champion/challenger model refresh (no auto-promotion);",
        "- a read-only, non-developer decision cockpit;",
        "- a reproducible privacy-safe portfolio demo.", "",
        "## Trust model",
        "- AI proposes, deterministic code validates, the human decides;",
        "- no release Git writes from workflow/intelligence/cockpit layers;",
        "- GO COMMIT / exact-head CI / GO MERGE unchanged;",
        "- every claim is FACT / OBSERVATION / EVIDENCE / INFERENCE / "
        "HYPOTHESIS; UNKNOWN stays UNKNOWN.", "",
        "## Measured evidence",
        f"- acceptance cases: {acceptance} (evidence: acceptance matrix);",
        f"- dogfood history: {evidence.dogfood_history_source} "
        f"({evidence.dogfood_real_rows} real rows);",
        f"- outcome links: {evidence.outcome_links} "
        f"({evidence.outcome_reconciled} reconciled);",
        f"- prediction error (MAE): "
        f"{metrics[METRIC_PREDICTION_ERROR].value}",
        f"- source coverage: {evidence.source_coverage} source(s);",
        f"- workflow families exercised: "
        f"{', '.join(evidence.workflow_families) or 'none'};",
        f"- trust-boundary scan clean: {evidence.trust_clean} "
        f"({evidence.trust_scanned_files} files scanned).", "",
        "## Limitations",
    ]
    lines.extend(f"- {item}" for item in evidence.limitations) \
        if evidence.limitations else lines.append("- none recorded")
    return "\n".join(lines) + "\n"


def case_studies_markdown(evidence: PortfolioEvidence) -> str:
    parts = ["# Case studies", ""]
    for study in evidence.case_studies:
        parts.append(study.to_markdown().rstrip("\n"))
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def evidence_pack(evidence: PortfolioEvidence) -> dict[str, Any]:
    metrics = [metric.to_dict() for metric in evidence.value_metrics()]
    return {
        "schema_version": model.SCHEMA_VERSION,
        "kind": "external_evidence_pack",
        "generated_at": evidence.generated_at,
        "version": evidence.version,
        "metrics": metrics,
        "trust_boundaries": {
            "clean": evidence.trust_clean,
            "scanned_files": evidence.trust_scanned_files,
            "release_git_writes_from_intelligence": 0,
            "human_gates": ["GO COMMIT", "exact-head CI", "GO MERGE"],
        },
        "case_studies": [study.to_dict() for study in evidence.case_studies],
        "architecture": {
            "chain": list(ARCHITECTURE_CHAIN),
            "text": architecture_text(),
            "dot": architecture_dot(),
        },
        "reproducibility": {
            "command": "scripts/trajectory demo portfolio",
            "privacy_safe": True,
            "uses_private_data": False,
            "inputs": "SAMPLE inputs only, clearly labelled",
        },
        "limitations": list(evidence.limitations),
    }


def build_in_public_drafts(evidence: PortfolioEvidence) -> dict[str, Any]:
    """Draft-only build-in-public material. Never auto-published."""
    metrics = {metric.name: metric for metric in evidence.value_metrics()}
    draft_topics = (
        ("project origin",
         "Why TrajectoryOS exists: turning unstructured intent into a "
         "structured, auditable portfolio."),
        ("trusted agents",
         "Why the agent proposes but the human decides, and how the trust "
         "boundaries are enforced in code."),
        ("human gates",
         "How GO COMMIT / exact-head CI / GO MERGE keep consequential "
         "decisions with the human."),
        ("self-hosting",
         "How TrajectoryOS is developed with its own gated workflow."),
        ("predictive ML",
         "Advisory prediction with explicit uncertainty and a guarded "
         "champion/challenger refresh that never auto-promotes."),
        ("real-world workflows",
         "Career and life-sciences intelligence over real or clearly labelled "
         "sample inputs."),
    )
    drafts: list[dict[str, Any]] = []
    for title, angle in draft_topics:
        drafts.append({
            "title": title,
            "angle": angle,
            "status": "DRAFT",
            "published": False,
            "evidence_metrics": [
                metric.to_dict() for metric in metrics.values()
                if metric.evidence],
        })
    return {
        "schema_version": model.SCHEMA_VERSION,
        "kind": "build_in_public_drafts",
        "generated_at": evidence.generated_at,
        "auto_publish": False,
        "drafts": drafts,
        "rule": "never claim a metric that is not present in the evidence pack",
    }


def reproducibility_doc() -> str:
    return (
        "# Reproducibility\n\n"
        "Run the privacy-safe demo (SAMPLE inputs only):\n\n"
        "```bash\n"
        "scripts/trajectory demo portfolio\n"
        "```\n\n"
        "The demo writes a complete artifact set under the chosen runtime "
        "root and prints the cockpit. It never reads private data, never "
        "publishes and never performs a Git release write.\n\n"
        "Canonical quality gate:\n\n"
        "```bash\n"
        "bash scripts/quality.sh\n"
        "```\n")


def write_portfolio(root: str, evidence: PortfolioEvidence) -> dict[str, str]:
    """Persist every portfolio document; return the written paths."""
    base = Path(root) / "realworld" / "portfolio"
    base.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    documents = {
        "executive-overview.md": executive_overview(evidence),
        "case-studies.md": case_studies_markdown(evidence),
        "architecture.txt": architecture_text(),
        "architecture.dot": architecture_dot(),
        "reproducibility.md": reproducibility_doc(),
    }
    for name, content in documents.items():
        (base / name).write_text(content, encoding="utf-8")
        written[name] = str(base / name)
    model.write_json(str(base / "evidence-pack.json"),
                     evidence_pack(evidence))
    written["evidence-pack.json"] = str(base / "evidence-pack.json")
    model.write_json(str(base / "build-in-public.json"),
                     build_in_public_drafts(evidence))
    written["build-in-public.json"] = str(base / "build-in-public.json")
    (base / "README.md").write_text(
        "# External evidence pack\n\n"
        "- `executive-overview.md` — 60-second overview\n"
        "- `case-studies.md` — three case studies\n"
        "- `architecture.txt` / `architecture.dot` — value chain\n"
        "- `evidence-pack.json` — metrics, trust boundaries, artifacts\n"
        "- `build-in-public.json` — DRAFT material (never auto-published)\n"
        "- `reproducibility.md` — reproduce the demo\n",
        encoding="utf-8")
    written["README.md"] = str(base / "README.md")
    return written


__all__ = [
    "ARCHITECTURE_CHAIN",
    "FAMILY",
    "METRIC_CORRECTIONS",
    "METRIC_EVIDENCE_COVERAGE",
    "METRIC_OUTCOME_CAPTURE",
    "METRIC_OUTPUT_COMPLETENESS",
    "METRIC_PREDICTION_ERROR",
    "METRIC_REUSE",
    "METRIC_SOURCE_COVERAGE",
    "METRIC_TIME_TO_DELIVERABLE",
    "METRIC_UNRESOLVED_UNKNOWNS",
    "CaseStudy",
    "PortfolioEvidence",
    "ValueMetric",
    "architecture_dot",
    "architecture_text",
    "build_in_public_drafts",
    "case_studies_markdown",
    "evidence_pack",
    "executive_overview",
    "reproducibility_doc",
    "write_portfolio",
]
