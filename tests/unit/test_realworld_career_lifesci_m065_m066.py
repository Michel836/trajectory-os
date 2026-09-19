"""M065/M066 — career and life-sciences intelligence unit tests."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.realworld import career, lifesci, model


def _career() -> career.CareerInputs:
    return career.CareerInputs(
        company="Acme Pharma", role="Head of Data & AI",
        role_description=(
            "You must build a data platform. Experience with regulatory "
            "compliance is required. Data quality is a problem."),
        company_evidence=(career.EvidenceSource(
            "news-1", "Acme faces a data quality problem and rising costs."),),
        profile_evidence=(career.EvidenceSource(
            "cv", "Built a regulatory reporting data platform"),),
        inputs_are_fixture=True)


def test_career_artifacts_and_claim_types(tmp_path: Path) -> None:
    result = career.run_career_intelligence(
        _career(), root=str(tmp_path), generated_at="2026-01-01T00:00:00Z")
    assert set(result.artifacts) >= {
        "company-analysis.md", "role-fit.md", "evidence-map.md", "gaps.md",
        "business-problem-hypotheses.md", "ai-data-opportunity.md",
        "value-proposition.md", "interview-brief.md"}
    labels = {claim.label for claim in result.claims}
    assert {model.FACT, model.EVIDENCE, model.INFERENCE,
            model.HYPOTHESIS} <= labels
    assert all(action.rationale and action.source and action.urgency
               for action in result.next_actions)
    assert (tmp_path / "realworld" / "career" / "career" /
            "lifeos-note.json").is_file()


def test_career_never_invents_personal_or_company_facts(
        tmp_path: Path) -> None:
    empty = career.CareerInputs(
        company="Acme", role="Analyst",
        role_description="Must have experience with reporting.",
        inputs_are_fixture=True)
    result = career.run_career_intelligence(
        empty, root=str(tmp_path), workflow_id="empty",
        generated_at="2026-01-01T00:00:00Z")
    assert all(claim.label != model.FACT for claim in result.claims)
    assert "not provided" in result.artifacts["value-proposition.md"]
    assert any("no company evidence" in unknown.description
               for unknown in result.unknowns)


def test_life_sciences_themes_entities_and_hypotheses(tmp_path: Path) -> None:
    records = (
        lifesci.MonitoringRecord(
            "n1", "2026-01-10",
            "Phase 3 oncology trial met endpoint; Novartis Pharma planned an "
            "FDA submission."),
        lifesci.MonitoringRecord(
            "n2", "2026-02-12",
            "Manufacturing supply shortage; oncology demand rising; Novartis "
            "Pharma faces a supply problem."),
    )
    result = lifesci.run_life_sciences_intelligence(
        records, root=str(tmp_path), inputs_are_fixture=True,
        generated_at="2026-01-01T00:00:00Z")
    assert "oncology" in result.artifacts["themes.md"]
    assert "Novartis" in result.artifacts["entities.md"]
    assert any(claim.label == model.HYPOTHESIS for claim in result.claims)
    assert "uncertainty" in result.artifacts["trends.md"]


def test_life_sciences_entity_detection_is_deterministic() -> None:
    entities = lifesci.detect_entities(
        "Novartis Pharma and Roche Ltd and Acme Therapeutics.")
    assert "Novartis" in entities
    assert "Roche" in entities
    assert entities == tuple(sorted(entities))
