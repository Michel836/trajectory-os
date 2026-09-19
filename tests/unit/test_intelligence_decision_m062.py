"""M062 — decision workspace / chief-of-staff unit tests (no Git)."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.intelligence import decision


def _snapshot(tmp_path: Path) -> decision.DecisionSnapshot:
    snapshot = decision.evaluate_decision(
        "Which mission next?",
        criteria=(
            decision.Criterion("value", 3.0, decision.MAXIMISE,
                               decision.PREDICTION),
            decision.Criterion("effort", 1.0, decision.MINIMISE,
                               decision.FACT),
        ),
        options=(
            decision.DecisionOption(
                option_id="a", title="Mission A",
                attributes={"effort": 6.0},
                predictions={"value": 0.8},
                prediction_confidence={"value": 0.7},
                risks=("slip",), constraints=("review",),
                dependencies=("gate",), expected_effort_hours=6.0),
            decision.DecisionOption(
                option_id="b", title="Mission B",
                attributes={"effort": 2.0},
                predictions={"value": 0.2},
                prediction_confidence={"value": 0.5}),
        ),
        generated_at="2026-01-01T00:00:00Z")
    decision.persist_decision(str(tmp_path), snapshot)
    return snapshot


def test_decision_lists_alternatives_risks_and_effort(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    assert len(snapshot.options) == 2
    first = snapshot.ranking[0]
    assert first.option.risks
    assert first.option.expected_effort_hours is not None
    assert first.option.dependencies


def test_fact_and_prediction_are_distinguished(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    classifications = {contribution.classification
                       for ranked in snapshot.ranking
                       for contribution in ranked.contributions}
    assert decision.FACT in classifications
    assert decision.PREDICTION in classifications


def test_uncertainty_is_reported(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    first = snapshot.ranking[0]
    assert first.uncertainty.get("kind") != "none"
    assert "average_confidence" in first.uncertainty


def test_human_remains_final_decision_maker(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    assert snapshot.human_decision_required is True
    assert snapshot.autonomous_execution_allowed is False


def test_snapshot_persists_and_reloads(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    reloaded = decision.load_decision(str(tmp_path), snapshot.decision_id)
    assert reloaded is not None
    assert reloaded.to_dict() == snapshot.to_dict()


def test_outcome_can_be_compared(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    decision.record_outcome(
        str(tmp_path), snapshot.decision_id, chosen_option_id="a",
        observed={"value": 0.7}, notes="observed",
        recorded_at="2026-01-02T00:00:00Z")
    comparison = decision.compare_outcome(str(tmp_path),
                                          snapshot.decision_id)
    assert comparison is not None
    assert comparison.followed_recommendation is True
    assert "value" in comparison.prediction_accuracy


def test_decision_summary_is_read_only(tmp_path: Path) -> None:
    _snapshot(tmp_path)
    summary = decision.decision_summary(str(tmp_path))
    assert summary["read_only"] is True
    assert summary["count"] == 1
    assert summary["decisions"][0]["autonomous_execution_allowed"] is False


def test_lifeos_note_is_human_readable(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    note = decision.to_lifeos_note(snapshot)
    assert "#trajectory-os" in note
    assert "Decision:" in note


def test_irreversible_flag_is_recorded() -> None:
    snapshot = decision.evaluate_decision(
        "Irreversible action?",
        criteria=(decision.Criterion("value", 1.0),),
        options=(decision.DecisionOption(
            option_id="a", title="Delete data", attributes={"value": 1.0},
            irreversible=True),),
        irreversible=True, generated_at="2026-01-01T00:00:00Z")
    assert snapshot.irreversible is True
    assert snapshot.autonomous_execution_allowed is False
