"""M058 — advisory backend/model recommendation unit tests (no Git)."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.intelligence import dataset, fixtures, model, routing


def _learning(tmp_path: Path, count: int) -> dataset.LearningDataset:
    root = tmp_path / "runs"
    fixtures.generate_fixture_runs(root, count=count)
    return dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")


def test_recommendation_is_evidence_based(tmp_path: Path) -> None:
    learning = _learning(tmp_path, 120)
    recommendation = routing.recommend_route(
        learning, generated_at="2026-01-01T00:00:00Z")
    assert recommendation.decision == routing.RECOMMEND
    assert recommendation.recommended_route is not None
    comparable = [c for c in recommendation.candidates if c.comparable]
    assert len(comparable) >= 2
    assert recommendation.explanation


def test_no_recommendation_when_evidence_is_thin(tmp_path: Path) -> None:
    learning = _learning(tmp_path, 4)
    recommendation = routing.recommend_route(
        learning, generated_at="2026-01-01T00:00:00Z")
    assert recommendation.decision == routing.NO_RECOMMENDATION
    assert recommendation.reason_code == routing.R_INSUFFICIENT
    assert recommendation.recommended_route is None


def test_advisory_semantics_are_explicit(tmp_path: Path) -> None:
    learning = _learning(tmp_path, 120)
    recommendation = routing.recommend_route(learning)
    assert recommendation.advisory_only is True
    assert recommendation.mutates_policy is False
    assert recommendation.release_gates_unchanged is True


def test_final_reviewer_and_harness_status_are_explicit(tmp_path: Path) -> None:
    learning = _learning(tmp_path, 120)
    recommendation = routing.recommend_route(learning)
    assert recommendation.final_reviewer == routing.CANONICAL_FINAL_REVIEWER
    assert recommendation.harness_status == routing.HARNESS_STATUS


def test_candidate_evidence_carries_uncertainty(tmp_path: Path) -> None:
    learning = _learning(tmp_path, 120)
    recommendation = routing.recommend_route(learning)
    comparable = [c for c in recommendation.candidates if c.comparable]
    assert all(c.success_interval is not None for c in comparable)
    for candidate in comparable:
        low, high = candidate.success_interval or (0.0, 0.0)
        assert 0.0 <= low <= high <= 1.0


def test_recommendation_persistence_round_trip(tmp_path: Path) -> None:
    learning = _learning(tmp_path, 120)
    recommendation = routing.recommend_route(learning)
    root = str(tmp_path)
    routing.persist_recommendation(root, recommendation)
    reloaded = routing.load_recommendation(root)
    assert reloaded is not None
    assert reloaded.recommendation_id == recommendation.recommendation_id
    assert reloaded.to_dict() == recommendation.to_dict()
