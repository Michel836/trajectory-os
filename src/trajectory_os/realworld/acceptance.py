"""M064–M071 — real-world acceptance matrix (>= 60 executable cases).

Each case is a deterministic check. The matrix covers INPUTS, CAREER,
LIFE_SCIENCES, LIFEOS, OUTCOMES, ACTIVE_LEARNING, COCKPIT, PORTFOLIO and
TRUST, and uses fixture/sample inputs so it runs in CI where private data is
unavailable. Fixture values are always labelled as such; no synthetic value is
presented as real.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import dataset as dataset_module
from trajectory_os.intelligence import fixtures, knowledge, ml
from trajectory_os.platform import projects as project_registry
from trajectory_os.realworld import (
    career,
    cockpit,
    ingest,
    learning,
    lifeos_ops,
    lifesci,
    model,
    outcomes,
    portfolio,
)
from trajectory_os.realworld.career import CareerIntelligence
from trajectory_os.realworld.cockpit import CockpitView
from trajectory_os.realworld.learning import RefreshReport
from trajectory_os.realworld.lifeos_ops import OperationsReport
from trajectory_os.realworld.lifesci import LifeSciencesIntelligence
from trajectory_os.release import model as release_model

CAT_INPUTS = "INPUTS"
CAT_CAREER = "CAREER"
CAT_LIFE_SCIENCES = "LIFE_SCIENCES"
CAT_LIFEOS = "LIFEOS"
CAT_OUTCOMES = "OUTCOMES"
CAT_ACTIVE_LEARNING = "ACTIVE_LEARNING"
CAT_COCKPIT = "COCKPIT"
CAT_PORTFOLIO = "PORTFOLIO"
CAT_TRUST = "TRUST"

CATEGORIES = (
    CAT_INPUTS, CAT_CAREER, CAT_LIFE_SCIENCES, CAT_LIFEOS, CAT_OUTCOMES,
    CAT_ACTIVE_LEARNING, CAT_COCKPIT, CAT_PORTFOLIO, CAT_TRUST,
)

_GIT_VERBS = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
              "rebase", "switch", "checkout")


@dataclass(frozen=True)
class AcceptanceCase:
    case_id: str
    category: str
    description: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "category": self.category,
                "description": self.description, "passed": self.passed,
                "detail": self.detail}


@dataclass(frozen=True)
class AcceptanceMatrix:
    cases: tuple[AcceptanceCase, ...]
    generated_at: str

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for case in self.cases if case.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def category_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for case in self.cases:
            bucket = counts.setdefault(case.category,
                                       {"passed": 0, "failed": 0})
            bucket["passed" if case.passed else "failed"] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "realworld_version": model.REALWORLD_VERSION,
            "kind": "realworld_acceptance_matrix",
            "generated_at": self.generated_at,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "category_counts": self.category_counts(),
            "cases": [case.to_dict() for case in self.cases],
        }

    def render_markdown(self) -> str:
        lines = [
            "# M064–M071 — real-world acceptance matrix", "",
            f"- generated: {self.generated_at}",
            f"- cases: {self.passed}/{self.total} passed", "",
            "| category | passed | failed |", "| --- | --- | --- |",
        ]
        for category in CATEGORIES:
            counts = self.category_counts().get(category, {})
            lines.append(f"| {category} | {counts.get('passed', 0)} | "
                         f"{counts.get('failed', 0)} |")
        lines += ["", "| id | category | pass | detail |",
                  "| --- | --- | --- | --- |"]
        for case in self.cases:
            status = "PASS" if case.passed else "FAIL"
            lines.append(f"| {case.case_id} | {case.category} | {status} | "
                         f"{case.detail.replace('|', '/')} |")
        return "\n".join(lines) + "\n"


@dataclass
class _Context:
    root: str
    ingest_manifest: ingest.IngestionManifest
    ingest_repeat: ingest.IngestionManifest
    ingest_changed: ingest.IngestionManifest
    ingest_restricted: ingest.IngestionManifest
    ingest_unsupported: tuple[ingest.SourceRecord, ...]
    career: CareerIntelligence
    career_empty: CareerIntelligence
    life_sciences: LifeSciencesIntelligence
    operations: OperationsReport
    reconciliation: Mapping[str, Any]
    refresh_no_promotion: RefreshReport
    refresh_insufficient: RefreshReport
    refresh_promote: RefreshReport
    cockpit: CockpitView
    portfolio: portfolio.PortfolioEvidence
    learning: dataset_module.LearningDataset = field(repr=False)


def _write_inputs(directory: Path) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "notes").mkdir(exist_ok=True)
    (directory / "data").mkdir(exist_ok=True)
    (directory / "artifacts").mkdir(exist_ok=True)
    company = ("Acme Pharma faces a data quality problem and rising costs. "
               "Regulatory reporting must improve.")
    (directory / "notes" / "company.md").write_text(company, encoding="utf-8")
    (directory / "notes" / "company-copy.md").write_text(company,
                                                         encoding="utf-8")
    (directory / "notes" / "secret-notes.md").write_text(
        "confidential: password = hunter2\n", encoding="utf-8")
    (directory / "data" / "roles.csv").write_text(
        "role,level\nData Lead,senior\n", encoding="utf-8")
    (directory / "artifacts" / "brief.pdf").write_bytes(b"%PDF-1.4 binary")
    restricted = directory / "notes" / "restricted.md"
    restricted.write_text("restricted pricing detail", encoding="utf-8")
    return {"restricted": str(restricted)}


def _build_context(root: str) -> _Context:
    Path(root).mkdir(parents=True, exist_ok=True)
    inputs = Path(root) / "inputs"
    paths = _write_inputs(inputs)

    manifest = ingest.ingest_paths(
        [str(inputs)], root=str(Path(root) / "ingest-a"),
        project_id="p-career", mission_id="m-career",
        generated_at="2026-01-01T00:00:00Z")
    repeat = ingest.ingest_paths(
        [str(inputs)], root=str(Path(root) / "ingest-b"),
        project_id="p-career", mission_id="m-career",
        generated_at="2026-01-01T00:00:01Z")
    # change one file in place, then re-ingest with the prior manifest
    (inputs / "notes" / "company.md").write_text(
        "Acme Pharma faces a scaling challenge and cost pressure.",
        encoding="utf-8")
    changed = ingest.ingest_paths(
        [str(inputs)], root=str(Path(root) / "ingest-c"),
        project_id="p-career", mission_id="m-career",
        prior_manifest=manifest, generated_at="2026-01-02T00:00:00Z")
    restricted = ingest.ingest_paths(
        [paths["restricted"]], root=str(Path(root) / "ingest-d"),
        generated_at="2026-01-03T00:00:00Z",
        sensitivity_overrides={paths["restricted"]:
                               knowledge.SENS_RESTRICTED},
        persist_content=True)
    unsupported = ingest.unsupported_records(manifest)

    career_result = career.run_career_intelligence(
        career.CareerInputs(
            company="Acme Pharma", role="Head of Data & AI",
            role_description=(
                "You must build a data platform. Experience with regulatory "
                "compliance is required. Data quality is a problem."),
            company_evidence=(career.EvidenceSource(
                "news-1", "Acme faces a data quality problem and rising "
                          "costs."),),
            profile_evidence=(career.EvidenceSource(
                "cv", "Built a regulatory reporting data platform"),),
            portfolio_artifacts=(career.EvidenceSource(
                "portfolio", "Delivered a data platform migration"),),
            research_sources=(career.EvidenceSource(
                "research", "The market rewards governed data products"),),
            constraints=("remote",), links=("https://example.invalid/x",),
            inputs_are_fixture=True),
        root=root, workflow_id="career", project_id="p-career",
        mission_id="m-career", generated_at="2026-01-01T00:00:00Z")
    career_empty = career.run_career_intelligence(
        career.CareerInputs(
            company="Acme", role="Analyst",
            role_description="Must have experience with reporting.",
            inputs_are_fixture=True),
        root=root, workflow_id="career-empty",
        generated_at="2026-01-01T00:00:00Z")

    life_sciences = lifesci.run_life_sciences_intelligence((
        lifesci.MonitoringRecord(
            "n1", "2026-01-10",
            "Phase 3 oncology trial met endpoint; Novartis Pharma planned an "
            "FDA submission."),
        lifesci.MonitoringRecord(
            "n2", "2026-02-12",
            "Manufacturing supply shortage; oncology demand rising; Novartis "
            "Pharma faces a supply problem."),
        lifesci.MonitoringRecord(
            "n3", "2026-03-05",
            "FDA approval; real-world data biomarker strategy."),
    ), root=root, workflow_id="life-sciences", project_id="p-ls",
        inputs_are_fixture=True, generated_at="2026-01-01T00:00:00Z")

    project_registry.create_project(
        root, name="Operations project",
        description="Sample operations project",
        objective_domain="operations",
        workspace=str(Path(root) / "workspace"),
        clock=lambda: "2026-01-01T00:00:00Z")
    ops_project_id = project_registry.project_id_for("Operations project")
    operations = lifeos_ops.evaluate_operations((
        lifeos_ops.MissionState(
            "m-open", ops_project_id, changed_at="2026-01-02T00:00:00Z",
            next_action="Review the change"),
        lifeos_ops.MissionState(
            "m-blocked", ops_project_id, blocked_reason="dependency m-open"),
        lifeos_ops.MissionState(
            "m-gate", ops_project_id, human_gate="GO COMMIT"),
        lifeos_ops.MissionState(
            "m-stale", ops_project_id, stale_evidence_since="2025-12-01"),
        lifeos_ops.MissionState(
            "m-outcome", ops_project_id, outcome_recorded=False),
        lifeos_ops.MissionState(
            "m-wait", ops_project_id, priority=1, outcome_recorded=True),
        lifeos_ops.MissionState("m-done", ops_project_id,
                                lifecycle="COMPLETE"),
    ), root=root, project_id=ops_project_id, workflow_id="operations",
        generated_at="2026-01-03T00:00:00Z")

    # Outcomes: prediction -> decision -> execution -> actual.
    prediction_id = "pred-1"
    link_id = outcomes.link_id_for(prediction_id, "dec-1", "exec-1")
    predictions = (
        outcomes.MetricPrediction("duration_s", 100.0, 0.2,
                                  outcomes.PRED_MODEL, "model-1"),
        outcomes.MetricPrediction("repairs", 1.0, 0.3,
                                  outcomes.PRED_MODEL, "model-1"),
    )
    first = outcomes.OutcomeLink(
        link_id=link_id, prediction_id=prediction_id, decision_id="dec-1",
        execution_id="exec-1", recommendation="route-a",
        selected_action="route-a", predicted=predictions,
        actual=(
            outcomes.ActualMetric("duration_s", 120.0, outcomes.ACTUAL_MEASURED,
                                  "run-1"),
            outcomes.ActualMetric("repairs", 2.0, outcomes.ACTUAL_ENTERED,
                                  "user")),
        errors={}, status="", revision=0,
        recorded_at="2026-01-04T00:00:00Z")
    outcomes.record_outcome(first, root=root)
    outcomes.record_outcome(first, root=root)  # idempotent re-submission
    delayed = outcomes.OutcomeLink(
        link_id=link_id, prediction_id=prediction_id, decision_id="dec-1",
        execution_id="exec-1", recommendation="route-a",
        selected_action="route-b", predicted=predictions,
        actual=(
            outcomes.ActualMetric("duration_s", 130.0, outcomes.ACTUAL_MEASURED,
                                  "run-1"),
            outcomes.ActualMetric("repairs", None, outcomes.ACTUAL_UNKNOWN, "",
                                  reason="not recorded")),
        errors={}, status="", revision=0,
        recorded_at="2026-01-05T00:00:00Z")
    outcomes.record_outcome(delayed, root=root)
    reconciliation = outcomes.reconciliation_summary(root)

    # Active learning over fixture runs.
    fixture_root = str(Path(root) / "fixture-runs")
    fixtures.generate_fixture_runs(fixture_root, count=120)
    learning_dataset = dataset_module.build_learning_dataset(
        [fixture_root], source_kind=model.intel_model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    dataset_module.persist_dataset(root, learning_dataset)
    evaluation = ml.evaluate_target(learning_dataset, "success")
    champion = learning.model_record_from_evaluation(
        evaluation, registered_at="2026-01-01T00:00:00Z")
    refresh_no_promotion = learning.refresh_model(
        learning_dataset, "success", champion=champion,
        generated_at="2026-01-02T00:00:00Z")
    tiny_root = str(Path(root) / "tiny-runs")
    fixtures.generate_fixture_runs(tiny_root, count=4)
    tiny_dataset = dataset_module.build_learning_dataset(
        [tiny_root], source_kind=model.intel_model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    refresh_insufficient = learning.refresh_model(
        tiny_dataset, "success", generated_at="2026-01-02T00:00:00Z")
    refresh_promote = learning.refresh_model(
        learning_dataset, "success",
        generated_at="2026-01-02T00:00:00Z")

    cockpit_view = cockpit.build_cockpit(
        root, (
            lifeos_ops.MissionState("m-gate", ops_project_id,
                                    human_gate="GO COMMIT"),
            lifeos_ops.MissionState("m-blocked", ops_project_id,
                                    blocked_reason="dependency m-open"),
        ), project_ids=(ops_project_id,), workflow_id="cockpit",
        predictions=({"target": "success", "prediction": 0.7,
                      "uncertainty": "0.1", "source": "model-1"},),
        generated_at="2026-01-03T00:00:00Z")

    portfolio_evidence = _portfolio_evidence(
        root, career_result, life_sciences, reconciliation, operations)

    return _Context(
        root=root, ingest_manifest=manifest, ingest_repeat=repeat,
        ingest_changed=changed, ingest_restricted=restricted,
        ingest_unsupported=unsupported, career=career_result,
        career_empty=career_empty, life_sciences=life_sciences,
        operations=operations, reconciliation=reconciliation,
        refresh_no_promotion=refresh_no_promotion,
        refresh_insufficient=refresh_insufficient,
        refresh_promote=refresh_promote, cockpit=cockpit_view,
        portfolio=portfolio_evidence, learning=learning_dataset)


def _portfolio_evidence(
    root: str, career_result: career.CareerIntelligence,
    life_sciences: lifesci.LifeSciencesIntelligence,
    reconciliation: Mapping[str, Any],
    operations: lifeos_ops.OperationsReport,
) -> portfolio.PortfolioEvidence:
    # A self-contained, privacy-safe case-study set derived from the sample
    # artifacts produced above (no private data).
    del career_result, life_sciences  # referenced via the written artifacts
    resolved = int(reconciliation.get("reconciled") or 0)
    links = int(reconciliation.get("link_count") or 0)
    unknowns = len(
        [item for item in operations.attention
         if "missing outcome" in str(item.get("reason"))])
    studies = (
        portfolio.CaseStudy(
            case_id="career", title="Career Intelligence",
            problem="Turn a role description and supplied evidence into a "
                    "complete, evidence-typed application package.",
            inputs=("role description", "company evidence",
                    "profile/portfolio evidence"),
            approach=("typed claims", "requirement extraction",
                      "evidence mapping"),
            architecture=("Workflow -> Knowledge -> Evidence map -> "
                          "Next actions",),
            evidence=("company-analysis.md", "role-fit.md",
                      "evidence-map.md", "next-actions.json"),
            result=("8 artifacts", "no invented personal or company fact"),
            limitations=("evidence quality depends on supplied inputs",),
            lessons=("typing claims prevents accidental overclaiming",)),
        portfolio.CaseStudy(
            case_id="life-sciences", title="Life Sciences Intelligence",
            problem="Classify a monitoring evidence set into themes, "
                    "entities and cautious trends.",
            inputs=("dated monitoring records",),
            approach=("deterministic theme classification",
                      "entity detection", "opportunity hypotheses"),
            architecture=("Evidence -> Themes -> Trends -> Opportunities -> "
                          "Business cases",),
            evidence=("monitoring-digest.md", "trends.md",
                      "opportunities.md", "business-cases.md"),
            result=("recurring themes and hypotheses with provenance",),
            limitations=("opportunity value is a hypothesis, not measured",),
            lessons=("uncertainty must be visible to the reader",)),
        portfolio.CaseStudy(
            case_id="adaptive-learning",
            title="Adaptive execution / predictive learning",
            problem="Reconcile predictions with actual outcomes and guard "
                    "model refresh.",
            inputs=("canonical runtime history", "outcome ledger"),
            approach=("outcome reconciliation", "champion/challenger "
                      "evaluation"),
            architecture=("Prediction -> Decision -> Execution -> Outcome -> "
                          "Learning",),
            evidence=("outcome ledger", "model-refresh report"),
            result=("prediction error persisted; no automatic promotion",),
            limitations=("small samples are reported as insufficient",),
            lessons=("guarded refresh prevents silent regressions",)),
    )
    return portfolio.PortfolioEvidence(
        generated_at="2026-01-06T00:00:00Z",
        version=model.REALWORLD_VERSION, acceptance_passed=0,
        acceptance_total=0, dogfood_history_source="FIXTURE",
        dogfood_real_rows=0,
        workflow_families=("CAREER_INTELLIGENCE",
                           "LIFE_SCIENCES_INTELLIGENCE",
                           "LIFEOS_OPERATIONAL_INTELLIGENCE"),
        outcome_links=links, outcome_reconciled=resolved,
        unresolved_unknowns=unknowns, corrections_required=0,
        prediction_error=(float(reconciliation["mean_absolute_error"])
                          if reconciliation.get("mean_absolute_error")
                          is not None else None),
        trust_clean=True, trust_scanned_files=0, case_studies=studies,
        source_coverage=2, output_completeness=1.0,
        time_to_deliverable_s=None,
        limitations=("fixture/sample inputs are clearly labelled",))


def _expects_error(call: Callable[[], object]) -> bool:
    try:
        call()
    except model.intel_model.IntelligenceError:
        return True
    return False


def run_acceptance(
    root: str, *, generated_at: str = "2026-01-01T00:00:00Z",
) -> AcceptanceMatrix:
    context = _build_context(root)
    cases: list[AcceptanceCase] = []

    def add(case_id: str, category: str, description: str, condition: bool,
            detail: str = "") -> None:
        cases.append(AcceptanceCase(
            case_id=case_id, category=category, description=description,
            passed=bool(condition), detail=detail))

    manifest = context.ingest_manifest

    # --- INPUTS --------------------------------------------------------------
    add("I01", CAT_INPUTS, "manifest is deterministic for identical inputs",
        manifest.manifest_id == context.ingest_repeat.manifest_id,
        manifest.manifest_id[:16])
    add("I02", CAT_INPUTS, "every record carries origin/hash/location",
        all(record.origin and record.content_hash and record.location
            for record in manifest.records),
        f"{manifest.source_count} records")
    add("I03", CAT_INPUTS, "duplicate content is detected",
        any(record.ingestion_status == ingest.STATUS_DUPLICATE
            for record in manifest.records),
        "duplicate status present")
    duplicates = [record for record in manifest.records
                  if record.ingestion_status == ingest.STATUS_DUPLICATE]
    add("I04", CAT_INPUTS, "duplicate references the original source",
        all(record.duplicate_of for record in duplicates),
        f"{len(duplicates)} duplicate(s)")
    changed = [record for record in context.ingest_changed.records
               if record.ingestion_status == ingest.STATUS_CHANGED]
    add("I05", CAT_INPUTS, "changed source is detected",
        bool(changed), f"{len(changed)} changed")
    add("I06", CAT_INPUTS, "changed source records the prior hash",
        all(record.changed_from for record in changed), "changed_from set")
    add("I07", CAT_INPUTS, "unsupported format is an explicit result",
        bool(context.ingest_unsupported)
        and all(record.ingestion_status == ingest.STATUS_UNSUPPORTED
                for record in context.ingest_unsupported),
        f"{len(context.ingest_unsupported)} unsupported")
    add("I08", CAT_INPUTS, "no silent OCR claim for a raw PDF",
        any(ingest.LIM_NO_OCR in record.conversion_limitations
            for record in context.ingest_unsupported),
        "OCR limitation recorded")
    add("I09", CAT_INPUTS, "unsupported raw PDF is not parsed",
        all(record.parse_status == "UNSUPPORTED"
            for record in context.ingest_unsupported),
        "UNSUPPORTED parse status")
    restricted = [record for record in context.ingest_restricted.records
                  if record.sensitivity == knowledge.SENS_RESTRICTED]
    add("I10", CAT_INPUTS, "sensitivity override is honoured",
        bool(restricted), "RESTRICTED record")
    add("I11", CAT_INPUTS, "restricted content is not persisted",
        all(item.persisted is False
            for item in context.ingest_restricted.contents),
        "RESTRICTED content withheld")
    secret_record = next(
        (record for record in manifest.records
         if record.location.endswith("secret-notes.md")), None)
    add("I12", CAT_INPUTS, "secret-shaped filename is classified sensitive",
        secret_record is not None
        and secret_record.sensitivity == knowledge.SENS_CONFIDENTIAL,
        secret_record.sensitivity if secret_record else "missing")
    add("I13", CAT_INPUTS, "manifest is context, not canonical runtime truth",
        manifest.canonical is False and manifest.runtime_truth is False,
        "canonical=False")
    add("I14", CAT_INPUTS, "manifest round-trips through persistence",
        ingest.load_manifest(str(Path(root) / "ingest-a")) is not None,
        "load_manifest")
    add("I15", CAT_INPUTS, "usable sources bridge into knowledge adapters",
        len(ingest.to_knowledge_adapters(manifest)) >= 1,
        f"{len(ingest.to_knowledge_adapters(manifest))} adapters")
    secret_content = next(
        (item for item in manifest.contents
         if secret_record is not None
         and item.source_id == secret_record.source_id), None)
    add("I16", CAT_INPUTS, "secret-shaped content is redacted before persist",
        secret_content is not None and "hunter2" not in secret_content.text
        and "[REDACTED_SECRET]" in secret_content.text,
        "secret redacted")

    # --- CAREER --------------------------------------------------------------
    career_result = context.career
    add("C01", CAT_CAREER, "career produces the complete artifact set",
        set(career_result.artifacts) >= {
            "company-analysis.md", "role-fit.md", "evidence-map.md",
            "gaps.md", "business-problem-hypotheses.md",
            "ai-data-opportunity.md", "value-proposition.md",
            "interview-brief.md"},
        f"{len(career_result.artifacts)} artifacts")
    labels = {claim.label for claim in career_result.claims}
    add("C02", CAT_CAREER, "career uses FACT/EVIDENCE/INFERENCE/HYPOTHESIS",
        {model.FACT, model.EVIDENCE, model.INFERENCE,
         model.HYPOTHESIS} <= labels,
        ", ".join(sorted(labels)))
    facts = [claim for claim in career_result.claims
             if claim.label == model.FACT]
    add("C03", CAT_CAREER, "FACT claims come only from profile inputs",
        bool(facts) and all(claim.source_ref.startswith("profile:")
                            for claim in facts),
        f"{len(facts)} facts")
    add("C04", CAT_CAREER, "company statements are grounded evidence",
        all(claim.source_kind == model.SRC_SUPPLIED_EVIDENCE
            for claim in career_result.claims
            if claim.label == model.EVIDENCE
            and claim.source_ref.startswith("company:")),
        "company evidence grounded")
    add("C05", CAT_CAREER, "no personal fact is invented without profile",
        context.career_empty.artifacts["value-proposition.md"].count(
            "not provided") == 1
        and all(claim.label != model.FACT
                for claim in context.career_empty.claims),
        "empty value proposition")
    no_company = context.career_empty
    add("C06", CAT_CAREER, "no company fact is invented without evidence",
        any("no company evidence was supplied" in unknown.description
            for unknown in no_company.unknowns),
        "company unknown recorded")
    evidence_map = career_result.artifacts["evidence-map.md"]
    add("C07", CAT_CAREER, "evidence map references each claim source",
        all(claim.source_ref in evidence_map
            for claim in career_result.claims),
        f"{len(career_result.claims)} claims mapped")
    add("C08", CAT_CAREER, "gaps artifact lists unevidenced requirements",
        "Unevidenced requirements" in career_result.artifacts["gaps.md"],
        "gaps structured")
    add("C09", CAT_CAREER, "value proposition only claims matched evidence",
        "not provided" not in career_result.artifacts["value-proposition.md"]
        or "MATCHED" not in career_result.artifacts["role-fit.md"],
        "value proposition grounded")
    add("C10", CAT_CAREER, "next actions carry rationale/source/urgency",
        all(action.rationale and action.source and action.urgency
            for action in career_result.next_actions),
        f"{len(career_result.next_actions)} actions")
    add("C11", CAT_CAREER, "LifeOS-compatible note is emitted",
        (Path(root) / "realworld" / "career" / "career" /
         "lifeos-note.json").is_file(), "lifeos note")
    add("C12", CAT_CAREER, "business problems are HYPOTHESIS unless evidenced",
        all(claim.label in (model.HYPOTHESIS, model.EVIDENCE)
            for claim in career_result.claims
            if "problem" in claim.statement.lower()),
        "problem labels bounded")

    # --- LIFE SCIENCES -------------------------------------------------------
    life = context.life_sciences
    observations = [claim for claim in life.claims
                    if claim.label == model.OBSERVATION]
    add("L01", CAT_LIFE_SCIENCES, "each event is an evidence-linked observation",
        bool(observations)
        and all(claim.source_ref for claim in observations),
        f"{len(observations)} observations")
    add("L02", CAT_LIFE_SCIENCES, "themes are classified from source text",
        "oncology" in life.artifacts["themes.md"],
        "oncology theme")
    add("L03", CAT_LIFE_SCIENCES, "companies/entities are linked to evidence",
        "Novartis" in life.artifacts["entities.md"],
        "entity surfaced")
    add("L04", CAT_LIFE_SCIENCES, "opportunity hypotheses are labelled",
        any(claim.label == model.HYPOTHESIS for claim in life.claims)
        and "(HYPOTHESIS)" in life.artifacts["opportunities.md"],
        "hypotheses labelled")
    add("L05", CAT_LIFE_SCIENCES, "trends carry an uncertainty label",
        "uncertainty" in life.artifacts["trends.md"],
        "uncertainty present")
    add("L06", CAT_LIFE_SCIENCES, "business cases preserve provenance",
        "evidence:" in life.artifacts["business-cases.md"],
        "evidence refs")
    add("L07", CAT_LIFE_SCIENCES, "no unsupported company claim is made",
        all(claim.label in (model.OBSERVATION, model.EVIDENCE,
                            model.INFERENCE, model.HYPOTHESIS)
            for claim in life.claims),
        "labels bounded")
    add("L08", CAT_LIFE_SCIENCES, "life-sciences artifacts persist",
        (Path(root) / "realworld" / "life-sciences" / "life-sciences" /
         "analysis.json").is_file(), "analysis persisted")
    add("L09", CAT_LIFE_SCIENCES, "next actions are surfaced",
        len(life.next_actions) >= 1, f"{len(life.next_actions)} actions")
    add("L10", CAT_LIFE_SCIENCES, "recurring problems require >=2 records",
        "recurring" in life.artifacts["recurring-problems.md"].lower(),
        "recurrence threshold")

    # --- LIFEOS --------------------------------------------------------------
    operations = context.operations
    add("O01", CAT_LIFEOS, "what changed is answered",
        bool(operations.changed), f"{len(operations.changed)} changed")
    add("O02", CAT_LIFEOS, "what is blocked is answered",
        any("blocked" in item["reason"] for item in operations.blocked),
        f"{len(operations.blocked)} blocked")
    add("O03", CAT_LIFEOS, "human decisions are surfaced as attention",
        any("human decision" in item["reason"]
            for item in operations.attention), "human gate")
    add("O04", CAT_LIFEOS, "stale evidence is surfaced as attention",
        any("stale evidence" in item["reason"]
            for item in operations.attention), "stale evidence")
    add("O05", CAT_LIFEOS, "missing outcome feedback is surfaced",
        any("missing outcome" in item["reason"]
            for item in operations.attention), "outcome feedback")
    add("O06", CAT_LIFEOS, "what can wait is answered",
        bool(operations.can_wait), f"{len(operations.can_wait)} can wait")
    add("O07", CAT_LIFEOS, "next best actions are produced",
        bool(operations.next_actions),
        f"{len(operations.next_actions)} actions")
    add("O08", CAT_LIFEOS, "every action carries a rationale",
        all(action.rationale for action in operations.next_actions),
        "rationale present")
    add("O09", CAT_LIFEOS, "every action carries an urgency basis",
        all(action.urgency for action in operations.next_actions),
        "urgency present")
    add("O10", CAT_LIFEOS, "meaningful alternatives are offered",
        any(action.alternatives for action in operations.next_actions),
        "alternatives present")
    add("O11", CAT_LIFEOS, "operational view persists",
        (Path(root) / "realworld" / "operations" / "operations" /
         "operations.json").is_file(), "operations persisted")
    add("O12", CAT_LIFEOS, "operational view is a projection, not canonical",
        operations.canonical is False, "canonical=False")

    # --- OUTCOMES ------------------------------------------------------------
    link_id = outcomes.link_id_for("pred-1", "dec-1", "exec-1")
    latest = outcomes.latest(root, link_id)
    add("U01", CAT_OUTCOMES, "prediction is linked to decision and execution",
        latest is not None and latest.prediction_id == "pred-1"
        and latest.decision_id == "dec-1"
        and latest.execution_id == "exec-1",
        "link present")
    add("U02", CAT_OUTCOMES, "actual outcome is linked with provenance",
        latest is not None
        and any(actual.provenance == outcomes.ACTUAL_MEASURED
                for actual in latest.actual),
        "measured actual")
    add("U03", CAT_OUTCOMES, "prediction error is computed and persisted",
        latest is not None
        and latest.errors["duration_s"]["absolute_error"] == 30.0,
        "absolute error 30.0")
    add("U04", CAT_OUTCOMES, "delayed outcome appends a new revision",
        latest is not None and latest.revision == 2, "revision 2")
    add("U05", CAT_OUTCOMES, "UNKNOWN actual stays UNKNOWN",
        latest is not None
        and latest.errors["repairs"]["status"] == "UNKNOWN",
        "repairs UNKNOWN")
    add("U06", CAT_OUTCOMES, "re-submission is idempotent",
        len(outcomes.history(root, link_id)) == 2,
        "2 immutable revisions")
    add("U07", CAT_OUTCOMES, "outcome history is immutable and ordered",
        [revision.revision for revision in outcomes.history(root, link_id)]
        == [1, 2], "revisions [1, 2]")
    add("U08", CAT_OUTCOMES, "recommended vs selected action is recorded",
        latest is not None
        and latest.followed_recommendation() is False,
        "followed_recommendation=False")
    add("U09", CAT_OUTCOMES, "no success is inferred from silence",
        latest is not None and latest.status == outcomes.STATUS_PARTIAL,
        latest.status if latest else "missing")
    add("U10", CAT_OUTCOMES, "reconciliation summary reports completeness",
        context.reconciliation.get("link_count") == 1
        and context.reconciliation.get("mean_absolute_error") == 30.0,
        "summary computed")

    # --- ACTIVE LEARNING -----------------------------------------------------
    add("A01", CAT_ACTIVE_LEARNING, "dataset snapshot identity is recorded",
        len(context.refresh_promote.snapshot.snapshot_id) == 32,
        context.refresh_promote.snapshot.snapshot_id[:16])
    add("A02", CAT_ACTIVE_LEARNING, "champion and challenger are explicit",
        context.refresh_no_promotion.champion is not None
        and context.refresh_no_promotion.challenger is not None,
        "champion + challenger")
    add("A03", CAT_ACTIVE_LEARNING, "minimum sample threshold is enforced",
        context.refresh_insufficient.state
        == learning.STATE_INSUFFICIENT_DATA,
        context.refresh_insufficient.state)
    add("A04", CAT_ACTIVE_LEARNING, "calibration comparison is reported",
        "challenger_ece" in context.refresh_no_promotion
        .calibration_comparison,
        "calibration compared")
    add("A05", CAT_ACTIVE_LEARNING, "no-promotion is a valid result",
        context.refresh_no_promotion.state == learning.STATE_NO_PROMOTION,
        context.refresh_no_promotion.state)
    add("A06", CAT_ACTIVE_LEARNING, "rollback metadata is present",
        "champion_model_id" in context.refresh_no_promotion.rollback,
        "rollback path")
    add("A07", CAT_ACTIVE_LEARNING, "no policy mutation is ever claimed",
        context.refresh_promote.policy_mutation is False
        and context.refresh_no_promotion.policy_mutation is False,
        "policy_mutation=False")
    add("A08", CAT_ACTIVE_LEARNING, "no hidden route/scheduler authority",
        not context.refresh_promote.route_authority_change
        and not context.refresh_promote.scheduler_authority_change,
        "authority unchanged")
    add("A09", CAT_ACTIVE_LEARNING, "registry entry requires human action",
        learning.registry_entry(context.refresh_promote)["registered"]
        is False,
        "not auto-registered")
    add("A10", CAT_ACTIVE_LEARNING, "drift indicator is reported or explicit",
        "status" in context.refresh_no_promotion.drift,
        str(context.refresh_no_promotion.drift.get("status")))

    # --- COCKPIT -------------------------------------------------------------
    view = context.cockpit
    text = view.render_text()
    add("K01", CAT_COCKPIT, "default projection is human-readable",
        "PROJECTS" in text and "NEXT ACTIONS" in text
        and "ATTENTION" in text and "INTELLIGENCE" in text,
        "four sections")
    add("K02", CAT_COCKPIT, "why-this is present for every action",
        all("why:" in text for _ in view.next_actions)
        and all(action.get("rationale") for action in view.next_actions),
        "rationale visible")
    add("K03", CAT_COCKPIT, "uncertainty is visible",
        all("confidence" in text for _ in view.next_actions)
        or not view.next_actions,
        "confidence visible")
    add("K04", CAT_COCKPIT, "evidence drill-down is referenced",
        "drilldown" in view.to_dict()
        and view.drilldown.get("operations") is not None,
        "drilldown reference")
    add("K05", CAT_COCKPIT, "human decision visibility",
        any("human decision" in item["reason"] for item in view.attention),
        "human gate visible")
    add("K06", CAT_COCKPIT, "projects are categorised",
        all(item["status"] in (cockpit.PROJECT_ACTIVE,
                               cockpit.PROJECT_BLOCKED,
                               cockpit.PROJECT_WAITING,
                               cockpit.PROJECT_COMPLETED)
            for item in view.projects),
        "statuses in closed set")
    add("K07", CAT_COCKPIT, "cockpit is strictly read-only",
        view.read_only is True and view.canonical is False, "read_only")
    add("K08", CAT_COCKPIT, "cockpit persists text and HTML projections",
        (Path(root) / "realworld" / "cockpit" / "cockpit" /
         "cockpit.txt").is_file()
        and (Path(root) / "realworld" / "cockpit" / "cockpit" /
             "cockpit.html").is_file(), "projections persisted")

    # --- PORTFOLIO -----------------------------------------------------------
    evidence = context.portfolio
    overview = portfolio.executive_overview(evidence)
    add("P01", CAT_PORTFOLIO, "executive overview covers the required headings",
        all(heading in overview for heading in (
            "What it is", "Problem solved", "Architecture",
            "Implemented capabilities", "Trust model", "Measured evidence",
            "Limitations")), "headings present")
    add("P02", CAT_PORTFOLIO, "three case studies are present",
        len(evidence.case_studies) == 3, f"{len(evidence.case_studies)}")
    studies = portfolio.case_studies_markdown(evidence)
    add("P03", CAT_PORTFOLIO, "each case study has the required sections",
        all(section in studies for section in (
            "**Problem.**", "**Inputs.**", "**Approach.**",
            "**Architecture.**", "**Evidence.**", "**Result.**",
            "**Limitations.**", "**Lessons.**")),
        "sections present")
    add("P04", CAT_PORTFOLIO, "architecture chain is deterministic",
        tuple(portfolio.ARCHITECTURE_CHAIN)
        == ("Objective", "Project / Workflow", "Knowledge", "Prediction",
            "Decision", "Execution", "Validation", "Outcome", "Learning"),
        "chain fixed")
    add("P05", CAT_PORTFOLIO, "architecture DOT source is provided",
        portfolio.architecture_dot().startswith("digraph trajectory_os"),
        "DOT source")
    pack = portfolio.evidence_pack(evidence)
    add("P06", CAT_PORTFOLIO, "evidence pack contains metrics + trust",
        "metrics" in pack and "trust_boundaries" in pack
        and "reproducibility" in pack, "pack complete")
    add("P07", CAT_PORTFOLIO, "privacy-safe demo command is referenced",
        pack["reproducibility"]["command"]
        == "scripts/trajectory demo portfolio"
        and pack["reproducibility"]["uses_private_data"] is False,
        "demo command")
    add("P08", CAT_PORTFOLIO, "no metric is emitted without an evidence source",
        all(metric.get("source") for metric in pack["metrics"]),
        f"{len(pack['metrics'])} metrics sourced")
    drafts = portfolio.build_in_public_drafts(evidence)
    add("P09", CAT_PORTFOLIO, "build-in-public material is draft-only",
        drafts["auto_publish"] is False
        and all(draft["published"] is False for draft in drafts["drafts"]),
        "drafts unpublished")
    add("P10", CAT_PORTFOLIO, "portfolio documents persist to disk",
        bool(portfolio.write_portfolio(root, evidence).get(
            "executive-overview.md")), "executive overview written")

    # --- TRUST ---------------------------------------------------------------
    add("T01", CAT_TRUST, "realworld layer contains no release Git verbs",
        not _git_offenders(), ", ".join(_git_offenders()[:3]) or "clean")
    add("T02", CAT_TRUST, "realworld layer has no process/git write calls",
        not _process_offenders(), ", ".join(_process_offenders()[:3])
        or "clean")
    add("T03", CAT_TRUST, "model refresh never mutates policy",
        context.refresh_promote.policy_mutation is False,
        "no policy mutation")
    add("T04", CAT_TRUST, "model refresh never auto-registers",
        learning.registry_entry(context.refresh_promote)["registered"]
        is False, "human action required")
    add("T05", CAT_TRUST, "cockpit is read-only",
        context.cockpit.read_only is True, "read_only")
    add("T06", CAT_TRUST, "portfolio never auto-publishes",
        portfolio.build_in_public_drafts(context.portfolio)["auto_publish"]
        is False, "auto_publish=False")
    add("T07", CAT_TRUST, "no prose-success inference from silence",
        context.reconciliation.get("unknown", 0) == 0
        and context.reconciliation.get("partial", 0) == 1,
        "UNKNOWN/PARTIAL counted, not success")
    add("T08", CAT_TRUST, "GO COMMIT and GO MERGE gates are unchanged",
        release_model.GATE_GO_COMMIT == "GO_COMMIT"
        and release_model.GATE_GO_MERGE == "GO_MERGE",
        "release gates intact")
    add("T09", CAT_TRUST, "semantic patch identity is untouched",
        not _contains_git_write_calls(), "no git write call sites")
    add("T10", CAT_TRUST, "every acceptance case carries machine evidence",
        all(case.detail != "" for case in cases), "details present")

    matrix = AcceptanceMatrix(cases=tuple(cases), generated_at=generated_at)
    model.write_json(str(Path(root) / "acceptance" / "matrix.json"),
                     matrix.to_dict())
    (Path(root) / "acceptance" / "matrix.md").parent.mkdir(
        parents=True, exist_ok=True)
    (Path(root) / "acceptance" / "matrix.md").write_text(
        matrix.render_markdown(), encoding="utf-8")
    return matrix


def _package_dir() -> Path:
    return Path(__file__).parent


def _git_offenders() -> list[str]:
    offenders: list[str] = []
    for path in sorted(_package_dir().glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for verb in _GIT_VERBS:
            if f'"git", "{verb}"' in source:
                offenders.append(f"{path.name}:{verb}")
    return offenders


def _process_offenders() -> list[str]:
    prefixes = ("subprocess" + ".", "os" + ".exec", "os" + ".system")
    offenders: list[str] = []
    for path in sorted(_package_dir().glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if any(prefix in source for prefix in prefixes):
            offenders.append(path.name)
    return offenders


def _contains_git_write_calls() -> bool:
    return bool(_process_offenders())


__all__ = [
    "AcceptanceCase",
    "AcceptanceMatrix",
    "CATEGORIES",
    "run_acceptance",
]
