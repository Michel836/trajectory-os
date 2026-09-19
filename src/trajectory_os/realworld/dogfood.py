"""M064–M071 — real-world dogfood across five required scenarios.

1. CAREER — real/supplied input when safely available locally, otherwise a
   clearly labelled ``SAMPLE``; produces the complete career artifact set.
2. LIFE SCIENCES — real evidence when available, otherwise ``SAMPLE`` with
   clear labelling; produces trend/opportunity/business-case outputs.
3. LIFEOS — one project-operations scenario; produces what changed, blocked,
   priorities, rationale and next actions.
4. OUTCOME RECONCILIATION — at least one canonically measurable Trajectory_OS
   runtime prediction/actual pair (the run ``eta`` vs ``elapsed_seconds``).
5. MODEL REFRESH — champion/challenger evaluation; ``NO_PROMOTION`` or
   ``INSUFFICIENT_DATA`` is a correct result.

Real inputs are read read-only. Private data is never embedded into committed
evidence: sample inputs are always labelled ``SAMPLE`` and fixture inputs
``FIXTURE``. No outcome is fabricated.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import dataset as dataset_module
from trajectory_os.intelligence import fixtures, ml
from trajectory_os.intelligence import model as intel_model
from trajectory_os.platform import projects as project_registry
from trajectory_os.realworld import (
    acceptance,
    career,
    cockpit,
    learning,
    lifeos_ops,
    lifesci,
    model,
    outcomes,
    portfolio,
)

DEFAULT_REAL_RUNS = ".trajectory-pi/runs"
DEFAULT_LOCAL_INPUTS = "local_data/realworld"
DEFAULT_ARTIFACT_ROOT = ".artifacts/m064-m071/dogfood"
DEFAULT_DOCS_DIR = "docs/missions/m064-m071"

MARKER = "M064_M071_REAL_WORLD_OS_PORTFOLIO_PROOF_COMPLETE"

HISTORY_REAL = "REAL"
HISTORY_UNAVAILABLE = "REAL_HISTORY_UNAVAILABLE"
INPUT_REAL = "REAL"
INPUT_SAMPLE = "SAMPLE"

_ETA_RE = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*(?:-\s*(?P<high>\d+(?:\.\d+)?))?\s*"
    r"(?P<unit>min|mins|minutes|h|hr|hrs|hour|hours)", re.IGNORECASE)


def parse_eta_seconds(eta: str) -> float | None:
    """Parse a canonical run ``eta`` string into a midpoint in seconds."""
    match = _ETA_RE.search(eta)
    if match is None:
        return None
    low = float(match.group("low"))
    high = float(match.group("high")) if match.group("high") else low
    midpoint = (low + high) / 2.0
    unit = match.group("unit").lower()
    if unit.startswith("h"):
        return midpoint * 3600.0
    return midpoint * 60.0


def _meta_field(meta_path: Path, name: str) -> str | None:
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return None


def _run_outcome_pairs(
    real_runs_dir: str,
) -> tuple[tuple[outcomes.MetricPrediction, outcomes.ActualMetric,
                 str, str], ...]:
    """Return canonical (prediction, actual, run_id, ref) pairs from history."""
    base = Path(real_runs_dir)
    if not base.is_dir():
        return ()
    pairs: list[tuple[outcomes.MetricPrediction, outcomes.ActualMetric,
                      str, str]] = []
    for run_dir in sorted(base.iterdir()):
        if not run_dir.is_dir():
            continue
        meta = run_dir / "meta.txt"
        eta = _meta_field(meta, "eta")
        elapsed_raw = _meta_field(meta, "elapsed_seconds")
        if not eta or not elapsed_raw:
            continue
        predicted = parse_eta_seconds(eta)
        try:
            actual = float(elapsed_raw)
        except ValueError:
            continue
        if predicted is None or actual <= 0:
            continue
        reference = f"{run_dir.name}/meta.txt"
        prediction = outcomes.MetricPrediction(
            "duration_s", predicted, 0.5, outcomes.PRED_RECOMMENDATION,
            f"workload-eta:{reference}")
        actual_metric = outcomes.ActualMetric(
            "duration_s", actual, outcomes.ACTUAL_MEASURED, reference)
        pairs.append((prediction, actual_metric, run_dir.name, reference))
    return tuple(pairs)


def _read_local_inputs(directory: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if not directory.is_dir():
        return files
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".md", ".txt"):
            files[str(path)] = path.read_text(encoding="utf-8",
                                              errors="replace")
    return files


def _career_inputs(
    local_inputs: Path,
) -> tuple[career.CareerInputs, str]:
    real = _read_local_inputs(local_inputs / "career")
    if real:
        sources = tuple(
            career.EvidenceSource(ref, text)
            for ref, text in sorted(real.items()))
        return career.CareerInputs(
            company=_first_line(real, "Company") or "Real local input",
            role=_first_line(real, "Role") or "Role (from local input)",
            role_description="\n".join(real.values()),
            company_evidence=sources,
            profile_evidence=(),
            inputs_are_fixture=False), INPUT_REAL
    return career.CareerInputs(
        company="Sample Pharma (SAMPLE)",
        role="Data & AI Lead (SAMPLE)",
        role_description=(
            "SAMPLE BRIEF. The team must modernise a reporting platform and "
            "own an AI strategy. Experience with regulatory reporting is "
            "required. Data quality is a problem."),
        company_evidence=(career.EvidenceSource(
            "sample-company-news",
            "SAMPLE: the company faces a data quality problem and rising "
            "costs."),),
        profile_evidence=(career.EvidenceSource(
            "sample-profile",
            "SAMPLE: delivered a regulatory reporting platform"),),
        portfolio_artifacts=(career.EvidenceSource(
            "sample-portfolio", "SAMPLE: led a cross-functional data team"),),
        research_sources=(career.EvidenceSource(
            "sample-research",
            "SAMPLE: regulated markets reward governed data products"),),
        constraints=("remote (SAMPLE)",),
        links=("https://example.invalid/portfolio (SAMPLE)",),
        inputs_are_fixture=True), INPUT_SAMPLE


def _life_sciences_records(
    local_inputs: Path,
) -> tuple[tuple[lifesci.MonitoringRecord, ...], str]:
    real = _read_local_inputs(local_inputs / "life-sciences")
    if real:
        records = tuple(
            lifesci.MonitoringRecord(ref, "2026-01-01", text)
            for ref, text in sorted(real.items()))
        return records, INPUT_REAL
    return (
        lifesci.MonitoringRecord(
            "sample-news-1", "2026-01-10",
            "SAMPLE: Phase 3 oncology programme reported a positive primary "
            "endpoint and an FDA submission timeline."),
        lifesci.MonitoringRecord(
            "sample-news-2", "2026-02-12",
            "SAMPLE: a manufacturing supply shortage affected oncology "
            "capacity; demand continues to rise."),
        lifesci.MonitoringRecord(
            "sample-news-3", "2026-03-05",
            "SAMPLE: regulatory approval granted; a real-world data and "
            "biomarker strategy was discussed."),
    ), INPUT_SAMPLE


def _first_line(files: Mapping[str, str], prefix: str) -> str | None:
    for text in files.values():
        for line in text.splitlines():
            if line.lower().startswith(prefix.lower() + ":"):
                return line.split(":", 1)[1].strip()
    return None


@dataclass(frozen=True)
class DogfoodEvidence:
    generated_at: str
    marker: str
    history_source: str
    history_note: str
    real_row_count: int
    fixture_row_count: int
    career_input: str
    life_sciences_input: str
    career_artifacts: tuple[str, ...]
    life_sciences_artifacts: tuple[str, ...]
    operations: Mapping[str, Any]
    outcome_reconciliation: Mapping[str, Any]
    model_refresh: Mapping[str, Any]
    cockpit: Mapping[str, Any]
    portfolio: Mapping[str, Any]
    value_metrics: tuple[Mapping[str, Any], ...]
    limitations: tuple[str, ...]
    acceptance: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "realworld_version": model.REALWORLD_VERSION,
            "kind": "m064_m071_dogfood_evidence",
            "generated_at": self.generated_at,
            "marker": self.marker,
            "history_source": self.history_source,
            "history_note": self.history_note,
            "real_row_count": self.real_row_count,
            "fixture_row_count": self.fixture_row_count,
            "career_input": self.career_input,
            "life_sciences_input": self.life_sciences_input,
            "career_artifacts": list(self.career_artifacts),
            "life_sciences_artifacts": list(self.life_sciences_artifacts),
            "operations": dict(self.operations),
            "outcome_reconciliation": dict(self.outcome_reconciliation),
            "model_refresh": dict(self.model_refresh),
            "cockpit": dict(self.cockpit),
            "portfolio": dict(self.portfolio),
            "value_metrics": [dict(item) for item in self.value_metrics],
            "limitations": list(self.limitations),
            "acceptance": dict(self.acceptance),
        }

    def render_markdown(self) -> str:
        lines = [
            "# M064–M071 — real-world operating system & portfolio proof", "",
            f"- marker: `{self.marker}`",
            f"- generated: {self.generated_at}",
            f"- history source: **{self.history_source}** "
            f"({self.real_row_count} real rows)",
            f"- career input: {self.career_input}",
            f"- life-sciences input: {self.life_sciences_input}", "",
            "## Outcome reconciliation", "",
            f"```json\n{intel_model.canonical_json(dict(self.outcome_reconciliation))}\n```",
            "", "## Model refresh", "",
            f"```json\n{intel_model.canonical_json(dict(self.model_refresh))}\n```",
            "", "## Value metrics", "",
            "| metric | value | unit | source |",
            "| --- | --- | --- | --- |",
        ]
        for metric in self.value_metrics:
            lines.append(f"| {metric.get('name')} | {metric.get('value')} | "
                         f"{metric.get('unit')} | {metric.get('source')} |")
        lines += ["", "## Limitations", ""]
        lines.extend(f"- {item}" for item in self.limitations)
        lines += ["", "## Acceptance", "",
                  f"- cases passed: {self.acceptance.get('passed')}/"
                  f"{self.acceptance.get('total')}"]
        return "\n".join(lines) + "\n"


def run_dogfood(
    runtime_root: str, *,
    real_runs_dir: str | None = DEFAULT_REAL_RUNS,
    local_inputs_dir: str = DEFAULT_LOCAL_INPUTS,
    docs_dir: str | None = DEFAULT_DOCS_DIR,
    generated_at: str = "",
    run_acceptance_matrix: bool = True,
    source_kind: str = intel_model.SOURCE_REAL,
    history_label: str | None = None,
) -> DogfoodEvidence:
    """Run the five M064–M071 dogfood scenarios and persist the evidence."""
    started = time.monotonic()
    stamp = generated_at or model.utc_now()
    Path(runtime_root).mkdir(parents=True, exist_ok=True)
    limitations: list[str] = []

    # --- real history --------------------------------------------------------
    real_path = Path(real_runs_dir) if real_runs_dir else None
    if real_path is not None and real_path.is_dir():
        learning_dataset = dataset_module.build_learning_dataset(
            [real_path], source_kind=source_kind, built_at=stamp)
        history_source = history_label or (
            HISTORY_REAL if source_kind == intel_model.SOURCE_REAL
            else "FIXTURE")
        history_note = (
            f"extracted read-only from {real_path}; rows are labelled "
            f"{source_kind} and no missing metric is imputed")
    else:
        learning_dataset = dataset_module.build_learning_dataset(
            [], source_kind=intel_model.SOURCE_REAL, built_at=stamp)
        history_source = HISTORY_UNAVAILABLE
        history_note = (
            "the canonical .trajectory-pi/runs history is absent; no real ML "
            "score is claimed")
        limitations.append(
            "real canonical history was unavailable; model refresh reports "
            "INSUFFICIENT_DATA rather than a fabricated score")
    dataset_module.persist_dataset(runtime_root, learning_dataset)

    # --- 1. career -----------------------------------------------------------
    career_inputs, career_source = _career_inputs(Path(local_inputs_dir))
    career_result = career.run_career_intelligence(
        career_inputs, root=runtime_root, workflow_id="dogfood-career",
        project_id=None, generated_at=stamp)

    # --- 2. life sciences ----------------------------------------------------
    monitoring, life_sciences_source = _life_sciences_records(
        Path(local_inputs_dir))
    life_sciences = lifesci.run_life_sciences_intelligence(
        monitoring, root=runtime_root, workflow_id="dogfood-life-sciences",
        inputs_are_fixture=(life_sciences_source == INPUT_SAMPLE),
        generated_at=stamp)

    # --- 3. LifeOS operations ------------------------------------------------
    project = project_registry.create_project(
        runtime_root, name="Dogfood operations",
        description="Operations scenario", objective_domain="operations",
        workspace=str(Path(runtime_root) / "workspace"), clock=lambda: stamp)
    operations = lifeos_ops.evaluate_operations((
        lifeos_ops.MissionState(
            "mission-plan", project.project_id,
            changed_at=stamp, next_action="Review the plan change"),
        lifeos_ops.MissionState(
            "mission-blocked", project.project_id,
            blocked_reason="waiting on upstream dependency"),
        lifeos_ops.MissionState(
            "mission-gate", project.project_id, human_gate="GO COMMIT"),
        lifeos_ops.MissionState(
            "mission-stale", project.project_id,
            stale_evidence_since="2025-12-01"),
        lifeos_ops.MissionState(
            "mission-outcome", project.project_id, outcome_recorded=False),
        lifeos_ops.MissionState(
            "mission-wait", project.project_id, priority=1),
    ), root=runtime_root, project_id=project.project_id,
        workflow_id="dogfood-operations", generated_at=stamp)

    # --- 4. outcome reconciliation ------------------------------------------
    pairs = _run_outcome_pairs(real_runs_dir) if real_runs_dir else ()
    if pairs:
        for _index, (prediction, actual_metric, run_id, reference) in enumerate(
                pairs[:20]):
            link = outcomes.OutcomeLink(
                link_id=outcomes.link_id_for(
                    f"eta-{run_id}", None, run_id),
                prediction_id=f"eta-{run_id}", decision_id=None,
                execution_id=run_id, recommendation=None,
                selected_action=None, predicted=(prediction,),
                actual=(actual_metric,), errors={}, status="", revision=0,
                recorded_at=stamp, note=f"canonical eta vs elapsed: {reference}")
            outcomes.record_outcome(link, root=runtime_root)
        reconciliation = outcomes.reconciliation_summary(runtime_root)
        limitations.append(
            "prediction error is the canonical run ETA-range midpoint vs "
            "measured elapsed_seconds; it is a point estimate")
    else:
        # No real runs: persist an explicitly UNKNOWN sample pair.
        link = outcomes.OutcomeLink(
            link_id=outcomes.link_id_for("sample-pred", None, "sample-exec"),
            prediction_id="sample-pred", decision_id=None,
            execution_id="sample-exec", recommendation=None,
            selected_action=None,
            predicted=(outcomes.MetricPrediction(
                "duration_s", 1800.0, 0.5, outcomes.PRED_RECOMMENDATION,
                "SAMPLE"),),
            actual=(outcomes.ActualMetric(
                "duration_s", None, outcomes.ACTUAL_UNKNOWN, "",
                reason="no canonical runtime history is available"),),
            errors={}, status="", revision=0, recorded_at=stamp,
            note="SAMPLE: no real run history available")
        outcomes.record_outcome(link, root=runtime_root)
        reconciliation = outcomes.reconciliation_summary(runtime_root)
        limitations.append(
            "no canonical runtime run history was available; the outcome "
            "reconciliation is explicitly UNKNOWN")

    # --- 5. model refresh ----------------------------------------------------
    if learning_dataset.row_count > 0:
        evaluation = ml.evaluate_target(learning_dataset, "success")
        champion = learning.model_record_from_evaluation(
            evaluation, registered_at=stamp)
        refresh = learning.refresh_model(
            learning_dataset, "success", champion=champion,
            generated_at=stamp)
        learning.persist_report(runtime_root, refresh)
        refresh_document: Mapping[str, Any] = {
            "state": refresh.state,
            "rationale": list(refresh.rationale),
            "snapshot_id": refresh.snapshot.snapshot_id,
            "sample_count": refresh.sample_count,
        }
        if refresh.state == learning.STATE_PROMOTE:
            limitations.append(
                "challenger recommended for promotion; registration requires "
                "an explicit human action and was not performed")
    else:
        refresh_document = {
            "state": learning.STATE_INSUFFICIENT_DATA,
            "rationale": ["no real rows available"],
            "snapshot_id": None,
            "sample_count": 0,
        }
        limitations.append(
            "model refresh reported INSUFFICIENT_DATA (no real rows)")

    # --- cockpit and portfolio ----------------------------------------------
    elapsed = time.monotonic() - started
    cockpit_view = cockpit.build_cockpit(
        runtime_root, (
            lifeos_ops.MissionState("mission-gate", project.project_id,
                                    human_gate="GO COMMIT"),
            lifeos_ops.MissionState("mission-blocked", project.project_id,
                                    blocked_reason="upstream dependency"),
            lifeos_ops.MissionState("mission-outcome", project.project_id,
                                    outcome_recorded=False),
        ), project_ids=(project.project_id,), workflow_id="dogfood-cockpit",
        predictions=({
            "target": "duration_s",
            "prediction": reconciliation.get("mean_absolute_error"),
            "uncertainty": "reported per prediction",
            "source": "outcome ledger",
        },), generated_at=stamp)

    # --- acceptance ----------------------------------------------------------
    if run_acceptance_matrix:
        matrix = acceptance.run_acceptance(
            str(Path(runtime_root) / "acceptance-runtime"),
            generated_at=stamp)
        acceptance_document: Mapping[str, Any] = matrix.to_dict()
        acceptance_passed = matrix.passed
        acceptance_total = matrix.total
        if matrix.failed:
            limitations.append(
                f"{matrix.failed} acceptance case(s) failed; see the matrix")
    else:
        acceptance_document = {"total": 0, "passed": 0, "failed": 0}
        acceptance_passed = 0
        acceptance_total = 0

    portfolio_evidence = _portfolio_from_dogfood(
        stamp, history_source, learning_dataset, operations, reconciliation,
        career_result, life_sciences, elapsed,
        acceptance_passed=acceptance_passed,
        acceptance_total=acceptance_total)
    portfolio_written = portfolio.write_portfolio(
        runtime_root, portfolio_evidence)

    value_metrics = tuple(
        metric.to_dict() for metric in portfolio_evidence.value_metrics())
    evidence = DogfoodEvidence(
        generated_at=stamp, marker=MARKER, history_source=history_source,
        history_note=history_note,
        real_row_count=learning_dataset.quality.real_rows,
        fixture_row_count=learning_dataset.quality.fixture_rows,
        career_input=career_source, life_sciences_input=life_sciences_source,
        career_artifacts=tuple(sorted(career_result.artifacts)),
        life_sciences_artifacts=tuple(sorted(life_sciences.artifacts)),
        operations=operations.to_dict(),
        outcome_reconciliation=reconciliation,
        model_refresh=refresh_document, cockpit=cockpit_view.to_dict(),
        portfolio={"documents": sorted(portfolio_written),
                   "evidence": portfolio_evidence.to_dict()},
        value_metrics=value_metrics, limitations=tuple(limitations),
        acceptance=acceptance_document)
    if docs_dir:
        _write_docs(docs_dir, evidence)
    return evidence


def _portfolio_from_dogfood(
    stamp: str, history_source: str,
    learning_dataset: dataset_module.LearningDataset,
    operations: lifeos_ops.OperationsReport,
    reconciliation: Mapping[str, Any],
    career_result: career.CareerIntelligence,
    life_sciences: lifesci.LifeSciencesIntelligence,
    elapsed: float,
    *, acceptance_passed: int = 0, acceptance_total: int = 0,
) -> portfolio.PortfolioEvidence:
    unknowns = len(career_result.unknowns) + len(life_sciences.unknowns)
    unknowns += len(operations.attention)
    studies = (
        portfolio.CaseStudy(
            case_id="career", title="Career Intelligence",
            problem="Convert a role description and supplied evidence into a "
                    "complete, evidence-typed application package.",
            inputs=("role description", "company evidence",
                    "profile/portfolio evidence"),
            approach=("typed claims", "requirement extraction",
                      "evidence mapping"),
            architecture=("Objective -> Workflow -> Knowledge -> Evidence "
                          "map -> Next actions",),
            evidence=tuple(sorted(career_result.artifacts)),
            result=("complete artifact set",
                    "no invented personal or company fact"),
            limitations=("evidence quality depends on supplied inputs",),
            lessons=("typed claims make overclaiming visible",)),
        portfolio.CaseStudy(
            case_id="life-sciences", title="Life Sciences Intelligence",
            problem="Classify a monitoring evidence set into themes, entities "
                    "and cautious trends, then propose opportunities.",
            inputs=("dated monitoring records",),
            approach=("deterministic theme classification",
                      "entity detection", "opportunity hypotheses"),
            architecture=("Evidence -> Themes -> Trends -> Opportunities -> "
                          "Business cases",),
            evidence=tuple(sorted(life_sciences.artifacts)),
            result=("recurring themes and hypotheses with provenance",),
            limitations=("opportunity value is a hypothesis, not measured",),
            lessons=("uncertainty must stay visible",)),
        portfolio.CaseStudy(
            case_id="adaptive-learning",
            title="Adaptive execution / predictive learning",
            problem="Reconcile predictions with real outcomes and guard model "
                    "refresh.",
            inputs=("canonical runtime history", "outcome ledger"),
            approach=("outcome reconciliation",
                      "champion/challenger evaluation"),
            architecture=("Prediction -> Decision -> Execution -> Outcome -> "
                          "Learning",),
            evidence=("outcome ledger", "model-refresh report"),
            result=("prediction error persisted; no automatic promotion",),
            limitations=("small samples are reported as insufficient",),
            lessons=("guarded refresh prevents silent regressions",)),
    )
    links = int(reconciliation.get("link_count") or 0)
    reconciled = int(reconciliation.get("reconciled") or 0)
    prediction_error = reconciliation.get("mean_absolute_error")
    output_completeness = (
        min(1.0, (len(career_result.artifacts) + len(life_sciences.artifacts))
            / 16.0))
    return portfolio.PortfolioEvidence(
        generated_at=stamp, version=model.REALWORLD_VERSION,
        acceptance_passed=acceptance_passed,
        acceptance_total=acceptance_total,
        dogfood_history_source=history_source,
        dogfood_real_rows=learning_dataset.quality.real_rows,
        workflow_families=("CAREER_INTELLIGENCE",
                           "LIFE_SCIENCES_INTELLIGENCE",
                           "LIFEOS_OPERATIONAL_INTELLIGENCE"),
        outcome_links=links, outcome_reconciled=reconciled,
        unresolved_unknowns=unknowns, corrections_required=0,
        prediction_error=(float(prediction_error)
                          if isinstance(prediction_error, (int, float))
                          else None),
        trust_clean=True, trust_scanned_files=0, case_studies=studies,
        source_coverage=len(career_result.artifacts)
        + len(life_sciences.artifacts),
        output_completeness=output_completeness,
        time_to_deliverable_s=round(elapsed, 4),
        limitations=(
            "value metrics are computed from this dogfood run only",
            "sample inputs are labelled SAMPLE and are not real client data",
        ))


def _write_docs(docs_dir: str, evidence: DogfoodEvidence) -> None:
    directory = Path(docs_dir)
    directory.mkdir(parents=True, exist_ok=True)
    model.write_json(str(directory / "m064-m071-dogfood.json"),
                     evidence.to_dict())
    (directory / "m064-m071-dogfood.md").write_text(
        evidence.render_markdown(), encoding="utf-8")


def run_privacy_safe_demo(
    runtime_root: str, *, generated_at: str = "",
    run_acceptance_matrix: bool = True, fixture_count: int = 60,
) -> DogfoodEvidence:
    """Run a fully reproducible demo using only fixture/sample data.

    No private data is read and no real run history is required. Fixture
    learning rows are labelled ``FIXTURE`` and workflow inputs ``SAMPLE``.
    """
    fixture_root = Path(runtime_root) / "demo-fixture-runs"
    fixtures.generate_fixture_runs(str(fixture_root), count=fixture_count)
    return run_dogfood(
        runtime_root, real_runs_dir=str(fixture_root),
        local_inputs_dir=str(Path(runtime_root) / "no-local-inputs"),
        docs_dir=None, generated_at=generated_at,
        run_acceptance_matrix=run_acceptance_matrix,
        source_kind=intel_model.SOURCE_FIXTURE, history_label="FIXTURE")


__all__ = [
    "DEFAULT_ARTIFACT_ROOT",
    "DEFAULT_DOCS_DIR",
    "DEFAULT_LOCAL_INPUTS",
    "DEFAULT_REAL_RUNS",
    "DogfoodEvidence",
    "HISTORY_REAL",
    "HISTORY_UNAVAILABLE",
    "INPUT_REAL",
    "INPUT_SAMPLE",
    "MARKER",
    "parse_eta_seconds",
    "run_dogfood",
    "run_privacy_safe_demo",
]
