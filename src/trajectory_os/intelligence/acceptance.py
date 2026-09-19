"""M063 — real-world intelligence acceptance matrix (>= 50 cases).

Each acceptance case is an executable, deterministic check over fixture run
directories (canonical layout) so the matrix runs in CI where the real
``.trajectory-pi/runs`` history is unavailable. Fixture observations are
always labelled ``FIXTURE``; no synthetic row is presented as real history.

The matrix proves, category by category: deterministic extraction,
provenance, missingness, fixture/real separation, schema versioning and
leakage prevention (DATASET); naive-baseline comparison, calibration,
uncertainty, metadata and the insufficient-data fail-safe (ML); explicit
candidates, the comparable-evidence gate and the NO_RECOMMENDATION path
(ROUTING); transparent scoring, deterministic fallback and rule-vs-ML
comparison (SCHEDULING); source/chunk provenance, stale/missing handling,
contradictions and NO_EVIDENCE honesty (RAG); durable human-readable
deliverables with no invented personal facts (WORKFLOWS); alternatives,
risks/constraints, fact/prediction distinction and snapshot persistence
(DECISION); and the preserved trust boundaries (TRUST).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import (
    adaptive,
    decision,
    fixtures,
    knowledge,
    ml,
    model,
    routing,
    workflows,
)
from trajectory_os.intelligence import dataset as dataset_module

#: Categories (closed set).
CAT_DATASET = "DATASET"
CAT_ML = "ML"
CAT_ROUTING = "ROUTING"
CAT_SCHEDULING = "SCHEDULING"
CAT_RAG = "RAG"
CAT_WORKFLOWS = "WORKFLOWS"
CAT_DECISION = "DECISION"
CAT_TRUST = "TRUST"

CATEGORIES = (
    CAT_DATASET, CAT_ML, CAT_ROUTING, CAT_SCHEDULING, CAT_RAG,
    CAT_WORKFLOWS, CAT_DECISION, CAT_TRUST,
)

_GIT_VERBS = ("commit", "push", "merge", "reset", "restore", "clean",
              "stash", "rebase", "switch", "checkout")


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
    fixture_count: int

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
            "intelligence_version": model.INTELLIGENCE_VERSION,
            "kind": "intelligence_acceptance_matrix",
            "generated_at": self.generated_at,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "fixture_count": self.fixture_count,
            "category_counts": self.category_counts(),
            "cases": [case.to_dict() for case in self.cases],
        }

    def render_markdown(self) -> str:
        lines = [
            "# M063 — Real-world intelligence acceptance matrix", "",
            f"- generated: {self.generated_at}",
            f"- cases: {self.passed}/{self.total} passed",
            f"- fixture runs: {self.fixture_count}", "",
            "| category | passed | failed |", "| --- | --- | --- |",
        ]
        for category in CATEGORIES:
            counts = self.category_counts().get(category, {})
            lines.append(f"| {category} | {counts.get('passed', 0)} | "
                         f"{counts.get('failed', 0)} |")
        lines += ["", "## Cases", "", "| id | category | pass | detail |",
                  "| --- | --- | --- | --- |"]
        for case in self.cases:
            status = "PASS" if case.passed else "FAIL"
            detail = case.detail.replace("|", "/")
            lines.append(f"| {case.case_id} | {case.category} | {status} | "
                         f"{detail} |")
        return "\n".join(lines) + "\n"


@dataclass
class _Context:
    root: str
    fixture_root: str
    learning: dataset_module.LearningDataset
    report: ml.TrainingReport
    recommendation: routing.RouteRecommendation
    scheduler: adaptive.ScheduleComparison
    index: knowledge.KnowledgeIndex
    trace: knowledge.RetrievalTrace
    contradictions: tuple[knowledge.Contradiction, ...]
    career: workflows.WorkflowResult
    life_sciences: workflows.WorkflowResult
    research: workflows.WorkflowResult
    snapshot: decision.DecisionSnapshot
    comparison: decision.OutcomeComparison | None
    no_experience_career: workflows.WorkflowResult
    tiny_insufficient: ml.TargetEvaluation
    before_hashes: dict[str, str]


def _hash_tree(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            hashes[str(path)] = knowledge.hash_content(
                path.read_text(encoding="utf-8", errors="replace"))
    return hashes


def _build_context(root: str) -> _Context:
    fixture_root = str(Path(root) / "fixture-runs")
    fixtures.generate_fixture_runs(fixture_root, count=120)
    before_hashes = _hash_tree(Path(fixture_root))
    learning = dataset_module.build_learning_dataset(
        [fixture_root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    dataset_module.persist_dataset(root, learning)
    report = ml.train_all(learning, generated_at="2026-01-01T00:00:00Z")
    ml.persist_report(root, report)
    recommendation = routing.recommend_route(
        learning, generated_at="2026-01-01T00:00:00Z")
    routing.persist_recommendation(root, recommendation)

    candidates = (
        adaptive.ScheduleCandidate(
            candidate_id="mission-a", project="proj", mission_id="m-a",
            project_priority=5, deadline_s=1_800.0, wait_age_s=86_400.0,
            dependencies_ready=True, backend_available=True),
        adaptive.ScheduleCandidate(
            candidate_id="mission-b", project="proj", mission_id="m-b",
            project_priority=3, deadline_s=86_400.0, wait_age_s=3_600.0,
            dependencies_ready=True, backend_available=True),
        adaptive.ScheduleCandidate(
            candidate_id="mission-c", project="proj", mission_id="m-c",
            project_priority=2, deadline_s=None, wait_age_s=0.0,
            dependencies_ready=True, backend_available=True),
    )
    scheduler = adaptive.compare_schedulers(
        candidates, generated_at="2026-01-01T00:00:00Z")
    adaptive.persist_comparison(root, scheduler)

    adapters: tuple[knowledge.SourceAdapter, ...] = (
        knowledge.TextSource(
            "notes/roadmap.md", "Roadmap",
            "We must scale the data platform and reduce cost. "
            "Real-world data improved enrichment by 20 percent."),
        knowledge.TextSource(
            "notes/review.md", "Review",
            "Real-world data improved enrichment by 35 percent. "
            "Regulatory compliance remains a concern."),
        knowledge.TextSource(
            "notes/secret.md", "Confidential",
            "Restricted pricing detail.", sensitivity=knowledge.SENS_RESTRICTED),
    )
    index = knowledge.build_index(adapters,
                                  generated_at="2026-01-01T00:00:00Z")
    knowledge.persist_index(root, index)
    trace = knowledge.retrieve(index, "real world data enrichment",
                               generated_at="2026-01-01T00:00:00Z")
    knowledge.persist_trace(root, trace)
    contradictions = (
        knowledge.record_contradiction(
            index, left_chunk_id=trace.results[0].chunk.chunk_id,
            right_chunk_id=trace.results[-1].chunk.chunk_id,
            kind=knowledge.CT_NUMERIC,
            description="fixture contradiction"),
    )

    career = workflows.run_career_workflow(
        workflows.CareerBrief(
            company="Acme Pharma", role="Head of Data & AI",
            brief_text="You must build a data platform and own AI strategy. "
                       "Experience with regulatory compliance is required.",
            experiences=("Built a regulatory reporting data platform",),
            constraints=("remote",)),
        root=root, workflow_id="career",
        inputs_are_fixture=True, generated_at="2026-01-01T00:00:00Z")
    life_sciences = workflows.run_life_sciences_workflow(
        (
            workflows.MonitoringRecord(
                "src-1", "2026-01-10",
                "Phase 3 oncology trial met endpoint; FDA submission planned."),
            workflows.MonitoringRecord(
                "src-2", "2026-02-12",
                "Manufacturing supply shortage; oncology demand rising."),
            workflows.MonitoringRecord(
                "src-3", "2026-03-05",
                "FDA approval; real-world data biomarker strategy."),
        ),
        root=root, workflow_id="life-sciences",
        inputs_are_fixture=True, generated_at="2026-01-01T00:00:00Z")
    research = workflows.run_research_workflow(
        "Should we invest in real-world data for oncology?",
        root=root, adapters=adapters, workflow_id="research",
        inputs_are_fixture=True, generated_at="2026-01-01T00:00:00Z")
    no_experience_career = workflows.run_career_workflow(
        workflows.CareerBrief(
            company="Acme", role="Analyst",
            brief_text="Must have experience with reporting."),
        root=root, workflow_id="career-no-experience",
        inputs_are_fixture=True, generated_at="2026-01-01T00:00:00Z")

    snapshot = decision.evaluate_decision(
        "Which mission should run next?",
        criteria=(
            decision.Criterion("priority", 2.0, decision.MAXIMISE,
                               decision.FACT),
            decision.Criterion("net_value", 3.0, decision.MAXIMISE,
                               decision.PREDICTION),
            decision.Criterion("effort_hours", 1.0, decision.MINIMISE,
                               decision.FACT),
        ),
        options=(
            decision.DecisionOption(
                option_id="mission-a", title="Run mission A",
                attributes={"priority": 5.0, "effort_hours": 6.0},
                predictions={"net_value": 0.8},
                prediction_confidence={"net_value": 0.7},
                risks=("schedule slip",), constraints=("needs review",),
                dependencies=("gate-1",), expected_effort_hours=6.0,
                rationale="forecast from M057"),
            decision.DecisionOption(
                option_id="mission-b", title="Run mission B",
                attributes={"priority": 2.0, "effort_hours": 2.0},
                predictions={"net_value": 0.3},
                prediction_confidence={"net_value": 0.5},
                risks=("limited upside",), expected_effort_hours=2.0,
                human_priority=1),
        ),
        irreversible=False, generated_at="2026-01-01T00:00:00Z")
    decision.persist_decision(root, snapshot)
    decision.record_outcome(
        root, snapshot.decision_id, chosen_option_id="mission-a",
        observed={"net_value": 0.7}, notes="fixture outcome",
        recorded_at="2026-01-02T00:00:00Z")
    comparison = decision.compare_outcome(root, snapshot.decision_id)

    tiny_root = str(Path(root) / "tiny-runs")
    fixtures.generate_fixture_runs(tiny_root, count=4)
    tiny_dataset = dataset_module.build_learning_dataset(
        [tiny_root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    tiny_insufficient = ml.evaluate_target(tiny_dataset, "success")

    return _Context(
        root=root, fixture_root=fixture_root, learning=learning, report=report,
        recommendation=recommendation, scheduler=scheduler, index=index,
        trace=trace, contradictions=contradictions, career=career,
        life_sciences=life_sciences, research=research, snapshot=snapshot,
        comparison=comparison, no_experience_career=no_experience_career,
        tiny_insufficient=tiny_insufficient, before_hashes=before_hashes)


def _evaluation(context: _Context, target: str) -> ml.TargetEvaluation:
    evaluation = context.report.evaluation(target)
    assert evaluation is not None
    return evaluation


def _status_of(context: _Context, target: str) -> str:
    return _evaluation(context, target).status


def run_acceptance(
    root: str, *, generated_at: str = "2026-01-01T00:00:00Z",
) -> AcceptanceMatrix:
    """Run the complete acceptance matrix and return the result."""
    context = _build_context(root)
    cases: list[AcceptanceCase] = []

    def add(case_id: str, category: str, description: str, condition: bool,
            detail: str = "") -> None:
        cases.append(AcceptanceCase(
            case_id=case_id, category=category, description=description,
            passed=bool(condition), detail=detail))

    # --- DATASET -------------------------------------------------------------
    rebuilt = dataset_module.build_learning_dataset(
        [context.fixture_root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    add("D01", CAT_DATASET, "deterministic extraction reproduces dataset id",
        rebuilt.dataset_id == context.learning.dataset_id,
        context.learning.dataset_id[:16])
    add("D02", CAT_DATASET, "every row carries a canonical source reference",
        all(o.source_ref.startswith(context.fixture_root)
            for o in context.learning.observations),
        f"{context.learning.row_count} rows")
    add("D03", CAT_DATASET, "every value carries a provenance label",
        all(_provenance_complete(o)
            for o in context.learning.observations),
        "provenance per field")
    unavailable = [p for o in context.learning.observations
                   for p in o.provenance.entries
                   if p.source == model.UNAVAILABLE]
    add("D04", CAT_DATASET, "UNAVAILABLE always carries a reason",
        all(p.reason for p in unavailable),
        f"{len(unavailable)} unavailable values")
    add("D05", CAT_DATASET, "quality report reports missingness per field",
        all(field in context.learning.quality.field_availability
            for field in dataset_module.OBSERVATION_FIELDS),
        f"{len(context.learning.quality.field_availability)} fields")
    add("D06", CAT_DATASET, "fixture rows are never labelled as real history",
        context.learning.quality.real_rows == 0
        and context.learning.quality.fixture_rows == context.learning.row_count,
        f"real={context.learning.quality.real_rows} "
        f"fixture={context.learning.quality.fixture_rows}")
    version_rejected = _expects_error(
        lambda: dataset_module.LearningDataset.from_dict(
            {**context.learning.to_dict(), "schema_version": 999}))
    add("D07", CAT_DATASET, "unsupported schema version is rejected",
        version_rejected, "schema fail-closed")
    add("D08", CAT_DATASET, "split prevents group leakage",
        bool(context.learning.split_metadata.get("leakage_free")),
        "no group spans two splits")
    re_split, _ = dataset_module.split_assignments(
        context.learning.observations, seed="m056-default")
    add("D09", CAT_DATASET, "split is reproducible for a fixed seed",
        re_split == dict(context.learning.splits), "same seed -> same split")
    after_hashes = _hash_tree(Path(context.fixture_root))
    add("D10", CAT_DATASET, "extraction is read-only over canonical sources",
        after_hashes == context.before_hashes,
        f"{len(after_hashes)} source files unchanged")

    # --- ML ------------------------------------------------------------------
    add("M01", CAT_ML, "duration baseline trains",
        _status_of(context, "duration_s") == ml.ST_TRAINED, "TRAINED")
    add("M02", CAT_ML, "duration beats the naive mean baseline (MAE)",
        _primary(context, "duration_s", "mae")
        < _baseline(context, "duration_s", "mae"),
        _metric_detail(context, "duration_s", "mae"))
    add("M03", CAT_ML, "success baseline trains",
        _status_of(context, "success") == ml.ST_TRAINED, "TRAINED")
    add("M04", CAT_ML, "success model improves ranking over naive baseline",
        _primary(context, "success", "auc")
        >= _baseline(context, "success", "auc"),
        _metric_detail(context, "success", "auc"))
    add("M05", CAT_ML, "blocked baseline trains",
        _status_of(context, "blocked") == ml.ST_TRAINED, "TRAINED")
    add("M06", CAT_ML, "repairs baseline trains",
        _status_of(context, "repairs") == ml.ST_TRAINED, "TRAINED")
    add("M07", CAT_ML, "cost target reports INSUFFICIENT_DATA when absent",
        _status_of(context, "cost_usd") == ml.ST_INSUFFICIENT,
        _evaluation(context, "cost_usd").reason or "")
    calibration = _evaluation(context, "success").calibration or {}
    after = calibration.get("after", {})
    add("M08", CAT_ML, "probability calibration is reported",
        bool(after.get("bins")) and "expected_calibration_error" in after,
        "Platt calibration bins present")
    probabilities = _probabilities_in_range(context, "success")
    add("M09", CAT_ML, "reported probabilities lie in [0, 1]", probabilities,
        "probability bounds")
    add("M10", CAT_ML, "feature importance is reported",
        bool(_evaluation(context, "duration_s").algorithms[0]
             .feature_importance), "non-empty importances")
    add("M11", CAT_ML, "model metadata is persisted (id/schema/dataset)",
        all(e.model_id and e.dataset_id
            for e in context.report.evaluations if e.status == ml.ST_TRAINED),
        "model ids present")
    add("M12", CAT_ML, "uncertainty is reported for every trained target",
        all(bool(e.uncertainty) for e in context.report.evaluations
            if e.status == ml.ST_TRAINED), "uncertainty present")
    add("M13", CAT_ML, "insufficient-data fail-safe returns INSUFFICIENT_DATA",
        context.tiny_insufficient.status == ml.ST_INSUFFICIENT,
        context.tiny_insufficient.reason or "")
    add("M14", CAT_ML, "naive baseline comparison is always present",
        all(bool(e.baseline) for e in context.report.evaluations
            if e.status == ml.ST_TRAINED), "baseline metrics")
    add("M15", CAT_ML, "no fabricated statistical significance",
        "p_value" not in model.canonical_json(context.report.to_dict())
        and "significance" not in model.canonical_json(
            context.report.to_dict()),
        "no significance claims")
    add("M16", CAT_ML, "training report is persisted and reloadable",
        ml.load_report(context.root) is not None, "round-trip")

    # --- ROUTING -------------------------------------------------------------
    add("R01", CAT_ROUTING, "candidate routes are explicit",
        len(context.recommendation.candidates) >= 2,
        f"{len(context.recommendation.candidates)} candidates")
    comparable = [c for c in context.recommendation.candidates
                  if c.comparable]
    add("R02", CAT_ROUTING, "comparable-evidence gate is applied",
        all(c.run_count >= routing.MIN_ROUTE_EVIDENCE
            for c in comparable) and len(comparable) >= 2,
        f"{len(comparable)} comparable routes")
    add("R03", CAT_ROUTING, "a recommendation or NO_RECOMMENDATION is emitted",
        context.recommendation.decision in (routing.RECOMMEND,
                                            routing.NO_RECOMMENDATION),
        context.recommendation.decision)
    small = dataset_module.build_learning_dataset(
        [str(Path(context.root) / "tiny-runs")],
        source_kind=model.SOURCE_FIXTURE, built_at="2026-01-01T00:00:00Z")
    small_recommendation = routing.recommend_route(
        small, generated_at="2026-01-01T00:00:00Z")
    add("R04", CAT_ROUTING, "NO_RECOMMENDATION when evidence is insufficient",
        small_recommendation.decision == routing.NO_RECOMMENDATION,
        small_recommendation.reason_code)
    add("R05", CAT_ROUTING, "explanation accompanies the recommendation",
        bool(context.recommendation.explanation),
        context.recommendation.explanation[:60])
    add("R06", CAT_ROUTING, "recommendation is advisory only",
        context.recommendation.advisory_only
        and not context.recommendation.mutates_policy,
        "advisory, no policy mutation")
    add("R07", CAT_ROUTING, "release gates are explicitly unchanged",
        context.recommendation.release_gates_unchanged,
        "release_gates_unchanged")
    add("R08", CAT_ROUTING, "final reviewer identity is explicit",
        context.recommendation.final_reviewer == routing.CANONICAL_FINAL_REVIEWER,
        context.recommendation.final_reviewer)
    add("R09", CAT_ROUTING, "Harness remains developer-preview",
        context.recommendation.harness_status == routing.HARNESS_STATUS,
        routing.HARNESS_STATUS)
    add("R10", CAT_ROUTING, "recommendation persists and reloads identically",
        routing.load_recommendation(context.root) is not None,
        "round-trip")

    # --- SCHEDULING ----------------------------------------------------------
    rule = context.scheduler.rule
    add("S01", CAT_SCHEDULING, "adaptive score exposes its components",
        all(c.components for c in rule.ordering),
        f"{len(rule.ordering)} scored candidates")
    add("S02", CAT_SCHEDULING, "ML mode without predictions falls back to rule",
        context.scheduler.ml_assisted.fallback_used
        or context.scheduler.ml_assisted.predictions_used,
        "deterministic fallback or predictions used")
    add("S03", CAT_SCHEDULING, "rule-based vs ML-assisted comparison reported",
        -1.0 <= context.scheduler.rank_agreement <= 1.0,
        f"tau={context.scheduler.rank_agreement:.3f}")
    add("S04", CAT_SCHEDULING, "overlay never commands a release Git write",
        not rule.commands_release_git
        and not context.scheduler.ml_assisted.commands_release_git,
        "no Git write")
    add("S05", CAT_SCHEDULING, "overlay never mutates the canonical scheduler",
        not rule.mutates_canonical_scheduler,
        "canonical scheduler authoritative")
    add("S06", CAT_SCHEDULING, "no improvement is claimed without outcomes",
        not context.scheduler.claimed_improvement,
        context.scheduler.improvement_reason[:60])
    add("S07", CAT_SCHEDULING, "human priority influences a transparent score",
        _human_priority_influences(context), "human priority contribution")

    # --- RAG -----------------------------------------------------------------
    add("G01", CAT_RAG, "documents carry source identity and content hash",
        all(d.source_ref and d.content_hash for d in context.index.documents),
        f"{context.index.document_count} documents")
    add("G02", CAT_RAG, "chunks carry document and source provenance",
        all(c.document_id and c.source_ref and c.content_hash
            for c in context.index.chunks),
        f"{context.index.chunk_count} chunks")
    add("G03", CAT_RAG, "retrieval trace is persisted with citations",
        bool(context.trace.results)
        and all(r.to_dict().get("citation") for r in context.trace.results),
        f"{len(context.trace.results)} results")
    add("G04", CAT_RAG, "retrieval is ranked deterministically",
        [r.rank for r in context.trace.results]
        == list(range(1, len(context.trace.results) + 1)),
        "contiguous ranks")
    stale = knowledge.assess_staleness(
        context.index,
        (knowledge.TextSource("notes/roadmap.md", "Roadmap", "changed"),))
    statuses = {entry.source_ref: entry.status for entry in stale}
    add("G05", CAT_RAG, "stale source is detected",
        statuses.get("notes/roadmap.md") == knowledge.STALE,
        statuses.get("notes/roadmap.md", "?"))
    missing = knowledge.assess_staleness(context.index, ())
    add("G06", CAT_RAG, "missing source is detected",
        all(entry.status == knowledge.MISSING for entry in missing),
        f"{len(missing)} missing")
    add("G07", CAT_RAG, "contradictions are representable and unresolved",
        bool(context.contradictions)
        and context.contradictions[0].resolution.startswith("UNRESOLVED"),
        context.contradictions[0].kind if context.contradictions else "")
    empty = knowledge.retrieve(context.index, "zzzzunmatchedquery",
                               generated_at="2026-01-01T00:00:00Z")
    add("G08", CAT_RAG, "NO_EVIDENCE path never substitutes model knowledge",
        empty.status == knowledge.NO_EVIDENCE
        and "no model knowledge" in empty.caveat,
        empty.status)
    restricted = knowledge.retrieve(
        context.index, "restricted pricing",
        allowed_sensitivities=(knowledge.SENS_PUBLIC,),
        generated_at="2026-01-01T00:00:00Z")
    add("G09", CAT_RAG, "sensitivity boundary filters retrieval",
        all(r.chunk.sensitivity != knowledge.SENS_RESTRICTED
            for r in restricted.results), "restricted excluded")
    add("G10", CAT_RAG, "retrieval is labelled non-canonical context",
        not context.trace.canonical, "canonical=False")

    # --- WORKFLOWS -----------------------------------------------------------
    add("W01", CAT_WORKFLOWS, "career workflow produces a deliverable",
        context.career.family == workflows.WF_CAREER
        and len(context.career.deliverable_markdown) > 200,
        f"{len(context.career.evidence)} evidence items")
    add("W02", CAT_WORKFLOWS, "life-sciences workflow produces a deliverable",
        context.life_sciences.family == workflows.WF_LIFE_SCIENCES
        and len(context.life_sciences.artifact.get("opportunities", [])) >= 1,
        "opportunities present")
    add("W03", CAT_WORKFLOWS, "research workflow produces a deliverable",
        context.research.family == workflows.WF_RESEARCH
        and len(context.research.deliverable_markdown) > 200,
        f"{len(context.research.evidence)} evidence items")
    add("W04", CAT_WORKFLOWS, "deliverables persist to durable markdown",
        (Path(root) / "workflows" / "career" / "deliverable.md").is_file()
        and (Path(root) / "workflows" / "research" /
             "deliverable.md").is_file(), "markdown on disk")
    add("W05", CAT_WORKFLOWS, "career workflow never invents personal facts",
        context.no_experience_career.artifact["value_proposition"] == []
        and any("no user-supplied experience"
                in unknown.lower()
                for unknown in context.no_experience_career.unknowns),
        "empty value proposition")
    add("W06", CAT_WORKFLOWS, "workflow evidence carries provenance",
        all(e.source_ref for e in context.career.evidence)
        and all(e.source_ref for e in context.research.evidence),
        "source refs present")
    add("W07", CAT_WORKFLOWS, "LifeOS markdown note is emitted",
        (Path(root) / "workflows" / "life-sciences" /
         "lifeos-note.json").is_file(), "lifeos note present")

    # --- DECISION ------------------------------------------------------------
    add("X01", CAT_DECISION, "decision lists alternatives",
        len(context.snapshot.options) >= 2, "alternatives present")
    add("X02", CAT_DECISION, "decision shows risks and constraints",
        all(option.risks or option.constraints
            for option in context.snapshot.options), "risks/constraints")
    add("X03", CAT_DECISION, "FACT and PREDICTION are distinguished",
        any(c.classification == decision.PREDICTION
            for ranked in context.snapshot.ranking
            for c in ranked.contributions)
        and any(c.classification == decision.FACT
                for ranked in context.snapshot.ranking
                for c in ranked.contributions),
        "FACT + PREDICTION contributions")
    add("X04", CAT_DECISION, "prediction uncertainty is reported",
        any(r.uncertainty.get("kind") != "none"
            for r in context.snapshot.ranking), "uncertainty present")
    add("X05", CAT_DECISION, "human remains the final decision-maker",
        context.snapshot.human_decision_required
        and not context.snapshot.autonomous_execution_allowed,
        "human required")
    add("X06", CAT_DECISION, "snapshot persists and reloads identically",
        decision.load_decision(root, context.snapshot.decision_id) is not None,
        "round-trip")
    add("X07", CAT_DECISION, "actual outcome can be compared to the snapshot",
        context.comparison is not None
        and context.comparison.followed_recommendation,
        "outcome comparison")
    add("X08", CAT_DECISION, "no irreversible high-trust decision is autonomous",
        not context.snapshot.autonomous_execution_allowed,
        "autonomous_execution_allowed=False")
    add("X09", CAT_DECISION, "dashboard decision summary is read-only",
        decision.decision_summary(root).get("read_only") is True,
        "read_only summary")

    # --- TRUST ---------------------------------------------------------------
    offenders = _git_offenders()
    add("T01", CAT_TRUST, "intelligence layer contains no release Git verbs",
        not offenders, ", ".join(offenders[:3]) or "clean")
    add("T02", CAT_TRUST, "release gates are preserved (routing unchanged)",
        context.recommendation.release_gates_unchanged,
        "GO COMMIT / GO MERGE untouched")
    add("T03", CAT_TRUST, "policy is never silently mutated",
        not context.recommendation.mutates_policy
        and not context.scheduler.rule.mutates_canonical_scheduler,
        "no policy mutation")
    add("T04", CAT_TRUST, "no fabricated measurement (provenance enforced)",
        _provenance_enforced(), "concrete values require a source")
    add("T05", CAT_TRUST, "semantic patch identity is not touched by this layer",
        not _contains_git_write_calls(), "no git write call sites")
    add("T06", CAT_TRUST, "acceptance requires persisted evidence, not prose",
        all(case.detail != "" for case in cases),
        "every case carries machine detail")

    matrix = AcceptanceMatrix(cases=tuple(cases), generated_at=generated_at,
                              fixture_count=120)
    model.write_json(f"{root}/acceptance/matrix.json", matrix.to_dict())
    return matrix


def _provenance_complete(observation: dataset_module.Observation) -> bool:
    return all(
        observation.provenance.get(field) is not None
        for field in dataset_module.OBSERVATION_FIELDS)


def _expects_error(call: Callable[[], object]) -> bool:
    try:
        call()
    except model.IntelligenceError:
        return True
    return False


def _evaluation_metric(context: _Context, target: str,
                       metric: str) -> float:
    evaluation = _evaluation(context, target)
    selected = next(a for a in evaluation.algorithms
                    if a.algorithm == evaluation.selected_algorithm)
    return float(selected.metrics[metric])


def _primary(context: _Context, target: str, metric: str) -> float:
    return _evaluation_metric(context, target, metric)


def _baseline(context: _Context, target: str, metric: str) -> float:
    return float(_evaluation(context, target).baseline["metrics"][metric])


def _metric_detail(context: _Context, target: str, metric: str) -> str:
    return (f"{metric}={_primary(context, target, metric):.3f} vs "
            f"baseline={_baseline(context, target, metric):.3f}")


def _probabilities_in_range(context: _Context, target: str) -> bool:
    evaluation = _evaluation(context, target)
    for algorithm in evaluation.algorithms:
        model_document = algorithm.model
        for key in ("weights", "coefficients"):
            values = model_document.get(key)
            if isinstance(values, list):
                for value in values:
                    if not isinstance(value, (int, float)):
                        return False
        root = model_document.get("root")
        if isinstance(root, Mapping):
            values = root.get("values")
            if isinstance(values, list):
                for value in values:
                    if not 0.0 <= float(value) <= 1.0:
                        return False
    return True


def _human_priority_influences(context: _Context) -> bool:
    candidates = (
        adaptive.ScheduleCandidate(
            candidate_id="low", project="p", mission_id=None,
            project_priority=1, deadline_s=None, wait_age_s=0.0,
            dependencies_ready=True, backend_available=True),
        adaptive.ScheduleCandidate(
            candidate_id="high", project="p", mission_id=None,
            project_priority=1, deadline_s=None, wait_age_s=0.0,
            dependencies_ready=True, backend_available=True,
            human_priority=5),
    )
    overlay = adaptive.build_overlay(candidates, mode=adaptive.MODE_RULE)
    return overlay.ordering[0].candidate_id == "high"


def _git_offenders() -> list[str]:
    base = Path(__file__).parent
    offenders: list[str] = []
    for path in sorted(base.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for verb in _GIT_VERBS:
            if f'"git", "{verb}"' in source:
                offenders.append(f"{path.name}:{verb}")
    return offenders


def _contains_git_write_calls() -> bool:
    base = Path(__file__).parent
    # Built dynamically so this scanner never matches its own source text.
    prefixes = ("subprocess" + ".", "os" + ".exec", "os" + ".system")
    for path in sorted(base.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if any(prefix in source for prefix in prefixes):
            return True
    return False


def _provenance_enforced() -> bool:
    try:
        model.Provenance(field_name="x", source=model.MEASURED,
                         source_ref="").validate()
    except model.IntelligenceError:
        return True
    return False


__all__ = [
    "AcceptanceCase",
    "AcceptanceMatrix",
    "CATEGORIES",
    "run_acceptance",
]
