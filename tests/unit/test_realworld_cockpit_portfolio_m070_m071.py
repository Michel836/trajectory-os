"""M070/M071 — cockpit and portfolio unit tests."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.platform import projects as project_registry
from trajectory_os.realworld import cockpit, lifeos_ops, portfolio


def _evidence() -> portfolio.PortfolioEvidence:
    studies = (
        portfolio.CaseStudy(
            case_id="a", title="A", problem="p", inputs=("i",),
            approach=("a",), architecture=("x",), evidence=("e",),
            result=("r",), limitations=("l",), lessons=("s",)),
        portfolio.CaseStudy(
            case_id="b", title="B", problem="p", inputs=("i",),
            approach=("a",), architecture=("x",), evidence=("e",),
            result=("r",), limitations=("l",), lessons=("s",)),
        portfolio.CaseStudy(
            case_id="c", title="C", problem="p", inputs=("i",),
            approach=("a",), architecture=("x",), evidence=("e",),
            result=("r",), limitations=("l",), lessons=("s",)),
    )
    return portfolio.PortfolioEvidence(
        generated_at="2026-01-01T00:00:00Z", version="test",
        acceptance_passed=10, acceptance_total=10,
        dogfood_history_source="FIXTURE", dogfood_real_rows=0,
        workflow_families=("CAREER_INTELLIGENCE",), outcome_links=0,
        outcome_reconciled=0, unresolved_unknowns=0, corrections_required=0,
        prediction_error=None, trust_clean=True, trust_scanned_files=0,
        case_studies=studies, source_coverage=1, output_completeness=1.0,
        time_to_deliverable_s=1.0, limitations=("sample",))


def test_cockpit_is_readable_and_read_only(tmp_path: Path) -> None:
    project = project_registry.create_project(
        tmp_path, name="Cockpit project", workspace=str(tmp_path / "ws"),
        clock=lambda: "2026-01-01T00:00:00Z")
    view = cockpit.build_cockpit(
        str(tmp_path), (
            lifeos_ops.MissionState("m-gate", project.project_id,
                                    human_gate="GO COMMIT"),
        ), project_ids=(project.project_id,), workflow_id="cockpit",
        predictions=({"target": "success", "prediction": 0.7,
                      "uncertainty": "0.1", "source": "model-1"},),
        generated_at="2026-01-01T00:00:00Z")
    text = view.render_text()
    assert "PROJECTS" in text and "NEXT ACTIONS" in text
    assert "ATTENTION" in text and "INTELLIGENCE" in text
    assert view.read_only is True
    assert view.drilldown


def test_portfolio_documents_and_no_auto_publish(tmp_path: Path) -> None:
    evidence = _evidence()
    overview = portfolio.executive_overview(evidence)
    assert "What it is" in overview
    assert "Measured evidence" in overview
    assert portfolio.architecture_dot().startswith("digraph")
    written = portfolio.write_portfolio(str(tmp_path), evidence)
    assert "executive-overview.md" in written
    drafts = portfolio.build_in_public_drafts(evidence)
    assert drafts["auto_publish"] is False
    assert all(draft["published"] is False for draft in drafts["drafts"])
    pack = portfolio.evidence_pack(evidence)
    assert all(metric["source"] for metric in pack["metrics"])
