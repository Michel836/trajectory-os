"""M063 — dogfood: real-history learning plus three practical workflows.

The dogfood deliberately separates three things that must never be conflated:

* **REAL history** — when ``.trajectory-pi/runs`` exists it is extracted into
  the learning dataset and the ML evaluation is trained on it. The actual
  sample size, missing metrics, limitations and what is (and is not)
  statistically supportable are reported verbatim;
* **SAMPLE workflow inputs** — the career, life-sciences and research
  workflows are run on safe, user-neutral sample inputs that are clearly
  marked ``FIXTURE``. No private or personal data is used;
* **FIXTURE-only mechanism proof** — the acceptance matrix (M063) proves the
  machinery with deterministic fixtures; it is never presented as a real ML
  score.

No outcome is invented: a decision snapshot is persisted, and the comparison
mechanism is demonstrated by the acceptance matrix rather than by fabricating
a post-decision result.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import (
    acceptance,
    adaptive,
    decision,
    knowledge,
    ml,
    model,
    routing,
    workflows,
)
from trajectory_os.intelligence import dataset as dataset_module

#: Default real-history directory (git-ignored; present only locally).
DEFAULT_REAL_RUNS = ".trajectory-pi/runs"
DEFAULT_ARTIFACT_ROOT = ".artifacts/m056-m063/dogfood"
DEFAULT_DOCS_DIR = "docs/missions/m056-m063"

#: Marker for the completed bundle.
MARKER = (
    "M056_M063_ADAPTIVE_INTELLIGENCE_PRACTICAL_WORKFLOWS_COMPLETE")

HISTORY_REAL = "REAL"
HISTORY_UNAVAILABLE = "REAL_HISTORY_UNAVAILABLE"


@dataclass(frozen=True)
class DogfoodEvidence:
    """The complete dogfood evidence document."""

    generated_at: str
    history_source: str
    history_note: str
    real_row_count: int
    fixture_row_count: int
    dataset_id: str | None
    ml_report: Mapping[str, Any] | None
    routing: Mapping[str, Any] | None
    scheduler: Mapping[str, Any] | None
    workflows: tuple[Mapping[str, Any], ...]
    decision: Mapping[str, Any] | None
    limitations: tuple[str, ...]
    acceptance: Mapping[str, Any]
    marker: str = MARKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "intelligence_version": model.INTELLIGENCE_VERSION,
            "kind": "m056_m063_dogfood_evidence",
            "generated_at": self.generated_at,
            "history_source": self.history_source,
            "history_note": self.history_note,
            "real_row_count": self.real_row_count,
            "fixture_row_count": self.fixture_row_count,
            "dataset_id": self.dataset_id,
            "ml_report": self.ml_report,
            "routing": self.routing,
            "scheduler": self.scheduler,
            "workflows": list(self.workflows),
            "decision": self.decision,
            "limitations": list(self.limitations),
            "acceptance": self.acceptance,
            "marker": self.marker,
        }

    def render_markdown(self) -> str:
        lines = [
            "# M056–M063 — Adaptive intelligence & practical workflows", "",
            f"- marker: `{self.marker}`",
            f"- generated: {self.generated_at}",
            f"- history source: {self.history_source}",
            f"- real rows: {self.real_row_count} / fixture rows: "
            f"{self.fixture_row_count}", "",
            "## ML evaluation (real history only)", "",
        ]
        if self.ml_report is None:
            lines.append("- no ML report (real history unavailable)")
        else:
            evaluations = self.ml_report.get("evaluations", [])
            lines += ["| target | status | selected | detail |",
                      "| --- | --- | --- | --- |"]
            for evaluation in evaluations if isinstance(evaluations, list) \
                    else []:
                lines.append(
                    f"| {evaluation.get('target')} | "
                    f"{evaluation.get('status')} | "
                    f"{evaluation.get('selected_algorithm') or '-'} | "
                    f"{evaluation.get('reason') or ''} |")
        lines += ["", "## Advisory routing", ""]
        if self.routing is None:
            lines.append("- unavailable")
        else:
            lines.append(f"- decision: {self.routing.get('decision')}")
            lines.append(f"- {self.routing.get('explanation')}")
        lines += ["", "## Scheduler comparison", ""]
        if self.scheduler is None:
            lines.append("- unavailable")
        else:
            lines.append(
                f"- rank agreement: {self.scheduler.get('rank_agreement')}")
            lines.append(
                f"- improvement claimed: "
                f"{self.scheduler.get('claimed_improvement')}")
        lines += ["", "## Practical workflow deliverables", ""]
        for workflow in self.workflows:
            lines.append(f"- {workflow.get('family')}: "
                         f"{workflow.get('title')}")
        lines += ["", "## Limitations", ""]
        lines.extend(f"- {item}" for item in self.limitations)
        lines += ["", "## Acceptance", "",
                  f"- cases passed: {self.acceptance.get('passed')}/"
                  f"{self.acceptance.get('total')}"]
        return "\n".join(lines) + "\n"


def _sample_career() -> workflows.CareerBrief:
    return workflows.CareerBrief(
        company="Sample Pharma (SAMPLE)",
        role="Data & AI Lead (SAMPLE)",
        brief_text=(
            "SAMPLE BRIEF. The team must modernise a reporting platform and "
            "own an AI strategy. Experience with regulatory reporting and "
            "stakeholder management is required. Data quality and rising "
            "costs are recurring problems."),
        experiences=(
            "SAMPLE: delivered a regulatory reporting platform",
            "SAMPLE: led a cross-functional data team"),
        constraints=("remote (SAMPLE)",),
        links=("https://example.invalid/portfolio (SAMPLE)",))


def _sample_monitoring() -> tuple[workflows.MonitoringRecord, ...]:
    return (
        workflows.MonitoringRecord(
            "sample-news-1", "2026-01-10",
            "SAMPLE: Phase 3 oncology programme reported a positive primary "
            "endpoint and an FDA submission timeline."),
        workflows.MonitoringRecord(
            "sample-news-2", "2026-02-12",
            "SAMPLE: a manufacturing supply shortage affected oncology "
            "capacity; demand continues to rise."),
        workflows.MonitoringRecord(
            "sample-news-3", "2026-03-05",
            "SAMPLE: regulatory approval granted; a real-world data and "
            "biomarker strategy was discussed."),
    )


def _sample_research() -> tuple[knowledge.SourceAdapter, ...]:
    return (
        knowledge.TextSource(
            "sample/research-a.md", "Sample study A",
            "SAMPLE: real-world data improved trial enrichment by 20 percent "
            "in oncology. Program cost was 2 million."),
        knowledge.TextSource(
            "sample/research-b.md", "Sample study B",
            "SAMPLE: real-world data improved trial enrichment by 35 percent "
            "in oncology. Program cost was 5 million."),
        knowledge.TextSource(
            "sample/research-c.md", "Sample strategy note",
            "SAMPLE: regulatory evidence requirements and data governance are "
            "the main constraints on scaling."),
    )


def run_dogfood(
    runtime_root: str,
    *,
    real_runs_dir: str | None = DEFAULT_REAL_RUNS,
    docs_dir: str | None = DEFAULT_DOCS_DIR,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
    run_acceptance_matrix: bool = True,
) -> DogfoodEvidence:
    """Run the M056–M063 dogfood and persist all evidence."""
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    Path(runtime_root).mkdir(parents=True, exist_ok=True)

    # --- real history (never fabricated) ------------------------------------
    real_path = Path(real_runs_dir) if real_runs_dir else None
    limitations: list[str] = []
    if real_path is not None and real_path.is_dir():
        learning = dataset_module.build_learning_dataset(
            [real_path], source_kind=model.SOURCE_REAL,
            built_at=stamp)
        history_source = HISTORY_REAL
        history_note = (
            f"extracted read-only from {real_path}; real rows are labelled "
            "REAL and no missing metric is imputed")
    else:
        learning = dataset_module.build_learning_dataset(
            [], source_kind=model.SOURCE_REAL, built_at=stamp)
        history_source = HISTORY_UNAVAILABLE
        history_note = (
            "the canonical .trajectory-pi/runs history is absent in this "
            "environment; no real ML score is claimed")
        limitations.append(
            "real canonical history was unavailable; ML targets report "
            "INSUFFICIENT_DATA rather than a fabricated score")
    dataset_module.persist_dataset(runtime_root, learning)

    # --- ML (only on real history rows) -------------------------------------
    ml_report: dict[str, Any] | None = None
    if learning.row_count > 0:
        report = ml.train_all(learning, generated_at=stamp)
        ml.persist_report(runtime_root, report)
        ml_report = report.to_dict()
        for evaluation in report.evaluations:
            if evaluation.status == ml.ST_INSUFFICIENT:
                limitations.append(
                    f"target {evaluation.target}: {evaluation.reason}")
                continue
            selected = next(
                a for a in evaluation.algorithms
                if a.algorithm == evaluation.selected_algorithm)
            limitations.append(
                f"target {evaluation.target}: "
                f"{evaluation.selected_algorithm} selected on validation; "
                "test metrics are point estimates on a small held-out "
                "split and are not statistically significant")
            _record_baseline_comparison(
                limitations, evaluation, selected)
    else:
        limitations.append(
            "no real rows exist, so every ML target is INSUFFICIENT_DATA")

    # --- missing metrics / data quality -------------------------------------
    quality = learning.quality
    for field_name in ("cost_usd", "prompt_tokens", "completion_tokens",
                       "total_tokens", "ttft_ms", "retries",
                       "protocol_failures", "ci_result"):
        if quality.field_availability.get(field_name, 0) == 0:
            limitations.append(
                f"metric {field_name!r} is unavailable for every observed "
                "run; it is reported as UNAVAILABLE with a reason")

    # --- advisory routing ----------------------------------------------------
    routing_document: dict[str, Any] | None = None
    if learning.row_count > 0:
        recommendation = routing.recommend_route(learning, generated_at=stamp)
        routing.persist_recommendation(runtime_root, recommendation)
        routing_document = recommendation.to_dict()

    # --- scheduler comparison (evidence-derived candidates) ------------------
    scheduler_document: dict[str, Any] | None = None
    if routing_document is not None:
        candidates = _scheduler_candidates(routing_document)
        comparison = adaptive.compare_schedulers(candidates,
                                                 generated_at=stamp)
        adaptive.persist_comparison(runtime_root, comparison)
        scheduler_document = comparison.to_dict()

    # --- practical workflows (sample inputs, clearly marked) ----------------
    knowledge_adapter = list(_sample_research())
    career = workflows.run_career_workflow(
        _sample_career(), root=runtime_root, workflow_id="dogfood-career",
        inputs_are_fixture=True, generated_at=stamp)
    life_sciences = workflows.run_life_sciences_workflow(
        _sample_monitoring(), root=runtime_root,
        workflow_id="dogfood-life-sciences", inputs_are_fixture=True,
        generated_at=stamp)
    research = workflows.run_research_workflow(
        "Should we invest in real-world data for oncology, and what does the "
        "evidence support?",
        root=runtime_root, adapters=knowledge_adapter,
        workflow_id="dogfood-research", inputs_are_fixture=True,
        generated_at=stamp)
    workflows_document = tuple(result.to_dict() for result in (
        career, life_sciences, research))

    # --- decision snapshot ---------------------------------------------------
    decision_document: dict[str, Any] | None = None
    if routing_document is not None:
        snapshot = _decision_from_routing(routing_document, stamp)
        decision.persist_decision(runtime_root, snapshot)
        decision_document = {
            "snapshot": snapshot.to_dict(),
            "outcome_comparison": None,
            "outcome_note": (
                "no post-decision outcome has occurred yet; the outcome "
                "comparison mechanism is proven by the acceptance matrix"),
        }

    # --- acceptance matrix ---------------------------------------------------
    if run_acceptance_matrix:
        acceptance_root = str(Path(runtime_root) / "acceptance-runtime")
        matrix = acceptance.run_acceptance(acceptance_root,
                                           generated_at=stamp)
        acceptance_document = matrix.to_dict()
        if matrix.failed:
            limitations.append(
                f"{matrix.failed} acceptance case(s) failed; see the matrix")
    else:
        acceptance_document = {"total": 0, "passed": 0, "failed": 0,
                               "cases": []}

    evidence = DogfoodEvidence(
        generated_at=stamp, history_source=history_source,
        history_note=history_note,
        real_row_count=quality.real_rows,
        fixture_row_count=quality.fixture_rows,
        dataset_id=learning.dataset_id if learning.row_count else None,
        ml_report=ml_report, routing=routing_document,
        scheduler=scheduler_document, workflows=workflows_document,
        decision=decision_document, limitations=tuple(limitations),
        acceptance=acceptance_document)
    if docs_dir:
        _write_docs(docs_dir, evidence)
    return evidence


def _record_baseline_comparison(
    limitations: list[str], evaluation: ml.TargetEvaluation,
    selected: ml.AlgorithmReport,
) -> None:
    """Record honestly when a model does not beat the naive baseline."""
    metric = ("log_loss" if evaluation.target in ("success", "blocked")
              else "mae")
    model_value = selected.metrics.get(metric)
    baseline_value = evaluation.baseline.get("metrics", {}).get(metric)
    if not isinstance(model_value, (int, float)) or not isinstance(
            baseline_value, (int, float)):
        return
    if float(model_value) >= float(baseline_value):
        limitations.append(
            f"target {evaluation.target}: the selected model does not beat "
            f"the naive baseline on the held-out split "
            f"({metric} {float(model_value):.4f} vs baseline "
            f"{float(baseline_value):.4f}); no superiority is claimed")


def _scheduler_candidates(
    routing_document: Mapping[str, Any],
) -> tuple[adaptive.ScheduleCandidate, ...]:
    candidates: list[adaptive.ScheduleCandidate] = []
    raw = routing_document.get("candidates", [])
    for index, candidate in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(candidate, Mapping) or not candidate.get("comparable"):
            continue
        success = candidate.get("success_rate")
        blocked = candidate.get("blocked_rate")
        duration = candidate.get("median_duration_s")
        label = str(candidate.get("model") or f"route-{index}")
        candidates.append(adaptive.ScheduleCandidate(
            candidate_id=f"route-{index}-{label}",
            project="portfolio", mission_id=None,
            project_priority=3,
            deadline_s=None,
            wait_age_s=float(index * 3_600),
            dependencies_ready=True, backend_available=True,
            predicted_success=(float(success)
                               if isinstance(success, (int, float)) else None),
            predicted_block_risk=(float(blocked)
                                  if isinstance(blocked, (int, float))
                                  else None),
            predicted_duration_s=(float(duration)
                                  if isinstance(duration, (int, float))
                                  else None)))
    return tuple(candidates)


def _decision_from_routing(
    routing_document: Mapping[str, Any], stamp: str,
) -> decision.DecisionSnapshot:
    criteria = (
        decision.Criterion("success_rate", 3.0, decision.MAXIMISE,
                           decision.PREDICTION,
                           "observed success rate (Wilson interval reported)"),
        decision.Criterion("blocked_rate", 2.0, decision.MINIMISE,
                           decision.PREDICTION,
                           "observed blocked rate"),
        decision.Criterion("median_duration_s", 1.0, decision.MINIMISE,
                           decision.FACT, "observed median duration"),
    )
    options: list[decision.DecisionOption] = []
    raw = routing_document.get("candidates", [])
    for index, candidate in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(candidate, Mapping) or not candidate.get("comparable"):
            continue
        success = candidate.get("success_rate")
        blocked = candidate.get("blocked_rate")
        duration = candidate.get("median_duration_s")
        interval = candidate.get("success_interval_95")
        confidence = None
        if isinstance(interval, list) and len(interval) == 2:
            confidence = max(0.0, min(
                1.0, 1.0 - (float(interval[1]) - float(interval[0]))))
        options.append(decision.DecisionOption(
            option_id=f"route-{index}",
            title=str(candidate.get("model") or f"route-{index}"),
            attributes={
                "median_duration_s": (float(duration)
                                      if isinstance(duration, (int, float))
                                      else 0.0)},
            predictions={
                "success_rate": (float(success)
                                 if isinstance(success, (int, float)) else 0.0),
                "blocked_rate": (float(blocked)
                                 if isinstance(blocked, (int, float)) else 0.0)},
            prediction_confidence=(
                {"success_rate": confidence}
                if confidence is not None else {}),
            risks=(f"blocked rate {blocked}",),
            rationale="route evidence from canonical history"))
    return decision.evaluate_decision(
        "Which implementation route should the (human) operator prefer?",
        criteria=criteria, options=options,
        irreversible=False, generated_at=stamp)


def _write_docs(docs_dir: str, evidence: DogfoodEvidence) -> None:
    directory = Path(docs_dir)
    directory.mkdir(parents=True, exist_ok=True)
    model.write_json(str(directory / "m056-m063-dogfood.json"),
                     evidence.to_dict())
    (directory / "m056-m063-dogfood.md").write_text(
        evidence.render_markdown(), encoding="utf-8")


__all__ = [
    "DEFAULT_ARTIFACT_ROOT",
    "DEFAULT_DOCS_DIR",
    "DEFAULT_REAL_RUNS",
    "DogfoodEvidence",
    "HISTORY_REAL",
    "HISTORY_UNAVAILABLE",
    "MARKER",
    "run_dogfood",
]
