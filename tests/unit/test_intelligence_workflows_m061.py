"""M061 — practical workflow family unit tests (fixture inputs only)."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.intelligence import knowledge, workflows


def _career() -> workflows.CareerBrief:
    return workflows.CareerBrief(
        company="Acme Pharma", role="Head of Data & AI",
        brief_text=(
            "You must build a data platform and own AI strategy. Experience "
            "with regulatory compliance is required. Costs are rising."),
        experiences=("Built a regulatory reporting data platform",),
        constraints=("remote",),
        links=("https://example.invalid/portfolio",))


def test_career_workflow_produces_evidence_and_deliverable(
        tmp_path: Path) -> None:
    result = workflows.run_career_workflow(
        _career(), root=str(tmp_path), workflow_id="career",
        inputs_are_fixture=True, generated_at="2026-01-01T00:00:00Z")
    assert result.family == workflows.WF_CAREER
    assert result.evidence
    assert "Evidence-based value proposition" in result.deliverable_markdown
    assert result.next_actions
    assert (tmp_path / "workflows" / "career" / "deliverable.md").is_file()
    assert (tmp_path / "workflows" / "career" /
            "lifeos-note.json").is_file()


def test_career_never_invents_personal_facts(tmp_path: Path) -> None:
    brief = workflows.CareerBrief(
        company="Acme", role="Analyst",
        brief_text="Must have experience with reporting and analysis.")
    result = workflows.run_career_workflow(
        brief, root=str(tmp_path), workflow_id="career-empty",
        inputs_are_fixture=True, generated_at="2026-01-01T00:00:00Z")
    assert result.artifact["value_proposition"] == []
    assert any("no user-supplied experience" in unknown.lower()
               for unknown in result.unknowns)


def test_life_sciences_workflow_classifies_and_proposes(
        tmp_path: Path) -> None:
    records = (
        workflows.MonitoringRecord(
            "s1", "2026-01-10",
            "Phase 3 oncology trial met endpoint; FDA submission planned."),
        workflows.MonitoringRecord(
            "s2", "2026-02-12",
            "Manufacturing supply shortage; oncology demand rising."),
        workflows.MonitoringRecord(
            "s3", "2026-03-05",
            "FDA approval; real-world data biomarker strategy."),
    )
    result = workflows.run_life_sciences_workflow(
        records, root=str(tmp_path), workflow_id="ls",
        inputs_are_fixture=True, generated_at="2026-01-01T00:00:00Z")
    assert result.family == workflows.WF_LIFE_SCIENCES
    assert result.artifact["opportunities"]
    assert all(item["classification"] == workflows.HYPOTHESIS
               for item in result.artifact["opportunities"])
    assert any(evidence.kind == workflows.FACT for evidence in result.evidence)


def test_research_workflow_surfaces_contradictions_and_citations(
        tmp_path: Path) -> None:
    adapters = (
        knowledge.TextSource("a.md", "A", "Cost was 2 million in 2025."),
        knowledge.TextSource("b.md", "B", "Cost was 5 million in 2025."),
    )
    result = workflows.run_research_workflow(
        "Should we invest in the programme?",
        root=str(tmp_path), adapters=adapters, workflow_id="research",
        inputs_are_fixture=True, generated_at="2026-01-01T00:00:00Z")
    assert result.family == workflows.WF_RESEARCH
    assert result.artifact["contradictions"]
    assert result.evidence
    assert "Contradictions" in result.deliverable_markdown


def test_research_workflow_reports_no_evidence_honestly(
        tmp_path: Path) -> None:
    result = workflows.run_research_workflow(
        "What is the unknown answer?",
        root=str(tmp_path), adapters=(), workflow_id="research-empty",
        inputs_are_fixture=True, generated_at="2026-01-01T00:00:00Z")
    assert any("no source evidence" in unknown.lower()
               for unknown in result.unknowns)


def test_decompose_question_is_deterministic() -> None:
    first = workflows.decompose_question("Is X true and is Y better?")
    second = workflows.decompose_question("Is X true and is Y better?")
    assert first == second
    assert len(first) >= 3
