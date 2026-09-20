"""M065/M066 — career and life-sciences intelligence unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from trajectory_os.operator import cli as operator_cli
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


# --- M065 CLI evidence provenance wiring -------------------------------------


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _run_career_cli(*extra: str, root: Path, tmp_path: Path) -> None:
    code = operator_cli.main([
        "realworld", "career", "--root", str(root),
        "--company", "Acme Pharma", "--role", "Head of Data & AI",
        *extra])
    assert code == 0


def test_career_cli_parser_exposes_typed_evidence_flags() -> None:
    parser = operator_cli.build_parser()
    args = parser.parse_args([
        "realworld", "career", "--root", "r", "--company", "c",
        "--role", "x", "--company-evidence", "a",
        "--profile-evidence", "b", "--portfolio-artifact", "c",
        "--research-source", "d", "--evidence", "e"])
    assert args.company_evidence == ["a"]
    assert args.profile_evidence == ["b"]
    assert args.portfolio_artifact == ["c"]
    assert args.research_source == ["d"]
    assert args.evidence == ["e"]


def test_career_cli_profile_evidence_is_user_input_and_fits_role(
        tmp_path: Path) -> None:
    brief = _write(
        tmp_path / "brief.md",
        "Experience with regulatory reporting is required.\n")
    cv = _write(
        tmp_path / "cv.md",
        "Built a regulatory reporting data platform.\n")
    root = tmp_path / "root"
    _run_career_cli("--brief-file", brief, "--profile-evidence", cv,
                    root=root, tmp_path=tmp_path)

    analysis = career.load_career_intelligence(str(root))
    assert analysis is not None
    profile_claims = [claim for claim in analysis["claims"]
                      if claim["source_ref"].startswith("profile:")]
    assert profile_claims
    assert all(claim["source_kind"] == model.SRC_USER_INPUT
               for claim in profile_claims)
    # A supplied CV must never be relabelled as company evidence.
    assert all(not claim["source_ref"].startswith("company:")
               for claim in profile_claims)
    role_fit = (root / "realworld" / "career" / "career" /
                "role-fit.md").read_text(encoding="utf-8")
    assert "[MATCHED]" in role_fit
    assert "regulatory reporting" in role_fit


def test_career_cli_legacy_evidence_is_company_evidence_only(
        tmp_path: Path) -> None:
    brief = _write(tmp_path / "brief.md", "Data quality is a problem.\n")
    legacy = _write(
        tmp_path / "legacy-company.md",
        "Acme faces a data quality problem and rising costs.\n")
    cv = _write(
        tmp_path / "cv.md",
        "A profile block that must not become company evidence.\n")
    root = tmp_path / "root"
    _run_career_cli(
        "--brief-file", brief, "--evidence", legacy,
        "--profile-evidence", cv, root=root, tmp_path=tmp_path)

    analysis = career.load_career_intelligence(str(root))
    assert analysis is not None
    company_claims = [claim for claim in analysis["claims"]
                      if claim["source_ref"].startswith("company:")]
    assert company_claims
    assert all(claim["source_kind"] == model.SRC_SUPPLIED_EVIDENCE
               for claim in company_claims)
    assert all("legacy-company.md" in claim["source_ref"]
               for claim in company_claims)
    assert all("cv.md" not in claim["source_ref"]
               for claim in company_claims)
    profile_claims = [claim for claim in analysis["claims"]
                      if claim["source_ref"].startswith("profile:")]
    assert profile_claims and all("cv.md" in claim["source_ref"]
                                  for claim in profile_claims)


def test_career_cli_portfolio_and_research_are_typed(
        tmp_path: Path) -> None:
    brief = _write(tmp_path / "brief.md", "Data quality is a problem.\n")
    portfolio = _write(tmp_path / "portfolio.md",
                       "Led a cross-functional data team.\n")
    research = _write(
        tmp_path / "research.md",
        "Regulated markets reward governed data products.\n")
    root = tmp_path / "root"
    _run_career_cli(
        "--brief-file", brief, "--portfolio-artifact", portfolio,
        "--research-source", research, root=root, tmp_path=tmp_path)

    analysis = career.load_career_intelligence(str(root))
    assert analysis is not None
    portfolio_claims = [claim for claim in analysis["claims"]
                        if claim["source_ref"].startswith("portfolio:")]
    research_claims = [claim for claim in analysis["claims"]
                       if claim["source_ref"].startswith("research:")]
    assert portfolio_claims and research_claims
    assert all(claim["source_kind"] == model.SRC_USER_INPUT
               for claim in portfolio_claims)
    assert all(claim["source_kind"] == model.SRC_SUPPLIED_EVIDENCE
               for claim in research_claims)


# --- issue #250: long real-world role descriptions ---------------------------


def _long_role_description() -> str:
    """A realistic role description comfortably beyond the claim bound."""
    lines = [
        f"Responsibility {index}: lead the design and delivery of governed "
        "data products across regulated healthcare and life-sciences "
        "programmes, including requirements gathering, stakeholder "
        "alignment and measurable adoption outcomes."
        for index in range(30)
    ]
    lines.append(
        "Tail requirement: demonstrated experience with oncology "
        "real-world evidence pipelines.")
    return "\n".join(lines)


def test_career_long_role_description_is_bounded_and_valid(
        tmp_path: Path) -> None:
    role_description = _long_role_description()
    # The global claim-size invariant is intentionally unchanged.
    assert len(role_description) > model.MAX_CLAIM_STATEMENT_LEN

    result = career.run_career_intelligence(
        career.CareerInputs(
            company="Talan", role="Healthcare Data Lead",
            role_description=role_description, inputs_are_fixture=True),
        root=str(tmp_path), workflow_id="long-role",
        generated_at="2026-01-01T00:00:00Z")

    role_claims = [claim for claim in result.claims
                   if claim.source_ref.startswith("role-description:")]
    assert len(role_claims) > 1
    assert all(claim.label == model.EVIDENCE for claim in role_claims)
    assert all(claim.source_kind == model.SRC_USER_INPUT
               for claim in role_claims)
    assert all(len(claim.statement) <= model.MAX_CLAIM_STATEMENT_LEN
               for claim in result.claims)
    # Explicit, stable and gapless segment provenance.
    assert [claim.source_ref for claim in role_claims] == [
        f"role-description:long-role:segment:{index}"
        for index in range(len(role_claims))]
    # Every in-memory claim, and every claim as persisted and reloaded,
    # satisfies the unchanged global claim validator.
    assert all(claim.validate() for claim in result.claims)
    analysis = career.load_career_intelligence(
        str(tmp_path), workflow_id="long-role")
    assert analysis is not None
    assert all(model.Claim.from_dict(claim).validate()
               for claim in analysis["claims"])


def test_career_long_role_description_keeps_full_extraction(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    role_description = _long_role_description()
    seen: dict[str, str] = {}
    real_extract = career.workflows.extract_requirements

    def _capture(text: str) -> tuple[str, ...]:
        seen["text"] = text
        return real_extract(text)

    monkeypatch.setattr(career.workflows, "extract_requirements", _capture)
    result = career.run_career_intelligence(
        career.CareerInputs(
            company="Talan", role="Healthcare Data Lead",
            role_description=role_description, inputs_are_fixture=True),
        root=str(tmp_path), workflow_id="long-role",
        generated_at="2026-01-01T00:00:00Z")

    # Requirement extraction receives the full, unsegmented description.
    assert seen["text"] == role_description
    # A requirement that only appears well beyond the claim bound is still
    # extracted and rendered in the role-fit artifact.
    assert "oncology real-world evidence pipelines" in (
        result.artifacts["role-fit.md"])


def test_career_short_and_empty_role_descriptions_stay_compatible(
        tmp_path: Path) -> None:
    short = "Must have experience with regulatory reporting."
    short_result = career.run_career_intelligence(
        career.CareerInputs(
            company="Acme", role="Analyst", role_description=short,
            inputs_are_fixture=True),
        root=str(tmp_path), workflow_id="short-role",
        generated_at="2026-01-01T00:00:00Z")
    short_role = [claim for claim in short_result.claims
                  if claim.source_ref.startswith("role-description:")]
    assert [(claim.statement, claim.source_ref) for claim in short_role] == [
        (short, "role-description:short-role")]

    empty_result = career.run_career_intelligence(
        career.CareerInputs(
            company="Acme", role="Analyst", role_description="   ",
            inputs_are_fixture=True),
        root=str(tmp_path), workflow_id="empty-role",
        generated_at="2026-01-01T00:00:00Z")
    empty_role = [claim for claim in empty_result.claims
                  if claim.source_ref.startswith("role-description:")]
    assert [(claim.statement, claim.source_ref) for claim in empty_role] == [
        ("role: Analyst", "role-description:empty-role")]
